"""명시 로컬 root의 KIS 계좌 독점. 거래/startup 승인이나 분산 lease가 아니다."""
from __future__ import annotations

from dataclasses import replace
import errno
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import threading
import weakref

from .requests import RequestAccount


# hook는 모듈당 한 번만 등록한다. registry는 lease 객체/계좌가 아니라 FD 목록만 갖는다.
_registry_lock = threading.RLock()
_descriptors: dict[object, list[int]] = {}


def _release(token: object, pid: int, *, quiet: bool = False) -> None:
    if pid != os.getpid():
        return
    with _registry_lock:
        failed = False
        for fd in reversed(_descriptors.pop(token, [])):
            try:
                os.close(fd)
            except OSError:
                # close 재시도는 재사용된 FD를 닫을 수 있다. 모든 다른 FD는 계속 정리한다.
                failed = True
        if failed and not quiet:
            raise LeaseError('lease_io_error') from None


def _finalize(token: object, pid: int) -> None:
    _release(token, pid, quiet=True)


def _before_fork() -> None:
    # 다른 thread의 acquire/close 중간 FD를 fork가 놓치지 않게 한다.
    _registry_lock.acquire()


def _after_fork_parent() -> None:
    _registry_lock.release()


def _after_fork_child() -> None:
    try:
        for descriptors in _descriptors.values():
            for fd in reversed(descriptors):
                try:
                    os.close(fd)  # LOCK_UN은 부모가 공유하는 flock까지 풀므로 절대 쓰지 않는다.
                except OSError:
                    pass
        _descriptors.clear()
    finally:
        _registry_lock.release()


os.register_at_fork(before=_before_fork, after_in_parent=_after_fork_parent,
                    after_in_child=_after_fork_child)


class LeaseError(RuntimeError):
    """계좌 원문/OS path를 싣지 않는 정적 거부 사유."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _identity(account: RequestAccount) -> str:
    try:
        if type(account) is not RequestAccount:
            raise ValueError
        account = replace(account)
    except (ValueError, TypeError, AttributeError):
        raise LeaseError('invalid_account') from None
    # RequestAccount가 이미 wire 환경/endpoint를 검증한다. 별칭/version은 독점 키가 아니다.
    payload = ['kis-account-lease-v1', 'KIS', account.environment,
               account.account_no, account.account_product_cd]
    return sha256(json.dumps(payload, ensure_ascii=True, separators=(',', ':')).encode('ascii')).hexdigest()


def _root_info(info):
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise LeaseError('unsafe_root')


def _lock_info(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise LeaseError('unsafe_lock')


def _inode(info):
    return info.st_dev, info.st_ino


class AccountLease:
    """같은 host/고정 root의 협력 프로세스에만 적용한다. 설치자가 수명을 닫는다."""

    def __init__(self):
        self._pid = os.getpid()
        self._token = object()
        self._owned_fds = []
        _descriptors[self._token] = self._owned_fds
        self._finalizer = weakref.finalize(self, _finalize, self._token, self._pid)
        self._directories = []
        self._directory_inodes = {}
        self._fd = None
        self._closed = True
        self._invalid = False
        self._name = ''

    @classmethod
    def acquire(cls, root: Path, account: RequestAccount) -> AccountLease:
        with _registry_lock:
            return cls._acquire(root, account)

    @classmethod
    def _acquire(cls, root: Path, account: RequestAccount) -> AccountLease:
        identity = _identity(account)
        if not isinstance(root, Path) or not root.is_absolute() or '..' in root.parts:
            raise LeaseError('unsafe_root')
        lease = cls()
        lease._name = identity + '.lock'
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        stage = 'unsafe_root'
        try:
            fd = os.open('/', flags)
            lease._owned_fds.append(fd)
            lease._directories.append((fd, None))
            lease._directory_inodes[fd] = _inode(os.fstat(fd))
            for name in root.parts[1:]:
                fd = os.open(name, flags, dir_fd=fd)
                lease._owned_fds.append(fd)
                lease._directories.append((fd, name))
                lease._directory_inodes[fd] = _inode(os.fstat(fd))
            _root_info(os.fstat(fd))
            stage = 'unsafe_lock'
            lock_flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            lease._fd = os.open(lease._name, lock_flags | os.O_CREAT, 0o600, dir_fd=fd)
            lease._owned_fds.append(lease._fd)
            _lock_info(os.fstat(lease._fd))
            lease._file_inode = _inode(os.fstat(lease._fd))
            try:
                fcntl.flock(lease._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EACCES):
                    raise LeaseError('account_in_use') from None
                raise LeaseError('lease_io_error') from None
            lease._closed = False
            lease.assert_held()
            return lease
        except BaseException as exc:
            lease.close()
            if isinstance(exc, LeaseError):
                raise
            if isinstance(exc, OSError):
                raise LeaseError(stage) from None
            raise

    def assert_held(self) -> None:
        with _registry_lock:
            self._assert_held()

    def _assert_held(self) -> None:
        if self._pid != os.getpid():
            self._invalid = True
            raise LeaseError('forked_lease')
        if self._closed:
            raise LeaseError('lease_closed')
        if self._invalid:
            raise LeaseError('lease_invalid')
        try:
            for index, (fd, name) in enumerate(self._directories):
                info = os.fstat(fd)
                linked = (os.stat('/', follow_symlinks=False) if index == 0 else
                          os.stat(name, dir_fd=self._directories[index - 1][0], follow_symlinks=False))
                if (not stat.S_ISDIR(info.st_mode) or not stat.S_ISDIR(linked.st_mode)
                        or _inode(info) != self._directory_inodes[fd] or _inode(info) != _inode(linked)):
                    raise LeaseError('lease_invalid')
            _root_info(info)
            _root_info(linked)
            info = os.fstat(self._fd)
            linked = os.stat(self._name, dir_fd=self._directories[-1][0], follow_symlinks=False)
            _lock_info(info)
            _lock_info(linked)
            if _inode(info) != self._file_inode or _inode(info) != _inode(linked):
                raise LeaseError('lease_invalid')
        except (OSError, LeaseError):
            self._invalid = True
            raise LeaseError('lease_invalid') from None

    def close(self) -> None:
        with _registry_lock:
            self._closed = True
            self._fd, self._directories = None, []
            self._directory_inodes.clear()
            self._finalizer.detach()
            # child hook가 이미 닫은 숫자가 재사용되어도 _release의 PID gate가 다시 닫지 않는다.
            _release(self._token, self._pid)
