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


async def fetch_disclosure_summary(top_n: int = 5, days: int = 2) -> str:
    """최근 N일 공시 중요도 상위 요약 ('' = 실패/데이터 없음 — fail-open)"""
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(_URL) as resp:
                if resp.status != 200:
                    return ""
                data = await resp.json(content_type=None)

        events: List[Dict[str, Any]] = data.get("events") or []
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
