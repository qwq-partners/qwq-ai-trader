"""판단 기록 종료: 실제 파일 보존, 동시 제출, 시간 제한과 러너 정리 순서."""
import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

from src.analytics import llm_decision_record as rec


def row(key):
    return {"decision_id": key, "decision_available_at": "2026-10-13T12:00:00+09:00"}


def saved(recorder):
    return [json.loads(line)["decision_id"] for path in recorder.directory.glob("*.jsonl")
            for line in path.read_text().splitlines()]


def test_close_drains_and_rejects_late_records_without_restart(tmp_path):
    r = rec.DecisionRecorder(tmp_path, enabled=True)
    assert r.submit(row("before")) == "queued"
    try:
        assert r.close(1) is True
        worker = r._thread
        assert not worker.is_alive()
        assert r.submit(row("after")) == "dropped_closed"
        assert r.status_of("after") == "dropped_closed"
        assert r.stats["dropped_closed"] == 1
        assert r.close(0) is True
        assert r._thread is worker and saved(r) == ["before"]
    finally:
        r.close(1)


def test_full_queue_timeout_still_drains_and_exits_when_disk_recovers(tmp_path, monkeypatch):
    r = rec.DecisionRecorder(tmp_path, enabled=True, queue_size=1)
    entered, release = threading.Event(), threading.Event()
    write = r._write
    def stalled(record):
        entered.set()
        release.wait(2)
        write(record)
    monkeypatch.setattr(r, "_write", stalled)
    r.submit(row("first"))
    assert entered.wait(1)
    r.submit(row("second"))
    try:
        start = time.monotonic()
        assert r.close(0.02) is False
        assert time.monotonic() - start < 0.3
        assert r.submit(row("late")) == "dropped_closed"
    finally:
        release.set()
        r.close(1)
    assert not r._thread.is_alive()
    assert saved(r) == ["first", "second"]
    assert r.close(0) is True


@pytest.mark.parametrize("failure", ["file_cap", "invalid_time", "unexpected_write_error"])
def test_close_does_not_claim_failed_records_were_saved(tmp_path, monkeypatch, failure):
    r = rec.DecisionRecorder(tmp_path, enabled=True, max_file_bytes=1 if failure == "file_cap" else 1000)
    record = row("failed")
    if failure == "invalid_time":
        record["decision_available_at"] = None
    elif failure == "unexpected_write_error":
        def broken(record):
            raise RuntimeError("synthetic disk failure")
        monkeypatch.setattr(r, "_write", broken)
    r.submit(record)
    assert r.close(0.3) is False
    assert not r._thread.is_alive()
    assert r.status_of("failed") == ("dropped_file_cap" if failure == "file_cap" else "write_error")
    assert r.close(0) is False


def test_submit_close_race_never_accepts_an_unsaved_record(tmp_path):
    r = rec.DecisionRecorder(tmp_path, enabled=True, queue_size=64)
    gate = threading.Barrier(17)
    results = {}
    def submit(i):
        gate.wait()
        results[str(i)] = r.submit(row(str(i)))
    workers = [threading.Thread(target=submit, args=(i,)) for i in range(16)]
    for worker in workers:
        worker.start()
    gate.wait()
    closed = r.close(1)
    for worker in workers:
        worker.join(1)
        assert not worker.is_alive()
    try:
        assert closed is True
        accepted = {key for key, status in results.items() if status == "queued"}
        assert set(saved(r)) == accepted
        assert len(saved(r)) == len(accepted)
        assert set(results.values()) <= {"queued", "dropped_closed"}
        assert r.stats["queued"] == r.stats["written"] == len(accepted)
    finally:
        r.close(1)


@pytest.mark.asyncio
async def test_async_close_does_not_create_unused_recorder(monkeypatch):
    monkeypatch.setattr(rec, "_recorder", None)
    assert await rec.close_decision_recorder(timeout=0) is True
    assert rec._recorder is None


@pytest.mark.asyncio
async def test_async_close_bounds_stalled_disk_without_blocking_loop(tmp_path, monkeypatch):
    r = rec.DecisionRecorder(tmp_path, enabled=True)
    monkeypatch.setattr(rec, "_recorder", r)
    entered, release = threading.Event(), threading.Event()
    write = r._write
    def stalled(record):
        entered.set()
        release.wait(2)
        write(record)
    monkeypatch.setattr(r, "_write", stalled)
    r.submit(row("a"))
    assert entered.wait(1)
    ticked = []
    async def tick():
        await asyncio.sleep(0.005)
        ticked.append(True)
    task = asyncio.create_task(tick())
    try:
        start = time.monotonic()
        assert await rec.close_decision_recorder(timeout=0.03) is False
        assert time.monotonic() - start < 0.3
        assert ticked == [True]
        assert r.submit(row("late")) == "dropped_closed"
        assert asyncio.get_running_loop()._default_executor is None
    finally:
        release.set()
        await task
        r.close(1)


@pytest.mark.asyncio
async def test_runner_drains_final_llm_record_before_token_cleanup(tmp_path, monkeypatch):
    from scripts.run_trader import UnifiedTradingBot
    from src.utils import llm
    r = rec.DecisionRecorder(tmp_path, enabled=True)
    monkeypatch.setattr(rec, "_recorder", r)
    async def close_llm():
        assert r.submit(row("last-llm")) == "queued"
    async def close_token():
        assert saved(r) == ["last-llm"]
        assert r.submit(row("too-late")) == "dropped_closed"
    monkeypatch.setattr(llm, "close_llm_manager", close_llm)
    bot = object.__new__(UnifiedTradingBot)
    bot.broker = bot._us_engine = bot.dashboard = bot.ws_feed = None
    bot._entry_observation_runtime = None
    bot._token_manager = SimpleNamespace(close=close_token)
    try:
        await bot.shutdown()
        assert r.status_of("too-late") == "dropped_closed"
    finally:
        r.close(1)


@pytest.mark.asyncio
async def test_cancelled_async_drain_keeps_recorder_closed_and_worker_can_finish(tmp_path, monkeypatch):
    r = rec.DecisionRecorder(tmp_path, enabled=True)
    monkeypatch.setattr(rec, "_recorder", r)
    release = threading.Event()
    write = r._write
    def stalled(record):
        release.wait(2)
        write(record)
    monkeypatch.setattr(r, "_write", stalled)
    r.submit(row("a"))
    task = asyncio.create_task(rec.close_decision_recorder(timeout=1))
    try:
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert r.submit(row("late")) == "dropped_closed"
    finally:
        release.set()
        assert r.close(1) is True
    assert saved(r) == ["a"]


@pytest.mark.asyncio
async def test_runner_failed_storage_is_reported_and_token_cleanup_continues(tmp_path, monkeypatch):
    from scripts.run_trader import UnifiedTradingBot
    from src.utils import llm
    from loguru import logger
    r = rec.DecisionRecorder(tmp_path, enabled=True, max_file_bytes=1)
    monkeypatch.setattr(rec, "_recorder", r)
    async def close_llm():
        r.submit(row("failed"))
    cleaned = []
    async def close_token():
        cleaned.append(True)
    monkeypatch.setattr(llm, "close_llm_manager", close_llm)
    bot = object.__new__(UnifiedTradingBot)
    bot.broker = bot._us_engine = bot.dashboard = bot.ws_feed = None
    bot._entry_observation_runtime = None
    bot._token_manager = SimpleNamespace(close=close_token)
    messages = []
    sink = logger.add(lambda msg: messages.append(msg.record), level="WARNING")
    try:
        await bot.shutdown()
        assert cleaned == [True]
        assert r.status_of("failed") == "dropped_file_cap"
        assert any("종료 저장 미완료" in message["message"] for message in messages)
    finally:
        logger.remove(sink)
        r.close(1)


def test_idle_worker_timeout_racing_last_submit_does_not_drop_accepted_record(tmp_path, monkeypatch):
    import queue
    r = rec.DecisionRecorder(tmp_path, enabled=True)
    timed_out, release = threading.Event(), threading.Event()
    get = r._queue.get
    calls = 0
    def interrupted_get(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            timed_out.set()
            release.wait(2)
            raise queue.Empty
        return get(*args, **kwargs)
    monkeypatch.setattr(r._queue, "get", interrupted_get)
    r.submit(row("first"))
    assert timed_out.wait(1)
    assert r.submit(row("last")) == "queued"
    try:
        assert r.close(0) is False
    finally:
        release.set()
        drained = r.close(1)
    assert drained is True
    assert saved(r) == ["first", "last"]


@pytest.mark.asyncio
@pytest.mark.parametrize("signal_name", ["SIGTERM", "SIGINT"])
async def test_registered_signals_during_shutdown_do_not_cancel_drain(tmp_path, monkeypatch, signal_name):
    import signal
    from scripts.run_trader import UnifiedTradingBot
    from src.utils import llm

    handlers = {}
    monkeypatch.setattr(signal, "signal", lambda sig, handler: handlers.__setitem__(sig, handler))
    recorder = rec.DecisionRecorder(tmp_path, enabled=True)
    monkeypatch.setattr(rec, "_recorder", recorder)
    entered, release = asyncio.Event(), asyncio.Event()
    async def close_llm():
        entered.set()
        await release.wait()
        assert recorder.submit(row("final")) == "queued"
    monkeypatch.setattr(llm, "close_llm_manager", close_llm)
    bot = object.__new__(UnifiedTradingBot)
    bot.broker = bot._us_engine = bot.dashboard = bot.ws_feed = bot._token_manager = None
    bot._entry_observation_runtime = None
    bot._stop_event = asyncio.Event()
    bot._loop = asyncio.get_running_loop()
    bot.engine = SimpleNamespace(stop=lambda: None)
    bot._setup_signal_handlers()
    task = asyncio.create_task(bot.shutdown())
    try:
        await entered.wait()
        handler = handlers[getattr(signal, signal_name)]
        handler(getattr(signal, signal_name), None)
        handler(getattr(signal, signal_name), None)
        await asyncio.sleep(0)
        assert bot._stop_event.is_set() and not task.done()
        assert task.cancelling() == 0
        release.set()
        await task
        assert saved(recorder) == ["final"]
        assert recorder.close(0) is True
    finally:
        release.set()
        await task
        recorder.close(1)
