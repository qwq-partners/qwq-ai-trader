"""Offline-only N4 scale measurements; this module deliberately has no product imports at import time."""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
import inspect
import json
import math
from time import perf_counter
import tracemalloc


SIZES = (0, 100, 1000, 5000)
KINDS = ("capture", "owner", "sweep")
TRIALS = 3
CONNECTION_GATE_MS = 50.0


def classify_measurement(*, snapshot_stable, wall_max_ms):
    """Classify a measurement without treating unavailable capture as fast success."""
    capture_supported = snapshot_stable is True
    wall_is_measured = (type(wall_max_ms) in (int, float)
                        and math.isfinite(wall_max_ms) and wall_max_ms >= 0)
    return {
        "capture_supported": capture_supported,
        "connection_eligible": capture_supported and wall_is_measured
        and wall_max_ms <= CONNECTION_GATE_MS,
    }


def _synthetic_rows(runtime, size):
    """Grow private test RAM from real lifecycle rows, never from public ``owner.state`` copies."""
    from test_execution_runtime import NOW
    asyncio.get_running_loop()  # makes accidental use outside the async harness obvious
    # The caller has already made the template before calling this helper.
    template_intent = deepcopy(runtime.owner._state["intents"]["scale-template"])
    template_attempt = deepcopy(runtime.owner._state["attempts"]["scale-template"])
    state = runtime.owner._state
    state["intents"] = {}
    state["attempts"] = {}
    state["outbox"] = {}
    for index in range(size):
        intent_id = f"scale-intent-{index}"
        attempt_id = f"scale-attempt-{index}"
        symbol = f"scale-symbol-{index}"
        intent = deepcopy(template_intent)
        attempt = deepcopy(template_attempt)
        intent.update(symbol=symbol, attempt_ids=[attempt_id])
        attempt.update(attempt_id=attempt_id, intent_id=intent_id, symbol=symbol)
        attempt["order_ref"] = dict(attempt["order_ref"], order_no=attempt_id)
        # This is the exact current protection-decision row shape from runtime quote admission;
        # its links point at the independently copied lifecycle intent above.
        outbox = {
            "kind": "protection_decision", "symbol": symbol, "intent_id": intent_id,
            "decision": ["sell_all", attempt["quantity"], "synthetic_scale"],
            "status": "pending", "observed_at": NOW.isoformat(),
            "provenance": {"market_as_of": NOW.isoformat(), "source": "synthetic_scale",
                           "source_event_id": attempt_id, "received_at": NOW.isoformat()},
        }
        state["intents"][intent_id] = intent
        state["attempts"][attempt_id] = attempt
        state["outbox"][f"scale-command-{index}"] = outbox

    assert len(state["intents"]) == len(state["attempts"]) == len(state["outbox"]) == size
    assert len({id(row) for row in state["intents"].values()}) == size
    assert len({id(row) for row in state["attempts"].values()}) == size
    assert len({id(row) for row in state["outbox"].values()}) == size
    assert all(row["intent_id"] in state["intents"] for row in state["attempts"].values())
    assert all(state["intents"][row["intent_id"]]["attempt_ids"] == [row["attempt_id"]]
               for row in state["attempts"].values())
    assert all(row["intent_id"] in state["intents"] for row in state["outbox"].values())
    # All synthetic attempts are explicitly open; no terminal reservation is fabricated.
    assert all(row["state"] not in ("final_filled", "final_cancelled", "final_rejected")
               for row in state["attempts"].values())


def _producer_baseline(producer):
    return {name: deepcopy(getattr(producer, name)) for name in (
        "_episodes", "_last_quote", "_sources", "_restart_originals", "_restart_retries",
        "_restart_checked", "_recovery_required", "_pending_reasons", "_stats",
    )}


def _restore_case(runtime, producer, state_baseline, producer_baseline):
    runtime.owner._state = deepcopy(state_baseline)
    for name, value in producer_baseline.items():
        setattr(producer, name, deepcopy(value))


async def _timed_once(operation):
    """Measure synchronous loop blocking separately from elapsed wall time."""
    loop = asyncio.get_running_loop()
    observed_stall = []
    queued_at = loop.time()
    loop.call_soon(lambda: observed_stall.append((loop.time() - queued_at) * 1000))
    started = perf_counter()
    value = operation()
    if inspect.isawaitable(value):
        value = await value
    elapsed = (perf_counter() - started) * 1000
    await asyncio.sleep(0)
    return value, elapsed, max(observed_stall, default=0.0)


async def _measure(operation, reset):
    wall, stalls = [], []
    for _ in range(TRIALS):
        reset()  # Copies/reset are setup, explicitly outside the latency timer.
        _, elapsed, stall = await _timed_once(operation)
        wall.append(elapsed)
        stalls.append(stall)

    reset()  # Tracing is intentionally a separate operation, never added to wall timing.
    tracemalloc.start()
    try:
        value = operation()
        if inspect.isawaitable(value):
            await value
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return max(wall), max(stalls), peak


@contextmanager
def _forbid_sweep_writers(runtime, producer):
    """Fail loudly if the benchmark crosses any owner/store/gateway recovery writer."""
    originals = []

    def blocked(*_args, **_kwargs):
        raise AssertionError("scale sweep invoked a forbidden writer")

    async def blocked_async(*_args, **_kwargs):
        blocked()

    class ForbiddenGateway:
        def __getattr__(self, _name):
            blocked()

    candidates = (
        (runtime, "release_protection_pending", blocked_async),
        (runtime, "resume_protection_admission", blocked_async),
        (producer, "_submit", blocked_async),
        (runtime.owner, "mutate", blocked_async),
        (runtime.lifecycle, "prepare", blocked_async),
        (runtime.lifecycle, "claim", blocked_async),
        (runtime.lifecycle, "record_result", blocked_async),
    )
    store = getattr(runtime.owner, "store", None)
    if store is not None:
        candidates += ((store, "commit", blocked_async),)
    for target, name, replacement in candidates:
        if hasattr(target, name):
            originals.append((target, name, getattr(target, name)))
            setattr(target, name, replacement)
    gateway = runtime.gateway
    runtime.gateway = ForbiddenGateway()
    try:
        yield
    finally:
        runtime.gateway = gateway
        for target, name, original in reversed(originals):
            setattr(target, name, original)


async def measure_case(size, kind, *, tmp_path, sweep_phase=None):
    """Return a non-identifying dict for one test-owned, offline scale measurement."""
    if type(size) is not int or size not in SIZES:
        raise ValueError("unsupported_scale_size")
    if kind not in KINDS:
        raise ValueError("unsupported_scale_kind")
    if sweep_phase not in (None, "cold", "warm"):
        raise ValueError("unsupported_sweep_phase")
    if kind == "sweep" and sweep_phase is None:
        raise ValueError("sweep_phase_required")
    if kind != "sweep" and sweep_phase is not None:
        raise ValueError("sweep_phase_only_applies_to_sweep")

    from test_execution_runtime import NOW, opened, setup
    from src.execution.safety.protection_producer import ProtectionProducer
    from src.execution.safety.recovery_capture import capture_recovery_snapshot
    from src.execution.safety.recovery_diagnostics import build_recovery_diagnostic

    _, _, store, runtime = await setup(tmp_path)
    try:
        # One real lifecycle call supplies the row shape; subsequent records are independent copies.
        await opened(runtime, "scale-template", side="sell")
        _synthetic_rows(runtime, size)
        producer = ProtectionProducer(runtime, clock=lambda: NOW, indicator_source=lambda _symbol: {})
        runtime._protection_producer = producer
        if sweep_phase == "warm":
            # The cold-start index construction is intentionally excluded from warm latency.
            producer._restart_cooldowns(NOW)
        state_baseline = deepcopy(runtime.owner._state)
        producer_baseline = _producer_baseline(producer)

        def reset():
            _restore_case(runtime, producer, state_baseline, producer_baseline)

        def captured_report():
            before = deepcopy(runtime.owner._state)
            report = build_recovery_diagnostic(capture_recovery_snapshot(runtime, captured_at=NOW))
            assert runtime.owner._state == before
            assert report["automatic_action_allowed"] is False
            return report

        if kind == "capture":
            operation = captured_report
            guard = None
        elif kind == "owner":
            def operation():
                before = deepcopy(runtime.owner._state)
                copied = runtime.owner.state
                assert copied == before and copied is not runtime.owner._state
                assert runtime.owner._state == before
                return copied
            guard = None
        else:
            async def operation():
                before = deepcopy(runtime.owner._state)
                await producer._sweep(NOW)
                assert runtime.owner._state == before
            guard = _forbid_sweep_writers(runtime, producer)

        if guard is None:
            wall_max_ms, loop_stall_ms, peak_bytes = await _measure(operation, reset)
        else:
            with guard:
                wall_max_ms, loop_stall_ms, peak_bytes = await _measure(operation, reset)

        reset()
        report = captured_report()  # Schema/status output is outside owner/sweep latency timing.
        report_json = json.dumps(report, sort_keys=True, separators=(",", ":"))
        finding_codes = sorted(row["code"] for row in report["findings"])
        classification = classify_measurement(snapshot_stable=report["snapshot_stable"],
                                              wall_max_ms=wall_max_ms)
        return {
            "schema_version": 1, "kind": kind, "sweep_phase": sweep_phase,
            "size": size, "synthetic": True, "status": "measured",
            "wall_max_ms": wall_max_ms, "loop_stall_ms": loop_stall_ms,
            "peak_bytes": peak_bytes, "report_bytes": len(report_json),
            "snapshot_stable": report["snapshot_stable"],
            "counts_complete": report["counts_complete"], "finding_codes": finding_codes,
            "record_counts": {"intents": size, "attempts": size, "outbox": size},
            "rows_linked": True, "unique_rows": True, "terminal_reservations": 0,
            "synthetic_finality": "none",
            **classification,
        }
    finally:
        await store.close()
