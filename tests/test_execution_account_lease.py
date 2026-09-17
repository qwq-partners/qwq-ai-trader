"""명시 tmp root의 실제 Linux 계좌 lease. spawn도 격리 guard를 먼저 설치한다."""
import conftest as isolation  # 반드시 모든 production import보다 먼저

import errno
import gc
import importlib
import multiprocessing
import os
from pathlib import Path
import stat
import threading
import weakref

import pytest

from src.execution.safety.requests import RequestAccount


def api():
    return importlib.import_module('src.execution.safety.account_lease')


def account(**changes):
    values = dict(account_scope='synthetic-scope', account_no='SYNTHETIC_ACCOUNT',
                  account_product_cd='SYNTHETIC_PRODUCT', environment='prod',
                  endpoint='https://openapi.koreainvestment.com:9443', config_version=1)
    return RequestAccount(**(values | changes))


def private_root(tmp_path):
    root = tmp_path / 'private-leases'
    root.mkdir(mode=0o700)
    return root


def lock_path(root):
    paths = tuple(root.iterdir())
    assert len(paths) == 1
    return paths[0]


def fd_count():
    return len(os.listdir('/proc/self/fd'))


def test_exclusive_acquire_release_keeps_private_empty_lock_file(tmp_path):
    mod, root = api(), private_root(tmp_path)
    lease = mod.AccountLease.acquire(root, account())
    try:
        lease.assert_held()
        path = lock_path(root)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.read_bytes() == b''
        with pytest.raises(mod.LeaseError, match='account_in_use'):
            mod.AccountLease.acquire(root, account())
    finally:
        lease.close()
    lease.close()
    assert path.exists()
    with pytest.raises(mod.LeaseError, match='lease_closed'):
        lease.assert_held()
    replacement = mod.AccountLease.acquire(root, account())
    replacement.close()


@pytest.mark.parametrize('changes', [dict(account_scope='another-alias'), dict(config_version=200)])
def test_alias_and_config_version_cannot_split_account_exclusion(tmp_path, changes):
    mod, root = api(), private_root(tmp_path)
    lease = mod.AccountLease.acquire(root, account())
    try:
        with pytest.raises(mod.LeaseError, match='account_in_use'):
            mod.AccountLease.acquire(root, account(**changes))
    finally:
        lease.close()


@pytest.mark.parametrize('changes', [dict(account_no='OTHER_SYNTHETIC_ACCOUNT'),
                                    dict(account_product_cd='OTHER_SYNTHETIC_PRODUCT')])
def test_different_wire_account_or_product_can_hold_distinct_lease(tmp_path, changes):
    mod, root = api(), private_root(tmp_path)
    one = mod.AccountLease.acquire(root, account())
    try:
        two = mod.AccountLease.acquire(root, account(**changes))
        try:
            one.assert_held()
            two.assert_held()
            assert len(tuple(root.iterdir())) == 2
        finally:
            two.close()
    finally:
        one.close()


@pytest.mark.parametrize('kind', ['missing', 'relative', 'dotdot', 'string', 'file', 'symlink', 'ancestor'])
def test_unsafe_root_is_not_created_or_followed(tmp_path, kind):
    mod, root = api(), private_root(tmp_path)
    target = root
    if kind == 'missing': target = tmp_path / 'missing'
    if kind == 'relative': target = Path('relative-root')
    if kind == 'dotdot': target = root / '..' / root.name
    if kind == 'string': target = str(root)
    if kind == 'file':
        target = tmp_path / 'file'
        target.write_bytes(b'preserve')
    if kind == 'symlink':
        target = tmp_path / 'link'
        target.symlink_to(root, target_is_directory=True)
    if kind == 'ancestor':
        parent = tmp_path / 'parent-link'
        parent.symlink_to(tmp_path, target_is_directory=True)
        target = parent / root.name
    with pytest.raises(mod.LeaseError, match='unsafe_root'):
        mod.AccountLease.acquire(target, account())
    assert not tuple(root.iterdir())
    assert not (tmp_path / 'missing').exists()


@pytest.mark.parametrize('mode', [0o000, 0o500, 0o755, 0o770, 0o1700])
def test_root_requires_exact_0700_without_chmod(tmp_path, mode):
    mod, root = api(), private_root(tmp_path)
    root.chmod(mode)
    try:
        with pytest.raises(mod.LeaseError, match='unsafe_root'):
            mod.AccountLease.acquire(root, account())
        assert stat.S_IMODE(root.stat().st_mode) == mode
    finally:
        root.chmod(0o700)
    assert not tuple(root.iterdir())


def test_parent_directories_do_not_require_private_root_mode(tmp_path):
    mod = api()
    parent = tmp_path / 'shared-parent'
    parent.mkdir(mode=0o755)
    root = private_root(parent)
    lease = mod.AccountLease.acquire(root, account())
    lease.assert_held()
    lease.close()


@pytest.mark.parametrize('field,value', [('environment', 'virtual'), ('endpoint', 'https://invalid.test'),
    ('account_no', ''), ('account_product_cd', 1), ('config_version', True), ('account_scope', '')])
def test_tampered_request_account_revalidated_before_filesystem(tmp_path, field, value):
    mod, root = api(), private_root(tmp_path)
    bad = account()
    object.__setattr__(bad, field, value)
    with pytest.raises(mod.LeaseError, match='invalid_account'):
        mod.AccountLease.acquire(root, bad)
    assert not tuple(root.iterdir())


def test_plain_mapping_is_not_account_authority(tmp_path):
    mod, root = api(), private_root(tmp_path)
    with pytest.raises(mod.LeaseError, match='invalid_account'):
        mod.AccountLease.acquire(root, dict(account_no='SYNTHETIC_ACCOUNT'))
    assert not tuple(root.iterdir())


def test_identity_repr_and_errors_do_not_disclose_original_account(tmp_path):
    mod, root = api(), private_root(tmp_path)
    lease = mod.AccountLease.acquire(root, account())
    try:
        with pytest.raises(mod.LeaseError) as caught:
            mod.AccountLease.acquire(root, account())
        text = repr(lease) + repr(caught.value) + str(caught.value) + lock_path(root).name
        for secret in ('SYNTHETIC_ACCOUNT', 'SYNTHETIC_PRODUCT', 'synthetic-scope'):
            assert secret not in text
        assert len(lock_path(root).stem) == 64
        assert caught.value.__cause__ is None
    finally:
        lease.close()


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo', 'directory', 'mode', 'special_mode'])
def test_existing_unsafe_lock_rejected_without_repair(tmp_path, kind):
    mod, root = api(), private_root(tmp_path)
    lease = mod.AccountLease.acquire(root, account())
    path = lock_path(root)
    lease.close()
    path.unlink()
    if kind == 'symlink':
        target = tmp_path / 'untouched'
        target.write_bytes(b'preserve')
        path.symlink_to(target)
    if kind == 'hardlink':
        target = tmp_path / 'hard-target'
        target.write_bytes(b'preserve')
        target.chmod(0o600)
        os.link(target, path)
    if kind == 'fifo': os.mkfifo(path, 0o600)
    if kind == 'directory': path.mkdir(mode=0o700)
    if kind in ('mode', 'special_mode'):
        path.write_bytes(b'preserve')
        path.chmod(0o644 if kind == 'mode' else 0o4600)
    before = fd_count()
    with pytest.raises(mod.LeaseError, match='unsafe_lock'):
        mod.AccountLease.acquire(root, account())
    assert fd_count() == before
    assert path.lstat().st_nlink >= 1
    if kind in ('symlink', 'hardlink'): assert target.read_bytes() == b'preserve'
    if kind in ('mode', 'special_mode'): assert path.read_bytes() == b'preserve'


@pytest.mark.parametrize('kind', ['lock_unlink', 'lock_replace', 'lock_chmod', 'lock_hardlink',
                                 'lock_symlink', 'root_replace', 'ancestor_replace'])
def test_path_changes_permanently_invalidate_live_lease(tmp_path, kind):
    mod = api()
    parent = tmp_path / 'parent'
    parent.mkdir()
    root = private_root(parent)
    lease = mod.AccountLease.acquire(root, account())
    path = lock_path(root)
    try:
        if kind == 'lock_unlink': path.unlink()
        if kind == 'lock_replace':
            path.rename(root / 'old')
            path.write_bytes(b'')
            path.chmod(0o600)
        if kind == 'lock_chmod': path.chmod(0o644)
        if kind == 'lock_hardlink': os.link(path, root / 'second-name')
        if kind == 'lock_symlink':
            path.rename(root / 'old')
            path.symlink_to(root / 'old')
        if kind == 'root_replace':
            root.rename(parent / 'old-root')
            root.mkdir(mode=0o700)
        if kind == 'ancestor_replace':
            parent.rename(tmp_path / 'old-parent')
            parent.mkdir()
            root.mkdir(mode=0o700)
        with pytest.raises(mod.LeaseError, match='lease_invalid'):
            lease.assert_held()
    finally:
        lease.close()


@pytest.mark.parametrize('target', ['root', 'lock'])
def test_restoring_mode_cannot_revive_invalid_lease(tmp_path, target):
    mod, root = api(), private_root(tmp_path)
    lease = mod.AccountLease.acquire(root, account())
    path = root if target == 'root' else lock_path(root)
    original = 0o700 if target == 'root' else 0o600
    try:
        path.chmod(0o755 if target == 'root' else 0o644)
        with pytest.raises(mod.LeaseError, match='lease_invalid'):
            lease.assert_held()
        path.chmod(original)
        with pytest.raises(mod.LeaseError, match='lease_invalid'):
            lease.assert_held()
        with pytest.raises(mod.LeaseError, match='account_in_use'):
            mod.AccountLease.acquire(root, account())
    finally:
        path.chmod(original)
        lease.close()


def test_root_owner_is_checked_without_chown(tmp_path, monkeypatch):
    mod, root = api(), private_root(tmp_path)
    uid = os.geteuid()
    with monkeypatch.context() as patch:
        patch.setattr(mod.os, 'geteuid', lambda: uid + 1)
        with pytest.raises(mod.LeaseError, match='unsafe_root'):
            mod.AccountLease.acquire(root, account())
    assert not tuple(root.iterdir())


def test_lock_owner_checked_separately_from_root(tmp_path, monkeypatch):
    mod, root = api(), private_root(tmp_path)
    real = mod.os.fstat
    def different_file_uid(fd):
        original = real(fd)
        values = list(original)
        if stat.S_ISREG(original.st_mode): values[4] += 1
        return os.stat_result(values)
    with monkeypatch.context() as patch:
        patch.setattr(mod.os, 'fstat', different_file_uid)
        with pytest.raises(mod.LeaseError, match='unsafe_lock'):
            mod.AccountLease.acquire(root, account())


def test_assert_checks_current_uid_and_latches_failure(tmp_path, monkeypatch):
    mod, root = api(), private_root(tmp_path)
    lease = mod.AccountLease.acquire(root, account())
    uid = os.geteuid()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(mod.os, 'geteuid', lambda: uid + 1)
            with pytest.raises(mod.LeaseError, match='lease_invalid'):
                lease.assert_held()
        with pytest.raises(mod.LeaseError, match='lease_invalid'):
            lease.assert_held()
    finally:
        lease.close()


@pytest.mark.parametrize('target', ['root', 'lock'])
def test_acquire_revalidates_paths_after_flock(tmp_path, monkeypatch, target):
    mod, root = api(), private_root(tmp_path)
    real = mod.fcntl.flock
    def replace_after_lock(fd, operation):
        result = real(fd, operation)
        if target == 'root':
            root.rename(tmp_path / 'old-root')
            root.mkdir(mode=0o700)
        else:
            path = lock_path(root)
            path.rename(root / 'old-lock')
            path.write_bytes(b'')
            path.chmod(0o600)
        return result
    before = fd_count()
    with monkeypatch.context() as patch:
        patch.setattr(mod.fcntl, 'flock', replace_after_lock)
        try:
            lease = mod.AccountLease.acquire(root, account())
        except mod.LeaseError:
            pass
        else:
            lease.close()
            pytest.fail('a replaced path was admitted after flock')
    assert fd_count() == before


def test_checks_never_reacquire_and_close_never_unlocks(tmp_path, monkeypatch):
    mod, root = api(), private_root(tmp_path)
    lease = mod.AccountLease.acquire(root, account())
    def forbidden_flock(*args):
        raise AssertionError('flock called outside acquire')
    with monkeypatch.context() as patch:
        patch.setattr(mod.fcntl, 'flock', forbidden_flock)
        lease.assert_held()
        lease.close()
        lease.close()


def test_existing_file_contents_and_age_are_not_lease_authority(tmp_path):
    mod, root = api(), private_root(tmp_path)
    lease = mod.AccountLease.acquire(root, account())
    path = lock_path(root)
    lease.close()
    path.write_bytes(b'legacy-content-not-a-pid')
    os.utime(path, (1, 1))
    second = mod.AccountLease.acquire(root, account())
    second.assert_held()
    second.close()
    assert path.read_bytes() == b'legacy-content-not-a-pid'
    assert path.stat().st_mtime == 1


def test_contended_acquisition_does_not_leak_fds(tmp_path):
    mod, root = api(), private_root(tmp_path)
    before = fd_count()
    lease = mod.AccountLease.acquire(root, account())
    holding = fd_count()
    try:
        for _ in range(20):
            with pytest.raises(mod.LeaseError, match='account_in_use'):
                mod.AccountLease.acquire(root, account())
        assert fd_count() == holding
    finally:
        lease.close()
    assert fd_count() == before


def _spawn_probe(root, changes, channel, hold):
    # 이 모듈의 첫 import conftest가 spawn interpreter에도 먼저 실행된다.
    mod = api()
    lease = None
    try:
        try:
            lease = mod.AccountLease.acquire(Path(root), account(**changes))
            lease.assert_held()
            result = 'held'
        except mod.LeaseError as exc:
            result = exc.reason
        channel.send((result, tuple(isolation.VIOLATIONS)))
        if hold and lease is not None:
            assert channel.recv() == 'close'
    finally:
        if lease is not None: lease.close()
        channel.send(('closed', tuple(isolation.VIOLATIONS)))
        channel.close()


def _launch_probe(root, *, changes=None, hold=False):
    ctx = multiprocessing.get_context('spawn')
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_spawn_probe, args=(str(root), changes or {}, child, hold))
    process.start()
    child.close()
    if not parent.poll(10):
        process.terminate()
        process.join(10)
        parent.close()
        process.close()
        pytest.fail('spawn child did not report')
    outcome, violations = parent.recv()
    assert violations == ()
    return process, parent, outcome


def _join(process, channel, *, release=False, final_report=False):
    if release: channel.send('close')
    process.join(10)
    if process.is_alive():
        process.terminate()
        process.join(10)
        pytest.fail('child did not finish')
    assert process.exitcode == 0
    if final_report:
        assert channel.poll(10)
        assert channel.recv() == ('closed', ())
    channel.close()
    process.close()


def test_two_spawn_processes_share_account_lock_not_alias_or_version(tmp_path):
    root = private_root(tmp_path)
    first, channel, outcome = _launch_probe(root, hold=True)
    assert outcome == 'held'
    try:
        second, second_channel, outcome = _launch_probe(root, changes=dict(account_scope='other-db-scope', config_version=900))
        _join(second, second_channel, final_report=True)
        assert outcome == 'account_in_use'
        other, other_channel, outcome = _launch_probe(root, changes=dict(account_no='DIFFERENT_ACCOUNT'))
        _join(other, other_channel, final_report=True)
        assert outcome == 'held'
    finally:
        _join(first, channel, release=True, final_report=True)
    replacement, replacement_channel, outcome = _launch_probe(root)
    _join(replacement, replacement_channel, final_report=True)
    assert outcome == 'held'


def _fork_probe(lease, root, before_inodes, lock_fd, channel):
    mod = api()
    inherited = []
    for fd, expected in before_inodes.items():
        try:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == expected: inherited.append(fd)
        except OSError:
            pass
    reason = None
    try:
        lease.assert_held()
    except mod.LeaseError as exc:
        reason = exc.reason
    descriptor = os.open(Path(root).parent / 'child-private-file', os.O_RDWR | os.O_CREAT, 0o600)
    if descriptor != lock_fd:
        os.dup2(descriptor, lock_fd)
        os.close(descriptor)
    lease.close()
    try:
        os.fstat(lock_fd)
        reused_survives = True
        os.close(lock_fd)
    except OSError:
        reused_survives = False
    try:
        separate = mod.AccountLease.acquire(Path(root), account())
    except mod.LeaseError as exc:
        separate_reason = exc.reason
    else:
        separate_reason = 'held'
        separate.close()
    channel.send((reason, inherited, reused_survives, separate_reason, tuple(isolation.VIOLATIONS)))
    channel.close()


def test_fork_child_cannot_use_unlock_or_double_close_parent_lease(tmp_path):
    mod, root = api(), private_root(tmp_path)
    lease = mod.AccountLease.acquire(root, account())
    descriptors = [lease._fd] + [fd for fd, _ in lease._directories]
    before_inodes = {fd: (os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in descriptors}
    ctx = multiprocessing.get_context('fork')
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_fork_probe, args=(lease, str(root), before_inodes, lease._fd, child))
    try:
        process.start()
        child.close()
        assert parent.poll(10)
        reason, inherited, reused, separate, violations = parent.recv()
        _join(process, parent)
        assert reason == 'forked_lease'
        assert inherited == []
        assert reused is True
        assert separate == 'account_in_use'
        assert violations == ()
        lease.assert_held()
    finally:
        lease.close()


def _fork_wait(channel):
    channel.send(tuple(isolation.VIOLATIONS))
    assert channel.recv() == 'close'
    channel.close()


def test_idle_fork_child_does_not_extend_parent_lease_after_parent_close(tmp_path):
    mod, root = api(), private_root(tmp_path)
    lease = mod.AccountLease.acquire(root, account())
    ctx = multiprocessing.get_context('fork')
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_fork_wait, args=(child,))
    try:
        process.start()
        child.close()
        assert parent.poll(10)
        assert parent.recv() == ()
        lease.close()
        replacement = mod.AccountLease.acquire(root, account())
        replacement.close()
    finally:
        lease.close()
        _join(process, parent, release=True)


def test_live_fds_are_close_on_exec(tmp_path):
    mod, root = api(), private_root(tmp_path)
    lease = mod.AccountLease.acquire(root, account())
    try:
        descriptors = [lease._fd] + [fd for fd, _ in lease._directories]
        assert all(not os.get_inheritable(fd) for fd in descriptors)
    finally:
        lease.close()


def test_repeated_close_does_not_retain_lease_objects_or_descriptors(tmp_path):
    mod, root = api(), private_root(tmp_path)
    before = fd_count()
    references = []
    for _ in range(30):
        lease = mod.AccountLease.acquire(root, account())
        references.append(weakref.ref(lease))
        lease.close()
    del lease
    gc.collect()
    assert all(reference() is None for reference in references)
    assert fd_count() == before


def test_flock_error_does_not_disclose_account_or_leak_fds(tmp_path, monkeypatch):
    mod, root = api(), private_root(tmp_path)
    def fail_flock(*args):
        raise OSError(errno.EIO, 'SYNTHETIC_ACCOUNT private failure')
    before = fd_count()
    with monkeypatch.context() as patch:
        patch.setattr(mod.fcntl, 'flock', fail_flock)
        with pytest.raises(mod.LeaseError, match='lease_io_error') as caught:
            mod.AccountLease.acquire(root, account())
    assert 'SYNTHETIC_ACCOUNT' not in str(caught.value)
    assert fd_count() == before


def test_unreferenced_lease_does_not_leave_orphan_descriptors(tmp_path):
    mod, root = api(), private_root(tmp_path)
    before = fd_count()
    lease = mod.AccountLease.acquire(root, account())
    descriptors = [lease._fd] + [fd for fd, _ in lease._directories]
    expected = {fd: (os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in descriptors}
    ref = weakref.ref(lease)
    del lease
    gc.collect()
    try:
        assert ref() is None
        assert fd_count() == before
    finally:
        # RED에서도 이 시험이 만든 tmp 파일 FD를 다음 시험으로 넘기지 않는다.
        for fd in descriptors:
            try:
                info = os.fstat(fd)
                if (info.st_dev, info.st_ino) == expected[fd]: os.close(fd)
            except OSError: pass


@pytest.mark.parametrize('field', ['account_no', 'account_product_cd', 'environment'])
def test_missing_account_field_is_static_invalid_account(tmp_path, field):
    mod, root = api(), private_root(tmp_path)
    bad = account()
    object.__delattr__(bad, field)
    with pytest.raises(mod.LeaseError, match='invalid_account'):
        mod.AccountLease.acquire(root, bad)
    assert not tuple(root.iterdir())


def test_assert_stat_failure_is_permanent_and_does_not_leak_raw_error(tmp_path, monkeypatch):
    mod, root = api(), private_root(tmp_path)
    lease = mod.AccountLease.acquire(root, account())
    def broken_stat(*args, **kwargs):
        raise OSError(errno.EIO, 'SYNTHETIC_ACCOUNT raw failure')
    try:
        with monkeypatch.context() as patch:
            patch.setattr(mod.os, 'stat', broken_stat)
            with pytest.raises(mod.LeaseError, match='lease_invalid') as caught:
                lease.assert_held()
        assert 'SYNTHETIC_ACCOUNT' not in str(caught.value)
        with pytest.raises(mod.LeaseError, match='lease_invalid'):
            lease.assert_held()
    finally:
        lease.close()


def test_close_error_drains_other_fds_without_retrying_closed_number(tmp_path, monkeypatch):
    mod, root = api(), private_root(tmp_path)
    before = fd_count()
    lease = mod.AccountLease.acquire(root, account())
    target, real, calls = lease._fd, os.close, []
    def uncertain_close(fd):
        calls.append(fd)
        real(fd)
        if fd == target: raise OSError(errno.EINTR, 'SYNTHETIC_ACCOUNT close uncertain')
    with monkeypatch.context() as patch:
        patch.setattr(mod.os, 'close', uncertain_close)
        with pytest.raises(mod.LeaseError, match='lease_io_error'):
            lease.close()
        lease.close()
    assert calls.count(target) == 1
    assert fd_count() == before


def _fork_check_partial_fd(fd, expected, channel):
    try:
        info = os.fstat(fd)
        inherited = (info.st_dev, info.st_ino) == expected
    except OSError:
        inherited = False
    channel.send((inherited, tuple(isolation.VIOLATIONS)))
    channel.close()


def test_fork_serializes_with_another_threads_partial_acquire(tmp_path, monkeypatch):
    mod, root = api(), private_root(tmp_path)
    lock_opened, fork_waiting = threading.Event(), threading.Event()
    output, errors = {}, []
    main_thread = threading.get_ident()
    real_lock, real_open = mod._registry_lock, mod.os.open
    class ObservedLock:
        def acquire(self):
            if threading.get_ident() == main_thread and lock_opened.is_set():
                fork_waiting.set()
            return real_lock.acquire()
        def release(self): return real_lock.release()
        def __enter__(self): self.acquire(); return self
        def __exit__(self, *exc): self.release()
    def paused_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if str(path).endswith('.lock'):
            output['fd'] = fd
            info = os.fstat(fd)
            output['inode'] = (info.st_dev, info.st_ino)
            lock_opened.set()
            assert fork_waiting.wait(10)
        return fd
    def acquire_in_thread():
        try: output['lease'] = mod.AccountLease.acquire(root, account())
        except BaseException as exc: errors.append(exc)
    with monkeypatch.context() as patch:
        patch.setattr(mod, '_registry_lock', ObservedLock())
        patch.setattr(mod.os, 'open', paused_open)
        worker = threading.Thread(target=acquire_in_thread)
        worker.start()
        assert lock_opened.wait(10)
        ctx = multiprocessing.get_context('fork')
        parent, child = ctx.Pipe()
        process = ctx.Process(target=_fork_check_partial_fd,
                              args=(output['fd'], output['inode'], child))
        try:
            process.start()
            child.close()
            assert parent.poll(10)
            assert parent.recv() == (False, ())
            _join(process, parent)
        finally:
            fork_waiting.set()
            worker.join(10)
            if 'lease' in output: output['lease'].close()
        assert not worker.is_alive()
        assert not errors
