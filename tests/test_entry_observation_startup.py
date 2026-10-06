"""One-use startup selection uses only explicitly supplied temporary files."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import threading

import pytest

from src.analytics import entry_observation_startup as startup

NOW = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def envelope(tmp_path):
    payload = {
        "version": "entry-observation-once-v1",
        "manifest_path": str(tmp_path / "manifest.json"),
        "receipt_path": str(tmp_path / "receipt.json"),
        "not_before": "2026-10-01T00:00:00Z",
        "latest_start_at": "2026-10-01T00:05:00+00:00",
    }
    path = tmp_path / "envelope.json"
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    return path, payload


def rewrite(envelope, **changes):
    path, payload = envelope
    payload.update(changes)
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("offset", [-1, 300, 301, 86400])
def test_off_window_skips_missing_manifest_and_receipt_parent(envelope, offset):
    path = rewrite(envelope, receipt_path=str(envelope[0].parent / "absent" / "receipt"))
    assert startup.claim_once_manifest(path, now=NOW + timedelta(seconds=offset)) is None
    assert not (path.parent / "absent").exists()


@pytest.mark.parametrize("offset", [0, 299.999999])
def test_inside_window_returns_manifest_only_after_durable_private_intent(envelope, offset, monkeypatch):
    path, payload = envelope
    sync_types = []
    fsync = os.fsync

    def record_sync(fd):
        sync_types.append(stat.S_IFMT(os.fstat(fd).st_mode))
        fsync(fd)

    monkeypatch.setattr(startup.os, "fsync", record_sync)
    assert startup.claim_once_manifest(path, now=NOW + timedelta(seconds=offset)) == payload["manifest_path"]
    receipt = Path(payload["receipt_path"])
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    assert receipt.stat().st_uid == os.geteuid()
    assert receipt.stat().st_nlink == 1
    intent = json.loads(receipt.read_text())
    assert intent["status"] == "attempt_consumed"
    assert sync_types == [stat.S_IFREG, stat.S_IFDIR]
    assert startup.claim_once_manifest(path, now=NOW) is None


def test_concurrent_claims_allow_only_one_manifest(envelope):
    path, payload = envelope
    barrier = threading.Barrier(12)

    def claim(_):
        barrier.wait()
        return startup.claim_once_manifest(path, now=NOW)

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(claim, range(12)))
    assert results.count(payload["manifest_path"]) == 1
    assert results.count(None) == 11


@pytest.mark.parametrize("contents", [b"", b"{partial", b"SENSITIVE DO NOT READ"])
def test_partial_receipt_is_consumed_without_reading_or_overwriting(envelope, contents, monkeypatch):
    path, payload = envelope
    receipt = Path(payload["receipt_path"])
    receipt.write_bytes(contents)
    receipt.chmod(0o600)
    real_open = os.open

    def forbid_receipt_read(name, flags, *args, **kwargs):
        if name == receipt.name and not flags & os.O_EXCL:
            pytest.fail("existing receipt must not be opened")
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(startup.os, "open", forbid_receipt_read)
    assert startup.claim_once_manifest(path, now=NOW) is None
    assert receipt.read_bytes() == contents


@pytest.mark.parametrize("which", [1, 2])
def test_fsync_failure_consumes_attempt_and_sanitizes_error(envelope, monkeypatch, which):
    path, payload = envelope
    fsync = os.fsync
    count = 0

    def fail_sync(fd):
        nonlocal count
        count += 1
        if count == which:
            raise OSError("sensitive filesystem detail")
        fsync(fd)

    monkeypatch.setattr(startup.os, "fsync", fail_sync)
    with pytest.raises(startup.ObservationStartupError) as caught:
        startup.claim_once_manifest(path, now=NOW)
    assert "sensitive" not in str(caught.value)
    assert Path(payload["receipt_path"]).is_file()
    assert startup.claim_once_manifest(path, now=NOW) is None


@pytest.mark.parametrize("changes", [
    {"version": "other"}, {"extra": "unknown"}, {"not_before": "2026-10-01T00:00:00"},
    {"not_before": "2026-10-01T09:00:00+09:00"},
    {"latest_start_at": "2026-10-01T00:00:00Z"},
    {"latest_start_at": "2026-09-30T23:59:00Z"},
    {"latest_start_at": "2026-10-01T00:05:00.000001Z"},
    {"manifest_path": "relative.json"}, {"receipt_path": "/tmp/../receipt"},
    {"manifest_path": "/tmp/secret\nname"}, {"receipt_path": "/tmp/"},
    {"manifest_path": "a" * 4097}, {"not_before": 1},
])
def test_invalid_envelope_never_creates_receipt(envelope, changes):
    path = rewrite(envelope, **changes)
    with pytest.raises(startup.ObservationStartupError):
        startup.claim_once_manifest(path, now=NOW)
    assert not (path.parent / "receipt.json").exists()


@pytest.mark.parametrize("which", ["envelope", "manifest"])
def test_receipt_cannot_alias_input_paths(envelope, which):
    path, payload = envelope
    path = rewrite(envelope, receipt_path=str(path) if which == "envelope" else payload["manifest_path"])
    with pytest.raises(startup.ObservationStartupError):
        startup.claim_once_manifest(path, now=NOW)


@pytest.mark.parametrize("body", ["[]", "{}", "null", "{invalid", '{"version":1,"version":2}', 'x' * 65537], ids=['array', 'empty', 'null', 'invalid', 'duplicate', 'oversized'])
def test_invalid_json_or_oversized_request_is_sanitized(envelope, body):
    path, _ = envelope
    path.write_text(body)
    with pytest.raises(startup.ObservationStartupError) as caught:
        startup.claim_once_manifest(path, now=NOW)
    assert str(path) not in str(caught.value)


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo", "hardlink", "public", "other_owner"])
def test_unsafe_existing_receipt_rejected_without_mutation(envelope, kind, monkeypatch):
    path, payload = envelope
    receipt = Path(payload["receipt_path"])
    if kind == "symlink":
        receipt.symlink_to(path)
    elif kind == "directory":
        receipt.mkdir()
    elif kind == "fifo":
        os.mkfifo(receipt)
    elif kind == "hardlink":
        os.link(path, receipt)
    else:
        receipt.write_bytes(b"untouched")
        receipt.chmod(0o644 if kind == "public" else 0o600)
        if kind == "other_owner":
            monkeypatch.setattr(startup.os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(startup.ObservationStartupError):
        startup.claim_once_manifest(path, now=NOW)
    if kind in {"public", "other_owner"}:
        assert receipt.read_bytes() == b"untouched"


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo", "hardlink", "writable"])
def test_unsafe_request_rejected_without_reading_special_file(envelope, kind):
    path, _ = envelope
    if kind == "hardlink":
        os.link(path, path.with_suffix(".copy"))
    elif kind == "writable":
        path.chmod(0o666)
    else:
        saved = path.with_suffix(".saved")
        path.rename(saved)
        if kind == "symlink":
            path.symlink_to(saved)
        elif kind == "directory":
            path.mkdir()
        else:
            os.mkfifo(path)
    with pytest.raises(startup.ObservationStartupError):
        startup.claim_once_manifest(path, now=NOW)


@pytest.mark.parametrize("target", ["envelope", "receipt"])
def test_symlink_parent_rejected(envelope, target):
    path, payload = envelope
    link = path.parent / "link"
    link.symlink_to(path.parent, target_is_directory=True)
    if target == "envelope":
        path = link / path.name
    else:
        path = rewrite(envelope, receipt_path=str(link / "receipt.json"))
    with pytest.raises(startup.ObservationStartupError):
        startup.claim_once_manifest(path, now=NOW)
    assert not Path(payload["receipt_path"]).exists()


def test_receipt_parent_requires_private_existing_directory(envelope):
    path, _ = envelope
    public = path.parent / "public"
    public.mkdir(mode=0o755)
    public.chmod(0o755)  # 호출자 umask와 무관하게 거부할 공개 권한을 만든다.
    rewrite(envelope, receipt_path=str(public / "receipt"))
    with pytest.raises(startup.ObservationStartupError):
        startup.claim_once_manifest(path, now=NOW)
    assert stat.S_IMODE(public.stat().st_mode) == 0o755
    assert not (public / "receipt").exists()


def test_naive_now_is_rejected(envelope):
    with pytest.raises(startup.ObservationStartupError):
        startup.claim_once_manifest(envelope[0], now=NOW.replace(tzinfo=None))


@pytest.mark.parametrize("mode", [0o000, 0o200, 0o400])
def test_private_receipt_with_restricted_owner_permissions_is_still_consumed(envelope, mode):
    path, payload = envelope
    receipt = Path(payload["receipt_path"])
    receipt.write_bytes(b"")
    receipt.chmod(mode)
    assert startup.claim_once_manifest(path, now=NOW) is None
    assert stat.S_IMODE(receipt.stat().st_mode) == mode


def test_failed_receipt_write_leaves_consumed_empty_receipt(envelope, monkeypatch):
    path, payload = envelope

    def fail_write(fd, content):
        raise OSError("private filesystem information")

    monkeypatch.setattr(startup.os, "write", fail_write)
    with pytest.raises(startup.ObservationStartupError) as caught:
        startup.claim_once_manifest(path, now=NOW)
    assert "private filesystem information" not in str(caught.value)
    assert Path(payload["receipt_path"]).read_bytes() == b""
    assert startup.claim_once_manifest(path, now=NOW) is None


def test_symlink_parent_replacement_does_not_redirect_receipt(envelope, monkeypatch):
    path, _ = envelope
    safe = path.parent / "safe"
    safe.mkdir(mode=0o700)
    moved = path.parent / "moved"
    outside = path.parent / "outside"
    outside.mkdir(mode=0o700)
    rewrite(envelope, receipt_path=str(safe / "receipt"))
    real_open = os.open

    def replace_after_anchor(name, flags, *args, **kwargs):
        fd = real_open(name, flags, *args, **kwargs)
        if name == "safe" and flags & os.O_DIRECTORY:
            safe.rename(moved)
            safe.symlink_to(outside, target_is_directory=True)
        return fd

    monkeypatch.setattr(startup.os, "open", replace_after_anchor)
    assert startup.claim_once_manifest(path, now=NOW) == envelope[1]["manifest_path"]
    assert not (outside / "receipt").exists()
    assert (moved / "receipt").is_file()
