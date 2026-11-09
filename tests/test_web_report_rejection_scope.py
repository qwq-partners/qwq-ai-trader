"""R02-WEB-REJECTION-RECURRENCE (2026-11-09): 11/9 웹 3슬롯 거절을 설명하는 프롬프트-계약 불일치의 최소 수정 ①②.

① `unsupported_numeric/latin` 거절에 `validation_scope`(uncited_source = 전체 출처 어딘가에는 있어 블록 인용 범위만 벗어남,
   absent = 입력 어디에도 없음)만 덧붙인다. 위반 토큰·응답 본문은 어디에도 저장하지 않는다. 검증 계약은 완화하지 않는다.
② 프롬프트에 블록별 인용 범위(E 포함)·관측 시각 표기 그대로·등락률 없는 지수 서술 지시 세 문장과 스키마 예시의 E 인용을 더한다.
fixture 는 원장 Do/판독의 11/9 M 줄·뉴스 제목을 합성한 것이며 운영 DB 를 읽지 않는다. 자연 수락을 보장하지 않는다.
"""
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.analytics import web_report as web
from src.analytics.market_report_analysis import AnalysisValidationError, _public_bundle
from src.analytics.report_delivery import ReportStore
from src.utils.llm_roles import LLMRole, get_role_profile
from tests.test_web_report import AFTER_DAY, AFTER_NOW, after_market, llm_reply, web_bundle

SENTINEL = 'SENTINELACCT'

# 11/9 after 섹터 10개와 같은 모양(값은 합성, M12 = -5.33% 는 원장 판독의 값)
_SECTORS = [('은행', 1.25), ('보험', 0.8), ('철강', 0.45), ('건설', 0.1), ('화학', -0.35), ('증권', -0.9),
            ('바이오', -1.4), ('소프트웨어', -2.1), ('자동차', -3.05), ('2차전지', -5.33)]


def _sectors(start):
    return [{'id': f'M{start + i}', 'name': name, 'pct': pct, 'as_of': '2026-11-09', 'source': 'KIS'}
            for i, (name, pct) in enumerate(_SECTORS)]


def after_facts_1109():
    """after: KOSPI 5,019.73 · KOSDAQ 638.97 `관측 11/09 15:32 KST` 등락률 없음(분 단위 참고) + 섹터 10."""
    return {'version': web.FACTS_VERSION, 'kind': 'after', 'generated_at': '2026-11-09T16:41:56+09:00',
            'basis_date': '2026-11-09', 'us_basis_date': '2026-11-07', 'us': [],
            'indices': [{'id': 'M1', 'label': 'KOSPI', 'price': 5019.73, 'pct': None, 'observed_at': '2026-11-09T15:32:00+09:00',
                         'source': 'KIS', 'basis': 'indicative_minute'},
                        {'id': 'M2', 'label': 'KOSDAQ', 'price': 638.97, 'pct': None, 'observed_at': '2026-11-09T15:32:00+09:00',
                         'source': 'KIS', 'basis': 'indicative_minute'}],
            'sectors': _sectors(3)}


def midday_facts_1109():
    """midday: KOSPI 5,121.15 (+1.21%) · KOSDAQ 656.35 (+1.87%) `관측 11/09 13:08 KST` 제공기관 대비율 + 섹터 10."""
    return {'version': web.FACTS_VERSION, 'kind': 'midday', 'generated_at': '2026-11-09T13:10:50+09:00',
            'basis_date': '2026-11-09', 'us_basis_date': '2026-11-07', 'us': [],
            'indices': [{'id': 'M1', 'label': 'KOSPI', 'price': 5121.15, 'pct': 1.21, 'observed_at': '2026-11-09T13:08:00+09:00',
                         'source': 'KIS', 'basis': 'provider_reference'},
                        {'id': 'M2', 'label': 'KOSDAQ', 'price': 656.35, 'pct': 1.87, 'observed_at': '2026-11-09T13:08:00+09:00',
                         'source': 'KIS', 'basis': 'provider_reference'}],
            'sectors': _sectors(3)}


def morning_facts_1109():
    """morning: 미국 3지수만(S&P500 +0.31% · 나스닥 +1.09% · SOX +4.21%), 국내 지수·섹터 0."""
    return {'version': web.FACTS_VERSION, 'kind': 'morning', 'generated_at': '2026-11-09T07:40:55+09:00',
            'basis_date': '2026-11-07', 'us_basis_date': '2026-11-07', 'indices': [], 'sectors': [],
            'us': [{'id': 'M1', 'label': 'S&P500', 'pct': 0.31, 'date': '2026-11-07', 'source': 'Yahoo Finance'},
                   {'id': 'M2', 'label': '나스닥', 'pct': 1.09, 'date': '2026-11-07', 'source': 'Yahoo Finance'},
                   {'id': 'M3', 'label': '미 반도체 SOX', 'pct': 4.21, 'date': '2026-11-07', 'source': 'Yahoo Finance'}]}


# 11/9 뉴스 제목 중 판독에 쓰인 것만 같은 번호에 두고 나머지는 숫자·영문 없는 합성 제목으로 채운다(E1~E23).
_TITLES = {1: '코스피 장중 5170선 터치 뒤 상승폭 축소', 2: 'AI 반도체 수요 기대가 이어졌다', 4: '코스피, 5000선 겨우 사수',
           5: '코스피 5100선 회복', 19: '코스피 5018.85 마감', 23: '엔비디아 CEO 방한 이슈 부각'}


def bundle_1109():
    news = dict(kind='news', source='합성뉴스', published_at='2026-11-09T09:00:00+09:00', publication_status='verified',
                collected_at='2026-11-09T12:00:00+09:00', observed_at=None, event_date=None,
                content_kind='article_excerpt', limitations='원천 제공 발췌', url=None, excerpt='합성 발췌다.')
    records = [dict(news, id=f'E{i}', title=_TITLES.get(i, f'합성 기사 제목 {"가나다라마바사아자차카타파하"[i % 14]}')) for i in range(1, 24)]
    return _public_bundle(dict(version='public-research-v1', captured_at='2026-11-09T12:00:00+09:00',
                               coverage=[dict(source='news_storage', status='ok', detail='합성')], records=records))


BUNDLE = bundle_1109()
FACTS = {'after': after_facts_1109(), 'midday': midday_facts_1109(), 'morning': morning_facts_1109()}


def base(kind):
    """원장 합성 재현의 기본 응답과 같은 모양: 숫자·영문 없는 한글 문장만."""
    if kind == 'morning':
        mv = {'text': '미국 S&P500은 +0.31%로 마감했다.', 'evidence_ids': ['M1']}
        sv = {'text': '미 반도체 SOX가 +4.21%로 올랐다.', 'evidence_ids': ['M3']}
    else:
        mv = {'text': 'KOSPI는 장중 참고값 기준 관측됐다.', 'evidence_ids': ['M1', 'M2']}
        sv = {'text': '반도체 섹터 ETF가 상대적으로 강했다.', 'evidence_ids': ['M3']}
    return {'stance': '중립', 'headline': '시장은 혼조 흐름을 보였다.', 'summary': ['관측 자료는 제한적이다.', '해석에는 조건이 붙는다.'],
            'market_view': mv, 'sector_view': sv,
            'issues': [{'title': '뉴스 사건', 'event': '사건이 보고됐다.', 'impact': '영향은 제한적일 수 있다.',
                        'counterpoint': '반론도 있다.', 'watch': '원문 확인이 필요하다.', 'evidence_ids': ['E1']}],
            'macro_view': None,
            'outlook': {'base': '보합이 기본이다.', 'upside': '수급이 개선되면 상방이다.', 'downside': '악화되면 하방이다.', 'evidence_ids': ['M1']},
            'checklist': ['자료 갱신 확인', '사건 원문 확인']}


def mv(kind, text, ids):
    obj = base(kind)
    obj['market_view'] = {'text': text, 'evidence_ids': ids}
    return obj


def sm(kind, text):
    obj = base(kind)
    obj['summary'][0] = text
    return obj


def _validate(kind, obj):
    return web.validate_web_analysis(json.dumps(obj, ensure_ascii=False), FACTS[kind], BUNDLE)


def test_fixture_lines_match_the_ledger_readout():
    lines = dict(web.fact_lines(FACTS['after']))
    assert lines['M1'] == 'KOSPI 5,019.73 · 관측 11/09 15:32 KST · 분 단위 참고 관측, 확정 종가 아님, 전일 대비 미확보 · 출처 KIS'
    assert lines['M12'].startswith('섹터 ETF 2차전지 -5.33% · 기준일 2026-11-09')
    assert dict(web.fact_lines(FACTS['midday']))['M1'].startswith('KOSPI 5,121.15 (+1.21%) · 관측 11/09 13:08 KST')
    assert {r['id']: r['title'] for r in BUNDLE['records']}['E4'] == '코스피, 5000선 겨우 사수'


PASSES = [
    ('after', 'M값 그대로 5,019.73', mv('after', 'KOSPI는 15:32 참고값 5,019.73으로 관측됐다.', ['M1', 'M2'])),
    ('after', '뉴스 숫자 5000선 + E4 인용', mv('after', 'KOSPI는 5000선을 간신히 지켰다.', ['M1', 'M2', 'E4'])),
    ('after', '뉴스 마감값 5018.85 + E19 인용', mv('after', 'KOSPI는 5018.85로 마감했다.', ['M1', 'M2', 'E19'])),
    ('after', '관측 시각 15:32 그대로', mv('after', 'KOSPI는 15:32 기준 참고값이다.', ['M1', 'M2'])),
    ('after', '날짜 11/09 그대로', mv('after', 'KOSPI는 11/09 참고값이다.', ['M1', 'M2'])),
    ('after', '섹터 M12 -5.33% 인용 포함', mv('after', '2차전지 섹터 ETF가 -5.33%로 약했다.', ['M1', 'M2', 'M12'])),
    ('after', '섹터 값 -5.33% 만, M12 인용 포함', mv('after', '최하위 섹터 ETF가 -5.33%로 약했다.', ['M1', 'M2', 'M12'])),
    ('after', 'KOSDAQ 638.97 그대로', mv('after', 'KOSDAQ은 638.97로 관측됐다.', ['M1', 'M2'])),
    ('midday', 'M값 5,121.15 (+1.21%)', mv('midday', 'KOSPI는 5,121.15로 +1.21% 상승 중이다.', ['M1', 'M2'])),
    ('midday', '부호 없는 1.21%', mv('midday', 'KOSPI는 1.21% 상승 중이다.', ['M1', 'M2'])),
    ('midday', '뉴스 숫자 5100선 + E5 인용', mv('midday', 'KOSPI는 5100선을 회복했다.', ['M1', 'M2', 'E5'])),
    ('midday', '관측 시각 13:08 그대로', mv('midday', 'KOSPI는 13:08 기준 제공기관 대비율이다.', ['M1', 'M2'])),
    ('morning', 'summary 영문 S&P500/SOX(입력 표기)', sm('morning', '미국 S&P500과 SOX가 상승 마감했다.')),
    ('morning', 'summary AI(E2 제목)', sm('morning', 'AI 관련주 변동성이 커졌다.')),
    ('morning', 'summary CEO(E23 제목)', sm('morning', '엔비디아 CEO 이슈가 부각됐다.')),
    ('morning', 'summary 美(한자, 라틴 아님)', sm('morning', '美 증시가 상승 마감했다.')),
]

REJECTS = [
    ('after', '뉴스 숫자 5000선, M만 인용', mv('after', 'KOSPI는 5000선을 간신히 지켰다.', ['M1', 'M2']), 'unsupported_numeric', 'uncited_source'),
    ('after', '뉴스 마감값 5018.85, M만 인용', mv('after', 'KOSPI는 5018.85로 마감했다.', ['M1', 'M2']), 'unsupported_numeric', 'uncited_source'),
    ('after', '섹터 값 -5.33% 인용 없이', mv('after', '최하위 섹터 ETF가 -5.33%로 약했다.', ['M1', 'M2']), 'unsupported_numeric', 'uncited_source'),
    # 표기 안의 숫자(2차전지의 2)가 첫 위반 토큰이면 값 풀 밖이라 absent — 문서화한 상한(표기 풀 미대조)
    ('after', '섹터 표기 2차전지 인용 없이', mv('after', '2차전지 섹터 ETF가 -5.33%로 약했다.', ['M1', 'M2']), 'unsupported_numeric', 'absent'),
    ('after', '천 단위 절삭 5,019', mv('after', 'KOSPI는 5,019 부근에서 관측됐다.', ['M1', 'M2']), 'unsupported_numeric', 'absent'),
    ('after', '관측 시각 한글화 15시 32분', mv('after', 'KOSPI는 15시 32분 기준 참고값이다.', ['M1', 'M2']), 'unsupported_numeric', 'absent'),
    ('after', '날짜 한글화 9일', mv('after', 'KOSPI는 9일 장중 참고값이다.', ['M1', 'M2']), 'unsupported_numeric', 'absent'),
    ('after', '부호 제거 5.33%', mv('after', '2차전지 섹터 ETF가 5.33% 하락했다.', ['M1', 'M2', 'M12']), 'unsupported_numeric', 'absent'),
    ('after', '전일 대비 포인트 계산(미제공)', mv('after', 'KOSPI는 전일보다 1.7포인트 낮다.', ['M1', 'M2']), 'unsupported_numeric', 'absent'),
    ('midday', '뉴스 숫자 5100선, M만 인용', mv('midday', 'KOSPI는 5100선을 회복했다.', ['M1', 'M2']), 'unsupported_numeric', 'uncited_source'),
    ('midday', '천 단위 절삭 5,121', mv('midday', 'KOSPI는 5,121 부근이다.', ['M1', 'M2']), 'unsupported_numeric', 'absent'),
    ('midday', '13시 8분 한글화', mv('midday', 'KOSPI는 13시 8분 기준 제공기관 대비율이다.', ['M1', 'M2']), 'unsupported_numeric', 'absent'),
    ('midday', '소수 1자리 반올림 1.2%', mv('midday', 'KOSPI는 약 1.2% 상승 중이다.', ['M1', 'M2']), 'unsupported_numeric', 'absent'),
    ('midday', '두 지수 차이 계산 0.66%p', mv('midday', 'KOSDAQ이 KOSPI보다 0.66%p 더 올랐다.', ['M1', 'M2']), 'unsupported_numeric', 'absent'),
    ('morning', 'summary Fed(입력에 없음)', sm('morning', 'Fed 금리 경로가 변수다.'), 'unsupported_latin', 'absent'),
    ('morning', 'summary FOMC(입력에 없음)', sm('morning', 'FOMC 전까지 관망세가 예상된다.'), 'unsupported_latin', 'absent'),
    ('morning', 'summary Nasdaq(입력은 나스닥)', sm('morning', '미국 Nasdaq이 +1.09% 올랐다.'), 'unsupported_latin', 'absent'),
    ('morning', 'summary NVIDIA(입력은 엔비디아)', sm('morning', 'NVIDIA CEO 이슈가 부각됐다.'), 'unsupported_latin', 'absent'),
    ('morning', 'summary HBM(입력에 없음)', sm('morning', 'HBM 수요 기대가 이어졌다.'), 'unsupported_latin', 'absent'),
]


@pytest.mark.parametrize('kind', ['after', 'midday', 'morning'])
def test_base_response_passes(kind):
    assert _validate(kind, base(kind))['stance'] == '중립'


@pytest.mark.parametrize('kind,label,obj', PASSES, ids=[f'{k}:{l}' for k, l, _ in PASSES])
def test_ledger_pass_cases_still_pass(kind, label, obj):
    assert _validate(kind, obj)['stance'] == '중립'


@pytest.mark.parametrize('kind,label,obj,code,scope', REJECTS, ids=[f'{k}:{l}' for k, l, *_ in REJECTS])
def test_ledger_reject_cases_keep_their_code_and_gain_only_a_scope(kind, label, obj, code, scope):
    field = 'summary[0]' if kind == 'morning' else 'market_view.text'
    with pytest.raises(AnalysisValidationError) as info:
        _validate(kind, obj)
    error = info.value
    assert (error.code, error.field, error.scope) == (code, field, scope)
    assert str(error) == f'{code}: {field}'                              # 토큰은 메시지에 없다
    assert web._web_validation_diagnostic(error) == {'validation_code': code, 'validation_field': field, 'validation_scope': scope}


def test_scope_is_block_relative_so_summary_and_headline_are_always_absent_or_cited():
    """headline/summary/checklist 는 전체 출처가 풀이라 unsupported_numeric 이면 absent 다; 같은 토큰이 블록에서는 uncited_source."""
    with pytest.raises(AnalysisValidationError) as info:
        _validate('after', sm('after', 'KOSPI는 5,019 부근에서 관측됐다.'))
    assert (info.value.field, info.value.scope) == ('summary[0]', 'absent')
    ok = _validate('after', sm('after', 'KOSPI는 5000선을 간신히 지켰다.'))       # summary 는 E4 를 인용하지 않아도 전체 풀
    assert ok['summary'][0].startswith('KOSPI는 5000선')
    with pytest.raises(AnalysisValidationError) as info:
        _validate('after', mv('after', 'KOSPI는 5000선을 간신히 지켰다.', ['M1', 'M2']))
    assert info.value.scope == 'uncited_source'


def test_other_codes_and_forged_scopes_never_reach_metadata():
    fallback = {'validation_code': 'composition_failure', 'validation_field': 'response'}
    plain = AnalysisValidationError('too_long', 'headline')
    plain.scope = 'uncited_source'                                        # 숫자·영문 코드가 아니면 범위를 붙이지 않는다
    assert web._web_validation_diagnostic(plain) == {'validation_code': 'too_long', 'validation_field': 'headline'}
    forged = web._reject('unsupported_numeric', 'market_view.text', SENTINEL)
    forged.scope = SENTINEL
    assert web._web_validation_diagnostic(forged) == {'validation_code': 'unsupported_numeric', 'validation_field': 'market_view.text'}
    unscoped = web._reject('unsupported_latin', 'summary[1]', SENTINEL)   # scope 미분류(검증기 밖에서 만든 예외) → 코드·경로만
    assert web._web_validation_diagnostic(unscoped) == {'validation_code': 'unsupported_latin', 'validation_field': 'summary[1]'}
    bad_path = web._reject('unsupported_numeric', f'market_view.text; {SENTINEL}', '5000')
    bad_path.scope = 'absent'
    assert web._web_validation_diagnostic(bad_path) == fallback
    assert SENTINEL not in str(forged) and SENTINEL not in str(unscoped)


@pytest.mark.asyncio
async def test_rejected_row_persists_scope_but_never_the_token(tmp_path):
    path = tmp_path / 'r.sqlite3'
    store = ReportStore(path)
    # web_bundle() 의 E4 거시 발췌 `1432.5 원/달러` 를 M만 인용한 market_view 에 쓰면 인용 범위 밖(uncited_source)
    obj = base('after')
    obj['market_view'] = {'text': f'KOSPI는 1432.5 수준의 환율 부담을 받았고 계좌 {SENTINEL} 언급은 없다.', 'evidence_ids': ['M1']}
    obj['sector_view'] = {'text': '반도체 섹터 ETF가 상대적으로 약했다.', 'evidence_ids': ['M3']}
    manager = SimpleNamespace(complete_role=AsyncMock(return_value=llm_reply(json.dumps(obj, ensure_ascii=False))))
    row = await web.prepare_web_report(store, kind='after', day=AFTER_DAY, snapshot=after_market(), now=AFTER_NOW,
                                       public_evidence=web_bundle(), manager=manager)
    assert row['status'] == 'failed' and row['result']['reason'] == 'invalid_output' and row['result']['analysis'] is None
    assert row['metadata']['validation_code'] == 'unsupported_numeric'
    assert row['metadata']['validation_field'] == 'market_view.text'
    assert row['metadata']['validation_scope'] == 'uncited_source'
    serialized = json.dumps(row['metadata'], ensure_ascii=False) + json.dumps(row['result'], ensure_ascii=False)
    assert SENTINEL not in serialized and '1432.5' not in serialized          # 토큰·응답 본문은 metadata/result 어디에도 없다
    reopened = ReportStore(path).analysis_receipt('web', 'after', AFTER_DAY)
    assert reopened['metadata']['validation_scope'] == 'uncited_source'
    assert manager.complete_role.await_count == 1


def test_prompt_states_block_scope_verbatim_timestamps_and_missing_change_rule():
    prompt, system = web.build_web_prompt(FACTS['after'], BUNDLE, kind='after', day='2026-11-09')
    assert '그 블록의 evidence_ids에 넣은 줄에 있는 것이어야' in system and 'E도 인용 가능' in system
    assert '11/09 15:32처럼 입력 표기 그대로' in system and '한글화(15시 32분)·절삭(5,019)·반올림(1.2%)·계산값(0.66%p)' in system
    assert '등락률이 없는 지수' in system and '"전일 대비 미확보"로 서술' in system
    assert '"evidence_ids":["M1","M2","E4"]' in web._SCHEMA and '"evidence_ids":["M3","E2"]' in web._SCHEMA
    assert 'M1: KOSPI 5,019.73 · 관측 11/09 15:32 KST' in prompt and web._SCHEMA in prompt
    # 검증 계약은 그대로다: 프롬프트 문장이 바뀌어도 거절 종류 집합·필드 경로는 불변
    assert web._DIAGNOSTIC_CODES >= {'unsupported_numeric', 'unsupported_latin'} and web._DIAGNOSTIC_SCOPES == {'uncited_source', 'absent'}


def test_prompt_size_is_checked_against_the_role_input_limit_exactly(monkeypatch):
    prompt, system = web.build_web_prompt(FACTS['midday'], BUNDLE, kind='midday', day='2026-11-09')
    size = len((system + prompt).encode('utf-8'))
    role, profile = get_role_profile(LLMRole.WEB_REPORT)
    assert role is LLMRole.WEB_REPORT and size < profile.max_input_bytes == 96 * 1024
    monkeypatch.setattr(web, 'get_role_profile', lambda r: (role, replace(profile, max_input_bytes=size)))
    assert web.build_web_prompt(FACTS['midday'], BUNDLE, kind='midday', day='2026-11-09') == (prompt, system)
    monkeypatch.setattr(web, 'get_role_profile', lambda r: (role, replace(profile, max_input_bytes=size - 1)))
    with pytest.raises(ValueError, match='web prompt too large'):
        web.build_web_prompt(FACTS['midday'], BUNDLE, kind='midday', day='2026-11-09')
