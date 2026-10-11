"""59차(2026-10-10) — manual 보유의 당일 변동을 뺀 '전략 귀속 일일 손익' 측정.

10/8 관측: 사용자 수동 보유 087010 이 -24.7% 로 출발해 신규 전략 신호 3건 중 2건이
일일 손실 한도(-27.6%)에 막혔다(나머지 1건은 수량 0). 게이트 판정은 계좌 전체 그대로 두고(한도 완화 없음),
거부 사유·자산 스냅샷에 전략 귀속 손익을 함께 남긴다.
"""
import json
import pytest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from src.core.engine import UnifiedEngine
from src.core.types import KST, OrderSide, Portfolio, Position, RiskConfig
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
    pf.daily_start_unrealized_by_symbol = None
    assert pf.daily_start_unrealized_pnl == Decimal("-1000000")
    assert pf.manual_daily_unrealized_delta is None
    assert pf.strategy_effective_daily_pnl is None


def test_legacy_baseline_with_zero_total_is_still_unmeasured():
    """리뷰 P2: 시작 manual +100만 / 전략 -100만 = 총합 0 — 총합 0 을 '확보' 로 추론하면 안 된다."""
    pf = _pf(manual_now="1000000", manual_start="1000000",
             strat_now="-1000000", strat_start="-1000000")
    assert pf.daily_start_unrealized_pnl == Decimal("0")
    pf.daily_start_unrealized_by_symbol = None          # 옛 파일 복원 상태
    pf.daily_start_manual_symbols = set()
    assert pf.effective_daily_pnl == Decimal("0")
    assert pf.strategy_effective_daily_pnl is None       # 가격 불변인데 -100만 으로 표시되면 결함


def test_unknown_baseline_without_manual_positions_is_still_unmeasured():
    """재리뷰 P2: 옛 파일 복원(총합 -100만) 뒤 manual 종목이 수동 매도로 사라진 상태 —
    effective 는 +100만 이지만 귀속은 None 이어야 한다(+100만 으로 '측정 완료' 표시 금지)."""
    pf = Portfolio(cash=Decimal("1000000"), daily_start_unrealized_pnl=Decimal("-1000000"))
    assert pf.daily_start_unrealized_by_symbol is None and not pf.positions
    assert pf.effective_daily_pnl == Decimal("1000000")
    assert pf.manual_daily_unrealized_delta is None
    assert pf.strategy_effective_daily_pnl is None


def test_manual_sold_by_user_intraday_keeps_its_baseline():
    """리뷰 P2: 시작 미실현 -100만 manual 종목을 사용자가 수동 매도 → 동기화가 포지션 제거.
    effective_daily_pnl 은 +100만 으로 뛰지만(기존 한계) 전략 귀속은 0 이어야 한다."""
    pf = _pf(manual_now="-1000000", manual_start="-1000000")
    del pf.positions["087010"]
    assert pf.effective_daily_pnl == Decimal("1000000")
    assert pf.manual_daily_unrealized_delta == Decimal("1000000")
    assert pf.strategy_effective_daily_pnl == Decimal("0")


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
    pf.daily_start_unrealized_by_symbol = None
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
    assert saved["daily_start_manual_symbols"] == ["087010"]

    fresh = SimpleNamespace(portfolio=Portfolio(), _counted_buy_order_ids=set(),
                            _DAILY_STATS_PATH=fake._DAILY_STATS_PATH, _daily_stats_restored=False)
    UnifiedEngine.restore_daily_stats(fresh)
    assert fresh.portfolio.daily_start_unrealized_by_symbol == {
        "087010": Decimal("-1000000"), "005930": Decimal("0")}
    assert fresh.portfolio.daily_start_manual_symbols == {"087010"}


def test_unmeasured_baseline_is_not_written_and_restores_as_none(tmp_path):
    pf = Portfolio(daily_start_unrealized_pnl=Decimal("-1000000"))   # 옛 파일에서 복원된 상태 가정
    fake = SimpleNamespace(portfolio=pf, _counted_buy_order_ids=set(),
                           _DAILY_STATS_PATH=tmp_path / "engine_daily_stats.json")
    UnifiedEngine._save_daily_stats(fake)
    assert "daily_start_unrealized_by_symbol" not in json.loads(fake._DAILY_STATS_PATH.read_text())
    fresh = SimpleNamespace(portfolio=Portfolio(), _counted_buy_order_ids=set(),
                            _DAILY_STATS_PATH=fake._DAILY_STATS_PATH, _daily_stats_restored=False)
    UnifiedEngine.restore_daily_stats(fresh)
    assert fresh.portfolio.daily_start_unrealized_by_symbol is None


def test_corrupt_attribution_field_does_not_break_core_restore(tmp_path):
    """리뷰 P1: 귀속 필드 파손(null 값)이 핵심 복원·복원 완료 플래그를 깨면 DB 백필이 총합
    기준선(유효한 0)을 덮어써 게이트가 바뀐다 — 귀속만 미측정으로 떨어져야 한다."""
    from datetime import date
    path = tmp_path / "engine_daily_stats.json"
    path.write_text(json.dumps({"date": date.today().isoformat(), "daily_pnl": "-50000",
                                "daily_start_unrealized_pnl": "0", "daily_trades": 3,
                                "counted_buy_order_ids": ["o1"],
                                "daily_start_unrealized_by_symbol": {"087010": None}}))
    fresh = SimpleNamespace(portfolio=Portfolio(), _counted_buy_order_ids=set(),
                            _DAILY_STATS_PATH=path, _daily_stats_restored=False)
    UnifiedEngine.restore_daily_stats(fresh)
    assert fresh._daily_stats_restored is True
    assert fresh.portfolio.daily_pnl == Decimal("-50000")
    assert fresh.portfolio.daily_start_unrealized_pnl == Decimal("0")
    assert fresh.portfolio.daily_trades == 3 and fresh._counted_buy_order_ids == {"o1"}
    assert fresh.portfolio.daily_start_unrealized_by_symbol is None


def test_corrupt_manual_symbols_drop_attribution_to_unmeasured(tmp_path):
    """재리뷰 P2: manual 집합이 null/문자열/기준선 밖 종목이면 귀속 전체를 미확보(None)로 — 빈 집합 수용 금지."""
    from datetime import date
    for bad in (None, "087010", ["999999"], 0):
        path = tmp_path / f"stats-{type(bad).__name__}.json"
        path.write_text(json.dumps({"date": date.today().isoformat(), "daily_pnl": "0",
                                    "daily_start_unrealized_pnl": "-1000000", "daily_trades": 0,
                                    "daily_start_unrealized_by_symbol": {"087010": "-1000000"},
                                    "daily_start_manual_symbols": bad}))
        fresh = SimpleNamespace(portfolio=Portfolio(), _counted_buy_order_ids=set(),
                                _DAILY_STATS_PATH=path, _daily_stats_restored=False)
        UnifiedEngine.restore_daily_stats(fresh)
        assert fresh._daily_stats_restored is True
        assert fresh.portfolio.daily_start_unrealized_pnl == Decimal("-1000000")
        assert fresh.portfolio.daily_start_unrealized_by_symbol is None, bad
        assert fresh.portfolio.strategy_effective_daily_pnl is None, bad


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
    assert fresh.portfolio.daily_start_unrealized_by_symbol is None


def test_manual_to_strategy_without_creation_identity_is_unmeasured():
    """62차(Codex 인계 재검토 지적 1): 시작 manual -100만 → 사용자 매도 → 봇이 같은 종목을 전략으로 재매수(미실현 0).
    진입 시각만으로는 재생성을 증명하지 못하므로 귀속을 미측정으로 둔다."""
    from datetime import datetime as _dt
    pf = Portfolio(cash=Decimal("1000000"))
    pf.positions["X"] = Position(symbol="X", quantity=10, avg_price=Decimal("200000"),
                                 current_price=Decimal("100000"), strategy="manual")
    pf.mark_daily_start()
    del pf.positions["X"]
    pf.positions["X"] = Position(symbol="X", quantity=5, avg_price=Decimal("100000"),
                                 current_price=Decimal("100000"), strategy="sepa_trend",
                                 entry_time=_dt.now(KST))                      # 기준선 뒤 재생성
    assert pf.effective_daily_pnl == Decimal("1000000")
    assert pf.manual_daily_unrealized_delta is None
    assert pf.strategy_effective_daily_pnl is None
    pf.positions["X"].current_price = Decimal("90000")           # 가격이 변해도 소유 전환의 근거는 부족
    assert pf.strategy_effective_daily_pnl is None


def test_strategy_to_manual_without_creation_identity_is_unmeasured():
    """반대 방향: 시작 sepa +50만 → 봇 매도(실현 +50만) → 사용자가 같은 종목 manual 재매수(미실현 0) → 생성 identity가 없으므로 귀속 미측정."""
    from datetime import datetime as _dt
    pf = Portfolio(cash=Decimal("1000000"))
    pf.positions["Y"] = Position(symbol="Y", quantity=10, avg_price=Decimal("100000"),
                                 current_price=Decimal("150000"), strategy="sepa_trend")
    pf.mark_daily_start()
    del pf.positions["Y"]
    pf.daily_pnl = Decimal("500000")
    pf.positions["Y"] = Position(symbol="Y", quantity=3, avg_price=Decimal("150000"),
                                 current_price=Decimal("150000"), strategy="manual",
                                 entry_time=_dt.now(KST))                      # 기준선 뒤 재생성
    assert pf.effective_daily_pnl == Decimal("0")
    assert pf.manual_daily_unrealized_delta is None
    assert pf.strategy_effective_daily_pnl is None
    pf.positions["Y"].current_price = Decimal("140000")           # 가격이 변해도 귀속을 추정하지 않음
    assert pf.manual_daily_unrealized_delta is None
    assert pf.strategy_effective_daily_pnl is None


def test_tag_change_does_not_assign_ownership_without_lot_provenance():
    """이전 진입 시각도 종목의 소유 전환 내역을 증명하지 않는다."""
    pf = Portfolio(cash=Decimal("1000000"))
    pf.positions["X"] = Position(symbol="X", quantity=10, avg_price=Decimal("200000"),
                                 current_price=Decimal("100000"), strategy="manual")
    pf.positions["Y"] = Position(symbol="Y", quantity=10, avg_price=Decimal("100000"),
                                 current_price=Decimal("150000"), strategy="sepa_trend")
    pf.mark_daily_start()
    for position in pf.positions.values():
        position.entry_time = pf.daily_start_marked_at - timedelta(days=1)
    pf.positions["X"].strategy = "sepa_trend"
    pf.positions["Y"].strategy = "manual"
    assert pf.strategy_effective_daily_pnl is None
    pf.positions["X"].current_price = Decimal("90000")
    pf.positions["Y"].current_price = Decimal("160000")
    assert pf.manual_daily_unrealized_delta is None
    assert pf.strategy_effective_daily_pnl is None


def test_same_day_entry_before_backfilled_baseline_does_not_prove_ownership():
    """당일 진입 뒤 장중 백필이 기준선을 만들어도 태그 전환의 귀속은 미측정이다."""
    from datetime import datetime as _dt, timedelta
    pf = Portfolio(cash=Decimal("1000000"))
    pf.positions["X"] = Position(symbol="X", quantity=10, avg_price=Decimal("200000"),
                                 current_price=Decimal("100000"), strategy="manual",
                                 entry_time=_dt.now(KST) - timedelta(minutes=30))   # 오늘 진입, 기준선보다 앞
    pf.mark_daily_start()
    pf.positions["X"].strategy = "sepa_trend"
    assert pf.strategy_effective_daily_pnl is None                  # 시각만으로 소유 전환을 확정하지 않음


def test_tag_change_is_unmeasured_for_naive_and_aware_entry_times():
    """진입 시각은 호스트 로컬 Fill 또는 KST DB 복원값일 수 있다. 어느 쪽도 생성 identity를 증명하지 않는다."""
    from datetime import datetime as _dt, timedelta, timezone
    pf = Portfolio(cash=Decimal("1000000"))
    pf.positions["X"] = Position(symbol="X", quantity=10, avg_price=Decimal("200000"),
                                 current_price=Decimal("100000"), strategy="manual")
    pf.mark_daily_start()
    pf.daily_start_marked_at = _dt(2026, 10, 12, 8, 30, tzinfo=KST)        # 고정 기준선 시각
    del pf.positions["X"]

    def _rebuy(entry_time):
        pf.positions["X"] = Position(symbol="X", quantity=5, avg_price=Decimal("100000"),
                                     current_price=Decimal("90000"), strategy="sepa_trend", entry_time=entry_time)
        return pf.strategy_effective_daily_pnl

    after_utc = _dt(2026, 10, 11, 23, 45, tzinfo=timezone.utc)              # = KST 10/12 08:45 > 기준선
    assert _rebuy(after_utc) is None
    before_utc = _dt(2026, 10, 11, 23, 15, tzinfo=timezone.utc)             # = KST 10/12 08:15 < 기준선
    assert _rebuy(before_utc) is None
    # Fill과 같은 호스트 로컬 naive 값도 결과를 측정 가능으로 바꾸지 않는다.
    after_local_naive = (pf.daily_start_marked_at + timedelta(hours=1)).astimezone().replace(tzinfo=None)
    assert _rebuy(after_local_naive) is None
    before_local_naive = (pf.daily_start_marked_at - timedelta(hours=1)).astimezone().replace(tzinfo=None)
    assert _rebuy(before_local_naive) is None


def test_tag_change_without_marked_at_is_unmeasured():
    """옛 파일(기준선 시각 없음)에서 태그가 바뀐 종목은 재생성과 구별할 수 없어 None."""
    from datetime import datetime as _dt
    pf = Portfolio(cash=Decimal("1000000"))
    pf.positions["X"] = Position(symbol="X", quantity=10, avg_price=Decimal("200000"),
                                 current_price=Decimal("100000"), strategy="manual", entry_time=_dt.now(KST))
    pf.mark_daily_start()
    pf.daily_start_marked_at = None
    pf.positions["X"].strategy = "sepa_trend"
    assert pf.strategy_effective_daily_pnl is None
    pf.positions["X"].strategy = "manual"                             # 태그 불변이면 측정 가능
    assert pf.strategy_effective_daily_pnl == Decimal("0")


def test_daily_stats_roundtrip_keeps_marked_at(tmp_path):
    pf = _pf(manual_now="-5000000", manual_start="-1000000")
    assert pf.daily_start_marked_at is not None and pf.daily_start_marked_at.tzinfo is not None
    fake = SimpleNamespace(portfolio=pf, _counted_buy_order_ids=set(),
                           _DAILY_STATS_PATH=tmp_path / "engine_daily_stats.json")
    UnifiedEngine._save_daily_stats(fake)
    fresh = SimpleNamespace(portfolio=Portfolio(), _counted_buy_order_ids=set(),
                            _DAILY_STATS_PATH=fake._DAILY_STATS_PATH, _daily_stats_restored=False)
    UnifiedEngine.restore_daily_stats(fresh)
    assert fresh.portfolio.daily_start_marked_at == pf.daily_start_marked_at


@pytest.mark.parametrize("start_strategy,end_strategy", [
    ("manual", "sepa_trend"), ("sepa_trend", "manual"),
])
@pytest.mark.parametrize("known_baseline_time", [True, False])
def test_tag_change_without_entry_time_is_unmeasured(start_strategy, end_strategy, known_baseline_time):
    """Missing entry provenance cannot distinguish relabeling from same-symbol re-entry."""
    pf = Portfolio(cash=Decimal("1000000"))
    pf.positions["SYNTHETIC"] = Position(
        symbol="SYNTHETIC", quantity=10, avg_price=Decimal("200000"),
        current_price=Decimal("100000"), strategy=start_strategy, entry_time=None,
    )
    pf.mark_daily_start()
    if not known_baseline_time:
        pf.daily_start_marked_at = None
    pf.positions["SYNTHETIC"].strategy = end_strategy
    pf.positions["SYNTHETIC"].current_price = Decimal("90000")
    assert pf.effective_daily_pnl == Decimal("-100000")
    assert pf.manual_daily_unrealized_delta is None
    assert pf.strategy_effective_daily_pnl is None


@pytest.mark.parametrize("start_strategy", ["manual", "sepa_trend"])
def test_unchanged_tag_without_entry_time_preserves_measurement(start_strategy):
    pf = Portfolio(cash=Decimal("1000000"))
    pf.positions["SYNTHETIC"] = Position(
        symbol="SYNTHETIC", quantity=10, avg_price=Decimal("200000"),
        current_price=Decimal("100000"), strategy=start_strategy, entry_time=None,
    )
    pf.mark_daily_start()
    pf.positions["SYNTHETIC"].current_price = Decimal("90000")
    expected = Decimal("0") if start_strategy == "manual" else Decimal("-100000")
    assert pf.strategy_effective_daily_pnl == expected


def test_partial_fill_timestamp_backfill_cannot_prove_recreation():
    pf = Portfolio(cash=Decimal("1000000"))
    position = Position(symbol="SYNTHETIC", quantity=10, avg_price=Decimal("200000"),
                        current_price=Decimal("100000"), strategy="manual", entry_time=None)
    pf.positions[position.symbol] = position
    pf.mark_daily_start()
    # The existing holding survives a partial fill; engine.on_fill can fill this missing timestamp.
    position.quantity = 9
    position.strategy = "sepa_trend"
    position.entry_time = pf.daily_start_marked_at + timedelta(seconds=1)
    pf.daily_pnl = Decimal("-100000")
    assert pf.positions[position.symbol] is position
    assert pf.effective_daily_pnl == Decimal("0")
    assert pf.strategy_effective_daily_pnl is None


def test_unknown_ownership_keeps_account_loss_gate_blocked(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    risk = RiskManager(RiskConfig(daily_max_loss_pct=5.0), Decimal("20000000"), market="KR")
    risk.set_cash_verification(True)
    pf = _pf(manual_now="-5000000", manual_start="-1000000")
    before_allowed, before_reason = risk.can_open_position(
        "SYNTHETIC", OrderSide.BUY, 10, Decimal("70000"), pf, strategy_type="sepa_trend")
    original_effective = pf.effective_daily_pnl
    pf.positions["087010"].strategy = "sepa_trend"
    pf.positions["087010"].entry_time = pf.daily_start_marked_at + timedelta(seconds=1)
    after_allowed, after_reason = risk.can_open_position(
        "SYNTHETIC", OrderSide.BUY, 10, Decimal("70000"), pf, strategy_type="sepa_trend")
    assert not before_allowed and not after_allowed
    assert "전면 차단" in before_reason and "전면 차단" in after_reason
    assert "전략 귀속 미측정" in after_reason
    assert pf.effective_daily_pnl == original_effective


@pytest.mark.parametrize("include_manual", [False, True])
def test_nonmanual_strategy_retag_preserves_measurement(include_manual):
    pf = Portfolio(cash=Decimal("1000000"))
    pf.positions["STRATEGY"] = Position(
        symbol="STRATEGY", quantity=10, avg_price=Decimal("200000"),
        current_price=Decimal("100000"), strategy="sepa_trend",
    )
    if include_manual:
        pf.positions["MANUAL"] = Position(
            symbol="MANUAL", quantity=10, avg_price=Decimal("100000"),
            current_price=Decimal("100000"), strategy="manual",
        )
    pf.mark_daily_start()
    pf.positions["STRATEGY"].strategy = "core_holding"
    pf.positions["STRATEGY"].current_price = Decimal("90000")
    if include_manual:
        pf.positions["MANUAL"].current_price = Decimal("95000")
    expected_manual = Decimal("-50000") if include_manual else Decimal("0")
    assert pf.manual_daily_unrealized_delta == expected_manual
    assert pf.strategy_effective_daily_pnl == Decimal("-100000")


def test_one_ambiguous_symbol_makes_whole_attribution_unmeasured():
    pf = _pf(manual_now="-1000000", manual_start="-1000000",
             strat_now="-100000", strat_start="0")
    assert pf.strategy_effective_daily_pnl == Decimal("-100000")
    pf.positions["005930"].strategy = "manual"
    # The unchanged manual holding cannot make the other symbol's ownership known.
    assert pf.manual_daily_unrealized_delta is None
    assert pf.strategy_effective_daily_pnl is None
    assert pf.effective_daily_pnl == Decimal("-100000")
