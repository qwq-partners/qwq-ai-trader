"""Web page is a pure view of stored, already-sent report rows; no new IO."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.analytics import market_report_html as web
from src.analytics.report_delivery import ReportStore

KST = ZoneInfo('Asia/Seoul')
NOW = datetime(2026, 10, 30, 16, 40, 12, tzinfo=KST)
DAY = '2026-10-30'

# Exact body shape produced by audience_reports on 2026-10-30 (after slot).
AFTER_BODY = (
    '🇰🇷 <b>한국증시 마감점검</b>\n10/30 16:40 KST\n\n<b>시장 판단</b>\n'
    '시점이 확인된 지수 참고 가격입니다. 전일 대비 등락·시장 방향 판단자료는 미확보입니다.\n\n'
    '<b>주요 지표 · 국내 지수 분 단위 참고 관측</b>\n• KOSPI 5,730.74 · 10/30 15:32 KST\n• KOSDAQ 724.38 · 10/30 15:32 KST\n'
    '확정 종가 아님 · 해당 분 관측값이며 봉 완료 여부 미확인\n\n'
    '<b>업종 흐름 · 한국 상장 섹터 ETF 2026-10-30</b>\n확인 10개 중 최근 1세션 등락 (전체 업종 순위 아님)\n'
    '• 은행 +0.42%\n• 보험 +0.13%\n• 자동차 -4.60%\n• 반도체 -6.49%\n\n'
    '<b>국내 관전 포인트</b>\n• 확정 종가와 전일 종가를 대조한 뒤 방향을 판단합니다.\n• ETF 은행·반도체의 상대 순위 유지·역전 여부를 확인합니다.\n\n'
    '자료 범위: 표시된 관측 표본; 시장 전체 종목 수·당일 누적 수급은 미확보.\n출처: KIS')

RESEARCH_BODY = (
    '🇰🇷 <b>한국증시 모닝브리프</b>\n10/30 07:40 KST\n\n<b>시장 판단</b>\n'
    'LLM 해석: 서버 수요 확대의 수혜와 투자 비용 부담을 함께 판단해야 한다.\n판단 근거: E1, E2\n\n'
    '<b>핵심 이슈와 영향</b>\n• 1. 서버 공급 확대의 이익 전환 여부\n사건: 가온반도체가 공급 확대를 밝혔다.\n'
    '영향: 매출에 긍정적이다.\n반대: 비용 부담이 함께 확인됐다.\n관찰: 영업이익률 개선 여부를 확인한다.\n근거: E1, E2\n\n'
    '<b>주요 지표 · 미국 2026-10-29</b>\n• S&amp;P500 +0.52%\n• 나스닥 -0.10%\nSOX−나스닥: +1.20%p\n\n'
    '<b>근거 / 출처</b>\n수집 범위: 뉴스 저장소 확보 / 공시 색인 미확보\n'
    '• E1 · 한빛뉴스 · 가온반도체 서버용 제품 공급 확대 <script>x</script> · 발행 표기 2026/10/28 15:00 KST · 수집 2026/10/29 07:20 KST · 기사 발췌\n'
    '원문: https://example.com/research/server-supply\n'
    '• E2 · 한빛뉴스 · 가온반도체 설비 투자 비용 증가 · 발행시각 미확인\n원문: javascript:alert(1)\n\n'
    '자료 범위: 표시된 관측 표본.\n출처: KIS, Yahoo Finance')

ANALYSIS = {'thesis': {'text': '서버 수요 확대의 수혜와 투자 비용 부담을 함께 판단해야 한다.', 'evidence_ids': ['E1', 'E2']},
            'issues': [{'title': '서버 공급 확대의 이익 전환 여부', 'event': '가온반도체가 공급 확대를 밝혔다.',
                        'impact': '매출에 긍정적이다.', 'counterpoint': '비용 부담이 함께 확인됐다.',
                        'watch': '영업이익률 개선 여부를 확인한다.', 'evidence_ids': ['E1', 'E2']}]}


def test_factual_day_page_renders_cards_bars_and_skipped_slot():
    page = web.render_day_page(DAY, [
        {'kind': 'morning', 'status': 'accepted', 'body': AFTER_BODY.replace('마감점검', '모닝브리프'), 'analysis': None},
        {'kind': 'midday', 'status': 'skipped_missing_current_market_basis', 'body': None, 'analysis': None},
        {'kind': 'after', 'status': 'accepted', 'body': AFTER_BODY, 'analysis': None},
    ], generated_at=NOW, neighbours=('2026-10-29', None), base='https://qwq.ai.kr')
    assert '<article class="slot default" id="after">' in page and page.count('slot default') == 1
    assert '5,730.74' in page and '확정 종가 아님' in page and '전일 대비 미확보' in page
    assert 'class="f down" style="width:50.0%"' in page           # 반도체 -6.49 scales to the full half
    assert 'class="v up">+0.42%' in page and 'class="v down">-6.49%' in page
    assert '<td>2</td><td>ETF 은행·반도체' in page and '<td>3</td>' not in page   # trailer lines are notes, not conditions
    assert '자료 범위: 표시된 관측 표본' in page
    assert '원천 시점이 확인된 장중 지수 자료가 없어' in page and '장중 13:10 보류' in page
    assert '<b>' not in page.split('<main')[1].split('</main>')[0].replace('<br>', '')  # Telegram tags never leak
    assert 'href="https://qwq.ai.kr/reports/2026-10-29"' in page and 'noindex' in page
    assert '생성 2026/10/30 16:40:12 KST' in page and '투자 권유가 아니며' in page


def test_research_slot_uses_frozen_analysis_and_escapes_untrusted_text():
    page = web.render_day_page(DAY, [{'kind': 'morning', 'status': 'accepted', 'body': RESEARCH_BODY, 'analysis': ANALYSIS}],
                               generated_at=NOW, base='https://qwq.ai.kr')
    assert '<p class="thesis">서버 수요 확대의 수혜와' in page
    assert '<dt class="c">반대</dt><dd>비용 부담이 함께 확인됐다.</dd>' in page
    assert '<a class="badge" href="#morning-E1">E1</a>' in page and 'id="morning-E1"' in page
    assert '<script>' not in page and '&lt;script&gt;x&lt;/script&gt;' in page
    assert 'href="https://example.com/research/server-supply"' in page and 'javascript:' not in page
    assert 'S&amp;P500' in page and 'class="v up">+0.52%' in page
    assert '<span class="badge warn">공시 색인 미확보</span>' in page and '<span class="badge">뉴스 저장소 확보</span>' in page


def test_with_web_link_respects_body_limits(monkeypatch):
    monkeypatch.setenv('REPORT_WEB_BASE_URL', 'https://qwq.ai.kr/')
    linked = web.with_web_link(AFTER_BODY, DAY, 'after')
    assert linked.endswith('\n\n📄 전문·근거 보기: https://qwq.ai.kr/reports/2026-10-30#after')
    tall = '\n'.join(['x'] * 59)
    assert web.with_web_link(tall, DAY, 'after') == tall                   # 60-line ceiling binds
    monkeypatch.setenv('REPORT_WEB_BASE_URL', '')
    assert web.with_web_link(AFTER_BODY, DAY, 'after') == AFTER_BODY       # disabled


@pytest.mark.parametrize('name, expected', [('', 'index.html'), ('index', 'index.html'), ('2026-10-30', '2026-10-30.html'),
                                            ('2026-10-30.html', '2026-10-30.html'), ('../etc/passwd', None),
                                            ('2026-10-30.html.bak', None), ('index.html', 'index.html')])
def test_report_file_name_allowlist(name, expected):
    assert web.report_file_name(name) == expected


def test_write_web_pages_from_store_is_idempotent_and_atomic(tmp_path, monkeypatch):
    monkeypatch.setenv('REPORT_WEB_BASE_URL', 'https://qwq.ai.kr')
    store = ReportStore(tmp_path / 'r.sqlite3')
    assert store.claim('market', 'morning', DAY, 'snap-m', RESEARCH_BODY)
    store.finish('market', 'morning', DAY, 'accepted')
    assert store.claim('market', 'midday', DAY, 'snap-d')
    store.finish('market', 'midday', DAY, 'skipped_missing_current_market_basis')
    assert store.claim('market', 'after', DAY, 'snap-a', AFTER_BODY)
    store.finish('market', 'after', DAY, 'accepted')
    assert store.claim('market', 'morning', '2026-10-31', 'h', None) and store.claim('market', 'after', '2026-10-31', 'h2', None)
    store.finish('market', 'morning', '2026-10-31', 'skipped_holiday'); store.finish('market', 'after', '2026-10-31', 'skipped_holiday')
    html_dir = tmp_path / 'html'
    target = web.write_web_pages(store, DAY, html_dir=html_dir, now=NOW)
    assert target == html_dir / '2026-10-30.html' and (html_dir / 'index.html').exists()
    assert '/reports/2026-10-31' not in target.read_text()   # holiday-only neighbour has no page
    assert not list(html_dir.glob('.tmp-*'))
    page = target.read_text()
    assert 'id="after"' in page and 'E1' in page and '장중 13:10 보류' in page
    index = (html_dir / 'index.html').read_text()
    assert '2026년 10월 30일' in index and '2026년 10월 31일' not in index   # holiday-only day hidden
    assert '장전 API 수락' in index and '장중 자료 보류' in index
    assert web.write_web_pages(store, DAY, html_dir=html_dir, now=NOW).read_text() == page
    monkeypatch.setenv('REPORT_WEB_BASE_URL', '')
    assert web.write_web_pages(store, DAY, html_dir=tmp_path / 'none', now=NOW) is None
    assert not (tmp_path / 'none').exists()


def test_market_days_newest_first_with_statuses(tmp_path):
    store = ReportStore(tmp_path / 'r.sqlite3')
    for day, kind, status in [('2026-10-29', 'after', 'accepted'), ('2026-10-30', 'morning', 'accepted'), ('2026-10-30', 'after', 'rejected')]:
        assert store.claim('market', kind, day, day + kind, 'body')
        store.finish('market', kind, day, status)
    assert store.claim('engine', 'daily', '2026-10-30', 'e', 'private')
    days = store.market_days()
    assert [d['report_date'] for d in days] == ['2026-10-30', '2026-10-29']
    assert days[0]['statuses'] == {'morning': 'accepted', 'after': 'rejected'}
    assert store.market_days(limit=1) == days[:1]


def test_fact_tags_unsafe_urls_and_own_link_line_follow_review_fixes():
    body = RESEARCH_BODY.replace('• S&amp;P500 +0.52%', '• S&amp;P500 +0.52% [F1]') \
        .replace('원문: https://example.com/research/server-supply', "원문: https://ex.com/a'b") \
        + '\n\n📄 전문·근거 보기: https://qwq.ai.kr/reports/2026-10-30#morning'
    body = body.replace('<b>업종 흐름', '<b>업종 흐름') if '업종 흐름' in body else body
    page = web.render_day_page(DAY, [{'kind': 'morning', 'status': 'accepted', 'body': body, 'analysis': ANALYSIS}],
                               generated_at=NOW, base='https://qwq.ai.kr')
    assert 'class="v up">+0.52%' in page and '<span class="tag">F1</span>' in page     # tagged fact still a card
    assert "원문 https://ex.com/a&#x27;b" in page and 'href="https://ex.com' not in page  # text, never an href
    assert '전문·근거 보기' not in page                                                  # no self link
    sectors = web._sectors({'heading': '업종 흐름 · x', 'lines': ['• 은행 +0.42% [F3]']})
    assert 'class="f up"' in sectors and '<span class="tag">F3</span>' in sectors


def test_unsent_stored_body_is_labelled_and_never_the_default_tab():
    page = web.render_day_page(DAY, [
        {'kind': 'morning', 'status': 'accepted', 'body': AFTER_BODY, 'analysis': None},
        {'kind': 'after', 'status': 'rejected', 'body': AFTER_BODY, 'analysis': None},
    ], generated_at=NOW, base='https://qwq.ai.kr')
    assert '<article class="slot default" id="morning">' in page
    assert '거절 · 미발송 저장 본문' in page
    only_rejected = web.render_day_page(DAY, [{'kind': 'after', 'status': 'unknown', 'body': AFTER_BODY, 'analysis': None}],
                                        generated_at=NOW, base='https://qwq.ai.kr')
    assert '<article class="slot default" id="after">' in only_rejected and '불명 · 미발송 저장 본문' in only_rejected


def test_base_url_shape_and_character_limit(monkeypatch):
    for bad in ('http://qwq.ai.kr', 'https://qwq.ai.kr/<b>', 'https://a b', 'https://qwq.ai.kr/?x=1&amp;y'):
        monkeypatch.setenv('REPORT_WEB_BASE_URL', bad)
        assert web.web_base_url() == '' and web.with_web_link(AFTER_BODY, DAY, 'after') == AFTER_BODY
    monkeypatch.setenv('REPORT_WEB_BASE_URL', 'https://qwq.ai.kr:8443/site')
    assert web.day_url(DAY, 'after') == 'https://qwq.ai.kr:8443/site/reports/2026-10-30#after'
    wide = '가' * 2960                                                   # 2960 + link line > 3000 chars
    assert web.with_web_link(wide, DAY, 'after') == wide
    assert web.with_web_link('가' * 2900, DAY, 'after') != '가' * 2900


@pytest.mark.asyncio
async def test_scheduler_refresh_failure_and_skip_paths_do_not_change_delivery(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from datetime import datetime as dt
    from src.analytics.audience_reports import MarketSnapshot
    from src.schedulers import report_scheduler
    from src.schedulers.report_scheduler import ReportSchedule
    monkeypatch.setenv('REPORT_WEB_BASE_URL', 'https://qwq.ai.kr')
    store = ReportStore(tmp_path / 'r.sqlite3')
    now = dt(2026, 10, 30, 13, 12, tzinfo=KST)
    notifier = AsyncMock()
    # Midday with no usable basis → skipped, and the page is written for that day.
    assert await ReportSchedule(store, notifier, clock=lambda: now).tick(now, True, MarketSnapshot(), '') == 'skipped_missing_current_market_basis'
    page = (tmp_path / 'html' / '2026-10-30.html').read_text()
    assert '장중 13:10 보류' in page
    notifier.publish_report.assert_not_awaited()
    # Page writer explodes → delivery result and slot status unchanged.
    monkeypatch.setattr(report_scheduler, 'write_web_pages', lambda *a, **k: (_ for _ in ()).throw(OSError('disk')))
    after = dt(2026, 10, 30, 16, 42, tzinfo=KST)
    notifier.publish_report = AsyncMock(return_value='accepted')
    market = MarketSnapshot(sectors=({'name': '은행', 'r1': 0.42, 'as_of': '2026-10-30', 'source': 'KIS'},
                                     {'name': '반도체', 'r1': -6.49, 'as_of': '2026-10-30', 'source': 'KIS'}), kr_basis_date='2026-10-30')
    assert await ReportSchedule(store, notifier, clock=lambda: after).tick(after, True, market, '') == 'accepted'
    sent = notifier.publish_report.await_args.args[4]
    assert sent.endswith('https://qwq.ai.kr/reports/2026-10-30#after')


@pytest.mark.asyncio
async def test_reports_route_serves_only_allowlisted_generated_files(tmp_path, monkeypatch):
    from aiohttp import web as aioweb
    from aiohttp.test_utils import TestClient, TestServer
    from src.dashboard import server as dashboard
    html_dir = tmp_path / 'html'; html_dir.mkdir()
    (html_dir / '2026-10-30.html').write_text('<!DOCTYPE html><p>day</p>')
    (html_dir / 'index.html').write_text('<!DOCTYPE html><p>index</p>')
    (html_dir / 'secret.txt').write_text('nope')
    monkeypatch.setattr('src.analytics.market_report_html.default_html_dir', lambda: html_dir)
    app = aioweb.Application()
    handler = dashboard.DashboardServer._serve_report
    app.router.add_get('/reports', lambda r: handler(None, r))
    app.router.add_get('/reports/', lambda r: handler(None, r))
    app.router.add_get('/reports/{name}', lambda r: handler(None, r))
    async with TestClient(TestServer(app)) as client:
        for path, text in (('/reports', 'index'), ('/reports/', 'index'), ('/reports/index', 'index'),
                           ('/reports/2026-10-30', 'day'), ('/reports/2026-10-30.html', 'day')):
            response = await client.get(path)
            assert response.status == 200 and response.content_type == 'text/html' and text in await response.text()
            assert response.headers['Cache-Control'] == 'no-cache'
        for path in ('/reports/secret.txt', '/reports/2026-10-31', '/reports/..%2Freports.sqlite3', '/reports/%2e%2e'):
            assert (await client.get(path)).status == 404
