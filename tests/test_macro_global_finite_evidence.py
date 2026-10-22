"""거시·글로벌 숫자 근거의 결측/실제 0% 구분을 오프라인 검증한다."""
import asyncio
import json
import sys
import types
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from src.experts.global_micro_expert import GlobalMicroExpert
from src.experts.macro_economist import MacroEconomist
from src.experts.types import ExpertConfig


INVALID_PRICES = [float('nan'), float('inf'), -float('inf'), True, False, 0.0, -1.0]
INVALID_NUMBERS = INVALID_PRICES[:5]


def fake_prices(monkeypatch, histories):
    class Ticker:
        def __init__(self, ticker):
            self.ticker = ticker

        def history(self, **kwargs):
            values = histories.get(self.ticker, [])
            return pd.DataFrame({'Close': pd.Series(values, dtype=object).values},
                                index=pd.date_range('2026-10-01', periods=len(values)))
    monkeypatch.setitem(sys.modules, 'yfinance', types.SimpleNamespace(Ticker=Ticker))


@pytest.mark.parametrize('bad', INVALID_PRICES)
@pytest.mark.parametrize('endpoint', [0, -1])
def test_invalid_etf_endpoints_are_missing_not_zero_or_shifted(monkeypatch, bad, endpoint):
    values = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    values[endpoint] = bad
    fake_prices(monkeypatch, {'SMH': values})
    expert = GlobalMicroExpert(ExpertConfig())
    assert asyncio.run(expert._fetch_sector_etf_returns()) == {}


@pytest.mark.parametrize('bad', INVALID_PRICES)
@pytest.mark.parametrize('endpoint', [0, -1])
def test_invalid_basket_members_do_not_enter_sample_count(monkeypatch, bad, endpoint):
    invalid = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    invalid[endpoint] = bad
    fake_prices(monkeypatch, {'^SOX': invalid, 'SMH': [100.0] * 6,
                             'NVDA': [100.0] * 5 + [90.0]})
    result = asyncio.run(MacroEconomist(ExpertConfig())._fetch_semis_basket_5d())
    assert result['n_samples'] == 2
    assert result['avg_5d_pct'] == -5.0
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('bad', INVALID_PRICES)
def test_invalid_commodity_levels_are_omitted(monkeypatch, bad):
    fake_prices(monkeypatch, {'CL=F': [90.0, 91.0, bad]})
    assert asyncio.run(GlobalMicroExpert(ExpertConfig())._fetch_sector_etf_returns()) == {}


@pytest.mark.parametrize('bad', INVALID_NUMBERS)
def test_global_analysis_does_not_count_invalid_numbers(monkeypatch, bad):
    expert = GlobalMicroExpert(ExpertConfig())
    monkeypatch.setattr(expert, '_perplexity_search', AsyncMock(return_value='context'))
    monkeypatch.setattr(expert, '_fetch_sector_etf_returns', AsyncMock(return_value={
        'semiconductors': bad, 'battery': bad, 'biotech': bad, 'copper': 5.12, 'wti': 95.0,
    }))
    opinion = asyncio.run(expert._analyze())
    assert opinion.data_status == 'partial'
    assert set(opinion.missing_inputs) == {'반도체 ETF(SMH)', '배터리 ETF', '바이오 ETF'}
    assert opinion.score == 0
    assert opinion.raw_evidence['sector_returns'] == {'copper': 5.12, 'wti': 95.0}
    json.dumps(opinion.raw_evidence, allow_nan=False)


@pytest.mark.parametrize('bad', INVALID_NUMBERS)
def test_macro_invalid_inputs_cannot_count_or_score(monkeypatch, bad):
    expert = MacroEconomist(ExpertConfig())
    monkeypatch.setattr(expert, '_fetch_yfinance_indicators', AsyncMock(return_value={
        'us10y': bad, 'dxy': bad, 'krw_usd': bad, 'vix': bad, 'wti': bad,
    }))
    monkeypatch.setattr(expert, '_fetch_semis_basket_5d', AsyncMock(return_value={'avg_5d_pct': bad}))
    monkeypatch.setattr(expert, '_fetch_macro_context', AsyncMock(return_value=''))
    monkeypatch.setattr(expert, '_load_manual_overrides', lambda: {'cpi_yoy': bad})
    opinion = asyncio.run(expert._analyze())
    assert opinion.data_status == 'insufficient'
    assert opinion.score == 0
    assert len(opinion.missing_inputs) == 7
    assert opinion.raw_evidence['prices'] == {}
    json.dumps(opinion.raw_evidence, allow_nan=False)


@pytest.mark.parametrize('bad', INVALID_NUMBERS)
def test_macro_indicator_bool_or_nonfinite_does_not_create_rate_delta(monkeypatch, bad):
    fake_prices(monkeypatch, {'^TNX': [bad, 5.12], '^IRX': [1.0, bad]})
    expert = MacroEconomist(ExpertConfig())
    result = asyncio.run(expert._fetch_yfinance_indicators())
    assert result == {'us10y': 5.12}
    assert expert._indicator_evidence['us10y'] == {
        'source': 'yfinance:^TNX', 'as_of': '2026-10-02T00:00:00'}


def test_valid_endpoints_preserve_signed_and_zero_returns_without_dropping_holes(monkeypatch):
    fake_prices(monkeypatch, {'SMH': [100.0, 101.0, float('nan'), 102.0, 103.0, 90.0],
                             'LIT': [100.0] * 6, 'XBI': [100.0] * 5 + [105.0],
                             'CL=F': [0.0, float('nan'), 95.0]})
    result = asyncio.run(GlobalMicroExpert(ExpertConfig())._fetch_sector_etf_returns())
    assert result == {'semiconductors': -10.0, 'battery': 0.0, 'biotech': 5.0, 'wti': 95.0}


def test_overflow_return_is_not_evidence(monkeypatch):
    fake_prices(monkeypatch, {'SMH': [1e-308] * 5 + [1e308]})
    assert asyncio.run(GlobalMicroExpert(ExpertConfig())._fetch_sector_etf_returns()) == {}
    assert asyncio.run(MacroEconomist(ExpertConfig())._fetch_semis_basket_5d()) == {}


@pytest.mark.parametrize('bad', [0.0, -1.0, float('nan'), float('inf'), True])
def test_global_without_any_valid_input_is_insufficient(monkeypatch, bad):
    expert = GlobalMicroExpert(ExpertConfig())
    monkeypatch.setattr(expert, '_perplexity_search', AsyncMock(return_value='context'))
    monkeypatch.setattr(expert, '_fetch_sector_etf_returns', AsyncMock(return_value={
        'copper': bad, 'wti': bad,
    }))
    opinion = asyncio.run(expert._analyze())
    assert opinion.data_status == 'insufficient'
    assert opinion.score == 0
    assert len(opinion.missing_inputs) == 5
    assert opinion.raw_evidence['sector_returns'] == {}


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), True])
def test_macro_missing_optional_basket_does_not_inflate_confidence(monkeypatch, bad):
    expert = MacroEconomist(ExpertConfig())
    monkeypatch.setattr(expert, '_fetch_yfinance_indicators', AsyncMock(return_value={
        'us10y': 5.12, 'dxy': 103.0, 'krw_usd': 1350.0, 'vix': 15.0, 'wti': 70.0,
    }))
    monkeypatch.setattr(expert, '_fetch_semis_basket_5d', AsyncMock(return_value={'avg_5d_pct': bad}))
    monkeypatch.setattr(expert, '_fetch_macro_context', AsyncMock(return_value=''))
    monkeypatch.setattr(expert, '_load_manual_overrides', lambda: {})
    expert._indicator_evidence = {'us10y': {'source': 'yfinance:^TNX',
                                         'as_of': '2026-10-20T00:00:00', 'change_bp': bad}}
    opinion = asyncio.run(expert._analyze())
    assert opinion.data_status == 'ok'  # 핵심 5종의 기존 기준은 그대로다.
    assert opinion.confidence == 0.65
    assert set(opinion.missing_inputs) == {'CPI(수동 오버라이드)', '반도체 바스켓 5일'}
    assert opinion.key_findings[0] == 'US10Y 5.12% · 전일 변화 미확인'
    assert opinion.raw_evidence['indicator_evidence']['us10y']['as_of'] == '2026-10-20T00:00:00'
    json.dumps(opinion.raw_evidence, allow_nan=False)


@pytest.mark.parametrize('cpi', [-1.0, 0.0])
def test_macro_valid_signed_cpi_and_zero_basket_survive_validation(monkeypatch, cpi):
    expert = MacroEconomist(ExpertConfig())
    monkeypatch.setattr(expert, '_fetch_yfinance_indicators', AsyncMock(return_value={
        'us10y': 0.0, 'wti': -1.0,
    }))
    monkeypatch.setattr(expert, '_fetch_semis_basket_5d', AsyncMock(return_value={'avg_5d_pct': 0.0}))
    monkeypatch.setattr(expert, '_fetch_macro_context', AsyncMock(return_value=''))
    monkeypatch.setattr(expert, '_load_manual_overrides', lambda: {'cpi_yoy': cpi, 'vix': False})
    opinion = asyncio.run(expert._analyze())
    assert opinion.data_status == 'partial'
    assert opinion.score == 8
    assert opinion.raw_evidence['prices'] == {'cpi_yoy': cpi, 'semis_basket_5d_pct': 0.0}
    assert 'US10Y' in opinion.missing_inputs and 'WTI' in opinion.missing_inputs


def test_global_zero_returns_remain_observed_numeric_inputs(monkeypatch):
    expert = GlobalMicroExpert(ExpertConfig())
    monkeypatch.setattr(expert, '_perplexity_search', AsyncMock(return_value='context'))
    monkeypatch.setattr(expert, '_fetch_sector_etf_returns', AsyncMock(return_value={
        'semiconductors': 0.0, 'battery': 0.0, 'biotech': 0.0, 'copper': 4.0, 'wti': 80.0,
    }))
    opinion = asyncio.run(expert._analyze())
    assert opinion.data_status == 'ok'
    assert opinion.missing_inputs == []
    assert opinion.score == 0
