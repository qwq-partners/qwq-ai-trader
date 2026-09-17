"""기한·비실행·inode 검증 후 승인 원장만 삭제한다."""
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import pytest


def load():
    path = Path(__file__).resolve().parents[1] / 'scripts/ops/toss_observer/retention.py'
    assert path.exists(), 'retention 구현 필요'
    spec = importlib.util.spec_from_file_location('observer_retention', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cohort(tmp_path):
    tmp_path.chmod(0o700)
    token = tmp_path / 'tokens'
    token.mkdir(mode=0o700)
    (token / 'protected').write_bytes(b'protected')
    directory = tmp_path / 'cohorts' / ('a' * 64)
    directory.mkdir(parents=True, mode=0o700)
    directory.parent.chmod(0o700)
    ledger = directory / 'observations.jsonl'
    ledger.write_bytes(b'observations\n')
    ledger.chmod(0o600)
    sender = tmp_path / 'sender.lock'
    sender.touch(mode=0o600)
    return {'state_directory': str(tmp_path), 'ledger_path': str(ledger),
        'sender_lock_path': str(sender), 'plan_canonical_hash': 'a' * 64,
        'service_uid': os.getuid(), 'service_gid': os.getgid(),
        'retention_at': '2026-10-22T09:00:00+00:00', 'grant_id': 'g', 'config_hash': 'b' * 64}


def test_retention_only_deletes_ledger_and_writes_receipt(cohort):
    m = load()
    result = m.run_retention(cohort, now=datetime(2026, 10, 23, tzinfo=timezone.utc),
                             service_active=lambda: False, apply=True)
    assert result['deleted'] is True
    assert not Path(cohort['ledger_path']).exists()
    assert (Path(cohort['state_directory']) / 'tokens/protected').read_bytes() == b'protected'
    assert Path(cohort['sender_lock_path']).exists()
    assert Path(cohort['ledger_path']).with_name('retention.jsonl').exists()


@pytest.mark.parametrize('change', ['early', 'active', 'wrong_uid', 'symlink', 'escape', 'corrupt_receipt'])
def test_retention_refuses_unsafe_cleanup(cohort, change):
    m = load()
    now = datetime(2026, 10, 23, tzinfo=timezone.utc)
    original = Path(cohort['ledger_path'])
    if change == 'early':
        now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    elif change == 'wrong_uid':
        cohort['service_uid'] += 1
    elif change == 'symlink':
        moved = original.with_name('original')
        original.rename(moved)
        original.symlink_to(moved)
    elif change == 'escape':
        cohort['ledger_path'] = str(original.parent / '../observations.jsonl')
    elif change == 'corrupt_receipt':
        original.with_name('retention.jsonl').write_bytes(b'broken')
    with pytest.raises(m.RetentionError):
        m.run_retention(cohort, now=now, service_active=lambda: change == 'active', apply=True)
    assert original.read_bytes() == b'observations\n'


def test_retention_dry_run_has_no_writes(cohort):
    m = load()
    before = sorted(Path(cohort['state_directory']).rglob('*'))
    result = m.run_retention(cohort, now=datetime(2026, 10, 23, tzinfo=timezone.utc),
                             service_active=lambda: False)
    assert result['deleted'] is False
    assert sorted(Path(cohort['state_directory']).rglob('*')) == before


def test_completed_retention_is_idempotent_but_recreated_ledger_refused(cohort):
    m = load()
    args = dict(now=datetime(2026, 10, 23, tzinfo=timezone.utc), service_active=lambda: False, apply=True)
    m.run_retention(cohort, **args)
    assert m.run_retention(cohort, **args) == {'eligible': True, 'deleted': False, 'already_deleted': True}
    Path(cohort['ledger_path']).write_bytes(b'new_data')
    with pytest.raises(m.RetentionError):
        m.run_retention(cohort, **args)
    assert Path(cohort['ledger_path']).read_bytes() == b'new_data'


def test_receipt_fsync_failure_preserves_ledger(cohort, monkeypatch):
    m = load()
    monkeypatch.setattr(m.os, 'fsync', lambda fd: (_ for _ in ()).throw(OSError('disk')))
    with pytest.raises(m.RetentionError):
        m.run_retention(cohort, now=datetime(2026, 10, 23, tzinfo=timezone.utc),
                        service_active=lambda: False, apply=True)
    assert Path(cohort['ledger_path']).read_bytes() == b'observations\n'


def test_sender_lock_blocks_cleanup(cohort):
    import fcntl
    m = load()
    with open(cohort['sender_lock_path'], 'r+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(m.RetentionError):
            m.run_retention(cohort, now=datetime(2026, 10, 23, tzinfo=timezone.utc),
                            service_active=lambda: False, apply=True)
    assert Path(cohort['ledger_path']).exists()
