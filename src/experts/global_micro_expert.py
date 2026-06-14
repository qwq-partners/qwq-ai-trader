"""global-micro-expert — 글로벌 섹터/공급망 (반도체·2차전지·바이오·조선·원자재)

데이터 소스 (신규 키 0개):
- Perplexity: 글로벌 공급망, HBM 수주, 컨퍼런스콜 요지
- yfinance: 원자재(WTI, 구리, 리튬), 글로벌 ETF (SMH, LIT)
"""

from __future__ import annotations

import asyncio
import json
from typing import Dict, List

from loguru import logger

from .base import ExpertAgent
from .types import ExpertOpinion, RegimeBias


# 섹터별 ETF/원자재 매핑
_SECTOR_TICKERS = {
    "semiconductors": "SMH",        # 반도체
    "battery": "LIT",               # 리튬·배터리
    "biotech": "XBI",               # 바이오
    "shipbuilding": None,           # 직접 ETF 없음 — Perplexity 의존
    "wti": "CL=F",
    "copper": "HG=F",
    "gold": "GC=F",
}


class GlobalMicroExpert(ExpertAgent):
    name = "global_micro_expert"
    refresh_minutes = 360   # 6시간
    cost_per_call_usd = 0.01

    async def _analyze(self) -> ExpertOpinion:
        # 1) 글로벌 산업 공급망 (Perplexity 배치)
        industry_text = await self._perplexity_search(
            "Provide a 1-line update each for the following global industries today: "
            "(1) Semiconductor HBM/AI chip demand and supply, "
            "(2) Battery/EV demand and lithium price trend, "
            "(3) Shipbuilding new orders (LNG carriers, defense), "
            "(4) Biotech FDA approvals or clinical results. "
            "Output as numbered list, very concise.",
            max_tokens=500,
        )

        # 2) 원자재·섹터 ETF
        sector_returns = await self._fetch_sector_etf_returns()

        # 3) 점수 계산
        score = 0
        findings: List[str] = []
        sectors: List[str] = []

        # 반도체 ETF
        smh = sector_returns.get("semiconductors")
        if isinstance(smh, (int, float)):
            if smh > 3.0:
                score += 12
                findings.append(f"반도체 ETF(SMH) +{smh:.1f}% (5일)")
                sectors.append("반도체")
            elif smh < -3.0:
                score -= 10
                findings.append(f"반도체 ETF(SMH) {smh:.1f}%")

        # 배터리
        lit = sector_returns.get("battery")
        if isinstance(lit, (int, float)):
            if lit > 4.0:
                score += 8
                findings.append(f"리튬·배터리 ETF +{lit:.1f}%")
                sectors.append("2차전지")
            elif lit < -5.0:
                score -= 5

        # 바이오
        xbi = sector_returns.get("biotech")
        if isinstance(xbi, (int, float)):
            if xbi > 3.0:
                score += 5
                sectors.append("바이오")

        # 원자재
        copper = sector_returns.get("copper")
        if isinstance(copper, (int, float)):
            if copper > 4.5:
                score += 5
                findings.append(f"구리 ${copper:.2f} (경기 확장)")
                sectors.append("산업금속")
        wti = sector_returns.get("wti")
        if isinstance(wti, (int, float)):
            if wti > 90:
                score -= 5
                findings.append(f"WTI ${wti:.1f} (인플레 압박)")

        # 산업 텍스트 단서
        if industry_text:
            t = industry_text.lower()
            if "hbm" in t and ("강세" in industry_text or "수주" in industry_text or "demand" in t):
                score += 8
                findings.append("HBM 수요 견조 — SK하이닉스/삼성 수혜")
                if "반도체" not in sectors:
                    sectors.append("반도체")
            if "lng" in t or "shipbuild" in t or "조선" in industry_text:
                if "수주" in industry_text or "order" in t:
                    score += 5
                    findings.append("조선 LNG/방산 수주 모멘텀")
                    sectors.append("조선")
            findings.append(f"산업: {industry_text[:150]}")

        # LLM 종합 한 줄
        llm_finding = await self._llm_synthesize(industry_text, sector_returns)
        if llm_finding:
            findings.insert(0, llm_finding)

        bias = (
            RegimeBias.BULL if score > 10
            else RegimeBias.BEAR if score < -10
            else RegimeBias.NEUTRAL
        )
        confidence = min(0.75, 0.35 + len(findings) * 0.07)

        return self._build_opinion(
            score=score,
            bias=bias,
            confidence=confidence,
            findings=findings,
            sectors=sectors,
            raw=dict(
                sector_returns=sector_returns,
                has_industry_text=bool(industry_text),
            ),
            valid_hours=6,
        )

    async def _fetch_sector_etf_returns(self) -> Dict[str, float]:
        """섹터 ETF/원자재 5일 수익률"""
        def _one(ticker: str):
            try:
                import yfinance as yf
                hist = yf.Ticker(ticker).history(period="10d", interval="1d", auto_adjust=False)
                if hist.empty or len(hist) < 3:
                    return ticker, None
                close = hist["Close"]
                last = float(close.iloc[-1])
                # 5일 변화 또는 가능한 최대
                idx = max(0, len(close) - 6)
                base = float(close.iloc[idx])
                if ticker in ("CL=F", "HG=F", "GC=F"):
                    return ticker, last  # 원자재는 현재가
                ret = (last - base) / base * 100 if base > 0 else 0.0
                return ticker, round(ret, 2)
            except Exception:
                return ticker, None

        tasks = []
        keys = []
        for k, t in _SECTOR_TICKERS.items():
            if t is None:
                continue
            keys.append(k)
            tasks.append(asyncio.to_thread(_one, t))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: Dict[str, float] = {}
        for k, r in zip(keys, results):
            if isinstance(r, tuple) and r[1] is not None:
                out[k] = r[1]
        return out

    async def _llm_synthesize(self, industry_text: str, sector_returns: Dict) -> str:
        if self.llm_manager is None or not industry_text:
            return ""
        try:
            from src.utils.llm import LLMTask
            prompt = (
                "글로벌 섹터 동향이 KR 종목에 미칠 영향을 한국어 한 줄로 요약.\n"
                f"산업: {industry_text[:300]}\n"
                f"섹터수익률: {json.dumps(sector_returns, ensure_ascii=False)}"
            )
            resp = await self.llm_manager.complete(
                prompt, task=LLMTask.QUICK_ANALYSIS,
                system="글로벌 산업 분석가. 한 줄로.",
            )
            if resp.success and resp.content:
                return resp.content.strip()[:150]
        except Exception as e:
            logger.debug(f"[글로벌] LLM 종합 실패: {e}")
        return ""
