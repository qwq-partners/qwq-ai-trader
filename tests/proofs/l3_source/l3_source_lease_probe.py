"""시험 전용 warm SQL source 수명 증명. 제품 복구/JSON decoder가 아니다.

증명 도메인: slot의 checkpoint 두 셀, cursor, head/tail, page.next/rows,
SQL list의 각 tuple 및 tuple 두 원소, 정리 함수의 명시 row/page 임시 참조.
Python 평가 스택/allocator/GC 전체 또는 latency를 계측했다는 뜻이 아니다.
worker는 등록된 graph를 빌드하고 terminal 이후 driver만 이를 해제한다.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import gc
import sqlite3
import sys
import threading
import weakref

from src.execution.safety.store import ExecutionStateStore
from src.execution.safety.owner_ticket_gate import OwnerTicketGate
from src.execution.safety.recovery_projection import OwnerToken


_PREFIX = "policy-registration:"
_DISPOSAL_BUDGET = 256


@dataclass
class SourceProbeControls:
    pause_at: str = "none"
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    fault: str = "none"
    rollback_fail: bool = False
    mutation: str = "none"

    def __post_init__(self):
        if type(self.pause_at) is not str or self.pause_at not in (
            "none", "after_checkpoint", "page", "done_uncollected", "before_driver"
        ):
            raise ValueError("invalid_probe_control")
        if type(self.fault) is not str or self.fault not in (
            "none", "known_sql", "foreign", "submit", "driver_create", "driver_prestart_cancel",
            "native_close", "native_unknown", "foreign_then_close"
        ):
            raise ValueError("invalid_probe_control")
        if type(self.mutation) is not str or self.mutation not in (
            "none", "skip_proof_registration", "premature_proof_completion",
            "retain_cursor", "retain_metadata"
        ):
            raise ValueError("invalid_probe_control")
        if (type(self.rollback_fail) is not bool
                or type(self.entered) is not threading.Event
                or type(self.release) is not threading.Event):
            raise ValueError("invalid_probe_control")


class _KnownSourceError(Exception):
    pass


class _ForeignSourceError(Exception):
    def __str__(self):
        raise AssertionError("foreign_error_must_not_be_formatted")

    def __repr__(self):
        raise AssertionError("foreign_error_must_not_be_formatted")


class _Page:
    __slots__ = ("rows", "next", "__weakref__")

    def __init__(self):
        self.rows = None
        self.next = None

    def take_rows(self, rows):
        # sqlite3의 exact list/tuple만 인수한다. 일반 Sequence/callback은 없다.
        self.rows = rows

    def validate_receipt_versions(self, revision):
        for row in self.rows:
            if (type(row) is not tuple or len(row) != 2
                    or type(row[0]) is not str or not row[0].startswith(_PREFIX)
                    or type(row[1]) is not int or not 0 < row[1] <= revision):
                raise _KnownSourceError("invalid_receipt_version")


class _SourceSlot:
    def __init__(self, supervisor, store, token, controls):
        self.supervisor = supervisor
        self.store = store
        # 연결 객체를 강하게 보유하므로 재활용된 id를 같은 lifetime으로 오인하지 않는다.
        self.connection = store._connection
        self.connection_nonce = object()
        self.token = token
        self.controls = controls
        self.revision = None
        self.encoded = None
        self.head = None
        self.tail = None
        self.cursor = None
        self.owned_edges = 0
        self.first_error = None
        self.disposal_fault = None
        self.foreign_error = None
        self.cleanup_error = None
        self.native_cursors = []  # 최대5, SQL 제출 전에 등록된 bounded registry.
        self.native_witnesses = []  # 최대5, weak target은 source를 pin하지 않는다.
        self.native_metadata_alias = None
        self.native_retired = False
        self.native_fault = None
        self.native_audit = []  # 최대5개의 fixed scalar tuple, actual audit와 상한은 별개.
        self.worker = None
        self.worker_settled = False
        self.transaction_settled = False
        self.driver = None
        self.driver_started = False
        self.status = "EMPTY"
        self.cleanup_proof = supervisor.loop.create_future()
        self.ready = supervisor.loop.create_future()
        self.cancel_latch = asyncio.Event()

    def bind_encoded_row(self, revision, encoded):
        self.revision = revision
        self.encoded = encoded
        self.owned_edges += 2

    def register_empty_page(self):
        # shell은 fetchmany보다 먼저 bounded linked registry에 들어간다.
        page = _Page()
        if self.tail is None:
            self.head = page
            self.tail = page
            self.owned_edges += 2
        else:
            self.tail.next = page
            self.tail = page
            self.owned_edges += 1
        return page

    @property
    def source_cleaned(self):
        # counter 외 실제 owning fields를 별도로 확인한다.
        return (self.revision is None and self.encoded is None
                and self.head is None and self.tail is None and self.cursor is None)

    def mark_fault(self, code):
        if self.disposal_fault is None:
            self.disposal_fault = code
        self.status = "FAULT"
        if not self.ready.done():
            self.ready.set_result(self.first_error or self.disposal_fault)
        if self.controls.mutation == "premature_proof_completion" and not self.cleanup_proof.done():
            self.cleanup_proof.set_result(None)

    def advance_disposal(self, max_edges):
        if type(max_edges) is not int or not 4 <= max_edges <= 256:
            raise ValueError("invalid_disposal_budget")
        if (not self.worker_settled or asyncio.current_task() is not self.driver
                or self.status != "DISPOSING"):
            raise ValueError("disposal_not_authorized")
        removed = 0
        while removed < max_edges and not self.source_cleaned:
            if self.encoded is not None:
                self.encoded = None
                self.owned_edges -= 1
                removed += 1
            elif self.revision is not None:
                self.revision = None
                self.owned_edges -= 1
                removed += 1
            elif self.cursor is not None:
                raise ValueError("native_cursor_not_retired")
            elif self.head.rows:
                if max_edges - removed < 4:
                    break
                row = self.head.rows.pop()
                # list edge1 + native tuple outgoing2 + local scratch1.
                del row
                self.owned_edges -= 3
                removed += 4
            elif self.head.rows is not None:
                self.head.rows = None
                self.owned_edges -= 1
                removed += 1
            elif self.tail is self.head:
                self.tail = None
                self.owned_edges -= 1
                removed += 1
            else:
                cost = 2 + int(self.head.next is not None)
                if max_edges - removed < cost:
                    break
                page = self.head
                self.head = page.next
                page.next = None
                del page
                # root + (next edge if present) + local scratch.
                # next가 있으면 새 head edge도 생기므로 live 순감소는 항상1.
                self.owned_edges -= 1
                removed += cost
        return removed


def _worker_pause(slot, phase):
    if slot.controls.pause_at == phase and not slot.controls.entered.is_set():
        slot.controls.entered.set()
        if not slot.controls.release.wait(timeout=8):
            raise _KnownSourceError("control_timeout")


class _NativeFault(Exception):
    pass


def _native_execute(slot, sql, parameters=()):
    if len(slot.native_cursors) >= 5:
        raise _NativeFault("native_inventory_overflow")
    cursor = slot.connection.cursor()
    if type(cursor) is not sqlite3.Cursor:
        raise _NativeFault("native_cursor_type")
    slot.native_cursors.append(cursor)
    slot.native_witnesses.append(weakref.ref(cursor))
    index = len(slot.native_cursors) - 1
    cursor.execute(sql, parameters)
    return index


def _native_snapshot(slot, index, check_aliases):
    """고정 Cursor만 읽는 tp_traverse 관측. GC 설정/수집을 변경하지 않는다."""
    cursor = slot.native_cursors[index]
    if type(cursor) is not sqlite3.Cursor or cursor.row_factory is not None:
        raise _NativeFault("unknown_native_domain")
    refs = gc.get_referents(cursor)
    if len(refs) > 7:
        raise _NativeFault("unknown_native_domain")
    statement_edges = 0
    for position in range(len(refs)):
        if (refs[position] is type(cursor) or refs[position] is slot.connection
                or refs[position] is cursor.description or refs[position] is cursor.row_factory
                or refs[position] is cursor.lastrowid):
            continue
        if (type(refs[position]).__module__ == "sqlite3"
                and type(refs[position]).__name__ == "Statement"):
            statement_refs = gc.get_referents(refs[position])
            if len(statement_refs) != 1 or statement_refs[0] is not type(refs[position]):
                raise _NativeFault("unknown_statement_domain")
            statement_edges += len(statement_refs)
            del statement_refs
        else:
            # detect_types 비0의 row_cast_map도 이 알 수 없는 영역으로 거부한다.
            raise _NativeFault("unknown_native_domain")
    nested_edges = 0
    if check_aliases:
        description = cursor.description
        if description is not None:
            if type(description) is not tuple or len(description) != 2:
                raise _NativeFault("unknown_metadata_domain")
            # cursor + refs list + description local + getrefcount argument.
            if sys.getrefcount(description) != 4:
                raise _NativeFault("native_metadata_alias")
            nested_edges = len(description)
            for position in range(2):
                descriptor = description[position]
                if (type(descriptor) is not tuple or len(descriptor) != 7
                        or type(descriptor[0]) is not str
                        or descriptor[1] is not None or descriptor[2] is not None
                        or descriptor[3] is not None or descriptor[4] is not None
                        or descriptor[5] is not None or descriptor[6] is not None):
                    raise _NativeFault("unknown_metadata_domain")
                if sys.getrefcount(descriptor) != 3:
                    raise _NativeFault("native_descriptor_alias")
                nested_edges += len(descriptor)
            del descriptor
        del description
    result = (len(refs), nested_edges, statement_edges)
    del refs, cursor
    return result


def _retire_native(slot):
    """같은 SQL callable 안에서 종료한다. loop로 native 해제를 넘기지 않는다.

    상한:5*(26+23)+7=252 outgoing edges.
    26=cursor fields/type7 + description16 + cursor owning roots2 + statement type1.
    S23/cursor=전후 referents list edges14+list roots2, 두 번째 cursor local1,
    description/descriptor aliases3, statement 관측 list edge/root/인수 최대3.
    7=두 bounded registry roots2+weak witness slots 최대5.
    실제 audit는 tp_traverse 전후 수/metadata 수이며 252를 실측값으로 반환하지 않는다.
    numeric diagnostics/index/control refs, 기존 connection weak registry/cache는
    native source graph의 마지막 참조가 아니며 이 retirement에서 해제하지 않는다.
    """
    if len(slot.native_cursors) > 5:
        raise _NativeFault("native_inventory_overflow")
    while slot.native_cursors:
        index = len(slot.native_cursors) - 1
        if slot.controls.fault == "native_unknown":
            slot.native_cursors[index].row_factory = object()
        before = _native_snapshot(slot, index, False)
        if slot.controls.fault in ("native_close", "foreign_then_close"):
            raise _NativeFault("native_close_failed")
        if (slot.controls.mutation == "retain_metadata"
                and slot.native_cursors[index].description is not None):
            slot.native_metadata_alias = slot.native_cursors[index].description
        slot.native_cursors[index].close()
        after = _native_snapshot(slot, index, True)
        slot.native_audit.append((before[0], after[0], after[1], before[2], after[2]))
        if slot.controls.mutation == "retain_cursor":
            slot.cursor = slot.native_cursors[index]
        # 반환 임시참조를 만드는 pop 대신 exact list의 slot을 직접 삭제한다.
        del slot.native_cursors[index]
        if slot.native_witnesses[index]() is not None:
            raise _NativeFault("native_cursor_survived")
    slot.native_retired = slot.native_metadata_alias is None
    for index in range(len(slot.native_witnesses)):
        if slot.native_witnesses[index]() is not None:
            slot.native_retired = False
    if not slot.native_retired:
        raise _NativeFault("native_retirement_uncertified")


def _read_source(slot):
    """기존 store executor의 callable 하나. 결과 Future는 항상 None이다."""
    conn = slot.connection
    row = rows = page = None
    try:
        if (conn is not slot.store._connection or type(conn) is not sqlite3.Connection
                or conn.row_factory is not None or conn.text_factory is not str):
            raise _NativeFault("source_connection_changed")
        _native_execute(slot, "BEGIN")
        checkpoint = _native_execute(slot, "SELECT version, state FROM checkpoint WHERE id=1")
        row = slot.native_cursors[checkpoint].fetchone()
        if (row is None or type(row[0]) is not int or row[0] < 0
                or type(row[1]) is not str):
            raise _KnownSourceError("invalid_source_row")
        slot.bind_encoded_row(row[0], row[1])
        row = None
        _worker_pause(slot, "after_checkpoint")
        if slot.controls.fault == "known_sql":
            _native_execute(slot, "SELECT * FROM l3_nonexistent_table")
        receipt = _native_execute(slot,
            "SELECT commit_id, version FROM commits WHERE substr(commit_id, 1, ?) = ?",
            (len(_PREFIX), _PREFIX))
        while True:
            page = slot.register_empty_page()
            rows = slot.native_cursors[receipt].fetchmany(64)
            page.take_rows(rows)
            slot.owned_edges += 1 + 3 * len(rows)
            rows = None
            if not page.rows:
                break
            page.validate_receipt_versions(slot.revision)
            _worker_pause(slot, "page")
            if slot.controls.fault in ("foreign", "foreign_then_close"):
                # args/traceback의 실제 source graph를 삭제하거나 sanitize하지 않는다.
                raise _ForeignSourceError(page.rows)
        _native_execute(slot, "COMMIT")
    except _KnownSourceError as exc:
        slot.first_error = exc.args[0]
    except sqlite3.Error:
        slot.first_error = "known_sql"
    except _NativeFault as exc:
        slot.native_fault = exc.args[0]
        slot.disposal_fault = exc.args[0]
    except BaseException as exc:
        slot.foreign_error = exc
        slot.first_error = "foreign_source_error"
        slot.disposal_fault = "foreign_source_error"
    finally:
        try:
            if conn.in_transaction:
                if slot.controls.rollback_fail:
                    _native_execute(slot, "ROLLBACK TO l3_nonexistent_savepoint")
                _native_execute(slot, "ROLLBACK")
            slot.transaction_settled = not conn.in_transaction
        except sqlite3.Error:
            slot.disposal_fault = "rollback_failed"
        except BaseException as exc:
            if slot.foreign_error is None:
                slot.foreign_error = exc
            else:
                slot.cleanup_error = exc
            slot.disposal_fault = "foreign_rollback_error"
        try:
            _retire_native(slot)
        except _NativeFault as exc:
            slot.native_fault = exc.args[0]
            if slot.disposal_fault is None:
                slot.disposal_fault = exc.args[0]
        except BaseException as exc:
            if slot.foreign_error is None:
                slot.foreign_error = exc
            else:
                slot.cleanup_error = exc
            if slot.disposal_fault is None:
                slot.disposal_fault = "cursor_close_failed"
        # scalar tuple2/list≤64 worker handoff alias를 terminal 전에 해제한다.
        # graph 본체는 등록돼 있다. foreign traceback은 fault slot에 보유한다.
        row = rows = page = None


async def _loop_pause(slot, phase):
    if slot.controls.pause_at != phase:
        return
    slot.controls.entered.set()
    resumed = slot.supervisor.loop.create_future()

    def observe_release():
        if resumed.done():
            return
        if slot.controls.release.is_set():
            resumed.set_result(None)
        else:
            slot.supervisor.loop.call_later(.001, observe_release)

    slot.supervisor.loop.call_soon(observe_release)
    await resumed


async def _drive(slot):
    slot.driver_started = True
    await _loop_pause(slot, "before_driver")
    await asyncio.shield(slot.worker)
    slot.worker_settled = True
    await _loop_pause(slot, "done_uncollected")
    if slot.disposal_fault is not None:
        slot.mark_fault(slot.disposal_fault)
        return
    if slot.first_error is not None:
        slot.cancel_latch.set()
        slot.ready.set_result(slot.first_error)
    elif slot.cancel_latch.is_set():
        slot.ready.set_result("source_cancelled")
    else:
        slot.status = "SOURCE_READY"
        slot.ready.set_result(None)
    await slot.cancel_latch.wait()
    slot.status = "DISPOSING"
    while not slot.source_cleaned:
        removed = slot.advance_disposal(_DISPOSAL_BUDGET)
        if not 0 < removed <= _DISPOSAL_BUDGET:
            slot.mark_fault("invalid_disposal_accounting")
            return
        await asyncio.sleep(0)
    if (slot.worker_settled and slot.transaction_settled and slot.native_retired
            and slot.owned_edges == 0 and slot.disposal_fault is None):
        slot.status = "CLEANED"
        slot.cleanup_proof.set_result(None)
        slot.supervisor.clear_if_same(slot)
    else:
        slot.mark_fault("cleanup_uncertified")


class ProbeSupervisor:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.active = None

    def clear_if_same(self, slot):
        if self.active is slot:
            self.active = None

    def observe_worker_done(self, slot, future):
        slot.worker_settled = True
        if future.cancelled():
            slot.mark_fault("worker_cancelled")
        elif future.exception() is not None:
            slot.foreign_error = future.exception()
            slot.mark_fault("worker_unexpected_error")

    def observe_driver_done(self, slot, task):
        if not slot.driver_started:
            slot.mark_fault("driver_never_started")
        elif task.cancelled():
            slot.mark_fault("driver_cancelled")
        elif task.exception() is not None:
            slot.foreign_error = task.exception()
            slot.mark_fault("driver_failed")
        elif slot.status not in ("CLEANED", "FAULT"):
            slot.mark_fault("driver_ended_without_cleanup")


class SourceProbeHandle:
    def __init__(self, slot):
        self._slot = slot

    @property
    def status(self):
        return self._slot.status

    async def wait_source(self):
        try:
            error = await asyncio.shield(self._slot.ready)
        except asyncio.CancelledError:
            self.request_cancel()
            raise
        if error is not None:
            raise ValueError(error)
        if self._slot.cancel_latch.is_set() or self.status != "SOURCE_READY":
            raise ValueError("source_unavailable")

    def request_cancel(self):
        if self.status not in ("CLEANED", "FAULT"):
            self._slot.cancel_latch.set()

    async def wait_closed(self):
        try:
            await asyncio.shield(self._slot.cleanup_proof)
        except asyncio.CancelledError:
            self.request_cancel()
            raise

    def validate_source(self, store, token):
        slot = self._slot
        if slot.status != "SOURCE_READY" or slot.cancel_latch.is_set():
            raise ValueError("source_unavailable")
        if (store is not slot.store or store._connection is not slot.connection
                or type(token) is not OwnerToken or token != slot.token
                or token.revision != slot.revision):
            self.request_cancel()
            raise ValueError("stale_source")

    def advance_disposal(self, max_edges):
        return self._slot.advance_disposal(max_edges)


def start_source_probe(supervisor, store, gate, ticket, token, controls):
    if type(supervisor) is not ProbeSupervisor or supervisor.loop is not asyncio.get_running_loop():
        raise ValueError("invalid_supervisor")
    if supervisor.active is not None:
        raise ValueError("source_already_active")
    if type(store) is not ExecutionStateStore:
        raise ValueError("invalid_source_store")
    if store._connection is None or store._closed:
        raise ValueError("cold_source_unsupported")
    if (sys.implementation.name != "cpython" or sys.version_info[:3] != (3, 12, 3)
            or type(store._connection) is not sqlite3.Connection
            or store._connection.row_factory is not None
            or store._connection.text_factory is not str):
        raise ValueError("unsupported_native_source")
    if type(gate) is not OwnerTicketGate:
        raise ValueError("inactive_ticket")
    state = gate._active_state
    if (state is None or state.ticket is not ticket or not state._active
            or not state._drains_open or not gate.lock.locked()):
        raise ValueError("inactive_ticket")
    if type(token) is not OwnerToken or type(controls) is not SourceProbeControls:
        raise ValueError("invalid_probe_input")
    controls.__post_init__()
    slot = _SourceSlot(supervisor, store, token, controls)
    supervisor.active = slot
    if controls.mutation != "skip_proof_registration":
        gate.register_submitted_drain(ticket, slot.cleanup_proof)
    slot.status = "SUBMITTED"
    try:
        if controls.fault == "submit":
            raise RuntimeError("fixed_submit_fault")
        slot.worker = supervisor.loop.run_in_executor(store._executor, _read_source, slot)
    except RuntimeError:
        slot.first_error = "submit_failed"
        slot.transaction_settled = True
        slot.native_retired = True  # native 자원을 하나도 할당하지 않았다.
        slot.worker = supervisor.loop.create_future()
        slot.worker.set_result(None)
    slot.worker.add_done_callback(lambda future: supervisor.observe_worker_done(slot, future))
    driver = _drive(slot)
    if controls.fault == "driver_create":
        driver.close()
        slot.mark_fault("driver_start_failed")
    else:
        try:
            slot.driver = supervisor.loop.create_task(driver)
        except BaseException as exc:
            driver.close()
            slot.foreign_error = exc
            slot.mark_fault("driver_start_failed")
        else:
            slot.driver.add_done_callback(lambda task: supervisor.observe_driver_done(slot, task))
            if controls.fault == "driver_prestart_cancel":
                slot.driver.cancel()
    return SourceProbeHandle(slot)


def inspect_source(handle):
    """Oracle 전용 사본. source 경로는 이 거대 tuple을 사용하지 않는다."""
    slot = handle._slot
    if slot.status != "SOURCE_READY" or slot.cancel_latch.is_set():
        raise ValueError("source_unavailable")
    rows = []
    page = slot.head
    while page is not None:
        rows.extend((row[0][len(_PREFIX):], row[1]) for row in page.rows)
        page = page.next
    return slot.revision, slot.encoded, tuple(rows)
