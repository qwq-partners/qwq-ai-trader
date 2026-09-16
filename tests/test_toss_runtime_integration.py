"""Approved synthetic grant→real worker/auth/OAuth/GET/ledger, fake HTTP only.

The registry fixture hardens /tmp metadata in-memory and supplies synthetic
service identity; this is not evidence of real deployment permissions.
"""
import asyncio
from datetime import datetime
from decimal import Decimal
import json
import threading

import pytest

from test_toss_live_authority import authority_fixture
from test_toss_http_body import Response
from src.data.providers.toss import oauth, transport
from src.data.providers.toss.market_types import Quote
from src.data.providers.toss.runtime import TossWorker, WorkerCommand
from src.data.providers.toss.runtime_factory import build_app


def test_real_chain_is_lazy_observation_only_and_restart_is_not_resend(tmp_path, monkeypatch):
    monkeypatch.setenv('TOSS_API', '1')
    mod, kwargs, grant, stamp, ticks = authority_fixture(tmp_path, monkeypatch)
    requests, credentials, sessions = [], [], []
    parent_thread = threading.get_ident()

    def credential_loader():
        credentials.append(threading.get_ident())
        return oauth.Credentials(grant['client_identity'], 'synthetic-secret')

    class Session:
        def __init__(self, **options):
            assert options['trust_env'] is False
            assert options['auto_decompress'] is False
            self._retry_connection = True
            self.closed = False
            sessions.append(self)

        def request(self, method, url, **options):
            assert self._retry_connection is False
            assert options['ssl'] is True and options['allow_redirects'] is False
            requests.append((method, url, threading.get_ident()))
            if method == 'POST':
                assert url.endswith('/oauth2/token')
                body = {'access_token': 'synthetic-runtime-token', 'token_type': 'Bearer', 'expires_in': 86400}
            else:
                assert url.endswith('/api/v1/prices')
                body = {'result': [{'symbol': '005930', 'lastPrice': '100', 'currency': 'KRW',
                                     'timestamp': stamp[0].isoformat()}]}
            return Response(json.dumps(body).encode())

        async def close(self):
            self.closed = True

    monkeypatch.setattr(oauth, 'environment_credentials', credential_loader)
    monkeypatch.setattr(transport.aiohttp, 'ClientSession', Session)

    async def run():
        from src.data.providers.toss.observation import select_snapshot
        from src.data.providers.toss.observation_ledger import ObservationLedger
        authority = mod.load_authority(**kwargs)
        worker = TossWorker(lambda stop: build_app(authority, stop), ownership_key='runtime-integration')
        assert requests == [] and credentials == [] and sessions == []
        assert not (tmp_path / 'tokens').exists() and not (tmp_path / 'ledger.jsonl').exists()
        await worker.start(timeout=5)
        kis = Quote('005930', Decimal('100'), None, None, 'partial',
                     frozenset({'observed_at', 'fetched_at'}), 'unknown', 'KRW')
        snapshot = select_snapshot(candidates=(('005930', 90),), holdings=('005930',),
                                   source_success_at=stamp[0], now=stamp[0],
                                   policy=authority.plan.document, kis_quotes={'005930': kis})
        command = WorkerCommand('prices', '2026-09-16T09:00:00+09:00', snapshot)
        result = await asyncio.wrap_future(worker.submit(command))
        assert result.outcome == 'success' and result.observation_count == 1
        assert result.valid_pairs == 0 and result.ledger_complete
        assert result.production_eligible is False
        assert await worker.stop(timeout=5) == 'closed'
        assert all(session.closed for session in sessions)
        # Normal restart uses the cached token, not a second bootstrap grant.
        restarted_authority = mod.load_authority(**kwargs)
        restarted = TossWorker(lambda stop: build_app(restarted_authority, stop), ownership_key='runtime-integration')
        await restarted.start(timeout=5)
        repeated = await asyncio.wrap_future(restarted.submit(command))
        assert repeated.outcome == 'duplicate'
        assert await restarted.stop(timeout=5) == 'closed'
        summary = ObservationLedger.read_only_summary(tmp_path / 'ledger.jsonl',
                    plan_hash=authority.plan.canonical_hash, max_bytes=1_000_000)
        assert summary['selected_attempts'] == 1 and summary['terminal_attempts'] == 1
        assert summary['valid_pairs'] == 0 and not summary['incomplete']
        assert summary['expected_slots'] is not None
        return result

    asyncio.run(run())
    assert [method for method, _, _ in requests] == ['POST', 'GET']
    assert len(credentials) == 1
    assert all(thread != parent_thread for thread in credentials)
    assert all(thread != parent_thread for _, _, thread in requests)


def test_master_flag_revoked_before_worker_start_has_zero_credentials_or_files(tmp_path, monkeypatch):
    mod, kwargs, _, _, _ = authority_fixture(tmp_path, monkeypatch)
    authority = mod.load_authority(**kwargs)
    monkeypatch.setenv('TOSS_API', '0')
    with pytest.raises(Exception, match='stopping'):
        build_app(authority, threading.Event())
    assert not (tmp_path / 'tokens').exists()
    assert not (tmp_path / 'sender.lock').exists()
    assert not (tmp_path / 'ledger.jsonl').exists()


def test_runtime_rejects_credentials_for_an_unapproved_client_before_post(tmp_path, monkeypatch):
    from src.data.providers.toss.token_store import TokenError
    mod, kwargs, grant, _, _ = authority_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv('TOSS_API', '1')
    calls = []
    def credentials():
        calls.append('key_loaded')
        return oauth.Credentials('different-synthetic-client', 'synthetic-secret')
    class Session:
        def __init__(self, **kwargs):
            self._retry_connection = True
        def request(self, method, url, **kwargs):
            calls.append('POST')
            return Response(json.dumps({'access_token': 'synthetic-token',
                'token_type': 'Bearer', 'expires_in': 86400}).encode())
        async def close(self): pass
    monkeypatch.setattr(oauth, 'environment_credentials', credentials)
    monkeypatch.setattr(transport.aiohttp, 'ClientSession', Session)
    async def exercise():
        app = build_app(mod.load_authority(**kwargs), threading.Event())
        assert calls == []
        try:
            with pytest.raises(TokenError) as failure:
                await app.start()
            assert str(failure.value) == 'issuance_unknown'
        finally:
            await app.close()
    asyncio.run(exercise())
    assert calls == ['key_loaded']
