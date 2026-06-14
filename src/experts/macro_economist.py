"""macro-economist — 글로벌 거시 지표 추적

데이터 소스 (신규 키 0개):
- yfinance: ^TNX(US10Y), KRW=X, DX-Y.NYB(DXY), CL=F(WTI), GC=F(금), HG=F(구리)
- Perplexity: 최신 CPI/PCE/NFP, FOMC 결정, dot plot
- 수동 오버라이드: ~/.cache/ai_trader/manual_macro_overrides.json
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from .base import ExpertAgent
from .types import ExpertOpinion, RegimeBias


_MANUAL_OVERRIDE_PATH = Path.home() / ".cache" / "ai_trader" / "manual_macro_overrides.json"

# 추적할 지표 (yfinance 티커)
_TICKERS = {
    "us10y": "^TNX",            # 10년 국채금리 (%)
    "us_short_yield": "^IRX",   # 13주 T-bill (단기금리, P2-3 명명 정정)
    "dxy": "DX-Y.NYB",          # 달러지수
    "krw_usd": "KRW=X",         # 원/달러
    "wti": "CL=F",              # 원유
    "gold": "GC=F",             # 금
    "copper": "HG=F",           # 구리
    "vix": "^VIX",              # VIX
}

# 임계값 (bear 신호 트리거)
_THRESHOLDS = {
    "us10y_high": 4.5,      # 위험자산 압박
    "dxy_high": 105.0,      # 신흥국 통화 압박
    "krw_high": 1400.0,     # 한국 외환 위험
    "vix_high": 25.0,       # 변동성 체제
    "wti_low": 60.0,        # 경기 둔화 신호
}


class MacroEconomist(ExpertAgent):
    name = "macro_economist"
    refresh_minutes = 240
    cost_per_call_usd = 0.01

    async def _analyze(self) -> ExpertOpinion:
        # 1) yfinance 지표 병렬 fetch
        prices, semis_avg = await asyncio.gather(
            self._fetch_yfinance_indicators(),
            self._fetch_semis_basket_5d(),
            return_exceptions=True,
        )
        if not isinstance(prices, dict):
            prices = {}
        if not isinstance(semis_avg, dict):
            semis_avg = {}

        # 2) 수동 오버라이드 머지
        overrides = self._load_manual_overrides()
        prices.update(overrides)

        # 3) Perplexity 매크로 컨텍스트
        macro_context = await self._fetch_macro_context()

        # 4) 점수 계산
        score, findings = self._score_indicators(prices, macro_context)

        # 4-2) 반도체 바스켓 5일 평균 (2026-06-07 추가, 약한 가중)
        # us_market_expert와 중복 방지 위해 약한 가중치(-8/+5)
        basket_avg = semis_avg.get("avg_5d_pct")
        if isinstance(basket_avg, (int, float)):
            if basket_avg <= -3.0:
                score -= 8
                findings.append(f"반도체 바스켓 5일 평균 {basket_avg:+.1f}% — 그룹 단기 약세")
            elif basket_avg >= 3.0:
                score += 5
                findings.append(f"반도체 바스켓 5일 평균 {basket_avg:+.1f}% — 그룹 강세")
            prices["semis_basket_5d_pct"] = round(float(basket_avg), 2)
        bias = (
            RegimeBias.BULL if score > 15
            else RegimeBias.BEAR if score < -15
            else RegimeBias.NEUTRAL
        )

        # 5) LLM 종합 (있을 때만)
        llm_finding = await self._llm_synthesize(prices, macro_context)
        if llm_finding:
            findings.insert(0, llm_finding)

        # confidence는 보유 지표 수에 비례
        confidence = min(0.85, 0.3 + len(prices) * 0.07)

        affected = self._affected_sectors(prices)

        return self._build_opinion(
            score=score,
            bias=bias,
            confidence=confidence,
            findings=findings,
            sectors=affected,
            raw=dict(
                prices=prices,
                has_macro_context=bool(macro_context),
                manual_override_keys=list(overrides.keys()),
            ),
            valid_hours=4,
        )

    # ─────────────────────────────────────────
    # yfinance — to_thread로 비동기화
    # ─────────────────────────────────────────
    # P1-3 (2026-05-29 리뷰): 지표별 정상 범위 (yfinance 비정상값 차단)
    _VALID_RANGES: Dict[str, tuple] = {
        "us10y": (0.5, 8.0),
        "us_short_yield": (0.0, 8.0),
        "dxy": (80.0, 120.0),
        "krw_usd": (1100.0, 1500.0),  # 1503 spike 사고 방지
        "wti": (20.0, 200.0),
        "gold": (1000.0, 4500.0),     # 2026 시점 금값 상한 보정
        "copper": (2.0, 8.0),
        "vix": (8.0, 80.0),
    }

    # ─────────────────────────────────────────
    # 반도체 바스켓 5일 평균 (2026-06-07 추가)
    # SOX + SMH + NVDA + AMD + TSM 5일 변동률 평균
    # ─────────────────────────────────────────
    async def _fetch_semis_basket_5d(self) -> Dict[str, Any]:
        def _one(ticker: str) -> Optional[float]:
            try:
                import yfinance as yf
                hist = yf.Ticker(ticker).history(period="10d", interval="1d", auto_adjust=False)
                if hist.empty or len(hist) < 6:
                    return None
                close = hist["Close"]
                last = float(close.iloc[-1])
                base = float(close.iloc[-6])
                if base <= 0:
                    return None
                return round((last - base) / base * 100, 2)
            except Exception:
                return None

        tickers = ["^SOX", "SMH", "NVDA", "AMD", "TSM"]
        results = await asyncio.gather(
            *[asyncio.to_thread(_one, t) for t in tickers],
            return_exceptions=True,
        )
        vals = [r for r in results if isinstance(r, (int, float))]
        if not vals:
            return {}
        return {
            "avg_5d_pct": round(sum(vals) / len(vals), 2),
            "n_samples": len(vals),
            "tickers": tickers,
        }

    async def _fetch_yfinance_indicators(self) -> Dict[str, float]:
        try:
            import yfinance as yf
        except ImportError:
            logger.warning("[거시] yfinance 미설치")
            return {}

        def _fetch_one(ticker: str) -> Optional[float]:
            try:
                hist = yf.Ticker(ticker).history(period="2d", interval="1d", auto_adjust=False)
                if hist.empty:
                    return None
                return float(hist["Close"].iloc[-1])
            except Exception:
                return None

        tasks = [
            asyncio.to_thread(_fetch_one, tkr) for tkr in _TICKERS.values()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        out: Dict[str, float] = {}
        for (name, _), result in zip(_TICKERS.items(), results):
            if isinstance(result, (int, float)) and result is not None:
                val = float(result)
                # 정상 범위 검증
                lo, hi = self._VALID_RANGES.get(name, (-1e9, 1e9))
                if not (lo <= val <= hi):
                    logger.warning(
                        f"[거시] {name}={val} 정상범위[{lo}, {hi}] 이탈 — 폐기"
                    )
                    continue
                out[name] = round(val, 3)
        return out

    # ─────────────────────────────────────────
    # 수동 오버라이드 (사용자가 FOMC/CPI 발표일 직접 입력)
    # ─────────────────────────────────────────
    def _load_manual_overrides(self) -> Dict[str, Any]:
        if not _MANUAL_OVERRIDE_PATH.exists():
            return {}
        try:
            with _MANUAL_OVERRIDE_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            # {"cpi_yoy": 3.2, "fed_decision": "hold", ...} 형식
            if isinstance(data, dict):
                return data
        except Exception as e:
            logger.warning(f"[거시] manual_overrides 로드 실패: {e}")
        return {}

    # ─────────────────────────────────────────
    # Perplexity 매크로 컨텍스트
    # ─────────────────────────────────────────
    async def _fetch_macro_context(self) -> str:
        return await self._perplexity_search(
            "Provide a brief one-line summary for each of the following, with the most recent figures available: "
            "(1) latest US CPI YoY %, (2) latest US Nonfarm Payrolls change, "
            "(3) Fed funds rate decision and dot plot 2026 median, "
            "(4) any major central bank action this week. "
            "Output as numbered list, no extra commentary.",
            max_tokens=400,
        )

    # ─────────────────────────────────────────
    # 점수 계산 (규칙 기반)
    # ─────────────────────────────────────────
    def _score_indicators(
        self,
        prices: Dict[str, Any],
        macro_context: str,
    ) -> tuple[int, List[str]]:
        score = 0
        findings: List[str] = []

        us10y = prices.get("us10y")
        if isinstance(us10y, (int, float)):
            if us10y > _THRESHOLDS["us10y_high"]:
                score -= 15
                findings.append(f"US10Y {us10y:.2f}% — 위험자산 압박")
            elif us10y < 3.5:
                score += 10
                findings.append(f"US10Y {us10y:.2f}% — 완화적")

        dxy = prices.get("dxy")
        if isinstance(dxy, (int, float)):
            if dxy > _THRESHOLDS["dxy_high"]:
                score -= 10
                findings.append(f"DXY {dxy:.1f} — 달러 강세")
            elif dxy < 100:
                score += 5

        krw = prices.get("krw_usd")
        if isinstance(krw, (int, float)):
            if krw > _THRESHOLDS["krw_high"]:
                score -= 12
                findings.append(f"KRW {krw:.0f} — 외환 위험 구간")
            elif krw < 1300:
                score += 5

        vix = prices.get("vix")
        if isinstance(vix, (int, float)):
            if vix > _THRESHOLDS["vix_high"]:
                score -= 15
                findings.append(f"VIX {vix:.1f} — 변동성 체제")
            elif vix < 15:
                score += 8
                findings.append(f"VIX {vix:.1f} — 안정")

        wti = prices.get("wti")
        if isinstance(wti, (int, float)):
            if wti < _THRESHOLDS["wti_low"]:
                score -= 5
                findings.append(f"WTI ${wti:.1f} — 경기 둔화 시그널")

        # CPI 수동 override 반영
        cpi = prices.get("cpi_yoy")
        if isinstance(cpi, (int, float)):
            if cpi > 3.5:
                score -= 8
                findings.append(f"CPI {cpi:.1f}% — 인플레 우려")
            elif cpi < 2.5:
                score += 8

        if macro_context:
            findings.append(f"매크로: {macro_context[:120]}")

        return score, findings

    # ─────────────────────────────────────────
    # 영향 섹터 매핑
    # ─────────────────────────────────────────
    def _affected_sectors(self, prices: Dict[str, Any]) -> List[str]:
        sectors: List[str] = []
        if isinstance(prices.get("us10y"), (int, float)) and prices["us10y"] > 4.5:
            sectors.extend(["성장주", "리츠", "유틸리티"])
        if isinstance(prices.get("krw_usd"), (int, float)) and prices["krw_usd"] > 1400:
            sectors.append("수입의존주")
        if isinstance(prices.get("wti"), (int, float)) and prices["wti"] > 90:
            sectors.append("에너지")
        if isinstance(prices.get("copper"), (int, float)) and prices["copper"] > 4.5:
            sectors.append("산업금속")
        return sectors

    # ─────────────────────────────────────────
    # LLM 한 줄 종합
    # ─────────────────────────────────────────
    async def _llm_synthesize(
        self,
        prices: Dict[str, Any],
        macro_context: str,
    ) -> str:
        if self.llm_manager is None:
            return ""
        try:
            from src.utils.llm import LLMTask
            prompt = (
                "다음 거시지표와 컨텍스트를 보고 KR/US 주식에 미치는 영향을 한국어 한 줄로 요약하세요.\n"
                f"지표: {json.dumps(prices, ensure_ascii=False)}\n"
                f"매크로 컨텍스트: {macro_context[:300]}"
            )
            resp = await self.llm_manager.complete(
                prompt,
                task=LLMTask.MARKET_ANALYSIS,  # P2-4: 거시 종합은 heavy 모델
                system="당신은 거시 경제 분석가입니다. 한 줄로 핵심만.",
            )
            if resp.success and resp.content:
                return resp.content.strip()[:150]
        except Exception as e:
            logger.debug(f"[거시] LLM 종합 실패: {e}")
        return ""
