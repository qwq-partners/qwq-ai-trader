"""위험 기반 사이징 — 실제 손절 기준 (2026-09-14 리뷰 후속 F1/T2)

기준 결함(F1, de111b7): 사이징 분모는 ATR×2(4~8 클램프)였지만 신규 체결 등록(kr_scheduler
register_position)은 price_history 없이 호출되어 ATR 동적 손절이 생기지 않고 전략별 고정 SL
(sepa 5 / gap 3.5 / vcp 4, run_trader._strategy_exit_params) 이 적용된다. → 분모를 ExitManager 의
실제 해석(resolve_stop) 으로 교체하고, 모든 오버레이 뒤 매수수수료 포함 계획 위험 ≤ equity×0.7% 를 최종 검사.

실행: venv/bin/python -m pytest tests/test_risk_sizing.py -q -p no:cacheprovider
프로덕션 캐시·네트워크 무접촉 — Path.home() 을 tmp_path 로 패치, 합성 값만 사용.
"""
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.engine import RiskManager  # noqa: E402
from src.core.event import SignalEvent  # noqa: E402
from src.core.types import (  # noqa: E402
    OrderSide, Position, RiskConfig, Signal, SignalStrength, StrategyType,
)
from src.strategies.exit_manager import ExitConfig, ExitManager  # noqa: E402
from src.utils.fee_calculator import get_fee_calculator  # noqa: E402
from src.utils.sizing import atr_position_multiplier, planned_risk, risk_quantity_cap  # noqa: E402
from src.utils.stop_policy import make_entry_stop_resolver  # noqa: E402

EQ = Decimal("10000000")
PRICE = Decimal("10000")
SYM = "005930"

# run_trader._strategy_exit_params 실효값 (default.yml + evolved_overrides 병합, 2026-09-14 코드 확인):
#   sepa 5.0 (default.yml kr.strategies.sepa_trend, evolved 미덮음) / gap 3.5 (evolved gap_and_go.stop_loss_pct) /
#   vcp 4.0 (run_trader 하드코딩) / core 10.0 is_core
STRATEGY_EXIT_PARAMS = {
    "sepa_trend": {"stop_loss_pct": 5.0, "trailing_stop_pct": 3.0, "stale_high_days": 3},
    "gap_and_go": {"stop_loss_pct": 3.5, "trailing_stop_pct": 1.5},
    "vcp_breakout": {"stop_loss_pct": 4.0, "trailing_stop_pct": 2.5, "stale_high_days": 3},
    "core_holding": {"stop_loss_pct": 10.0, "trailing_stop_pct": 12.0, "is_core": True},
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _em():
    """운영 ExitConfig 실효값 (evolved: stop 5 / min 4 / max 8 / atr 2)."""
    return ExitManager(ExitConfig(stop_loss_pct=5.0, min_stop_pct=4.0, max_stop_pct=8.0,
                                  atr_multiplier=2.0))


def _rm(monkeypatch, *, mode, cash="5000000", em=None, min_value=200000,
        overlays=None):
    """엔진 RiskManager 최소 구성. overlays: {"calendar": 1.1, "vol": 0.5, "team": 1.2}."""
    ov = overlays or {}
    monkeypatch.setattr("src.utils.calendar_seasonality.calendar_multiplier",
                        lambda *_a, **_k: (ov.get("calendar", 1.0), ""))
    monkeypatch.setattr("src.utils.volatility_targeting.vol_targeting_multiplier",
                        lambda *_a, **_k: (ov.get("vol", 1.0), ""))
    monkeypatch.setattr("src.utils.team_conviction.team_conviction_multiplier",
                        lambda *_a, **_k: (ov.get("team", 1.0), ""))
    rm = object.__new__(RiskManager)
    rm.config = RiskConfig(
        sizing_mode=mode, risk_per_trade_pct=0.7, risk_max_position_pct=18.0,
        base_position_pct=25.0, max_position_pct=28.0, min_position_value=min_value,
        default_stop_loss_pct=2.8,      # evolved risk_config 값 — ExitManager 손절이 아니므로 분모에 쓰이면 안 됨
        daily_max_loss_pct=5.0,
        strategy_allocation={"sepa_trend": 40.0, "vcp_breakout": 10.0, "gap_and_go": 15.0,
                             "core_holding": 30.0},
    )
    rm.engine = SimpleNamespace(
        portfolio=SimpleNamespace(total_equity=EQ, effective_daily_pnl=Decimal("0"),
                                  get_strategy_allocation=lambda _s: Decimal("0")),
        get_available_cash=lambda: Decimal(cash),
    )
    rm._reserved_by_order = {}
    rm._get_core_reserve = lambda: Decimal("0")
    rm._get_core_actual_value = lambda: Decimal("0")
    rm._pending_strategy_notional = lambda _s: Decimal("0")
    if em is not None:
        rm._resolve_entry_stop = make_entry_stop_resolver(em, STRATEGY_EXIT_PARAMS)
    return rm


def _sig(atr_pct, strategy=StrategyType.SEPA_TREND, strength=SignalStrength.NORMAL,
         price=PRICE, **extra):
    """실제 Signal → SignalEvent.from_signal (전략이 넣는 atr_pct·position_multiplier 메타 포함)."""
    meta = {"atr_pct": atr_pct}
    if isinstance(atr_pct, (int, float)) and atr_pct > 0:
        meta["position_multiplier"] = atr_position_multiplier(atr_pct)
    meta.update(extra)
    s = Signal(symbol=SYM, side=OrderSide.BUY, strength=strength, strategy=strategy,
               price=price, metadata=meta)
    return SignalEvent.from_signal(s, source="test")


def _register_like_scheduler(em, strategy, qty, atr_hint):
    """kr_scheduler 신규 fill 등록 경로 미러 — price_history 없음, atr_pct_hint 만 전달."""
    p = STRATEGY_EXIT_PARAMS[strategy]
    pos = Position(symbol=SYM, quantity=qty, avg_price=PRICE, current_price=PRICE,
                   strategy=strategy)
    em.register_position(
        pos, stop_loss_pct=p.get("stop_loss_pct"), trailing_stop_pct=p.get("trailing_stop_pct"),
        stale_high_days=p.get("stale_high_days"), is_core=p.get("is_core", False),
        atr_pct_hint=atr_hint,
    )
    return em.get_state(SYM)


def _net_stop_price(stop_pct):
    """수수료 포함 순손익률이 -stop_pct 를 막 넘는 가격 / 아직 안 넘는 가격."""
    just_above = PRICE * (1 - Decimal(str(stop_pct)) / 100) + 40
    just_below = PRICE * (1 - Decimal(str(stop_pct)) / 100) - 40
    return just_above, just_below


# ── 헬퍼: 최종 위험 상한 수량 ──────────────────────────────────────────────────

def test_helper_cap_139_not_140_with_buy_fee():
    fee = get_fee_calculator("KR")
    assert planned_risk(PRICE, 139, Decimal("5"), fee) == Decimal("69509.75")
    assert planned_risk(PRICE, 140, Decimal("5"), fee) == Decimal("70009.85")
    assert risk_quantity_cap(EQ, PRICE, Decimal("5"), risk_per_trade_pct=0.7) == 139
    assert risk_quantity_cap(EQ, PRICE, Decimal("4"), risk_per_trade_pct=0.7) == 174
    assert risk_quantity_cap(EQ, PRICE, Decimal("0"), risk_per_trade_pct=0.7) == 0
    assert risk_quantity_cap(EQ, Decimal("0"), Decimal("5"), risk_per_trade_pct=0.7) == 0


# ── F1: SEPA ATR 1%·6% 모두 실제 고정 SL 5% 분모 (신규 fill 에 dynamic stop 없음) ───

@pytest.mark.parametrize("atr", [1.0, 6.0])
def test_f1_sepa_denominator_is_fixed_sl_regardless_of_atr(home, monkeypatch, atr):
    em = _em()
    rm = _rm(monkeypatch, mode="risk", em=em)
    sig = _sig(atr)
    assert rm._calculate_position_size(sig) == 139
    meta = sig.signal.metadata
    assert (meta["sizing_mode"], meta["risk_stop_pct"], meta["stop_source"]) == ("risk", 5.0, "strategy")

    # 신규 등록 경로 (price_history 없음): ATR hint 는 트레일링용, 손절은 고정 5% 그대로
    st = _register_like_scheduler(em, "sepa_trend", 139, atr)
    assert st.dynamic_stop_pct is None and st.atr_pct == pytest.approx(atr)
    above, below = _net_stop_price(5.0)
    assert em.update_price(SYM, above) is None
    sig_exit = em.update_price(SYM, below)
    assert sig_exit[0] == "sell_all" and "(SL=5.00%" in sig_exit[2]


def test_f1_strategy_specific_fixed_sl(home, monkeypatch):
    em = _em()
    rm = _rm(monkeypatch, mode="risk", em=em)
    # vcp 4% → 174주이나 전략 예산 10% = 1,000,000 → 100주 (계획 위험 40,000 ≤ 70,000)
    s = _sig(6.0, StrategyType.VCP_BREAKOUT)
    assert rm._calculate_position_size(s) == 100
    assert s.signal.metadata["risk_stop_pct"] == 4.0
    # gap 3.5% → 2,000,000 → 상한 18% 1,800,000 → 예산 15% 1,500,000 → 150주 (위험 52,500)
    g = _sig(2.5, StrategyType.GAP_AND_GO)
    assert rm._calculate_position_size(g) == 150
    assert g.signal.metadata["risk_stop_pct"] == 3.5


@pytest.mark.parametrize("atr", [None, 0, 0.0, "abc"])
def test_atr_missing_or_garbage_still_uses_real_sl(home, monkeypatch, atr):
    rm = _rm(monkeypatch, mode="risk", em=_em())
    sig = _sig(atr)
    assert rm._calculate_position_size(sig) == 139
    assert sig.signal.metadata["risk_stop_pct"] == 5.0


def test_global_fallback_when_strategy_has_no_fixed_sl(home, monkeypatch):
    em = _em()
    rm = _rm(monkeypatch, mode="risk", em=em)
    rm.config.strategy_allocation["strategic_swing"] = 40.0
    sig = _sig(2.5, StrategyType.STRATEGIC_SWING)      # STRATEGY_EXIT_PARAMS 에 없음 → ExitConfig 5.0
    assert rm._calculate_position_size(sig) == 139
    assert sig.signal.metadata["stop_source"] == "global"


# ── 최종 불변조건: 증액 오버레이는 0.7% 를 못 넘고, 축소는 되돌리지 않는다 ────────

def test_boost_overlays_cannot_exceed_risk_budget(home, monkeypatch):
    rm = _rm(monkeypatch, mode="risk", em=_em(), overlays={"calendar": 1.1, "team": 1.2})
    sig = _sig(2.5, position_multiplier=1.3)           # LLM 배율(ATR 배율과 다름) 도 곱해짐
    q = rm._calculate_position_size(sig)
    assert q == 139
    assert planned_risk(PRICE, q, Decimal("5"), get_fee_calculator("KR")) <= EQ * Decimal("0.007")


def test_downscale_overlay_is_not_reinflated(home, monkeypatch):
    rm = _rm(monkeypatch, mode="risk", em=_em(), overlays={"vol": 0.5})
    assert rm._calculate_position_size(_sig(2.5)) == 70   # 1,400,000 × 0.5 → 70주 (< 139 이지만 유지)


def test_three_share_correction_then_cap_allows_two_shares(home, monkeypatch):
    # 가격 50만원: 1,400,000 → 2주 → 3주 보정(150만 ≤ 상한 180만) → 위험 상한 2주 (3주면 75,010 > 70,000)
    rm = _rm(monkeypatch, mode="risk", em=_em())
    assert rm._calculate_position_size(_sig(2.5, price=Decimal("500000"))) == 2


def test_cap_reduction_below_min_value_is_rejected(home, monkeypatch):
    rm = _rm(monkeypatch, mode="risk", em=_em(), min_value=1200000)
    # 2주 × 50만 = 100만 < 최소 120만 → 명시 거부 (0)
    assert rm._calculate_position_size(_sig(2.5, price=Decimal("500000"))) == 0


# ── 무효 설정·미배선은 신규 주문 거부 (fail-closed, nominal 자동 복귀 없음) ────────

def test_invalid_real_sl_rejects_order(home, monkeypatch):
    em = _em()
    rm = _rm(monkeypatch, mode="risk", em=em)
    em.config.stop_loss_pct = 0.0                       # global 까지 무효
    rm.config.strategy_allocation["strategic_swing"] = 40.0
    sig = _sig(2.5, StrategyType.STRATEGIC_SWING)       # 전략 SL 없음 → global 0 → ValueError
    assert rm._calculate_position_size(sig) == 0
    assert "sizing_mode" not in sig.signal.metadata     # 태그 없음 = pending 미생성 경로


def test_missing_resolver_rejects_in_risk_mode(home, monkeypatch):
    rm = _rm(monkeypatch, mode="risk", em=None)         # 콜백 미배선
    assert rm._calculate_position_size(_sig(2.5)) == 0


# ── core / nominal 은 기존 결과 보존 ────────────────────────────────────────────

def test_engine_nominal_mode_unchanged(home, monkeypatch):
    rm = _rm(monkeypatch, mode="nominal", em=_em())
    sig = _sig(2.5)                                     # sepa 25% × ATR 배율 0.956 = 2,390,625 → 239주
    assert rm._calculate_position_size(sig) == 239
    assert "sizing_mode" not in sig.signal.metadata


def test_core_holding_bypasses_risk_mode(home, monkeypatch):
    rm = _rm(monkeypatch, mode="risk", em=_em())
    sig = _sig(2.5, StrategyType.CORE_HOLDING)          # 코어 10% × ATR 배율 0.956 = 956,250 → 95주 (nominal 경로 그대로)
    assert rm._calculate_position_size(sig) == 95
    assert "sizing_mode" not in sig.signal.metadata


def test_engine_risk_mode_respects_cash_and_strategy_cap(home, monkeypatch):
    rm = _rm(monkeypatch, mode="risk", cash="500000", em=_em())
    assert rm._calculate_position_size(_sig(2.5)) == 38        # 현금 500,000 / (10,000×1.3) 증거금
    rm = _rm(monkeypatch, mode="risk", em=_em())
    rm.config.strategy_allocation["sepa_trend"] = 5.0          # 전략 예산 5% = 500,000 → 50주
    assert rm._calculate_position_size(_sig(2.5)) == 50


# ── 급락 cap: 사이징 분모와 ExitManager 판정이 같은 해석 ──────────────────────────

def test_crash_cap_same_interpretation_as_exit_manager(home, monkeypatch):
    em = _em()
    em.apply_intraday_crash_params("crash")             # SL cap 2.5
    rm = _rm(monkeypatch, mode="risk", em=em)
    sig = _sig(2.5)
    # 0.7/2.5 = 28% → 상한 18% = 1,800,000 → 180주 (위험 45,006 ≤ 70,000)
    assert rm._calculate_position_size(sig) == 180
    assert (sig.signal.metadata["risk_stop_pct"], sig.signal.metadata["stop_source"]) == (2.5, "strategy")

    st = _register_like_scheduler(em, "sepa_trend", 180, 2.5)
    above, below = _net_stop_price(2.5)
    assert em.update_price(SYM, above) is None
    assert "(SL=2.50%" in em.update_price(SYM, below)[2]

    # 코어는 급락 cap 제외 — 사이징은 nominal 경로라 태그 없음
    core = _sig(2.5, StrategyType.CORE_HOLDING)
    assert rm._calculate_position_size(core) == 95     # nominal 경로 (10% × ATR 배율 0.956)
    assert "risk_stop_pct" not in core.signal.metadata
    assert em.resolve_stop(dynamic_stop_pct=10.0, fixed_stop_pct=None, is_core=True).crash_capped is False
