"""명시 경로에만 저장하는 단일 writer SQLite 실행 checkpoint."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any


class StoreError(RuntimeError):
    """저장/복구에 실패했다. 빈 상태로 자동 복구하지 않는다."""


class StoreConflict(StoreError):
    """동일 commit ID의 내용 또는 예상 version이 충돌한다."""


def encode_state(state: dict) -> str:
    """JSON 도메인을 손실 없이 고정한다. 객체/비유한 숫자는 허용하지 않는다."""
    def validate(value: Any) -> None:
        if value is None or type(value) in (str, int, bool, float):
            return
        if type(value) is list:
            for item in value:
                validate(item)
            return
        if type(value) is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("checkpoint 키는 문자열이어야 합니다")
                validate(item)
            return
        raise ValueError("checkpoint는 명시적 JSON 값만 지원합니다")
    if type(state) is not dict:
        raise ValueError("checkpoint는 dict여야 합니다")
    validate(state)
    return json.dumps(state, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


class ExecutionStateStore:
    """연결과 모든 SQL은 한 전용 스레드에서만 실행한다.

    호출 취소 시에도 제출한 writer 작업이 끝날 때까지 기다린 후 취소를
    전달한다. 취소가 commit 취소를 의미하지 않으므로 caller는 복구 장벽을
    유지하고 load/lookup_commit으로 실제 결과를 확인해야 한다.
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: Path):
        if not isinstance(path, Path) or not path.is_absolute():
            raise ValueError("실행 저장소의 절대 Path를 명시해야 합니다")
        self.path = path
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="execution-store")
        self._connection: sqlite3.Connection | None = None
        self._closed = False

    async def _run(self, function, *args):
        if self._closed:
            raise StoreError("실행 저장소가 닫혀 있습니다")
        future = asyncio.get_running_loop().run_in_executor(self._executor, function, *args)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # 이미 시작된 SQL을 버리고 다음 명령을 보내면 순서/결과가 불명확해진다.
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if future.done() and not future.cancelled():
                future.exception()  # writer 예외는 복구에서 확인; 미회수 경고 방지
            raise

    def _private_files(self) -> None:
        for path in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            if path.is_symlink():
                raise StoreError("실행 저장소의 심볼릭 링크는 지원하지 않습니다")
            if path.exists():
                os.chmod(path, 0o600)

    def _open(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection
        conn = None
        try:
            if any(parent.is_symlink() for parent in (self.path, *self.path.parents)):
                raise StoreError("실행 저장소의 심볼릭 링크는 지원하지 않습니다")
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.path.parent, 0o700)
            self._private_files()
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                created = False
            else:
                os.close(fd)
                created = True
            conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            if not created:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version != self.SCHEMA_VERSION:
                    raise StoreError("지원하지 않는 실행 저장소 schema")
                tables = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"checkpoint", "commits"}.issubset(tables):
                    raise StoreError("실행 저장소 필수 테이블 누락")
                if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise StoreError("실행 저장소 무결성 검사 실패")
                row = conn.execute("SELECT version, state FROM checkpoint WHERE id=1").fetchone()
                if row is None or type(row[0]) is not int or row[0] < 0:
                    raise StoreError("실행 checkpoint 누락/손상")
                encode_state(json.loads(row[1]))
            if conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise StoreError("실행 저장소 WAL 활성화 실패")
            conn.execute("PRAGMA synchronous=FULL")
            if created:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute("CREATE TABLE checkpoint (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, state TEXT NOT NULL)")
                    conn.execute("CREATE TABLE commits (commit_id TEXT PRIMARY KEY, expected_version INTEGER NOT NULL, version INTEGER NOT NULL UNIQUE, payload_digest TEXT NOT NULL)")
                    conn.execute("INSERT INTO checkpoint VALUES (1, 0, '{}')")
                    conn.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            self._private_files()
            self._connection = conn
            return conn
        except Exception as exc:
            if conn is not None:
                conn.close()
            if isinstance(exc, StoreError):
                raise
            raise StoreError("실행 저장소 열기 실패") from exc

    def _load(self) -> tuple[int, dict]:
        try:
            row = self._open().execute("SELECT version, state FROM checkpoint WHERE id=1").fetchone()
            if row is None:
                raise StoreError("실행 checkpoint 누락")
            state = json.loads(row[1])
            encode_state(state)
            return row[0], state
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise StoreError("실행 checkpoint 읽기 실패") from exc

    async def load(self) -> tuple[int, dict]:
        return await self._run(self._load)

    def _load_with_policy_receipts(self) -> tuple[int, dict, dict[str, int]]:
        """같은 SQLite 읽기 snapshot에서 checkpoint와 등록 ID 전체를 대조한다."""
        conn = self._open()
        try:
            conn.execute('BEGIN')
            version, state = self._load()
            prefix = 'policy-registration:'
            rows = conn.execute(
                'SELECT commit_id, version FROM commits WHERE substr(commit_id, 1, ?) = ?',
                (len(prefix), prefix)).fetchall()
            receipts = {command[len(prefix):]: committed for command, committed in rows}
            if any(type(committed) is not int or not 0 < committed <= version
                   for committed in receipts.values()):
                raise StoreError('invalid_policy_registration_sql_receipt')
            conn.execute('COMMIT')
            return version, state, receipts
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise StoreError('실행 등록 영수증 읽기 실패') from exc
        finally:
            if conn.in_transaction:
                conn.execute('ROLLBACK')

    async def load_with_policy_receipts(self) -> tuple[int, dict, dict[str, int]]:
        return await self._run(self._load_with_policy_receipts)

    def _commit(self, expected_version: int, payload: str, commit_id: str) -> int:
        conn = self._open()
        digest = hashlib.sha256(payload.encode()).hexdigest()
        try:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute("SELECT expected_version, version, payload_digest FROM commits WHERE commit_id=?", (commit_id,)).fetchone()
            if previous is not None:
                if previous[0] != expected_version or previous[2] != digest:
                    raise StoreConflict("commit ID 내용 충돌")
                conn.execute("COMMIT")
                return previous[1]
            current = conn.execute("SELECT version FROM checkpoint WHERE id=1").fetchone()
            if current is None or current[0] != expected_version:
                raise StoreConflict("checkpoint version 충돌")
            version = expected_version + 1
            conn.execute("INSERT INTO commits VALUES (?, ?, ?, ?)",
                         (commit_id, expected_version, version, digest))
            conn.execute("UPDATE checkpoint SET version=?, state=? WHERE id=1", (version, payload))
            conn.execute("COMMIT")
            self._private_files()
            return version
        except Exception as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if isinstance(exc, StoreError):
                raise
            raise StoreError("실행 checkpoint commit 실패; 결과 대사 필요") from exc

    async def commit(self, expected_version: int, state: dict, commit_id: str) -> int:
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version은 0 이상 정수여야 합니다")
        if not isinstance(commit_id, str) or not commit_id.strip():
            raise ValueError("commit_id가 필요합니다")
        return await self._run(self._commit, expected_version, encode_state(state), commit_id)

    def _lookup_commit(self, commit_id: str) -> int | None:
        try:
            row = self._open().execute("SELECT version FROM commits WHERE commit_id=?", (commit_id,)).fetchone()
            return row[0] if row is not None else None
        except sqlite3.Error as exc:
            raise StoreError("실행 commit 조회 실패") from exc

    async def lookup_commit(self, commit_id: str) -> int | None:
        return await self._run(self._lookup_commit, commit_id)

    def _close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    async def close(self) -> None:
        if self._closed:
            return
        try:
            await self._run(self._close)
        finally:
            self._closed = True
            self._executor.shutdown(wait=False)
