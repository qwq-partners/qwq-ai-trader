"""실제 core 큐/SQLite와 합성 PostgreSQL pool 경계의 원장 전달 계약."""
import asyncio
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json

import pytest

from src.execution.safety.journal_delivery import (
    ExecutionEnvelope, SinkReceipt, PostgresExecutionJournal, OutboxDispatcher,
)
from src.execution.safety.runtime import KRExecutionRuntime
from src.execution.safety.store import ExecutionStateStore, StoreError
from test_execution_runtime import NOW, setup, opened, observed, queued


class Pool:
    """SQL 자체를 실행하지 않는 외부 DB 경계. transaction commit/rollback만 모사."""
    def __init__(self):
        self.rows = {}
        self.lock = asyncio.Lock()
        self.fail_before_commit = False
        self.lose_commit_ack = False
        self.gate = False
        self.gate_after_commit = False
        self.entered, self.resume = asyncio.Event(), asyncio.Event()
        self.insertions = 0
        self.commits = 0
        self.statements = []

    def acquire(self):
        return Connection(self)


class Connection:
    def __init__(self, pool):
        self.pool, self.pending = pool, None
        self.wrote = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        if self.wrote and self.pool.gate_after_commit:
            self.pool.entered.set()
            await self.pool.resume.wait()
        return False

    def transaction(self, **kwargs):
        connection = self
        class Transaction:
            async def __aenter__(self):
                await connection.pool.lock.acquire()
                connection.pending = deepcopy(connection.pool.rows)
                return self

            async def __aexit__(self, exc_type, *_):
                pool = connection.pool
                try:
                    if exc_type is None:
                        if pool.gate:
                            pool.entered.set()
                            await pool.resume.wait()
                        if pool.fail_before_commit:
                            pool.fail_before_commit = False
                            raise OSError("synthetic precommit")
                        pool.insertions += len(connection.pending) - len(pool.rows)
                        pool.rows = connection.pending
                        pool.commits += 1
                        if pool.lose_commit_ack:
                            pool.lose_commit_ack = False
                            raise OSError("synthetic committed response loss")
                finally:
                    connection.pending = None
                    pool.lock.release()
                return False
        return Transaction()

    async def execute(self, sql, *params):
        self.wrote = True
        self.pool.statements.append(sql)
        assert self.pending is not None
        assert "INSERT INTO execution_journal" in sql and "ON CONFLICT" in sql
        key, digest, schema, payload = params
        if key not in self.pending:
            self.pending[key] = {"execution_key": key, "payload_digest": digest,
                                 "sink_schema_version": schema, "payload": json.loads(payload)}
        return "INSERT 0 1"

    async def fetchrow(self, sql, key):
        self.pool.statements.append(sql)
        assert "FROM execution_journal" in sql and "execution_key = $1" in sql
        rows = self.pool.rows if self.pending is None else self.pending
        return deepcopy(rows.get(key))


async def filled(tmp_path, *, store_type=ExecutionStateStore, qty=40):
    engine, exits, store, runtime = await setup(tmp_path, store_type=store_type)
    ref = await opened(runtime, "B1")
    obs = await observed(runtime, ref, qty, str(qty * 10000))
    assert (await queued(engine, obs)).status == "APPLIED"
    return engine, exits, store, runtime, ref, obs


def immutable_domains(state):
    return {key: value for key, value in state.items() if key not in {"outbox", "cursors"}}


def test_envelope_is_immutable_detached_and_delivery_metadata_does_not_change_digest(tmp_path):
    async def scenario():
        _, _, store, runtime, _, obs = await filled(tmp_path)
        try:
            row = runtime.owner.state["outbox"][obs.observation_id]
            event = ExecutionEnvelope.from_outbox(obs.observation_id, row)
            original = event.payload_json
            row["portfolio"]["cash"] = "0"
            assert event.payload_json == original
            with pytest.raises(FrozenInstanceError):
                event.payload_json = "{}"
            row = runtime.owner.state["outbox"][obs.observation_id]
            row.update(status="delivered", delivery={"arbitrary": "metadata"})
            assert ExecutionEnvelope.from_outbox(obs.observation_id, row).payload_digest == event.payload_digest
            assert "portfolio" not in repr(event)
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["before", "lost_ack"])
def test_actual_queue_store_sink_failure_reopen_replay_is_once(tmp_path, failure):
    async def scenario():
        engine, exits, store, runtime, _, obs = await filled(tmp_path)
        pool = Pool()
        pool.fail_before_commit = failure == "before"
        pool.lose_commit_ack = failure == "lost_ack"
        sink = PostgresExecutionJournal(pool)
        before = runtime.owner.state
        try:
            outcome = await OutboxDispatcher(runtime.owner, sink).drain()
            assert outcome.delivered == 0
            assert runtime.owner.state["outbox"][obs.observation_id]["status"] == "pending"
            assert runtime.owner.state["cursors"][obs.order_key]["journal_pending"]
            assert immutable_domains(runtime.owner.state) == immutable_domains(before)
            assert len(pool.rows) == (1 if failure == "lost_ack" else 0)
        finally:
            await store.close()
        reopened = ExecutionStateStore(tmp_path / "execution" / "state.sqlite3")
        again = KRExecutionRuntime(reopened, engine, exits, clock=lambda: NOW)
        try:
            await again.restore()
            outcome = await OutboxDispatcher(again.owner, sink).drain()
            assert outcome.delivered == 1 and pool.insertions == 1
            assert len(pool.rows) == 1
            row = again.owner.state["outbox"][obs.observation_id]
            assert row["status"] == "delivered" and row["delivery"]["durable_status"] == "committed"
            assert not again.owner.state["cursors"][obs.order_key]["journal_pending"]
            assert immutable_domains(again.owner.state) == immutable_domains(before)
            assert not again.trading_ready
            assert (await OutboxDispatcher(again.owner, sink).drain()).delivered == 0
        finally:
            await reopened.close()
    asyncio.run(scenario())


def test_sink_commit_ack_lookup_and_same_key_conflict(tmp_path):
    async def scenario():
        _, _, store, runtime, _, obs = await filled(tmp_path)
        try:
            row = runtime.owner.state["outbox"][obs.observation_id]
            event = ExecutionEnvelope.from_outbox(obs.observation_id, row)
            pool, sink = Pool(), None
            sink = PostgresExecutionJournal(pool)
            ack = await sink.apply_once(event)
            assert type(ack) is SinkReceipt and pool.commits == 1
            assert await sink.lookup(event.execution_key) == ack
            assert await sink.apply_once(event) == ack and pool.insertions == 1
            row["realized_pnl"] = "123"
            conflict = ExecutionEnvelope.from_outbox(obs.observation_id, row)
            with pytest.raises(ValueError):
                await sink.apply_once(conflict)
            assert pool.rows[event.execution_key]["payload"]["realized_pnl"] == "0"
            assert pool.rows[event.execution_key]["payload"] == json.loads(event.payload_json)
        finally:
            await store.close()
    asyncio.run(scenario())


def test_two_dispatcher_instances_share_db_uniqueness(tmp_path):
    async def scenario():
        _, _, store, runtime, _, obs = await filled(tmp_path)
        try:
            pool = Pool()
            pool.gate = True
            sink = PostgresExecutionJournal(pool)
            first = asyncio.create_task(OutboxDispatcher(runtime.owner, sink).drain())
            await asyncio.wait_for(pool.entered.wait(), 1)
            second = asyncio.create_task(OutboxDispatcher(runtime.owner, sink).drain())
            await asyncio.sleep(0)
            pool.resume.set()
            outcomes = await asyncio.gather(first, second)
            assert sum(outcome.delivered for outcome in outcomes) == 1
            assert pool.insertions == 1 and len(pool.rows) == 1
            assert runtime.owner.state["outbox"][obs.observation_id]["status"] == "delivered"
        finally:
            await store.close()
    asyncio.run(scenario())


def test_old_ack_cannot_clear_new_fill_cursor_and_source_order_is_preserved(tmp_path):
    async def scenario():
        engine, _, store, runtime, ref, first_obs = await filled(tmp_path)
        try:
            pool = Pool()
            pool.gate = True
            drain = asyncio.create_task(OutboxDispatcher(runtime.owner, PostgresExecutionJournal(pool)).drain(limit=1))
            await asyncio.wait_for(pool.entered.wait(), 1)
            second_obs = await observed(runtime, ref, 100, "1000000")
            assert (await asyncio.wait_for(queued(engine, second_obs), 1)).status == "APPLIED"
            before = runtime.owner.state
            pool.resume.set()
            assert (await drain).delivered == 1
            assert runtime.owner.state["cursors"][first_obs.order_key]["journal_pending"]
            assert immutable_domains(runtime.owner.state) == immutable_domains(before)
            assert (await OutboxDispatcher(runtime.owner, PostgresExecutionJournal(pool)).drain()).delivered == 1
            assert list(pool.rows) == [first_obs.observation_id, second_obs.observation_id]
            assert not runtime.owner.state["cursors"][first_obs.order_key]["journal_pending"]
        finally:
            await store.close()
    asyncio.run(scenario())


class AckFailStore(ExecutionStateStore):
    fail_ack = False

    async def commit(self, expected_version, state, commit_id):
        if self.fail_ack and commit_id.startswith("command:journal-ack:"):
            self.fail_ack = False
            raise StoreError("synthetic owner ACK failure")
        return await super().commit(expected_version, state, commit_id)


def test_sink_success_owner_ack_failure_keeps_pending_until_restore(tmp_path):
    async def scenario():
        _, _, store, runtime, _, obs = await filled(tmp_path, store_type=AckFailStore)
        try:
            pool = Pool()
            sink = PostgresExecutionJournal(pool)
            store.fail_ack = True
            outcome = await OutboxDispatcher(runtime.owner, sink).drain()
            assert outcome.delivered == 0 and pool.insertions == 1
            assert not runtime.owner.healthy
            assert (await store.load())[1]["outbox"][obs.observation_id]["status"] == "pending"
            await runtime.restore()
            assert (await OutboxDispatcher(runtime.owner, sink).drain()).delivered == 1
            assert pool.insertions == 1
        finally:
            await store.close()
    asyncio.run(scenario())


def test_cancellation_during_external_commit_preserves_pending_and_propagates(tmp_path):
    async def scenario():
        _, _, store, runtime, _, obs = await filled(tmp_path)
        try:
            pool = Pool()
            pool.gate = True
            task = asyncio.create_task(OutboxDispatcher(runtime.owner, PostgresExecutionJournal(pool)).drain())
            await asyncio.wait_for(pool.entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert runtime.owner.state["outbox"][obs.observation_id]["status"] == "pending"
            pool.gate = False
            assert (await OutboxDispatcher(runtime.owner, PostgresExecutionJournal(pool)).drain()).delivered == 1
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("bad_receipt", [True, None, {"queued": True}])
def test_non_durable_receipt_is_not_an_ack(tmp_path, bad_receipt):
    class BadSink:
        async def lookup(self, key): return None
        async def apply_once(self, event): return bad_receipt
    async def scenario():
        _, _, store, runtime, _, obs = await filled(tmp_path)
        try:
            outcome = await OutboxDispatcher(runtime.owner, BadSink()).drain()
            assert outcome.delivered == 0
            assert runtime.owner.state["outbox"][obs.observation_id]["status"] == "pending"
            assert runtime.owner.state["cursors"][obs.order_key]["journal_pending"]
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("version", [None, True, 0, "1"])
def test_legacy_or_invalid_source_order_stays_pending(tmp_path, version):
    async def scenario():
        _, _, store, runtime, _, obs = await filled(tmp_path)
        try:
            def corrupt(state):
                row = state["outbox"][obs.observation_id]
                if version is None:
                    row.pop("source_version")
                else:
                    row["source_version"] = version
                return state
            await runtime.owner.mutate("synthetic-source-version", corrupt)
            pool = Pool()
            outcome = await OutboxDispatcher(runtime.owner, PostgresExecutionJournal(pool)).drain()
            assert outcome.delivered == 0 and not pool.rows
            assert runtime.owner.state["outbox"][obs.observation_id]["status"] == "pending"
        finally:
            await store.close()
    asyncio.run(scenario())


def test_trade_storage_factory_requires_existing_db_pool_without_json_fallback():
    from src.data.storage.trade_storage import TradeStorage
    storage = TradeStorage.__new__(TradeStorage)
    storage.pool, storage._db_available = None, False
    with pytest.raises(RuntimeError):
        storage.execution_journal()
    storage.pool, storage._db_available = Pool(), True
    assert isinstance(storage.execution_journal(), PostgresExecutionJournal)


@pytest.mark.parametrize("corruption", ["duplicate_version", "payload_race", "cursor_ahead", "initial_r"])
def test_invalid_ordering_payload_race_and_unacknowledged_cursor_fail_closed(tmp_path, corruption):
    async def scenario():
        engine, _, store, runtime, ref, obs = await filled(tmp_path)
        pool = Pool()
        drain = None
        try:
            if corruption == "duplicate_version":
                second = await observed(runtime, ref, 100, "1000000")
                await queued(engine, second)
            if corruption == "payload_race":
                pool.gate = True
                drain = asyncio.create_task(OutboxDispatcher(runtime.owner, PostgresExecutionJournal(pool)).drain())
                await asyncio.wait_for(pool.entered.wait(), 1)
            def change(state):
                if corruption == "duplicate_version":
                    state["outbox"][second.observation_id]["source_version"] = state["outbox"][obs.observation_id]["source_version"]
                elif corruption == "payload_race":
                    state["outbox"][obs.observation_id]["realized_pnl"] = "99"
                elif corruption == "cursor_ahead":
                    state["cursors"][obs.order_key]["quantity"] = 100
                else:
                    state["outbox"]["initial-r"] = {"kind": "initial_r", "status": "pending"}
                return state
            await runtime.owner.mutate("synthetic-corruption", change)
            if drain is not None:
                pool.resume.set()
                outcome = await drain
            else:
                outcome = await OutboxDispatcher(runtime.owner, PostgresExecutionJournal(pool)).drain()
            assert runtime.owner.state["cursors"][obs.order_key]["journal_pending"]
            if corruption != "cursor_ahead":
                assert outcome.delivered == 0
                assert runtime.owner.state["outbox"][obs.observation_id]["status"] == "pending"
            assert len(pool.rows) == (1 if corruption in {"payload_race", "cursor_ahead"} else 0)
        finally:
            pool.resume.set()
            if drain is not None:
                await drain
            await store.close()
    asyncio.run(scenario())


class AckGatedStore(ExecutionStateStore):
    def __init__(self, path):
        super().__init__(path)
        self.armed = False
        self.after_commit = False
        self.entered, self.resume = asyncio.Event(), asyncio.Event()

    async def commit(self, expected_version, state, commit_id):
        if self.armed and commit_id.startswith("command:journal-ack:"):
            self.armed = False
            if self.after_commit:
                await super().commit(expected_version, state, commit_id)
            self.entered.set()
            await self.resume.wait()
        return await super().commit(expected_version, state, commit_id)


@pytest.mark.parametrize("after_commit", [False, True])
def test_cancel_during_owner_ack_requires_restore_and_never_reapplies_economics(tmp_path, after_commit):
    async def scenario():
        _, _, store, runtime, _, obs = await filled(tmp_path, store_type=AckGatedStore)
        try:
            before = immutable_domains(runtime.owner.state)
            pool = Pool()
            sink = PostgresExecutionJournal(pool)
            store.armed, store.after_commit = True, after_commit
            task = asyncio.create_task(OutboxDispatcher(runtime.owner, sink).drain())
            await asyncio.wait_for(store.entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert pool.insertions == 1 and not runtime.owner.healthy
            await runtime.restore()
            await OutboxDispatcher(runtime.owner, sink).drain()
            assert immutable_domains(runtime.owner.state) == before
            assert runtime.owner.state["outbox"][obs.observation_id]["status"] == "delivered"
            assert not runtime.owner.state["cursors"][obs.order_key]["journal_pending"]
            assert pool.insertions == 1
        finally:
            await store.close()
    asyncio.run(scenario())


def test_batch_uses_source_version_and_excludes_protection_decisions(tmp_path):
    async def scenario():
        engine, _, store, runtime, ref, first = await filled(tmp_path)
        try:
            second = await observed(runtime, ref, 100, "1000000")
            await queued(engine, second)
            def protection_event(state):
                state["outbox"]["protection-proposal"] = {"kind": "protection_decision", "status": "pending"}
                return state
            await runtime.owner.mutate("synthetic-protection-proposal", protection_event)
            await runtime.restore()  # checkpoint의 key 정렬 순서는 경제 순서가 아니다.
            pool = Pool()
            outcome = await OutboxDispatcher(runtime.owner, PostgresExecutionJournal(pool)).drain()
            assert outcome.delivered == 2 and list(pool.rows) == [first.observation_id, second.observation_id]
            assert runtime.owner.state["outbox"]["protection-proposal"]["status"] == "pending"
        finally:
            await store.close()
    asyncio.run(scenario())


def test_cancellation_after_external_commit_recovers_by_lookup(tmp_path):
    async def scenario():
        _, _, store, runtime, _, obs = await filled(tmp_path)
        try:
            pool = Pool()
            pool.gate_after_commit = True
            sink = PostgresExecutionJournal(pool)
            task = asyncio.create_task(OutboxDispatcher(runtime.owner, sink).drain())
            await asyncio.wait_for(pool.entered.wait(), 1)
            assert pool.insertions == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert runtime.owner.state["outbox"][obs.observation_id]["status"] == "pending"
            pool.gate_after_commit = False
            assert (await OutboxDispatcher(runtime.owner, sink).drain()).delivered == 1
            assert pool.insertions == 1
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["execution_key", "payload_digest"])
def test_wrong_durable_receipt_identity_preserves_pending(tmp_path, field):
    class WrongSink:
        async def lookup(self, key): return None
        async def apply_once(self, event):
            values = {"execution_key": event.execution_key, "payload_digest": event.payload_digest}
            values[field] = "f" * 64
            return SinkReceipt(**values)
    async def scenario():
        _, _, store, runtime, _, obs = await filled(tmp_path)
        try:
            assert (await OutboxDispatcher(runtime.owner, WrongSink()).drain()).delivered == 0
            assert runtime.owner.state["cursors"][obs.order_key]["journal_pending"]
        finally:
            await store.close()
    asyncio.run(scenario())


def test_db_lookup_verifies_full_payload_and_jsonb_string_representation(tmp_path):
    async def scenario():
        _, _, store, runtime, _, obs = await filled(tmp_path)
        try:
            event = ExecutionEnvelope.from_outbox(obs.observation_id, runtime.owner.state["outbox"][obs.observation_id])
            pool = Pool()
            sink = PostgresExecutionJournal(pool)
            receipt = await sink.apply_once(event)
            pool.rows[event.execution_key]["payload"] = event.payload_json
            assert await sink.lookup(event.execution_key) == receipt
            body = json.loads(event.payload_json)
            body["realized_pnl"] = "123"
            pool.rows[event.execution_key]["payload"] = body
            with pytest.raises(ValueError):
                await sink.lookup(event.execution_key)
        finally:
            await store.close()
    asyncio.run(scenario())


def test_existing_storage_schema_initialization_sends_dedicated_journal_ddl():
    from src.data.storage.trade_storage import TradeStorage
    statements = []
    class DDLConnection:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def execute(self, sql): statements.append(sql)
    class DDLPool:
        def acquire(self): return DDLConnection()
    storage = TradeStorage.__new__(TradeStorage)
    storage.pool = DDLPool()
    asyncio.run(storage._ensure_tables())
    schema = statements[0]
    assert "CREATE TABLE IF NOT EXISTS execution_journal" in schema
    assert "execution_key       TEXT PRIMARY KEY" in schema
    assert "payload             JSONB NOT NULL" in schema
    assert "payload_digest      CHAR(64) NOT NULL" in schema
