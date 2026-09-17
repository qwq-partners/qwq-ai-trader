#!/usr/bin/env python3
"""독립 Toss 서비스 새 설치. 기본 dry-run이며 기존 대상은 덮어쓰지 않는다."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import grp
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import socket
import stat
import subprocess
import sys

PREFIX = Path('/')
USER = 'qwq-toss-observer'
ETC = Path('/etc/qwq-toss-observer')
STATE = Path('/var/lib/qwq-toss-observer')
RELEASES = Path('/opt/qwq-toss-observer/releases')
LIBEXEC = Path('/usr/libexec/qwq-toss-observer')
UNITS = ('qwq-toss-observer.service', 'qwq-toss-observer-retention.service',
         'qwq-toss-observer-retention.timer')
UTC = timezone.utc


class InstallError(Exception):
    pass


def helper():
    path = Path(__file__).with_name('launcher.py')
    spec = importlib.util.spec_from_file_location('toss_install_launcher', path)
    module = importlib.util.module_from_spec(spec)
    exec(compile(safe_read(path), str(path), 'exec'), module.__dict__)
    return module


def physical(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise InstallError('unsafe_path')
    return PREFIX / path.relative_to('/')


def safe_read(path, *, maximum=1048576):
    path = Path(path)
    for parent in path.parents:
        if parent.is_symlink():
            raise InstallError('unsafe_file')
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
            raise InstallError('unsafe_file')
        chunks = bytearray()
        while len(chunks) <= maximum:
            block = os.read(fd, min(65536, maximum + 1 - len(chunks)))
            if not block:
                break
            chunks.extend(block)
        after = os.fstat(fd)
        if len(chunks) > maximum or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise InstallError('file_changed')
        return bytes(chunks)
    except OSError:
        raise InstallError('unsafe_file') from None
    finally:
        if fd is not None:
            os.close(fd)


def parse_credentials(raw):
    """쉘·변수 확장 없이 두 이름만 읽는다. quoted escape는 의도 추정 없이 거부."""
    try:
        text = raw.decode('utf-8')
        result = {}
        for line in text.split('\n'):
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue
            if '=' not in stripped:
                if stripped.startswith(('TOSS_CLIENT_ID', 'TOSS_CLIENT_SECRET')):
                    raise ValueError
                continue
            key, value = stripped.split('=', 1)
            key = key.strip()
            if key not in {'TOSS_CLIENT_ID', 'TOSS_CLIENT_SECRET'}:
                continue
            if key in result or '\r' in line or '\x00' in line:
                raise ValueError
            value = value.strip()
            if value.startswith(('"', "'")):
                if len(value) < 2 or value[-1] != value[0] or '\\' in value:
                    raise ValueError
                value = value[1:-1]
            if re.fullmatch(r'[\x21-\x7e]{1,4096}', value) is None:
                raise ValueError
            result[key] = value
        if set(result) != {'TOSS_CLIENT_ID', 'TOSS_CLIENT_SECRET'}:
            raise ValueError
        return result
    except (ValueError, UnicodeError, AttributeError):
        raise InstallError('credentials_invalid') from None


def credentials_bytes(values):
    def quote(value):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return ''.join(f'{key}={quote(values[key])}\n' for key in
                   ('TOSS_CLIENT_ID', 'TOSS_CLIENT_SECRET')).encode('ascii')


def read_credentials(path):
    return parse_credentials(safe_read(path))


def write_new(path, payload, *, mode=0o644):
    fd = None
    try:
        for parent in Path(path).parents:
            if parent.is_symlink():
                raise InstallError('unsafe_path')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
        os.fchmod(fd, mode)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise InstallError('write_failed')
            view = view[written:]
        os.fsync(fd)
        parent = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except OSError:
        raise InstallError('target_exists_or_write_failed') from None
    finally:
        if fd is not None:
            os.close(fd)


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise InstallError('invalid_json') from None


def inspect_artifact(path):
    path = Path(path).absolute()
    manifest = strict_json(safe_read(path / 'manifest.json', maximum=8388608))
    h = helper()
    try:
        actual = h.build_manifest(path, release_id=manifest['release_id'],
                                  python_version=manifest['python_version'])
        if actual != manifest or manifest['python_version'] != '.'.join(map(str, sys.version_info[:3])):
            raise InstallError('artifact_mismatch')
    except (KeyError, TypeError):
        raise InstallError('artifact_mismatch') from None
    return {'release_id': manifest['release_id'], 'manifest': manifest,
            'artifact_sha256': hashlib.sha256(h.canonical_bytes(manifest)).hexdigest()}


def observation_plan(plan_id, dates):
    if not isinstance(plan_id, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,128}', plan_id) is None:
        raise InstallError('plan_invalid')
    try:
        if len(dates) != 3 or dates != sorted(set(dates)):
            raise ValueError
        for day in dates:
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError
    except (ValueError, TypeError):
        raise InstallError('plan_invalid') from None
    return {'schema_version': 1, 'plan_id': plan_id, 'dataset_kind': 'live',
        'origin': 'https://openapi.tossinvest.com', 'spec_version': '1.2.17',
        'spec_sha256': '791082da4cb379117ed9fdc29a45bd42746f7a1aec368da1e9f4e1f3bfbff5b4',
        'dates': dates, 'sessions': [{'name': 'regular', 'start': '09:00', 'end': '15:20'}],
        'calendar_time': '08:55',
        'selection': {'candidate_limit': 0, 'max_snapshot_age_seconds': 300,
                      'max_symbols': 20, 'rule': 'score_desc_symbol_asc_holdings_first'},
        'limits': {'job_timeout_seconds': 20, 'cleanup_timeout_seconds': 10,
            'preflight_timeout_seconds': 5, 'max_pages': 2, 'max_retries': 1,
            'circuit_failure_threshold': 3, 'circuit_open_seconds': 300,
            'ledger_max_bytes': 16777216, 'response_max_bytes': 262144,
            'response_max_depth': 8, 'response_max_nodes': 10000, 'response_max_string': 16384,
            'parse_timeout_seconds': 0.2, 'auth_max_issues': 4,
            'groups': {'PRICES': 1, 'CANDLES': 1, 'MARKET_INFO': 1}},
        'comparison': {'max_age_seconds': 60, 'max_skew_seconds': 5, 'outlier_pct': 0.5,
                      'min_valid_pairs': 100, 'expected_market_basis': 'unknown'},
        'acceptance': {'min_coverage': 0.95, 'max_provider_failure_rate': 0.05,
            'max_p95_pct': 0.5, 'max_outlier_rate': 0.05, 'max_missed_slots': 11,
            'max_latency_seconds': 20, 'retention_days': 30, 'min_business_days': 3}}


def validate_window(dates, start, end):
    try:
        if start.utcoffset() != timedelta(0) or end.utcoffset() != timedelta(0) or not timedelta(0) < end - start <= timedelta(days=7):
            raise ValueError
        kst = timezone(timedelta(hours=9))
        if start >= datetime.fromisoformat(dates[0] + 'T08:55:00').replace(tzinfo=kst):
            raise ValueError
        if end != datetime.fromisoformat(dates[-1] + 'T18:00:00').replace(tzinfo=kst):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise InstallError('grant_window_invalid') from None


def check_targets(digest):
    targets = (ETC, STATE, RELEASES / digest, LIBEXEC,
               *(Path('/etc/systemd/system') / unit for unit in UNITS))
    for target in targets:
        path = physical(target)
        if path.exists() or path.is_symlink():
            raise InstallError('protected_target_exists')
        for parent in path.parents:
            if parent.is_symlink():
                raise InstallError('unsafe_parent')
            if parent.exists():
                info = parent.stat()
                if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
                    raise InstallError('unsafe_parent')


def run_command(args):
    result = subprocess.run(args, env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if result.returncode:
        raise InstallError('system_operation_failed')


def check_account_available():
    try:
        pwd.getpwnam(USER)
    except KeyError:
        pass
    else:
        raise InstallError('service_user_exists')
    try:
        grp.getgrnam(USER)
    except KeyError:
        pass
    else:
        raise InstallError('service_group_exists')


def create_user():
    check_account_available()
    run_command(['/usr/sbin/useradd', '--system', '--user-group', '--no-create-home',
                 '--home-dir', '/nonexistent', '--shell', '/usr/sbin/nologin', USER])
    account, group = pwd.getpwnam(USER), grp.getgrnam(USER)
    if account.pw_uid == 0 or account.pw_gid != group.gr_gid or group.gr_mem:
        raise InstallError('service_identity_invalid')
    if os.getgrouplist(USER, group.gr_gid) != [group.gr_gid]:
        raise InstallError('service_identity_invalid')
    return account.pw_uid, group.gr_gid


def make_directory(path, *, uid=0, gid=0, mode=0o755):
    path = Path(path)
    missing = []
    for parent in path.parents:
        if parent.is_symlink():
            raise InstallError('unsafe_parent')
        if parent.exists():
            break
        missing.append(parent)
    for parent in reversed(missing):
        parent.mkdir(mode=0o755)
        parent.chmod(0o755)
        os.chown(parent, 0, 0)
    path.mkdir(mode=mode, exist_ok=False)
    path.chmod(mode)
    os.chown(path, uid, gid)


def documents(info, plan, *, uid, gid, client_id, grant_id, now, expires_at):
    h = helper()
    plan_raw = h.canonical_bytes(plan)
    plan_hash = hashlib.sha256(plan_raw).hexdigest()
    root = RELEASES / info['artifact_sha256']
    cohort = STATE / 'cohorts' / plan_hash
    deployment = {'schema_version': 1, 'release_id': info['release_id'],
        'artifact_sha256': info['artifact_sha256'], 'release_root': str(root),
        'manifest_path': str(root / 'manifest.json'), 'registry_path': str(ETC / 'registry.json'),
        'plan_path': str(ETC / 'plan.json'), 'grant_id': grant_id,
        'plan_raw_hash': plan_hash, 'plan_canonical_hash': plan_hash,
        'client_identity': client_id, 'host_identity': socket.gethostname(),
        'service_uid': uid, 'service_gid': gid, 'service_gids': [gid],
        'state_directory': str(STATE), 'token_directory': str(STATE / 'tokens'),
        'sender_lock_path': str(STATE / 'sender.lock'),
        'ledger_path': str(cohort / 'observations.jsonl'), 'status_path': str(cohort / 'status.json'),
        'receipt_directory': str(STATE / 'starts'),
        'retention_at': (expires_at + timedelta(days=30)).isoformat(), 'policy': h.service_policy()}
    deployment['config_hash'] = h.configuration_hash(deployment)
    grant = {key: deployment[key] for key in ('release_id', 'config_hash', 'client_identity',
        'host_identity', 'service_uid', 'token_directory', 'sender_lock_path', 'ledger_path',
        'plan_raw_hash', 'plan_canonical_hash', 'grant_id')}
    grant.update(schema_version=1, approval_reference='user-approved-observer-spec-20260917',
        terms_reference='user-confirmed-query-terms-20260917',
        storage_reference='user-confirmed-storage-30days-20260917',
        issuance_ownership_reference='user-confirmed-sole-issuer-20260917',
        not_before=now.isoformat(), expires_at=expires_at.isoformat(), role='issuer',
        origin=plan['origin'], spec_version=plan['spec_version'], spec_sha256=plan['spec_sha256'],
        capabilities={'query': True, 'renewal': True, 'bootstrap': True})
    return deployment, plan_raw, {'schema_version': 1, 'grants': [grant]}


def provision(artifact, info, plan, credentials, *, grant_id, now, expires_at):
    uid, gid = create_user()
    h = helper()
    deployment, plan_raw, registry = documents(info, plan, uid=uid, gid=gid,
        client_id=credentials['TOSS_CLIENT_ID'], grant_id=grant_id, now=now, expires_at=expires_at)
    destination = physical(Path(deployment['release_root']))
    make_directory(destination)
    for item in info['manifest']['files']:
        relative = Path(item['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise InstallError('unsafe_artifact')
        payload = safe_read(Path(artifact) / relative, maximum=67108864)
        if len(payload) != item['size'] or hashlib.sha256(payload).hexdigest() != item['sha256']:
            raise InstallError('artifact_changed')
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        write_new(target, payload)
        os.chown(target, 0, 0)
    write_new(destination / 'manifest.json', h.canonical_bytes(info['manifest']))
    for path in destination.rglob('*'):
        os.chown(path, 0, 0)
        path.chmod(0o755 if path.is_dir() else 0o644)
    # 검증 후 저장소의 파일을 복사하지 않고 실제 봉인본 launcher를 사용한다.
    make_directory(physical(LIBEXEC))
    write_new(physical(LIBEXEC / 'launcher.py'), safe_read(destination / 'bootstrap/launcher.py'))
    write_new(physical(LIBEXEC / 'retention.py'), safe_read(Path(__file__).with_name('retention.py')))
    make_directory(physical(ETC))
    write_new(physical(ETC / 'plan.json'), plan_raw)
    write_new(physical(ETC / 'registry.json'), h.canonical_bytes(registry))
    write_new(physical(ETC / 'deployment.json'), h.canonical_bytes(deployment))
    write_new(physical(ETC / 'credentials.env'), credentials_bytes(credentials), mode=0o600)
    make_directory(physical(STATE), uid=uid, gid=gid, mode=0o700)
    for path in ('tokens', 'starts', 'cohorts', 'cohorts/' + deployment['plan_canonical_hash']):
        make_directory(physical(STATE / path), uid=uid, gid=gid, mode=0o700)
    for unit in UNITS:
        write_new(physical(Path('/etc/systemd/system') / unit), safe_read(Path(__file__).with_name(unit)))
    return deployment


def install(*, artifact, credential_source, plan_id, grant_id, dates, expires_at,
            now=None, apply=False, expected_digest=None):
    now = datetime.now(UTC) if now is None else now
    plan = observation_plan(plan_id, dates)
    validate_window(dates, now, expires_at)
    if not isinstance(grant_id, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,128}', grant_id) is None:
        raise InstallError('grant_invalid')
    info = inspect_artifact(artifact)
    if expected_digest is not None and info['artifact_sha256'] != expected_digest:
        raise InstallError('artifact_mismatch')
    check_targets(info['artifact_sha256'])
    check_account_available()
    if not apply:
        return {'ready': True, 'applied': False, 'artifact_sha256': info['artifact_sha256']}
    if os.geteuid() != 0 or expected_digest is None:
        raise InstallError('root_and_digest_required')
    credentials = read_credentials(credential_source)
    provision(artifact, info, plan, credentials, grant_id=grant_id, now=now, expires_at=expires_at)
    return {'ready': True, 'applied': True, 'artifact_sha256': info['artifact_sha256']}


def activate():
    if os.geteuid() != 0:
        raise InstallError('root_required')
    run_command(['/usr/bin/systemctl', 'daemon-reload'])
    run_command(['/usr/bin/systemctl', 'enable', '--now', 'qwq-toss-observer-retention.timer'])
    run_command(['/usr/bin/systemctl', 'start', 'qwq-toss-observer.service'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact', type=Path, required=True)
    parser.add_argument('--artifact-sha256', required=True)
    parser.add_argument('--credential-source', type=Path, required=True)
    parser.add_argument('--plan-id', required=True)
    parser.add_argument('--grant-id', required=True)
    parser.add_argument('--dates', nargs=3, required=True)
    parser.add_argument('--expires-at', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--activate', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.activate and not args.apply:
            raise InstallError('activation_requires_apply')
        result = install(artifact=args.artifact, credential_source=args.credential_source,
            expected_digest=args.artifact_sha256, plan_id=args.plan_id, grant_id=args.grant_id,
            dates=args.dates, expires_at=datetime.fromisoformat(args.expires_at), apply=args.apply)
        if args.activate:
            activate()
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        print('observer_install_failed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
