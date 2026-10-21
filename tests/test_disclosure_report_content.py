import asyncio
from datetime import datetime

from src.data.providers import disclosure_feed as feed


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 22, 7, 30)


def test_digest_dates_meaning_escaping_and_missing_direction(monkeypatch):
    async def events():
        return [dict(name='<회사>', code='005930', label='유상증자', score=80,
                     rcept_dt='20261021', meaning='주식 발행 & 조달', rcept_no='20261021000001')]
    monkeypatch.setattr(feed, '_fetch_events', events)
    monkeypatch.setattr(feed, 'datetime', Clock)
    text = asyncio.run(feed.fetch_disclosure_summary())
    assert '2026-10-21' in text and '2026-10-22 07:30' in text
    assert '&lt;회사&gt;' in text and '발행 &amp; 조달' in text
    assert 'https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261021000001' in text
    assert '호재·악재 점수 아님' in text and '원문' in text


def test_invalid_and_future_events_do_not_displace_valid_rows(monkeypatch):
    async def events():
        return [None, dict(name='future', score=100, rcept_dt='20261023'),
                dict(name='nan', score=float('nan'), rcept_dt='20261021'),
                dict(name='valid', score=2, rcept_dt='20261020', label='정정')]
    monkeypatch.setattr(feed, '_fetch_events', events)
    monkeypatch.setattr(feed, 'datetime', Clock)
    text = asyncio.run(feed.fetch_disclosure_summary())
    assert 'valid' in text and 'future' not in text and 'nan' not in text
    assert '일반 설명 미제공' in text


def test_no_data_and_failure_have_distinct_meaning(monkeypatch):
    async def empty():
        return []
    async def failed():
        raise RuntimeError('offline')
    monkeypatch.setattr(feed, '_fetch_events', empty)
    assert '대상 공시 없음' in asyncio.run(feed.fetch_disclosure_summary())
    monkeypatch.setattr(feed, '_fetch_events', failed)
    assert '조회 실패' in asyncio.run(feed.fetch_disclosure_summary())
