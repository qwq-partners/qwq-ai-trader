"""earnings-expert — 실적·펀더멘탈 (어닝 캘린더·서프라이즈·드리프트)

데이터 소스 (신규 키 0개):
- Finnhub: 어닝 캘린더, 컨센서스, surprise (US)
- DART: 한국 잠정실적·공시
- news-curator: 가이던스 헤드라인
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any, Dict, List

import aiohttp
from loguru import logger

from .base import ExpertAgent
from .types import ExpertOpinion, RegimeBias


class EarningsExpert(ExpertAgent):
    name = "earnings_expert"
    refresh_minutes = 240   # 4시간
    cost_per_call_usd = 0.008

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._finnhub_key = os.getenv("FINNHUB_API_KEY", "")
        self._dart_key = os.getenv("DART_API_KEY", "")

    async def _analyze(self) -> ExpertOpinion:
        # 1) 임박 어닝 (US, 5영업일)
        upcoming_us = await self._fetch_us_upcoming_earnings(days_ahead=5)

        # 2) 최근 어닝 surprise (US, 7일)
        recent_us = await self._fetch_us_recent_surprises(days_back=7)

        # 3) KR 잠정실적 공시 (DART, P1-10 추가)
        kr_recent = await self._fetch_kr_recent_disclosures(days_back=3)

        # 3) 점수 계산
        score = 0
        findings: List[str] = []

        # 평균 surprise
        avg_surprise = recent_us.get("avg_surprise")
        if isinstance(avg_surprise, (int, float)):
            if avg_surprise > 0.05:
                score += 15
                findings.append(f"US 어닝 평균 surprise +{avg_surprise*100:.1f}% — 강세")
            elif avg_surprise < -0.02:
                score -= 10
                findings.append(f"US 어닝 평균 surprise {avg_surprise*100:.1f}% — 약세")

        # raise/cut 비율
        beats = recent_us.get("beats", 0)
        misses = recent_us.get("misses", 0)
        total = beats + misses
        if total > 5:
            beat_rate = beats / total
            if beat_rate > 0.7:
                score += 10
                findings.append(f"US beat 비율 {beat_rate*100:.0f}% ({beats}/{total})")
            elif beat_rate < 0.4:
                score -= 8

        # 임박 어닝 종목 수
        upcoming_n = len(upcoming_us.get("symbols", []))
        if upcoming_n > 0:
            findings.append(
                f"향후 5일 임박 어닝: {upcoming_n}건 ({', '.join(upcoming_us['symbols'][:5])})"
            )

        # P1-10 (2026-05-29 리뷰): KR 잠정실적 공시 반영
        kr_count = kr_recent.get("count", 0)
        if kr_count > 0:
            findings.append(
                f"KR 잠정실적 공시 3일: {kr_count}건 (DART)"
            )
            # 다수 발표 = 어닝시즌 진행 시그널
            if kr_count >= 10:
                score += 5

        bias = (
            RegimeBias.BULL if score > 10
            else RegimeBias.BEAR if score < -10
            else RegimeBias.NEUTRAL
        )
        confidence = min(0.8, 0.4 + (total / 20) * 0.4) if total > 0 else 0.3

        affected_symbols = upcoming_us.get("symbols", [])[:10]

        return self._build_opinion(
            score=score,
            bias=bias,
            confidence=confidence,
            findings=findings,
            symbols=affected_symbols,
            raw=dict(
                upcoming=upcoming_us,
                recent=recent_us,
            ),
            valid_hours=4,
        )

    # ─────────────────────────────────────────
    # 임박 어닝 (US)
    # ─────────────────────────────────────────
    async def _fetch_us_upcoming_earnings(self, days_ahead: int = 5) -> Dict[str, Any]:
        if not self._finnhub_key:
            return {"symbols": []}
        try:
            today = datetime.now().date()
            end = today + timedelta(days=days_ahead)
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    "https://finnhub.io/api/v1/calendar/earnings",
                    params={
                        "from": today.isoformat(),
                        "to": end.isoformat(),
                        "token": self._finnhub_key,
                    },
                ) as resp:
                    if resp.status != 200:
                        return {"symbols": []}
                    data = await resp.json()
            calendar = data.get("earningsCalendar", []) or []
            # 시총 큰 종목 위주 (Finnhub 안 줌 → 임의 정렬)
            symbols = list({it.get("symbol", "") for it in calendar if it.get("symbol")})[:20]
            return {"symbols": symbols, "count": len(calendar)}
        except Exception as e:
            logger.debug(f"[실적] upcoming 실패: {e}")
            return {"symbols": []}

    # ─────────────────────────────────────────
    # KR 잠정실적 공시 (DART, P1-10 추가)
    # ─────────────────────────────────────────
    async def _fetch_kr_recent_disclosures(self, days_back: int = 3) -> Dict[str, Any]:
        """DART 공시검색 — 잠정실적 키워드"""
        if not self._dart_key:
            return {"count": 0}
        try:
            end = datetime.now().date()
            start = end - timedelta(days=days_back)
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                # DART 공시검색 API (전자공시 list.json)
                async with session.get(
                    "https://opendart.fss.or.kr/api/list.json",
                    params={
                        "crtfc_key": self._dart_key,
                        "bgn_de": start.strftime("%Y%m%d"),
                        "end_de": end.strftime("%Y%m%d"),
                        "pblntf_ty": "A",   # 정기공시
                        "page_count": 100,
                    },
                ) as resp:
                    if resp.status != 200:
                        return {"count": 0}
                    data = await resp.json()
            if data.get("status") != "000":
                return {"count": 0}
            items = data.get("list", []) or []
            # 잠정실적/연결재무 키워드 필터
            relevant = [
                it for it in items
                if any(k in (it.get("report_nm", "") or "") for k in ("잠정실적", "연결재무", "분기보고", "반기보고"))
            ]
            return {
                "count": len(relevant),
                "total": len(items),
                "sample": [
                    {"corp": it.get("corp_name"), "report": it.get("report_nm")}
                    for it in relevant[:5]
                ],
            }
        except Exception as e:
            logger.debug(f"[실적] DART 실패: {e}")
            return {"count": 0}

    # ─────────────────────────────────────────
    # 최근 surprise (US)
    # ─────────────────────────────────────────
    async def _fetch_us_recent_surprises(self, days_back: int = 7) -> Dict[str, Any]:
        if not self._finnhub_key:
            return {}
        try:
            today = datetime.now().date()
            start = today - timedelta(days=days_back)
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    "https://finnhub.io/api/v1/calendar/earnings",
                    params={
                        "from": start.isoformat(),
                        "to": today.isoformat(),
                        "token": self._finnhub_key,
                    },
                ) as resp:
                    if resp.status != 200:
                        return {}
                    data = await resp.json()
            calendar = data.get("earningsCalendar", []) or []
            surprises = []
            beats = misses = 0
            for it in calendar:
                actual = it.get("epsActual")
                est = it.get("epsEstimate")
                if actual is None or est is None:
                    continue
                try:
                    est_f = float(est)
                    if abs(est_f) < 0.1:
                        continue
                    surprise = (float(actual) - est_f) / abs(est_f)
                    surprise = max(-0.5, min(0.5, surprise))
                    surprises.append(surprise)
                    if surprise > 0.01:
                        beats += 1
                    elif surprise < -0.01:
                        misses += 1
                except (ValueError, TypeError):
                    continue
            if not surprises:
                return {}
            return {
                "count": len(surprises),
                "avg_surprise": round(sum(surprises) / len(surprises), 4),
                "beats": beats,
                "misses": misses,
            }
        except Exception as e:
            logger.debug(f"[실적] recent 실패: {e}")
            return {}
