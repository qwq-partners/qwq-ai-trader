"""T11 담당 C — 돈 경로 기준선 특성화 (2026-09-15, 인수 조건 §4 #9)

**기존 동작을 그대로 고정**하는 특성화 테스트다. 여기 적힌 값은 "옳다"는 주장이 아니라
"T11 이전에 이랬다"는 기록이며, EntryPlan shadow 배선(계획 필드 추가·metadata 적재·
engine shadow 훅) 전후로 **바뀌면 안 되는** 것만 담는다.

고정 대상:
  1. PendingSignal → Signal 변환 (batch_analyzer.execute_pending_signals)
     — price/target/stop/score/confidence/strength/strategy 와 기존 metadata 키
  2. engine.RiskManager.on_signal 의 Order (수량·가격·order_type=MARKET·strategy·reason·signal_score)
     — ENTRY_PLAN_SHADOW on/off, 계획 유무 4조합에서 동일

프로덕션 캐시·네트워크 무접촉. 실행:
    venv/bin/python -m pytest tests/test_t11_money_path_baseline.py -q -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))

from src.core.batch_analyzer import PendingSignal  # noqa: E402
from src.core.types import OrderSide, OrderType, SignalStrength, StrategyType  # noqa: E402

from test_risk_sizing import _em, _rm, _sig, home  # noqa: E402,F401
from test_t11_entry_plan import (  # noqa: E402
    NOW, SYM, _order_env, _plan, _run_execute,
)


# ── 1) PendingSignal → Signal 변환 기준선 ─────────────────────────────────────

def _bare_pending(**kw) -> PendingSignal:
    """T11 확장 필드가 전부 기본값인 계획 = T11 이전 pending 과 동일한 상태."""
    base = dict(
        symbol=SYM, name="삼성전자", strategy="sepa_trend", side="buy",
        entry_price=10000.0, max_entry_price=10300.0, stop_price=9500.0,
        target_price=11000.0, score=70.0, reason="추세 정렬",
        created_at=(NOW - timedelta(hours=2)).isoformat(),
        expires_at=(datetime.now() + timedelta(hours=5)).isoformat(),
        atr_pct=2.5,
    )
    base.update(kw)
    return PendingSignal(**base)


def test_baseline_signal_fields_from_pending(monkeypatch):
    events = _run_execute(monkeypatch, [_bare_pending()], quote_price=10100.0)
    assert len(events) == 1
    s = events[0].signal

    assert s.symbol == SYM
    assert s.side is OrderSide.BUY
    assert s.strength is SignalStrength.STRONG          # neutral 레짐 + intraday normal
    assert s.strategy is StrategyType.SEPA_TREND
    assert s.price == Decimal("10100.0")                # 현재가 (계획의 entry_price 아님)
    assert s.target_price == Decimal("11000.0")
    assert s.stop_price == Decimal("9500.0")            # neutral 은 타이트닝 없음
    assert s.score == 70.0
    assert s.confidence == 0.7
    assert s.reason == "추세 정렬"


def test_baseline_signal_metadata_keys(monkeypatch):
    events = _run_execute(monkeypatch, [_bare_pending()], quote_price=10100.0)
    meta = events[0].signal.metadata
    baseline_keys = {
        "batch_signal", "name", "atr_pct", "position_multiplier", "market_regime",
        "intraday_state", "intraday_kospi_pct", "sector", "gap_pct", "indicators",
    }
    assert baseline_keys <= set(meta)
    assert meta["batch_signal"] is True
    assert meta["name"] == "삼성전자"
    assert meta["atr_pct"] == 2.5
    assert meta["market_regime"] == "neutral"
    assert meta["intraday_state"] == "normal"
    assert meta["gap_pct"] == 1.0                       # (10100-10000)/10000
    # T11 은 키를 **추가**만 한다 — 기존 키를 지우거나 바꾸지 않는다
    assert set(meta) - baseline_keys <= {"entry_plan"}


def test_baseline_regime_stop_tightening(monkeypatch):
    """하락장 손절 타이트닝·강도 강등은 기존 동작 — T11 이 건드리지 않는다."""
    ba_events = _run_execute(monkeypatch, [_bare_pending()], quote_price=10100.0)
    assert ba_events[0].signal.stop_price == Decimal("9500.0")


# ── 2) on_signal Order 기준선 (플래그 on/off × 계획 유무) ────────────────────

def _order_of(monkeypatch, *, flag: str, with_plan: bool):
    monkeypatch.setenv("ENTRY_PLAN_SHADOW", flag)
    rm = _rm(monkeypatch, mode="nominal", em=_em())
    _order_env(monkeypatch, rm)
    meta = {"entry_plan": _plan().to_dict()} if with_plan else {}
    ev = _sig(2.5, **meta)
    orders = asyncio.run(rm.on_signal(ev))
    assert orders, "기준선: 게이트 통과 시 주문이 나온다"
    return orders[0].order


def _fingerprint(o):
    return (o.symbol, o.side, o.order_type, o.quantity, o.price, o.strategy,
            o.reason, o.signal_score)


def test_baseline_buy_order_shape(home, monkeypatch):
    o = _order_of(monkeypatch, flag="0", with_plan=False)
    assert o.symbol == SYM
    assert o.side is OrderSide.BUY
    assert o.order_type is OrderType.MARKET       # 운영 주문 방식 불변 (D5)
    assert o.quantity == 239                      # nominal 25% × ATR 2.5 배율
    assert o.price == Decimal("10000")
    assert o.strategy == "sepa_trend"


@pytest.mark.parametrize("flag,with_plan", [
    ("0", False), ("0", True), ("1", False), ("1", True),
])
def test_order_identical_across_flag_and_plan(home, monkeypatch, flag, with_plan):
    """§4 #9: 플래그·계획 유무와 무관하게 주문 결과가 기준선과 같다."""
    base = _fingerprint(_order_of(monkeypatch, flag="0", with_plan=False))
    assert _fingerprint(_order_of(monkeypatch, flag=flag, with_plan=with_plan)) == base


def test_plan_metadata_does_not_change_position_size(home, monkeypatch):
    """계획 metadata 가 사이징 입력(atr_pct/position_multiplier)에 섞이지 않는다."""
    rm = _rm(monkeypatch, mode="risk", em=_em())
    assert rm._calculate_position_size(_sig(2.5)) == 139
    rm2 = _rm(monkeypatch, mode="risk", em=_em())
    assert rm2._calculate_position_size(_sig(2.5, entry_plan=_plan().to_dict())) == 139
