"""명시 경로의 유한 관측 구간을 보존하는 선택적 원장. 운영 자동 설치/재개 없음.

전용 스레드만 파일을 만진다. publish는 유한 큐에 넣고 반환하며 최초 실패 후
저장을 재개하지 않는다. 해시는 우발적 손상 검사용이며 작성자/사전등록 인증이 아니다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import stat
import threading
from uuid import uuid4

from .entry_observation import EntryObservationBuffer
from .entry_price_shadow import _timestamp as parse_timestamp
from .selection_basis import BASIS_FIELDS, TERM_FIELDS, MAX_TERMS
from .selection_source_status import SCAN_FIELDS, RUN_FIELDS, ORDER
from .entry_gate_trace import TRACE_FIELDS, STEP_FIELDS, STAGES, SCAN_FIELDS as GATE_SCAN_FIELDS

from .kis_frame_diagnostics import FIELDS as FRAME_FIELDS, validate_settings as validate_frame_settings, validate_diagnostic

MAX_LINE_BYTES = 1024 * 1024
ZERO_HASH = "0" * 64
FIELDS = {
    "scan": "scan_id observed_at session route_origin population_scope candidates scan_admission_ref selection_basis_expected selection_basis_max_candidates selection_basis_max_terms",
    "rest_quote": "candidate_id observed_at requested_at source source_as_of quote",
    "signal": "candidate_id signal_id observed_at event_timestamp strategy price signal_target_price signal_stop_price",
    "emit_result": "candidate_id signal_id observed_at emitted",
    "order_ready": "signal_id symbol order_id observed_at requested_quantity order_reference_price order_type risk_stop_pct effective_stop_pct base_stop_source effective_stop_source crash_cap_applied is_core strategy stop_snapshot_ref capital_budget capital_snapshot capital_snapshot_ref capital_snapshot_status stop_basis stop_resolved_at_stage fill_applied transport_status",
    "ws_quote": "quote_id symbol observed_at ask bid ask_size bid_size provenance",
    "quote_subscription": "observed_at status generation connection_id candidate_id tr_id symbol reason expires_at frame_diagnostic",
}
FIELDS['selection_basis'] = 'candidate_id symbol observed_at basis_status ' + ' '.join(sorted(BASIS_FIELDS))
FIELDS['scan'] += ' ' + ' '.join(sorted(SCAN_FIELDS | GATE_SCAN_FIELDS))
FIELDS['entry_gate_trace'] = ' '.join(sorted(TRACE_FIELDS))
NESTED = {
    "candidates": set("symbol price score screened_at change_pct volume atr_pct candidate_id".split()),
    "quote": set("price open high low volume change_pct".split()),
    "provenance": set("tr_id exchange_time hour_class_code received_at message_count source_as_of connection_id generation".split()),
    "capital_snapshot": set(("version basis stage captured_at signal_id order_id symbol strategy requested_quantity "
        "current_order_reservation_included equity cash cash_after_reserve prior_pending_cash_reserved "
        "core_cash_reserved cash_capacity_before strategy_allocation_pct strategy_held_notional "
        "strategy_pending_reserved strategy_cap_notional strategy_remaining_notional reference_price").split()),
}
NESTED['source_terms'] = TERM_FIELDS
NESTED['selection_source_runs'] = RUN_FIELDS
NESTED['steps'] = STEP_FIELDS
NESTED['frame_diagnostic'] = FRAME_FIELDS


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_json(value)).hexdigest()


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


def _positive(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name}: 양의 정수 필요")


def _sha(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("SHA-256 소문자 64자리 필요")


def _timeout(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("유한 양수 timeout 필요")


async def _wait_until(predicate, timeout_seconds):
    # default executor의 무기한 Event.wait/join은 프로세스 종료까지 붙잡는다.
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("관측 원장 I/O 대기 한도 초과")
        await asyncio.sleep(0.01)


def _record_shape(record, maximum):
    """알려진 필드와 얕은 scalar 구조만 허용. 큰 문자열/배열은 직렬화 전에 중단."""
    if type(record) is not dict or record.get("kind") not in FIELDS:
        raise ValueError("지원하지 않는 관측 종류")
    _positive(record.get("sequence"), "sequence")
    if "frame_diagnostic" in record:
        if record.get("kind") != "quote_subscription" or record.get("status") != "connection_gap":
            raise ValueError("frame 진단은 connection_gap 전용")
        validate_diagnostic(record["frame_diagnostic"], reason=record.get("reason"))
    budget = maximum

    def scalar(value):
        nonlocal budget
        if isinstance(value, str):
            if len(value) > maximum:
                raise ValueError("관측 문자열 한도 초과")
            budget -= len(value.encode("utf-8")) + 2
        elif value is None or type(value) is bool:
            budget -= 5
        elif type(value) is int:
            budget -= len(str(value))
        elif type(value) is float and math.isfinite(value):
            budget -= len(repr(value))
        else:
            raise ValueError("관측 값은 유한 JSON scalar만 허용")
        if budget < 0:
            raise ValueError("관측 크기 한도 초과")

    def mapping(values, allowed, nested=False):
        nonlocal budget
        if type(values) is not dict or set(values) - allowed:
            raise ValueError("관측의 허용하지 않은 필드")
        for key, value in values.items():
            scalar(key)
            budget -= 2
            if nested and key in NESTED:
                if key == "capital_snapshot" and value is None:
                    scalar(None)
                elif key in ("candidates", "source_terms", "selection_source_runs", "steps"):
                    limit = {'source_terms': MAX_TERMS, 'selection_source_runs': len(ORDER), 'steps':len(STAGES)}.get(key, maximum // 4)
                    if not isinstance(value, list) or len(value) > limit:
                        raise ValueError("후보 배열 한도/형식 위반")
                    for item in value:
                        mapping(item, NESTED[key])
                else:
                    mapping(value, NESTED[key])
            else:
                scalar(value)
    mapping(record, set(FIELDS[record["kind"]].split()) | {"kind", "sequence"}, nested=True)


class ObservationJournal:
    """open/close는 명시 호출. 경로 재사용·자동 append 재개·기본 경로 없음."""

    @classmethod
    async def open(cls, buffer, path, *, study_ref, study_sha256, queue_capacity,
                   batch_size, max_bytes, max_record_bytes, open_timeout_seconds=5.0):
        _timeout(open_timeout_seconds)
        if (not isinstance(buffer, EntryObservationBuffer) or buffer._records
                or buffer._journal_sink is not None or buffer._capture_closed):
            raise ValueError("아직 사용하지 않은 관측 버퍼 필요")
        path = Path(path)
        if not path.is_absolute():
            raise ValueError("명시 절대 경로 필요")
        if not isinstance(study_ref, str) or not study_ref.strip() or len(study_ref) > 200:
            raise ValueError("제한된 연구 규약 참조 필요")
        _sha(study_sha256)
        for name, value in (("queue_capacity", queue_capacity), ("batch_size", batch_size),
                            ("max_bytes", max_bytes), ("max_record_bytes", max_record_bytes)):
            _positive(value, name)
        if batch_size > queue_capacity or max_record_bytes > MAX_LINE_BYTES - 2048:
            raise ValueError("batch/레코드 크기 한도 위반")
        self = cls()
        self.buffer, self.path = buffer, path
        self._queue = queue.Queue(maxsize=queue_capacity)
        self._batch_size, self._max_bytes, self._max_record_bytes = batch_size, max_bytes, max_record_bytes
        self._ready, self._closing, self._abort = threading.Event(), threading.Event(), threading.Event()
        self._failed = threading.Event()
        self._error = None
        self._open_error = None
        self._close_task = None
        self._sealed = self._fsync_confirmed = False
        self._last_hash, self._row_count, self._persisted, self._bytes = ZERO_HASH, 0, 0, 0
        self._final_status = None
        self._final_count = 0
        self._header = {"format": "entry-observation-journal-v1", "capture_id": uuid4().hex,
            "evaluation_epoch": buffer.evaluation_epoch, "created_at": _timestamp(),
            "study_ref": study_ref, "study_sha256": study_sha256,
            "capacity": buffer.capacity, "queue_capacity": queue_capacity, "batch_size": batch_size,
            "max_bytes": max_bytes, "max_record_bytes": max_record_bytes}
        if buffer.frame_diagnostics_settings is not None:
            self._header.update(format="entry-observation-journal-v2",
                                frame_diagnostics=dict(buffer.frame_diagnostics_settings))
        self._thread = threading.Thread(target=self._run, name="entry-observation-journal", daemon=True)
        self._thread.start()
        try:
            await _wait_until(self._ready.is_set, open_timeout_seconds)
            if self._open_error is not None:
                raise self._open_error
            if buffer._records or buffer._journal_sink is not None or buffer._capture_closed:
                raise ValueError("원장 준비 중 버퍼 사용/설치가 시작됨")
            buffer._journal_sink = self
            return self
        except BaseException:
            self._abort.set()
            self._closing.set()
            # 커널의 진행 중 I/O는 강제 중단할 수 없다. daemon이 반환되면 fd를 정리한다.
            raise

    def _fail(self, reason):
        self._error = self._error or reason
        self._failed.set()

    @property
    def failure_reason(self):
        return self._error

    def offer(self, record):
        # 파일 쓰기/잠금 대기는 없다. 메모리 버퍼가 보유한 불변 사본을 큐가 참조한다.
        if self._failed.is_set():
            self.buffer.mark_incomplete("journal_failed")
            return
        try:
            if "frame_diagnostic" in record and "frame_diagnostics" not in self._header:
                raise ValueError("선언되지 않은 frame 진단")
            _record_shape(record, self._max_record_bytes)
            self._queue.put_nowait(record)
        except (ValueError, TypeError, queue.Full):
            self._fail("journal_record_rejected_or_queue_full")
            self.buffer.mark_incomplete(self._error)

    def _write_row(self, kind, payload):
        row = {"row": self._row_count + 1, "previous_hash": self._last_hash,
               "kind": kind, "payload": payload}
        checksum = _digest(row)
        line = _json({**row, "hash": checksum}) + b"\n"
        if len(line) > MAX_LINE_BYTES or self._bytes + len(line) > self._max_bytes:
            raise ValueError("원장 파일/행 크기 한도 초과")
        self._file.write(line)
        self._last_hash, self._row_count = checksum, row["row"]
        self._bytes += len(line)

    def _sync(self):
        self._file.flush()
        os.fsync(self._file.fileno())

    def _write_batch(self, rows):
        for record in rows:
            if len(_json(record)) > self._max_record_bytes or record["sequence"] != self._persisted + 1:
                raise ValueError("기록 크기/관측 순서 불일치")
            self._write_row("record", record)
            self._persisted += 1
        self._sync()

    def _run(self):
        parent_fd = None
        try:
            parent_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            fd = os.open(self.path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent_fd)
            with os.fdopen(fd, "wb") as self._file:
                self._write_row("start", self._header)
                self._sync()
                os.fsync(parent_fd)  # 새 파일 이름도 디렉터리에 동기화한다.
                self._ready.set()
                while not self._closing.is_set() or not self._queue.empty():
                    if self._abort.is_set():
                        return
                    try:
                        rows = [self._queue.get(timeout=0.05)]
                    except queue.Empty:
                        continue
                    while len(rows) < self._batch_size:
                        try:
                            rows.append(self._queue.get_nowait())
                        except queue.Empty:
                            break
                    self._write_batch(rows)
                if self._abort.is_set():
                    return
                if parse_timestamp(self._sealed_at, "수집 종료") < parse_timestamp(self._header["created_at"], "수집 시작"):
                    raise ValueError("수집 경계 시각 역전")
                status = dict(self._final_status)
                persisted_drops = self._final_count - self._persisted
                reasons = list(status["incomplete_reasons"])
                if self._error:
                    reasons.append(self._error)
                status.update(complete=status["complete"] and not self._failed.is_set() and persisted_drops == 0,
                    incomplete_reasons=sorted(set(reasons)), record_count=self._persisted,
                    accepted_record_count=self._final_count, persistence_dropped_records=persisted_drops,
                    sealed_at=self._sealed_at)
                self._write_row("seal", status)
                self._sync()
                self._sealed = self._fsync_confirmed = True
        except Exception as exc:
            if not self._ready.is_set():
                self._open_error = exc
            self._fail("journal_writer_failed")
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
            self._ready.set()

    async def close(self, *, timeout_seconds=5.0):
        """명시 관측 종료 경계를 먼저 고정. 취소된 호출자는 다음 close로 결과 회수 가능."""
        _timeout(timeout_seconds)
        if self._close_task is None:
            self.buffer._capture_closed = True
            self._final_status = self.buffer.capture_status()
            self._final_count = len(self.buffer._records)
            self._sealed_at = _timestamp()
            self._closing.set()
            self._close_task = asyncio.create_task(self._finish(timeout_seconds))
        return await asyncio.shield(self._close_task)

    async def _finish(self, timeout_seconds):
        try:
            await _wait_until(lambda: not self._thread.is_alive(), timeout_seconds)
        except TimeoutError:
            self._fail("journal_close_timeout")
            self._abort.set()
        except asyncio.CancelledError:
            self._fail("journal_shutdown_cancelled")
            self._abort.set()
            raise
        return {"sealed": self._sealed, "fsync_confirmed": self._fsync_confirmed,
                "persisted_records": self._persisted, "error": self._error, "path": str(self.path)}


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("중복 JSON 키")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("JSON NaN/Infinity 금지")


def read_observation_journal(path, *, max_bytes, expected_study_sha256):
    """원본 무변경 strict reader. 무봉인 prefix는 결손 개수조차 불명으로 반환."""
    _positive(max_bytes, "max_bytes")
    _sha(expected_study_sha256)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise ValueError("정규 파일/읽기 크기 한도 위반")
        header, seal, records, previous, total = None, None, [], ZERO_HASH, 0
        for seq in range(1, max_bytes + 1):
            line = handle.readline(min(MAX_LINE_BYTES, max_bytes - total) + 1)
            if not line:
                break
            total += len(line)
            if total > max_bytes or len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
                raise ValueError("원장 크기 초과/부분 기록")
            row = json.loads(line, object_pairs_hook=_unique_keys, parse_constant=_reject_constant)
            if type(row) is not dict or set(row) != {"row", "previous_hash", "kind", "payload", "hash"}:
                raise ValueError("원장 envelope 불일치")
            checksum = row.pop("hash")
            if (type(row["row"]) is not int or row["row"] != seq or row["previous_hash"] != previous
                    or checksum != _digest(row) or seal is not None):
                raise ValueError("원장 해시/순서/봉인 불일치")
            previous = checksum
            kind, payload = row["kind"], row["payload"]
            if seq == 1:
                keys = {"format", "capture_id", "evaluation_epoch", "created_at", "study_ref", "study_sha256",
                        "capacity", "queue_capacity", "batch_size", "max_bytes", "max_record_bytes"}
                if type(payload) is dict and payload.get("format") == "entry-observation-journal-v2":
                    keys.add("frame_diagnostics")
                    validate_frame_settings(payload.get("frame_diagnostics"))
                if (kind != "start" or type(payload) is not dict or set(payload) != keys
                        or payload["format"] not in ("entry-observation-journal-v1", "entry-observation-journal-v2")
                        or payload["study_sha256"] != expected_study_sha256):
                    raise ValueError("원장 헤더/연구 규약 불일치")
                for key in ("capacity", "queue_capacity", "batch_size", "max_bytes", "max_record_bytes"):
                    _positive(payload[key], key)
                if (payload["batch_size"] > payload["queue_capacity"]
                        or payload["max_record_bytes"] > MAX_LINE_BYTES - 2048):
                    raise ValueError("헤더 크기/큐 계약 불일치")
                for key in ("capture_id", "evaluation_epoch", "study_ref", "created_at"):
                    if not isinstance(payload[key], str) or not payload[key].strip():
                        raise ValueError("원장 식별자 누락")
                parse_timestamp(payload["created_at"], "수집 시작")
                header = payload
            elif kind == "record":
                _record_shape(payload, header["max_record_bytes"])
                if "frame_diagnostic" in payload and "frame_diagnostics" not in header:
                    raise ValueError("선언되지 않은 frame 진단")
                if (payload["sequence"] != len(records) + 1 or len(records) >= header["capacity"]
                        or len(_json(payload)) > header["max_record_bytes"]):
                    raise ValueError("원래 관측 sequence 불일치")
                records.append(payload)
            elif kind == "seal":
                keys = {"schema_version", "evaluation_epoch", "complete", "incomplete_reasons", "dropped_records",
                        "record_count", "accepted_record_count", "persistence_dropped_records", "sealed_at"}
                if type(payload) is not dict or set(payload) != keys:
                    raise ValueError("봉인 schema 불일치")
                for key in ("dropped_records", "record_count", "accepted_record_count", "persistence_dropped_records"):
                    if type(payload[key]) is not int or payload[key] < 0:
                        raise ValueError("봉인 개수 불일치")
                reasons = payload["incomplete_reasons"]
                if (type(payload["schema_version"]) is not int or payload["schema_version"] != 1
                        or payload["evaluation_epoch"] != header["evaluation_epoch"]
                        or type(payload["complete"]) is not bool or not isinstance(reasons, list)
                        or any(not isinstance(v, str) for v in reasons)
                        or payload["record_count"] != len(records)
                        or payload["accepted_record_count"] > header["capacity"]
                        or payload["accepted_record_count"] != len(records) + payload["persistence_dropped_records"]
                        or (payload["complete"] and (reasons or payload["dropped_records"] or payload["persistence_dropped_records"]))):
                    raise ValueError("봉인 완전성/개수 불일치")
                if parse_timestamp(payload["sealed_at"], "수집 종료") < parse_timestamp(header["created_at"], "수집 시작"):
                    raise ValueError("수집 경계 시각 역전")
                seal = payload
            else:
                raise ValueError("지원하지 않는 원장 행 순서")
        if header is None or total > header["max_bytes"]:
            raise ValueError("원장 헤더 부재/선언 크기 초과")
    return {"schema_version": 1, "evaluation_epoch": header["evaluation_epoch"],
        **({"frame_diagnostics": dict(header["frame_diagnostics"])} if "frame_diagnostics" in header else {}),
        "complete": seal["complete"] if seal else False,
        "incomplete_reasons": seal["incomplete_reasons"] if seal else ["journal_unsealed"],
        "dropped_records": seal["dropped_records"] if seal else None, "records": records,
        "journal": {**({"format": header["format"], "frame_diagnostics": dict(header["frame_diagnostics"]),
                           "created_at": header["created_at"]} if "frame_diagnostics" in header else {}),
            "capture_id": header["capture_id"], "study_ref": header["study_ref"],
            "study_sha256": header["study_sha256"], "sealed": seal is not None,
            "capture_closed_at": seal["sealed_at"] if seal else None,
            "dropped_count_exact": seal is not None,
            "persistence_dropped_records": seal["persistence_dropped_records"] if seal else None,
            "structurally_valid": True, "durability_confirmed": False,
            "scope": "sealed_window" if seal else "durable_prefix_only"}}
