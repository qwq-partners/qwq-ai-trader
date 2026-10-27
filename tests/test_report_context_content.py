from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import pytest

from src.analytics import report_context as context


def bot(cash=0, verified=True):
    return SimpleNamespace(
        engine=SimpleNamespace(get_available_cash=lambda: Decimal(str(cash)), broker=SimpleNamespace(is_connected=True)),
        risk_manager=SimpleNamespace(_cash_verified=verified),
    )


@pytest.fixture(autouse=True)
def recent_sync(monkeypatch):
    from src.utils import loop_heartbeat
    monkeypatch.setattr(loop_heartbeat, 'loop_status', lambda now: {
        'kr_portfolio_sync': {'last_success': now.timestamp()-60, 'consecutive_failures': 0, 'idle_reason': None}})


def test_cash_and_buy_halt_are_explicit_without_private_amounts():
    text = context.render_runtime_context(bot(), kill_status={"halt_buy": True, "halt_all": False})
    assert '신규 매수 중지' in text and '가용현금 없음' in text
    assert '보유 관리' in text and '원' not in text
    assert '정상매매' not in text


def test_unverified_or_nonfinite_cash_cannot_be_presented_as_funded():
    for sample in (bot(1000, False), bot('NaN'), SimpleNamespace()):
        text = context.render_runtime_context(sample, kill_status={})
        assert '현금 상태 미확인' in text
        assert '매수 가능' not in text


def test_weekly_claim_once_including_restart_and_iso_year(tmp_path):
    saturday = datetime(2027, 1, 2, 0, 5)
    assert context.claim_weekly_report(saturday, tmp_path)
    assert not context.claim_weekly_report(saturday, tmp_path)
    assert not context.claim_weekly_report(datetime(2027, 1, 3, 0, 5), tmp_path)
    assert context.claim_weekly_report(datetime(2027, 1, 9, 0, 5), tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ['principles-2026-W53.json', 'principles-2027-W01.json']


def test_cached_brief_requires_same_korean_day_and_nonfuture_timestamp():
    from zoneinfo import ZoneInfo
    now = datetime(2026, 10, 22, 7, 30, tzinfo=ZoneInfo('Asia/Seoul'))
    base = {'text': 'brief', 'kr_date': '2026-10-22', 'generated_at': '2026-10-21T22:00:00+00:00'}
    assert context.current_morning_brief(base, now) == 'brief'
    assert context.current_morning_brief({**base, 'kr_date': '2026-10-21'}, now) is None
    assert context.current_morning_brief({**base, 'generated_at': '2026-10-22T07:40:00+09:00'}, now) is None
    assert context.current_morning_brief({**base, 'generated_at': '2026-10-22T00:00:00'}, now) is None


def test_pending_report_has_condition_window_and_escaped_basis():
    pending = [SimpleNamespace(name='<회사>', symbol='005930', strategy='sepa_trend', score=80,
        entry_price=50000, max_entry_price=51000, target_price=55000, stop_price=48000,
        entry_mode='breakout', breakout_trigger=50500, reason='거래량 & 돌파',
        created_at='2026-10-22T08:20:00', expires_at='2026-10-22T15:00:00')]
    text = context.render_pending_watchlist(pending, now=datetime(2026, 10, 22, 8, 25))
    assert '&lt;회사&gt;' in text and '거래량 &amp; 돌파' in text
    assert '50,500' in text and '51,000' in text and '15:00' in text
    assert '주문 승인 아님' in text and '생성' in text
    assert '체결' in text


def test_empty_watchlist_is_not_no_market_opportunity():
    text = context.render_pending_watchlist([], now=datetime(2026, 10, 22, 8, 25))
    assert '현재 대기 후보 0개' in text and '시장 전체에 기회가 없다는 뜻은 아님' in text


def test_cash_getter_failure_does_not_suppress_report():
    def failed():
        raise RuntimeError("unavailable")
    sample = bot()
    sample.engine.get_available_cash = failed
    text = context.render_runtime_context(sample, kill_status={"halt_buy": True})
    assert "현금 상태 미확인" in text and "신규 매수 중지" in text
    assert "<b>" not in text
