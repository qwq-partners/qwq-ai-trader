"""임시 원장의 writer 경합은 전송 전에 보수적으로 차단한다."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3
from threading import Event

import pytest

from src.analytics.model_comparison_execution import ComparisonCallGuard
from src.utils.llm_budget import BudgetBlocked
from tests.test_model_comparison_execution import paid_once, request, setup_guard
from tests.test_model_comparison_policy_lifecycle import call, register, storage_failure_details


@pytest.mark.parametrize('winner_policy', ['old', 'new'])
def test_writer_contention_blocks_before_request_reservation_or_dispatch(tmp_path, monkeypatch, winner_policy):
    ledger, old, guard = setup_guard(tmp_path, cap=2)
    new = replace(old, experiment_id='new')
    register(ledger, new)
    other = ComparisonCallGuard(ledger=ledger, policy=new)
    call(guard, 'shared')
    winner, loser = (guard, other) if winner_policy == 'old' else (other, guard)
    locked, release = Event(), Event()
    original = winner._check_policy
    dispatches = []

    def pause_after_write_lock(db):
        assert db.in_transaction
        locked.set()
        assert release.wait(5), 'writer의 테스트 해제 신호 누락'
        return original(db)

    async def transport(req):
        dispatches.append(req.model)
        return await paid_once(req)

    def run(one, name):
        return asyncio.run(one.call(request(), dispatch=transport, attempt_id=name))

    # 시간 기반 sleep 대신 실제 BEGIN IMMEDIATE 직후 보유자를 멈춘다.
    # 원장의 기존 0.1초 잠금 대기는 바꾸지 않는다.
    monkeypatch.setattr(winner, '_check_policy', pause_after_write_lock)
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(run, winner, 'winner-2')
        try:
            assert locked.wait(5), '첫 호출이 writer 잠금을 획득하지 못함'
            with pytest.raises(BudgetBlocked) as blocked:
                run(loser, 'loser-2')
            details = storage_failure_details(blocked.value)
            cause = blocked.value.__context__
            assert blocked.value.reason == 'comparison_storage_unavailable', details
            assert isinstance(cause, sqlite3.OperationalError), details
            assert cause.sqlite_errorcode == sqlite3.SQLITE_BUSY, details
            assert details['phase'][0][0] == '_admit', details
            assert 'BEGIN IMMEDIATE' in details['phase'][0][2], details
            assert dispatches == []
            with ledger._connect(readonly=True) as db:
                assert db.execute('SELECT count(*) FROM comparison_requests').fetchone()[0] == 1
                assert db.execute('SELECT count(*) FROM comparison_attempts').fetchone()[0] == 1
                assert db.execute('SELECT count(*) FROM reservations').fetchone()[0] == 1
        finally:
            release.set()
        assert first.result().value == 'approve'
    assert dispatches == [request().model]
    assert loser.lookup('loser-2') is None
    assert len(guard.day_attempts('2026-10-13')) == 2
    with ledger._connect(readonly=True) as db:
        assert db.execute('SELECT count(*) FROM comparison_requests').fetchone()[0] == 2
        assert db.execute('SELECT count(*) FROM reservations').fetchone()[0] == 2
    assert ledger.status()['settled_count'] == 2
