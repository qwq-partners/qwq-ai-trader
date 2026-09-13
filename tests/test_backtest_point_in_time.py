"""백테스터 정보 시점(point-in-time) 회귀 테스트 — 리뷰 후속 계획 T5 (F2·F8).

F2: T+1 시가 주문의 진입 ATR 이 진입일 전체 봉(당일 고저 포함)을 썼다 → 미래 고저만 바꿔도 수량·초기 손절이 바뀜.
F8: 시가 체결 사이징의 equity 가 당일 종가 평가였다 → 보유 종목의 미래 종가만 바꿔도 신규 주문 수량이 바뀜.

모든 시계열은 합성 DataFrame — 네트워크(pykrx/FDR)·운영 상태 파일 무접촉.
실행: venv/bin/python -m pytest tests/test_backtest_point_in_time.py -q -p no:cacheprovider
"""

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_bt():
    spec = importlib.util.spec_from_file_location(
        "_bt_strategies_for_pit_test", ROOT / "scripts" / "backtest_strategies.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


bt = _load_bt()

# bdate_range("2026-01-05", 40): idx[29]=2026-02-13(금) → idx[30]=2026-02-16(월) — 주말 갭 포함
FILL_DAY = "2026-02-16"
SIGNAL_DAY = "2026-02-13"
# 체결일 봉만 다른 두 변형: 같은 과거·같은 시가(10,000), 당일 고저·종가만 다름
FUTURE_BAR_WILD = {FILL_DAY: {"고가": 10900.0, "저가": 9500.0, "종가": 9700.0}}
FUTURE_BAR_NO_STOP = {FILL_DAY: {"고가": 10500.0, "저가": 9700.0, "종가": 10400.0}}   # 손절·익절·트레일링 미접촉


def _frame(n=40, close=10000.0, rng=0.011, overrides=None) -> pd.DataFrame:
    """평평한 종가·일정 범위(±1.1% → ATR≈2.2%)의 합성 OHLCV. overrides={날짜: {컬럼: 값}}."""
    idx = pd.bdate_range("2026-01-05", periods=n)
    df = pd.DataFrame({"시가": close, "고가": close * (1 + rng), "저가": close * (1 - rng),
                       "종가": close, "거래량": 1_000_000.0}, index=idx)
    for date, vals in (overrides or {}).items():
        for col, v in vals.items():
            df.loc[pd.Timestamp(date), col] = v
    return df


def _engine(frames, **cfg_kw):
    cfg = bt.BacktestConfig(strategies=["sepa"], **cfg_kw)
    eng = bt.BacktestEngine(cfg)
    eng.universe.tickers = list(frames)
    eng.universe.names = {t: t for t in frames}
    eng.universe.ohlcv = {t: bt.BTIndicators.compute(df.copy()) for t, df in frames.items()}
    eng.regime.kospi_data = None   # 레짐 NEUTRAL 고정
    return eng


def _order(symbol, signal_day=SIGNAL_DAY, **extra):
    o = {"symbol": symbol, "strategy": bt.StrategyType.SEPA, "score": 80.0,
         "signal_close": 10000.0, "signal_date": signal_day, "indicator_asof": signal_day}
    o.update(extra)
    return o


def _held(symbol, qty, entry=10000.0):
    return bt.BTPosition(symbol=symbol, name=symbol, strategy=bt.StrategyType.SEPA,
                         entry_date="2026-02-02", entry_price=entry, quantity=qty,
                         cost_basis=entry * qty, highest_price=entry)


# ── F2: 진입 ATR 은 체결일 이전 확정 봉만 ─────────────────────────────────────
@pytest.mark.parametrize("sizing", ["nominal", "risk"])
def test_f2_future_bar_of_fill_day_does_not_change_open_fill(sizing):
    """같은 과거·같은 시가 → 체결일 고저·종가만 달라도 시가 수량·초기 손절이 같아야 한다."""
    qty, stop = {}, {}
    for tag, ov in {"A": None, "B": FUTURE_BAR_WILD}.items():
        eng = _engine({"X": _frame(overrides=ov)}, sizing=sizing)
        eng.pending_buys = [_order("X")]
        eng._execute_pending_buys(FILL_DAY)
        p = eng.positions["X"]
        qty[tag], stop[tag] = p.quantity, p.atr_stop_pct
    assert stop["A"] == stop["B"], f"초기 손절이 미래 봉에 의존: {stop}"
    assert qty["A"] == qty["B"], f"시가 수량이 미래 봉에 의존: {qty}"


def test_entry_atr_uses_only_bars_before_fill_day():
    eng_a = _engine({"X": _frame()})
    eng_b = _engine({"X": _frame(overrides=FUTURE_BAR_WILD)})
    prev_atr = float(eng_a.universe.ohlcv["X"].loc[pd.Timestamp(SIGNAL_DAY), "atr_pct"])
    assert eng_a._entry_atr("X", FILL_DAY) == pytest.approx(prev_atr)
    assert eng_b._entry_atr("X", FILL_DAY) == pytest.approx(prev_atr)
    # T+0 종가 체결은 당일 봉이 확정된 뒤 → 포함 허용
    today_atr = float(eng_b.universe.ohlcv["X"].loc[pd.Timestamp(FILL_DAY), "atr_pct"])
    assert eng_b._entry_atr("X", FILL_DAY, include_day=True) == pytest.approx(today_atr)
    assert today_atr != pytest.approx(prev_atr)
    # 이전 봉이 없거나 ATR 미산출(워밍업) → None
    assert eng_a._entry_atr("X", "2026-01-05") is None
    assert eng_a._entry_atr("X", "2026-01-08") is None


# ── F8: 시가 체결 사이징의 equity 는 시가 평가 ─────────────────────────────────
def test_f8_future_close_of_held_symbol_does_not_change_open_fill_quantity():
    """보유 500주·현금 500만·모든 시가 1만원: 보유 종목 당일 종가 1만→1.2만이어도 신규 수량 250주."""
    qty = {}
    for close_h in (10000.0, 12000.0):
        h = _frame(overrides={FILL_DAY: {"고가": close_h * 1.01, "종가": close_h}})
        eng = _engine({"H": h, "N": _frame()})
        eng.cash = 5_000_000.0
        eng.positions["H"] = _held("H", 500)
        eng.pending_buys = [_order("N")]
        eng._execute_pending_buys(FILL_DAY)
        qty[close_h] = eng.positions["N"].quantity
    assert qty[10000.0] == qty[12000.0] == 250, f"신규 수량이 보유 종목 미래 종가에 의존: {qty}"


def test_calc_equity_phase_open_vs_close():
    h = _frame(overrides={FILL_DAY: {"고가": 12100.0, "종가": 12000.0}})
    eng = _engine({"H": h})
    eng.cash = 1_000_000.0
    eng.positions["H"] = _held("H", 100)
    assert eng._calc_equity(FILL_DAY, phase="open") == pytest.approx(1_000_000 + 100 * 10000)
    assert eng._calc_equity(FILL_DAY, phase="close") == pytest.approx(1_000_000 + 100 * 12000)
    assert eng._calc_equity(FILL_DAY) == eng._calc_equity(FILL_DAY, phase="close")   # 기본은 EOD
    with pytest.raises(ValueError):
        eng._calc_equity(FILL_DAY, phase="noon")


def test_open_equity_uses_last_confirmed_close_when_symbol_has_no_bar():
    """체결일 봉이 없는 보유 종목(거래정지)은 마지막 확정 종가로 평가."""
    h = _frame(overrides={SIGNAL_DAY: {"종가": 9000.0, "저가": 8900.0}}).drop(pd.Timestamp(FILL_DAY))
    eng = _engine({"H": h})
    eng.cash = 1_000_000.0
    eng.positions["H"] = _held("H", 100)
    assert eng._calc_equity(FILL_DAY, phase="open") == pytest.approx(1_000_000 + 100 * 9000)


def test_no_bar_on_fill_day_makes_no_fill():
    """신규 주문 종목의 체결일 시가가 없으면(거래정지·누락봉) 체결을 만들지 않는다 — 이전 봉 시가로 대체 금지."""
    eng = _engine({"X": _frame().drop(pd.Timestamp(FILL_DAY))})
    eng.pending_buys = [_order("X")]
    eng._execute_pending_buys(FILL_DAY)
    assert "X" not in eng.positions and eng.trades == []


# ── 신호·지표 시점 기록과 검사 ─────────────────────────────────────────────────
def test_pending_orders_carry_signal_date_and_indicator_asof_before_fill():
    frames = {"X": _frame(), "Y": _frame().drop(pd.Timestamp(SIGNAL_DAY))}   # Y: 신호일 거래정지
    eng = _engine(frames)
    eng.scorer.score_sepa = lambda data: 90.0
    eng._generate_signals(SIGNAL_DAY)
    by = {o["symbol"]: o for o in eng.pending_buys}
    assert by["X"]["signal_date"] == SIGNAL_DAY and by["X"]["indicator_asof"] == SIGNAL_DAY
    assert by["Y"]["signal_date"] == SIGNAL_DAY and by["Y"]["indicator_asof"] == "2026-02-12"
    eng._execute_pending_buys(FILL_DAY)   # 주말 갭 뒤 체결 — 둘 다 체결일보다 앞선다
    assert {p.entry_date for p in eng.positions.values()} == {FILL_DAY}
    assert set(eng.positions) == {"X", "Y"}


def test_indicator_asof_not_before_fill_day_is_rejected():
    eng = _engine({"X": _frame()})
    eng.pending_buys = [_order("X", indicator_asof=FILL_DAY)]
    with pytest.raises(ValueError):
        eng._execute_pending_buys(FILL_DAY)
    eng.pending_buys = [_order("X", signal_day=FILL_DAY)]
    with pytest.raises(ValueError):
        eng._execute_pending_buys(FILL_DAY)
    # T+0 종가 체결은 당일 신호 허용
    eng.pending_buys = [_order("X", signal_day=FILL_DAY)]
    eng._execute_pending_buys(FILL_DAY, use_close=True)
    assert eng.positions["X"].entry_price == 10000.0


# ── 초기 손절 정책 · R 분모 고정 ────────────────────────────────────────────────
def test_entry_stop_mode_default_cli_and_metrics_keys_unchanged():
    """게이트 호환: 기본값·CLI·metrics 키 불변 (live_policy 는 A/B 러너·T6 가 명시)."""
    assert bt.BacktestConfig().entry_stop_mode == "atr_dynamic"
    ns = bt.build_parser().parse_args([])
    assert (ns.entry_stop_mode, ns.sizing, ns.exit_policy) == ("atr_dynamic", "nominal", "ladder")
    assert bt.build_parser().parse_args(["--entry-stop-mode", "live_policy"]).entry_stop_mode == "live_policy"
    with pytest.raises(ValueError):
        bt.BacktestEngine(bt.BacktestConfig(entry_stop_mode="fixed"))
    an = bt.ResultAnalyzer(bt.BacktestConfig(), [],
                           [("2026-01-02", 10_000_000.0), ("2026-01-05", 10_000_000.0)])
    assert set(an.metrics()) == {
        "initial", "final", "total_return_pct", "cagr_pct", "mdd_pct", "mdd_start", "mdd_end",
        "sharpe", "win_rate", "profit_factor", "total_trades", "wins", "losses", "total_fees",
        "start_date", "end_date", "avg_holding_days", "position_level"}


@pytest.mark.parametrize("sizing", ["nominal", "risk"])
def test_live_policy_uses_fixed_strategy_stop_and_freezes_initial_risk(sizing):
    eng = _engine({"X": _frame(overrides=FUTURE_BAR_NO_STOP)}, sizing=sizing,
                  entry_stop_mode="live_policy", sepa_stop_loss_pct=5.0)
    eng.pending_buys = [_order("X")]
    eng._execute_pending_buys(FILL_DAY)
    p = eng.positions["X"]
    assert p.atr_stop_pct == 5.0                       # ATR 무관, 전략 고정 SL
    assert p.atr_pct > 0                               # ATR 은 트레일링 연동용으로만 보관
    assert p.initial_risk == pytest.approx(p.cost_basis * 0.05)
    if sizing == "risk":
        assert p.quantity == 140                       # equity×0.7% / 5% = 14% → 140주
    buy = eng.trades[0]
    assert buy.stop_pct == 5.0 and buy.initial_risk == pytest.approx(p.initial_risk)
    # 이후 봉의 ATR 이 커져도 손절·초기 위험은 소급 변경되지 않는다
    eng._check_exits(FILL_DAY)
    assert p.remaining_quantity == p.quantity and p.atr_stop_pct == 5.0
    assert p.initial_risk == pytest.approx(p.cost_basis * 0.05)


def test_atr_dynamic_keeps_entry_stop_and_initial_risk_frozen():
    eng = _engine({"X": _frame(overrides=FUTURE_BAR_NO_STOP)}, sizing="risk")
    eng.pending_buys = [_order("X")]
    eng._execute_pending_buys(FILL_DAY)
    p = eng.positions["X"]
    s0, r0 = p.atr_stop_pct, p.initial_risk
    assert s0 == pytest.approx(4.4, abs=0.05) and r0 == pytest.approx(p.cost_basis * s0 / 100)
    eng._check_exits(FILL_DAY)            # 당일 ATR(2.8%)로 손절 재계산되더라도 진입 값은 불변
    assert p.remaining_quantity == p.quantity
    assert p.atr_stop_pct == s0 and p.initial_risk == r0
    assert eng.trades[0].stop_pct == s0 and eng.trades[0].initial_risk == pytest.approx(r0)
