"""kr-economy-expert — 한국 거시 경제 (한은·수출입·반도체 사이클·환율)

데이터 소스 (신규 키 0개):
- Perplexity: 한은 기준금리, 관세청 수출입, 한국 경제지표
- yfinance: KRW=X, KOSPI, 반도체 ETF
- DART API (이미 보유): 주요 수출 기업 공시
"""

from __future__ import annotations

import asyncio
import json
from typing import Dict, List

from loguru import logger

from .base import ExpertAgent
from .types import ExpertOpinion, RegimeBias


class KREconomyExpert(ExpertAgent):
    name = "kr_economy_expert"
    refresh_minutes = 480   # 8시간
    cost_per_call_usd = 0.01

    async def _analyze(self) -> ExpertOpinion:
        # 1) Perplexity 한국 거시 통합 질의 (배치 1회)
        macro_text = await self._perplexity_search(
            "다음 한국 거시 지표의 최신 수치를 각각 한 줄로 알려주세요: "
            "(1) 한국은행 기준금리, (2) 최근 월간 수출증감률 YoY %, "
            "(3) 반도체 수출 비중, (4) 가계부채/GDP 비율, "
            "(5) 다음 금통위 예정일과 컨센서스. "
            "번호로 구분하여 답하세요.",
            max_tokens=500,
        )

        # 2) yfinance KRW + KOSPI 보조
        krw_state = await self._fetch_krw_state()

        # 3) 점수 계산 (룰 + LLM 종합)
        score = 0
        findings: List[str] = []

        krw = krw_state.get("krw_usd")
        if isinstance(krw, (int, float)):
            if krw > 1400:
                score -= 12
                findings.append(f"KRW {krw:.0f}원 — 외환 위험")
            elif krw > 1380:
                score -= 5
            elif krw < 1300:
                score += 5

        # macro_text에 단서 추출
        if macro_text:
            findings.append(f"한국 거시: {macro_text[:200]}")
            # 수출 증가 단서
            t = macro_text.lower()
            if "수출" in macro_text and ("증가" in macro_text or "yoy +" in t or "회복" in macro_text):
                score += 8
                findings.append("수출 모멘텀 회복 시그널")
            if "금리 인하" in macro_text or "rate cut" in t:
                score += 6
                findings.append("금리 인하 기대")
            if "부동산" in macro_text and ("위험" in macro_text or "부실" in macro_text):
                score -= 10
                findings.append("부동산 PF 리스크 부각")

        # 4) LLM 종합 한 줄
        llm_summary = await self._llm_synthesize(macro_text, krw_state)
        if llm_summary:
            findings.insert(0, llm_summary)

        bias = (
            RegimeBias.BULL if score > 10
            else RegimeBias.BEAR if score < -10
            else RegimeBias.NEUTRAL
        )
        confidence = min(0.7, 0.3 + len(findings) * 0.08)

        return self._build_opinion(
            score=score,
            bias=bias,
            confidence=confidence,
            findings=findings,
            sectors=["수출주", "반도체", "자동차"] if score > 0 else ["내수주", "방어주"],
            raw=dict(
                has_macro_text=bool(macro_text),
                krw=krw_state,
            ),
            valid_hours=8,
        )

    async def _fetch_krw_state(self) -> Dict[str, float]:
        def _sync():
            try:
                import yfinance as yf
                hist = yf.Ticker("KRW=X").history(period="30d", interval="1d", auto_adjust=False)
                if hist.empty:
                    return {}
                last = float(hist["Close"].iloc[-1])
                ma20 = float(hist["Close"].tail(20).mean())
                return {
                    "krw_usd": round(last, 1),
                    "vs_ma20_pct": round((last - ma20) / ma20 * 100, 2) if ma20 > 0 else 0.0,
                }
            except Exception as e:
                logger.debug(f"[한국거시] KRW 실패: {e}")
                return {}
        return await asyncio.to_thread(_sync)

    async def _llm_synthesize(self, macro_text: str, krw_state: Dict) -> str:
        if self.llm_manager is None or not macro_text:
            return ""
        try:
            from src.utils.llm import LLMTask
            prompt = (
                "한국 거시 지표를 보고 KR 주식시장에 미치는 영향을 한국어 한 줄로 요약하세요.\n"
                "환율을 언급할 때는 비교 기준을 반드시 명시하세요 (예: '20일 평균 대비 -2.9%'). "
                "기준 없는 '원화 강세/약세' 표현 금지 — 거시 전문가는 절대수준 기준이라 모순돼 보일 수 있음.\n"
                f"매크로: {macro_text[:400]}\n"
                f"환율 상태: {json.dumps(krw_state, ensure_ascii=False)}"
            )
            resp = await self.llm_manager.complete(
                prompt, task=LLMTask.MARKET_ANALYSIS,  # P2-4: 거시 종합은 heavy
                system="당신은 한국 경제 분석가입니다. 한 줄로.",
            )
            if resp.success and resp.content:
                return resp.content.strip()[:150]
        except Exception as e:
            logger.debug(f"[한국거시] LLM 종합 실패: {e}")
        return ""
