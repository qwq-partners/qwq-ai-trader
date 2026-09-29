"""크로스 검증 가점/감점 분리 (2026-09-29)

결함: 가점 규칙(+5 등)이 `penalties` 목록에 섞여 있어 점수가 **오른** 신호도 통계·로그·
signal_events 사유·대시보드·게이트 성적표에서 '감점'으로 집계됐다(1주 '감점' 63건 중 32건이 상승).
결정: DB 스키마·event_type 무변경. 방향은 저장된 점수로 판정한다 —
가점 = penalized ∧ block_gate='G2_cross' ∧ adjusted_score > score. G4_llm soft-reject 행은
score(G2 전) < adjusted(G2 후) 여도 감점이다.

운영 캐시·네트워크·DB 무접촉 — 시계는 동결, DB 는 sqlite 메모리로 실제 SQL 을 실행.
"""
import asyncio
import datetime as _datetime_mod
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, Path(__file__).parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import src.core.cross_validator as cv_mod  # noqa: E402
from src.analytics.gate_performance import GatePerformanceAnalyzer  # noqa: E402
from src.data.storage.signal_event_storage import (  # noqa: E402
    BOOST_SQL, SignalEventStorage, is_cross_boost,
)
from test_entry_risk_lifecycle import _order_path  # noqa: E402
from test_risk_sizing import _em, _rm, _sig, home  # noqa: E402,F401

FIXED = datetime(2026, 9, 29, 14, 0)   # 시간대 규칙(09:00~10:30·12:30~13:00) 밖


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED if tz is None else FIXED.replace(tzinfo=tz)


# ── cross_validator: 통계·로그 ───────────────────────────────────────────────

def _validate(monkeypatch, memory_adj, metadata_extra=None):
    monkeypatch.setattr(cv_mod, "datetime", _Frozen)
    monkeypatch.setattr(_datetime_mod, "datetime", _Frozen)   # validate 내부 `from datetime import datetime as _dt`
    cv = cv_mod.CrossStrategyValidator(
        market="KR",
        trade_memory=SimpleNamespace(get_score_adjustment=lambda _s, _sec: memory_adj),
    )
    cv._load_panel_outlook = lambda: None   # 운영 캐시 무접촉
    meta = {"atr_pct": 3.0, "per": 10.0, "pbr": 1.0, "foreign_net_buy": 1.0}   # 지표결손 감점 없음
    meta.update(metadata_extra or {})
    msgs = []
    hid = logger.add(lambda m: msgs.append(str(m)), level="INFO")
    try:
        ok, adj, _ = cv.validate(symbol="005930", side="buy", strategy="sepa_trend",
                                 score=70, metadata=meta, market_regime="bull")
    finally:
        logger.remove(hid)
    logs = [m for m in msgs if "[크로스검증] 005930" in m]
    return ok, adj, cv.get_stats(), logs


def test_boost_only_counts_boosted_and_logs_gain(monkeypatch):
    ok, adj, st, logs = _validate(monkeypatch, +3)
    assert ok and adj == 73
    assert (st["boosted"], st["penalized"], st["passed"]) == (1, 0, 1)
    assert len(logs) == 1 and "가점: 70→73" in logs[0] and "감점" not in logs[0]


def test_penalty_only_counts_penalized_and_logs_loss(monkeypatch):
    ok, adj, st, logs = _validate(monkeypatch, -3)
    assert ok and adj == 67
    assert (st["boosted"], st["penalized"]) == (0, 1)
    assert len(logs) == 1 and "감점: 70→67" in logs[0]


def test_offset_counts_neither_and_logs_once(monkeypatch):
    # 외국인 매수 상위 섹터 +5 와 메모리 -5 가 상쇄
    ok, adj, st, logs = _validate(monkeypatch, -5, {"sector": "반도체", "foreign_top_sectors": ["반도체"]})
    assert ok and adj == 70
    assert (st["boosted"], st["penalized"]) == (0, 0)
    assert len(logs) == 1 and "조정 상쇄: 70→70" in logs[0]


# ── engine: signal_events 사유 ───────────────────────────────────────────────

@pytest.mark.parametrize("delta, word", [(+3, "가점"), (-3, "감점")])
def test_engine_g2_reason_follows_direction(home, monkeypatch, delta, word):
    rm = _rm(monkeypatch, mode="risk", em=_em())
    sig = _sig(2.5)
    sig.score = 60.0
    _, logged = _order_path(monkeypatch, rm, sig, cv_delta=delta)
    g2 = [e for e in logged if e.get("block_gate") == "G2_cross"]
    assert len(g2) == 1
    assert g2[0]["event_type"] == "penalized"
    assert g2[0]["block_reason"] == f"크로스 검증 {word} 60→{60 + delta}"
    assert (g2[0]["score"], g2[0]["adjusted_score"]) == (60.0, 60.0 + delta)


# ── signal_event_storage: 요약·필터 (실제 SQL 을 sqlite 에서 실행) ─────────────

ROWS = [
    # symbol, event_type, block_gate, score, adjusted_score
    ("G2UP", "penalized", "G2_cross", 60.0, 63.0),    # 가점
    ("G2DN", "penalized", "G2_cross", 60.0, 55.0),    # 감점
    ("G4UP", "penalized", "G4_llm", 80.0, 88.0),      # G2 가점 뒤 G4 soft-reject → 감점
    ("PASS", "passed", None, 70.0, 70.0),
    ("BLK", "blocked", "G2_cross", 60.0, 40.0),
]
_PG_WINDOW = "WHERE event_time >= NOW() - ($1 || ' days')::interval"


def _sqlite_storage():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE signal_events (id INTEGER PRIMARY KEY, event_time TEXT, symbol TEXT,
                  name TEXT, strategy TEXT, score REAL, adjusted_score REAL, side TEXT, event_type TEXT,
                  block_gate TEXT, block_reason TEXT, market_regime TEXT, sector TEXT)""")
    for i, (sym, et, gate, sc, adj) in enumerate(ROWS):
        db.execute("INSERT INTO signal_events (event_time, symbol, score, adjusted_score, side, event_type,"
                   " block_gate) VALUES (?,?,?,?,'buy',?,?)", (f"2026-09-29T10:{i:02d}", sym, sc, adj, et, gate))

    class _Conn:
        async def fetchrow(self, sql, *_a):   # get_stats 요약 — 기간 조건만 떼고 그대로 실행
            assert _PG_WINDOW in sql
            return db.execute(sql.replace(_PG_WINDOW, "")).fetchone()
        async def fetch(self, sql, *a):
            if "::interval" in sql:           # get_stats 의 게이트/전략/일별 집계 — 여기선 대상 아님
                return []
            return db.execute(re.sub(r"\$(\d)", r":p\1", sql),
                              {f"p{i + 1}": v for i, v in enumerate(a)}).fetchall()
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    st = SignalEventStorage.__new__(SignalEventStorage)
    st._pool = SimpleNamespace(acquire=lambda: _Conn())

    async def _ensure_init(): return None
    st._ensure_init = _ensure_init
    return st


def test_storage_summary_splits_boost_from_penalty():
    stats = asyncio.run(_sqlite_storage().get_stats(days=7))
    assert (stats["total_buy"], stats["passed"], stats["blocked"]) == (5, 1, 1)
    assert (stats["penalized"], stats["boosted"]) == (2, 1)


@pytest.mark.parametrize("etype, expect", [
    ("boosted", {"G2UP"}),
    ("penalized", {"G2DN", "G4UP"}),          # G4_llm 은 점수가 올라 있어도 감점
    (None, {r[0] for r in ROWS}),
    ("blocked", {"BLK"}),
])
def test_storage_filter_splits_boost_from_penalty(etype, expect):
    rows = asyncio.run(_sqlite_storage().get_recent(limit=50, event_type=etype))
    assert {r["symbol"] for r in rows} == expect


def test_python_predicate_matches_sql():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE signal_events (symbol TEXT, event_type TEXT, block_gate TEXT, score REAL,"
               " adjusted_score REAL)")
    db.executemany("INSERT INTO signal_events VALUES (?,?,?,?,?)", ROWS)
    sql_hits = {r[0] for r in db.execute(f"SELECT symbol FROM signal_events WHERE {BOOST_SQL}")}
    py_hits = {sym for sym, et, gate, sc, adj in ROWS
               if is_cross_boost({"event_type": et, "block_gate": gate, "score": sc, "adjusted_score": adj})}
    assert sql_hits == py_hits == {"G2UP"}


# ── gate_performance: 버킷 ───────────────────────────────────────────────────

def test_gate_performance_buckets_boost_separately():
    ga = GatePerformanceAnalyzer.__new__(GatePerformanceAnalyzer)
    ga.horizon_days = 20
    t0 = datetime(2026, 8, 3, 10, 0)
    sigs = [{"symbol": f"00000{i}", "name": "", "strategy": "sepa_trend", "score": sc,
             "adjusted_score": adj, "event_type": et, "block_gate": gate, "block_reason": "",
             "market_regime": "bull", "event_time": t0, "metadata": {}}
            for i, (_sym, et, gate, sc, adj) in enumerate(ROWS)]

    async def _fetch(_lb): return sigs
    ga._fetch_signals = _fetch
    ga._load_prices = lambda symbols, _s, _e: {s: object() for s in symbols}
    ga._forward_return = lambda _df, _t: 1.0
    ga._save = lambda _r: None

    res = asyncio.run(ga.analyze(lookback_days=90))
    assert {g: v["samples"] for g, v in res["gates"].items()} == {
        "BOOST_G2_cross": 1, "PEN_G2_cross": 1, "PEN_G4_llm": 1,
        "PASSED(대조군)": 1, "G2_cross": 1,
    }


def _g(avg, n=30):
    return {"samples": n, "avg_return": avg, "avg_excess": avg, "avg_clipped": avg,
            "median_return": avg, "opportunity_loss_cnt": 0, "opportunity_loss_pct": 40.0,
            "avoided_cnt": 0, "avoided_pct": 10.0, "best": None, "worst": None}


def test_adjusted_pass_buckets_get_neutral_verdict_and_bands():
    """점수 조정 후 통과 버킷(BOOST_/PEN_, |wiki 포함)에는 차단형 권고가 붙지 않는다 — 차단 게이트는 그대로."""
    gates = {"PASSED(대조군)": _g(1.0), "BOOST_G2_cross": _g(5.0), "BOOST_G2_cross|wiki": _g(-5.0),
             "PEN_G2_cross": _g(5.0), "G2_cross": _g(5.0), "G1_regime": _g(-5.0)}
    ga = GatePerformanceAnalyzer.__new__(GatePerformanceAnalyzer)
    lines = {v.split(":")[0].lstrip("⚠️✅➖ "): v for v in ga._build_verdicts(gates) if not v.startswith("[")}
    for b in ("BOOST_G2_cross", "BOOST_G2_cross|wiki", "PEN_G2_cross"):
        assert "판정 대상 아님" in lines[b], lines[b]
        assert not any(w in lines[b] for w in ("완화 검토", "선별 효과", "차단 신호")), lines[b]
    assert "완화 검토" in lines["G2_cross"] and "선별 효과" in lines["G1_regime"]   # 대조군: 차단 게이트 문구 유지

    report = GatePerformanceAnalyzer.format_report(
        {"lookback_days": 90, "horizon_days": 20, "total_analyzed": 0, "verdicts": [], "gates": gates})
    detail = {ln.split(":")[0]: ln for ln in report.splitlines() if "건 | 평균" in ln}
    assert "+3% 이상 40%" in detail["PEN_G2_cross"] and "기회손실" not in detail["PEN_G2_cross"]
    assert "-3% 이하 10%" in detail["BOOST_G2_cross|wiki"]
    assert "기회손실 40%" in detail["G2_cross"]


def test_adjusted_pass_bucket_small_samples():
    """표본 부족 검사가 중립 분기보다 먼저 — 없는 대조군 비교값(+0.00%)을 찍지 않는다."""
    ga = GatePerformanceAnalyzer.__new__(GatePerformanceAnalyzer)
    one = ga._build_verdicts({"PASSED(대조군)": _g(1.0), "BOOST_G2_cross": _g(5.0, n=1)})
    assert one == ["[대조군] 통과 신호 초과 +1.00% (30건, 고유 symbol-day)",
                   "BOOST_G2_cross: 표본 부족 (1건 < 30) — 판단 보류"]
    thin = ga._build_verdicts({"PASSED(대조군)": _g(1.0, n=29), "BOOST_G2_cross": _g(5.0)})
    assert len(thin) == 1 and "대조군 표본 부족" in thin[0] and "+0.00%" not in thin[0], thin
