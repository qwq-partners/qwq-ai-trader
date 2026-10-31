"""Public research → one model request → source-linked text; all IO synthetic."""
import json
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

import pytest

from src.analytics.audience_reports import MarketSnapshot
from src.analytics.report_delivery import ReportStore
from src.schedulers.report_scheduler import ReportSchedule
from tests.test_llm_roles import manager
from tests.test_llm_transport_contract import Session, openai_reply
from tests.test_market_report_analysis import DAY, NOW
from tests.test_report_photo_delivery import notifier


def research_fixture():
    """Invented company and publisher; never a live recommendation or model output."""
    common = dict(kind='news', source='한빛뉴스', published_at='2026-10-28T15:00:00+09:00',
                  publication_status='verified', collected_at='2026-10-29T07:20:00+09:00',
                  observed_at=None, event_date=None, content_kind='excerpt', limitations=[])
    return dict(version='public-research-v1', captured_at=NOW.isoformat(),
                coverage=[dict(source='news_storage', status='ok', detail='합성 기사 두 건')],
                records=[dict(common, id='E1', title='가온반도체 서버용 제품 공급 확대',
                              excerpt='가온반도체가 서버용 제품 공급을 늘린다고 밝혔다.',
                              url='https://example.com/research/server-supply'),
                         dict(common, id='E2', title='가온반도체 설비 투자 비용 증가',
                              excerpt='가온반도체는 설비 투자 비용 증가가 단기 수익성에 부담이라고 설명했다.',
                              url='https://example.com/research/investment-cost')])


def research_response():
    return dict(thesis=dict(text='서버 수요 확대의 수혜와 투자 비용 부담을 함께 판단해야 한다.',
                            evidence_ids=['E1', 'E2']),
                issues=[dict(title='서버 공급 확대의 이익 전환 여부',
                             event='가온반도체가 서버용 제품 공급 확대와 설비 투자 비용 증가를 밝혔다.',
                             impact='공급 확대는 매출에 긍정적이나 투자 비용이 증가하면 이익 개선 속도는 늦어질 수 있다.',
                             counterpoint='투자 비용 부담이 함께 확인돼 공급 확대만으로 수익성 개선을 단정하기 어렵다.',
                             watch='후속 실적 발표에서 서버 매출 증가가 영업이익률 개선으로 이어지는지 확인한다.',
                             evidence_ids=['E1', 'E2'])])


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid, capture', [(False, False), (True, False), (False, True)])
async def test_public_events_reach_real_role_and_full_text_transport(tmp_path, monkeypatch, invalid, capture):
    llm = manager(tmp_path)
    output = research_response()
    if invalid:
        output['issues'][0]['evidence_ids'] = ['E999']
    session = Session(openai_reply(json.dumps(output, ensure_ascii=False)))
    llm.openai._session = session
    bundle = research_fixture()
    factory = AsyncMock(return_value=bundle)
    if capture:
        from src.analytics.market_research_sources import capture_public_research
        from src.data.providers import disclosure_feed
        monkeypatch.setattr(disclosure_feed, 'peek_report_snapshot', lambda: {'status': 'not_ready'})
        rows = [dict(title=r['title'], content=r['excerpt'], source=r['source'], url=r['url'],
                     collected_at=r['collected_at'], private_account='PRIVATE_SOURCE_CANARY')
                for r in bundle['records']]
        bot = SimpleNamespace(theme_detector=SimpleNamespace(_storage=SimpleNamespace(
            get_report_news=AsyncMock(return_value={'status': 'ok', 'rows': rows}))),
            account='PRIVATE_BOT_CANARY')
        async def capture_sources(*, now):
            return await capture_public_research(bot, now=now)
        factory = AsyncMock(side_effect=capture_sources)
    market = MarketSnapshot(
        sectors=tuple({'name': name, 'r1': value, 'as_of': '2026-10-28', 'source': 'KIS'}
                      for name, value in [('금융', 1.2), ('반도체', -.8), ('자동차', -1.4)]),
        kr_basis_date='2026-10-28')
    telegram = notifier()
    reply = AsyncMock(status=200)
    reply.__aenter__.return_value = reply
    reply.json.return_value = {'ok': True}
    telegram._session = Mock(closed=False)
    telegram._session.post.return_value = reply
    store = ReportStore(tmp_path / 'reports.sqlite3')
    try:
        schedule = ReportSchedule(store, telegram, analysis_manager=llm,
                                  public_evidence_factory=factory, clock=lambda: NOW)
        assert await schedule.tick(NOW, True, market, 'PRIVATE_ACCOUNT_CANARY') == 'accepted'
        analysis = store.analysis_receipt('market', 'morning', DAY)
        receipt = store.receipt('market', 'morning', DAY)
        assert analysis['status'] == ('failed' if invalid else 'ready')
        assert receipt['body'] == analysis['final_body']
        assert analysis['public_evidence']['records'][0]['title'] == bundle['records'][0]['title']
        assert analysis['evidence_sha256']
        assert factory.await_count == 1
        assert len(session.payloads) == 1
        payload = session.payloads[0]
        assert payload['model'] == 'gpt-6.1-sol'
        assert payload['reasoning_effort'] == 'medium'
        assert payload['response_format'] == {'type': 'json_object'}
        prompt = json.dumps(payload, ensure_ascii=False)
        assert '가온반도체' in prompt and '설비 투자 비용' in prompt
        assert all(secret not in prompt for secret in ('PRIVATE_ACCOUNT_CANARY', 'PRIVATE_SOURCE_CANARY', 'PRIVATE_BOT_CANARY'))
        assert '전체 업종 순위 아님' in receipt['body']
        assert llm.monthly_budget.status()['settled_count'] == 1
        if invalid:
            assert '사실 요약' in receipt['body']
            assert 'E999' not in receipt['body']
        else:
            assert '서버 공급 확대의 이익 전환 여부' in receipt['body']
            assert '투자 비용 부담' in receipt['body']
            assert 'https://example.com/research/server-supply' in receipt['body']
        assert store.archived_photo('market', 'morning', DAY) is None
        assert receipt['media_type'] == 'text/html' and receipt['caption'] is None
        telegram._session.post.assert_called_once()
        call = telegram._session.post.call_args
        assert call.args[0].endswith('/sendMessage')
        assert call.kwargs['json'] == {'chat_id': 'channel', 'text': analysis['final_body'],
                                       'parse_mode': 'HTML', 'disable_notification': False,
                                       'link_preview_options': {'is_disabled': True}}
        bundle['records'][0]['title'] = 'MUTATED_AFTER_FREEZE'
        assert 'MUTATED_AFTER_FREEZE' not in json.dumps(store.analysis_receipt('market', 'morning', DAY))
        assert await ReportSchedule(store, telegram, analysis_manager=llm,
                                    public_evidence_factory=AsyncMock(side_effect=AssertionError('refetched'))).tick(
                                        NOW.replace(minute=44), True, MarketSnapshot(), 'CHANGED_PRIVATE') == 'already_consumed'
        assert len(session.payloads) == 1
        telegram._session.post.assert_called_once()
    finally:
        llm.openai._session = llm.gemini._session = None
        await llm.close()
