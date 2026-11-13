"""V05 재판정 연구 스크립트 — 입력 구성·응답 해석·같은 날 비교 통계·비용 상한 정지."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from scripts.research import team_rejudge as t


def _row(symbol, at, stance="hold", approved=True, did="d1", reports=None):
    return {"deliberation_id": did, "symbol": symbol, "name": "가나", "decided_at": at, "recorded_at": at,
            "decision": {"stance": stance, "approved": approved},
            "reports": reports if reports is not None else [{"kind": "news", "symbol": symbol, "score": 3, "summary": "s",
                                                             "findings": [], "error": None, "ref_id": None}]}


def test_items_first_deliberation_and_dedup(tmp_path):
    f = tmp_path / "deliberations_20260916.jsonl"
    rows = [_row("000001", "2026-09-16T11:30:00", "buy", did="b"), _row("000001", "2026-09-16T10:30:00", "hold", did="a"),
            _row("000001", "2026-09-16T10:30:00", "buy", did="a"),   # 같은 id 는 마지막 행이 정본
            _row("000002", "2026-09-16T10:30:00", "buy", approved="yes", did="c")]
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    items = {i["id"]: i for i in t.build_items(str(tmp_path), "2026-09-16")}
    assert items["2026-09-16:000001"]["prod"] == "buy_approved" and items["2026-09-16:000002"]["prod"] == "invalid"
    assert items["2026-09-16:000001"]["reports"] == [{"kind": "news", "score": 3, "summary": "s"}]   # None·빈 값·내부 필드 제거
    assert t.build_items(str(tmp_path), "2026-09-15") == []


def test_compact_keeps_false_and_zero_marks_error():
    assert t.compact({"a": 0, "b": False, "c": None, "d": [], "error": "Traceback…", "m": {"x": None}}) == \
        {"a": 0, "b": False, "error": "자료 오류"}


def test_parse_judgment_strict():
    assert t.parse_judgment('```json\n{"decision":"buy","score":70,"reason":"r"}\n```') == ("buy", 70)
    for bad in ('{"decision":"hold","score":70}', '{"decision":"buy","score":70.5}', '{"decision":"buy","score":true}',
                '{"decision":"buy","score":101}', "없음", None, '["buy"]'):
        assert t.parse_judgment(bad) is None


def test_within_day_cancels_market_and_weights():
    rows = [("d1", True, {"net20": 0.10}), ("d1", False, {"net20": 0.06}), ("d1", False, {"net20": 0.08}),
            ("d2", True, {"net20": -0.20}), ("d2", False, {"net20": -0.22}),
            ("d3", True, {"net20": 0.5})]   # 한쪽만 있는 날은 빠진다
    parts = t.within_day(rows, "net20")
    assert [p[0] for p in parts] == ["d1", "d2"]
    assert abs(parts[0][1] - 0.03) < 1e-12 and abs(parts[1][1] - 0.02) < 1e-12
    assert abs(t.wmean(parts) - (0.03 * (2 / 3) + 0.02 * 0.5) / (2 / 3 + 0.5)) < 1e-12


def test_bootstrap_deterministic_and_verdict():
    parts = [(f"d{i:02d}", 0.03 + 0.001 * (i % 3), 1.0) for i in range(12)]
    lo, hi = t.block_bootstrap(parts, resamples=500)
    assert (lo, hi) == t.block_bootstrap(parts, resamples=500) and 0.03 <= lo <= hi <= 0.032
    assert t.verdict(0.031, lo, hi, 40, 60, 12) == "선별력 있음"
    assert t.verdict(0.031, lo, hi, 29, 60, 12) == "판정 불가(표본 부족)"
    assert t.verdict(0.010, 0.001, 0.02, 40, 60, 12) == "미입증"       # 기준 미달
    assert t.verdict(0.030, -0.001, 0.06, 40, 60, 12) == "미입증"      # 하한이 0 아래
    assert t.verdict(-0.030, -0.05, -0.01, 40, 60, 12) == "음의 선별"


def test_weighted_t_matches_weighted_point_estimate():
    from scripts.research.candidate_forward_excess import nw_t
    # 가중치가 큰 날이 음수면 가중 평균은 음수다 — t 도 같은 부호여야 한다(단순 평균은 양수)
    parts = [("d1", -0.02, 10.0), ("d2", 0.03, 1.0), ("d3", 0.03, 1.0), ("d4", -0.025, 9.0)]
    assert t.wmean(parts) < 0 < sum(p[1] for p in parts) / 4 and t.weighted_nw_t(parts, 1) < 0
    equal = [(f"d{i}", x, 2.0) for i, x in enumerate((0.01, 0.03, -0.01, 0.02, 0.04))]
    assert abs(t.weighted_nw_t(equal, 2) - nw_t([p[1] for p in equal], 2)) < 1e-9   # 같은 가중치면 단순 평균의 NW t
    assert t.weighted_nw_t(equal[:1], 2) is None


def test_split_by_day_median_excludes_ties():
    rows = [({"date": "d", "s": s}, {"net20": s}) for s in (1, 2, 2, 3)]
    assert sorted(g for _d, g, _v in t.split_by_day_median(rows, lambda i: i["s"])) == [False, True]


class _Client:
    def __init__(self, replies, yields=False):
        self.replies, self.calls, self.yields = list(replies), 0, yields

    async def complete(self, prompt, system, **kw):
        self.calls += 1
        if self.yields:
            await asyncio.sleep(0)   # 동시 요청이 실제로 겹치게 한다
        content, refusal = self.replies.pop(0)
        return SimpleNamespace(content=content, success=content is not None, refusal_detected=refusal, usage_known=True,
                               input_tokens=1000, output_tokens=100, request_sent=True, resolved_model=kw["model"],
                               model_identity_status="verified", finish_reason="end_turn", http_status=200,
                               transport_error_kind=None)


def _items(n):
    return [{"id": f"i{k}", "date": "2026-09-16", "symbol": f"{k:06d}", "name": "n", "decided_at": "2026-09-16T10:30:00",
             "reports": []} for k in range(n)]


def test_judge_retry_refusal_resume_and_budget(tmp_path):
    out = tmp_path / "j.jsonl"
    quote = lambda i, o: 0.01   # noqa: E731
    client = _Client([("잡음", False), ('{"decision":"pass","score":40,"reason":""}', False),   # 1번: 형식 오류 뒤 재시도 성공
                      (None, True)])                                                            # 2번: 거절은 재시도 없음
    r = asyncio.run(t.judge(_items(2), "claude-opus-5-5", str(out), 1.0, concurrency=1, client=client, quote=quote))
    recs = {x["id"]: x for x in map(json.loads, out.read_text().splitlines())}
    assert client.calls == 3 and recs["i0"]["status"] == "ok" and recs["i0"]["attempts"] == 2 and recs["i0"]["decision"] == "pass"
    assert recs["i1"]["status"] == "refusal" and recs["i1"]["attempts"] == 1 and not r["budget_stopped"]
    assert (r["attempted_now"], r["recorded_now"], r["remaining"], r["spent_est_usd"]) == (2, 2, 0, "0.0300")
    # 이어 하기: 이미 있는 항목은 다시 보내지 않고, 남은 예산이 재시도 포함 최악값(0.02)보다 작으면 전송하지 않는다
    client2 = _Client([])
    r2 = asyncio.run(t.judge(_items(3), "claude-opus-5-5", str(out), 0.04, concurrency=1, client=client2, quote=quote))
    assert client2.calls == 0 and r2["budget_stopped"] and len(out.read_text().splitlines()) == 2
    assert (r2["attempted_now"], r2["recorded_now"], r2["remaining"]) == (0, 0, 1)   # 보내지 않은 것은 보낸 것으로 세지 않는다
    # 다른 모델의 파일·중복 행에는 덧붙이지 않는다(호출 전에 중단)
    for model, lines in (("claude-fable-5-2", out.read_text()), ("claude-opus-5-5", out.read_text() * 2)):
        bad = tmp_path / f"bad-{model}.jsonl"
        bad.write_text(lines)
        client3 = _Client([])
        with pytest.raises(SystemExit):
            asyncio.run(t.judge(_items(3), model, str(bad), 1.0, concurrency=1, client=client3, quote=quote))
        assert client3.calls == 0


def test_judge_waits_for_temporary_hold_instead_of_stopping(tmp_path):
    """동시 요청의 보류분 때문에 잠시 모자란 것은 정지가 아니다 — 반환되면 이어서 보낸다."""
    out = tmp_path / "j.jsonl"
    ok = ('{"decision":"pass","score":40,"reason":""}', False)
    client = _Client([ok, ok, ok], yields=True)
    # 요청당 실제 0.01·보류 0.02. 상한 0.045 는 셋을 동시에 보류하지 못하지만(0.06) 앞 요청이 끝나면 셋 모두 가능(실제 0.03).
    r = asyncio.run(t.judge(_items(3), "claude-opus-5-5", str(out), "0.045", concurrency=3, client=client,
                            quote=lambda i, o: 0.01))
    assert (r["attempted_now"], r["recorded_now"], r["remaining"], r["budget_stopped"]) == (3, 3, 0, False)
    assert [json.loads(x)["est_usd"] for x in out.read_text().splitlines()] == ["0.01"] * 3   # 금액은 문자열로 남는다


def test_report_end_to_end(tmp_path):
    import csv
    days = [f"2026-09-{d:02d}" for d in range(1, 31)]
    prices = tmp_path / "p"
    prices.mkdir()

    def write(sym, closes):
        with open(prices / f"{sym}.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "open", "high", "low", "close"])
            for d, c in zip(days, closes):
                w.writerow([d, c, c, c, c])
    write(t.BENCH, [100] * 30)
    with open(tmp_path / "adj.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "close"])
        w.writerows([d, 100] for d in days)
    items, js = [], []
    for k in range(4):   # 짝수 종목은 20일 뒤 +10%, 홀수는 -10%. 모델은 짝수만 buy
        sym, up = f"{k:06d}", k % 2 == 0
        write(sym, [100] * 5 + [110 if up else 90] * 25)
        for d in days[:3]:
            items.append({"id": f"{d}:{sym}", "date": d, "symbol": sym, "prod": "hold", "analyst_sum": k})
            js.append({"id": f"{d}:{sym}", "status": "ok", "decision": "buy" if up else "pass", "score": 80 if up else 20,
                       "in": 10, "out": 5, "est_usd": 0.001, "model": "claude-opus-5-5", "prompt_version": t.PROMPT_VERSION,
                       "identity": "verified", "resolved": "claude-opus-5-5"})
    out = t.report(items, {"claude-opus-5-5": js}, str(prices), str(tmp_path / "adj.csv"), days[-1])
    h20 = out["models"]["claude-opus-5-5"]["decision"]["h20"]
    assert abs(h20["diff"] - 0.2) < 1e-9 and h20["days_both"] == 3 and h20["verdict"] == "판정 불가(표본 부족)"
    assert abs(out["models"]["claude-opus-5-5"]["score_high"]["h20"]["diff"] - 0.2) < 1e-9
    assert out["models"]["claude-opus-5-5"]["buy_share"] == 0.5 and out["with_net"]["h20"] == 12
    assert out["models"]["claude-opus-5-5"]["est_usd_conservative"] == "0.0120"
    assert out["models"]["claude-opus-5-5"]["complete"] is True
    # 비용 상한으로 멈춘 부분 표본은 그 항목만으로 보고한다(사전 등록 5절)
    part = t.report(items, {"claude-opus-5-5": js[:8]}, str(prices), str(tmp_path / "adj.csv"), days[-1])["models"]["claude-opus-5-5"]
    assert (part["complete"], part["judged"], part["decision"]["h20"]["n_in"] + part["decision"]["h20"]["n_out"]) == (False, 8, 8)
    assert part["judged_ids_sha256"] != out["models"]["claude-opus-5-5"]["judged_ids_sha256"]
    # 판정 파일이 기대 표본과 다르면(표본 밖 id·중복·다른 모델·미검증 신원) 집계를 거부한다
    for bad in (js + [{**js[0], "id": "2026-09-01:999999"}], js + js[:1], [{**j, "model": "claude-fable-5-2"} for j in js],
                [{**j, "identity": "unverified"} for j in js], [{**j, "prompt_version": "x"} for j in js]):
        with pytest.raises(SystemExit):
            t.report(items, {"claude-opus-5-5": bad}, str(prices), str(tmp_path / "adj.csv"), days[-1])


def test_doc_table_matches_committed_summary():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "docs/research"
    summary = json.loads((root / "v05-team-rejudge-summary-2026-11-13.json").read_text())
    doc = (root / "v05-team-rejudge-2026-11-13.md").read_text()
    rows = t.doc_rows(summary)
    assert len(rows) == 18 and all(r in doc for r in rows)
    assert all(v["primary"] == "판정 불가(표본 부족)" for v in summary["models"].values())
    assert set(summary["inputs"]) == {"items_sha256", "judgments_sha256", "prices_sha256", "bench_adjust_sha256"}
