"""전일 공시 요약 피드 (2026-08-11 — AIK Stock Data 공개 JSON, 사용자 승인)

아침 브리핑에 "최근 공시 중요도 상위 N건"을 공급한다. 데이터는 DART 공공데이터를
재가공한 개인 운영 무료 서비스(aikstockdata.com)라 지속성 보장이 없다 —
**fail-open 필수**: 실패·형식 변화 시 조회 미확인 문구 반환, 매매·브리핑을 절대 막지 않음.

필드 (2026-08-11 실측 schema_version 1.1):
  events[].rcept_dt "YYYYMMDD" / name / code / label(유형) / score(중요도, 기계 산정)
  / session(pre_open|intraday|after_close) / meaning(유형 일반 설명)

출처 표기 조건부 자유 이용 라이선스 — 브리핑에 출처 명시.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from html import escape
import math
import re
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
    """공시 이벤트 조회 + 종목별 캐시 갱신 (실패는 호출자가 구분하여 처리)"""
    global _cache_at
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(_URL) as resp:
            if resp.status != 200:
                raise ValueError(f"공시 피드 HTTP {resp.status}")
            data = await resp.json(content_type=None)
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ValueError("공시 피드 형식 불일치")
    events: List[Dict[str, Any]] = [e for e in data["events"] if isinstance(e, dict)]
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


def _safe_text(value, limit=120):
    return escape(" ".join(str(value).split())[:limit], quote=False)


async def fetch_disclosure_summary(top_n: int = 5, days: int = 2) -> str:
    """공시 유형·접수일·일반 설명을 보여 준다. 중요도는 가격 방향 점수가 아니다."""
    try:
        events = await _fetch_events()
        now = datetime.now()
        cutoff = (now - timedelta(days=days)).date()
        recent = []
        for event in events:
            if not isinstance(event, dict):
                continue
            score = event.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                continue
            try:
                receipt = datetime.strptime(str(event.get("rcept_dt", "")), "%Y%m%d").date()
            except (ValueError, TypeError):
                continue
            if cutoff <= receipt <= now.date():
                recent.append((event, receipt))
        recent.sort(key=lambda pair: -float(pair[0]["score"]))
        lines = [f"조회 {now:%Y-%m-%d %H:%M} KST · 접수일 {cutoff}~{now.date()}"]
        if not recent:
            lines.append("피드 내 대상 공시 없음 · 전체 DART 공시 부재를 뜻하지 않음")
        else:
            lines.append(f"피드 내 유효 {len(recent)}건 중 중요도 상위 {min(top_n, len(recent))}건 · 호재·악재 점수 아님")
        for event, receipt in recent[:top_n]:
            lines.append(f"• {_safe_text(event.get('name', '?'), 40)}({_safe_text(event.get('code', '?'), 12)}) "
                         f"[{_safe_text(event.get('label', '?'), 35)}] · 접수 {receipt}")
            lines.append(f"  유형 설명: {_safe_text(event.get('meaning') or '일반 설명 미제공')} "
                         "(개별 공시 원문 해석 아님)")
            receipt_no = str(event.get("rcept_no", ""))
            if re.fullmatch(r"[0-9]{14}", receipt_no):
                lines.append(f"  원문 https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt_no}")
        lines.append("확인할 것: 보유·관심 종목의 원문에서 금액·효력일·정정 여부 확인 후 영향 판단")
        lines.append("<i>출처: DART 공공데이터 (aikstockdata 가공) · 접수 시각 미제공</i>")
        return "\n".join(lines)
    except Exception as e:
        logger.debug(f"[공시피드] 조회 실패 (무시): {e}")
        return "공시 피드 조회 실패 — 공시가 없다는 의미가 아니며 원문 확인 필요"
