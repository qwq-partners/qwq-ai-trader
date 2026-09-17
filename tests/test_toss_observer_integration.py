"""Real threaded worker + actual append-only ledger; synthetic HTTP/authority."""
import asyncio
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from test_toss_observer_service import fixture_service, read_status


@pytest.mark.parametrize('input_failure,provider_failure', [(False, False), (True, False), (False, True)])
def test_actual_worker_and_durable_ledger_agree(tmp_path, monkeypatch, input_failure, provider_failure):
    from src.observation import toss_service
    real_reconcile = toss_service.reconcile_ledger
    module, args, events, _, at = fixture_service(tmp_path, monkeypatch, input_error=input_failure)
    monkeypatch.setattr(module, 'reconcile_ledger', real_reconcile)
    authority = args['deployment'].load()
    authority.clock, authority.now = time.monotonic, lambda: at[0]
    policy = authority.plan.document
    policy.update(dataset_kind='live')
    policy['limits'].update(max_pages=2, max_retries=1)
    policy['comparison'].update(max_age_seconds=60, max_skew_seconds=5, outlier_pct=.5,
                                expected_market_basis='unknown')
    calls = []
    class Client:
        async def start(self):
            events.append('synthetic-client-start')

        async def close(self):
            events.append('synthetic-client-close')

        async def get(self, path, *, params, budget):
            assert path == '/api/v1/prices' and params == {'symbols': '087010'}
            calls.append(path)
            if provider_failure:
                raise RuntimeError('synthetic provider failure')
            return {'result': [{'symbol': '087010', 'lastPrice': '100', 'currency': 'KRW',
                                'timestamp': at[0].isoformat()}]}

    async def cached_token(**kwargs):
        return 'synthetic-unused-token'

    def app_factory(auth, stopping):
        from src.data.providers.toss.runtime_factory import ObservationApp
        from src.data.providers.toss.observation import ObservationRunner
        from src.data.providers.toss.observation_ledger import ObservationLedger
        client = Client()
        ledger = ObservationLedger(tmp_path / 'observations.jsonl', plan_hash='a'*64, max_bytes=16777216)
        runner = ObservationRunner(client=client, ledger=ledger, policy=policy, now=lambda: at[0])
        return ObservationApp(auth, stopping, client, None, SimpleNamespace(get_token=cached_token), ledger, runner)

    monkeypatch.setattr(module, 'build_app', app_factory)
    original_write = module.StatusWriter.write
    def stop_after_durable_status(writer, document):
        original_write(writer, document)
        if document['last_ledger_complete_at'] is not None:
            args['stop_event'].set()
    monkeypatch.setattr(module.StatusWriter, 'write', stop_after_durable_status)
    args.pop('worker_factory')
    async def run():
        return await asyncio.wait_for(module.run_service(**args), timeout=3)
    assert asyncio.run(run()) == 0
    status = read_status(args)
    assert status['accounting_consistent'] is True
    assert len(calls) == int(not input_failure)
    assert status['by_kind']['prices']['observations'] == int(not input_failure and not provider_failure)
    assert status['last_valid_pair_at'] is None
    assert status['production_eligible'] is False
    assert 'synthetic-client-close' in events
    rows = [json.loads(line) for line in (tmp_path / 'observations.jsonl').read_text().splitlines()]
    assert any(row['type'] == ('missed' if input_failure else 'terminal') for row in rows)


def test_archive_closure_excludes_trading_modules_and_current_keys():
    from test_toss_observer_install import load
    builder = load('package_release')
    root = Path(__file__).parents[1]
    files = [path.relative_to(root).as_posix() for path in builder.source_files(root)]
    assert not any('/brokers/' in f or '/core/' in f or 'run_trader' in f or '.env' in f for f in files)
    for package in ('src/__init__.py', 'src/data/__init__.py', 'src/data/providers/__init__.py',
                    'src/utils/__init__.py', 'src/schedulers/__init__.py'):
        assert not (root / package).read_bytes()
    assert 'src/observation/toss_service.py' in files
