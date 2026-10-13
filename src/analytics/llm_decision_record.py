"""LLM 진입 판단의 재생 가능 기록 — `llm-decision-v1` (2026-10-13 64차).

판단 하나(signal_id ↔ decision_id)에 공통 입력 원문, 역할 프롬프트 지문, 호출 시도별 요청/실제 모델·시각·지연·
토큰·비용·예산 원장 연결, 결과(승인/거절/실패/폴백)와 결과 사용 가능 시각을 묶어 월별 JSONL 에 append 한다.
실시간 판단을 지연시키지 않도록 유한 큐 + 백그라운드 스레드로 쓰고, 큐/파일 상한을 넘으면 기록을 버리되
그 사실을 반환값(`record_status`)으로 호출자에 돌려준다 — 기록 실패를 성공처럼 처리하지 않는다.

응답 이후 최초 유효 호가·장 상태는 이 기록의 `decision_available_at` 과 관측 원장(ws_quote, market state)을
오프라인에서 시각으로 조인해 얻는다. 별도 WebSocket 을 열지 않는다. 자격증명·계좌 식별정보는 입력에 없다
(공통 입력은 `entry-llm-input-v1` 사실 블록, 역할 지시, 모델 응답 본문만 담는다).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections import OrderedDict
import queue
import threading
import time
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from loguru import logger

KST = ZoneInfo("Asia/Seoul")
SCHEMA_VERSION = "llm-decision-v1"
MAX_TEXT_CHARS = 2000          # 응답 본문 보존 상한 (재생용, 전체 프롬프트는 common_context 로 재구성)
MAX_FILE_BYTES = 64 * 1024 * 1024
QUEUE_SIZE = 256
ENV_FLAG = "LLM_DECISION_RECORD"   # 운영 런처가 "0" 이 아니면 켠다. 모듈 기본 기록기는 명시 "1" 일 때만 켜져 시험이 실제 홈에 쓰지 않는다


def now_kst() -> datetime:
    return datetime.now(KST)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None


def sha256_text(text: Optional[str]) -> Optional[str]:
    if not isinstance(text, str):
        return None
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _truncate(text: Optional[str]) -> Optional[str]:
    if not isinstance(text, str):
        return None
    return text if len(text) <= MAX_TEXT_CHARS else text[:MAX_TEXT_CHARS]


def _int_or_none(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _estimated_cost_usd(response) -> Optional[str]:
    """공급자가 사용량을 보고했고 단가표에 있는 모델만 추정 비용(Decimal 문자열). 결측·불명은 None(0 으로 대체하지 않음)."""
    if response is None or getattr(response, "usage_known", False) is not True:
        return None
    tokens_in, tokens_out = _int_or_none(getattr(response, "input_tokens", None)), _int_or_none(getattr(response, "output_tokens", None))
    if tokens_in is None or tokens_out is None:
        return None
    try:
        from ..utils.llm_pricing import rate_quote
        model = getattr(response, "requested_model", None)
        if model is None:
            model = getattr(response, "model", None)
        if not isinstance(model, str):
            return None
        quote = rate_quote(model, tokens_in, tokens_out)
    except Exception:
        return None
    return None if quote is None else str(Decimal(str(quote.cost_usd)))


def _sum_trail_cost(trail: Any) -> Dict[str, Any]:
    """trail(실제 시도 목록)의 알려진 비용 합 — 하나라도 불명이면 cost_complete=False."""
    if not isinstance(trail, list) or not trail:
        return {"calls": 0, "estimated_cost_usd": None, "cost_complete": False}
    total, complete = Decimal("0"), True
    for row in trail:
        cost = row.get("estimated_cost_usd") if isinstance(row, dict) else None
        if cost is None:
            if isinstance(row, dict) and row.get("request_sent") is False:
                continue                       # 미전송 시도는 비용 0 이 확정이다
            complete = False
            continue
        total += Decimal(str(cost))
    return {"calls": len(trail), "estimated_cost_usd": str(total) if complete else None, "cost_complete": complete}


def attempt_record(role: str, provider: Any, requested_at: Optional[datetime], response: Any, *,
                   role_system_sha256: Optional[str], role_suffix: Optional[str],
                   timed_out: bool = False, responded_at: Optional[datetime] = None,
                   trail: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """LLM 호출 시도 하나의 기록. `response` 는 LLMResponse, Exception, 또는 None(타임아웃·미도달).
    `trail` 은 응답이 없을 때(취소·예외) LLMManager 가 예외에 붙여 준 실제 호출 내역이다."""
    exc = response if isinstance(response, BaseException) else None
    resp = None if exc is not None else response
    if exc is not None:
        if trail is None:
            trail = getattr(exc, "_llm_trail", None)
        if resp is None:
            resp = getattr(exc, "_llm_response", None)   # 공급자 예외에 붙은 미전송/실패 응답(예산·토큰 보존)
    block = getattr(resp, "budget_block_reason", None)
    if timed_out:
        outcome = "timeout"
    elif exc is not None:
        outcome = "exception"
    elif resp is None:
        outcome = "not_sent"
    elif isinstance(block, str) and block:
        outcome = "budget_blocked"
    elif not getattr(resp, "request_sent", True):
        outcome = "not_sent"
    elif not getattr(resp, "success", False):
        outcome = "failed"
    elif not (getattr(resp, "content", None) or "").strip():
        outcome = "empty"
    else:
        outcome = "success"
    usage_known = getattr(resp, "usage_known", None) is True
    if timed_out and responded_at is None:
        responded_at_value = None          # 완료 시각이 없다 — 기록 생성 시각으로 대체하지 않는다
    else:
        responded_at_value = responded_at if responded_at is not None else now_kst()
    resolved = getattr(resp, "resolved_model", None)
    requested_model = getattr(resp, "requested_model", None)
    if requested_model is None:
        requested_model = getattr(resp, "model", None)
    content = _truncate(getattr(resp, "content", None))
    if trail is None:
        trail = getattr(resp, "attempt_trail", None)
    trail_summary = _sum_trail_cost(trail)
    return {
        "attempt_id": new_id("att"),
        "role": role,
        "provider": getattr(provider, "value", str(provider)) if provider is not None else None,
        "requested_at": _iso(requested_at),
        "responded_at": _iso(responded_at_value),
        "latency_ms": getattr(resp, "latency_ms", None),
        "requested_model": requested_model,
        "requested_reasoning_effort": getattr(resp, "requested_reasoning_effort", None),
        "requested_thinking_level": getattr(resp, "requested_thinking_level", None),
        # 공급자가 응답에 모델을 적어 준 경우만 확인된 신원이다. 요청 모델은 신원 증거가 아니다.
        "resolved_model": resolved,
        "model_identity": "provider_reported" if resolved else "unverified",
        # 공급자가 사용량을 보고한 경우만 토큰 수를 적는다 — 미확인은 0 이 아니라 None (4차 리뷰 P2)
        "input_tokens": _int_or_none(getattr(resp, "input_tokens", None)) if usage_known else None,
        "output_tokens": _int_or_none(getattr(resp, "output_tokens", None)) if usage_known else None,
        "reasoning_tokens": _int_or_none(getattr(resp, "reasoning_tokens", None)) if usage_known else None,
        "usage_known": getattr(resp, "usage_known", None),
        "billing_status": getattr(resp, "billing_status", None),
        # 최종 응답 1건의 추정 비용(Decimal 문자열). 재시도·폴백을 포함한 호출 전체는 calls 에 있다.
        "estimated_cost_usd": _estimated_cost_usd(resp),
        # LLMManager 가 이 응답을 만들기까지 실제로 보낸 공급자 시도(재시도·폴백 각 1행, 예약 토큰 포함)
        "calls": trail if isinstance(trail, list) else None,
        "calls_count": trail_summary["calls"],
        "calls_estimated_cost_usd": trail_summary["estimated_cost_usd"],
        "calls_cost_complete": trail_summary["cost_complete"],
        "budget": {
            "policy_mode": getattr(resp, "budget_policy_mode", None),
            "policy_version": getattr(resp, "budget_policy_version", None),
            "priority": getattr(resp, "budget_priority", None),
            "block_reason": block if isinstance(block, str) and block else None,
            "reservation_token": getattr(resp, "budget_reservation_token", None),
        },
        "outcome": outcome,
        "error": (str(exc)[:200] if exc is not None else getattr(resp, "error", None)),
        "finish_reason": getattr(resp, "finish_reason", None),
        "http_status": getattr(resp, "http_status", None),
        "provider_error_code": getattr(resp, "provider_error_code", None),
        "role_system_sha256": role_system_sha256,
        "role_suffix": role_suffix,
        "response_text": content,
        "response_sha256": sha256_text(getattr(resp, "content", None)),
    }


def safe_attempt_record(role: str, provider: Any, requested_at: Optional[datetime], response: Any, **kwargs) -> Dict[str, Any]:
    """attempt_record 의 예외를 판단 경로로 올리지 않는다 — 실패 시 record_error 행을 남긴다 (리뷰 P1)."""
    try:
        return attempt_record(role, provider, requested_at, response, **kwargs)
    except Exception as error:
        logger.warning(f"[판단기록] 시도 기록 생성 실패 (판단 영향 없음): {error}")
        return {"attempt_id": new_id("att"), "role": role, "outcome": "record_error",
                "error": str(error)[:200], "requested_at": _iso(requested_at)}


def decision_record(*, signal_id: Optional[str], symbol: str, strategy: str, score: Any, market_regime: Any,
                    common_context: Optional[str], stage: str, attempts: List[Dict[str, Any]],
                    approved: Optional[bool], result: Dict[str, Any], budget_context: Optional[Dict[str, Any]],
                    decision_available_at: Optional[datetime] = None,
                    collected_at: Optional[datetime] = None) -> Dict[str, Any]:
    """판단 하나의 기록. `common_context` 는 `entry-llm-input-v1` 사실 블록 원문(비밀값 없음)이다."""
    try:
        from ..utils.entry_risk import applied_sha
        code_version = applied_sha()
    except Exception:
        code_version = None
    available = decision_available_at if decision_available_at is not None else now_kst()
    return {
        "schema_version": SCHEMA_VERSION,
        "decision_id": new_id("dec"),
        "decision_kind": "entry_llm_second_check",
        "signal_id": signal_id,
        "symbol": symbol,
        "strategy": strategy,
        "score": float(score) if isinstance(score, (int, float)) else score,
        "market_regime": str(market_regime) if market_regime is not None else None,
        "code_version": code_version,
        "context_version": "entry-llm-input-v1",
        "inputs": {
            "common_context": common_context,
            "common_context_sha256": sha256_text(common_context),
            # 원천 관측 시각은 71차 기준 아직 확보되지 않았다 — 현재 시각으로 채우지 않는다.
            "source_observed_at": None,
            # 공통 입력을 구성한 시각(호출 전). 모르면 None — 판단 완료 시각으로 대체하지 않는다.
            "collected_at": _iso(collected_at),
        },
        "stage": stage,
        "attempts": attempts,
        "result": {"approved": approved, **result},
        "budget_context": budget_context,
        "decision_available_at": _iso(available),
        # 응답 이후 최초 유효 호가·장 상태는 관측 원장과 decision_available_at 으로 오프라인 조인한다.
        "first_quote_after": None,
    }


class DecisionRecorder:
    """유한 큐 + 백그라운드 스레드의 월별 JSONL 기록기. submit 은 디스크 I/O를 기다리지 않는다."""

    def __init__(self, directory: Optional[Path] = None, *, enabled: Optional[bool] = None,
                 max_file_bytes: int = MAX_FILE_BYTES, queue_size: int = QUEUE_SIZE):
        self.directory = Path(directory) if directory is not None else (
            Path.home() / ".cache" / "ai_trader" / "llm_decisions")
        self.enabled = (os.getenv(ENV_FLAG) == "1") if enabled is None else bool(enabled)
        self.max_file_bytes = int(max_file_bytes)
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=int(queue_size))
        self.stats = {"queued": 0, "written": 0, "dropped_queue_full": 0, "dropped_file_cap": 0,
                      "write_errors": 0, "dropped_closed": 0}
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # 판단 ID별 최종 저장 결과(written / dropped_file_cap / write_error) — 최근 1024건
        self._outcomes: "OrderedDict[str, str]" = OrderedDict()

    def status_of(self, decision_id: Optional[str]) -> Optional[str]:
        """판단 ID 의 저장 결과. 아직 큐에 있으면 None. `queued` 는 큐 수락일 뿐 저장 완료가 아니다."""
        with self._lock:
            return self._outcomes.get(decision_id) if decision_id else None

    def _note_outcome(self, record: Dict[str, Any], status: str) -> None:
        key = record.get("decision_id") if isinstance(record, dict) else None
        if not key:
            return
        self._outcomes[key] = status
        while len(self._outcomes) > 1024:
            self._outcomes.popitem(last=False)

    # ── 제출 ───────────────────────────────────────────────
    def submit(self, record: Dict[str, Any]) -> str:
        """큐 수락·비활성·포화·종료 거절 상태를 반환한다. 디스크 I/O는 기다리지 않는다."""
        # 종료 경계와 큐 수락을 같은 락으로 묶어 수락한 기록이 종료 뒤에 남지 않게 한다.
        with self._lock:
            if self._closed.is_set():
                self.stats["dropped_closed"] += 1
                self._note_outcome(record, "dropped_closed")
                status = "dropped_closed"
            elif not self.enabled:
                return "disabled"
            else:
                self._ensure_thread()
                try:
                    self._queue.put_nowait(record)
                except queue.Full:
                    status = "dropped_queue_full"
                else:
                    self.stats["queued"] += 1
                    return "queued"
            if status == "dropped_queue_full":
                self.stats["dropped_queue_full"] += 1
                self._note_outcome(record, status)
        logger.warning(f"[판단기록] 기록 수락 거절: {status} (판단 영향 없음)")
        return status

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="llm-decision-recorder", daemon=True)
        self._thread.start()

    # ── 쓰기 ───────────────────────────────────────────────
    def _path_for(self, record: Dict[str, Any]) -> Path:
        stamp = record.get("decision_available_at")
        if not isinstance(stamp, str) or not re.match(r"^\d{4}-\d{2}-", stamp):
            raise ValueError(f"decision_available_at 가 ISO 시각이 아니다: {stamp!r}")   # write_error 로 집계
        return self.directory / f"decisions-{stamp[:7]}.jsonl"

    def _write(self, record: Dict[str, Any]) -> None:
        path = None
        try:
            path = self._path_for(record)          # 잘못된 시각은 여기서 ValueError → write_error 집계
            # ensure_ascii=True: 응답의 단독 surrogate 등 비정상 문자도 이스케이프돼 인코딩 예외가 없다 (재리뷰 P2)
            line = (json.dumps(record, ensure_ascii=True, default=str) + "\n").encode("utf-8")
            path.parent.mkdir(parents=True, exist_ok=True)
            current = path.stat().st_size if path.exists() else 0
            if current + len(line) > self.max_file_bytes:
                with self._lock:
                    self.stats["dropped_file_cap"] += 1
                    self._note_outcome(record, "dropped_file_cap")
                logger.warning(f"[판단기록] 월 파일 상한 초과로 기록 폐기: {path.name} ({current} bytes)")
                return
            with open(path, "ab") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            with self._lock:
                self.stats["written"] += 1
                self._note_outcome(record, "written")
        except Exception as error:   # 직렬화·인코딩·OS 오류 모두 실패로 집계하고 스레드는 계속 돈다
            with self._lock:
                self.stats["write_errors"] += 1
                self._note_outcome(record, "write_error")
            logger.warning(f"[판단기록] 쓰기 실패 (판단에는 영향 없음): {error}")

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.05)
            except queue.Empty:
                # get의 시간 초과 뒤 종료 경계 전에 마지막 제출이 들어왔을 수 있다.
                if self._closed.is_set() and self._queue.empty():
                    return
                continue
            try:
                self._write(item)
            except Exception as error:  # 어떤 예외도 기록 스레드를 멈추게 하지 않는다
                with self._lock:
                    self.stats["write_errors"] += 1
                    self._note_outcome(item, "write_error")
                logger.warning(f"[판단기록] 기록 처리 예외 (계속): {error}")
            if self._closed.is_set() and self._queue.empty():
                return

    def flush(self, timeout: float = 5.0) -> bool:
        """큐가 비고 쓰기가 끝날 때까지 대기 (시험·종료용). 시간 초과면 False."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.empty():
                with self._lock:
                    done = self.stats["written"] + self.stats["dropped_file_cap"] + self.stats["write_errors"]
                    if done >= self.stats["queued"]:
                        return True
            time.sleep(0.01)
        return False

    def close(self, timeout: float = 5.0) -> bool:
        """제출을 닫고 제한 시간 동안 대기. 수락한 기록 전부 저장·스레드 종료 시만 True.

        시간 초과에도 종료 경계는 유지하고 daemon은 남은 큐를 계속 처리한다.
        반복 호출은 같은 종료를 기다리며 파일 상한/쓰기 실패를 성공으로 바꾸지 않는다.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            self._closed.set()
            worker = self._thread
        if worker is not None:
            worker.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            return (worker is None or not worker.is_alive()) and self.stats["written"] == self.stats["queued"]


_recorder: Optional[DecisionRecorder] = None
_recorder_lock = threading.Lock()


async def close_decision_recorder(timeout: float = 5.0) -> bool:
    """기존 기록기만 닫는다. executor 없이 기다려 느린 디스크가 루프/프로세스 종료를 묶지 않는다."""
    with _recorder_lock:
        recorder = _recorder
    if recorder is None:
        return True
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)
    recorder.close(0)
    while recorder._thread is not None and recorder._thread.is_alive():
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.01, remaining))
    return recorder.close(0)


def get_decision_recorder() -> DecisionRecorder:
    global _recorder
    with _recorder_lock:
        if _recorder is None:
            _recorder = DecisionRecorder()
        return _recorder


def set_decision_recorder(recorder: Optional[DecisionRecorder]) -> None:
    """시험·운영 주입용. None 이면 다음 호출에서 기본 기록기를 다시 만든다."""
    global _recorder
    with _recorder_lock:
        _recorder = recorder


def install_production_recorder() -> DecisionRecorder:
    """운영 런처가 KR 초기화에서 호출 — LLM_DECISION_RECORD 가 "0" 이 아니면 기록을 켠다."""
    recorder = DecisionRecorder(enabled=os.getenv(ENV_FLAG, "1") != "0")
    set_decision_recorder(recorder)
    logger.info(f"[판단기록] llm-decision-v1 기록 {'활성' if recorder.enabled else '비활성'}: {recorder.directory}")
    return recorder
