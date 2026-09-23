"""Adversarial N3 boundary acceptance over the real in-memory runtime fixtures."""
import asyncio
import importlib.util
import json

import pytest


def _diagnostics():
    """Keep the proposed modules out of collection while this file is RED."""
    capture_spec = importlib.util.find_spec("src.execution.safety.recovery_capture")
    diagnostic_spec = importlib.util.find_spec("src.execution.safety.recovery_diagnostics")
    assert capture_spec is not None, "recovery_capture module is not implemented"
    assert diagnostic_spec is not None, "recovery_diagnostics module is not implemented"
    from src.execution.safety import recovery_capture, recovery_diagnostics
    assert hasattr(recovery_capture, "capture_recovery_snapshot")
    assert hasattr(recovery_diagnostics, "build_recovery_diagnostic")
    return recovery_capture, recovery_diagnostics


def _finding_codes(report):
    return {finding["code"] for finding in report["findings"]}


@pytest.mark.parametrize("mutation", [
    "restart_originals", "restart_checked", "restart_retries", "same_count_task_replacement",
])
def test_two_sample_capture_marks_private_producer_ram_mutation_volatile(
        tmp_path, monkeypatch, mutation):
    """A fixed owner version and unchanged task count cannot authenticate RAM stability."""
    capture, diagnostics = _diagnostics()
    from src.execution.safety.protection_producer import ProtectionProducer
    from test_execution_runtime import NOW, setup

    async def scenario():
        _, _, store, runtime = await setup(tmp_path)
        first_task = replacement_task = None
        try:
            producer = ProtectionProducer(runtime, clock=lambda: NOW,
                                          indicator_source=lambda _symbol: {})
            runtime._protection_producer = producer
            producer._restart_originals = {"scope": []}
            producer._restart_checked = set()
            producer._restart_retries = {}
            if mutation == "same_count_task_replacement":
                first_task = asyncio.create_task(asyncio.sleep(0))
                replacement_task = asyncio.create_task(asyncio.sleep(0))
                await asyncio.gather(first_task, replacement_task)
                producer._tasks = {first_task}

            owner_version = runtime.owner.version
            original_read = capture._read_sample
            calls = 0

            def mutate_between_samples(*args, **kwargs):
                nonlocal calls
                sample = original_read(*args, **kwargs)
                calls += 1
                if calls == 1:
                    if mutation == "restart_originals":
                        producer._restart_originals = {"scope": ["changed"]}
                    elif mutation == "restart_checked":
                        producer._restart_checked = {"changed"}
                    elif mutation == "restart_retries":
                        producer._restart_retries = {"scope": {"reason": "changed"}}
                    else:
                        # Same cardinality is intentionally insufficient; the task references differ.
                        assert first_task is not replacement_task
                        producer._tasks = {replacement_task}
                return sample

            monkeypatch.setattr(capture, "_read_sample", mutate_between_samples)
            snapshot = capture.capture_recovery_snapshot(runtime, captured_at=NOW)
            report = diagnostics.build_recovery_diagnostic(snapshot)

            assert calls == 2
            assert runtime.owner.version == owner_version
            assert report["snapshot_stable"] is False
            assert "snapshot_volatile" in _finding_codes(report)
        finally:
            await store.close()

    asyncio.run(scenario())


def test_acknowledged_cancel_child_is_unconfirmed_without_exposing_identifiers(tmp_path):
    """A cancel ACK is nonterminal evidence, even through the actual lifecycle coordinator."""
    capture, diagnostics = _diagnostics()
    from src.execution.safety.lifecycle import CommandKind, CommandResult, CommandStatus, OrderRef
    from test_execution_runtime import NOW, setup

    async def scenario():
        _, _, store, runtime = await setup(tmp_path)
        try:
            parent_ref = OrderRef("scope", "KR", "2026-09-18", "KRX", "private-parent-order")
            await runtime.lifecycle.prepare("private-intent", "private-parent", 3, "005930", "sell")
            assert await runtime.lifecycle.claim("private-parent", "sender")
            assert await runtime.lifecycle.record_result("private-parent", "sender", CommandResult(
                CommandStatus.ACKNOWLEDGED, "private-parent", parent_ref))
            await runtime.lifecycle.prepare("private-intent", "private-cancel", 3, "005930", "sell",
                                            command=CommandKind.CANCEL,
                                            parent_attempt_id="private-parent", order_ref=parent_ref)
            assert await runtime.lifecycle.claim("private-cancel", "sender")
            cancel_ref = OrderRef("scope", "KR", "2026-09-18", "KRX", "private-cancel-order",
                                  parent_order_no="private-parent-order")
            assert await runtime.lifecycle.record_result("private-cancel", "sender", CommandResult(
                CommandStatus.ACKNOWLEDGED, "private-cancel", cancel_ref))
            assert runtime.owner.state["attempts"]["private-cancel"]["state"] == "reconciling"

            report = diagnostics.build_recovery_diagnostic(
                capture.capture_recovery_snapshot(runtime, captured_at=NOW))
            assert "cancel_unconfirmed" in _finding_codes(report)
            assert "private-" not in json.dumps(report, sort_keys=True)
        finally:
            await store.close()

    asyncio.run(scenario())


def test_in_memory_bad_cancel_parent_link_is_reported_without_identifier_output(tmp_path):
    """Synthetic in-memory corruption: supported lifecycle row, then only its parent link is broken."""
    capture, diagnostics = _diagnostics()
    from src.execution.safety.lifecycle import CommandKind, CommandResult, CommandStatus, OrderRef
    from test_execution_runtime import NOW, setup

    async def scenario():
        _, _, store, runtime = await setup(tmp_path)
        try:
            parent_ref = OrderRef("scope", "KR", "2026-09-18", "KRX", "private-parent-order")
            await runtime.lifecycle.prepare("private-intent", "private-parent", 3, "005930", "sell")
            assert await runtime.lifecycle.claim("private-parent", "sender")
            assert await runtime.lifecycle.record_result("private-parent", "sender", CommandResult(
                CommandStatus.ACKNOWLEDGED, "private-parent", parent_ref))
            await runtime.lifecycle.prepare("private-intent", "private-cancel", 3, "005930", "sell",
                                            command=CommandKind.CANCEL,
                                            parent_attempt_id="private-parent", order_ref=parent_ref)

            def synthetic_bad_parent_link(state):
                state["attempts"]["private-cancel"]["parent_attempt_id"] = "private-missing-parent"
                return state

            await runtime.owner.mutate("synthetic-bad-parent-link", synthetic_bad_parent_link)
            report = diagnostics.build_recovery_diagnostic(
                capture.capture_recovery_snapshot(runtime, captured_at=NOW))
            assert "attempt_link_inconsistent" in _finding_codes(report)
            assert "private-" not in json.dumps(report, sort_keys=True)
        finally:
            await store.close()

    asyncio.run(scenario())


def test_cyclic_custom_attempt_payload_is_refused_without_object_hooks(tmp_path):
    """Synthetic in-memory corruption must not run repr/str/bool/eq while refusing it."""
    capture, diagnostics = _diagnostics()
    from test_execution_runtime import NOW, setup

    class HostilePayload:
        def __repr__(self):
            raise AssertionError("capture called repr")

        def __str__(self):
            raise AssertionError("capture called str")

        def __bool__(self):
            raise AssertionError("capture called bool")

        def __eq__(self, other):
            raise AssertionError("capture called eq")

    async def scenario():
        _, _, store, runtime = await setup(tmp_path)
        try:
            # Direct RAM mutation is intentional: the durable owner rejects this malformed payload.
            cyclic = {"payload": HostilePayload()}
            cyclic["self"] = cyclic
            runtime.owner.state["attempts"]["private-cyclic-attempt"] = cyclic
            report = diagnostics.build_recovery_diagnostic(
                capture.capture_recovery_snapshot(runtime, captured_at=NOW))
            assert report["snapshot_stable"] is None
            assert "snapshot_unavailable" in _finding_codes(report)
            assert "private-" not in json.dumps(report, sort_keys=True)
        finally:
            await store.close()

    asyncio.run(scenario())
