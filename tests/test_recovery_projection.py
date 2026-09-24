"""Task-1 contracts for detached, indexed recovery checkpoint projections."""
from __future__ import annotations

import asyncio
import ast
import math
from pathlib import Path

import pytest

from src.execution.safety.recovery_diagnostics import _classify
from src.execution.safety.recovery_projection import (
    PRODUCER_INDEX_DEPENDENCIES,
    OwnerToken,
    build_owner_recovery_models,
    build_owner_recovery_models_cooperatively,
    detach_checkpoint,
    freeze_checkpoint_facts,
    thaw_checkpoint_facts,
)


def canonical_protection(with_position=False):
    from datetime import datetime, timezone
    from decimal import Decimal
    from src.core.types import Position
    from src.execution.safety.protection import encode_protection
    from src.strategies.exit_manager import ExitManager
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)
    manager = ExitManager(market="KR", persist=False, clock=lambda: now)
    if with_position:
        manager.register_position(Position("S", quantity=1, avg_price=Decimal("10"),
                                           current_price=Decimal("10"), entry_time=now))
    return encode_protection(manager)


def checkpoint(rows: int = 1):
    attempts, intents, outbox = {}, {}, {}
    for number in range(rows):
        symbol, intent_id, attempt_id = f"{number % 5:06d}", f"intent-{number}", f"attempt-{number}"
        intents[intent_id] = {"symbol": symbol, "side": "sell", "target_quantity": 1,
                              "attempt_ids": [attempt_id]}
        attempts[attempt_id] = {"attempt_id": attempt_id, "intent_id": intent_id, "symbol": symbol,
                                "kind": "submit", "side": "sell", "state": "prepared",
                                "command_status": "acknowledged", "observed_quantity": 0,
                                "applied_quantity": 0, "reserved_quantity": 0,
                                "reserved_cash": "0", "reserved_exposure": "0",
                                "reserved_planned_risk": "0"}
        outbox[f"audit-{number}"] = {"kind": "protection_decision", "symbol": symbol,
                                       "intent_id": intent_id, "decision": ["sell_all", 1, "reason"]}
    return {
        "attempts": attempts, "intents": intents, "inbox": {}, "outbox": outbox,
        "portfolio": {"positions": {}},
        "protection": canonical_protection(),
        "protection_quote_admissions": {}, "latest_explicit_quote": {}, "quote_price_views": {},
        "day_valuation_view": {}, "day_valuations": {},
    }


def owner_oracle(state):
    return _classify({"state": state, "runtime": {"_protection_unattributed_failed": False,
                      "_protection_failures": {}}, "producer": None})


def test_owner_token_rejects_boolean_epochs():
    with pytest.raises(ValueError, match="invalid_owner_token"):
        OwnerToken("owner-a", True, 0, 1)


def test_full_builder_is_detached_and_matches_uncapped_owner_oracle():
    state = checkpoint()
    result = build_owner_recovery_models(state, token=OwnerToken("owner-a", 4, 1, 20))
    state["attempts"].clear()
    assert dict(result.projection.findings) == owner_oracle(checkpoint())
    assert result.index.attempts_for_symbol("000000")
    assert result.index.complete


def test_full_builder_owner_codes_match_uncapped_n3_oracle():
    state = checkpoint(3)
    state["attempts"]["attempt-1"]["observed_quantity"] = 2
    assert dict(build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1)).projection.findings) == owner_oracle(state)


def test_scoped_lookup_does_not_iterate_unrelated_history(monkeypatch):
    result = build_owner_recovery_models(checkpoint(5_000), token=OwnerToken("a", 0, 0, 1))
    from src.execution.safety.recovery_projection import FrozenMap, ProducerReadView
    def forbidden(*args):
        raise AssertionError("unrelated retained history iteration")
    monkeypatch.setattr(FrozenMap, "__iter__", forbidden)
    monkeypatch.setattr(ProducerReadView, "all_attempts", forbidden)
    assert len(result.index.attempts_for_intent("intent-4999")) == 1


def test_malformed_orphan_duplicate_and_missing_links_are_not_dropped():
    state = checkpoint()
    state["intents"]["intent-0"]["attempt_ids"] = ["missing"]
    state["attempts"]["orphan"] = {"not": "a valid attempt"}
    result = build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1))
    assert not result.projection.complete
    assert result.index.invalid_facts()
    assert dict(result.projection.findings).get("evidence_invalid", 0) >= 1


def test_consumer_cannot_mutate_fact_or_change_sibling_view():
    result = build_owner_recovery_models(checkpoint(), token=OwnerToken("a", 0, 0, 1))
    fact = result.index.attempts_for_intent("intent-0")[0]
    with pytest.raises((AttributeError, TypeError)):
        fact.data["state"] = "changed"
    with pytest.raises(AttributeError):
        fact.data._values = {}
    assert result.index.attempts_for_symbol("000000")[0].data["state"] == "prepared"


def test_same_revision_different_incarnation_is_not_the_same_model():
    one = build_owner_recovery_models(checkpoint(), token=OwnerToken("one", 0, 0, 1))
    two = build_owner_recovery_models(checkpoint(), token=OwnerToken("two", 0, 0, 1))
    assert one.projection.token != two.projection.token


def test_freeze_thaw_preserves_finite_float_type_and_exact_value():
    frozen = freeze_checkpoint_facts({"a": [10.0, 0.10, 5.0]})
    thawed = thaw_checkpoint_facts(frozen)
    assert thawed == {"a": [10.0, 0.10, 5.0]}
    assert all(type(value) is float for value in thawed["a"])


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_freeze_rejects_nan_positive_and_negative_infinity(value):
    with pytest.raises(ValueError, match="non_finite_checkpoint_float"):
        freeze_checkpoint_facts({"value": value})


def test_bounded_protection_and_quote_queries():
    state = checkpoint(20)
    state["protection"]["states"]["000000"] = {"remaining_quantity": 1}
    state["protection"]["degraded"]["000000"] = {"reason": "x"}
    state["protection"]["pending_owners"]["000000"] = "intent-0"
    state["protection_quote_admissions"]["command"] = {"symbol": "000000", "intent_id": "intent-0"}
    state["latest_explicit_quote"]["000000"] = {"price": "10", "admission_version": 1}
    result = build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1)).index
    assert result.protection_for_symbol("000000") and result.latest_quote_for_symbol("000000")
    assert result.pending_recovery_symbols() == ("000000",)
    assert result.pending_admissions() and result.admission("command")
    assert result.audits_for_symbol("000000") and result.active_open_buy_symbols() == ()
    assert result.has_degraded_protection


def test_cooperative_builder_yields_every_256_facts_or_5ms():
    yields = []

    async def run():
        await build_owner_recovery_models_cooperatively(detach_checkpoint(checkpoint(300)), token=OwnerToken("a", 0, 0, 1),
                                                        yield_hook=lambda: yields.append(True))
    asyncio.run(run())
    assert yields


def test_cancelled_full_build_never_returns_partial_models():
    async def run():
        task = asyncio.create_task(build_owner_recovery_models_cooperatively(
            detach_checkpoint(checkpoint(5_000)), token=OwnerToken("a", 0, 0, 1)))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())


def test_active_open_buy_requires_submit_and_keeps_reserved_terminal_buy_open():
    state = checkpoint()
    row = state["attempts"]["attempt-0"]
    state["intents"]["intent-0"]["side"] = row["side"] = "buy"
    assert build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1)).index.active_open_buy_symbols() == ("000000",)
    row.update(kind="cancel", state="rejected", reserved_quantity=0)
    assert build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1)).index.active_open_buy_symbols() == ()
    row.update(kind="submit", reserved_quantity=1)
    assert build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1)).index.active_open_buy_symbols() == ("000000",)


def test_orphan_duplicate_and_scalar_rows_are_retained_as_invalid_without_crashing():
    state = checkpoint()
    state["attempts"]["attempt-0"]["attempt_id"] = "other"
    state["intents"]["intent-0"]["attempt_ids"] *= 2
    state["outbox"]["scalar"] = 1
    state["outbox"]["audit-0"]["effect_source"] = "bogus"
    result = build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1))
    assert not result.index.complete and not result.projection.complete
    assert {fact.category for fact in result.index.invalid_facts()} >= {"attempt", "intent", "audit", "outbox"}


def test_join_view_retains_exact_audit_key_and_pending_pair_multiplicity():
    state = checkpoint(2)
    state["intents"]["intent-1"]["symbol"] = "000000"
    state["attempts"]["attempt-1"]["symbol"] = "000000"
    state["outbox"]["audit-1"].update(symbol="000000", intent_id="intent-1")
    state["protection"]["pending_owners"] = {"000000": "orphan"}
    join = build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1)).join_view
    assert join.audits_by_intent["intent-0"] == 1
    assert join.audits_by_key[("intent-0", "000000", ("sell_all", 1, "reason"))] == 1
    assert join.pending_fallback_by_pair[("000000", "orphan")] == 1


def test_quote_and_protection_dependency_sources_are_indexed_and_conflict_is_invalid():
    state = checkpoint()
    symbol = "only-input"
    state["protection"]["entry_times"][symbol] = "2026-01-01T00:00:00+00:00"
    state["protection"]["exit_exempt"] = [symbol]
    state["protection"]["integrity_reset_symbols"] = [symbol]
    state["protection"]["orders"]["order"] = {"symbol": symbol}
    state["quote_price_views"][symbol] = {"price": "10", "source_version": 2, "source": "kis", "source_event_id": "event"}
    state["day_valuation_view"] = {"symbol": symbol, "source_version": 2}
    state["day_valuations"] = {"valuation": {"symbol": symbol}}
    state["latest_explicit_quote"][symbol] = {"price": "11", "admission_version": 2,
                                                 "source": "kis", "source_event_id": "other"}
    index = build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1)).index
    assert index.protection_quote_input(symbol)["entry_times"]
    assert index.latest_quote_for_symbol(symbol)
    assert any(f.category == "quote" for f in index.invalid_facts())


def test_active_recovery_excludes_terminal_history_and_keeps_portfolio_and_pending_symbols():
    state = checkpoint()
    state["attempts"]["attempt-0"].update(state="final_rejected", reserved_quantity=0)
    state["portfolio"]["positions"] = {"portfolio-only": {"quantity": 1}}
    state["protection"]["pending_owners"] = {"pending-only": "intent-0"}
    result = build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1)).index
    assert "000000" not in result.active_recovery_symbols()
    assert {"portfolio-only", "pending-only"} <= set(result.active_recovery_symbols())


def test_pending_admissions_follow_source_version_not_command_id():
    state = checkpoint()
    state["protection_quote_admissions"] = {
        "z-command": {"symbol": "000000", "intent_id": "intent-0", "source_version": 1},
        "a-command": {"symbol": "000000", "intent_id": "intent-0", "source_version": 2},
    }
    assert build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1)).index.next_quote_admission().command_id == "z-command"


def test_public_read_view_and_frozen_map_are_deeply_immutable():
    from src.execution.safety.recovery_projection import FrozenMap
    child = {"inside": []}
    frozen = FrozenMap({"child": child})
    child["inside"].append("mutated")
    assert frozen["child"]["inside"] == ()
    index = build_owner_recovery_models(checkpoint(), token=OwnerToken("a", 0, 0, 1)).index
    with pytest.raises((AttributeError, TypeError)):
        index.complete = False


def test_full_builder_rejects_non_exact_owner_token():
    with pytest.raises(ValueError, match="invalid_owner_token"):
        build_owner_recovery_models(checkpoint(), token="owner-a")


def test_cooperative_full_build_does_not_stall_event_loop_over_50ms():
    # Spec §11.2 requires a fresh serial env-i process for each timing cell.
    import os
    import subprocess
    import sys
    if not os.environ.get("RECOVERY_TIMING_CELL"):
        for _ in range(2):
            outcome = subprocess.run([sys.executable, "-m", "pytest", __file__, "-q", "-s",
                "-p", "no:cacheprovider", "-k", "does_not_stall"],
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": "UTC",
                     "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                     "RECOVERY_TIMING_CELL": "1"}, capture_output=True, text=True, timeout=30)
            if "noisy_host_inconclusive" not in outcome.stdout:
                break
        assert outcome.returncode == 0, outcome.stdout + outcome.stderr
        for line in outcome.stdout.splitlines():
            if line.startswith("{"):
                print(line)
        return
    state = checkpoint(5_000)
    async def run():
        gaps, previous, running = [], asyncio.get_running_loop().time(), True

        async def heartbeat():
            nonlocal previous
            while running:
                future = asyncio.get_running_loop().create_future()
                asyncio.get_running_loop().call_at(previous + .001, future.set_result, None)
                await future
                now = asyncio.get_running_loop().time()
                gaps.append(now - previous)
                previous = now

        loop = asyncio.get_running_loop()
        baseline = []
        started = loop.time()
        deadline = started + .001
        while loop.time() - started < .250:
            future = loop.create_future()
            loop.call_at(deadline, future.set_result, None)
            await future
            now = loop.time()
            baseline.append(now - deadline + .001)
            deadline = now + .001
        assert max(baseline) <= .025, "noisy_host_inconclusive"
        previous = asyncio.get_running_loop().time()
        pulse = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        started = loop.time()
        payload = detach_checkpoint(state)
        detach_seconds = loop.time() - started
        await build_owner_recovery_models_cooperatively(payload, token=OwnerToken("a", 0, 0, 1))
        build_seconds = loop.time() - started
        running = False
        await pulse
        print({"baseline_max": max(baseline), "detach_seconds": detach_seconds,
               "total_seconds": build_seconds, "stall_max": max(gaps)})
        assert detach_seconds < .05
        assert gaps and max(gaps) < 0.05
    asyncio.run(run())


def test_invalid_attempt_audit_protection_and_admission_schemas_fail_closed():
    state = checkpoint()
    state["attempts"]["attempt-0"].update(state="not-a-state", command_status="not-a-status",
                                               observed_quantity=True)
    state["outbox"]["audit-0"]["decision"][1] = 2
    state["protection"]["states"]["000000"] = {}
    state["protection_quote_admissions"]["bad"] = {"symbol": "000000", "intent_id": "intent-0",
                                                       "source_version": True, "status": "nope"}
    result = build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1)).index
    assert not result.complete
    assert {fact.category for fact in result.invalid_facts()} >= {"attempt", "audit", "protection", "admission"}


def test_pending_fallback_only_when_intent_absent_and_no_matching_admission():
    state = checkpoint()
    state["protection"]["pending_owners"] = {"has-intent": "intent-0", "has-admission": "missing",
                                                "needs-fallback": "orphan"}
    state["protection_quote_admissions"]["match"] = {"symbol": "has-admission", "intent_id": "missing",
                                                         "source_version": 1}
    pairs = build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1)).join_view.pending_fallback_by_pair
    assert ("needs-fallback", "orphan") in pairs
    assert ("has-intent", "intent-0") not in pairs
    assert ("has-admission", "missing") not in pairs


def test_quote_price_view_uses_real_validation_and_scopes_day_valuation():
    state = checkpoint()
    state["quote_price_views"] = {"wanted": {"price": "10", "source_version": 1,
        "received_at": "2026-01-01T01:00:00+00:00", "market_as_of": "2026-01-01T02:00:00+00:00",
        "source": "kis", "source_event_id": "event"}}
    state["day_valuation_view"] = {"symbol": "wanted", "value": 1}
    state["day_valuations"] = {"wanted": {"value": 1}, "unrelated": {"value": 2}}
    result = build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1)).index
    quote = result.latest_quote_for_symbol("wanted")
    assert quote and "unrelated" not in quote.data["day_valuations"]
    assert any(f.category == "quote" for f in result.invalid_facts())


def test_public_freezer_rejects_fact_dtos_and_non_string_frozen_map_keys():
    from src.execution.safety.recovery_projection import FrozenMap, InvalidRecoveryFact
    with pytest.raises(ValueError):
        freeze_checkpoint_facts({"fact": InvalidRecoveryFact("x", None, "x")})
    with pytest.raises(ValueError):
        FrozenMap({1: "not-json"})


def test_cooperative_builder_detaches_before_background_work_can_observe_mutation():
    state = checkpoint(2)
    async def run():
        task = asyncio.create_task(build_owner_recovery_models_cooperatively(detach_checkpoint(state), token=OwnerToken("a", 0, 0, 1)))
        state["attempts"]["attempt-0"]["state"] = "final_rejected"
        result = await task
        return result
    result = asyncio.run(run())
    assert result.index.attempts_for_intent("intent-0")[0].state == "prepared"


def test_cooperative_entry_requires_explicit_detached_handoff():
    with pytest.raises(ValueError, match="detached_checkpoint_required"):
        build_owner_recovery_models_cooperatively(checkpoint(), token=OwnerToken("a", 0, 0, 1))


def test_build_session_advances_real_work_and_cancellation_drops_private_state(monkeypatch):
    import src.execution.safety.recovery_projection as projection
    assert hasattr(projection, "RecoveryBuildSession"), "resumable builder API missing"
    source = checkpoint(300)
    payload = projection.detach_checkpoint(source)
    source["attempts"].clear()
    session = projection.RecoveryBuildSession(payload, token=OwnerToken("a", 0, 0, 1))
    phases = set()
    while session.result is None:
        assert session.advance() <= 256
        phases.add(session.phase)
    assert {"freeze", "classify", "join", "index", "complete"} <= phases
    assert len(session.result.index.attempts_for_symbol("000000")) == 60
    for phase in ("freeze", "classify", "join", "index"):
        cancelled = projection.RecoveryBuildSession(projection.detach_checkpoint(checkpoint(300)),
                                                     token=OwnerToken("a", 0, 0, 1))
        while cancelled.phase != phase:
            cancelled.advance()
        cancelled.close()
        assert cancelled.result is None
        assert cancelled.phase == "cancelled"
        with pytest.raises(ValueError, match="closed_build_session"):
            cancelled.advance()


def test_production_builder_does_not_call_n3_oracle(monkeypatch):
    import src.execution.safety.recovery_projection as projection
    def forbidden(*args, **kwargs):
        raise AssertionError("N3 classifier is test oracle only")
    monkeypatch.setattr(projection, "_classify", forbidden, raising=False)
    assert dict(build_owner_recovery_models(checkpoint(3), token=OwnerToken("a", 0, 0, 1)).projection.findings) == {
        "pending_sell": 3}


def test_selected_day_valuation_is_scoped_and_sets_quote_time_floor():
    state = checkpoint()
    state["day_valuation_view"] = {"evidence_id": "selected", "rollover_version": 4}
    state["day_valuations"] = {
        "selected": {"payload": {"prices": [
            {"symbol": "wanted", "price": "10", "as_of": "2026-09-24T09:00:00+09:00"},
            {"symbol": "other", "price": "20", "as_of": "2026-09-24T09:00:00+09:00"}]}},
        "old": {"payload": {"prices": [{"symbol": "history", "price": "1"}]}},
    }
    result = build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 4)).index
    quote = result.latest_quote_for_symbol("wanted")
    assert quote is not None
    assert quote.data["time_floor"] == "2026-09-24T09:00:00+09:00"
    assert quote.data["day_valuations"]["symbol"] == "wanted"
    assert "other" not in repr(quote) and "history" not in repr(quote)
    assert result.latest_quote_for_symbol("history") is None


def test_protection_symbol_input_does_not_embed_other_symbols():
    state = checkpoint()
    state["protection"]["exit_exempt"] = ["000000", "unrelated"]
    state["protection"]["integrity_reset_symbols"] = ["unrelated"]
    state["protection"]["orders"] = {"x": {"symbol": "000000"}, "y": {"symbol": "unrelated"}}
    value = build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1)).index.protection_quote_input("000000")
    assert value["exit_exempt"] == ("000000",)
    assert value["integrity_reset_symbols"] == ()
    assert "unrelated" not in repr(value)


@pytest.mark.parametrize("query,args", [
    ("protection_for_symbol", ("000000",)), ("protection_quote_input", ("000000",)),
    ("latest_quote_for_symbol", ("000000",)), ("pending_recovery_symbols", ()),
    ("pending_admissions", ()), ("admission", ("command",)),
    ("audits_for_symbol", ("000000",)), ("active_open_buy_symbols", ()),
    ("has_degraded_protection", ()), ("attempts_for_symbol", ("000000",)),
    ("attempts_for_intent", ("intent-0",)), ("intent", ("intent-0",)),
    ("intents_for_symbol", ("000000",)), ("admissions_for_symbol", ("000000",)),
    ("admissions_for_intent", ("intent-0",)), ("audits_for_intent", ("intent-0",)),
    ("pending_symbols_for_intent", ("intent-0",)), ("pending_owner_for_symbol", ("000000",)),
    ("active_recovery_symbols", ()), ("next_quote_admission", ()),
])
def test_each_scoped_query_has_no_history_iteration(monkeypatch, query, args):
    from src.execution.safety.recovery_projection import FrozenMap, ProducerReadView
    state = checkpoint(10)
    state["protection"]["states"]["000000"] = {"remaining_quantity": 1}
    state["protection"]["pending_owners"]["000000"] = "intent-0"
    state["protection"]["degraded"]["000000"] = {"quantity": 1, "reason": "repair"}
    state["protection_quote_admissions"]["command"] = {"symbol": "000000", "intent_id": "intent-0"}
    state["latest_explicit_quote"]["000000"] = {"price": "10"}
    state["attempts"]["attempt-0"]["side"] = "buy"
    index = build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1)).index
    def forbidden(*args):
        raise AssertionError("unrelated retained history iteration")
    monkeypatch.setattr(FrozenMap, "__iter__", forbidden)
    monkeypatch.setattr(ProducerReadView, "all_attempts", forbidden)
    value = getattr(index, query)
    assert (value(*args) if callable(value) else value)


def test_query_source_use_inventory_covers_every_durable_dependency_or_exclusion():
    import src.execution.safety.recovery_projection as projection
    reads = set()
    class TracedMap(projection.FrozenMap):
        __slots__ = ("path",)
        def __init__(self, source, path=""):
            object.__setattr__(self, "_values", source)
            self.path = path
        def __getitem__(self, key):
            if type(key) is not str:
                raise KeyError(key)
            path = self.path + "." + key if self.path else key
            if self.path in ("", "protection", "portfolio"):
                reads.add(path)
            value = super().__getitem__(key)
            return TracedMap(value, path) if isinstance(value, projection.FrozenMap) else value
    projection.build_producer_index(TracedMap(freeze_checkpoint_facts(checkpoint())), token=OwnerToken("a", 0, 0, 1))
    actual = reads - {"protection", "portfolio"}
    assert actual - projection.PRODUCER_INDEX_EXCLUSIONS == projection.PRODUCER_INDEX_DEPENDENCIES
    assert not ({"market_sources", "entry_quotes"} & actual)


@pytest.mark.parametrize("field", [
    "config", "states", "entry_times", "exit_exempt", "max_holding_days", "current_regime",
    "intraday_crash_level", "integrity_reset_symbols", "degraded", "orders", "pending_owners",
])
def test_every_protection_input_add_update_delete(field):
    from copy import deepcopy
    from datetime import datetime, timezone
    from src.execution.safety.protection import decode_protection
    state = checkpoint(0)
    state["protection"] = canonical_protection(with_position=True)
    state["portfolio"]["positions"] = {"S": {"quantity": 1}}
    root = state["protection"]
    first_state = root["states"]["S"]
    order = {"symbol": "S", "side": "SELL", "intent_id": "intent-0", "kind": "exit",
             "base_quantity": 1, "cumulative_quantity": 0, "reset_applied": False}
    first, second = {
        "config": ({**root["config"], "stop_loss_pct": 10.0}, {**root["config"], "stop_loss_pct": 11.0}),
        "states": ({"S": first_state}, {"S": {**first_state, "remaining_quantity": 0}}),
        "entry_times": ({"S": "2026-09-23T00:00:00+00:00"}, {"S": "2026-09-24T00:00:00+00:00"}),
        "exit_exempt": (["S"], []), "max_holding_days": (5, 6),
        "current_regime": ("neutral", "trending_bull"), "intraday_crash_level": ("normal", "crash"),
        "integrity_reset_symbols": (["S"], []),
        "degraded": ({"S": {"quantity": 1, "reason": "a"}}, {"S": {"quantity": 2, "reason": "b"}}),
        "orders": ({"order": order}, {"order": {**order, "cumulative_quantity": 1}}),
        "pending_owners": ({"S": "intent-0"}, {"S": "other"}),
    }[field]
    snapshots = []
    for value in (first, second, None):
        if value is None:
            state["protection"].pop(field, None)
        else:
            state["protection"][field] = deepcopy(value)
            decode_protection(state["protection"], clock=lambda: datetime(2026, 9, 24, tzinfo=timezone.utc))
        index = build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1)).index
        assert index.complete is (value is not None)
        selected = thaw_checkpoint_facts(index.protection_quote_input("S"))[field]
        expected = value
        if field in ("states", "entry_times", "degraded", "pending_owners"):
            expected = None if value is None else value.get("S")
        elif field == "orders":
            expected = [] if value is None else list(value.values())
        elif field in ("exit_exempt", "integrity_reset_symbols"):
            expected = [] if value is None else value
        assert selected == expected
        snapshots.append(index.protection_quote_input("S"))
    assert snapshots[0][field] != snapshots[1][field]


@pytest.mark.parametrize("field,value", [("reserved_cash", "NaN"), ("applied_quantity", 2),
                                         ("reserved_planned_risk", None)])
def test_each_invalid_reservation_or_applied_quantity_fails_producer_closed(field, value):
    state = checkpoint()
    state["attempts"]["attempt-0"][field] = value
    assert not build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1)).index.complete


def test_terminal_cash_only_reservation_remains_active():
    state = checkpoint()
    state["attempts"]["attempt-0"].update(state="final_cancelled", side="buy", reserved_cash="1")
    state["intents"]["intent-0"]["side"] = "buy"
    index = build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1)).index
    assert index.active_open_buy_symbols() == ("000000",)
    assert index.active_recovery_symbols() == ("000000",)


def explicit_quote(price="10", event="event", stamp="2026-09-24T10:00:00+09:00"):
    from src.execution.safety.protection_recovery import digest
    row = {"price": price, "as_of": stamp, "received_at": stamp,
           "source": "synthetic", "source_event_id": event, "market_data": {}, "admission_version": 4}
    payload = {"symbol": "000000", **{key: row[key] for key in
               ("price", "as_of", "source", "source_event_id", "market_data")}}
    row["payload_digest"] = digest(payload)
    return row


@pytest.mark.parametrize("fault", ["as_of", "received_at", "source_event_id", "source_version",
                                   "missing_explicit", "unknown_source", "future_version", "digest"])
def test_quote_invalidity_matches_real_product_validators(fault):
    from src.execution.safety.runtime import KRExecutionRuntime
    from src.execution.safety.market_source import validate_price_views
    state = checkpoint()
    exact = explicit_quote()
    view = {"price": exact["price"], "market_as_of": exact["as_of"], "received_at": exact["received_at"],
            "source": exact["source"], "source_event_id": exact["source_event_id"], "source_version": 4}
    state["latest_explicit_quote"]["000000"] = exact
    state["quote_price_views"]["000000"] = view
    if fault == "missing_explicit":
        state["latest_explicit_quote"].clear()
    elif fault == "unknown_source":
        view["market_as_of"] = None
    elif fault == "future_version":
        view["source_version"] = 5
    elif fault == "digest":
        exact["payload_digest"] = "conflicting"
    elif fault == "source_version":
        view[fault] = 3
    else:
        exact[fault] = "different" if fault == "source_event_id" else "2026-09-24T09:00:00+09:00"
    runtime = object.__new__(KRExecutionRuntime)
    with pytest.raises((ValueError, TypeError)):
        runtime._validate_explicit_quotes(state, 4)
        validate_price_views(state, 4)
    index = build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 4)).index
    assert not index.complete
    assert any(row.category == "quote" for row in index.invalid_facts())


@pytest.mark.parametrize("root", ["latest_explicit_quote", "quote_price_views", "day_valuation_view", "day_valuations"])
def test_quote_sources_add_update_delete_and_stale_floor_match_runtime(root):
    from datetime import datetime
    from src.execution.safety.runtime import KRExecutionRuntime
    runtime = object.__new__(KRExecutionRuntime)
    state = checkpoint()
    state["day_valuation_view"] = {"evidence_id": "day", "rollover_version": 4}
    day = {"payload": {"prices": [{"symbol": "000000", "price": "10",
                                    "as_of": "2026-09-24T09:00:00+09:00"}]}}
    state["day_valuations"] = {"day": day}
    for stage in ("add", "update", "delete"):
        exact = explicit_quote(event=stage, stamp=f"2026-09-24T{10 if stage == 'add' else 11}:00:00+09:00")
        if root == "latest_explicit_quote":
            state[root] = {} if stage == "delete" else {"000000": exact}
        elif root == "quote_price_views":
            state["latest_explicit_quote"] = {"000000": exact}
            state[root] = {} if stage == "delete" else {"000000": {
                "price": exact["price"], "source_version": 4, "received_at": exact["received_at"],
                "market_as_of": exact["as_of"], "source": exact["source"], "source_event_id": exact["source_event_id"]}}
        elif root == "day_valuation_view":
            state["day_valuations"]["new"] = {"payload": {"prices": [{"symbol": "000000", "price": "20",
                                                   "as_of": "2026-09-25T09:00:00+09:00"}]}}
            state[root] = {} if stage == "delete" else {"evidence_id": "day" if stage == "add" else "new", "rollover_version": 4}
        else:
            state[root] = {} if stage == "delete" else {"day": {"payload": {"prices": [{"symbol": "000000",
                "price": "10", "as_of": exact["as_of"]}]}}}
        index = build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 4)).index
        quote = index.latest_quote_for_symbol("000000")
        expected = runtime._quote_time_floor(state, "000000")
        actual = quote.data["time_floor"] if quote else None
        assert (datetime.fromisoformat(actual) if actual else None) == expected
        if expected:
            from src.execution.safety.application import ApplicationBlocked
            with pytest.raises(ApplicationBlocked, match="stale_market_quote"):
                runtime._require_quote_freshness(state, "000000", datetime.fromisoformat("2026-09-23T10:00:00+09:00"))


def test_all_supported_owner_findings_and_join_cartesian_multiplicity_match_n3():
    state = checkpoint(6)
    state["inbox"]["pending"] = {"status": "RECEIVED"}
    state["attempts"]["attempt-0"].update(side="buy", state="blocked_unknown", command_status="unknown", reserved_cash="1")
    state["intents"]["intent-0"]["side"] = "buy"
    state["attempts"]["attempt-1"].update(state="final_cancelled", observed_quantity=1, reserved_quantity=1)
    state["attempts"]["attempt-2"].update(kind="cancel", parent_attempt_id="missing")
    state["attempts"]["attempt-3"].update(applied_quantity=1)
    state["outbox"]["orphan"] = {"kind": "protection_decision", "symbol": "missing", "intent_id": "missing", "decision": ["sell_all", 2, "x"]}
    state["outbox"]["duplicate"] = dict(state["outbox"]["audit-0"])
    state["protection"]["pending_owners"] = {"bad-symbol": "intent-0", "another": "intent-0", "orphan": "absent"}
    state["protection"]["states"] = {"bad-symbol": {"remaining_quantity": 2, "pending_target_qty": 7}}
    state["protection"]["degraded"] = {"repair": {"quantity": 2, "reason": "repair"}}
    state["protection_quote_admissions"] = {"a": {"intent_id": "intent-0", "symbol": "x"},
                                              "b": {"intent_id": "intent-0", "symbol": "y"}}
    counts = owner_oracle(state)
    assert set(counts) >= {"unapplied_inbox", "observation_not_applied", "unknown_buy", "remaining_reservation",
        "terminal_reservation", "cancel_unconfirmed", "attempt_link_inconsistent", "protection_unsubmitted",
        "protection_link_inconsistent", "protection_quantity_inconsistent", "repair_only", "pending_sell", "evidence_invalid"}
    result = build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 4))
    assert dict(result.projection.findings) == counts
    assert result.join_view.audits_by_key[("intent-0", "000000", ("sell_all", 1, "reason"))] == 2
    assert dict(result.join_view.pending_fallback_by_pair) == {("orphan", "absent"): 1}


def test_session_time_budget_and_real_phase_cancellation_without_workers(monkeypatch):
    import src.execution.safety.recovery_projection as projection
    def forbidden(*args, **kwargs):
        raise AssertionError("background worker forbidden")
    monkeypatch.setattr(asyncio, "to_thread", forbidden)
    ticks = iter(n * .003 for n in range(100000))
    session = projection.RecoveryBuildSession(detach_checkpoint(checkpoint()), token=OwnerToken("a", 0, 0, 1),
                                               clock=lambda: next(ticks))
    assert session.advance() == 1
    assert session.result is None
    session.close()
    async def run():
        task = asyncio.create_task(build_owner_recovery_models_cooperatively(detach_checkpoint(checkpoint()),
                                    token=OwnerToken("a", 0, 0, 1)))
        await task
    asyncio.run(run())


def test_protection_schema_and_market_have_no_product_writer():
    """Track DTO aliases, item/delete writes, and update/pop/setdefault calls."""
    violations = []
    for path in (Path(__file__).parents[1] / "src").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for function in (node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
            aliases = {arg.arg for arg in function.args.args if arg.arg == "protection"}
            if path.name == "protection.py":
                aliases |= {arg.arg for arg in function.args.args if arg.arg == "dto"}
            def is_dto(node):
                if isinstance(node, ast.Name):
                    return node.id in aliases
                if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                    return node.slice.value == "protection"
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name) and node.func.id in ("deepcopy", "dict", "_fmap") and node.args:
                        return is_dto(node.args[0])
                    return (isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                            and node.args and isinstance(node.args[0], ast.Constant)
                            and node.args[0].value == "protection")
                return False
            for _ in range(5):
                for node in ast.walk(function):
                    if isinstance(node, ast.Assign) and is_dto(node.value):
                        aliases.update(target.id for target in node.targets if isinstance(target, ast.Name))
            for node in ast.walk(function):
                if (isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del))
                        and isinstance(node.slice, ast.Constant) and node.slice.value in ("schema", "market")
                        and is_dto(node.value)):
                    violations.append((str(path), node.lineno))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and is_dto(node.func.value):
                    keys = {keyword.arg for keyword in node.keywords}
                    if node.func.attr in ("pop", "setdefault", "__setitem__") and node.args and isinstance(node.args[0], ast.Constant):
                        keys.add(node.args[0].value)
                    if node.func.attr == "update" and node.args and isinstance(node.args[0], ast.Dict):
                        keys.update(key.value for key in node.args[0].keys if isinstance(key, ast.Constant))
                    if node.func.attr in ("update", "pop", "setdefault", "__setitem__") and keys & {"schema", "market"}:
                        violations.append((str(path), node.lineno))
    assert violations == []


@pytest.mark.parametrize("root", ["market_sources", "entry_quotes"])
def test_excluded_source_change_is_rejected_by_actual_owner_version_gate(tmp_path, monkeypatch, root):
    from decimal import Decimal
    from test_execution_command_owner import fixture
    from test_execution_market_source import market_event
    async def run():
        f = await fixture(tmp_path, monkeypatch)
        runtime, commands = f["runtime"], f["commands"]
        try:
            before = runtime.owner.version
            if root == "market_sources":
                await runtime.observe_market(await market_event(monkeypatch, f["clock"][0]))
            else:
                await commands.observe_entry_quote("005930", Decimal("10000"), as_of=f["clock"][0],
                    source="synthetic", event_id="first", expected_version=before)
            state, version = runtime.owner.state, runtime.owner.version
            assert state[root] and version > before
            with pytest.raises(ValueError, match="stale_execution_version"):
                await commands.observe_entry_quote("other", Decimal("10000"), as_of=f["clock"][0],
                    source="synthetic", event_id="stale", expected_version=before)
            assert runtime.owner.version == version and runtime.owner.state == state
            assert not f["broker"]._session.posts
        finally:
            await runtime.shutdown()
            await f["store"].close()
    asyncio.run(run())


@pytest.mark.parametrize("path,value", [
    (("protection", "states"), []), (("protection", "orders"), []),
    (("protection", "pending_owners"), []), (("portfolio", "positions"), []),
    (("protection", "exit_exempt"), [True]), (("protection", "integrity_reset_symbols"), ["x", "x"]),
    (("protection", "degraded", "000000"), {"reason": "x", "quantity": True}),
    (("protection", "orders", "order"), {"symbol": "000000", "side": "unsupported"}),
    (("protection", "states", "000000"), {"remaining_quantity": 1, "pending_target_qty": True}),
    (("protection_quote_admissions", "command"), {"symbol": "000000", "intent_id": "intent-0", "source_version": 1}),
])
def test_malformed_producer_containers_and_rows_never_certify_complete(path, value):
    state = checkpoint()
    target = state
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    result = build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1))
    assert not result.index.complete
    assert result.index.invalid_facts()


def test_valid_quote_admission_without_intent_is_complete():
    from src.execution.safety.protection_recovery import digest
    state = checkpoint(0)
    request = {"symbol": "000000", "price": "10", "market_data": {}, "intent_id": None,
               "observed_at": "2026-09-24T10:00:00+09:00", "market_as_of": None,
               "source": None, "source_event_id": None}
    state["protection_quote_admissions"]["quote:one"] = {
        **request, "payload_digest": digest(request), "status": "RECEIVED", "source_version": 1,
        "admitted_at": request["observed_at"]}
    result = build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1))
    assert result.index.complete
    assert result.index.admission("quote:one").intent_id is None


@pytest.mark.parametrize("field,value", [
    ("entry_times", {"S": True}), ("max_holding_days", True), ("max_holding_days", -1),
    ("current_regime", "unsupported"), ("intraday_crash_level", "unsupported"),
    ("config_missing", None), ("config_extra", None), ("config_bool_numeric", True),
])
def test_canonical_protection_rejection_never_certifies_producer_complete(field, value):
    from datetime import datetime, timezone
    from src.execution.safety.protection import decode_protection
    state = checkpoint(0)
    dto = state["protection"]
    clock = lambda: datetime(2026, 9, 24, tzinfo=timezone.utc)
    decode_protection(dto, clock=clock)
    assert build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1)).index.complete
    if field == "config_missing":
        dto["config"].pop("stop_loss_pct")
    elif field == "config_extra":
        dto["config"]["extra"] = 1
    elif field == "config_bool_numeric":
        dto["config"]["stop_loss_pct"] = value
    else:
        dto[field] = value
    with pytest.raises(ValueError):
        decode_protection(dto, clock=clock)
    result = build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1))
    assert not result.index.complete
    assert not result.projection.complete
    assert any(row.category == "protection" for row in result.index.invalid_facts())


def test_canonical_protection_valid_root_variants_remain_complete():
    from datetime import datetime, timezone
    from src.execution.safety.protection import decode_protection, encode_protection
    state = checkpoint(0)
    dto = state["protection"]
    dto.update(entry_times={"S": "2026-09-24T10:00:00+09:00"}, max_holding_days=0,
               current_regime="neutral", intraday_crash_level="severe")
    dto["config"]["stop_loss_pct"] = 10.0
    decoded = decode_protection(dto, clock=lambda: datetime(2026, 9, 24, tzinfo=timezone.utc))
    assert encode_protection(decoded) == dto
    result = build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1))
    assert result.index.complete and result.projection.complete
    assert thaw_checkpoint_facts(result.index.protection_quote_input("S"))["config"] == dto["config"]


@pytest.mark.parametrize("symbol", ["", " bad ", "valid-symbol"])
def test_explicit_quote_symbol_identity_matches_canonical_validator(symbol):
    from src.execution.safety.runtime import KRExecutionRuntime
    from src.execution.safety.protection_recovery import digest
    state = checkpoint(0)
    row = explicit_quote()
    runtime = object.__new__(KRExecutionRuntime)
    row["payload_digest"] = digest(runtime._market_quote_payload(symbol, row))
    state["latest_explicit_quote"][symbol] = row
    if symbol == "valid-symbol":
        runtime._validate_explicit_quotes(state, 4)
    else:
        with pytest.raises(ValueError):
            runtime._validate_explicit_quotes(state, 4)
    result = build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 4))
    assert result.index.complete is (symbol == "valid-symbol")
    assert result.projection.complete is (symbol == "valid-symbol")


def test_none_pending_owner_and_intentless_admission_match_n3_fail_closed():
    from src.execution.safety.protection_recovery import digest
    state = checkpoint(0)
    request = {"symbol": "S", "price": "10", "market_data": {}, "intent_id": None,
               "observed_at": "2026-09-24T10:00:00+09:00", "market_as_of": None,
               "source": None, "source_event_id": None}
    state["protection_quote_admissions"]["quote:one"] = {
        **request, "payload_digest": digest(request), "status": "RECEIVED", "source_version": 1,
        "admitted_at": request["observed_at"]}
    state["protection"]["pending_owners"] = {"S": None}
    assert owner_oracle(state) == {}
    result = build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1))
    assert dict(result.projection.findings) == {}
    assert not result.index.complete and not result.projection.complete
    assert any(row.category == "pending_owner" for row in result.index.invalid_facts())


def test_randomized_pending_admission_boundary_matches_uncapped_n3():
    import random
    rng = random.Random(40924)
    for _ in range(1000):
        state = checkpoint(2)
        identities = [None, "intent-0", "intent-1", "missing"]
        state["protection"]["pending_owners"] = {
            symbol: rng.choice(identities) for symbol in ("S", "000000", "000001") if rng.randrange(2)}
        state["protection_quote_admissions"] = {
            command: {"symbol": rng.choice(["S", "000000", "000001"]), "intent_id": rng.choice(identities)}
            for command in ("audit-0", "audit-1", "quote:one") if rng.randrange(2)}
        result = build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 4))
        assert dict(result.projection.findings) == owner_oracle(state)


@pytest.mark.parametrize("rows", [5_000, 100_000, "long_protection_row"])
@pytest.mark.parametrize("automatic_gc", [True, False], ids=["normal_gc", "controlled_work"])
def test_every_real_advance_call_including_finalization_stays_below_5ms(rows, automatic_gc):
    """Separate unpreemptible GC from controllable work; never forgive a seal."""
    import gc
    import os
    import subprocess
    import sys
    import time
    from src.execution.safety.recovery_projection import RecoveryBuildSession
    if not os.environ.get("RECOVERY_SLICE_CELL"):
        mode = "normal_gc" if automatic_gc else "controlled_work"
        node = f"{__file__}::test_every_real_advance_call_including_finalization_stays_below_5ms[{mode}-{rows}]"
        for _ in range(2):
            outcome = subprocess.run([sys.executable, "-m", "pytest", node, "-q", "-s", "-p", "no:cacheprovider"],
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": "UTC",
                     "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                     "RECOVERY_SLICE_CELL": "1"}, capture_output=True, text=True, timeout=30)
            if "noisy_host_inconclusive" not in outcome.stdout:
                break
        assert outcome.returncode == 0, outcome.stdout + outcome.stderr
        for line in outcome.stdout.splitlines():
            if line.startswith("{"):
                print(line)
        return
    state = checkpoint(0 if rows == "long_protection_row" else rows)
    if rows == "long_protection_row":
        from datetime import datetime, timezone
        from src.execution.safety.protection import decode_protection
        state["protection"] = canonical_protection(with_position=True)
        state["portfolio"]["positions"] = {"S": {"quantity": 1}}
        state["protection"]["states"]["S"]["exit_history"] = [
            {"timestamp": "2026-09-24T00:00:00+00:00", "action": "sell_partial",
             "quantity": 1, "reason": "history", "remaining_before": 1} for _ in range(10000)]
        decode_protection(state["protection"], clock=lambda: datetime(2026, 9, 24, tzinfo=timezone.utc))
    thresholds = gc.get_threshold()
    assert gc.isenabled()
    async def run():
        loop = asyncio.get_running_loop()
        baseline = []
        started = loop.time()
        while loop.time() - started < .250:
            deadline = loop.time() + .001
            future = loop.create_future()
            loop.call_at(deadline, future.set_result, None)
            await future
            baseline.append(loop.time() - deadline + .001)
        assert max(baseline) <= .025, "noisy_host_inconclusive"
        session = RecoveryBuildSession(detach_checkpoint(state), token=OwnerToken("a", 0, 0, 1))
        measurements, collections = [], []
        gc_started = {}
        collected_seconds = 0.0
        def observe_gc(phase, info):
            nonlocal collected_seconds
            generation = info["generation"]
            if phase == "start":
                gc_started[generation] = time.perf_counter()
            else:
                duration = time.perf_counter() - gc_started[generation]
                collections.append((generation, duration))
                collected_seconds += duration
        gc.callbacks.append(observe_gc)
        if not automatic_gc:
            # Harness-only isolation, never a product workaround. Normal-GC
            # slices and the separate strict 50 ms heartbeat remain required.
            gc.disable()
        try:
            while session.result is None:
                phase = session.phase
                before_gc = collected_seconds
                started = time.perf_counter()
                examined = session.advance()
                elapsed = time.perf_counter() - started
                during_gc = collected_seconds - before_gc
                measurements.append((elapsed, elapsed - during_gc, phase, session.phase, examined, during_gc))
                assert examined <= 256
                await asyncio.sleep(0)
            assert gc.isenabled() is automatic_gc
        finally:
            if not automatic_gc:
                gc.enable()
            gc.callbacks.remove(observe_gc)
        print({"rows": rows, "automatic_gc": automatic_gc, "baseline_max": max(baseline),
               "advance_max": max(measurements), "controlled_max": max(row[1] for row in measurements),
               "finalization": measurements[-1], "over_5ms_count": sum(row[0] >= .005 for row in measurements),
               "controlled_overruns": [row for row in measurements if row[1] >= .005],
               "gc_max": max(collections, key=lambda row: row[1], default=None)})
        assert session.result.index.complete
        assert gc.isenabled() and gc.get_threshold() == thresholds
        # A wall overrun is permitted only for GC overlapping that exact call.
        assert max(row[1] for row in measurements) < .005
        assert all(row[0] < .005 or row[5] > 0 for row in measurements)
    asyncio.run(run())
