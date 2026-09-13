"""2026-09-13 전략·아키텍처 리뷰가 잡은 계측 결함 회귀 테스트

- counterfactual 추적기: 미가격 항목 우선·오래된 순 (상위 50 슬롯 점유로 신규 126건 미처리)
- 초과수익 요약 라인 (KODEX200 대비 x5)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analytics.counterfactual_tracker import CounterfactualTracker  # noqa: E402


def test_pending_order_prioritizes_unpriced_then_oldest():
    state = {
        "a": {"date": "2026-08-14", "entry_px": 100.0, "r20": None},   # 가격 있음, r20 대기
        "b": {"date": "2026-09-10", "entry_px": None, "r20": None},    # 미가격(최신)
        "c": {"date": "2026-09-01", "entry_px": None, "r20": None},    # 미가격(구)
        "d": {"date": "2026-07-01", "entry_px": 50.0, "r20": 1.0},     # 완료 → 제외
    }
    order = [k for k, _ in CounterfactualTracker._pending_order(state)]
    assert order == ["c", "b", "a"]


def test_rows_parses_kis_daily_bars():
    rows = CounterfactualTracker._rows([
        {"stck_bsop_date": "20260901", "stck_clpr": "1000"},
        {"date": "20260902", "close": 1010},
        {"date": "bad", "close": 5},
    ])
    assert rows == [("2026-09-01", 1000.0), ("2026-09-02", 1010.0)]


def test_summary_appends_excess_line(monkeypatch, tmp_path):
    t = object.__new__(CounterfactualTracker)
    t._state = {
        "k1": {"source": "rule11", "r5": 1.0, "x5": -2.0},
        "k2": {"source": "rule11", "r5": 3.0, "x5": 1.0},
        "k3": {"source": "team_hold", "r5": 0.5, "x5": None},
    }
    monkeypatch.setattr(t, "_summary_base", lambda: "base")
    out = t.summary()
    assert out.startswith("base") and "KODEX200 대비 초과(r5): rule11 n=2 x5 -0.50% (음수 50%)" in out
