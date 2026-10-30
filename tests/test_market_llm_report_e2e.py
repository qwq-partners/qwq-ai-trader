"""Real report composition, role transport and photo delivery; synthetic IO only."""
import hashlib
import json
from unittest.mock import AsyncMock, Mock

import pytest

from src.analytics.audience_reports import MarketSnapshot
from src.analytics.daily_report import record_morning_brief_dispatch
from src.analytics.report_delivery import ReportStore
from src.schedulers.report_scheduler import ReportSchedule
from tests.test_llm_roles import manager
from tests.test_llm_transport_contract import Session, openai_reply
from tests.test_market_report_analysis import DAY, NOW, response
from tests.test_report_photo_delivery import notifier


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', [False, True])
async def test_role_result_is_composed_archived_and_sent_once(tmp_path, invalid):
    llm = manager(tmp_path)
    output = response()
    if invalid:
        output['judgment']['text'] = '근거 없는 목표 수익률 99%입니다.'
    session = Session(openai_reply(json.dumps(output, ensure_ascii=False)))
    llm.openai._session = session
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
    def record(**kwargs):
        return record_morning_brief_dispatch(**kwargs, archive_dir=tmp_path / 'archive')
    try:
        schedule = ReportSchedule(store, telegram, record, analysis_manager=llm)
        assert await schedule.tick(NOW, True, market, 'PRIVATE_ACCOUNT_CANARY') == 'accepted'
        analysis = store.analysis_receipt('market', 'morning', DAY)
        receipt = store.receipt('market', 'morning', DAY)
        assert analysis['status'] == ('failed' if invalid else 'ready')
        assert receipt['body'] == analysis['final_body']
        assert ('분석 미확보' if invalid else 'LLM 해석:') in receipt['body']
        assert '99%' not in receipt['body']
        assert '금융 +1.20%' in receipt['body'] and '자동차 -1.40%' in receipt['body']
        assert '전체 업종 순위 아님' in receipt['body']
        assert len(session.payloads) == 1
        payload = session.payloads[0]
        assert payload['response_format'] == {'type': 'json_object'}
        assert payload['model'] == 'gpt-6.1-sol'
        assert 'PRIVATE_ACCOUNT_CANARY' not in json.dumps(payload)
        assert llm.monthly_budget.status()['settled_count'] == 1
        photo = store.archived_photo('market', 'morning', DAY)
        assert photo.startswith(b'\x89PNG\r\n\x1a\n')
        assert receipt['image_sha256'] == hashlib.sha256(photo).hexdigest()
        telegram._session.post.assert_called_once()
        assert telegram._session.post.call_args.args[0].endswith('/sendPhoto')
        dispatch = json.loads(next((tmp_path / 'archive').glob('*.json')).read_text())['dispatch'][0]
        assert dispatch['sent_text'] == receipt['caption']
        assert dispatch['semantic_report_body'] == receipt['body']
        assert await ReportSchedule(ReportStore(store.path), telegram, record, analysis_manager=llm).tick(
            NOW.replace(minute=44), True, MarketSnapshot(), 'CHANGED_PRIVATE') == 'already_consumed'
        assert len(session.payloads) == 1
        telegram._session.post.assert_called_once()
    finally:
        llm.openai._session = llm.gemini._session = None
        await llm.close()
