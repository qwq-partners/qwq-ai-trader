"""65차 — 판단 기록 ↔ 관측 원장 조인: 첫 호가·신선도·같은 연결의 채널별 장 상태·원장 완전성·평가 불가 사유 (합성 자료)."""
import json

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
            "provenance": {"exchange_time": "090100", **conn}}


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
    return dict(parsed, kind="market_state", symbol=symbol, observed_at=observed_at, **conn)


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
