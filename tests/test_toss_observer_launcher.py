"""합성 릴리스로 검증 전 실행·파일 탈출 경계를 검사한다."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest


LAUNCHER = Path(__file__).parents[1] / 'scripts/ops/toss_observer/launcher.py'


def api():
    if not LAUNCHER.exists():
        pytest.fail('launcher 구현 없음', pytrace=False)
    spec = importlib.util.spec_from_file_location('observer_test_launcher', LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sealed_release(tmp_path, monkeypatch):
    module = api()
    monkeypatch.setattr(os, 'getgroups', lambda: [os.getgid()])
    config = tmp_path / 'etc'
    releases = tmp_path / 'releases'
    state = tmp_path / 'state'
    for path in (config, releases, state):
        path.mkdir(mode=0o755)
    monkeypatch.setattr(module, 'CONFIG_DIRECTORY', config)
    monkeypatch.setattr(module, 'RELEASES_DIRECTORY', releases)
    monkeypatch.setattr(module, 'STATE_DIRECTORY', state)
    # 테스트 사용자 파일만 운영 root 파일로 모사한다. production 우회 옵션은 없다.
    original = os.fstat
    root = str(tmp_path)
    def trusted_metadata(fd):
        result = original(fd)
        name = os.readlink(f'/proc/self/fd/{fd}')
        if name == '/tmp' or name.startswith('/tmp/pytest-of-') or name.startswith(root):
            values = list(result)
            values[4] = values[5] = 0
            if name == '/tmp':
                values[0] = stat.S_IFDIR | 0o755
            return os.stat_result(values)
        return result
    monkeypatch.setattr(os, 'fstat', trusted_metadata)
    release = releases / 'staging'
    release.mkdir()
    release.chmod(0o755)
    for relative in ('app', 'deps', 'bootstrap'):
        (release / relative).mkdir(parents=True, mode=0o755)
        (release / relative).chmod(0o755)
    (release / 'app/probe.py').write_text('VALUE = 1\n')
    (release / 'bootstrap/launcher.py').write_bytes(LAUNCHER.read_bytes())
    (release / 'app/probe.py').chmod(0o644)
    (release / 'bootstrap/launcher.py').chmod(0o644)
    trusted_launcher = tmp_path / 'launcher.py'
    trusted_launcher.write_bytes(LAUNCHER.read_bytes())
    trusted_launcher.chmod(0o644)
    monkeypatch.setattr(module, 'TRUSTED_LAUNCHER', trusted_launcher)
    manifest = module.build_manifest(release, release_id='synthetic-release',
                                     python_version='.'.join(map(str, sys.version_info[:3])))
    digest = hashlib.sha256(module.canonical_bytes(manifest)).hexdigest()
    (release / 'manifest.json').write_bytes(module.canonical_bytes(manifest))
    (release / 'manifest.json').chmod(0o644)
    destination = releases / digest
    release.rename(destination)
    document = dict(schema_version=1, release_id='synthetic-release', artifact_sha256=digest,
        release_root=str(destination), manifest_path=str(destination / 'manifest.json'),
        registry_path=str(config / 'registry.json'), plan_path=str(config / 'plan.json'),
        grant_id='synthetic-grant', config_hash='', client_identity='synthetic-client',
        host_identity=socket.gethostname(), service_uid=os.getuid(), service_gid=os.getgid(),
        service_gids=sorted(set(os.getgroups())), state_directory=str(state),
        token_directory=str(state / 'tokens'), sender_lock_path=str(state / 'sender.lock'),
        ledger_path=str(state / 'cohorts' / ('a' * 64) / 'observations.jsonl'),
        status_path=str(state / 'cohorts' / ('a' * 64) / 'status.json'),
        receipt_directory=str(state / 'starts'), retention_at='2026-10-22T09:00:00+00:00',
        policy=module.service_policy(), plan_raw_hash='b' * 64, plan_canonical_hash='a' * 64)
    document['config_hash'] = module.configuration_hash(document)
    return module, document


@pytest.fixture
def approved_release(sealed_release):
    from test_toss_live_authority import plan_document
    module, document = sealed_release
    plan = plan_document()
    plan.update(dates=['2026-09-18', '2026-09-21', '2026-09-22'], calendar_time='08:55',
        sessions=[dict(name='regular', start='09:00', end='15:20')])
    plan['selection'].update(candidate_limit=0, max_symbols=20)
    plan['limits'].update(job_timeout_seconds=20, cleanup_timeout_seconds=10,
        preflight_timeout_seconds=5, max_pages=2, circuit_open_seconds=300,
        ledger_max_bytes=16777216, response_max_bytes=262144, response_max_depth=8,
        response_max_string=16384, parse_timeout_seconds=.2, auth_max_issues=4,
        groups=dict(PRICES=1, CANDLES=1, MARKET_INFO=1))
    plan['comparison'].update(outlier_pct=.5, min_valid_pairs=100, expected_market_basis='unknown')
    plan['acceptance'].update(min_coverage=.95, max_provider_failure_rate=.05, max_p95_pct=.5,
        max_outlier_rate=.05, max_missed_slots=11, max_latency_seconds=20, retention_days=30)
    raw = json.dumps(plan, indent=2).encode()
    document['plan_raw_hash'] = hashlib.sha256(raw).hexdigest()
    document['plan_canonical_hash'] = hashlib.sha256(module.canonical_bytes(plan)).hexdigest()
    cohort = Path(document['state_directory']) / 'cohorts' / document['plan_canonical_hash']
    document.update(ledger_path=str(cohort / 'observations.jsonl'), status_path=str(cohort / 'status.json'))
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=1)
    document['retention_at'] = (end + timedelta(days=30)).isoformat()
    document['config_hash'] = module.configuration_hash(document)
    grant = {key: document[key] for key in ('grant_id', 'client_identity', 'host_identity',
        'service_uid', 'token_directory', 'sender_lock_path', 'ledger_path', 'release_id',
        'config_hash', 'plan_raw_hash', 'plan_canonical_hash')}
    grant.update(schema_version=1, approval_reference='synthetic-approval', terms_reference='synthetic-terms',
        storage_reference='synthetic-storage', issuance_ownership_reference='synthetic-ownership',
        not_before=(now - timedelta(minutes=1)).isoformat(), expires_at=end.isoformat(), role='issuer',
        origin=plan['origin'], spec_version=plan['spec_version'], spec_sha256=plan['spec_sha256'],
        capabilities=dict(query=True, renewal=True, bootstrap=True))
    for key, payload in [('plan_path', raw), ('registry_path', module.canonical_bytes(dict(schema_version=1, grants=[grant])))]:
        Path(document[key]).write_bytes(payload)
        Path(document[key]).chmod(0o644)
    path = module.CONFIG_DIRECTORY / 'deployment.json'
    path.write_bytes(module.canonical_bytes(document))
    path.chmod(0o644)
    return module, document, plan, grant


def test_changed_release_rejected_before_service_import(sealed_release):
    module, document = sealed_release
    module.verify_release(document)
    (Path(document['release_root']) / 'app/probe.py').write_text('VALUE = 2\n')
    with pytest.raises(module.LaunchError):
        module.verify_release(document)


@pytest.mark.parametrize('change', ['extra', 'symlink', 'hardlink', 'mode', 'parent', 'bootstrap'])
def test_release_rejects_unsafe_files(sealed_release, change):
    module, document = sealed_release
    root = Path(document['release_root'])
    probe = root / 'app/probe.py'
    if change == 'extra':
        (root / 'deps/extra.py').write_text('raise RuntimeError()')
    elif change == 'symlink':
        probe.unlink()
        probe.symlink_to(root / 'bootstrap/launcher.py')
    elif change == 'hardlink':
        os.link(probe, root / 'alias.py')
    elif change == 'mode':
        probe.chmod(0o666)
    elif change == 'parent':
        root.parent.chmod(0o777)
    else:
        module.TRUSTED_LAUNCHER.write_text('changed')
    with pytest.raises(module.LaunchError):
        module.verify_release(document)


def test_off_subprocess_ignores_arguments_and_python_injection(tmp_path):
    api()
    marker = tmp_path / 'executed'
    (tmp_path / 'sitecustomize.py').write_text(f'open({str(marker)!r}, "w").close()')
    (tmp_path / 'evil.pth').write_text(f'import pathlib; pathlib.Path({str(marker)!r}).touch()')
    result = subprocess.run(['/usr/bin/python3', '-I', '-S', str(LAUNCHER), '--bad'],
        env={'TOSS_API': '0', 'PYTHONPATH': str(tmp_path), 'PYTHONHOME': str(tmp_path)},
        cwd=tmp_path, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and result.stdout == result.stderr == ''
    assert not marker.exists()


@pytest.mark.parametrize('value', ['true', 'yes', '01', ' 1'])
def test_invalid_flag_has_only_fixed_error_code(value):
    api()
    result = subprocess.run(['/usr/bin/python3', '-I', '-S', str(LAUNCHER)],
        env={'TOSS_API': value}, capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert result.stdout == '' and result.stderr == 'observer_launch_denied\n'


def test_nonisolated_on_fails_without_reading_deployment(monkeypatch):
    module = api()
    monkeypatch.setenv('TOSS_API', '1')
    monkeypatch.setattr(module, 'load_verified_document', lambda *_: pytest.fail('파일 접근'))
    assert module.main([]) == 1


@pytest.mark.parametrize('payload', [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"unexpected":1}'])
def test_strict_deployment_json_rejects_ambiguous_document(sealed_release, payload):
    module, _ = sealed_release
    path = module.CONFIG_DIRECTORY / 'deployment.json'
    path.write_bytes(payload)
    path.chmod(0o644)
    with pytest.raises(module.LaunchError):
        module.load_verified_document(path)


@pytest.mark.parametrize('field,value', [('service_uid', 0), ('service_uid', 123456),
    ('service_gid', 123456), ('service_gids', [0]), ('host_identity', 'other-host'),
    ('policy', {}), ('unknown', True), ('plan_raw_hash', 'x')])
def test_deployment_rejects_identity_and_configuration_mismatch(approved_release, field, value):
    module, document, *_ = approved_release
    path = module.CONFIG_DIRECTORY / 'deployment.json'
    path.write_bytes(module.canonical_bytes(document))
    path.chmod(0o644)
    assert module.load_verified_document(path) == document
    document[field] = value
    document['config_hash'] = module.configuration_hash(document)
    path = module.CONFIG_DIRECTORY / 'deployment.json'
    path.write_bytes(module.canonical_bytes(document))
    path.chmod(0o644)
    with pytest.raises(module.LaunchError):
        module.load_verified_document(path)


def test_verified_document_accepts_bound_plan_without_state_access(approved_release):
    module, document, *_ = approved_release
    loaded = module.load_verified_document(module.CONFIG_DIRECTORY / 'deployment.json')
    assert loaded == document
    assert list(Path(document['state_directory']).iterdir()) == []


@pytest.mark.parametrize('target', ['plan_path', 'registry_path'])
def test_import_gate_rejects_untrusted_approval_files(approved_release, target):
    module, document, *_ = approved_release
    Path(document[target]).chmod(0o666)
    with pytest.raises(module.LaunchError):
        module.load_verified_document(module.CONFIG_DIRECTORY / 'deployment.json')


def test_plan_raw_hash_is_checked_before_import(approved_release):
    module, document, *_ = approved_release
    path = Path(document['plan_path'])
    path.write_bytes(path.read_bytes() + b'\n')
    with pytest.raises(module.LaunchError):
        module.load_verified_document(module.CONFIG_DIRECTORY / 'deployment.json')


def test_artifact_deadline_interrupts_verification(sealed_release, monkeypatch):
    module, document = sealed_release
    ticks = iter([100, 131])
    monkeypatch.setattr(module.time, 'monotonic', lambda: next(ticks, 131))
    with pytest.raises(module.LaunchError):
        module.verify_release(document)


def test_builder_rejects_special_file_without_blocking(sealed_release):
    module, document = sealed_release
    root = Path(document['release_root'])
    os.mkfifo(root / 'app/fifo', 0o644)
    with pytest.raises(module.LaunchError):
        module.build_manifest(root, release_id='synthetic', python_version='3.12.3')


@pytest.mark.parametrize('tamper', [False, True])
def test_isolated_bootstrap_checks_real_authority_and_never_executes_site_hooks(approved_release, tmp_path, tamper):
    module, document, _, grant = approved_release
    root = Path(document['release_root'])
    source = LAUNCHER.parents[3]
    for relative in ('src/__init__.py', 'src/data/__init__.py', 'src/data/providers/__init__.py',
                     'src/observation/toss_deployment.py', 'src/utils/__init__.py', 'src/utils/data_freshness.py'):
        target = root / 'app' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    shutil.copytree(source / 'src/data/providers/toss', root / 'app/src/data/providers/toss',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    marker = tmp_path / 'site-executed'
    malicious = f'import pathlib; pathlib.Path({str(marker)!r}).touch()\n'
    for directory in (root / 'deps', tmp_path):
        (directory / 'sitecustomize.py').write_text(malicious)
        (directory / 'injection.pth').write_text(malicious)
    for path in root.rglob('*'):
        path.chmod(0o755 if path.is_dir() else 0o644)
    manifest = module.build_manifest(root, release_id=document['release_id'],
                                    python_version='.'.join(map(str, sys.version_info[:3])))
    digest = hashlib.sha256(module.canonical_bytes(manifest)).hexdigest()
    (root / 'manifest.json').write_bytes(module.canonical_bytes(manifest))
    next_root = root.parent / digest
    root.rename(next_root)
    document.update(artifact_sha256=digest, release_root=str(next_root), manifest_path=str(next_root / 'manifest.json'))
    document['config_hash'] = module.configuration_hash(document)
    grant['config_hash'] = document['config_hash']
    Path(document['registry_path']).write_bytes(module.canonical_bytes(dict(schema_version=1, grants=[grant])))
    (module.CONFIG_DIRECTORY / 'deployment.json').write_bytes(module.canonical_bytes(document))
    if tamper:
        (next_root / 'app/src/__init__.py').write_text(malicious)
    # 테스트 프로세스만 root 소유 tempfile을 모사한다. 실제 -I -S bootstrap과 승인 로더를 실행한다.
    harness = r'''
import importlib.util, os, pathlib, stat, sys
assert sys.flags.isolated and sys.flags.no_site and 'site' not in sys.modules
base = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('test_bootstrap', base / 'launcher.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.CONFIG_DIRECTORY = base / 'etc'
module.RELEASES_DIRECTORY = base / 'releases'
module.STATE_DIRECTORY = base / 'state'
module.TRUSTED_LAUNCHER = base / 'launcher.py'
original = os.fstat
def metadata(fd):
    info = original(fd)
    name = os.readlink('/proc/self/fd/' + str(fd))
    if name == '/tmp' or name.startswith('/tmp/'):
        values = list(info)
        values[4] = values[5] = 0
        if name == '/tmp':
            values[0] = stat.S_IFDIR | 0o755
        return os.stat_result(values)
    return info
os.fstat = metadata
os.getgroups = lambda: [os.getgid()]
code = module.main(['--deployment', str(base / 'etc/deployment.json'), '--check'])
assert 'site' not in sys.modules
assert 'src.observation.toss_service' not in sys.modules
assert 'src.data.providers.toss.oauth' not in sys.modules
assert 'scripts.run_trader' not in sys.modules
print(str(code) + ':' + str('src' in sys.modules))
'''
    result = subprocess.run(['/usr/bin/python3', '-I', '-S', '-c', harness, str(tmp_path)],
        env={'TOSS_API': '1', 'PYTHONPATH': str(tmp_path), 'PYTHONHOME': str(tmp_path)},
        cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ('1:False\n' if tamper else '0:True\n'), result.stderr
    assert result.stderr == ('observer_launch_denied\n' if tamper else '')
    assert not marker.exists()
    assert list(Path(document['state_directory']).iterdir()) == []
