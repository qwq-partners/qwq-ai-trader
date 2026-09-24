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
    freeze_checkpoint_facts,
    thaw_checkpoint_facts,
)


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
        "protection": {"schema": 1, "market": "KR", "config": {"stop": 10.0}, "states": {},
                       "entry_times": {}, "exit_exempt": [], "max_holding_days": 5.0,
                       "current_regime": "neutral", "intraday_crash_level": "normal",
                       "integrity_reset_symbols": [], "degraded": {}, "orders": {},
                       "pending_owners": {}},
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


def test_full_builder_owner_codes_match_uncapped_n3_oracle():
    state = checkpoint(3)
    state["attempts"]["attempt-1"]["observed_quantity"] = 2
    assert dict(build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1)).projection.findings) == owner_oracle(state)


def test_scoped_lookup_does_not_iterate_unrelated_history(monkeypatch):
    result = build_owner_recovery_models(checkpoint(5_000), token=OwnerToken("a", 0, 0, 1))
    assert hasattr(result.index, "__slots__")
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


def test_query_source_use_inventory_covers_every_durable_dependency_or_exclusion():
    required = {"latest_explicit_quote", "quote_price_views", "day_valuation_view", "day_valuations",
                "protection.config", "protection.states", "protection.entry_times",
                "protection.exit_exempt", "protection.max_holding_days", "protection.current_regime",
                "protection.intraday_crash_level", "protection.integrity_reset_symbols",
                "protection.degraded", "protection.orders", "protection.pending_owners"}
    assert required <= set(PRODUCER_INDEX_DEPENDENCIES)


def test_protection_schema_and_market_have_no_product_writer():
    result = build_owner_recovery_models(checkpoint(), token=OwnerToken("a", 0, 0, 1))
    value = result.index.protection_quote_input("000000")
    assert value["schema"] == 1 and value["market"] == "KR"


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
        await build_owner_recovery_models_cooperatively(checkpoint(300), token=OwnerToken("a", 0, 0, 1),
                                                        yield_hook=lambda: yields.append(True))
    asyncio.run(run())
    assert yields


def test_cancelled_full_build_never_returns_partial_models():
    async def run():
        task = asyncio.create_task(build_owner_recovery_models_cooperatively(
            checkpoint(5_000), token=OwnerToken("a", 0, 0, 1)))
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
    state["protection"]["pending_owners"] = {"000000": "intent-0"}
    join = build_owner_recovery_models(state, token=OwnerToken("a", 0, 0, 1)).join_view
    assert join.audits_by_intent["intent-0"] == 1
    assert join.audits_by_key[("intent-0", "000000", ("sell_all", 1, "reason"))] == 1
    assert join.pending_fallback_by_pair[("000000", "intent-0")] == 1


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


def test_source_use_inventory_is_not_a_self_certifying_union_and_exclusions_are_unused():
    import inspect
    import src.execution.safety.recovery_projection as projection
    source = inspect.getsource(projection.build_producer_index) + inspect.getsource(projection.ProducerReadView)
    tree = ast.parse(source)
    literals = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and type(node.value) is str}
    used = set().union(*[set(paths) for paths in projection.PRODUCER_INDEX_SOURCE_USES.values()])
    assert used <= set(projection.PRODUCER_INDEX_DEPENDENCIES) | set(projection.PRODUCER_INDEX_EXCLUSIONS)
    assert "market_sources" not in literals and "entry_quotes" not in literals


def test_full_builder_rejects_non_exact_owner_token():
    with pytest.raises(ValueError, match="invalid_owner_token"):
        build_owner_recovery_models(checkpoint(), token="owner-a")


def test_cooperative_full_build_does_not_stall_event_loop_over_50ms():
    async def run():
        gaps, previous, running = [], asyncio.get_running_loop().time(), True

        async def heartbeat():
            nonlocal previous
            while running:
                await asyncio.sleep(0.001)
                now = asyncio.get_running_loop().time()
                gaps.append(now - previous)
                previous = now

        pulse = asyncio.create_task(heartbeat())
        await build_owner_recovery_models_cooperatively(checkpoint(5_000), token=OwnerToken("a", 0, 0, 1))
        running = False
        await pulse
        assert gaps and max(gaps) < 0.05
    asyncio.run(run())


def test_schema_and_market_are_read_only_format_exclusions_in_builder_ast():
    import inspect
    import src.execution.safety.recovery_projection as projection
    source = inspect.getsource(projection.build_producer_index) + inspect.getsource(projection._protection_input)
    assert '["schema"] =' not in source and '["market"] =' not in source
    assert {"protection.schema", "protection.market"} <= set(projection.PRODUCER_INDEX_EXCLUSIONS)
