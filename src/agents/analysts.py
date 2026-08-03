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
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from loguru import logger

from .types import AnalystKind, AnalystReport

# 개별 분석가 타임아웃 (초) — 장중 경로이므로 짧게
ANALYST_TIMEOUT = 15.0

# ── 근거 신선도 하드 한계 (2026-08-03) ────────────────────────────
# 상대 가중치 감쇠만으로는 부족하다. `aggregate_score`가 가중평균을 쓰기 때문에
#   Σ(score×w/k) / Σ(w/k) = Σ(score×w) / Σ(w)
# 로 감쇠가 **상쇄된다**. 실측: 전부 신선 +62 / 전부 4시간 전 +62 (동일).
# 즉 모든 근거가 함께 낡으면 종합 점수는 전혀 변하지 않는다.
#
# 그래서 절대 기준을 둔다: TTL을 넘긴 근거는 아예 제외하고,
# 남은 근거가 부족하면 매수 판단 자체를 못 하게 한다 (fail-closed).
HARD_TTL_MIN: Dict[str, float] = {
    "technical": 45.0,      # 지표 — 장중 가격 기반이라 가장 짧다
    "fundamental": 180.0,   # 수급/공시 — 하루 단위로 갱신
    "news": 180.0,          # 뉴스 sentiment
}

# 매수 판단에 필요한 최소 근거량
MIN_VALID_SOURCES = 2       # 유효 보고서 수
MIN_TOTAL_WEIGHT = 0.5      # 감쇠 후 가중치 합 (정보량의 절대 하한)


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
                # ⚠️ 2026-08-03: 아래 이름들이 전부 ValidationResult와 어긋나 있었다.
                #    passed→approved, reason→block_reason,
                #    supply_demand→supply_demand_result, short_selling→short_selling_result.
                #    getattr 기본값이 조용히 삼켜서 이 분석가는 **항상 score=0**을 내면서
                #    confidence=0.7을 주장했다. 가중평균에서 뉴스 점수를 절반으로
                #    희석시키기만 하는 유령 근거였다 (심의 13건 전부 score 0).
                #    하위 필드도 숫자가 아니라 bool이다 — 순매수 "여부"만 알 수 있다.
                passed = getattr(result, "approved", None)
                reason = getattr(result, "block_reason", "") or ""

                sd = getattr(result, "supply_demand_result", None)
                if sd is not None:
                    foreign = bool(getattr(sd, "foreign_net_buying", False))
                    inst = bool(getattr(sd, "institutional_net_buying", False))
                    metrics["foreign_net_buying"] = foreign
                    metrics["institutional_net_buying"] = inst
                    # 외국인·기관 동반 순매수는 강한 신호.
                    # 반대쪽(순매도)은 감점하지 않는다 — bool은 "순매수 아님"까지만
                    # 말해주고 순매도인지 관망인지는 구분하지 못한다.
                    if foreign and inst:
                        score += 30
                        findings.append("외국인·기관 동반 순매수")
                    elif foreign or inst:
                        score += 10
                        findings.append("외국인" if foreign else "기관")

                ss = getattr(result, "short_selling_result", None)
                if ss is not None and bool(getattr(ss, "in_top50", False)):
                    metrics["short_top50"] = True
                    score -= 15
                    findings.append("공매도 상위 50종목")

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
        # stock_validator는 수급 30분 / 트렌드 2시간 캐시를 쓴다.
        # 캐시 히트 시각을 알 수 없으므로 보수적으로 TTL 절반만큼 지난 것으로 본다
        # (실제보다 신선하다고 가정해 과대평가하는 것보다 낫다).
        return AnalystReport(
            kind=AnalystKind.FUNDAMENTAL, symbol=symbol, score=score,
            summary=summary, findings=findings, metrics=metrics,
            confidence=confidence,
            data_as_of=datetime.now() - timedelta(minutes=15),
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
                      indicators: Optional[Dict[str, Any]] = None,
                      indicators_as_of: Optional[datetime] = None) -> AnalystReport:
        """
        Args:
            indicators: 이미 계산된 지표 (스크리너 결과 재사용)
            indicators_as_of: 그 지표가 계산된 시각.
                스크리닝은 5분 주기라 심의 시점엔 최대 수십 분 지난 값일 수 있다.
                None이면 현재로 간주하므로, 재사용 시 반드시 실제 시각을 넘길 것.
        """
        findings: List[str] = []
        metrics: Dict[str, Any] = dict(indicators or {})
        score = 0
        as_of = indicators_as_of or datetime.now()

        try:
            # 지표를 외부에서 받으면 그대로 쓴다 (스크리너가 이미 계산한 값 재사용)
            if not metrics and self._prices is not None:
                df = await self._prices(symbol)
                if df is not None and not df.empty:
                    from ..indicators.technical import compute_indicators
                    metrics = compute_indicators(df) or {}
                    # 조회 시각이 아니라 **마지막 관측 시각**이 데이터의 나이다.
                    # 일봉을 방금 받아왔다고 그 값이 실시간인 것은 아니다.
                    try:
                        last_idx = df.index[-1]
                        obs = getattr(last_idx, "to_pydatetime", None)
                        if obs is not None:
                            as_of = obs()
                        elif isinstance(last_idx, datetime):
                            as_of = last_idx
                    except (IndexError, AttributeError, TypeError, ValueError):
                        pass    # 인덱스가 시각이 아니면 호출 시각을 유지

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
            data_as_of=as_of,
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

            # news_curator는 종목 sentiment를 1시간 TTL로 캐시한다 (_SYMBOL_TTL).
            # 캐시 생성 시각을 알 수 없어 보수적으로 TTL 절반을 가정한다.
            return AnalystReport(
                kind=AnalystKind.NEWS, symbol=symbol, score=score,
                summary=summary[:200], findings=findings,
                metrics={"score": score, "tags": list(tags), "items": items},
                confidence=confidence,
                data_as_of=datetime.now() - timedelta(minutes=30),
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
                  indicators: Optional[Dict[str, Any]] = None,
                  indicators_as_of: Optional[datetime] = None) -> List[AnalystReport]:
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
            _guard(self.technical.analyze(symbol, name, indicators, indicators_as_of),
                   AnalystKind.TECHNICAL),
            _guard(self.news.analyze(symbol, name), AnalystKind.NEWS),
        )
        return list(results)

    @staticmethod
    def aggregate_score(reports: List[AnalystReport],
                        apply_freshness_decay: bool = True) -> int:
        """
        신뢰도 가중 평균 점수 (-100 ~ +100).

        실패한 보고서(confidence=0)는 자동으로 배제된다.

        apply_freshness_decay=True면 **데이터 나이에 따라 가중치를 감쇠**시킨다.
        수급 캐시(최대 30분)와 뉴스 캐시(최대 1시간)와 방금 계산한 지표를
        같은 무게로 합치면 종합 점수가 과거를 반영하게 되기 때문이다.
        """
        weighted = 0.0
        total_w = 0.0
        for r in reports:
            if not r.ok or AnalystTeam.is_expired(r):
                continue
            w = (r.freshness_decayed_confidence() if apply_freshness_decay
                 else max(0.0, r.confidence))
            weighted += r.score * w
            total_w += w
        if total_w <= 0:
            return 0
        return int(round(weighted / total_w))

    @staticmethod
    def is_expired(report: AnalystReport) -> bool:
        """소스별 hard TTL 초과 여부 — 초과분은 집계에서 통째로 제외한다"""
        ttl = HARD_TTL_MIN.get(report.kind.value)
        if ttl is None:
            return False
        return report.age_minutes > ttl

    @staticmethod
    def evidence_quality(reports: List[AnalystReport]) -> tuple:
        """
        매수 판단을 내려도 될 만큼 근거가 남아 있는지 검사한다.

        가중평균은 상대 비중만 보므로 "정보가 얼마나 남았는가"를 알려주지 못한다.
        여기서 절대량(유효 소스 수 + 감쇠 후 가중치 합)을 따로 확인한다.

        Returns:
            (ok: bool, reason: str, total_weight: float)
        """
        valid = [r for r in reports if r.ok and not AnalystTeam.is_expired(r)]
        total_w = sum(r.freshness_decayed_confidence() for r in valid)

        expired = [r.kind.value for r in reports
                   if r.ok and AnalystTeam.is_expired(r)]
        if len(valid) < MIN_VALID_SOURCES:
            return (False,
                    f"유효 근거 {len(valid)}개 < {MIN_VALID_SOURCES}개"
                    + (f" (TTL 초과: {expired})" if expired else ""),
                    total_w)
        if total_w < MIN_TOTAL_WEIGHT:
            return (False,
                    f"근거 총량 부족 (가중치 {total_w:.2f} < {MIN_TOTAL_WEIGHT}) "
                    f"— 남은 근거가 모두 낡음",
                    total_w)
        return (True, "", total_w)

    @staticmethod
    def freshness_summary(reports: List[AnalystReport]) -> str:
        """보고서별 데이터 나이 요약 (토론 프롬프트·로그용)"""
        parts = []
        for r in reports:
            if not r.ok:
                continue
            age = r.age_minutes
            parts.append(f"{r.kind.value} {age:.0f}분 전" if age >= 1
                         else f"{r.kind.value} 실시간")
        return ", ".join(parts)
