"""Opus A diagnosis: real owner/SQL, only external stages/model/clock synthetic.

Products stay frozen. Failing expectations document provenance defects; controls
explicitly avoid inventing TTL or requiring unconsumed OHLC.
"""
import asyncio
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest

from test_execution_regime_noon_replay import Inputs, c3_owned, stage_input
from test_execution_regime_owner import quote
from test_execution_runtime import NOW


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


async def classify_facts(tmp_path, monkeypatch, *, quote_transform=None, overnight=None,
                         delayed=False, screener=True, last_bar_date=None):
    clock = [NOW]
    _, _, store, runtime, owner, _ = await c3_owned(tmp_path, monkeypatch, clock=lambda: clock[0])
    class Provider(Inputs):
        async def fetch_us_overnight(self):
            return stage_input('us_overnight', {'overnight': deepcopy(overnight)} if overnight is not None else None)
        def snapshot_screener(self):
            return stage_input('screener', {'closes': [100.0] * 25,
                'last_bar_date': last_bar_date or NOW.date().isoformat(), 'loaded_at': (NOW - timedelta(hours=1)).isoformat()}
                if screener else None)
        async def fetch_index_price(self, code):
            if delayed: clock[0] = NOW + timedelta(seconds=1)
            value = quote(code, pct=-3.0)
            if quote_transform: quote_transform(value)
            return stage_input('index' + code, {'quote': value})
    class Model:
        calls = 0
        async def complete_json(self, **kwargs):
            self.calls += 1
            return {'regime': 'ranging'}
    model = Model()
    try:
        receipt = await owner.classify('followup', inputs_provider=Provider(), llm=model)
        assert receipt.status == 'accepted'
        assert model.calls == 1
        state = (await store.load())[1]
        source = state['risk_sources']['records'][receipt.operation_id]
        inputs = state['risk_input_seals']['records'][receipt.operation_id]['original']['request']['inputs']
        assert inputs['input_meta'] == source['terminal']['envelope']['payload']['result']['input_meta']
        assert 'classifier:' + receipt.operation_id in state['regime_policy']['applications']
        await runtime.restore()
        assert runtime.owner.state == state
        assert not runtime.trading_ready and not runtime.engine._event_queue
        return inputs, state
    finally:
        await runtime.shutdown()
        await store.close()


@pytest.mark.parametrize('variant,accepted', [('old_same_day', True), ('yesterday', False),
    ('future', False), ('wrong_tr', False), ('unused_ohlc_missing', True)])
def test_noon_and_prompt_have_matching_validity_without_added_ttl(tmp_path, monkeypatch, variant, accepted):
    def transform(value):
        observation = value['_observation']
        if variant in {'old_same_day', 'yesterday', 'future'}:
            delta = {'old_same_day': timedelta(hours=-1), 'yesterday': timedelta(days=-1),
                     'future': timedelta(seconds=1)}[variant]
            observation['received_at'] = (NOW + delta).isoformat()
        elif variant == 'wrong_tr': observation['source_tr'] = 'UNPROVEN'
        else:
            for key in ('open', 'high', 'low'):
                value[key] = 0.0
                observation['fields'][key] = {'status': 'missing', 'value': None}
    inputs, state = asyncio.run(classify_facts(tmp_path, monkeypatch, quote_transform=transform))
    assert bool(state['regime_policy']['noon_caps']) is accepted
    assert inputs['input_meta']['kospi_today_pct'] == (-3.0 if accepted else None)


@pytest.mark.parametrize('offset', [-3600, 1])
def test_kr_provenance_preserves_original_receipt_not_classification_clock(tmp_path, monkeypatch, offset):
    received = NOW + timedelta(seconds=offset)
    def transform(value): value['_observation']['received_at'] = received.isoformat()
    inputs, state = asyncio.run(classify_facts(tmp_path, monkeypatch,
        quote_transform=transform, delayed=offset > 0))
    assert inputs['input_meta']['kospi_today_pct'] == -3.0
    assert received.isoformat(timespec='seconds') in inputs['input_meta']['kr_as_of']
    assert '시장 시각 미제공' in inputs['input_meta']['kr_as_of']
    assert received.strftime('%Y-%m-%d %H:%M') in inputs['input_meta']['kospi_bars_as_of']
    for record in state['risk_sources']['records'].values():
        if record['ticket']['kind'] == 'noon_index':
            assert record['terminal']['envelope']['market_as_of'] is None


@pytest.mark.parametrize('entry', [
    {'missing': True, 'change_pct': None},
    {'missing': False, 'price': 100.0, 'change_pct': None, 'missing_fields': ['change_pct']},
])
def test_explicit_normalized_missing_cannot_fall_back_to_legacy_number(tmp_path, monkeypatch, entry):
    overnight = {'indices_normalized': {'SP500': entry}, 'indices': {'S&P500': {'change_pct': 9.0}}}
    inputs, _ = asyncio.run(classify_facts(tmp_path, monkeypatch, overnight=overnight))
    assert 'SP500' in inputs['input_meta']['missing_fields']
    assert 'S&P500: 결측' in inputs['prompt']


def test_provisional_bar_uses_receipt_time_not_later_classification_time(tmp_path, monkeypatch):
    received = NOW - timedelta(hours=1)
    def transform(value): value['_observation']['received_at'] = received.isoformat()
    inputs, _ = asyncio.run(classify_facts(tmp_path, monkeypatch, quote_transform=transform))
    assert received.strftime('%Y-%m-%d %H:%M') in inputs['input_meta']['kospi_bars_as_of']


def test_future_screener_bar_already_rejects_provisional_update(tmp_path, monkeypatch):
    inputs, _ = asyncio.run(classify_facts(tmp_path, monkeypatch,
        last_bar_date=(NOW + timedelta(days=1)).date().isoformat()))
    assert '미래' in inputs['input_meta']['kospi_bars_as_of']
    assert '당일 잠정봉' not in inputs['input_meta']['kospi_bars_as_of']


@pytest.mark.parametrize('value', [2.0, 0.0])
def test_absent_normalized_entry_preserves_legacy_alias_fallback(tmp_path, monkeypatch, value):
    overnight = {'indices': {'S&P500': {'change_pct': value}, '반도체(SOX)': {'change_pct': -1.0},
                              'VIX(공포지수)': {'value': 19.0}}}
    inputs, _ = asyncio.run(classify_facts(tmp_path, monkeypatch, overnight=overnight))
    assert f'S&P500: {value:+.2f}%' in inputs['prompt']
    assert '반도체ETF(SOX): -1.00%' in inputs['prompt']
    assert all(key not in inputs['input_meta']['missing_fields'] for key in ('SP500', 'SOX', 'VIX'))


def test_us_missing_source_times_are_not_replaced_by_classification_now(tmp_path, monkeypatch):
    overnight = {'indices_normalized': {'SP500': {'missing': False, 'change_pct': 2.0,
        'fetched_at': None, 'as_of': None}}}
    inputs, _ = asyncio.run(classify_facts(tmp_path, monkeypatch, overnight=overnight))
    assert NOW.isoformat(timespec='seconds') not in (inputs['input_meta']['us_as_of'] or '')


def test_us_all_missing_producer_shape_does_not_invent_fetch_time(tmp_path, monkeypatch):
    overnight = {'indices': {}, 'indices_normalized': {
        key: {'price': None, 'change': None, 'change_pct': None, 'fetched_at': None,
              'as_of': None, 'source': 'yahoo_finance', 'missing': True,
              'reason': 'US 시장 데이터 조회 실패', 'missing_fields': ['price', 'change_pct']}
        for key in ('SP500', 'NASDAQ', 'SOX', 'VIX')}}
    inputs, _ = asyncio.run(classify_facts(tmp_path, monkeypatch, overnight=overnight))
    assert all(key in inputs['input_meta']['missing_fields'] for key in ('SP500', 'NASDAQ', 'SOX', 'VIX'))
    assert NOW.isoformat(timespec='seconds') not in (inputs['input_meta']['us_as_of'] or '')


def test_each_consumed_us_index_keeps_its_own_market_timestamp(tmp_path, monkeypatch):
    market_a, market_b = '2026-09-17T20:00:00+00:00', '2026-09-17T21:00:00+00:00'
    overnight = {'indices_normalized': {
        'SP500': {'missing': False, 'change_pct': 2.0, 'as_of': market_a,
                  'fetched_at': (NOW - timedelta(hours=1)).isoformat()},
        'NASDAQ': {'missing': False, 'change_pct': -2.0, 'as_of': market_b,
                   'fetched_at': (NOW - timedelta(minutes=30)).isoformat()}}}
    inputs, _ = asyncio.run(classify_facts(tmp_path, monkeypatch, overnight=overnight))
    assert market_a in inputs['prompt'] and market_b in inputs['prompt']


def test_kr_two_indices_keep_distinct_receipt_times(tmp_path, monkeypatch):
    earlier = NOW - timedelta(minutes=20)
    def transform(value):
        value['_observation']['received_at'] = (earlier if value['_observation']['index_code'] == '0001' else NOW).isoformat()
    inputs, _ = asyncio.run(classify_facts(tmp_path, monkeypatch, quote_transform=transform))
    assert earlier.isoformat() in inputs['input_meta']['kr_as_of']
    assert NOW.isoformat() in inputs['input_meta']['kr_as_of']
    assert 'KOSPI' in inputs['input_meta']['kr_as_of'] and 'KOSDAQ' in inputs['input_meta']['kr_as_of']


def test_normalized_valid_zero_wins_over_conflicting_raw_alias(tmp_path, monkeypatch):
    overnight = {'indices_normalized': {'SP500': {'missing': False, 'change_pct': 0.0}},
                 'indices': {'S&P500': {'change_pct': 9.0}}}
    inputs, _ = asyncio.run(classify_facts(tmp_path, monkeypatch, overnight=overnight))
    assert 'S&P500: +0.00%' in inputs['prompt']
    assert 'SP500' not in inputs['input_meta']['missing_fields']


def test_malformed_normalized_container_stops_before_model_and_application(tmp_path, monkeypatch):
    """Post-fix positive guard; the valid model response cannot mask a call."""
    async def scenario():
        _, _, store, runtime, owner, _ = await c3_owned(tmp_path, monkeypatch)
        class Model:
            calls = 0
            async def complete_json(self, **kwargs):
                self.calls += 1
                return {'regime': 'ranging'}
        model = Model()
        try:
            receipt = await owner.classify('malformed-guard',
                inputs_provider=Inputs(malformed_us=True), llm=model)
            assert receipt.status == 'failed'
            assert model.calls == 0
            state = (await store.load())[1]
            assert not state['regime_policy']['applications']
            assert state['risk_sources']['records'][receipt.operation_id]['terminal'] is not None
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())
