"""주문 의도·체결 관측·메모리 적용 영수증의 계좌 범위 영구 원장.

원장은 과거 체결을 재생하지 않는다. handoff_returned는 호출부 반환의 증거이며
거래 저널 DB의 영구 저장 증거가 아니다. 모든 공개 I/O는 async이고, 트랜잭션은
직렬화된 작업 스레드에서 FULL synchronous로 커밋한다.
"""

from __future__ import annotations

import asyncio
import json
import hashlib
import os
import sqlite3
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
            task = asyncio.create_task(asyncio.to_thread(self._transaction, operation, frozen_payload))
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


def _can_close(state: dict, session_id: str) -> bool:
    sessions = state["sessions"]
    if session_id not in sessions or next(reversed(sessions)) != session_id:
        return False
    if sessions[session_id]["prior_unclean"]:
        return False
    return all(
        order["status"] in {"not_sent", "rejected"}
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
        "receipt": {"execution_id", "stage"},
    }
    if operation not in shapes or set(payload) != shapes[operation]:
        raise ExecutionLedgerError("알 수 없는 이벤트 또는 필드")
    sessions, orders = state["sessions"], state["orders"]
    if operation == "open":
        if session_id in sessions:
            raise ExecutionLedgerError("이전 실행 ID를 재사용할 수 없습니다")
        prior_unclean = any(not value["clean"] or value["prior_unclean"] for value in sessions.values())
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
