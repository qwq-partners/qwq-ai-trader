"""설치 경계: 자격 분리, dry-run, 덮어쓰기와 경로 탈출 방지."""
import importlib.util
import json
import os
from pathlib import Path
from datetime import datetime, timezone

import pytest

BASE = Path(__file__).resolve().parents[1] / 'scripts/ops/toss_observer'


def load(name):
    path = BASE / (name + '.py')
    assert path.exists(), f'{name} 구현 필요'
    spec = importlib.util.spec_from_file_location('observer_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_credentials_only_two_fields_and_shell_text_is_literal():
    m = load('install')
    result = m.parse_credentials(b'KIS_APPKEY=private\nTOSS_CLIENT_ID="test-id"\nTOSS_CLIENT_SECRET=abc$()\\xyz\n')
    assert result == {'TOSS_CLIENT_ID': 'test-id', 'TOSS_CLIENT_SECRET': 'abc$()\\xyz'}
    rendered = m.credentials_bytes(result)
    assert b'KIS' not in rendered
    assert b'TOSS_CLIENT_SECRET="abc$()\\\\xyz"' in rendered


@pytest.mark.parametrize('raw', [b'TOSS_CLIENT_ID=a\nTOSS_CLIENT_ID=b\nTOSS_CLIENT_SECRET=c',
    b'TOSS_CLIENT_ID=a', b'TOSS_CLIENT_ID=a\nTOSS_CLIENT_SECRET="a b"',
    b'TOSS_CLIENT_ID=a\nTOSS_CLIENT_SECRET="line\\nnext"',
    b'TOSS_CLIENT_ID=a\nTOSS_CLIENT_SECRET="unterminated'])
def test_invalid_credentials_rejected_without_values(raw):
    m = load('install')
    with pytest.raises(m.InstallError, match='credentials_invalid'):
        m.parse_credentials(raw)


def test_exclusive_write_preserves_existing_and_symlink(tmp_path):
    m = load('install')
    path = tmp_path / 'protected'
    path.write_bytes(b'original')
    with pytest.raises(m.InstallError):
        m.write_new(path, b'new', mode=0o600)
    assert path.read_bytes() == b'original'
    alias = tmp_path / 'alias'
    alias.symlink_to(path)
    with pytest.raises(m.InstallError):
        m.write_new(alias, b'new', mode=0o600)
    assert path.read_bytes() == b'original'


def test_plan_fixed_policy_and_dates_are_explicit():
    m = load('install')
    p = m.observation_plan('test-plan', ['2026-09-18', '2026-09-21', '2026-09-22'])
    assert p['selection'] == {'candidate_limit': 0, 'max_snapshot_age_seconds': 300,
                               'max_symbols': 20, 'rule': 'score_desc_symbol_asc_holdings_first'}
    assert p['limits']['auth_max_issues'] == 4
    assert p['acceptance']['retention_days'] == 30
    assert p['dates'][-1] == '2026-09-22'
    with pytest.raises(m.InstallError):
        m.observation_plan('test-plan', ['2026-09-22', '2026-09-18'])


def test_grant_date_bounds_reject_rollover_and_unapproved_days():
    m = load('install')
    dates = ['2026-09-18', '2026-09-21', '2026-09-22']
    start = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
    end = datetime(2026, 9, 22, 9, tzinfo=timezone.utc)
    m.validate_window(dates, start, end)
    with pytest.raises(m.InstallError):
        m.validate_window(dates, start, datetime(2026, 9, 22, 5, tzinfo=timezone.utc))
    with pytest.raises(m.InstallError):
        m.validate_window(dates, start, datetime(2026, 9, 25, 9, tzinfo=timezone.utc))


def test_dry_run_does_not_read_credentials_or_mutate(tmp_path, monkeypatch):
    m = load('install')
    monkeypatch.setattr(m, 'inspect_artifact', lambda path: {'release_id': 'abc', 'artifact_sha256': 'a' * 64})
    monkeypatch.setattr(m, 'check_targets', lambda digest: None)
    # The installed service account is external host state; this dry-run
    # contract requires the installer to see the not-yet-provisioned state.
    def missing_account(name):
        raise KeyError(name)
    monkeypatch.setattr(m.pwd, 'getpwnam', missing_account)
    monkeypatch.setattr(m.grp, 'getgrnam', missing_account)
    monkeypatch.setattr(m, 'read_credentials', lambda *a: pytest.fail('자격 읽기 금지'))
    monkeypatch.setattr(m, 'provision', lambda *a, **k: pytest.fail('변경 금지'))
    result = m.install(artifact=tmp_path, credential_source=tmp_path / 'not-read',
        plan_id='p', grant_id='g', dates=['2026-09-18', '2026-09-21', '2026-09-22'],
        expires_at=datetime(2026, 9, 22, 9, tzinfo=timezone.utc),
        now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc))
    assert result == {'ready': True, 'applied': False, 'artifact_sha256': 'a' * 64}
    assert not list(tmp_path.iterdir())


def test_existing_service_account_never_repurposed(monkeypatch):
    m = load('install')
    monkeypatch.setattr(m.pwd, 'getpwnam', lambda name: object())
    monkeypatch.setattr(m, 'run_command', lambda *args: pytest.fail('기존 계정 수정 금지'))
    with pytest.raises(m.InstallError, match='service_user_exists'):
        m.create_user()


def test_existing_service_group_never_repurposed(monkeypatch):
    m = load('install')
    def missing_account(name):
        raise KeyError(name)
    monkeypatch.setattr(m.pwd, 'getpwnam', missing_account)
    monkeypatch.setattr(m.grp, 'getgrnam', lambda name: object())
    monkeypatch.setattr(m, 'run_command', lambda *args: pytest.fail('기존 그룹 수정 금지'))
    with pytest.raises(m.InstallError, match='service_group_exists'):
        m.create_user()


def test_new_directories_do_not_depend_on_installer_umask(tmp_path, monkeypatch):
    m = load('install')
    monkeypatch.setattr(m.os, 'chown', lambda *args: None)
    old = os.umask(0o077)
    try:
        m.make_directory(tmp_path / 'parent/child')
    finally:
        os.umask(old)
    assert (tmp_path / 'parent').stat().st_mode & 0o777 == 0o755
    assert (tmp_path / 'parent/child').stat().st_mode & 0o777 == 0o755


def test_install_wrong_artifact_digest_does_not_read_credentials(tmp_path, monkeypatch):
    m = load('install')
    monkeypatch.setattr(m, 'inspect_artifact', lambda _: {'artifact_sha256': 'a' * 64})
    monkeypatch.setattr(m, 'read_credentials', lambda *args: pytest.fail('자격 접근 금지'))
    with pytest.raises(m.InstallError, match='artifact_mismatch'):
        m.install(artifact=tmp_path, credential_source=tmp_path / 'secret', plan_id='p', grant_id='g',
            dates=['2026-09-18', '2026-09-21', '2026-09-22'],
            now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
            expires_at=datetime(2026, 9, 22, 9, tzinfo=timezone.utc), apply=True,
            expected_digest='b' * 64)


def test_artifact_build_contract_and_symlink_rejection(tmp_path, monkeypatch):
    m = load('install')
    artifact = tmp_path / 'artifact'
    artifact.mkdir()
    (artifact / 'manifest.json').symlink_to(tmp_path / 'missing')
    with pytest.raises(m.InstallError):
        m.inspect_artifact(artifact)


def test_real_temp_layout_binds_plan_and_keeps_credential_scope(tmp_path, monkeypatch):
    import hashlib
    import sys
    from types import SimpleNamespace
    from src.data.providers.toss.approval import ObservationPlan, LiveObservationGrant
    m = load('install')
    # 실제 root 작업 대신 계정 생성/chown만 차단; 파일 게시와 계약 생산은 실제 구현.
    monkeypatch.setattr(m, 'PREFIX', tmp_path / 'root')
    monkeypatch.setattr(m, 'create_user', lambda: (os.getuid(), os.getgid()))
    monkeypatch.setattr(m.os, 'chown', lambda *args: None)
    canon = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    contract = SimpleNamespace(canonical_bytes=canon, service_policy=lambda: {},
        configuration_hash=lambda value: hashlib.sha256(canon({k: v for k, v in value.items() if k != 'config_hash'})).hexdigest())
    monkeypatch.setattr(m, 'helper', lambda: contract)
    staging = tmp_path / 'stage'
    (staging / 'bootstrap').mkdir(parents=True)
    payload = b'# synthetic stdlib launcher\n'
    (staging / 'bootstrap/launcher.py').write_bytes(payload)
    manifest = {'schema_version': 1, 'release_id': 'test',
        'python_version': '.'.join(map(str, sys.version_info[:3])), 'import_paths': ['app', 'deps'],
        'files': [{'path': 'bootstrap/launcher.py', 'size': len(payload), 'mode': 0o644,
                   'sha256': hashlib.sha256(payload).hexdigest()}]}
    info = {'manifest': manifest, 'release_id': 'test', 'artifact_sha256': hashlib.sha256(canon(manifest)).hexdigest()}
    (tmp_path / 'root/etc/systemd/system').mkdir(parents=True)
    plan = m.observation_plan('test', ['2026-09-18', '2026-09-21', '2026-09-22'])
    result = m.provision(staging, info, plan, {'TOSS_CLIENT_ID': 'test-client', 'TOSS_CLIENT_SECRET': 'synthetic'},
        grant_id='test-grant', now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
        expires_at=datetime(2026, 9, 22, 9, tzinfo=timezone.utc))
    base = tmp_path / 'root/etc/qwq-toss-observer'
    loaded = ObservationPlan.from_bytes((base / 'plan.json').read_bytes())
    grant = LiveObservationGrant.from_document(json.loads((base / 'registry.json').read_bytes())['grants'][0])
    assert loaded.raw_hash == loaded.canonical_hash == result['plan_raw_hash'] == grant.plan_raw_hash
    assert grant.config_hash == result['config_hash']
    assert result['retention_at'] == '2026-10-22T09:00:00+00:00'
    assert (base / 'credentials.env').stat().st_mode & 0o777 == 0o600
    assert list((tmp_path / 'root/var/lib/qwq-toss-observer/tokens').iterdir()) == []
    with pytest.raises((m.InstallError, FileExistsError)):
        m.provision(staging, info, plan, {'TOSS_CLIENT_ID': 'different', 'TOSS_CLIENT_SECRET': 'different'},
            grant_id='another', now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
            expires_at=datetime(2026, 9, 22, 9, tzinfo=timezone.utc))
    assert b'synthetic' in (base / 'credentials.env').read_bytes()
