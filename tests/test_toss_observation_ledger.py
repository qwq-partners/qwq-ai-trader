from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest


def snapshot(symbols=("005930",)):
    return {"snapshot_id": "snap-1", "selected_at": "2026-09-16T09:00:00+09:00",
            "source_success_at": "2026-09-16T08:59:00+09:00", "selection_partial": False,
            "symbols": list(symbols), "kis": {}}


def test_constructor_is_inert_and_reservation_is_durable(tmp_path):
    from src.data.providers.toss.observation_ledger import ObservationLedger

    path = tmp_path / "private" / "ledger.jsonl"
    ledger = ObservationLedger(path, plan_hash="a" * 64, max_bytes=10_000)
    assert not path.exists()
    ledger.open()
    attempts = ledger.reserve_slot("2026-09-16T09:00:00+09:00", kind="prices", snapshot=snapshot())
    assert len(attempts) == 1 and len(attempts[0]) == 64
    assert path.exists()
    lines = path.read_text().splitlines()
    assert [json.loads(line)["type"] for line in lines] == ["slot"]
    assert all(json.loads(line)["hash"] for line in lines)


def test_reopen_marks_unfinished_without_replaying_or_inventing_symbols(tmp_path):
    from src.data.providers.toss.observation_ledger import ObservationLedger

    path = tmp_path / "ledger.jsonl"
    ledger = ObservationLedger(path, plan_hash="a" * 64, max_bytes=10_000)
    ledger.open()
    ledger.reserve_slot("slot", kind="prices", snapshot=snapshot(("A", "B")))
    ledger.close()
    reopened = ObservationLedger(path, plan_hash="a" * 64, max_bytes=10_000)
    reopened.open()
    assert reopened.summary()["interrupted"] == 2
    assert reopened.reserve_slot("slot", kind="prices", snapshot=snapshot(("C",))) is None
    assert reopened.summary()["duplicate_submissions"] == 1


def test_corrupt_chain_is_preserved_and_fails_closed(tmp_path):
    from src.data.providers.toss.observation_ledger import LedgerError, ObservationLedger

    path = tmp_path / "ledger.jsonl"
    path.write_text('{"not":"a ledger row"}\n')
    ledger = ObservationLedger(path, plan_hash="a" * 64, max_bytes=10_000)
    with pytest.raises(LedgerError, match="ledger_corrupt"):
        ledger.open()
    assert path.read_text() == '{"not":"a ledger row"}\n'
    assert ledger.summary()["incomplete"] is True


def test_terminal_is_idempotent_and_conflicting_second_terminal_fails(tmp_path):
    from src.data.providers.toss.observation_ledger import LedgerError, ObservationLedger

    ledger = ObservationLedger(tmp_path / "ledger", plan_hash="a" * 64, max_bytes=10_000)
    ledger.open()
    attempt, = ledger.reserve_slot("slot", kind="prices", snapshot=snapshot())
    ledger.begin(attempt)
    ledger.finish(attempt, reason="provider_failure")
    ledger.finish(attempt, reason="provider_failure")
    with pytest.raises(LedgerError, match="ledger_conflict"):
        ledger.finish(attempt, reason="success")
    assert ledger.summary()["terminal_attempts"] == 1


def test_unsafe_or_oversize_file_fails_closed(tmp_path):
    from src.data.providers.toss.observation_ledger import LedgerError, ObservationLedger

    path = tmp_path / "ledger"
    path.write_text("x" * 100)
    with pytest.raises(LedgerError, match="ledger_corrupt"):
        ObservationLedger(path, plan_hash="a" * 64, max_bytes=10).open()
    assert path.exists()


def test_review_cli_is_directly_runnable_and_read_only(tmp_path):
    from src.data.providers.toss.observation_ledger import ObservationLedger
    ledger_path = tmp_path / "ledger"
    ledger = ObservationLedger(ledger_path, plan_hash="a" * 64, max_bytes=10_000); ledger.open()
    snap = snapshot(())
    snap["snapshot_id"] = "calendar:2026-09-16"
    attempt, = ledger.reserve_slot("calendar", kind="calendar", snapshot=snap)
    ledger.finish(attempt, reason="success", observation={"holiday": False, "requested_date": "2026-09-16"})
    before = ledger_path.read_bytes()
    command = [sys.executable, "scripts/review_toss_observation.py", "--ledger", str(ledger_path),
               "--plan-hash", "a" * 64, "--max-bytes", "10000"]
    result = subprocess.run(command, cwd=".", capture_output=True, text=True, check=False)
    assert result.returncode == 0 and json.loads(result.stdout)["production_eligible"] is False
    assert ledger_path.read_bytes() == before


def make_ledger(tmp_path):
    from src.data.providers.toss.observation_ledger import ObservationLedger
    ledger = ObservationLedger(tmp_path / "ledger", plan_hash="a" * 64, max_bytes=1_000_000)
    ledger.open()
    return ledger


def append_hash_valid(path, fields):
    from hashlib import sha256
    rows = [json.loads(line) for line in path.read_bytes().splitlines()]
    row = dict(rows[-1], **fields, sequence=len(rows) + 1, previous_hash=rows[-1]["hash"])
    if fields.get("type") == "terminal":
        for key in ("slot_id", "kind", "snapshot", "schedule"):
            row.pop(key, None)
    row.pop("hash")
    encode = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    row["hash"] = sha256(encode(row)).hexdigest()
    with path.open("ab") as handle:
        handle.write(encode(row) + b"\n")


def test_writer_lock_prevents_second_owner_and_releases_on_close(tmp_path):
    from src.data.providers.toss.observation_ledger import LedgerError, ObservationLedger
    first = make_ledger(tmp_path)
    first.reserve_slot("s", kind="prices", snapshot=snapshot())
    second = ObservationLedger(first.path, plan_hash="a" * 64, max_bytes=1_000_000)
    with pytest.raises(LedgerError, match="ledger_unavailable"):
        second.open()
    assert first.summary()["interrupted"] == 0
    first.close()
    fresh = ObservationLedger(first.path, plan_hash="a" * 64, max_bytes=1_000_000)
    fresh.open()
    assert fresh.summary()["interrupted"] == 1


def test_short_writes_and_eintr_produce_a_complete_reloadable_reservation(tmp_path, monkeypatch):
    from src.data.providers.toss.observation_ledger import ObservationLedger
    ledger = make_ledger(tmp_path)
    real_write = os.write
    calls = []
    def partial(fd, data):
        calls.append(len(data))
        if len(calls) == 1:
            raise InterruptedError()
        return real_write(fd, data[:max(1, len(data) // 2)])
    monkeypatch.setattr(os, "write", partial)
    ledger.reserve_slot("s", kind="prices", snapshot=snapshot())
    ledger.close()
    reopened = ObservationLedger(ledger.path, plan_hash="a" * 64, max_bytes=1_000_000)
    reopened.open()
    assert reopened.summary()["interrupted"] == 1


@pytest.mark.parametrize("stage", ["slot", "begin", "terminal"])
@pytest.mark.parametrize("fault", ["zero_write", "fsync"])
def test_failed_durable_ack_poison_writer_and_preserve_disk(tmp_path, monkeypatch, stage, fault):
    from src.data.providers.toss.observation_ledger import LedgerError
    ledger = make_ledger(tmp_path)
    if stage != "slot":
        attempt, = ledger.reserve_slot("s", kind="prices", snapshot=snapshot())
    if stage == "terminal":
        ledger.begin(attempt)
    if fault == "zero_write":
        monkeypatch.setattr(os, "write", lambda fd, data: 0)
    else:
        def broken(fd): raise OSError("private error")
        monkeypatch.setattr(os, "fsync", broken)
    with pytest.raises(LedgerError, match="ledger_incomplete"):
        if stage == "slot": ledger.reserve_slot("s", kind="prices", snapshot=snapshot())
        elif stage == "begin": ledger.begin(attempt)
        else: ledger.finish(attempt, reason="provider_failure")
    assert ledger.summary()["incomplete"]
    with pytest.raises(LedgerError):
        ledger.reserve_slot("another", kind="prices", snapshot=snapshot())


def test_new_file_and_directory_are_fsynced_before_reservation_ack(tmp_path, monkeypatch):
    from src.data.providers.toss.observation_ledger import ObservationLedger
    import stat
    real_sync, kinds = os.fsync, []
    def sync(fd):
        kinds.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        real_sync(fd)
    monkeypatch.setattr(os, "fsync", sync)
    ledger = ObservationLedger(tmp_path / "new" / "ledger", plan_hash="a" * 64, max_bytes=10000)
    ledger.open()
    ledger.reserve_slot("s", kind="prices", snapshot=snapshot())
    assert kinds.count(True) >= 2 and False in kinds


def test_replaced_path_cannot_receive_or_ack_more_rows(tmp_path):
    from src.data.providers.toss.observation_ledger import LedgerError
    ledger = make_ledger(tmp_path)
    ledger.reserve_slot("s", kind="prices", snapshot=snapshot())
    ledger.path.rename(tmp_path / "original")
    ledger.path.touch(mode=0o600)
    with pytest.raises(LedgerError):
        ledger.reserve_slot("s2", kind="prices", snapshot=snapshot())
    assert ledger.path.read_bytes() == b""


@pytest.mark.parametrize("observation", [{"valid_pairs": -7, "excluded_pairs": True},
    {"valid_pairs": 1, "excluded_pairs": 0}, "bad", {"holiday": {}, "requested_date": "2026-09-16"}])
def test_invalid_terminal_schema_is_rejected_before_append_and_on_reload(tmp_path, observation):
    from src.data.providers.toss.observation_ledger import LedgerError, ObservationLedger
    ledger = make_ledger(tmp_path)
    attempt, = ledger.reserve_slot("s", kind="prices", snapshot=snapshot())
    before = ledger.path.read_bytes()
    with pytest.raises(LedgerError):
        ledger.finish(attempt, reason="success", observation=observation)
    assert ledger.path.read_bytes() == before
    ledger.close()
    append_hash_valid(ledger.path, {"type": "terminal", "attempt_id": attempt,
        "reason": "success", "observation": observation})
    report = ObservationLedger.read_only_summary(ledger.path, plan_hash="a" * 64, max_bytes=1_000_000)
    assert report["incomplete"] and report["error_code"] == "ledger_corrupt"


def test_missing_read_only_input_is_structured_failure_and_creates_nothing(tmp_path):
    from src.data.providers.toss.observation_ledger import ObservationLedger
    path = tmp_path / "absent"
    report = ObservationLedger.read_only_summary(path, plan_hash="a" * 64, max_bytes=10000)
    assert report["incomplete"] and report["error_code"] == "ledger_unavailable"
    assert not path.exists()


def test_kind_slots_missed_tombstones_and_duplicates_survive_restart(tmp_path):
    from src.data.providers.toss.observation_ledger import ObservationLedger
    ledger = make_ledger(tmp_path)
    price_ids = ledger.reserve_slot("s", kind="prices", snapshot=snapshot())
    calendar_ids = ledger.reserve_slot("s", kind="calendar", snapshot=snapshot(()))
    assert price_ids and calendar_ids and price_ids != calendar_ids
    ledger.record_missed("m", kind="prices")
    ledger.record_missed("m", kind="prices")
    assert ledger.reserve_slot("m", kind="prices", snapshot=snapshot()) is None
    assert ledger.reserve_slot("s", kind="prices", snapshot=snapshot(("OTHER",))) is None
    ledger.close()
    fresh = ObservationLedger(ledger.path, plan_hash="a" * 64, max_bytes=1_000_000)
    fresh.open()
    report = fresh.summary()
    assert (report["expected_slots"], report["recorded_slots"], report["missed_slots"]) == (None, 2, 1)
    assert report["coverage_status"] == "unavailable"
    assert report["duplicate_submissions"] == 3
    assert report["by_kind"]["prices"]["selected_attempts"] == 1
    assert report["by_kind"]["calendar"]["selected_attempts"] == 1


def test_conflicting_same_reason_calendar_payload_is_not_idempotent(tmp_path):
    from src.data.providers.toss.observation_ledger import LedgerError, ObservationLedger
    ledger = make_ledger(tmp_path)
    snap = dict(snapshot(()), snapshot_id="calendar:2026-09-16")
    attempt, = ledger.reserve_slot("s", kind="calendar", snapshot=snap)
    obs = {"holiday": False, "requested_date": "2026-09-16"}
    ledger.finish(attempt, reason="success", observation=obs)
    original = ledger.path.read_bytes()
    ledger.finish(attempt, reason="success", observation=dict(obs))
    assert ledger.path.read_bytes() == original
    with pytest.raises(LedgerError): ledger.finish(attempt, reason="success", observation=dict(obs, holiday=True))
    ledger.close()
    append_hash_valid(ledger.path, {"observation": dict(obs, holiday=True)})
    report = ObservationLedger.read_only_summary(ledger.path, plan_hash="a" * 64, max_bytes=1_000_000)
    assert report["incomplete"] and report["terminal_attempts"] == 1


def test_exact_repeated_terminal_on_disk_counts_once(tmp_path):
    from src.data.providers.toss.observation_ledger import ObservationLedger
    ledger = make_ledger(tmp_path)
    attempt, = ledger.reserve_slot("s", kind="prices", snapshot=snapshot())
    ledger.finish(attempt, reason="provider_failure")
    ledger.close()
    append_hash_valid(ledger.path, {})
    report = ObservationLedger.read_only_summary(ledger.path, plan_hash="a" * 64, max_bytes=1_000_000)
    assert report["terminal_attempts"] == report["provider_failures"] == 1 and not report["incomplete"]


@pytest.mark.parametrize("kind", ["missing", "truncated", "symlink", "hardlink", "public_parent", "bad_decimal"])
def test_cli_invalid_input_is_structured_nonzero_and_read_only(tmp_path, kind):
    from src.data.providers.toss.observation_ledger import ObservationLedger
    path = tmp_path / "ledger"
    if kind == "truncated":
        path.write_bytes(b'{"schema_version":1'); path.chmod(0o600)
    elif kind in {"symlink", "hardlink"}:
        target = tmp_path / "target"; target.touch(mode=0o600)
        if kind == "symlink": path.symlink_to(target)
        else: os.link(target, path)
    elif kind == "public_parent":
        path.touch(mode=0o600); tmp_path.chmod(0o755)
    elif kind == "bad_decimal":
        ledger = make_ledger(tmp_path)
        ledger.reserve_slot("s", kind="prices", snapshot=snapshot())
        ledger.close()
        snap = snapshot(); snap.pop("kis")
        snap["cohort"] = {"dataset_kind": "synthetic", "session": "regular", "min_valid_pairs": 1, "outlier_pct": "bad",
                          "max_age_seconds": 120, "max_skew_seconds": 30, "expected_market_basis": "krx"}
        append_hash_valid(path, {"snapshot": snap})
    before = path.read_bytes() if path.exists() else None
    command = [sys.executable, "scripts/review_toss_observation.py", "--ledger", str(path), "--plan-hash", "a" * 64, "--max-bytes", "1000000"]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 2 and "Traceback" not in result.stderr
    report = json.loads(result.stdout)
    assert report["incomplete"] and not report["production_eligible"]
    assert (path.read_bytes() if path.exists() else None) == before


def test_second_process_cannot_own_active_journal(tmp_path):
    ledger = make_ledger(tmp_path)
    command = [sys.executable, "-c", "from src.data.providers.toss.observation_ledger import ObservationLedger,LedgerError\nimport sys\ntry:\n ObservationLedger(sys.argv[1],plan_hash='a'*64,max_bytes=1000000).open()\nexcept LedgerError as exc:\n print(exc.code)\n", str(ledger.path)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.strip() == "ledger_unavailable"


def test_configured_plan_counts_fixed_grid_and_never_invents_coverage(tmp_path):
    from src.data.providers.toss.observation_ledger import ObservationLedger
    ledger = make_ledger(tmp_path)
    policy = {"dataset_kind": "synthetic", "dates": ("2026-09-16",), "calendar_time": "08:00",
        "sessions": ({"name": "regular", "start": "09:02", "end": "09:12"},),
        "comparison": {"min_valid_pairs": 1, "outlier_pct": 5, "max_age_seconds": 120,
                       "max_skew_seconds": 30, "expected_market_basis": "krx"}}
    ledger.configure_plan(policy)
    ledger.record_missed("2026-09-16T09:05:00+09:00", kind="prices")
    ledger.record_missed("not-a-plan-slot", kind="prices")
    ledger.close()
    report = ObservationLedger.read_only_summary(ledger.path, plan_hash="a" * 64, max_bytes=1_000_000)
    assert report["expected_slots"] == 3 and report["unaccounted_slots"] == 2
    assert report["unexpected_slots"] == 1 and report["coverage_scope"] == "full_plan"
    assert report["coverage_status"] == "incomplete"


def test_committed_terminal_is_detached_from_caller_and_idempotency_is_type_strict(tmp_path):
    from src.data.providers.toss.observation_ledger import LedgerError
    ledger = make_ledger(tmp_path)
    snap = dict(snapshot(()), snapshot_id="calendar:2026-09-16")
    attempt, = ledger.reserve_slot("s", kind="calendar", snapshot=snap)
    obs = {"holiday": False, "requested_date": "2026-09-16"}
    ledger.finish(attempt, reason="success", observation=obs)
    obs["holiday"] = True
    # Caller mutation must neither alter the persisted outcome nor conflict with
    # an exact replay of the original terminal.
    ledger.finish(attempt, reason="success", observation={"holiday": False, "requested_date": "2026-09-16"})
    with pytest.raises(LedgerError):
        ledger.finish(attempt, reason="success", observation={"holiday": 0, "requested_date": "2026-09-16"})


@pytest.mark.parametrize("flag,value", [("--plan-hash", "invalid"), ("--max-bytes", "-1")])
def test_cli_invalid_bounds_remain_structured_without_traceback(tmp_path, flag, value):
    args = {"--ledger": str(tmp_path / "missing"), "--plan-hash": "a" * 64, "--max-bytes": "1000"}
    args[flag] = value
    result = subprocess.run([sys.executable, "scripts/review_toss_observation.py", *(v for pair in args.items() for v in pair)], capture_output=True, text=True)
    assert result.returncode == 2 and "Traceback" not in result.stderr
    assert json.loads(result.stdout)["incomplete"]


@pytest.mark.parametrize("fault", ["mkdir", "directory_fsync"])
def test_directory_creation_failures_prevent_any_reservation(tmp_path, monkeypatch, fault):
    from src.data.providers.toss.observation_ledger import LedgerError, ObservationLedger
    import stat
    ledger = ObservationLedger(tmp_path / "private" / "ledger", plan_hash="a" * 64, max_bytes=10000)
    if fault == "mkdir":
        def mkdir(*args, **kwargs): raise OSError("private mkdir")
        monkeypatch.setattr(os, "mkdir", mkdir)
    else:
        real_sync = os.fsync
        def sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode): raise OSError("private directory fsync")
            real_sync(fd)
        monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises(LedgerError): ledger.open()
    with pytest.raises(LedgerError): ledger.reserve_slot("s", kind="prices", snapshot=snapshot())
    assert ledger.summary()["incomplete"] and ledger.summary()["selected_attempts"] == 0


def test_created_directory_fsync_failure_closes_its_descriptor(tmp_path, monkeypatch):
    from src.data.providers.toss.observation_ledger import LedgerError, ObservationLedger
    real_sync, synced = os.fsync, []
    def sync(fd):
        synced.append(fd)
        if len(synced) == 2: raise OSError("new directory fsync failure")
        real_sync(fd)
    monkeypatch.setattr(os, "fsync", sync)
    ledger = ObservationLedger(tmp_path / "private" / "ledger", plan_hash="a" * 64, max_bytes=10000)
    with pytest.raises(LedgerError): ledger.open()
    with pytest.raises(OSError): os.fstat(synced[1])


def test_approved_plan_allows_unsorted_nonoverlapping_sessions_and_zero_age(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.configure_plan({"dataset_kind": "live", "dates": ("2026-09-16",), "calendar_time": "08:00",
        "sessions": ({"name": "regular", "start": "09:00", "end": "09:10"}, {"name": "pre", "start": "08:00", "end": "08:10"}),
        "comparison": {"min_valid_pairs": 1, "outlier_pct": 5, "max_age_seconds": 0,
                       "max_skew_seconds": 0, "expected_market_basis": "unconfirmed_basis"}})
    assert ledger.summary()["expected_slots"] == 5
