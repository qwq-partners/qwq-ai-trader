"""위험 기반 사이징 (2026-09-13 리뷰 권고 ③) — 헬퍼 + 엔진 _calculate_position_size risk 모드

근거: docs/research/exit-policy-ab-2026-09.md (ladder/current/risk 가 두 윈도우 모두 게이트 통과).
실행: QWQ_VERIFY_PYTHON=venv/bin/python bash scripts/dev/verify.sh 또는 pytest tests/test_risk_sizing.py -q
"""
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.engine import RiskManager  # noqa: E402
from src.core.event import SignalEvent  # noqa: E402
from src.core.types import RiskConfig, SignalStrength, StrategyType  # noqa: E402
from src.utils.sizing import atr_position_multiplier, risk_position_value  # noqa: E402

EQ = Decimal("10000000")


# ── 헬퍼: equity × 위험% / 손절% (ATR×2, 4~8 클램프), 상한 max_position_pct ────────

def test_helper_atr_to_stop_and_value():
    v, stop = risk_position_value(EQ, 2.5, risk_per_trade_pct=0.7, max_position_pct=18.0)
    assert (stop, v) == (5.0, Decimal("1400000"))                # 0.7/5 = 14%
    v, stop = risk_position_value(EQ, 1.0, risk_per_trade_pct=0.7, max_position_pct=18.0)
    assert (stop, v) == (4.0, Decimal("1750000"))                # 하한 클램프 4% → 17.5%
    v, stop = risk_position_value(EQ, 6.0, risk_per_trade_pct=0.7, max_position_pct=18.0)
    assert (stop, v) == (8.0, Decimal("875000"))                 # 상한 클램프 8% → 8.75%


def test_helper_fallback_and_cap():
    v, stop = risk_position_value(EQ, None, risk_per_trade_pct=0.7, max_position_pct=18.0,
                                  fallback_stop_pct=4.0)
    assert (stop, v) == (4.0, Decimal("1750000"))                # ATR 없음 → 기본 손절폭
    v, stop = risk_position_value(EQ, 0.0, risk_per_trade_pct=0.7, max_position_pct=18.0,
                                  fallback_stop_pct=5.0)
    assert (stop, v) == (5.0, Decimal("1400000"))                # ATR 0 도 폴백 (0 은 무효)
    v, _ = risk_position_value(EQ, 1.0, risk_per_trade_pct=1.0, max_position_pct=18.0)
    assert v == Decimal("1800000")                               # 25% > 상한 18% → 캡
    v, stop = risk_position_value(EQ, 2.5, risk_per_trade_pct=0.7, max_position_pct=18.0,
                                  stop_params=(1.5, 3.0, 6.0))   # ExitConfig 주입값 존중
    assert (stop, v) == (3.75, Decimal("1800000"))               # 18.67% → 상한 18% 캡


# ── 엔진 경로: risk 모드 vs nominal 모드 (같은 신호) ───────────────────────────────

def _rm(monkeypatch, *, mode, cash="5000000"):
    for target in ("src.utils.calendar_seasonality.calendar_multiplier",
                   "src.utils.volatility_targeting.vol_targeting_multiplier",
                   "src.utils.team_conviction.team_conviction_multiplier"):
        monkeypatch.setattr(target, lambda *_a, **_k: (1.0, ""))
    rm = object.__new__(RiskManager)
    rm.config = RiskConfig(
        sizing_mode=mode, risk_per_trade_pct=0.7, risk_max_position_pct=18.0,
        base_position_pct=25.0, max_position_pct=28.0, min_position_value=200000,
        default_stop_loss_pct=4.0, daily_max_loss_pct=5.0,
        strategy_allocation={"sepa_trend": 40.0, "vcp_breakout": 10.0},
    )
    rm.engine = SimpleNamespace(
        portfolio=SimpleNamespace(total_equity=EQ, effective_daily_pnl=Decimal("0"),
                                  get_strategy_allocation=lambda _s: Decimal("0")),
        get_available_cash=lambda: Decimal(cash),
    )
    rm._reserved_by_order = {}                       # _reserved_cash 프로퍼티의 원천
    rm._get_core_reserve = lambda: Decimal("0")
    rm._get_core_actual_value = lambda: Decimal("0")
    rm._pending_strategy_notional = lambda _s: Decimal("0")
    return rm


def _sig(atr_pct, strategy=StrategyType.SEPA_TREND, strength=SignalStrength.NORMAL):
    meta = {"atr_pct": atr_pct, "position_multiplier": atr_position_multiplier(atr_pct)}
    return SignalEvent(symbol="005930", price=Decimal("10000"), strategy=strategy,
                       strength=strength, signal=SimpleNamespace(metadata=meta))


def test_engine_risk_mode_sizes_by_stop_and_tags_metadata(monkeypatch):
    rm = _rm(monkeypatch, mode="risk")
    sig = _sig(2.5)                                   # 손절 5% → 14% = 1,400,000 → 140주
    assert rm._calculate_position_size(sig) == 140
    assert sig.signal.metadata["sizing_mode"] == "risk"
    assert sig.signal.metadata["risk_stop_pct"] == 5.0
    # ATR 배율(0.956)은 손절폭에 이미 반영 → 이중 축소 없음 (140 그대로)
    assert rm._calculate_position_size(_sig(6.0, StrategyType.VCP_BREAKOUT)) == 87   # 8% → 8.75%


def test_engine_risk_mode_uses_injected_exit_stop_params_and_ignores_strength(monkeypatch):
    rm = _rm(monkeypatch, mode="risk")
    rm._exit_stop_params = (2.0, 4.0, 6.0)            # ExitConfig max_stop 6 → ATR 6 은 6% 손절
    assert rm._calculate_position_size(_sig(6.0)) == 116          # 0.7/6 = 11.67%
    assert rm._calculate_position_size(_sig(2.5, strength=SignalStrength.VERY_STRONG)) == 140


def test_engine_nominal_mode_unchanged(monkeypatch):
    rm = _rm(monkeypatch, mode="nominal")
    sig = _sig(2.5)                                   # sepa 25% × ATR 배율 0.956 = 2,390,625 → 239주
    assert rm._calculate_position_size(sig) == 239
    assert "sizing_mode" not in sig.signal.metadata


def test_engine_risk_mode_respects_cash_and_strategy_cap(monkeypatch):
    rm = _rm(monkeypatch, mode="risk", cash="500000")
    assert rm._calculate_position_size(_sig(2.5)) == 38           # 현금 500,000 / (10,000×1.3) 증거금
    rm = _rm(monkeypatch, mode="risk")
    rm.config.strategy_allocation["sepa_trend"] = 5.0             # 전략 예산 5% = 500,000 → 50주
    assert rm._calculate_position_size(_sig(2.5)) == 50
