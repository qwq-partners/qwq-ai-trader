"""Offline regressions for producer completion, journal identity and admission evidence."""
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from src.analytics import model_comparison_input as mci
from src.analytics.model_comparison_worker import read_incomplete_journal, read_offer_journal, row_sha256
from tests.test_model_comparison_worker import build, envelope, FakeOpenAI, wait


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


def offers(worker):
    path, = worker.journal_dir.glob("offers-*.jsonl")
    return read_offer_journal(path, max_bytes=1 << 20)


def test_late_storage_failure_is_durable_after_bounded_stop(tmp_path, monkeypatch):
    worker, ledger, *_ = build(tmp_path)
    original = ledger._connect
    reached, release = threading.Event(), threading.Event()

    class Connection:
        def __init__(self, db): self.db = db
        def __getattr__(self, name): return getattr(self.db, name)
        def execute(self, sql, *args):
            if sql.startswith("UPDATE comparison_pairs SET result="):
                reached.set()
                assert release.wait(15), "test did not release storage"
                raise sqlite3.OperationalError("delayed pair storage failure")
            return self.db.execute(sql, *args)

    @contextmanager
    def connect(**kwargs):
        with original(**kwargs) as db:
            yield Connection(db)

    monkeypatch.setattr(ledger, "_connect", connect)
    worker.start()
    try:
        assert worker.offer(envelope(worker)) == "accepted"
        assert reached.wait(5)
        stopped = worker.stop(0)
        assert stopped["alive"] is True and stopped["drained"] is False
        assert stopped["journal_drained"] is False
        assert offers(worker)["open_runs"]
        assert worker.stop(0)["journal_drained"] is False
    finally:
        release.set()
        assert wait(lambda: not worker.health()["alive"])
    assert wait(lambda: not worker._journal_thread.is_alive())
    path, = worker.journal_dir.glob("incomplete-*.jsonl")
    recovered = read_incomplete_journal(path, max_bytes=1 << 20)
    record, = recovered["records"]
    assert record["row"] == worker.incomplete_results[0]
    assert recovered["open_runs"] == [] and recovered["closed_runs"]
    read = offers(worker)
    assert read["records"][-1]["reason"] == "pair_result_unpersisted"
    before = path.read_bytes()
    assert worker.stop(0)["journal_drained"] is True
    assert worker.stop(0)["journal_drained"] is True
    assert path.read_bytes() == before


def test_offer_in_progress_cannot_land_behind_journal_close(tmp_path, monkeypatch):
    worker, *_ = build(tmp_path)
    reached, release, close_written = threading.Event(), threading.Event(), threading.Event()
    original_put, original_write = worker._journal_put, worker._journal_write

    def paused_offer(record, timeout=None):
        if record["kind"] == "offer":
            reached.set()
            assert release.wait(5), "test did not release offer"
        return original_put(record, timeout)

    def observe_close(record, path=None):
        result = original_write(record, path)
        if record["kind"] == "journal_close":
            close_written.set()
        return result

    monkeypatch.setattr(worker, "_journal_put", paused_offer)
    monkeypatch.setattr(worker, "_journal_write", observe_close)
    worker.start()
    env = envelope(worker)
    results = []
    offer_thread = threading.Thread(target=lambda: results.append(worker.offer(env)))
    stopper = threading.Thread(target=lambda: worker.stop(0))
    try:
        offer_thread.start()
        assert reached.wait(5)
        stopper.start()
        close_written.wait(0.3)  # Old code seals while this admitted offer is paused.
    finally:
        release.set()
        offer_thread.join(5)
        stopper.join(5)
        worker.stop(3)
    assert not offer_thread.is_alive() and not stopper.is_alive()
    assert results == ["accepted"]
    read = offers(worker)
    assert len([r for r in read["records"] if r["kind"] == "offer"]) == 1
    assert read["open_runs"] == [] and len(read["closed_runs"]) == 1
    assert worker.health()["journal_depth"] == 0


def test_recreated_worker_has_independent_journal_closure_and_gap_evidence(tmp_path):
    first, ledger, op, ge = build(tmp_path)
    first.start()
    assert first.offer(envelope(first, "first")) == "accepted"
    assert wait(lambda: first.health()["finished"] == 1)
    first.stop(3)
    old = offers(first)
    second, *_ = build(tmp_path, ledger=ledger, op=op, ge=ge)
    second.start()
    try:
        env = envelope(second, "second")
        assert env["producer"]["run_id"] == mci.RUN_ID
        assert second.offer(env) == "accepted"
        assert wait(lambda: second.health()["finished"] == 1)
        assert wait(lambda: len(offers(second)["records"]) == 6)
        during = offers(second)
        assert len(during["runs"]) == 2
        assert during["closed_runs"] == old["closed_runs"]
        assert len(during["open_runs"]) == 1
    finally:
        second.stop(3)
    path, = first.journal_dir.glob("offers-*.jsonl")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows
                            if not (row["kind"] == "offer" and row.get("decision_id") == "first")))
    final = offers(first)
    assert final["sequence_gaps"] == 1
    assert len(final["closed_runs"]) == 2 and final["open_runs"] == []


@pytest.mark.parametrize("conflicting", [False, True])
def test_duplicate_journal_sequence_is_not_silently_collapsed(tmp_path, conflicting):
    path = tmp_path / "offers.jsonl"
    offer = {"schema_version": "comparison-offer-v1", "kind": "offer", "run_id": "legacy",
             "journal_seq": 1, "decision_id": "first", "status": "accepted"}
    repeated = dict(offer, decision_id="other") if conflicting else dict(offer)
    close = {"schema_version": "comparison-offer-v1", "kind": "journal_close", "run_id": "legacy",
             "journal_seq": 2, "final_seq": 2, "journal_failed_total": 0, "journal_dropped_total": 0}
    path.write_text("".join(json.dumps(row) + "\n" for row in (offer, repeated, close)))
    read = read_offer_journal(path, max_bytes=1 << 20)
    assert read["skipped_lines"] >= 1
    assert read["duplicate_sequences"] == 1
    assert read["conflicting_sequences"] == int(conflicting)


class Inflight(FakeOpenAI):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()

    async def complete(self, *args, **kwargs):
        self.entered.set()
        await asyncio.Event().wait()


@pytest.mark.parametrize("unreadable", [False, True])
def test_shutdown_distinguishes_queued_and_durably_admitted_pair(tmp_path, monkeypatch, unreadable):
    op = Inflight()
    worker, ledger, *_ = build(tmp_path, op=op)
    worker.start()
    try:
        assert worker.offer(envelope(worker, "running")) == "accepted"
        assert op.entered.wait(5)
        assert worker.offer(envelope(worker, "queued")) == "accepted"
        if unreadable:
            def failed_read(day):
                raise sqlite3.OperationalError("read unavailable")
            monkeypatch.setattr(worker.runner, "admitted_pair_ids", failed_read)
    finally:
        result = worker.stop(0)
    assert result["alive"] is False and result["journal_drained"] is True
    transitions = {r["decision_id"]: r for r in offers(worker)["records"]
                   if r["kind"] == "transition" and r["status"] == "incomplete"}
    assert transitions["queued"]["reason"] == "shutdown_not_started"
    assert transitions["queued"]["admitted"] is False
    assert transitions["running"]["reason"] == "cancelled_at_shutdown"
    assert transitions["running"]["admitted"] is (None if unreadable else True)


def test_cancellation_before_claim_records_no_admission(tmp_path, monkeypatch):
    worker, ledger, op, ge = build(tmp_path)
    entered = threading.Event()

    async def before_claim(spec):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(worker.runner, "run", before_claim)
    worker.start()
    assert worker.offer(envelope(worker)) == "accepted"
    assert entered.wait(5)
    worker.stop(0)
    record = offers(worker)["records"][-1]
    assert record["reason"] == "cancelled_at_shutdown" and record["admitted"] is False
    assert not worker.runner.admitted_pair_ids(ledger.today().isoformat())
    assert not op.calls and not ge.calls


def test_real_close_preserves_drops_in_incomplete_stream_without_drop_marker(tmp_path):
    worker, *_ = build(tmp_path, journal_queue=1)
    path = tmp_path / "incomplete.jsonl"
    row = {"pair_id": "previous-day", "decision_id": "previous-day"}
    worker._journal_write({"schema_version": "comparison-offer-v1", "kind": "incomplete_result",
                           "run_id": worker._journal_run_id, "pair_id": row["pair_id"], "row": row,
                           "row_sha256": row_sha256(row), "recorded_at": "2026-10-13T09:00:00+09:00"}, path)
    # The next day's recovery row is dropped by the real bounded queue. Its drop
    # marker can be written to offers only; this incomplete stream has the old row.
    worker._journal_put({"kind": "offer", "at": "2026-10-14T09:00:00+09:00"})
    next_row = {"pair_id": "next-day", "decision_id": "next-day"}
    worker._journal_put({"schema_version": "comparison-offer-v1", "kind": "incomplete_result",
                         "decision_id": "next-day", "pair_id": next_row["pair_id"],
                         "row": next_row, "row_sha256": row_sha256(next_row),
                         "recorded_at": "2026-10-14T09:00:00+09:00"})
    worker._journal_write({"schema_version": "comparison-offer-v1", "kind": "journal_drop",
                           "run_id": worker._journal_run_id, "at": "2026-10-14T09:00:00+09:00",
                           "dropped_total": worker.health()["journal_dropped"]}, tmp_path / "offers.jsonl")
    worker._journal_write({"schema_version": "comparison-offer-v1", "kind": "journal_close",
                           "run_id": worker._journal_run_id, "at": "2026-10-14T09:00:00+09:00"}, path)
    read = read_incomplete_journal(path, max_bytes=1 << 20)
    assert read["dropped_records"] == 1
    assert [r["row"]["decision_id"] for r in read["records"]] == ["previous-day"]
    assert read["skipped_lines"] == 0 and read["open_runs"] == []


@pytest.mark.parametrize("marker,closing,expected", [(2, 5, 5), (5, 2, 5), (5, 5, 5), (0, 0, 0)])
def test_drop_marker_and_close_use_per_run_high_water(tmp_path, marker, closing, expected):
    path = tmp_path / "offers.jsonl"
    base = {"schema_version": "comparison-offer-v1", "run_id": "legacy"}
    rows = [dict(base, kind="journal_drop", journal_seq=1, dropped_total=marker),
            dict(base, kind="journal_close", journal_seq=2, final_seq=2,
                 journal_failed_total=0, journal_dropped_total=closing)]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    read = read_offer_journal(path, max_bytes=1 << 20)
    assert read["dropped_records"] == expected
    assert read["skipped_lines"] == 0


@pytest.mark.parametrize("invalid", [None, -1, True, "1", 1.5])
def test_invalid_close_drop_count_is_incomplete_evidence(tmp_path, invalid):
    path = tmp_path / "incomplete.jsonl"
    close = {"schema_version": "comparison-offer-v1", "kind": "journal_close", "run_id": "legacy",
             "journal_seq": 1, "final_seq": 1, "journal_failed_total": 0, "journal_dropped_total": invalid}
    path.write_text(json.dumps(close) + "\n")
    read = read_incomplete_journal(path, max_bytes=1 << 20)
    assert read["skipped_lines"] == 1
    assert read["closed_runs"] == [] and read["open_runs"] == ["legacy"]


def test_legacy_close_without_drop_count_remains_readable(tmp_path):
    path = tmp_path / "offers.jsonl"
    close = {"schema_version": "comparison-offer-v1", "kind": "journal_close", "run_id": "legacy",
             "journal_seq": 1, "final_seq": 1, "journal_failed_total": 0}
    path.write_text(json.dumps(close) + "\n")
    read = read_offer_journal(path, max_bytes=1 << 20)
    assert read["dropped_records"] == 0 and read["skipped_lines"] == 0
    assert read["closed_runs"] == ["legacy"] and read["open_runs"] == []
