#!/usr/bin/env python3
"""고정 manifest의 단발 파일 전용 판독. 설치·네트워크·주문 동작 없음."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import signal
import stat
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
VERSION = 'deterministic-capture-readout-v1'
PREGATE_VERSION = 'deterministic-pregate-readout-v1'
MAX_INPUT = 268435456
MAX_STUDY = 4 * 1024 * 1024
MAX_OUTPUT = 32 * 1024 * 1024
CHILD_TIMEOUT = 90
TOOLS = (
    ('readiness', 'report_capture_readiness.py'),
    ('gate-trace', 'report_entry_gate_trace.py'),
    ('frame-diagnostics', 'report_kis_frame_diagnostics.py'),
    ('evaluation-unreviewed', 'prepare_entry_evaluation.py'),
)
FIELDS = {'version', 'study', 'study_sha256', 'ledger', 'decisions', 'output_dir',
          'not_before', 'not_after', 'source_sha256'}
PREGATE_FIELDS = FIELDS - {'decisions'}
PROFILE_ALIASES = {'legacy': VERSION, 'pregate': PREGATE_VERSION}
PREGATE_SCRIPTS = ('report_pregate_candidate_observation.py', 'report_entry_gate_trace.py')


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result

    def constant(_):
        raise ValueError('nonfinite JSON value')

    return json.loads(raw, object_pairs_hook=unique, parse_constant=constant)


def _path(value):
    if not isinstance(value, str) or not value.startswith('/') or '\x00' in value:
        raise ValueError('absolute path required')
    if value != os.path.normpath(value) or value.startswith('//') or value == '/':
        raise ValueError('normalized non-root path required')
    return Path(value)


def _directory(path):
    """각 구성요소를 nofollow로 열어 상위 symlink 우회도 거부한다."""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in _path(str(path)).parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_read(path):
    parent = _directory(path.parent) if path.parent != Path('/') else os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        return os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_small(path, maximum):
    fd = _open_read(path)
    with os.fdopen(fd, 'rb') as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise ValueError('bounded regular file required')
        raw = handle.read(maximum + 1)
        after = os.fstat(handle.fileno())
    if len(raw) > maximum or _identity(before) != _identity(after):
        raise ValueError('file changed or exceeded bound')
    return raw


def _version(version):
    version = PROFILE_ALIASES.get(version, version)
    if version not in (VERSION, PREGATE_VERSION):
        raise ValueError('unknown readout profile')
    return version


def source_fingerprint(version=VERSION):
    """실행 코드 전체의 경로/바이트 결합 SHA. Git·환경 파일은 읽지 않는다."""
    version = _version(version)
    if version == VERSION:
        files = [ROOT / 'scripts' / script for _, script in TOOLS]
    else:
        files = [ROOT / 'scripts' / script for script in PREGATE_SCRIPTS]
    files.append(ROOT / 'scripts/ops/run_capture_readout.py')
    for optional in ('scripts/__init__.py', 'scripts/ops/__init__.py'):
        path = ROOT / optional
        if path.exists():
            files.append(path)
    for base, dirs, names in os.walk(ROOT / 'src', followlinks=False):
        dirs[:] = sorted(d for d in dirs if d != '__pycache__')
        if any((Path(base) / d).is_symlink() for d in dirs):
            raise ValueError('source directory symlink')
        files.extend(Path(base) / name for name in names if name.endswith('.py'))
    digest = hashlib.sha256()
    for path in sorted(files):
        raw = _read_small(path, MAX_STUDY)
        digest.update(str(path.relative_to(ROOT)).encode() + b'\0')
        digest.update(hashlib.sha256(raw).digest())
    return digest.hexdigest()


def _time(value):
    if not isinstance(value, str):
        raise ValueError('aware ISO timestamp required')
    value = datetime.fromisoformat(value)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('aware ISO timestamp required')
    return value


def _manifest(path):
    path = _path(str(path))
    raw = _read_small(path, 65536)
    data = _json(raw)
    if type(data) is not dict or not isinstance(data.get('version'), str):
        raise ValueError('manifest schema mismatch')
    version = _version(data['version'])
    fields = FIELDS if version == VERSION else PREGATE_FIELDS
    if set(data) != fields or data['version'] != version:
        raise ValueError('manifest schema mismatch')
    for field in ('study_sha256', 'source_sha256'):
        if not isinstance(data[field], str) or not re.fullmatch('[0-9a-f]{64}', data[field]):
            raise ValueError('expected SHA-256 required')
    input_keys = ('study', 'ledger', 'decisions') if version == VERSION else ('study', 'ledger')
    paths = [_path(data[key]) for key in (*input_keys, 'output_dir')]
    if len(set(paths)) != len(paths):
        raise ValueError('paths must be distinct')
    output = paths[-1]
    if output == ROOT or ROOT in output.parents:
        raise ValueError('outputs must be outside source tree')
    if any(p == output or output in p.parents for p in [path, *paths[:-1]]):
        raise ValueError('output must not contain inputs or manifest')
    start, end = _time(data['not_before']), _time(data['not_after'])
    if (start.utcoffset() != end.utcoffset() or start.date() != end.date()
            or not start < end or (end - start).total_seconds() > 3600):
        raise ValueError('same-date window of at most one hour required')
    return data, hashlib.sha256(raw).hexdigest()


def _window(data, now):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('aware execution clock required')
    return _time(data['not_before']) <= now <= _time(data['not_after'])


def _inspect_optional(path, *, directory=False):
    """미래 경로의 부재는 허용하고 존재하는 모든 구성요소는 검증한다."""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = path.parts[1:]
        for part in parts[:-1]:
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                return None
            os.close(fd)
            fd = nxt
        try:
            info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if directory:
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise ValueError('existing output must be a private directory')
        elif not stat.S_ISREG(info.st_mode):
            raise ValueError('existing input must be a regular file without symlinks')
        return info
    finally:
        os.close(fd)


def check(path):
    """미래 수집 파일은 없어도 된다. once/output 쓰기는 하지 않는다."""
    try:
        data, sha = _manifest(path)
        if source_fingerprint(data['version']) != data['source_sha256']:
            raise ValueError('source fingerprint mismatch')
        parent = _directory(Path(data['output_dir']).parent)
        os.close(parent)
        _inspect_optional(Path(data['output_dir']), directory=True)
        for key in (('study', 'ledger', 'decisions') if data['version'] == VERSION else ('study', 'ledger')):
            info = _inspect_optional(Path(data[key]))
            maximum = MAX_STUDY if key == 'study' else MAX_INPUT
            if info is not None and info.st_size > maximum:
                raise ValueError(f'{key}: input size bound exceeded')
            if key == 'study' and info is not None:
                if hashlib.sha256(_read_small(Path(data[key]), MAX_STUDY)).hexdigest() != data['study_sha256']:
                    raise ValueError('study hash mismatch')
        return {'status': 'preflight_ok', 'manifest_sha256': sha,
                'source_sha256': data['source_sha256'], 'future_inputs_checked': False}
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
        return {'status': 'invalid_manifest', 'error': str(exc)}


def _new_file(directory_fd, name):
    return os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                   0o600, dir_fd=directory_fd)


def _write_json(directory_fd, name, value):
    fd = _new_file(directory_fd, name)
    with os.fdopen(fd, 'wb') as handle:
        handle.write((json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n').encode())
        handle.flush()
        os.fsync(handle.fileno())
    os.fsync(directory_fd)


def _output(path):
    parent = _directory(path.parent)
    try:
        try:
            os.mkdir(path.name, 0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
        fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise ValueError('output directory must be owned by current user with mode 0700')
    return fd


def _snapshot(source, directory_fd, name, maximum, *, allow_append=False):
    """고정 크기 prefix를 스트리밍 복사. 판단 원본의 추가 append는 허용한다."""
    fd = _open_read(source)
    with os.fdopen(fd, 'rb', buffering=0) as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise ValueError(f'{name}: bounded regular input required')
        digest = hashlib.sha256()
        remaining = before.st_size
        target = _new_file(directory_fd, name)
        with os.fdopen(target, 'wb') as dest:
            while remaining:
                chunk = handle.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise ValueError(f'{name}: input truncated during snapshot')
                dest.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            dest.flush()
            os.fsync(dest.fileno())
        after = os.fstat(handle.fileno())
        if allow_append:
            if after.st_size < before.st_size:
                raise ValueError('decision input truncated during snapshot')
            # 같은 prefix를 다시 읽어 append 외 변화를 검출한다. 완결성 증명은 아니다.
            handle.seek(0)
            verify = hashlib.sha256()
            remaining = before.st_size
            while remaining:
                chunk = handle.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise ValueError('decision prefix changed')
                verify.update(chunk)
                remaining -= len(chunk)
            if verify.digest() != digest.digest():
                raise ValueError('decision prefix changed')
        elif _identity(before) != _identity(after):
            raise ValueError(f'{name}: input changed during snapshot')
    os.fsync(directory_fd)
    return {'path': str(source), 'bytes': before.st_size, 'sha256': digest.hexdigest(),
            'identity_at_open': list(_identity(before)), 'identity_after_copy': list(_identity(after)),
            'snapshot_kind': 'fixed_prefix_finality_unverified' if allow_append else 'stable_file'}


def _limits():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT, MAX_OUTPUT))
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))


def _commands(output, timestamp, version=VERSION):
    version = _version(version)
    study, ledger = str(output / 'study.json'), str(output / 'ledger.jsonl')
    if version == PREGATE_VERSION:
        args = ['--study', study, '--journal', ledger, '--as-of', timestamp,
                '--max-journal-bytes', str(MAX_INPUT)]
        return [('pregate-candidate-observation',
                 [sys.executable, '-I', '-B', '-X', 'pycache_prefix=' + str(output / 'tmp/pycache'),
                  str(ROOT / 'scripts/report_pregate_candidate_observation.py'), *args])]
    shared = ['--study', study, '--journal', ledger, '--max-journal-bytes', str(MAX_INPUT)]
    arguments = (
        ['--study', study, '--ledger', ledger, '--decisions', str(output / 'decisions.jsonl'),
         '--analysis-as-of', timestamp, '--max-bytes', str(MAX_INPUT)],
        [*shared, '--as-of', timestamp],
        [*shared, '--as-of', timestamp],
        [*shared, '--analysis-as-of', timestamp],
    )
    return [(name, [sys.executable, '-I', '-B', '-X', 'pycache_prefix=' + str(output / 'tmp/pycache'),
                    str(ROOT / 'scripts' / script), *args])
            for (name, script), args in zip(TOOLS, arguments)]


def _child(name, argv, output, directory_fd):
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
           'HOME': str(output / 'home'), 'TMPDIR': str(output / 'tmp')}
    stdout = _new_file(directory_fd, name + '.json')
    try:
        stderr = _new_file(directory_fd, name + '.stderr')
    except BaseException:
        os.close(stdout)
        raise
    timed_out = False
    try:
        with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                              cwd=output, env=env, shell=False, start_new_session=True,
                              preexec_fn=_limits) as child:
            try:
                rc = child.wait(timeout=CHILD_TIMEOUT)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(child.pid, signal.SIGKILL)
                rc = child.wait()
        os.fsync(stdout)
        os.fsync(stderr)
        size = os.fstat(stdout).st_size
    finally:
        os.close(stdout)
        os.close(stderr)
    valid_json = False
    if rc == 0 and size < MAX_OUTPUT:
        try:
            valid_json = isinstance(_json(_read_small(output / (name + '.json'), MAX_OUTPUT)), dict)
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            pass
    return {'name': name, 'argv': argv, 'returncode': rc, 'timed_out': timed_out,
            'valid_json': valid_json, 'stdout_bytes': size}


def _pregate_report(report, *, study_sha256, analysis_as_of):
    if (not isinstance(report, dict)
            or report.get('schema_version') != 'pregate-candidate-readout-v1'
            or report.get('study_sha256') != study_sha256
            or report.get('analysis_as_of') != analysis_as_of):
        raise ValueError('pregate report binding mismatch')
    denominator = report.get('denominator')
    validation = report.get('validation')
    if (not isinstance(denominator, dict)
            or denominator.get('population_scope') != 'first_returned_scan_candidates'
            or type(denominator.get('returned_candidates')) is not int
            or type(denominator.get('sampled_candidates')) is not int
            or denominator.get('sampling_limit') != 5
            or denominator['sampled_candidates'] != min(5, denominator['returned_candidates'])
            or not isinstance(report.get('candidates'), list)
            or len(report['candidates']) != denominator['returned_candidates']
            or not isinstance(validation, dict)
            or not all(isinstance(validation.get(key), dict) and validation[key]
                       for key in ('frame', 'selection', 'gates'))):
        raise ValueError('pregate report schema mismatch')
    if (report.get('account_return') is not None or report.get('profitability_pass') is not False
            or report.get('production_eligible') is not False
            or report.get('source_authenticity_verified') is not False):
        raise ValueError('pregate economic invariant mismatch')


def _execute(data, directory_fd, timestamp, manifest_sha):
    output = Path(data['output_dir'])
    pregate = data['version'] == PREGATE_VERSION
    result = {'version': data['version'], 'status': 'blocked', 'analysis_as_of': timestamp,
              'manifest_sha256': manifest_sha, 'source_sha256': data['source_sha256'],
              'commands': [], 'inputs': {}, 'human_review_required': True,
              'decision_recording_finality': 'not_applicable' if pregate else 'unverified', 'account_return': None,
              'profitability_pass': False, 'production_eligible': False}
    if pregate:
        result['source_authenticity_verified'] = False
    try:
        _write_json(directory_fd, 'manifest.json', data)
        for subdir in ('home', 'tmp'):
            os.mkdir(subdir, 0o700, dir_fd=directory_fd)
        result['inputs']['study'] = _snapshot(Path(data['study']), directory_fd, 'study.json', MAX_STUDY)
        if result['inputs']['study']['sha256'] != data['study_sha256']:
            raise ValueError('study hash mismatch')
        result['inputs']['ledger'] = _snapshot(Path(data['ledger']), directory_fd, 'ledger.jsonl', MAX_INPUT)
        if not pregate:
            try:
                result['inputs']['decisions'] = _snapshot(Path(data['decisions']), directory_fd,
                                                         'decisions.jsonl', MAX_INPUT, allow_append=True)
            except FileNotFoundError:
                result['inputs']['decisions'] = {'status': 'missing', 'path': data['decisions']}
        for name, argv in _commands(output, timestamp, data['version']):
            if source_fingerprint(data['version']) != data['source_sha256']:
                raise ValueError('source fingerprint changed')
            result['commands'].append(_child(name, argv, output, directory_fd))
        if source_fingerprint(data['version']) != data['source_sha256']:
            raise ValueError('source fingerprint changed')
        if not all(c['returncode'] == 0 and c['valid_json'] for c in result['commands']):
            raise ValueError('one or more diagnostics failed; inspect command receipts')
        if pregate:
            report = _json(_read_small(output / 'pregate-candidate-observation.json', MAX_OUTPUT))
            _pregate_report(report, study_sha256=data['study_sha256'], analysis_as_of=timestamp)
        else:
            readiness = _json(_read_small(output / 'readiness.json', MAX_OUTPUT))
            journal, technical = readiness.get('journal', {}), readiness.get('technical', {})
            if not (isinstance(journal, dict) and isinstance(technical, dict)
                    and journal.get('verified') is True and journal.get('complete') is True
                    and type(journal.get('dropped_records')) is int and journal['dropped_records'] == 0
                    and technical.get('technical_ready') is True):
                raise ValueError('capture integrity/completeness gate blocked')
        result['status'] = 'diagnostics_complete'
        result['note'] = ('Pregate diagnostics completed; inspect observed prices, states, and limitations. '
                          'This is not price evaluability or profit.' if pregate else
                          'Commands completed; inspect individual reports and missing evidence. This is not price evaluability or profit.')
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, subprocess.SubprocessError) as exc:
        result['error'] = str(exc)
    _write_json(directory_fd, 'summary.json', result)
    return result


def run(path, *, now=None):
    """now는 단위 테스트 주입 전용. CLI는 실제 aware UTC 시계만 사용한다."""
    clock = now if now is not None else lambda: datetime.now(timezone.utc)
    output_fd = lock_fd = None
    try:
        data, manifest_sha = _manifest(path)
        if not _window(data, clock()):
            return {'status': 'time_rejected'}
        output_path = Path(data['output_dir'])
        # 기존 once가 있으면 소스/입력 상태와 무관하게 재실행을 거부한다.
        try:
            existing = _directory(output_path)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            try:
                os.stat('attempted.json', dir_fd=existing, follow_symlinks=False)
                return {'status': 'already_attempted'}
            except FileNotFoundError:
                pass
            finally:
                os.close(existing)
        if source_fingerprint(data['version']) != data['source_sha256']:
            raise ValueError('source fingerprint mismatch')
        output_fd = _output(output_path)
        lock_fd = os.open('run.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                          0o600, dir_fd=output_fd)
        lock_info = os.fstat(lock_fd)
        if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1
                or lock_info.st_uid != os.geteuid() or stat.S_IMODE(lock_info.st_mode) != 0o600):
            raise ValueError('private regular lock file required')
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'busy'}
        actual = clock()
        if not _window(data, actual):
            return {'status': 'time_rejected'}
        try:
            _write_json(output_fd, 'attempted.json', {'version': data['version'], 'attempted_at': actual.isoformat(),
                                                     'manifest_sha256': manifest_sha,
                                                     'source_sha256': data['source_sha256'],
                                                     'state': 'attempted_no_automatic_retry'})
        except FileExistsError:
            return {'status': 'already_attempted'}
        return _execute(data, output_fd, actual.isoformat(), manifest_sha)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
        return {'status': 'invalid_manifest', 'error': str(exc)}
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if output_fd is not None:
            os.close(output_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--profile', choices=tuple(PROFILE_ALIASES))
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--check', action='store_true')
    group.add_argument('--source-fingerprint', action='store_true')
    args = parser.parse_args(argv)
    if args.source_fingerprint:
        if args.manifest is not None:
            parser.error('--source-fingerprint does not accept --manifest')
        print(source_fingerprint(args.profile or VERSION))
        return 0
    if args.profile is not None:
        parser.error('--profile is only valid with --source-fingerprint')
    if args.manifest is None:
        parser.error('--manifest required')
    result = check(args.manifest) if args.check else run(args.manifest)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return {'diagnostics_complete': 0, 'preflight_ok': 0, 'blocked': 2,
            'already_attempted': 3, 'busy': 3, 'time_rejected': 4}.get(result['status'], 5)


if __name__ == '__main__':
    raise SystemExit(main())
