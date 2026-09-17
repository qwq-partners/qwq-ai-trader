"""실제 ExitManager의 후보 상태 격리와 부분체결 보호 계약."""
from copy import deepcopy
from dataclasses import fields
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from src.core.types import Position
from src.execution.safety.application import FillDelta, FillObservation
from src.execution.safety.protection import (
    encode_protection, decode_protection, publish_protection,
    reduce_protection, quote_protection,
)
from src.strategies.exit_manager import ExitConfig, ExitManager, ExitStage, PositionExitState


NOW = datetime(2026, 9, 18, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))
SYMBOL = "005930"


def manager(tmp_path):
    return ExitManager(persist=False, state_dir=tmp_path / "absent", clock=lambda: NOW)


def position(quantity, price="10000"):
    return Position(SYMBOL, quantity=quantity, avg_price=Decimal(price),
                    current_price=Decimal(price), entry_time=NOW)


def observation(quantity, *, order="entry", side="BUY", params=None, price="10000"):
    return FillObservation("scope", "KR", "2026-09-18", "KRX", order, SYMBOL,
                           side, quantity, Decimal(price) * quantity,
                           metadata={"registration_params": params or {}})


def reduce(dto, before, after, qty, cumulative, *, order="entry", side="BUY",
           kind=None, intent="entry-intent", now=NOW, params=None):
    kind = kind if kind is not None else ("exit" if side == "SELL" else "same_entry_fill")
    return reduce_protection(dto, before=before, after=after,
                             observation=observation(cumulative, order=order, side=side, params=params),
                             delta=FillDelta(qty, Decimal("10000") * qty, Decimal("0")),
                             fill_kind=kind, intent_id=intent, now=now)


def initial(tmp_path, quantity=40):
    dto, status = reduce(encode_protection(manager(tmp_path)), None, position(quantity),
                         quantity, quantity, kind="initial_entry")
    assert status == "ready"
    return dto


def test_full_snapshot_roundtrip_is_detached_and_has_no_file_effects(tmp_path):
    original = manager(tmp_path)
    original.register_position(position(100), atr_pct_hint=2.0, strategy_name="sepa_trend")
    state = original.get_state(SYMBOL)
    state.current_stage = ExitStage.SECOND
    state.highest_price = Decimal("13000.125")
    state.breakeven_activated = True
    state.pending_stage = ExitStage.THIRD
    state.pending_since = NOW - timedelta(minutes=50)
    state.pending_target_qty = 20
    state.pending_filled_qty = 3
    state.initial_risk_amount = Decimal("50000.25")
    state.actual_stop_pct = Decimal("5")
    original._current_regime = "trending_bull"
    original._intraday_crash_level = "severe"
    original._max_holding_days = 23
    original._integrity_reset_symbols.add(SYMBOL)
    original.add_exit_exempt("000660")
    dto = encode_protection(original)
    assert set(dto["states"][SYMBOL]) == {field.name for field in fields(PositionExitState)}
    assert set(dto["config"]) == {field.name for field in fields(ExitConfig)}
    restored = decode_protection(dto, clock=lambda: NOW)
    assert encode_protection(restored) == dto
    restored.get_state(SYMBOL).highest_price = Decimal("99999")
    restored.config.stop_loss_pct = 99
    assert original.get_state(SYMBOL).highest_price == Decimal("13000.125")
    assert original.config.stop_loss_pct == 5
    assert dto["states"][SYMBOL]["highest_price"] == "13000.125"
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("corruption", ["missing_root", "missing_state", "unknown_field", "nan", "bool_quantity", "naive_time", "bad_stage", "missing_config", "schema"])
def test_decode_rejects_corrupt_snapshot_without_installing_defaults(tmp_path, corruption):
    dto = initial(tmp_path)
    if corruption == "missing_root": del dto["orders"]
    elif corruption == "missing_state": del dto["states"][SYMBOL]["pending_filled_qty"]
    elif corruption == "unknown_field": dto["states"][SYMBOL]["surprise"] = 1
    elif corruption == "nan": dto["states"][SYMBOL]["entry_price"] = "NaN"
    elif corruption == "bool_quantity": dto["states"][SYMBOL]["remaining_quantity"] = True
    elif corruption == "naive_time": dto["entry_times"][SYMBOL] = "2026-09-18T10:00:00"
    elif corruption == "bad_stage": dto["states"][SYMBOL]["current_stage"] = "unknown"
    elif corruption == "missing_config": del dto["config"]["include_fees"]
    elif corruption == "schema": dto["schema"] = 2
    with pytest.raises(ValueError):
        decode_protection(dto, clock=lambda: NOW)


def test_naive_legacy_export_is_explicit_kst_and_decode_utc_clock_is_equivalent(tmp_path):
    original = manager(tmp_path)
    original.register_position(position(10))
    original._entry_times[SYMBOL] = NOW.replace(tzinfo=None)
    original.get_state(SYMBOL).pending_since = NOW.replace(tzinfo=None)
    dto = encode_protection(original)
    assert dto["entry_times"][SYMBOL] == "2026-09-18T10:00:00+09:00"
    restored = decode_protection(dto, clock=lambda: NOW.astimezone(ZoneInfo("UTC")))
    assert restored._entry_times[SYMBOL] == NOW


def test_same_entry_partial_does_not_reset_existing_protection(tmp_path):
    dto = initial(tmp_path)
    row = dto["states"][SYMBOL]
    row.update(current_stage="first", breakeven_activated=True, highest_price="14000",
               initial_risk_amount="20000", actual_stop_pct="5", pending_stage="second",
               pending_since=NOW.isoformat(), pending_target_qty=10, pending_filled_qty=2)
    dto["pending_owners"][SYMBOL] = "sell-intent"
    before = deepcopy(dto)
    updated, status = reduce(dto, position(40), position(100, "10600"), 60, 100)
    assert status == "ready" and dto == before
    changed = updated["states"][SYMBOL]
    assert changed["remaining_quantity"] == changed["original_quantity"] == changed["initial_quantity"] == 100
    assert changed["entry_price"] == "10600"
    for name in ("current_stage", "breakeven_activated", "highest_price", "pending_stage", "pending_since", "pending_target_qty", "pending_filled_qty", "initial_risk_amount", "actual_stop_pct"):
        assert changed[name] == row[name]
    assert updated["entry_times"] == dto["entry_times"]


def test_addon_accumulates_small_fills_then_resets_only_once(tmp_path):
    dto = initial(tmp_path, 100)
    dto["states"][SYMBOL].update(current_stage="second", breakeven_activated=True,
                                highest_price="15000", initial_risk_amount="50000", actual_stop_pct="5")
    dto, status = reduce(dto, position(100), position(104), 4, 4, order="addon", kind="distinct_add_on", intent="addon-intent")
    assert status == "ready" and dto["states"][SYMBOL]["current_stage"] == "second"
    dto, status = reduce(dto, position(104), position(110), 6, 10, order="addon", intent="addon-intent")
    assert status == "ready" and dto["states"][SYMBOL]["current_stage"] == "none"
    assert dto["states"][SYMBOL]["breakeven_activated"] is False
    dto["states"][SYMBOL].update(current_stage="first", breakeven_activated=True)
    dto, status = reduce(dto, position(110), position(120), 10, 20, order="addon", intent="addon-intent")
    assert status == "ready" and dto["states"][SYMBOL]["current_stage"] == "first"
    assert dto["states"][SYMBOL]["breakeven_activated"] is True
    assert dto["states"][SYMBOL]["highest_price"] == "15000"
    assert dto["states"][SYMBOL]["initial_risk_amount"] == "50000"


@pytest.mark.parametrize("owner", [None, "other", "sell-intent"])
def test_sell_delta_only_advances_matching_pending_owner(tmp_path, owner):
    dto = initial(tmp_path, 100)
    dto["states"][SYMBOL].update(pending_stage="first", pending_since=NOW.isoformat(),
                                pending_target_qty=10, pending_filled_qty=0)
    if owner is not None: dto["pending_owners"][SYMBOL] = owner
    dto, status = reduce(dto, position(100), position(96), 4, 4, order="sell", side="SELL", intent="sell-intent")
    assert status == "ready" and dto["states"][SYMBOL]["remaining_quantity"] == 96
    assert dto["states"][SYMBOL]["pending_filled_qty"] == (4 if owner == "sell-intent" else 0)
    dto, status = reduce(dto, position(96), position(90), 6, 10, order="sell", side="SELL", intent="sell-intent")
    assert dto["states"][SYMBOL]["current_stage"] == ("first" if owner == "sell-intent" else "none")
    assert (SYMBOL not in dto["pending_owners"]) if owner == "sell-intent" else True


def test_missing_protection_degrades_and_later_fill_cannot_fake_recovery(tmp_path):
    dto = initial(tmp_path)
    del dto["states"][SYMBOL]
    dto, status = reduce(dto, position(40), position(50), 10, 50)
    assert status == "degraded" and dto["degraded"][SYMBOL]["quantity"] == 50
    dto, status = reduce(dto, position(50), position(60), 10, 60)
    assert status == "degraded" and dto["degraded"][SYMBOL]["quantity"] == 60
    assert SYMBOL not in dto["states"]


def test_registration_failure_keeps_diagnostic_state_and_confirmed_quantity(tmp_path, monkeypatch):
    dto = encode_protection(manager(tmp_path))
    def fail(*args, **kwargs): raise RuntimeError("private detail")
    monkeypatch.setattr(ExitManager, "register_position", fail)
    updated, status = reduce(dto, None, position(40), 40, 40, kind="initial_entry")
    assert status == "degraded" and updated["degraded"][SYMBOL]["quantity"] == 40
    assert "private detail" not in str(updated)
    assert not dto["degraded"]


def test_exemption_survives_fills_and_full_close_clears_owned_protection(tmp_path):
    em = manager(tmp_path)
    em.add_exit_exempt(SYMBOL)
    dto, status = reduce(encode_protection(em), None, position(10), 10, 10, kind="initial_entry")
    assert status == "exempt" and dto["states"][SYMBOL]["remaining_quantity"] == 10
    dto, status = reduce(dto, position(10), None, 10, 10, order="sell", side="SELL")
    assert SYMBOL not in dto["states"] and SYMBOL not in dto["degraded"]
    assert SYMBOL in dto["exit_exempt"]


def test_quote_cannot_expire_pending_or_emit_competing_stop(tmp_path):
    dto = initial(tmp_path, 100)
    row = dto["states"][SYMBOL]
    row.update(pending_stage="first", pending_since=(NOW - timedelta(hours=2)).isoformat(),
               pending_target_qty=10, pending_filled_qty=3)
    dto["pending_owners"][SYMBOL] = "original-sell"
    for price in (Decimal("12000"), Decimal("9000")):
        result, decision = quote_protection(dto, symbol=SYMBOL, price=price, now=NOW, intent_id="other")
        assert decision is None
        for name in ("pending_stage", "pending_since", "pending_target_qty", "pending_filled_qty"):
            assert result["states"][SYMBOL][name] == row[name]
        assert result["pending_owners"][SYMBOL] == "original-sell"


def test_new_partial_exit_requires_explicit_intent_owner_without_mutating_input(tmp_path):
    dto = initial(tmp_path, 100)
    original = deepcopy(dto)
    with pytest.raises(ValueError):
        quote_protection(dto, symbol=SYMBOL, price=Decimal("11200"), now=NOW)
    assert dto == original
    updated, decision = quote_protection(dto, symbol=SYMBOL, price=Decimal("11200"), now=NOW, intent_id="take-profit")
    assert decision[0:2] == ("sell_partial", 10)
    assert updated["pending_owners"][SYMBOL] == "take-profit"
    assert updated["states"][SYMBOL]["highest_price"] == "11200"


def test_publish_validates_before_assign_and_preserves_live_io_dependencies(tmp_path):
    live = manager(tmp_path)
    clock, path, verifier = live._clock, live._stage_file, live._pending_verifier
    dto = initial(tmp_path)
    bad = deepcopy(dto)
    del bad["states"][SYMBOL]["current_stage"]
    with pytest.raises(ValueError): publish_protection(live, bad, clock=lambda: NOW)
    assert live.get_state(SYMBOL) is None
    publish_protection(live, dto, clock=lambda: NOW)
    assert live.get_state(SYMBOL).remaining_quantity == 40
    assert live._clock is clock and live._stage_file == path and live._pending_verifier is verifier
    dto["states"][SYMBOL]["remaining_quantity"] = 999
    assert live.get_state(SYMBOL).remaining_quantity == 40


@pytest.mark.parametrize("corruption", ["negative_quantity", "bad_bool", "config_nan", "invalid_order", "degraded_bool", "owner_whitespace"])
def test_extension_and_config_validation_cannot_install_corrupt_control(tmp_path, corruption):
    dto = initial(tmp_path)
    if corruption == "negative_quantity": dto["states"][SYMBOL]["initial_quantity"] = -1
    elif corruption == "bad_bool": dto["states"][SYMBOL]["breakeven_activated"] = 1
    elif corruption == "config_nan": dto["config"]["stop_loss_pct"] = float("nan")
    elif corruption == "invalid_order": next(iter(dto["orders"].values()))["reset_applied"] = 1
    elif corruption == "degraded_bool": dto["degraded"][SYMBOL] = {"quantity": True, "reason": "failure"}
    elif corruption == "owner_whitespace": dto["pending_owners"][SYMBOL] = " owner"
    with pytest.raises(ValueError): decode_protection(dto, clock=lambda: NOW)


def test_sell_protection_mismatch_never_uses_legacy_silent_clamp(tmp_path):
    dto = initial(tmp_path, 100)
    dto["states"][SYMBOL]["remaining_quantity"] = 3
    updated, status = reduce(dto, position(100), position(90), 10, 10, order="sell", side="SELL")
    assert status == "degraded"
    assert updated["degraded"][SYMBOL]["quantity"] == 90
    assert updated["states"][SYMBOL] == dto["states"][SYMBOL]


def test_initial_registration_whitelist_failure_never_fabricates_ready_defaults(tmp_path):
    dto, status = reduce(encode_protection(manager(tmp_path)), None, position(10), 10, 10,
                         kind="initial_entry", params={"price_history": {"close": [1]}})
    assert status == "degraded" and SYMBOL not in dto["states"]


def test_valid_registration_parameters_use_real_stop_and_core_policy(tmp_path):
    params = {"is_core": True, "stop_loss_pct": 10.0, "trailing_stop_pct": 12.0,
              "max_holding_days": 0, "strategy_name": "core_holding", "first_exit_ratio": 0.0}
    dto, status = reduce(encode_protection(manager(tmp_path)), None, position(10), 10, 10,
                         kind="initial_entry", params=params)
    assert status == "ready"
    row = dto["states"][SYMBOL]
    assert row["is_core"] and row["stop_loss_pct"] == 10.0
    assert row["max_holding_days"] == 0 and row["first_exit_ratio"] == 0.0


def test_order_kind_conflict_cannot_reclassify_initial_as_addon(tmp_path):
    dto = initial(tmp_path)
    updated, status = reduce(dto, position(40), position(50), 10, 50, kind="distinct_add_on")
    assert status == "degraded"
    assert updated["states"][SYMBOL]["remaining_quantity"] == 40


def test_pending_suppressed_stop_does_not_append_phantom_exit_history(tmp_path):
    dto = initial(tmp_path, 100)
    dto["states"][SYMBOL].update(pending_stage="first", pending_since=NOW.isoformat(),
                                pending_target_qty=10, pending_filled_qty=0)
    updated, decision = quote_protection(dto, symbol=SYMBOL, price=Decimal("9000"), now=NOW)
    assert decision is None
    assert updated["states"][SYMBOL]["exit_history"] == dto["states"][SYMBOL]["exit_history"]


def test_full_sell_calculation_failure_does_not_leave_nonexistent_holding_degraded(tmp_path, monkeypatch):
    dto = initial(tmp_path, 10)
    def fail(*args, **kwargs): raise RuntimeError("failed real calculation")
    monkeypatch.setattr(ExitManager, "on_fill", fail)
    updated, status = reduce(dto, position(10), None, 10, 10, order="sell", side="SELL")
    # 경제 보유0 확정 뒤에는 보호할 잔량이 없다. 계산 실패 진단은 후속 outbox 범위다.
    assert status == "ready" and SYMBOL not in updated["degraded"]
    assert SYMBOL not in updated["states"] and SYMBOL not in updated["entry_times"]


def test_full_sell_preserves_order_watermark_and_removes_protection(tmp_path):
    dto = initial(tmp_path, 10)
    updated, status = reduce(dto, position(10), None, 10, 10, order="sell", side="SELL")
    key = observation(10, order="sell", side="SELL").order_key
    assert status == "ready" and updated["orders"][key]["cumulative_quantity"] == 10
    assert SYMBOL not in updated["states"]


def test_buy_cannot_be_classified_as_exit(tmp_path):
    dto = initial(tmp_path)
    updated, status = reduce(dto, position(40), position(50), 10, 50, kind="exit")
    assert status == "degraded" and updated["states"][SYMBOL]["remaining_quantity"] == 40


def test_publication_refuses_different_market_before_changing_live_state(tmp_path):
    live = ExitManager(market="US", persist=False, state_dir=tmp_path, clock=lambda: NOW)
    with pytest.raises(ValueError):
        publish_protection(live, initial(tmp_path), clock=lambda: NOW)
    assert live.market == "US" and live.get_state(SYMBOL) is None


def test_quote_high_and_breakeven_survive_later_fill_and_utc_clock(tmp_path):
    dto = initial(tmp_path)
    dto["states"][SYMBOL]["current_stage"] = "first"
    dto, decision = quote_protection(dto, symbol=SYMBOL, price=Decimal("10600"),
                                     now=NOW.astimezone(ZoneInfo("UTC")))
    assert dto["states"][SYMBOL]["highest_price"] == "10600"
    assert dto["states"][SYMBOL]["breakeven_activated"] is True
    dto, status = reduce(dto, position(40), position(100), 60, 100)
    assert status == "ready" and dto["states"][SYMBOL]["highest_price"] == "10600"
    assert dto["states"][SYMBOL]["breakeven_activated"] is True


def test_degraded_full_close_clears_only_closed_symbol_and_retains_exemption(tmp_path):
    dto = initial(tmp_path)
    dto["degraded"] = {SYMBOL: {"quantity": 40, "reason": "unknown"},
                       "000660": {"quantity": 10, "reason": "unknown"}}
    dto["exit_exempt"] = [SYMBOL]
    dto, status = reduce(dto, position(40), None, 40, 40, order="sell", side="SELL")
    assert status == "exempt" and SYMBOL not in dto["states"]
    assert dto["degraded"] == {"000660": {"quantity": 10, "reason": "unknown"}}
    assert dto["exit_exempt"] == [SYMBOL]


def test_publish_preserves_live_exemption_set_identity_for_risk_reader(tmp_path):
    live = manager(tmp_path)
    risk_reference = live._exit_exempt
    dto = initial(tmp_path)
    dto["exit_exempt"] = [SYMBOL]
    publish_protection(live, dto, clock=lambda: NOW)
    assert risk_reference is live._exit_exempt and SYMBOL in risk_reference
    live.remove_exit_exempt(SYMBOL)
    assert SYMBOL not in risk_reference
    live.add_exit_exempt("000660")
    assert "000660" in risk_reference


def test_distinct_addon_after_partial_sell_restores_legacy_original_and_high(tmp_path):
    dto = initial(tmp_path, 100)
    dto, status = reduce(dto, position(100), position(40), 60, 60,
                         order="partial-sell", side="SELL")
    assert status == "ready" and dto["states"][SYMBOL]["original_quantity"] == 100
    dto["states"][SYMBOL].update(current_stage="first", highest_price="10500")
    addon = observation(4, order="addon", price="11000")
    after = position(44)
    after.current_price = Decimal("11000")
    updated, status = reduce_protection(dto, before=position(40), after=after,
                                        observation=addon, delta=FillDelta(4, Decimal("44000"), Decimal("0")),
                                        fill_kind="distinct_add_on", intent_id="addon", now=NOW)
    assert status == "ready"
    row = updated["states"][SYMBOL]
    assert row["original_quantity"] == row["remaining_quantity"] == 44
    assert row["highest_price"] == "11000"
    assert row["current_stage"] == "none"


@pytest.mark.parametrize("degraded", [False, True])
def test_normal_state_requires_entry_time_but_degraded_diagnostic_may_lack_it(tmp_path, degraded):
    dto = initial(tmp_path)
    del dto["entry_times"][SYMBOL]
    if degraded:
        dto["degraded"][SYMBOL] = {"quantity": 40, "reason": "missing_entry_time"}
        restored = decode_protection(dto, clock=lambda: NOW)
        assert encode_protection(restored) == dto
        unchanged, decision = quote_protection(dto, symbol=SYMBOL, price=Decimal("12000"), now=NOW)
        assert decision is None and unchanged == dto
    else:
        with pytest.raises(ValueError):
            decode_protection(dto, clock=lambda: NOW)


def test_missing_entry_time_cannot_be_published_over_live_protection(tmp_path):
    live = manager(tmp_path)
    live.register_position(position(20))
    dto = initial(tmp_path)
    del dto["entry_times"][SYMBOL]
    with pytest.raises(ValueError):
        publish_protection(live, dto, clock=lambda: NOW)
    assert live.get_state(SYMBOL).remaining_quantity == 20
    assert live._entry_times[SYMBOL] == NOW
