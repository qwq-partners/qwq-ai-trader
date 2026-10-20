"""Contracts for the bounded, offline observed regular-flow supplement."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import json

import pytest


AT = "2026-10-19T01:20:00+00:00"
CNT_SHA = "833b105418e98807d3e24753f97ed008e0f5f6972a0937a1a0f40d252543624d"
BOOK_SHA = "a22543dd4a1c64c01210eef281203771da0d1d58e2e7d4c08643c35a3fc60ff3"


def _stamp(seconds):
    return (datetime(2026, 10, 19, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _study(policy=True):
    data = {
        "evaluation_epoch": "synthetic-flow-v1", "as_of": AT,
        "outcome_basis": "fixed_horizon_bid_markout",
        "capture": {"version": "runner-first-scan-v4", "scan_admission_ref": "synthetic",
                    "source_version_ref": "synthetic-source", "configuration_ref": "synthetic-config",
                    "selection_basis": {"version": "selection-basis-v2", "max_candidates": 30, "max_source_terms": 16},
                    "entry_gate_trace": {"max_candidates": 30}, "start_at": "2026-10-19T00:59:00+00:00",
                    "admission_end_at": "2026-10-19T01:04:00+00:00", "end_at": AT,
                    "channels": {"max_candidates": 5, "market_state_evidence": True}},
        "markout_policy": {"version": "fixed-horizon-bid-v1", "policy_ref": "synthetic-pregate-900",
                            "fixed_at": "2026-10-18T00:00:00+00:00", "horizon_seconds": 900,
                            "max_delay_seconds": 5, "anchor": "entry_quote_observed_at"},
    }
    if policy:
        data["observed_regular_flow_policy"] = {
            "version": "pregate-observed-regular-flow-v1", "fixed_at": "2026-10-18T00:00:00+00:00",
            "preparation_seconds": 5, "state_max_age_seconds": 5,
            "horizon_seconds": 900, "max_markout_delay_seconds": 5,
        }
    return data


def _book(sequence, quote_id, observed, received, *, symbol="000001", generation=1, bid="100", ask="101"):
    return {"kind": "ws_quote", "sequence": sequence, "quote_id": quote_id, "symbol": symbol,
            "observed_at": observed, "bid": bid, "ask": ask, "bid_size": 1, "ask_size": 1,
            "provenance": {"tr_id": "H0STASP0", "message_count": 1, "source_as_of": None,
                           "received_at": received, "connection_id": "c1", "generation": generation,
                           "raw_field_count": 63, "market_state_schema": "H0STASP0-63-v1",
                           "market_state_source_sha256": BOOK_SHA, "market_state_status": "raw_observed",
                           "exchange_time": "100000", "hour_class_code": "0", "market_class_code": "2"}}


def _cnt(sequence, observed, received, *, symbol="000001", generation=1, **over):
    row = {"kind": "market_state", "sequence": sequence, "symbol": symbol, "observed_at": observed,
           "received_at": received, "tr_id": "H0STCNT0", "connection_id": "c1", "generation": generation,
           "local_frame_index": sequence, "record_index": 1, "schema_version": "kis-market-state-raw-v1",
           "market_state_schema": "kis-public-guide-20261012", "market_state_source_sha256": CNT_SHA,
           "market_state_status": "raw_only", "raw_field_count": 47, "business_date": "20261019",
           "exchange_time": "100000", "new_mkop_cls_code": "20", "trht_yn": "N",
           "hour_cls_code": "0", "market_cls_code": "2"}
    row.update(over)
    return row


def _mko(sequence, observed, received, *, code="112", vi="N", symbol="000001", generation=1, **over):
    row = {"kind": "market_state", "sequence": sequence, "symbol": symbol, "observed_at": observed,
           "received_at": received, "tr_id": "H0STMKO0", "connection_id": "c1", "generation": generation,
           "local_frame_index": sequence, "record_index": 1, "schema_version": "kis-market-state-raw-v1",
           "market_state_schema": "kis-public-guide-20261012",
           "market_state_source_sha256": "4c76c54995ff112312ecf9e98f4f194d6edfe2f0749300b8639c10ff0d40c3f5",
           "market_state_status": "raw_only", "raw_field_count": 11, "mkop_cls_code": code,
           "vi_cls_code": vi, "iscd_stat_cls_code": "00", "trht_yn": "N"}
    row.update(over)
    return row


def fixture(*, candidate_count=1, policy=True):
    study = _study(policy)
    candidates = [{"candidate_id": f"scan:{n:06d}", "symbol": f"{n:06d}"} for n in range(1, candidate_count + 1)]
    records = [{"kind": "scan", "sequence": 1, "scan_id": "scan", "observed_at": _stamp(0),
                "population_scope": "first_returned_scan_candidates", "scan_admission_ref": "synthetic",
                "selection_basis_expected": True, "selection_basis_max_candidates": 30,
                "selection_basis_max_terms": 16, "candidates": candidates}]
    for index, candidate in enumerate(candidates[:5], 1):
        symbol, cid = candidate["symbol"], candidate["candidate_id"]
        start = len(records) + 1
        records.extend([
            {"kind": "quote_subscription", "sequence": start, "observed_at": _stamp(2), "candidate_id": cid,
             "symbol": symbol, "tr_id": "H0STASP0", "status": "allocated", "connection_id": "c1", "generation": 1},
            _book(start + 1, f"original-{symbol}", _stamp(3), _stamp(3), symbol=symbol),
            _cnt(start + 2, _stamp(4), _stamp(4), symbol=symbol),
            _book(start + 3, f"anchor-{symbol}", _stamp(6), _stamp(5), symbol=symbol),
            _cnt(start + 4, _stamp(905), _stamp(905), symbol=symbol),
            _book(start + 5, f"markout-{symbol}", _stamp(906), _stamp(906), symbol=symbol, bid="102", ask="103"),
        ])
    raw = json.dumps(study, separators=(",", ":")).encode()
    return raw, {"schema_version": 1, "evaluation_epoch": study["evaluation_epoch"], "complete": True,
                 "dropped_records": 0, "incomplete_reasons": [],
                 "journal": {"study_sha256": hashlib.sha256(raw).hexdigest(), "sealed": True,
                             "structurally_valid": True, "persistence_dropped_records": 0,
                             "capture_closed_at": AT}, "records": records}


def _patch_strict(monkeypatch):
    strict = importlib.import_module("src.analytics.pregate_candidate_readout")
    monkeypatch.setattr(strict, "validate_window_contract", lambda *_: None)
    monkeypatch.setattr(strict, "validate_study_binding", lambda *_: None)
    monkeypatch.setattr(strict, "build_frame_report", lambda *_a, **_k: {"source_complete": True})
    monkeypatch.setattr(strict, "build_selection_report", lambda *_: {"ok": True})
    monkeypatch.setattr(strict, "build_gate_report", lambda *_a, **_k: {"references": {
        "entry_gate_source_version_ref": "synthetic-source", "entry_gate_configuration_ref": "synthetic-config"}})
    return strict


def _report(monkeypatch, raw, observations):
    _patch_strict(monkeypatch)
    mod = importlib.import_module("src.analytics.pregate_observed_regular_flow")
    return mod.build_observed_regular_flow_report(raw, observations,
        study_sha256=hashlib.sha256(raw).hexdigest(), analysis_as_of=AT)


def test_preserves_strict_report_and_reports_only_causally_observed_regular_pair(monkeypatch):
    raw, observations = fixture()
    strict = _patch_strict(monkeypatch).build_pregate_candidate_report(
        raw, observations, study_sha256=hashlib.sha256(raw).hexdigest(), analysis_as_of=AT)
    report = _report(monkeypatch, raw, observations)
    row = report["candidates"][0]
    assert report["strict_report"] == strict
    assert report["schema_version"] == "pregate-observed-regular-flow-v1"
    assert row["original_entry_quote"]["quote_id"] == "original-000001"
    assert row["anchor_quote"]["quote_id"] == "anchor-000001"
    assert row["markout_quote"]["quote_id"] == "markout-000001"
    assert row["anchor_state"]["flow_context"] == "observed_regular_flow"
    assert row["markout_state"]["flow_context"] == "observed_regular_flow"
    assert row["anchor_delay_from_first_book"] == 3.0
    assert row["gross_quote_move_pct"] == pytest.approx((102 / 101 - 1) * 100)
    assert report["counts"] == {"denominator": 1, "eligible_pairs": 1}
    assert report["account_return"] is None and report["net_pnl"] is None
    assert report["profitability_pass"] is False and report["production_eligible"] is False


@pytest.mark.parametrize("mutate", [
    lambda study: study.pop("observed_regular_flow_policy"),
    lambda study: study["observed_regular_flow_policy"].update(preparation_seconds=True),
    lambda study: study["observed_regular_flow_policy"].update(version="wrong"),
    lambda study: study["observed_regular_flow_policy"].update(fixed_at="2026-10-19T01:00:00+00:00"),
])
def test_rejects_missing_or_malformed_opt_in_policy_even_for_empty_scan(monkeypatch, mutate):
    raw, observations = fixture(candidate_count=0)
    study = json.loads(raw); mutate(study); raw = json.dumps(study, separators=(",", ":")).encode()
    observations["journal"]["study_sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError):
        _report(monkeypatch, raw, observations)


def test_freezes_first_book_after_qualifying_cnt_when_it_has_wrong_generation(monkeypatch):
    raw, observations = fixture()
    anchor = next(row for row in observations["records"] if row.get("quote_id") == "anchor-000001")
    anchor["provenance"]["generation"] = 2
    later = deepcopy(anchor); later["sequence"] = len(observations["records"]) + 1
    later["quote_id"] = "tempting-later"; later["provenance"]["generation"] = 1
    observations["records"].append(later)
    row = _report(monkeypatch, raw, observations)["candidates"][0]
    assert row["anchor_quote"]["quote_id"] == "anchor-000001"
    assert row["anchor_state"]["flow_context"] == "unknown"
    assert row["markout_quote"]["quote_id"] == "markout-000001"


def test_missing_endpoint_cnt_keeps_quote_but_not_flow_or_price_move(monkeypatch):
    raw, observations = fixture()
    observations["records"] = [row for row in observations["records"]
                               if not (row.get("tr_id") == "H0STCNT0" and row["sequence"] > 4)]
    for sequence, row in enumerate(observations["records"], 1): row["sequence"] = sequence
    row = _report(monkeypatch, raw, observations)["candidates"][0]
    assert row["markout_quote"]["quote_id"] == "markout-000001"
    assert row["markout_state"]["flow_context"] == "unknown"
    assert row["gross_quote_move_pct"] is None


@pytest.mark.parametrize("mutate,expect_anchor", [
    (lambda state: state.update(business_date="20261018"), False),
    (lambda state: state.update(raw_field_count=46), False),
    (lambda state: state.update(observed_at=_stamp(5)), True),  # publication equal to BOOK receipt is not causal
    (lambda state: state.update(generation=2), True),
])
def test_latest_cnt_defect_is_never_replaced_by_an_older_or_later_normal_record(monkeypatch, mutate, expect_anchor):
    raw, observations = fixture()
    state = next(row for row in observations["records"] if row.get("tr_id") == "H0STCNT0")
    mutate(state)
    row = _report(monkeypatch, raw, observations)["candidates"][0]
    if expect_anchor:
        assert row["anchor_quote"]["quote_id"] == "anchor-000001"
        assert row["anchor_state"]["flow_context"] == "unknown"
    else:
        assert row["anchor_quote"] is None


def test_preparation_deadline_does_not_substitute_a_later_normal_cnt(monkeypatch):
    raw, observations = fixture()
    state = next(row for row in observations["records"] if row.get("tr_id") == "H0STCNT0")
    state["received_at"] = state["observed_at"] = _stamp(8)
    row = _report(monkeypatch, raw, observations)["candidates"][0]
    assert row["anchor_quote"] is None
    assert "qualifying_cnt_missing_in_preparation_window" in row["reasons"]


def test_adverse_mko_marks_context_contrary_without_substituting_endpoint(monkeypatch):
    raw, observations = fixture()
    endpoint = next(row for row in observations["records"] if row.get("quote_id") == "markout-000001")
    observations["records"].insert(-1, _mko(6, _stamp(905), _stamp(905), code="164"))
    for sequence, row in enumerate(observations["records"], 1): row["sequence"] = sequence
    row = _report(monkeypatch, raw, observations)["candidates"][0]
    assert row["markout_quote"]["quote_id"] == endpoint["quote_id"]
    assert row["markout_state"]["flow_context"] == "contrary_observed"
    assert row["gross_quote_move_pct"] is None


def test_preserves_empty_and_all_sixteen_unsampled_candidates_in_denominator(monkeypatch):
    raw, observations = fixture(candidate_count=21)
    report = _report(monkeypatch, raw, observations)
    assert report["counts"]["denominator"] == 21
    assert [row["rank"] for row in report["candidates"]] == list(range(1, 22))
    assert all(row["anchor_quote"] is None for row in report["candidates"][5:])
