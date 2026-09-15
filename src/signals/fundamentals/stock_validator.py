"""
종목 검증 오케스트레이터

뉴스 검증과 DART 공시 검증을 병렬 실행하여 진입 전 리스크를 평가합니다.
수급/공매도/트렌드 결과 필드는 호환용이며, 미획득 상태를 유지합니다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional, Tuple

from loguru import logger

from .news_verifier import NewsVerifier, NewsCheckResult
from .dart_checker import DartChecker, DartCheckResult


@dataclass
class SupplyDemandResult:
    """수급 검증 결과 (외국인/기관 순매수)"""
    foreign_net_buying: bool = False
    institutional_net_buying: bool = False
    confidence_adjustment: float = 0.0


@dataclass
class ShortSellingResult:
    """공매도 검증 결과"""
    in_top50: bool = False
    confidence_adjustment: float = 0.0


@dataclass
class TrendBuzzResult:
    """검색 트렌드 검증 결과"""
    trend_direction: str = "neutral"  # rising / falling / neutral
    confidence_adjustment: float = 0.0


@dataclass
class ValidationResult:
    """종목 검증 결과"""
    approved: bool = True
    confidence_adjustment: float = 0.0
    block_reason: str = ""
    news_result: Optional[NewsCheckResult] = None
    dart_result: Optional[DartCheckResult] = None
    supply_demand_result: Optional[SupplyDemandResult] = None
    short_selling_result: Optional[ShortSellingResult] = None
    trend_buzz_result: Optional[TrendBuzzResult] = None
    # ── T11 근거 계약 (2026-09-15) — 기존 approved 의미·기본값·소비자(batch 검증 흐름)
    #    동작은 그대로 둔다. 신규 소비자(에이전트 팀 분석가)만 이 두 필드로
    #    "실제 검증을 수행했는가"를 approved 와 분리해 판단한다.
    validated: bool = True        # 실제 검증 수행 여부 (미연결·예외 시 False)
    data_status: str = "full"     # full | partial | insufficient | error


class StockValidator:
    """직접 뉴스/공시 기반 종목 검증 관리자 (추가 소스 미획득 유지)"""

    def __init__(self):
        self.news_verifier = NewsVerifier()
        self.dart_checker = DartChecker()

    async def initialize(self):
        """DART corp_code 초기화"""
        try:
            await self.dart_checker.ensure_corp_code_map()
        except Exception as e:
            logger.warning(f"[종목검증] DART 초기화 실패 (무시): {e}")

        logger.info("[종목검증] StockValidator 초기화 완료")

    async def validate(self, symbol: str, stock_name: str) -> ValidationResult:
        """
        종목 검증 (뉴스 + 공시 병렬 실행)

        Args:
            symbol: 종목코드 (예: "005930")
            stock_name: 종목명 (예: "삼성전자")

        Returns:
            ValidationResult: 검증 결과
        """
        try:
            (news_result, _news_ok), (dart_result, _dart_ok) = await asyncio.gather(
                self._safe_check_news(symbol, stock_name),
                self._safe_check_dart(symbol),
            )

            # 기존 미연결 출력과 호환: 비활성 소스를 정상 조회/위험 없음으로
            # 승격하지 않는다. 직접 뉴스/DART 성공만으로 전체 근거는 충족되지 않는다.
            sd_result = SupplyDemandResult()
            ss_result = ShortSellingResult()
            tb_result = TrendBuzzResult()

            # 알려진 DART 위험은 다른 소스의 미획득과 무관하게 차단한다.
            if dart_result.risk_level == "block":
                reason = f"위험 공시 감지: {', '.join(dart_result.risk_disclosures[:3])}"
                logger.info(f"[종목검증] {symbol} {stock_name} 차단: {reason}")
                return ValidationResult(
                    approved=False,
                    confidence_adjustment=-1.0,
                    block_reason=reason,
                    news_result=news_result,
                    dart_result=dart_result,
                    supply_demand_result=sd_result,
                    short_selling_result=ss_result,
                    trend_buzz_result=tb_result,
                    validated=True,
                    data_status="partial",
                )

            # confidence 조정 합산 (범위 제한: -0.30 ~ +0.25)
            total_adj = (
                news_result.confidence_adjustment
                + dart_result.confidence_adjustment
            )
            total_adj = max(-0.30, min(0.25, total_adj))

            return ValidationResult(
                approved=True,
                confidence_adjustment=total_adj,
                news_result=news_result,
                dart_result=dart_result,
                supply_demand_result=sd_result,
                short_selling_result=ss_result,
                trend_buzz_result=tb_result,
                validated=False,
                data_status="insufficient",
            )

        except Exception as e:
            # 예외 시 통과 (API 실패가 거래를 막지 않음) — 단, 실제로 검증하지 못했음을
            # validated=False/data_status="error"로 남겨 소비자가 "정보 없음"과
            # "위험 미발견"을 혼동하지 않게 한다.
            logger.debug(f"[종목검증] {symbol} 검증 예외 (통과): {e}")
            return ValidationResult(approved=True, validated=False, data_status="error")

    # ───────────────────── 기존 검증 (뉴스/DART) ─────────────────────

    async def _safe_check_news(self, symbol: str, stock_name: str) -> Tuple[NewsCheckResult, bool]:
        """뉴스 검증 (예외 안전)

        Returns:
            (result, ok) — ok=False는 미설정(NAVER_CLIENT_ID/SECRET 없음) 또는
            조회 예외로 기본값을 대신 돌려준 경우다 (T11 리뷰 r4 blocking #1,
            2026-09-15). 미획득을 정상 조회와 구분해 유지한다.
        """
        if not getattr(self.news_verifier, "_enabled", True):
            return NewsCheckResult(), False
        try:
            result = await self.news_verifier.check_news(symbol, stock_name)
        except Exception as e:
            logger.debug(f"[종목검증] 뉴스 검증 오류 ({symbol}): {e}")
            return NewsCheckResult(), False
        # 생산자 내부에서 HTTP 실패를 삼키고 기본값을 돌려준 경우는 fetched=False 다 (R-A r5)
        return result, bool(getattr(result, "fetched", True))

    async def _safe_check_dart(self, symbol: str) -> Tuple[DartCheckResult, bool]:
        """DART 공시 검증 (예외 안전)

        Returns:
            (result, ok) — ok=False는 미설정(DART_API_KEY 없음) 또는 조회 예외로
            기본값을 대신 돌려준 경우다 (T11 리뷰 r4 blocking #1, 2026-09-15).
            미획득을 정상 조회와 구분해 유지한다.
        """
        if not getattr(self.dart_checker, "_enabled", True):
            return DartCheckResult(), False
        # corp_code 매핑이 비었거나(initialize 실패) 종목이 매핑에 없으면 check_disclosures 는
        # 조회 없이 기본값을 돌려준다 — 그 조건과 1:1 로 미획득(ok=False) 처리 (R-A r5 blocking)
        corp_map = getattr(self.dart_checker, "_corp_code_map", None)
        if isinstance(corp_map, dict) and (not corp_map or str(symbol).lstrip("A").strip() not in corp_map):
            return DartCheckResult(), False
        try:
            result = await self.dart_checker.check_disclosures(symbol)
        except Exception as e:
            logger.debug(f"[종목검증] DART 검증 오류 ({symbol}): {e}")
            return DartCheckResult(), False
        # 생산자 내부에서 HTTP/API 실패를 삼키고 기본값을 돌려준 경우는 fetched=False 다
        return result, bool(getattr(result, "fetched", True))


# 전역 싱글톤 (클래스 정의 이후 배치)
_stock_validator: Optional[StockValidator] = None


def get_stock_validator() -> StockValidator:
    """전역 싱글톤 StockValidator 반환"""
    global _stock_validator
    if _stock_validator is None:
        _stock_validator = StockValidator()
    return _stock_validator
