"""H3 가치·퀄리티 — 미래정보 없음·접수일 시점·비중 변화분 비용 (합성 입력, 네트워크 없음)."""
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_spec = importlib.util.spec_from_file_location(
    "index_plus_h3", Path(__file__).resolve().parents[1] / "scripts/research/index_plus_h3.py")
h3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h3)
h2 = h3.h2

CAL = pd.bdate_range("2019-01-01", periods=500)
ME = h2.month_ends(CAL)
CODES = [f"{i:05d}0" for i in range(12)]
SHARES = pd.Series([1e6 * (1 + i % 5) for i in range(12)], index=CODES)


def _daily(seed, drift):
    rng = np.random.default_rng(seed)
    c = 100 * np.cumprod(1 + drift + rng.normal(0, 0.01, len(CAL)))
    return pd.DataFrame({"open": c * 1.001, "close": c, "volume": 1e5}, index=CAL)


def _panel(dailies):
    cols = {c: {} for c in h3.PANEL_COLS}
    for code, d in dailies.items():
        f = h2.features(d, CAL, ME)
        for c in cols:
            cols[c][code] = f[c]
    return {c: pd.DataFrame(v) for c, v in cols.items()}


def _fund(rows):
    f = pd.DataFrame(rows, columns=["code", "fy", "rcept_date", "equity", "net_income"])
    f["rcept_date"] = pd.to_datetime(f["rcept_date"])
    return f


def _base_fund():
    rows = []
    for i, c in enumerate(CODES):
        rows.append((c, 2019, "2020-03-30", 1e8 * (1 + (i * 7) % 11), 1e7 * (1 + (i * 5) % 9)))
        rows.append((c, 2020, "2020-10-15", 1e8 * (1 + (i * 3) % 11), 1e7 * (1 + (i * 2) % 9)))
    return _fund(rows)


@pytest.fixture(autouse=True)
def _small_universe(monkeypatch):
    monkeypatch.setattr(h3, "UNIVERSE_N", 8)          # 12 종목 중 근사 시총 상위 8


def test_selection_at_t_ignores_future_prices_and_filings():
    base = {c: _daily(i, 0.0004 * (i - 6)) for i, c in enumerate(CODES)}
    k = int(np.searchsorted(CAL[ME], pd.Timestamp("2020-08-31")))
    t = CAL[ME[k]]
    assert t == pd.Timestamp("2020-08-31") and ME[k] >= 250
    shocked = {}
    for j, (c, d) in enumerate(base.items()):
        s = d.copy()
        s.iloc[ME[k] + 1:, :2] *= (0.2 if j % 2 else 5.0)         # t 다음 날부터 가격 폭등/폭락
        shocked[c] = s
    fa = _base_fund()
    fb = fa.copy()
    late = fb["fy"] == 2020                                         # t 뒤 접수된 재무를 뒤집는다
    fb.loc[late, "equity"] *= 0.1
    fb.loc[late, "net_income"] *= -3.0
    fb = pd.concat([fb, _fund([(c, 2019, "2020-09-15", 1e12, -1e12) for c in CODES])])  # t 뒤 정정 공시
    pa, pb = _panel(base), _panel(shocked)
    prm = dict(top_n=4, score="combo", weight="cap")
    for kk in range(k - 2, k + 1):                                # t 이하 월말의 선택·비중이 같다
        tt = CAL[ME[kk]]
        ua, ub = h3.universe_at(pa, kk, tt, SHARES, fa), h3.universe_at(pb, kk, tt, SHARES, fb)
        pd.testing.assert_frame_equal(ua, ub)
        pd.testing.assert_series_equal(h3.target_weights(ua, **prm), h3.target_weights(ub, **prm))
    assert len(h3.target_weights(ua, **prm)) == 4
    # 전제: 미래 충격이 실제로 뒤 월말 선택을 바꾼다(시험이 공허하지 않다)
    t2 = CAL[ME[k + 2]]
    wa = h3.target_weights(h3.universe_at(pa, k + 2, t2, SHARES, fa), **prm)
    wb = h3.target_weights(h3.universe_at(pb, k + 2, t2, SHARES, fb), **prm)
    assert not wa.equals(wb)


def test_filing_after_t_is_not_used():
    f = _fund([("A", 2019, "2020-03-30", 100.0, 10.0), ("A", 2020, "2021-03-30", 200.0, 20.0),
               ("A", 2018, "2021-06-01", 999.0, 99.0),           # 옛 사업연도의 늦은 정정 공시
               ("B", 2020, "2021-05-01", 50.0, 5.0)])
    at = lambda d: h3.fundamentals_asof(f, pd.Timestamp(d))     # noqa: E731
    assert at("2020-03-29").empty
    assert at("2021-03-29").loc["A", "equity"] == 100.0 and "B" not in at("2021-03-29").index
    assert at("2021-03-30").loc["A", "equity"] == 200.0          # 접수일 당일부터 사용
    assert at("2021-07-01").loc["A", "equity"] == 200.0          # 가장 최근 사업연도 우선
    assert at("2021-05-01").loc["B", "equity"] == 50.0
    # 유니버스 결합: 접수 전 종목은 재무 없음 → 점수·비중에서 빠진다
    sig = pd.DataFrame({"hist": 300.0, "last_close": [10.0, 20.0]}, index=["A", "B"])
    u = h3.scored_universe(sig, pd.Series(1e3, index=["A", "B"]), at("2021-04-30"))
    assert list(u["has_fin"]) == [False, True] and u.index[0] == "B"   # B 가 시총 1위
    assert list(h3.target_weights(u, top_n=None, score="combo", weight="cap").index) == ["A"]


def test_pick_statement_prefers_complete_cfs():
    f = pd.DataFrame({"fy": 2020, "corp_code": ["X", "X", "Y", "Y"], "fs_div": ["CFS", "OFS", "CFS", "OFS"],
                      "equity": [1.0, 2.0, np.nan, 4.0], "net_income": [1.0, 2.0, 1.0, 4.0]})
    p = h3.pick_statement(f).set_index("corp_code")
    assert p.loc["X", "fs_div"] == "CFS" and p.loc["Y", "fs_div"] == "OFS"


def _px_panel(prices):
    idx = pd.DatetimeIndex(pd.bdate_range("2021-01-29", periods=len(prices), freq="BME"))
    px = pd.DataFrame(prices, index=idx)
    return {"open_exec": px, "last_close": px}, pd.Series(1000.0, index=idx)


def test_cost_only_on_weight_change():
    panel, bench = _px_panel([{"A": 100.0, "B": 100.0, "C": 100.0}] * 4)
    w = [pd.Series({"A": 0.5, "B": 0.5}), pd.Series({"A": 0.6, "B": 0.4}), pd.Series({"A": 0.6, "C": 0.4})]
    r = {(x["k"], x["symbol"]): x for x in h3.positions(panel, w, bench, 0, 3)}
    B, S = h3.BUY_COST, h3.SELL_COST
    assert r[(0, "A")]["f_buy"] == 1 and r[(0, "A")]["f_sell"] == 0          # 신규 매수, 다음 달 비중 증가 → 매도 없음
    assert np.isclose(r[(0, "B")]["f_sell"], 0.2)                             # 0.5 → 0.4: 매도분 0.1/0.5
    assert np.isclose(r[(0, "B")]["net_return"], (1 - S * 0.2) / (1 + B) - 1)
    assert np.isclose(r[(1, "A")]["f_buy"], 0.1 / 0.6)                        # 0.5 → 0.6: 매수분 0.1/0.6
    assert np.isclose(r[(1, "A")]["net_return"], 1 / (1 + B * 0.1 / 0.6) - 1)
    assert r[(1, "B")]["f_buy"] == 0 and r[(1, "B")]["f_sell"] == 1          # 편출
    assert r[(2, "A")]["f_buy"] == 0 and r[(2, "A")]["f_sell"] == 1          # 비중 그대로 → 매수 0, 마지막 달 청산
    g = h3.account(list(r.values()))
    assert np.isclose(g["port"].iloc[1], 0.6 * r[(1, "A")]["net_return"] + 0.4 * r[(1, "B")]["net_return"])


def test_price_drift_of_cap_weights_is_free():
    """시총가중은 가격이 움직여도 드리프트 비중 = 새 목표 비중 → 비용 0 (거래 없음)."""
    panel, bench = _px_panel([{"A": 100.0, "B": 100.0}, {"A": 200.0, "B": 100.0}, {"A": 200.0, "B": 50.0}])
    w = [pd.Series({"A": 0.5, "B": 0.5}), pd.Series({"A": 2 / 3, "B": 1 / 3})]
    r = {(x["k"], x["symbol"]): x for x in h3.positions(panel, w, bench, 0, 2)}
    assert np.isclose(r[(0, "A")]["f_sell"], 0) and np.isclose(r[(0, "B")]["f_sell"], 0)
    assert np.isclose(r[(1, "A")]["f_buy"], 0) and np.isclose(r[(1, "B")]["f_buy"], 0)
    assert np.isclose(r[(0, "A")]["net_return"], 2 / (1 + h3.BUY_COST) - 1)


def test_halted_new_entry_is_skipped_and_renormalised():
    idx = pd.DatetimeIndex(pd.bdate_range("2021-01-29", periods=2, freq="BME"))
    panel = {"open_exec": pd.DataFrame({"A": [100.0, np.nan], "B": [np.nan, 100.0]}, index=idx)}
    w = h3.executable([pd.Series({"A": 0.7, "B": 0.3}), pd.Series({"A": 0.5, "B": 0.5})], panel)
    assert dict(w[0]) == {"A": 1.0}                    # B 시가 없음 → 그달 제외, 재정규화
    assert dict(w[1]) == {"A": 0.5, "B": 0.5}          # A 는 정지여도 계속 보유
