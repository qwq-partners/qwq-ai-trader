"""승인 import closure만 고정하고 offline wheel 설치를 강제한다."""
import importlib.util
from pathlib import Path
import pytest


def load():
    path = Path(__file__).resolve().parents[1] / 'scripts/ops/toss_observer/package_release.py'
    assert path.exists(), 'packager 구현 필요'
    spec = importlib.util.spec_from_file_location('observer_package', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_allowlist_excludes_trading_entry_and_credentials(tmp_path):
    m = load()
    repo = Path(__file__).resolve().parents[1]
    sources = m.source_files(repo)
    names = [str(p.relative_to(repo)) for p in sources]
    assert 'src/data/providers/toss/token.py' in names
    assert 'src/utils/data_freshness.py' in names
    assert 'scripts/run_trader.py' not in names
    assert not any('.env' in name or 'broker' in name or 'engine' in name for name in names)


def test_source_symlink_rejected(tmp_path):
    m = load()
    (tmp_path / 'src/data/providers').mkdir(parents=True)
    (tmp_path / 'src/data/providers/toss').symlink_to('/tmp')
    with pytest.raises(m.PackageError):
        m.source_files(tmp_path)


def test_wheel_command_never_downloads_or_compiles(tmp_path):
    m = load()
    command = m.pip_command(python='/usr/bin/python3', wheelhouse=tmp_path,
                            lock=tmp_path / 'lock', target=tmp_path / 'deps')
    assert '--no-index' in command and '--require-hashes' in command
    assert '--no-compile' in command and '--only-binary=:all:' in command
    assert '--target' in command and '--no-deps' not in command


def test_reject_existing_or_symlink_destination(tmp_path):
    m = load()
    with pytest.raises(m.PackageError):
        m.package(repo=tmp_path, output=tmp_path, wheelhouse=tmp_path, release_id='test')
