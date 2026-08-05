"""
Manus API 클라이언트 — 배치성 LLM 작업을 OpenAI API 대신 Manus 에이전트로 처리한다.

Manus는 chat-completion이 아니라 **태스크 기반 비동기 API**다:
    POST /v2/task.create → GET /v2/task.listMessages 폴링
    → status_update.agent_status == "stopped" 시 assistant_message /
      structured_output_result에서 결과 수집

응답이 수십 초~수 분 걸리므로 **실시간 매매 경로에는 쓰지 않는다.**
배치 작업(거래 복기, 전략 진화, 주간 분석)에만 쓴다.

- 인증: `x-manus-api-key` 헤더 (.env MANUS_API_KEY)
- 구조화 응답: task.create에 structured_output_schema를 주면
  완료 후 structured_output_result 이벤트로 검증된 JSON이 온다
- interactive_mode는 쓰지 않는다(기본 false) — 배치 경로에서 되물음 처리 불가.
  그래도 waiting 상태가 오면 task.stop 후 실패 처리한다.

문서: https://open.manus.ai/docs/v2/introduction
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import aiohttp
from loguru import logger

BASE_URL = "https://api.manus.ai"
# 기본값 — config `llm.manus`에서 덮어쓸 수 있다
DEFAULT_PROFILE = "manus-1.6"
DEFAULT_TIMEOUT = 600.0
POLL_INTERVAL = 5.0


@dataclass
class ManusResponse:
    """Manus 실행 결과 (기존 CodexResponse와 호환되는 최소 형태)"""
    content: str
    model: str
    success: bool = True
    error: Optional[str] = None
    latency_ms: float = 0.0

    def json(self) -> Optional[Dict[str, Any]]:
        """구조화 응답 파싱 (structured_output_schema 사용 시)"""
        if not self.content:
            return None
        try:
            return json.loads(self.content)
        except json.JSONDecodeError:
            # 스키마를 안 준 경우 코드펜스가 붙어올 수 있다
            txt = self.content.strip()
            if txt.startswith("```"):
                txt = txt.split("```")[1]
                if txt.startswith("json"):
                    txt = txt[4:]
                try:
                    return json.loads(txt.strip())
                except json.JSONDecodeError:
                    return None
            return None


class ManusClient:
    """Manus API v2 래퍼 (task.create + 폴링)"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        agent_profile: str = DEFAULT_PROFILE,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.api_key = api_key or os.getenv("MANUS_API_KEY", "")
        self.agent_profile = agent_profile
        self.timeout = timeout
        self.stats = {"calls": 0, "success": 0, "failed": 0, "timeout": 0}

    # ── 가용성 ─────────────────────────────────────────────
    def is_available(self) -> bool:
        """API 키가 설정돼 있는지 (유효성은 호출 시 판별)"""
        return bool(self.api_key)

    # ── 실행 ───────────────────────────────────────────────
    async def complete(
        self,
        prompt: str,
        *,
        input_data: Optional[str] = None,
        output_schema: Optional[Dict[str, Any]] = None,
        agent_profile: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> ManusResponse:
        """
        Manus 태스크를 1건 실행하고 완료까지 폴링한다.

        Args:
            prompt: 지시문
            input_data: 거래 내역 JSON처럼 큰 입력. 주면 <data> 블록으로 붙는다.
            output_schema: JSON Schema. 주면 structured_output_result로 강제된다.
            agent_profile: manus-1.6 | manus-1.6-lite | manus-1.6-max
        """
        profile = agent_profile or self.agent_profile
        tmo = timeout if timeout is not None else self.timeout
        started = time.monotonic()
        self.stats["calls"] += 1

        def _fail(reason: str) -> ManusResponse:
            self.stats["failed"] += 1
            logger.warning(f"[Manus] 실패: {reason}")
            return ManusResponse("", profile, success=False, error=reason,
                                 latency_ms=(time.monotonic() - started) * 1000)

        if not self.api_key:
            return _fail("MANUS_API_KEY 미설정")

        content = prompt if not input_data else f"{prompt}\n\n<data>\n{input_data}\n</data>"
        payload: Dict[str, Any] = {
            "message": {"content": content},
            "agent_profile": profile,
            "hide_in_task_list": True,       # 웹앱 태스크 목록 오염 방지
            "share_visibility": "private",
        }
        if output_schema is not None:
            payload["structured_output_schema"] = output_schema

        task_id: Optional[str] = None
        try:
            async with aiohttp.ClientSession(
                headers={"x-manus-api-key": self.api_key},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as sess:
                # 1) 태스크 생성
                async with sess.post(f"{BASE_URL}/v2/task.create", json=payload) as r:
                    status_code = r.status
                    data = await r.json(content_type=None)
                if not data.get("ok"):
                    err = (data.get("error") or {}).get("message") or f"HTTP {status_code}"
                    return _fail(f"task.create: {err}")
                task_id = data["task_id"]
                logger.debug(f"[Manus] 태스크 생성 {task_id} (profile={profile})")

                # 2) 완료 폴링
                deadline = started + tmo
                while time.monotonic() < deadline:
                    await asyncio.sleep(POLL_INTERVAL)
                    async with sess.get(
                        f"{BASE_URL}/v2/task.listMessages",
                        params={"task_id": task_id, "order": "desc", "limit": "50"},
                    ) as r:
                        msgs = await r.json(content_type=None)
                    if not msgs.get("ok"):
                        continue  # 일시 오류 — 다음 폴링에서 재시도
                    events: List[Dict[str, Any]] = msgs.get("messages") or []

                    # desc 정렬이므로 첫 status_update가 최신 상태
                    status = next(
                        ((e.get("status_update") or {}).get("agent_status")
                         for e in events if e.get("type") == "status_update"),
                        None,
                    )
                    if status == "stopped":
                        return self._extract(events, output_schema, profile, started)
                    if status == "error":
                        detail = next(
                            (str(e.get("error_message"))[:200]
                             for e in events if e.get("type") == "error_message"),
                            "에이전트 오류",
                        )
                        return _fail(f"태스크 오류: {detail}")
                    if status == "waiting":
                        # 배치 경로 — 추가 입력에 응답할 수 없으므로 중단
                        await self._stop(sess, task_id)
                        return _fail("에이전트가 추가 입력 대기 (배치 경로 미지원)")

                # 타임아웃 — 태스크를 멈춰 크레딧 낭비 방지
                self.stats["timeout"] += 1
                await self._stop(sess, task_id)
                return _fail(f"타임아웃({tmo}s)")

        except Exception as e:
            return _fail(f"실행 오류: {e}")

    def _extract(
        self,
        events: List[Dict[str, Any]],
        output_schema: Optional[Dict[str, Any]],
        profile: str,
        started: float,
    ) -> ManusResponse:
        """완료된 태스크 이벤트에서 최종 결과를 뽑는다 (events는 desc 정렬)"""
        latency = (time.monotonic() - started) * 1000
        content = ""

        if output_schema is not None:
            sor = next(
                (e.get("structured_output_result") or {}
                 for e in events if e.get("type") == "structured_output_result"),
                None,
            )
            if sor and sor.get("success") and sor.get("value") is not None:
                content = json.dumps(sor["value"], ensure_ascii=False)

        if not content:
            # 최신 assistant_message 텍스트
            raw = next(
                ((e.get("assistant_message") or {}).get("content")
                 for e in events if e.get("type") == "assistant_message"),
                None,
            )
            if raw is not None:
                content = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)

        if not content:
            self.stats["failed"] += 1
            logger.warning("[Manus] 완료됐지만 결과 이벤트 없음")
            return ManusResponse("", profile, success=False,
                                 error="빈 응답", latency_ms=latency)

        self.stats["success"] += 1
        logger.info(f"[Manus] 완료 ({latency/1000:.1f}s, profile={profile}, {len(content)}자)")
        return ManusResponse(content, profile, success=True, latency_ms=latency)

    @staticmethod
    async def _stop(sess: aiohttp.ClientSession, task_id: str) -> None:
        """태스크 중단 (실패해도 무시 — 이미 실패 경로)"""
        with contextlib.suppress(Exception):
            async with sess.post(f"{BASE_URL}/v2/task.stop", json={"task_id": task_id}):
                pass

    def get_stats(self) -> Dict[str, Any]:
        s = dict(self.stats)
        if s["calls"]:
            s["success_rate"] = round(s["success"] / s["calls"] * 100, 1)
        return s


_client: Optional[ManusClient] = None


def get_manus_client(**kwargs) -> ManusClient:
    global _client
    if _client is None:
        _client = ManusClient(**kwargs)
    return _client
