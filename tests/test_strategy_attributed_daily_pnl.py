"""59차(2026-10-10) — manual 보유의 당일 변동을 뺀 '전략 귀속 일일 손익' 측정.

10/8 관측: 사용자 수동 보유 087010 이 -24.7% 로 출발해 신규 전략 신호 3건이 모두
일일 손실 한도(-27.6%)에 막혔다. 게이트 판정은 계좌 전체 그대로 두고(한도 완화 없음),
거부 사유·자산 스냅샷에 전략 귀속 손익을 함께 남긴다.
"""
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from src.core.engine import UnifiedEngine
from src.core.types import OrderSide, Portfolio, Position, RiskConfig
from src.risk.manager import RiskManager


def _pf(manual_now, manual_start, strat_now=None, strat_start=None, realized="0"):
    pf = Portfolio(cash=Decimal("4000000"), initial_capital=Decimal("20000000"))
    pf.positions["087010"] = Position(symbol="087010", quantity=120, avg_price=Decimal("200000"),
                                      current_price=Decimal("200000"), strategy="manual")
    pf.positions["087010"].current_price = Decimal("200000") + Decimal(manual_start) / 120
    if strat_now is not None:
        pf.positions["005930"] = Position(symbol="005930", quantity=10, avg_price=Decimal("70000"),
                                          current_price=Decimal("70000"), strategy="sepa_trend")
        pf.positions["005930"].current_price = Decimal("70000") + Decimal(strat_start) / 10
    pf.mark_daily_start()  # 장 시작 기준선(총합+종목별)
    pf.positions["087010"].current_price = Decimal("200000") + Decimal(manual_now) / 120
    if strat_now is not None:
        pf.positions["005930"].current_price = Decimal("70000") + Decimal(strat_now) / 10
    pf.daily_pnl = Decimal(realized)
    return pf


def test_manual_delta_is_excluded_from_strategy_pnl():
    pf = _pf(manual_now="-5000000", manual_start="-1000000",
             strat_now="-100000", strat_start="0", realized="-50000")
    # 계좌 전체: 실현 -5만 + 미실현 변동(-510만 - (-100만)) = -415만
    assert pf.effective_daily_pnl == Decimal("-4150000")
    assert pf.manual_daily_unrealized_delta == Decimal("-4000000")
    # 전략 귀속: -415만 - (-400만) = -15만 (sepa -10만 + 실현 -5만)
    assert pf.strategy_effective_daily_pnl == Decimal("-150000")


def test_no_manual_positions_equals_effective():
    pf = Portfolio(cash=Decimal("1000000"))
    pf.positions["005930"] = Position(symbol="005930", quantity=10, avg_price=Decimal("70000"),
                                      current_price=Decimal("70000"), strategy="sepa_trend")
    pf.mark_daily_start()
    pf.positions["005930"].current_price = Decimal("69000")
    assert pf.manual_daily_unrealized_delta == Decimal("0")
    assert pf.strategy_effective_daily_pnl == pf.effective_daily_pnl == Decimal("-10000")


def test_legacy_total_only_baseline_is_unmeasured():
    """옛 engine_daily_stats.json(총합 기준선만) 복원 상태 — 귀속 불가는 None 이어야 한다(0 금지)."""
    pf = _pf(manual_now="-5000000", manual_start="-1000000")
    pf.daily_start_unrealized_by_symbol = {}
    assert pf.daily_start_unrealized_pnl == Decimal("-1000000")
    assert pf.manual_daily_unrealized_delta is None
    assert pf.strategy_effective_daily_pnl is None


def test_manual_position_opened_today_counts_fully():
    pf = Portfolio(cash=Decimal("1000000"))
    pf.mark_daily_start()  # 보유 없음
    pf.positions["087010"] = Position(symbol="087010", quantity=1, avg_price=Decimal("100000"),
                                      current_price=Decimal("90000"), strategy="manual")
    assert pf.manual_daily_unrealized_delta == Decimal("-10000")
    assert pf.strategy_effective_daily_pnl == Decimal("0")


def test_gate_still_blocks_but_reason_carries_attribution(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    risk = RiskManager(RiskConfig(daily_max_loss_pct=5.0), Decimal("20000000"), market="KR")
    risk.set_cash_verification(True)
    pf = _pf(manual_now="-5000000", manual_start="-1000000")  # 자산 ≈ 1,900만, 당일 -400만 ≈ -21%
    allowed, reason = risk.can_open_position(
        "005930", OrderSide.BUY, 10, Decimal("70000"), pf, strategy_type="sepa_trend")
    assert not allowed                      # 판정 불변: 계좌 전체 한도로 차단
    assert "일일 손실 한도 초과" in reason and "전면 차단" in reason
    assert "전략 귀속 +0.0%" in reason     # 전략 손실은 0


def test_gate_reason_marks_unmeasured_on_legacy_baseline(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    risk = RiskManager(RiskConfig(daily_max_loss_pct=5.0), Decimal("20000000"), market="KR")
    risk.set_cash_verification(True)
    pf = _pf(manual_now="-5000000", manual_start="-1000000")
    pf.daily_start_unrealized_by_symbol = {}
    allowed, reason = risk.can_open_position(
        "005930", OrderSide.BUY, 10, Decimal("70000"), pf, strategy_type="sepa_trend")
    assert not allowed and "전략 귀속 미측정" in reason


def test_daily_stats_roundtrip_keeps_per_symbol_baseline(tmp_path):
    pf = _pf(manual_now="-5000000", manual_start="-1000000", strat_now="-100000", strat_start="0")
    fake = SimpleNamespace(portfolio=pf, _counted_buy_order_ids={"o1"},
                           _DAILY_STATS_PATH=tmp_path / "engine_daily_stats.json")
    UnifiedEngine._save_daily_stats(fake)
    saved = json.loads(fake._DAILY_STATS_PATH.read_text())
    assert {k: Decimal(v) for k, v in saved["daily_start_unrealized_by_symbol"].items()} == {
        "087010": Decimal("-1000000"), "005930": Decimal("0")}

    fresh = SimpleNamespace(portfolio=Portfolio(), _counted_buy_order_ids=set(),
                            _DAILY_STATS_PATH=fake._DAILY_STATS_PATH, _daily_stats_restored=False)
    UnifiedEngine.restore_daily_stats(fresh)
    assert fresh.portfolio.daily_start_unrealized_by_symbol == {
        "087010": Decimal("-1000000"), "005930": Decimal("0")}


def test_restore_without_per_symbol_key_leaves_empty_dict(tmp_path):
    """48차 이전 형식의 파일 — 키 부재는 {} 로 복원되어 '미측정' 경로로 간다."""
    from datetime import date
    path = tmp_path / "engine_daily_stats.json"
    path.write_text(json.dumps({"date": date.today().isoformat(), "daily_pnl": "0",
                                "daily_start_unrealized_pnl": "-1000000", "daily_trades": 0}))
    fresh = SimpleNamespace(portfolio=Portfolio(), _counted_buy_order_ids=set(),
                            _DAILY_STATS_PATH=path, _daily_stats_restored=False)
    UnifiedEngine.restore_daily_stats(fresh)
    assert fresh.portfolio.daily_start_unrealized_pnl == Decimal("-1000000")
    assert fresh.portfolio.daily_start_unrealized_by_symbol == {}
