"""
AI Trading Bot - LLM 통합 모듈

OpenAI와 Gemini를 통합 관리하며, 용도별 최적 모델을 자동 선택합니다.

설계 철학:
- 가벼운 작업(테마 탐지, 분류) -> Gemini Flash (저비용, 빠름)
- 중요한 작업(시장 분석, 복기) -> GPT-4 (깊은 분석)
- 폴백 체인: GPT-4 -> GPT-3.5 -> Gemini -> 규칙 기반
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

from .llm_pricing import rate_quote
from .llm_budget import MonthlyBudgetLedger, BudgetBlocked, BudgetStateError


class LLMProvider(str, Enum):
    """LLM 제공자"""
    OPENAI = "openai"
    GEMINI = "gemini"


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
    THEME_DETECTION = "theme_detection"      # 테마 탐지 -> Gemini Flash
    STOCK_MAPPING = "stock_mapping"          # 종목 매핑 -> Gemini Flash
    NEWS_SUMMARY = "news_summary"            # 뉴스 요약 -> Gemini Flash
    MARKET_ANALYSIS = "market_analysis"      # 시장 분석 -> GPT-4
    TRADE_REVIEW = "trade_review"            # 거래 복기 -> GPT-4
    QUICK_CLASSIFY = "quick_classify"        # 빠른 분류 -> Gemini Flash
    STRATEGY_ANALYSIS = "strategy_analysis"  # 전략 분석/진화 -> GPT-4
    QUICK_ANALYSIS = "quick_analysis"        # 빠른 실시간 분석 -> Gemini Flash
    WIKI_INGEST = "wiki_ingest"              # 위키 교훈 추출 -> Gemini Flash
    REPORT_SUMMARY = "report_summary"       # Non-decision summaries; economy eligible.


@dataclass
class LLMConfig:
    """LLM 설정"""
    openai_api_key: str = ""
    gemini_api_key: str = ""

    # 모델 선택
    # Heavy: 깊은 분석, 거래 복기에 사용 (GPT-5.4 최신 모델)
    # 2026-08-02: gpt-5.4 → gpt-5.6-sol (gpt-5.4 deprecated 예정, sol은 일반 API 동작 확인)
    openai_model_heavy: str = "gpt-5.6-sol"
    # Light: 빠른 분류, 테마 탐지에 사용
    openai_model_light: str = "gpt-5-mini"
    gemini_model_heavy: str = "gemini-3.1-pro-preview"
    gemini_model_light: str = "gemini-3.1-flash-lite-preview"

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

    def __post_init__(self):
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
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
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
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            daily_budget_usd=llm_cfg.get("daily_budget", 5.0),
            monthly_budget_usd=llm_cfg.get("monthly_budget"),
            budget_state_path=llm_cfg.get("budget_state_path"),
            budget_policy_enabled=llm_cfg.get("budget_policy_enabled", False),
            openai_model_heavy=(llm_cfg.get("openai_model_heavy") or "gpt-5.6-sol"),
            openai_model_light=(llm_cfg.get("openai_model_light") or "gpt-5-mini"),
            gemini_model_heavy=(
                llm_cfg.get("gemini_model_heavy") or "gemini-3.1-pro-preview"
            ),
            gemini_model_light=(
                llm_cfg.get("gemini_model_light") or "gemini-3.1-flash-lite-preview"
            ),
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
    inferred_nonbillable_rejections: int = 0

    def add(self, input_tokens: int, output_tokens: int, model: str,
            *, usage_known: bool = True):
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
                content = (choice.get("message") or {}).get("content") or ""
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


class GeminiClient(BaseLLMClient):
    """Google Gemini 클라이언트"""

    def __init__(self, config: LLMConfig):
        self.config = config
        self.api_key = config.gemini_api_key
        self.base_url = "https://generativelanguage.googleapis.com/v1beta"
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
        """Gemini output billing includes candidate and thought tokens separately."""
        model = model if model is not None else self.config.gemini_model_light
        _validate_controls(model, LLMProvider.GEMINI, kwargs)
        level = kwargs.get("thinking_level")
        metadata = dict(requested_model=model, requested_thinking_level=level,
                        usage_known=False, request_sent=False, billing_status="not_sent")
        if not self.api_key:
            return LLMResponse("", model, LLMProvider.GEMINI, success=False,
                               error="Gemini API key not configured", **metadata)
        generation = {"temperature": temperature,
                      "maxOutputTokens": max_tokens if max_tokens is not None else self.config.max_output_tokens}
        if model == "gemini-3.8-flash":
            generation.pop("temperature")
        if level is not None:
            generation["thinkingConfig"] = {"thinkingLevel": level}
        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        start_time = time.perf_counter()
        input_count = output_count = 0
        try:
            session = await self._get_session()
            metadata["request_sent"] = True
            metadata["billing_status"] = "unknown"
            async with session.post(
                f"{self.base_url}/models/{model}:generateContent?key={self.api_key}",
                headers={"Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": full_prompt}]}], "generationConfig": generation},
            ) as resp:
                metadata["http_status"] = resp.status
                data = await resp.json()
                if not isinstance(data, dict):
                    raise ValueError("Provider response must be an object")
                metadata.update(_provider_errors(data))
                usage = data.get("usageMetadata") or {}
                if not isinstance(usage, dict):
                    usage = {}
                input_count = _token_count(usage.get("promptTokenCount"))
                output_count = (_token_count(usage.get("candidatesTokenCount"))
                                + _token_count(usage.get("thoughtsTokenCount")))
                metadata.update(_gemini_usage(data, usage))
                metadata["resolved_model"] = data.get("modelVersion")
                candidates = data.get("candidates") or []
                candidate = candidates[0] if candidates else {}
                metadata["finish_reason"] = candidate.get("finishReason")
                parts = (candidate.get("content") or {}).get("parts") or []
                content = "".join(part.get("text", "") for part in parts if not part.get("thought"))
                metadata["billing_status"] = _billing_status(data, resp.status, metadata["usage_known"])
                success = resp.status == 200 and bool(candidates)
                return LLMResponse(
                    content, model, LLMProvider.GEMINI,
                    input_tokens=_token_count(usage.get("promptTokenCount")),
                    output_tokens=output_count,
                    latency_ms=(time.perf_counter() - start_time) * 1000,
                    success=success,
                    error=None if success else f"Gemini HTTP {resp.status} or missing candidates",
                    **metadata,
                )
        except asyncio.CancelledError as exc:
            # Preserve the original exception type: asyncio.timeout checks it exactly.
            exc._llm_response = LLMResponse(
                "", model, LLMProvider.GEMINI, success=False,
                input_tokens=input_count, output_tokens=output_count,
                error="Request cancelled",
                latency_ms=(time.perf_counter() - start_time) * 1000,
                **metadata,
            )
            raise
        except Exception as exc:
            if isinstance(exc, aiohttp.ClientConnectorError) and metadata.get("http_status") is None:
                metadata.update(request_sent=False, billing_status="not_sent")
            return LLMResponse(
                "", model, LLMProvider.GEMINI, success=False,
                input_tokens=input_count, output_tokens=output_count,
                error="Request timeout" if isinstance(exc, asyncio.TimeoutError) else "Gemini transport failure",
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
            "fallback": (LLMProvider.GEMINI, "heavy"),
        },
        LLMTask.THEME_DETECTION: {
            "primary": (LLMProvider.GEMINI, "light"),
            "fallback": (LLMProvider.OPENAI, "light"),
        },
        LLMTask.STOCK_MAPPING: {
            "primary": (LLMProvider.GEMINI, "light"),
            "fallback": (LLMProvider.OPENAI, "light"),
        },
        LLMTask.NEWS_SUMMARY: {
            "primary": (LLMProvider.GEMINI, "light"),
            "fallback": (LLMProvider.OPENAI, "light"),
        },
        LLMTask.MARKET_ANALYSIS: {
            "primary": (LLMProvider.OPENAI, "heavy"),
            "fallback": (LLMProvider.GEMINI, "heavy"),
        },
        LLMTask.TRADE_REVIEW: {
            "primary": (LLMProvider.OPENAI, "heavy"),
            "fallback": (LLMProvider.GEMINI, "heavy"),
        },
        LLMTask.QUICK_CLASSIFY: {
            "primary": (LLMProvider.GEMINI, "light"),
            "fallback": (LLMProvider.OPENAI, "light"),
        },
        LLMTask.STRATEGY_ANALYSIS: {
            "primary": (LLMProvider.OPENAI, "heavy"),
            "fallback": (LLMProvider.GEMINI, "heavy"),
        },
        LLMTask.QUICK_ANALYSIS: {
            "primary": (LLMProvider.GEMINI, "light"),
            "fallback": (LLMProvider.OPENAI, "light"),
        },
        LLMTask.WIKI_INGEST: {
            "primary": (LLMProvider.GEMINI, "light"),
            "fallback": (LLMProvider.OPENAI, "light"),
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
                    economy_summary_models=(self.config.openai_model_light, self.config.gemini_model_light),
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
        for field_name in ("openai_model_heavy", "openai_model_light", "gemini_model_heavy",
                           "gemini_model_light", "openai_model_light_shadow"):
            model = getattr(self.config, field_name)
            if field_name == "openai_model_light_shadow" and model == "":
                continue
            if not isinstance(model, str) or not model.strip():
                warning = f"{field_name}:invalid_model"
            elif rate_quote(model, 0, 0) is None:
                warning = f"{field_name}:unpriced_model"
            elif model not in (_OPENAI_EFFORTS if field_name.startswith("openai_") else _GEMINI_LEVELS):
                warning = f"{field_name}:unsupported_provider_controls"
            else:
                continue
            self.configuration_warnings.append(warning)
            logger.warning(f"[LLM] configuration preflight: {warning}")

        self.openai = OpenAIClient(self.config)
        self.gemini = GeminiClient(self.config)

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
            f"LLMManager 초기화: OpenAI={'설정됨' if self.config.openai_api_key else '없음'}, "
            f"Gemini={'설정됨' if self.config.gemini_api_key else '없음'}"
        )

    def _get_client(self, provider: LLMProvider) -> BaseLLMClient:
        """제공자별 클라이언트 반환"""
        if provider == LLMProvider.OPENAI:
            return self.openai
        return self.gemini

    def _get_model(self, provider: LLMProvider, weight: str) -> str:
        """제공자와 가중치에 따른 모델명 반환"""
        if provider == LLMProvider.OPENAI:
            return self.config.openai_model_heavy if weight == "heavy" else self.config.openai_model_light
        else:
            return self.config.gemini_model_heavy if weight == "heavy" else self.config.gemini_model_light

    def _check_budget(self) -> bool:
        """Process-local post-usage gate, not a strict global spending cap.

        Concurrent/in-flight calls, unknown timeout charges and process restarts
        prevent strict enforcement. Unknown cost blocks subsequent paid attempts.
        """
        # 날짜가 바뀌면 리셋
        today = datetime.now().date()
        if today > self._usage_reset_date:
            self.daily_usage = LLMUsage()
            self._usage_reset_date = today

        return (not self.daily_usage.cost_missing
                and self.daily_usage.estimated_cost_usd < self.config.daily_budget_usd)

    async def complete(
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
                budget_block_reason="daily_unknown_cost" if self.daily_usage.cost_missing else "daily_limit_exceeded",
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
            primary_client, prompt, system, primary_model, task.value, attempt_role, **kwargs
        )

        if response.success:
            self.stats["success_count"] += 1
            self._maybe_fire_shadow(prompt, system, task, kwargs, response)
            return response

        if response.budget_block_reason:
            self.stats["error_count"] += 1
            self.stats["fallback_skipped_monthly_budget"] = self.stats.get("fallback_skipped_monthly_budget", 0) + 1
            return response

        if not self._check_budget():
            self.stats["error_count"] += 1
            reason = ("fallback_skipped_unknown_cost" if self.daily_usage.cost_missing
                      else "fallback_skipped_budget")
            self.stats[reason] = self.stats.get(reason, 0) + 1
            return response

        # Fallback 시도
        logger.warning(f"Primary LLM 실패 ({primary_provider.value}): {response.error}, 폴백 시도")
        self.stats["fallback_count"] += 1

        fallback_provider, fallback_weight = task_config["fallback"]
        fallback_client = self._get_client(fallback_provider)
        fallback_model = self._get_model(fallback_provider, fallback_weight)

        response = await self._complete_attempt(
            fallback_client, prompt, system, fallback_model, task.value, "fallback", **kwargs
        )

        if response.success:
            self.stats["success_count"] += 1
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

        if not self._check_budget():
            logger.warning("일일 LLM 예산 초과")
            return LLMResponse(
                content="", model="", provider=provider,
                success=False, error="Daily budget exceeded",
                request_sent=False, usage_known=False, billing_status="not_sent",
                budget_block_reason="daily_unknown_cost" if self.daily_usage.cost_missing else "daily_limit_exceeded",
            )

        client = self._get_client(provider)
        model = self._get_model(provider, weight)

        response = await self._complete_attempt(
            client, prompt, system, model, "complete_with", "primary", **kwargs
        )
        attempts = 0
        while (retry_on_empty > attempts
               and response.success
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

    def _settle_monthly(self, token: Optional[str], response: LLMResponse, model: str) -> None:
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
                self.monthly_budget.settle(token, outcome="unknown")
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
            provider = LLMProvider.OPENAI if _client is self.openai else LLMProvider.GEMINI
            response = self._monthly_blocked_response(provider, _model, "manager_closing")
            response.error = "LLM manager closing"
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
                provider = LLMProvider.OPENAI if _client is self.openai else LLMProvider.GEMINI
                response = self._monthly_blocked_response(provider, _model, "manager_closing")
            response.budget_block_reason = "manager_closing"
            return response

    def _attempt_done(self, task: asyncio.Task) -> None:
        self._attempt_tasks.discard(task)
        if not task.cancelled():
            task.exception()  # 호출자 종료 후 남은 요청의 예외도 회수한다.
        self._start_resource_close()

    async def _run_complete_attempt(self, _client: BaseLLMClient, _prompt: str, _system: str,
                                    _model: str, _task: str, _role: str, **kwargs) -> LLMResponse:
        provider = LLMProvider.OPENAI if _client is self.openai else LLMProvider.GEMINI
        token = None
        priority = kwargs.pop("budget_priority", "standard")
        if _role == "shadow":
            priority = "research"
        policy = None
        original_model, original_kwargs = _model, dict(kwargs)
        if self.config.monthly_budget_usd is not None:
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
                                supported = (_OPENAI_EFFORTS if provider == LLMProvider.OPENAI else _GEMINI_LEVELS)
                                if 'low' not in supported.get(_model, set()):
                                    raise BudgetBlocked('unsupported_economy_controls')
                                if provider == LLMProvider.OPENAI:
                                    kwargs['reasoning_effort'] = 'low'
                                else:
                                    kwargs['thinking_level'] = 'low'
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
                self._annotate_budget(response, policy, priority)
                self._account_attempt(response, _task, _role)
                return response
        try:
            response = await _client.complete(_prompt, _system, model=_model, **kwargs)
        except asyncio.CancelledError as exc:
            response = getattr(exc, "_llm_response", None)
            if response is None:
                response = LLMResponse("", _model, provider, success=False,
                                       error="Request cancelled", usage_known=False, billing_status="unknown")
            if response is not None:
                self._annotate_budget(response, policy, priority)
                self._settle_monthly(token, response, _model)
                self._account_attempt(response, _task, _role)
            exc._llm_response = response
            raise
        except ValueError as exc:
            # Transport validation errors are local and retain their public raise contract.
            unsent = LLMResponse("", _model, provider, success=False, error="Invalid local controls",
                                 request_sent=False, usage_known=False, billing_status="not_sent")
            self._annotate_budget(unsent, policy, priority)
            self._settle_monthly(token, unsent, _model)
            self._account_attempt(unsent, _task, _role)
            exc._llm_response = unsent
            raise
        except Exception as exc:
            unknown = LLMResponse("", _model, provider, success=False,
                                  error="Provider invocation failed", usage_known=False, billing_status="unknown")
            self._annotate_budget(unknown, policy, priority)
            self._settle_monthly(token, unknown, _model)
            self._account_attempt(unknown, _task, _role)
            exc._llm_response = unknown
            raise
        self._annotate_budget(response, policy, priority)
        self._settle_monthly(token, response, _model)
        self._account_attempt(response, _task, _role)
        return response

    @staticmethod
    def _annotate_budget(response, policy, priority):
        response.budget_priority = priority
        if policy:
            response.budget_policy_mode = policy['policy_mode']
            response.budget_policy_version = policy['policy_version']

    def _account_attempt(self, response: LLMResponse, task: str, role: str) -> None:
        """Record known usage even for empty/error responses before another attempt."""
        if response.billing_status == "rejected_before_generation":
            self.daily_usage.inferred_nonbillable_rejections += 1
        elif response.request_sent:
            self.daily_usage.add(response.input_tokens, response.output_tokens,
                                 response.requested_model or response.model,
                                 usage_known=response.usage_known)
        self._audit_attempt(response, task, role)

    def _audit_attempt(self, response: LLMResponse, task: str, role: str) -> None:
        """Metadata-only audit; no prompts, content, free-text errors, keys or raw payload."""
        if self.audit_dir is None:
            return
        now = datetime.now()
        today = now.strftime("%Y%m%d")
        try:
            self.audit_dir.mkdir(parents=True, exist_ok=True)
            if self._audit_log_date != today:
                cutoff = (now - timedelta(days=self._LLM_LOG_RETENTION_DAYS)).strftime("%Y%m%d")
                try:
                    for path in self.audit_dir.glob("llm-audit-*.jsonl"):
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
            quote = rate_quote(model, response.input_tokens, response.output_tokens)
            row = {
                "ts": now.isoformat(), "task": task, "provider": response.provider.value,
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
                "budget_block_reason": response.budget_block_reason,
                "budget_policy_mode": response.budget_policy_mode,
                "budget_policy_version": response.budget_policy_version,
                "budget_priority": response.budget_priority,
                "http_status": response.http_status,
                "provider_error_code": response.provider_error_code,
                "provider_error_type": response.provider_error_type,
                "provider_error_status": response.provider_error_status,
                "cost_basis": ("pre_generation_rejection_inference_not_invoice_proof"
                               if response.billing_status == "rejected_before_generation"
                               else "reported_tokens_estimate" if response.usage_known else "unknown"),
                "outcome": ("error" if not response.success else
                            "success" if (response.content or "").strip() else "empty"),
                "attempt_role": role,
                "estimated_cost_usd": quote.cost_usd if quote and response.usage_known else None,
                "price_basis": quote.basis if quote else "unpriced",
            }
            with (self.audit_dir / f"llm-audit-{today}.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
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
            if _retry == 0:
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
            "budget_remaining_usd": (None if self.daily_usage.cost_missing else
                                     round(self.config.daily_budget_usd - self.daily_usage.estimated_cost_usd, 4)),
            "cost_missing": self.daily_usage.cost_missing,
            "unpriced_requests": self.daily_usage.unpriced_requests,
            "unknown_usage_requests": self.daily_usage.unknown_usage_requests,
            "inferred_nonbillable_rejections": self.daily_usage.inferred_nonbillable_rejections,
            "budget_scope": "process_local_post_usage",
            "audit_enabled": self.audit_dir is not None,
            "configuration_warnings": list(self.configuration_warnings),
            "monthly_budget": self.get_monthly_budget_status(),
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
