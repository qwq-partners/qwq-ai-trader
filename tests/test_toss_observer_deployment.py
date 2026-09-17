"""시작 영수증의 단일 수명·실패 후 보존 계약."""
import importlib
import os
from pathlib import Path
import stat
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from test_toss_observer_launcher import approved_release, sealed_release


def api():
    try:
        return importlib.import_module('src.observation.toss_deployment')
    except ModuleNotFoundError:
        pytest.fail('deployment 구현 없음', pytrace=False)


@pytest.fixture
def receipt_fixture(tmp_path, monkeypatch):
    module = api()
    starts = tmp_path / 'starts'
    starts.mkdir(mode=0o700)
    original = os.fstat
    def trusted_parents(fd):
        result = original(fd)
        name = os.readlink(f'/proc/self/fd/{fd}')
        if name != str(tmp_path) and not name.startswith(str(tmp_path) + '/'):
            values = list(result)
            values[4] = values[5] = 0
            if name == '/tmp':
                values[0] = stat.S_IFDIR | 0o755
            return os.stat_result(values)
        return result
    monkeypatch.setattr(os, 'fstat', trusted_parents)
    document = dict(grant_id='synthetic-grant', receipt_directory=str(starts),
        state_directory=str(tmp_path), service_uid=os.geteuid(), service_gid=os.getegid(),
        release_id='synthetic-release', config_hash='a' * 64, artifact_sha256='b' * 64,
        plan_raw_hash='c' * 64, plan_canonical_hash='d' * 64,
        client_identity='synthetic-client', host_identity='synthetic-host',
        token_directory=str(tmp_path / 'tokens'), sender_lock_path=str(tmp_path / 'sender.lock'),
        ledger_path=str(tmp_path / 'ledger.jsonl'))
    authority = SimpleNamespace(grant=SimpleNamespace(**document),
        plan=SimpleNamespace(raw_hash='c' * 64, canonical_hash='d' * 64),
        authority_hash='e' * 64, clock=lambda: 100,
        require=lambda *args, **kwargs: 105)
    return module, document, authority


def test_grant_start_is_durable_and_reuse_is_rejected(receipt_fixture):
    module, document, authority = receipt_fixture
    module.claim_start(document, authority)
    entries = list(Path(document['receipt_directory']).iterdir())
    assert len(entries) == 1 and entries[0].stat().st_mode & 0o777 == 0o600
    with pytest.raises(Exception):
        module.claim_start(document, authority)
    assert len(list(entries[0].parent.iterdir())) == 1


@pytest.mark.parametrize('failed_call', [1, 2])
def test_grant_start_is_consumed_after_fsync_failure(receipt_fixture, monkeypatch, failed_call):
    module, document, authority = receipt_fixture
    original = os.fsync
    calls = []
    def failing(fd):
        calls.append(fd)
        if len(calls) == failed_call:
            raise OSError('synthetic fsync failure')
        original(fd)
    monkeypatch.setattr(os, 'fsync', failing)
    with pytest.raises(Exception):
        module.claim_start(document, authority)
    assert len(list(Path(document['receipt_directory']).iterdir())) == 1
    with pytest.raises(Exception):
        module.claim_start(document, authority)


def test_expired_authority_cannot_claim(receipt_fixture):
    module, document, authority = receipt_fixture
    def expired(*args, **kwargs):
        raise RuntimeError('approval_expired')
    authority.require = expired
    with pytest.raises(Exception):
        module.claim_start(document, authority)
    assert list(Path(document['receipt_directory']).iterdir()) == []


def test_wrong_grant_cannot_consume_another_receipt(receipt_fixture):
    module, document, authority = receipt_fixture
    document['grant_id'] = 'other'
    with pytest.raises(Exception):
        module.claim_start(document, authority)
    assert list(Path(document['receipt_directory']).iterdir()) == []


def test_competing_starts_only_one_returns_success(receipt_fixture):
    module, document, authority = receipt_fixture
    def attempt(_):
        try:
            module.claim_start(document, authority)
            return True
        except Exception:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert results.count(True) == 1
    assert len(list(Path(document['receipt_directory']).iterdir())) == 1


@pytest.mark.parametrize('change', ['symlink', 'permissions', 'grant_path', 'uid'])
def test_receipt_refuses_unsafe_target(receipt_fixture, change):
    module, document, authority = receipt_fixture
    path = Path(document['receipt_directory'])
    if change == 'symlink':
        path.rename(path.parent / 'original')
        path.symlink_to(path.parent / 'original')
    elif change == 'permissions':
        path.chmod(0o777)
    elif change == 'uid':
        document['service_uid'] += 1
    else:
        document['receipt_directory'] = str(path.parent / 'other-starts')
    with pytest.raises(Exception):
        module.claim_start(document, authority)
    assert list(path.iterdir()) == []


def test_make_deployment_loads_real_authority_without_claiming_or_credentials(approved_release, monkeypatch):
    module = api()
    _, document, *_ = approved_release
    from src.data.providers.toss import oauth
    monkeypatch.setattr(oauth, 'environment_credentials', lambda: pytest.fail('credential accessor 호출'))
    deployment = module.make_deployment(document)
    authority = deployment.load()
    assert authority.grant.grant_id == 'synthetic-grant'
    assert authority.plan.document['selection']['candidate_limit'] == 0
    assert list(Path(document['state_directory']).iterdir()) == []


@pytest.mark.parametrize('change', ['expired', 'config', 'grant', 'capabilities'])
def test_make_deployment_rejects_unapproved_grant(approved_release, change):
    module = api()
    launcher, document, plan, grant = approved_release
    if change == 'expired':
        grant['expires_at'] = datetime.now(timezone.utc).isoformat()
    elif change == 'config':
        grant['config_hash'] = 'f' * 64
    elif change == 'grant':
        document['grant_id'] = 'another'
    else:
        grant['capabilities']['bootstrap'] = False
    Path(document['registry_path']).write_bytes(launcher.canonical_bytes(dict(schema_version=1, grants=[grant])))
    with pytest.raises(Exception):
        module.make_deployment(document)
    assert list(Path(document['state_directory']).iterdir()) == []


def test_make_deployment_rejects_broader_plan_even_if_registry_binds_it(approved_release):
    import hashlib
    module = api()
    launcher, document, plan, grant = approved_release
    plan['limits']['auth_max_issues'] = 5
    raw = launcher.canonical_bytes(plan)
    Path(document['plan_path']).write_bytes(raw)
    document['plan_raw_hash'] = document['plan_canonical_hash'] = hashlib.sha256(raw).hexdigest()
    grant.update(plan_raw_hash=document['plan_raw_hash'], plan_canonical_hash=document['plan_canonical_hash'])
    Path(document['registry_path']).write_bytes(launcher.canonical_bytes(dict(schema_version=1, grants=[grant])))
    with pytest.raises(Exception):
        module.make_deployment(document)
