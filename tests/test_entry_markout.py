"""사전 고정 시점 호가 평가. 합성 호가만 사용하며 주문/현행 청산 재생이 아니다."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from test_received_entry_shadow import received_payload, observed_bundle
from src.analytics.entry_price_shadow import build_report
from src.analytics.entry_observation import prepare_input


def policy():
    return {"version": "fixed-horizon-bid-v1", "policy_ref": "synthetic-10min-v1",
            "fixed_at": "2026-09-30T09:00:00+09:00", "horizon_seconds": 600,
            "max_delay_seconds": 5}


def markout_payload():
    p = received_payload()
    p.update(outcome_basis="fixed_horizon_bid_markout", markout_policy=policy())
    row = p["opportunities"][0]
    row.pop("outcome")
    row["target"]["price"] = "10200"  # 가격 조건 B는 현금. A 손익과 비교한다.
    row["markout_quote"] = {**deepcopy(row["quote"]), "quote_id": "mark-one",
                            "ask": "9510", "bid": "9500", "bid_size": 100,
                            "received_at": "2026-09-30T10:10:02+09:00",
                            "observed_at": "2026-09-30T10:10:03+09:00",
                            "selection_rule": "first_recorded_krx_at_or_after_horizon"}
    return p


def test_fixed_horizon_evaluates_avoided_loss_and_missed_profit_separately():
    p = markout_payload(); before = deepcopy(p)
    r = build_report(p); row = r["opportunities"][0]
    assert row["gate_status"] == "cash"
    assert row["outcome_status"] == "complete_markout_proxy"
    assert Decimal(row["a_net_pnl"]) == -50000
    assert Decimal(row["delta_net_pnl"]) == 50000
    assert row["evaluation_at"] == "2026-09-30T01:10:02+00:00"
    assert r["outcome_basis"] == "fixed_horizon_bid_markout"
    assert r["markout_policy"]["horizon_seconds"] == 600
    assert Decimal(r["summary"]["known_avoided_loss"]) == 50000
    assert Decimal(r["summary"]["known_missed_gain"]) == 0
    assert r["production_eligible"] is False and r["account_return"] is None
    assert p == before
    row = p["opportunities"][0]
    row["markout_quote"].update(ask="11010", bid="11000")
    r = build_report(p)
    assert Decimal(r["summary"]["known_missed_gain"]) == 100000
    assert Decimal(r["summary"]["complete_delta_net_pnl"]) == -100000


def test_markout_charges_fees_and_slippage_once_with_same_quantity():
    p = markout_payload()
    p["fees"] = {"buy_commission_rate": "0.000140527", "sell_commission_rate": "0.000130527", "sell_tax_rate": "0.002"}
    p["policy"].update(entry_slippage_bps=10, exit_slippage_bps=20)
    row = p["opportunities"][0]; row["target"]["price"] = "12000"
    r = build_report(p)["opportunities"][0]
    # 매수 1,001,000 + 141 / 매도 948,100 - 반올림(123.75+1,896.2)=2,020
    assert Decimal(r["a_net_pnl"]) == Decimal("-55061")
    assert r["a_quantity"] == r["b_quantity"] == 100
    assert Decimal(r["delta_net_pnl"]) == 0


@pytest.mark.parametrize("field,value", [
    ("bid_size", 99), ("bid_size", True), ("bid", "9520"), ("bid", "0"),
    ("tr_id", "H0NXASP0"), ("message_count", 2), ("source_as_of", "invented"),
    ("received_at", "2026-09-30T10:10:01+09:00"),
    ("observed_at", "2026-09-30T10:10:08+09:00"),
    ("received_at", "2026-09-30T10:10:02"),
    ("observed_at", "2026-09-30T16:00:01+09:00"),
    ("session", "VI"), ("session_evidence_ref", None), ("quote_id", "quote-one"),
    ("selection_rule", "best_later_bid"),
])
def test_bad_markout_keeps_entry_decision_but_never_creates_profit(field, value):
    p = markout_payload(); p["opportunities"][0]["markout_quote"][field] = value
    r = build_report(p)
    assert r["opportunities"][0]["gate_status"] == "cash"
    assert r["opportunities"][0]["outcome_status"] == "unknown"
    assert r["summary"]["complete_delta_net_pnl"] is None


@pytest.mark.parametrize("defect", ["late_freeze", "overnight", "unmatured", "missing_quote"])
def test_invalid_or_not_yet_mature_horizon_does_not_become_zero_pnl(defect):
    p = markout_payload()
    if defect == "late_freeze": p["markout_policy"]["fixed_at"] = "2026-09-30T10:00:01+09:00"
    if defect == "overnight": p["markout_policy"]["horizon_seconds"] = 86400
    if defect == "unmatured": p["as_of"] = "2026-09-30T10:09:00+09:00"
    if defect == "missing_quote": p["opportunities"][0].pop("markout_quote")
    r = build_report(p)
    assert r["summary"]["complete_delta_net_pnl"] is None
    assert r["opportunities"][0]["a_net_pnl"] is None


@pytest.mark.parametrize("defect", ["policy_missing", "zero_horizon", "bool_horizon", "negative_delay",
                                   "mixed_outcome", "strict_mode", "unknown_mode"])
def test_report_rejects_mixed_or_undefined_evaluation_contract(defect):
    p = markout_payload()
    if defect == "policy_missing": p.pop("markout_policy")
    if defect == "zero_horizon": p["markout_policy"]["horizon_seconds"] = 0
    if defect == "bool_horizon": p["markout_policy"]["horizon_seconds"] = True
    if defect == "negative_delay": p["markout_policy"]["max_delay_seconds"] = -1
    if defect == "mixed_outcome": p["opportunities"][0]["outcome"] = {"basis": "common_policy_exit_proxy"}
    if defect == "strict_mode": p["data_basis"] = "market_timestamp"
    if defect == "unknown_mode": p["outcome_basis"] = "choose_best"
    with pytest.raises(ValueError): build_report(p)


def markout_bundle():
    c, o, inputs = observed_bundle()
    c.update(outcome_basis="fixed_horizon_bid_markout", markout_policy=policy())
    o["records"][0]["candidates"].pop()  # 이 합성 사례의 원래 모집단은 1후보다.
    o["records"][1]["quote"]["high"] = "10200"
    q = deepcopy(o["records"][-1]); q.update(sequence=6, quote_id="mark-one", ask="9510", bid="9500", bid_size=100,
                                             observed_at="2026-09-30T10:10:03+09:00")
    q["provenance"]["received_at"] = "2026-09-30T10:10:02+09:00"
    o["records"].append(q)
    inputs[0].pop("exit_policy_ref")
    inputs[0]["markout_session"] = {"quote_id": "mark-one", "session": "KRX_REGULAR_CONTINUOUS",
                                    "evidence_ref": "synthetic-session"}
    return c, o, inputs


def test_native_assembly_selects_first_bid_and_requires_no_invented_exit_policy():
    args = markout_bundle(); before = deepcopy(args)
    r = prepare_input(*args)
    assert r["payload"]["opportunities"][0]["markout_quote"]["quote_id"] == "mark-one"
    assert Decimal(r["report"]["summary"]["complete_delta_net_pnl"]) == 50000
    assert args == before


@pytest.mark.parametrize("defect", ["first_size", "first_time", "bad_proof", "overflow", "outcome_override"])
def test_markout_assembly_does_not_replace_first_bad_quote_or_hide_loss(defect):
    c, o, i = markout_bundle()
    q = deepcopy(o["records"][-1]); q.update(quote_id="later-better", sequence=7)
    o["records"].append(q)
    if defect == "first_size": o["records"][-2]["bid_size"] = 1
    if defect == "first_time": o["records"][-2]["provenance"]["received_at"] = "bad-time"
    if defect == "bad_proof": i[0]["markout_session"]["quote_id"] = "later-better"
    if defect == "overflow": o.update(complete=False, dropped_records=1)
    if defect == "outcome_override":
        i[0]["outcome"] = {"basis": "common_policy_exit_proxy"}
        with pytest.raises(ValueError): prepare_input(c, o, i)
        return
    result = prepare_input(c, o, i)
    assert result["report"]["summary"]["complete_delta_net_pnl"] is None


def test_markout_cli_runs_only_explicit_synthetic_input(tmp_path):
    c, o, i = markout_bundle(); p = tmp_path / "markout.json"
    p.write_text(json.dumps({"context": c, "observations": o, "evaluation_inputs": i}))
    before = p.read_bytes()
    r = subprocess.run([sys.executable, str(ROOT / "scripts/compare_entry_price_shadow.py"), "--observations", str(p)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert Decimal(out["report"]["summary"]["complete_delta_net_pnl"]) == 50000
    assert out["report"]["outcome_basis"] == "fixed_horizon_bid_markout"
    assert p.read_bytes() == before


def test_supported_session_end_is_exclusive_and_no_overnight_rollover():
    p = markout_payload()
    p['markout_policy']['horizon_seconds'] = 19198  # 10:00:02 + 5:19:58 = 15:20:00
    row = build_report(p)['opportunities'][0]
    assert row['outcome_reason'] == 'MARKOUT_OUTSIDE_SUPPORTED_SESSION'
    assert row['a_net_pnl'] is None


def test_unselected_candidate_stays_in_denominator_when_other_markout_is_complete():
    c, o, i = markout_bundle()
    o['records'][0]['candidates'].append({'candidate_id':'scan:MISSING', 'symbol':'MISSING'})
    r = prepare_input(c, o, i)['report']
    assert r['counts']['total'] == 2 and r['counts']['unknown'] == 1
    assert r['summary']['known_pair_delta_net_pnl'] == '50000'
    assert r['summary']['complete_delta_net_pnl'] is None


def test_markout_policy_cannot_silently_change_the_time_anchor():
    p = markout_payload(); p['markout_policy']['anchor'] = 'decision_at'
    with pytest.raises(ValueError): build_report(p)
