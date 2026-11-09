"""R02-WEB-REJECTION-DIAGNOSTICS — 웹 분석 검증 거절의 안전한 사유 코드·필드 경로 영속화 (2026-11-02).

11/2 웹 보고 3건은 `invalid_output` 이었지만 로그에 예외 유형(ValueError)만 남아 원인을 판독할 수 없었다.
여기서는 (1) 검증기가 응답 본문 없이 고정 코드+스키마 경로만 올리는지, (2) 실패 행의 기존 metadata 에
그 진단이 저장되고 재시작(저장소 재오픈) 뒤에도 같은 값을 읽는지, (3) 민감정보 모양의 합성 sentinel 이
저장·로그 어디에도 새지 않는지, (4) 판정·대체 본문·호출 횟수·과거 행 처리가 그대로인지 확인한다.
전부 합성 입력 — 네트워크·운영 캐시 무접촉. 11/2 실제 실패의 원인을 재현했다고 주장하지 않는다.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from loguru import logger

from src.analytics import web_report as web
from src.analytics.market_report_analysis import AnalysisValidationError, ResearchEvidenceUnavailable, _public_bundle
from src.analytics.report_delivery import ReportStore
from tests.test_web_report import DAY, NOW, llm_reply, morning_market, web_bundle, web_response

SENTINEL = 'SENTINELACCT'          # 민감정보 모양의 합성 표식 — 어디에도 저장되면 안 된다
FACTS = web.build_facts(morning_market(), 'morning', NOW)
BUNDLE = _public_bundle(web_bundle())


def _validate(obj):
    return web.validate_web_analysis(json.dumps(obj, ensure_ascii=False), FACTS, BUNDLE)


def test_valid_analysis_still_passes_unchanged():
    assert _validate(web_response())['headline'].startswith('서버 수요')


@pytest.mark.parametrize('updates,code,field', [
    (dict(stance='강세'), 'invalid_stance', 'stance'),
    (dict(headline='가' * 71), 'too_long', 'headline'),
    (dict(headline=''), 'invalid_prose', 'headline'),
    (dict(summary=['금융 업종 ETF만 올랐다.', f'계좌 {SENTINEL} 12% 급등이 확인됐다.']), 'unsupported_numeric', 'summary[1]'),
    (dict(summary=['금융 업종 ETF만 올랐다.', f'{SENTINEL} 보도가 있었다.']), 'unsupported_latin', 'summary[1]'),
    (dict(summary=['금융 업종 ETF만 올랐다.', '2026-01-01 공시가 나왔다.']), 'unsupported_date', 'summary[1]'),
    (dict(summary=['금융 업종 ETF만 올랐다.']), 'invalid_item', 'summary'),      # 2~4개 범위 밖
    (dict(market_view=dict(text='반도체 비중 확대 추천 구간이다.', evidence_ids=['M1'])), 'recommendation_vocabulary', 'market_view.text'),
    (dict(market_view=dict(text='표본에서 금융만 올랐다.', evidence_ids=['E9'])), 'invalid_evidence', 'market_view.evidence_ids'),
    (dict(sector_view=dict(text='표본에서 금융만 올랐다.')), 'invalid_item', 'sector_view'),
    (dict(issues=[]), 'invalid_item', 'issues'),
    (dict(outlook=dict(base='기본 흐름이다.', upside='상방 <b>태그</b>', downside='하방이다.', evidence_ids=['M1'])), 'invalid_prose', 'outlook.upside'),
    (dict(checklist=['항목 하나']), 'invalid_item', 'checklist'),
])
def test_rejections_raise_fixed_code_and_schema_path_without_response_text(updates, code, field):
    obj = web_response(**updates)
    with pytest.raises(AnalysisValidationError) as info:
        _validate(obj)
    error = info.value
    assert (error.code, error.field) == (code, field)
    assert str(error) == f'{code}: {field}' and SENTINEL not in str(error)
    assert isinstance(error, ValueError)                      # 기존 ValueError 호출자 호환
    diagnostic = web._web_validation_diagnostic(error)
    assert (diagnostic['validation_code'], diagnostic['validation_field']) == (code, field)
    # R02-WEB-REJECTION-RECURRENCE: 숫자·영문 거절에만 범위가 붙고(여기 sentinel 은 입력에 없어 absent), 토큰은 어디에도 없다
    expected_scope = {'validation_scope': 'absent'} if code in ('unsupported_numeric', 'unsupported_latin') else {}
    assert diagnostic == {'validation_code': code, 'validation_field': field, **expected_scope}
    assert SENTINEL not in json.dumps(diagnostic)


def test_issue_field_paths_are_indexed_and_json_errors_are_safe():
    obj = web_response()
    obj['issues'][0]['event'] = f'{SENTINEL} 사건 2026-01-01 발생'
    with pytest.raises(AnalysisValidationError) as info:
        _validate(obj)
    assert (info.value.code, info.value.field) == ('unsupported_date', 'issues[0].event')
    with pytest.raises(AnalysisValidationError) as info:
        web.validate_web_analysis('{"stance": "중립", "stance": "중립"}', FACTS, BUNDLE)
    assert (info.value.code, info.value.field) == ('invalid_json', 'response')
    with pytest.raises(AnalysisValidationError) as info:
        web.validate_web_analysis(f'not json {SENTINEL}', FACTS, BUNDLE)
    assert (info.value.code, info.value.field) == ('invalid_json', 'response') and SENTINEL not in str(info.value)
    with pytest.raises(AnalysisValidationError) as info:
        _validate({'stance': '중립'})
    assert (info.value.code, info.value.field) == ('invalid_schema', 'response')
    # 사건 근거 없이 관측만 인용 → 기존대로 ResearchEvidenceUnavailable, 진단은 고정 no_event_cited/issues
    only_facts = web_response()
    only_facts['issues'][0]['evidence_ids'] = ['M1']
    with pytest.raises(ResearchEvidenceUnavailable) as info:
        _validate(only_facts)
    assert web._web_validation_diagnostic(info.value) == {'validation_code': 'no_event_cited', 'validation_field': 'issues'}


def test_diagnostic_allowlist_rejects_forged_or_unknown_values():
    fallback = {'validation_code': 'composition_failure', 'validation_field': 'response'}
    assert web._web_validation_diagnostic(AnalysisValidationError('invalid_prose', 'summary[99]')) == fallback
    assert web._web_validation_diagnostic(AnalysisValidationError(f'DROP TABLE {SENTINEL}', 'headline')) == fallback
    assert web._web_validation_diagnostic(AnalysisValidationError('invalid_prose', f'headline; {SENTINEL}')) == fallback
    assert web._web_validation_diagnostic(RuntimeError(SENTINEL)) == fallback
    assert web._web_validation_diagnostic(None) == fallback
    # 허용 경로는 스키마의 실제 위치뿐이다
    assert 'issues[3].watch' in web._DIAGNOSTIC_FIELDS and 'issues[4]' not in web._DIAGNOSTIC_FIELDS
    assert 'checklist[4]' in web._DIAGNOSTIC_FIELDS and 'summary[4]' not in web._DIAGNOSTIC_FIELDS


def _capture_logs():
    lines = []
    handle = logger.add(lambda message: lines.append(str(message)), level='DEBUG')
    return lines, handle


@pytest.mark.asyncio
async def test_rejected_row_persists_safe_diagnostic_and_leaks_nothing(tmp_path):
    path = tmp_path / 'r.sqlite3'
    store = ReportStore(path)
    bad = web_response(summary=['금융 업종 ETF만 올랐다.', f'계좌 {SENTINEL} 12% 급등이 확인됐다.'])
    manager = SimpleNamespace(complete_role=AsyncMock(return_value=llm_reply(json.dumps(bad, ensure_ascii=False))))
    lines, handle = _capture_logs()
    try:
        row = await web.prepare_web_report(store, kind='morning', day=DAY, snapshot=morning_market(), now=NOW,
                                           public_evidence=web_bundle(), manager=manager)
    finally:
        logger.remove(handle)
    # 판정·대체 처리는 그대로: failed / invalid_output / 분석 None / 사실 보존 / 호출 1회
    assert row['status'] == 'failed' and row['result']['reason'] == 'invalid_output'
    assert row['result']['analysis'] is None and len(row['result']['facts']['sectors']) == 3
    assert manager.complete_role.await_count == 1
    # 새로 영속화되는 것은 고정 코드와 스키마 경로뿐
    assert row['metadata']['validation_code'] == 'unsupported_numeric'
    assert row['metadata']['validation_field'] == 'summary[1]'
    assert row['metadata']['reason'] == 'invalid_output' and row['metadata']['resolved_model'] == 'gpt-6-astra'
    # 원응답·sentinel 은 저장 행 어디에도, 로그에도 없다
    serialized = json.dumps(row, ensure_ascii=False, default=str)
    assert SENTINEL not in serialized and '12%' not in serialized
    assert any('응답 검증 실패' in line and 'unsupported_numeric' in line and 'summary[1]' in line for line in lines)
    assert all(SENTINEL not in line and '12%' not in line for line in lines)
    # 저장소를 닫고 다시 열어도 같은 진단을 읽는다(재시작 후 조회)
    reopened = ReportStore(path).analysis_receipt('web', 'morning', DAY)
    assert reopened['metadata']['validation_code'] == 'unsupported_numeric'
    assert reopened['metadata']['validation_field'] == 'summary[1]'
    assert reopened['status'] == 'failed'
    # 같은 슬롯 재요청은 동결된 행을 돌려주고 재호출하지 않는다
    again = await web.prepare_web_report(store, kind='morning', day=DAY, snapshot=morning_market(), now=NOW,
                                         public_evidence=web_bundle(), manager=manager)
    assert again['metadata'] == row['metadata'] and manager.complete_role.await_count == 1


@pytest.mark.asyncio
async def test_unexpected_validator_exception_has_fixed_fallback_only(tmp_path, monkeypatch):
    store = ReportStore(tmp_path / 'r.sqlite3')
    manager = SimpleNamespace(complete_role=AsyncMock(return_value=llm_reply(json.dumps(web_response(), ensure_ascii=False))))

    def _boom(*_a, **_k):
        raise RuntimeError(f'validator crashed with {SENTINEL}')
    monkeypatch.setattr(web, 'validate_web_analysis', _boom)
    lines, handle = _capture_logs()
    try:
        row = await web.prepare_web_report(store, kind='morning', day=DAY, snapshot=morning_market(), now=NOW,
                                           public_evidence=web_bundle(), manager=manager)
    finally:
        logger.remove(handle)
    assert row['status'] == 'failed' and row['result']['reason'] == 'invalid_output'
    assert row['metadata']['validation_code'] == 'composition_failure'
    assert row['metadata']['validation_field'] == 'response'
    assert SENTINEL not in json.dumps(row, ensure_ascii=False, default=str)
    assert all(SENTINEL not in line for line in lines)


@pytest.mark.asyncio
async def test_no_event_cited_and_validated_rows_keep_prior_shape(tmp_path):
    store = ReportStore(tmp_path / 'r.sqlite3')
    only_facts = web_response()
    only_facts['issues'][0]['evidence_ids'] = ['M1']
    manager = SimpleNamespace(complete_role=AsyncMock(return_value=llm_reply(json.dumps(only_facts, ensure_ascii=False))))
    row = await web.prepare_web_report(store, kind='morning', day=DAY, snapshot=morning_market(), now=NOW,
                                       public_evidence=web_bundle(), manager=manager)
    assert row['status'] == 'failed' and row['result']['reason'] == 'no_event_cited'
    assert row['metadata']['validation_code'] == 'no_event_cited' and row['metadata']['validation_field'] == 'issues'
    good = SimpleNamespace(complete_role=AsyncMock(return_value=llm_reply(json.dumps(web_response(), ensure_ascii=False))))
    ok = await web.prepare_web_report(store, kind='midday', day=DAY, snapshot=morning_market(), now=NOW,
                                      public_evidence=web_bundle(), manager=good)
    assert ok['status'] == 'ready' and ok['metadata']['reason'] == 'validated'
    assert 'validation_code' not in ok['metadata'] and 'validation_field' not in ok['metadata']


def test_legacy_failed_rows_read_without_inferred_diagnostic(tmp_path):
    """11/2 처럼 진단 없이 저장된 과거 행은 그대로 읽히고, 원인을 추정해 채우지 않는다."""
    store = ReportStore(tmp_path / 'r.sqlite3')
    assert store.claim_analysis('web', 'morning', DAY, 'snap', 'M1: x', web.PROMPT_VERSION, owner='old')
    assert store.complete_analysis('web', 'morning', DAY, owner='old', status='failed', final_body=web._BODY,
                                   result={'version': web.PROMPT_VERSION, 'facts': {}, 'analysis': None, 'reason': 'invalid_output'},
                                   metadata={'reason': 'invalid_output', 'resolved_model': 'gpt-6-astra'})
    row = store.analysis_receipt('web', 'morning', DAY)
    assert row['status'] == 'failed' and row['metadata'] == {'reason': 'invalid_output', 'resolved_model': 'gpt-6-astra'}
    assert 'validation_code' not in row['metadata']
