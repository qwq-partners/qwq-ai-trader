from datetime import datetime, timezone
import pytest
from src.data.storage.news_storage import NewsStorage
from src.experts.news_curator import NewsCurator, NewsItem

@pytest.mark.asyncio
async def test_storage_does_not_connect_if_pool_is_missing():
    storage = NewsStorage('unused')
    result = await storage.get_report_news(now=datetime.now(timezone.utc))
    assert result == {'status': 'not_ready', 'rows': []}


def test_curator_preserves_bounded_summary_and_publication_time():
    item = NewsItem(title='Title', summary='a' * 700, url='https://example.com', source='naver', published_at=datetime(2026, 10, 31, tzinfo=timezone.utc))
    evidence = NewsCurator._article_evidence(item)
    assert len(evidence['summary']) == 600
    assert evidence['publication_status'] == 'provided'
    assert evidence['published_at'] == '2026-10-31T00:00:00+00:00'


def test_disclosure_snapshot_has_attempt_state_without_trading_cache(monkeypatch):
    from src.data.providers import disclosure_feed as feed
    monkeypatch.setattr(feed, '_report_status', 'empty')
    monkeypatch.setattr(feed, '_report_events', [{'name': 'old'}])
    monkeypatch.setattr(feed, '_symbol_cache', {'123456': [{'name': 'trading'}]})
    assert feed.peek_report_snapshot()['events'] == []
    assert feed._symbol_cache['123456'][0]['name'] == 'trading'


def test_news_publication_parser_rejects_naive_and_invalid_times():
    from src.experts.news_curator import _publication_time
    assert _publication_time('Fri, 30 Oct 2026 15:00:00 +0900').utcoffset().total_seconds() == 9 * 3600
    assert _publication_time(0).tzinfo is not None
    assert _publication_time('bad') is None
    assert _publication_time('Fri, 30 Oct 2026 15:00:00') is None

@pytest.mark.asyncio
async def test_dart_metadata_survives_existing_response(monkeypatch):
    from src.experts.earnings_expert import EarningsExpert
    import src.experts.earnings_expert as module
    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self):
            return {'status': '000', 'list': [{'corp_name': 'Company', 'report_nm': '분기보고서', 'rcept_dt': '20261031', 'rcept_no': '20261031000001'}]}
    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, *args, **kwargs): return Response()
    monkeypatch.setattr(module.aiohttp, 'ClientSession', Session)
    expert = object.__new__(EarningsExpert)
    expert._dart_key = 'synthetic'
    result = await expert._fetch_kr_recent_disclosures()
    assert result['status'] == 'ok'
    assert result['sample'][0] == {'corp': 'Company', 'report': '분기보고서', 'rcept_dt': '20261031', 'rcept_no': '20261031000001'}
    assert 'first page' in result['scope']

@pytest.mark.asyncio
async def test_dart_missing_configuration_is_not_an_empty_result():
    from src.experts.earnings_expert import EarningsExpert
    expert = object.__new__(EarningsExpert)
    expert._dart_key = ''
    result = await expert._fetch_kr_recent_disclosures()
    assert result['status'] == 'not_ready'

@pytest.mark.asyncio
async def test_no_individual_observation_time_is_invented_from_expert_issue_time(monkeypatch):
    from types import SimpleNamespace
    from src.analytics.market_research_sources import capture_public_research
    from src.data.providers import disclosure_feed as feed
    monkeypatch.setattr(feed, 'peek_report_snapshot', lambda: {'status': 'not_ready', 'events': []})
    now = datetime(2026, 10, 31, 3, tzinfo=timezone.utc)
    opinion = SimpleNamespace(issued_at=now, raw_evidence={'kr_disclosures': {'status': 'ok', 'sample': [{'corp': 'A', 'report': 'R', 'rcept_dt': '20261031'}]}})
    bot = SimpleNamespace(expert_orchestrator=SimpleNamespace(agents={'earnings_expert': SimpleNamespace(_cached_opinion=opinion)}))
    result = await capture_public_research(bot, now=now)
    assert result['records'][0]['observed_at'] is None
