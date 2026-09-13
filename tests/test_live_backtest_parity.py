"""실엔진 ↔ 백테스터 parity (2026-09-14 리뷰 후속 T6).

같은 합성 주문·가격 시퀀스를 **실엔진**(engine.RiskManager._calculate_position_size +
stop_policy/ExitManager.resolve_stop + ExitManager.update_price)과 **백테스터**
(BacktestEngine, entry_stop_mode=live_policy, sizing=risk)에 넣고
초기 손절 · 수량 · 손절 시점 · 익절 수량/단계 · 비용 · R · 슬롯을 비교한다.

남아 있는 알려진 차이는 xfail(strict) 로 고정한다 — 조용히 통과시키지 않는다.
그 범위(손절 판정 기준, 장중 고가 접촉)는 승격 보류 사유로 문서에 기록한다
(docs/research/exit-policy-ab-2026-09.md §parity).

합성 값만 사용 — 네트워크·KIS·운영 상태 파일 무접촉.
실행: venv/bin/python -m pytest tests/test_live_backtest_parity.py -q -p no:cacheprovider
"""

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 실엔진 사이징·청산 픽스처는 T2 회귀 테스트의 것을 그대로 재사용한다 (기준이 갈라지지 않게)
from tests.test_risk_sizing import (  # noqa: E402
    EQ, PRICE, SYM, _em, _register_like_scheduler, _rm, _sig,
)
from src.risk.manager import RiskManager as LiveRiskManager  # noqa: E402
from src.utils.fee_calculator import get_fee_calculator  # noqa: E402


def _load_bt():
    spec = importlib.util.spec_from_file_location(
        "_bt_strategies_for_parity_test", ROOT / "scripts" / "backtest_strategies.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


bt = _load_bt()

SEPA_SL = 5.0                  # 실엔진 sepa 고정 SL (run_trader._strategy_exit_params)
FILL_DAY = "2026-02-16"
SIGNAL_DAY = "2026-02-13"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """캘린더·변동성·팀 오버레이 캐시가 운영 경로를 보지 않게 한다."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


# ── 백테스터 측 합성 실행 ─────────────────────────────────────────────────────

def _frame(n=40, close=10000.0, rng=0.011, overrides=None) -> pd.DataFrame:
    idx = pd.bdate_range("2026-01-05", periods=n)
    df = pd.DataFrame({"시가": close, "고가": close * (1 + rng), "저가": close * (1 - rng),
                       "종가": close, "거래량": 1_000_000.0}, index=idx)
    for date, vals in (overrides or {}).items():
        for col, v in vals.items():
            df.loc[pd.Timestamp(date), col] = v
    return df


def _bt_engine(frames, **cfg_kw):
    kw = dict(strategies=["sepa"], sizing="risk", entry_stop_mode="live_policy",
              sepa_stop_loss_pct=SEPA_SL, risk_per_trade_pct=0.7,
              risk_max_position_pct=18.0, first_exit_pct=10.0, first_exit_ratio=0.1,
              min_position_value=200_000, min_cash_reserve_pct=5.0)
    kw.update(cfg_kw)
    cfg = bt.BacktestConfig(**kw)
    eng = bt.BacktestEngine(cfg)
    eng.universe.tickers = list(frames)
    eng.universe.names = {t: t for t in frames}
    eng.universe.ohlcv = {t: bt.BTIndicators.compute(df.copy()) for t, df in frames.items()}
    eng.regime.kospi_data = None           # 레짐 NEUTRAL 고정 (실엔진 픽스처도 레짐 미적용)
    eng.cash = float(EQ)
    return eng


def _bt_fill(frames=None, **cfg_kw):
    """단일 매수 체결 → BTPosition"""
    eng = _bt_engine(frames or {"X": _frame()}, **cfg_kw)
    eng.pending_buys = [{"symbol": "X", "strategy": bt.StrategyType.SEPA, "score": 80.0,
                         "signal_close": 10000.0, "signal_date": SIGNAL_DAY,
                         "indicator_asof": SIGNAL_DAY}]
    eng._execute_pending_buys(FILL_DAY)
    return eng, eng.positions["X"]


# ── 1. 수량·초기 손절·진입 비용·R 분모 ────────────────────────────────────────

def test_parity_quantity_initial_stop_cost_and_risk(home, monkeypatch):
    rm = _rm(monkeypatch, mode="risk", em=_em())
    sig = _sig(2.2)
    live_qty = rm._calculate_position_size(sig)
    live_stop = sig.signal.metadata["risk_stop_pct"]

    _, pos = _bt_fill()
    assert pos.quantity == live_qty == 139
    assert pos.atr_stop_pct == live_stop == SEPA_SL

    # 진입 비용(매수수수료 포함)과 R 분모가 같은 정의인지
    fee = get_fee_calculator("KR")
    live_cost = PRICE * live_qty + fee.calculate_buy_fee(PRICE * live_qty)
    assert pos.cost_basis == pytest.approx(float(live_cost), rel=1e-6)
    live_risk = live_cost * Decimal(str(SEPA_SL)) / 100
    assert pos.initial_risk == pytest.approx(float(live_risk), rel=1e-6)
    assert pos.initial_risk <= float(EQ) * 0.007 + 1     # 위험 예산 이내 (원 단위 반올림 여유)


def test_parity_quantity_across_stop_widths(home, monkeypatch):
    """손절폭이 달라도 두 경로의 수량이 같아야 한다 (cap 이 걸리는 3% 포함)."""
    for stop, expected in ((5.0, 139), (4.0, 174), (3.0, 180)):     # 3%: 상한 18% 가 결정
        rm = _rm(monkeypatch, mode="risk", em=_em())
        rm._resolve_entry_stop = lambda _s, _p=stop: SimpleNamespace(
            stop_pct=Decimal(str(_p)), source="strategy", crash_capped=False)
        live = rm._calculate_position_size(_sig(2.2))
        _, pos = _bt_fill(sepa_stop_loss_pct=stop)
        assert (live, pos.quantity) == (expected, expected), f"stop={stop}"


# ── 2. 손절 시점 ─────────────────────────────────────────────────────────────

def _live_stop_trigger_price(stop_pct=SEPA_SL):
    """실엔진이 손절로 판정하기 시작하는 최고가(1원 단위 탐색) — 수수료 포함 순손익률 기준."""
    em = _em()
    _register_like_scheduler(em, "sepa_trend", 139, 2.2)
    price = int(PRICE * (1 - Decimal(str(stop_pct)) / 100)) + 80
    while price > 0:
        if em.update_price(SYM, Decimal(price)) is not None:
            return price
        price -= 1
    raise AssertionError("손절 미발동")


def test_parity_stop_trigger_band_is_bounded_and_documented(home):
    """실엔진(net 손익률)과 백테스터(가격 하락률)의 손절 발동가 차이는 왕복 수수료폭 이내."""
    live_px = _live_stop_trigger_price()
    bt_px = float(PRICE) * (1 - SEPA_SL / 100)
    gap_pct = (live_px - bt_px) / float(PRICE) * 100
    # 실엔진이 먼저(더 높은 가격에서) 발동한다 — 왕복 수수료 0.227% 이내
    assert 0 < gap_pct <= 0.3, f"손절 발동가 차이 {gap_pct:.3f}%p (live {live_px} vs bt {bt_px})"


@pytest.mark.xfail(strict=True, reason=(
    "알려진 미지원 차이: 백테스터 손절은 가격 하락률(entry×(1-SL%)), 실엔진은 수수료 포함 "
    "순손익률. 약 0.22%p 먼저 발동한다. 청산 의미 변경은 이번 범위 밖(T5/T6 금지) — "
    "해당 범위 승격 보류, docs/research/exit-policy-ab-2026-09.md §parity 참조"))
def test_parity_stop_trigger_price_exact():
    assert _live_stop_trigger_price() == int(float(PRICE) * (1 - SEPA_SL / 100))


def test_parity_stop_fill_price_and_position_closed(home):
    """손절 봉에서 양쪽 모두 전량 청산 — 백테스터는 손절가(갭이면 시가) 체결."""
    stop_bar = {FILL_DAY: {"고가": 10050.0, "저가": 9400.0, "종가": 9450.0}}
    eng, pos = _bt_fill({"X": _frame(overrides=stop_bar)})
    eng._check_exits(FILL_DAY)
    assert pos.remaining_quantity == 0
    sell = eng.trades[-1]
    assert sell.side == "SELL" and sell.reason.startswith("손절")
    assert sell.price == pytest.approx(10000.0 * (1 - SEPA_SL / 100))

    em = _em()
    _register_like_scheduler(em, "sepa_trend", 139, 2.2)
    sig = em.update_price(SYM, Decimal("9400"))
    assert sig is not None and sig[0] == "sell_all"


# ── 3. 익절 단계·수량 ────────────────────────────────────────────────────────

def test_parity_first_take_profit_stage_and_quantity(home):
    """+10% 도달 시 양쪽 모두 잔여의 10%(=13주) 1차 익절, 단계 FIRST."""
    tp_bar = {FILL_DAY: {"고가": 11400.0, "저가": 9900.0, "종가": 11300.0}}
    eng, pos = _bt_fill({"X": _frame(overrides=tp_bar)})
    eng._check_exits(FILL_DAY)
    tp = [t for t in eng.trades if t.side == "SELL"]
    assert len(tp) == 1 and "1차 익절" in tp[0].reason
    assert tp[0].quantity == 13 and pos.exit_stage == bt.ExitStage.FIRST
    assert pos.remaining_quantity == 139 - 13

    em = _em()
    em.config.first_exit_pct = 10.0
    em.config.first_exit_ratio = 0.1
    _register_like_scheduler(em, "sepa_trend", 139, 2.2)
    live = em.update_price(SYM, Decimal("11300"))
    assert live is not None and live[0] == "sell_partial" and live[1] == 13
    em.on_fill(SYM, live[1], Decimal("11300"))          # 실엔진 단계 승급은 체결 확인 후
    st = em.get_state(SYM)
    assert st.current_stage.value == "first" and st.remaining_quantity == 139 - 13


@pytest.mark.xfail(strict=True, reason=(
    "알려진 미지원 차이: 백테스터는 일봉 고가 접촉으로 익절을 판정하고 종가에 체결하지만, "
    "실엔진은 체결가(현재가) 기준이다. 장중 고가만 목표를 넘긴 봉에서 결과가 갈린다 — "
    "일봉 근사의 구조적 한계, docs/research/exit-policy-ab-2026-09.md §parity"))
def test_parity_take_profit_touch_semantics():
    tp_only_high = {FILL_DAY: {"고가": 11400.0, "저가": 9900.0, "종가": 10100.0}}
    eng, _ = _bt_fill({"X": _frame(overrides=tp_only_high)})
    eng._check_exits(FILL_DAY)
    bt_sold = any(t.side == "SELL" for t in eng.trades)

    em = _em()
    em.config.first_exit_pct = 10.0
    _register_like_scheduler(em, "sepa_trend", 139, 2.2)
    live_sold = em.update_price(SYM, Decimal("10100")) is not None
    assert bt_sold == live_sold


# ── 4. 비용(수수료) ──────────────────────────────────────────────────────────

def test_parity_fee_model():
    fee = get_fee_calculator("KR")
    amount = Decimal("1390000")
    # 실엔진은 원 단위 반올림, 백테스터는 실수 — 차이는 1원 미만이어야 한다
    assert bt.BTFeeCalculator.buy_fee(float(amount)) == pytest.approx(
        float(fee.calculate_buy_fee(amount)), abs=1.0)
    assert bt.BTFeeCalculator.sell_fee(float(amount)) == pytest.approx(
        float(fee.calculate_sell_fee(amount)), abs=1.0)
    assert bt.BUY_FEE_RATE == float(fee.config.buy_commission_rate)
    assert bt.SELL_FEE_RATE == float(fee.config.total_sell_rate)


# ── 5. 슬롯 정책 (실효 8개 가중 슬롯) ────────────────────────────────────────

def _live_weight(remaining, original, stage):
    """실엔진 RiskManager._get_position_weight 를 합성 state 로 호출."""
    rm = object.__new__(LiveRiskManager)
    rm._exit_manager = SimpleNamespace(_states={SYM: SimpleNamespace(
        original_quantity=original, remaining_quantity=remaining,
        current_stage=SimpleNamespace(value=stage))})
    return rm._get_position_weight(SYM)


@pytest.mark.parametrize("remaining,original,stage", [
    (100, 100, "none"), (90, 100, "first"), (50, 100, "second"), (30, 100, "third"),
    (10, 100, "trailing"), (1, 100, "trailing"), (5, 100, "none"),
])
def test_parity_slot_weight_matches_live(remaining, original, stage):
    assert bt.live_slot_weight(remaining, original, stage) == pytest.approx(
        _live_weight(remaining, original, stage))


def test_live_weighted_slot_cap_uses_weighted_sum():
    """분할익절로 잔여가 준 포지션은 슬롯을 일부만 차지한다 (실엔진 max_positions 가중 카운트)."""
    eng = _bt_engine({"X": _frame()}, slot_policy="live_weighted", max_positions_weighted=2.0)
    for i, (remain, stage) in enumerate([(100, bt.ExitStage.NONE),
                                         (20, bt.ExitStage.TRAILING)]):
        p = bt.BTPosition(symbol=f"S{i}", name=f"S{i}", strategy=bt.StrategyType.SEPA,
                          entry_date="2026-02-02", entry_price=10000.0, quantity=100,
                          cost_basis=1_000_000.0, highest_price=10000.0)
        p.remaining_quantity = remain
        p.exit_stage = stage
        eng.positions[p.symbol] = p
    # 1.0 + max(0.1, 0.2×0.5)=0.1 → 1.1 슬롯 (fixed 라면 2 슬롯으로 만석)
    assert eng._short_slot_usage() == pytest.approx(1.1)
    eng.config.slot_policy = "fixed"
    assert eng._short_slot_usage() == 2.0
