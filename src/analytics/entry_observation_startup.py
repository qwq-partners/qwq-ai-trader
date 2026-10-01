"""Select a locally configured observation manifest for at most one startup.

A receipt records an attempted startup, never successful observation. Once the
exclusive create succeeds, later failures must leave it in place. This module
neither loads the manifest nor starts a runtime, and is not an approval boundary.
The caller must still validate the selected manifest before initializing it.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
import re
import stat
from typing import Iterator

_MAX_REQUEST_BYTES = 64 * 1024
_FIELDS = {"version", "manifest_path", "receipt_path", "not_before", "latest_start_at"}
_UTC_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)\Z")


class ObservationStartupError(ValueError):
    """An invalid request or unsafe/unpersisted claim (without input details)."""


def _absolute_path(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 4096:
        raise ObservationStartupError("Invalid observation startup path")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ObservationStartupError("Invalid observation startup path")
    # Reject ambiguous aliases instead of resolving symlinks or consulting cwd.
    if not value.startswith("/") or any(part in {"", ".", ".."} for part in value[1:].split("/")):
        raise ObservationStartupError("Expected canonical absolute startup path")
    return value


@contextmanager
def _parent_fd(path: str) -> Iterator[tuple[int, str]]:
    """Anchor every traversal component with O_NOFOLLOW and a directory fd."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        parts = path[1:].split("/")
        for part in parts[:-1]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd, parts[-1]
    finally:
        os.close(fd)


def _regular_owned(info: os.stat_result, *, receipt: bool) -> None:
    mode = stat.S_IMODE(info.st_mode)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or bool(mode & (0o7177 if receipt else 0o7022))
    ):
        raise ObservationStartupError("Unsafe observation startup file")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ObservationStartupError("Duplicate observation startup field")
        result[key] = value
    return result


def _read_request(path: str) -> dict:
    with _parent_fd(path) as (parent, name):
        # The metadata check avoids opening a FIFO/device at all. O_NONBLOCK and
        # the second check also handle replacement between stat and open.
        _regular_owned(os.stat(name, dir_fd=parent, follow_symlinks=False), receipt=False)
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        try:
            info = os.fstat(fd)
            _regular_owned(info, receipt=False)
            if info.st_size > _MAX_REQUEST_BYTES:
                raise ObservationStartupError("Observation startup request is too large")
            chunks = []
            total = 0
            while total <= _MAX_REQUEST_BYTES:
                chunk = os.read(fd, min(8192, _MAX_REQUEST_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            if total > _MAX_REQUEST_BYTES:
                raise ObservationStartupError("Observation startup request is too large")
        finally:
            os.close(fd)
    result = json.loads(b"".join(chunks).decode("utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(result, dict) or set(result) != _FIELDS:
        raise ObservationStartupError("Invalid observation startup fields")
    if result["version"] != "entry-observation-once-v1":
        raise ObservationStartupError("Unsupported observation startup version")
    return result


def _utc_time(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40 or not _UTC_TIME.fullmatch(value):
        raise ObservationStartupError("Observation startup times must be aware UTC")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _claim(path: str, claimed_at: datetime) -> bool:
    with _parent_fd(path) as (parent, name):
        parent_info = os.fstat(parent)
        if parent_info.st_uid != os.geteuid() or stat.S_IMODE(parent_info.st_mode) & 0o7077:
            raise ObservationStartupError("Receipt parent must be a private owned directory")
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
        except FileExistsError:
            # An empty/truncated receipt also consumed the attempt. Never read,
            # replace, repair or remove it, even after an earlier fsync failure.
            _regular_owned(os.stat(name, dir_fd=parent, follow_symlinks=False), receipt=True)
            return False
        try:
            info = os.fstat(fd)
            _regular_owned(info, receipt=True)
            if stat.S_IMODE(info.st_mode) != 0o600:
                raise ObservationStartupError("New receipt must have private read/write permissions")
            intent = json.dumps({
                "version": "entry-observation-once-receipt-v1",
                "status": "attempt_consumed",
                "claimed_at": claimed_at.isoformat(),
            }, sort_keys=True).encode("ascii") + b"\n"
            view = memoryview(intent)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise ObservationStartupError("Could not persist observation startup receipt")
                view = view[written:]
            os.fsync(fd)
            os.fsync(parent)
        finally:
            os.close(fd)
    return True


def claim_once_manifest(
    request_path: str | os.PathLike[str], now: datetime | None = None,
) -> str | None:
    """Return an absolute manifest path once in [not_before, latest_start_at).

    The request must contain exactly version, manifest_path, receipt_path,
    not_before and latest_start_at. UTC timestamps bound a window of at most
    five minutes. The receipt's existing parent must be private and owned by
    the current effective uid. No directory or permissions are created/fixed.
    All failures are sanitized; any receipt already created remains consumed.
    """
    try:
        path = _absolute_path(os.fspath(request_path))
        request = _read_request(path)
        manifest = _absolute_path(request["manifest_path"])
        receipt = _absolute_path(request["receipt_path"])
        if receipt in {path, manifest}:
            raise ObservationStartupError("Receipt must differ from startup input paths")
        start = _utc_time(request["not_before"])
        end = _utc_time(request["latest_start_at"])
        if not timedelta(0) < end - start <= timedelta(minutes=5):
            raise ObservationStartupError("Invalid observation startup window")
        clock = datetime.now(timezone.utc) if now is None else now
        if not isinstance(clock, datetime) or clock.tzinfo is None or clock.utcoffset() != timedelta(0):
            raise ObservationStartupError("Observation startup clock must be aware UTC")
        if clock < start or clock >= end:
            return None
        return manifest if _claim(receipt, clock) else None
    except ObservationStartupError as error:
        raise error from None
    except (OSError, ValueError, TypeError, RecursionError):
        raise ObservationStartupError("Could not validate or persist observation startup request") from None
