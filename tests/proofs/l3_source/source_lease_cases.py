"""Warm-source ownership proofs; no production restore/performance claim.

The breaks these tests catch are premature gate release, mixed SQL snapshots,
lost source ownership, skipped receipt rows, and uncharged disposal edges.
"""
# guard 결속과 직접 child 거부 전에는 stdlib만 import한다.
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType


_ROOT = Path(__file__).resolve().parents[3]
_GUARD_PATH = _ROOT / "tests/conftest.py"
_GUARD_SHA256 = "7b7b26940309a2a165d9bdba7611b6730e83b90ae9ea5f9e725764ee4c0a23f7"
_HELPER_PATH = _ROOT / "tests/proofs/l3_source/l3_source_lease_probe.py"


def _exact_modules(path, basename, error):
    """별칭 대신 실제 객체 수를 세고 module __getattr__는 호출하지 않는다."""
    found = {}
    for name, module in tuple(sys.modules.items()):
        named = name.rsplit(".", 1)[-1] == basename
        if not issubclass(type(module), ModuleType):
            if named:
                raise RuntimeError(error)
            continue
        namespace = ModuleType.__getattribute__(module, "__dict__")
        filename = namespace.get("__file__")
        if type(filename) is not str:
            if named:
                raise RuntimeError(error)
            continue
        # 무관한 파일명은 제외하며 임의 속성 getter를 호출하지 않는다.
        if not named and Path(filename).name != path.name:
            continue
        if Path(filename).resolve() != path:
            raise RuntimeError(error)
        found[id(module)] = module
    if len(found) > 1:
        raise RuntimeError(error)
    return tuple(found.values())


def _load_exact_file(name, path, source):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        # 재읽기나 pyc 없이 방금 검증한 bytes를 실행한다.
        exec(compile(source, str(path), "exec"), module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _bind_exact_guard() -> dict:
    """고정 guard의 설치 상태를 관측한다. 환경변수 자격 우회는 없다."""
    error = "source_proof_guard_invalid"
    try:
        source = _GUARD_PATH.read_bytes()
        digest = hashlib.sha256(source).hexdigest()
        if digest != _GUARD_SHA256:
            raise RuntimeError(error)
        modules = _exact_modules(_GUARD_PATH, "conftest", error)
        if not modules and __name__ == "__main__":
            _load_exact_file("conftest", _GUARD_PATH, source)
            modules = _exact_modules(_GUARD_PATH, "conftest", error)
        if len(modules) != 1:
            raise RuntimeError(error)
        namespace = ModuleType.__getattribute__(modules[0], "__dict__")
        violations = namespace.get("VIOLATIONS")
        if type(violations) is not list or violations:
            raise RuntimeError(error)
        return {"path": _GUARD_PATH.relative_to(_ROOT).as_posix(), "sha256": digest,
                "module_count": len(modules), "violations": len(violations)}
    except Exception:
        raise RuntimeError(error) from None


def _direct_child():
    arguments = sys.argv[1:]
    if arguments == ["--source-guard-child"]:
        print(json.dumps({"schema": "qwq.source-guard/v1", "guard": _bind_exact_guard()},
                         sort_keys=True), flush=True)
        return
    if len(arguments) == 4 and arguments[0] == "--source-fault-child":
        raise RuntimeError("source_proof_native_subject_unqualified")
    raise RuntimeError("source_proof_invalid_arguments")


if __name__ == "__main__":
    try:
        _bind_exact_guard()
        _direct_child()
    except Exception as error:
        # traceback·입력 경로·환경 내용을 child 출력에 싣지 않는다.
        if type(error) is RuntimeError and error.args in (
                ("source_proof_guard_invalid",),
                ("source_proof_invalid_arguments",),
                ("source_proof_native_subject_unqualified",)):
            sys.exit(error.args[0])
        sys.exit("source_proof_guard_invalid")
    sys.exit(0)


_bind_exact_guard()


def setup_module():
    """function fixture 전에 거부한다. 자격이 확인된 native runtime은 아직 없다."""
    raise RuntimeError("source_proof_native_subject_unqualified")


def _bind_exact_helper():
    error = "source_proof_helper_invalid"
    try:
        modules = _exact_modules(_HELPER_PATH, "l3_source_lease_probe", error)
        if not modules:
            _load_exact_file("l3_source_lease_probe", _HELPER_PATH, _HELPER_PATH.read_bytes())
            modules = _exact_modules(_HELPER_PATH, "l3_source_lease_probe", error)
        if len(modules) != 1:
            raise RuntimeError(error)
        return modules[0]
    except Exception:
        raise RuntimeError(error) from None


import asyncio
from contextlib import contextmanager
import dis
from dataclasses import replace
import os
import selectors
import sqlite3
import subprocess
import threading
import time
import weakref

import pytest

from src.execution.safety.store import ExecutionStateStore
from src.execution.safety.owner_ticket_gate import OwnerTicketGate, OwnerTicketKind
from src.execution.safety.recovery_projection import OwnerToken
probe = _bind_exact_helper()
ProbeSupervisor = probe.ProbeSupervisor
SourceProbeControls = probe.SourceProbeControls
start_source_probe = probe.start_source_probe
inspect_source = probe.inspect_source


@contextmanager
def verified_connections():
    """실제 변경 전 _open→connect 인수와 반환 identity를 관측하는 fixture.

    detect_types readback을 꾸미지 않는다. 이 도메인은 private connection 직접
    대입·factory 동시변경이 없는 synthetic 단일 owner에만 조건부로 한정된다.
    """
    real_connect = sqlite3.connect
    real_open = ExecutionStateStore._open
    local = threading.local()

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        if getattr(local, "in_open", False):
            local.creation = (connection, args, kwargs)
        return connection

    def open_store(store):
        fresh = store._connection is None
        local.in_open = True
        local.creation = None
        try:
            result = real_open(store)
            if fresh:
                assert local.creation is not None
                connection, args, kwargs = local.creation
                assert connection is result and result is store._connection
                assert type(connection) is sqlite3.Connection
                detect_types = args[2] if len(args) > 2 else kwargs.get("detect_types", 0)
                assert type(detect_types) is int and detect_types == 0
                assert kwargs.get("factory", sqlite3.Connection) is sqlite3.Connection
                assert connection.row_factory is None and connection.text_factory is str
                store._l3_creation_observed = True
            assert getattr(store, "_l3_creation_observed", False)
            return result
        finally:
            local.creation = None  # 확인용 connection 강참조는 source 관측 전에 버린다.
            local.in_open = False

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        patch.setattr(ExecutionStateStore, "_open", open_store)
        yield


@pytest.fixture(autouse=True)
def native_connection_provenance():
    with verified_connections():
        yield


async def observed(predicate):
    """Scheduled observation, not a sleep-based ordering assumption."""
    loop = asyncio.get_running_loop()
    result = loop.create_future()
    deadline = loop.time() + 4

    def check():
        if result.done():
            return
        if predicate():
            result.set_result(None)
        elif loop.time() >= deadline:
            result.set_exception(AssertionError("observation_deadline"))
        else:
            loop.call_later(.001, check)

    loop.call_soon(check)
    await result


async def seed(store, count=1):
    await store.load()

    def insert():
        conn = store._connection
        conn.execute("BEGIN")
        conn.execute("UPDATE checkpoint SET version=?, state=? WHERE id=1",
                     (max(1, count), '{"unknown":[1,true,"한글"]}'))
        for i in range(count):
            conn.execute("INSERT INTO commits VALUES (?, ?, ?, ?)",
                         (f"policy-registration:r{i}", i, i + 1, "test"))
        conn.execute("COMMIT")

    await store._run(insert)


@pytest.mark.parametrize("count", [0, 1])
def test_page_head_move_counts_live_edges_separately_from_removed_work(tmp_path, count):
    """유한 primitive RED: next 전이와 terminal root 제거를 각각 대조한다."""
    async def scenario():
        store = ExecutionStateStore(tmp_path / "source.sqlite3")
        await seed(store, count)
        supervisor = ProbeSupervisor()
        slot = probe._SourceSlot(supervisor, store, OwnerToken("probe", 1, 1, 1),
                                 SourceProbeControls())
        supervisor.active = slot
        try:
            await store._run(probe._read_source, slot)
            slot.worker_settled = True
            slot.driver = asyncio.current_task()
            slot.status = "DISPOSING"
            for _ in range(16):
                if slot.source_cleaned:
                    break
                removed = slot.advance_disposal(4)
                assert 0 < removed <= 4
                assert slot.owned_edges == len(owned_edges(slot))
            assert slot.source_cleaned and slot.owned_edges == 0
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("count", [0, 1])
def test_sql_worker_terminal_retires_all_native_cursors(tmp_path, count):
    """기존 native 실패를 gate 대기 없이 실제 warm SQL로 재현한다."""
    async def scenario():
        store = ExecutionStateStore(tmp_path / "source.sqlite3")
        await seed(store, count)
        supervisor = ProbeSupervisor()
        slot = probe._SourceSlot(supervisor, store, OwnerToken("probe", 1, 1, 1),
                                 SourceProbeControls())
        supervisor.active = slot
        try:
            await store._run(probe._read_source, slot)
            assert slot.cursor is None, "native_cursor_survived_worker_terminal"
            assert all(witness() is None for witness in slot.native_witnesses if witness)
            assert slot.native_metadata_alias is None
            assert slot.native_retired
            # SQL native 자원만 종료하고 등록 source graph는 보존해야 한다.
            assert slot.encoded == '{"unknown":[1,true,"한글"]}'
            assert slot.head is not None
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("factory", ["row_factory", "text_factory"])
def test_wrong_native_factory_is_rejected_before_callback_or_sql(tmp_path, monkeypatch, factory):
    async def scenario():
        store = ExecutionStateStore(tmp_path / "source.sqlite3")
        await seed(store)
        calls = []

        def forbidden(*args):
            calls.append("callback")
            raise AssertionError("factory_must_not_run")

        submit = store._executor.submit
        submitted = []

        def submit_observer(*args, **kwargs):
            submitted.append(True)
            return submit(*args, **kwargs)

        monkeypatch.setattr(store._executor, "submit", submit_observer)
        setattr(store._connection, factory, forbidden)
        gate, supervisor = OwnerTicketGate(asyncio.Lock()), ProbeSupervisor()
        async with gate.hold(OwnerTicketKind.RESTORE) as ticket:
            with pytest.raises(ValueError, match="unsupported_native_source"):
                start_source_probe(supervisor, store, gate, ticket,
                    OwnerToken("probe", 1, 1, 1), SourceProbeControls())
            assert not calls and not submitted and supervisor.active is None
        setattr(store._connection, factory, None if factory == "row_factory" else str)
        await store.close()
    asyncio.run(scenario())


def assert_native_terminal(slot):
    assert slot.cursor is None
    assert not slot.native_cursors
    assert slot.native_metadata_alias is None
    assert all(witness() is None for witness in slot.native_witnesses)
    assert slot.native_retired


@pytest.mark.parametrize("mutation", ["retain_cursor", "retain_metadata"])
def test_native_retirement_oracle_kills_retained_resource_mutations(tmp_path, mutation):
    async def scenario():
        store = ExecutionStateStore(tmp_path / "source.sqlite3")
        await seed(store)
        supervisor = ProbeSupervisor()
        slot = probe._SourceSlot(supervisor, store, OwnerToken("probe", 1, 1, 1),
                                 SourceProbeControls(mutation=mutation))
        supervisor.active = slot
        try:
            await store._run(probe._read_source, slot)
            with pytest.raises(AssertionError):
                assert_native_terminal(slot)
            assert slot.disposal_fault is not None and not slot.cleanup_proof.done()
        finally:
            await store.close()
    asyncio.run(scenario())


def test_native_actual_audit_matches_independent_tp_traverse_observation(tmp_path, monkeypatch):
    actual = probe.gc.get_referents
    records = []

    def observe(value):
        refs = actual(value)
        if type(value) is sqlite3.Cursor:
            metadata = 0
            for ref in refs:
                if type(ref) is tuple:
                    assert len(ref) == 2
                    metadata += len(ref) + sum(len(item) for item in ref)
            records.append((id(value), len(refs), metadata))
        return refs

    monkeypatch.setattr(probe.gc, "get_referents", observe)

    async def scenario():
        store = ExecutionStateStore(tmp_path / "source.sqlite3")
        await seed(store, 1)
        supervisor = ProbeSupervisor()
        slot = probe._SourceSlot(supervisor, store, OwnerToken("probe", 1, 1, 1),
                                 SourceProbeControls())
        supervisor.active = slot
        try:
            await store._run(probe._read_source, slot)
            assert_native_terminal(slot)
            assert len(slot.native_witnesses) == 4
            assert len(records) == 8
            assert len({record[0] for record in records}) == 4
            assert sum(record[2] for record in records[1::2]) == 32
            for index, audit in enumerate(slot.native_audit):
                before, after = records[2 * index:2 * index + 2]
                assert before[0] == after[0]
                assert audit[:3] == (before[1], after[1], after[2])
            # 실제 direct outgoing shape를 상한과 구분한다. witness roots도 bounded다.
            assert sum(record[1] + record[2] for record in records[1::2]) < 4 * 26
        finally:
            await store.close()
    asyncio.run(scenario())


def owned_edges(slot):
    """Independent graph walk: IDs only survive this observer's return.

    Never uses slot.owned_edges/accounting helpers. Page roots, linked registry,
    SQL list slots, native tuple slots and encoded checkpoint cells are counted.
    """
    edges = set()
    for name in ("revision", "encoded", "head", "tail", "cursor"):
        value = getattr(slot, name)
        if value is not None:
            edges.add((id(slot), name, id(value)))
    page = slot.head
    while page is not None:
        if page.next is not None:
            edges.add((id(page), "next", id(page.next)))
        if page.rows is not None:
            edges.add((id(page), "rows", id(page.rows)))
            for row in page.rows:
                edges.add((id(page.rows), id(row), id(row)))
                for i, value in enumerate(row):
                    edges.add((id(row), i, id(value)))
        page = page.next
    return edges


def test_source_receipts_and_cancel_cleanup(tmp_path):
    async def scenario():
        store = ExecutionStateStore(tmp_path / "source.sqlite3")
        await store.commit(0, {"unknown": [1, True, "한글"]}, "policy-registration:r1")
        gate, supervisor = OwnerTicketGate(asyncio.Lock()), ProbeSupervisor()
        try:
            async with gate.hold(OwnerTicketKind.RESTORE) as ticket:
                handle = start_source_probe(supervisor, store, gate, ticket,
                    OwnerToken("probe", 1, 1, 1), SourceProbeControls())
                await handle.wait_source()
                version, text, receipts = inspect_source(handle)
                assert version == 1 and receipts == (("r1", 1),)
                assert '"unknown"' in text and "한글" in text
                del text, receipts
                handle.validate_source(store, OwnerToken("probe", 1, 1, 1))
                handle.validate_source(store, OwnerToken("probe", 1, 1, 1))
                slot = supervisor.active
                handle.request_cancel()
                await handle.wait_closed()
                assert handle.status == "CLEANED"
                assert not owned_edges(slot)
                assert slot.worker.result() is None
                handle.request_cancel()
                with pytest.raises(ValueError, match="source_unavailable"):
                    handle.validate_source(store, OwnerToken("probe", 1, 1, 1))
            assert supervisor.active is None
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["before_driver", "after_checkpoint", "page", "done_uncollected"])
def test_repeated_caller_cancellation_keeps_sql_slot_and_gate(tmp_path, phase):
    async def scenario():
        store = ExecutionStateStore(tmp_path / "source.sqlite3")
        await seed(store, 65)
        gate, supervisor = OwnerTicketGate(asyncio.Lock()), ProbeSupervisor()
        controls = SourceProbeControls(pause_at=phase)
        started, entered = asyncio.Event(), asyncio.Event()
        handles = []

        async def owner():
            async with gate.hold(OwnerTicketKind.RESTORE) as ticket:
                handle = start_source_probe(supervisor, store, gate, ticket,
                    OwnerToken("probe", 1, 1, 65), controls)
                handles.append(handle)
                started.set()
                await handle.wait_source()
                raise AssertionError("paused source unexpectedly ready")

        async def follower():
            async with gate.hold(OwnerTicketKind.PRODUCER_VIEW):
                assert supervisor.active is None
                entered.set()

        task = asyncio.create_task(owner())
        await started.wait()
        await observed(controls.entered.is_set)
        slot = supervisor.active
        task.cancel()
        next_task = asyncio.create_task(follower())
        await observed(lambda: len(gate._queue) == 2)
        task.cancel()
        assert not entered.is_set() and gate.lock.locked()
        assert supervisor.active is slot and not slot.worker.cancelled()
        controls.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await next_task
        assert not owned_edges(slot) and slot.worker.result() is None
        assert handles[0].status == "CLEANED"
        await store.close()
    asyncio.run(scenario())


def disposal_observer(original, observations, failures=None):
    deleted = {instruction.offset for instruction in dis.get_instructions(original)
               if instruction.opname == "DELETE_FAST" and instruction.argval in ("row", "page")}
    moved_head = {instruction.offset for instruction in dis.get_instructions(original)
                  if instruction.opname == "STORE_ATTR" and instruction.argval == "head"}

    def measured(slot, max_edges):
        before = owned_edges(slot)
        scratch_releases = 0
        head_releases = 0

        def trace(frame, event, arg):
            nonlocal scratch_releases, head_releases
            if frame.f_code is original.__code__:
                frame.f_trace_opcodes = True
                if event == "opcode" and frame.f_lasti in deleted:
                    scratch_releases += 1
                if (event == "opcode" and frame.f_lasti in moved_head
                        and slot.head is not None):
                    head_releases += 1
            return trace

        # Opcode observation never reads f_locals or retains a frame/graph.
        previous_trace = sys.gettrace()
        previous_opcode_flag = sys._getframe().f_trace_opcodes
        # Python3.12는 settrace 시점의 활성 frame flag로 INSTRUCTION을 켠다.
        sys._getframe().f_trace_opcodes = True
        sys.settrace(trace)
        try:
            removed = original(slot, max_edges)
        finally:
            sys._getframe().f_trace_opcodes = previous_opcode_flag
            sys.settrace(previous_trace)
        after = owned_edges(slot)
        delta = before - after
        initial_head_releases = sum(edge[0] == id(slot) and edge[1] == "head"
                                    for edge in delta)
        actual = len(delta) + scratch_releases + head_releases - initial_head_releases
        if failures is None:
            assert removed == actual, (removed, actual, scratch_releases, head_releases)
            assert 0 < removed <= max_edges
            assert slot.owned_edges == len(after)
        elif (removed != actual or not 0 < removed <= max_edges
              or slot.owned_edges != len(after)) and not failures:
            # proof를 건드리지 않고 첫 실패의 scalar만 회수한다.
            failures.append((removed, actual, max_edges, slot.owned_edges, len(after)))
        observations.append(removed)
        return removed

    return measured


def test_disposal_oracle_counts_transient_head_edges_in_one_step(tmp_path):
    """실제 source의 1행+빈 page를 한 step에 정리하는 유한 oracle RED."""
    async def scenario():
        store = ExecutionStateStore(tmp_path / "source.sqlite3")
        await seed(store, 1)
        supervisor = ProbeSupervisor()
        slot = probe._SourceSlot(supervisor, store, OwnerToken("probe", 1, 1, 1),
                                 SourceProbeControls())
        supervisor.active = slot
        try:
            await store._run(probe._read_source, slot)
            slot.worker_settled = True
            slot.driver = asyncio.current_task()
            slot.status = "DISPOSING"
            observe = disposal_observer(probe._SourceSlot.advance_disposal, [])
            assert observe(slot, 31) == 14  # native cursor는 worker phase에서 이미 종료됐다.
            assert slot.source_cleaned and slot.owned_edges == 0
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("count", [0, 1, 64, 65, 4097])
@pytest.mark.parametrize("budget", [4, 5, 31, 32, 255, 256])
def test_every_receipt_and_actual_disposal_edge_is_accounted(tmp_path, monkeypatch, count, budget):
    # Losing a row or charging an entire page as one edge breaks this oracle.
    monkeypatch.setattr(probe, "_DISPOSAL_BUDGET", budget)
    observations = []
    failures = []
    measured = disposal_observer(probe._SourceSlot.advance_disposal, observations, failures)
    monkeypatch.setattr(probe._SourceSlot, "advance_disposal", measured)

    async def scenario():
        store = ExecutionStateStore(tmp_path / "source.sqlite3")
        await seed(store, count)
        gate, supervisor = OwnerTicketGate(asyncio.Lock()), ProbeSupervisor()
        async with gate.hold(OwnerTicketKind.RESTORE) as ticket:
            handle = start_source_probe(supervisor, store, gate, ticket,
                OwnerToken("probe", 1, 1, max(1, count)), SourceProbeControls())
            await handle.wait_source()
            result = inspect_source(handle)
            assert result[0] == max(1, count)
            assert sorted(result[2]) == sorted((f"r{i}", i + 1) for i in range(count))
            del result
            slot = supervisor.active
            refs, sizes = [], []
            page = slot.head
            while page is not None:
                refs.append(weakref.ref(page))
                sizes.append(len(page.rows))
                # list + 이 generator의 local + getrefcount 인수만 존재한다.
                assert all(sys.getrefcount(row) == 3 for row in page.rows)
                page = page.next
            del page
            assert sizes[-1] == 0 and max(sizes) <= 64
            assert sum(sizes) == count
            assert len(owned_edges(slot)) == slot.owned_edges
            handle.request_cancel()
            await handle.wait_closed()
            assert not owned_edges(slot) and slot.owned_edges == 0
            assert all(ref() is None for ref in refs)
            assert observations
            assert not failures
        await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("bad", [True, False, 0, 1, 2, 3, 257, 4.0])
def test_invalid_disposal_budget_has_no_side_effect(tmp_path, bad):
    async def scenario():
        store = ExecutionStateStore(tmp_path / "source.sqlite3")
        await seed(store)
        gate, supervisor = OwnerTicketGate(asyncio.Lock()), ProbeSupervisor()
        async with gate.hold(OwnerTicketKind.RESTORE) as ticket:
            handle = start_source_probe(supervisor, store, gate, ticket,
                OwnerToken("probe", 1, 1, 1), SourceProbeControls())
            await handle.wait_source()
            before = owned_edges(supervisor.active)
            with pytest.raises(ValueError, match="invalid_disposal_budget"):
                handle.advance_disposal(bad)
            assert handle.status == "SOURCE_READY"
            assert owned_edges(supervisor.active) == before
            handle.request_cancel()
            await handle.wait_closed()
        await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("axis", ["incarnation", "publication_epoch", "fault_epoch", "revision", "store", "connection"])
def test_stale_source_never_validates_and_cleans(tmp_path, axis):
    async def scenario():
        store = ExecutionStateStore(tmp_path / "source.sqlite3")
        other = ExecutionStateStore(tmp_path / "other.sqlite3")
        await seed(store)
        token = OwnerToken("probe", 1, 1, 1)
        gate, supervisor = OwnerTicketGate(asyncio.Lock()), ProbeSupervisor()
        async with gate.hold(OwnerTicketKind.RESTORE) as ticket:
            handle = start_source_probe(supervisor, store, gate, ticket, token, SourceProbeControls())
            await handle.wait_source()
            target, changed = store, token
            if axis == "store":
                target = other
            elif axis == "connection":
                await store._run(store._close)
                await store.load()
            else:
                changed = replace(token, **{axis: "other" if axis == "incarnation" else 2})
            with pytest.raises(ValueError, match="stale_source"):
                handle.validate_source(target, changed)
            await handle.wait_closed()
            assert handle.status == "CLEANED"
        await store.close()
        await other.close()
    asyncio.run(scenario())


def test_cold_wrong_ticket_and_second_start_submit_nothing(tmp_path):
    async def scenario():
        store = ExecutionStateStore(tmp_path / "source.sqlite3")
        gate, supervisor = OwnerTicketGate(asyncio.Lock()), ProbeSupervisor()
        token = OwnerToken("probe", 1, 1, 1)
        async with gate.hold(OwnerTicketKind.RESTORE) as ticket:
            with pytest.raises(ValueError, match="cold_source_unsupported"):
                start_source_probe(supervisor, store, gate, ticket, token, SourceProbeControls())
            assert supervisor.active is None and not store.path.exists()
            await seed(store)
            with pytest.raises(ValueError, match="inactive_ticket"):
                start_source_probe(supervisor, store, gate, object(), token, SourceProbeControls())
            assert supervisor.active is None
            handle = start_source_probe(supervisor, store, gate, ticket, token, SourceProbeControls())
            slot = supervisor.active
            with pytest.raises(ValueError, match="source_already_active"):
                start_source_probe(supervisor, store, gate, ticket, token, SourceProbeControls())
            assert supervisor.active is slot
            handle.request_cancel()
            await handle.wait_closed()
        await store.close()
    asyncio.run(scenario())


def test_wal_writer_between_selects_cannot_mix_snapshot(tmp_path):
    async def scenario():
        store = ExecutionStateStore(tmp_path / "source.sqlite3")
        await seed(store)
        controls = SourceProbeControls(pause_at="after_checkpoint")
        gate, supervisor = OwnerTicketGate(asyncio.Lock()), ProbeSupervisor()
        async with gate.hold(OwnerTicketKind.RESTORE) as ticket:
            handle = start_source_probe(supervisor, store, gate, ticket,
                OwnerToken("probe", 1, 1, 1), controls)
            await observed(controls.entered.is_set)
            with sqlite3.connect(store.path, isolation_level=None) as other:
                other.execute("BEGIN IMMEDIATE")
                other.execute("UPDATE checkpoint SET version=2,state='{}'")
                other.execute("DELETE FROM commits")
                other.execute("INSERT INTO commits VALUES ('policy-registration:new',1,2,'test')")
                other.execute("COMMIT")
            controls.release.set()
            await handle.wait_source()
            old = inspect_source(handle)
            assert old == (1, '{"unknown":[1,true,"한글"]}', (("r0", 1),))
            del old
            handle.request_cancel()
            await handle.wait_closed()
        assert await store.load_with_policy_receipts() == (2, {}, {"new": 2})
        await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("fault, code", [("known_sql", "known_sql"), ("submit", "submit_failed")])
def test_known_failure_preserves_cause_but_completes_real_cleanup(tmp_path, fault, code):
    async def scenario():
        store = ExecutionStateStore(tmp_path / "source.sqlite3")
        await seed(store)
        gate, supervisor = OwnerTicketGate(asyncio.Lock()), ProbeSupervisor()
        async with gate.hold(OwnerTicketKind.RESTORE) as ticket:
            handle = start_source_probe(supervisor, store, gate, ticket,
                OwnerToken("probe", 1, 1, 1), SourceProbeControls(fault=fault))
            slot = supervisor.active
            with pytest.raises(ValueError, match=code):
                await handle.wait_source()
            await handle.wait_closed()
            assert slot.first_error == code and slot.disposal_fault is None
            assert not owned_edges(slot)
        await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("version", [0, -1, 2, 1.5, "bad"])
def test_invalid_sql_receipt_is_rejected_without_losing_cleanup(tmp_path, version):
    async def scenario():
        store = ExecutionStateStore(tmp_path / "source.sqlite3")
        await seed(store)
        await store._run(lambda: store._connection.execute(
            "UPDATE commits SET version=?", (version,)))
        gate, supervisor = OwnerTicketGate(asyncio.Lock()), ProbeSupervisor()
        async with gate.hold(OwnerTicketKind.RESTORE) as ticket:
            handle = start_source_probe(supervisor, store, gate, ticket,
                OwnerToken("probe", 1, 1, 1), SourceProbeControls())
            slot = supervisor.active
            with pytest.raises(ValueError, match="invalid_receipt_version"):
                await handle.wait_source()
            await handle.wait_closed()
            assert slot.first_error == "invalid_receipt_version"
            assert not owned_edges(slot) and slot.transaction_settled
        await store.close()
    asyncio.run(scenario())


def test_repeated_wait_closed_cancellation_preserves_cleanup_proof(tmp_path):
    async def scenario():
        store = ExecutionStateStore(tmp_path / "source.sqlite3")
        await seed(store, 65)
        gate, supervisor = OwnerTicketGate(asyncio.Lock()), ProbeSupervisor()
        controls = SourceProbeControls(pause_at="done_uncollected")
        async with gate.hold(OwnerTicketKind.RESTORE) as ticket:
            handle = start_source_probe(supervisor, store, gate, ticket,
                OwnerToken("probe", 1, 1, 65), controls)
            await observed(controls.entered.is_set)
            slot = supervisor.active
            try:
                for _ in range(2):
                    waiter = asyncio.create_task(handle.wait_closed())
                    await observed(lambda: waiter._fut_waiter is not None)
                    waiter.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await waiter
                    assert not slot.cleanup_proof.done()
                assert supervisor.active is slot and owned_edges(slot)
            finally:
                handle.request_cancel()
                controls.release.set()
            await asyncio.shield(slot.cleanup_proof)
            assert not owned_edges(slot) and supervisor.active is None
        await store.close()
    asyncio.run(scenario())


def test_last_public_handle_does_not_own_source(tmp_path):
    async def scenario():
        store = ExecutionStateStore(tmp_path / "source.sqlite3")
        await seed(store, 65)
        gate, supervisor = OwnerTicketGate(asyncio.Lock()), ProbeSupervisor()
        controls = SourceProbeControls(pause_at="done_uncollected")
        async with gate.hold(OwnerTicketKind.RESTORE) as ticket:
            handle = start_source_probe(supervisor, store, gate, ticket,
                OwnerToken("probe", 1, 1, 65), controls)
            await observed(controls.entered.is_set)
            slot = supervisor.active
            handle.request_cancel()
            ref = weakref.ref(handle)
            try:
                del handle
                assert ref() is None  # 실제 마지막 공개 handle 참조, 느슨한 대기 없음.
                assert supervisor.active is slot and owned_edges(slot)
                assert slot.worker_settled and not slot.worker.cancelled()
            finally:
                controls.release.set()
            await asyncio.shield(slot.cleanup_proof)
            assert not owned_edges(slot) and supervisor.active is None
        await store.close()
    asyncio.run(scenario())


async def fault_child(path, fault, mutation):
    store = ExecutionStateStore(path)
    await seed(store, 65)
    gate, supervisor = OwnerTicketGate(asyncio.Lock()), ProbeSupervisor()
    started, exiting, follower_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    follower_queued = asyncio.Event()
    owner_context = gate.hold(OwnerTicketKind.RESTORE)

    async def owner():
        async with owner_context as ticket:
            controls = SourceProbeControls(fault="known_sql" if fault == "rollback" else fault,
                rollback_fail=fault == "rollback", mutation=mutation)
            start_source_probe(supervisor, store, gate, ticket,
                OwnerToken("probe", 1, 1, 65), controls)
            started.set()
            await follower_queued.wait()
            await observed(lambda: supervisor.active.disposal_fault is not None
                           and supervisor.active.worker_settled)
            exiting.set()

    async def follower():
        async with gate.hold(OwnerTicketKind.PRODUCER_VIEW):
            follower_entered.set()

    owner_task = asyncio.create_task(owner())
    await started.wait()
    slot = supervisor.active
    follower_task = asyncio.create_task(follower())
    await observed(lambda: len(gate._queue) == 2)
    queued = True
    follower_queued.set()
    await exiting.wait()

    def draining():
        state = gate._active_state
        # Real gate state AND actual coroutine await chain, not helper reports.
        current = owner_context.gen.ag_await
        names = []
        while current is not None:
            code = getattr(current, "cr_code", None)
            if code is not None:
                names.append(code.co_name)
            current = getattr(current, "cr_await", None)
        return (state is not None and not state._drains_open
                and slot.cleanup_proof in state._drains
                and "_settle_drains" in names and not slot.cleanup_proof.done())

    # Mutation 설정을 읽지 않고 실제 drain 대기 또는 두 ticket의 종료를 관측한다.
    # owner 종료만으로 follower의 진입/퇴장 시점을 추정하지 않는다.
    await observed(lambda: draining() or (owner_task.done() and follower_task.done()))
    turn = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(turn.set_result, None)
    await turn
    if owner_task.done():
        owner_task.result()  # containment/task 실패는 유효 record가 될 수 없다.
    if follower_task.done():
        follower_task.result()
    drain_seen = draining()
    assert drain_seen or (owner_task.done() and follower_task.done())
    assert slot.status == "FAULT"
    assert slot.driver is not None or fault == "driver_create"
    if fault in ("foreign", "foreign_then_close"):
        assert slot.foreign_error.args[0] is slot.head.rows
        assert slot.foreign_error.__traceback__ is not None
        assert slot.first_error == "foreign_source_error"
        if fault == "foreign_then_close":
            assert slot.native_fault == "native_close_failed"
    elif fault == "rollback":
        assert slot.first_error == "known_sql"
        assert slot.disposal_fault == "rollback_failed"
        assert not slot.transaction_settled
    elif fault in ("driver_create", "driver_prestart_cancel"):
        assert not slot.driver_started
    result = {"case": "source_fault", "fault": slot.disposal_fault is not None,
        "worker_settled": slot.worker_settled, "follower_queued": queued,
        "exit_drain_observed": drain_seen, "post_fault_turn": True,
        "same_slot": supervisor.active is slot, "lock_held": gate.lock.locked(),
        "cleanup_completed": slot.cleanup_proof.done(),
        "follower_entered": follower_entered.is_set()}
    guard = _bind_exact_guard()
    print(json.dumps({"schema": "qwq.source-fault/v1", "guard": guard, "record": result},
                     sort_keys=True), flush=True)
    await asyncio.Future()  # Parent terminates: no claim of graceful cleanup.


def parse_fault_record(data):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            assert key not in result, "duplicate_child_key"
            result[key] = value
        return result

    result = json.loads(data, object_pairs_hook=unique_object)
    assert type(result) is dict and set(result) == set(FAULT_RECORD)
    assert type(result["case"]) is str and result["case"] == "source_fault"
    assert all(type(value) is bool for key, value in result.items() if key != "case")
    return result


def parse_fault_envelope(raw: str | bytes) -> dict:
    """합성 outer 증거를 검증한 뒤 기존 inner oracle로 원래 dict를 반환한다."""
    error = "source_proof_fault_envelope_invalid"

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(error)
            result[key] = value
        return result

    try:
        if type(raw) is str:
            raw = raw.encode("utf-8")
        if type(raw) is not bytes or len(raw) > 4096:
            raise ValueError(error)
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
        if (type(result) is not dict or set(result) != {"schema", "guard", "record"}
                or type(result["schema"]) is not str
                or result["schema"] != "qwq.source-fault/v1"):
            raise ValueError(error)
        guard = result["guard"]
        if (type(guard) is not dict
                or set(guard) != {"path", "sha256", "module_count", "violations"}
                or type(guard["path"]) is not str or guard["path"] != "tests/conftest.py"
                or type(guard["sha256"]) is not str or guard["sha256"] != _GUARD_SHA256
                or type(guard["module_count"]) is not int or guard["module_count"] != 1
                or type(guard["violations"]) is not int or guard["violations"] != 0):
            raise ValueError(error)
        return parse_fault_record(json.dumps(result["record"]))
    except (AssertionError, ValueError, TypeError, RecursionError):
        raise ValueError(error) from None


def assert_fault_mutation_record(result, mutation):
    assert mutation in ("skip_proof_registration", "premature_proof_completion")
    expected = {"case": "source_fault", "fault": True, "worker_settled": True,
        "follower_queued": True, "exit_drain_observed": False, "post_fault_turn": True,
        "same_slot": True, "lock_held": False,
        "cleanup_completed": mutation == "premature_proof_completion",
        "follower_entered": True}
    assert result == expected


def run_fault_child(tmp_path, fault, mutation="none"):
    deadline = time.monotonic() + 10
    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
        "--source-fault-child", str(tmp_path / "child.sqlite3"), fault, mutation],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": "UTC",
             "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
             "PYTHONPATH": os.pathsep.join((str(_ROOT / "tests"), str(_ROOT)))})
    selector = selectors.DefaultSelector()
    selector.register(child.stdout, selectors.EVENT_READ)
    data = b""
    try:
        while b"\n" not in data:
            remaining = deadline - time.monotonic()
            assert remaining > 0, "child_observation_timeout"
            assert selector.select(remaining), "child_observation_timeout"
            chunk = os.read(child.stdout.fileno(), 65536)
            assert chunk, "child_terminated_before_observation"
            data += chunk
        assert data.count(b"\n") == 1, "duplicate_child_record"
        result = parse_fault_envelope(data)
        assert child.poll() is None, "child_not_alive"
        return result
    finally:
        selector.close()
        child.terminate()
        remaining = deadline - time.monotonic()
        try:
            child.wait(timeout=max(.001, remaining))
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=1)
            raise AssertionError("child_termination_deadline")
        extra_stdout = child.stdout.read()
        child.stdout.close()
        child.stderr.close()
        assert not extra_stdout, "duplicate_child_record"
        assert time.monotonic() <= deadline, "child_total_deadline"


FAULT_RECORD = {"case": "source_fault", "fault": True, "worker_settled": True,
    "follower_queued": True, "exit_drain_observed": True, "post_fault_turn": True,
    "same_slot": True, "lock_held": True, "cleanup_completed": False,
    "follower_entered": False}


@pytest.mark.parametrize("fault", ["foreign", "rollback", "driver_create", "driver_prestart_cancel",
                                  "native_close", "native_unknown", "foreign_then_close"])
def test_uncertified_fault_keeps_actual_gate_drain_pending(tmp_path, fault):
    assert run_fault_child(tmp_path, fault) == FAULT_RECORD


@pytest.mark.parametrize("mutation", ["skip_proof_registration", "premature_proof_completion"])
def test_fault_protocol_kills_false_cleanup_mutations(tmp_path, mutation):
    assert_fault_mutation_record(run_fault_child(tmp_path, "foreign", mutation), mutation)


@pytest.mark.parametrize("corruption", ["wrong_case", "duplicate_key"])
def test_fault_record_oracle_rejects_invalid_identity_or_duplicate_keys(corruption):
    """잘못된 case/중복키 정규화가 gate 증거로 수용되는 결함을 잡는다."""
    raw = ('{"case":"source_fault","fault":true,"worker_settled":true,'
           '"follower_queued":true,"exit_drain_observed":true,"post_fault_turn":true,'
           '"same_slot":true,"lock_held":true,"cleanup_completed":false,'
           '"follower_entered":false}')
    if corruption == "wrong_case":
        raw = raw.replace('"source_fault"', '"wrong"')
    else:
        raw = raw.replace('"fault":true', '"fault":false,"fault":true')
    with pytest.raises((AssertionError, ValueError)):
        parse_fault_record(raw)


@pytest.mark.parametrize("mutation, completed", [
    ("skip_proof_registration", False), ("premature_proof_completion", True)])
@pytest.mark.parametrize("changed_fact", [
    "fault", "worker_settled", "follower_queued", "exit_drain_observed",
    "post_fault_turn", "same_slot", "lock_held", "cleanup_completed", "follower_entered"])
def test_fault_record_oracle_rejects_wrong_but_valid_mutation_facts(mutation, completed, changed_fact):
    """단순 정상 record와 다름이 아니라 해당 변이의 모든 관측 사실을 요구한다."""
    record = {"case": "source_fault", "fault": True, "worker_settled": True,
        "follower_queued": True, "exit_drain_observed": False, "post_fault_turn": True,
        "same_slot": True, "lock_held": False, "cleanup_completed": completed,
        "follower_entered": True}
    record[changed_fact] = not record[changed_fact]
    parsed = parse_fault_record(json.dumps(record))
    with pytest.raises(AssertionError):
        assert_fault_mutation_record(parsed, mutation)


@pytest.mark.parametrize("invalid", ["", "{}", "[]", '{"case":"source_fault"}',
    '{"case":"source_fault","fault":true'])
def test_fault_record_oracle_rejects_missing_or_malformed_observation(invalid):
    # Decoder 실패는 mutation-kill 판정에 도달하지 않는다.
    with pytest.raises((AssertionError, ValueError)):
        parse_fault_record(invalid)


@pytest.mark.parametrize("invalid", [0, 1, None, "true"])
def test_fault_record_oracle_requires_exact_boolean_schema(invalid):
    record = {"case": "source_fault", "fault": invalid, "worker_settled": True,
        "follower_queued": True, "exit_drain_observed": False, "post_fault_turn": True,
        "same_slot": True, "lock_held": False, "cleanup_completed": False,
        "follower_entered": True}
    with pytest.raises(AssertionError):
        parse_fault_record(json.dumps(record))
