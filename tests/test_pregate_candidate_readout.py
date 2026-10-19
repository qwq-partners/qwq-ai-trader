"""Focused contracts for the offline pre-gate candidate reader."""
from copy import deepcopy
import hashlib
import importlib
import json

import pytest


AT = "2026-10-19T01:20:00+00:00"


def module():
    return importlib.import_module("src.analytics.pregate_candidate_readout")


def fixture():
    study = {"evaluation_epoch": "synthetic-firstscan-v4", "capture": {
        "version": "runner-first-scan-v4", "scan_admission_ref": "synthetic",
        "start_at": "2026-10-19T00:59:00+00:00", "admission_end_at": "2026-10-19T01:10:00+00:00",
        "end_at": AT, "channels": {"max_candidates": 5, "market_state_evidence": True}},
        "markout_policy": {"version": "fixed-horizon-bid-v1", "policy_ref": "synthetic-pregate-900",
        "fixed_at": "2026-10-18T00:00:00+00:00", "horizon_seconds": 900, "max_delay_seconds": 5,
        "anchor": "entry_quote_observed_at"}}
    raw = json.dumps(study, separators=(",", ":")).encode()
    candidates = [{"candidate_id": f"scan:{n:06d}", "symbol": f"{n:06d}"} for n in range(7)]
    rows = [{"kind": "scan", "sequence": 1, "scan_id": "scan", "observed_at": "2026-10-19T01:00:00+00:00",
             "population_scope": "first_returned_scan_candidates", "scan_admission_ref": "synthetic",
             "candidates": candidates}]
    for n in range(5):
        symbol = f"{n:06d}"; cid = f"scan:{symbol}"
        rows += [
            {"kind": "quote_subscription", "sequence": len(rows)+1, "observed_at": "2026-10-19T01:00:01+00:00",
             "candidate_id": cid, "symbol": symbol, "tr_id": "H0STASP0", "status": "desired",
             "connection_id": "c1", "generation": 1, "reason": None},
            {"kind": "quote_subscription", "sequence": len(rows)+2, "observed_at": "2026-10-19T01:00:02+00:00",
             "candidate_id": cid, "symbol": symbol, "tr_id": "H0STASP0", "status": "allocated",
             "connection_id": "c1", "generation": 1, "reason": None},
            {"kind": "ws_quote", "sequence": len(rows)+3, "quote_id": f"q-{n}", "symbol": symbol,
             "observed_at": "2026-10-19T01:00:05+00:00", "bid": "100", "ask": "101", "bid_size": 1,
             "ask_size": 1, "provenance": {"tr_id": "H0STASP0", "message_count": 1, "source_as_of": None,
                 "received_at": "2026-10-19T01:00:04+00:00", "connection_id": "c1", "generation": 1}},
            {"kind": "ws_quote", "sequence": len(rows)+4, "quote_id": f"m-{n}", "symbol": symbol,
             "observed_at": "2026-10-19T01:15:06+00:00", "bid": "102", "ask": "103", "bid_size": 1,
             "ask_size": 1, "provenance": {"tr_id": "H0STASP0", "message_count": 1, "source_as_of": None,
                 "received_at": "2026-10-19T01:15:05+00:00", "connection_id": "c1", "generation": 1}},
        ]
    journal = {"study_sha256": hashlib.sha256(raw).hexdigest(), "sealed": True, "structurally_valid": True,
               "persistence_dropped_records": 0, "capture_closed_at": AT}
    return raw, {"schema_version": 1, "evaluation_epoch": study["evaluation_epoch"], "complete": True,
                 "dropped_records": 0, "incomplete_reasons": [], "journal": journal, "records": rows}


def valid(monkeypatch):
    m = module()
    monkeypatch.setattr(m, "validate_study_binding", lambda *_: None)
    monkeypatch.setattr(m, "build_frame_report", lambda *_a, **_k: {"source_complete": True})
    monkeypatch.setattr(m, "build_selection_report", lambda *_: {"ok": True})
    monkeypatch.setattr(m, "build_gate_report", lambda *_a, **_k: {"ok": True})
    return m


def test_preserves_all_first_candidates_but_samples_top_five(monkeypatch):
    raw, obs = fixture(); m = valid(monkeypatch)
    report = m.build_pregate_candidate_report(raw, obs, study_sha256=hashlib.sha256(raw).hexdigest(), analysis_as_of=AT)
    assert report["denominator"]["returned_candidates"] == 7
    assert len(report["candidates"]) == 7
    assert report["candidates"][0]["entry_quote"]["quote_id"] == "q-0"
    assert report["candidates"][0]["markout_quote"]["quote_id"] == "m-0"
    assert all(row["allocation_status"] == "not_subscribed_by_fixed_top_n" for row in report["candidates"][5:])
    assert report["account_return"] is None and report["profitability_pass"] is False


def test_empty_first_scan_is_not_replaced(monkeypatch):
    raw, obs = fixture(); obs["records"][0]["candidates"] = []; m = valid(monkeypatch)
    report = m.build_pregate_candidate_report(raw, obs, study_sha256=hashlib.sha256(raw).hexdigest(), analysis_as_of=AT)
    assert report["denominator"]["returned_candidates"] == 0 and report["candidates"] == []


def test_bad_first_and_future_markout_are_not_replaced(monkeypatch):
    raw, obs = fixture(); m = valid(monkeypatch)
    first = next(r for r in obs["records"] if r.get("quote_id") == "q-0")
    first["provenance"]["message_count"] = 2
    later = deepcopy(first); later.update(sequence=len(obs["records"])+1, quote_id="better", observed_at="2026-10-19T01:00:06+00:00")
    later["provenance"].update(received_at="2026-10-19T01:00:05+00:00", message_count=1)
    obs["records"].append(later)
    report = m.build_pregate_candidate_report(raw, obs, study_sha256=hashlib.sha256(raw).hexdigest(), analysis_as_of=AT)
    assert report["candidates"][0]["entry_quote"]["quote_id"] == "q-0"
    assert "first_krx_book_invalid" in report["candidates"][0]["reasons"]


@pytest.mark.parametrize("field,value", [("bid", "0"), ("ask", "NaN"), ("bid_size", 0), ("ask_size", True), ("ask", "99")])
def test_bad_first_book_price_or_depth_is_preserved_not_replaced(monkeypatch, field, value):
    raw, obs = fixture(); m = valid(monkeypatch)
    first = next(r for r in obs["records"] if r.get("quote_id") == "q-0")
    first[field] = value
    report = m.build_pregate_candidate_report(raw, obs, study_sha256=hashlib.sha256(raw).hexdigest(), analysis_as_of=AT)
    assert report["candidates"][0]["entry_quote"]["quote_id"] == "q-0"
    assert "first_krx_book_invalid" in report["candidates"][0]["reasons"]


def test_markout_clock_regression_does_not_select_later_quote(monkeypatch):
    raw, obs = fixture(); m = valid(monkeypatch); study = json.loads(raw)
    entry = next(r for r in obs["records"] if r.get("quote_id") == "q-0")
    prior = deepcopy(entry); prior.update(sequence=100, quote_id="before-target", observed_at="2026-10-19T01:15:10+00:00")
    prior["provenance"]["received_at"] = "2026-10-19T01:15:04+00:00"
    candidate = deepcopy(entry); candidate.update(sequence=101, quote_id="regressed", observed_at="2026-10-19T01:15:06+00:00")
    candidate["provenance"]["received_at"] = "2026-10-19T01:15:06+00:00"
    selected, reason = m._markout([entry, prior, candidate], entry, m._timestamp(AT, "as_of"), study,
                                  m._timestamp(AT, "capture_end"))
    assert selected is None and reason == "markout_stream_invalid"


@pytest.mark.parametrize("defect", ["unsealed", "dropped", "study_mismatch"])
def test_integrity_mismatch_is_rejected(monkeypatch, defect):
    raw, obs = fixture(); m = valid(monkeypatch)
    if defect == "unsealed": obs["journal"]["sealed"] = False
    elif defect == "dropped": obs["dropped_records"] = 1
    else: obs["journal"]["study_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        m.build_pregate_candidate_report(raw, obs, study_sha256=hashlib.sha256(raw).hexdigest(), analysis_as_of=AT)


def test_same_time_or_stale_market_state_is_never_normal(monkeypatch):
    m = module()
    entry = {"sequence": 4, "kind": "ws_quote", "symbol": "000001", "observed_at": "2026-10-19T01:00:02+00:00",
             "bid": "100", "ask": "101", "bid_size": 1, "ask_size": 1,
             "provenance": {"tr_id": "H0STASP0", "message_count": 1, "source_as_of": None,
                            "received_at": "2026-10-19T01:00:01+00:00", "connection_id": "c", "generation": 1}}
    monkeypatch.setattr(m, "classify_market_state", lambda *_a, **_k: {"verdict": "normal_continuous", "basis": [], "source_refs": []})
    state = {"sequence": 3, "kind": "market_state", "symbol": "000001", "tr_id": "H0STCNT0", "received_at": "2026-10-19T01:00:01+00:00"}
    report = m._market_state([state], entry, m._timestamp(AT, "as_of"))
    assert report["verdict"] == "unknown" and "state_not_strictly_prior:H0STCNT0" in report["basis"]


def test_cli_rejects_symlink_fifo_and_duplicate_study_without_writing(tmp_path):
    cli = importlib.import_module("scripts.report_pregate_candidate_observation")
    journal = tmp_path / "journal.jsonl"; journal.write_bytes(b"not a journal\n")
    target = tmp_path / "target.json"; target.write_text("{}")
    link = tmp_path / "link.json"; link.symlink_to(target)
    assert cli.main(["--journal", str(journal), "--study", str(link), "--as-of", AT, "--max-journal-bytes", "100"]) == 2
    fifo = tmp_path / "study.fifo"
    import os
    os.mkfifo(fifo)
    assert cli.main(["--journal", str(journal), "--study", str(fifo), "--as-of", AT, "--max-journal-bytes", "100"]) == 2
    duplicate = tmp_path / "duplicate.json"; raw = b'{"x":1,"x":2}' ; duplicate.write_bytes(raw)
    before = (journal.read_bytes(), raw)
    assert cli.main(["--journal", str(journal), "--study", str(duplicate), "--as-of", AT, "--max-journal-bytes", "100"]) == 2
    assert (journal.read_bytes(), duplicate.read_bytes()) == before
