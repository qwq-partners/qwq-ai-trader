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

import hashlib
import json
import os
import queue
import threading
import uuid
from datetime import datetime
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
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _truncate(text: Optional[str]) -> Optional[str]:
    if not isinstance(text, str):
        return None
    return text if len(text) <= MAX_TEXT_CHARS else text[:MAX_TEXT_CHARS]


def _estimated_cost_usd(response) -> Optional[float]:
    """공급자가 사용량을 보고했고 단가표에 있는 모델만 추정 비용을 낸다. 아니면 None(미확인)."""
    if response is None or not getattr(response, "usage_known", False):
        return None
    try:
        from ..utils.llm_pricing import rate_quote
        quote = rate_quote(getattr(response, "model", None) or "",
                           int(getattr(response, "input_tokens", 0) or 0),
                           int(getattr(response, "output_tokens", 0) or 0))
    except Exception:
        return None
    return None if quote is None else float(quote.cost_usd)


def attempt_record(role: str, provider: Any, requested_at: Optional[datetime], response: Any, *,
                   role_system_sha256: Optional[str], role_suffix: Optional[str],
                   timed_out: bool = False, responded_at: Optional[datetime] = None) -> Dict[str, Any]:
    """LLM 호출 시도 하나의 기록. `response` 는 LLMResponse, Exception, 또는 None(타임아웃·미도달)."""
    exc = response if isinstance(response, BaseException) else None
    resp = None if exc is not None else response
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
    resolved = getattr(resp, "resolved_model", None)
    requested_model = getattr(resp, "requested_model", None) or getattr(resp, "model", None)
    content = _truncate(getattr(resp, "content", None))
    return {
        "attempt_id": new_id("att"),
        "role": role,
        "provider": getattr(provider, "value", str(provider)) if provider is not None else None,
        "requested_at": _iso(requested_at),
        "responded_at": _iso(responded_at if responded_at is not None else now_kst()),
        "latency_ms": getattr(resp, "latency_ms", None),
        "requested_model": requested_model,
        "requested_reasoning_effort": getattr(resp, "requested_reasoning_effort", None),
        "requested_thinking_level": getattr(resp, "requested_thinking_level", None),
        # 공급자가 응답에 모델을 적어 준 경우만 확인된 신원이다. 요청 모델은 신원 증거가 아니다.
        "resolved_model": resolved,
        "model_identity": "provider_reported" if resolved else "unverified",
        "input_tokens": getattr(resp, "input_tokens", None),
        "output_tokens": getattr(resp, "output_tokens", None),
        "reasoning_tokens": getattr(resp, "reasoning_tokens", None),
        "usage_known": getattr(resp, "usage_known", None),
        "billing_status": getattr(resp, "billing_status", None),
        "estimated_cost_usd": _estimated_cost_usd(resp),
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


def decision_record(*, signal_id: Optional[str], symbol: str, strategy: str, score: Any, market_regime: Any,
                    common_context: Optional[str], stage: str, attempts: List[Dict[str, Any]],
                    approved: Optional[bool], result: Dict[str, Any], budget_context: Optional[Dict[str, Any]],
                    decision_available_at: Optional[datetime] = None) -> Dict[str, Any]:
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
            "collected_at": _iso(available),
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
    """유한 큐 + 백그라운드 스레드의 월별 JSONL 기록기. submit 은 절대 블로킹하지 않는다."""

    def __init__(self, directory: Optional[Path] = None, *, enabled: Optional[bool] = None,
                 max_file_bytes: int = MAX_FILE_BYTES, queue_size: int = QUEUE_SIZE):
        self.directory = Path(directory) if directory is not None else (
            Path.home() / ".cache" / "ai_trader" / "llm_decisions")
        self.enabled = (os.getenv(ENV_FLAG) == "1") if enabled is None else bool(enabled)
        self.max_file_bytes = int(max_file_bytes)
        self._queue: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=int(queue_size))
        self.stats = {"queued": 0, "written": 0, "dropped_queue_full": 0, "dropped_file_cap": 0,
                      "write_errors": 0}
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    # ── 제출 ───────────────────────────────────────────────
    def submit(self, record: Dict[str, Any]) -> str:
        """기록을 큐에 넣고 상태 문자열을 돌려준다: queued / disabled / dropped_queue_full."""
        if not self.enabled:
            return "disabled"
        self._ensure_thread()
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            with self._lock:
                self.stats["dropped_queue_full"] += 1
            return "dropped_queue_full"
        with self._lock:
            self.stats["queued"] += 1
        return "queued"

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="llm-decision-recorder", daemon=True)
        self._thread.start()

    # ── 쓰기 ───────────────────────────────────────────────
    def _path_for(self, record: Dict[str, Any]) -> Path:
        stamp = record.get("decision_available_at") or now_kst().isoformat()
        return self.directory / f"decisions-{str(stamp)[:7]}.jsonl"

    def _write(self, record: Dict[str, Any]) -> None:
        path = self._path_for(record)
        line = (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            current = path.stat().st_size if path.exists() else 0
            if current + len(line) > self.max_file_bytes:
                with self._lock:
                    self.stats["dropped_file_cap"] += 1
                return
            with open(path, "ab") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            with self._lock:
                self.stats["written"] += 1
        except OSError as error:
            with self._lock:
                self.stats["write_errors"] += 1
            logger.warning(f"[판단기록] 쓰기 실패 (판단에는 영향 없음): {error}")

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            self._write(item)

    def flush(self, timeout: float = 5.0) -> bool:
        """큐가 비고 쓰기가 끝날 때까지 대기 (시험·종료용). 시간 초과면 False."""
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.empty():
                with self._lock:
                    done = self.stats["written"] + self.stats["dropped_file_cap"] + self.stats["write_errors"]
                    if done >= self.stats["queued"]:
                        return True
            time.sleep(0.01)
        return False

    def close(self, timeout: float = 5.0) -> None:
        self.flush(timeout)
        if self._thread is not None and self._thread.is_alive():
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass


_recorder: Optional[DecisionRecorder] = None
_recorder_lock = threading.Lock()


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
