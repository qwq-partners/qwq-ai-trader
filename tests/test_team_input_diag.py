"""팀 심의 입력 집계 — 중복 제거·종료일·자료 상태·기술 오류 최다 종목 분리, 원장 문자열·종목 코드 미출력."""
import json
import stat

from scripts.research import team_input_diag as t


def _row(did, symbol, slot, tech_error=None, calls=2):
    return {"deliberation_id": did, "symbol": symbol, "slot": slot, "model_calls": [{}] * calls,
            "decision": {"stance": "hold", "approved": tech_error is None}, "assessment": {"data_sufficiency": "partial"},
            "reports": [{"kind": "fundamental", "data_status": "insufficient", "score": 10, "confidence": 0.7,
                         "summary": "비밀요약", "limitations": ["캐시 시각 미제공", "비밀한계"],
                         "metrics": {"foreign_net_buying": False}, "evidence": [{}]},
                        {"kind": "technical", "data_status": "error" if tech_error is not None else "full", "score": 0,
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


def test_missing_id_rows_merge_like_the_production_loader_and_bad_scores_are_counted(tmp_path):
    rows = [_row(None, "000001", "10:30"), _row(None, "000002", "10:30")]   # id 없는 행은 운영 로더처럼 하나로 묶인다
    rows[1]["reports"][0]["score"] = None
    rows[1]["reports"][1]["score"] = "높음"
    (tmp_path / "deliberations_20261112.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows) + "[1, 2]\n")
    s = t.summarize(t.load(str(tmp_path), "2026-11-12")[0])
    assert s["rows"] == 1 and s["kinds"]["fundamental"]["score_sign"] == {"비정상": 1} == s["kinds"]["technical"]["score_sign"]


def test_output_is_private_even_when_the_file_already_exists(tmp_path):
    out = tmp_path / "summary.json"
    out.write_text("old")
    out.chmod(0o644)
    t.write_private(str(out), {"a": 1})
    assert stat.S_IMODE(out.stat().st_mode) == 0o600 and json.loads(out.read_text()) == {"a": 1}


def test_no_ledger_value_reaches_the_output(tmp_path):
    """허용 목록 밖의 값은 어느 필드에 있어도 결과에 실리지 않는다(비정상 행·빈 문자열 오류 포함)."""
    bad = _row("x", "비밀종목", "비밀슬롯", "")   # 빈 문자열 오류도 오류다
    bad["assessment"] = {"data_sufficiency": "비밀충분성"}
    bad["decision"] = {"stance": "비밀판정", "approved": "비밀승인"}
    bad["reports"][0].update(score="비밀점수", confidence="비밀신뢰", data_status="비밀상태", metrics={"비밀지표": False})
    bad["reports"].append({"kind": "비밀종류", "data_status": "full", "score": float("inf")})
    bad["reports"].append("비밀보고")
    (tmp_path / "deliberations_20261112.jsonl").write_text(json.dumps(bad, ensure_ascii=False) + "\n")
    s = t.summarize(t.load(str(tmp_path), "2026-11-12")[0])
    assert "비밀" not in json.dumps(s, ensure_ascii=False)
    assert s["kinds"]["fundamental"]["score_conf"] == {"비정상": 1} and s["kinds"]["other"]["score_sign"] == {"비정상": 1}
    assert s["decision"] == {"기술 오류": {"other|other": 1}} and s["data_sufficiency"] == {"other": 1}
    assert s["tech_error"]["by_slot_top_symbol"] == {"other": 1} and s["kinds"]["technical"]["error"] == {"other": 1}
