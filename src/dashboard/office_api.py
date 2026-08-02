"""
QWQ AI Trader - 가상 오피스(Agent Virtual Office) API

픽셀아트 사무실 시각화(KbWen/agent-virtual-office, MIT)를 대시보드에 통합하기 위한
상태 브릿지. 프론트엔드는 8개 역할(role)의 상태만 알면 되므로, 여기서
"트레이딩 엔진 상태 → 8개 역할"로 환산해 내려준다.

역할 매핑 (운영 8명 + 전문가 7명 → 캐릭터 8명):
    pm       총괄     — 엔진/스케줄러/세션
    arch     체제분석 — market_regime (강세/약세/횡보)
    dev      스크리너 — 스크리닝·시그널 생성
    qa       검증관   — 크로스 검증 게이트
    ops      집행관   — 주문 집행·체결
    res      전문가팀 — 전문가 7명 + 테마 탐지
    gate     리스크   — RiskManager + 킬스위치
    designer 진화     — 자가 진화·복기

엔드포인트:
    GET  /api/office/status         현재 상태 (엔진 파생 + 외부 POST 병합)
    POST /api/office/status         외부 도구 상태 푸시 (Claude Code 훅 등)
    GET  /api/office/status/stream  SSE 푸시 (변경 시에만 전송)
    POST /api/office/lang           언어 설정 영속화

외부 POST는 역할별 TTL 5분이며, 살아있는 동안 엔진 파생 상태를 덮어쓴다.
(Claude Code로 코드를 만지는 동안에는 그 활동이 보이고, 조용해지면 자동으로
엔진 상태 표시로 되돌아온다.)

보안: OFFICE_STATUS_TOKEN 환경변수가 설정돼 있으면 POST에 Bearer 토큰을 요구한다.
"""

import asyncio
import hashlib
import hmac
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from aiohttp import web
from loguru import logger

# ─── 전송 계약 (upstream statusContract.mjs와 동일하게 유지) ───────────────
VALID_ROLES = ["pm", "arch", "dev", "qa", "ops", "res", "gate", "designer"]
VALID_STATUSES = ["idle", "working", "blocked", "done", "planning", "awaiting-approval"]
VALID_MOODS = ["normal", "rushing", "frustrated", "stuck", "smooth", "intense", "idle"]
BLOCKED_REASONS = [
    "test-run-failed", "build-failed", "deps-failed", "blocked-unknown",
    "permission-denied", "api-rate-limit", "api-auth-failed",
]
CARRY_FIELDS = ["task", "label", "hint", "reasonCode", "activeFile", "skill"]

ACTIVE_STATUSES = ("working", "blocked", "planning", "awaiting-approval")

# 외부 POST 상태 유효시간 (upstream과 동일하게 5분)
EXTERNAL_TTL_SEC = 300
# 엔진 파생 상태 캐시 (GET이 1~8초 주기로 폴링되므로 과도한 재계산 방지)
DERIVE_CACHE_SEC = 3.0
# 진화 결과 파일 확인 캐시
EVOLUTION_CACHE_SEC = 60.0
# Claude Code 훅 파일 스캔 캐시
HOOK_SCAN_CACHE_SEC = 2.0

CACHE_DIR = Path.home() / ".cache" / "ai_trader"
STATE_FILE = CACHE_DIR / "office_status.json"
LANG_FILE = CACHE_DIR / "office_lang.txt"
# Claude Code 훅(office-status-hook.js)이 세션별 상태를 남기는 위치
HOOK_DIR = Path.home() / ".claude"

MAX_BODY_BYTES = 16384

REGIME_KR = {
    "bull": "강세", "bear": "약세", "sideways": "횡보",
    "neutral": "중립", "unknown": "판단중",
}
SESSION_KR = {
    "pre_market": "장 전", "regular": "정규장", "after_hours": "시간외",
    "next": "넥스트장", "closed": "장 마감",
}


def _cap(value: Any, limit: int = 200) -> Optional[str]:
    return value[:limit] if isinstance(value, str) else None


# `value or default`은 0/0.0을 default로 되돌려 놓는다 (프로젝트 절대 금지 패턴).
# 값이 None이거나 변환 불가일 때만 default를 쓴다.
def _int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _agent(role: str, status: str, task: str = "", **extra) -> Dict[str, Any]:
    """역할 1명의 상태 dict 생성 (carry 필드 기본 None)"""
    out: Dict[str, Any] = {"role": role, "status": status}
    for field in CARRY_FIELDS:
        out[field] = None
    out["task"] = _cap(task) if task else None
    for key, val in extra.items():
        if key in CARRY_FIELDS:
            out[key] = _cap(val)
    return out


def normalize_post(body: Any) -> Dict[str, Any]:
    """POST 본문 정규화 (shorthand + full format 모두 지원)

    upstream `normalizePost`와 동일한 규칙:
      - 알 수 없는 role은 폐기, 중복 role은 첫 항목만
      - 잘못된 status는 'idle'로 강등
      - 문자열 필드는 200자 컷
    """
    if not isinstance(body, dict):
        body = {}

    agents: List[Dict[str, Any]] = []

    if body.get("type") == "office-status":
        seen = set()
        raw_agents = body.get("agents")
        if not isinstance(raw_agents, list):
            raw_agents = []
        for item in raw_agents[:50]:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            if role not in VALID_ROLES or role in seen:
                continue
            seen.add(role)
            status = item.get("status")
            agent = {
                "role": role,
                "status": status if status in VALID_STATUSES else "idle",
            }
            for field in CARRY_FIELDS:
                val = _cap(item.get(field))
                if field == "reasonCode" and val not in BLOCKED_REASONS:
                    val = None
                agent[field] = val
            agents.append(agent)
    else:
        # shorthand: {"dev": "working", "qa": "테스트 중", "workflow": "..."}
        for role in VALID_ROLES:
            val = body.get(role)
            if val is None:
                continue
            is_status = val in VALID_STATUSES
            if not is_status and not isinstance(val, str):
                continue
            agent = {
                "role": role,
                "status": val if is_status else "working",
                "task": None if is_status else _cap(val),
            }
            for field in CARRY_FIELDS:
                if field in ("task", "activeFile"):
                    continue
                v = _cap(body.get(field))
                if field == "reasonCode" and v not in BLOCKED_REASONS:
                    v = None
                agent[field] = v
            agents.append(agent)
        # activeFile은 단일 역할일 때만 부여 (여러 명이 같은 파일을 편집하는 것처럼 보이는 왜곡 방지)
        active_file = _cap(body.get("activeFile")) if len(agents) == 1 else None
        for agent in agents:
            agent["activeFile"] = active_file

    mood = body.get("mood") if body.get("mood") in VALID_MOODS else None
    return {
        "type": "office-status",
        "agents": agents,
        "workflow": _cap(body.get("workflow")),
        "mood": mood,
        "moodDuration": _clamp_mood_duration(body.get("moodDuration")) if mood else None,
        "source": _cap(body.get("source"), 50) or "api",
    }


def _clamp_mood_duration(raw: Any) -> int:
    try:
        n = float(raw)
    except (TypeError, ValueError):
        n = 60000.0
    return int(min(max(n, 1000), 3_600_000))


class OfficeStatusStore:
    """외부 POST 상태 보관 (역할별 TTL, 파일 영속화)"""

    def __init__(self):
        self._agents: Dict[str, Dict[str, Any]] = {}   # role -> {"agent":..., "ts": float}
        self._workflow: Optional[str] = None
        self._workflow_ts: float = 0.0
        self._mood: Optional[str] = None
        self._mood_ts: float = 0.0
        self._mood_duration: int = 60000
        self._source: str = "api"
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        """재시작 후에도 직전 외부 상태를 이어받는다 (TTL 내면 유효)"""
        try:
            if not STATE_FILE.exists():
                return
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            now = time.time()
            for role, item in (data.get("agents") or {}).items():
                if role not in VALID_ROLES or not isinstance(item, dict):
                    continue
                ts = _float(item.get("ts"))
                if now - ts > EXTERNAL_TTL_SEC:
                    continue
                self._agents[role] = {"agent": item.get("agent"), "ts": ts}
            self._workflow = data.get("workflow")
            self._workflow_ts = _float(data.get("workflow_ts"))
            self._source = data.get("source") or "api"
        except Exception as e:
            logger.debug(f"[오피스] 상태 파일 로드 실패 (무시): {e}")

    def _persist(self) -> None:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "agents": self._agents,
                "workflow": self._workflow,
                "workflow_ts": self._workflow_ts,
                "source": self._source,
            }, ensure_ascii=False), encoding="utf-8")
            tmp.replace(STATE_FILE)
        except Exception as e:
            logger.debug(f"[오피스] 상태 파일 저장 실패 (무시): {e}")

    async def apply(self, payload: Dict[str, Any]) -> int:
        """정규화된 POST 페이로드 반영"""
        now = time.time()
        async with self._lock:
            for agent in payload["agents"]:
                self._agents[agent["role"]] = {"agent": agent, "ts": now}
            if payload.get("workflow"):
                self._workflow = payload["workflow"]
                self._workflow_ts = now
            if payload.get("mood"):
                self._mood = payload["mood"]
                self._mood_ts = now
                self._mood_duration = _int(payload.get("moodDuration"), 60000)
            self._source = payload.get("source") or "api"
            self._persist()
            return len(payload["agents"])

    def snapshot(self) -> Dict[str, Any]:
        """TTL이 살아있는 외부 상태만 반환 (agents: role -> {agent, ts})"""
        now = time.time()
        agents = {
            role: {"agent": item["agent"], "ts": item["ts"]}
            for role, item in self._agents.items()
            if item.get("agent") and now - item["ts"] <= EXTERNAL_TTL_SEC
        }
        workflow = self._workflow if now - self._workflow_ts <= EXTERNAL_TTL_SEC else None
        mood = None
        if self._mood and (now - self._mood_ts) * 1000 <= self._mood_duration:
            mood = self._mood
        return {
            "agents": agents,
            "workflow": workflow,
            "workflow_ts": self._workflow_ts,
            "mood": mood,
            "source": self._source if agents else None,
        }


class OfficeAPIHandler:
    """가상 오피스 상태 API 핸들러"""

    def __init__(self, data_collector=None):
        self.dc = data_collector
        self.store = OfficeStatusStore()
        self._derive_cache: Optional[Dict[str, Any]] = None
        self._derive_cache_ts: float = 0.0
        self._evolution_cache: Optional[Dict[str, Any]] = None
        self._evolution_cache_ts: float = 0.0
        self._hook_cache: Optional[Dict[str, Any]] = None
        self._hook_cache_ts: float = 0.0
        # 내용이 같으면 _seq/ETag를 유지해 304가 성립하도록 직전 페이로드 보관
        self._last_payload: Optional[Dict[str, Any]] = None
        self._last_sig: Optional[str] = None
        self._last_etag: Optional[str] = None
        self._seq_last: int = 0

    # ── 라우팅 ────────────────────────────────────────────────────────────
    def setup(self, app: web.Application) -> None:
        app.router.add_get("/api/office/status", self.get_status)
        app.router.add_post("/api/office/status", self.post_status)
        app.router.add_get("/api/office/status/stream", self.stream_status)
        app.router.add_post("/api/office/lang", self.post_lang)

    # ── 엔진 상태 → 8개 역할 ──────────────────────────────────────────────
    def _bot(self):
        return getattr(self.dc, "bot", None) if self.dc else None

    def _evolution_state(self) -> Dict[str, Any]:
        """최신 복기(advice) 파일 기준 진화 상태 (60초 캐시)"""
        now = time.time()
        if self._evolution_cache and now - self._evolution_cache_ts < EVOLUTION_CACHE_SEC:
            return self._evolution_cache

        result: Dict[str, Any] = {"today": False, "date": None}
        try:
            evo_dir = CACHE_DIR / "evolution"
            # 복기 결과(advice_*.json)가 없으면 진화 상태 파일의 갱신 시각으로 대체
            candidates = list(evo_dir.glob("advice_*.json")) if evo_dir.exists() else []
            state_file = evo_dir / "evolution_state.json"
            if state_file.exists():
                candidates.append(state_file)
            if candidates:
                latest = max(candidates, key=lambda p: p.stat().st_mtime)
                mtime = datetime.fromtimestamp(latest.stat().st_mtime)
                result["date"] = mtime.strftime("%m/%d")
                result["today"] = mtime.date() == datetime.now().date()
        except Exception:
            pass

        self._evolution_cache = result
        self._evolution_cache_ts = now
        return result

    def _derive_agents(self) -> Dict[str, Any]:
        """트레이딩 엔진 상태에서 8개 역할의 상태를 산출"""
        now = time.time()
        if self._derive_cache and now - self._derive_cache_ts < DERIVE_CACHE_SEC:
            return self._derive_cache

        result = self._derive_agents_uncached()
        self._derive_cache = result
        self._derive_cache_ts = now
        return result

    def _derive_agents_uncached(self) -> Dict[str, Any]:
        bot = self._bot()
        if bot is None:
            # KR 봇 미연결 (대시보드 단독 실행 등) — 전원 대기
            return {
                "agents": [_agent(r, "idle", "엔진 미연결") for r in VALID_ROLES],
                "workflow": "엔진 미연결",
                "mood": "idle",
            }

        agents: List[Dict[str, Any]] = []
        engine = getattr(bot, "engine", None)
        stats = getattr(engine, "stats", None) if engine else None
        running = bool(getattr(bot, "running", False))
        paused = bool(getattr(engine, "paused", False)) if engine else False

        # 세션
        session = "closed"
        try:
            session = bot._get_current_session().value
        except Exception:
            pass
        session_kr = SESSION_KR.get(session, session)
        market_open = session in ("regular", "pre_market", "next", "after_hours")

        # 리스크 스냅샷 (can_trade / 체제 / 크로스검증)
        risk: Dict[str, Any] = {}
        try:
            risk = (self.dc.get_risk() if self.dc else None) or {}
        except Exception as e:
            logger.debug(f"[오피스] 리스크 조회 실패 (무시): {e}")

        # 킬스위치
        ks: Dict[str, Any] = {}
        try:
            from src.risk import kill_switch
            ks = kill_switch.status("KR") or {}
        except Exception:
            pass

        # ── pm: 엔진/스케줄러 총괄 ──
        if not running:
            agents.append(_agent("pm", "idle", "엔진 정지"))
        elif paused:
            agents.append(_agent("pm", "blocked", "엔진 일시정지", reasonCode="blocked-unknown"))
        elif session == "pre_market":
            agents.append(_agent("pm", "planning", "장 전 준비"))
        elif session == "closed":
            agents.append(_agent("pm", "idle", "장 마감"))
        else:
            uptime = _int(getattr(stats, "uptime_seconds", None)) if stats else 0
            agents.append(_agent("pm", "working", f"{session_kr} 운영",
                                 hint=f"가동 {uptime // 3600}시간"))

        # ── arch: 시장 체제 판단 ──
        regime = str(risk.get("market_regime") or "unknown")
        regime_kr = REGIME_KR.get(regime, regime)
        regime_llm = str(risk.get("market_regime_llm") or "")
        if not running or session == "closed":
            agents.append(_agent("arch", "idle", f"체제: {regime_kr}"))
        elif session == "pre_market":
            agents.append(_agent("arch", "planning", f"체제 진단: {regime_kr}",
                                 hint=regime_llm or None))
        else:
            agents.append(_agent("arch", "working", f"체제: {regime_kr}",
                                 hint=regime_llm or None))

        # ── dev: 스크리닝·시그널 ──
        screener = getattr(bot, "screener", None)
        screened = 0
        try:
            screened = len(getattr(screener, "_last_screened", []) or [])
        except Exception:
            pass
        signals = _int(getattr(stats, "signals_generated", None)) if stats else 0
        if not running or session == "closed":
            agents.append(_agent("dev", "idle", f"오늘 신호 {signals}건"))
        elif screened > 0:
            agents.append(_agent("dev", "working", f"스크리닝 {screened}종목",
                                 hint=f"신호 {signals}건"))
        else:
            agents.append(_agent("dev", "working", "후보 탐색 중", hint=f"신호 {signals}건"))

        # ── qa: 크로스 검증 게이트 ──
        cv = risk.get("cross_validator") or {}
        cv_total = _int(cv.get("total"))
        cv_passed = _int(cv.get("passed"))
        cv_blocked = _int(cv.get("blocked"))
        if cv_total == 0:
            agents.append(_agent("qa", "idle" if not market_open else "working",
                                 "검증 대기" if not market_open else "검증 준비"))
        elif cv_passed == 0 and cv_blocked >= 3:
            agents.append(_agent("qa", "blocked", f"{cv_blocked}건 전량 거부",
                                 reasonCode="blocked-unknown",
                                 hint="검증 규칙에 걸려 통과 신호 없음"))
        else:
            agents.append(_agent("qa", "working", f"검증 {cv_total}건",
                                 hint=f"통과 {cv_passed} · 거부 {cv_blocked}"))

        # ── ops: 주문 집행 ──
        pending = 0
        try:
            rm = getattr(engine, "risk_manager", None)
            pending = len(getattr(rm, "_pending_orders", set()) or set())
        except Exception:
            pass
        submitted = _int(getattr(stats, "orders_submitted", None)) if stats else 0
        filled = _int(getattr(stats, "orders_filled", None)) if stats else 0
        if ks.get("halt_all"):
            agents.append(_agent("ops", "blocked", "전면 동결", reasonCode="permission-denied",
                                 hint=str(ks.get("reason") or "KILL_SWITCH_ALL")))
        elif pending > 0:
            agents.append(_agent("ops", "working", f"체결 대기 {pending}건",
                                 hint=f"제출 {submitted} · 체결 {filled}"))
        elif filled > 0:
            agents.append(_agent("ops", "done", f"체결 {filled}건",
                                 hint=f"제출 {submitted}건"))
        else:
            agents.append(_agent("ops", "idle", "주문 없음"))

        # ── gate: 리스크 한도 ──
        can_trade = bool(risk.get("can_trade", True))
        pos_cnt = _int(risk.get("position_count"))
        max_pos = _int(risk.get("max_positions"))
        loss_pct = _float(risk.get("daily_loss_pct"))
        loss_limit = _float(risk.get("daily_loss_limit_pct"), 5.0)
        if ks.get("active"):
            agents.append(_agent("gate", "blocked", "킬스위치 ON",
                                 reasonCode="permission-denied",
                                 hint=str(ks.get("reason") or "신규 매수 차단")))
        elif not can_trade:
            agents.append(_agent("gate", "blocked", f"거래 중단 ({loss_pct:+.1f}%)",
                                 reasonCode="blocked-unknown",
                                 hint=f"일일 손실 한도 {loss_limit:.1f}%"))
        elif loss_pct <= -loss_limit * 0.7:
            agents.append(_agent("gate", "working", f"손실 감시 {loss_pct:+.1f}%",
                                 hint=f"한도 {loss_limit:.1f}%"))
        elif market_open and running:
            agents.append(_agent("gate", "working", f"포지션 {pos_cnt}/{max_pos}",
                                 hint=f"일일손익 {loss_pct:+.2f}%"))
        else:
            agents.append(_agent("gate", "idle", f"포지션 {pos_cnt}/{max_pos}"))

        # ── res: 전문가팀 + 테마 ──
        expert_cnt = 0
        try:
            orch = getattr(bot, "expert_orchestrator", None)
            expert_cnt = len(getattr(orch, "agents", {}) or {})
        except Exception:
            pass
        theme_cnt = 0
        try:
            detector = getattr(bot, "theme_detector", None)
            theme_cnt = len(getattr(detector, "_themes", {}) or {})
        except Exception:
            pass
        if expert_cnt == 0:
            agents.append(_agent("res", "idle", "전문가 미가동"))
        elif session == "closed":
            agents.append(_agent("res", "idle", f"전문가 {expert_cnt}명 대기",
                                 hint=f"테마 {theme_cnt}건"))
        else:
            agents.append(_agent("res", "working", f"전문가 {expert_cnt}명 분석",
                                 hint=f"테마 {theme_cnt}건"))

        # ── designer: 자가 진화 ──
        evo = self._evolution_state()
        if evo["today"]:
            agents.append(_agent("designer", "done", "복기 완료", hint=f"{evo['date']} 적용"))
        elif evo["date"]:
            agents.append(_agent("designer", "idle", "복기 대기", hint=f"최근 {evo['date']}"))
        else:
            agents.append(_agent("designer", "idle", "복기 이력 없음"))

        # ── 분위기(mood) ──
        mood = "normal"
        if not running or session == "closed":
            mood = "idle"
        elif ks.get("active") or not can_trade:
            mood = "frustrated"
        elif loss_pct <= -loss_limit * 0.5:
            mood = "stuck"
        elif pending >= 3:
            mood = "intense"
        elif loss_pct >= 1.0:
            mood = "smooth"
        elif session == "pre_market":
            mood = "rushing"

        workflow = f"{session_kr} · 체제 {regime_kr}"
        return {"agents": agents, "workflow": workflow, "mood": mood}

    # ── Claude Code 훅 파일 스캔 ──────────────────────────────────────────
    def _scan_hook_files(self) -> Dict[str, Any]:
        """~/.claude/office-status*.json 스캔

        upstream의 Claude Code 훅(office-status-hook.js)은 HTTP가 아니라 파일로
        상태를 남긴다. 세션마다 `office-status-<슬러그>.json`이 생기므로 전부 읽어
        역할별로 가장 최근 파일이 이기도록 병합한다. (TTL 5분)
        """
        now = time.time()
        if self._hook_cache and now - self._hook_cache_ts < HOOK_SCAN_CACHE_SEC:
            return self._hook_cache

        agents: Dict[str, Dict[str, Any]] = {}
        workflow: Optional[str] = None
        workflow_ts = 0.0
        source: Optional[str] = None

        try:
            if HOOK_DIR.exists():
                newest_agent_ts = 0.0
                for path in HOOK_DIR.glob("office-status*.json"):
                    try:
                        stat = path.stat()
                        if now - stat.st_mtime > EXTERNAL_TTL_SEC:
                            continue
                        # 이벤트 루프 보호: 훅이 쓰는 파일은 수 KB다. 비정상적으로 큰
                        # 파일은 읽지 않는다 (POST 본문 상한의 4배까지만 허용).
                        if stat.st_size > MAX_BODY_BYTES * 4:
                            continue
                        mtime = stat.st_mtime
                        raw = json.loads(path.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    payload = normalize_post(raw)
                    for agent in payload["agents"]:
                        prev = agents.get(agent["role"])
                        if prev is None or mtime >= prev["ts"]:
                            agents[agent["role"]] = {"agent": agent, "ts": mtime}
                    if payload.get("workflow") and mtime >= workflow_ts:
                        workflow = payload["workflow"]
                        workflow_ts = mtime
                    # source는 가장 최근 파일 기준 (glob 순서에 좌우되지 않게)
                    if payload["agents"] and mtime >= newest_agent_ts:
                        newest_agent_ts = mtime
                        source = payload.get("source") or "claude-cli"
        except Exception as e:
            logger.debug(f"[오피스] 훅 파일 스캔 실패 (무시): {e}")

        result = {
            "agents": agents,
            "workflow": workflow,
            "workflow_ts": workflow_ts,
            "source": source,
        }
        self._hook_cache = result
        self._hook_cache_ts = now
        return result

    # ── 페이로드 조립 ─────────────────────────────────────────────────────
    def _next_seq(self) -> str:
        now = int(time.time() * 1000)
        self._seq_last = now if now > self._seq_last else self._seq_last + 1
        return str(self._seq_last)

    def _build_payload(self) -> Dict[str, Any]:
        """엔진 파생 상태 + 외부 상태(POST / Claude Code 훅 파일) 병합

        내용이 직전과 같으면 동일한 _seq/ETag를 유지해 304와 SSE 중복 전송을 막는다.
        """
        derived = self._derive_agents()
        posted = self.store.snapshot()
        hooked = self._scan_hook_files()

        # 역할별로 더 최근 신호가 이긴다 (POST vs 훅 파일)
        ext_agents: Dict[str, Dict[str, Any]] = dict(hooked["agents"])
        for role, item in posted["agents"].items():
            prev = ext_agents.get(role)
            if prev is None or item["ts"] >= prev["ts"]:
                ext_agents[role] = item

        merged: List[Dict[str, Any]] = []
        for agent in derived["agents"]:
            ext = ext_agents.get(agent["role"])
            merged.append(ext["agent"] if ext else agent)
        # 파생 목록에 없는 역할이 외부에서 들어온 경우 (현재는 8역할 전부 파생하므로 보통 없음)
        known = {a["role"] for a in merged}
        for role, item in ext_agents.items():
            if role not in known:
                merged.append(item["agent"])

        if posted["workflow"] and posted["workflow_ts"] >= hooked["workflow_ts"]:
            workflow = posted["workflow"]
        else:
            workflow = hooked["workflow"] or posted["workflow"] or derived["workflow"]

        active = sum(1 for a in merged if a.get("status") in ACTIVE_STATUSES)
        payload = {
            "type": "office-status",
            "agents": merged,
            "activeCount": active,
            "workflow": workflow,
            "mood": posted["mood"] or derived["mood"],
            "moodDuration": 60000,
            "source": posted["source"] or hooked["source"] or "qwq-engine",
        }

        sig = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if sig == self._last_sig and self._last_payload is not None:
            return self._last_payload

        payload["_seq"] = self._next_seq()
        self._last_sig = sig
        self._last_payload = payload
        self._last_etag = '"' + hashlib.md5(sig.encode("utf-8")).hexdigest() + '"'
        return payload

    # ── 핸들러 ────────────────────────────────────────────────────────────
    async def get_status(self, request: web.Request) -> web.Response:
        payload = self._build_payload()
        etag = self._last_etag or ""
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers={"ETag": etag})
        return web.json_response(payload, headers={"ETag": etag}, dumps=_dumps)

    def _authorized(self, request: web.Request) -> bool:
        token = os.getenv("OFFICE_STATUS_TOKEN", "").strip()
        if not token:
            return True
        auth = request.headers.get("Authorization", "")
        return hmac.compare_digest(auth, f"Bearer {token}")

    async def post_status(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)
        if request.content_length and request.content_length > MAX_BODY_BYTES:
            return web.json_response({"ok": False, "error": "Body too large"}, status=413)
        # content.read(n)은 스트림 버퍼에 있는 만큼만 돌려줄 수 있어 본문이 잘릴 수 있다.
        # (Content-Length가 없는 chunked 요청) → EOF까지 누적하되 상한에서 중단.
        try:
            chunks: List[bytes] = []
            size = 0
            async for chunk in request.content.iter_chunked(4096):
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    return web.json_response({"ok": False, "error": "Body too large"}, status=413)
                chunks.append(chunk)
            body = json.loads(b"".join(chunks).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)
        except (asyncio.CancelledError, ConnectionResetError):
            raise
        except Exception as e:
            logger.debug(f"[오피스] POST 본문 읽기 실패: {e}")
            return web.json_response({"ok": False, "error": "Read failed"}, status=400)

        payload = normalize_post(body)
        count = await self.store.apply(payload)
        return web.json_response({"ok": True, "agents": count})

    async def post_lang(self, request: web.Request) -> web.Response:
        try:
            lang = (await request.text())[:16].strip()
        except Exception:
            lang = ""
        if lang not in ("ko", "en", "zh-TW"):
            return web.json_response({"ok": False, "error": "Invalid lang"}, status=400)
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            LANG_FILE.write_text(lang, encoding="utf-8")
        except Exception as e:
            logger.debug(f"[오피스] 언어 저장 실패 (무시): {e}")
        return web.json_response({"ok": True})

    async def stream_status(self, request: web.Request) -> web.StreamResponse:
        """SSE — 상태가 바뀔 때만 전송 (프론트 폴링 부하 제거)"""
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )
        await response.prepare(request)

        last_seq = None
        try:
            while True:
                payload = self._build_payload()
                if payload.get("_seq") != last_seq:
                    last_seq = payload.get("_seq")
                    data = _dumps(payload)
                    await response.write(f"event: status\ndata: {data}\n\n".encode("utf-8"))
                else:
                    await response.write(b": keepalive\n\n")
                await asyncio.sleep(2)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        except Exception as e:
            logger.debug(f"[오피스] SSE 종료: {e}")
        return response


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def setup_office_api_routes(app: web.Application, data_collector=None) -> OfficeAPIHandler:
    """가상 오피스 API 라우트 등록"""
    handler = OfficeAPIHandler(data_collector)
    handler.setup(app)
    return handler
