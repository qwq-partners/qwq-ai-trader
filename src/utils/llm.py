"""
AI Trading Bot - LLM 통합 모듈

GPT general-task routing with an inert legacy Gemini compatibility boundary.

설계 철학:
- 가벼운 작업(테마 탐지, 분류) -> OpenAI light, no fallback
- 중요한 작업(시장 분석, 복기) -> OpenAI heavy
- Heavy failure -> distinct OpenAI light at most once
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable
import aiohttp
from loguru import logger

from .llm_roles import LLMRole, ROLE_POLICY_VERSION, get_role_profile
from .llm_pricing import rate_quote
from .llm_budget import MonthlyBudgetLedger, BudgetBlocked, BudgetStateError
from contextvars import ContextVar

# 64차: complete()/complete_with() 한 호출 동안 실제 공급자 시도(재시도·폴백 포함)를 모아 응답에 붙인다
_ATTEMPT_TRAIL: ContextVar = ContextVar("llm_attempt_trail", default=None)


class LLMProvider(str, Enum):
    """LLM 제공자"""
    OPENAI = "openai"
    GEMINI = "gemini"
    ANTHROPIC = "anthropic"


# Manus로 보내도 되는 태스크 (2026-08-03 Codex용 추가 → 2026-08-05 Manus 전환)
#   Manus는 태스크 기반 에이전트라 응답이 수십 초~수 분 — 실시간 경로에 쓰면 매매가 지연된다.
#   config에서 실수로 quick_*/theme_* 를 넣어도 이 allowlist가 막는다 —
#   YAML 주석은 강제력이 없으므로 코드로 고정한다.
MANUS_ALLOWED_TASKS = frozenset({
    "trade_review",        # 거래 복기 (20:30 진화)
    "strategy_analysis",   # 전략 분석/진화, 주간 복기
    "market_analysis",     # 장전 시장 진단 (배치)
})


class LLMTask(str, Enum):
    """LLM 작업 유형 - 용도별 최적 모델 자동 선택"""
    THEME_DETECTION = "theme_detection"      # 테마 탐지 -> OpenAI light
    STOCK_MAPPING = "stock_mapping"          # 종목 매핑 -> OpenAI light
    NEWS_SUMMARY = "news_summary"            # 뉴스 요약 -> OpenAI light
    MARKET_ANALYSIS = "market_analysis"      # 시장 분석 -> OpenAI heavy
    TRADE_REVIEW = "trade_review"            # 거래 복기 -> OpenAI heavy
    QUICK_CLASSIFY = "quick_classify"        # 빠른 분류 -> OpenAI light
    STRATEGY_ANALYSIS = "strategy_analysis"  # 전략 분석/진화 -> OpenAI heavy
    QUICK_ANALYSIS = "quick_analysis"        # 빠른 실시간 분석 -> OpenAI light
    WIKI_INGEST = "wiki_ingest"              # 위키 교훈 추출 -> OpenAI light
    REPORT_SUMMARY = "report_summary"       # Non-decision summaries; economy eligible.


@dataclass
class LLMConfig:
    """LLM 설정"""
    openai_api_key: str = ""
    gemini_api_key: str = ""  # Deprecated, ignored by the disabled client.
    anthropic_api_key: str = field(default="", repr=False)
    anthropic_api_key_file: str = ""
    claude_model: str = "claude-opus-5-5"
    claude_cli_enabled: bool = True
    claude_cli_command: str = "claude"

    # 모델 선택
    # Heavy: 깊은 분석, 거래 복기에 사용 (GPT-5.4 최신 모델)
    # 2026-08-02: gpt-5.4 → gpt-5.6-sol (gpt-5.4 deprecated 예정, sol은 일반 API 동작 확인)
    openai_model_heavy: str = "gpt-5.6-sol"
    # Light: 빠른 분류, 테마 탐지에 사용
    openai_model_light: str = "gpt-5-mini"
    # Inert compatibility labels for legacy callers and historical audit rows.
    gemini_model_heavy: str = "gemini-3.1-pro-preview"
    gemini_model_light: str = "gemini-3.1-flash-lite"

    # 2026-06-17 Phase 3: Shadow A/B — light 후속 모델 비교용 (deprecation 마이그레이션)
    # 빈 문자열이면 비활성, 값이 있으면 openai_model_light 호출 직후 동일 프롬프트로 shadow 호출
    openai_model_light_shadow: str = ""

    # ── Manus API 라우팅 (2026-08-05, Codex CLI 대체) ──────────
    # 배치성 작업을 OpenAI API 대신 Manus 에이전트 API로 처리한다.
    # 태스크 기반 비동기 API라 응답이 수십 초~수 분 — 실시간 매매 경로에는 쓰지 않는다.
    # 실패하면 기존 API 경로로 자동 폴백한다.
    manus_enabled: bool = False
    manus_agent_profile: str = "manus-1.6"
    manus_timeout_sec: float = 600.0
    # Manus로 보낼 LLMTask 값들 (배치성만)
    manus_tasks: tuple = ("trade_review", "strategy_analysis")

    # 타임아웃 (Thinking 모델은 추론에 시간이 걸림)
    timeout_seconds: int = 120

    # 토큰 제한 (max_completion_tokens는 reasoning + 응답 합산)
    max_input_tokens: int = 4000
    max_output_tokens: int = 16000

    # 비용 관리
    daily_budget_usd: float = 5.0  # 일일 예산
    monthly_budget_usd: Optional[float] = None  # Explicit activation only.
    budget_state_path: Optional[str] = None
    budget_policy_enabled: bool = False
    missing_cost_policy: str = "block"

    def __post_init__(self):
        if not isinstance(self.claude_cli_enabled, bool):
            raise ValueError("claude_cli_enabled must be boolean")
        if self.claude_model != "claude-opus-5-5":
            raise ValueError("Unsupported Claude model identity")
        if not isinstance(self.claude_cli_command, str) or not self.claude_cli_command.strip():
            raise ValueError("claude_cli_command must be an executable name or path")
        if (not isinstance(self.anthropic_api_key_file, str) or
                (self.anthropic_api_key_file and not Path(self.anthropic_api_key_file).expanduser().is_absolute())):
            raise ValueError("anthropic_api_key_file must be absolute or home-relative")
        if (not isinstance(self.missing_cost_policy, str)
                or self.missing_cost_policy not in {"block", "log_and_continue"}):
            raise ValueError("llm.missing_cost_policy must be block or log_and_continue")
        if not isinstance(self.budget_policy_enabled, bool):
            raise ValueError("llm.budget_policy_enabled must be boolean")
        if self.budget_policy_enabled and self.monthly_budget_usd is None:
            raise ValueError("llm.budget_policy_enabled requires monthly_budget")
        if (isinstance(self.daily_budget_usd, bool)
                or not isinstance(self.daily_budget_usd, (int, float))
                or not math.isfinite(self.daily_budget_usd)
                or self.daily_budget_usd < 0):
            raise ValueError("llm.daily_budget must be a finite nonnegative USD number")
        if self.monthly_budget_usd is not None and (
                isinstance(self.monthly_budget_usd, bool)
                or not isinstance(self.monthly_budget_usd, (int, float))
                or not math.isfinite(self.monthly_budget_usd) or self.monthly_budget_usd < 0):
            raise ValueError("llm.monthly_budget must be a finite nonnegative USD number")
        if self.monthly_budget_usd is not None and self.budget_state_path is not None:
            if (not isinstance(self.budget_state_path, str) or not self.budget_state_path.strip()
                    or not Path(self.budget_state_path).expanduser().is_absolute()):
                raise ValueError("llm.budget_state_path must be an explicit absolute or home-relative path")

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            anthropic_api_key_file=os.getenv("ANTHROPIC_API_KEY_FILE", "~/.config/qwq-ai-trader/anthropic-api-key"),
            claude_cli_command=os.getenv("CLAUDE_CLI_COMMAND", "claude"),
        )

    @classmethod
    def from_config(cls, cfg: Optional[Dict[str, Any]] = None) -> "LLMConfig":
        """env(API 키) + YAML(모델명) 결합 로드.

        2026-06-16: GPT-5 snapshot deprecation 대비 — 모델명을 config로 빼서
        코드 수정 없이 evolved_overrides.yml에서 교체 가능하게 함.

        cfg는 default.yml/evolved_overrides 머지된 dict 또는 그 안의 `llm:` 섹션.
        모델 키가 없거나 빈 문자열이면 dataclass 기본값 유지.
        daily_budget is USD; primary/fallback are legacy informational labels,
        not overrides for TASK_CONFIG routes.
        """
        llm_cfg = cfg or {}
        if "llm" in llm_cfg and isinstance(llm_cfg["llm"], dict):
            llm_cfg = llm_cfg["llm"]
        return cls(
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            anthropic_api_key_file=os.getenv("ANTHROPIC_API_KEY_FILE", "~/.config/qwq-ai-trader/anthropic-api-key"),
            claude_model=llm_cfg.get("claude_model", "claude-opus-5-5"),
            claude_cli_enabled=llm_cfg.get("claude_cli_enabled", True),
            claude_cli_command=os.getenv("CLAUDE_CLI_COMMAND", "claude"),
            daily_budget_usd=llm_cfg.get("daily_budget", 5.0),
            monthly_budget_usd=llm_cfg.get("monthly_budget"),
            budget_state_path=llm_cfg.get("budget_state_path"),
            budget_policy_enabled=llm_cfg.get("budget_policy_enabled", False),
            missing_cost_policy=llm_cfg.get("missing_cost_policy", "block"),
            openai_model_heavy=(llm_cfg.get("openai_model_heavy") or "gpt-5.6-sol"),
            openai_model_light=(llm_cfg.get("openai_model_light") or "gpt-5-mini"),
            openai_model_light_shadow=(llm_cfg.get("openai_model_light_shadow") or ""),
            manus_enabled=bool((llm_cfg.get("manus") or {}).get("enabled", False)),
            manus_agent_profile=(
                (llm_cfg.get("manus") or {}).get("agent_profile") or "manus-1.6"
            ),
            manus_timeout_sec=float((llm_cfg.get("manus") or {}).get("timeout_sec", 600.0)),
            manus_tasks=tuple(
                (llm_cfg.get("manus") or {}).get("tasks")
                or ("trade_review", "strategy_analysis")
            ),
        )


@dataclass
class LLMUsage:
    """토큰 사용량 추적"""
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0

    cost_missing: bool = False
    unpriced_requests: int = 0
    unknown_usage_requests: int = 0
    bounded_unknown_requests: int = 0
    uncertain_cost_ceiling_usd: float = 0.0
    inferred_nonbillable_rejections: int = 0

    @property
    def unbounded_cost_missing(self) -> bool:
        return self.cost_missing and (
            self.unpriced_requests > 0 or self.bounded_unknown_requests == 0
            or self.unknown_usage_requests != self.bounded_unknown_requests)

    def add(self, input_tokens: int, output_tokens: int, model: str,
            *, usage_known: bool = True, uncertain_cost_ceiling_usd: Optional[float] = None):
        """Add every sent attempt. Known subtotal stays a float, never a full bill."""
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        quote = rate_quote(model, input_tokens, output_tokens)
        if quote is None:
            self.unpriced_requests += 1
            self.cost_missing = True
        elif usage_known:
            self.estimated_cost_usd += quote.cost_usd
        if not usage_known:
            self.unknown_usage_requests += 1
            self.cost_missing = True
            if (quote is not None and isinstance(uncertain_cost_ceiling_usd, (int, float))
                    and not isinstance(uncertain_cost_ceiling_usd, bool)
                    and math.isfinite(uncertain_cost_ceiling_usd) and uncertain_cost_ceiling_usd > 0):
                self.bounded_unknown_requests += 1
                self.uncertain_cost_ceiling_usd += uncertain_cost_ceiling_usd


@dataclass
class LLMResponse:
    """LLM 응답"""
    content: str
    model: str
    provider: LLMProvider
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    success: bool = True
    error: Optional[str] = None
    requested_model: Optional[str] = None
    resolved_model: Optional[str] = None  # Only provider-reported model/modelVersion.
    requested_reasoning_effort: Optional[str] = None
    requested_thinking_level: Optional[str] = None
    reasoning_tokens: Optional[int] = None
    visible_output_tokens: Optional[int] = None
    finish_reason: Optional[str] = None
    usage_known: bool = True  # Legacy manually constructed responses remain compatible.
    request_sent: bool = True
    billing_status: str = "reported_usage"
    http_status: Optional[int] = None
    provider_error_code: Optional[str] = None
    provider_error_type: Optional[str] = None
    provider_error_status: Optional[str] = None
    budget_block_reason: Optional[str] = None
    budget_policy_mode: Optional[str] = None
    budget_policy_version: Optional[str] = None
    budget_priority: Optional[str] = None
    budget_reservation_token: Optional[str] = None  # 64차: 월 예산 원장 reservations.token 연결 (정산 시 기록)
    attempt_trail: Optional[list] = None  # 64차: 이 응답을 만들기까지의 실제 공급자 시도 목록(재시도·폴백 각 1행)
    refusal_detected: bool = False  # 공급자가 명시한 거절 여부만; 본문은 저장하지 않는다.
    transport_error_kind: Optional[str] = None
    uncertain_cost_ceiling_usd: Optional[float] = None  # Durable hold, never actual spend.
    missing_cost_policy: str = "block"
    cost_accounting_skipped: bool = False
    transport: Optional[str] = None
    billing_channel: str = "api"
    model_identity_status: Optional[str] = None
    subscription_estimated_cost_usd: Optional[float] = None
    actual_incremental_cost_usd: Optional[float] = None
    requested_max_tokens: Optional[int] = None


_AUDIT_FINISH_REASONS = frozenset({
    "end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal",
    "stop", "length", "content_filter", "STOP", "MAX_TOKENS", "SAFETY",
    "RECITATION", "OTHER",
})


def _billing_status(data: dict, status: int, usage_known: bool) -> str:
    if usage_known:
        return "reported_usage"
    # Explicit availability policy inference, NOT provider-reported billed zero.
    # A present malformed usage/output field invalidates the rejection inference.
    if (status in {400, 401, 403, 404, 413, 422, 429}
            and isinstance(data.get("error"), dict) and data["error"]
            and not any(key in data for key in
                        ("usage", "usageMetadata", "choices", "candidates", "output", "content"))):
        return "rejected_before_generation"
    return "unknown"


# Validated 2026-10-12 against https://developers.openai.com/api/docs/models
# and https://developers.openai.com/api/docs/guides/reasoning
_OPENAI_EFFORTS = {
    "gpt-5-mini": {"minimal", "low", "medium", "high"},
    "gpt-5.6-sol": {"none", "low", "medium", "high", "xhigh", "max"},
    "gpt-6-astra": {"low", "medium", "high", "xhigh", "max"},
    "gpt-6.1-sol": {"low", "medium", "high", "xhigh", "max"},
}
# Validated 2026-10-12: https://ai.google.dev/gemini-api/docs/generate-content/gemini-3
# Gemini 3.8: https://ai.google.dev/gemini-api/docs/generate-content/latest-model
_GEMINI_LEVELS = {
    "gemini-3.1-pro-preview": {"low", "medium", "high"},
    "gemini-3.1-flash-lite": {"minimal", "low", "medium", "high"},
    "gemini-3.1-flash-lite-preview": {"minimal", "low", "medium", "high"},
    "gemini-3.8-flash": {"low", "medium", "high"},
    "gemini-4-argon": {"low", "medium", "high"},
}


def _validate_controls(model: str, provider: LLMProvider, controls: dict) -> None:
    # Public contract: invalid explicit controls raise ValueError before any HTTP.
    effort, level = controls.get("reasoning_effort"), controls.get("thinking_level")
    seed = controls.get("seed")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)
                             or not -(2 ** 63) <= seed < 2 ** 63):
        raise ValueError("seed must be a signed 64-bit integer")
    if any(controls.get(key) is not None for key in ("thinking_budget", "thinkingConfig", "thinking_config")):
        raise ValueError("Unsupported thinking control; use explicit thinking_level")
    if provider == LLMProvider.OPENAI:
        if level is not None:
            raise ValueError("thinking_level is not supported by OpenAI transport")
        if effort is not None and (not isinstance(effort, str) or effort not in _OPENAI_EFFORTS.get(model, set())):
            raise ValueError("Unsupported reasoning_effort for requested OpenAI model")
    else:
        if effort is not None:
            raise ValueError("reasoning_effort is not supported by Gemini transport")
        if level is not None and (not isinstance(level, str) or level not in _GEMINI_LEVELS.get(model, set())):
            raise ValueError("Unsupported thinking_level for requested Gemini model")


def _valid_token_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _token_count(value: Any) -> int:
    return value if _valid_token_count(value) else 0


def _known_usage(usage: dict, input_key: str, output_key: str) -> bool:
    return _valid_token_count(usage.get(input_key)) and _valid_token_count(usage.get(output_key))


_PROVIDER_ERROR_LABELS = frozenset({
    "invalid_api_key", "insufficient_quota", "rate_limit_exceeded", "model_not_found",
    "invalid_request_error", "authentication_error", "rate_limit_error", "server_error",
    "permission_error", "context_length_exceeded", "content_policy_violation",
    "INVALID_ARGUMENT", "UNAUTHENTICATED", "PERMISSION_DENIED", "NOT_FOUND",
    "RESOURCE_EXHAUSTED", "FAILED_PRECONDITION", "INTERNAL", "UNAVAILABLE",
    "DEADLINE_EXCEEDED",
})


def _provider_errors(data: dict) -> dict:
    error = data.get("error")
    if not isinstance(error, dict):
        return {}
    result = {}
    for key in ("code", "type", "status"):
        value = error.get(key)
        if value is not None:
            result[f"provider_error_{key}"] = (
                value if isinstance(value, str) and value in _PROVIDER_ERROR_LABELS
                else "unknown_provider_error"
            )
    return result


def _gemini_usage(data: dict, usage: dict) -> dict:
    """Proto3 int32 omits zeros; infer them only with an evidenced response shape.

    2026-10-12: https://ai.google.dev/api/generate-content#UsageMetadata
    totalTokenCount = prompt + candidates + thoughts. Malformed present values
    never become zero and contradictory totals leave accounting incomplete.
    """
    candidates = data.get("candidates") or []
    no_visible_parts = isinstance(candidates, list)
    for candidate in candidates if isinstance(candidates, list) else []:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("content", {}), dict):
            no_visible_parts = False
            break
        parts = candidate.get("content", {}).get("parts", [])
        if not isinstance(parts, list) or any(
                not isinstance(part, dict) or (not part.get("thought") and bool(part.get("text")))
                for part in parts):
            no_visible_parts = False
    feedback = data.get("promptFeedback") or {}
    block_reason = feedback.get("blockReason") if isinstance(feedback, dict) else None
    blocked = (not candidates and isinstance(block_reason, str) and block_reason in {
        "SAFETY", "OTHER", "BLOCKLIST", "PROHIBITED_CONTENT", "IMAGE_SAFETY",
    })
    valid_present = all(_valid_token_count(usage[key]) for key in
                        ("promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount", "totalTokenCount")
                        if key in usage)
    prompt_valid = _valid_token_count(usage.get("promptTokenCount"))
    visible_known = ("candidatesTokenCount" in usage or
                     (no_visible_parts and ("thoughtsTokenCount" in usage or blocked)))
    known = prompt_valid and valid_present and visible_known
    input_count = _token_count(usage.get("promptTokenCount"))
    visible = _token_count(usage.get("candidatesTokenCount"))
    thoughts = _token_count(usage.get("thoughtsTokenCount"))
    if "totalTokenCount" in usage and usage["totalTokenCount"] != input_count + visible + thoughts:
        known = False
    return {
        "usage_known": known,
        "visible_output_tokens": visible if known else usage.get("candidatesTokenCount"),
        "reasoning_tokens": thoughts if known else usage.get("thoughtsTokenCount"),
    }


def _extract_json(text: str) -> dict:
    """LLM 응답에서 JSON을 robust하게 추출"""
    text = text.strip()
    # 1. 마크다운 코드 블록 처리
    if "```" in text:
        blocks = re.findall(r'```(?:json)?\s*([\s\S]*?)```', text)
        if blocks:
            text = blocks[0].strip()
    # 2. 첫 번째 { ... } 또는 [ ... ] 블록 추출
    if not text.startswith(("{", "[")):
        match = re.search(r'(\{[\s\S]*\}|\[[\s\S]*\])', text)
        if match:
            text = match.group(1)
    return json.loads(text)


class BaseLLMClient(ABC):
    """LLM 클라이언트 기본 클래스"""

    @abstractmethod
    async def complete(self, prompt: str, system: str = "", **kwargs) -> LLMResponse:
        pass

    @abstractmethod
    async def complete_json(self, prompt: str, system: str = "", **kwargs) -> Dict[str, Any]:
        pass


class OpenAIClient(BaseLLMClient):
    """OpenAI 클라이언트"""

    def __init__(self, config: LLMConfig):
        self.config = config
        self.api_key = config.openai_api_key
        self.base_url = "https://api.openai.com/v1"
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def complete(
        self,
        prompt: str,
        system: str = "",
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> LLMResponse:
        """Construct the actual request; reject unsupported explicit controls locally."""
        model = model if model is not None else self.config.openai_model_light
        effort = kwargs.get("reasoning_effort")
        _validate_controls(model, LLMProvider.OPENAI, kwargs)
        metadata = dict(requested_model=model, requested_reasoning_effort=effort,
                        usage_known=False, request_sent=False, billing_status="not_sent")
        if not self.api_key:
            return LLMResponse("", model, LLMProvider.OPENAI, success=False,
                               error="OpenAI API key not configured", **metadata)
        body = {"model": model, "messages": [],
                "max_completion_tokens": max_tokens if max_tokens is not None else self.config.max_output_tokens}
        if system:
            body["messages"].append({"role": "system", "content": system})
        body["messages"].append({"role": "user", "content": prompt})
        if not model.startswith(("gpt-5", "gpt-6")):
            body["temperature"] = temperature
        if effort is not None:
            body["reasoning_effort"] = effort
        if kwargs.get("seed") is not None:
            body["seed"] = int(kwargs["seed"])
        start_time = time.perf_counter()
        input_count = output_count = 0
        try:
            session = await self._get_session()
            metadata["request_sent"] = True
            metadata["billing_status"] = "unknown"
            async with session.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=body,
            ) as resp:
                metadata["http_status"] = resp.status
                data = await resp.json()
                if not isinstance(data, dict):
                    raise ValueError("Provider response must be an object")
                metadata.update(_provider_errors(data))
                usage = data.get("usage") or {}
                if not isinstance(usage, dict):
                    usage = {}
                input_count = _token_count(usage.get("prompt_tokens"))
                output_count = _token_count(usage.get("completion_tokens"))
                details = usage.get("completion_tokens_details")
                reasoning_tokens = details.get("reasoning_tokens") if isinstance(details, dict) else None
                metadata.update(
                    resolved_model=data.get("model"),
                    usage_known=_known_usage(usage, "prompt_tokens", "completion_tokens"),
                    reasoning_tokens=reasoning_tokens if _valid_token_count(reasoning_tokens) else None,
                )
                metadata["billing_status"] = _billing_status(data, resp.status, metadata["usage_known"])
                choices = data.get("choices") or []
                choice = choices[0] if choices else {}
                metadata["finish_reason"] = choice.get("finish_reason")
                message = choice.get("message") or {}
                metadata["refusal_detected"] = bool(message.get("refusal"))
                content = message.get("content") or ""
                if not isinstance(content, str):
                    raise ValueError("Provider content must be text")
                success = resp.status == 200 and bool(choices)
                return LLMResponse(
                    content, model, LLMProvider.OPENAI,
                    input_tokens=_token_count(usage.get("prompt_tokens")),
                    output_tokens=_token_count(usage.get("completion_tokens")),
                    latency_ms=(time.perf_counter() - start_time) * 1000,
                    success=success,
                    error=None if success else f"OpenAI HTTP {resp.status} or missing choices",
                    **metadata,
                )
        except asyncio.CancelledError as exc:
            # Preserve the original exception type: asyncio.timeout checks it exactly.
            exc._llm_response = LLMResponse(
                "", model, LLMProvider.OPENAI, success=False,
                input_tokens=input_count, output_tokens=output_count,
                error="Request cancelled",
                latency_ms=(time.perf_counter() - start_time) * 1000,
                **metadata,
            )
            raise
        except Exception as exc:
            # Connector errors prove that no connection was established. Generic
            # socket errors, response loss and cancellation can follow a send.
            if isinstance(exc, aiohttp.ClientConnectorError) and metadata.get("http_status") is None:
                metadata.update(request_sent=False, billing_status="not_sent")
            return LLMResponse(
                "", model, LLMProvider.OPENAI, success=False,
                input_tokens=input_count, output_tokens=output_count,
                error="Request timeout" if isinstance(exc, asyncio.TimeoutError) else "OpenAI transport failure",
                latency_ms=(time.perf_counter() - start_time) * 1000,
                **metadata,
            )

    async def complete_json(
        self,
        prompt: str,
        system: str = "",
        **kwargs
    ) -> Dict[str, Any]:
        """JSON 응답 생성"""
        system = system or "You must respond with valid JSON only. No markdown, no explanation."
        response = await self.complete(prompt, system, **kwargs)

        if not response.success:
            return {"error": response.error}

        try:
            return _extract_json(response.content)
        except (json.JSONDecodeError, ValueError):
            return {"error": "Invalid JSON response", "raw": response.content}


def _disabled_gemini_response(model: str) -> LLMResponse:
    """Stable unsent contract for retired provider calls; never admits spend."""
    return LLMResponse(
        "", model, LLMProvider.GEMINI, success=False, requested_model=model,
        error="Provider disabled by user policy", provider_error_code="provider_disabled",
        request_sent=False, usage_known=False, billing_status="not_sent")


class GeminiClient(BaseLLMClient):
    """Deprecated inert compatibility client. Historical records remain readable."""

    def __init__(self, config: LLMConfig):
        self.config = config
        self._session = None

    async def complete(
        self, prompt: str, system: str = "", model: Optional[str] = None,
        temperature: float = 0.3, max_tokens: Optional[int] = None, **kwargs,
    ) -> LLMResponse:
        return _disabled_gemini_response(
            model if model is not None else self.config.gemini_model_light)

    async def complete_json(
        self,
        prompt: str,
        system: str = "",
        **kwargs
    ) -> Dict[str, Any]:
        """JSON 응답 생성"""
        system = system or "응답은 반드시 유효한 JSON 형식으로만 해주세요. 마크다운이나 설명 없이 JSON만 출력하세요."
        response = await self.complete(prompt, system, **kwargs)

        if not response.success:
            return {"error": response.error}

        try:
            return _extract_json(response.content)
        except (json.JSONDecodeError, ValueError):
            return {"error": "Invalid JSON response", "raw": response.content}


class LLMManager:
    """
    LLM 통합 관리자

    용도별 최적 모델을 자동 선택하고 폴백을 처리합니다.
    """

    # 작업별 최적 설정
    TASK_CONFIG = {
        LLMTask.REPORT_SUMMARY: {
            "primary": (LLMProvider.OPENAI, "heavy"),
            "fallback": (LLMProvider.OPENAI, "light"),
        },
        LLMTask.THEME_DETECTION: {
            "primary": (LLMProvider.OPENAI, "light"),
            "fallback": None,
        },
        LLMTask.STOCK_MAPPING: {
            "primary": (LLMProvider.OPENAI, "light"),
            "fallback": None,
        },
        LLMTask.NEWS_SUMMARY: {
            "primary": (LLMProvider.OPENAI, "light"),
            "fallback": None,
        },
        LLMTask.MARKET_ANALYSIS: {
            "primary": (LLMProvider.OPENAI, "heavy"),
            "fallback": (LLMProvider.OPENAI, "light"),
        },
        LLMTask.TRADE_REVIEW: {
            "primary": (LLMProvider.OPENAI, "heavy"),
            "fallback": (LLMProvider.OPENAI, "light"),
        },
        LLMTask.QUICK_CLASSIFY: {
            "primary": (LLMProvider.OPENAI, "light"),
            "fallback": None,
        },
        LLMTask.STRATEGY_ANALYSIS: {
            "primary": (LLMProvider.OPENAI, "heavy"),
            "fallback": (LLMProvider.OPENAI, "light"),
        },
        LLMTask.QUICK_ANALYSIS: {
            "primary": (LLMProvider.OPENAI, "light"),
            "fallback": None,
        },
        LLMTask.WIKI_INGEST: {
            "primary": (LLMProvider.OPENAI, "light"),
            "fallback": None,
        },
    }

    # LLM 응답 로그 경로
    _LLM_LOG_DIR = Path.home() / ".cache" / "ai_trader" / "llm_responses"
    _LLM_LOG_RETENTION_DAYS = 7
    _CLOSE_GRACE_SECONDS = 5.0
    _CLOSE_FORCE_SECONDS = 1.0
    _CLOSE_RESOURCE_SECONDS = 1.0
    # 2026-06-17 Phase 3: Shadow A/B 비교 로그 (light 후속 모델 마이그레이션)
    _SHADOW_LOG_DIR = Path.home() / ".cache" / "ai_trader" / "llm_shadow"

    def __init__(self, config: Optional[LLMConfig] = None, *, audit_dir: Optional[Path] = None):
        self.audit_dir = Path(audit_dir) if audit_dir is not None else None
        self._audit_log_date: Optional[str] = None
        self.config = config or LLMConfig.from_env()
        self.monthly_budget: Optional[MonthlyBudgetLedger] = None
        self._monthly_budget_error: Optional[str] = None
        if self.config.monthly_budget_usd is not None:
            try:
                if not isinstance(self.config.budget_state_path, str) or not self.config.budget_state_path.strip():
                    raise BudgetStateError("missing_budget_state_path")
                self.monthly_budget = MonthlyBudgetLedger(
                    self.config.budget_state_path, self.config.monthly_budget_usd,
                    policy_enabled=self.config.budget_policy_enabled,
                    missing_cost_policy=self.config.missing_cost_policy,
                    economy_summary_models=(self.config.openai_model_light,),
                )
            except BudgetBlocked as exc:
                self._monthly_budget_error = exc.reason
                logger.error("[LLM] monthly budget storage unavailable; paid calls blocked")
        self._shadow_tasks: set[asyncio.Task] = set()
        self._attempt_tasks: set[asyncio.Task] = set()
        self._close_task: Optional[asyncio.Task] = None
        self._resource_close_task: Optional[asyncio.Task] = None
        self._closing = False
        self.configuration_warnings = []
        for field_name in ("openai_model_heavy", "openai_model_light", "openai_model_light_shadow"):
            model = getattr(self.config, field_name)
            if field_name == "openai_model_light_shadow" and model == "":
                continue
            if not isinstance(model, str) or not model.strip():
                warning = f"{field_name}:invalid_model"
            elif rate_quote(model, 0, 0) is None:
                warning = f"{field_name}:unpriced_model"
            elif model not in _OPENAI_EFFORTS:
                warning = f"{field_name}:unsupported_provider_controls"
            else:
                continue
            self.configuration_warnings.append(warning)
            logger.warning(f"[LLM] configuration preflight: {warning}")

        self.openai = OpenAIClient(self.config)
        self.gemini = GeminiClient(self.config)
        # Lazy construction: OpenAI-only callers neither inspect subscription
        # authentication nor open the API credential file.
        self.claude_cli = None
        self.anthropic = None
        self.requires_independent_review = True
        self._claude_transport_cooldowns = {}
        self._subscription_usage = {"attempts": 0, "sent": 0, "input_tokens": 0,
                                    "output_tokens": 0, "list_cost_estimate_usd": 0.0,
                                    "actual_incremental_cost_usd": None,
                                    "scope": "process_lifetime_subscription_not_api_bill"}

        # 사용량 추적
        self.daily_usage = LLMUsage()
        self._usage_reset_date = datetime.now().date()

        # LLM 로그 상태
        self._llm_log_date: Optional[str] = None

        # 통계
        self.stats = {
            "total_requests": 0,
            "success_count": 0,
            "fallback_count": 0,
            "error_count": 0,
        }

        logger.info(
            f"LLMManager 초기화: OpenAI={'설정됨' if self.config.openai_api_key else '없음'}"
        )

    @staticmethod
    def provider_policy_block_reason(provider: LLMProvider) -> Optional[str]:
        """Pure policy preflight: independent of credentials, spend and HTTP."""
        return "provider_disabled" if provider == LLMProvider.GEMINI else None

    def _get_client(self, provider: LLMProvider) -> BaseLLMClient:
        """제공자별 클라이언트 반환"""
        if provider == LLMProvider.OPENAI:
            return self.openai
        if provider == LLMProvider.GEMINI:
            return self.gemini
        if provider == LLMProvider.ANTHROPIC:
            self._ensure_claude_clients()
            return self.claude_cli
        raise ValueError(f"Unsupported LLM provider: {provider}")

    def _get_model(self, provider: LLMProvider, weight: str) -> str:
        """제공자와 가중치에 따른 모델명 반환"""
        if provider == LLMProvider.OPENAI:
            return self.config.openai_model_heavy if weight == "heavy" else self.config.openai_model_light
        if provider == LLMProvider.GEMINI:
            return self.config.gemini_model_heavy if weight == "heavy" else self.config.gemini_model_light
        if provider == LLMProvider.ANTHROPIC:
            return self.config.claude_model
        raise ValueError(f"Unsupported LLM provider: {provider}")

    def _ensure_claude_clients(self):
        if self.claude_cli is None or self.anthropic is None:
            from .claude_transport import ClaudeCLIClient, AnthropicAPIClient
            if self.claude_cli is None:
                self.claude_cli = ClaudeCLIClient(self.config)
            if self.anthropic is None:
                self.anthropic = AnthropicAPIClient(self.config)

    def _client_provider(self, client):
        if client is self.openai:
            return LLMProvider.OPENAI
        if client is self.gemini:
            return LLMProvider.GEMINI
        if client is not None and (client is self.claude_cli or client is self.anthropic):
            return LLMProvider.ANTHROPIC
        raise ValueError("Unregistered LLM client")

    def _stamp_transport(self, response, client):
        # Trust the registered transport, never a model/response claim of free use.
        if client is not None and client is self.claude_cli:
            response.provider = LLMProvider.ANTHROPIC
            response.transport = "claude_cli"
            response.billing_channel = "subscription"
        elif client is not None and client is self.anthropic:
            response.provider = LLMProvider.ANTHROPIC
            response.transport = "anthropic_messages"
            response.billing_channel = "api"
        else:
            response.billing_channel = "api"
        return response

    @staticmethod
    def _task_controls(model: str, weight: str, controls: dict) -> dict:
        """Bound common light reasoning without changing explicit role controls."""
        result = dict(controls)
        if weight == "light" and result.get("reasoning_effort") is None:
            supported = _OPENAI_EFFORTS.get(model, set())
            for effort in ("minimal", "low"):
                if effort in supported:
                    result["reasoning_effort"] = effort
                    break
        return result

    def _daily_unknown_blocks(self) -> bool:
        return (self.config.missing_cost_policy == "block"
                and self.daily_usage.unbounded_cost_missing)

    @staticmethod
    def _trail_has_unknown_cost(trail: Optional[list]) -> bool:
        return any(item.get("uncertain_cost_ceiling_usd") is not None
                   or (item.get("request_sent") and item.get("usage_known") is False
                       and item.get("billing_status") != "rejected_before_generation")
                   for item in (trail or []))

    def _check_budget(self) -> bool:
        """Process-local post-usage gate, not a strict global spending cap.

        Concurrent/in-flight calls, unknown timeout charges and process restarts
        prevent strict enforcement. The explicit missing-cost policy controls
        unknown amounts; certified ceilings remain occupied alongside known cost.
        """
        # 날짜가 바뀌면 리셋
        today = datetime.now().date()
        if today > self._usage_reset_date:
            self.daily_usage = LLMUsage()
            self._usage_reset_date = today

        return (not self._daily_unknown_blocks()
                and (self.daily_usage.estimated_cost_usd + self.daily_usage.uncertain_cost_ceiling_usd)
                < self.config.daily_budget_usd)

    async def complete_role(
        self, prompt: str, role: LLMRole | str, system: str = "",
    ) -> LLMResponse:
        """고정 역할을 한 번만 호출한다. 분류·폴백·빈 응답/JSON 재시도는 없다.

        호출자가 모델/출력량/우선순위/시한을 덮어쓸 수 없다. 역할 이름은 공통
        원장·감사·시도 이력의 task로 남아 기존 task별 절약 라우팅과 구분된다.
        """
        role, profile = get_role_profile(role)
        provider = LLMProvider(profile.provider)
        controls = profile.controls()
        if rate_quote(profile.model, 0, 0) is None:
            raise ValueError("Unpriced LLM role model")
        _validate_controls(profile.model, provider, controls)
        if not isinstance(prompt, str) or not isinstance(system, str):
            raise ValueError("Role prompt and system must be text")

        token = _ATTEMPT_TRAIL.set([])
        self.stats["total_requests"] += 1
        try:
            if self._closing:
                response = self._monthly_blocked_response(provider, profile.model, "manager_closing")
            elif not self._check_budget():
                reason = "daily_unknown_cost" if self._daily_unknown_blocks() else "daily_limit_exceeded"
                response = self._monthly_blocked_response(provider, profile.model, reason)
                response.error = "Daily budget exceeded"
            elif len(prompt.encode("utf-8")) + len(system.encode("utf-8")) > profile.max_input_bytes:
                response = LLMResponse(
                    "", profile.model, provider, success=False, requested_model=profile.model,
                    error="Role input byte limit exceeded", request_sent=False,
                    usage_known=False, billing_status="not_sent")
            else:
                deadline = asyncio.get_running_loop().time() + profile.timeout_seconds
                try:
                    async with asyncio.timeout_at(deadline):
                        response = await self._complete_attempt(
                            self._get_client(provider), prompt, system, profile.model,
                            role.value, "primary", _deadline=deadline,
                            _role_finish_reason="stop" if provider == LLMProvider.OPENAI else "STOP",
                            budget_priority=profile.budget_priority, **controls)
                except TimeoutError as exc:
                    # timeout이 변환한 취소 원인에는 공통 정산 경계의 응답이 남아 있다.
                    response = getattr(exc.__cause__, "_llm_response", None)
                    if response is None:
                        response = LLMResponse(
                            "", profile.model, provider, success=False, requested_model=profile.model,
                            error="Role deadline exceeded", request_sent=False,
                            usage_known=False, billing_status="not_sent")
                    response.error = "Role deadline exceeded"
            response.budget_priority = profile.budget_priority
            self.stats["success_count" if response.success else "error_count"] += 1
        except BaseException as exc:
            self._attach_trail_to_exception(exc, _ATTEMPT_TRAIL.get())
            raise
        finally:
            trail = _ATTEMPT_TRAIL.get()
            _ATTEMPT_TRAIL.reset(token)
        self._attach_trail(response, trail)
        return response

    async def complete(
        self,
        prompt: str,
        task: LLMTask = LLMTask.QUICK_CLASSIFY,
        system: str = "",
        **kwargs
    ) -> LLMResponse:
        """complete_impl 을 감싸 이 호출의 실제 시도 trail 을 응답에 붙인다 (64차, 동작 불변)."""
        token = _ATTEMPT_TRAIL.set([])
        try:
            response = await self._complete_impl(prompt, task, system, **kwargs)
        except BaseException as exc:
            self._attach_trail_to_exception(exc, _ATTEMPT_TRAIL.get())
            raise
        finally:
            trail = _ATTEMPT_TRAIL.get()
            _ATTEMPT_TRAIL.reset(token)
        self._attach_trail(response, trail)
        return response

    async def _complete_impl(
        self,
        prompt: str,
        task: LLMTask = LLMTask.QUICK_CLASSIFY,
        system: str = "",
        **kwargs
    ) -> LLMResponse:
        """
        용도에 맞는 최적 모델로 완성

        Args:
            prompt: 프롬프트
            task: 작업 유형 (자동으로 최적 모델 선택)
            system: 시스템 프롬프트
        """
        self.stats["total_requests"] += 1
        attempt_role = kwargs.pop("_attempt_role", "primary")
        if self._closing:
            response = self._monthly_blocked_response(LLMProvider.OPENAI, "", "manager_closing")
            response.error = "LLM manager closing"
            self.stats["error_count"] += 1
            return response

        # 예산 체크
        if not self._check_budget():
            logger.warning("일일 LLM 예산 초과")
            return LLMResponse(
                content="", model="", provider=LLMProvider.OPENAI,
                success=False, error="Daily budget exceeded",
                request_sent=False, usage_known=False, billing_status="not_sent",
                budget_block_reason="daily_unknown_cost" if self._daily_unknown_blocks() else "daily_limit_exceeded",
            )

        # ── Manus 라우팅 (배치 태스크 한정) ──────────────────────
        # 배치 작업을 Manus 에이전트로 처리한다. 실패하면 아래 API 경로로 그대로 폴백한다.
        # 실시간 태스크는 config에 잘못 넣어도 아래 allowlist가 막는다 (주석은 강제력이 없다).
        if (self.config.monthly_budget_usd is None and self.config.manus_enabled
                and task.value in self.config.manus_tasks
                and task.value in MANUS_ALLOWED_TASKS):
            manus_resp = await self._try_manus(prompt, system, task, kwargs)
            if manus_resp is not None:
                return manus_resp
            # None이면 Manus 실패 → API 폴백 (로그는 _try_manus에서 남긴다)

        # 작업별 설정 가져오기
        task_config = self.TASK_CONFIG.get(task, self.TASK_CONFIG[LLMTask.QUICK_CLASSIFY])

        # Primary 시도
        primary_provider, primary_weight = task_config["primary"]
        primary_client = self._get_client(primary_provider)
        primary_model = self._get_model(primary_provider, primary_weight)

        response = await self._complete_attempt(
            primary_client, prompt, system, primary_model, task.value, attempt_role,
            **self._task_controls(primary_model, primary_weight, kwargs)
        )

        if response.refusal_detected:
            # A provider refusal remains a refusal even if transport cleanup
            # subsequently times out. Never turn it into cross-provider retry.
            response.success = False
            response.error = "Provider refused request"
            self.stats["error_count"] += 1
            return response

        if response.http_status in {401, 403}:
            # A billing-policy relaxation does not authorize bypassing a known
            # authentication/permission refusal, including body-read timeouts.
            self.stats["error_count"] += 1
            return response

        if response.success:
            self.stats["success_count"] += 1
            if not self._trail_has_unknown_cost(_ATTEMPT_TRAIL.get()):
                self._maybe_fire_shadow(prompt, system, task, kwargs, response)
            return response

        if response.budget_block_reason:
            self.stats["error_count"] += 1
            self.stats["fallback_skipped_monthly_budget"] = self.stats.get("fallback_skipped_monthly_budget", 0) + 1
            return response

        # Missing cost during repair ends this logical call instead of creating
        # another provider send. The next independent call may still proceed.
        if attempt_role == "json_retry" and self._trail_has_unknown_cost(_ATTEMPT_TRAIL.get()):
            self.stats["error_count"] += 1
            return response

        if not self._check_budget():
            self.stats["error_count"] += 1
            reason = ("fallback_skipped_unknown_cost" if self._daily_unknown_blocks()
                      else "fallback_skipped_budget")
            self.stats[reason] = self.stats.get(reason, 0) + 1
            return response

        fallback = task_config.get("fallback")
        if fallback is None:
            self.stats["error_count"] += 1
            return response
        fallback_provider, fallback_weight = fallback
        fallback_model = self._get_model(fallback_provider, fallback_weight)
        # Economy mode may already have selected light for the primary attempt.
        if (fallback_provider == response.provider
                and fallback_model in (response.model, response.resolved_model)):
            self.stats["error_count"] += 1
            return response
        logger.warning(f"Primary LLM 실패 ({primary_provider.value}): {response.error}, 폴백 시도")
        self.stats["fallback_count"] += 1
        fallback_client = self._get_client(fallback_provider)

        response = await self._complete_attempt(
            fallback_client, prompt, system, fallback_model, task.value, "fallback",
            **self._task_controls(fallback_model, fallback_weight, kwargs)
        )

        if response.success:
            self.stats["success_count"] += 1
            if not self._trail_has_unknown_cost(_ATTEMPT_TRAIL.get()):
                self._maybe_fire_shadow(prompt, system, task, kwargs, response)
        else:
            self.stats["error_count"] += 1
            logger.error(f"Fallback LLM도 실패 ({fallback_provider.value}): {response.error}")

        return response

    async def _try_manus(
        self,
        prompt: str,
        system: str,
        task: LLMTask,
        kwargs: Dict[str, Any],
    ) -> Optional[LLMResponse]:
        """
        Manus API로 처리를 시도한다.

        Returns:
            성공 시 LLMResponse, 실패 시 None (호출측이 API로 폴백)

        Manus는 별도 과금 체계(크레딧)라 OpenAI 토큰 통계·daily_usage에 더하지 않는다.
        """
        try:
            from .manus_client import get_manus_client
            client = get_manus_client(
                agent_profile=self.config.manus_agent_profile,
                timeout=self.config.manus_timeout_sec,
            )
            if not client.is_available():
                logger.debug("[LLM] MANUS_API_KEY 미설정 → API 사용")
                return None

            # system 프롬프트는 별도 인자가 없으므로 지시문 앞에 붙인다
            full_prompt = f"{system}\n\n{prompt}" if system else prompt

            resp = await client.complete(
                full_prompt,
                output_schema=kwargs.get("output_schema"),
            )
            if not resp.success or not resp.content:
                logger.info(
                    f"[LLM] Manus 실패({resp.error}) → API 폴백 (task={task.value})"
                )
                return None

            self.stats["success_count"] += 1
            self.stats["manus_count"] = self.stats.get("manus_count", 0) + 1
            logger.info(
                f"[LLM] Manus 처리 완료 (task={task.value}, "
                f"{resp.latency_ms/1000:.1f}s)"
            )
            return LLMResponse(
                content=resp.content,
                model=f"manus:{resp.model}",
                provider=LLMProvider.OPENAI,
                latency_ms=resp.latency_ms,
                success=True,
            )
        except Exception as e:
            logger.warning(f"[LLM] Manus 라우팅 오류 → API 폴백: {e}")
            return None

    async def complete_with(
        self,
        prompt: str,
        provider: LLMProvider,
        weight: str = "light",
        system: str = "",
        retry_on_empty: int = 0,
        **kwargs
    ) -> LLMResponse:
        """complete_with_impl 을 감싸 이 호출의 실제 시도 trail 을 응답에 붙인다 (64차, 동작 불변)."""
        token = _ATTEMPT_TRAIL.set([])
        try:
            response = await self._complete_with_impl(prompt, provider, weight, system, retry_on_empty, **kwargs)
        except BaseException as exc:
            self._attach_trail_to_exception(exc, _ATTEMPT_TRAIL.get())
            raise
        finally:
            trail = _ATTEMPT_TRAIL.get()
            _ATTEMPT_TRAIL.reset(token)
        self._attach_trail(response, trail)
        return response

    @staticmethod
    def _attach_trail(response, trail) -> None:
        try:
            if response is not None and isinstance(trail, list):
                response.attempt_trail = list(trail)
        except Exception:
            pass

    @classmethod
    def _attach_trail_to_exception(cls, exc: BaseException, trail) -> None:
        """타임아웃 취소·공급자 예외에서도 이미 보낸 시도(비용·예약 토큰)를 잃지 않는다 (64차 재리뷰 P1)."""
        try:
            if isinstance(trail, list):
                exc._llm_trail = list(trail)
                cls._attach_trail(getattr(exc, "_llm_response", None), trail)
        except Exception:
            pass

    async def _complete_with_impl(
        self,
        prompt: str,
        provider: LLMProvider,
        weight: str = "light",
        system: str = "",
        retry_on_empty: int = 0,
        **kwargs
    ) -> LLMResponse:
        """
        특정 provider를 명시해 호출한다 (폴백 없음).

        complete()는 task별 primary→fallback으로 "하나의 답"을 만든다.
        서로 다른 모델의 판단을 **비교**하려면 provider를 고정해야 하므로
        멀티-LLM 합의 검증(adversarial_validator)이 이 메서드를 쓴다.

        Args:
            provider: 사용할 provider (OPENAI / GEMINI)
            weight: 모델 등급 ("light" | "heavy" 등, _get_model 규약을 따름)
            retry_on_empty: **success=True인데 본문이 빈** 경우의 재시도 횟수.
                gpt-5 계열은 200 응답에 content만 비어 오는 경우가 실측으로 확인됐다
                (6회 중 1회 수준). 판정용 호출은 빈 응답이 곧 판단 불가이므로,
                재현성이 중요한 경로에서만 1 이상을 준다. 기본 0 = 기존 동작 유지.
        """
        self.stats["total_requests"] += 1
        if self.provider_policy_block_reason(provider) == "provider_disabled":
            response = _disabled_gemini_response(self._get_model(provider, weight))
            self._account_attempt(response, "complete_with", "primary")
            self.stats["error_count"] += 1
            return response

        if provider == LLMProvider.ANTHROPIC:
            response = await self._complete_claude(prompt, system, **kwargs)
            self.stats["success_count" if response.success else "error_count"] += 1
            return response

        if not self._check_budget():
            logger.warning("일일 LLM 예산 초과")
            return LLMResponse(
                content="", model="", provider=provider,
                success=False, error="Daily budget exceeded",
                request_sent=False, usage_known=False, billing_status="not_sent",
                budget_block_reason="daily_unknown_cost" if self._daily_unknown_blocks() else "daily_limit_exceeded",
            )

        client = self._get_client(provider)
        model = self._get_model(provider, weight)

        response = await self._complete_attempt(
            client, prompt, system, model, "complete_with", "primary", **kwargs
        )
        attempts = 0
        while (retry_on_empty > attempts
               and response.success
               and not self._trail_has_unknown_cost(_ATTEMPT_TRAIL.get())
               and not (response.content or "").strip()
               and self._check_budget()):
            attempts += 1
            self.stats["empty_retry"] = self.stats.get("empty_retry", 0) + 1
            logger.warning(
                f"[LLM] {model} 빈 응답 (success=True, content='') "
                f"— 재시도 {attempts}/{retry_on_empty}"
            )
            response = await self._complete_attempt(
                client, prompt, system, model, "complete_with", "empty_retry", **kwargs
            )

        if response.success:
            self.stats["success_count"] += 1
        else:
            self.stats["error_count"] += 1

        return response

    async def _complete_claude(self, prompt: str, system: str, **kwargs) -> LLMResponse:
        """One subscription attempt, then at most one separately admitted API call."""
        has_api_cap = "api_max_tokens" in kwargs
        api_cap = kwargs.pop("api_max_tokens", None)
        if has_api_cap and (type(api_cap) is not int or not 1 <= api_cap <= 800):
            raise ValueError("Claude api_max_tokens must be an integer from 1 to 800")
        total = kwargs.pop("call_timeout_seconds", 12.0)
        if (isinstance(total, bool) or not isinstance(total, (int, float))
                or not math.isfinite(total) or not 0 < total <= 20):
            raise ValueError("Claude call_timeout_seconds must be positive and at most 20")
        if kwargs.get("reasoning_effort", "low") != "low":
            raise ValueError("Claude engine roles require low effort")
        if any(kwargs.get(k) is not None for k in ("seed", "temperature", "thinking_level")):
            raise ValueError("Unsupported Claude role controls")
        kwargs.setdefault("reasoning_effort", "low")
        kwargs.setdefault("max_tokens", 400)
        model = self.config.claude_model
        loop = asyncio.get_running_loop()
        started, deadline = loop.time(), loop.time() + total
        self._ensure_claude_clients()
        cooldown = self._claude_cooldown_response(self.claude_cli, model)
        if cooldown is not None:
            response = cooldown
            self._account_attempt(response, "complete_with", "primary")
        elif self.config.claude_cli_enabled:
            response = await self._complete_attempt(
                self.claude_cli, prompt, system, model, "complete_with", "primary",
                _deadline=min(deadline, started + min(8.0, total / 2)), **kwargs)
            if response.transport_error_kind in {"cli_rate_limited", "cli_auth_unavailable"}:
                self._claude_transport_cooldowns["claude_cli"] = (
                    loop.time() + 300.0, response.transport_error_kind)
        else:
            response = LLMResponse("", model, LLMProvider.ANTHROPIC, success=False,
                                   error="Claude CLI disabled", requested_model=model,
                                   request_sent=False, usage_known=False, billing_status="not_sent",
                                   transport_error_kind="cli_unavailable")
            self._stamp_transport(response, self.claude_cli)
            self._account_attempt(response, "complete_with", "primary")
        permitted = {"cli_unavailable", "cli_timeout", "cli_rate_limited",
                     "cli_auth_unavailable", "cli_incomplete"}
        if (response.success or response.refusal_detected or self._closing
                or response.transport_error_kind not in permitted or loop.time() >= deadline):
            return response
        # The subscription attempt is reaped/settled before API admission.
        cooldown = self._claude_cooldown_response(self.anthropic, model)
        if cooldown is not None:
            self._account_attempt(cooldown, "complete_with", "fallback")
            return cooldown
        # Without an explicit API-only cap, preserve the original 400-token
        # transport boundary even when the CLI was skipped by configuration/cooldown.
        base_cap = kwargs.get("max_tokens")
        if not has_api_cap and type(base_cap) is int and base_cap > 400:
            response = LLMResponse("", model, LLMProvider.ANTHROPIC, success=False,
                                   error="invalid_request", requested_model=model,
                                   requested_max_tokens=base_cap if base_cap <= 1000000 else None,
                                   request_sent=False, usage_known=False, billing_status="not_sent",
                                   transport_error_kind="invalid_request")
            self._stamp_transport(response, self.anthropic)
            self._account_attempt(response, "complete_with", "fallback")
            return response
        if not self._check_budget():
            response = self._monthly_blocked_response(
                LLMProvider.ANTHROPIC, model,
                "daily_unknown_cost" if self._daily_unknown_blocks() else "daily_limit_exceeded")
            self._stamp_transport(response, self.anthropic)
            self._account_attempt(response, "complete_with", "fallback")
            return response
        self.stats["fallback_count"] += 1
        api_kwargs = dict(kwargs)
        if has_api_cap:
            api_kwargs["max_tokens"] = api_cap
        response = await self._complete_attempt(
            self.anthropic, prompt, system, model, "complete_with", "fallback",
            _deadline=deadline, **api_kwargs)
        if not response.success and response.http_status in {401, 402, 403, 429}:
            self._claude_transport_cooldowns["anthropic_messages"] = (
                loop.time() + 300.0, "transport_cooldown")
        return response

    def _claude_cooldown_response(self, client, model):
        transport = "claude_cli" if client is self.claude_cli else "anthropic_messages"
        until, kind = self._claude_transport_cooldowns.get(transport, (0.0, None))
        if asyncio.get_running_loop().time() >= until:
            return None
        response = LLMResponse("", model, LLMProvider.ANTHROPIC, success=False,
                               error="Claude transport cooldown", requested_model=model,
                               request_sent=False, usage_known=False, billing_status="not_sent",
                               transport_error_kind=kind)
        self._stamp_transport(response, client)
        return response

    def _monthly_blocked_response(self, provider: LLMProvider, model: str, reason: str) -> LLMResponse:
        return LLMResponse("", model, provider, success=False,
                           error=f"Monthly budget blocked: {reason}", requested_model=model,
                           request_sent=False, usage_known=False, billing_status="not_sent",
                           budget_block_reason=reason)

    def get_monthly_budget_status(self) -> Dict[str, Any]:
        if self.config.monthly_budget_usd is None:
            return {"enabled": False}
        status_error = self._monthly_budget_error
        if status_error is None and self.monthly_budget is not None:
            try:
                return {"enabled": True, **self.monthly_budget.status()}
            except BudgetBlocked as exc:
                status_error = exc.reason
        return {"enabled": True, "blocked": True, "block_reason": status_error,
                "spent_usd": None, "reserved_usd": None, "available_usd": None,
                "monthly_limit_usd": self.config.monthly_budget_usd}

    @staticmethod
    def _append_trail(response: LLMResponse, task: str, role: str) -> None:
        """현재 complete()/complete_with() 호출의 trail 에 이 실제 시도 1행을 더한다 (64차). 실패해도 호출을 막지 않는다."""
        trail = _ATTEMPT_TRAIL.get()
        if trail is None:
            return
        try:
            cost = None
            if response.billing_channel == "api" and response.request_sent and response.usage_known and isinstance(response.input_tokens, int) \
                    and isinstance(response.output_tokens, int):
                _model = response.requested_model if response.requested_model is not None else response.model
                quote = rate_quote(_model, response.input_tokens, response.output_tokens)
                if quote is not None:
                    from decimal import Decimal
                    cost = str(Decimal(str(quote.cost_usd)))
            trail.append({
                "provider": getattr(response.provider, "value", str(response.provider)),
                "transport": response.transport, "billing_channel": response.billing_channel,
                "model_identity_status": response.model_identity_status,
                "subscription_estimated_cost_usd": response.subscription_estimated_cost_usd,
                "actual_incremental_cost_usd": response.actual_incremental_cost_usd,
                "task": task, "role": role,
                "requested_model": response.requested_model if response.requested_model is not None else response.model,
                "resolved_model": response.resolved_model,
                "requested_max_tokens": response.requested_max_tokens,
                "finish_reason": (response.finish_reason
                                  if isinstance(response.finish_reason, str)
                                  and response.finish_reason in _AUDIT_FINISH_REASONS else None),
                "request_sent": response.request_sent, "success": response.success,
                "billing_status": response.billing_status, "usage_known": response.usage_known,
                # 사용량이 보고된 시도만 토큰 수를 적는다 — 미확인(usage_known=False)은 None (4차 리뷰 P2)
                "input_tokens": response.input_tokens if (response.request_sent and response.usage_known is True) else None,
                "output_tokens": response.output_tokens if (response.request_sent and response.usage_known is True) else None,
                "estimated_cost_usd": cost,
                "transport_error_kind": response.transport_error_kind,
                "uncertain_cost_ceiling_usd": response.uncertain_cost_ceiling_usd,
                "missing_cost_policy": response.missing_cost_policy,
                "cost_accounting_skipped": response.cost_accounting_skipped,
                "budget_reservation_token": response.budget_reservation_token,
                "budget_block_reason": response.budget_block_reason,
                "latency_ms": response.latency_ms, "error": response.error,
                "recorded_at": datetime.now().astimezone().isoformat(),
            })
        except Exception:
            pass

    def _settle_monthly(self, token: Optional[str], response: LLMResponse, model: str) -> None:
        # 64차: 판단 기록이 예산 원장 행과 조인할 수 있게 응답에 예약 토큰을 남긴다 (정산 결과와 무관)
        try:
            response.budget_reservation_token = token
        except Exception:
            pass
        if token is None or self.monthly_budget is None:
            return
        try:
            if not response.request_sent:
                self.monthly_budget.settle(token, outcome="not_sent")
            elif response.billing_status == "rejected_before_generation":
                self.monthly_budget.settle(token, outcome="rejected")
            elif response.usage_known:
                quote = rate_quote(model, response.input_tokens, response.output_tokens,
                                   on_date=self.monthly_budget.today())
                if quote is None:
                    self.monthly_budget.settle(token, outcome="unknown")
                else:
                    self.monthly_budget.settle(token, outcome="known", actual_cost_usd=quote.cost_usd)
            else:
                outcome = ("timeout" if response.transport_error_kind == "timeout"
                           and response.http_status in (None, 200)
                           and not response.refusal_detected else "unknown")
                response.uncertain_cost_ceiling_usd = self.monthly_budget.settle(token, outcome=outcome)
        except (BudgetBlocked, ValueError, TypeError) as exc:
            self._monthly_budget_error = exc.reason if isinstance(exc, BudgetBlocked) else "invalid_usage"
            # Preserve every other in-flight request's lock and right to settle.
            # settle() normally drops this fd itself; abandon is idempotent and
            # also covers validation/write errors before settle was entered.
            self.monthly_budget.abandon(token)
            logger.error("[LLM] monthly budget settlement failed; paid calls blocked")

    async def _complete_attempt(self, _client: BaseLLMClient, _prompt: str, _system: str,
                                _model: str, _task: str, _role: str, **kwargs) -> LLMResponse:
        if self._closing:
            provider = self._client_provider(_client)
            response = self._monthly_blocked_response(provider, _model, "manager_closing")
            response.error = "LLM manager closing"
            self._stamp_transport(response, _client)
            self._account_attempt(response, _task, _role)
            return response
        # 호출자 전체가 아니라 이 요청의 전송·정산 수명만 소유한다.
        attempt = asyncio.create_task(self._run_complete_attempt(
            _client, _prompt, _system, _model, _task, _role, **kwargs
        ))
        self._attempt_tasks.add(attempt)
        attempt.add_done_callback(self._attempt_done)
        try:
            return await attempt
        except asyncio.CancelledError as exc:
            caller = asyncio.current_task()
            if (not self._closing or _role == "shadow"
                    or (caller is not None and caller.cancelling())):
                # 호출자 취소/timeout 계약은 유지한다.
                raise
            # manager가 소유 요청만 취소한 경우 호출자의 후처리는 계속한다.
            response = getattr(exc, "_llm_response", None)
            if response is None:
                # 시작 전 취소는 예약/전송이 없고, 시작 후 취소는 아래 경계가 기록한다.
                provider = self._client_provider(_client)
                response = self._monthly_blocked_response(provider, _model, "manager_closing")
                self._stamp_transport(response, _client)
            response.budget_block_reason = "manager_closing"
            return response

    def _attempt_done(self, task: asyncio.Task) -> None:
        self._attempt_tasks.discard(task)
        if not task.cancelled():
            task.exception()  # 호출자 종료 후 남은 요청의 예외도 회수한다.
        self._start_resource_close()

    async def _run_complete_attempt(self, _client: BaseLLMClient, _prompt: str, _system: str,
                                    _model: str, _task: str, _role: str, **kwargs) -> LLMResponse:
        def account(response):
            cap = kwargs.get("max_tokens")
            response.requested_max_tokens = cap if type(cap) is int and 1 <= cap <= 1000000 else None
            self._account_attempt(response, _task, _role)

        provider = self._client_provider(_client)
        subscription = _client is self.claude_cli
        if self.provider_policy_block_reason(provider) == "provider_disabled":
            response = _disabled_gemini_response(_model)
            account(response)
            return response
        token = None
        deadline = kwargs.pop("_deadline", None)
        role_finish_reason = kwargs.pop("_role_finish_reason", None)
        priority = kwargs.pop("budget_priority", "standard")
        if _role == "shadow":
            priority = "research"
        policy = None
        original_model, original_kwargs = _model, dict(kwargs)
        if self.config.monthly_budget_usd is not None and not subscription:
            try:
                if self._monthly_budget_error is not None or self.monthly_budget is None:
                    raise BudgetStateError(self._monthly_budget_error or "storage_unavailable")
                # Reselect only before admission/HTTP. Other processes can move
                # the policy between observation and BEGIN IMMEDIATE admission.
                for selection in range(3):
                    _model, kwargs = original_model, dict(original_kwargs)
                    if self.config.budget_policy_enabled:
                        policy = self.monthly_budget.status()
                        mode = policy['policy_mode']
                        if _role == 'shadow' and mode in {'economy', 'essential'}:
                            raise BudgetBlocked('economy_shadow_paused')
                        if mode == 'economy' and priority != 'essential':
                            if _task == LLMTask.REPORT_SUMMARY.value:
                                _model = self._get_model(provider, 'light')
                                supported = _OPENAI_EFFORTS
                                if 'low' not in supported.get(_model, set()):
                                    raise BudgetBlocked('unsupported_economy_controls')
                                kwargs['reasoning_effort'] = 'low'
                            if _task in {t.value for t in (
                                    LLMTask.REPORT_SUMMARY, LLMTask.NEWS_SUMMARY,
                                    LLMTask.QUICK_CLASSIFY, LLMTask.QUICK_ANALYSIS,
                                    LLMTask.THEME_DETECTION, LLMTask.STOCK_MAPPING, LLMTask.WIKI_INGEST)}:
                                cap = kwargs.get('max_tokens')
                                cap = self.config.max_output_tokens if cap is None else cap
                                if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0:
                                    kwargs['max_tokens'] = min(cap, 4096)
                    output_limit = kwargs.get('max_tokens')
                    if output_limit is None:
                        output_limit = self.config.max_output_tokens
                    try:
                        token = self.monthly_budget.reserve(
                            _model, _prompt, _system, output_limit, policy_priority=priority,
                            expected_policy_mode=policy['policy_mode'] if policy else None,
                            task=_task, role=_role)
                        break
                    except BudgetBlocked as exc:
                        if exc.reason != 'budget_profile_changed' or selection == 2:
                            raise
            except BudgetBlocked as exc:
                response = self._monthly_blocked_response(provider, _model, exc.reason)
                self._stamp_transport(response, _client)
                self._annotate_budget(response, policy, priority)
                account(response)
                return response
        # 원장의 동기 입장 검사가 시한을 넘겼으면 HTTP 진입 전에 예약을 해제한다.
        # 이벤트 루프의 timeout callback만으로는 동기 검사 직후 전송을 막을 수 없다.
        if deadline is not None and asyncio.get_running_loop().time() >= deadline:
            response = LLMResponse(
                "", _model, provider, success=False, requested_model=_model,
                error="Role deadline exceeded", request_sent=False,
                usage_known=False, billing_status="not_sent")
            self._stamp_transport(response, _client)
            self._annotate_budget(response, policy, priority)
            self._settle_monthly(token, response, _model)
            account(response)
            return response
        try:
            if provider == LLMProvider.ANTHROPIC:
                response = await _client.complete(_prompt, _system, model=_model,
                                                  deadline=deadline, **kwargs)
            else:
                response = await _client.complete(_prompt, _system, model=_model, **kwargs)
        except asyncio.CancelledError as exc:
            response = getattr(exc, "_llm_response", None)
            if response is None:
                response = LLMResponse("", _model, provider, success=False,
                                       error="Request cancelled", usage_known=False, billing_status="unknown")
            if response is not None:
                self._stamp_transport(response, _client)
                self._annotate_budget(response, policy, priority)
                self._settle_monthly(token, response, _model)
                account(response)
            exc._llm_response = response
            raise
        except ValueError as exc:
            # Transport validation errors are local and retain their public raise contract.
            unsent = LLMResponse("", _model, provider, success=False, error="Invalid local controls",
                                 request_sent=False, usage_known=False, billing_status="not_sent")
            self._stamp_transport(unsent, _client)
            self._annotate_budget(unsent, policy, priority)
            self._settle_monthly(token, unsent, _model)
            account(unsent)
            exc._llm_response = unsent
            raise
        except Exception as exc:
            unknown = LLMResponse("", _model, provider, success=False,
                                  error="Provider invocation failed", usage_known=False, billing_status="unknown")
            self._stamp_transport(unknown, _client)
            self._annotate_budget(unknown, policy, priority)
            self._settle_monthly(token, unknown, _model)
            account(unknown)
            exc._llm_response = unknown
            raise
        self._stamp_transport(response, _client)
        # Provider refusal is an unsuccessful result on every route, including
        # fallback; record that fact before the attempt trail and audit persist.
        if response.refusal_detected:
            response.success = False
            response.error = "Provider refused request"
        # Fixed roles additionally require complete, non-empty output.
        if role_finish_reason is not None and response.success and (
                response.finish_reason != role_finish_reason or response.refusal_detected
                or not (response.content or "").strip()):
            response.success = False
            response.error = "Role completion incomplete, refused or empty"
        self._annotate_budget(response, policy, priority)
        self._settle_monthly(token, response, _model)
        account(response)
        return response

    @staticmethod
    def _annotate_budget(response, policy, priority):
        response.budget_priority = priority
        if policy:
            response.budget_policy_mode = policy['policy_mode']
            response.budget_policy_version = policy['policy_version']

    def _account_attempt(self, response: LLMResponse, task: str, role: str) -> None:
        """Record known usage even for empty/error responses before another attempt."""
        response.missing_cost_policy = self.config.missing_cost_policy
        response.cost_accounting_skipped = bool(
            response.billing_channel == "api"
            and
            self.config.missing_cost_policy == "log_and_continue"
            and response.request_sent and not response.usage_known
            and response.billing_status != "rejected_before_generation"
            and response.uncertain_cost_ceiling_usd is None)
        if response.cost_accounting_skipped:
            logger.warning("[LLM] 비용 미확인 시도: 금액 합산 제외, 미확인 기록 보존 (log_and_continue)")
        self._append_trail(response, task, role)
        if response.billing_channel == "subscription":
            usage = self._subscription_usage
            usage["attempts"] += 1
            usage["sent"] += int(response.request_sent)
            if response.usage_known:
                usage["input_tokens"] += response.input_tokens
                usage["output_tokens"] += response.output_tokens
            estimate = response.subscription_estimated_cost_usd
            if type(estimate) in (int, float) and math.isfinite(estimate) and estimate >= 0:
                usage["list_cost_estimate_usd"] += estimate
        elif response.billing_status == "rejected_before_generation":
            self.daily_usage.inferred_nonbillable_rejections += 1
        elif response.request_sent:
            self.daily_usage.add(response.input_tokens, response.output_tokens,
                                 response.requested_model or response.model,
                                 usage_known=response.usage_known,
                                 uncertain_cost_ceiling_usd=response.uncertain_cost_ceiling_usd)
        self._audit_attempt(response, task, role)

    def _audit_attempt(self, response: LLMResponse, task: str, role: str) -> None:
        """Metadata-only audit; no prompts, content, free-text errors, keys or raw payload."""
        is_role = task in {item.value for item in LLMRole}
        audit_dir = self.audit_dir
        role_sink = audit_dir is None and is_role and self.config.monthly_budget_usd is not None
        if role_sink and self.config.budget_state_path:
            audit_dir = Path(self.config.budget_state_path).expanduser().parent / "role-audit"
        if audit_dir is None:
            return
        now = datetime.now()
        today = now.strftime("%Y%m%d")
        try:
            audit_dir.mkdir(mode=0o700 if role_sink else 0o777, parents=True, exist_ok=True)
            if not role_sink and self._audit_log_date != today:
                cutoff = (now - timedelta(days=self._LLM_LOG_RETENTION_DAYS)).strftime("%Y%m%d")
                try:
                    for path in audit_dir.glob("llm-audit-*.jsonl"):
                        date_part = path.stem.removeprefix("llm-audit-")
                        if re.fullmatch(r"\d{8}", date_part) and date_part < cutoff:
                            try:
                                path.unlink()
                            except OSError:
                                logger.warning("[LLM] metadata audit retention failed")
                except OSError:
                    logger.warning("[LLM] metadata audit retention scan failed")
                self._audit_log_date = today
            model = response.requested_model or response.model
            quote = (rate_quote(model, response.input_tokens, response.output_tokens)
                     if response.billing_channel == "api" else None)
            row = {
                "ts": now.isoformat(), "task": task, "provider": response.provider.value,
                "transport": response.transport, "billing_channel": response.billing_channel,
                "model_identity_status": response.model_identity_status,
                "subscription_estimated_cost_usd": response.subscription_estimated_cost_usd,
                "actual_incremental_cost_usd": response.actual_incremental_cost_usd,
                "requested_model": model, "resolved_model": response.resolved_model,
                "requested_reasoning_effort": response.requested_reasoning_effort,
                "requested_thinking_level": response.requested_thinking_level,
                "input_tokens": response.input_tokens if response.usage_known else None,
                "output_tokens": response.output_tokens if response.usage_known else None,
                "reasoning_tokens": response.reasoning_tokens,
                "visible_output_tokens": response.visible_output_tokens,
                "finish_reason": response.finish_reason, "elapsed_ms": response.latency_ms,
                "request_sent": response.request_sent, "usage_known": response.usage_known,
                "billing_status": response.billing_status,
                "transport_error_kind": response.transport_error_kind,
                "uncertain_cost_ceiling_usd": response.uncertain_cost_ceiling_usd,
                "missing_cost_policy": response.missing_cost_policy,
                "cost_accounting_skipped": response.cost_accounting_skipped,
                "budget_block_reason": response.budget_block_reason,
                "budget_policy_mode": response.budget_policy_mode,
                "budget_policy_version": response.budget_policy_version,
                "budget_priority": response.budget_priority,
                "http_status": response.http_status,
                "provider_error_code": response.provider_error_code,
                "provider_error_type": response.provider_error_type,
                "provider_error_status": response.provider_error_status,
                "cost_basis": ("subscription_list_estimate_not_invoice"
                               if response.billing_channel == "subscription" else
                               "pre_generation_rejection_inference_not_invoice_proof"
                               if response.billing_status == "rejected_before_generation"
                               else "reported_tokens_estimate" if response.usage_known else "unknown"),
                "outcome": ("error" if not response.success else
                            "success" if (response.content or "").strip() else "empty"),
                "attempt_role": role,
                "estimated_cost_usd": quote.cost_usd if quote and response.usage_known else None,
                "price_basis": quote.basis if quote else "unpriced",
            }
            if is_role:
                row["role_policy_version"] = ROLE_POLICY_VERSION
                row["refusal_detected"] = response.refusal_detected
            line = json.dumps(row, ensure_ascii=False) + "\n"
            filename = f"llm-audit-{today}.jsonl"
            if role_sink:
                # 자동 역할 감사만 전용 비공개 경로에 기록한다. symlink를 따라가지 않는다.
                directory_fd = os.open(audit_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fchmod(directory_fd, 0o700)
                    if getattr(self, "_role_audit_log_date", None) != today:
                        cutoff = (now - timedelta(days=self._LLM_LOG_RETENTION_DAYS)).strftime("%Y%m%d")
                        try:
                            for name in os.listdir(directory_fd):
                                match = re.fullmatch(r"llm-audit-(\d{8})\.jsonl", name)
                                if match and match[1] < cutoff:
                                    os.unlink(name, dir_fd=directory_fd)
                        except OSError:
                            logger.warning("[LLM] metadata audit retention failed")
                        self._role_audit_log_date = today
                    fd = os.open(filename, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory_fd)
                    with os.fdopen(fd, "a", encoding="utf-8") as stream:
                        os.fchmod(stream.fileno(), 0o600)
                        stream.write(line)
                finally:
                    os.close(directory_fd)
            else:
                with (audit_dir / filename).open("a", encoding="utf-8") as stream:
                    stream.write(line)
        except Exception:
            logger.warning("[LLM] metadata audit write failed")

    def _maybe_fire_shadow(
        self,
        prompt: str,
        system: str,
        task: LLMTask,
        kwargs: dict,
        primary_response: LLMResponse,
    ) -> None:
        """2026-06-17 Phase 3: Shadow A/B — light 후속 모델 마이그레이션 비교.

        조건: openai_model_light_shadow 설정 && primary가 openai_model_light였을 때만 호출.
        fire-and-forget — 본 응답은 즉시 반환됨.
        """
        shadow_model = self.config.openai_model_light_shadow
        if not shadow_model or self._closing:
            return
        if primary_response.model != self.config.openai_model_light:
            return
        try:
            shadow_task = asyncio.create_task(
                self._fire_shadow(prompt, system, task, kwargs, primary_response, shadow_model)
            )
            self._shadow_tasks.add(shadow_task)
            shadow_task.add_done_callback(self._shadow_done)
        except Exception as e:
            logger.debug(f"[LLM-Shadow] 스케줄 실패: {e}")

    def _shadow_done(self, task: asyncio.Task) -> None:
        self._shadow_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("[LLM] shadow task failed")

    async def close(self) -> None:
        """신규 요청을 막고 정상 완료를 기다린 뒤 소유 요청만 제한 시간 내 취소한다."""
        self._closing = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._drain_and_close())
        current = asyncio.current_task()
        if (current in self._attempt_tasks or current in self._shadow_tasks
                or current is self._close_task or current is self._resource_close_task):
            # 요청 내부 재진입은 자신을 기다리지 않고 완료 callback에 정리를 맡긴다.
            return
        await asyncio.shield(self._close_task)

    async def _drain_and_close(self) -> None:
        pending = set(self._attempt_tasks) | set(self._shadow_tasks)
        if pending:
            _, pending = await asyncio.wait(pending, timeout=self._CLOSE_GRACE_SECONDS)
        if pending:
            # shadow wrapper를 함께 취소하면 같은 요청에 취소가 중복 전달된다.
            for task in self._attempt_tasks.copy():
                if not task.done():
                    task.cancel()
            _, pending = await asyncio.wait(pending, timeout=self._CLOSE_FORCE_SECONDS)
        if pending:
            logger.warning("[LLM] 종료 유예 초과: 진행 중 요청의 정산 소유권을 유지합니다")
        self._start_resource_close()
        if self._resource_close_task is not None:
            done, _ = await asyncio.wait(
                {self._resource_close_task}, timeout=self._CLOSE_RESOURCE_SECONDS
            )
            if not done:
                logger.warning("[LLM] HTTP 정리 유예 초과: 백그라운드 정리를 유지합니다")

    def _start_resource_close(self) -> None:
        # 취소를 무시하는 요청도 정산 전에는 lock/HTTP 세션을 해제하지 않는다.
        if self._closing and not self._attempt_tasks and self._resource_close_task is None:
            self._resource_close_task = asyncio.create_task(self._close_resources())
            self._resource_close_task.add_done_callback(self._resource_close_done)

    @staticmethod
    def _resource_close_done(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error("[LLM] 자원 정리 실패")

    async def _close_resources(self) -> None:
        if self.monthly_budget is not None:
            self.monthly_budget.close()
        for client in (self.openai, self.gemini):
            if client._session is not None and not client._session.closed:
                await client._session.close()
        for client in (self.claude_cli, self.anthropic):
            if client is not None:
                await client.close()

    async def _fire_shadow(
        self,
        prompt: str,
        system: str,
        task: LLMTask,
        kwargs: dict,
        primary_response: LLMResponse,
        shadow_model: str,
    ) -> None:
        """Shadow 호출 + 비교 로그 기록."""
        if not self._check_budget():
            return
        import time
        import uuid
        pair_id = uuid.uuid4().hex[:12]
        t0 = time.perf_counter()
        try:
            shadow_resp = await self._complete_attempt(
                self.openai, prompt, system, shadow_model, task.value, "shadow", **kwargs
            )
        except Exception as exc:
            # The central attempt boundary has already accounted this failure.
            shadow_resp = getattr(exc, "_llm_response", None) or LLMResponse(
                content="", model=shadow_model, provider=LLMProvider.OPENAI,
                success=False, error="Shadow invocation failed", usage_known=False,
                request_sent=not isinstance(exc, ValueError),
                billing_status="not_sent" if isinstance(exc, ValueError) else "unknown",
            )
        latency_ms = int((time.perf_counter() - t0) * 1000)
        try:
            self._SHADOW_LOG_DIR.mkdir(parents=True, exist_ok=True)
            today = datetime.now().strftime("%Y%m%d")
            log_path = self._SHADOW_LOG_DIR / f"{today}.jsonl"
            entry = {
                "ts": datetime.now().isoformat(),
                "pair_id": pair_id,
                "task": task.value,
                "primary": {
                    "model": primary_response.model,
                    "success": primary_response.success,
                    "content": (primary_response.content or "")[:5120],
                    "in_tokens": primary_response.input_tokens,
                    "out_tokens": primary_response.output_tokens,
                },
                "shadow": {
                    "model": shadow_resp.model,
                    "success": shadow_resp.success,
                    "content": (shadow_resp.content or "")[:5120],
                    "error": shadow_resp.error or "",
                    "in_tokens": shadow_resp.input_tokens,
                    "out_tokens": shadow_resp.output_tokens,
                    "latency_ms": latency_ms,
                },
            }
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            logger.debug(f"[LLM-Shadow] 로그 실패: {e}")

    async def complete_json(
        self,
        prompt: str,
        task: LLMTask = LLMTask.QUICK_CLASSIFY,
        system: str = "",
        _retry: int = 0,
        **kwargs
    ) -> Dict[str, Any]:
        """JSON 응답 생성 (Invalid JSON 시 최대 1회 재시도)"""
        response = await self.complete(
            prompt, task, system, _attempt_role="json_retry" if _retry else "primary", **kwargs
        )

        if not response.success:
            self._log_llm_response(
                task=task.value, model=response.model or "",
                raw=response.error or "", parsed=None, success=False,
            )
            result = {"error": response.error}
            if response.budget_block_reason:
                result['budget_block_reason'] = response.budget_block_reason
            return result

        try:
            parsed = _extract_json(response.content)
            self._log_llm_response(
                task=task.value, model=response.model or "",
                raw=response.content or "", parsed=parsed, success=True,
            )
            return parsed
        except (json.JSONDecodeError, ValueError):
            self._log_llm_response(
                task=task.value, model=response.model or "",
                raw=response.content or "", parsed=None, success=False,
            )
            # 1회 재시도 — JSON 형식만 요청하는 접미 추가
            if _retry == 0 and not self._trail_has_unknown_cost(response.attempt_trail):
                retry_prompt = (
                    prompt + "\n\n**중요: 위 지시에 따라 반드시 순수 JSON만 출력하세요. "
                    "코드블록(```), 설명 텍스트, 마크다운 없이 JSON 객체만 응답하세요.**"
                )
                logger.debug(f"[LLM] Invalid JSON 재시도 (task={task.value})")
                return await self.complete_json(
                    retry_prompt, task, system, _retry=1, **kwargs
                )
            return {"error": "Invalid JSON", "raw": response.content}

    def _log_llm_response(
        self, task: str, model: str, raw: str,
        parsed: Optional[Dict], success: bool,
    ) -> None:
        """LLM 응답을 JSONL 파일에 기록 (감사 추적용)."""
        try:
            today = datetime.now().strftime("%Y%m%d")

            # 날짜 변경 시 오래된 로그 정리 (1일 1회)
            if self._llm_log_date != today:
                self._llm_log_date = today
                self._cleanup_old_logs()

            self._LLM_LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_path = self._LLM_LOG_DIR / f"{today}.jsonl"

            # raw 응답 5KB 제한
            truncated_raw = raw[:5120] if raw else ""

            entry = {
                "ts": datetime.now().isoformat(),
                "task": task,
                "model": model,
                "raw": truncated_raw,
                "parsed": parsed,
                "success": success,
            }

            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

        except Exception as e:
            logger.debug(f"[LLM] 응답 로그 기록 실패: {e}")

    def _cleanup_old_logs(self) -> None:
        """7일 이상 된 LLM 응답 로그 삭제."""
        try:
            if not self._LLM_LOG_DIR.exists():
                return
            cutoff = datetime.now() - timedelta(days=self._LLM_LOG_RETENTION_DAYS)
            cutoff_str = cutoff.strftime("%Y%m%d")
            for f in self._LLM_LOG_DIR.glob("*.jsonl"):
                if f.stem < cutoff_str:
                    f.unlink()
                    logger.debug(f"[LLM] 오래된 로그 삭제: {f.name}")
        except Exception as e:
            logger.debug(f"[LLM] 로그 정리 실패: {e}")

    def get_usage_summary(self) -> Dict[str, Any]:
        """사용량 요약"""
        return {
            "date": str(self._usage_reset_date),
            "input_tokens": self.daily_usage.input_tokens,
            "output_tokens": self.daily_usage.output_tokens,
            "estimated_cost_usd": round(self.daily_usage.estimated_cost_usd, 4),
            "budget_remaining_usd": (None if self._daily_unknown_blocks() else
                                     round(max(0, self.config.daily_budget_usd
                                               - self.daily_usage.estimated_cost_usd
                                               - self.daily_usage.uncertain_cost_ceiling_usd), 4)),
            "uncertain_cost_ceiling_usd": self.daily_usage.uncertain_cost_ceiling_usd,
            "bounded_unknown_requests": self.daily_usage.bounded_unknown_requests,
            "missing_cost_policy": self.config.missing_cost_policy,
            "skipped_unknown_requests": (
                self.daily_usage.unknown_usage_requests - self.daily_usage.bounded_unknown_requests
                if self.config.missing_cost_policy == "log_and_continue" else 0),
            "cost_accounting_complete": not self.daily_usage.cost_missing,
            "cost_missing": self.daily_usage.cost_missing,
            "unpriced_requests": self.daily_usage.unpriced_requests,
            "unknown_usage_requests": self.daily_usage.unknown_usage_requests,
            "inferred_nonbillable_rejections": self.daily_usage.inferred_nonbillable_rejections,
            "budget_scope": "process_local_post_usage",
            "audit_enabled": self.audit_dir is not None,
            "configuration_warnings": list(self.configuration_warnings),
            "monthly_budget": self.get_monthly_budget_status(),
            "cost_scope": "api_only_excludes_subscription",
            "subscription_usage": dict(self._subscription_usage),
            "stats": self.stats,
        }


# 전역 인스턴스 (선택적 사용)
_llm_manager: Optional[LLMManager] = None
_pending_config: Optional[LLMConfig] = None


def set_llm_config(cfg: LLMConfig) -> None:
    """전역 LLM 설정 등록 (app 시작 시 호출).

    2026-06-16: GPT-5 deprecation 대비, 모델명을 config로 주입할 수 있게.
    이후 get_llm_manager()는 이 설정으로 LLMManager 인스턴스화.
    이미 인스턴스가 있으면 다음 호출까지 영향 없음 — 시작 시점에 호출 권장.
    """
    global _pending_config
    _pending_config = cfg


def get_llm_manager() -> LLMManager:
    """전역 LLM 매니저 인스턴스"""
    global _llm_manager
    if _llm_manager is None:
        if _pending_config is None or _pending_config.monthly_budget_usd is None:
            raise RuntimeError("LLM monthly budget configuration required before initialization")
        _llm_manager = LLMManager(config=_pending_config)
    return _llm_manager


def get_budget_monitor_manager() -> Optional[LLMManager]:
    """Initialize only when application config explicitly enables the monitor."""
    config = _llm_manager.config if _llm_manager is not None else _pending_config
    if config is None or not config.budget_policy_enabled:
        return None
    return get_llm_manager()


async def close_llm_manager() -> None:
    """Close an existing singleton only; shutdown must not initialize paid clients."""
    global _llm_manager
    manager = _llm_manager
    if manager is not None:
        await manager.close()
        _llm_manager = None


def unmetered_paid_calls_allowed() -> bool:
    """Read current/pending policy without initializing a manager or ledger."""
    config = _llm_manager.config if _llm_manager is not None else _pending_config
    return config is not None and config.monthly_budget_usd is None
