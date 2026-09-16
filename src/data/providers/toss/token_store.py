"""명시 경로 전용 토큰 저장소. 운영 경로·환경변수·네트워크를 참조하지 않는다."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
import fcntl
import hashlib
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
REVOCATION_SLOTS = 256
REVOCATION_BYTES = 1024
MAX_IDENTITY_BYTES = 256
MAX_GENERATION = 2 ** 63 - 1


def _generation_ok(value, *, minimum=0):
    return type(value) is int and minimum <= value <= MAX_GENERATION


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
                or not _generation_ok(self.generation, minimum=1)
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
                or not isinstance(client_identity, str) or not client_identity.strip()
                or len(json.dumps(client_identity, ensure_ascii=True).encode("ascii")) > MAX_IDENTITY_BYTES):
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

    def _read(self, name, *, limit=MAX_BYTES):
        with self._directory() as directory:
            fd = None
            try:
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
                except FileNotFoundError:
                    return None
                metadata = os.fstat(fd)
                self._check(metadata)
                if metadata.st_size > limit:
                    raise TokenError("invalid_cache")
                payload = bytearray()
                while len(payload) <= limit:
                    chunk = os.read(fd, min(8192, limit + 1 - len(payload)))
                    if not chunk:
                        break
                    payload.extend(chunk)
                if len(payload) > limit:
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
                or not _generation_ok(data["generation"])
                or not isinstance(data["failed_digest"], str)
                or (data["kind"] == "auth_unavailable" and (len(data["failed_digest"]) != 64
                    or any(c not in "0123456789abcdef" for c in data["failed_digest"])))
                or (data["kind"] != "auth_unavailable" and data["failed_digest"] != "")):
            raise TokenError("auth_unavailable")
        return data

    def save_state(self, kind, generation=0, failed_digest=""):
        if not _generation_ok(generation):
            raise TokenError("auth_unavailable")
        self._write("toss_auth_state.json", {"schema_version": 1, "client_identity": self.client_identity,
                    "origin": ORIGIN, "kind": kind, "generation": generation, "failed_digest": failed_digest})

    @staticmethod
    def _digest_ok(value):
        return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)

    @staticmethod
    def _observation_id(digest, generation):
        return hashlib.sha256(f"{digest}:{generation}".encode()).hexdigest()

    def _observation(self, data):
        if (set(data) != {"schema_version", "client_identity", "origin", "failed_digest", "generation"}
                or type(data["schema_version"]) is not int or data["schema_version"] != 1
                or data["client_identity"] != self.client_identity or data["origin"] != ORIGIN
                or not self._digest_ok(data["failed_digest"])
                or not _generation_ok(data["generation"])):
            raise TokenError("auth_unavailable")
        return self._observation_id(data["failed_digest"], data["generation"])

    def _publish_immutable(self, name, data):
        """완성된 파일만 link로 게시한다. 경쟁자의 동일 슬롯을 덮어쓰지 않는다."""
        payload = json.dumps(data, allow_nan=False, separators=(",", ":")).encode()
        if len(payload) > REVOCATION_BYTES:
            raise TokenError("auth_unavailable")
        with self._directory() as directory:
            temporary = ".token-" + uuid.uuid4().hex
            fd = None
            created = False
            try:
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
                try:
                    os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                except FileExistsError:
                    return False
                # 두 링크가 남는 짧은 구간은 독자가 안전 오류로 거부할 수 있다.
                os.unlink(temporary, dir_fd=directory)
                created = False
                os.fsync(directory)
                return True
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

    def _sync_existing(self, name):
        # 경쟁자의 link 직후 중복 관측도 durable 완료를 확인한다.
        with self._directory() as directory:
            fd = None
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
                self._check(os.fstat(fd))
                os.fsync(fd)
                os.fsync(directory)
            except OSError:
                raise TokenError("storage_failed") from None
            finally:
                if fd is not None:
                    os.close(fd)

    def block_revocations(self):
        """관측 유실/포화 시 보수적 영속 차단. 자동 해제 API는 없다."""
        name = "toss_revoked_overflow.json"
        if not self._publish_immutable(name, {"schema_version": 1, "overflow": True,
                "client_identity": self.client_identity, "origin": ORIGIN}):
            self._sync_existing(name)

    def observe_revocation(self, digest, generation):
        if self._read("toss_revoked_overflow.json", limit=REVOCATION_BYTES) is not None:
            raise TokenError("auth_unavailable")
        data = {"schema_version": 1, "client_identity": self.client_identity, "origin": ORIGIN,
                "failed_digest": digest, "generation": generation}
        observation_id = self._observation(data)
        start = int(observation_id[:8], 16) % REVOCATION_SLOTS
        for offset in range(REVOCATION_SLOTS):
            name = f"toss_revoked_slot_{(start + offset) % REVOCATION_SLOTS:03d}.json"
            existing = self._read(name, limit=REVOCATION_BYTES)
            if existing is None:
                if self._publish_immutable(name, data):
                    return
                existing = self._read(name, limit=REVOCATION_BYTES)
            if existing is None:
                raise TokenError("auth_unavailable")
            if self._observation(existing) == observation_id:
                self._sync_existing(name)
                return
        self.block_revocations()
        raise TokenError("auth_unavailable")

    def load_revocations(self):
        if self._read("toss_revoked_overflow.json", limit=REVOCATION_BYTES) is not None:
            raise TokenError("auth_unavailable")
        observations = {}
        for index in range(REVOCATION_SLOTS):
            data = self._read(f"toss_revoked_slot_{index:03d}.json", limit=REVOCATION_BYTES)
            if data is not None:
                observation_id = self._observation(data)
                if observation_id in observations:
                    raise TokenError("auth_unavailable")
                observations[observation_id] = data
        # overflow 게시가 실패했어도 포화 슬롯 자체가 재시작 후 발급을 막는다.
        if len(observations) == REVOCATION_SLOTS:
            raise TokenError("auth_unavailable")
        return observations

    def load_revocation_resolutions(self):
        data = self._read("toss_revoked_resolutions.json")
        if data is None:
            return {}
        if (set(data) != {"schema_version", "client_identity", "origin", "resolutions"}
                or type(data["schema_version"]) is not int or data["schema_version"] != 1
                or data["client_identity"] != self.client_identity or data["origin"] != ORIGIN
                or not isinstance(data["resolutions"], dict) or len(data["resolutions"]) > REVOCATION_SLOTS):
            raise TokenError("auth_unavailable")
        for observation_id, proof in data["resolutions"].items():
            if (not self._digest_ok(observation_id) or not isinstance(proof, dict)
                    or set(proof) != {"cache_digest", "generation"}
                    or not self._digest_ok(proof["cache_digest"])
                    or not _generation_ok(proof["generation"], minimum=1)):
                raise TokenError("auth_unavailable")
        return data["resolutions"]

    def save_revocation_resolutions(self, resolutions):
        # 호출자는 issuer lock을 가진다. 관측 파일/issuance_unknown은 변경하지 않는다.
        if any(not _generation_ok(proof["generation"], minimum=1) for proof in resolutions.values()):
            raise TokenError("auth_unavailable")
        self._write("toss_revoked_resolutions.json", {"schema_version": 1,
                    "client_identity": self.client_identity, "origin": ORIGIN, "resolutions": resolutions})

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
