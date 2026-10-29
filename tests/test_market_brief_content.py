from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.analytics.audience_reports import MarketSnapshot, render_market_report

KST = ZoneInfo('Asia/Seoul')
NOW = datetime(2026, 10, 29, 7, 40, tzinfo=KST)
STAMP = datetime(2026, 10, 28, 16, 0, tzinfo=ZoneInfo('America/New_York')).timestamp()


def us_snapshot(values):
    symbols = ('^GSPC', '^IXIC', '^SOX')
    return MarketSnapshot(us_basis_date='2026-10-28', us_quotes={
        symbol: {'price': 100, 'change_pct': value, 'market_time': STAMP}
        for symbol, value in zip(symbols, values)})


def test_us_three_index_morning_has_observed_takeaway_and_actual_source():
    text = render_market_report('morning', us_snapshot((.28, .27, 1.75)), NOW)
    assert '한국증시 모닝브리프' in text and '시장 판단' in text
    assert '미국' in text and '3개 지수 동반 상승' in text
    assert '1.48%p' in text and '나스닥' in text
    assert '개장 후' in text and 'Yahoo Finance' in text
    assert '출처: Yahoo Finance' in text and '출처: KIS' not in text


@pytest.mark.parametrize(('values', 'phrase'), [
    ((-.2, -.4, -.1), '3개 지수 동반 하락'),
    ((.2, -.4, 0), '혼조·보합'),
    ((0, 0, 0), '혼조·보합'),
])
def test_us_directions_remain_within_observed_subset(values, phrase):
    text = render_market_report('morning', us_snapshot(values), NOW)
    assert phrase in text
    if all(value < 0 for value in values):
        assert '동반 상승' not in text


def test_partial_and_tiny_relative_difference_do_not_overstate():
    text = render_market_report('morning', us_snapshot((.28,)), NOW)
    assert 'S&amp;P500' in text and '1개 지수 동반' not in text
    text = render_market_report('morning', us_snapshot((.28, .2701, .2702)), NOW)
    assert '0.00%p' not in text and '웃돌아' not in text and '밑돌아' not in text


def test_sector_subset_relative_weakness_and_actual_provider():
    sectors = tuple({'name': name, 'r1': pct, 'source': 'KIS:' + code, 'as_of': '2026-10-28'}
                    for name, pct, code in [('반도체', -1, '123'), ('은행', -2, '456')])
    text = render_market_report('morning', MarketSnapshot(sectors=sectors, kr_basis_date='2026-10-28'), NOW)
    assert '업종 흐름' in text and '상대 상위' in text and '전체 업종 순위 아님' in text
    assert '강세' not in text and '출처: KIS' in text and 'Yahoo Finance' not in text


def test_unknown_source_is_attributed_without_assuming_exchange_and_html_is_safe():
    label = '<b>' + '&' * 500
    sectors = tuple({'name': label, 'r1': i, 'source': '<other&source>', 'as_of': '2026-10-28'} for i in range(30))
    text = render_market_report('morning', MarketSnapshot(sectors=sectors, kr_basis_date='2026-10-28'), NOW)
    assert '<other&source>' not in text and 'KRX' not in text
    assert '&lt;other&amp;source&gt;' in text and '<b>' + '&' not in text
    assert len(text) <= 1800 and len(text.splitlines()) <= 35


def test_us_missing_future_and_mismatched_dates_remain_ineligible():
    snapshot = us_snapshot((.28,))
    row = snapshot.us_quotes['^GSPC']
    for updated in ({'market_time': None}, {'market_time': NOW.timestamp() + 1}, {'market_time': STAMP - 86400}):
        assert render_market_report('morning', MarketSnapshot(us_basis_date='2026-10-28', us_quotes={'^GSPC': {**row, **updated}}), NOW) is None


def test_displayed_rounding_does_not_claim_rise_or_relative_leader():
    text = render_market_report('morning', us_snapshot((.0001, .0002, .0003)), NOW)
    assert '동반 상승' not in text
    sectors = tuple({'name': name, 'r1': pct, 'source': 'KIS:123', 'as_of': '2026-10-28'}
                    for name, pct in [('반도체', -1.0001), ('은행', -1.0002)])
    text = render_market_report('morning', MarketSnapshot(sectors=sectors, kr_basis_date='2026-10-28'), NOW)
    assert '상대 상위' not in text


def test_future_sector_date_cannot_be_presented():
    sector = {'name': '반도체', 'r1': 1, 'source': 'KIS:123', 'as_of': '2026-10-30'}
    assert render_market_report('morning', MarketSnapshot(sectors=(sector,), kr_basis_date='2026-10-30'), NOW) is None


def test_domestic_indices_and_mixed_data_summarize_displayed_values():
    now = NOW.replace(hour=13, minute=10)
    rows = tuple({'label': label, 'price': 3000, 'change_pct': pct, 'source': 'kis',
                  'source_observed_at': now.isoformat(), 'received_at': now.isoformat()}
                 for label, pct in [('KOSPI', 1.2), ('KOSDAQ', -.5)])
    text = render_market_report('midday', MarketSnapshot(indices=rows), now)
    assert '한국증시 장중시황' in text and '혼조·보합' in text
    assert 'KIS' in text and 'Yahoo Finance' not in text


def test_mixed_max_length_labels_and_sources_stay_safe_and_bounded():
    now = NOW.replace(hour=16, minute=40)
    indices = tuple({'label': '<' + '&' * 400, 'price': 3000, 'change_pct': pct,
                     'source': source, 'source_observed_at': now.isoformat(), 'received_at': now.isoformat(),
                     'engine_cash': 'PRIVATE_CANARY'}
                    for pct, source in [(1, 'kis'), (-1, '<foreign&>')])
    sectors = tuple({'name': '<' + '&' * 400, 'r1': pct, 'source': f'<provider{pct}&>', 'as_of': '2026-10-29'}
                    for pct in range(20))
    text = render_market_report('after', MarketSnapshot(indices=indices, sectors=sectors, kr_basis_date='2026-10-29'), now)
    assert '<foreign&>' not in text and '&lt;foreign&amp;&gt;' in text
    assert len(text) <= 1800 and len(text.splitlines()) <= 35
    assert 'PRIVATE_CANARY' not in text


def test_missing_and_mismatched_sector_dates_are_excluded():
    sector = {'name': '반도체', 'r1': 1, 'source': 'KIS:123', 'as_of': '2026-10-27'}
    for basis in (None, '2026-10-28', 'invalid'):
        assert render_market_report('morning', MarketSnapshot(sectors=(sector,), kr_basis_date=basis), NOW) is None


def test_morning_watch_uses_only_available_us_indices():
    text = render_market_report('morning', us_snapshot((.28, .27, 1.75)), NOW)
    assert '삼성전자' in text and 'SK하이닉스' in text and 'SOX' in text
    assert '개장 후' in text and '일치' in text and '확산' in text
    text = render_market_report('morning', us_snapshot((.28,)), NOW)
    watch = text.split('<b>국내 관전 포인트</b>')[1]
    assert 'S&amp;P500' in watch and '코스피' in watch and '코스닥' in watch
    assert 'SOX' not in watch and '반도체' not in watch


def test_sector_only_summary_names_both_ends_without_repeating_values():
    sectors = tuple({'name': name, 'r1': value, 'source': 'KIS:123', 'as_of': '2026-10-28'}
                    for name, value in [('반도체', -1), ('은행', -2)])
    text = render_market_report('morning', MarketSnapshot(sectors=sectors, kr_basis_date='2026-10-28'), NOW)
    summary = text.split('<b>시장 판단</b>')[1].split('<b>업종 흐름')[0]
    assert '반도체 -1.00%' in summary and '은행 -2.00%' in summary
    assert text.count('반도체 -1.00%') == 1 and text.count('은행 -2.00%') == 1
    assert '강세' not in text
    watch = text.split('<b>국내 관전 포인트</b>')[1]
    assert '반도체' in watch and '은행' in watch and '역전' in watch


def test_midday_and_after_watch_reflect_the_available_basis():
    now = NOW.replace(hour=13, minute=10)
    def row(label, pct, observed):
        return {'label': label, 'price': 3000, 'change_pct': pct, 'source': 'kis',
                'source_observed_at': observed, 'received_at': now.isoformat()}
    rows = (row('KOSPI', 1, now.isoformat()), row('KOSDAQ', -1, now.isoformat()))
    text = render_market_report('midday', MarketSnapshot(indices=rows), now)
    assert '방향 차이' in text and '축소' in text and '확대' in text
    rows = (rows[0], row('KOSDAQ', 1, now.isoformat()))
    assert '동행' in render_market_report('midday', MarketSnapshot(indices=rows), now)
    text = render_market_report('midday', MarketSnapshot(indices=(row('KOSPI', 1, None),)), now)
    watch = text.split('<b>국내 관전 포인트</b>')[1]
    assert '원천시각' in watch and '전일 기준 거래일' in watch and '우선' in watch
    assert '동행' not in watch and '방향 차이' not in watch
    after = now.replace(hour=16, minute=40)
    obs = {'code': '0001', 'price': 3000, 'source': 'KIS/FHKUP03500200',
           'source_observed_at': after.replace(hour=15, minute=32).isoformat(),
           'received_at': after.isoformat(), 'observation_kind': 'historical_minute', 'finality': 'unverified'}
    text = render_market_report('after', MarketSnapshot(index_observations=(obs,), kr_basis_date='2026-10-29'), after)
    watch = text.split('<b>국내 관전 포인트</b>')[1]
    assert '확정 종가' in watch and '전일 종가' in watch and '방향' in watch
    assert '%' not in text


def test_mixed_morning_marks_kr_basis_and_stays_mobile():
    previous = NOW.replace(day=28, hour=15, minute=35)
    indices = tuple({'label': label, 'price': 3000, 'change_pct': pct, 'source': 'kis',
                     'source_observed_at': previous.isoformat(), 'received_at': previous.isoformat()}
                    for label, pct in [('KOSPI', 1), ('KOSDAQ', -1)])
    sectors = tuple({'name': '<' + '&' * 100 + str(i), 'r1': i, 'source': 'KIS:123', 'as_of': '2026-10-28'} for i in range(4))
    us = us_snapshot((.28, .27, 1.75))
    snapshot = MarketSnapshot(indices=indices, sectors=sectors, kr_basis_date='2026-10-28',
                              us_quotes=us.us_quotes, us_basis_date=us.us_basis_date)
    text = render_market_report('morning', snapshot, NOW)
    summary = text.split('<b>시장 판단</b>')[1].split('<b>주요 지표')[0]
    assert '기준일' in summary and '2026-10-28' in summary
    assert '&lt;' in text and len(text) <= 1800 and len(text.splitlines()) <= 35
    watch = text.split('<b>국내 관전 포인트</b>')[1]
    assert '&lt;' in watch and '역전' in watch


def test_midday_both_flat_does_not_claim_direction_gap():
    now = NOW.replace(hour=13, minute=10)
    rows = tuple({'label': label, 'price': 3000, 'change_pct': 0, 'source': 'kis',
                  'source_observed_at': now.isoformat(), 'received_at': now.isoformat()}
                 for label in ('KOSPI', 'KOSDAQ'))
    text = render_market_report('midday', MarketSnapshot(indices=rows), now)
    watch = text.split('<b>국내 관전 포인트</b>')[1]
    assert '방향 차이' not in watch and '보합' in watch


@pytest.mark.parametrize(('values', 'difference'), [
    ((.28, .266, .274), None),
    ((.28, .2651, 1.7549), '1.48%p'),
    ((.28, 1.7549, .2651), '1.48%p'),
])
def test_sox_relative_difference_uses_displayed_rounded_values(values, difference):
    text = render_market_report('morning', us_snapshot(values), NOW)
    if difference is None:
        assert '%p' not in text
    else:
        assert difference in text and '1.49%p' not in text


def test_us_names_escape_ampersand_in_summary_rows_and_watch():
    for values in ((.28,), (.28, .27, 1.75), (.28, .27)):
        text = render_market_report('morning', us_snapshot(values), NOW)
        assert 'S&P500' not in text and 'S&amp;P500' in text
        assert '<b>시장 판단</b>' in text
        if len(values) == 1:
            assert text.count('S&amp;P500') == 3
        if len(values) == 2:
            assert 'S&amp;P500' in text.split('<b>국내 관전 포인트</b>')[1]
