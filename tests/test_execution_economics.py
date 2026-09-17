"""실제 Portfolio/Decimal 비용을 사용하는 순수 경제 checkpoint 계약."""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import asyncio

from src.core.types import Portfolio, Position, PositionSide, Market, RiskMetrics
from src.risk.manager import DailyStats
from src.execution.safety.application import FillObservation, FillDelta
from src.execution.safety.application import FillApplicationCoordinator, FillReduction
from src.execution.safety.store import ExecutionStateStore
from src.execution.safety.economics import (
    encode_portfolio, decode_portfolio, new_risk_state, encode_risk,
    publish_risk, validate_risk, reduce_economics,
)

NOW = datetime(2026, 9, 17, 1, tzinfo=timezone.utc)
DAY = "2026-09-17"


def obs(quantity, amount, *, side="BUY", order="00001", metadata=None):
    return FillObservation("account-internal", "KR", DAY, "KRX", order, "005930", side,
                           quantity, Decimal(amount), metadata=metadata or {})


def add_attempt(state, observation, *, intent="intent-buy", quantity=100):
    ref = {"account_scope": "account-internal", "market": "KR", "order_date": DAY,
           "exchange": "KRX", "order_no": observation.order_id, "org_no": "", "parent_order_no": ""}
    attempt_id = observation.order_id
    state.setdefault("intents", {})[intent] = {"symbol": "005930", "side": observation.side.lower(),
                                              "target_quantity": quantity, "attempt_ids": [attempt_id]}
    state.setdefault("attempts", {})[attempt_id] = {
        "attempt_id": attempt_id, "intent_id": intent, "kind": "submit", "command": "submit",
        "state": "partial", "status": "partial", "order_ref": ref, "symbol": "005930",
        "side": observation.side.lower(), "strategy": "sepa_trend", "quantity": quantity,
        "observed_quantity": observation.cumulative_quantity,
        "observed_amount": str(observation.cumulative_amount), "applied_quantity": 0,
        "reserved_quantity": quantity, "reserved_cash": "1100000" if observation.side == "BUY" else "0",
    }
    return state


def baseline():
    return {"portfolio": encode_portfolio(Portfolio(cash=Decimal("2000000"), initial_capital=Decimal("2000000"))),
            "risk": new_risk_state(DAY), "lots": {}, "outbox": {},
            "startup_reconciliation": {"ready": False}}


def record_observation(state, observation):
    """테스트 입력의 선행 reconcile을 명시한다. 고정 최대대금을 만들지 않는다."""
    state = deepcopy(state)
    attempt = state["attempts"][observation.order_id]
    attempt["observed_quantity"] = observation.cumulative_quantity
    attempt["observed_amount"] = str(observation.cumulative_amount)
    return state


def step(state, observation, quantity, amount):
    result = reduce_economics(record_observation(state, observation), observation,
                             FillDelta(quantity, Decimal(amount), Decimal("0")), now=NOW)
    # ApplicationCoordinator가 소유한 applied cursor를 다음 순수 호출 입력에 제공한다.
    result.state.setdefault("cursors", {})[observation.order_key] = {
        "identity": observation.identity, "quantity": observation.cumulative_quantity,
        "amount": str(observation.cumulative_amount), "fee": "0"}
    return result


def test_portfolio_full_dto_roundtrip_and_naive_export_uses_kst():
    position = Position("005930", name="테스트", side=PositionSide.LONG, quantity=3,
                        avg_price=Decimal("10000"), current_price=Decimal("12000"),
                        stop_loss=Decimal("9500"), take_profit=Decimal("15000"),
                        trailing_stop_pct=3.0, highest_price=Decimal("13000"), strategy="sepa_trend",
                        entry_time=datetime(2026, 9, 17, 9), sector="반도체", trade_id="trade-one")
    position.entry_signal_score = 75.5
    dto = encode_portfolio(Portfolio(cash=Decimal("10"), positions={"005930": position}))
    assert dto["schema"] == 1
    assert dto["positions"]["005930"]["entry_time"] == "2026-09-17T09:00:00+09:00"
    decoded = decode_portfolio(dto)
    assert decoded.positions["005930"].highest_price == Decimal("13000")
    assert decoded.positions["005930"].entry_signal_score == 75.5
    assert encode_portfolio(decoded) == dto
    decoded.positions["005930"].quantity = 99
    assert dto["positions"]["005930"]["quantity"] == 3


@pytest.mark.parametrize("corruption", ["missing", "unknown", "cash_nan", "bool_count", "us", "position_bool", "position_identity", "naive_decode"])
def test_portfolio_dto_rejects_malformed_fields(corruption):
    dto = encode_portfolio(Portfolio(positions={"005930": Position("005930", quantity=1, avg_price=Decimal("10000"), entry_time=NOW)}))
    if corruption == "missing": del dto["cash"]
    elif corruption == "unknown": dto["unknown"] = 1
    elif corruption == "cash_nan": dto["cash"] = "NaN"
    elif corruption == "bool_count": dto["daily_trades"] = True
    elif corruption == "us": dto["currency"] = "USD"
    elif corruption == "position_bool": dto["positions"]["005930"]["quantity"] = True
    elif corruption == "position_identity": dto["positions"]["005930"]["symbol"] = "000660"
    elif corruption == "naive_decode": dto["positions"]["005930"]["entry_time"] = "2026-09-17T09:00:00"
    with pytest.raises(ValueError): decode_portfolio(dto)


def test_buy_and_sell_40_plus_60_cash_fees_pnl_and_scoped_count():
    state = add_attempt(baseline(), obs(40, "400000"))
    first = step(state, obs(40, "400000"), 40, "400000")
    assert first.fill_kind == "initial_entry"
    assert first.before_position is None and first.after_position.quantity == 40
    assert first.state["portfolio"]["cash"] == "1599944"
    second = step(first.state, obs(100, "1060000"), 60, "660000")
    assert second.fill_kind == "same_entry_fill"
    assert second.state["portfolio"]["cash"] == "939851"
    assert second.after_position.avg_price == Decimal("10600")
    assert second.state["portfolio"]["daily_trades"] == 1
    assert second.state["risk"]["daily_stats"]["trades"] == 1
    assert second.state["risk"]["buy_fee_remaining"]["005930"] == "149"
    assert second.state["attempts"]["00001"]["reserved_quantity"] == 0
    assert second.state["attempts"]["00001"]["reserved_cash"] == "0"
    sell = obs(40, "480000", side="SELL", order="00002")
    state = add_attempt(second.state, sell, intent="intent-sell")
    third = step(state, sell, 40, "480000")
    assert third.after_position.quantity == 60
    assert third.state["portfolio"]["cash"] == "1418828"
    assert Decimal(third.state["portfolio"]["daily_pnl"]) == Decimal("54917.4")
    assert Decimal(third.state["risk"]["buy_fee_remaining"]["005930"]) == Decimal("89.4")
    last = step(third.state, obs(100, "1200000", side="SELL", order="00002"), 60, "720000")
    assert last.after_position is None
    assert last.state["portfolio"]["positions"] == {}
    assert last.state["portfolio"]["cash"] == "2137294"
    assert Decimal(last.state["portfolio"]["daily_pnl"]) == Decimal("137294")
    assert last.state["risk"]["daily_stats"]["total_pnl"] == "0"
    assert len(last.state["outbox"]) == 4
    assert all(row["fee_basis"] == "estimated_order_cumulative" for row in last.state["outbox"].values())
    assert state["portfolio"]["cash"] == "939851"  # 후보 입력 변조 없음


@pytest.mark.parametrize("problem", ["oversell", "missing_basis", "day", "naive_now", "no_attempt", "duplicate_attempt", "observed_behind", "applied_ahead", "intent_missing", "nonzero_fee", "unknown_metadata"])
def test_invalid_economic_inputs_fail_without_mutating_state(problem):
    observation = obs(40, "400000")
    state = add_attempt(baseline(), observation)
    kwargs = {"now": NOW}
    if problem in ("oversell", "missing_basis"):
        observation = obs(40, "400000", side="SELL")
        state = add_attempt(baseline(), observation)
        qty = 20 if problem == "oversell" else 40
        state["portfolio"] = encode_portfolio(Portfolio(positions={"005930": Position("005930", quantity=qty, avg_price=Decimal("10000"))}))
    elif problem == "day": state["risk"]["day"] = "2026-09-16"
    elif problem == "naive_now": kwargs["now"] = NOW.replace(tzinfo=None)
    elif problem == "no_attempt": state["attempts"].clear()
    elif problem == "duplicate_attempt": state["attempts"]["duplicate"] = deepcopy(state["attempts"]["00001"])
    elif problem == "observed_behind": state["attempts"]["00001"]["observed_quantity"] = 20
    elif problem == "applied_ahead": state["attempts"]["00001"]["applied_quantity"] = 1
    elif problem == "intent_missing": state["intents"].clear()
    elif problem == "nonzero_fee":
        observation = FillObservation("account-internal", "KR", DAY, "KRX", "00001", "005930", "BUY", 40, Decimal("400000"), Decimal("1"))
    elif problem == "unknown_metadata": observation = obs(40, "400000", metadata={"approve_anything": True})
    original = deepcopy(state)
    with pytest.raises(ValueError):
        reduce_economics(state, observation, FillDelta(40, Decimal("400000"), Decimal("0")), **kwargs)
    assert state == original


def test_new_order_after_stop_consumes_one_use_but_partial_does_not_repeat_count():
    observation = obs(40, "400000")
    state = add_attempt(baseline(), observation)
    state["risk"]["stop_loss_today"] = ["005930"]
    result = step(state, observation, 40, "400000")
    assert result.state["risk"]["stop_loss_rebound_used"] == ["005930"]
    second = step(result.state, obs(100, "1000000"), 60, "600000")
    assert second.state["risk"]["stop_loss_rebound_used"] == ["005930"]
    assert second.state["portfolio"]["daily_trades"] == 1


def test_loss_exit_count_once_per_intent_and_full_exit_legacy_price_basis():
    state = add_attempt(baseline(), obs(100, "1000000"))
    state = step(state, obs(100, "1000000"), 100, "1000000").state
    metadata = {"exit_type": "stop_loss"}
    sell = obs(40, "360000", side="SELL", order="00002", metadata=metadata)
    state = add_attempt(state, sell, intent="stop-intent")
    first = step(state, sell, 40, "360000")
    assert first.state["risk"]["daily_exit_count"] == 1
    assert first.state["risk"]["stop_loss_today"] == ["005930"]
    assert first.state["risk"]["exited_today"] == {}
    final = step(first.state, obs(100, "840000", side="SELL", order="00002", metadata=metadata), 60, "480000")
    assert final.state["risk"]["daily_exit_count"] == 1
    assert final.state["risk"]["exited_today"]["005930"]["price"] == "8000"


def test_addon_lot_kind_stays_distinct_and_initial_r_stays_pending():
    state = step(add_attempt(baseline(), obs(100, "1000000")), obs(100, "1000000"), 100, "1000000").state
    addon = obs(5, "50000", order="00003")
    state = add_attempt(state, addon, intent="addon", quantity=20)
    first = step(state, addon, 5, "50000")
    second = step(first.state, obs(20, "200000", order="00003"), 15, "150000")
    assert first.fill_kind == second.fill_kind == "distinct_add_on"
    assert len(second.state["lots"]) == 2
    assert second.state["lots"][addon.order_key]["pre_buy_quantity"] == 100
    assert second.state["lots"][addon.order_key]["initial_r_status"] == "pending"


def manager():
    return SimpleNamespace(daily_stats=DailyStats(date=date(2026, 9, 17)), metrics=RiskMetrics(),
                           market="KR", _consecutive_losses=0, _stop_loss_today=set(),
                           _stop_loss_rebound_used=set(), _exited_today={}, _daily_exit_count=0,
                           _daily_exit_count_date=date(2026, 9, 17))


def test_risk_roundtrip_validates_before_publication_without_persistence():
    live = manager()
    live._stop_loss_today = {"005930"}
    dto = encode_risk(live, day=DAY)
    copy = validate_risk(dto)
    copy["stop_loss_today"].append("000660")
    assert dto["stop_loss_today"] == ["005930"]
    other = manager()
    publish_risk(other, dto)
    assert other._stop_loss_today == {"005930"}
    assert encode_risk(other, day=DAY) == dto
    bad = deepcopy(dto)
    bad["daily_stats"]["trades"] = True
    with pytest.raises(ValueError): publish_risk(other, bad)
    assert encode_risk(other, day=DAY) == dto
    with pytest.raises(ValueError): encode_risk(other, day="2026-09-18")


def test_decode_requires_iso_string_not_datetime_object():
    dto = encode_portfolio(Portfolio(positions={"005930": Position("005930", entry_time=NOW)}))
    dto["positions"]["005930"]["entry_time"] = NOW
    with pytest.raises(ValueError): decode_portfolio(dto)


def test_delta_requires_finite_decimal_not_float_even_if_numerically_equal():
    observation = obs(40, "400000")
    with pytest.raises(ValueError):
        reduce_economics(add_attempt(baseline(), observation), observation,
                         FillDelta(40, 400000.0, Decimal("0")), now=NOW)


def test_risk_publication_uses_explicit_kst_legacy_clock_projection():
    risk = new_risk_state(DAY)
    risk["exited_today"]["005930"] = {"price": "8000", "time": NOW.isoformat(), "sector": ""}
    live = manager()
    publish_risk(live, risk)
    assert live._exited_today["005930"]["time"] == "2026-09-17T10:00:00"
    restored = encode_risk(live, day=DAY)
    assert datetime.fromisoformat(restored["exited_today"]["005930"]["time"]) == NOW


def test_closed_lot_late_partial_cannot_recreate_position():
    first = obs(40, "400000")
    state = step(add_attempt(baseline(), first), first, 40, "400000").state
    sell = obs(40, "400000", side="SELL", order="00002")
    state = step(add_attempt(state, sell, intent="exit", quantity=40), sell, 40, "400000").state
    with pytest.raises(ValueError):
        step(state, obs(100, "1000000"), 60, "600000")


def test_unknown_legacy_buy_fee_is_not_made_known_by_addon():
    state = baseline()
    state["portfolio"] = encode_portfolio(Portfolio(cash=Decimal("2000000"), positions={
        "005930": Position("005930", quantity=100, avg_price=Decimal("10000"))}))
    state["risk"]["cost_basis_remaining"]["005930"] = "1000000"
    observation = obs(10, "100000")
    state = step(add_attempt(state, observation, quantity=10), observation, 10, "100000").state
    assert "005930" not in state["risk"]["buy_fee_remaining"]
    sell = obs(110, "1100000", side="SELL", order="00002")
    with pytest.raises(ValueError):
        step(add_attempt(state, sell, intent="exit", quantity=110), sell, 110, "1100000")


def test_coordinator_restart_replay_preserves_count_risk_and_cost_basis(tmp_path):
    async def scenario():
        path = tmp_path / "execution.sqlite3"
        def reducer(state, observation, delta):
            result = reduce_economics(state, observation, delta, now=NOW)
            return FillReduction(result.state, protection_status="degraded")
        observation = obs(40, "400000")
        state = add_attempt(baseline(), observation)
        state["risk"]["stop_loss_today"] = ["005930"]
        store = ExecutionStateStore(path)
        owner = FillApplicationCoordinator(store, lambda *_: None, reducer)
        await owner.restore()
        await owner.mutate("baseline", lambda _: state)
        assert (await owner.apply(observation)).status == "APPLIED"
        await store.close()
        store = ExecutionStateStore(path)
        owner = FillApplicationCoordinator(store, lambda *_: None, reducer)
        await owner.restore()
        assert (await owner.apply(observation)).status == "ALREADY_APPLIED"
        await owner.mutate("observed-100", lambda state: record_observation(state, obs(100, "1060000")))
        assert (await owner.apply(obs(100, "1060000"))).status == "APPLIED"
        assert owner.state["portfolio"]["cash"] == "939851"
        assert owner.state["portfolio"]["daily_trades"] == 1
        assert owner.state["risk"]["stop_loss_rebound_used"] == ["005930"]
        assert owner.state["risk"]["cost_basis_remaining"]["005930"] == "1060000"
        await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("problem", ["evidence_conflict", "count_divergence", "stale_cost_basis", "count_without_cursor"])
def test_cross_checkpoint_inconsistency_is_not_silently_repaired(problem):
    observation = obs(40, "400000")
    state = add_attempt(baseline(), observation)
    if problem == "evidence_conflict": state["attempts"]["00001"]["evidence_conflict"] = True
    elif problem == "count_divergence": state["risk"]["daily_stats"]["trades"] = 1
    elif problem == "stale_cost_basis": state["risk"]["cost_basis_remaining"]["000660"] = "100"
    elif problem == "count_without_cursor": state["risk"]["counted_buy_orders"] = [observation.order_key]
    with pytest.raises(ValueError): step(state, observation, 40, "400000")


def test_same_entry_continuation_after_partial_stop_does_not_consume_v_token():
    first = obs(40, "400000")
    state = step(add_attempt(baseline(), first), first, 40, "400000").state
    stop = obs(10, "90000", side="SELL", order="00002", metadata={"exit_type": "stop_loss"})
    state = step(add_attempt(state, stop, intent="partial-stop", quantity=10), stop, 10, "90000").state
    result = step(state, obs(100, "1000000"), 60, "600000")
    assert result.after_position.quantity == 90
    assert result.fill_kind == "same_entry_fill"
    assert result.state["risk"]["stop_loss_today"] == ["005930"]
    assert result.state["risk"]["stop_loss_rebound_used"] == []


def test_full_exit_remainder_consumes_exact_cost_basis_for_fractional_average():
    buy = obs(3, "100")
    state = step(add_attempt(baseline(), buy, quantity=3), buy, 3, "100").state
    sell = obs(1, "40", side="SELL", order="00002")
    state = step(add_attempt(state, sell, intent="exit", quantity=3), sell, 1, "40").state
    result = step(state, obs(3, "120", side="SELL", order="00002"), 2, "80")
    assert result.state["portfolio"]["cash"] == "2000020"
    assert Decimal(result.state["portfolio"]["daily_pnl"]) == Decimal("20")
    assert not result.state["risk"]["cost_basis_remaining"]


def test_existing_full_exit_symbol_restriction_does_not_count_second_intent():
    buy = obs(100, "1000000")
    state = step(add_attempt(baseline(), buy), buy, 100, "1000000").state
    state["risk"]["exited_today"]["005930"] = {"price": "10000", "time": NOW.isoformat(), "sector": ""}
    state["risk"]["daily_exit_count"] = 1
    sell = obs(100, "900000", side="SELL", order="00002", metadata={"exit_type": "stop_loss"})
    result = step(add_attempt(state, sell, intent="second-stop"), sell, 100, "900000")
    assert result.state["risk"]["daily_exit_count"] == 1
    assert result.state["risk"]["count_loss_intents"] == ["second-stop"]


@pytest.mark.parametrize("field,value", [("daily_exit_count", True), ("schema", True),
                                         ("stop_loss_today", ["005930", "005930"]),
                                         ("cost_basis_remaining", {"005930": "NaN"})])
def test_risk_schema_rejects_nonfinite_bool_and_duplicate_keys(field, value):
    state = new_risk_state(DAY)
    state[field] = value
    with pytest.raises(ValueError): validate_risk(state)


@pytest.mark.parametrize("stored_amount", ["399999", "400001"])
def test_equal_observed_quantity_requires_exact_amount(stored_amount):
    observation = obs(40, "400000")
    state = add_attempt(baseline(), observation)
    state["attempts"]["00001"]["observed_amount"] = stored_amount
    original = deepcopy(state)
    with pytest.raises(ValueError):
        reduce_economics(state, observation, FillDelta(40, Decimal("400000"), Decimal("0")), now=NOW)
    assert state == original


def test_later_larger_observation_can_precede_applying_earlier_consistent_fill():
    state = add_attempt(baseline(), obs(100, "1000000"))
    result = reduce_economics(state, obs(40, "400000"), FillDelta(40, Decimal("400000"), Decimal("0")), now=NOW)
    assert result.after_position.quantity == 40
    assert result.state["attempts"]["00001"]["applied_quantity"] == 40


@pytest.mark.parametrize("corruption", ["missing", "order_ref", "symbol", "attempt_id", "intent_id", "kind", "quantity", "amount", "lifecycle", "bool_quantity"])
def test_partial_buy_requires_original_lot_identity_and_cursor_totals(corruption):
    first = obs(40, "400000")
    state = step(add_attempt(baseline(), first), first, 40, "400000").state
    state["risk"]["stop_loss_today"] = ["005930"]
    lot = state["lots"][first.order_key]
    if corruption == "missing": del state["lots"][first.order_key]
    elif corruption == "order_ref": lot["order_ref"]["org_no"] = "different"
    elif corruption == "symbol": lot["symbol"] = "000660"
    elif corruption == "attempt_id": lot["attempt_id"] = "other"
    elif corruption == "intent_id": lot["intent_id"] = "other"
    elif corruption == "kind": lot["kind"] = "distinct_add_on"
    elif corruption == "quantity": lot["quantity"] = 39
    elif corruption == "amount": lot["amount"] = "400001"
    elif corruption == "lifecycle": lot["lifecycle_id"] = "other"
    elif corruption == "bool_quantity": lot["quantity"] = True
    second = obs(60, "600000")
    state = record_observation(state, second)
    original = deepcopy(state)
    with pytest.raises(ValueError):
        reduce_economics(state, second, FillDelta(20, Decimal("200000"), Decimal("0")), now=NOW)
    assert state == original
    assert state["risk"]["stop_loss_rebound_used"] == []


def test_missing_lot_after_restart_remains_failed_with_durable_inbox(tmp_path):
    async def scenario():
        path = tmp_path / "execution.sqlite3"
        first = obs(40, "400000")
        state = step(add_attempt(baseline(), first), first, 40, "400000").state
        del state["lots"][first.order_key]
        state["risk"]["stop_loss_today"] = ["005930"]
        second = obs(60, "600000")
        state = record_observation(state, second)
        store = ExecutionStateStore(path)
        await store.commit(0, state, "inconsistent-checkpoint")
        await store.close()
        store = ExecutionStateStore(path)
        def reducer(candidate, observation, delta):
            result = reduce_economics(candidate, observation, delta, now=NOW)
            return FillReduction(result.state)
        owner = FillApplicationCoordinator(store, lambda *_: None, reducer)
        await owner.restore()
        receipt = await owner.apply(second)
        assert receipt.status == "FAILED"
        assert owner.state["portfolio"] == state["portfolio"]
        assert owner.state["risk"] == state["risk"]
        assert owner.state["cursors"][first.order_key]["quantity"] == 40
        assert owner.state["attempts"]["00001"]["applied_quantity"] == 40
        assert owner.state["inbox"][second.observation_id]["status"] == "RECEIVED"
        await store.close()
    asyncio.run(scenario())
