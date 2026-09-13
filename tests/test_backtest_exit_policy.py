"""백테스터 exit_policy=channel 청산 함수 단위 테스트 (2026-09 청산 정책 A/B).

`scripts/backtest_strategies.channel_exit` 가 `src/strategies/harvest_shadow.exit_step` 와
같은 의미(갭관통 시가 / 저가 관통 스탑가 / 채널 종가 이탈 / +30% runner / 스탑 승급)인지
합성 봉으로 확인한다. 네트워크·프로덕션 상태 파일 무접촉.

실행: venv/bin/python -m pytest tests/test_backtest_exit_policy.py -q
"""

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent


def _load_bt():
    spec = importlib.util.spec_from_file_location(
        "_bt_strategies_for_test", ROOT / "scripts" / "backtest_strategies.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


bt = _load_bt()


def _pos(entry=10000.0, stop_pct=5.0, strategy=None):
    p = bt.BTPosition(symbol="X", name="X", strategy=strategy or bt.StrategyType.SEPA,
                      entry_date="2026-01-02", entry_price=entry, quantity=100,
                      cost_basis=entry * 100, highest_price=entry, atr_stop_pct=stop_pct)
    p.stop_price = entry * (1 - stop_pct / 100)
    return p


def _bar(o, h, lo, c, low10=np.nan, low20=np.nan, **extra):
    d = {"시가": o, "고가": h, "저가": lo, "종가": c, "low10": low10, "low20": low20,
         "atr_pct": 2.5, "ma5": c, "prev_low": lo}
    d.update(extra)
    return pd.Series(d)


def test_gap_through_exits_at_open():
    p = _pos()
    px, why = bt.channel_exit(p, _bar(9400, 9600, 9300, 9550, low10=9700))
    assert px == 9400 and "갭관통" in why


def test_intraday_stop_exits_at_stop_price():
    p = _pos()
    px, why = bt.channel_exit(p, _bar(9800, 9900, 9450, 9700, low10=9700))
    assert px == 9500 and why == "손절"


def test_channel_break_exits_at_close_and_stop_ratchets_otherwise():
    p = _pos()
    # 채널(9800) 위 종가 → 청산 없음, 스탑이 채널×0.999로 승급
    assert bt.channel_exit(p, _bar(10000, 10300, 9900, 10200, low10=9800)) is None
    assert abs(p.stop_price - 9800 * 0.999) < 1e-6
    # 종가가 채널(10000) 아래 → 종가 청산
    px, why = bt.channel_exit(p, _bar(10100, 10150, 9950, 9990, low10=10000))
    assert px == 9990 and why.startswith("채널이탈 (10일")


def test_runner_switches_to_20d_channel_after_30pct():
    p = _pos()
    # +30% 도달 → runner. 10일 저가(12500)는 무시하고 20일 저가(11000) 기준
    assert bt.channel_exit(p, _bar(12800, 13100, 12600, 12900, low10=12500, low20=11000)) is None
    assert p.runner is True and abs(p.stop_price - 11000 * 0.999) < 1e-6
    px, why = bt.channel_exit(p, _bar(12000, 12100, 11500, 11800, low10=12500, low20=11900))
    assert px == 11800 and "20일" in why


def test_nan_channel_keeps_stop():
    p = _pos()
    assert bt.channel_exit(p, _bar(10000, 10200, 9900, 10100)) is None
    assert p.stop_price == 9500


def test_channel_policy_has_no_partial_take_profit_but_ladder_does():
    bar = _bar(11000, 11300, 10900, 11200, low10=9800)  # 고가 +13%, 종가 +12%
    ladder = bt.BTExitManager(bt.BacktestConfig(exit_policy="ladder", first_exit_pct=10.0))
    acts = ladder.check_exit(_pos(), bar, regime=None)
    assert acts and "1차 익절" in acts[0][3]

    channel = bt.BTExitManager(bt.BacktestConfig(exit_policy="channel"))
    p = _pos()
    assert channel.check_exit(p, bar, regime=None) == []
    assert p.remaining_quantity == 100 and p.exit_stage == bt.ExitStage.NONE


def test_min_holding_days_blocks_non_stop_exits():
    cfg = bt.BacktestConfig(exit_policy="channel", stale_high_days=1,
                            stale_high_min_pnl_pct=3.0, min_holding_days=2)
    mgr = bt.BTExitManager(cfg)
    p = _pos()
    p.highest_price = 10500  # 신고가 실패 카운트가 바로 쌓이도록
    flat = _bar(10000, 10050, 9950, 10000, low10=9800)
    assert mgr.check_exit(p, flat, regime=None) == []          # day1: 손절 외 금지
    acts = mgr.check_exit(p, flat, regime=None)                 # day2: 추세 무효화 허용
    assert acts and "추세 무효화" in acts[0][3]


def test_position_level_roundtrip_aggregation():
    cfg = bt.BacktestConfig()
    T = bt.Trade
    trades = [
        T("X", "X", "sepa", "BUY", "2026-01-02", 10000, 100, 1_000_000, 140.5, stop_pct=5.0),
        T("X", "X", "sepa", "SELL", "2026-01-05", 11000, 10, 110_000, 234.4, holding_days=2),
        T("X", "X", "sepa", "SELL", "2026-01-08", 10500, 90, 945_000, 2013.3, holding_days=5),
    ]
    an = bt.ResultAnalyzer(cfg, trades, [("2026-01-02", 10_000_000), ("2026-01-08", 10_050_000)])
    ps = an.positions()
    assert len(ps) == 1 and ps[0]["holding_days"] == 5
    expected = (110_000 - 234.4 + 945_000 - 2013.3) - (1_000_000 + 140.5)
    assert abs(ps[0]["pnl"] - expected) < 1e-6
    assert abs(ps[0]["r"] - ps[0]["pnl_pct"] / 5.0) < 1e-9
    m = an.position_metrics(ps)
    assert m["trades"] == 1 and m["win_rate"] == 100.0 and m["max_consec_losses"] == 0
