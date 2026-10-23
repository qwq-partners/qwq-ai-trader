#!/usr/bin/env python3
"""Prepare a separate sealed report install; default dry-run, no service/user operations."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys

PREFIX = Path('/')
ETC = Path('/etc/qwq-toss-report')
STATE = Path('/var/lib/qwq-toss-report')
RELEASES = Path('/opt/qwq-toss-report/releases')
LIBEXEC = Path('/usr/libexec/qwq-toss-report')

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



def inspect_inputs(*, artifact, deployment_source, registry_source, plan_source, expected_digest):
    info = inspect_artifact(artifact)
    if info['artifact_sha256'] != expected_digest:
        raise InstallError('artifact_mismatch')
    h = helper()
    document = strict_json(safe_read(deployment_source))
    plan_raw = safe_read(plan_source, maximum=262144)
    registry_raw = safe_read(registry_source, maximum=262144)
    plan, registry = strict_json(plan_raw), strict_json(registry_raw)
    try:
        h._keys(document, h.DEPLOYMENT_KEYS)
        root = RELEASES / expected_digest
        cohort = STATE / 'cohorts' / document['plan_canonical_hash']
        expected = dict(release_root=str(root), manifest_path=str(root / 'manifest.json'),
            registry_path=str(ETC/'registry.json'), plan_path=str(ETC/'plan.json'),
            state_directory=str(STATE), token_directory=str(STATE/'tokens'),
            sender_lock_path=str(STATE/'sender.lock'), ledger_path=str(cohort/'observations.jsonl'),
            status_path=str(cohort/'status.json'), receipt_directory=str(STATE/'starts'),
            artifact_sha256=expected_digest, release_id=info['release_id'])
        if (any(document[k] != v for k, v in expected.items())
                or type(document['schema_version']) is not int or document['schema_version'] != 1
                or h.configuration_hash(document) != document['config_hash']
                or document['policy'] != h.service_policy()
                or hashlib.sha256(plan_raw).hexdigest() != document['plan_raw_hash']
                or hashlib.sha256(h.canonical_bytes(plan)).hexdigest() != document['plan_canonical_hash']):
            raise ValueError
        for key in ('service_uid', 'service_gid'):
            if type(document[key]) is not int or not 1 <= document[key] <= 2**32-2:
                raise ValueError
        groups = document['service_gids']
        if (type(groups) is not list or groups != sorted(set(groups))
                or any(type(g) is not int or not 1 <= g <= 2**32-2 for g in groups)):
            raise ValueError
        for key in ('grant_id', 'client_identity', 'host_identity'):
            h._string(document[key])
        h._keys(registry, ('schema_version', 'grants'))
        if type(registry['schema_version']) is not int or registry['schema_version'] != 1 or len(registry['grants']) != 1:
            raise ValueError
        grant = registry['grants'][0]
        if (plan['schema_version'] != 3 or grant['schema_version'] != 3
                or grant['role'] != 'issuer'
                or plan['report_display'] != dict(purpose='daily_report_current_price', max_age_seconds=60)
                or plan['sessions'] != [dict(name='after', start='16:00', end='16:05')]
                or grant['capabilities'] != dict(query=True, renewal=True, bootstrap=True, report_display=True)):
            raise ValueError
        for key in ('release_id', 'config_hash', 'client_identity', 'host_identity', 'service_uid',
                    'token_directory', 'sender_lock_path', 'ledger_path', 'plan_raw_hash', 'plan_canonical_hash', 'grant_id'):
            if document[key] != grant[key]:
                raise ValueError
        for key in ('approval_reference', 'terms_reference', 'storage_reference', 'issuance_ownership_reference'):
            h._string(grant[key])
    except (ValueError, TypeError, KeyError, h.LaunchError):
        raise InstallError('documents_invalid') from None
    return dict(info, deployment=document, registry_raw=registry_raw, plan_raw=plan_raw)


def check_targets(digest):
    for target in (ETC, STATE, RELEASES / digest, LIBEXEC):
        path = physical(target)
        if path.exists() or path.is_symlink():
            raise InstallError('protected_target_exists')
        for parent in path.parents:
            if parent.is_symlink():
                raise InstallError('unsafe_parent')
            if parent.exists():
                metadata = parent.stat()
                if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
                    raise InstallError('unsafe_parent')


def provision(artifact, info):
    """Only new report paths; no account creation, credential file, unit or activation."""
    document = info['deployment']
    destination = physical(Path(document['release_root']))
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
    h = helper()
    write_new(destination / 'manifest.json', h.canonical_bytes(info['manifest']))
    for path in destination.rglob('*'):
        os.chown(path, 0, 0)
        path.chmod(0o755 if path.is_dir() else 0o644)
    make_directory(physical(LIBEXEC))
    write_new(physical(LIBEXEC/'launcher.py'), safe_read(destination/'bootstrap/launcher.py'))
    make_directory(physical(ETC))
    write_new(physical(ETC/'deployment.json'), h.canonical_bytes(document))
    write_new(physical(ETC/'plan.json'), info['plan_raw'])
    write_new(physical(ETC/'registry.json'), info['registry_raw'])
    uid, gid = document['service_uid'], document['service_gid']
    make_directory(physical(STATE), uid=uid, gid=gid, mode=0o700)
    for name in ('tokens', 'starts', 'cohorts', 'cohorts/' + document['plan_canonical_hash']):
        make_directory(physical(STATE/name), uid=uid, gid=gid, mode=0o700)


def install(*, artifact, deployment_source, registry_source, plan_source, expected_digest, apply=False):
    info = inspect_inputs(artifact=artifact, deployment_source=deployment_source,
        registry_source=registry_source, plan_source=plan_source, expected_digest=expected_digest)
    check_targets(info['artifact_sha256'])
    if apply:
        if os.geteuid() != 0:
            raise InstallError('root_required')
        provision(artifact, info)
    return dict(ready=True, applied=apply, artifact_sha256=info['artifact_sha256'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('artifact', 'deployment-source', 'registry-source', 'plan-source'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--artifact-sha256', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = install(artifact=args.artifact, deployment_source=args.deployment_source,
            registry_source=args.registry_source, plan_source=args.plan_source,
            expected_digest=args.artifact_sha256, apply=args.apply)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        print('report_install_failed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
