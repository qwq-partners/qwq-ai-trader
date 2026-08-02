"""
로컬 Codex CLI 클라이언트 — 배치성 LLM 작업을 OpenAI API 대신 구독 한도로 처리한다.

`codex exec`는 비대화형 실행을 지원하고, **최종 응답만 stdout으로** 내보낸다
(진행 로그는 stderr). 덕분에 subprocess로 붙이기 쉽다.

API 대비 이점:
    - ChatGPT 구독 한도를 쓰므로 **API 과금이 발생하지 않는다**
    - `--output-schema`로 JSON 응답 구조를 강제할 수 있어 파싱 실패가 없다

대신 프로세스 기동 오버헤드가 2~3초 있고 실측 응답이 7~12초라,
**실시간 매매 경로에는 쓰지 않는다.** 배치 작업(거래 복기, 전략 진화, 주간 분석)에만 쓴다.

■ 실행 시 반드시 지킬 것 (실측으로 확인한 함정)

1. **stdin을 안 쓸 때도 반드시 닫아야 한다.**
   그냥 두면 `Reading additional input from stdin...` 상태로 무한 대기한다.
   (배경 실행에서 25분을 통째로 날린 적이 있다.)
2. stdout과 stderr를 합치지 말 것. 합치면 진행 로그가 응답에 섞인다.
3. 파일 접근이 필요하면 이 호스트에서는 `danger-full-access`가 필요하다 —
   bubblewrap이 네트워크 네임스페이스를 만들지 못해
   (`bwrap: loopback: Failed RTM_NEWADDR`) 샌드박스를 쓰는 모드가 전부 실패한다.
   데이터를 stdin으로 넘기는 순수 분석이라면 `read-only`로 충분하다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

# 기본값 — config `llm.codex`에서 덮어쓸 수 있다
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_TIMEOUT = 240.0
DEFAULT_SANDBOX = "read-only"


@dataclass
class CodexResponse:
    """Codex 실행 결과 (LLMResponse와 호환되는 최소 형태)"""
    content: str
    model: str
    success: bool = True
    error: Optional[str] = None
    latency_ms: float = 0.0

    def json(self) -> Optional[Dict[str, Any]]:
        """구조화 응답 파싱 (--output-schema 사용 시)"""
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


class CodexClient:
    """`codex exec` 래퍼"""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        sandbox: str = DEFAULT_SANDBOX,
        cwd: Optional[str] = None,
        binary: str = "codex",
    ):
        self.model = model
        self.timeout = timeout
        self.sandbox = sandbox
        self.cwd = cwd or str(Path(__file__).resolve().parents[2])
        self.binary = binary
        self.stats = {"calls": 0, "success": 0, "failed": 0, "timeout": 0}

    # ── 가용성 ─────────────────────────────────────────────
    def is_available(self) -> bool:
        """codex 실행 파일이 있는지 (인증 여부는 호출 시 판별)"""
        return shutil.which(self.binary) is not None

    # ── 실행 ───────────────────────────────────────────────
    async def complete(
        self,
        prompt: str,
        *,
        input_data: Optional[str] = None,
        output_schema: Optional[Dict[str, Any]] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
        sandbox: Optional[str] = None,
    ) -> CodexResponse:
        """
        Codex를 1회 실행한다.

        Args:
            prompt: 지시문 (인자로 전달)
            input_data: stdin으로 넘길 데이터. 넘기면 `<stdin>` 블록으로 붙는다.
                        거래 내역 JSON처럼 큰 입력에 쓴다.
            output_schema: JSON Schema. 주면 응답이 그 구조를 따르도록 강제된다.
            sandbox: read-only | workspace-write | danger-full-access
        """
        mdl = model or self.model
        tmo = timeout or self.timeout
        sbx = sandbox or self.sandbox
        started = time.monotonic()
        self.stats["calls"] += 1

        if not self.is_available():
            self.stats["failed"] += 1
            return CodexResponse("", mdl, success=False, error="codex 실행 파일 없음")

        schema_path: Optional[str] = None
        try:
            args = [
                self.binary, "exec",
                "--ephemeral",              # 세션 파일 미저장
                "--skip-git-repo-check",
                "-s", sbx,
                "-m", mdl,
            ]

            if output_schema is not None:
                fd, schema_path = tempfile.mkstemp(suffix=".json", prefix="codex_schema_")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(output_schema, f, ensure_ascii=False)
                args += ["--output-schema", schema_path]

            args.append(prompt)

            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,   # 항상 연결 후 닫는다 (무한 대기 방지)
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,  # 진행 로그 — 응답과 섞지 않는다
                cwd=self.cwd,
            )

            payload = (input_data or "").encode("utf-8")
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=payload), timeout=tmo
                )
            except asyncio.TimeoutError:
                self.stats["timeout"] += 1
                self.stats["failed"] += 1
                with contextlib.suppress(Exception):
                    proc.kill()
                    await proc.wait()
                logger.warning(f"[Codex] 타임아웃 ({tmo}s) — model={mdl}")
                return CodexResponse("", mdl, success=False, error=f"타임아웃({tmo}s)")

            latency = (time.monotonic() - started) * 1000
            content = (stdout or b"").decode("utf-8", errors="replace").strip()
            err_txt = (stderr or b"").decode("utf-8", errors="replace")

            if proc.returncode != 0 or not content:
                self.stats["failed"] += 1
                # stderr에서 의미 있는 줄만 추린다 (배너·설정 덤프 제외)
                reason = ""
                for line in err_txt.splitlines():
                    if any(k in line.lower() for k in ("error", "not supported", "denied")):
                        reason = line.strip()[:200]
                        break
                reason = reason or f"exit={proc.returncode}, 빈 응답"
                logger.warning(f"[Codex] 실패: {reason}")
                return CodexResponse("", mdl, success=False, error=reason,
                                     latency_ms=latency)

            self.stats["success"] += 1
            logger.info(f"[Codex] 완료 ({latency/1000:.1f}s, model={mdl}, {len(content)}자)")
            return CodexResponse(content, mdl, success=True, latency_ms=latency)

        except Exception as e:
            self.stats["failed"] += 1
            logger.warning(f"[Codex] 실행 오류: {e}")
            return CodexResponse("", mdl, success=False, error=str(e))
        finally:
            if schema_path:
                try:
                    os.unlink(schema_path)
                except OSError:
                    pass

    def get_stats(self) -> Dict[str, Any]:
        s = dict(self.stats)
        if s["calls"]:
            s["success_rate"] = round(s["success"] / s["calls"] * 100, 1)
        return s


_client: Optional[CodexClient] = None


def get_codex_client(**kwargs) -> CodexClient:
    global _client
    if _client is None:
        _client = CodexClient(**kwargs)
    return _client
