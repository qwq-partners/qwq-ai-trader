"""59차(2026-10-10) — manual 보유의 당일 변동을 뺀 '전략 귀속 일일 손익' 측정.

10/8 관측: 사용자 수동 보유 087010 이 -24.7% 로 출발해 신규 전략 신호 3건 중 2건이
일일 손실 한도(-27.6%)에 막혔다(나머지 1건은 수량 0). 게이트 판정은 계좌 전체 그대로 두고(한도 완화 없음),
거부 사유·자산 스냅샷에 전략 귀속 손익을 함께 남긴다.
"""
import json
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


def test_manual_sold_then_rebought_as_strategy_keeps_baseline_on_manual():
    """62차(Codex 인계 재검토 지적 1): 시작 manual -100만 → 사용자 매도 → 봇이 같은 종목을 전략으로 재매수(미실현 0).
    사라진 manual 몫 +100만은 manual 에, 새 전략 포지션은 전략에 — 전략 귀속은 0."""
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
    assert pf.manual_daily_unrealized_delta == Decimal("1000000")
    assert pf.strategy_effective_daily_pnl == Decimal("0")
    pf.positions["X"].current_price = Decimal("90000")           # 새 전략 포지션의 -5만은 전략 몫
    assert pf.strategy_effective_daily_pnl == Decimal("-50000")


def test_strategy_sold_then_rebought_as_manual_keeps_baseline_on_strategy():
    """반대 방향: 시작 sepa +50만 → 봇 매도(실현 +50만) → 사용자가 같은 종목 manual 재매수(미실현 0) → 전략 귀속 0."""
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
    assert pf.manual_daily_unrealized_delta == Decimal("0")
    assert pf.strategy_effective_daily_pnl == Decimal("0")
    pf.positions["Y"].current_price = Decimal("140000")           # 새 manual 포지션의 -3만은 manual 몫
    assert pf.manual_daily_unrealized_delta == Decimal("-30000")
    assert pf.strategy_effective_daily_pnl == Decimal("0")


def test_in_place_tag_change_follows_start_tag():
    """재생성 없이 태그만 바뀐 보유(entry_time 과거/None)는 시작 태그를 따른다 — 누적 미실현이 당일 손익으로 튀지 않는다."""
    pf = Portfolio(cash=Decimal("1000000"))
    pf.positions["X"] = Position(symbol="X", quantity=10, avg_price=Decimal("200000"),
                                 current_price=Decimal("100000"), strategy="manual")
    pf.positions["Y"] = Position(symbol="Y", quantity=10, avg_price=Decimal("100000"),
                                 current_price=Decimal("150000"), strategy="sepa_trend")
    pf.mark_daily_start()
    pf.positions["X"].strategy = "sepa_trend"
    pf.positions["Y"].strategy = "manual"
    assert pf.strategy_effective_daily_pnl == Decimal("0")
    pf.positions["X"].current_price = Decimal("90000")            # 시작 manual 종목의 변동 → manual 몫
    pf.positions["Y"].current_price = Decimal("160000")           # 시작 전략 종목의 변동 → 전략 몫
    assert pf.manual_daily_unrealized_delta == Decimal("-100000")
    assert pf.strategy_effective_daily_pnl == Decimal("100000")


def test_same_day_entry_then_backfill_then_tag_change_is_not_recreation():
    """재리뷰 P2: 당일 진입(entry_time 오늘) 뒤 장중 재시작 백필이 기준선을 만들고 태그만 바뀜 — 재생성이 아니다."""
    from datetime import datetime as _dt, timedelta
    pf = Portfolio(cash=Decimal("1000000"))
    pf.positions["X"] = Position(symbol="X", quantity=10, avg_price=Decimal("200000"),
                                 current_price=Decimal("100000"), strategy="manual",
                                 entry_time=_dt.now(KST) - timedelta(minutes=30))   # 오늘 진입, 기준선보다 앞
    pf.mark_daily_start()
    pf.positions["X"].strategy = "sepa_trend"
    assert pf.strategy_effective_daily_pnl == Decimal("0")          # 누적 -100만이 전략 손익으로 튀면 결함


def test_recreation_detection_is_timezone_aware():
    """재리뷰 P2: 기준선·진입 시각을 고정해 비교한다 — aware(UTC) 는 절대 시각, naive 는 호스트 로컬(Fill.timestamp 계약).
    날짜 문자열이 다른 같은 절대 시각(KST 10/12 08:45 = UTC 10/11 23:45)도 올바르게 판정해야 한다."""
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
    assert _rebuy(after_utc) == Decimal("-50000")                           # 재생성 → 새 전략 포지션 -5만
    before_utc = _dt(2026, 10, 11, 23, 15, tzinfo=timezone.utc)             # = KST 10/12 08:15 < 기준선
    assert _rebuy(before_utc) == Decimal("0")                               # 기존 보유의 태그 전환 → 시작 태그 manual 이 변동 전부
    # naive = 호스트 로컬(Fill.timestamp 와 같은 생성 경로) — UTC/KST 어느 호스트에서도 같은 절대 시각으로 비교
    after_local_naive = (pf.daily_start_marked_at + timedelta(hours=1)).astimezone().replace(tzinfo=None)
    assert _rebuy(after_local_naive) == Decimal("-50000")
    before_local_naive = (pf.daily_start_marked_at - timedelta(hours=1)).astimezone().replace(tzinfo=None)
    assert _rebuy(before_local_naive) == Decimal("0")


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
