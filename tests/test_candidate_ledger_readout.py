"""V02-R 판독 — 실제 기록 모듈이 쓴 원장 읽기·첫 스캔 규칙·날짜별 3분위·최소 표본 전 수익 비공개·판정."""
import csv
import gzip
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from scripts.research import candidate_ledger_readout as r
from src.analytics import candidate_ledger as cl


@pytest.fixture
def ledger_on(candidate_ledger_off, monkeypatch):
    monkeypatch.setenv("CANDIDATE_LEDGER", "1")
    yield
    assert cl.drain()


def _stock(symbol, score, **kw):
    return SimpleNamespace(symbol=symbol, score=score, **kw)


def test_reads_what_the_recorder_writes(tmp_path, ledger_on):
    basis = {"source_terms": [{"source_id": "kis_volume_rank", "weighted_score": 40.0}], "occurrence_bonus": 3.0}
    at = datetime(2026, 11, 16, 10, 0, 0)
    s1 = cl.record_scan([_stock("000001", 90, selection_basis=basis, has_foreign_buying=True), _stock("000002", 70)],
                        "regular", now=at, ledger_dir=tmp_path)
    cl.record_regime(s1, False, {"change_pct": -1.2}, None, now=at, ledger_dir=tmp_path)   # 차단, KOSDAQ 조회 실패
    cl.record_scan([_stock("000001", 50), _stock("000003", 60)], "next", now=datetime(2026, 11, 16, 16, 0), ledger_dir=tmp_path)
    cl.record_gate("deadbeef", gate="cash", ok=True, now=at, ledger_dir=tmp_path)           # 짝 없는 게이트 행
    cl.record_scan([_stock("000001", 80)], "regular", now=datetime(2026, 11, 17, 9, 30), ledger_dir=tmp_path)
    assert cl.drain()
    day1, day2 = tmp_path / "candidates_20261116.jsonl", tmp_path / "candidates_20261117.jsonl"
    day1.write_text(day1.read_text() + '{"kind":"scan","scan_id":"x","cand')               # 쓰다가 죽어 잘린 줄
    with gzip.open(f"{day2}.gz", "wt") as g:                                                # 판독이 끝난 달은 gzip 해도 읽힌다
        g.write(day2.read_text())
    day2.unlink()

    obs, stats = r.build_obs(str(tmp_path), "2026-11-17")
    by = {(o["date"], o["symbol"]): o for o in obs}
    first = by["2026-11-16", "000001"]
    assert first["score"] == 90 and first["sources"] == ["kis_volume_rank"] and first["bonus"] == 3.0   # 그날 첫 스캔의 점수
    assert first["regime_ok"] is False and first["foreign"] is True and first["inst"] is None and not first["after_close"]
    assert by["2026-11-16", "000002"]["sources"] is None                                    # 점수 구성 없음 ≠ 원천 0개
    assert by["2026-11-16", "000003"]["after_close"] and by["2026-11-17", "000001"]["score"] == 80
    assert {k: stats[k] for k in ("days", "scans", "symbol_days", "after_close", "skipped_lines", "orphan_gates",
                                  "regime_unobserved", "basis_rows", "candidate_rows")} == {
        "days": 2, "scans": 3, "symbol_days": 4, "after_close": 1, "skipped_lines": 1, "orphan_gates": 1,
        "regime_unobserved": 1, "basis_rows": 1, "candidate_rows": 5}
    assert stats["gates"]["regime"] == {"없음": 2, "차단": 1} and stats["sources"] == {"kis_volume_rank": 1}
    assert stats["null_rate"]["rsi"] == 1.0 and stats["null_rate"]["score"] == 0.0
    assert len(r.build_obs(str(tmp_path), "2026-11-16")[0]) == 3                            # 고정 종료일 뒤 파일은 읽지 않는다
    day2.write_text("")
    with pytest.raises(SystemExit):                                                         # 같은 날짜가 두 파일이면 멈춘다
        r.build_obs(str(tmp_path), "2026-11-17")


def test_day_terciles_ties_and_small_days():
    def rows(d, scores):
        return [({"date": d, "score": s}, {"i": i}) for i, s in enumerate(scores)]
    got = r.day_terciles(rows("a", [80, 80, 80, 90, 90, 90, 85, None]) + rows("b", [1, 2]) + rows("c", [5, 5, 5, 5]))
    assert sorted((g, v["i"]) for d, g, v in got if d == "a") == [(False, 0), (False, 1), (False, 2), (True, 3), (True, 4), (True, 5)]
    assert {d for d, _g, _v in got} == {"a"}   # 후보 3개 미만·전부 동점인 날은 뺀다


def _rows(days, per_day, edge_of_day, h=5):
    """날짜마다 점수 0..per_day-1, 점수 상위 3분위의 수익만 edge."""
    k = per_day // 3
    return [({"date": f"d{d:02d}", "score": s}, {f"ret{h}": edge_of_day(d) if s >= per_day - k else 0.0})
            for d in range(days) for s in range(per_day)]


@pytest.mark.parametrize("edge,expected", [(0.02, "선정 신호 있음"), (0.005, "미입증"), (-0.02, "음의 신호")])
def test_judge_thresholds(edge, expected):
    j = r.judge(_rows(15, 21, lambda d: edge), 5)
    assert j["verdict"] == expected and j["diff"] == pytest.approx(edge) and j["days_both"] == 15 and j["symbol_days"] == 315


def test_judge_needs_interval_clear_of_zero():
    j = r.judge(_rows(15, 21, lambda d: 0.10 if d % 2 == 0 else -0.07), 5)   # 평균 +2.07pp 지만 날마다 부호가 뒤집힌다
    assert j["diff"] > r.THRESHOLD[5] and j["ci95"][0] < 0 and j["verdict"] == "미입증"


@pytest.mark.parametrize("days,per_day", [(14, 30), (15, 19)])
def test_no_return_statistics_before_minimum_sample(days, per_day):
    assert r.judge(_rows(days, per_day, lambda d: 0.5), 5) == {
        "symbol_days": days * per_day, "days_both": days, "verdict": "판정 불가"}   # 차이·구간을 계산하지 않는다


def test_combine():
    assert r.combine(["선정 신호 있음", "선정 신호 있음"]) == "선정 신호 있음"
    assert r.combine(["선정 신호 있음", "판정 불가"]) == "선정 신호 있음(잠정 — 한 보유 기간만)"
    assert r.combine(["음의 신호", "미입증"]) == "음의 신호(잠정 — 한 보유 기간만)"
    assert r.combine(["선정 신호 있음", "음의 신호"]) == "미입증" and r.combine(["미입증", "판정 불가"]) == "미입증"
    assert r.combine(["판정 불가", "판정 불가"]) == "판정 불가"


def test_report_end_to_end_with_synthetic_prices(tmp_path):
    cal = [(date(2026, 11, 16) + timedelta(days=t)).isoformat() for t in range(25)]

    def write(name, close_of):
        with open(tmp_path / name, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "open", "high", "low", "close"])
            w.writerows([d, *[close_of(t)] * 4] for t, d in enumerate(cal))

    write(f"{r.BENCH}.csv", lambda t: 30000 + 100 * ((t * 7) % 6))
    with open(tmp_path / "adj.csv", "w", newline="") as f:
        csv.writer(f).writerows([["date", "close"], *[[d, 30000 + 100 * ((t * 7) % 6)] for t, d in enumerate(cal)]])
    for i in range(21):   # 점수가 높을수록 매일 더 오른다
        write(f"{i:06d}.csv", lambda t, i=i: round(1000 * (1 + 0.001 * i) ** t, 4))
    obs = [{"date": cal[d], "symbol": f"{i:06d}", "t": f"{cal[d]}T10:00:00", "after_close": False, "score": 50 + i,
            "sources": ["kis_volume_rank"] if i % 2 else [], "bonus": None, "foreign": None, "inst": None, "regime_ok": None}
           for d in range(16) for i in range(21)]
    obs.append({**obs[0], "symbol": "999999", "after_close": True})   # 장 마감 뒤 첫 등장 — 가격 파일이 없어도 된다

    out = r.report(obs, str(tmp_path), str(tmp_path / "adj.csv"), cal[-1])
    h5, h20 = out["primary"]["h5"], out["primary"]["h20"]
    assert h5["verdict"] == "선정 신호 있음" and h5["diff"] > 0.05 and h5["ci95"][0] > 0 and "note" not in h5
    assert h20 == {"symbol_days": 5 * 21, "days_both": 5, "verdict": "판정 불가"}   # 20일은 아직 5일치뿐 — 수익 통계 없음
    assert set(out["tradability"]) == set(out["exploratory"]) == {"h5"} and out["tradability"]["h5"]["net_positive"] is True
    assert out["verdict"] == "선정 신호 있음(잠정 — 한 보유 기간만)" and out["after_close_excluded"] == 1
    assert out["excluded"] == {"h20:horizon_not_reached": 11 * 21}
    src = out["exploratory"]["h5"]["원천 kis_volume_rank"]
    assert src["n_in"] == 16 * 10 and src["n_out"] == 16 * 11 and src["days_both"] == 16
    assert out["exploratory"]["h5"]["수급 표시"]["diff"] is None   # 표시를 모르는 후보는 비교에 넣지 않는다

    for name in (f"{r.BENCH}.csv", "adj.csv"):   # 벤치마크가 평탄하면 β 를 못 구한다 — '비용을 못 넘음'으로 적지 않는다
        (tmp_path / name).write_text((tmp_path / name).read_text().replace("30100", "30000").replace("30200", "30000")
                                     .replace("30300", "30000").replace("30400", "30000").replace("30500", "30000"))
    flat = r.report(obs, str(tmp_path), str(tmp_path / "adj.csv"), cal[-1])
    assert flat["tradability"]["h5"] is None and flat["primary"]["h5"]["note"] == "거래 가능성 판정 불가(β 추정 불가)"
