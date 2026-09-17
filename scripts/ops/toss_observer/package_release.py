#!/usr/bin/env python3
"""명시 wheelhouse와 승인 소스 closure만 쓰는 오프라인 릴리스 빌더."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys


class PackageError(Exception):
    pass


def launcher_helpers():
    path = Path(__file__).with_name('launcher.py')
    spec = importlib.util.spec_from_file_location('toss_release_launcher', path)
    module = importlib.util.module_from_spec(spec)
    # mtime 기반 pyc를 실행하지 않고 정확히 지정한 helper 소스를 사용한다.
    exec(compile(regular_file(path).read_bytes(), str(path), 'exec'), module.__dict__)
    return module


def regular_file(path):
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise PackageError('unsafe_source')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PackageError('unsafe_source')
    return path


def source_files(repo):
    repo = Path(repo).absolute()
    directory = repo / 'src/data/providers/toss'
    if directory.is_symlink() or not directory.is_dir():
        raise PackageError('unsafe_source')
    files = list(directory.glob('*.py'))
    fixed = ('src/__init__.py', 'src/data/__init__.py', 'src/data/providers/__init__.py',
        'src/utils/__init__.py', 'src/utils/data_freshness.py', 'src/utils/loop_heartbeat.py',
        'src/schedulers/__init__.py', 'src/schedulers/toss_shadow.py')
    files.extend(repo / name for name in fixed)
    # 통합 전 작업공간에는 서비스 파일이 없을 수 있다. package()는 별도 필수검사.
    files.extend((repo / 'src/observation').glob('*.py'))
    try:
        return sorted(regular_file(path) for path in files)
    except OSError:
        raise PackageError('source_missing') from None


def pip_command(*, python, wheelhouse, lock, target):
    return [str(python), '-I', '-m', 'pip', '--isolated', 'install', '--no-index',
        '--find-links', str(wheelhouse), '--require-hashes', '--only-binary=:all:',
        '--no-compile', '--no-cache-dir', '--disable-pip-version-check', '--target', str(target), '-r', str(lock)]


def package(*, repo, output, wheelhouse, release_id, python=sys.executable):
    repo, output, wheelhouse = map(lambda p: Path(p).absolute(), (repo, output, wheelhouse))
    if output.exists() or output.is_symlink():
        raise PackageError('destination_exists')
    for parent in output.parents:
        if parent.is_symlink():
            raise PackageError('unsafe_destination')
    if not output.parent.is_dir() or wheelhouse.is_symlink() or not wheelhouse.is_dir():
        raise PackageError('invalid_directory')
    files = source_files(repo)
    for name in ('__init__.py', 'toss_service.py', 'toss_positions.py', 'toss_deployment.py'):
        if repo / 'src/observation' / name not in files:
            raise PackageError('source_missing')
    launcher = regular_file(repo / 'scripts/ops/toss_observer/launcher.py')
    lock = regular_file(Path(__file__).with_name('requirements.lock'))
    helper = launcher_helpers()
    output.mkdir(mode=0o755)
    for source in files:
        dest = output / 'app' / source.relative_to(repo)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(source.read_bytes())
    (output / 'bootstrap').mkdir()
    (output / 'bootstrap/launcher.py').write_bytes(launcher.read_bytes())
    # 환경의 credentials/proxy/PIP_*를 자식에 넘기지 않는다.
    completed = subprocess.run(pip_command(python=python, wheelhouse=wheelhouse,
        lock=lock, target=output / 'deps'), env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    if completed.returncode:
        raise PackageError('offline_dependency_install_failed')
    for path in output.rglob('*'):
        if path.is_symlink():
            raise PackageError('unsafe_dependency')
        path.chmod(0o755 if path.is_dir() else 0o644)
    output.chmod(0o755)
    manifest = helper.build_manifest(output, release_id=release_id,
        python_version='.'.join(map(str, sys.version_info[:3])))
    raw = helper.canonical_bytes(manifest)
    (output / 'manifest.json').write_bytes(raw)
    (output / 'manifest.json').chmod(0o644)
    return {'artifact_sha256': hashlib.sha256(raw).hexdigest(), 'release_id': release_id}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--wheelhouse', type=Path, required=True)
    parser.add_argument('--release-id', required=True)
    args = parser.parse_args(argv)
    try:
        result = package(repo=args.repo, output=args.output, wheelhouse=args.wheelhouse,
                         release_id=args.release_id)
        print(result['artifact_sha256'])
        return 0
    except Exception:
        print('release_build_failed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
