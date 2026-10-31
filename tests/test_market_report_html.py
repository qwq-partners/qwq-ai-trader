"""Web page is a pure view of stored rows (web analysis, facts, evidence, delivery status); no new IO."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.analytics import market_report_html as web
from src.analytics.report_delivery import ReportStore
from src.analytics.web_report import build_facts
from tests.test_web_report import AFTER_NOW, after_market, morning_market, web_bundle, web_response

KST = ZoneInfo('Asia/Seoul')
NOW = datetime(2026, 10, 30, 16, 40, 12, tzinfo=KST)
DAY = '2026-10-30'

# Exact body shape produced by audience_reports on 2026-10-30 (after slot) — a pre-web day.
AFTER_BODY = (
    '🇰🇷 <b>한국증시 마감점검</b>\n10/30 16:40 KST\n\n<b>시장 판단</b>\n'
    '시점이 확인된 지수 참고 가격입니다. 전일 대비 등락·시장 방향 판단자료는 미확보입니다.\n\n'
    '<b>주요 지표 · 국내 지수 분 단위 참고 관측</b>\n• KOSPI 5,730.74 · 10/30 15:32 KST\n• KOSDAQ 724.38 · 10/30 15:32 KST\n'
    '확정 종가 아님 · 해당 분 관측값이며 봉 완료 여부 미확인\n\n'
    '<b>업종 흐름 · 한국 상장 섹터 ETF 2026-10-30</b>\n확인 10개 중 최근 1세션 등락 (전체 업종 순위 아님)\n'
    '• 은행 +0.42%\n• 보험 +0.13%\n• 자동차 -4.60%\n• 반도체 -6.49%\n\n'
    '<b>국내 관전 포인트</b>\n• 확정 종가와 전일 종가를 대조한 뒤 방향을 판단합니다.\n• ETF 은행·반도체의 상대 순위 유지·역전 여부를 확인합니다.\n\n'
    '자료 범위: 표시된 관측 표본; 시장 전체 종목 수·당일 누적 수급은 미확보.\n출처: KIS')

MORNING_BODY = ('🇰🇷 <b>한국증시 모닝브리프</b>\n10/30 07:40 KST\n\n<b>시장 판단</b>\nx\n\n'
                '<b>주요 지표 · 미국 2026-10-29</b>\n• S&amp;P500 +0.52%\n• 나스닥 -0.10%\nSOX−나스닥: +1.20%p\n\n'
                '<b>업종 흐름 · 한국 상장 섹터 ETF 2026-10-29</b>\n• 금융 +1.20% [F3]\n\n자료 범위: x.\n출처: KIS')


def analysed_slot(kind='morning', status='accepted', **updates):
    slot = {'kind': kind, 'status': status, 'sent_at': '10/30 07:40 KST',
            'facts': build_facts(morning_market(), 'morning', datetime(2026, 10, 29, 7, 40, tzinfo=KST)),
            'analysis': web_response(), 'evidence': web_bundle(), 'web_reason': 'validated',
            'web_meta': {'resolved_model': 'gpt-6-astra'}}
    slot.update(updates)
    return slot


def test_legacy_body_becomes_facts_only_page_with_cards_bars_and_skipped_slot():
    facts = web.facts_from_body(AFTER_BODY)
    assert [i['label'] for i in facts['indices']] == ['KOSPI', 'KOSDAQ'] and facts['indices'][0]['basis'] == 'indicative_minute'
    assert facts['indices'][0]['price'] == 5730.74 and facts['indices'][0]['pct'] is None and facts['basis_date'] == '2026-10-30'
    assert [s['pct'] for s in facts['sectors']] == [0.42, 0.13, -4.6, -6.49]
    us = web.facts_from_body(MORNING_BODY)
    assert us['us'][0] == {'id': 'M1', 'label': 'S&P500', 'pct': 0.52, 'date': '2026-10-29', 'source': 'Yahoo Finance'}
    assert us['sectors'][0]['name'] == '금융' and us['sectors'][0]['as_of'] == '2026-10-29'      # [F3] tail tolerated
    page = web.render_day_page(DAY, [
        {'kind': 'morning', 'status': 'accepted', 'sent_at': '10/30 07:40 KST', 'facts': us, 'analysis': None, 'evidence': None, 'web_reason': 'no_web_row'},
        {'kind': 'midday', 'status': 'skipped_missing_current_market_basis', 'facts': None, 'analysis': None},
        {'kind': 'after', 'status': 'accepted', 'sent_at': web.sent_time_of(AFTER_BODY), 'facts': facts, 'analysis': None, 'evidence': None, 'web_reason': 'no_web_row'},
    ], generated_at=NOW, neighbours=('2026-10-29', None), base='https://qwq.ai.kr')
    assert '<article class="slot default" id="after">' in page and page.count('slot default') == 1
    assert '5,730.74' in page and '전일 대비 미확보' in page and '분 단위 참고 · 확정 종가 아님' in page
    assert 'class="f down" style="width:50.0%"' in page            # 반도체 -6.49 fills the full half
    assert 'class="v up">+0.42%' in page and 'class="v down">-6.49%' in page and 'class="v up">+0.52%' in page
    assert '웹 분석 행이 없습니다' in page and '<b>분석 미확보</b>' in page and '지수 2개 · 섹터 ETF 4개' in page
    assert '원천 시점이 확인된 장중 지수 자료가 없어' in page and '장중 13:10 보류' in page
    assert '관전 포인트' not in page and 'LLM 해석' not in page         # Telegram-only sections are not mirrored
    assert 'href="https://qwq.ai.kr/reports/2026-10-29"' in page and 'noindex' in page and 'cdn.' not in page   # no external requests
    assert '페이지 생성 2026/10/30 16:40:12 KST' in page and '투자 권유가 아니며' in page


def test_analysed_slot_renders_web_native_sections_citations_and_escapes_untrusted_text():
    page = web.render_day_page(DAY, [analysed_slot()], generated_at=NOW, base='https://qwq.ai.kr')
    assert 'class="stance flat"' in page and '<h2 class="headline">서버 수요 기대와 투자 비용 부담이 맞서는 장</h2>' in page
    assert '<li>금융 업종 ETF만 오르고 반도체·자동차 ETF는 내렸다.</li>' in page
    assert '<h3>시장 판단</h3>' in page and '<dt class="c">반론</dt><dd>비용 부담이 함께 확인돼' in page
    assert '<div class="o up"><span>상방 조건</span>' in page and '<ol class="check"><li>반도체 ETF의 개장 직후 방향</li>' in page
    assert '<span class="cite"><a href="#morning-M1">M1</a><a href="#morning-M2">M2</a><a href="#morning-M3">M3</a></span>' in page
    assert 'id="morning-M1"' in page and 'id="morning-E1"' in page and 'id="morning-E3"' in page
    import re as _re
    ids = _re.findall(r' id="([^"]+)"', page)
    assert len(ids) == len(set(ids))                                      # citation anchors are unique (review P1)
    assert '.slot:has(:target){display:block}' in page and 'aria-current="page"' in page
    assert '<script>' not in page and '&lt;script&gt;x&lt;/script&gt;' in page
    assert 'href="https://example.com/research/server-supply"' in page and 'href="https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261029000001"' in page
    assert '인용하지 않은 수집 자료 2건' in page                     # E3/E4 are shown but folded
    assert '<td class="num">1432.5 원/달러</td>' in page and '<span class="chip warn">공시 색인 준비 전</span>' in page
    assert '모델 gpt-6-astra' in page and '거시 · 해외' not in page   # macro_view null → section omitted
    unsafe = analysed_slot()
    unsafe['evidence']['records'][0]['url'] = 'javascript:alert(1)'
    unsafe['evidence']['records'][2]['url'] = "https://ex.com/a'b"
    page = web.render_day_page(DAY, [unsafe], generated_at=NOW, base='https://qwq.ai.kr')
    assert 'javascript:' not in page and 'href="https://ex.com/a&#x27;b"' in page


def test_all_slots_skipped_still_shows_the_last_reason():
    page = web.render_day_page(DAY, [{'kind': k, 'status': 'skipped_missing_market_basis', 'facts': None, 'analysis': None} for k in web.MARKET_KINDS],
                               generated_at=NOW, base='https://qwq.ai.kr')
    assert '<article class="slot default" id="after">' in page and '원천 시점이 확인된 필수 자료가 없어' in page


def test_failed_web_row_shows_reason_and_keeps_data_column():
    slot = analysed_slot(analysis=None, web_reason='invalid_output', web_meta={'requested_model': 'gpt-6-astra'})
    page = web.render_day_page(DAY, [slot], generated_at=NOW, base='https://qwq.ai.kr')
    assert '모델 응답이 근거 검증을 통과하지 못해' in page and '<b>분석 미확보</b>' in page
    assert 'class="v up">+1.20%' in page and 'id="morning-E1"' in page and '<h3>시장 판단</h3>' not in page


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
    assert store.claim('market', 'morning', DAY, 'snap-m', AFTER_BODY.replace('마감점검', '모닝브리프'))
    store.finish('market', 'morning', DAY, 'accepted')
    assert store.claim('market', 'midday', DAY, 'snap-d')
    store.finish('market', 'midday', DAY, 'skipped_missing_current_market_basis')
    assert store.claim('market', 'after', DAY, 'snap-a', AFTER_BODY)
    store.finish('market', 'after', DAY, 'accepted')
    facts = build_facts(after_market(), 'after', AFTER_NOW)
    assert store.claim_analysis('web', 'after', DAY, 'wsnap', 'M1: x', 'web-report-v1', owner='w', public_evidence=web_bundle())
    assert store.complete_analysis('web', 'after', DAY, owner='w', status='ready', final_body='웹',
                                   result={'facts': facts, 'analysis': web_response(stance='경계'), 'reason': 'validated'},
                                   metadata={'resolved_model': 'gpt-6-astra'})
    assert store.claim('market', 'morning', '2026-10-31', 'h', None) and store.claim('market', 'after', '2026-10-31', 'h2', None)
    store.finish('market', 'morning', '2026-10-31', 'skipped_holiday'); store.finish('market', 'after', '2026-10-31', 'skipped_holiday')
    html_dir = tmp_path / 'html'
    target = web.write_web_pages(store, DAY, html_dir=html_dir, now=NOW)
    assert target == html_dir / '2026-10-30.html' and (html_dir / 'index.html').exists()
    page = target.read_text()
    assert '/reports/2026-10-31' not in page                              # holiday-only neighbour has no page
    assert not list(html_dir.glob('.tmp-*'))
    assert '<article class="slot default" id="after">' in page and 'class="stance down"' in page
    assert '서버 수요 기대와 투자 비용 부담이 맞서는 장' in page and '장중 13:10 보류' in page
    assert page.count('class="v down">-6.49%') == 2                       # legacy morning facts + web after facts
    assert '웹 분석 행이 없습니다' in page                                # morning slot has no web row → legacy facts only
    index = (html_dir / 'index.html').read_text()
    assert '2026년 10월 30일' in index and '2026년 10월 31일' not in index   # holiday-only day hidden
    assert '서버 수요 기대와 투자 비용 부담이 맞서는 장' in index and '<b>경계</b>' in index
    assert '장전 텔레그램 발송' in index and '장중 자료 보류' in index
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


def test_unsent_stored_body_is_labelled_and_never_the_default_tab():
    facts = web.facts_from_body(AFTER_BODY)
    page = web.render_day_page(DAY, [
        {'kind': 'morning', 'status': 'accepted', 'sent_at': '10/30 07:40 KST', 'facts': facts, 'analysis': None},
        {'kind': 'after', 'status': 'rejected', 'sent_at': '10/30 16:40 KST', 'facts': facts, 'analysis': None},
    ], generated_at=NOW, base='https://qwq.ai.kr')
    assert '<article class="slot default" id="morning">' in page
    assert '10/30 16:40 KST 발송 거절' in page
    only_rejected = web.render_day_page(DAY, [{'kind': 'after', 'status': 'unknown', 'sent_at': 'x', 'facts': facts, 'analysis': None}],
                                        generated_at=NOW, base='https://qwq.ai.kr')
    assert '<article class="slot default" id="after">' in only_rejected and '발송 불명' in only_rejected


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
    # Page writer explodes → delivery result and slot status unchanged; web row still frozen (facts only).
    monkeypatch.setattr(report_scheduler, 'write_web_pages', lambda *a, **k: (_ for _ in ()).throw(OSError('disk')))
    after = dt(2026, 10, 30, 16, 42, tzinfo=KST)
    notifier.publish_report = AsyncMock(return_value='accepted')
    market = MarketSnapshot(sectors=({'name': '은행', 'r1': 0.42, 'as_of': '2026-10-30', 'source': 'KIS'},
                                     {'name': '반도체', 'r1': -6.49, 'as_of': '2026-10-30', 'source': 'KIS'}), kr_basis_date='2026-10-30')
    assert await ReportSchedule(store, notifier, clock=lambda: after).tick(after, True, market, '') == 'accepted'
    sent = notifier.publish_report.await_args.args[4]
    assert sent.endswith('https://qwq.ai.kr/reports/2026-10-30#after')
    row = store.analysis_receipt('web', 'after', '2026-10-30')
    assert row['status'] == 'failed' and row['result']['reason'] == 'research_evidence_missing'
    assert [s['name'] for s in row['result']['facts']['sectors']] == ['은행', '반도체']


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
