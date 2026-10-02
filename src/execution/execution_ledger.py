"""주문 의도·체결 관측·메모리 적용 영수증의 계좌 범위 영구 원장.

원장은 과거 체결을 재생하지 않는다. handoff_returned는 호출부 반환의 증거이며
거래 저널 DB의 영구 저장 증거가 아니다. 실행 중 I/O는 async이며 작업 스레드에서
FULL synchronous로 커밋한다. 오프라인 내보내기는 별도 동기 읽기 전용 API다.
"""

from __future__ import annotations

import asyncio
import json
import hashlib
from concurrent.futures import ThreadPoolExecutor
import os
import sqlite3
import stat
from datetime import datetime
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
from pathlib import Path
from typing import Any


class ExecutionLedgerError(RuntimeError):
    """불명확한 기록 또는 저장 실패. 자동 복구·정상 간주를 허용하지 않는다."""


class ExecutionLedger:
    """같은 인스턴스의 같은 session open만 멱등이며 이전 session 쓰기는 금지한다.

    snapshot: {session_id, prior_unclean, orders: {key: {session_id, facts,
    odno, orgno, status, observed_quantity, observed_average, terminal_quantity,
    executions: [{execution_id, from_quantity, to_quantity, price,
    portfolio_applied, handoff_returned}]}}}.
    """

    def __init__(self, path: Path, scope: str):
        self.path = Path(path)
        self.scope = scope
        self._session_id: str | None = None
        self._lock = asyncio.Lock()
        self._fault = False
        # 공용 to_thread 풀을 쓰지 않는다 — pykrx 등이 풀을 점유해도 손절 POST 전 기록이 뒤에서 기다리지 않게 (48차 P2)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="execution-ledger")

    async def open(self, session_id: str) -> dict:
        return await self._run("open", session_id=session_id)

    async def intent(self, key: str, facts: dict) -> None:
        await self._run("intent", key=key, facts=facts)

    async def accepted(self, key: str, odno: str, orgno: str) -> None:
        await self._run("accepted", key=key, odno=odno, orgno=orgno)

    async def outcome(self, key: str, status: str) -> None:
        await self._run("outcome", key=key, status=status)

    async def observe(
        self, key: str, cumulative_quantity: int, cumulative_average: Decimal,
        terminal: bool = False,
    ) -> str | None:
        if not isinstance(cumulative_average, Decimal):
            raise ExecutionLedgerError("누적 평균가는 Decimal이어야 합니다")
        return await self._run(
            "observe", key=key, quantity=cumulative_quantity,
            average=str(cumulative_average), terminal=terminal,
        )

    async def receipt(self, execution_id: str, stage: str) -> None:
        await self._run("receipt", execution_id=execution_id, stage=stage)

    async def acknowledge(self, note: str) -> dict:
        """운영자가 명시 대사를 마친 과거 비정상 종료·미완결 주문을 보류 사유에서 제외한다.

        과거 기록은 바꾸지 않고 `acknowledged` 표시만 남긴다. 현재 실행이 소유한 주문은
        대상이 아니며, 체결을 재생하거나 장부/잔고를 고치지 않는다.
        """
        return await self._run("acknowledge", note=note)

    async def close_session(self) -> bool:
        if self._fault:
            return False
        return await self._run("close")

    async def snapshot(self) -> dict:
        return await self._run("snapshot")

    async def _run(self, operation: str, **payload: Any) -> Any:
        # 취소돼도 작업 스레드가 끝나기 전에는 직렬화 락을 풀지 않는다.
        async with self._lock:
            if self._fault and operation not in {"snapshot", "close"}:
                raise ExecutionLedgerError("현재 원장에 저장 실패가 있습니다")
            try:
                frozen_payload = json.loads(_encode(payload))
            except (TypeError, ValueError) as exc:
                raise ExecutionLedgerError("허용하지 않은 입력 형식") from exc
            loop = asyncio.get_running_loop()
            task = asyncio.ensure_future(loop.run_in_executor(self._executor, self._transaction, operation, frozen_payload))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                self._fault = True
                # 반복 취소도 스레드 실행과 다음 쓰기를 겹치게 만들지 않는다.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if task.done() and not task.cancelled():
                    task.exception()
                raise

    def _transaction(self, operation: str, payload: dict) -> Any:
        connection = None
        try:
            _text(self.scope)
            if operation == "open":
                _text(payload["session_id"])
            elif self._session_id is None:
                raise ExecutionLedgerError("open 이전에는 원장을 사용할 수 없습니다")
            if operation == "close" and self._fault:
                return False
            # 이미 존재하는 빈/손상 파일을 새 원장으로 바꾸지 않는다.
            existed = self.path.exists()
            if not existed and operation != "open":
                raise _CorruptLedger("열린 원장 파일이 사라졌습니다")
            directories_to_sync = []
            if not existed:
                directory = self.path.parent
                directories_to_sync.append(directory)
                while not directory.exists():
                    directory = directory.parent
                    directories_to_sync.append(directory)
                self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            if not existed:
                self._initialize(connection)
            self._validate_header(connection, full=operation == "open")
            state = self._load(connection) if operation == "open" else self._load_projection(connection)
            session_id = payload["session_id"] if operation == "open" else self._session_id
            if operation == "snapshot":
                result = self._snapshot(state)
            elif operation == "open" and self._session_id is not None:
                if session_id != self._session_id or next(reversed(state["sessions"])) != session_id:
                    raise ExecutionLedgerError("다른 실행으로 다시 열 수 없습니다")
                result = self._snapshot(state)
            elif operation == "close" and not _can_close(state, session_id):
                result = False
            else:
                event_payload = {} if operation in {"open", "close"} else payload
                before = _encode(state)
                result = _apply(state, session_id, operation, event_payload)
                if _encode(state) != before:
                    connection.execute(
                        "INSERT INTO events(session_id, operation, payload) VALUES (?, ?, ?)",
                        (session_id, operation, _encode(event_payload)),
                    )
                    self._save_projection(connection, state)
                if operation == "open":
                    result = self._snapshot(state, session_id)
            connection.commit()
            # 최초 파일/상위 디렉터리 이름도 커밋 반환 전에 내구성을 확인한다.
            for directory in directories_to_sync:
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            if operation == "open":
                self._session_id = session_id
            return result
        except ExecutionLedgerError as exc:
            if isinstance(exc, _CorruptLedger):
                self._fault = True
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, DecimalException) as exc:
            self._fault = True
            raise ExecutionLedgerError("실행 원장의 읽기/쓰기 또는 검증 실패") from exc
        finally:
            if connection is not None:
                try:
                    if connection.in_transaction:
                        connection.rollback()
                finally:
                    connection.close()

    def _initialize(self, connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA user_version = 1")
        connection.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.executemany("INSERT INTO metadata VALUES (?, ?)", [
            ("scope", self.scope),
            ("state_digest", _digest({"sessions": {}, "orders": {}})),
            ("event_sequence", "0"),
        ])
        connection.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        connection.execute("CREATE TABLE orders (order_key TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        connection.execute(
            "CREATE TABLE events (sequence INTEGER PRIMARY KEY, session_id TEXT NOT NULL, "
            "operation TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TRIGGER events_no_update BEFORE UPDATE ON events "
            "BEGIN SELECT RAISE(ABORT, 'immutable execution history'); END"
        )
        connection.execute(
            "CREATE TRIGGER events_no_delete BEFORE DELETE ON events "
            "BEGIN SELECT RAISE(ABORT, 'immutable execution history'); END"
        )

    def _validate_header(self, connection: sqlite3.Connection, *, full: bool) -> None:
        if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise _CorruptLedger("지원하지 않는 원장 스키마")
        if full and connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise _CorruptLedger("SQLite 무결성 검사 실패")
        metadata = dict(connection.execute("SELECT name, value FROM metadata"))
        if set(metadata) != {"scope", "state_digest", "event_sequence"} or metadata["scope"] != self.scope:
            raise _CorruptLedger("원장의 계좌 범위 또는 메타데이터 불일치")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if tables != {"metadata", "sessions", "orders", "events"}:
            raise _CorruptLedger("지원하지 않는 원장 테이블")
        for table, columns in (
            ("metadata", ["name", "value"]), ("sessions", ["session_id", "payload"]),
            ("orders", ["order_key", "payload"]),
            ("events", ["sequence", "session_id", "operation", "payload"]),
        ):
            if [row[1] for row in connection.execute(f"PRAGMA table_info({table})")] != columns:
                raise _CorruptLedger("지원하지 않는 원장 열")

    def _load(self, connection: sqlite3.Connection) -> dict:
        state = {"sessions": {}, "orders": {}}
        try:
            rows = connection.execute(
                "SELECT sequence, session_id, operation, payload FROM events ORDER BY sequence"
            )
            for expected, (sequence, session_id, operation, payload) in enumerate(rows, 1):
                if sequence != expected:
                    raise ExecutionLedgerError("이벤트 순서 손상")
                _apply(state, session_id, operation, json.loads(payload))
            if _encode(self._load_projection(connection)) != _encode(state):
                raise ExecutionLedgerError("이벤트와 현재 상태 불일치")
        except (ExecutionLedgerError, ValueError, TypeError, KeyError, DecimalException) as exc:
            raise _CorruptLedger("실행 이력 검증 실패") from exc
        return state

    @staticmethod
    def _load_projection(connection: sqlite3.Connection) -> dict:
        # 시작 시 전체 이력 검증 후에는 projection 봉인과 마지막 이벤트만 확인한다.
        # 외부 변경/부분 쓰기를 정상 상태로 해석하지 않으면서 이벤트 재생을 피한다.
        metadata = dict(connection.execute("SELECT name, value FROM metadata"))
        state = {}
        for table, key_column in (("sessions", "session_id"), ("orders", "order_key")):
            state[table] = {
                key: json.loads(payload) for key, payload in connection.execute(
                    f"SELECT {key_column}, payload FROM {table} ORDER BY rowid"
                )
            }
        sequence = connection.execute("SELECT COALESCE(MAX(sequence), 0) FROM events").fetchone()[0]
        if metadata["state_digest"] != _digest(state) or metadata["event_sequence"] != str(sequence):
            raise _CorruptLedger("현재 상태 봉인 또는 이벤트 끝 불일치")
        return state

    @staticmethod
    def _save_projection(connection: sqlite3.Connection, state: dict) -> None:
        for table, key_column in (("sessions", "session_id"), ("orders", "order_key")):
            for key, value in state[table].items():
                connection.execute(
                    f"INSERT INTO {table}({key_column}, payload) VALUES (?, ?) "
                    f"ON CONFLICT({key_column}) DO UPDATE SET payload = excluded.payload "
                    f"WHERE {table}.payload != excluded.payload",
                    (key, _encode(value)),
                )
        sequence = connection.execute("SELECT COALESCE(MAX(sequence), 0) FROM events").fetchone()[0]
        connection.executemany("UPDATE metadata SET value = ? WHERE name = ?", [
            (_digest(state), "state_digest"), (str(sequence), "event_sequence"),
        ])

    def _snapshot(self, state: dict, session_id: str | None = None) -> dict:
        session_id = session_id if session_id is not None else self._session_id
        return {
            "session_id": session_id,
            "prior_unclean": state["sessions"][session_id]["prior_unclean"],
            "orders": state["orders"],
        }


class _CorruptLedger(ExecutionLedgerError):
    """현재 실행에서 해제할 수 없는 영구 기록 불일치."""


def _encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_encode(value).encode("utf-8")).hexdigest()


def _text(value: Any, *, empty: bool = False) -> None:
    if not isinstance(value, str) or (not empty and not value.strip()) or "\x00" in value:
        raise ExecutionLedgerError("문자열 필드가 유효하지 않습니다")


def _facts(facts: Any) -> None:
    required = {"local_id", "symbol", "side", "quantity", "order_date", "strategy", "reason", "partial_exit"}
    if not isinstance(facts, dict) or set(facts) != required:
        raise ExecutionLedgerError("주문 사실 필드가 일치하지 않습니다")
    for name in ("local_id", "symbol", "order_date"):
        _text(facts[name])
    for name in ("strategy", "reason"):
        _text(facts[name], empty=True)
    if facts["side"] not in ("buy", "sell"):
        raise ExecutionLedgerError("주문 방향 오류")
    if type(facts["quantity"]) is not int or not 0 < facts["quantity"] <= 2**63 - 1:
        raise ExecutionLedgerError("주문 수량 오류")
    if type(facts["partial_exit"]) is not bool:
        raise ExecutionLedgerError("부분 청산 형식 오류")
    date = facts["order_date"]
    try:
        if len(date) != 8 or not date.isascii() or not date.isdigit():
            raise ValueError
        datetime.strptime(date, "%Y%m%d")
    except ValueError as exc:
        raise ExecutionLedgerError("주문 날짜 오류") from exc


def order_unresolved(record: dict) -> bool:
    """접수됐지만 최종 수량·적용·후처리 반환이 모두 확인되지 않은 주문."""
    if record["status"] in ("not_sent", "rejected"):
        return False
    if record["status"] in ("unknown", "modified"):
        return True
    terminal = record["terminal_quantity"]
    return (terminal is None or terminal != record["observed_quantity"]
            or any(not e["handoff_returned"] for e in record["executions"]))


def _can_close(state: dict, session_id: str) -> bool:
    sessions = state["sessions"]
    if session_id not in sessions or next(reversed(sessions)) != session_id:
        return False
    if sessions[session_id]["prior_unclean"]:
        return False
    return all(
        order.get("acknowledged") is True
        or order["status"] in {"not_sent", "rejected"}
        or (
            order["status"] in {"accepted", "canceled"}
            and order["terminal_quantity"] is not None
            and all(execution["handoff_returned"] for execution in order["executions"])
        )
        for order in state["orders"].values()
    )


def _apply(state: dict, session_id: str, operation: str, payload: dict) -> Any:
    """저장 시와 다시 읽을 때 동일 전이 규칙으로 전체 이력을 검사한다."""
    _text(session_id)
    if not isinstance(payload, dict):
        raise ExecutionLedgerError("이벤트 내용 형식 오류")
    shapes = {
        "open": set(), "close": set(), "intent": {"key", "facts"},
        "accepted": {"key", "odno", "orgno"}, "outcome": {"key", "status"},
        "observe": {"key", "quantity", "average", "terminal"},
        "receipt": {"execution_id", "stage"}, "acknowledge": {"note"},
    }
    if operation not in shapes or set(payload) != shapes[operation]:
        raise ExecutionLedgerError("알 수 없는 이벤트 또는 필드")
    sessions, orders = state["sessions"], state["orders"]
    if operation == "open":
        if session_id in sessions:
            raise ExecutionLedgerError("이전 실행 ID를 재사용할 수 없습니다")
        # 운영자가 명시 확인(acknowledge)한 비정상 종료는 다음 실행에 상속하지 않는다.
        prior_unclean = any(
            (not value["clean"] or value["prior_unclean"]) and value.get("acknowledged") is not True
            for value in sessions.values()
        )
        sessions[session_id] = {"clean": False, "prior_unclean": prior_unclean}
        return None
    if not sessions or next(reversed(sessions)) != session_id:
        raise ExecutionLedgerError("현재 실행만 기록할 수 있습니다")
    if operation == "close":
        if not _can_close(state, session_id):
            raise ExecutionLedgerError("미완결 실행을 정상 종료할 수 없습니다")
        sessions[session_id]["clean"] = True
        return True
    if sessions[session_id]["clean"]:
        raise ExecutionLedgerError("종료한 실행에는 기록할 수 없습니다")
    if operation == "acknowledge":
        _text(payload["note"])
        for other_id, value in sessions.items():
            if other_id != session_id and (not value["clean"] or value["prior_unclean"]):
                value["acknowledged"] = True
        for order in orders.values():
            if order["session_id"] != session_id and order_unresolved(order):
                order["acknowledged"] = True
        sessions[session_id]["prior_unclean"] = False
        return None
    if operation == "intent":
        key, facts = payload["key"], payload["facts"]
        _text(key)
        _facts(facts)
        if key in orders:
            if orders[key]["session_id"] != session_id or orders[key]["facts"] != facts:
                raise ExecutionLedgerError("주문 key의 기존 사실과 충돌합니다")
            return None
        orders[key] = {
            "session_id": session_id, "facts": facts, "odno": "", "orgno": "",
            "status": "intent", "observed_quantity": 0, "observed_average": "0",
            "terminal_quantity": None, "executions": [],
        }
        return None
    if operation == "receipt":
        _text(payload["execution_id"])
        if payload["stage"] not in {"portfolio_applied", "handoff_returned"}:
            raise ExecutionLedgerError("알 수 없는 영수증 종류")
        for order in orders.values():
            for execution in order["executions"]:
                if execution["execution_id"] == payload["execution_id"]:
                    if order["session_id"] != session_id:
                        raise ExecutionLedgerError("과거 실행에 현재 적용 영수증을 붙일 수 없습니다")
                    if payload["stage"] == "handoff_returned" and not execution["portfolio_applied"]:
                        raise ExecutionLedgerError("메모리 적용 전에 후처리를 완료할 수 없습니다")
                    execution[payload["stage"]] = True
                    return None
        raise ExecutionLedgerError("관측되지 않은 체결 ID")
    key = payload["key"]
    _text(key)
    if key not in orders or orders[key]["session_id"] != session_id:
        raise ExecutionLedgerError("현재 실행이 소유하지 않은 주문")
    order = orders[key]
    if operation == "accepted":
        _text(payload["odno"])
        _text(payload["orgno"], empty=True)
        if order["odno"]:
            if (order["odno"], order["orgno"]) != (payload["odno"], payload["orgno"]):
                raise ExecutionLedgerError("접수 주문 identity 충돌")
            return None
        if order["status"] != "intent":
            raise ExecutionLedgerError("현재 주문 상태에서 접수 확정 불가")
        for other_key, other in orders.items():
            if other_key != key and other["odno"] == payload["odno"] and other["facts"]["order_date"] == order["facts"]["order_date"]:
                raise ExecutionLedgerError("동일 날짜 broker 주문번호가 중복됩니다")
        order.update(odno=payload["odno"], orgno=payload["orgno"], status="accepted")
        return None
    if operation == "outcome":
        status = payload["status"]
        if status not in {"not_sent", "rejected", "unknown", "canceled", "modified"}:
            raise ExecutionLedgerError("지원하지 않는 주문 결과")
        if order["status"] == status:
            return None
        if order["status"] in {"not_sent", "rejected", "unknown", "modified"}:
            raise ExecutionLedgerError("확정/불명 결과를 다시 해석할 수 없습니다")
        if status in {"not_sent", "rejected"} and order["status"] != "intent":
            raise ExecutionLedgerError("접수 주문을 미전송/거절로 바꿀 수 없습니다")
        if status in {"canceled", "modified"} and not order["odno"]:
            raise ExecutionLedgerError("접수 identity 없이 취소/정정을 확정할 수 없습니다")
        order["status"] = status
        if status in {"not_sent", "rejected"}:
            order["terminal_quantity"] = 0
        return None
    return _observe(order, key, payload)


def _observe(order: dict, key: str, payload: dict) -> str | None:
    if order["status"] not in {"accepted", "canceled"} or not order["odno"]:
        raise ExecutionLedgerError("명확한 미정정 접수 주문만 관측할 수 있습니다")
    quantity, terminal = payload["quantity"], payload["terminal"]
    if type(quantity) is not int or not 0 <= quantity <= order["facts"]["quantity"] or type(terminal) is not bool:
        raise ExecutionLedgerError("누적 체결 수량/종료 형식 오류")
    _text(payload["average"])
    try:
        average = Decimal(payload["average"])
    except InvalidOperation as exc:
        raise ExecutionLedgerError("누적 평균가 오류") from exc
    if not average.is_finite() or average < 0 or (quantity > 0 and average == 0) or (quantity == 0 and average != 0):
        raise ExecutionLedgerError("누적 평균가 오류")
    previous_quantity = order["observed_quantity"]
    previous_average = Decimal(order["observed_average"])
    if quantity < previous_quantity or (quantity == previous_quantity and average != previous_average):
        raise ExecutionLedgerError("누적 수량 회귀 또는 동일 수량 평균가 충돌")
    if order["terminal_quantity"] is not None and quantity != order["terminal_quantity"]:
        raise ExecutionLedgerError("종료한 주문의 수량 변경")
    execution_id = None
    if quantity > previous_quantity:
        with localcontext() as context:
            context.prec = max(50, len(average.as_tuple().digits) + 20, len(previous_average.as_tuple().digits) + 20)
            delta_notional = average * quantity - previous_average * previous_quantity
            if delta_notional <= 0:
                raise ExecutionLedgerError("증분 체결 금액 회귀")
            price = delta_notional / (quantity - previous_quantity)
        execution_id = f"{key}:{quantity}"
        order["executions"].append({
            "execution_id": execution_id, "from_quantity": previous_quantity,
            "to_quantity": quantity, "price": _decimal_string(price),
            "portfolio_applied": False, "handoff_returned": False,
        })
        order["observed_quantity"] = quantity
        order["observed_average"] = _decimal_string(average)
    if terminal or quantity == order["facts"]["quantity"]:
        order["terminal_quantity"] = quantity
    return execution_id


def _decimal_string(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


_READ_MAX_BYTES = 128 * 1024 * 1024
_READ_MAX_EVENTS = 100_000


def read_execution_ledger(path: Path, expected_scope: str) -> dict:
    """중지 상태의 독립 SQLite 복사본을 변경 없이 검증하여 내보낸다.

    원본 파일 128MiB, 이벤트 100,000개 이내만 지원한다. 심볼릭 링크,
    일반 파일 외 입력, WAL 헤더와 -wal/-shm/-journal 동반 파일은 거부한다.
    unix-none VFS는 WAL 공유 메모리를 지원하지 않으므로 검사 직후 WAL로
    바뀌어도 shm을 만들지 않는다. 잠금 없는 VFS인 만큼 실행 중 DB는 지원하지
    않으며 바이트 지문과 파일 identity를 트랜잭션 전후에 다시 검사한다.
    플랫폼에 이 VFS가 없으면 자동 대체하지 않고 실패한다. 시각은 추정하지 않는다.
    """
    connection = None
    descriptor = None
    try:
        _text(expected_scope)
        source = Path(path)
        if not source.is_absolute():
            source = Path.cwd() / source
        initial = _readonly_source_stat(source)
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if _file_identity(os.fstat(descriptor)) != _file_identity(initial):
            raise ExecutionLedgerError("원장 파일 identity 변경")
        header = os.read(descriptor, 100)
        if len(header) != 100 or header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
            raise ExecutionLedgerError("독립 rollback SQLite 복사본만 지원합니다")
        # as_uri가 #, ?, %, 비ASCII 파일명까지 URI 경계와 분리한다.
        connection = sqlite3.connect(
            source.as_uri() + "?mode=ro&vfs=unix-none", uri=True,
            isolation_level=None, timeout=0,
        )
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("BEGIN")
        validator = ExecutionLedger(source, expected_scope)
        validator._validate_header(connection, full=True)
        event_count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        if not 1 <= event_count <= _READ_MAX_EVENTS:
            raise ExecutionLedgerError("지원 범위를 벗어난 이벤트 개수")
        source_sha256 = _readonly_source_hash(descriptor)
        state = validator._load(connection)
        metadata = dict(connection.execute("SELECT name, value FROM metadata"))
        if source_sha256 != _readonly_source_hash(descriptor):
            raise ExecutionLedgerError("읽는 중 원장 바이트 변경")
        if _file_identity(os.fstat(descriptor)) != _file_identity(initial):
            raise ExecutionLedgerError("읽는 중 원장 identity 변경")
        if _file_identity(_readonly_source_stat(source)) != _file_identity(initial):
            raise ExecutionLedgerError("읽는 중 원장 경로 변경")
        result = {
            "format": "execution-ledger-export-v1",
            "account_scope": expected_scope,
            "schema_version": 1,
            "event_count": event_count,
            "state_digest": metadata["state_digest"],
            "source_sha256": source_sha256,
            "sessions": state["sessions"],
            "orders": state["orders"],
        }
        connection.rollback()
        connection.close()
        connection = None
        # 연결 종료도 sidecar를 만들거나 바꾸지 않았는지 확인한다.
        if _file_identity(_readonly_source_stat(source)) != _file_identity(initial):
            raise ExecutionLedgerError("연결 종료 중 원장 변경")
        return result
    except (ExecutionLedgerError, sqlite3.Error, OSError, ValueError, TypeError,
            KeyError, DecimalException, RecursionError):
        # 파일명·계좌범위·원시 SQLite payload가 오류 경로로 노출되지 않는다.
        raise ExecutionLedgerError("읽기 전용 실행 원장 검증 실패") from None
    finally:
        if connection is not None:
            connection.close()
        if descriptor is not None:
            os.close(descriptor)


def _file_identity(value: os.stat_result) -> tuple:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def _readonly_source_stat(path: Path) -> os.stat_result:
    for candidate in (path, *path.parents):
        if stat.S_ISLNK(candidate.lstat().st_mode):
            raise ExecutionLedgerError("심볼릭 링크 원장은 지원하지 않습니다")
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode) or not 100 <= value.st_size <= _READ_MAX_BYTES:
        raise ExecutionLedgerError("원장 파일 형식/크기 범위 오류")
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            Path(str(path) + suffix).lstat()
        except FileNotFoundError:
            continue
        raise ExecutionLedgerError("SQLite 동반 파일이 있는 자료는 지원하지 않습니다")
    return value


def _readonly_source_hash(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    total = 0
    while chunk := os.read(descriptor, 1024 * 1024):
        total += len(chunk)
        if total > _READ_MAX_BYTES:
            raise ExecutionLedgerError("원장 파일 읽기 한도 초과")
        digest.update(chunk)
    return digest.hexdigest()
