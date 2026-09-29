"""09-29 감사에서 확인된 기존 결함 수정 (A 묶음).

- A1 싱글톤 락: 쥔 쪽이 있으면 아무것도 죽이지 않고 False, 락 파일의 PID 도 지우지 않는다
- A2 ExitManager pending 검증자: 거래소 미체결을 종목 지정으로 조회한다 (첫 페이지 한계 → 판단 불가)
- A5 신호 기록: 조정 점수 0 이 원점수로 덮이지 않는다
"""
from __future__ import annotations

import ast
import asyncio
import fcntl
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def lock_env(tmp_path, monkeypatch):
    from scripts import run_trader
    lock, pid = tmp_path / "unified_trader.lock", tmp_path / "unified_trader.pid"
    monkeypatch.setattr(run_trader, "LOCK_FILE", lock)
    monkeypatch.setattr(run_trader, "PID_FILE", pid)
    monkeypatch.setattr(run_trader, "_lock_fd", None)
    kills = []
    monkeypatch.setattr(run_trader.os, "kill", lambda *a: kills.append(a))
    yield run_trader, lock, pid, kills
    if run_trader._lock_fd:
        run_trader._lock_fd.close()


def test_a1_held_lock_refuses_without_killing_or_truncating(lock_env):
    run_trader, lock, pid, kills = lock_env
    pid.write_text("424242")                       # 운영 봇 PID
    with open(lock, "a") as bot:                   # 운영 봇이 락을 쥔 상태
        fcntl.flock(bot.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        bot.write("424242")
        bot.flush()
        assert run_trader.acquire_singleton_lock() is False
    assert kills == []
    assert lock.read_text() == "424242" and pid.read_text() == "424242"
    assert run_trader._lock_fd is None


def test_a1_free_lock_is_taken_without_killing_the_stale_pid(lock_env):
    run_trader, lock, pid, kills = lock_env
    pid.write_text("424242")                       # 크래시 뒤 남은(재사용됐을 수 있는) PID
    lock.write_text("424242")
    assert run_trader.acquire_singleton_lock() is True
    assert kills == []
    assert lock.read_text() == str(os.getpid()) and pid.read_text() == str(os.getpid())


def test_a2_pending_verifier_queries_exchange_orders_by_symbol():
    tree = ast.parse((ROOT / "scripts" / "run_trader.py").read_text(encoding="utf-8"))
    fns = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_sell_outstanding"]
    assert len(fns) == 1
    calls = [c for c in ast.walk(fns[0]) if isinstance(c, ast.Call)
             and isinstance(c.func, ast.Attribute) and c.func.attr == "get_exchange_open_orders"]
    assert [ast.unparse(c) for c in calls] == ["_b.get_exchange_open_orders(symbol=_sym)"]


@pytest.mark.parametrize("adjusted, expected", [(0.0, 0.0), (None, 70.0), (55.0, 55.0)])
def test_a5_zero_adjusted_score_is_kept(adjusted, expected):
    from src.data.storage.signal_event_storage import SignalEventStorage
    st = SignalEventStorage.__new__(SignalEventStorage)
    written = []

    async def _write(**kwargs):
        written.append(kwargs)
    st._write = _write

    async def run():
        await st.log(symbol="005930", score=70.0, adjusted_score=adjusted, event_type="passed")
        await asyncio.sleep(0)
    asyncio.run(run())
    assert written[0]["adjusted_score"] == expected
