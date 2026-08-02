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
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable
import aiohttp
from loguru import logger


class LLMProvider(str, Enum):
    """LLM 제공자"""
    OPENAI = "openai"
    GEMINI = "gemini"


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

    # 타임아웃 (Thinking 모델은 추론에 시간이 걸림)
    timeout_seconds: int = 120

    # 토큰 제한 (max_completion_tokens는 reasoning + 응답 합산)
    max_input_tokens: int = 4000
    max_output_tokens: int = 16000

    # 비용 관리
    daily_budget_usd: float = 5.0  # 일일 예산

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
        키가 없거나 빈 문자열이면 dataclass 기본값 유지.
        """
        llm_cfg = cfg or {}
        if "llm" in llm_cfg and isinstance(llm_cfg["llm"], dict):
            llm_cfg = llm_cfg["llm"]
        return cls(
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            openai_model_heavy=(llm_cfg.get("openai_model_heavy") or "gpt-5.4"),
            openai_model_light=(llm_cfg.get("openai_model_light") or "gpt-5-mini"),
            gemini_model_heavy=(
                llm_cfg.get("gemini_model_heavy") or "gemini-3.1-pro-preview"
            ),
            gemini_model_light=(
                llm_cfg.get("gemini_model_light") or "gemini-3.1-flash-lite-preview"
            ),
            openai_model_light_shadow=(llm_cfg.get("openai_model_light_shadow") or ""),
        )


@dataclass
class LLMUsage:
    """토큰 사용량 추적"""
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0

    def add(self, input_tokens: int, output_tokens: int, model: str):
        """사용량 추가"""
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

        # 비용 추정 (대략적)
        if "gpt-5-mini" in model:
            self.estimated_cost_usd += (input_tokens * 0.0004 + output_tokens * 0.0016) / 1000
        elif "gpt-5" in model:
            self.estimated_cost_usd += (input_tokens * 0.003 + output_tokens * 0.015) / 1000
        elif "gpt-4" in model:
            self.estimated_cost_usd += (input_tokens * 0.01 + output_tokens * 0.03) / 1000
        elif "gpt-3.5" in model:
            self.estimated_cost_usd += (input_tokens * 0.0005 + output_tokens * 0.0015) / 1000
        elif "gemini" in model:
            # gemini-3-flash-preview, gemini-2.5-pro 등
            if "pro" in model:
                self.estimated_cost_usd += (input_tokens * 0.00125 + output_tokens * 0.005) / 1000
            else:
                self.estimated_cost_usd += (input_tokens * 0.000075 + output_tokens * 0.0003) / 1000


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
        """텍스트 생성"""
        if not self.api_key:
            return LLMResponse(
                content="", model="", provider=LLMProvider.OPENAI,
                success=False, error="OpenAI API key not configured"
            )

        model = model if model is not None else self.config.openai_model_light
        max_tokens = max_tokens if max_tokens is not None else self.config.max_output_tokens

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        start_time = datetime.now()

        try:
            session = await self._get_session()

            # GPT-5 시리즈 모두 max_completion_tokens 사용 (OpenAI API v2 정책)
            # GPT-5 전체(gpt-5-mini 포함)가 temperature 커스텀 미지원 (기본값 1만 허용)
            is_gpt5 = model.startswith("gpt-5")
            is_thinking_no_temp = is_gpt5

            body = {"model": model, "messages": messages}
            # OpenAI API 최신 권장: 모든 모델에서 max_completion_tokens 사용
            body["max_completion_tokens"] = max_tokens

            if not is_thinking_no_temp:
                body["temperature"] = temperature

            # 추론 강도 (gpt-5 계열 전용).
            # gpt-5는 추론 토큰을 max_completion_tokens 안에서 소비하므로,
            # 판정(YES/NO)처럼 단순한 작업에 기본 추론 강도를 쓰면
            # 토큰을 추론에만 다 쓰고 본문이 빈 문자열로 온다 (success=True, content='').
            # 짧은 판정 작업은 reasoning_effort="low"로 호출할 것.
            reasoning_effort = kwargs.get("reasoning_effort")
            if is_gpt5 and reasoning_effort:
                body["reasoning_effort"] = reasoning_effort

            async with session.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            ) as resp:
                data = await resp.json()

                if resp.status != 200:
                    error_msg = data.get("error", {}).get("message", str(data))
                    return LLMResponse(
                        content="", model=model, provider=LLMProvider.OPENAI,
                        success=False, error=error_msg
                    )

                content = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})

                latency = (datetime.now() - start_time).total_seconds() * 1000

                return LLMResponse(
                    content=content,
                    model=model,
                    provider=LLMProvider.OPENAI,
                    input_tokens=usage.get("prompt_tokens", 0),
                    output_tokens=usage.get("completion_tokens", 0),
                    latency_ms=latency,
                )

        except asyncio.TimeoutError:
            return LLMResponse(
                content="", model=model, provider=LLMProvider.OPENAI,
                success=False, error="Request timeout"
            )
        except Exception as e:
            return LLMResponse(
                content="", model=model, provider=LLMProvider.OPENAI,
                success=False, error=str(e)
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
        """텍스트 생성"""
        if not self.api_key:
            return LLMResponse(
                content="", model="", provider=LLMProvider.GEMINI,
                success=False, error="Gemini API key not configured"
            )

        model = model if model is not None else self.config.gemini_model_light
        max_tokens = max_tokens if max_tokens is not None else self.config.max_output_tokens

        # 시스템 프롬프트를 사용자 프롬프트에 통합
        full_prompt = f"{system}\n\n{prompt}" if system else prompt

        start_time = datetime.now()

        try:
            session = await self._get_session()

            url = f"{self.base_url}/models/{model}:generateContent?key={self.api_key}"

            async with session.post(
                url,
                headers={"Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": full_prompt}]}],
                    "generationConfig": {
                        "temperature": temperature,
                        "maxOutputTokens": max_tokens,
                    }
                }
            ) as resp:
                data = await resp.json()

                if resp.status != 200:
                    error_msg = data.get("error", {}).get("message", str(data))
                    return LLMResponse(
                        content="", model=model, provider=LLMProvider.GEMINI,
                        success=False, error=error_msg
                    )

                # 응답 추출
                candidates = data.get("candidates", [])
                if not candidates:
                    return LLMResponse(
                        content="", model=model, provider=LLMProvider.GEMINI,
                        success=False, error="No candidates in response"
                    )

                parts = candidates[0].get("content", {}).get("parts", [])
                content = parts[0].get("text", "") if parts else ""

                # 토큰 사용량 (Gemini는 usageMetadata 제공)
                usage = data.get("usageMetadata", {})

                latency = (datetime.now() - start_time).total_seconds() * 1000

                return LLMResponse(
                    content=content,
                    model=model,
                    provider=LLMProvider.GEMINI,
                    input_tokens=usage.get("promptTokenCount", 0),
                    output_tokens=usage.get("candidatesTokenCount", 0),
                    latency_ms=latency,
                )

        except asyncio.TimeoutError:
            return LLMResponse(
                content="", model=model, provider=LLMProvider.GEMINI,
                success=False, error="Request timeout"
            )
        except Exception as e:
            return LLMResponse(
                content="", model=model, provider=LLMProvider.GEMINI,
                success=False, error=str(e)
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
    # 2026-06-17 Phase 3: Shadow A/B 비교 로그 (light 후속 모델 마이그레이션)
    _SHADOW_LOG_DIR = Path.home() / ".cache" / "ai_trader" / "llm_shadow"

    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or LLMConfig.from_env()

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
        """일일 예산 체크"""
        # 날짜가 바뀌면 리셋
        today = datetime.now().date()
        if today > self._usage_reset_date:
            self.daily_usage = LLMUsage()
            self._usage_reset_date = today

        return self.daily_usage.estimated_cost_usd < self.config.daily_budget_usd

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

        # 예산 체크
        if not self._check_budget():
            logger.warning("일일 LLM 예산 초과")
            return LLMResponse(
                content="", model="", provider=LLMProvider.OPENAI,
                success=False, error="Daily budget exceeded"
            )

        # 작업별 설정 가져오기
        task_config = self.TASK_CONFIG.get(task, self.TASK_CONFIG[LLMTask.QUICK_CLASSIFY])

        # Primary 시도
        primary_provider, primary_weight = task_config["primary"]
        primary_client = self._get_client(primary_provider)
        primary_model = self._get_model(primary_provider, primary_weight)

        response = await primary_client.complete(prompt, system, model=primary_model, **kwargs)

        if response.success:
            self.stats["success_count"] += 1
            self.daily_usage.add(response.input_tokens, response.output_tokens, response.model)
            self._maybe_fire_shadow(prompt, system, task, kwargs, response)
            return response

        # Fallback 시도
        logger.warning(f"Primary LLM 실패 ({primary_provider.value}): {response.error}, 폴백 시도")
        self.stats["fallback_count"] += 1

        fallback_provider, fallback_weight = task_config["fallback"]
        fallback_client = self._get_client(fallback_provider)
        fallback_model = self._get_model(fallback_provider, fallback_weight)

        response = await fallback_client.complete(prompt, system, model=fallback_model, **kwargs)

        if response.success:
            self.stats["success_count"] += 1
            self.daily_usage.add(response.input_tokens, response.output_tokens, response.model)
            self._maybe_fire_shadow(prompt, system, task, kwargs, response)
        else:
            self.stats["error_count"] += 1
            logger.error(f"Fallback LLM도 실패 ({fallback_provider.value}): {response.error}")

        return response

    async def complete_with(
        self,
        prompt: str,
        provider: LLMProvider,
        weight: str = "light",
        system: str = "",
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
        """
        self.stats["total_requests"] += 1

        if not self._check_budget():
            logger.warning("일일 LLM 예산 초과")
            return LLMResponse(
                content="", model="", provider=provider,
                success=False, error="Daily budget exceeded"
            )

        client = self._get_client(provider)
        model = self._get_model(provider, weight)
        response = await client.complete(prompt, system, model=model, **kwargs)

        if response.success:
            self.stats["success_count"] += 1
            self.daily_usage.add(response.input_tokens, response.output_tokens, response.model)
        else:
            self.stats["error_count"] += 1

        return response

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
        if not shadow_model:
            return
        if primary_response.model != self.config.openai_model_light:
            return
        try:
            asyncio.create_task(
                self._fire_shadow(prompt, system, task, kwargs, primary_response, shadow_model)
            )
        except Exception as e:
            logger.debug(f"[LLM-Shadow] 스케줄 실패: {e}")

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
        import time
        import uuid
        pair_id = uuid.uuid4().hex[:12]
        t0 = time.time()
        try:
            shadow_resp = await self.openai.complete(
                prompt, system, model=shadow_model, **kwargs
            )
        except Exception as e:
            shadow_resp = LLMResponse(
                content="", model=shadow_model, provider=LLMProvider.OPENAI,
                success=False, error=str(e)[:200],
            )
        latency_ms = int((time.time() - t0) * 1000)
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
        response = await self.complete(prompt, task, system, **kwargs)

        if not response.success:
            self._log_llm_response(
                task=task.value, model=response.model or "",
                raw=response.error or "", parsed=None, success=False,
            )
            return {"error": response.error}

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
            "budget_remaining_usd": round(self.config.daily_budget_usd - self.daily_usage.estimated_cost_usd, 4),
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
        _llm_manager = LLMManager(config=_pending_config)
    return _llm_manager
