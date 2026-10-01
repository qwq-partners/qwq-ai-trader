"""관측 원장 내구 경계. 임시 경로/합성 자료만 쓰고 운영 자료는 읽지 않는다."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading

import pytest

from src.analytics.entry_observation import EntryObservationBuffer


def journal_module():
    try:
        from src.analytics import entry_observation_journal
        return entry_observation_journal
    except ImportError:
        pytest.fail("선택적 관측 원장 미구현")


def record(n=1):
    return {"kind": "ws_quote", "quote_id": f"q-{n}", "symbol": "005930",
            "observed_at": "2026-10-01T01:00:01+00:00", "ask": "10000", "bid": "9990",
            "ask_size": 10, "bid_size": 10, "provenance": {"tr_id": "H0STASP0",
            "received_at": "2026-10-01T01:00:00+00:00", "source_as_of": None,
            "message_count": 1}}


async def opened(tmp_path, **overrides):
    m = journal_module()
    buffer = EntryObservationBuffer(evaluation_epoch="synthetic", capacity=100)
    args = dict(study_ref="synthetic-study", study_sha256="a" * 64, queue_capacity=20,
                batch_size=5, max_bytes=100000, max_record_bytes=10000)
    args.update(overrides)
    path = tmp_path / "capture.jsonl"
    journal = await m.ObservationJournal.open(buffer, path, **args)
    return buffer, journal, path


def read(path, **overrides):
    args = dict(max_bytes=100000, expected_study_sha256="a" * 64)
    args.update(overrides)
    return journal_module().read_observation_journal(path, **args)


@pytest.mark.asyncio
async def test_clean_seal_preserves_records_and_boundary_after_shutdown_gap(tmp_path):
    buffer, journal, path = await opened(tmp_path)
    original = record()
    assert buffer.publish(original)
    original["ask"] = "mutated"
    result = await journal.close()
    assert result["sealed"] and result["fsync_confirmed"]
    assert buffer.publish(record(2)) is False
    buffer.mark_incomplete("socket_closed_after_capture_boundary")
    loaded = read(path)
    assert loaded["complete"] is True and loaded["dropped_records"] == 0
    assert loaded["records"] == buffer.export()["records"]
    assert loaded["records"][0]["ask"] == "10000"
    assert loaded["journal"]["durability_confirmed"] is False
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_existing_file_and_nonempty_buffer_never_overwritten(tmp_path):
    _, journal, path = await opened(tmp_path)
    before = path.read_bytes()
    with pytest.raises((ValueError, FileExistsError)):
        await opened(tmp_path)
    assert path.read_bytes() == before
    await journal.close()
    b = EntryObservationBuffer(evaluation_epoch="synthetic", capacity=5)
    b.publish(record())
    with pytest.raises(ValueError):
        await journal_module().ObservationJournal.open(b, tmp_path / "other.jsonl", study_ref="x",
            study_sha256="a"*64, queue_capacity=2, batch_size=1, max_bytes=10000, max_record_bytes=1000)
    assert not (tmp_path / "other.jsonl").exists()


@pytest.mark.asyncio
async def test_unsealed_prefix_is_unknown_and_does_not_invent_zero_loss(tmp_path):
    buffer, journal, path = await opened(tmp_path)
    buffer.publish(record())
    await journal.close()
    raw = path.read_bytes()
    path.write_bytes(b"\n".join(raw.splitlines()[:-1]) + b"\n")
    before = path.read_bytes()
    result = read(path)
    assert not result["complete"] and result["dropped_records"] is None
    assert result["journal"]["dropped_count_exact"] is False
    assert len(result["records"]) == 1 and path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["torn_tail", "changed_price", "duplicate_line", "after_seal", "wrong_study"])
async def test_bad_journal_rejected_without_repair(tmp_path, corruption):
    buffer, journal, path = await opened(tmp_path)
    buffer.publish(record())
    await journal.close()
    lines = path.read_bytes().splitlines(keepends=True)
    kwargs = {}
    if corruption == "torn_tail": lines[-1] = lines[-1][:-8]
    elif corruption == "changed_price": lines[1] = lines[1].replace(b"10000", b"90000")
    elif corruption == "duplicate_line": lines.insert(2, lines[1])
    elif corruption == "after_seal": lines.append(lines[1])
    else: kwargs["expected_study_sha256"] = "b"*64
    path.write_bytes(b"".join(lines)); before = path.read_bytes()
    with pytest.raises(ValueError): read(path, **kwargs)
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_queue_overflow_is_sticky_and_publish_does_not_wait_for_disk(tmp_path, monkeypatch):
    buffer, journal, path = await opened(tmp_path, queue_capacity=1, batch_size=1)
    entered, release = threading.Event(), threading.Event()
    original = journal._write_batch
    def slow(rows):
        entered.set()
        assert release.wait(5)
        return original(rows)
    monkeypatch.setattr(journal, "_write_batch", slow)
    try:
        buffer.publish(record(1))
        assert await asyncio.to_thread(entered.wait, 2)
        # writer가 멈췄는데도 호출 경로는 반환된다. 수면 시간 비교로 테스트하지 않는다.
        assert buffer.publish(record(2))
        assert buffer.publish(record(3))
        assert buffer.publish(record(4))
        assert buffer.export()["complete"] is False
    finally:
        release.set()
    await journal.close()
    loaded = read(path)
    assert loaded["complete"] is False
    assert [r["quote_id"] for r in loaded["records"]] == ["q-1", "q-2"]
    assert loaded["journal"]["persistence_dropped_records"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["secret_key", "nested_secret", "unknown_kind", "oversized", "nan"])
async def test_persistence_boundary_rejects_unapproved_data(tmp_path, defect):
    buffer, journal, path = await opened(tmp_path, max_record_bytes=800)
    value = record()
    if defect == "secret_key": value["approval_key"] = "synthetic-do-not-persist"
    elif defect == "nested_secret": value["provenance"]["token"] = "synthetic-do-not-persist"
    elif defect == "unknown_kind": value["kind"] = "broker_raw_reply"
    elif defect == "oversized": value["symbol"] = "S" * 2000
    else: value["ask"] = float("nan")
    buffer.publish(value)
    await journal.close()
    loaded = read(path)
    assert loaded["complete"] is False and loaded["records"] == []
    assert b"synthetic-do-not-persist" not in path.read_bytes()


@pytest.mark.asyncio
async def test_writer_failure_does_not_escape_callback_or_create_complete_file(tmp_path, monkeypatch):
    buffer, journal, path = await opened(tmp_path)
    def fail(rows): raise OSError("synthetic disk failure")
    monkeypatch.setattr(journal, "_write_batch", fail)
    assert buffer.publish(record())
    result = await journal.close()
    assert not result["sealed"] and not result["fsync_confirmed"]
    assert read(path)["complete"] is False


@pytest.mark.asyncio
async def test_close_cancellation_cannot_cancel_shared_seal_or_extend_boundary(tmp_path, monkeypatch):
    buffer, journal, path = await opened(tmp_path, batch_size=1)
    entered, release = threading.Event(), threading.Event()
    original = journal._write_batch
    def slow(rows):
        entered.set(); assert release.wait(5)
        return original(rows)
    monkeypatch.setattr(journal, "_write_batch", slow)
    buffer.publish(record())
    assert await asyncio.to_thread(entered.wait, 2)
    first = asyncio.create_task(journal.close())
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError): await first
    assert buffer.publish(record(2)) is False
    release.set()
    assert (await journal.close())["sealed"]
    assert len(read(path)["records"]) == 1


@pytest.mark.asyncio
async def test_memory_overflow_and_existing_gap_survive_seal(tmp_path):
    buffer, journal, path = await opened(tmp_path)
    buffer.capacity = 1
    assert buffer.publish(record())
    assert buffer.publish(record(2)) is False
    buffer.mark_incomplete("socket_closed")
    await journal.close()
    loaded = read(path)
    assert loaded["complete"] is False and loaded["dropped_records"] == 1
    assert "socket_closed" in loaded["incomplete_reasons"]


@pytest.mark.asyncio
async def test_reader_size_limit_rejects_before_loading(tmp_path):
    buffer, journal, path = await opened(tmp_path)
    buffer.publish(record())
    await journal.close()
    with pytest.raises(ValueError): read(path, max_bytes=10)


@pytest.mark.asyncio
async def test_async_disk_error_also_invalidates_memory_export(tmp_path, monkeypatch):
    buffer, journal, path = await opened(tmp_path)
    def fail(rows): raise OSError("synthetic")
    monkeypatch.setattr(journal, "_write_batch", fail)
    buffer.publish(record())
    await journal.close()
    assert buffer.export()["complete"] is False


@pytest.mark.asyncio
async def test_file_byte_limit_keeps_prefix_unknown(tmp_path):
    buffer, journal, path = await opened(tmp_path, max_bytes=1300)
    for i in range(10): buffer.publish(record(i))
    result = await journal.close()
    assert not result["sealed"] and path.stat().st_size <= 1300
    assert not read(path)["complete"]


@pytest.mark.asyncio
async def test_cli_replays_exact_study_and_supplements_without_changing_inputs(tmp_path, capsys):
    from test_received_entry_shadow import observed_bundle
    from src.analytics.entry_observation import prepare_input
    from scripts.compare_entry_price_shadow import main
    context, observations, inputs = observed_bundle()
    study_path = tmp_path / "study.json"
    study_path.write_text(json.dumps(context), encoding="utf-8")
    inputs_path = tmp_path / "inputs.json"
    inputs_path.write_text(json.dumps(inputs), encoding="utf-8")
    digest = hashlib.sha256(study_path.read_bytes()).hexdigest()
    m = journal_module()
    buffer = EntryObservationBuffer(evaluation_epoch=context["evaluation_epoch"], capacity=100)
    path = tmp_path / "capture.jsonl"
    journal = await m.ObservationJournal.open(buffer, path, study_ref="synthetic-test-v1",
        study_sha256=digest, queue_capacity=20, batch_size=5, max_bytes=100000, max_record_bytes=10000)
    for row in observations["records"]: buffer.publish(row)
    await journal.close()
    before = {p: p.read_bytes() for p in (path, study_path, inputs_path)}
    args = ["--journal", str(path), "--study", str(study_path), "--evaluation-inputs", str(inputs_path),
            "--max-journal-bytes", "100000"]
    assert main(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["report"] == prepare_input(context, observations, inputs)["report"]
    assert output["journal"]["study_sha256"] == digest
    assert all(p.read_bytes() == data for p, data in before.items())
    # 순서 보존된 무봉인 prefix는 알려진 완전 이익으로 승격되지 않는다.
    path.write_bytes(b"\n".join(path.read_bytes().splitlines()[:-1]) + b"\n")
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert not result["capture_complete"] and result["ready_opportunities"] == 0
    assert result["report"]["summary"]["complete_delta_net_pnl"] is None
    study_path.write_text(json.dumps({**context, "as_of": "2030-01-01T00:00:00+09:00"}))
    assert main(args) == 2


def test_cli_requires_explicit_journal_inputs(tmp_path):
    from scripts.compare_entry_price_shadow import main
    with pytest.raises(SystemExit) as error:
        main(["--journal", str(tmp_path / "missing")])
    assert error.value.code == 2


def rewrite_chain(path, mutate):
    rows = [json.loads(line) for line in path.read_bytes().splitlines()]
    mutate(rows)
    previous = "0" * 64
    def encode(v): return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    for row in rows:
        row.pop("hash")
        row["previous_hash"] = previous
        previous = hashlib.sha256(encode(row)).hexdigest()
        row["hash"] = previous
    path.write_bytes(b"\n".join(encode(r) for r in rows) + b"\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["bad_seal_time", "time_reversal", "capacity", "batch_capacity", "record_bytes"])
async def test_reader_checks_declared_bounds_and_boundary_even_with_consistent_hashes(tmp_path, defect):
    buffer, journal, path = await opened(tmp_path)
    buffer.publish(record(1)); buffer.publish(record(2))
    await journal.close()
    def mutate(rows):
        if defect == "bad_seal_time": rows[-1]["payload"]["sealed_at"] = "no-date"
        elif defect == "time_reversal": rows[-1]["payload"]["sealed_at"] = "2000-01-01T00:00:00+00:00"
        elif defect == "capacity": rows[0]["payload"]["capacity"] = 1
        elif defect == "batch_capacity": rows[0]["payload"]["batch_size"] = 100
        else:
            size = len(json.dumps(rows[1]["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
            rows[0]["payload"]["max_record_bytes"] = size - 1
    rewrite_chain(path, mutate)
    with pytest.raises(ValueError): read(path)


@pytest.mark.asyncio
async def test_symlink_target_and_special_file_are_not_opened_as_journal(tmp_path):
    import os
    target = tmp_path / "target"
    target.write_text("untouched")
    link = tmp_path / "capture.jsonl"
    link.symlink_to(target)
    with pytest.raises((ValueError, OSError)): await opened(tmp_path)
    assert target.read_text() == "untouched"
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError): read(fifo)


def test_cancelled_close_does_not_pin_process_executor_shutdown(tmp_path):
    import subprocess
    import sys
    code = '''
import asyncio,threading,sys
from pathlib import Path
from src.analytics.entry_observation import EntryObservationBuffer
from src.analytics.entry_observation_journal import ObservationJournal
async def main():
 b=EntryObservationBuffer(evaluation_epoch="synthetic",capacity=2)
 j=await ObservationJournal.open(b,Path(sys.argv[1]),study_ref="synthetic",study_sha256="a"*64,queue_capacity=2,batch_size=1,max_bytes=10000,max_record_bytes=1000)
 entered=threading.Event()
 def blocked(rows):
  entered.set();threading.Event().wait()
 j._write_batch=blocked
 b.publish({"kind":"emit_result","candidate_id":"s:x","signal_id":"sig","observed_at":"2026-10-01T00:00:00+00:00","emitted":True})
 while not entered.is_set(): await asyncio.sleep(0.001)
 closing=asyncio.create_task(j.close())
 await asyncio.sleep(0.02)
 closing.cancel()
 try: await closing
 except asyncio.CancelledError: pass
 print("caller returned",flush=True)
asyncio.run(main())
'''
    try:
        result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "blocked.jsonl")],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=3)
    except subprocess.TimeoutExpired:
        pytest.fail("취소된 원장 종료가 executor shutdown을 붙잡음")
    assert result.returncode == 0 and "caller returned" in result.stdout


@pytest.mark.asyncio
async def test_close_timeout_returns_incomplete_without_waiting_on_stalled_disk(tmp_path, monkeypatch):
    buffer, journal, path = await opened(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = journal._write_batch
    def slow(rows):
        entered.set(); assert release.wait(5)
        original(rows)
    monkeypatch.setattr(journal, "_write_batch", slow)
    try:
        buffer.publish(record())
        while not entered.is_set(): await asyncio.sleep(0.001)
        result = await journal.close(timeout_seconds=0.02)
        assert not result["sealed"] and result["error"] == "journal_close_timeout"
        assert not buffer.export()["complete"]
    finally:
        release.set()
    while journal._thread.is_alive(): await asyncio.sleep(0.001)
    assert read(path)["complete"] is False


@pytest.mark.asyncio
async def test_new_file_sync_includes_parent_directory(tmp_path, monkeypatch):
    import os
    import stat
    m = journal_module()
    synced_directory = []
    real_sync = os.fsync
    def track(fd):
        synced_directory.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        return real_sync(fd)
    monkeypatch.setattr(m.os, "fsync", track)
    _, journal, _ = await opened(tmp_path)
    await journal.close()
    assert any(synced_directory)
