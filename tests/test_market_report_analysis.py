import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.analytics.audience_reports import MarketSnapshot, render_market_report
from src.analytics.market_report_analysis import build_analysis_prompt, compose_analysis, factual_fallback
from src.analytics.market_report_image import _parse, render_market_report_image

NOW = datetime(2026, 10, 29, 7, 40, tzinfo=ZoneInfo('Asia/Seoul'))
DAY = '2026-10-29'


def source():
    sectors = tuple({'name': n, 'r1': v, 'source': 'KIS:123', 'as_of': '2026-10-28'}
                    for n, v in [('금융', 1.2), ('반도체', -0.8), ('자동차', -1.4)])
    return render_market_report('morning', MarketSnapshot(sectors=sectors, kr_basis_date='2026-10-28'), NOW)


def response():
    return {'judgment': {'text': '금융은 올랐지만 반도체와 자동차는 내려 표본 전체의 동반 강세로 보기 어렵습니다.', 'evidence_ids': ['F2', 'F3']},
            'importance': {'text': '금융만의 상승과 두 업종의 약세가 공존해 상승 확산 여부를 확인해야 합니다.', 'evidence_ids': ['F2', 'F3']},
            'watchpoints': [{'focus': '금융과 반도체·자동차', 'why': '금융 상승에도 두 업종이 하락해 동반 강세 근거가 부족합니다.',
                             'confirm': '다음 장에 두 업종이 함께 반등하면 상승 확산을 확인합니다.',
                             'invalidate': '금융만 상승하고 두 업종 약세가 이어지면 선별적 상승으로 재검토합니다.', 'evidence_ids': ['F2', 'F3']}]}


def test_sector_extremes_are_facts_and_analysis_is_image_compatible():
    body = source()
    prompt, system = build_analysis_prompt(body, kind='morning', day=DAY)
    assert '금융 +1.20%' in prompt and '자동차 -1.40%' in prompt
    assert '유지될지 역전될지도' not in prompt
    assert DAY in prompt and '조건' in system
    assert '영문' in system and '쓰지' in system and '120자' in prompt and '1800자' in prompt
    assert '개장 후' in prompt
    final, validated = compose_analysis(body, json.dumps(response(), ensure_ascii=False), kind='morning', day=DAY)
    assert validated == response()
    assert '금융 +1.20%' in final and '자동차 -1.40%' in final
    assert '전체 업종 순위 아님' in final and '출처: KIS' in final
    assert 'LLM 해석' in final and _parse(final) is not None
    assert '확인:' in final and '재검토:' in final
    assert render_market_report_image(final) is not None
    assert len(final) <= 1800 and len(final.splitlines()) <= 35
    fallback = factual_fallback(body)
    assert '분석 미확보' in fallback and '금융 +1.20%' in fallback and '자동차 -1.40%' in fallback
    assert '유지될지 역전될지도' not in fallback and _parse(fallback) is not None


@pytest.mark.parametrize('mutate', [
    lambda x: x.update(extra='x'),
    lambda x: x['judgment'].update(evidence_ids=['F999']),
    lambda x: x['judgment'].update(text='금융이 99% 상승했습니다.'),
    lambda x: x['judgment'].update(text='<script>금융</script>'),
    lambda x: x['judgment'].update(text='금융\n상승'),
    lambda x: x['judgment'].update(text='금융\u2028상승'),
    lambda x: x['judgment'].update(text='금융\u2029상승'),
    lambda x: x['judgment'].update(text='ABC 상승'),
    lambda x: x['judgment'].update(text='ＡＩ 업종 강세'),
    lambda x: x['judgment'].update(text='금융 상승 ②'),
    lambda x: x['judgment'].update(text='금융 상승 ²'),
    lambda x: x['judgment'].update(text='금융 상승 Ⅳ'),
    lambda x: x['watchpoints'][0].update(confirm='동행하거나 다르게 움직입니다.'),
    lambda x: x['importance'].update(evidence_ids=[]),
    lambda x: x['watchpoints'].extend(x['watchpoints'] * 2),
])
def test_rejects_invalid_model_output(mutate):
    obj = response()
    mutate(obj)
    with pytest.raises(ValueError):
        compose_analysis(source(), json.dumps(obj, ensure_ascii=False), kind='morning', day=DAY)


def test_thin_inputs_and_unconfirmed_references_survive():
    now = NOW.replace(hour=13, minute=10)
    row = {'label': 'KOSPI', 'price': 3000, 'change_pct': -1, 'source': 'kis',
           'source_observed_at': None, 'received_at': now.isoformat()}
    body = render_market_report('midday', MarketSnapshot(indices=(row,)), now)
    prompt, _ = build_analysis_prompt(body, kind='midday', day=DAY)
    assert '원천시각 미제공' in prompt and '전일 기준 거래일 미확인' in prompt
    assert '남은 장' in prompt
    assert '원천시각 미제공' in factual_fallback(body)


def test_mixed_us_values_are_not_replaced_by_headline_interpretations():
    stamp = datetime(2026, 10, 28, 16, tzinfo=ZoneInfo('America/New_York')).timestamp()
    quotes = {s: {'price': 100, 'change_pct': v, 'market_time': stamp}
              for s, v in [('^GSPC', .2), ('^IXIC', -.4), ('^SOX', 0)]}
    body = render_market_report('morning', MarketSnapshot(us_basis_date='2026-10-28', us_quotes=quotes), NOW)
    prompt, _ = build_analysis_prompt(body, kind='morning', day=DAY)
    assert '+0.20%' in prompt and '-0.40%' in prompt and '+0.00%' in prompt
    assert '삼성전자' not in prompt and 'Yahoo Finance' in prompt


def test_negative_top_is_relative_not_absolute_and_fallback_keeps_it():
    sectors = tuple({'name': n, 'r1': v, 'source': 'KIS:123', 'as_of': '2026-10-28'}
                    for n, v in [('은행', -1), ('자동차', -2)])
    body = render_market_report('morning', MarketSnapshot(sectors=sectors, kr_basis_date='2026-10-28'), NOW)
    prompt, _ = build_analysis_prompt(body, kind='morning', day=DAY)
    assert '은행 -1.00%' in prompt and '자동차 -2.00%' in prompt
    assert '은행 -1.00%' in factual_fallback(body) and '전체 업종 순위 아님' in prompt


def test_rejects_overlong_final_and_duplicate_json_keys():
    obj = response()
    obj['judgment']['text'] = '가' * 120
    obj['importance']['text'] = '나' * 120
    obj['watchpoints'] = [dict(obj['watchpoints'][0], focus='라' * 120, why='가' * 120, confirm='나' * 120, invalidate='다' * 120)] * 2
    with pytest.raises(ValueError):
        compose_analysis(source().replace('• 반도체 -0.80%', '• ' + '긴표본 ' * 100), json.dumps(obj, ensure_ascii=False), kind='morning', day=DAY)
    with pytest.raises(ValueError):
        compose_analysis(source(), '{"judgment":{},"judgment":{}}', kind='morning', day=DAY)


def test_domestic_basis_year_and_two_watchpoints_fit_image():
    previous = NOW.replace(day=28, hour=15, minute=35)
    indices = tuple({'label': label, 'price': 3000, 'change_pct': pct, 'source': 'kis',
                     'source_observed_at': previous.isoformat(), 'received_at': previous.isoformat()}
                    for label, pct in [('KOSPI', 1), ('KOSDAQ', -1)])
    sectors = tuple({'name': n, 'r1': v, 'source': 'KIS:123', 'as_of': '2026-10-28'}
                    for n, v in [('금융', 1.2), ('반도체', -.8), ('자동차', -1.4)])
    body = render_market_report('morning', MarketSnapshot(indices=indices, sectors=sectors,
                                 kr_basis_date='2026-10-28'), NOW)
    obj = response()
    obj['judgment']['evidence_ids'] = ['F5', 'F6', 'F7']
    obj['importance']['evidence_ids'] = ['F5', 'F6', 'F7']
    obj['watchpoints'][0]['evidence_ids'] = ['F5', 'F6', 'F7']
    obj['watchpoints'].append({'focus': '지수 방향 차이', 'why': '관측 지수의 방향이 갈렸습니다.',
                               'confirm': '개장 후 지수 차이가 이어지는지 확인합니다.',
                               'invalidate': '두 지수가 같은 방향을 보이면 차이 지속 해석을 재검토합니다.',
                               'evidence_ids': ['F2', 'F3']})
    prompt, _ = build_analysis_prompt(body, kind='morning', day=DAY)
    final, _ = compose_analysis(body, json.dumps(obj, ensure_ascii=False), kind='morning', day=DAY)
    assert '국내 지수 기준일 2026-10-28' in prompt and '국내 지수 기준일 2026-10-28' in final
    assert '국내 지수 기준일 2026-10-28' in factual_fallback(body)
    image = render_market_report_image(final)
    assert image is not None and image.height <= 3000


def test_after_prompt_uses_next_session():
    now = NOW.replace(hour=16, minute=40)
    row = {'label': 'KOSPI', 'price': 3000, 'change_pct': 1, 'source': 'kis',
           'source_observed_at': now.isoformat(), 'received_at': now.isoformat()}
    body = render_market_report('after', MarketSnapshot(indices=(row,), kr_basis_date=DAY), now)
    prompt, _ = build_analysis_prompt(body, kind='after', day=DAY)
    assert '다음 거래 세션' in prompt


def test_prompt_scopes_mixed_domestic_us_and_etf_facts():
    previous = NOW.replace(day=28, hour=15, minute=35)
    row = {'label': 'KOSPI', 'price': 3000, 'change_pct': 1, 'source': 'kis',
           'source_observed_at': previous.isoformat(), 'received_at': previous.isoformat()}
    stamp = datetime(2026, 10, 28, 16, tzinfo=ZoneInfo('America/New_York')).timestamp()
    quotes = {'^GSPC': {'price': 100, 'change_pct': -.2, 'market_time': stamp}}
    sectors = ({'name': '금융', 'r1': 1, 'source': 'KIS:123', 'as_of': '2026-10-28'},)
    body = render_market_report('morning', MarketSnapshot(indices=(row,), sectors=sectors,
        kr_basis_date='2026-10-28', us_basis_date='2026-10-28', us_quotes=quotes), NOW)
    prompt, _ = build_analysis_prompt(body, kind='morning', day=DAY)
    lines = prompt.splitlines()
    domestic = next(line for line in lines if 'KOSPI 3,000.00' in line)
    us = next(line for line in lines if 'S&P500 -0.20%' in line)
    etf = next(line for line in lines if '금융 +1.00%' in line)
    assert '국내 지수 전일 종가 대비' in domestic and '국내 지수 기준일 2026-10-28' in domestic
    assert '미국 2026-10-28' in us
    assert '섹터 ETF 2026-10-28' in etf and '전체 업종 순위 아님' in etf


def test_reference_facts_include_shared_limitations_and_after_fallback_keeps_no_comparison():
    now = NOW.replace(hour=13, minute=10)
    row = {'label': 'KOSPI', 'price': 3000, 'change_pct': -1, 'source': 'kis',
           'source_observed_at': None, 'received_at': now.isoformat()}
    body = render_market_report('midday', MarketSnapshot(indices=(row,)), now)
    prompt, _ = build_analysis_prompt(body, kind='midday', day=DAY)
    line = next(line for line in prompt.splitlines() if 'KOSPI 3,000.00' in line)
    assert '원천시각 미제공' in line and '전일 기준 거래일 미확인' in line
    after = NOW.replace(hour=16, minute=40)
    obs = {'code': '0001', 'price': 3000, 'source': 'KIS/FHKUP03500200',
           'source_observed_at': after.replace(hour=15, minute=32).isoformat(),
           'received_at': after.isoformat(), 'observation_kind': 'historical_minute', 'finality': 'unverified'}
    body = render_market_report('after', MarketSnapshot(index_observations=(obs,), kr_basis_date=DAY), after)
    prompt, _ = build_analysis_prompt(body, kind='after', day=DAY)
    line = next(line for line in prompt.splitlines() if 'KOSPI 3,000.00' in line)
    assert '확정 종가 아님' in line and '전일 대비 등락·시장 방향 판단자료는 미확보' in line
    assert '전일 대비 등락·시장 방향 판단자료는 미확보' in factual_fallback(body)
    obj = {'judgment': {'text': '지수 참고값만 확인됩니다.', 'evidence_ids': ['F1']},
           'importance': {'text': '방향 판단을 위해 비교 기준이 필요합니다.', 'evidence_ids': ['F1']}, 'watchpoints': []}
    final, _ = compose_analysis(body, json.dumps(obj, ensure_ascii=False), kind='after', day=DAY)
    assert '전일 대비 등락·시장 방향 판단자료는 미확보' in final


def test_duplicate_fact_text_keeps_each_section_context():
    stamp = datetime(2026, 10, 28, 16, tzinfo=ZoneInfo('America/New_York')).timestamp()
    quotes = {'^IXIC': {'price': 100, 'change_pct': -.4, 'market_time': stamp}}
    sectors = ({'name': '나스닥', 'r1': -.4, 'source': 'KIS:123', 'as_of': '2026-10-28'},)
    body = render_market_report('morning', MarketSnapshot(sectors=sectors, kr_basis_date='2026-10-28',
                               us_basis_date='2026-10-28', us_quotes=quotes), NOW)
    prompt, _ = build_analysis_prompt(body, kind='morning', day=DAY)
    lines = [line for line in prompt.splitlines() if line.endswith('• 나스닥 -0.40%')]
    assert len(lines) == 2
    assert '미국 2026-10-28' in lines[0] and '섹터 ETF 2026-10-28' in lines[1]
