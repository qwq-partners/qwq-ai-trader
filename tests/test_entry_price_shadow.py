"""가격 조건의 비용·시점·결측 경계. 모든 자료는 합성 입력이다."""
from copy import deepcopy
from decimal import Decimal
import importlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)) if str(ROOT) not in sys.path else None


def module():
    # 최초 RED가 수집 오류가 아니라 미구현 기능의 실패로 남는다.
    try:
        return importlib.import_module("src.analytics.entry_price_shadow")
    except ModuleNotFoundError:
        pytest.fail("주문 없는 가격 조건 계산기가 아직 구현되지 않았다")


def payload():
    return {
        "schema_version": 1,
        "dataset_kind": "synthetic",
        "evaluation_epoch": "test-v1",
        "as_of": "2026-09-30T16:00:00+09:00",
        "fee_model_ref": "synthetic-explicit-zero-fees",
        "fees": {"buy_commission_rate": "0", "sell_commission_rate": "0", "sell_tax_rate": "0"},
        "policy": {"max_quote_age_seconds": 5, "max_decision_delay_seconds": 5,
                   "entry_slippage_bps": "0", "exit_slippage_bps": "0"},
        "opportunities": [{
            "opportunity_id": "one", "symbol": "SYNTH1", "evaluation_epoch": "test-v1",
            "route_origin": "live_screening", "strategy": "gap_and_go",
            "decision_at": "2026-09-30T10:00:00+09:00",
            "baseline_basis": "order_free_policy", "baseline_eligible": True,
            "quantity": 100, "capital_budget": "1100000",
            "target": {"price": "11000", "basis": "intraday_high_at_decision",
                       "as_of": "2026-09-30T10:00:00+09:00", "available_at": "2026-09-30T10:00:00+09:00"},
            "stop": {"pct": "5", "basis": "net_pnl", "policy_ref": "synthetic-fixed-net5",
                     "as_of": "2026-09-30T10:00:00+09:00", "available_at": "2026-09-30T10:00:00+09:00"},
            "quote": {"ask": "10000", "bid": "9990", "ask_size": 100,
                      "as_of": "2026-09-30T10:00:01+09:00", "received_at": "2026-09-30T10:00:02+09:00",
                      "session": "KRX_REGULAR_CONTINUOUS"},
            "outcome": {"basis": "common_policy_exit_proxy", "exit_policy_ref": "synthetic-fixed-net5",
                        "legs": [{"at": "2026-09-30T11:00:00+09:00", "price": "9500", "quantity": 100}]},
        }],
    }


def report(data=None):
    return module().build_report(payload() if data is None else data)


def row(data=None):
    return report(data)["opportunities"][0]


def test_fee_free_cap_and_same_quantity_pair():
    r = row()
    assert r["gate_status"] == "allow"
    assert Decimal(r["net_rr"]) == 2
    assert Decimal(r["planned_net_loss"]) == 50000
    assert Decimal(r["max_ask_whole_krw"]) == 10232
    assert r["a_quantity"] == r["b_quantity"] == 100
    assert Decimal(r["a_net_pnl"]) == Decimal(r["b_net_pnl"]) == -50000
    assert Decimal(r["delta_net_pnl"]) == 0
    for ask, status in [("10232", "allow"), ("10233", "cash")]:
        d = payload()
        d["opportunities"][0]["quote"]["ask"] = ask
        assert row(d)["gate_status"] == status


def test_net_stop_does_not_pay_round_trip_costs_twice():
    d = payload()
    d["fees"] = {"buy_commission_rate": "0.000140527", "sell_commission_rate": "0.000130527", "sell_tax_rate": "0.002"}
    r = row(d)
    # 매수 1,000,000 + 매수비용 141의 5%; 매도비용은 net 손절에 이미 포함된다.
    assert Decimal(r["planned_net_loss"]) == Decimal("50007.05")
    assert Decimal(r["target_net_gain"]) == 97515
    assert Decimal(r["a_net_pnl"]) == -52165


def test_rejected_entry_counts_avoided_loss_and_missed_profit():
    d = payload()
    d["opportunities"][0]["target"]["price"] = "10200"
    loss = row(d)
    assert loss["gate_status"] == "cash"
    assert loss["a_quantity"] == 100 and loss["b_quantity"] == 0
    assert Decimal(loss["b_net_pnl"]) == 0
    assert Decimal(loss["delta_net_pnl"]) == 50000
    d["opportunities"][0]["outcome"]["legs"][0]["price"] = "11000"
    winner = row(d)
    assert winner["gate_status"] == loss["gate_status"]
    assert winner["max_ask_whole_krw"] == loss["max_ask_whole_krw"]
    assert Decimal(winner["delta_net_pnl"]) == -100000


@pytest.mark.parametrize("part,field,value", [
    ("target", "price", None), ("target", "price", "NaN"),
    ("target", "price", "Infinity"), ("target", "price", True),
    ("target", "basis", "future_daily_high"),
    ("target", "as_of", "2026-09-30T10:00:01+09:00"),
    ("target", "available_at", "2026-09-30T10:00:01+09:00"),
    ("target", "as_of", "2026-09-29T10:00:00+09:00"),
    ("stop", "basis", "gross_price"), ("stop", "pct", "0"),
    ("stop", "pct", "100"), ("stop", "policy_ref", ""),
    ("stop", "available_at", "2026-09-30T10:00:01+09:00"),
    ("quote", "ask", "0"), ("quote", "bid", "10001"),
    ("quote", "ask_size", 99), ("quote", "ask_size", None),
    ("quote", "session", "VI"),
    ("quote", "as_of", "2026-09-30T09:59:59+09:00"),
    ("quote", "as_of", "2026-09-30T10:00:03+09:00"),
    ("quote", "received_at", "2026-09-30T10:00:07+09:00"),
    ("quote", "as_of", "2026-09-30T10:00:01"),
])
def test_invalid_or_unavailable_evidence_is_unknown_not_cash(part, field, value):
    d = payload()
    d["opportunities"][0][part][field] = value
    result = report(d)
    r = result["opportunities"][0]
    assert r["gate_status"] == "unknown"
    assert r["b_net_pnl"] is None and r["delta_net_pnl"] is None
    assert result["counts"]["unknown"] == 1
    assert result["performance_status"] == "incomplete"


@pytest.mark.parametrize("key,value", [("quantity", 0), ("quantity", True), ("quantity", 1.5),
                                     ("capital_budget", "999999"), ("baseline_eligible", None),
                                     ("baseline_basis", "kill_switch_blocked"),
                                     ("evaluation_epoch", "old-version")])
def test_invalid_common_contract_is_unknown(key, value):
    d = payload()
    d["opportunities"][0][key] = value
    assert row(d)["gate_status"] == "unknown"


def test_known_baseline_cash_and_out_of_scope_are_not_price_rejections():
    d = payload()
    d["opportunities"][0]["baseline_eligible"] = False
    r = row(d)
    assert r["gate_status"] == "baseline_cash"
    assert r["a_quantity"] == r["b_quantity"] == 0
    assert Decimal(r["delta_net_pnl"]) == 0
    d["opportunities"][0]["route_origin"] = "batch"
    assert row(d)["gate_status"] == "out_of_scope"


@pytest.mark.parametrize("field,value", [("route_origin", None), ("route_origin", ""),
                                        ("strategy", None), ("strategy", [])])
def test_missing_route_is_unknown_and_stays_in_denominator(field, value):
    d = payload()
    d["opportunities"][0][field] = value
    result = report(d)
    assert result["opportunities"][0]["gate_status"] == "unknown"
    assert result["counts"]["in_scope"] == 1


def test_missing_outcome_never_becomes_zero_return():
    d = payload()
    d["opportunities"][0].pop("outcome")
    r = row(d)
    assert r["gate_status"] == "allow"
    assert r["a_net_pnl"] is None and r["b_net_pnl"] is None
    assert report(d)["summary"]["complete_delta_net_pnl"] is None


def test_partial_exits_use_common_quantity_and_fee_rounding_per_leg():
    d = payload()
    d["fees"] = {"buy_commission_rate": "0.000140527", "sell_commission_rate": "0.000130527", "sell_tax_rate": "0.002"}
    d["opportunities"][0]["outcome"]["legs"] = [
        {"at": "2026-09-30T11:00:00+09:00", "price": "10500", "quantity": 40},
        {"at": "2026-09-30T12:00:00+09:00", "price": "11000", "quantity": 60},
    ]
    assert Decimal(row(d)["a_net_pnl"]) == 77558  # 80,000 - 141 - 895 - 1,406


@pytest.mark.parametrize("mutation", ["partial", "oversell", "early", "future", "mixed_policy", "actual_fill"])
def test_bad_exit_evidence_does_not_change_gate_or_create_pnl(mutation):
    d = payload()
    outcome = d["opportunities"][0]["outcome"]
    if mutation == "partial": outcome["legs"][0]["quantity"] = 99
    if mutation == "oversell": outcome["legs"][0]["quantity"] = 101
    if mutation == "early": outcome["legs"][0]["at"] = "2026-09-30T09:59:00+09:00"
    if mutation == "future": outcome["legs"][0]["at"] = "2026-10-01T11:00:00+09:00"
    if mutation == "mixed_policy": outcome["exit_policy_ref"] = "different-policy"
    if mutation == "actual_fill": outcome["basis"] = "actual_fill"
    r = row(d)
    assert r["gate_status"] == "allow"
    assert r["a_net_pnl"] is None and r["delta_net_pnl"] is None
    assert r["outcome_status"] == "unknown"


def test_slippage_reduces_cap_and_net_outcome():
    d = payload()
    d["policy"].update(entry_slippage_bps="10", exit_slippage_bps="30")
    r = row(d)
    assert Decimal(r["entry_price_proxy"]) == 10010
    assert Decimal(r["target_net_gain"]) == 95700
    assert Decimal(r["planned_net_loss"]) == 50050
    assert Decimal(r["a_net_pnl"]) == -53850
    assert Decimal(r["max_ask_whole_krw"]) < 10232


def test_all_rows_remain_in_denominator_and_incomplete_total_is_null():
    d = payload()
    second = deepcopy(d["opportunities"][0])
    second.update(opportunity_id="two", symbol="SYNTH2")
    second["target"]["price"] = None
    d["opportunities"].append(second)
    r = report(d)
    assert r["counts"]["total"] == r["counts"]["in_scope"] == 2
    assert r["counts"]["paired_outcomes"] == 1
    assert r["summary"]["complete_delta_net_pnl"] is None
    assert r["production_eligible"] is False and r["account_return"] is None


def test_duplicate_id_or_economic_opportunity_fails_dataset():
    for same_id in (True, False):
        d = payload()
        second = deepcopy(d["opportunities"][0])
        if not same_id: second["opportunity_id"] = "other-id"
        d["opportunities"].append(second)
        with pytest.raises(ValueError, match="중복"):
            report(d)


def test_whitespace_does_not_hide_duplicate_economic_opportunity():
    d = payload()
    second = deepcopy(d["opportunities"][0])
    second.update(opportunity_id="other-id", route_origin=" live_screening ", strategy=" gap_and_go ")
    d["opportunities"].append(second)
    with pytest.raises(ValueError, match="중복"):
        report(d)


def test_intraday_high_must_cover_the_decision_instant():
    d = payload()
    d["opportunities"][0]["target"].update(
        as_of="2026-09-30T09:00:00+09:00", available_at="2026-09-30T09:00:00+09:00")
    r = row(d)
    assert r["gate_status"] == "unknown"
    assert r["reason_codes"] == ["NOT_KNOWN_THROUGH_DECISION:target"]
    assert r["a_net_pnl"] is None and r["delta_net_pnl"] is None


def test_serialization_is_deterministic_and_inputs_are_unchanged():
    d = payload()
    original = deepcopy(d)
    assert report(d) == report(d)
    assert d == original
    json.dumps(report(d), allow_nan=False)


def test_equivalent_aware_timestamps_and_zero_fees_are_valid():
    d = payload()
    o = d["opportunities"][0]
    o["decision_at"] = "2026-09-30T01:00:00Z"
    o["target"]["as_of"] = "2026-09-30T01:00:00Z"
    assert row(d)["gate_status"] == "allow"


@pytest.mark.parametrize("fees", [None, {}, {"buy_commission_rate":"NaN", "sell_commission_rate":"0", "sell_tax_rate":"0"}])
def test_missing_or_invalid_cost_model_is_not_free_trading(fees):
    d = payload()
    d["fees"] = fees
    with pytest.raises(ValueError): report(d)


def test_cli_writes_only_stdout_and_rejects_invalid_json(tmp_path, capsys):
    try:
        cli = importlib.import_module("scripts.compare_entry_price_shadow")
    except ModuleNotFoundError:
        pytest.fail("명시 입력 전용 CLI가 아직 구현되지 않았다")
    p = tmp_path / "input.json"
    p.write_text(json.dumps(payload()))
    before = p.read_bytes()
    assert cli.main(["--input", str(p)]) == 0
    r = json.loads(capsys.readouterr().out)
    assert r["production_eligible"] is False
    assert p.read_bytes() == before
    p.write_text('{"schema_version":NaN}')
    assert cli.main(["--input", str(p)]) == 2
    assert capsys.readouterr().out == ""
