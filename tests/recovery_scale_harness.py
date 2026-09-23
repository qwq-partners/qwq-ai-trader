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
FORBIDDEN_SWEEP_SEAMS = {
    'owner.mutate', 'owner.store.commit', 'lifecycle.prepare', 'lifecycle.claim',
    'lifecycle.record_result', 'runtime.release_protection_pending',
    'runtime.resume_protection_admission', 'producer._submit', 'gateway.any',
}


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
    """Grow private RAM from real terminal/live lifecycle rows, never public copies."""
    from test_execution_runtime import NOW
    asyncio.get_running_loop()  # makes accidental use outside the async harness obvious
    # The caller made these through real lifecycle transitions before this test-only copy step.
    terminal_intent = deepcopy(runtime.owner._state["intents"]["scale-terminal-template"])
    terminal_attempt = deepcopy(runtime.owner._state["attempts"]["scale-terminal-template"])
    live_intent = deepcopy(runtime.owner._state["intents"]["scale-live-template"])
    live_attempt = deepcopy(runtime.owner._state["attempts"]["scale-live-template"])
    live_count = min(size, 2)
    state = runtime.owner._state
    state["intents"] = {}
    state["attempts"] = {}
    state["outbox"] = {}
    for index in range(size):
        intent_id = f"scale-intent-{index}"
        attempt_id = f"scale-attempt-{index}"
        symbol = f"scale-symbol-{index % 5}"
        is_live = index < live_count
        intent = deepcopy(live_intent if is_live else terminal_intent)
        attempt = deepcopy(live_attempt if is_live else terminal_attempt)
        intent.update(symbol=symbol, attempt_ids=[attempt_id])
        attempt.update(attempt_id=attempt_id, intent_id=intent_id, symbol=symbol)
        if attempt["order_ref"] is not None:
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
    terminal = list(state["attempts"].values())[live_count:]
    assert all(row["state"] == "final_rejected" and row["command_status"] == "rejected"
               and row["reserved_quantity"] == 0 and row["reserved_cash"] == "0" for row in terminal)
    return {"total_events": size, "live_events": live_count,
            "terminal_events": size - live_count, "symbol_count": min(size, 5)}


def _producer_baseline(producer):
    return {name: deepcopy(getattr(producer, name)) for name in (
        "_episodes", "_last_quote", "_sources", "_restart_originals", "_restart_retries",
        "_restart_checked", "_recovery_required", "_pending_reasons", "_stats",
    )}


def _restore_case(runtime, producer, state_baseline, producer_baseline):
    runtime.owner._state = deepcopy(state_baseline)
    for name, value in producer_baseline.items():
        setattr(producer, name, deepcopy(value))


def _phase(name):
    print(json.dumps({"scale_phase": name}, sort_keys=True, allow_nan=False), flush=True)


def _validate_unchanged(owner, baseline):
    assert owner._state == baseline


async def _timed_once(operation):
    """Report one scheduled callback's delay to first yield, not continuous loop stall."""
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


async def _measure(operation, reset, validate):
    wall, first_yield_delays = [], []
    for _ in range(TRIALS):
        _phase("reset")
        reset()  # Copies/reset are setup, explicitly outside the operation timer.
        _phase("normal")
        value, elapsed, first_yield_delay = await _timed_once(operation)
        _phase("validation")
        validate(value)  # Validation is deliberately outside both normal and tracing windows.
        wall.append(elapsed)
        first_yield_delays.append(first_yield_delay)

    _phase("reset")
    reset()
    _phase("tracing")
    tracemalloc.start()
    try:
        value = operation()
        if inspect.isawaitable(value):
            value = await value
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    _phase("validation")
    validate(value)
    return max(wall), max(first_yield_delays), peak


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

    owner = getattr(runtime, 'owner', None)
    lifecycle = getattr(runtime, 'lifecycle', None)
    store = getattr(owner, 'store', None)
    candidates = (
        ('runtime.release_protection_pending', runtime, 'release_protection_pending'),
        ('runtime.resume_protection_admission', runtime, 'resume_protection_admission'),
        ('producer._submit', producer, '_submit'),
        ('owner.mutate', owner, 'mutate'),
        ('lifecycle.prepare', lifecycle, 'prepare'),
        ('lifecycle.claim', lifecycle, 'claim'),
        ('lifecycle.record_result', lifecycle, 'record_result'),
        ('owner.store.commit', store, 'commit'),
    )
    missing = {label for label, target, name in candidates if target is None or not hasattr(target, name)}
    if not hasattr(runtime, 'gateway'):
        missing.add('gateway.any')
    if missing:
        raise AssertionError('missing forbidden sweep seams: ' + ','.join(sorted(missing)))

    installed, gateway_installed = set(), False
    gateway = runtime.gateway
    try:
        for label, target, name in candidates:
            originals.append((target, name, getattr(target, name)))
            setattr(target, name, blocked_async)
            installed.add(label)
        runtime.gateway = ForbiddenGateway()  # Sweep must not touch gateway; capture never installs this guard.
        gateway_installed = True
        installed.add('gateway.any')
        assert installed == FORBIDDEN_SWEEP_SEAMS
        yield installed
    finally:
        if gateway_installed:
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
    from src.execution.safety.lifecycle import CommandResult, CommandStatus
    from src.execution.safety.protection_producer import ProtectionProducer
    from src.execution.safety.recovery_capture import capture_recovery_snapshot
    from src.execution.safety.recovery_diagnostics import build_recovery_diagnostic

    _, _, store, runtime = await setup(tmp_path)
    try:
        _phase("setup")
        # The primary cohort is genuine test-fixture lifecycle rejection history, not padded open rows.
        await runtime.lifecycle.prepare("scale-terminal-template", "scale-terminal-template", 100,
                                        "005930", "sell")
        assert await runtime.lifecycle.claim("scale-terminal-template", "scale-sender")
        assert await runtime.lifecycle.record_result("scale-terminal-template", "scale-sender",
                                                     CommandResult(CommandStatus.REJECTED,
                                                                   "scale-terminal-template"))
        await opened(runtime, "scale-live-template", side="sell")
        cohort = _synthetic_rows(runtime, size)
        producer = ProtectionProducer(runtime, clock=lambda: NOW, indicator_source=lambda _symbol: {})
        runtime._protection_producer = producer
        _phase("warm")
        if sweep_phase == "warm":
            # The cold-start index construction is intentionally excluded from warm latency.
            producer._restart_cooldowns(NOW)
        state_baseline = deepcopy(runtime.owner._state)
        producer_baseline = _producer_baseline(producer)

        def reset():
            _restore_case(runtime, producer, state_baseline, producer_baseline)

        def capture_json():
            # The capture operation is exactly capture + builder + finite JSON serialization.
            return json.dumps(build_recovery_diagnostic(
                capture_recovery_snapshot(runtime, captured_at=NOW)), sort_keys=True,
                separators=(",", ":"), allow_nan=False)

        if kind == "capture":
            operation = capture_json

            def validate(value):
                report = json.loads(value)
                _validate_unchanged(runtime.owner, state_baseline)
                assert report["automatic_action_allowed"] is False

            guard = None
        elif kind == "owner":
            def operation():
                return runtime.owner.state

            def validate(value):
                _validate_unchanged(runtime.owner, state_baseline)
                assert value == state_baseline and value is not runtime.owner._state

            guard = None
        else:
            async def operation():
                await producer._sweep(NOW)

            def validate(_value):
                _validate_unchanged(runtime.owner, state_baseline)

            guard = _forbid_sweep_writers(runtime, producer)

        if guard is None:
            wall_max_ms, first_yield_delay_ms, peak_bytes = await _measure(operation, reset, validate)
        else:
            with guard as installed:
                assert installed == FORBIDDEN_SWEEP_SEAMS
                wall_max_ms, first_yield_delay_ms, peak_bytes = await _measure(operation, reset, validate)

        _phase("reset")
        reset()
        _phase("report")
        report_json = capture_json()  # Schema/status output is outside owner/sweep latency timing.
        report = json.loads(report_json)
        _validate_unchanged(runtime.owner, state_baseline)
        finding_codes = sorted(row["code"] for row in report["findings"])
        classification = classify_measurement(snapshot_stable=report["snapshot_stable"],
                                              wall_max_ms=wall_max_ms)
        return {
            "schema_version": 1, "kind": kind, "sweep_phase": sweep_phase,
            "size": size, "synthetic": True, "status": "measured",
            "wall_max_ms": wall_max_ms, "first_yield_delay_ms": first_yield_delay_ms,
            "peak_bytes": peak_bytes, "report_bytes": len(report_json),
            "snapshot_stable": report["snapshot_stable"],
            "counts_complete": report["counts_complete"], "finding_codes": finding_codes,
            "record_counts": {"intents": size, "attempts": size, "outbox": size},
            "rows_linked": True, "unique_rows": True, "terminal_reservations": 0,
            "cohort": cohort, "synthetic_finality": "synthetic_lifecycle_rejected",
            **classification,
        }
    finally:
        _phase("cleanup")
        await store.close()
