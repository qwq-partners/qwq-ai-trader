"""Synthetic citation-local evidence and safe rejection diagnostics."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.analytics.market_report_analysis import build_analysis_prompt, compose_analysis
from tests.test_market_research_content import market, evidence, analysis, NOW
from tests.test_market_report_runner import response
from src.analytics.market_report_runner import prepare_market_report
from src.analytics.report_delivery import ReportStore


def compose(obj):
    return compose_analysis(market(), json.dumps(obj, ensure_ascii=False), kind='morning', day='2026-10-29', public_evidence=evidence())


def test_etf_general_term_requires_real_etf_section():
    obj = analysis()
    obj['thesis'] = {'text': 'ETF 표본은 전체 업종이 아닙니다.', 'evidence_ids': ['E1']}
    obj['issues'][0]['counterpoint'] = 'ETF 표본은 전체 업종이 아닙니다.'
    obj['issues'].append(dict(obj['issues'][0], evidence_ids=['E2']))
    assert compose(obj)[1] == obj
    fake = market().replace('한국 상장 섹터 ETF', '가상 업종 EPS')
    with pytest.raises(ValueError) as exc:
        compose_analysis(fake, json.dumps(obj, ensure_ascii=False), kind='morning', day='2026-10-29', public_evidence=evidence())
    assert (exc.value.code, exc.value.field) == ('unsupported_latin', 'thesis.text')


def test_context_does_not_authorize_unrelated_tokens_or_heading_numbers():
    obj = analysis()
    obj['issues'][0]['evidence_ids'].append('F2')
    for prose, code in [('EPS 전망입니다.', 'unsupported_latin'), ('표본 2026개입니다.', 'unsupported_numeric'), ('수익률 1.0%입니다.', 'unsupported_numeric')]:
        obj['issues'][0]['counterpoint'] = prose
        with pytest.raises(ValueError) as exc:
            compose(obj)
        assert (exc.value.code, exc.value.field) == (code, 'issues[0].counterpoint')


@pytest.mark.parametrize('value,code', [('민감 CANARY 내용', 'unsupported_latin'), ('값 98765입니다.', 'unsupported_numeric'), ('2025-01-01 기준입니다.', 'unsupported_date'), ('가'*121, 'too_long'), ('비밀\n내용', 'invalid_control')])
def test_prose_errors_have_only_fixed_code_and_field(value, code):
    obj = analysis()
    obj['issues'][0]['impact'] = value
    with pytest.raises(ValueError) as exc:
        compose(obj)
    assert (exc.value.code, exc.value.field) == (code, 'issues[0].impact')
    assert value not in str(exc.value)


def test_json_and_ids_errors_are_safe():
    with pytest.raises(ValueError) as exc:
        compose_analysis(market(), 'CANARY {', kind='morning', day='2026-10-29', public_evidence=evidence())
    assert (exc.value.code, exc.value.field) == ('invalid_json', 'response')
    obj = analysis(); obj['thesis']['evidence_ids'] = ['SECRET_ID']
    with pytest.raises(ValueError) as exc:
        compose(obj)
    assert (exc.value.code, exc.value.field) == ('invalid_evidence', 'thesis.evidence_ids')
    assert 'SECRET_ID' not in str(exc.value)


def test_prompt_explains_local_evidence_and_exact_numbers():
    p, s = build_analysis_prompt(market(), kind='morning', day='2026-10-29', public_evidence=evidence())
    assert all(term in p+s for term in ('각 항목', '소수 자릿수', 'ETF', 'evidence_ids', '자체 점검'))


@pytest.mark.asyncio
async def test_runner_records_safe_rejection_and_does_not_retry(tmp_path):
    obj = analysis(); obj['thesis'] = {'text': 'EPS 표본입니다.', 'evidence_ids': ['E1']}
    obj['issues'][0]['impact'] = 'PRIVATE_CANARY'
    manager = SimpleNamespace(complete_role=AsyncMock(return_value=response(content=json.dumps(obj, ensure_ascii=False))))
    store = ReportStore(tmp_path/'r.db')
    kwargs = dict(kind='morning', day='2026-10-29', source_snapshot_id='synthetic', source_body=market(), manager=manager, public_evidence=evidence(), now=NOW)
    row = await prepare_market_report(store, **kwargs)
    assert row['status'] == 'failed' and row['metadata']['reason'] == 'invalid_output'
    assert row['metadata']['validation_code'] == 'unsupported_latin'
    assert row['metadata']['validation_field'] == 'thesis.text'
    assert 'PRIVATE_CANARY' not in json.dumps(row['metadata'])
    assert await prepare_market_report(store, **kwargs) == row
    manager.complete_role.assert_awaited_once()


def test_empty_etf_section_and_other_heading_cannot_authorize_latin():
    from src.analytics.market_report_analysis import _extract
    body = market()
    _, sections, _, _ = _extract(body)
    first = next(x for x in sections if x.startswith('<b>업종 흐름'))
    watch = '<b>국내 관전 포인트</b>'
    before, after = body.split(first, 1)
    _, tail = after.split(watch, 1)
    before = '\n'.join(line for line in before.splitlines() if not line.startswith('최근 1세션 관측 표본 상대 상위 ')) + '\n'
    empty = before + first + '\n확인 0개\n• 설명만\n<b>주요 지표 · EPS 2020-01-01</b>\n• 시장 +1.00%\n' + watch + tail
    obj = analysis()
    obj['thesis'] = {'text': 'ETF 표본입니다.', 'evidence_ids': ['E1']}
    with pytest.raises(ValueError) as exc:
        compose_analysis(empty, json.dumps(obj, ensure_ascii=False), kind='morning', day='2026-10-29', public_evidence=evidence())
    assert (exc.value.code, exc.value.field) == ('unsupported_latin', 'thesis.text')
    obj['thesis'] = {'text': 'EPS 기준입니다.', 'evidence_ids': ['F2']}
    with pytest.raises(ValueError) as exc:
        compose_analysis(empty, json.dumps(obj, ensure_ascii=False), kind='morning', day='2026-10-29', public_evidence=evidence())
    assert (exc.value.code, exc.value.field) == ('unsupported_latin', 'thesis.text')


@pytest.mark.asyncio
async def test_unexpected_composition_failure_has_fixed_metadata(tmp_path, monkeypatch):
    import src.analytics.market_report_analysis as module
    def bad(*args, **kwargs):
        raise RuntimeError('PRIVATE_DIAGNOSTIC_CANARY')
    monkeypatch.setattr(module, 'compose_analysis', bad)
    manager = SimpleNamespace(complete_role=AsyncMock(return_value=response()))
    store = ReportStore(tmp_path/'r.db')
    row = await prepare_market_report(store, kind='morning', day='2026-10-29', source_snapshot_id='synthetic', source_body=market(), manager=manager, public_evidence=evidence(), now=NOW)
    assert row['metadata']['reason'] == 'invalid_output'
    assert row['metadata']['validation_code'] == 'composition_failure'
    assert row['metadata']['validation_field'] == 'response'
    assert 'PRIVATE_DIAGNOSTIC_CANARY' not in json.dumps(row['metadata'])
    manager.complete_role.assert_awaited_once()


def test_diagnostic_allowlist_rejects_forged_values():
    from src.analytics.market_report_analysis import AnalysisValidationError
    from src.analytics.market_report_runner import _validation_diagnostic
    diagnostic = _validation_diagnostic(AnalysisValidationError('SECRET_CANARY', 'thesis.text'))
    assert diagnostic == {'validation_code': 'composition_failure', 'validation_field': 'response'}
    diagnostic = _validation_diagnostic(AnalysisValidationError('unsupported_latin', 'SECRET_CANARY'))
    assert diagnostic == {'validation_code': 'composition_failure', 'validation_field': 'response'}


@pytest.mark.asyncio
async def test_broken_diagnostic_still_completes_factual_report(tmp_path, monkeypatch):
    import src.analytics.market_report_runner as runner
    def broken(*args):
        raise RuntimeError('PRIVATE_DIAGNOSTIC_CANARY')
    monkeypatch.setattr(runner, '_validation_diagnostic', broken)
    obj = analysis(); obj['thesis'] = {'text': 'EPS 전망입니다.', 'evidence_ids': ['E1']}
    manager = SimpleNamespace(complete_role=AsyncMock(return_value=response(content=json.dumps(obj, ensure_ascii=False))))
    store = ReportStore(tmp_path/'r.db')
    row = await prepare_market_report(store, kind='morning', day='2026-10-29', source_snapshot_id='synthetic', source_body=market(), manager=manager, public_evidence=evidence(), now=NOW)
    assert row['status'] == 'failed' and row['metadata']['reason'] == 'invalid_output'
    assert 'PRIVATE_DIAGNOSTIC_CANARY' not in json.dumps(row['metadata'])
    manager.complete_role.assert_awaited_once()
