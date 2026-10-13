"""비교 작업자 — 전용 스레드 + 자체 asyncio 루프. 운영 이벤트 루프에서는 `offer()` 의 put_nowait 만 일어난다.

공급자 호출·SQLite·수량 재생은 전부 작업자 스레드에서 기존 PairedComparisonRunner/ComparisonCallGuard 로 한다.
수락(accepted)은 큐 접수일 뿐 저장·완료 증거가 아니다 — finished 만 pair 결과 저장 완료다.

생명주기:
- 큐는 메모리다. 프로세스 재시작 시 큐 내용은 사라지고, SQLite `comparison_pairs` 의 result NULL 행(중단된 pair)은
  자동 재실행하지 않는다(러너가 `comparison_pair_incomplete_no_automatic_retry` 로 막는다 — 조회는 Task1 복구 도구).
- 같은 decision_id 재전달은 이 작업자 안에서 rejected_duplicate, 재시작 뒤에는 러너가 저장된 결과를 돌려줘
  공급자 호출·예약·과금이 추가되지 않는다.
- 하루 쌍 상한·입력 크기 상한은 offer 에서 큐에 넣기 전에 거부한다. 당일 입장 수는 start() 때 원장
  `comparison_pairs.admission_day` 에서 읽어 메모리로 이어 센다(재시작 뒤에도 유지). 최종 판정은 러너 claim 이다.
- stop(deadline): 새 수락을 닫고 시한 안에서 수락분을 처리한다. 시한을 넘기면 진행 중 작업을 취소하고 남은 큐를
  incomplete 로 센다. 공급자가 이미 받았을 수 있는 요청은 원장에 미해결로 남고 재전송하지 않는다.

저널(append-only JSONL, `journal_dir`, KST 월 파일): `offers-YYYY-MM.jsonl` 에 offer(봉투 전체)·상태 전이,
`incomplete-YYYY-MM.jsonl` 에 저장 실패한 원래 pair 행(복구 증거)을 쓴다. 운영 루프는 유한 큐에 넣기만 하고 저널
스레드가 쓴다. 큐 포화는 journal_dropped, 쓰기 실패는 journal_failed 로 세며 판단·offer 결과를 바꾸지 않는다.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import threading
from typing import Any

from loguru import logger

from . import model_comparison_input as mci
from .model_comparison_runner import (PAIR_BLOCK_REASONS, PairResultPersistenceError, PairSpec,
                                      PairedComparisonRunner, RoleControls, _json, context_bytes)

COUNTERS = ("offered", "accepted", "rejected_full", "rejected_disabled", "rejected_stale", "rejected_duplicate",
            "rejected_unattested", "rejected_invalid", "rejected_control_mismatch",
            "rejected_daily_pair_cap", "rejected_input_too_large",
            "expired", "started", "finished", "incomplete", "blocked", "journal_dropped", "journal_failed")
_INTEGRITY = {"unattested", "envelope_hash_mismatch", "context_hash_mismatch"}
ENV_ENABLE, ENV_MANIFEST = "COMPARISON_WORKER", "COMPARISON_EXPERIMENT_MANIFEST"
JOURNAL_VERSION = "comparison-offer-v1"
_KST = timezone(timedelta(hours=9))


def _kst_now():
    from .llm_decision_record import now_kst
    return now_kst()


def _time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def row_sha256(row) -> str:
    """pair 행의 정규화 JSON(sha256). 원장 저장 바이트와 같은 직렬화다."""
    return hashlib.sha256(_json(row).encode()).hexdigest()


def _record_stamp(record) -> str:
    """offer 는 recorded_at, 전이/표식은 at. None 만 대체하고 빈 문자열은 그대로 넘겨 시각 파서가 거부하게 한다."""
    stamp = record.get("recorded_at")
    return record["at"] if stamp is None else stamp


def _read_journal(path, max_bytes, kinds, check=None) -> dict:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive int")
    path = Path(path)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return {"path": str(path), "missing": True, "records": [], "skipped_lines": 0}
    if size > max_bytes:
        raise ValueError("journal_too_large")      # 일부만 읽은 분모는 보고하지 않는다
    records, skipped, seqs, dropped, closed = [], 0, {}, {}, {}
    for line in path.read_bytes().splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(record, dict) or record.get("schema_version") != JOURNAL_VERSION:
            skipped += 1
            continue
        run, seq = record.get("run_id"), record.get("journal_seq")
        if isinstance(run, str) and type(seq) is int and seq > 0:
            seqs.setdefault(run, set()).add(seq)
        if record.get("kind") == "journal_drop":
            total = record.get("dropped_total")
            if isinstance(run, str) and type(total) is int and total >= 0:
                dropped[run] = max(dropped.get(run, 0), total)
            continue
        if record.get("kind") == "journal_close":
            final, failed_total = record.get("final_seq"), record.get("journal_failed_total")
            if isinstance(run, str) and type(final) is int and type(failed_total) is int:
                closed[run] = {"final_seq": final, "journal_failed_total": failed_total}
            continue
        if record.get("kind") not in kinds or (check is not None and not check(record)):
            skipped += 1
            continue
        records.append(record)
    # 번호 빈틈 = run 별 (최대 번호 − 존재 번호 수). 번호 없는 옛 기록은 증거 없음으로 둔다.
    # 중간 빈틈: 번호 최대값 − 존재 수. 끝부분 유실: 종료 표식의 final_seq 보다 작은 최대 번호. 표식이 없는 run 은 미종료.
    gaps = sum(max(s) - len(s) for s in seqs.values())
    tail_loss = sum(max(0, info["final_seq"] - max(seqs.get(run, {0}))) for run, info in closed.items())
    open_runs = sorted(run for run in seqs if run not in closed)
    failed = sum(info["journal_failed_total"] for info in closed.values())
    unsequenced = sum(1 for r in records if not isinstance(r.get("run_id"), str) or type(r.get("journal_seq")) is not int)
    return {"path": str(path), "missing": False, "records": records, "skipped_lines": skipped,
            "sequence_gaps": gaps + tail_loss, "dropped_records": sum(dropped.values()),
            "unsequenced_records": unsequenced, "write_failures": failed,
            "runs": sorted(seqs), "open_runs": open_runs, "closed_runs": sorted(closed)}


def read_offer_journal(path, *, max_bytes) -> dict:
    """offers-YYYY-MM.jsonl → {path, missing, records(offer/transition), skipped_lines, sequence_gaps, dropped_records,
    unsequenced_records, write_failures, runs, open_runs, closed_runs}. journal_drop/journal_close 마커는 records 에 넣지 않고
    dropped_records·closed_runs 로만 반영한다. open_runs(종료 표식 없는 run)가 있으면 끝부분 유실을 판정할 수 없다."""
    return _read_journal(path, max_bytes, ("offer", "transition"))


def _incomplete_ok(record) -> bool:
    row = record.get("row")
    try:
        return (isinstance(row, dict) and record.get("pair_id") == row.get("pair_id")
                and record.get("row_sha256") == row_sha256(row))
    except (TypeError, ValueError):
        return False


def read_incomplete_journal(path, *, max_bytes) -> dict:
    """incomplete-YYYY-MM.jsonl → 같은 형식. row_sha256 이 행과 맞지 않는 줄은 skipped 로 센다."""
    return _read_journal(path, max_bytes, ("incomplete_result",), _incomplete_ok)


class ComparisonWorker:
    def __init__(self, runner: PairedComparisonRunner, *, manifest: dict, max_queue: int = 8,
                 max_input_age_seconds: float = 5.0, max_concurrent_pairs: int = 1,
                 shutdown_deadline_seconds: float = 20, clock=None, journal_dir=None, journal_queue: int = 1024):
        if not isinstance(runner, PairedComparisonRunner):
            raise ValueError("PairedComparisonRunner_required")
        if max_concurrent_pairs != 1:
            raise ValueError("max_concurrent_pairs must be 1 for the initial experiment")
        if not isinstance(manifest, dict) or manifest.get("enabled") is not True:
            raise ValueError("manifest_disabled")
        if manifest.get("experiment_id") != runner.guard.policy.experiment_id:
            raise ValueError("manifest_experiment_mismatch")
        self.runner, self.manifest = runner, json.loads(mci._canon(manifest))
        self.config_sha256 = hashlib.sha256(mci._canon(self.manifest).encode()).hexdigest()
        self._roles = {k: RoleControls(**self.manifest[k]) for k in ("current", "candidate", "bear")}
        self._fallback = tuple(RoleControls(**c) for c in self.manifest.get("fallback_chain", []))
        self.max_input_age_seconds = float(max_input_age_seconds)
        self.shutdown_deadline_seconds = shutdown_deadline_seconds
        self.clock = clock or _kst_now
        self._queue: "queue.Queue[dict]" = queue.Queue(maxsize=int(max_queue))
        self._lock = threading.Lock()
        self._counts = dict.fromkeys(COUNTERS, 0)
        self._seen: "OrderedDict[str, None]" = OrderedDict()   # ponytail: 최근 4096 ID, 하루 판단 수보다 훨씬 크다
        self.last_errors: deque = deque(maxlen=32)
        self.incomplete_results: deque = deque(maxlen=64)       # 메모리 사본. 영속 증거는 incomplete 저널이다
        self._accepting, self._stop, self._abort = False, threading.Event(), threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._current: asyncio.Task | None = None
        self.policy = runner.guard.policy
        # KST 날짜 → 그날 입장(또는 입장 예정)으로 센 pair_id. start() 때 원장에서 당일분을 채운다.
        self._day_pairs: dict[str, set] = {}
        self._journal_seq: dict[str, int] = {}   # 파일 경로별 단조 번호(저널 완전성 증거, 쓰는 시점에 부여)
        self._dropped_written = 0        # 저널에 이미 남긴 journal_dropped 누계
        self._fresh: dict[str, tuple[str, str]] = {}            # decision_id → 이번 offer 가 새로 센 (day, pair_id)
        self.journal_dir = (Path(journal_dir) if journal_dir is not None
                            else Path.home() / ".cache" / "ai_trader" / "comparison_worker")
        self._journal: "queue.Queue[dict | None]" = queue.Queue(maxsize=int(journal_queue))
        self._journal_thread: threading.Thread | None = None
        # 이 manifest 가 고정한 운영 조건 — 봉투의 실제 운영 조건과 다르면 비교가 아니다.
        self._expected_controls = {
            "bull": self.manifest["current"], "bear": self.manifest["bear"],
            "fallback_chain": self.manifest.get("fallback_chain", []),
            "fallback_chain_timeout_seconds": self.manifest["fallback_chain_timeout_seconds"],
            "empty_retries": self.manifest["empty_retries"],
            "strict_on_disagreement": self.manifest["strict_on_disagreement"],
        }

    # ── 생산자 쪽(운영 루프) ─────────────────────────────────
    def producer_config(self) -> dict:
        return {"experiment_id": self.manifest["experiment_id"], "config_sha256": self.config_sha256}

    def _note(self, key, decision_id=None, reason=None):
        self._counts[key] += 1
        if reason is not None:
            self.last_errors.append((decision_id, reason))
        return key

    def offer(self, envelope) -> str:
        """비차단. 반환은 accepted 또는 rejected_* 하나다. 저널 기록은 큐에 넣기만 한다(파일 I/O 없음)."""
        now = self.clock()
        with self._lock:
            status, decision_id, reason = self._decide(envelope, now)
        self._journal_put({"schema_version": JOURNAL_VERSION, "kind": "offer", "recorded_at": now.isoformat(),
                           "decision_id": decision_id, "status": status, "reason": reason,
                           "received_at": now.isoformat(),
                           "envelope": dict(envelope) if isinstance(envelope, dict) else envelope})
        return status

    def _decide(self, envelope, now):
        """(status, decision_id, reason). self._lock 안에서만 부른다."""
        self._counts["offered"] += 1
        decision_id = envelope.get("decision_id") if isinstance(envelope, dict) else None

        def note(key, why=None):
            self._note(key, decision_id, why)
            return key, decision_id, why
        if not self._accepting:
            return note("rejected_disabled")
        reason = mci.verify_envelope(envelope)
        if reason is not None:
            return note("rejected_unattested" if reason in _INTEGRITY else "rejected_invalid", reason)
        if now < _time(envelope["handed_off_at"]):
            return note("rejected_invalid", "clock_reversal")
        controls = envelope["role_controls"]
        producer = envelope["producer"] if isinstance(envelope.get("producer"), dict) else {}
        if (not isinstance(controls, dict)
                or any(controls.get(k) != v for k, v in self._expected_controls.items())
                or producer.get("experiment_id") != self.manifest["experiment_id"]
                or producer.get("config_sha256") != self.config_sha256
                or producer.get("source_sha256") != mci.source_sha256()
                or producer.get("run_id") != mci.RUN_ID or producer.get("pid") != os.getpid()):
            return note("rejected_control_mismatch", "control_or_producer_mismatch")
        if (now - _time(envelope["input_available_at"])).total_seconds() > self.max_input_age_seconds:
            return note("rejected_stale", "input_too_old_at_offer")
        if decision_id in self._seen:
            return note("rejected_duplicate")
        if context_bytes(envelope["common_context"]) > self.policy.max_common_context_bytes:
            return note("rejected_input_too_large", "comparison_input_too_large")
        pair_id = self.runner.pair_id(decision_id)
        day = self.runner.guard.ledger.today().isoformat()
        if day not in self._day_pairs:
            self._day_pairs = {day: set()}          # 지난 날짜는 버린다. 이 프로세스만 입장시키므로 새 날은 0부터다
        counted = self._day_pairs[day]
        # 이미 센 pair(재시작 뒤 같은 판단 재전달)는 러너가 저장 결과를 돌려줘 지출이 없다 — 상한에 다시 세지 않는다.
        if pair_id not in counted and len(counted) >= self.policy.pairs_per_day:
            return note("rejected_daily_pair_cap", "comparison_daily_pair_cap")
        try:
            self._queue.put_nowait(dict(envelope, received_at=now.isoformat()))
        except queue.Full:
            return note("rejected_full")
        if pair_id not in counted:
            counted.add(pair_id)
            self._fresh[decision_id] = (day, pair_id)
        self._seen[decision_id] = None
        while len(self._seen) > 4096:
            self._seen.popitem(last=False)
        return note("accepted")

    def _resync(self, decision_id):
        """러너가 claim 전에 실패했으면(원장 입장 없음) 그날 수에서 뺀다. 작업자 스레드에서만 원장을 읽는다."""
        pair_id = self.runner.pair_id(decision_id) if isinstance(decision_id, str) else None
        try:
            day = self.runner.guard.ledger.today().isoformat()
            admitted = self.runner.admitted_pair_ids(day)
        except Exception:
            return                                   # 확인 불가면 보수적으로 센 채 둔다(과소 사용)
        with self._lock:
            if pair_id not in admitted:
                self._day_pairs.get(day, set()).discard(pair_id)

    def _uncount(self, decision_id):
        """러너에 닿지 못한 수락분(만료·종료)은 그날 쌍 수에서 뺀다. 입장 없이 상한을 소진하지 않게."""
        day, pair_id = self._fresh.pop(decision_id, (None, None))
        if day is not None:
            self._day_pairs.get(day, set()).discard(pair_id)

    # ── 저널 ────────────────────────────────────────────────
    def _journal_put(self, record, timeout=None):
        # 완전성 증거(재리뷰 P2): run_id 는 여기서, 파일별 단조 journal_seq 는 쓰는 시점에 붙인다(월 분할·스트림 분리 뒤에도
        # 독자가 파일 하나 안의 빈틈만 유실로 세게 — 3회차 P2).
        record = dict(record, run_id=mci.RUN_ID)
        try:
            if timeout is None:
                self._journal.put_nowait(record)
            else:
                self._journal.put(record, timeout=timeout)
        except queue.Full:
            with self._lock:
                self._note("journal_dropped", record.get("decision_id"), "journal_queue_full")

    def _transition(self, decision_id, status, reason=None, *, admitted=None):
        # admitted: 원장 입장 성공 여부(True/False/None=미확인). 보고서 표본 선정은 finished 또는 admitted=True 만 실행으로 본다(6회차 P2).
        self._journal_put({"schema_version": JOURNAL_VERSION, "kind": "transition", "decision_id": decision_id,
                           "status": status, "reason": reason, "admitted": admitted, "at": self.clock().isoformat(),
                           "pair_id": self.runner.pair_id(decision_id) if isinstance(decision_id, str) else None},
                          timeout=1.0)

    def journal_path(self, kind: str, stamp: str) -> Path:
        month = _time(stamp).astimezone(_KST).strftime("%Y-%m")
        return self.journal_dir / f"{'incomplete' if kind == 'incomplete_result' else 'offers'}-{month}.jsonl"

    def _journal_write(self, record, path=None):
        path = path if path is not None else self.journal_path(record["kind"], _record_stamp(record))
        with self._lock:
            self._journal_seq[str(path)] = self._journal_seq.get(str(path), 0) + 1
            record = dict(record, journal_seq=self._journal_seq[str(path)])
            if record.get("kind") == "journal_close":
                record["final_seq"] = self._journal_seq[str(path)]
                record["journal_failed_total"] = self._counts["journal_failed"]
                record["journal_dropped_total"] = self._counts["journal_dropped"]
        line = json.dumps(record, sort_keys=True, ensure_ascii=True, default=str) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def _journal_run(self):
        while True:
            record = self._journal.get()
            if record is None:
                return
            try:
                # 큐 포화로 버린 기록이 있으면 그 사실을 먼저 남긴다 — 파일만 보고 "완전"으로 읽히지 않게 한다.
                with self._lock:
                    dropped = self._counts["journal_dropped"]
                    marker = (dropped > self._dropped_written)
                    if marker:
                        drop_record = {"schema_version": JOURNAL_VERSION, "kind": "journal_drop", "run_id": mci.RUN_ID,
                                       "dropped_total": dropped, "at": _record_stamp(record)}
                if marker:
                    self._journal_write(drop_record)
                    with self._lock:
                        self._dropped_written = dropped
                if record.get("kind") == "journal_close":
                    with self._lock:
                        touched = [Path(k) for k in self._journal_seq]
                    for path in touched:                     # 이 run 이 쓴 모든 파일(offers·incomplete)에 종료 표식
                        self._journal_write(record, path=path)
                    continue
                self._journal_write(record)
            except Exception as error:   # 기록 실패는 판단·offer 결과를 바꾸지 않는다
                with self._lock:
                    self._note("journal_failed", record.get("decision_id"), f"journal_write:{type(error).__name__}")

    # ── 작업자 스레드 ────────────────────────────────────────
    def start(self) -> "ComparisonWorker":
        if self._thread is not None:
            raise RuntimeError("worker_already_started")
        day = self.runner.guard.ledger.today().isoformat()
        self._day_pairs = {day: set(self.runner.admitted_pair_ids(day))}   # 재시작 뒤에도 당일 상한 유지
        self._journal_thread = threading.Thread(target=self._journal_run, name="comparison-journal", daemon=True)
        self._journal_thread.start()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._consume()), name="comparison-worker",
                                        daemon=True)   # 멈춘 공급자 호출이 프로세스 종료를 묶지 않게
        self._thread.start()
        with self._lock:
            self._accepting = True
        return self

    async def _consume(self):
        self._loop = asyncio.get_running_loop()
        try:
            while True:
                try:
                    envelope = self._queue.get_nowait()
                except queue.Empty:
                    if self._stop.is_set():
                        return
                    await asyncio.sleep(0.01)   # ponytail: 10ms 폴링, 큐 이벤트가 필요해질 때 교체
                    continue
                self._current = asyncio.create_task(self._process(envelope))
                try:
                    await self._current
                except asyncio.CancelledError:
                    pass
                finally:
                    self._current = None
        finally:
            await self._close_clients()

    def _spec(self, env) -> PairSpec:
        fields = dict(decision_id=env["decision_id"], signal_id=env["signal_id"], symbol=env["symbol"],
                      common_context=env["common_context"], input_available_at=env["input_available_at"],
                      enqueued_at=env["handed_off_at"], neutral_sizing=env["neutral_sizing"])
        controls = env["role_controls"]
        return PairSpec(**fields, current=self._roles["current"], candidate=self._roles["candidate"],
                        bear=self._roles["bear"], pricing_basis="paired_live_shadow",
                        fixed_at=self.manifest["fixed_at"],
                        strict_on_disagreement=self.manifest["strict_on_disagreement"],
                        fallback_chain_timeout_seconds=self.manifest["fallback_chain_timeout_seconds"],
                        fallback_prompt=(env["common_context"] + controls["fallback_prompt_suffix"]
                                         if self._fallback else None),
                        fallback_system=controls["fallback_system"], fallback_chain=self._fallback,
                        empty_retries=self.manifest["empty_retries"],
                        horizon_seconds=self.manifest["horizon_seconds"],
                        max_input_age_seconds=self.max_input_age_seconds,
                        evaluation_anchor="input_available_at", live_provenance="in_process_attested",
                        attestation=mci.attest_pair(**fields))

    def _admitted(self, decision_id):
        """원장 입장(claim) 성공 여부 — 작업자 스레드에서만 원장을 읽는다. 확인 불가는 None."""
        try:
            day = self.runner.guard.ledger.today().isoformat()
            return self.runner.pair_id(decision_id) in self.runner.admitted_pair_ids(day)
        except Exception:
            return None

    def _end(self, decision_id, status, reason=None, *, uncount=False, admitted=None):
        with self._lock:
            self._note(status, decision_id, reason)
            if uncount:
                self._uncount(decision_id)
        self._transition(decision_id, status, reason, admitted=admitted)

    async def _process(self, env):
        decision_id = env.get("decision_id")
        if self._abort.is_set():
            return self._end(decision_id, "incomplete", "shutdown_not_started", uncount=True)
        if (self.clock() - _time(env["input_available_at"])).total_seconds() > self.max_input_age_seconds:
            return self._end(decision_id, "expired", "input_too_old_at_dispatch", uncount=True)
        with self._lock:
            self._counts["started"] += 1
            self._fresh.pop(decision_id, None)   # 러너에 넘긴 뒤에는 입장 여부를 원장이 정한다
        self._transition(decision_id, "started")
        try:
            await self.runner.run(self._spec(env))
        except asyncio.CancelledError:
            self._end(decision_id, "incomplete", "cancelled_at_shutdown")
            raise
        except PairResultPersistenceError as error:
            row = error.result
            with self._lock:
                self.incomplete_results.append(row)
            # 복구(restore_pair_result)가 요구하는 영속 증거. 작업자 스레드라 잠깐 기다려도 된다.
            self._journal_put({"schema_version": JOURNAL_VERSION, "kind": "incomplete_result",
                               "recorded_at": self.clock().isoformat(), "decision_id": decision_id,
                               "pair_id": row.get("pair_id"), "row_sha256": row_sha256(row), "row": row}, timeout=5.0)
            self._end(decision_id, "incomplete", "pair_result_unpersisted", admitted=self._admitted(decision_id))
            logger.warning(f"[비교작업자] pair 결과 저장 실패 — 원래 행을 incomplete 저널에 보존: {row.get('pair_id')}")
        except Exception as error:
            reason = (getattr(error, "reason", None) or str(error) or type(error).__name__)[:200]
            self._resync(decision_id)
            self._end(decision_id, "blocked" if reason in PAIR_BLOCK_REASONS else "incomplete", reason,
                      admitted=self._admitted(decision_id))
            logger.warning(f"[비교작업자] pair 미완료 (판단 영향 없음): {reason}")
        else:
            self._end(decision_id, "finished", admitted=True)

    async def _close_clients(self):
        adapter = getattr(self.runner, "adapter", None)
        for client in (getattr(adapter, "openai", None), getattr(adapter, "gemini", None)):
            session = getattr(client, "_session", None)
            if session is not None and not session.closed:
                try:
                    await session.close()
                except Exception:
                    pass

    def stop(self, deadline_seconds: float | None = None) -> dict:
        """새 수락을 닫고 시한까지 drain. 시한 초과분은 incomplete. 반환은 health() + drained."""
        deadline = self.shutdown_deadline_seconds if deadline_seconds is None else deadline_seconds
        with self._lock:
            self._accepting = False
        self._stop.set()
        thread, drained = self._thread, True
        if thread is not None:
            thread.join(max(0.0, deadline))
            if thread.is_alive():
                drained = False
                self._abort.set()
                loop, task = self._loop, self._current
                if loop is not None and task is not None:
                    loop.call_soon_threadsafe(task.cancel)
                while True:
                    try:
                        env = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    self._end(env.get("decision_id"), "incomplete", "shutdown_not_started", uncount=True)
                thread.join(2.0)
        # 상태 전이·incomplete 증거가 모두 쓰인 뒤 돌려준다(저널 큐를 비운 뒤 종료).
        journal = self._journal_thread
        if journal is not None and journal.is_alive():
            # 종료 표식: 파일별 생산 번호·실패 수를 남겨 독자가 끝부분 유실을 판정한다(4회차 P2)
            try:
                self._journal.put({"schema_version": JOURNAL_VERSION, "kind": "journal_close", "run_id": mci.RUN_ID,
                                   "at": self.clock().isoformat()}, timeout=2.0)
                self._journal.put(None, timeout=2.0)
            except queue.Full:
                pass
            journal.join(5.0)
        return {**self.health(), "drained": drained,
                "journal_drained": journal is None or not journal.is_alive()}

    def health(self) -> dict:
        with self._lock:
            return {**self._counts, "enabled": self._accepting,
                    "alive": self._thread is not None and self._thread.is_alive(),
                    "experiment_id": self.manifest["experiment_id"], "queue_depth": self._queue.qsize(),
                    "max_queue": self._queue.maxsize, "incomplete_results": len(self.incomplete_results),
                    "journal_dir": str(self.journal_dir), "journal_depth": self._journal.qsize(),
                    "last_errors": list(self.last_errors)}


def attach(worker: ComparisonWorker, cross_validator) -> None:
    cross_validator._comparison_producer_config = worker.producer_config()
    cross_validator._comparison_handoff = worker.offer


def _raw_clients():
    from ..utils.llm import GeminiClient, LLMConfig, OpenAIClient
    config = LLMConfig.from_env()
    return OpenAIClient(config), GeminiClient(config)


def install_from_env(cross_validator, *, env=None) -> ComparisonWorker | None:
    """COMPARISON_WORKER=1 과 COMPARISON_EXPERIMENT_MANIFEST 가 둘 다 있고 manifest.enabled=true 일 때만 켠다.

    공유 월 원장은 운영 LLMManager 가 이미 연 것만 쓴다(새 원장·기준선 생성 없음). 실패는 None + 경고.
    """
    env = os.environ if env is None else env
    if env.get(ENV_ENABLE) != "1" or not env.get(ENV_MANIFEST):
        return None
    try:
        manifest = json.loads(Path(env[ENV_MANIFEST]).read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("enabled") is not True:
            logger.info("[비교작업자] manifest.enabled=false — 작업자 미생성")
            return None
        from ..utils.llm import get_llm_manager
        from .model_comparison_execution import ComparisonCallGuard, ComparisonExecutionPolicy
        from .model_comparison_runner import SingleAttemptAdapter
        ledger = get_llm_manager().monthly_budget
        if ledger is None:
            logger.warning("[비교작업자] 공유 월 원장 없음 — 작업자 미생성")
            return None
        policy = dict(manifest["execution_policy"], experiment_id=manifest["experiment_id"])
        policy["allowed_models"] = tuple(policy["allowed_models"])
        guard = ComparisonCallGuard(ledger=ledger, policy=ComparisonExecutionPolicy(**policy))
        openai, gemini = _raw_clients()
        age = manifest.get("max_input_age_seconds")
        if isinstance(age, bool) or not isinstance(age, (int, float)) or not (0 < float(age) <= 60):
            raise ValueError("manifest.max_input_age_seconds must be a number in (0, 60]")   # 보고서와 같은 값을 쓴다(5회차 P2)
        worker = ComparisonWorker(PairedComparisonRunner(guard=guard, adapter=SingleAttemptAdapter(
            openai=openai, gemini=gemini)), manifest=manifest, max_input_age_seconds=float(age))
        mci.source_sha256()   # 첫 판단 전에 소스 지문을 미리 계산해 운영 루프에서 파일을 읽지 않게 한다
        attach(worker.start(), cross_validator)
        logger.info(f"[비교작업자] 시작: experiment={manifest['experiment_id']}")
        return worker
    except Exception as error:
        logger.warning(f"[비교작업자] 설치 실패 (판단 영향 없음): {type(error).__name__}: {error}")
        return None
