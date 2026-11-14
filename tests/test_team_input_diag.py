"""팀 심의 입력 집계 — 중복 제거·종료일·자료 상태·기술 오류 최다 종목 분리, 원장 문자열·종목 코드 미출력."""
import json

from scripts.research import team_input_diag as t


def _row(did, symbol, slot, tech_error=None, calls=2):
    return {"deliberation_id": did, "symbol": symbol, "slot": slot, "model_calls": [{}] * calls,
            "decision": {"stance": "hold", "approved": tech_error is None}, "assessment": {"data_sufficiency": "partial"},
            "reports": [{"kind": "fundamental", "data_status": "insufficient", "score": 10, "confidence": 0.7,
                         "summary": "비밀요약", "limitations": ["캐시 시각 미제공", "비밀한계"],
                         "metrics": {"foreign_net_buying": False}, "evidence": [{}]},
                        {"kind": "technical", "data_status": "error" if tech_error else "full", "score": 0,
                         "error": tech_error, "evidence": []}]}


def test_summary(tmp_path):
    (tmp_path / "deliberations_20261112.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        _row("a", "000001", "10:30", "지표 없음"), _row("a", "000001", "10:30", "지표 없음", calls=3),   # 같은 id → 마지막 행
        _row("b", "000001", "11:30", "지표 없음"), _row("c", "000002", "10:30", "비밀오류"), _row("d", "000003", "10:30")]) + "{잘린 줄")
    (tmp_path / "deliberations_20261113.jsonl").write_text(json.dumps(_row("e", "000009", "10:30")) + "\n")
    rows, digest = t.load(str(tmp_path), "2026-11-12")
    s = t.summarize(rows)
    assert (s["rows"], s["days"], s["symbols"], s["debate_turns"]) == (4, 1, 3, 9)
    assert s["kinds"]["fundamental"]["status"] == {"insufficient": 4} and s["kinds"]["fundamental"]["always_false_metrics"] == ["foreign_net_buying"]
    assert s["kinds"]["fundamental"]["score_conf"] == {"10|0.7": 4}
    assert s["kinds"]["fundamental"]["limits"] == {"cache_time_missing": 4, "other": 4}
    assert s["kinds"]["technical"]["error"] == {"no_indicators": 2, "other": 1}
    dumped = json.dumps(s, ensure_ascii=False)   # 원장의 문자열·종목 코드는 결과에 실리지 않는다
    assert not any(x in dumped for x in ("비밀", "000001", "000002", "000003"))
    assert s["tech_error"] == {"total": 3, "symbols": 2, "top_symbol": {"errors": 2, "rows": 2, "debate_turns": 5},
                               "by_slot_top_symbol": {"10:30": 1, "11:30": 1}, "by_slot_others": {"10:30": 1, "11:30": 0},
                               "by_day_others": {"2026-11-12": 1}}
    assert s["decision"] == {"기술 오류": {"hold|False": 3}, "기술 정상": {"hold|True": 1}}
    assert t.load(str(tmp_path), "2026-11-13")[1] != digest and len(t.load(str(tmp_path), "2026-11-13")[0]) == 5
