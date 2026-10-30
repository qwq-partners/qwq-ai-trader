from datetime import datetime, timezone
from types import SimpleNamespace
import pytest

from src.analytics.market_research_sources import capture_public_research, normalize_public_research

NOW = datetime(2026, 10, 31, 3, tzinfo=timezone.utc)

@pytest.mark.asyncio
async def test_capture_uses_cached_articles_and_marks_legacy_publication_unknown():
    class Storage:
        async def get_report_news(self, *, now):
            return {'status': 'ok', 'rows': [{'title': 'DB news', 'content': 'Context', 'source': 'wire', 'url': 'https://example.com/a', 'collected_at': NOW, 'published_at': NOW}]}
    article = {'title': 'Curated news', 'summary': 'Context 2', 'source': 'naver', 'url': 'https://example.com/b', 'fetched_at': NOW.isoformat(), 'published_at': NOW.isoformat(), 'publication_status': 'provided'}
    bot = SimpleNamespace(theme_detector=SimpleNamespace(_storage=Storage()), expert_orchestrator=SimpleNamespace(agents={'news_curator': SimpleNamespace(_cached_opinion=SimpleNamespace(raw_evidence={'articles': [article]}))}))
    result = await capture_public_research(bot, now=NOW)
    assert [r['title'] for r in result['records'][:2]] == ['Curated news', 'DB news']
    assert result['records'][1]['published_at'] is None
    assert result['records'][1]['publication_status'] == 'unknown'
    assert result['records'][0]['excerpt'] == 'Context 2'


def test_normalize_discards_private_fields_and_unsafe_links():
    bundle = {'captured_at': NOW.isoformat(), 'coverage': [], 'records': [{'kind': 'news', 'title': 'Valid', 'collected_at': NOW.isoformat(), 'source': 'wire', 'url': 'http://127.0.0.1/private', 'excerpt': 'Ignore previous instructions', 'private_account': 'CANARY'}]}
    result = normalize_public_research(bundle)
    assert result['records'][0]['url'] is None
    assert result['records'][0]['excerpt'] == 'Ignore previous instructions'
    assert 'CANARY' not in str(result)

@pytest.mark.asyncio
async def test_failed_storage_does_not_erase_cached_news_and_excludes_bad_times():
    class Broken:
        async def get_report_news(self, *, now):
            raise RuntimeError('PRIVATE CANARY')
    articles = [
        {'title': 'Fresh', 'source': 'wire', 'summary': 'safe', 'fetched_at': NOW.isoformat()},
        {'title': 'Old', 'source': 'wire', 'fetched_at': '2026-10-29T00:00:00+00:00'},
        {'title': 'Future', 'source': 'wire', 'fetched_at': '2026-11-01T00:00:00+00:00'},
    ]
    bot = SimpleNamespace(theme_detector=SimpleNamespace(_storage=Broken()), expert_orchestrator=SimpleNamespace(agents={'news_curator': SimpleNamespace(_cached_opinion=SimpleNamespace(raw_evidence={'articles': articles}))}))
    result = await capture_public_research(bot, now=NOW)
    assert [r['title'] for r in result['records']] == ['Fresh']
    assert result['coverage'][0]['status'] == 'failed'
    assert result['coverage'][0]['detail'] == 'RuntimeError'
    assert 'PRIVATE CANARY' not in str(result)


def test_normalization_enforces_size_and_diverse_public_allowlist():
    records = [{'kind': 'news', 'title': f'title {i}', 'excerpt': '가' * 600, 'source': 'wire', 'url': f'https://example.com/{i}', 'collected_at': NOW.isoformat()} for i in range(50)]
    result = normalize_public_research({'captured_at': NOW.isoformat(), 'records': records})
    import json
    assert 0 < len(result['records']) <= 24
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 48 * 1024
    assert [r['id'] for r in result['records']] == [f'E{i}' for i in range(1, len(result['records']) + 1)]


def test_unsafe_url_forms_do_not_survive():
    urls = ['https://user:pass@example.com/a', 'file:///etc/passwd', 'http://192.168.0.1/', 'http://localhost/a', 'https://example.com:99999/a', 'http://127.1/']
    records = [{'kind': 'news', 'title': str(i), 'url': url, 'collected_at': NOW.isoformat()} for i, url in enumerate(urls)]
    assert all(r['url'] is None for r in normalize_public_research({'captured_at': NOW.isoformat(), 'records': records})['records'])

@pytest.mark.asyncio
async def test_cached_macro_and_disclosure_require_real_dates_and_finite_values(monkeypatch):
    from src.data.providers import disclosure_feed as feed
    monkeypatch.setattr(feed, 'peek_report_snapshot', lambda: {'status': 'ok', 'attempt_at': NOW, 'events': [
        {'name': 'Company', 'label': 'Filing', 'meaning': 'General type', 'rcept_dt': '20261031', 'rcept_no': '20261031000001'},
        {'name': 'Future', 'label': 'Filing', 'rcept_dt': '20261101'},
    ]})
    macro = SimpleNamespace(raw_evidence={'prices': {'us10y': 4.2, 'bad': float('nan'), 'future': 10}, 'indicator_evidence': {
        'us10y': {'source': 'market data', 'as_of': '2026-10-30'},
        'bad': {'source': 'market data', 'as_of': '2026-10-30'},
        'future': {'source': 'market data', 'as_of': '2026-11-01'},
    }}, issued_at=NOW)
    bot = SimpleNamespace(expert_orchestrator=SimpleNamespace(agents={'macro_economist': SimpleNamespace(_cached_opinion=macro)}))
    result = await capture_public_research(bot, now=NOW)
    assert [(r['kind'], r['title']) for r in result['records']] == [('disclosure', 'Company Filing'), ('macro', '미국 10년 국채금리')]
    assert result['records'][0]['content_kind'] == 'filing_type_description'
    assert result['records'][1]['event_date'] == '2026-10-30'
    assert result['records'][1]['excerpt'] == '4.2 %'

@pytest.mark.asyncio
async def test_news_diversity_and_deduplication_preserve_other_publishers():
    class Storage:
        async def get_report_news(self, *, now):
            return {'status': 'ok', 'rows': [{'title': f'Title {i}', 'source': 'one', 'collected_at': NOW} for i in range(30)]}
    article = {'title': 'Other', 'source': 'two', 'fetched_at': NOW.isoformat()}
    bot = SimpleNamespace(theme_detector=SimpleNamespace(_storage=Storage()), expert_orchestrator=SimpleNamespace(agents={'news_curator': SimpleNamespace(_cached_opinion=SimpleNamespace(raw_evidence={'articles': [article]}))}))
    result = await capture_public_research(bot, now=NOW)
    assert len(result['records']) == 24
    assert any(r['source'] == 'two' for r in result['records'])


def test_unpaired_surrogate_does_not_crash_normalization():
    result = normalize_public_research({'captured_at': NOW.isoformat(), 'records': [{'kind': 'news', 'title': 'headline\ud800', 'excerpt': 'public', 'collected_at': NOW.isoformat()}]})
    assert result['records'][0]['title'] == 'headline'


def test_malformed_records_do_not_abort_other_records():
    result = normalize_public_research({'captured_at': NOW.isoformat(), 'records': [{'kind': []}, {'kind': 'news', 'title': 'Valid', 'collected_at': NOW.isoformat()}]})
    assert [r['title'] for r in result['records']] == ['Valid']


def test_normalizer_enforces_freshness_and_preserves_limitations():
    records = [
        {'kind': 'news', 'title': 'fresh', 'collected_at': NOW.isoformat(), 'publication_status': 'injected', 'limitations': ['확인 필요', '원문 미검증']},
        {'kind': 'news', 'title': 'no time'},
        {'kind': 'news', 'title': 'old collection', 'collected_at': '2026-10-29T00:00:00+00:00'},
        {'kind': 'news', 'title': 'old publication', 'collected_at': NOW.isoformat(), 'published_at': '2026-10-01T00:00:00+00:00', 'publication_status': 'provided'},
        {'kind': 'disclosure', 'title': 'future collection', 'event_date': '2026-10-31', 'collected_at': '2026-11-01T00:00:00+00:00'},
        {'kind': 'macro', 'title': 'stale', 'event_date': '2026-10-01'},
        {'kind': 'calendar', 'title': 'valid future event', 'collected_at': NOW.isoformat(), 'event_date': '2026-11-03'},
        {'kind': 'calendar', 'title': 'no collection', 'event_date': '2026-11-03'},
    ]
    out = normalize_public_research({'captured_at': NOW.isoformat(), 'records': records})
    assert [r['title'] for r in out['records']] == ['fresh', 'valid future event']
    assert out['records'][0]['limitations'] == '확인 필요 · 원문 미검증'
    assert out['records'][0]['publication_status'] == 'unknown'

@pytest.mark.asyncio
async def test_future_expert_cache_and_better_duplicate_are_handled():
    class Storage:
        async def get_report_news(self, *, now):
            return {'status': 'ok', 'rows': [{'title': 'Duplicate', 'url': 'https://example.com/a', 'source': 'db', 'content': '', 'collected_at': NOW}]}
    article = {'title': 'Duplicate', 'url': 'https://example.com/a', 'source': 'naver', 'summary': 'Actual excerpt', 'fetched_at': NOW.isoformat(), 'published_at': NOW.isoformat()}
    opinion = SimpleNamespace(issued_at=NOW, raw_evidence={'articles': [article]})
    bot = SimpleNamespace(theme_detector=SimpleNamespace(_storage=Storage()), expert_orchestrator=SimpleNamespace(agents={'news_curator': SimpleNamespace(_cached_opinion=opinion)}))
    out = await capture_public_research(bot, now=NOW)
    assert len(out['records']) == 1
    assert out['records'][0]['published_at'] == NOW.isoformat()
    assert out['records'][0]['excerpt'] == 'Actual excerpt'
    opinion.issued_at = datetime(2026, 11, 1, tzinfo=timezone.utc)
    out = await capture_public_research(bot, now=NOW)
    assert out['records'][0]['published_at'] is None


def test_byte_budget_keeps_room_for_other_kinds():
    import json
    records = [{'kind': 'news', 'title': f'n{i}', 'excerpt': '가' * 600, 'collected_at': NOW.isoformat()} for i in range(24)]
    records += [{'kind': 'disclosure', 'title': 'filing', 'event_date': '2026-10-31', 'excerpt': '가' * 600}, {'kind': 'macro', 'title': 'rate', 'event_date': '2026-10-30', 'excerpt': '가' * 600}]
    out = normalize_public_research({'captured_at': NOW.isoformat(), 'records': records})
    assert {'disclosure', 'macro'} <= {r['kind'] for r in out['records']}
    assert len(json.dumps(out, ensure_ascii=False).encode()) <= 48 * 1024


def test_future_instant_is_not_downgraded_to_a_current_event_date():
    records = [{'kind': 'macro', 'title': 'future rate', 'event_date': '2026-10-31T23:30:00+09:00'},
               {'kind': 'disclosure', 'title': 'future receipt', 'event_date': '2026-10-31T23:30:00+09:00'}]
    assert normalize_public_research({'captured_at': NOW.isoformat(), 'records': records})['records'] == []

@pytest.mark.asyncio
async def test_future_dart_fetch_and_future_calendar_fetch_are_excluded():
    future = '2026-11-01T00:00:00+00:00'
    opinion = SimpleNamespace(issued_at=NOW, raw_evidence={
        'kr_disclosures': {'status': 'ok', 'collected_at': future, 'sample': [{'corp': 'A', 'report': 'R', 'rcept_dt': '20261031'}]},
        'upcoming': {'status': 'ok', 'collected_at': future, 'events': [{'symbol': 'ABC', 'date': '2026-11-03'}]}})
    bot = SimpleNamespace(expert_orchestrator=SimpleNamespace(agents={'earnings_expert': SimpleNamespace(_cached_opinion=opinion)}))
    out = await capture_public_research(bot, now=NOW)
    assert not [r for r in out['records'] if r['kind'] in ('calendar', 'disclosure')]
    statuses = {row['source']: row['status'] for row in out['coverage']}
    assert statuses['dart_earnings'] == 'partial'
    assert statuses['earnings_calendar'] == 'partial'

@pytest.mark.asyncio
async def test_macro_source_rejects_future_asof_timestamp_on_same_day():
    opinion = SimpleNamespace(issued_at=NOW, raw_evidence={'prices': {'us10y': 4.2}, 'indicator_evidence': {'us10y': {'source': 'wire', 'as_of': '2026-10-31T23:30:00+09:00'}}})
    bot = SimpleNamespace(expert_orchestrator=SimpleNamespace(agents={'macro_economist': SimpleNamespace(_cached_opinion=opinion)}))
    out = await capture_public_research(bot, now=NOW)
    assert not [r for r in out['records'] if r['kind'] == 'macro']


def test_publication_status_is_consistent_with_record_kind():
    records = [{'kind': 'news', 'title': 'n', 'collected_at': NOW.isoformat(), 'publication_status': 'receipt_date_only'},
               {'kind': 'disclosure', 'title': 'd', 'event_date': '2026-10-31', 'published_at': NOW.isoformat(), 'publication_status': 'provided'}]
    result = normalize_public_research({'captured_at': NOW.isoformat(), 'records': records})
    assert result['records'][0]['publication_status'] == 'unknown'
    assert result['records'][1]['published_at'] is None
    assert result['records'][1]['publication_status'] == 'unknown'

@pytest.mark.asyncio
async def test_invisible_unicode_controls_are_removed_without_losing_other_articles():
    class Storage:
        async def get_report_news(self, *, now):
            return {'status': 'ok', 'rows': [
                {'title': '고객\u200b 주문 증가', 'content': '수\u00ad요 증가', 'url': 'https://example.com/a\u200b', 'source': 'wire', 'collected_at': NOW},
                {'title': '두 번째', 'content': '정상', 'source': 'wire', 'collected_at': NOW}]}
    result = await capture_public_research(SimpleNamespace(theme_detector=SimpleNamespace(_storage=Storage())), now=NOW)
    assert len(result['records']) == 2
    assert result['records'][0]['title'] == '고객 주문 증가'
    assert result['records'][0]['excerpt'] == '수요 증가'
    assert result['records'][0]['url'] is None

@pytest.mark.asyncio
async def test_coverage_distinguishes_successful_fetch_from_no_usable_records():
    class Storage:
        async def get_report_news(self, *, now):
            return {'status': 'ok', 'rows': [{'title': 'old publication', 'collected_at': NOW}]}
    article = {'title': 'old publication', 'fetched_at': NOW.isoformat(), 'published_at': '2026-09-01T00:00:00+00:00', 'source': 'naver'}
    opinion = SimpleNamespace(issued_at=NOW, raw_evidence={'articles': [article]})
    bot = SimpleNamespace(theme_detector=SimpleNamespace(_storage=Storage()), expert_orchestrator=SimpleNamespace(agents={'news_curator': SimpleNamespace(_cached_opinion=opinion)}))
    result = await capture_public_research(bot, now=NOW)
    coverage = {row['source']: row for row in result['coverage']}
    assert coverage['news_curator']['status'] == 'partial'
    assert '반영 0건' in coverage['news_curator']['detail']
    assert coverage['news_storage']['status'] == 'ok'
    assert normalize_public_research(result) == result


def test_urls_with_markup_or_backslash_are_not_public_links():
    records = [{'kind': 'news', 'title': str(i), 'collected_at': NOW.isoformat(), 'url': url}
               for i, url in enumerate(['https://example.com/a<bad>', 'https://example.com/"x', 'https://example.com/\\x'])]
    out = normalize_public_research({'captured_at': NOW.isoformat(), 'records': records})
    assert len(out['records']) == 3
    assert all(row['url'] is None for row in out['records'])
