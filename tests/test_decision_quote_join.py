"""65차 — 판단 기록 ↔ 관측 원장 조인: 첫 호가·신선도·장 상태·평가 불가 사유 (합성 자료)."""
import json

from src.analytics.decision_quote_join import join_decisions, load_decisions, load_ledger_payloads, parse_time, summarize

DECIDED = "2026-10-14T09:01:00+09:00"              # = 00:01:00Z


def _decision(symbol="005490", decided=DECIDED, approved=True, did="dec-1"):
    return {"schema_version": "llm-decision-v1", "decision_id": did, "signal_id": "sig-1", "symbol": symbol,
            "stage": "adversarial", "result": {"approved": approved}, "decision_available_at": decided}


def _env(payload, row=1):
    return {"row": row, "previous_hash": "0" * 24, "kind": "record", "payload": payload, "hash": "x"}


def _quote(symbol, observed_at, ask="313000", ask_size=12, qid="q1"):
    return {"kind": "ws_quote", "symbol": symbol, "observed_at": observed_at, "ask": ask, "ask_size": ask_size,
            "bid": "312500", "bid_size": 40, "quote_id": qid, "provenance": {"exchange_time": "090100"}}


def _state(symbol, observed_at, status="raw_only", verdict="unknown"):
    return {"kind": "market_state", "symbol": symbol, "observed_at": observed_at, "market_state_status": status,
            "state_verdict": verdict, "market_cls_code": "2", "trht_yn": "N", "vi_cls_code": "0", "hour_cls_code": "0"}


def test_naive_ledger_times_are_utc_and_first_quote_after_decision_is_picked():
    payloads = [_quote("005490", "2026-10-14T00:00:59.5", qid="before"),      # 판단 0.5초 전 — 제외
                _quote("005490", "2026-10-14T00:01:00.2", qid="first"),
                _quote("005490", "2026-10-14T00:01:03.0", qid="later"),
                _state("005490", "2026-10-14T00:00:58.0")]
    rows = join_decisions([_decision()], payloads)
    r = rows[0]
    assert r["evaluable"] is True and r["reasons"] == []
    assert r["first_quote_after"]["quote_id"] == "first" and abs(r["first_quote_after"]["delay_seconds"] - 0.2) < 1e-6
    assert r["first_quote_after"]["ask"] == "313000" and r["market_state_at_quote"]["market_state_status"] == "raw_only"
    assert parse_time("2026-10-14T00:01:00.2", "x").tzinfo is not None


def test_reasons_cover_missing_stale_invalid_and_unknown_state():
    no_quote = join_decisions([_decision(symbol="000660")], [_quote("005490", "2026-10-14T00:01:00.2")])[0]
    assert no_quote["evaluable"] is False and no_quote["reasons"] == ["no_quote_after_decision"]
    stale = join_decisions([_decision()], [_quote("005490", "2026-10-14T00:02:30.0")], max_delay_seconds=60)[0]
    assert "stale_quote" in stale["reasons"] and "market_state_unknown" in stale["reasons"]
    bad = join_decisions([_decision()], [_quote("005490", "2026-10-14T00:01:01.0", ask="0", ask_size=0),
                                          _state("005490", "2026-10-14T00:00:50.0", status="invalid_values")])[0]
    assert set(bad["reasons"]) == {"invalid_ask", "invalid_ask_size", "market_state_not_raw"}
    assert bad["first_quote_after"]["ask"] is None and bad["first_quote_after"]["ask_size"] is None
    missing = join_decisions([_decision(decided=None)], [])[0]
    assert missing["reasons"] == ["decision_time_missing"]


def test_market_state_uses_last_record_at_or_before_quote():
    payloads = [_state("005490", "2026-10-14T00:00:30.0", verdict="a"), _state("005490", "2026-10-14T00:01:00.5", verdict="b"),
                _state("005490", "2026-10-14T00:01:02.0", verdict="c"), _quote("005490", "2026-10-14T00:01:01.0")]
    r = join_decisions([_decision()], payloads)[0]
    assert r["market_state_at_quote"]["state_verdict"] == "b" and abs(r["market_state_at_quote"]["age_seconds"] - 0.5) < 1e-6


def test_loaders_and_summary(tmp_path):
    dec = tmp_path / "decisions.jsonl"
    dec.write_text(json.dumps(_decision()) + "\n" + json.dumps(_decision(did="dec-2", approved=False)) + "\n")
    led = tmp_path / "engine.jsonl"
    led.write_text("\n".join(json.dumps(x) for x in (
        {"row": 1, "previous_hash": "0", "kind": "start", "payload": {"format": "entry-observation-journal-v2"}, "hash": "h"},
        _env(_quote("005490", "2026-10-14T00:01:00.2"), 2), _env(_state("005490", "2026-10-14T00:00:58.0"), 3),
        _env({"kind": "scan", "symbol": "005490", "observed_at": "2026-10-14T00:00:00"}, 4),
        {"row": 5, "previous_hash": "0", "kind": "seal", "payload": {}, "hash": "h"})) + "\n")
    rows = join_decisions(load_decisions(dec), load_ledger_payloads(led))
    s = summarize(rows)
    assert s["decisions"] == 2 and s["evaluable"] == 2 and s["approved_evaluable"] == 1 and s["reasons"] == {}
    assert "체결 가능성" in s["note"]
