"""미국 전문가의 유한 수치·실제 가격 구간 계약 (합성 공급자만 사용)."""

import asyncio
import json
import sys
import types
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from src.experts.types import ExpertConfig
from src.experts.us_market_expert import USMarketExpert, _INDEX_TICKERS, _SECTOR_TICKERS


BAD_NUMBERS = [float('nan'), float('inf'), float('-inf'), True, False]
BAD_PRICES = BAD_NUMBERS + [0.0, -100.0]


def _history(monkeypatch, prices):
    frame = pd.DataFrame({'Close': pd.Series(prices, dtype=object).values},
                         index=pd.bdate_range('2026-09-01', periods=len(prices)))
    fake = types.ModuleType('yfinance')
    fake.Ticker = lambda ticker: types.SimpleNamespace(history=lambda **kwargs: frame)
    monkeypatch.setitem(sys.modules, 'yfinance', fake)
    return frame


def _expert(monkeypatch, *, indices=None, sectors=None, vix=18.0, semis=None, earnings=None):
    expert = USMarketExpert(ExpertConfig())
    values = {
        '_fetch_index_states': indices if indices is not None else {'SPY': {'vs_ma50_pct': 0.0, 'vs_ma200_pct': 0.0}},
        '_fetch_sector_rs': sectors if sectors is not None else {'XLK': 0.0},
        '_fetch_vix': vix,
        '_fetch_semis_state': semis if semis is not None else {'sox_5d_pct': 0.0},
        '_fetch_earnings_season': earnings if earnings is not None else {},
    }
    for name, value in values.items():
        monkeypatch.setattr(expert, name, AsyncMock(return_value=value))
    return expert


@pytest.mark.parametrize('bad', BAD_NUMBERS)
@pytest.mark.parametrize('vix_valid', [False, True])
def test_invalid_core_and_sector_evidence_cannot_score_or_look_complete(monkeypatch, bad, vix_valid):
    expert = _expert(monkeypatch,
        indices={ticker: {'last': bad, 'vs_ma50_pct': bad, 'vs_ma200_pct': bad}
                 for ticker in _INDEX_TICKERS},
        sectors=({ticker: bad for ticker in _SECTOR_TICKERS},
                 {ticker: {'session_count': 19} for ticker in _SECTOR_TICKERS}),
        vix=18.0 if vix_valid else bad,
        semis={key: bad for key in ('sox_1d_pct', 'sox_5d_pct', 'sox_last', 'smh_5d_pct')},
        earnings={'avg_surprise': bad})
    opinion = asyncio.run(expert._analyze())
    assert opinion.score == 0
    assert opinion.data_status == ('partial' if vix_valid else 'insufficient')
    assert len(opinion.missing_inputs) == (3 if vix_valid else 4)
    assert opinion.affected_sectors == []
    assert opinion.key_findings == []
    assert opinion.raw_evidence['sector_returns'] == {}
    assert opinion.raw_evidence['sector_periods'] == {}
    assert opinion.raw_evidence['avg_surprise'] is None
    assert opinion.raw_evidence['indices']['SPY']['vs_ma50_pct'] is None
    assert opinion.raw_evidence['semis']['sox_5d_pct'] is None
    json.dumps(opinion.raw_evidence, allow_nan=False)


@pytest.mark.parametrize('bad', BAD_NUMBERS)
def test_invalid_sector_returns_are_excluded_before_ranking(monkeypatch, bad):
    expert = _expert(monkeypatch, sectors={'XLF': bad, 'XLE': bad, 'XLK': 0.0, 'XLV': -2.0})
    opinion = asyncio.run(expert._analyze())
    assert opinion.score == 0
    assert opinion.data_status == 'ok'
    assert opinion.affected_sectors == ['XLK', 'XLV']
    assert opinion.raw_evidence['sector_returns'] == {'XLK': 0.0, 'XLV': -2.0}


def test_valid_signed_and_zero_ratios_preserve_existing_score(monkeypatch):
    expert = _expert(monkeypatch,
        indices={'SPY': {'vs_ma50_pct': 3.0, 'vs_ma200_pct': -1.0}},
        sectors={'XLE': 0.0, 'XLK': -1.0}, vix=14.0,
        semis={'sox_5d_pct': -3.0, 'sox_1d_pct': -2.0},
        earnings={'avg_surprise': -0.1})
    opinion = asyncio.run(expert._analyze())
    assert opinion.score == -26
    assert opinion.data_status == 'ok'
    assert opinion.raw_evidence['sector_returns']['XLE'] == 0.0


@pytest.mark.parametrize('bad', BAD_PRICES)
@pytest.mark.parametrize('endpoint', [0, -1])
def test_sector_invalid_exact_endpoint_is_missing_not_earlier_bar_or_zero(monkeypatch, bad, endpoint):
    prices = [100.0] * 20
    prices[endpoint] = bad
    _history(monkeypatch, prices)
    returns, periods = asyncio.run(USMarketExpert(ExpertConfig())._fetch_sector_rs())
    assert returns == {}
    assert periods == {}


@pytest.mark.parametrize('last,expected', [(100.0, 0.0), (90.0, -10.0), (110.0, 10.0)])
def test_sector_valid_return_preserves_dates_and_period(monkeypatch, last, expected):
    frame = _history(monkeypatch, [100.0] * 19 + [last])
    returns, periods = asyncio.run(USMarketExpert(ExpertConfig())._fetch_sector_rs())
    assert returns['XLF'] == expected
    assert periods['XLF'] == {'as_of': frame.index[-1].date().isoformat(),
                             'start': '2026-09-01', 'source': 'yfinance:XLF', 'session_count': 19}


@pytest.mark.parametrize('bad', BAD_PRICES)
def test_latest_invalid_index_and_vix_prices_are_missing(monkeypatch, bad):
    _history(monkeypatch, [100.0] * 199 + [bad])
    expert = USMarketExpert(ExpertConfig())
    assert asyncio.run(expert._fetch_index_states()) == {}
    assert asyncio.run(expert._fetch_vix()) is None


@pytest.mark.parametrize('bad', BAD_PRICES)
def test_ma_window_does_not_skip_invalid_historical_price(monkeypatch, bad):
    prices = [100.0] * 200
    prices[-2] = bad
    _history(monkeypatch, prices)
    state = asyncio.run(USMarketExpert(ExpertConfig())._fetch_index_states())
    assert state['SPY']['last'] == 100.0
    assert state['SPY']['vs_ma50_pct'] is None
    assert state['SPY']['vs_ma200_pct'] is None


@pytest.mark.parametrize('bad', BAD_PRICES)
@pytest.mark.parametrize('endpoint', [0, -2, -1])
def test_semis_invalid_endpoint_only_removes_dependent_returns(monkeypatch, bad, endpoint):
    prices = [100.0] * 6
    prices[endpoint] = bad
    _history(monkeypatch, prices)
    state = asyncio.run(USMarketExpert(ExpertConfig())._fetch_semis_state())
    assert state.get('sox_1d_pct') == (0.0 if endpoint == 0 else None)
    assert state.get('sox_5d_pct') == (0.0 if endpoint == -2 else None)
    assert state.get('smh_5d_pct') == (0.0 if endpoint == -2 else None)
    json.dumps(state, allow_nan=False)


@pytest.mark.parametrize('bad', BAD_NUMBERS)
@pytest.mark.parametrize('field', ['epsActual', 'epsEstimate'])
def test_invalid_earnings_observation_is_excluded_before_clamp(monkeypatch, bad, field):
    calendar = [{'epsActual': 2.0, 'epsEstimate': 2.0},
                {'epsActual': 1.0, 'epsEstimate': 1.0, field: bad}]
    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self): return {'earningsCalendar': calendar}
    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, *args, **kwargs): return Response()
    monkeypatch.setattr('src.experts.us_market_expert.aiohttp.ClientSession', Session)
    expert = USMarketExpert(ExpertConfig())
    expert._finnhub_key = 'offline-fixture'
    earnings = asyncio.run(expert._fetch_earnings_season())
    assert earnings == {'count': 1, 'avg_surprise': 0.0}


def test_ma50_remains_available_when_only_older_ma200_bar_is_missing(monkeypatch):
    prices = [100.0] * 200
    prices[0] = float('nan')
    frame = _history(monkeypatch, prices)
    state = asyncio.run(USMarketExpert(ExpertConfig())._fetch_index_states())['SPY']
    assert state['vs_ma50_pct'] == 0.0
    assert state['vs_ma200_pct'] is None
    assert state['as_of'] == frame.index[-1].date().isoformat()
    assert state['source'] == 'yfinance:SPY'


def test_valid_price_fetches_preserve_index_vix_and_semis_values(monkeypatch):
    _history(monkeypatch, [100.0] * 199 + [110.0])
    expert = USMarketExpert(ExpertConfig())
    state = asyncio.run(expert._fetch_index_states())['SPY']
    assert state['last'] == 110.0
    assert state['vs_ma50_pct'] == 9.78
    assert state['vs_ma200_pct'] == 9.95
    assert asyncio.run(expert._fetch_vix()) == 110.0
    assert asyncio.run(expert._fetch_semis_state()) == {
        'sox_1d_pct': 10.0, 'sox_5d_pct': 10.0, 'sox_last': 110.0, 'smh_5d_pct': 10.0}


def test_overflowing_price_return_is_missing_even_with_finite_endpoints(monkeypatch):
    _history(monkeypatch, [1e-300] * 19 + [1e300])
    returns, periods = asyncio.run(USMarketExpert(ExpertConfig())._fetch_sector_rs())
    assert returns == {}
    assert periods == {}


@pytest.mark.parametrize('bad', [0.0, -1.0])
def test_nonpositive_vix_is_not_a_low_volatility_bonus(monkeypatch, bad):
    opinion = asyncio.run(_expert(monkeypatch, vix=bad)._analyze())
    assert opinion.score == 0
    assert opinion.data_status == 'partial'
    assert opinion.raw_evidence['vix'] is None
    assert opinion.missing_inputs == ['VIX']
