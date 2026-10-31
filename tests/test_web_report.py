"""Web-only report: facts from the snapshot, own prompt/schema, durable web row; all IO synthetic."""
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from src.analytics import web_report as web
from src.analytics.audience_reports import MarketSnapshot
from src.analytics.market_report_analysis import ResearchEvidenceUnavailable, _public_bundle
from src.analytics.report_delivery import ReportStore

KST = ZoneInfo('Asia/Seoul')
NOW = datetime(2026, 10, 29, 7, 40, tzinfo=KST)
DAY = '2026-10-29'
AFTER_NOW = datetime(2026, 10, 30, 16, 40, tzinfo=KST)
AFTER_DAY = '2026-10-30'


def morning_market():
    """Same synthetic sectors the Telegram e2e tests use; no index rows for the morning slot."""
    return MarketSnapshot(
        sectors=tuple({'name': name, 'r1': value, 'as_of': '2026-10-28', 'source': 'KIS'}
                      for name, value in [('금융', 1.2), ('반도체', -.8), ('자동차', -1.4)]),
        kr_basis_date='2026-10-28')


def after_market():
    """After-close slot: indicative minute prices plus ten eligible sector ETFs."""
    rows = tuple({'code': code, 'label': label, 'price': price, 'source_observed_at': '2026-10-30T15:32:00+09:00',
                  'received_at': '2026-10-30T16:31:00+09:00', 'source': 'KIS/FHKUP03500200',
                  'observation_kind': 'historical_minute', 'finality': 'unverified'}
                 for code, label, price in (('0001', 'KOSPI', 5730.74), ('1001', 'KOSDAQ', 724.38)))
    sectors = [('은행', 0.42), ('보험', 0.13), ('철강', 0.05), ('건설', -0.4), ('화학', -1.1), ('증권', -1.3),
               ('바이오', -2.2), ('소프트웨어', -3.0), ('자동차', -4.6), ('반도체', -6.49)]
    return MarketSnapshot(
        sectors=tuple({'name': n, 'r1': v, 'as_of': '2026-10-30', 'source': 'KIS'} for n, v in sectors)
        + ({'name': '오래된', 'r1': 9.9, 'as_of': '2026-10-29', 'source': 'KIS'},
           {'name': '출처없음', 'r1': 1.0, 'as_of': '2026-10-30', 'source': 'unknown'}),
        kr_basis_date='2026-10-30', index_observations=rows)


def web_bundle():
    """Invented publisher, company and filing; never a live source or recommendation."""
    news = dict(kind='news', source='한빛뉴스', published_at='2026-10-28T15:00:00+09:00',
                publication_status='verified', collected_at='2026-10-29T07:20:00+09:00',
                observed_at=None, event_date=None, content_kind='article_excerpt', limitations='원천 제공 발췌')
    return dict(version='public-research-v1', captured_at=NOW.isoformat(),
                coverage=[dict(source='news_storage', status='ok', detail='합성 기사'),
                          dict(source='dart_earnings', status='not_ready', detail='준비 전')],
                records=[dict(news, id='E1', title='가온반도체 서버용 제품 공급 확대',
                              excerpt='가온반도체가 서버용 제품 공급을 늘린다고 밝혔다.',
                              url='https://example.com/research/server-supply'),
                         dict(news, id='E2', title='가온반도체 설비 투자 비용 증가 <script>x</script>',
                              excerpt='설비 투자 비용 증가가 단기 수익성에 부담이라고 설명했다.',
                              url='https://example.com/research/investment-cost'),
                         dict(kind='disclosure', id='E3', title='가온반도체 분기보고서', excerpt=None, source='DART',
                              url='https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261029000001', published_at=None,
                              publication_status='receipt_date_only', collected_at='2026-10-29T07:00:00+09:00',
                              observed_at=None, event_date='2026-10-29', content_kind='filing_index',
                              limitations='정기공시 첫 페이지의 일부 제목 · 원문 내용과 실적 방향 미검증'),
                         dict(kind='macro', id='E4', title='원/달러 환율', excerpt='1432.5 원/달러', source='합성 거시',
                              url=None, published_at=None, publication_status='observation_date',
                              collected_at=None, observed_at=None, event_date='2026-10-28',
                              content_kind='indicator_value', limitations='지표 기준일 · 보고서 발행일이 아님')])


def web_response(**updates):
    """Validates against morning_market() + web_bundle(): no digits, Latin only from the inputs."""
    obj = dict(
        stance='중립',
        headline='서버 수요 기대와 투자 비용 부담이 맞서는 장',
        summary=['금융 업종 ETF만 오르고 반도체·자동차 ETF는 내렸다.',
                 '가온반도체는 서버용 공급 확대와 설비 투자 비용 증가를 함께 밝혔다.',
                 '개장 후 반도체 업종 ETF의 반등 여부가 첫 확인 과제다.'],
        market_view=dict(text='확인된 섹터 ETF 표본에서는 금융만 상승하고 반도체와 자동차가 하락해 상승 확산으로 보기 어렵다. '
                              '지수 관측이 없어 시장 전체 방향은 확정하지 않는다.', evidence_ids=['M1', 'M2', 'M3']),
        sector_view=dict(text='금융의 상대 강세가 이어질지, 반도체 약세가 수요 기대로 되돌려질지가 업종 흐름의 분기점이다.',
                         evidence_ids=['M1', 'M2']),
        issues=[dict(title='서버 공급 확대의 이익 전환 여부',
                     event='가온반도체가 서버용 제품 공급 확대와 설비 투자 비용 증가를 밝혔다.',
                     impact='공급 확대는 매출에 긍정적이지만 투자 비용이 늘면 이익 개선 속도는 늦어질 수 있다.',
                     counterpoint='비용 부담이 함께 확인돼 공급 확대만으로 수익성 개선을 단정하기 어렵다.',
                     watch='다음 실적 발표에서 서버 매출 증가가 영업이익률 개선으로 이어지는지 확인한다.',
                     evidence_ids=['E1', 'E2'])],
        macro_view=None,
        outlook=dict(base='개장 후 반도체 ETF가 추가 하락하지 않으면 전일의 업종 분화가 완화되는 흐름을 기본으로 본다.',
                     upside='반도체와 자동차 ETF가 함께 반등하면 상승 확산으로 해석한다.',
                     downside='금융만 오르고 두 업종 약세가 이어지면 선별 장세로 재검토한다.',
                     evidence_ids=['M2', 'M3', 'E1']),
        checklist=['반도체 ETF의 개장 직후 방향', '가온반도체 공급 확대 관련 후속 보도', '금융 ETF 상대 강세 유지 여부'])
    obj.update(updates)
    return obj


def llm_reply(content):
    return SimpleNamespace(content=content, success=True, finish_reason='stop', request_sent=True, usage_known=True,
                           refusal_detected=False, requested_model='gpt-6-astra', resolved_model='gpt-6-astra')


def test_facts_keep_every_eligible_sector_and_label_index_basis():
    facts = web.build_facts(after_market(), 'after', AFTER_NOW)
    assert [row['label'] for row in facts['indices']] == ['KOSPI', 'KOSDAQ']
    assert facts['indices'][0]['basis'] == 'indicative_minute' and facts['indices'][0]['pct'] is None
    assert [s['name'] for s in facts['sectors']][:2] == ['은행', '보험'] and facts['sectors'][-1]['name'] == '반도체'
    assert len(facts['sectors']) == 10                      # stale date and unknown source excluded, nothing truncated
    ids = [fid for fid, _ in web.fact_lines(facts)]
    assert ids == [f'M{i}' for i in range(1, 13)]
    lines = dict(web.fact_lines(facts))
    assert lines['M1'].startswith('KOSPI 5,730.74 · 관측 10/30 15:32 KST · 분 단위 참고 관측, 확정 종가 아님')
    assert '확인 10개 표본, 전체 업종 순위 아님' in lines['M12']
    morning = web.build_facts(morning_market(), 'morning', NOW)
    assert morning['indices'] == [] and [s['name'] for s in morning['sectors']] == ['금융', '반도체', '자동차']


def test_prompt_is_bounded_and_validation_rejects_uncited_or_unsafe_prose():
    facts = web.build_facts(morning_market(), 'morning', NOW)
    bundle = _public_bundle(web_bundle())
    prompt, system = web.build_web_prompt(facts, bundle, kind='morning', day=DAY)
    assert '애널리스트' in system and 'M1: 섹터 ETF 금융 +1.20%' in prompt and '"stance"' in prompt
    assert len((prompt + system).encode()) < 96 * 1024
    ok = web.validate_web_analysis(json.dumps(web_response(), ensure_ascii=False), facts, bundle)
    assert ok['stance'] == '중립' and ok['macro_view'] is None
    bad = [web_response(stance='강세'),
           web_response(headline='목표 지수 5000 돌파 기대'),                       # number absent from every source
           web_response(summary=['NVIDIA 실적이 변수다', '둘째 줄이다']),             # Latin absent from inputs
           web_response(market_view=dict(text='금융 <b>강세</b>다', evidence_ids=['M1'])),
           web_response(market_view=dict(text='해당 문장이다', evidence_ids=['E9'])),
           web_response(checklist=['하나만']),
           web_response(summary=['금융 ETF는 -1.20% 내렸다.', '둘째 줄이다']),              # sign flipped vs M1 +1.20% (review P2-1)
           web_response(headline='코스피 3% 급락 우려, 외국인 1조 순매도'),                 # boilerplate digits are not a licence
           web_response(summary=['시각 10시 30분 기준이다.', '둘째 줄이다']),             # timestamp digits are not citable facts
           web_response(checklist=['가온반도체 적극 매수 권고', '둘째 항목이다']),         # recommendation vocabulary (review P2-2)
           web_response(checklist=['가온반도체 목표가 상향', '둘째 항목이다']),
           web_response(outlook=dict(base='기본이다', upside='상방이다', downside='하방이다'))]  # missing evidence_ids
    for obj in bad:
        with pytest.raises(ValueError):
            web.validate_web_analysis(json.dumps(obj, ensure_ascii=False), facts, bundle)
    only_facts = web_response(issues=[dict(web_response()['issues'][0], evidence_ids=['M1'])])
    with pytest.raises(ResearchEvidenceUnavailable):
        web.validate_web_analysis(json.dumps(only_facts, ensure_ascii=False), facts, bundle)
    signed = web_response(summary=['금융 ETF는 +1.20% 올랐고 반도체는 -0.8% 내렸다.', '둘째 줄이다'])
    assert web.validate_web_analysis(json.dumps(signed, ensure_ascii=False), facts, bundle)['summary'][0].startswith('금융')
    cited = web_response(issues=[dict(web_response()['issues'][0], event='환율 1432.5 원/달러 부담이다', evidence_ids=['E1', 'E4'])])
    assert web.validate_web_analysis(json.dumps(cited, ensure_ascii=False), facts, bundle)['issues'][0]['event'].startswith('환율')


@pytest.mark.asyncio
async def test_prepare_web_report_freezes_one_row_per_slot_with_facts_on_every_outcome(tmp_path):
    store = ReportStore(tmp_path / 'r.sqlite3')
    manager = SimpleNamespace(complete_role=AsyncMock(return_value=llm_reply(json.dumps(web_response(), ensure_ascii=False))))
    row = await web.prepare_web_report(store, kind='morning', day=DAY, snapshot=morning_market(), now=NOW,
                                       public_evidence=web_bundle(), manager=manager)
    assert row['status'] == 'ready' and row['result']['analysis']['headline'].startswith('서버 수요')
    assert [s['name'] for s in row['result']['facts']['sectors']] == ['금융', '반도체', '자동차']
    assert row['public_evidence']['records'][0]['title'] == '가온반도체 서버용 제품 공급 확대'
    assert manager.complete_role.call_args.kwargs['role'] == web.LLMRole.WEB_REPORT
    assert row['metadata'] == {'requested_model': 'gpt-6-astra', 'resolved_model': 'gpt-6-astra', 'usage_known': True, 'finish_reason': 'stop',
                               'request_sent': True, 'success': True, 'refusal_detected': False, 'reason': 'validated'}
    again = await web.prepare_web_report(store, kind='morning', day=DAY, snapshot=MarketSnapshot(), now=NOW,
                                         public_evidence=None, manager=manager)
    assert again['result'] == row['result'] and manager.complete_role.await_count == 1   # frozen, never re-paid
    # No public events → facts only, no model call.
    missing = await web.prepare_web_report(store, kind='midday', day=DAY, snapshot=morning_market(), now=NOW,
                                           public_evidence=None, manager=manager)
    assert missing['status'] == 'failed' and missing['result']['reason'] == 'research_evidence_missing'
    assert missing['result']['analysis'] is None and len(missing['result']['facts']['sectors']) == 3
    assert manager.complete_role.await_count == 1
    # Provider exception with no response → send state unknown (same conservative rule as the Telegram runner), facts kept.
    broken = SimpleNamespace(complete_role=AsyncMock(side_effect=RuntimeError('boom')))
    failed = await web.prepare_web_report(store, kind='after', day=DAY, snapshot=after_market(), now=AFTER_NOW,
                                          public_evidence=web_bundle(), manager=broken)
    assert failed['status'] == 'unknown' and failed['result']['reason'] == 'provider_exception'
    assert len(failed['result']['facts']['sectors']) == 10
    # Orphaned attempt is closed as abandoned with facts, never retried.
    store.claim_analysis('web', 'morning', AFTER_DAY, 'snap', 'M1: x', web.PROMPT_VERSION, owner='old')
    orphan = await web.prepare_web_report(store, kind='morning', day=AFTER_DAY, snapshot=morning_market(), now=AFTER_NOW,
                                          public_evidence=web_bundle(), manager=manager)
    assert orphan['status'] == 'abandoned' and orphan['result']['reason'] == 'interrupted' and manager.complete_role.await_count == 1


@pytest.mark.asyncio
async def test_scheduler_prepares_web_row_before_send_and_never_blocks_telegram(tmp_path, monkeypatch):
    from src.analytics.market_report_html import with_web_link
    from src.schedulers.report_scheduler import ReportSchedule
    from tests.test_market_research_e2e import research_response
    monkeypatch.setenv('REPORT_WEB_BASE_URL', 'https://qwq.ai.kr')
    store = ReportStore(tmp_path / 'r.sqlite3')
    telegram = SimpleNamespace(publish_report=AsyncMock(return_value='accepted'))
    replies = [llm_reply(json.dumps(research_response(), ensure_ascii=False)),
               llm_reply(json.dumps(web_response(), ensure_ascii=False))]
    manager = SimpleNamespace(complete_role=AsyncMock(side_effect=replies))
    schedule = ReportSchedule(store, telegram, analysis_manager=manager,
                              public_evidence_factory=AsyncMock(return_value=web_bundle()), clock=lambda: NOW)
    assert await schedule.tick(NOW, True, morning_market(), '') == 'accepted'
    roles = [c.kwargs['role'] for c in manager.complete_role.call_args_list]
    assert roles == [web.LLMRole.MARKET_BRIEF, web.LLMRole.WEB_REPORT]
    telegram_row = store.analysis_receipt('market', 'morning', DAY)
    web_row = store.analysis_receipt('web', 'morning', DAY)
    assert telegram_row['status'] == 'ready' and web_row['status'] == 'ready'
    sent = telegram.publish_report.await_args.args[4]
    assert sent == with_web_link(telegram_row['final_body'], DAY, 'morning')          # Telegram body untouched by the web step
    assert '서버 수요 기대와 투자 비용 부담' not in sent                              # web prose never leaks into Telegram
    page = (tmp_path / 'html' / '2026-10-29.html').read_text()
    assert '서버 수요 기대와 투자 비용 부담이 맞서는 장' in page and 'class="stance flat"' in page
    # Provider failure inside the web step → delivery still accepted; the web row records the failure with facts.
    exploding = SimpleNamespace(complete_role=AsyncMock(side_effect=[
        llm_reply(json.dumps(research_response(), ensure_ascii=False)), RuntimeError('web down')]))
    later = NOW.replace(day=30)
    schedule2 = ReportSchedule(store, telegram, analysis_manager=exploding,
                               public_evidence_factory=AsyncMock(return_value=web_bundle()), clock=lambda: later)
    assert await schedule2.tick(later, True, morning_market(), '') == 'accepted'      # fake notifier: no slot rows
    failed_row = store.analysis_receipt('web', 'morning', '2026-10-30')
    assert failed_row['status'] == 'unknown' and failed_row['result']['reason'] == 'provider_exception'
    assert len(failed_row['result']['facts']['sectors']) == 3
    # The web preparation itself blowing up is logged and ignored.
    monkeypatch.setattr('src.analytics.web_report.prepare_web_report', AsyncMock(side_effect=OSError('disk')))
    day31 = NOW.replace(day=31)
    schedule3 = ReportSchedule(store, telegram, analysis_manager=SimpleNamespace(complete_role=AsyncMock(
        return_value=llm_reply(json.dumps(research_response(), ensure_ascii=False)))),
        public_evidence_factory=AsyncMock(return_value=web_bundle()), clock=lambda: day31)
    assert await schedule3.tick(day31, True, morning_market(), '') == 'accepted'
    assert store.analysis_receipt('web', 'morning', '2026-10-31') is None


def test_store_web_audience_keeps_facts_on_failure_and_allows_larger_results(tmp_path):
    store = ReportStore(tmp_path / 'r.sqlite3')
    assert store.claim_analysis('web', 'after', AFTER_DAY, 'snap', 'M1: x', 'web-report-v1', owner='o')
    assert not store.claim_analysis('web', 'after', AFTER_DAY, 'snap', 'M1: x', 'web-report-v1', owner='o2')
    big = {'facts': {'sectors': [{'name': '업종' * 10, 'pct': 1.0}] * 300}, 'analysis': None, 'reason': 'provider_failed'}
    assert 16384 < len(json.dumps(big, ensure_ascii=False).encode()) < 65536
    assert store.complete_analysis('web', 'after', AFTER_DAY, owner='o', status='failed', final_body='웹', result=big)
    assert store.analysis_receipt('web', 'after', AFTER_DAY)['result']['reason'] == 'provider_failed'
    assert store.claim_analysis('market', 'after', AFTER_DAY, 'snap', 'body', 'v', owner='m')
    with pytest.raises(ValueError):          # market rows keep the old contract: no result on failure
        store.complete_analysis('market', 'after', AFTER_DAY, owner='m', status='failed', final_body='b', result={'x': 1})
    assert store.market_days() == []        # analysis rows never appear as market slots
    with pytest.raises(ValueError):          # delivery API never takes the web audience (review P2-9)
        store.claim('web', 'after', AFTER_DAY, 'snap', 'body')
    with pytest.raises(ValueError):
        store.claim_analysis('engine', 'daily', AFTER_DAY, 'snap', 'body', 'v', owner='e')
