"""
엔진 탭 API — 자가수정 에이전트 상태 + 엔진 로그 + LLM 운영 루프
"""

import asyncio
import json
import re
import time
from pathlib import Path

from aiohttp import web

CACHE_DIR = Path.home() / ".cache" / "ai_trader"
SELF_HEALER_DIR = Path(__file__).parent.parent.parent / "scripts" / "self_healer"
STATE_FILE = SELF_HEALER_DIR / "state.json"
HISTORY_FILE = CACHE_DIR / "self_healer_history.json"
SERVICE_NAME = "qwq-ai-trader"
HEALER_SERVICE = "qwq-self-healer"

# 로그 NOISE 패턴 (patterns.yaml 동기화)
NOISE_PATTERNS = [
    "잔고 조회 실패.*주말",
    "asyncio.CancelledError",
    "WebSocket.*reconnect",
    "heartbeat",
    "pykrx.*failed.*no valid cache",
    "Stock master.*failed",
    "scikit-learn",
    "mcp.*미설치",
    "MCP.*실패.*무시",
    "No module named.*mcp",
    "장외 시간",
    "WS.*장 시작",
    "주말.*API",
    "IBK투자증권",
    "ClientConnectorError",
    "Session is closed",
]

VALID_LOG_LEVELS = {"error", "warning", "info"}

# NOISE 패턴 사전 컴파일
NOISE_COMPILED = [re.compile(p, re.IGNORECASE) for p in NOISE_PATTERNS]

# 메모리 캐시 (systemctl 반복 호출 방지)
_status_cache = {"data": None, "ts": 0}
_STATUS_CACHE_TTL = 5


def _read_json_safe(path: Path, default=None):
    """JSON 파일 안전 읽기"""
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return default


class EngineAPIHandler:
    """엔진 탭 API 핸들러"""

    async def get_healer_status(self, request: web.Request) -> web.Response:
        """GET /api/engine/healer/status — 자가수정 에이전트 상태"""
        now = time.time()

        # 캐시 TTL 체크
        if _status_cache["data"] is not None and (now - _status_cache["ts"]) < _STATUS_CACHE_TTL:
            return web.json_response(_status_cache["data"])

        # systemctl is-active (비동기)
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "is-active", HEALER_SERVICE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            service_active = stdout.decode().strip() == "active"
        except Exception:
            service_active = False

        # state.json 읽기
        state = _read_json_safe(STATE_FILE, {})
        fixes_today = state.get("fixes_today", 0)
        last_ts = state.get("last_fix_timestamp", 0)
        cooldown_remaining = max(0, 300 - (now - last_ts)) if last_ts > 0 else 0
        history = state.get("history", [])
        last_fix = history[-1] if history else None

        data = {
            "service_active": service_active,
            "fixes_today": fixes_today,
            "max_fixes_per_day": 3,
            "cooldown_remaining_secs": int(cooldown_remaining),
            "last_fix_at": last_fix.get("timestamp") if last_fix else None,
            "last_fix_summary": last_fix.get("summary") if last_fix else None,
        }

        _status_cache["data"] = data
        _status_cache["ts"] = now
        return web.json_response(data)

    async def get_healer_history(self, request: web.Request) -> web.Response:
        """GET /api/engine/healer/history — 수정 이력"""
        # 영속 히스토리 우선, 없으면 state.json 히스토리
        history = _read_json_safe(HISTORY_FILE, None)
        if history is None:
            state = _read_json_safe(STATE_FILE, {})
            history = state.get("history", [])

        # 최신순 정렬
        history = list(reversed(history[-50:]))
        return web.json_response(history)

    async def get_logs(self, request: web.Request) -> web.Response:
        """GET /api/engine/logs — 엔진 로그 (ERROR/WARNING 필터)"""
        # 파라미터 화이트리스트 검증
        level_param = request.query.get("level", "error,warning")
        requested_levels = {l.strip().lower() for l in level_param.split(",")}
        levels = requested_levels & VALID_LOG_LEVELS
        if not levels:
            levels = {"error", "warning"}

        noise = request.query.get("noise", "hide") != "show"
        try:
            limit = min(int(request.query.get("limit", "100")), 200)
        except (ValueError, TypeError):
            limit = 100

        # journalctl 비동기 실행
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "journalctl", "-u", SERVICE_NAME,
                "-n", "500", "--no-pager",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            raw_lines = stdout.decode("utf-8", errors="replace").splitlines()
        except asyncio.TimeoutError:
            if proc:
                proc.kill()
                await proc.wait()
            return web.json_response({"logs": [], "total": 0, "noise_filtered": 0})
        except Exception:
            if proc:
                proc.kill()
                await proc.wait()
            return web.json_response({"logs": [], "total": 0, "noise_filtered": 0})

        # 파싱 + 필터링
        logs = []
        noise_count = 0
        # journalctl 라인: "Mar 08 15:49:40 HOST SERVICE[PID]: 15:49:40 | LEVEL | source | message"
        log_pattern = re.compile(
            r"(\d{2}:\d{2}:\d{2})\s*\|\s*(ERROR|WARNING|INFO)\s*\|\s*([^|]+)\|\s*(.*)",
            re.IGNORECASE,
        )

        for raw_line in reversed(raw_lines):
            m = log_pattern.search(raw_line)
            if not m:
                continue

            ts, level_str, source, message = m.group(1), m.group(2).upper(), m.group(3).strip(), m.group(4).strip()

            if level_str.lower() not in levels:
                continue

            # NOISE 필터
            if noise:
                is_noise = False
                for np in NOISE_COMPILED:
                    if np.search(message):
                        is_noise = True
                        noise_count += 1
                        break
                if is_noise:
                    continue

            logs.append({
                "timestamp": ts,
                "level": level_str,
                "source": source,
                "message": message,
            })

            if len(logs) >= limit:
                break

        return web.json_response({
            "logs": logs,
            "total": len(logs),
            "noise_filtered": noise_count,
        })

    async def get_llm_regime(self, request: web.Request) -> web.Response:
        """GET /api/engine/llm-regime — LLM 레짐 현황"""
        data = _read_json_safe(CACHE_DIR / "llm_regime_today.json")
        if data is None:
            return web.json_response({"empty": True, "message": "오늘 레짐 미분류"})
        return web.json_response(data)

    async def get_daily_bias(self, request: web.Request) -> web.Response:
        """GET /api/engine/daily-bias — Daily Bias"""
        data = _read_json_safe(CACHE_DIR / "daily_bias.json")
        if data is None:
            return web.json_response({"empty": True, "message": "Daily Bias 미생성"})
        return web.json_response(data)

    async def get_false_negatives(self, request: web.Request) -> web.Response:
        """GET /api/engine/false-negatives — False Negative 분석"""
        data = _read_json_safe(CACHE_DIR / "false_negative_patterns.json", [])
        if not data:
            return web.json_response({"latest": None, "history": []})

        latest = data[-1] if data else None
        history = [{"date": e.get("date", ""), "missed_count": e.get("missed_count", 0)} for e in data]

        return web.json_response({"latest": latest, "history": history})


def setup_engine_api_routes(app: web.Application):
    """엔진 API 라우트 등록"""
    handler = EngineAPIHandler()

    app.router.add_get("/api/engine/healer/status", handler.get_healer_status)
    app.router.add_get("/api/engine/healer/history", handler.get_healer_history)
    app.router.add_get("/api/engine/logs", handler.get_logs)
    app.router.add_get("/api/engine/llm-regime", handler.get_llm_regime)
    app.router.add_get("/api/engine/daily-bias", handler.get_daily_bias)
    app.router.add_get("/api/engine/false-negatives", handler.get_false_negatives)
