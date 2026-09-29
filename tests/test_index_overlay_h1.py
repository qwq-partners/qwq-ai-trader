"""H1 지수 오버레이 — 미래정보 없음·비용 계산 (합성 입력, 네트워크 없음)."""
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

_spec = importlib.util.spec_from_file_location(
    "index_overlay_h1", Path(__file__).resolve().parents[1] / "scripts/research/index_overlay_h1.py")
h1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h1)


def _close(n=400, seed=1):
    rng = np.random.default_rng(seed)
    return pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.02, n)), index=pd.bdate_range("2020-01-01", periods=n))


def test_weights_at_t_do_not_see_future_closes():
    rng = np.random.default_rng(2)             # 완만한 저변동 상승 — 폭락 전날 비중이 확실히 1
    c = pd.Series(100 * np.cumprod(1 + 0.001 + rng.normal(0, 0.003, 400)),
                  index=pd.bdate_range("2020-01-01", periods=400))
    k = 300
    shocked = c.copy()
    shocked.iloc[k:] *= 0.5                      # t≥k 폭락 — t<k 비중은 그대로여야 한다
    a, b = h1.weights(c), h1.weights(shocked)
    pd.testing.assert_frame_equal(a.iloc[:k], b.iloc[:k])
    # 그 폭락일 수익은 전날 비중으로 받는다 (당일 비중으로 피하지 못한다)
    ra, rb = h1.simulate(c, a["H1c"]), h1.simulate(shocked, b["H1c"])
    day = c.index[k]
    assert b["H1c"].iloc[k - 1] == 1.0 and b["H1c"].iloc[k] == 0.0   # 전제: 폭락일 당일 비중은 이미 0
    assert rb[day] == b["H1c"].iloc[k - 1] * (shocked.iloc[k] / shocked.iloc[k - 1] - 1) - h1.COST * abs(
        b["H1c"].iloc[k - 1] - b["H1c"].iloc[k - 2])
    assert ra[:day].iloc[:-1].equals(rb[:day].iloc[:-1])


def test_buy_and_hold_has_no_cost_and_matches_index():
    c = _close()
    ret = h1.simulate(c, h1.weights(c)["B0"])
    assert np.isclose((1 + ret).prod(), c.iloc[-1] / c.iloc[0])   # 첫 수익(1일차)부터 전날 비중 1


def test_cost_is_charged_on_weight_change():
    c = pd.Series([100.0] * 6, index=pd.bdate_range("2024-01-01", periods=6))
    w = pd.Series([1.0, 1.0, 0.0, 0.0, 1.0, 1.0], index=c.index)
    ret = h1.simulate(c, w)                    # 가격 불변 → 수익은 비용뿐
    assert np.isclose(ret.sum(), -2 * h1.COST)
