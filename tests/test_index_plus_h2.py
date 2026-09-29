"""H2 대형주 모멘텀 — 미래정보 없음·교체분 비용 (합성 입력, 네트워크 없음)."""
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

_spec = importlib.util.spec_from_file_location(
    "index_plus_h2", Path(__file__).resolve().parents[1] / "scripts/research/index_plus_h2.py")
h2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h2)

CAL = pd.bdate_range("2020-01-01", periods=420)
ME = h2.month_ends(CAL)


def _daily(seed, drift):
    rng = np.random.default_rng(seed)
    c = 100 * np.cumprod(1 + drift + rng.normal(0, 0.01, len(CAL)))
    return pd.DataFrame({"open": c * 1.001, "close": c, "volume": 1e5 * (1 + seed)}, index=CAL)


def _panel(dailies):
    cols = {c: {} for c in h2.SIGNAL_COLS + ["last_close", "open_exec"]}
    for code, d in dailies.items():
        f = h2.features(d, CAL, ME)
        for c in cols:
            cols[c][code] = f[c]
    return {c: pd.DataFrame(v) for c, v in cols.items()}


def test_selection_at_t_ignores_future_prices_and_volumes():
    base = {f"{i:05d}0": _daily(i, 0.0005 * (i - 6)) for i in range(12)}
    k = len(ME) - 3
    i = ME[k]
    assert i >= 252
    shocked = {}
    for j, (code, d) in enumerate(base.items()):
        s = d.copy()
        s.iloc[i + 1:, :2] *= (0.2 if j % 2 else 5.0)       # t 다음 날부터 가격 폭등/폭락
        s.iloc[i + 1:, 2] *= (100 if j % 2 else 0.01)       # 거래량도 뒤집는다
        if j % 3 == 0:
            s.iloc[i + 1, 2] = 0.0                          # t 다음 날 거래정지
        shocked[code] = s
    pa, pb = _panel(base), _panel(shocked)
    for kk in range(k + 1):                                 # t 이하 모든 월말의 신호 열이 같다
        for c in h2.SIGNAL_COLS:
            pd.testing.assert_series_equal(pa[c].iloc[kk], pb[c].iloc[kk])
    prm = dict(top_n=3, universe_n=8, mom="mom12")
    ha, hb = h2.holdings(pa, **prm), h2.holdings(pb, **prm)
    assert ha[: k + 1] == hb[: k + 1] and len(ha[k]) == 3
    # 전제: 미래 충격이 실제로 다음 달 선택을 바꾼다(시험이 공허하지 않다)
    assert ha[k + 1] != hb[k + 1]


def test_external_turnover_is_used_and_future_turnover_ignored():
    d = _daily(3, 0.001)
    tv = pd.Series(np.arange(len(CAL), dtype=float) + 1, index=CAL)
    f = h2.features(d, CAL, ME, tv)
    i = ME[-2]
    assert f["turn60"].iloc[-2] == tv.iloc[i - 59:i + 1].mean()
    tv2 = tv.copy()
    tv2.iloc[i + 1:] = 1e12
    assert h2.features(d, CAL, ME, tv2)["turn60"].iloc[-2] == f["turn60"].iloc[-2]


def test_universe_is_liquidity_then_momentum():
    sig = pd.DataFrame({"hist": 300.0, "has252": True, "traded_t": True, "mom6": 0.0,
                        "turn60": [5, 4, 3, 2, 1.0], "mom12": [0.1, 0.2, 0.3, 0.9, 0.8]},
                       index=["A", "B", "C", "D", "E"])
    assert h2.select(sig, top_n=2, universe_n=3, mom="mom12") == ["C", "B"]   # D·E 는 유동성 밖


def test_cost_only_on_replaced_names():
    months = pd.DatetimeIndex(pd.bdate_range("2021-01-29", periods=4, freq="BME"))
    px = pd.DataFrame(100.0, index=months, columns=["A", "B", "C"])
    panel = {"open_exec": px, "last_close": px}
    held = [{"A", "B"}, {"A", "C"}, {"A", "C"}]
    bench = pd.Series(1000.0, index=months)
    rows = {(r["k"], r["symbol"]): r for r in h2.positions(panel, held, bench, 0, 3)}
    assert rows[(0, "A")]["bought"] and not rows[(0, "A")]["sold"]
    assert np.isclose(rows[(0, "A")]["net_return"], 1 / (1 + h2.BUY_COST) - 1)
    assert rows[(0, "B")]["sold"]
    assert np.isclose(rows[(0, "B")]["net_return"], (1 - h2.SELL_COST) / (1 + h2.BUY_COST) - 1)
    assert rows[(1, "A")]["net_return"] == 0.0                     # 계속 보유 — 비용 0
    assert rows[(1, "C")]["bought"] and not rows[(1, "C")]["sold"]
    assert rows[(2, "C")]["sold"] and rows[(2, "A")]["sold"]       # 마지막 달은 청산 매도
    assert all(r["bench_return"] == 0.0 for r in rows.values())


def test_halted_entry_is_skipped_but_continuing_hold_is_kept():
    months = pd.DatetimeIndex(pd.bdate_range("2021-01-29", periods=3, freq="BME"))
    oe = pd.DataFrame({"A": [100.0, np.nan, 110.0], "B": [np.nan, 100.0, 100.0]}, index=months)
    lc = pd.DataFrame({"A": [99.0, 105.0, 108.0], "B": [99.0, 99.0, 99.0]}, index=months)
    sig = {c: pd.DataFrame({"A": 300.0, "B": 300.0}, index=months) for c in ("hist", "turn60", "mom12", "mom6")}
    sig.update({c: pd.DataFrame({"A": True, "B": True}, index=months) for c in ("has252", "traded_t")})
    panel = {**sig, "open_exec": oe, "last_close": lc}
    held = h2.holdings(panel, top_n=2, universe_n=2, mom="mom12")
    assert held[0] == {"A"}                  # B 는 시가 없음(정지) → 그달 제외
    assert held[1] == {"A", "B"}             # A 는 정지여도 계속 보유
    r = {(x["k"], x["symbol"]): x for x in h2.positions(panel, held, pd.Series(1.0, index=months), 0, 2)}
    assert r[(0, "A")]["exit_px"] == 105.0 and r[(0, "A")]["px_fallback"]   # 정지 → t 까지 마지막 종가


def test_exclusion_rules():
    assert h2.exclusion_reason("005935", "삼성전자우")
    assert h2.exclusion_reason("395400", "SK리츠") == "리츠"
    assert h2.exclusion_reason("138040", "메리츠금융지주") is None
    assert h2.exclusion_reason("0126Z0", "삼성에피스홀딩스") is None
    assert h2.exclusion_reason("123450", "하나스팩1호") == "스팩"
