"""65차 — 판단 기록 ↔ 관측 원장 조인: 첫 호가·신선도·같은 연결의 채널별 장 상태·원장 완전성·평가 불가 사유 (합성 자료)."""
import json

import pytest

from src.analytics.decision_quote_join import join_decisions, load_decisions, load_ledger_payloads, parse_time, summarize
from src.analytics.kis_market_state import parse_market_state

DECIDED = "2026-10-14T09:01:00+09:00"              # = 00:01:00Z
OK_LEDGER = {"verified": True, "complete": True, "incomplete_reasons": []}
CONN = {"connection_id": "c1", "generation": 1}


def _decision(symbol="005490", decided=DECIDED, approved=True, did="dec-1"):
    return {"schema_version": "llm-decision-v1", "decision_id": did, "signal_id": "sig-1", "symbol": symbol,
            "stage": "adversarial", "result": {"approved": approved}, "decision_available_at": decided}


def _env(payload, row=1):
    return {"row": row, "previous_hash": "0" * 24, "kind": "record", "payload": payload, "hash": "x"}


def _quote(symbol, observed_at, ask="313000", ask_size=12, qid="q1", conn=CONN):
    return {"kind": "ws_quote", "symbol": symbol, "observed_at": observed_at, "ask": ask, "ask_size": ask_size,
            "bid": "312500", "bid_size": 40, "quote_id": qid,
            "provenance": {"exchange_time": "090100", "received_at": observed_at + "+00:00", **conn}}


def _state(symbol, observed_at, tr_id, *, conn=CONN, status=None, vi="0"):
    """실제 파서 출력으로 장 상태 레코드를 만든다 — 체결 채널(47)과 장운영 채널(11)은 필드가 다르다."""
    if tr_id == "H0STCNT0":
        fields = ["0"] * 47
        fields[1], fields[33], fields[35], fields[43], fields[46] = "090100", "20261014", "N", "0", "2"
    else:
        fields = ["0"] * 11
        fields[1], fields[3], fields[8] = "N", "2", vi
    parsed = parse_market_state(tr_id, fields)
    if status is not None:
        parsed["market_state_status"] = status
    return dict(parsed, kind="market_state", symbol=symbol, observed_at=observed_at,
                received_at=observed_at + "+00:00", **conn)


def test_naive_ledger_times_are_utc_and_first_quote_after_decision_is_picked():
    payloads = [_quote("005490", "2026-10-14T00:00:59.5", qid="before"),      # 판단 0.5초 전 — 제외
                _quote("005490", "2026-10-14T00:01:00.2", qid="first"),
                _quote("005490", "2026-10-14T00:01:03.0", qid="later"),
                _state("005490", "2026-10-14T00:00:58.0", "H0STMKO0"),
                _state("005490", "2026-10-14T00:00:57.0", "H0STCNT0")]
    r = join_decisions([_decision()], payloads, ledger_status=OK_LEDGER)[0]
    assert r["evaluable"] is True and r["reasons"] == []
    assert r["first_quote_after"]["quote_id"] == "first" and abs(r["first_quote_after"]["delay_seconds"] - 0.2) < 1e-6
    assert r["first_quote_after"]["ask"] == "313000" and r["first_quote_after"]["connection_id"] == "c1"
    assert set(r["market_state_at_quote"]) == {"H0STMKO0", "H0STCNT0"}
    assert r["market_state_at_quote"]["H0STMKO0"]["vi_cls_code"] == "0"            # VI 코드는 장운영 채널에만 있다
    assert r["market_state_at_quote"]["H0STCNT0"]["vi_cls_code"] is None and r["market_state_at_quote"]["H0STCNT0"]["market_cls_code"] == "2"
    assert parse_time("2026-10-14T00:01:00.2", "x").tzinfo is not None


def test_unverified_or_incomplete_ledger_never_counts_as_evaluable():
    payloads = [_quote("005490", "2026-10-14T00:01:00.2"), _state("005490", "2026-10-14T00:00:58.0", "H0STMKO0")]
    assert join_decisions([_decision()], payloads)[0]["reasons"] == ["ledger_unverified"]
    incomplete = {"verified": True, "complete": False, "incomplete_reasons": ["market_state_record_limit"]}
    rows = join_decisions([_decision()], payloads, ledger_status=incomplete)
    assert rows[0]["reasons"] == ["ledger_incomplete"] and rows[0]["evaluable"] is False
    s = summarize(rows, ledger_status=incomplete)
    assert s["ledger"]["incomplete_reasons"] == ["market_state_record_limit"] and s["evaluable"] == 0


def test_reasons_cover_missing_stale_invalid_unknown_and_vi_channel():
    no_quote = join_decisions([_decision(symbol="000660")], [_quote("005490", "2026-10-14T00:01:00.2")], ledger_status=OK_LEDGER)[0]
    assert no_quote["reasons"] == ["no_quote_after_decision"]
    stale = join_decisions([_decision()], [_quote("005490", "2026-10-14T00:02:30.0")], ledger_status=OK_LEDGER)[0]
    assert "stale_quote" in stale["reasons"] and "market_state_unknown" in stale["reasons"]
    bad = join_decisions([_decision()], [_quote("005490", "2026-10-14T00:01:01.0", ask="0", ask_size=0),
                                          _state("005490", "2026-10-14T00:00:50.0", "H0STMKO0", status="invalid_values")],
                         ledger_status=OK_LEDGER)[0]
    assert set(bad["reasons"]) == {"invalid_ask", "invalid_ask_size", "market_state_not_raw:H0STMKO0"}
    only_trade = join_decisions([_decision()], [_quote("005490", "2026-10-14T00:01:01.0"),
                                                _state("005490", "2026-10-14T00:00:50.0", "H0STCNT0")], ledger_status=OK_LEDGER)[0]
    assert only_trade["reasons"] == ["market_state_vi_channel_missing"]
    old_state = join_decisions([_decision()], [_quote("005490", "2026-10-14T00:01:01.0"),
                                               _state("005490", "2026-10-14T00:00:00.0", "H0STMKO0")],
                               ledger_status=OK_LEDGER, max_state_age_seconds=30)[0]
    assert old_state["reasons"] == ["market_state_stale:H0STMKO0"]
    missing = join_decisions([_decision(decided=None)], [], ledger_status=OK_LEDGER)[0]
    assert missing["reasons"] == ["decision_time_missing"]
    noprov = join_decisions([_decision()], [_quote("005490", "2026-10-14T00:01:01.0", conn={})], ledger_status=OK_LEDGER)[0]
    assert "quote_provenance_missing" in noprov["reasons"] and "market_state_unknown" in noprov["reasons"]


def test_market_state_requires_same_connection_and_generation_and_keeps_per_channel_latest():
    old_conn = {"connection_id": "c0", "generation": 0}
    payloads = [_state("005490", "2026-10-14T00:00:30.0", "H0STMKO0", conn=old_conn, vi="1"),     # 재연결 전 — 무시
                _state("005490", "2026-10-14T00:00:40.0", "H0STMKO0", vi="0"),
                _state("005490", "2026-10-14T00:00:50.0", "H0STCNT0"),                             # 체결 행이 뒤에 와도 VI 코드 유지
                _state("005490", "2026-10-14T00:01:02.0", "H0STMKO0", vi="2"),                     # 호가 뒤 — 무시
                _quote("005490", "2026-10-14T00:01:01.0")]
    r = join_decisions([_decision()], payloads, ledger_status=OK_LEDGER)[0]
    st = r["market_state_at_quote"]
    assert st["H0STMKO0"]["vi_cls_code"] == "0" and abs(st["H0STMKO0"]["age_seconds"] - 21.0) < 1e-6
    assert st["H0STCNT0"]["market_cls_code"] == "2" and abs(st["H0STCNT0"]["age_seconds"] - 11.0) < 1e-6
    assert r["evaluable"] is True
    reconnect_only = join_decisions([_decision()], [_state("005490", "2026-10-14T00:00:30.0", "H0STMKO0", conn=old_conn),
                                                     _quote("005490", "2026-10-14T00:01:01.0")], ledger_status=OK_LEDGER)[0]
    assert reconnect_only["reasons"] == ["market_state_unknown"]


def test_loaders_and_summary(tmp_path):
    dec = tmp_path / "decisions.jsonl"
    dec.write_text(json.dumps(_decision()) + "\n" + json.dumps(_decision(did="dec-2", approved=False)) + "\n")
    led = tmp_path / "engine.jsonl"
    led.write_text("\n".join(json.dumps(x) for x in (
        {"row": 1, "previous_hash": "0", "kind": "start", "payload": {"format": "entry-observation-journal-v2"}, "hash": "h"},
        _env(_quote("005490", "2026-10-14T00:01:00.2"), 2), _env(_state("005490", "2026-10-14T00:00:58.0", "H0STMKO0"), 3),
        _env({"kind": "scan", "symbol": "005490", "observed_at": "2026-10-14T00:00:00"}, 4),
        {"row": 5, "previous_hash": "0", "kind": "seal", "payload": {}, "hash": "h"})) + "\n")
    rows = join_decisions(load_decisions(dec), load_ledger_payloads(led), ledger_status=OK_LEDGER)
    s = summarize(rows, ledger_status=OK_LEDGER)
    assert s["decisions"] == 2 and s["evaluable"] == 2 and s["approved_evaluable"] == 1 and s["reasons"] == {}
    assert summarize(rows)["ledger"]["verified"] is False and "체결 가능성" in s["note"]


# ── 재리뷰 반영: 빈 필드·손상 시각·NaN 한도 ──

def test_empty_state_fields_are_not_evidence_but_zero_is():
    good = _state("005490", "2026-10-14T00:00:58.0", "H0STMKO0", vi="0")
    r_ok = join_decisions([_decision()], [_quote("005490", "2026-10-14T00:01:00.2"), good], ledger_status=OK_LEDGER)[0]
    assert r_ok["reasons"] == [] and r_ok["market_state_at_quote"]["H0STMKO0"]["vi_cls_code"] == "0"
    empty = dict(good); empty["vi_cls_code"] = ""; empty["trht_yn"] = None
    r_bad = join_decisions([_decision()], [_quote("005490", "2026-10-14T00:01:00.2"), empty], ledger_status=OK_LEDGER)[0]
    assert r_bad["evaluable"] is False
    assert r_bad["reasons"] == ["market_state_fields_missing:H0STMKO0:trht_yn,vi_cls_code"]


def test_corrupt_observed_at_marks_symbol_unevaluable_and_is_counted():
    corrupt = _quote("005490", "not-a-time", qid="corrupt")
    payloads = [corrupt, _quote("005490", "2026-10-14T00:01:00.2", qid="first"), _state("005490", "2026-10-14T00:00:58.0", "H0STMKO0")]
    rows = join_decisions([_decision(), _decision(symbol="000660", did="dec-2")], payloads, ledger_status=OK_LEDGER)
    assert "ledger_record_time_invalid" in rows[0]["reasons"] and rows[0]["evaluable"] is False
    assert rows[0]["first_quote_after"]["quote_id"] == "first"          # 선택은 하되 신뢰하지 않는다
    assert "ledger_record_time_invalid" not in rows[1]["reasons"]        # 다른 종목은 영향 없음
    assert summarize(rows, ledger_status=OK_LEDGER)["ledger_record_time_errors_by_symbol"] == {"005490": 1}


def test_time_limits_must_be_finite_and_nonnegative():
    import math
    import pytest
    for bad in (math.nan, math.inf, -1, True):
        with pytest.raises(ValueError):
            join_decisions([_decision()], [], ledger_status=OK_LEDGER, max_delay_seconds=bad)
        with pytest.raises(ValueError):
            join_decisions([_decision()], [], ledger_status=OK_LEDGER, max_state_age_seconds=bad)


# ── 3차 리뷰 반영: 누적 읽기 한도 / 빈 SHA ──

def test_read_limit_is_cumulative_and_rejects_partial_last_line(tmp_path):
    import pytest
    from src.analytics.decision_quote_join import _read_lines
    f = tmp_path / "d.jsonl"
    f.write_text(json.dumps(_decision()) + "\n" + json.dumps(_decision(did="dec-2")) + "\n")
    size = f.stat().st_size
    assert len(list(_read_lines(f, max_bytes=size))) == 2
    with pytest.raises(ValueError):
        list(_read_lines(f, max_bytes=size - 1))              # 두 번째 행이 한도를 넘는다
    g = tmp_path / "partial.jsonl"
    g.write_text(json.dumps(_decision()) + "\n" + '{"schema_version": "llm-decision-v1"')   # 개행 없는 부분 기록
    with pytest.raises(ValueError):
        list(_read_lines(g, max_bytes=1 << 20))
    for bad in (0, -1, True):
        with pytest.raises(ValueError):
            list(_read_lines(f, max_bytes=bad))


def test_cli_rejects_empty_or_malformed_sha(tmp_path):
    import subprocess, sys
    from pathlib import Path
    dec = tmp_path / "decisions.jsonl"; dec.write_text(json.dumps(_decision()) + "\n")
    led = tmp_path / "engine.jsonl"; led.write_text(json.dumps(_env(_quote("005490", "2026-10-14T00:01:00.2"))) + "\n")
    script = Path(__file__).resolve().parents[1] / "scripts" / "report_decision_quotes.py"
    for sha in ("", "abc"):
        proc = subprocess.run([sys.executable, str(script), "--decisions", str(dec), "--ledger", str(led), "--study-sha256", sha],
                              capture_output=True, text=True)
        assert proc.returncode == 2 and "study-sha256" in proc.stdout, (sha, proc.stdout)
    proc = subprocess.run([sys.executable, str(script), "--decisions", str(dec), "--ledger", str(led), "--max-delay-seconds", "nan"],
                          capture_output=True, text=True)
    assert proc.returncode == 2


def _received_quote(received, observed, qid="q1"):
    q = _quote("005490", observed, qid=qid)
    q["provenance"]["received_at"] = received
    return q


@pytest.mark.asyncio
async def test_strict_cli_preserves_receipt_clock_through_sealed_journal(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from src.analytics.entry_observation import EntryObservationBuffer
    from src.analytics.entry_observation_journal import ObservationJournal

    buffer = EntryObservationBuffer(evaluation_epoch="synthetic", capacity=100)
    ledger = tmp_path / "engine.jsonl"
    journal = await ObservationJournal.open(buffer, ledger, study_ref="synthetic",
                                           study_sha256="a" * 64, queue_capacity=20,
                                           batch_size=5, max_bytes=100000, max_record_bytes=10000)
    for payload in [
        _state("005490", "2026-10-14T00:00:59", "H0STMKO0"),
        _received_quote("2026-10-14T08:59:59.9+09:00", "2026-10-13T23:59:59.95+00:00", "before"),
        _received_quote("2026-10-14T09:01:00.2+09:00", "2026-10-14T00:01:00.3+00:00", "after"),
    ]:
        assert buffer.publish(payload)
    assert (await journal.close())["sealed"]
    decisions = tmp_path / "decisions.jsonl"
    decisions.write_text(json.dumps(_decision()) + "\n")
    before = ledger.read_bytes(), decisions.read_bytes()
    script = Path(__file__).resolve().parents[1] / "scripts/report_decision_quotes.py"
    result = subprocess.run([sys.executable, str(script), "--decisions", str(decisions),
                             "--ledger", str(ledger), "--study-sha256", "a" * 64],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["summary"]["schema_version"] == "decision-quote-join-v2"
    assert report["summary"]["time_basis"] == "local_received_at"
    assert report["summary"]["ledger"]["complete"] is True
    assert report["summary"]["evaluable"] == 1
    assert report["rows"][0]["first_quote_after"]["quote_id"] == "after"
    assert report["rows"][0]["first_quote_after"]["delay_seconds"] == pytest.approx(0.2)
    assert (ledger.read_bytes(), decisions.read_bytes()) == before


def test_newer_unique_state_clears_older_tie_and_unrelated_bad_receipt_is_isolated():
    states = [_state("005490", "2026-10-14T00:00:57", "H0STMKO0", vi="1"),
              _state("005490", "2026-10-14T00:00:57", "H0STMKO0", vi="2"),
              _state("005490", "2026-10-14T00:00:59", "H0STMKO0", vi="0")]
    bad = _quote("000660", "2026-10-14T00:01:00.2")
    bad["provenance"]["received_at"] = None
    rows = join_decisions([_decision()], states + [bad, _quote("005490", "2026-10-14T00:01:00.2")],
                          ledger_status=OK_LEDGER)
    assert rows[0]["evaluable"] is True
    assert rows[0]["market_state_at_quote"]["H0STMKO0"]["vi_cls_code"] == "0"
    assert summarize(rows)["ledger_time_error_reasons_by_symbol"] == {
        "000660": {"ledger_receipt_time_invalid": 1}}


def test_delayed_copy_does_not_turn_predecision_receipt_into_postdecision_quote():
    before = _received_quote("2026-10-14T00:00:59.9+00:00", "2026-10-14T00:01:00.2", "before")
    after = _received_quote("2026-10-14T00:01:00.3+00:00", "2026-10-14T00:01:00.4", "after")
    s = _state("005490", "2026-10-14T00:00:58", "H0STMKO0")
    row = join_decisions([_decision()], [before, after, s], ledger_status=OK_LEDGER)[0]
    assert row["evaluable"] is True
    q = row["first_quote_after"]
    assert q["quote_id"] == "after" and q["delay_seconds"] == pytest.approx(0.3)
    assert q["received_at"] == "2026-10-14T00:01:00.3+00:00"
    assert q["observed_at"] == "2026-10-14T00:01:00.4"
    assert q["capture_delay_seconds"] == pytest.approx(0.1)
    assert q["delivery_delay_seconds"] == pytest.approx(0.4)


def test_first_quote_uses_receipt_order_when_copy_order_differs():
    first = _received_quote("2026-10-14T00:01:00.1+00:00", "2026-10-14T00:01:00.9", "first")
    later = _received_quote("2026-10-14T00:01:00.2+00:00", "2026-10-14T00:01:00.3", "later")
    row = join_decisions([_decision()], [later, first], ledger_status=OK_LEDGER)[0]
    assert row["first_quote_after"]["quote_id"] == "first"
    assert row["first_quote_after"]["delay_seconds"] == pytest.approx(0.1)


def test_state_received_after_quote_cannot_leak_through_later_copy_time():
    q = _received_quote("2026-10-14T00:01:00.05+00:00", "2026-10-14T00:01:00.2")
    old = _state("005490", "2026-10-14T00:00:59", "H0STMKO0", vi="0")
    future = _state("005490", "2026-10-14T00:01:00.1", "H0STMKO0", vi="1")
    row = join_decisions([_decision()], [old, future, q], ledger_status=OK_LEDGER)[0]
    assert row["evaluable"] is True
    s = row["market_state_at_quote"]["H0STMKO0"]
    assert s["vi_cls_code"] == "0" and s["age_seconds"] == pytest.approx(1.05)
    assert s["received_at"] == "2026-10-14T00:00:59+00:00"


@pytest.mark.parametrize("kind", ["ws_quote", "market_state"])
@pytest.mark.parametrize("bad", [None, "not-a-time", "2026-10-14T00:01:00"])
def test_receipt_time_missing_invalid_or_naive_never_falls_back_to_copy_time(kind, bad):
    q = _quote("005490", "2026-10-14T00:01:00.2")
    s = _state("005490", "2026-10-14T00:00:59", "H0STMKO0")
    (q["provenance"] if kind == "ws_quote" else s)["received_at"] = bad
    row = join_decisions([_decision()], [q, s], ledger_status=OK_LEDGER)[0]
    assert row["evaluable"] is False and "ledger_receipt_time_invalid" in row["reasons"]


@pytest.mark.parametrize("kind", ["ws_quote", "market_state"])
def test_receipt_later_than_copy_is_a_clock_error(kind):
    q = _quote("005490", "2026-10-14T00:01:00.2")
    s = _state("005490", "2026-10-14T00:00:59", "H0STMKO0")
    (q["provenance"] if kind == "ws_quote" else s)["received_at"] = "2026-10-14T00:01:01+00:00"
    row = join_decisions([_decision()], [q, s], ledger_status=OK_LEDGER)[0]
    assert row["evaluable"] is False and "ledger_clock_order_invalid" in row["reasons"]


def test_equal_decision_receipt_is_excluded_and_duplicate_first_receipts_are_ambiguous():
    equal = _received_quote("2026-10-14T00:01:00+00:00", "2026-10-14T00:01:00.1")
    row = join_decisions([_decision()], [equal], ledger_status=OK_LEDGER)[0]
    assert row["first_quote_after"] is None and "no_quote_after_decision" in row["reasons"]
    q1 = _received_quote("2026-10-14T00:01:00.2+00:00", "2026-10-14T00:01:00.3", "q1")
    q2 = _received_quote("2026-10-14T00:01:00.2+00:00", "2026-10-14T00:01:00.4", "q2")
    s = _state("005490", "2026-10-14T00:00:59", "H0STMKO0")
    row = join_decisions([_decision()], [q1, q2, s], ledger_status=OK_LEDGER)[0]
    assert row["evaluable"] is False and "quote_receipt_time_ambiguous" in row["reasons"]


def test_equal_quote_state_receipt_cannot_establish_prior_state():
    q = _quote("005490", "2026-10-14T00:01:00.2")
    old = _state("005490", "2026-10-14T00:00:59", "H0STMKO0")
    equal = _state("005490", "2026-10-14T00:01:00.2", "H0STMKO0", vi="1")
    row = join_decisions([_decision()], [q, old, equal], ledger_status=OK_LEDGER)[0]
    assert row["evaluable"] is False and "market_state_quote_time_ambiguous:H0STMKO0" in row["reasons"]
    assert row["market_state_at_quote"]["H0STMKO0"]["vi_cls_code"] == "0"


def test_delivery_delay_still_rejects_an_old_quote_even_with_prompt_receipt():
    q = _received_quote("2026-10-14T00:01:00.2+00:00", "2026-10-14T00:02:05")
    s = _state("005490", "2026-10-14T00:00:59", "H0STMKO0")
    row = join_decisions([_decision()], [q, s], ledger_status=OK_LEDGER)[0]
    assert row["evaluable"] is False and "stale_quote" in row["reasons"]
    assert row["first_quote_after"]["delay_seconds"] == pytest.approx(0.2)


def test_decision_time_must_have_an_explicit_timezone():
    q = _quote("005490", "2026-10-14T00:01:00.2")
    s = _state("005490", "2026-10-14T00:00:59", "H0STMKO0")
    row = join_decisions([_decision(decided="2026-10-14T00:01:00")], [q, s], ledger_status=OK_LEDGER)[0]
    assert row["evaluable"] is False and "decision_time_missing" in row["reasons"]


def test_latest_state_is_selected_by_receipt_and_its_age_uses_receipt():
    q = _quote("005490", "2026-10-14T00:01:00.2")
    old = _state("005490", "2026-10-14T00:00:59.8", "H0STMKO0", vi="1")
    old["received_at"] = "2026-10-14T00:00:58+00:00"
    newer = _state("005490", "2026-10-14T00:00:59.2", "H0STMKO0", vi="0")
    newer["received_at"] = "2026-10-14T00:00:59+00:00"
    row = join_decisions([_decision()], [newer, old, q], ledger_status=OK_LEDGER)[0]
    state = row["market_state_at_quote"]["H0STMKO0"]
    assert row["evaluable"] is True
    assert state["vi_cls_code"] == "0" and state["age_seconds"] == pytest.approx(1.2)


def test_latest_state_receipt_ties_are_not_arbitrarily_treated_as_proven():
    q = _quote("005490", "2026-10-14T00:01:00.2")
    s1 = _state("005490", "2026-10-14T00:00:59", "H0STMKO0", vi="0")
    s2 = _state("005490", "2026-10-14T00:00:59", "H0STMKO0", vi="1")
    row = join_decisions([_decision()], [q, s1, s2], ledger_status=OK_LEDGER)[0]
    assert row["evaluable"] is False and "market_state_receipt_time_ambiguous:H0STMKO0" in row["reasons"]


@pytest.mark.parametrize('stage,result_extra', [
    ('unavailable', {}),
    ('single_fallback', {'failed': True}),
    ('adversarial', {'verdict': 'unavailable'}),
])
def test_fail_open_is_not_counted_as_validated_model_approval(stage, result_extra, tmp_path):
    decision = _decision()
    decision['stage'] = stage
    decision['result'].update(result_extra)
    decision['inputs'] = {'neutral_sizing': {'schema_version': 'neutral-policy-sizing-v1'}}
    path = tmp_path / 'decisions.jsonl'
    path.write_text(json.dumps(decision) + '\n')
    payloads = [_quote('005490', '2026-10-14T00:01:00.2'),
                _state('005490', '2026-10-14T00:00:58.0', 'H0STMKO0')]
    rows = join_decisions(load_decisions(path), payloads, ledger_status=OK_LEDGER)
    assert rows[0]['approved'] is True and rows[0]['evaluable'] is True
    summary = summarize(rows, ledger_status=OK_LEDGER)
    assert summary['approved_evaluable'] == 0
    assert summary['engine_allowed_evaluable'] == 1
    assert rows[0]['failed'] == result_extra.get('failed')
    assert rows[0]['verdict'] == result_extra.get('verdict')
