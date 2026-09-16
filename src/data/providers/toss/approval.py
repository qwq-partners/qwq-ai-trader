"""운영자 등록부와 명시 실행 identity를 대조하는 무자격 preflight."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from types import MappingProxyType

from .token import utc_now
from .token_store import ORIGIN

SPEC_VERSION = "1.2.17"
SPEC_SHA256 = "791082da4cb379117ed9fdc29a45bd42746f7a1aec368da1e9f4e1f3bfbff5b4"
MAX_DOCUMENT_BYTES = 262144


class ApprovalError(Exception):
    """입력 원문을 노출하지 않는 고정 거부 코드."""

    def __init__(self, code="approval_invalid"):
        self.code = code if code in {"approval_invalid", "approval_untrusted", "approval_denied", "approval_expired"} else "approval_invalid"
        super().__init__(self.code)


def _reject():
    raise ApprovalError()


def _keys(value, keys):
    if type(value) is not dict or set(value) != set(keys.split()):
        _reject()


def _string(value, maximum=256):
    if type(value) is not str or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        _reject()


def _number(value, low, high, *, integer=False):
    if type(value) not in ({int} if integer else {int, float}) or not low <= value <= high or not math.isfinite(value):
        _reject()


def _hash(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        _reject()


def _path(value):
    try:
        value = os.fspath(value)
    except TypeError:
        raise ApprovalError() from None
    _string(value, 4096)
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != value or len(path.parts) < 2:
        _reject()
    return path


def _timestamp(value):
    _string(value)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _reject()
    if parsed.utcoffset() != timedelta(0):
        _reject()
    return parsed


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _reject()
            result[key] = value
        return result
    if type(raw) is not bytes or len(raw) > MAX_DOCUMENT_BYTES:
        _reject()
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: _reject())
    except (ValueError, UnicodeError, RecursionError):
        raise ApprovalError() from None


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def _freeze(value):
    if type(value) is dict:
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze(item) for item in value)
    return value


def _time(value):
    if type(value) is not str or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value) is None:
        _reject()


@dataclass(frozen=True)
class ObservationPlan:
    document: object
    raw_hash: str
    canonical_hash: str

    @classmethod
    def from_bytes(cls, raw):
        p = _json(raw)
        _keys(p, "schema_version plan_id dataset_kind origin spec_version spec_sha256 dates sessions calendar_time selection limits comparison acceptance")
        if type(p["schema_version"]) is not int or p["schema_version"] != 1 or p["dataset_kind"] != "live" or p["origin"] != ORIGIN or p["spec_version"] != SPEC_VERSION or p["spec_sha256"] != SPEC_SHA256:
            _reject()
        _string(p["plan_id"])
        dates = p["dates"]
        if type(dates) is not list or not 1 <= len(dates) <= 366:
            _reject()
        for day in dates:
            if type(day) is not str or re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) is None:
                _reject()
            try:
                date.fromisoformat(day)
            except ValueError:
                _reject()
        if dates != sorted(set(dates)):
            _reject()
        sessions = p["sessions"]
        if type(sessions) is not list or not 1 <= len(sessions) <= 3:
            _reject()
        names, intervals = set(), []
        for session in sessions:
            _keys(session, "name start end")
            if type(session["name"]) is not str or session["name"] not in {"regular", "pre", "after"} or session["name"] in names:
                _reject()
            names.add(session["name"])
            _time(session["start"])
            _time(session["end"])
            if session["start"] >= session["end"]:
                _reject()
            intervals.append((session["start"], session["end"]))
        intervals.sort()
        if any(left[1] > right[0] for left, right in zip(intervals, intervals[1:])):
            _reject()
        _time(p["calendar_time"])
        s = p["selection"]
        _keys(s, "candidate_limit max_snapshot_age_seconds max_symbols rule")
        if s["rule"] != "score_desc_symbol_asc_holdings_first":
            _reject()
        _number(s["candidate_limit"], 0, 10000, integer=True)
        _number(s["max_symbols"], 1, 10000, integer=True)
        _number(s["max_snapshot_age_seconds"], 1, 86400, integer=True)
        if s["candidate_limit"] > s["max_symbols"]:
            _reject()
        limits = p["limits"]
        bounds = {"job_timeout_seconds": (0.001, 3600, False), "cleanup_timeout_seconds": (0.001, 300, False),
            "preflight_timeout_seconds": (0.001, 300, False), "max_pages": (1, 1000, True),
            "max_retries": (1, 1, True), "circuit_failure_threshold": (1, 1000, True),
            "circuit_open_seconds": (0.001, 86400, False), "ledger_max_bytes": (1, 1073741824, True),
            "response_max_bytes": (1, 16777216, True), "response_max_depth": (1, 64, True),
            "response_max_nodes": (1, 1000000, True), "response_max_string": (1, 1048576, True),
            "parse_timeout_seconds": (0.001, 30, False), "auth_max_issues": (1, 1000, True)}
        _keys(limits, " ".join(bounds) + " groups")
        for key, (low, high, integer) in bounds.items():
            _number(limits[key], low, high, integer=integer)
        _keys(limits["groups"], "PRICES CANDLES MARKET_INFO")
        for key, maximum in (("PRICES", 15), ("CANDLES", 20), ("MARKET_INFO", 3)):
            _number(limits["groups"][key], 1, maximum, integer=True)
        c = p["comparison"]
        _keys(c, "max_age_seconds max_skew_seconds outlier_pct min_valid_pairs expected_market_basis")
        for key in ("max_age_seconds", "max_skew_seconds"):
            _number(c[key], 0, 86400, integer=True)
        _number(c["outlier_pct"], 0, 100, integer=False)
        _number(c["min_valid_pairs"], 1, 10000000, integer=True)
        _string(c["expected_market_basis"])
        a = p["acceptance"]
        _keys(a, "min_coverage max_provider_failure_rate max_p95_pct max_outlier_rate max_missed_slots max_latency_seconds retention_days min_business_days")
        for key in ("min_coverage", "max_provider_failure_rate", "max_outlier_rate"):
            _number(a[key], 0, 1)
        _number(a["max_p95_pct"], 0, 100)
        _number(a["max_missed_slots"], 0, 1000000, integer=True)
        _number(a["max_latency_seconds"], 0.001, 3600)
        _number(a["retention_days"], 1, 3660, integer=True)
        _number(a["min_business_days"], 3, 366, integer=True)
        return cls(_freeze(p), hashlib.sha256(raw).hexdigest(), hashlib.sha256(_canonical(p)).hexdigest())


@dataclass(frozen=True)
class RegistryTrust:
    registry_path: Path
    operator_uid: int
    operator_gid: int


@dataclass(frozen=True)
class ExecutionIdentity:
    client_identity: str
    host_identity: str
    service_uid: int
    service_gids: tuple[int, ...]
    role: str
    token_directory: Path
    sender_lock_path: Path
    ledger_path: Path
    release_id: str
    config_hash: str


@dataclass(frozen=True)
class LiveObservationGrant:
    document: object

    def __getattr__(self, name):
        document = object.__getattribute__(self, "document")
        if name in document:
            return document[name]
        raise AttributeError(name)

    @classmethod
    def from_document(cls, g):
        _keys(g, "schema_version grant_id approval_reference terms_reference storage_reference issuance_ownership_reference not_before expires_at client_identity host_identity service_uid role token_directory sender_lock_path ledger_path release_id config_hash plan_raw_hash plan_canonical_hash origin spec_version spec_sha256 capabilities")
        if type(g["schema_version"]) is not int or g["schema_version"] != 1 or g["origin"] != ORIGIN or g["spec_version"] != SPEC_VERSION or g["spec_sha256"] != SPEC_SHA256:
            _reject()
        for key in ("grant_id", "approval_reference", "terms_reference", "storage_reference", "issuance_ownership_reference", "client_identity", "host_identity", "release_id"):
            _string(g[key])
        for key in ("config_hash", "plan_raw_hash", "plan_canonical_hash"):
            _hash(g[key])
        for key in ("token_directory", "sender_lock_path", "ledger_path"):
            _path(g[key])
        if len({g["token_directory"], g["sender_lock_path"], g["ledger_path"]}) != 3:
            _reject()
        _number(g["service_uid"], 1, 2**32 - 2, integer=True)
        if g["role"] not in ("reader", "issuer"):
            _reject()
        start, end = _timestamp(g["not_before"]), _timestamp(g["expires_at"])
        if not 0 < (end - start).total_seconds() <= 366 * 86400:
            _reject()
        _keys(g["capabilities"], "query renewal bootstrap")
        if any(type(v) is not bool for v in g["capabilities"].values()):
            _reject()
        if g["role"] == "reader" and (g["capabilities"]["renewal"] or g["capabilities"]["bootstrap"]):
            _reject()
        return cls(_freeze(g))


class ApprovedAuthority:
    def __init__(self, plan, grant, *, clock, now):
        self.plan, self.grant = plan, grant
        self.clock, self.now = clock, now
        self.authority_hash = hashlib.sha256((plan.raw_hash + hashlib.sha256(_canonical(dict(grant.document, capabilities=dict(grant.capabilities)))).hexdigest()).encode()).hexdigest()
        self._stopped = threading.Event()
        self._not_before, self._expires = _timestamp(grant.not_before), _timestamp(grant.expires_at)
        stamp, tick = self.now(), self.clock()
        if not isinstance(stamp, datetime) or stamp.utcoffset() != timedelta(0) or not math.isfinite(tick):
            _reject()
        self._deadline = tick + (self._expires - stamp).total_seconds()
        self.require("query", deadline=self._deadline)

    def stop(self):
        self._stopped.set()

    def require(self, operation, *, deadline):
        if self._stopped.is_set() or operation not in ("query", "renewal", "bootstrap") or not self.grant.capabilities[operation] or (operation != "query" and self.grant.role != "issuer"):
            raise ApprovalError("approval_denied")
        stamp, tick = self.now(), self.clock()
        if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                or type(tick) not in (int, float) or not math.isfinite(tick)
                or not isinstance(stamp, datetime) or stamp.utcoffset() != timedelta(0)):
            raise ApprovalError("approval_expired")
        bounded = min(deadline, self._deadline, tick + (self._expires - stamp).total_seconds())
        if not self._not_before <= stamp < self._expires or tick >= bounded:
            raise ApprovalError("approval_expired")
        return bounded


def _read_file(path, *, trust=None, identity=None):
    """각 부모를 descriptor로 고정하며 symlink/특수 파일/하드링크를 거부한다."""
    path = _path(path)
    fd = None
    try:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        for index, part in enumerate(path.parts[1:]):
            if trust is not None:
                _check_trusted(os.fstat(fd), trust, identity, directory=True)
            is_last = index == len(path.parts) - 2
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_NONBLOCK if is_last else os.O_DIRECTORY)
            following = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = following
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > MAX_DOCUMENT_BYTES:
            _reject()
        if trust is not None:
            _check_trusted(metadata, trust, identity, directory=False)
        payload = bytearray()
        while len(payload) <= MAX_DOCUMENT_BYTES:
            chunk = os.read(fd, min(8192, MAX_DOCUMENT_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(fd)
        if len(payload) > MAX_DOCUMENT_BYTES or (metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            _reject()
        return bytes(payload)
    except (OSError, TypeError, ValueError):
        raise ApprovalError("approval_untrusted") from None
    finally:
        if fd is not None:
            os.close(fd)


def _check_trusted(metadata, trust, identity, *, directory):
    mode = stat.S_IMODE(metadata.st_mode)
    if (metadata.st_uid not in ({0, trust.operator_uid} if directory else {trust.operator_uid})
            or (not directory and metadata.st_gid != trust.operator_gid)
            or mode & 0o022 or (metadata.st_uid == identity.service_uid and mode & 0o200)
            or (directory and not stat.S_ISDIR(metadata.st_mode))):
        raise ApprovalError("approval_untrusted")


def load_authority(*, registry_path, plan_path, grant_id, trust, identity, clock=time.monotonic, now=utc_now):
    if type(trust) is not RegistryTrust or type(identity) is not ExecutionIdentity:
        raise ApprovalError("approval_untrusted")
    if _path(registry_path) != _path(trust.registry_path):
        raise ApprovalError("approval_untrusted")
    for value in (trust.operator_uid, trust.operator_gid, identity.service_uid):
        _number(value, 0, 2**32 - 2, integer=True)
    if identity.service_uid == 0 or identity.service_uid == trust.operator_uid or type(identity.service_gids) is not tuple:
        raise ApprovalError("approval_untrusted")
    for gid in identity.service_gids:
        _number(gid, 0, 2**32 - 2, integer=True)
    registry = _json(_read_file(registry_path, trust=trust, identity=identity))
    _keys(registry, "schema_version grants")
    if type(registry["schema_version"]) is not int or registry["schema_version"] != 1 or type(registry["grants"]) is not list or not 1 <= len(registry["grants"]) <= 100:
        _reject()
    grants = [LiveObservationGrant.from_document(g) for g in registry["grants"]]
    if len({g.grant_id for g in grants}) != len(grants):
        _reject()
    matching = [g for g in grants if g.grant_id == grant_id]
    if len(matching) != 1:
        raise ApprovalError("approval_denied")
    grant = matching[0]
    for key in ("client_identity", "host_identity", "service_uid", "role", "release_id", "config_hash"):
        if getattr(identity, key) != getattr(grant, key):
            raise ApprovalError("approval_denied")
    for key in ("token_directory", "sender_lock_path", "ledger_path"):
        if str(_path(getattr(identity, key))) != getattr(grant, key):
            raise ApprovalError("approval_denied")
    plan = ObservationPlan.from_bytes(_read_file(plan_path))
    if plan.raw_hash != grant.plan_raw_hash or plan.canonical_hash != grant.plan_canonical_hash:
        raise ApprovalError("approval_denied")
    return ApprovedAuthority(plan, grant, clock=clock, now=now)
