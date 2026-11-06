"""R02-WEB-LABEL-NUMERIC (2026-11-06): 출처 줄에 그대로 적힌 숫자 포함 표기(S&P500·2차전지·10개 표본·1세션·관측 시각)는
그 표기 그대로일 때만 인용할 수 있고, 벗겨진 숫자(3%·1조·10%)와 미인용 사실의 표기는 지금처럼 거절한다. 전부 합성 입력."""
import json
from dataclasses import replace

import pytest

from src.analytics import web_report as web
from src.analytics.market_report_analysis import AnalysisValidationError, _public_bundle
from tests.test_web_report import AFTER_NOW, after_market, web_bundle, web_response

BUNDLE = _public_bundle(web_bundle())


def morning_us_facts():
    """11/6 07:40 운영 facts 와 같은 모양: 국내 지수 0, 미국 지수 3개(라벨 S&P500 에 숫자 포함), 섹터 0."""
    return {'version': web.FACTS_VERSION, 'kind': 'morning', 'generated_at': '2026-11-06T07:40:55+09:00',
            'basis_date': '2026-11-05', 'us_basis_date': '2026-11-05', 'indices': [], 'sectors': [],
            'us': [{'id': 'M1', 'label': 'S&P500', 'pct': -4.36, 'date': '2026-11-05', 'source': 'Yahoo Finance'},
                   {'id': 'M2', 'label': '나스닥', 'pct': -5.52, 'date': '2026-11-05', 'source': 'Yahoo Finance'},
                   {'id': 'M3', 'label': '미 반도체 SOX', 'pct': -12.25, 'date': '2026-11-05', 'source': 'Yahoo Finance'}]}


def after_facts_with_battery():
    snapshot = after_market()
    battery = ({'name': '2차전지', 'r1': -1.7, 'as_of': '2026-10-30', 'source': 'KIS'},)
    return web.build_facts(replace(snapshot, sectors=snapshot.sectors + battery), 'after', AFTER_NOW)


def _validate(obj, facts):
    return web.validate_web_analysis(json.dumps(obj, ensure_ascii=False), facts, BUNDLE)


def _rejects(obj, facts, field):
    with pytest.raises(AnalysisValidationError) as info:
        _validate(obj, facts)
    assert (info.value.code, info.value.field) == ('unsupported_numeric', field)


def test_label_digits_are_not_in_the_value_pool_so_only_the_verbatim_rule_licenses_them():
    facts = morning_us_facts()
    assert dict(web.fact_lines(facts))['M1'].startswith('미국 S&P500 -4.36%')
    assert '500' not in web.fact_numbers(facts)['M1'].split()
    assert web._digit_words([dict(web.fact_lines(facts))['M1']]) == {'S&P500'}      # -4.36% 는 값이지 표기가 아니다
    assert web._digit_words(['매출 10% 증가', '+1.20%', '(-5.65%)', '2026-11-06', '1432.5']) == set()
    assert web._digit_words(['확인 10개 표본 · 최근 1세션 · 관측 11/06 15:32 KST · 출처 KIS:091170']) == {'10개', '1세션', '11/06', '15:32', 'KIS:091170'}


def test_us_index_label_in_cited_fact_line_passes_with_particles():
    facts = morning_us_facts()
    obj = web_response(
        summary=['S&P500과 나스닥이 함께 내렸다.', '반도체 지수의 낙폭이 가장 컸다.'],
        market_view=dict(text='S&P500이 -4.36%, 나스닥이 -5.52% 내려 위험 회피가 확인됐다. 국내 지수 관측은 없다.',
                         evidence_ids=['M1', 'M2']),
        sector_view=dict(text='미 반도체 SOX의 -12.25% 하락이 국내 반도체 업종의 개장 부담이다.', evidence_ids=['M3']))
    ok = _validate(obj, facts)
    assert ok['market_view']['text'].startswith('S&P500이')


def test_sector_name_sample_count_session_and_observation_time_pass_verbatim():
    facts = after_facts_with_battery()
    lines = dict(web.fact_lines(facts))
    battery = next(fid for fid, text in lines.items() if '2차전지' in text)
    assert '확인 11개 표본' in lines[battery]            # 합성 섹터를 더해 표본은 11개다
    obj = web_response(
        market_view=dict(text='KOSPI 5,730.74는 15:32 관측값이라 확정 종가가 아니다.', evidence_ids=['M1']),
        sector_view=dict(text='확인 11개 표본의 최근 1세션에서 2차전지가 -1.70% 내렸고 2차전지의 반등 여부가 분기점이다.',
                         evidence_ids=[battery]))
    ok = _validate(obj, facts)
    assert ok['sector_view']['text'].startswith('확인 11개')


def test_bare_digits_and_uncited_labels_stay_rejected():
    morning = morning_us_facts()
    after = after_facts_with_battery()
    battery = next(fid for fid, text in web.fact_lines(after) if '2차전지' in text)
    # 벗겨진 숫자: 표기 안의 숫자는 그 표기 밖에서 쓸 수 없다
    _rejects(web_response(market_view=dict(text='지수가 500 포인트 내렸다.', evidence_ids=['M1'])), morning, 'market_view.text')
    _rejects(web_response(sector_view=dict(text='표본 10% 하락이 확인됐다.', evidence_ids=[battery])), after, 'sector_view.text')
    _rejects(web_response(sector_view=dict(text='확인 10개 표본이다.', evidence_ids=[battery])), after, 'sector_view.text')  # 실제 줄은 11개
    _rejects(web_response(headline='코스피 3% 급락 우려, 외국인 1조 순매도'), after, 'headline')
    # 숫자가 이어 붙은 변형은 표기 그대로가 아니다
    _rejects(web_response(market_view=dict(text='S&P5000이 내렸다.', evidence_ids=['M1'])), morning, 'market_view.text')
    _rejects(web_response(market_view=dict(text='KOSPI 15,730.74 관측이다.', evidence_ids=['M1'])), after, 'market_view.text')
    _rejects(web_response(sector_view=dict(text='12차전지가 내렸다.', evidence_ids=[battery])), after, 'sector_view.text')
    # 인용하지 않은 사실의 표기는 허용되지 않는다(문단별 인용 범위 유지)
    _rejects(web_response(market_view=dict(text='S&P500이 내렸다.', evidence_ids=['M2'])), morning, 'market_view.text')
    _rejects(web_response(market_view=dict(text='2차전지가 약했다.', evidence_ids=['M1'])), after, 'market_view.text')
    # 부호 뒤집기는 여전히 거절된다
    _rejects(web_response(market_view=dict(text='S&P500이 +4.36% 올랐다.', evidence_ids=['M1'])), morning, 'market_view.text')
    _rejects(web_response(sector_view=dict(text='2차전지가 +1.70% 올랐다.', evidence_ids=[battery])), after, 'sector_view.text')
    # 표기 앞에 붙은 부호도 표기 그대로가 아니다
    _rejects(web_response(sector_view=dict(text='표본이 -11개로 줄었다.', evidence_ids=[battery])), after, 'sector_view.text')


def test_unsigned_public_evidence_number_never_licenses_a_signed_rewrite():
    """독립 리뷰 P2: E 발췌의 부호 없는 10% 가 응답의 -10%/+10% 를 허용하면 안 된다."""
    raw = web_bundle()
    raw['records'][0]['excerpt'] = '가온반도체가 서버용 제품 공급을 10% 늘린다고 밝혔다.'
    bundle = _public_bundle(raw)
    facts = morning_us_facts()
    issue = web_response()['issues'][0]

    def with_event(text):
        return web_response(issues=[dict(issue, event=text, evidence_ids=['E1'])])
    ok = web.validate_web_analysis(json.dumps(with_event('가온반도체가 공급을 10% 늘린다고 밝혔다.'), ensure_ascii=False), facts, bundle)
    assert ok['issues'][0]['event'].endswith('밝혔다.')
    for text in ('가온반도체 공급이 -10% 줄었다.', '가온반도체 공급이 +10% 늘었다.', '가온반도체 공급이 10.0% 늘었다.'):
        with pytest.raises(AnalysisValidationError) as info:
            web.validate_web_analysis(json.dumps(with_event(text), ensure_ascii=False), facts, bundle)
        assert (info.value.code, info.value.field) == ('unsupported_numeric', 'issues[0].event')
