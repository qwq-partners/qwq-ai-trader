"""Actual KIS index adapter provenance; HTTP/auth/limiter are explicit fake boundaries."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.data.providers.kis_market_data import KISMarketData


NOW = datetime(2026, 9, 18, 1, tzinfo=timezone.utc)


async def provider(monkeypatch, output, *, http_status=200, rt_cd='0'):
    calls = {'get': 0, 'limiter': 0}
    class Response:
        status = http_status
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def json(self): return {'rt_cd': rt_cd, 'output': output}
    class Session:
        closed = False
        def get(self, url, *, headers, params):
            calls['get'] += 1
            assert headers['tr_id'] == 'FHPUP02100000'
            assert params == {'FID_COND_MRKT_DIV_CODE': 'U', 'FID_INPUT_ISCD': '0001'}
            return Response()
    async def headers(tr): return {'tr_id': tr}
    async def acquire(): calls['limiter'] += 1
    instance = KISMarketData(token_manager=SimpleNamespace(base_url='https://synthetic.invalid'))
    instance._session = Session()
    monkeypatch.setattr(instance, '_get_headers', headers)
    monkeypatch.setattr('src.data.providers.kis_market_data.kis_rate_limit.acquire', acquire)
    # Explicit source receipt clock only; existing cache TTL still uses its own clock.
    monkeypatch.setattr(instance, '_index_clock', lambda: NOW, raising=False)
    return instance, calls


def raw():
    return {'bstp_nmix_prpr': '3000.125', 'bstp_nmix_prdy_vrss': '-40.125',
            'bstp_nmix_prdy_ctrt': '-1.499', 'bstp_nmix_oprc': '3020.0',
            'bstp_nmix_hgpr': '3040.0', 'bstp_nmix_lwpr': '2980.0'}


@pytest.mark.parametrize('new_status', [200, 503])
def test_older_inflight_response_cannot_replace_cache_after_newer_attempt(monkeypatch, new_status):
    async def scenario():
        reached, release = asyncio.Event(), asyncio.Event()
        md, calls = await provider(monkeypatch, raw())
        class Response:
            def __init__(self, number):
                self.number = number
                self.status = 200 if number == 1 else new_status
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def json(self):
                if self.number == 1:
                    reached.set(); await release.wait()
                output = raw()
                output['bstp_nmix_prdy_ctrt'] = '0' if self.number == 1 else '-3'
                return {'rt_cd': '0', 'output': output}
        class Session:
            closed = False
            def get(self, *args, **kwargs):
                calls['get'] += 1
                return Response(calls['get'])
        md._session = Session()
        old = asyncio.create_task(md.fetch_index_price('0001'))
        await asyncio.wait_for(reached.wait(), 2)
        try:
            latest = await md.fetch_index_price('0001')
            release.set()
            assert (await old)['change_pct'] == 0.0
            if new_status == 200:
                cached = await md.fetch_index_price('0001')
                assert cached['_observation'] == latest['_observation']
                assert cached['change_pct'] == -3.0
                assert calls == {'get': 2, 'limiter': 2}
            else:
                assert latest is None
                assert 'index_price_0001' not in md._cache
        finally:
            release.set(); await asyncio.gather(old, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize('missing', ['absent', None, ''])
def test_real_adapter_preserves_missing_percent_without_promoting_legacy_zero(monkeypatch, missing):
    async def scenario():
        output = raw()
        if missing == 'absent': del output['bstp_nmix_prdy_ctrt']
        else: output['bstp_nmix_prdy_ctrt'] = missing
        md, calls = await provider(monkeypatch, output)
        quote = await md.fetch_index_price('0001')
        assert quote['change_pct'] == 0.0  # legacy numeric domain remains unchanged
        field = quote['_observation']['fields']['change_pct']
        assert field['status'] == 'missing' and field['value'] is None
        assert calls == {'get': 1, 'limiter': 1}
    asyncio.run(scenario())


@pytest.mark.parametrize('invalid', [False, True, float('nan'), float('inf')])
def test_real_adapter_does_not_certify_bool_or_nonfinite(monkeypatch, invalid):
    async def scenario():
        output = raw()
        output['bstp_nmix_prdy_ctrt'] = invalid
        md, _ = await provider(monkeypatch, output)
        quote = await md.fetch_index_price('0001')
        field = quote['_observation']['fields']['change_pct']
        assert field['status'] == 'invalid' and field['value'] is None
    asyncio.run(scenario())


def test_cache_preserves_original_receipt_and_identity_without_extra_get(monkeypatch):
    async def scenario():
        md, calls = await provider(monkeypatch, raw())
        first = await md.fetch_index_price('0001')
        observation = first['_observation']
        assert observation['market_as_of'] is None
        assert datetime.fromisoformat(observation['received_at']) == NOW
        assert observation['fields']['change_pct'] == {'status': 'valid', 'value': -1.5}
        assert observation['source_tr'] == 'FHPUP02100000' and observation['index_code'] == '0001'
        assert observation['observation_id']
        monkeypatch.setattr(md, '_index_clock', lambda: NOW + timedelta(seconds=5))
        second = await md.fetch_index_price('0001')
        assert second['_observation'] == observation
        assert calls == {'get': 1, 'limiter': 1}
        # New actual response, unlike cache read, has its own receipt/identity.
        md.clear_cache()
        third = await md.fetch_index_price('0001')
        assert third['_observation']['observation_id'] != observation['observation_id']
        assert datetime.fromisoformat(third['_observation']['received_at']) == NOW + timedelta(seconds=5)
        assert calls == {'get': 2, 'limiter': 2}
    asyncio.run(scenario())


def test_caller_mutation_does_not_edit_cached_original_observation(monkeypatch):
    async def scenario():
        md, _ = await provider(monkeypatch, raw())
        first = await md.fetch_index_price('0001')
        first['_observation']['fields']['change_pct']['value'] = 10.0
        first['price'] = 1.0
        second = await md.fetch_index_price('0001')
        assert second['price'] == 3000.12
        assert second['_observation']['fields']['change_pct']['value'] == -1.5
    asyncio.run(scenario())


def test_actual_index_cache_is_not_mutable_through_returned_quote(monkeypatch):
    async def scenario():
        md, calls = await provider(monkeypatch, raw())
        first = await md.fetch_index_price('0001')
        first['price'] = 1.0
        second = await md.fetch_index_price('0001')
        assert second['price'] == 3000.12
        assert calls == {'get': 1, 'limiter': 1}
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['http', 'api', 'empty', 'bad_numeric', 'bad_price'])
def test_real_failures_do_not_create_cached_success_or_extra_query(monkeypatch, fault):
    async def scenario():
        output = raw()
        if fault == 'empty': output = {}
        if fault == 'bad_numeric': output['bstp_nmix_prdy_ctrt'] = 'not-a-number'
        if fault == 'bad_price': output['bstp_nmix_prpr'] = '0'
        md, calls = await provider(monkeypatch, output,
                                  http_status=503 if fault == 'http' else 200,
                                  rt_cd='1' if fault == 'api' else '0')
        assert await md.fetch_index_price('0001') is None
        assert md._cache == {} and calls == {'get': 1, 'limiter': 1}
    asyncio.run(scenario())


@pytest.mark.parametrize('field', ['price', 'open', 'high', 'low', 'change', 'change_pct'])
def test_individual_missing_fields_are_not_certified_as_valid_zero(field):
    from src.data.providers.kis_index_observation import build_index_observation, INDEX_FIELDS
    output = raw()
    del output[INDEX_FIELDS[field]]
    observation = build_index_observation(output, '0001', received_at=NOW)
    assert observation['fields'][field] == {'status': 'missing', 'value': None}
    assert all(row['status'] == 'valid' for name, row in observation['fields'].items() if name != field)


@pytest.mark.parametrize('value', [False, True, 'NaN', 'Infinity', '-Infinity', {}, [], 10**400])
def test_metadata_has_only_finite_json_values_and_no_raw_response(value):
    import json
    from src.data.providers.kis_index_observation import build_index_observation
    output = raw()
    output['bstp_nmix_prdy_ctrt'] = value
    output['unrelated'] = 'do-not-copy-unrelated-payload'
    observation = build_index_observation(output, '0001', received_at=NOW)
    assert observation['fields']['change_pct'] == {'status': 'invalid', 'value': None}
    assert 'do-not-copy' not in json.dumps(observation, allow_nan=False)


@pytest.mark.parametrize('received_at', [None, NOW.replace(tzinfo=None), '2026-09-18'])
def test_receipt_requires_explicit_aware_clock(received_at):
    from src.data.providers.kis_index_observation import build_index_observation
    with pytest.raises(ValueError, match='aware_index_receipt_required'):
        build_index_observation(raw(), '0001', received_at=received_at)


def test_unverified_raw_date_time_fields_do_not_create_market_timestamp():
    from src.data.providers.kis_index_observation import build_index_observation
    output = {**raw(), 'stck_bsop_date': '20260918', 'stck_cntg_hour': '100000'}
    observation = build_index_observation(output, '0001', received_at=NOW)
    assert observation['market_as_of'] is None
