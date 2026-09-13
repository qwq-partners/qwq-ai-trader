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


# ── 체결 순서 모호성 (일봉 근사) — 보수 규칙, 전 셀 동일 적용 (2026-09 T5) ──────────
# 규칙: ① 손절 우선(익절과 같은 봉 접촉 시) ② 갭 관통(시가 ≤ 손절가)은 시가 체결
#       ③ 익절→트레일링→복합→익절후 저효율→보유 규칙 순, 모두 종가 체결, 익절은 봉당 한 단계
def _mgr(**kw):
    return bt.BTExitManager(bt.BacktestConfig(**kw))


def test_ladder_same_bar_take_profit_and_stop_prefers_stop():
    p = _pos()   # atr_pct 2.5 → 손절 5% = 9,500 / 1차 익절 5%(config 기본)
    acts = _mgr().check_exit(p, _bar(10000, 11200, 9400, 10800), regime=None)
    assert len(acts) == 1 and acts[0][3].startswith("손절")
    assert acts[0][1] == 100 and acts[0][2] == 9500 and p.remaining_quantity == 0


def test_ladder_gap_through_stop_fills_at_open():
    p = _pos()
    acts = _mgr().check_exit(p, _bar(9200, 9400, 9000, 9300), regime=None)
    assert acts[0][3].startswith("손절") and acts[0][2] == 9200   # 시장에 없는 9,500 체결 금지


def test_ladder_partial_then_trailing_same_bar_order():
    p = _pos()
    acts = _mgr(first_exit_pct=10.0).check_exit(p, _bar(10500, 11500, 10400, 10900), regime=None)
    assert [a[3].split(" ")[0] for a in acts] == ["1차", "트레일링"]
    assert (acts[0][1], acts[0][2]) == (30, 10900) and (acts[1][1], acts[1][2]) == (70, 10900)
    assert p.remaining_quantity == 0


def test_ladder_composite_precedes_holding_rules():
    p = _pos()
    p.exit_stage = bt.ExitStage.FIRST
    mgr = _mgr(enable_composite_exit=True, sepa_max_holding_days=1)
    acts = mgr.check_exit(p, _bar(10000, 10100, 9900, 9950, ma5=10100), regime=None)
    assert len(acts) == 1 and acts[0][3].startswith("복합청산")


def test_check_exit_does_not_rewrite_entry_stop():
    p = _pos(stop_pct=4.0)
    _mgr().check_exit(p, _bar(10000, 10100, 9900, 10050, atr_pct=3.0), regime=None)  # 당일 ATR 3%→6%
    assert p.atr_stop_pct == 4.0


def test_live_policy_stop_changes_only_on_regime_transition():
    """live_policy: 진입 고정 SL 5% 유지, 레짐이 바뀔 때만 레짐 SL 로 상태 전이 (실엔진 apply_regime_params 미러)."""
    mgr = _mgr(entry_stop_mode="live_policy")
    p = _pos(stop_pct=5.0)
    p.regime_seen = bt.RegimeType.NEUTRAL
    bar = _bar(9700, 9750, 9620, 9700, atr_pct=1.0)     # 저가 -3.8%: 5% 미접촉 / bear 3.5% 접촉
    assert mgr.check_exit(p, bar, regime=bt.RegimeType.NEUTRAL) == []     # 같은 레짐 → 전이 없음
    assert p.atr_stop_pct == 5.0 and p.live_stop_pct == 0.0
    acts = mgr.check_exit(p, bar, regime=bt.RegimeType.BEARISH)           # 전환 → SL 3.5%
    assert acts and acts[0][3] == "손절 -3.5%" and acts[0][2] == 9650
    assert p.atr_stop_pct == 5.0 and p.live_stop_pct == 3.5              # 진입 손절(R 분모)은 불변
