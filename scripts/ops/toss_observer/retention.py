#!/usr/bin/env python3
"""승인 cohort 원장만 보존기간 이후 삭제. auth/시작 기록은 접근하지 않는다."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


class RetentionError(Exception):
    pass


def _metadata(info, uid, *, directory=False):
    mode = 0o700 if directory else 0o600
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid or info.st_uid != uid or stat.S_IMODE(info.st_mode) != mode or (not directory and info.st_nlink != 1):
        raise RetentionError('unsafe_retention_path')


def _open_directory(path):
    if not path.is_absolute() or '..' in path.parts:
        raise RetentionError('unsafe_retention_path')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = following
        return fd
    except BaseException:
        os.close(fd)
        raise


def _append(fd, value):
    data = memoryview((json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode())
    while data:
        written = os.write(fd, data)
        if written <= 0:
            raise RetentionError('retention_receipt_failed')
        data = data[written:]
    os.fsync(fd)


def _completed_receipt(parent, uid, document):
    fd = None
    try:
        fd = os.open('retention.jsonl', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
        _metadata(os.fstat(fd), uid)
        payload = os.read(fd, 4097)
        if len(payload) > 4096 or not payload.endswith(b'\n'):
            raise RetentionError('retention_receipt_invalid')
        def pairs(items):
            value = {}
            for key, item in items:
                if key in value:
                    raise ValueError
                value[key] = item
            return value
        rows = [json.loads(line, object_pairs_hook=pairs) for line in payload.splitlines()]
        keys = {'schema_version', 'phase', 'plan_hash', 'config_hash', 'retention_at',
                'recorded_at', 'device', 'inode'}
        if (len(rows) != 2 or any(type(row) is not dict or set(row) != keys for row in rows)
                or rows[0]['phase'] != 'intent' or rows[1] != dict(rows[0], phase='completed')
                or type(rows[0]['schema_version']) is not int or rows[0]['schema_version'] != 1
                or rows[0]['plan_hash'] != document['plan_canonical_hash']
                or rows[0]['config_hash'] != document['config_hash']
                or rows[0]['retention_at'] != document['retention_at']
                or any(type(rows[0][key]) is not int or rows[0][key] < 0 for key in ('device', 'inode'))):
            raise RetentionError('retention_receipt_invalid')
        recorded = datetime.fromisoformat(rows[0]['recorded_at'])
        if recorded.utcoffset() != timezone.utc.utcoffset(recorded) or recorded < datetime.fromisoformat(document['retention_at']):
            raise RetentionError('retention_receipt_invalid')
        try:
            os.stat('observations.jsonl', dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return {'eligible': True, 'deleted': False, 'already_deleted': True}
        raise RetentionError('ledger_recreated')
    finally:
        if fd is not None:
            os.close(fd)


def run_retention(document, *, now, service_active, apply=False):
    """document는 trusted launcher에서 검증한 배치. 직접 호출도 삭제 경계를 재검사."""
    handles = []
    try:
        uid = document['service_uid']
        state = Path(document['state_directory'])
        digest = document['plan_canonical_hash']
        ledger = Path(document['ledger_path'])
        expiration = datetime.fromisoformat(document['retention_at'])
        if (type(uid) is not int or uid != os.geteuid()
                or not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None
                or ledger != state / 'cohorts' / digest / 'observations.jsonl'
                or Path(document['sender_lock_path']) != state / 'sender.lock'
                or now.utcoffset() != timezone.utc.utcoffset(now)
                or expiration.utcoffset() != timezone.utc.utcoffset(expiration)
                or now < expiration or service_active() is not False):
            raise RetentionError('retention_not_authorized')
        state_fd = _open_directory(state)
        handles.append(state_fd)
        _metadata(os.fstat(state_fd), uid, directory=True)
        lock = os.open('sender.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=state_fd)
        handles.append(lock)
        _metadata(os.fstat(lock), uid)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if service_active() is not False:
            raise RetentionError('service_active')
        parent = state_fd
        for part in ('cohorts', digest):
            parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            handles.append(parent)
            _metadata(os.fstat(parent), uid, directory=True)
        # 이미 게시된/부분 영수증은 자동 재삭제·수정하지 않는다.
        try:
            os.stat('retention.jsonl', dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            return _completed_receipt(parent, uid, document)
        fd = os.open('observations.jsonl', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
        handles.append(fd)
        info = os.fstat(fd)
        _metadata(info, uid)
        if not apply:
            return {'eligible': True, 'deleted': False}
        receipt = os.open('retention.jsonl', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                          0o600, dir_fd=parent)
        handles.append(receipt)
        _metadata(os.fstat(receipt), uid)
        evidence = {'schema_version': 1, 'phase': 'intent', 'plan_hash': digest,
            'config_hash': document['config_hash'], 'retention_at': document['retention_at'],
            'recorded_at': now.isoformat(), 'device': info.st_dev, 'inode': info.st_ino}
        _append(receipt, evidence)
        os.fsync(parent)
        visible = os.stat('observations.jsonl', dir_fd=parent, follow_symlinks=False)
        _metadata(visible, uid)
        if (visible.st_dev, visible.st_ino, visible.st_size, visible.st_mtime_ns) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
            raise RetentionError('ledger_changed')
        if service_active() is not False:
            raise RetentionError('service_active')
        os.unlink('observations.jsonl', dir_fd=parent)
        os.fsync(parent)
        _append(receipt, dict(evidence, phase='completed'))
        os.fsync(parent)
        return {'eligible': True, 'deleted': True}
    except RetentionError:
        raise
    except Exception:
        raise RetentionError('retention_failed') from None
    finally:
        for fd in reversed(handles):
            os.close(fd)


def service_active():
    result = subprocess.run(['/usr/bin/systemctl', 'show', 'qwq-toss-observer.service',
        '--property=ActiveState', '--value'], env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    if result.returncode:
        raise RetentionError('service_state_unknown')
    state = result.stdout.strip()
    if state not in {b'inactive', b'failed', b'active', b'activating', b'deactivating', b'reloading'}:
        raise RetentionError('service_state_unknown')
    return state not in {b'inactive', b'failed'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        if not sys.flags.isolated or not sys.flags.no_site:
            raise RetentionError('isolated_interpreter_required')
        path = Path('/usr/libexec/qwq-toss-observer/launcher.py')
        spec = importlib.util.spec_from_file_location('toss_retention_launcher', path)
        module = importlib.util.module_from_spec(spec)
        # root 보호 source를 직접 compile: 별도 pyc는 신뢰하지 않는다.
        exec(compile(path.read_bytes(), str(path), 'exec'), module.__dict__)
        document = module.load_verified_document(Path('/etc/qwq-toss-observer/deployment.json'))
        # 등록부의 만료는 삭제를 금지하지 않는다. 배치의 root/hash 검증과 분리한다.
        result = run_retention(document, now=datetime.now(timezone.utc),
                               service_active=service_active, apply=args.apply)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        print('observer_retention_incomplete', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
