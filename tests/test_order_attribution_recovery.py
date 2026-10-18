"""오프라인 귀속 연결은 명시 실행 ID와 전체 주문 identity를 요구한다."""

from copy import deepcopy
from hashlib import sha256
import json

import pytest

from src.execution.recovery_evidence import reconcile_execution_evidence


def stamp(ledger):
    state = {key: ledger[key] for key in ("sessions", "orders")}
    ledger["state_digest"] = sha256(json.dumps(
        state, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()).hexdigest()


def evidence(*, attributed=True):
    facts = {"local_id": "o1", "symbol": "005930", "side": "buy", "quantity": 10,
             "order_date": "20261002", "strategy": "gap", "reason": "entry", "partial_exit": False}
    if attributed:
        facts["attribution"] = {
            "version": "order-attribution-v1", "candidate_id": "candidate-1", "plan_id": "plan-1",
            "signal_id": "signal-1", "llm_decision_id": None,
            "missing_reasons": {"llm_decision_id": "not_available"}}
    order = {"session_id": "s1", "facts": facts, "odno": "000123", "orgno": "001",
             "status": "canceled", "observed_quantity": 8, "observed_average": "105",
             "terminal_quantity": 8, "executions": [
                 {"execution_id": "s1:o1:4", "from_quantity": 0, "to_quantity": 4,
                  "price": "100", "portfolio_applied": True, "handoff_returned": True},
                 {"execution_id": "s1:o1:8", "from_quantity": 4, "to_quantity": 8,
                  "price": "110", "portfolio_applied": True, "handoff_returned": True}]}
    ledger = {"format": "execution-ledger-export-v1", "schema_version": 1,
              "account_scope": "synthetic-scope", "event_count": 12, "source_sha256": "a" * 64,
              "sessions": {"s1": {"clean": True, "prior_unclean": False}}, "orders": {"s1:o1": order}}
    stamp(ledger)
    common = {"schema_version": 1, "account_scope": "synthetic-scope", "market": "KR",
              "captured_at": "2026-10-02T20:00:00+09:00", "window_start": "2026-10-02",
              "window_end": "2026-10-02", "declared_complete": True, "source_ref": "synthetic fixture"}
    broker = {**common, "source_kind": "broker_all_orders", "rows": [{
        "ord_dt": "20261002", "odno": "000123", "orgn_odno": "", "pdno": "005930",
        "sll_buy_dvsn_cd": "02", "ord_qty": "10", "tot_ccld_qty": "8", "avg_prvs": "105",
        "cncl_yn": "Y", "cnc_cfrm_qty": "2", "rmn_qty": "0", "rjct_qty": "0"}]}
    journal = {**common, "source_kind": "journal_events", "rows": [
        {"event_id": "e1", "trade_id": "t1", "execution_id": "s1:o1:4", "market": "KR",
         "event_date": "2026-10-02", "date_basis": "Asia/Seoul", "symbol": "005930",
         "side": "buy", "kis_order_no": "000123", "quantity": 4, "price": "100"},
        {"event_id": "e2", "trade_id": "t1", "execution_id": "s1:o1:8", "market": "KR",
         "event_date": "2026-10-02", "date_basis": "Asia/Seoul", "symbol": "005930",
         "side": "buy", "kis_order_no": "000123", "quantity": 4, "price": "110"}]}
    baseline = {**common, "source_kind": "baseline_snapshots", "rows": [
        {"owner": owner, "captured_at": common["captured_at"],
         "positions": [{"symbol": "005930", "quantity": 8}],
         "cash": {"amount": "0", "basis": "settled"}} for owner in ("broker", "internal")]}
    return ledger, broker, journal, baseline


def test_partial_fills_cancel_and_exact_links_preserve_input_and_durability_boundary():
    args = evidence()
    before = deepcopy(args)
    report = reconcile_execution_evidence(*args)
    chain = report["attribution_chains"][0]
    assert report["comparison_status"] == "consistent"
    assert chain["local_order_id"] == "o1"
    assert chain["order_key"] == "s1:o1"
    assert chain["broker_order_no"] == "000123"
    assert chain["account_scope"] == "synthetic-scope"
    assert chain["order_date"] == "2026-10-02"
    assert chain["attribution"]["candidate_id"] == "candidate-1"
    assert chain["status"] == "canceled"
    assert chain["requested_quantity"] == 10
    assert chain["unfilled_quantity_at_terminal"] == 2
    assert [e["delta_quantity"] for e in chain["executions"]] == [4, 4]
    assert [e["price"] for e in chain["executions"]] == ["100", "110"]
    assert [e["execution_id"] for e in chain["executions"]] == ["s1:o1:4", "s1:o1:8"]
    assert [e["journal_event_id"] for e in chain["executions"]] == ["e1", "e2"]
    assert all(e["journal_trade_id"] == "t1" for e in chain["executions"])
    assert all(e["journal_identity_status"] == "matched_supplied_row" for e in chain["executions"])
    assert all(e["portfolio_applied"] and e["handoff_returned"] for e in chain["executions"])
    assert report["journal_durability"] == "unverified"
    assert all(e["journal_durability"] == "unverified" for e in chain["executions"])
    assert not report["replay_allowed"] and not report["runtime_release_allowed"]
    assert args == before
    chain["attribution"]["missing_reasons"]["llm_decision_id"] = "invalid"
    assert args == before


@pytest.mark.parametrize("status", ["intent", "not_sent", "rejected", "unknown", "accepted", "canceled"])
def test_all_zero_execution_intents_remain_in_denominator(status):
    args = evidence()
    order = args[0]["orders"]["s1:o1"]
    order.update(status=status, observed_quantity=0, observed_average="0", executions=[],
                 terminal_quantity=0 if status in {"not_sent", "rejected", "canceled"} else None,
                 odno="000123" if status in {"accepted", "canceled"} else "")
    stamp(args[0])
    args[1]["rows"] = args[2]["rows"] = []
    report = reconcile_execution_evidence(*args)
    assert report["intent_count"] == 1
    assert len(report["attribution_chains"]) == 1
    chain = report["attribution_chains"][0]
    assert chain["status"] == status and chain["executions"] == []
    assert chain["broker_order_no"] == ("000123" if status in {"accepted", "canceled"} else None)


@pytest.mark.parametrize("attributed", [True, False])
def test_missing_execution_ids_never_invent_per_execution_links(attributed):
    args = evidence(attributed=attributed)
    for row in args[2]["rows"]:
        del row["execution_id"]
    report = reconcile_execution_evidence(*args)
    assert report["orders"][0]["journal_shape_match"] is True
    assert report["comparison_status"] == ("incomplete" if attributed else "consistent")
    for execution in report["attribution_chains"][0]["executions"]:
        assert execution["journal_identity_status"] == "missing_execution_id"
        assert execution["journal_event_id"] is None
        assert execution["journal_trade_id"] is None
    if not attributed:
        envelope = report["attribution_chains"][0]["attribution"]
        assert envelope["candidate_id"] is None
        assert envelope["missing_reasons"] == {
            field: "legacy_not_recorded" for field in
            ("candidate_id", "plan_id", "signal_id", "llm_decision_id")}
        assert "attribution" not in args[0]["orders"]["s1:o1"]["facts"]


@pytest.mark.parametrize("field,value", [
    ("event_date", "2026-10-01"), ("kis_order_no", "999999"),
    ("symbol", "000660"), ("side", "sell"), ("quantity", 5), ("price", "101"),
])
def test_matching_execution_id_requires_all_order_and_delta_facts(field, value):
    args = evidence()
    for source in args[1:]:
        source["window_start"] = "2026-10-01"
    args[2]["rows"][0][field] = value
    report = reconcile_execution_evidence(*args)
    execution = report["attribution_chains"][0]["executions"][0]
    assert execution["journal_identity_status"] == "conflict"
    assert execution["journal_event_id"] is None
    assert report["comparison_status"] == "mismatch"


def test_matching_execution_id_in_other_scope_never_links():
    args = evidence()
    args[2]["account_scope"] = "other-scope"
    report = reconcile_execution_evidence(*args)
    execution = report["attribution_chains"][0]["executions"][0]
    assert execution["journal_identity_status"] == "conflict"
    assert execution["journal_event_id"] is None
    assert report["comparison_status"] == "mismatch"


def test_duplicate_execution_id_across_different_event_trade_and_order_is_conflict():
    args = evidence()
    args[2]["rows"].append({**args[2]["rows"][0], "event_id": "e-other", "trade_id": "t-other",
                              "kis_order_no": "999999"})
    report = reconcile_execution_evidence(*args)
    execution = report["attribution_chains"][0]["executions"][0]
    assert execution["journal_identity_status"] == "conflict"
    assert execution["journal_event_id"] is None
    assert "duplicate_execution_id" in {i["code"] for i in report["issues"]}
    assert report["comparison_status"] == "mismatch"


def test_swapped_execution_ids_with_identical_quantity_price_across_orders_do_not_match():
    args = evidence()
    first = args[0]["orders"]["s1:o1"]
    first.update(status="accepted", observed_quantity=4, observed_average="100", terminal_quantity=4)
    first["facts"]["quantity"] = 4
    first["executions"] = first["executions"][:1]
    second = deepcopy(first)
    second["facts"]["local_id"] = "o2"
    second["odno"] = "000124"
    second["executions"][0]["execution_id"] = "s1:o2:4"
    args[0]["orders"]["s1:o2"] = second
    stamp(args[0])
    args[1]["rows"][0].update(ord_qty="4", tot_ccld_qty="4", avg_prvs="100", cncl_yn="N", cnc_cfrm_qty="0")
    args[1]["rows"].append({**args[1]["rows"][0], "odno": "000124"})
    args[2]["rows"] = [
        {**args[2]["rows"][0], "execution_id": "s1:o2:4"},
        {**args[2]["rows"][0], "execution_id": "s1:o1:4", "event_id": "e2", "kis_order_no": "000124"}]
    report = reconcile_execution_evidence(*args)
    assert all(o["journal_shape_match"] for o in report["orders"])
    assert all(c["executions"][0]["journal_identity_status"] == "conflict" for c in report["attribution_chains"])
    assert report["comparison_status"] == "mismatch"


@pytest.mark.parametrize("value", ["", "   ", "x\x00y", 12, True, "x" * 1025])
def test_present_invalid_journal_execution_ids_are_invalid_rows(value):
    args = evidence()
    args[2]["rows"][0]["execution_id"] = value
    report = reconcile_execution_evidence(*args)
    assert report["inputs"]["journal"]["invalid_rows"] == 1
    assert report["comparison_status"] != "consistent"


@pytest.mark.parametrize("change", [{"event_date": "2026-10-01"}, {"quantity": True}])
def test_duplicate_execution_id_in_invalid_row_still_blocks_link(change):
    args = evidence()
    args[2]["rows"].append({**args[2]["rows"][0], "event_id": "bad-row", "trade_id": "other-trade", **change})
    report = reconcile_execution_evidence(*args)
    execution = report["attribution_chains"][0]["executions"][0]
    assert execution["journal_identity_status"] == "conflict"
    assert execution["journal_event_id"] is None
    assert "duplicate_execution_id" in {i["code"] for i in report["issues"]}


def test_duplicate_event_id_with_distinct_execution_ids_blocks_both_links():
    args = evidence()
    args[2]["rows"][1]["event_id"] = "e1"
    report = reconcile_execution_evidence(*args)
    assert all(e["journal_identity_status"] == "conflict" for e in report["attribution_chains"][0]["executions"])
    assert report["comparison_status"] == "mismatch"


def test_unknown_terminal_quantity_does_not_invent_unfilled_total():
    args = evidence()
    args[0]["orders"]["s1:o1"]["terminal_quantity"] = None
    stamp(args[0])
    assert reconcile_execution_evidence(*args)["attribution_chains"][0]["unfilled_quantity_at_terminal"] is None


def test_outside_window_intents_are_counted_separately():
    args = evidence()
    args[0]["orders"]["s1:o1"]["facts"]["order_date"] = "20261001"
    stamp(args[0])
    report = reconcile_execution_evidence(*args)
    assert report["intent_count"] == 0
    assert report["attribution_chains"] == []
    assert report["orders_outside_window"] == 1


@pytest.mark.parametrize("execution_id", ["x" * 161, " leading:execution ", "실행:1"])
def test_execution_ids_keep_the_existing_ledger_text_contract(execution_id):
    args = evidence()
    args[0]["orders"]["s1:o1"]["executions"][0]["execution_id"] = execution_id
    args[2]["rows"][0]["execution_id"] = execution_id
    stamp(args[0])
    report = reconcile_execution_evidence(*args)
    execution = report["attribution_chains"][0]["executions"][0]
    assert execution["journal_identity_status"] == "matched_supplied_row"
    assert execution["execution_id"] == execution_id
    assert execution["journal_event_id"] == "e1"


def test_explicit_null_legacy_execution_id_is_missing_not_invalid():
    args = evidence(attributed=False)
    for row in args[2]["rows"]:
        row["execution_id"] = None
    report = reconcile_execution_evidence(*args)
    assert report["comparison_status"] == "consistent"
    assert report["inputs"]["journal"]["invalid_rows"] == 0
    assert all(e["journal_identity_status"] == "missing_execution_id"
               for e in report["attribution_chains"][0]["executions"])
