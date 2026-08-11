"""전일 공시 요약 피드 (2026-08-11 — AIK Stock Data 공개 JSON, 사용자 승인)

아침 브리핑에 "최근 공시 중요도 상위 N건"을 공급한다. 데이터는 DART 공공데이터를
재가공한 개인 운영 무료 서비스(aikstockdata.com)라 지속성 보장이 없다 —
**fail-open 필수**: 실패·형식 변화 시 빈 문자열 반환, 매매·브리핑을 절대 막지 않음.

필드 (2026-08-11 실측 schema_version 1.1):
  events[].rcept_dt "YYYYMMDD" / name / code / label(유형) / score(중요도, 기계 산정)
  / session(pre_open|intraday|after_close) / meaning(유형 일반 설명)

출처 표기 조건부 자유 이용 라이선스 — 브리핑에 출처 명시.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List

import aiohttp
from loguru import logger

_URL = "https://aikstockdata.com/data/public/disclosures.json"
_SESSION_KO = {"pre_open": "장전", "intraday": "장중", "after_close": "장후"}

# 종목별 캐시 (2026-08-11 — 크로스검증/스크리너 LLM 보조 컨텍스트용)
# validate 계열은 동기라 I/O 불가 → async 갱신 + 동기 캐시 조회로 분리.
# 캐시 미적재 시 빈 컨텍스트 = fail-open (매매 판단은 다른 소스로 계속)
_symbol_cache: Dict[str, List[Dict[str, Any]]] = {}
_cache_at: datetime | None = None
_CACHE_TTL_H = 6


async def _fetch_events() -> List[Dict[str, Any]]:
    """공시 이벤트 조회 + 종목별 캐시 갱신 (실패 시 빈 리스트)"""
    global _cache_at
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(_URL) as resp:
            if resp.status != 200:
                return []
            data = await resp.json(content_type=None)
    events: List[Dict[str, Any]] = data.get("events") or []
    if events:
        _symbol_cache.clear()
        for e in events:
            code = str(e.get("code", "")).strip()
            if code:
                _symbol_cache.setdefault(code, []).append(e)
        _cache_at = datetime.now()
    return events


async def refresh_disclosure_cache() -> int:
    """종목 캐시 갱신 (배치 스캔 시 호출). 반환: 이벤트 수 (실패 0 — fail-open)"""
    try:
        if (_cache_at is not None
                and (datetime.now() - _cache_at).total_seconds() < _CACHE_TTL_H * 3600):
            return sum(len(v) for v in _symbol_cache.values())
        return len(await _fetch_events())
    except Exception as e:
        logger.debug(f"[공시피드] 캐시 갱신 실패 (무시): {e}")
        return 0


def get_symbol_disclosure_context(code: str, days: int = 3) -> str:
    """종목의 최근 공시 컨텍스트 (동기, 캐시 전용 — '' = 없음/캐시 미적재)

    보조 소스 원칙: LLM 참고용 한 줄 요약만. 점수·차단에 직접 사용 금지.
    """
    try:
        events = _symbol_cache.get(str(code).strip())
        if not events:
            return ""
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        recent = sorted(
            (e for e in events if str(e.get("rcept_dt", "")) >= cutoff),
            key=lambda e: -float(e.get("score", 0) or 0),
        )[:2]
        if not recent:
            return ""
        parts = []
        for e in recent:
            sess = _SESSION_KO.get(str(e.get("session", "")), "")
            dt = str(e.get("rcept_dt", ""))
            parts.append(
                f"[{e.get('label', '?')}] 중요도 {float(e.get('score', 0) or 0):.0f}"
                + (f"·{sess}" if sess else "")
                + (f"·{dt[4:6]}/{dt[6:8]}" if len(dt) == 8 else "")
            )
        return " / ".join(parts)
    except Exception:
        return ""


async def fetch_disclosure_summary(top_n: int = 5, days: int = 2) -> str:
    """최근 N일 공시 중요도 상위 요약 ('' = 실패/데이터 없음 — fail-open)"""
    try:
        events = await _fetch_events()
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        recent = [
            e for e in events
            if str(e.get("rcept_dt", "")) >= cutoff
            and isinstance(e.get("score"), (int, float))
        ]
        if not recent:
            return ""
        recent.sort(key=lambda e: -float(e.get("score", 0)))

        lines = []
        for e in recent[:top_n]:
            sess = _SESSION_KO.get(str(e.get("session", "")), "")
            lines.append(
                f"· {e.get('name', '?')}({e.get('code', '?')}) "
                f"[{e.get('label', '?')}] 중요도 {e.get('score', 0):.0f}"
                + (f" · {sess}" if sess else "")
            )
        lines.append("<i>출처: DART 공공데이터 (aikstockdata 가공)</i>")
        return "\n".join(lines)
    except Exception as e:
        logger.debug(f"[공시피드] 조회 실패 (무시): {e}")
        return ""
