"""비교 작업자 — 전용 스레드 + 자체 asyncio 루프. 운영 이벤트 루프에서는 `offer()` 의 put_nowait 만 일어난다.

공급자 호출·SQLite·수량 재생은 전부 작업자 스레드에서 기존 PairedComparisonRunner/ComparisonCallGuard 로 한다.
수락(accepted)은 큐 접수일 뿐 저장·완료 증거가 아니다 — finished 만 pair 결과 저장 완료다.

생명주기:
- 큐는 메모리다. 프로세스 재시작 시 큐 내용은 사라지고, SQLite `comparison_pairs` 의 result NULL 행(중단된 pair)은
  자동 재실행하지 않는다(러너가 `comparison_pair_incomplete_no_automatic_retry` 로 막는다 — 조회는 Task1 복구 도구).
- 같은 decision_id 재전달은 이 작업자 안에서 rejected_duplicate, 재시작 뒤에는 러너가 저장된 결과를 돌려줘
  공급자 호출·예약·과금이 추가되지 않는다.
- stop(deadline): 새 수락을 닫고 시한 안에서 수락분을 처리한다. 시한을 넘기면 진행 중 작업을 취소하고 남은 큐를
  incomplete 로 센다. 공급자가 이미 받았을 수 있는 요청은 원장에 미해결로 남고 재전송하지 않는다.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import queue
import threading
from typing import Any

from loguru import logger

from . import model_comparison_input as mci
from .model_comparison_runner import PairResultPersistenceError, PairSpec, PairedComparisonRunner, RoleControls

COUNTERS = ("offered", "accepted", "rejected_full", "rejected_disabled", "rejected_stale", "rejected_duplicate",
            "rejected_unattested", "rejected_invalid", "rejected_control_mismatch",
            "expired", "started", "finished", "incomplete")
_INTEGRITY = {"unattested", "envelope_hash_mismatch", "context_hash_mismatch"}
ENV_ENABLE, ENV_MANIFEST = "COMPARISON_WORKER", "COMPARISON_EXPERIMENT_MANIFEST"


def _kst_now():
    from .llm_decision_record import now_kst
    return now_kst()


def _time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ComparisonWorker:
    def __init__(self, runner: PairedComparisonRunner, *, manifest: dict, max_queue: int = 8,
                 max_input_age_seconds: float = 5.0, max_concurrent_pairs: int = 1,
                 shutdown_deadline_seconds: float = 20, clock=None):
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
        self.incomplete_results: deque = deque(maxlen=64)       # 저장 실패한 원래 pair 행(메모리, 재시작 시 소실)
        self._accepting, self._stop, self._abort = False, threading.Event(), threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._current: asyncio.Task | None = None
        # 이 manifest 가 고정한 운영 조건 — 봉투의 실제 운영 조건과 다르면 비교가 아니다.
        self._expected_controls = {
            "bull": self.manifest["current"], "bear": self.manifest["bear"],
            "fallback_chain": self.manifest.get("fallback_chain", []),
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
        """비차단. 반환은 accepted 또는 rejected_* 하나다."""
        now = self.clock()
        with self._lock:
            self._counts["offered"] += 1
            if not self._accepting:
                return self._note("rejected_disabled")
            reason = mci.verify_envelope(envelope)
            decision_id = envelope.get("decision_id") if isinstance(envelope, dict) else None
            if reason is not None:
                return self._note("rejected_unattested" if reason in _INTEGRITY else "rejected_invalid",
                                  decision_id, reason)
            if now < _time(envelope["handed_off_at"]):
                return self._note("rejected_invalid", decision_id, "clock_reversal")
            controls = envelope["role_controls"]
            producer = envelope["producer"] if isinstance(envelope.get("producer"), dict) else {}
            if (not isinstance(controls, dict)
                    or any(controls.get(k) != v for k, v in self._expected_controls.items())
                    or producer.get("experiment_id") != self.manifest["experiment_id"]
                    or producer.get("config_sha256") != self.config_sha256
                    or producer.get("source_sha256") != mci.source_sha256()
                    or producer.get("run_id") != mci.RUN_ID or producer.get("pid") != os.getpid()):
                return self._note("rejected_control_mismatch", decision_id, "control_or_producer_mismatch")
            if (now - _time(envelope["input_available_at"])).total_seconds() > self.max_input_age_seconds:
                return self._note("rejected_stale", decision_id, "input_too_old_at_offer")
            if decision_id in self._seen:
                return self._note("rejected_duplicate", decision_id)
            try:
                self._queue.put_nowait(dict(envelope, received_at=now.isoformat()))
            except queue.Full:
                return self._note("rejected_full", decision_id)
            self._seen[decision_id] = None
            while len(self._seen) > 4096:
                self._seen.popitem(last=False)
            return self._note("accepted")

    # ── 작업자 스레드 ────────────────────────────────────────
    def start(self) -> "ComparisonWorker":
        if self._thread is not None:
            raise RuntimeError("worker_already_started")
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
                        fallback_prompt=(env["common_context"] + controls["fallback_prompt_suffix"]
                                         if self._fallback else None),
                        fallback_system=controls["fallback_system"], fallback_chain=self._fallback,
                        empty_retries=self.manifest["empty_retries"],
                        horizon_seconds=self.manifest["horizon_seconds"],
                        max_input_age_seconds=self.max_input_age_seconds,
                        evaluation_anchor="input_available_at", live_provenance="in_process_attested",
                        attestation=mci.attest_pair(**fields))

    async def _process(self, env):
        decision_id = env.get("decision_id")
        if self._abort.is_set():
            with self._lock:
                self._note("incomplete", decision_id, "shutdown_not_started")
            return
        if (self.clock() - _time(env["input_available_at"])).total_seconds() > self.max_input_age_seconds:
            with self._lock:
                self._note("expired", decision_id, "input_too_old_at_dispatch")
            return
        with self._lock:
            self._counts["started"] += 1
        try:
            await self.runner.run(self._spec(env))
        except asyncio.CancelledError:
            with self._lock:
                self._note("incomplete", decision_id, "cancelled_at_shutdown")
            raise
        except PairResultPersistenceError as error:
            with self._lock:
                self.incomplete_results.append(error.result)
                self._note("incomplete", decision_id, "pair_result_unpersisted")
            logger.warning(f"[비교작업자] pair 결과 저장 실패 — 원래 행 메모리 보존: {error.result.get('pair_id')}")
        except Exception as error:
            reason = getattr(error, "reason", None) or str(error) or type(error).__name__
            with self._lock:
                self._note("incomplete", decision_id, reason[:200])
            logger.warning(f"[비교작업자] pair 미완료 (판단 영향 없음): {reason[:200]}")
        else:
            with self._lock:
                self._counts["finished"] += 1

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
                    with self._lock:
                        self._note("incomplete", env.get("decision_id"), "shutdown_not_started")
                thread.join(2.0)
        return {**self.health(), "drained": drained}

    def health(self) -> dict:
        with self._lock:
            return {**self._counts, "enabled": self._accepting,
                    "alive": self._thread is not None and self._thread.is_alive(),
                    "experiment_id": self.manifest["experiment_id"], "queue_depth": self._queue.qsize(),
                    "max_queue": self._queue.maxsize, "incomplete_results": len(self.incomplete_results),
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
        worker = ComparisonWorker(PairedComparisonRunner(guard=guard, adapter=SingleAttemptAdapter(
            openai=openai, gemini=gemini)), manifest=manifest)
        mci.source_sha256()   # 첫 판단 전에 소스 지문을 미리 계산해 운영 루프에서 파일을 읽지 않게 한다
        attach(worker.start(), cross_validator)
        logger.info(f"[비교작업자] 시작: experiment={manifest['experiment_id']}")
        return worker
    except Exception as error:
        logger.warning(f"[비교작업자] 설치 실패 (판단 영향 없음): {type(error).__name__}: {error}")
        return None
