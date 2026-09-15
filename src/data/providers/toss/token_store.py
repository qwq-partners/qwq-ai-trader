"""명시 경로 전용 토큰 저장소. 운영 경로·환경변수·네트워크를 참조하지 않는다."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import time
import uuid

ORIGIN = "https://openapi.tossinvest.com"
MAX_BYTES = 65536
MAX_LIFETIME = 366 * 86400


class TokenError(Exception):
    """외부 예외/인증정보를 포함하지 않는 고정 사유."""

    CODES = frozenset({"disabled", "auth_unavailable", "issuance_unknown", "unsafe_storage",
                       "storage_failed", "invalid_cache", "deadline_exceeded", "approval_required"})

    def __init__(self, code: str):
        self.code = code if code in self.CODES else "auth_unavailable"
        super().__init__(self.code)


@dataclass(frozen=True)
class TokenRecord:
    access_token: str = field(repr=False)
    issued_at: datetime
    expires_at: datetime
    generation: int
    client_identity: str
    origin: str = ORIGIN
    schema_version: int = 1
    issuer_pid: int = field(default_factory=os.getpid)

    def validate(self, identity: str):
        if (not isinstance(self.access_token, str) or not self.access_token
                or len(self.access_token) > 16384
                or any(ord(c) < 33 or ord(c) > 126 for c in self.access_token)
                or self.client_identity != identity or self.origin != ORIGIN
                or type(self.schema_version) is not int or self.schema_version != 1
                or type(self.generation) is not int or self.generation < 1
                or type(self.issuer_pid) is not int or self.issuer_pid < 1):
            raise TokenError("invalid_cache")
        for value in (self.issued_at, self.expires_at):
            if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
                raise TokenError("invalid_cache")
        lifetime = (self.expires_at - self.issued_at).total_seconds()
        if not 0 < lifetime <= MAX_LIFETIME:
            raise TokenError("invalid_cache")


class SecureTokenStore:
    """생성자는 I/O가 없다. leaf만 생성하고 모든 경로의 symlink를 거부한다."""

    def __init__(self, directory: Path, client_identity: str):
        self.directory = Path(directory)
        if (not self.directory.is_absolute() or ".." in self.directory.parts
                or len(self.directory.parts) < 2
                or not isinstance(client_identity, str) or not client_identity.strip()):
            raise TokenError("unsafe_storage")
        self.client_identity = client_identity

    @staticmethod
    def _check(metadata, *, directory=False):
        wanted = 0o700 if directory else 0o600
        kind_ok = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
        if (not kind_ok or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != wanted
                or (not directory and metadata.st_nlink != 1)):
            raise TokenError("unsafe_storage")

    @contextmanager
    def _directory(self):
        fd = None
        try:
            fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            parts = self.directory.parts[1:]
            for index, part in enumerate(parts):
                if index == len(parts) - 1:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                        os.fsync(fd)
                    except FileExistsError:
                        pass
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                os.close(fd)
                fd = following
            self._check(os.fstat(fd), directory=True)
            yield fd
        except OSError:
            raise TokenError("unsafe_storage") from None
        finally:
            if fd is not None:
                os.close(fd)

    def _read(self, name):
        with self._directory() as directory:
            fd = None
            try:
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
                except FileNotFoundError:
                    return None
                metadata = os.fstat(fd)
                self._check(metadata)
                if metadata.st_size > MAX_BYTES:
                    raise TokenError("invalid_cache")
                payload = bytearray()
                while len(payload) <= MAX_BYTES:
                    chunk = os.read(fd, min(8192, MAX_BYTES + 1 - len(payload)))
                    if not chunk:
                        break
                    payload.extend(chunk)
                if len(payload) > MAX_BYTES:
                    raise TokenError("invalid_cache")
                data = json.loads(payload)
                if not isinstance(data, dict):
                    raise TokenError("invalid_cache")
                return data
            except (ValueError, UnicodeError):
                raise TokenError("invalid_cache") from None
            except OSError:
                raise TokenError("unsafe_storage") from None
            finally:
                if fd is not None:
                    os.close(fd)

    def _write(self, name, data):
        payload = json.dumps(data, allow_nan=False, separators=(",", ":")).encode()
        if len(payload) > MAX_BYTES:
            raise TokenError("invalid_cache")
        with self._directory() as directory:
            temporary = ".token-" + uuid.uuid4().hex
            fd = None
            created = False
            try:
                try:
                    self._check(os.stat(name, dir_fd=directory, follow_symlinks=False))
                except FileNotFoundError:
                    pass
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=directory)
                created = True
                self._check(os.fstat(fd))
                offset = 0
                while offset < len(payload):
                    written = os.write(fd, payload[offset:])
                    if written <= 0:
                        raise TokenError("storage_failed")
                    offset += written
                os.fsync(fd)
                os.close(fd)
                fd = None
                os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                created = False
                os.fsync(directory)
            except OSError:
                raise TokenError("storage_failed") from None
            finally:
                if fd is not None:
                    os.close(fd)
                if created:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except OSError:
                        pass

    def load(self):
        data = self._read("toss_token.json")
        if data is None:
            return None
        try:
            if set(data) != {item.name for item in fields(TokenRecord)}:
                raise TokenError("invalid_cache")
            data["issued_at"] = datetime.fromisoformat(data["issued_at"])
            data["expires_at"] = datetime.fromisoformat(data["expires_at"])
            record = TokenRecord(**data)
            record.validate(self.client_identity)
            return record
        except (TypeError, ValueError, KeyError, OverflowError):
            raise TokenError("invalid_cache") from None

    def save(self, record: TokenRecord):
        record.validate(self.client_identity)
        data = asdict(record)
        data["issued_at"] = record.issued_at.isoformat()
        data["expires_at"] = record.expires_at.isoformat()
        self._write("toss_token.json", data)

    def load_state(self):
        data = self._read("toss_auth_state.json")
        if data is None:
            return None
        if (set(data) != {"schema_version", "client_identity", "origin", "kind", "generation", "failed_digest"}
                or type(data["schema_version"]) is not int or data["schema_version"] != 1
                or data["client_identity"] != self.client_identity or data["origin"] != ORIGIN
                or not isinstance(data["kind"], str)
                or data["kind"] not in {"ready", "auth_unavailable", "issuance_unknown"}
                or type(data["generation"]) is not int or data["generation"] < 0
                or not isinstance(data["failed_digest"], str)
                or (data["kind"] == "auth_unavailable" and (len(data["failed_digest"]) != 64
                    or any(c not in "0123456789abcdef" for c in data["failed_digest"])))
                or (data["kind"] != "auth_unavailable" and data["failed_digest"] != "")):
            raise TokenError("auth_unavailable")
        return data

    def save_state(self, kind, generation=0, failed_digest=""):
        self._write("toss_auth_state.json", {"schema_version": 1, "client_identity": self.client_identity,
                    "origin": ORIGIN, "kind": kind, "generation": generation, "failed_digest": failed_digest})

    @asynccontextmanager
    async def lock(self, *, deadline, clock=time.monotonic):
        with self._directory() as directory:
            fd = None
            try:
                fd = os.open("toss_token.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             0o600, dir_fd=directory)
                self._check(os.fstat(fd))
                while True:
                    remaining = deadline - clock()
                    if not math.isfinite(remaining) or remaining <= 0:
                        raise TokenError("deadline_exceeded")
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        await asyncio.sleep(min(0.01, remaining))
                metadata = os.stat("toss_token.lock", dir_fd=directory, follow_symlinks=False)
                self._check(metadata)
                if (metadata.st_dev, metadata.st_ino) != (os.fstat(fd).st_dev, os.fstat(fd).st_ino):
                    raise TokenError("unsafe_storage")
                yield
            except OSError:
                raise TokenError("unsafe_storage") from None
            finally:
                if fd is not None:
                    os.close(fd)
