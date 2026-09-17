"""root 보호 bootstrap: 프로젝트/제3자 import 전에 실행 파일을 검증한다."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta


ROOT_UID = ROOT_GID = 0
CONFIG_DIRECTORY = Path('/etc/qwq-toss-observer')
RELEASES_DIRECTORY = Path('/opt/qwq-toss-observer/releases')
STATE_DIRECTORY = Path('/var/lib/qwq-toss-observer')
TRUSTED_LAUNCHER = Path('/usr/libexec/qwq-toss-observer/launcher.py')
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_RELEASE_BYTES = 256 * 1024 * 1024
MAX_FILES = 20000
DEPLOYMENT_KEYS = frozenset('schema_version release_id artifact_sha256 release_root manifest_path '
    'registry_path plan_path grant_id config_hash client_identity host_identity service_uid '
    'service_gid service_gids state_directory token_directory sender_lock_path ledger_path '
    'status_path receipt_directory retention_at policy plan_raw_hash plan_canonical_hash'.split())


class LaunchError(Exception):
    """경로·환경·원문을 출력하지 않는 고정 거부 코드."""
    def __init__(self):
        super().__init__('observer_launch_denied')


def canonical_bytes(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=True, allow_nan=False).encode('ascii')
    except (TypeError, ValueError, RecursionError):
        raise LaunchError() from None


def configuration_hash(document):
    return hashlib.sha256(canonical_bytes({k: v for k, v in document.items()
                                          if k != 'config_hash'})).hexdigest()


def service_policy():
    return {'positions_url': 'http://127.0.0.1:8080/api/positions',
        'input_timeout_seconds': 2, 'input_connect_timeout_seconds': .5,
        'input_max_bytes': 65536, 'input_max_rows': 64, 'input_max_depth': 8,
        'input_max_nodes': 4096, 'input_max_string': 1024, 'status_max_bytes': 16384,
        'artifact_timeout_seconds': 30, 'restart': 'no', 'cpu_quota_percent': 25,
        'memory_max_bytes': 201326592, 'tasks_max': 16, 'nice': 10, 'io_weight': 10,
        'cleanup_seconds': 10, 'stop_seconds': 20}


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise LaunchError()
            result[key] = value
        return result
    def invalid(_):
        raise LaunchError()
    if len(raw) > MAX_JSON_BYTES:
        raise LaunchError()
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError):
        raise LaunchError() from None


def _keys(document, keys):
    if type(document) is not dict or set(document) != set(keys):
        raise LaunchError()


def _hash(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise LaunchError()


def _string(value):
    if (type(value) is not str or not value.strip() or len(value) > 256
            or any(ord(c) < 32 for c in value)):
        raise LaunchError()


def _path(value):
    if type(value) is not str or len(value) > 4096 or '\x00' in value:
        raise LaunchError()
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or str(path) != value:
        raise LaunchError()
    return path


def _trusted(metadata, *, directory, exact_mode=None):
    mode = stat.S_IMODE(metadata.st_mode)
    if (metadata.st_uid != ROOT_UID or metadata.st_gid != ROOT_GID or mode & 0o022
            or (directory and not stat.S_ISDIR(metadata.st_mode))
            or (not directory and (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1))
            or (exact_mode is not None and mode != exact_mode)):
        raise LaunchError()


def _open_trusted(path, *, directory=False):
    """모든 부모를 NOFOLLOW descriptor로 고정하고 root 소유권을 확인한다."""
    path = _path(str(path))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        _trusted(os.fstat(fd), directory=True)
        for index, part in enumerate(path.parts[1:]):
            is_directory = directory or index != len(path.parts) - 2
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
            flags |= os.O_DIRECTORY if is_directory else os.O_NONBLOCK
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            _trusted(os.fstat(fd), directory=is_directory)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_fd(fd, maximum, *, digest=False, deadline=None):
    before = os.fstat(fd)
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or not 0 <= before.st_size <= maximum):
        raise LaunchError()
    result = hashlib.sha256() if digest else bytearray()
    size = 0
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            raise LaunchError()
        chunk = os.read(fd, min(65536, maximum + 1 - size))
        if not chunk:
            break
        size += len(chunk)
        if size > maximum:
            raise LaunchError()
        result.update(chunk) if digest else result.extend(chunk)
    after = os.fstat(fd)
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_ino,
        before.st_dev) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns,
                          after.st_ino, after.st_dev) or size != before.st_size:
        raise LaunchError()
    return (result.hexdigest(), size) if digest else bytes(result)


def _read_trusted(path, maximum=MAX_JSON_BYTES):
    fd = _open_trusted(path)
    try:
        return _read_fd(fd, maximum)
    finally:
        os.close(fd)


def _inventory(root, *, trusted, deadline):
    """전체 파일을 대조하여 미등재 import 가능 파일도 거부한다."""
    root_fd = (_open_trusted(root, directory=True) if trusted else
               os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC))
    entries, total = [], 0
    def walk(fd, prefix, depth):
        nonlocal total
        metadata = os.fstat(fd)
        if depth > 32 or stat.S_IMODE(metadata.st_mode) != 0o755:
            raise LaunchError()
        if trusted:
            _trusted(metadata, directory=True, exact_mode=0o755)
        names = sorted(os.listdir(fd))
        if len(names) > MAX_FILES:
            raise LaunchError()
        for name in names:
            if time.monotonic() >= deadline or len(entries) >= MAX_FILES:
                raise LaunchError()
            relative = prefix + name
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            directory = stat.S_ISDIR(info.st_mode)
            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC |
                            (os.O_DIRECTORY if directory else os.O_NONBLOCK), dir_fd=fd)
            try:
                info = os.fstat(child)
                if directory:
                    walk(child, relative + '/', depth + 1)
                else:
                    if stat.S_IMODE(info.st_mode) != 0o644:
                        raise LaunchError()
                    if trusted:
                        _trusted(info, directory=False, exact_mode=0o644)
                    digest, size = _read_fd(child, MAX_FILE_BYTES, digest=True, deadline=deadline)
                    total += size
                    if total > MAX_RELEASE_BYTES:
                        raise LaunchError()
                    if relative != 'manifest.json':
                        entries.append(dict(path=relative, sha256=digest, size=size, mode=0o644))
            finally:
                os.close(child)
    try:
        walk(root_fd, '', 0)
    finally:
        os.close(root_fd)
    return sorted(entries, key=lambda item: item['path'])


def build_manifest(release_root: Path, *, release_id: str, python_version: str) -> dict:
    _string(release_id)
    if type(python_version) is not str or re.fullmatch(r'\d+\.\d+\.\d+', python_version) is None:
        raise LaunchError()
    try:
        files = _inventory(release_root, trusted=False, deadline=time.monotonic() + 30)
        return dict(schema_version=1, release_id=release_id, python_version=python_version,
                    import_paths=['app', 'deps'], files=files)
    except (OSError, ValueError, TypeError):
        raise LaunchError() from None


@contextmanager
def _timeout(seconds):
    """메인 스레드 bootstrap/preflight를 POSIX 타이머로 제한한다."""
    def expired(*_):
        raise LaunchError()
    previous = signal.signal(signal.SIGALRM, expired)
    old_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *old_timer)
        signal.signal(signal.SIGALRM, previous)


def verify_release(document: dict) -> None:
    try:
        with _timeout(30):
            _verify_release(document)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise LaunchError() from None


def _verify_release(document):
    root = _path(document['release_root'])
    _hash(document['artifact_sha256'])
    if (root != RELEASES_DIRECTORY / document['artifact_sha256']
            or _path(document['manifest_path']) != root / 'manifest.json'):
        raise LaunchError()
    raw = _read_trusted(root / 'manifest.json')
    manifest = _json(raw)
    _keys(manifest, ('schema_version', 'release_id', 'python_version', 'import_paths', 'files'))
    if (type(manifest['schema_version']) is not int or manifest['schema_version'] != 1
            or manifest['release_id'] != document['release_id']
            or manifest['python_version'] != '.'.join(map(str, sys.version_info[:3]))
            or manifest['import_paths'] != ['app', 'deps']
            or hashlib.sha256(canonical_bytes(manifest)).hexdigest() != document['artifact_sha256']
            or type(manifest['files']) is not list or not 1 <= len(manifest['files']) <= MAX_FILES):
        raise LaunchError()
    for entry in manifest['files']:
        _keys(entry, ('path', 'sha256', 'size', 'mode'))
        if (type(entry['path']) is not str or not entry['path']
                or Path(entry['path']).is_absolute() or '..' in Path(entry['path']).parts
                or str(Path(entry['path'])) != entry['path']
                or type(entry['size']) is not int or not 0 <= entry['size'] <= MAX_FILE_BYTES
                or type(entry['mode']) is not int or entry['mode'] != 0o644):
            raise LaunchError()
        _hash(entry['sha256'])
    files = _inventory(root, trusted=True, deadline=time.monotonic() + 30)
    if files != manifest['files']:
        raise LaunchError()
    by_path = {item['path']: item for item in files}
    launcher = by_path.get('bootstrap/launcher.py')
    if (launcher is None or hashlib.sha256(_read_trusted(TRUSTED_LAUNCHER, MAX_FILE_BYTES)).hexdigest()
            != launcher['sha256']):
        raise LaunchError()
    for relative in ('app', 'deps', 'bootstrap'):
        fd = _open_trusted(root / relative, directory=True)
        os.close(fd)


def _validate_document(document):
    _keys(document, DEPLOYMENT_KEYS)
    if type(document['schema_version']) is not int or document['schema_version'] != 1:
        raise LaunchError()
    for key in ('release_id', 'grant_id', 'client_identity', 'host_identity'):
        _string(document[key])
    for key in ('artifact_sha256', 'config_hash', 'plan_raw_hash', 'plan_canonical_hash'):
        _hash(document[key])
    for key in ('service_uid', 'service_gid'):
        if type(document[key]) is not int or not 1 <= document[key] <= 2**32 - 2:
            raise LaunchError()
    groups = document['service_gids']
    if (type(groups) is not list or any(type(g) is not int or g != document['service_gid'] for g in groups)
            or groups != sorted(set(groups))
            or os.getuid() != document['service_uid'] or os.geteuid() != document['service_uid']
            or os.getgid() != document['service_gid'] or os.getegid() != document['service_gid']
            or sorted(set(os.getgroups())) != groups
            or socket.gethostname() != document['host_identity']
            or canonical_bytes(document['policy']) != canonical_bytes(service_policy())
            or configuration_hash(document) != document['config_hash']):
        raise LaunchError()
    cohort = STATE_DIRECTORY / 'cohorts' / document['plan_canonical_hash']
    expected = dict(registry_path=CONFIG_DIRECTORY / 'registry.json', plan_path=CONFIG_DIRECTORY / 'plan.json',
        state_directory=STATE_DIRECTORY, token_directory=STATE_DIRECTORY / 'tokens',
        sender_lock_path=STATE_DIRECTORY / 'sender.lock', receipt_directory=STATE_DIRECTORY / 'starts',
        ledger_path=cohort / 'observations.jsonl', status_path=cohort / 'status.json')
    for key, path in expected.items():
        if _path(document[key]) != path:
            raise LaunchError()
    stamp = datetime.fromisoformat(document['retention_at'])
    if stamp.utcoffset() != timedelta(0):
        raise LaunchError()


def load_verified_document(path: Path) -> dict:
    try:
        with _timeout(30):
            if _path(str(path)) != CONFIG_DIRECTORY / 'deployment.json':
                raise LaunchError()
            document = _json(_read_trusted(path))
            _validate_document(document)
            # 기존 승인 로더의 재검증과 별개로 import 전에 plan 내용도 결합한다.
            plan_raw = _read_trusted(Path(document['plan_path']), 262144)
            if (hashlib.sha256(plan_raw).hexdigest() != document['plan_raw_hash']
                    or hashlib.sha256(canonical_bytes(_json(plan_raw))).hexdigest()
                    != document['plan_canonical_hash']):
                raise LaunchError()
            _read_trusted(Path(document['registry_path']), 262144)
            _verify_release(document)
            return document
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise LaunchError() from None


def main(argv=None):
    flag = os.environ.get('TOSS_API', '0')
    if flag in ('0', ''):
        return 0
    try:
        if (flag != '1' or not sys.flags.isolated or not sys.flags.no_site
                or 'site' in sys.modules or Path(__file__) != TRUSTED_LAUNCHER):
            raise LaunchError()
        argv = sys.argv[1:] if argv is None else argv
        check = '--check' in argv
        options = [arg for arg in argv if arg != '--check']
        if (argv.count('--check') > 1 or options not in
                ([], ['--deployment', str(CONFIG_DIRECTORY / 'deployment.json')])):
            raise LaunchError()
        document = load_verified_document(CONFIG_DIRECTORY / 'deployment.json')
        sys.dont_write_bytecode = True
        root = Path(document['release_root'])
        sys.path[:0] = [str(root / 'app'), str(root / 'deps')]
        # .pth/site 초기화를 실행하지 않는다. 위 검증 이전에는 이 import가 없다.
        from src.observation.toss_deployment import make_deployment, claim_start
        with _timeout(5):
            deployment = make_deployment(document)
        if check:
            return 0
        import asyncio
        from src.observation.toss_service import run_service
        return asyncio.run(run_service(deployment=deployment, settings=document,
            claim_start=lambda authority: claim_start(document, authority)))
    except Exception:
        print('observer_launch_denied', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
