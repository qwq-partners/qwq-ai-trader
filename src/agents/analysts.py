"""
Analyst 팀 — 종목 하나를 세 관점에서 병렬 분석한다 (팬아웃/팬인 패턴).

설계 원칙: **LLM을 쓰지 않는다.**
    세 분석가는 이미 프로젝트에 있는 결정론적 로직을 재사용한다.
    - Fundamental : dart_checker(공시) + stock_validator(수급/공매도)
    - Technical   : indicators/technical.py 지표
    - News        : expert_orchestrator.get_news_sentiment(symbol)

    LLM은 다음 단계(Bull/Bear 토론, Trader, PM)에서만 쓴다.
    분석가까지 LLM으로 돌리면 종목당 호출이 7~8회로 뛰고, 무엇보다
    지표 계산처럼 답이 정해진 일에 확률적 모델을 쓸 이유가 없다.

각 분석가는 실패해도 중립 보고서(score=0, confidence=0)를 돌려주고
파이프라인을 멈추지 않는다 — 한 소스가 죽어도 나머지로 판단한다.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from loguru import logger

from .types import AnalystKind, AnalystReport

# 개별 분석가 타임아웃 (초) — 장중 경로이므로 짧게
ANALYST_TIMEOUT = 15.0


class FundamentalAnalyst:
    """공시·수급·공매도 기반 펀더멘탈 관점"""

    name = "fundamental"

    def __init__(self, stock_validator=None, dart_checker=None):
        self._validator = stock_validator
        self._dart = dart_checker

    async def analyze(self, symbol: str, name: str = "") -> AnalystReport:
        findings: List[str] = []
        metrics: Dict[str, Any] = {}
        score = 0
        # 데이터 소스가 하나도 없으면 "정보 없음"이다.
        # confidence=0이어야 aggregate_score의 가중치에서 빠진다 —
        # 0점을 0.3 가중치로 넣으면 다른 관점의 신호를 희석시킨다.
        confidence = 0.0

        if self._validator is None and self._dart is None:
            return AnalystReport(
                kind=AnalystKind.FUNDAMENTAL, symbol=symbol, score=0,
                summary="펀더멘탈 데이터 소스 미연결", confidence=0.0,
            )

        try:
            # 1) 종합 검증기 (수급/공매도/뉴스/공시를 한 번에)
            if self._validator is not None:
                result = await self._validator.validate(symbol, name or symbol)
                passed = getattr(result, "passed", None)
                reason = getattr(result, "reason", "") or ""

                sd = getattr(result, "supply_demand", None)
                if sd is not None:
                    foreign = getattr(sd, "foreign_net", None)
                    inst = getattr(sd, "institution_net", None)
                    metrics["foreign_net"] = foreign
                    metrics["institution_net"] = inst
                    # 외국인·기관 동반 순매수는 강한 신호
                    net_positive = sum(
                        1 for v in (foreign, inst) if v is not None and v > 0
                    )
                    if net_positive == 2:
                        score += 30
                        findings.append("외국인·기관 동반 순매수")
                    elif net_positive == 1:
                        score += 10
                        findings.append("기관 또는 외국인 순매수")
                    elif foreign is not None and inst is not None:
                        score -= 20
                        findings.append("외국인·기관 동반 순매도")

                ss = getattr(result, "short_selling", None)
                if ss is not None:
                    ratio = getattr(ss, "short_ratio", None)
                    metrics["short_ratio"] = ratio
                    if ratio is not None and ratio > 5.0:
                        score -= 15
                        findings.append(f"공매도 비중 높음 ({ratio:.1f}%)")

                if passed is False:
                    score -= 25
                    findings.append(f"검증 실패: {reason[:60]}")
                elif passed is True:
                    score += 10

                confidence = 0.7

            # 2) 공시 이상 징후
            if self._dart is not None:
                dart = await self._dart.check_disclosures(symbol, days=7)
                risky = getattr(dart, "has_risk", None)
                items = getattr(dart, "risk_items", None) or []
                metrics["dart_risk_items"] = len(items)
                if risky:
                    score -= 30
                    findings.append(f"공시 위험 신호 {len(items)}건")
                    confidence = max(confidence, 0.8)

        except Exception as e:
            logger.debug(f"[Analyst/fundamental] {symbol} 실패: {e}")
            return AnalystReport.failed(AnalystKind.FUNDAMENTAL, symbol, str(e))

        score = max(-100, min(100, score))
        summary = "; ".join(findings[:3]) if findings else "특이사항 없음"
        return AnalystReport(
            kind=AnalystKind.FUNDAMENTAL, symbol=symbol, score=score,
            summary=summary, findings=findings, metrics=metrics,
            confidence=confidence,
        )


class TechnicalAnalyst:
    """가격·거래량 지표 관점"""

    name = "technical"

    def __init__(self, price_provider=None):
        # price_provider: async def (symbol: str) -> DataFrame | None
        #   OHLCV 일봉을 반환하면 compute_indicators로 지표를 계산한다.
        #   스크리너가 이미 지표를 계산했다면 analyze(indicators=...)로 넘기는 편이
        #   중복 조회가 없어 더 낫다.
        self._prices = price_provider

    async def analyze(self, symbol: str, name: str = "",
                      indicators: Optional[Dict[str, Any]] = None) -> AnalystReport:
        findings: List[str] = []
        metrics: Dict[str, Any] = dict(indicators or {})
        score = 0

        try:
            # 지표를 외부에서 받으면 그대로 쓴다 (스크리너가 이미 계산한 값 재사용)
            if not metrics and self._prices is not None:
                df = await self._prices(symbol)
                if df is not None and not df.empty:
                    from ..indicators.technical import compute_indicators
                    metrics = compute_indicators(df) or {}

            if not metrics:
                return AnalystReport.failed(
                    AnalystKind.TECHNICAL, symbol, "지표 없음"
                )

            rsi = metrics.get("rsi_14")
            ma200_dist = metrics.get("ma200_distance_pct")
            atr_pct = metrics.get("atr_14")
            vol_ratio = metrics.get("volume_ratio")

            # RSI — 과열/과매도
            if rsi is not None:
                if rsi >= 75:
                    score -= 20
                    findings.append(f"RSI 과열 ({rsi:.0f})")
                elif rsi <= 30:
                    score += 15
                    findings.append(f"RSI 과매도 ({rsi:.0f})")
                elif 45 <= rsi <= 65:
                    score += 10
                    findings.append(f"RSI 중립 구간 ({rsi:.0f})")

            # MA200 이격 — 추세 위치
            if ma200_dist is not None:
                if ma200_dist > 40:
                    score -= 15
                    findings.append(f"MA200 과대 이격 (+{ma200_dist:.0f}%)")
                elif 0 < ma200_dist <= 25:
                    score += 25
                    findings.append(f"MA200 상단 건전 구간 (+{ma200_dist:.0f}%)")
                elif ma200_dist <= 0:
                    score -= 20
                    findings.append(f"MA200 하단 ({ma200_dist:.0f}%)")

            # 변동성
            if atr_pct is not None:
                metrics["atr_14"] = atr_pct
                if atr_pct > 7:
                    score -= 10
                    findings.append(f"변동성 과다 (ATR {atr_pct:.1f}%)")

            # 거래량
            if vol_ratio is not None and vol_ratio >= 2.0:
                score += 15
                findings.append(f"거래량 급증 ({vol_ratio:.1f}배)")

        except Exception as e:
            logger.debug(f"[Analyst/technical] {symbol} 실패: {e}")
            return AnalystReport.failed(AnalystKind.TECHNICAL, symbol, str(e))

        score = max(-100, min(100, score))
        summary = "; ".join(findings[:3]) if findings else "지표 중립"
        return AnalystReport(
            kind=AnalystKind.TECHNICAL, symbol=symbol, score=score,
            summary=summary, findings=findings, metrics=metrics,
            confidence=0.7 if findings else 0.4,
        )


class NewsAnalyst:
    """뉴스 sentiment 관점 — 기존 news_curator 재사용"""

    name = "news"

    def __init__(self, orchestrator=None):
        self._orch = orchestrator

    async def analyze(self, symbol: str, name: str = "") -> AnalystReport:
        if self._orch is None:
            return AnalystReport.failed(AnalystKind.NEWS, symbol, "orchestrator 없음")

        try:
            data = await self._orch.get_news_sentiment(symbol)
            if not data:
                # 뉴스가 없는 것은 오류가 아니라 "정보 없음"이다.
                # confidence=0으로 둬서 종합 점수 가중치에서 빠지게 한다
                # (0점을 0.3 가중치로 넣으면 다른 관점을 희석시킨다).
                return AnalystReport(
                    kind=AnalystKind.NEWS, symbol=symbol, score=0,
                    summary="관련 뉴스 없음", confidence=0.0,
                )

            # news_curator.get_symbol_sentiment 반환 스키마:
            #   {"score": -50, "tags": ["earnings_warning"], "items": 3}
            # score는 이미 -100~100 스케일이다. 스케일을 추정해 곱하면
            # 0.5(거의 중립)가 50점으로 증폭되는 사고가 난다 — 그대로 쓴다.
            try:
                score = int(float(data.get("score", 0)))
            except (TypeError, ValueError):
                score = 0
            score = max(-100, min(100, score))

            tags = data.get("tags") or []
            items = int(data.get("items", 0) or 0)

            findings = [str(t)[:60] for t in tags[:3]]
            if items:
                findings.append(f"관련 기사 {items}건")

            if tags:
                summary = f"sentiment {score:+d} / 태그: {', '.join(str(t) for t in tags[:3])}"
            elif items:
                summary = f"sentiment {score:+d} (기사 {items}건)"
            else:
                summary = "뉴스 중립"

            # 기사가 있어야 신뢰할 수 있다. 이벤트 태그가 붙으면 더 신뢰한다.
            if items <= 0:
                confidence = 0.2
            elif tags:
                confidence = 0.7
            else:
                confidence = 0.5

            return AnalystReport(
                kind=AnalystKind.NEWS, symbol=symbol, score=score,
                summary=summary[:200], findings=findings,
                metrics={"score": score, "tags": list(tags), "items": items},
                confidence=confidence,
            )
        except Exception as e:
            logger.debug(f"[Analyst/news] {symbol} 실패: {e}")
            return AnalystReport.failed(AnalystKind.NEWS, symbol, str(e))


class AnalystTeam:
    """세 분석가를 병렬 실행하고 보고서를 모은다 (팬아웃/팬인)"""

    def __init__(self, stock_validator=None, dart_checker=None,
                 orchestrator=None, price_provider=None):
        self.fundamental = FundamentalAnalyst(stock_validator, dart_checker)
        self.technical = TechnicalAnalyst(price_provider)
        self.news = NewsAnalyst(orchestrator)

    async def run(self, symbol: str, name: str = "",
                  indicators: Optional[Dict[str, Any]] = None) -> List[AnalystReport]:
        """
        세 관점을 동시에 수집한다.

        개별 분석가가 느리거나 죽어도 전체가 멈추지 않도록
        타임아웃과 예외를 각각 흡수한다.
        """
        async def _guard(coro, kind: AnalystKind):
            try:
                return await asyncio.wait_for(coro, timeout=ANALYST_TIMEOUT)
            except asyncio.TimeoutError:
                return AnalystReport.failed(kind, symbol, f"타임아웃({ANALYST_TIMEOUT}s)")
            except Exception as e:
                return AnalystReport.failed(kind, symbol, str(e))

        results = await asyncio.gather(
            _guard(self.fundamental.analyze(symbol, name), AnalystKind.FUNDAMENTAL),
            _guard(self.technical.analyze(symbol, name, indicators), AnalystKind.TECHNICAL),
            _guard(self.news.analyze(symbol, name), AnalystKind.NEWS),
        )
        return list(results)

    @staticmethod
    def aggregate_score(reports: List[AnalystReport]) -> int:
        """
        신뢰도 가중 평균 점수 (-100 ~ +100).

        실패한 보고서(confidence=0)는 자동으로 배제된다.
        """
        weighted = 0.0
        total_w = 0.0
        for r in reports:
            if not r.ok:
                continue
            w = max(0.0, r.confidence)
            weighted += r.score * w
            total_w += w
        if total_w <= 0:
            return 0
        return int(round(weighted / total_w))
