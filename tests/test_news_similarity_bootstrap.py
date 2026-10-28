"""Offline reproduction of similarity bootstrap and downstream contracts."""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

import src.signals.sentiment.kr_theme_detector as module
from src.signals.sentiment.kr_theme_detector import NewsArticle, NewsCollector, ThemeDetector, ThemeInfo


def article(url='synthetic-a', title='alpha semiconductor manufacturing demand increases'):
    return NewsArticle(title=title, content='synthetic production capacity and advanced memory supply', url=url,
                       published_at=datetime.now())


def seed(text='unrelated ocean fisheries harvest market prices', when=None):
    return {'title': text, 'content': '', 'text': text, 'published_at': when or datetime.now()}


@pytest.fixture
def collector(monkeypatch):
    assert module.SKLEARN_AVAILABLE
    c = NewsCollector(client_id='synthetic', client_secret='synthetic')
    c.search_news = AsyncMock(return_value=[])
    c.fetch_daum_finance_news = AsyncMock(return_value=[])
    c.fetch_mk_rss = AsyncMock(return_value=[])
    c.fetch_hankyung_rss = AsyncMock(return_value=[])
    c.fetch_stockplus_news = AsyncMock(return_value=[])
    monkeypatch.setattr(module.asyncio, 'sleep', AsyncMock())
    return c


def market_news(c, articles):
    async def search(query, **kwargs):
        return list(articles) if query == '증시' else []
    c.search_news.side_effect = search


@pytest.mark.asyncio
async def test_first_accepted_article_seeds_empty_cache(collector):
    a = article()
    market_news(collector, [a])
    assert await collector.get_market_news() == [a]
    assert len(collector._similarity_cache) == 1
    assert collector._similarity_cache[0]['text'] == a.text


@pytest.mark.asyncio
async def test_same_content_different_url_is_deduped_after_first_collect(collector):
    a = article()
    market_news(collector, [a])
    assert await collector.get_market_news() == [a]
    market_news(collector, [article(url='synthetic-other')])
    assert await collector.get_market_news() == []


@pytest.mark.asyncio
async def test_new_unrelated_article_is_accepted_from_seeded_cache(collector):
    collector._similarity_cache = [seed()]
    a = article()
    market_news(collector, [a])
    assert await collector.get_market_news() == [a]
    assert len(collector._similarity_cache) == 2


@pytest.mark.asyncio
async def test_full_ttl_expiry_reseeds_and_deduplicates(collector):
    collector._similarity_cache = [seed(when=datetime.now() - timedelta(hours=3))]
    a = article()
    market_news(collector, [a])
    assert await collector.get_market_news() == [a]
    assert len(collector._similarity_cache) == 1
    market_news(collector, [article(url='synthetic-other')])
    assert await collector.get_market_news() == []


def detector(monkeypatch, collector, llm):
    monkeypatch.setattr(module, 'NewsCollector', lambda: collector)
    d = ThemeDetector(llm_manager=llm)
    d._storage_initialized = True
    return d


@pytest.mark.asyncio
async def test_failed_llm_still_consumes_article_in_seeded_similarity_cache(monkeypatch, collector):
    collector._similarity_cache = [seed()]
    market_news(collector, [article()])
    llm = type('FakeLLM', (), {})()
    llm.complete_json = AsyncMock(return_value={'error': 'synthetic failure'})
    d = detector(monkeypatch, collector, llm)
    assert await d.detect_themes(force=True) == []
    assert llm.complete_json.await_count == 1
    assert len(collector._similarity_cache) == 2
    market_news(collector, [article(url='synthetic-other')])
    assert await d.detect_themes(force=True) == []
    assert llm.complete_json.await_count == 1


@pytest.mark.asyncio
async def test_empty_news_skips_adjustments_and_returns_only_fresh_themes(monkeypatch, collector):
    llm = type('FakeLLM', (), {})()
    llm.complete_json = AsyncMock()
    d = detector(monkeypatch, collector, llm)
    old = datetime.now() - timedelta(hours=2)
    stale = ThemeInfo(name='old', last_updated=old)
    d._themes = {'old': stale}
    d._stock_sentiments = {'123456': {'updated_at': old}}
    d._adjust_scores_by_sector = AsyncMock()
    d._adjust_scores_by_us_market = AsyncMock()
    returned = await d.detect_themes(force=True)
    assert returned == []
    assert d._themes == {} and d._stock_sentiments == {}
    d._adjust_scores_by_sector.assert_not_awaited()
    d._adjust_scores_by_us_market.assert_not_awaited()
    llm.complete_json.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['interval', 'empty_news', 'empty_result', 'exception', 'normal'])
async def test_all_return_paths_prune_stale_before_return(monkeypatch, collector, path):
    llm = type('FakeLLM', (), {})()
    llm.complete_json = AsyncMock(return_value={'themes': [], 'stock_impacts': []})
    d = detector(monkeypatch, collector, llm)
    now = datetime.now()
    fresh = ThemeInfo(name='fresh', last_updated=now)
    stale = ThemeInfo(name='stale', last_updated=now - timedelta(hours=2))
    d._themes = {'fresh': fresh, 'stale': stale}
    d._stock_sentiments = {'fresh': {'updated_at': now}, 'stale': {'updated_at': now - timedelta(hours=2)}}
    d._adjust_scores_by_sector = AsyncMock()
    d._adjust_scores_by_us_market = AsyncMock()
    force = path != 'interval'
    if path == 'interval':
        d._last_detection = now
    elif path != 'empty_news':
        market_news(collector, [article()])
    if path == 'exception':
        llm.complete_json.side_effect = RuntimeError('synthetic')
    if path == 'normal':
        llm.complete_json.return_value = {'themes': [{'theme': '로봇', 'score': 55}], 'stock_impacts': []}
    result = await d.detect_themes(force=force)
    assert 'stale' not in {item.name for item in result}
    assert 'fresh' in {item.name for item in result}
    assert 'stale' not in d._themes and 'stale' not in d._stock_sentiments
    assert 'fresh' in d._themes and 'fresh' in d._stock_sentiments
    assert d._adjust_scores_by_sector.await_count == (1 if path == 'normal' else 0)
    assert d._adjust_scores_by_us_market.await_count == (1 if path == 'normal' else 0)


@pytest.mark.asyncio
async def test_one_hour_boundary_preserves_existing_asymmetric_rules(monkeypatch, collector):
    fixed = datetime(2026, 10, 29, 12, 0)
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed
    monkeypatch.setattr(module, 'datetime', FrozenDateTime)
    d = detector(monkeypatch, collector, type('FakeLLM', (), {})())
    cutoff = fixed - timedelta(hours=1)
    d._themes = {name: ThemeInfo(name=name, last_updated=time) for name, time in (
        ('old', cutoff - timedelta(microseconds=1)), ('edge', cutoff), ('fresh', cutoff + timedelta(microseconds=1)))}
    d._stock_sentiments = {name: {'updated_at': time} for name, time in (
        ('old', cutoff - timedelta(microseconds=1)), ('edge', cutoff), ('fresh', cutoff + timedelta(microseconds=1)))}
    result = await d.detect_themes(force=True)
    assert {theme.name for theme in result} == {'fresh'}
    assert set(d._stock_sentiments) == {'edge', 'fresh'}


@pytest.mark.asyncio
async def test_cold_cache_suppresses_repeated_a_but_accepts_new_b(collector):
    a = article()
    market_news(collector, [a])
    assert await collector.get_market_news() == [a]
    market_news(collector, [article(url='second-url')])
    assert await collector.get_market_news() == []
    b = NewsArticle(title='ocean fisheries harvest market prices', content='anchovy catch abundance',
                    url='synthetic-b', published_at=datetime.now())
    market_news(collector, [b])
    assert await collector.get_market_news() == [b]


def test_disabled_sklearn_and_short_text_do_not_seed(monkeypatch, collector):
    monkeypatch.setattr(module, 'SKLEARN_AVAILABLE', False)
    assert collector._is_similar_to_existing(article()) is False
    assert collector._similarity_cache == []
    monkeypatch.setattr(module, 'SKLEARN_AVAILABLE', True)
    assert collector._is_similar_to_existing(NewsArticle(title='short')) is False
    assert collector._similarity_cache == []


def test_vectorizer_exception_keeps_prior_cache_and_accepts(monkeypatch, collector):
    original = seed()
    collector._similarity_cache = [original]
    def fail(**kwargs):
        raise ValueError('synthetic vectorization failure')
    monkeypatch.setattr(module, 'TfidfVectorizer', fail)
    assert collector._is_similar_to_existing(article()) is False
    assert collector._similarity_cache == [original]


def test_nonduplicate_insertion_caps_cache_at_500(collector):
    collector._similarity_cache = [seed(text=f'ocean fisheries harvest market identifier{i}') for i in range(500)]
    a = article()
    assert collector._is_similar_to_existing(a) is False
    assert len(collector._similarity_cache) == 500
    assert collector._similarity_cache[0]['text'].endswith('identifier1')
    assert collector._similarity_cache[-1]['text'] == a.text


@pytest.mark.asyncio
async def test_ttl_exact_boundary_retained_then_expired(monkeypatch, collector):
    fixed = datetime(2026, 10, 29, 12, 0)
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed
    monkeypatch.setattr(module, 'datetime', FrozenDateTime)
    a = article()
    a.published_at = fixed - timedelta(hours=2)
    collector._similarity_cache = [seed(text=a.text, when=a.published_at)]
    market_news(collector, [a])
    assert await collector.get_market_news() == []
    collector._similarity_cache[0]['published_at'] -= timedelta(microseconds=1)
    assert await collector.get_market_news() == [a]
    assert len(collector._similarity_cache) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('with_fresh', [False, True])
async def test_real_scheduler_emits_only_fresh_events(monkeypatch, collector, with_fresh):
    from types import SimpleNamespace
    from src.core.event import ThemeEvent, NewsEvent
    from src.schedulers.kr_scheduler import KRScheduler
    llm = type('FakeLLM', (), {})()
    d = detector(monkeypatch, collector, llm)
    now = datetime.now()
    d._themes = {'old': ThemeInfo(name='old', last_updated=now - timedelta(hours=2))}
    d._stock_sentiments = {'000001': {'updated_at': now - timedelta(hours=2), 'impact': 8}}
    if with_fresh:
        d._themes['fresh'] = ThemeInfo(name='fresh', last_updated=now, score=80)
        d._stock_sentiments['000002'] = {'updated_at': now, 'impact': 7, 'reason': 'synthetic'}
    engine = SimpleNamespace(emit=AsyncMock())
    config = SimpleNamespace(get=lambda *args: {})
    bot = SimpleNamespace(theme_detector=d, engine=engine, config=config, running=True)
    schedule = KRScheduler(bot)
    schedule._trim_watch_symbols = lambda: None
    async def stop(_):
        bot.running = False
    monkeypatch.setattr(module.asyncio, 'sleep', stop)
    await schedule.run_theme_detection()
    events = [call.args[0] for call in engine.emit.await_args_list]
    if with_fresh:
        assert len(events) == 2
        assert isinstance(events[0], ThemeEvent) and events[0].name == 'fresh'
        assert isinstance(events[1], NewsEvent) and events[1].symbols == ['000002']
    else:
        assert events == []


@pytest.mark.parametrize('already_seeded', [False, True])
def test_empty_vocabulary_does_not_seed_cache(collector, already_seeded):
    # Default TF-IDF rejects single-character tokens even though length >= 10.
    if already_seeded:
        collector._similarity_cache = [seed(text='a b c d e f')]
    original = list(collector._similarity_cache)
    assert collector._is_similar_to_existing(NewsArticle(title='a b c d e f')) is False
    assert collector._similarity_cache == original


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['returned_error', 'exception'])
async def test_cold_failure_consumes_article_and_preserves_storage_order(monkeypatch, collector, failure):
    llm = type('FakeLLM', (), {})()
    llm.complete_json = AsyncMock(return_value={'error': 'synthetic'})
    if failure == 'exception':
        llm.complete_json.side_effect = RuntimeError('synthetic')
    d = detector(monkeypatch, collector, llm)
    storage = type('FakeStorage', (), {})()
    storage.save_news_batch = AsyncMock(return_value=1)
    d._storage = storage
    market_news(collector, [article()])
    assert await d.detect_themes(force=True) == []
    assert storage.save_news_batch.await_count == (1 if failure == 'returned_error' else 0)
    assert len(collector._similarity_cache) == 1
    market_news(collector, [article(url='another-url')])
    assert await d.detect_themes(force=True) == []
    assert llm.complete_json.await_count == 1


@pytest.mark.asyncio
async def test_a_duplicate_then_b_drives_llm_and_success_side_effect_order(monkeypatch, collector):
    calls = []
    llm = type('FakeLLM', (), {})()
    async def complete(prompt, **kwargs):
        calls.append('llm')
        return {'themes': [{'theme': '로봇', 'score': 70}], 'stock_impacts': []}
    llm.complete_json = AsyncMock(side_effect=complete)
    d = detector(monkeypatch, collector, llm)
    storage = type('FakeStorage', (), {})()
    async def save_news(rows):
        calls.append('news')
        return len(rows)
    async def save_themes(rows):
        calls.append('themes')
    async def sector():
        calls.append('sector')
    async def us():
        calls.append('us')
    storage.save_news_batch = AsyncMock(side_effect=save_news)
    storage.save_themes_batch = AsyncMock(side_effect=save_themes)
    d._storage = storage
    d._adjust_scores_by_sector = AsyncMock(side_effect=sector)
    d._adjust_scores_by_us_market = AsyncMock(side_effect=us)
    market_news(collector, [article()])
    assert [t.name for t in await d.detect_themes(force=True)] == ['로봇']
    market_news(collector, [article(url='another-url')])
    assert [t.name for t in await d.detect_themes(force=True)] == ['로봇']
    b = NewsArticle(title='ocean fisheries harvest market prices', content='anchovy catch abundance', url='b')
    market_news(collector, [b])
    assert [t.name for t in await d.detect_themes(force=True)] == ['로봇']
    assert llm.complete_json.await_count == 2
    assert calls == ['llm', 'news', 'sector', 'us', 'themes'] * 2


@pytest.mark.asyncio
async def test_cancellation_propagates_and_finally_prunes(monkeypatch, collector):
    import asyncio
    d = detector(monkeypatch, collector, type('FakeLLM', (), {})())
    d._themes = {'old': ThemeInfo(name='old', last_updated=datetime.now() - timedelta(hours=2))}
    d._stock_sentiments = {'old': {'updated_at': datetime.now() - timedelta(hours=2)}}
    collector.get_market_news = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await d.detect_themes(force=True)
    assert d._themes == {} and d._stock_sentiments == {}


@pytest.mark.asyncio
async def test_collection_exception_returns_only_fresh_state(monkeypatch, collector):
    d = detector(monkeypatch, collector, type('FakeLLM', (), {})())
    d._themes = {'old': ThemeInfo(name='old', last_updated=datetime.now() - timedelta(hours=2)),
                 'fresh': ThemeInfo(name='fresh')}
    collector.get_market_news = AsyncMock(side_effect=RuntimeError('synthetic'))
    assert [theme.name for theme in await d.detect_themes(force=True)] == ['fresh']
    assert set(d._themes) == {'fresh'}


@pytest.mark.asyncio
async def test_same_collection_similar_content_different_urls_is_deduped(collector):
    a = article()
    market_news(collector, [a, article(url='another-url')])
    assert await collector.get_market_news() == [a]


@pytest.mark.asyncio
async def test_normal_return_rechecks_expiry_after_theme_storage_await(monkeypatch, collector):
    base = datetime(2026, 10, 29, 12, 0)
    current = [base]
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0]
    monkeypatch.setattr(module, 'datetime', FrozenDateTime)
    llm = type('FakeLLM', (), {})()
    llm.complete_json = AsyncMock(return_value={'themes': [{'theme': '로봇', 'score': 70}], 'stock_impacts': []})
    d = detector(monkeypatch, collector, llm)
    d._themes = {'aging': ThemeInfo(name='aging', last_updated=base - timedelta(minutes=59, seconds=59))}
    d._adjust_scores_by_sector = AsyncMock()
    d._adjust_scores_by_us_market = AsyncMock()
    storage = type('FakeStorage', (), {})()
    storage.save_news_batch = AsyncMock(return_value=1)
    async def save_themes(rows):
        current[0] = base + timedelta(seconds=2)
    storage.save_themes_batch = AsyncMock(side_effect=save_themes)
    d._storage = storage
    market_news(collector, [article()])
    result = await d.detect_themes(force=True)
    assert 'aging' not in {theme.name for theme in result}
    assert 'aging' not in d._themes
