"""Provided evidence consistency is never permission to replay historical fills."""

from copy import deepcopy
from hashlib import sha256
import json

import pytest

from src.execution.recovery_evidence import reconcile_execution_evidence


def evidence():
    order = {
        "session_id": "s1", "facts": {"local_id": "o1", "symbol": "005930",
        "side": "buy", "quantity": 10, "order_date": "20261002", "strategy": "gap",
        "reason": "entry", "partial_exit": False}, "odno": "000123", "orgno": "001",
        "status": "accepted", "observed_quantity": 10, "observed_average": "106",
        "terminal_quantity": 10, "executions": [
            {"execution_id": "s1:o1:4", "from_quantity": 0, "to_quantity": 4,
             "price": "100", "portfolio_applied": True, "handoff_returned": True},
            {"execution_id": "s1:o1:10", "from_quantity": 4, "to_quantity": 10,
             "price": "110", "portfolio_applied": True, "handoff_returned": True}]}
    ledger = {"format": "execution-ledger-export-v1", "schema_version": 1,
              "account_scope": "test-scope", "event_count": 10, "source_sha256": "a" * 64,
              "sessions": {"s1": {"clean": True, "prior_unclean": False}},
              "orders": {"s1:o1": order}}
    stamp(ledger)
    common = {"schema_version": 1, "account_scope": "test-scope", "market": "KR",
              "captured_at": "2026-10-02T20:00:00+09:00", "window_start": "2026-10-02",
              "window_end": "2026-10-02", "declared_complete": True,
              "source_ref": "synthetic fixture"}
    broker = {**common, "source_kind": "broker_all_orders", "rows": [{
        "ord_dt": "20261002", "odno": "000123", "orgn_odno": "", "pdno": "005930",
        "sll_buy_dvsn_cd": "02", "ord_qty": "10", "tot_ccld_qty": "10", "avg_prvs": "106",
        "cncl_yn": "N", "cnc_cfrm_qty": "0", "rmn_qty": "0", "rjct_qty": "0"}]}
    journal = {**common, "source_kind": "journal_events", "rows": [
        {"event_id": "e1", "trade_id": "t1", "market": "KR", "event_date": "2026-10-02",
         "date_basis": "Asia/Seoul", "symbol": "005930", "side": "buy",
         "kis_order_no": "000123", "quantity": 4, "price": "100"},
        {"event_id": "e2", "trade_id": "t1", "market": "KR", "event_date": "2026-10-02",
         "date_basis": "Asia/Seoul", "symbol": "005930", "side": "buy",
         "kis_order_no": "000123", "quantity": 6, "price": "110"}]}
    baseline = {**common, "source_kind": "baseline_snapshots", "rows": [
        {"owner": owner, "captured_at": common["captured_at"],
         "positions": [{"symbol": "005930", "quantity": 10}],
         "cash": {"amount": "0", "basis": "settled"}} for owner in ("broker", "internal")]}
    return ledger, broker, journal, baseline


def stamp(ledger):
    state = {key: ledger[key] for key in ("sessions", "orders")}
    ledger["state_digest"] = sha256(json.dumps(state, sort_keys=True, separators=(",", ":"),
                                             ensure_ascii=False).encode()).hexdigest()


def codes(report):
    return {item["code"] for item in report["issues"]}


def test_consistent_evidence_never_authorizes_replay_or_release():
    args = evidence()
    before = deepcopy(args)
    report = reconcile_execution_evidence(*args)
    assert report["comparison_status"] == "consistent"
    assert report["runtime_release_allowed"] is False
    assert report["replay_allowed"] is False
    assert report["state_mutated"] is False
    assert report["baseline_inclusion"] == "unverified"
    assert report["baseline"]["cash_match"] is True  # zero is evidence, not missing
    assert report["orders"][0]["journal_shape_match"] is True
    assert args == before


def test_compensated_missing_and_extra_deltas_do_not_pass_totals():
    args = evidence()
    args[2]["rows"] = [{**args[2]["rows"][0], "quantity": 10, "price": "106"}]
    report = reconcile_execution_evidence(*args)
    row = report["orders"][0]
    assert row["journal_quantity_match"] and row["journal_notional_match"]
    assert not row["journal_shape_match"]
    assert report["comparison_status"] == "mismatch"
    assert row["missing_journal_deltas"] == [
        {"quantity": 4, "price": "100.00", "count": 1},
        {"quantity": 6, "price": "110.00", "count": 1}]
    assert row["extra_journal_deltas"] == [{"quantity": 10, "price": "106", "count": 1}]


@pytest.mark.parametrize("source,index,field,value,code", [
    (1, 0, "pdno", "000660", "broker_identity_mismatch"),
    (1, 0, "tot_ccld_qty", "9", "invalid_row"),  # inconsistent remainder
    (1, 0, "orgn_odno", "000122", "unsupported_lineage"),
    (2, 0, "kis_order_no", None, "journal_identity_missing"),
    (2, 0, "quantity", True, "invalid_row"),
    (2, 0, "price", "NaN", "invalid_row"),
    (2, 0, "date_basis", "UTC", "invalid_row"),
    (2, 0, "side", "sell", "journal_identity_mismatch"),
])
def test_bad_or_unattributed_rows_remain_visible(source, index, field, value, code):
    args = evidence()
    args[source]["rows"][index][field] = value
    report = reconcile_execution_evidence(*args)
    assert report["comparison_status"] != "consistent"
    assert code in codes(report)
    assert report["inputs"]["journal"]["row_count"] == 2


@pytest.mark.parametrize("source", [1, 2, 3])
def test_duplicate_rows_are_never_silently_deduplicated(source):
    args = evidence()
    args[source]["rows"].append(deepcopy(args[source]["rows"][0]))
    report = reconcile_execution_evidence(*args)
    assert "duplicate_identity" in codes(report)
    assert report["comparison_status"] == "mismatch"


def test_same_odno_different_day_is_not_a_duplicate():
    args = evidence()
    for source in args[1:]:
        source["window_start"] = "2026-10-01"
    second = deepcopy(args[0]["orders"]["s1:o1"])
    second["facts"]["order_date"] = "20261001"
    second["facts"]["local_id"] = "o2"
    args[0]["orders"]["s1:o2"] = second
    stamp(args[0])
    args[1]["rows"].append({**args[1]["rows"][0], "ord_dt": "20261001"})
    args[2]["rows"] += [{**row, "event_date": "2026-10-01", "event_id": row["event_id"] + "-old"}
                       for row in deepcopy(args[2]["rows"])]
    report = reconcile_execution_evidence(*args)
    assert len(report["orders"]) == 2
    assert report["comparison_status"] == "consistent"


def test_zero_fill_cancel_is_preserved_and_requires_explicit_terminal_evidence():
    args = evidence()
    order = args[0]["orders"]["s1:o1"]
    order.update(status="canceled", observed_quantity=0, observed_average="0",
                 terminal_quantity=0, executions=[])
    stamp(args[0])
    args[1]["rows"][0].update(tot_ccld_qty="0", avg_prvs="0", cncl_yn="Y", cnc_cfrm_qty="10")
    args[2]["rows"] = []
    report = reconcile_execution_evidence(*args)
    assert report["orders"][0]["broker_terminal"] is True
    assert report["comparison_status"] == "consistent"
    args[1]["rows"][0].update(cncl_yn="N", cnc_cfrm_qty="0", rmn_qty="10")
    assert "broker_not_terminal" in codes(reconcile_execution_evidence(*args))


def test_unclean_empty_session_has_unknown_risk_window():
    args = evidence()
    args[0]["orders"] = {}
    args[0]["sessions"]["s1"]["clean"] = False
    stamp(args[0])
    args[1]["rows"] = args[2]["rows"] = []
    report = reconcile_execution_evidence(*args)
    assert report["comparison_status"] == "incomplete"
    assert report["unclean_sessions"] == ["s1"]
    assert report["session_risk_window"] == "unverified"


def test_decimal_delta_rounding_is_separate_from_cumulative_implied_amount():
    args = evidence()
    order = args[0]["orders"]["s1:o1"]
    order["executions"][1]["price"] = "110.0066666666666666666666667"
    order["observed_average"] = "106.004"
    stamp(args[0])
    args[1]["rows"][0]["avg_prvs"] = "106.004"
    args[2]["rows"][1]["price"] = "110.01"
    report = reconcile_execution_evidence(*args)
    assert report["comparison_status"] == "consistent"
    row = report["orders"][0]
    assert row["broker_implied_notional"] == "1060.040"
    assert row["expected_journal_notional"] == "1060.06"


def test_extra_account_order_and_unresolved_out_of_window_are_gaps():
    args = evidence()
    args[1]["rows"].append({**args[1]["rows"][0], "odno": "manual"})
    order = args[0]["orders"]["s1:o1"]
    order["facts"]["order_date"] = "20261001"
    order["terminal_quantity"] = None
    stamp(args[0])
    report = reconcile_execution_evidence(*args)
    assert {"unattributed_broker_order", "unresolved_outside_window"} <= codes(report)


@pytest.mark.parametrize("change,code", [
    ("missing_cash", "cash_unavailable"), ("basis", "cash_basis_mismatch"),
    ("time", "baseline_time_difference"), ("quantity", "baseline_position_mismatch"),
])
def test_baseline_does_not_invent_inclusion_or_cash_movements(change, code):
    args = evidence()
    row = args[3]["rows"][1]
    if change == "missing_cash": row["cash"]["amount"] = None
    elif change == "basis": row["cash"]["basis"] = "portfolio"
    elif change == "time": row["captured_at"] = "2026-10-02T19:00:00+09:00"
    else: row["positions"][0]["quantity"] = 9
    report = reconcile_execution_evidence(*args)
    assert code in codes(report)
    assert report["baseline_inclusion"] == "unverified"
    if change == "quantity":
        assert report["baseline"]["position_differences"] == [
            {"symbol": "005930", "broker_quantity": 10, "internal_quantity": 9}]


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("captured_at", "2026-10-02T20:00:00"),
    ("captured_at", "2026-10-01T20:00:00+09:00"), ("window_start", "2026-10-03"),
    ("declared_complete", "true"), ("source_ref", ""), ("rows", {}),
])
def test_unusable_envelope_is_rejected(field, value):
    args = evidence()
    args[1][field] = value
    with pytest.raises(ValueError): reconcile_execution_evidence(*args)


@pytest.mark.parametrize("field,value,code", [
    ("account_scope", "other", "scope_mismatch"),
    ("window_start", "2026-10-01", "window_mismatch"),
    ("declared_complete", False, "declared_incomplete"),
])
def test_declared_completeness_and_mixed_scope_do_not_authorize_recovery(field, value, code):
    args = evidence()
    args[2][field] = value
    report = reconcile_execution_evidence(*args)
    assert code in codes(report)
    assert report["comparison_status"] != "consistent"
    assert not report["runtime_release_allowed"]
