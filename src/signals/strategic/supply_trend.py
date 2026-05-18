"""
AI Trading Bot v2 - 수급 추세 탐지 (Layer 2)

2~4주간 기관/외국인이 연속 순매수 중인 종목 탐지.
매일 15:35 실행.

데이터: pykrx MCP (20영업일 외국인/기관 순매수)
유니버스: KIS API 당일 수급 상위 ~100 + KOSPI200/KOSDAQ150 → ~250종목
"""

import asyncio
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


@dataclass
class SupplyTrendStock:
    """수급 추세 종목"""
    symbol: str
    name: str
    score: float  # 0~100
    foreign_streak: int  # 외국인 연속 순매수 일수
    inst_streak: int  # 기관 연속 순매수 일수
    foreign_total: float  # 최근 10일 외국인 누적 순매수(주)
    inst_total: float  # 최근 10일 기관 누적 순매수(주)
    is_accelerating: bool = False  # 순매수 가속
    delta_ratio: float = 0.0  # 2026-05-11 P0-1: today_net / 직전 5일 평균 (사전징후 추적)
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SupplyTrendStock":
        return cls(**d)


class SupplyTrendDetector:
    """2~4주 수급 추세 탐지"""

    def __init__(self, kis_market_data=None):
        self._kis_market_data = kis_market_data
        self._cache_dir = Path.home() / ".cache" / "ai_trader" / "strategic"
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    async def detect_accumulation(self) -> List[SupplyTrendStock]:
        """수급 매집 종목 탐지

        2026-05-18 P0-1 데이터 파이프라인 수리:
        - 기존 pykrx MCP 의존 → KIS API FHKST01010900 (fetch_stock_investor_daily) 우선
        - 장전/장후 모두 D-1 데이터 정상 반환 가능
        - pykrx는 폴백
        """
        logger.info("[수급추세] 탐지 시작...")

        try:
            # 1) 유니버스 구성 (KIS API 당일 수급 상위)
            universe = await self._build_universe()
            if not universe:
                # 폴백: KIS 종가 기준 수급 상위 종목 (D-1 기준)
                logger.debug("[수급추세] 유니버스 비어있음 (장 시작 전) → KOSPI200 폴백")
                universe = await self._build_fallback_universe()
                if not universe:
                    logger.warning("[수급추세] 폴백 유니버스도 비어있음 → 종료")
                    return []

            logger.info(f"[수급추세] 유니버스: {len(universe)}종목")

            # 2) KIS API FHKST01010900 우선 (장전/장후 모두 D-1 데이터 제공)
            use_kis = self._kis_market_data is not None and hasattr(
                self._kis_market_data, "fetch_stock_investor_daily"
            )
            manager = None
            if not use_kis:
                from src.utils.mcp_client import get_mcp_manager
                manager = get_mcp_manager()
                if not manager.is_server_available("pykrx"):
                    logger.warning(
                        "[수급추세] KIS API + pykrx 모두 사용 불가 → 폴백 모드"
                    )
                    return self._fallback_daily_only(universe)

            stocks = []
            universe_items = list(universe.items())[:80]
            sem = asyncio.Semaphore(8)  # 동시 8개

            async def bounded_analyze(sym, nm):
                async with sem:
                    try:
                        if use_kis:
                            result = await self._analyze_supply_trend_kis(sym, nm)
                        else:
                            result = await self._analyze_supply_trend(manager, sym, nm)
                        if result and result.score >= 50:
                            return result
                    except Exception as e:
                        logger.debug(f"[수급추세] {sym} 분석 실패: {e}")
                    return None

            results = await asyncio.gather(
                *(bounded_analyze(sym, nm) for sym, nm in universe_items),
                return_exceptions=True,
            )
            for r in results:
                if r and not isinstance(r, Exception):
                    stocks.append(r)

            stocks.sort(key=lambda x: x.score, reverse=True)

            # P0-1 검증 강화: 30종목 미달 시 캐시 미저장 (잡음 방지)
            if len(stocks) < 30 and len(stocks) > 0:
                logger.warning(
                    f"[수급추세] 탐지 종목 {len(stocks)}개 < 30 — 이전 캐시 유지 (잡음 방지)"
                )
                return stocks
            self._save_cache(stocks)

            logger.info(
                f"[수급추세] 탐지 완료: {len(stocks)}종목 (점수 50+), 소스={'KIS_API' if use_kis else 'pykrx'}"
            )
            for s in stocks[:5]:
                logger.info(
                    f"  {s.symbol} {s.name}: 점수={s.score:.0f} "
                    f"외국인{s.foreign_streak}일 기관{s.inst_streak}일 delta={s.delta_ratio:.1f}x"
                )

            return stocks

        except Exception as e:
            logger.error(f"[수급추세] 탐지 오류: {e}")
            return []

    async def _build_fallback_universe(self) -> Dict[str, str]:
        """장 시작 전 폴백: stock_master에서 KOSPI/KOSDAQ 시총 상위 80종목

        KIS API FHKST01010900은 D-1 데이터 제공하므로 유니버스가 정적이어도 OK.
        2026-05-18 P0-1 데이터 파이프라인 수리: 08:15 실행 시 0종목 문제 해결.
        """
        universe = {}
        # 1차 폴백: 하드코딩 KOSPI 대형주 (최소 보장 — KIS FHKST01010900은 D-1 데이터)
        if True:  # 항상 적용 (다음 단계에서 stock_master 동적 교체 예정)
            universe = {
                "005930": "삼성전자", "000660": "SK하이닉스", "373220": "LG에너지솔루션",
                "207940": "삼성바이오로직스", "005380": "현대차", "000270": "기아",
                "012330": "현대모비스", "068270": "셀트리온", "035420": "NAVER",
                "035720": "카카오", "005490": "POSCO홀딩스", "051910": "LG화학",
                "006400": "삼성SDI", "066570": "LG전자", "105560": "KB금융",
                "055550": "신한지주", "086790": "하나금융지주", "316140": "우리금융지주",
                "024110": "기업은행", "032830": "삼성생명", "402340": "SK스퀘어",
                "017670": "SK텔레콤", "030200": "KT", "015760": "한국전력",
                "034730": "SK", "003550": "LG", "267260": "HD현대일렉트릭",
                "009540": "HD한국조선해양", "329180": "HD현대중공업", "012450": "한화에어로스페이스",
                "272210": "한화시스템", "079550": "LIG넥스원", "086280": "현대글로비스",
                "047810": "한국항공우주", "010140": "삼성중공업", "028260": "삼성물산",
                "009830": "한화솔루션", "010130": "고려아연", "011170": "롯데케미칼",
            }
        return universe

    async def _analyze_supply_trend_kis(
        self, symbol: str, name: str
    ) -> Optional[SupplyTrendStock]:
        """KIS API FHKST01010900 기반 수급 분석 (2026-05-18 P0-1 수리)

        장전/장후 모두 D-1 기준 30영업일 데이터 조회 가능.
        """
        if not self._kis_market_data:
            return None

        try:
            # KIS 종목별 투자자 일별 매매동향 (최근 30일)
            data = await self._kis_market_data.fetch_stock_investor_daily(
                symbol, days=30
            )
        except Exception:
            return None

        if not data or len(data) < 5:
            return None

        # date_str → 오래된 순 정렬
        sorted_dates = sorted(data.keys())
        foreign_daily = [data[d]["foreign_net_buy"] for d in sorted_dates]
        inst_daily = [data[d]["inst_net_buy"] for d in sorted_dates]

        # 연속 순매수 일수 계산
        foreign_streak = self._count_consecutive_positive(foreign_daily)
        inst_streak = self._count_consecutive_positive(inst_daily)

        # 최근 10일 누적
        recent_10 = min(10, len(foreign_daily))
        foreign_total = sum(foreign_daily[-recent_10:])
        inst_total = sum(inst_daily[-recent_10:])

        # 가속 판단
        is_accelerating = False
        if len(foreign_daily) >= 10:
            recent_5 = sum(foreign_daily[-5:]) + sum(inst_daily[-5:])
            prev_5 = sum(foreign_daily[-10:-5]) + sum(inst_daily[-10:-5])
            is_accelerating = recent_5 > prev_5 > 0

        # delta_ratio (P0-1 핵심)
        delta_ratio = 0.0
        if len(foreign_daily) >= 6:
            today_net = foreign_daily[-1] + inst_daily[-1]
            prev_5_avg = (sum(foreign_daily[-6:-1]) + sum(inst_daily[-6:-1])) / 5
            if prev_5_avg > 0:
                delta_ratio = today_net / prev_5_avg

        score = self._calculate_trend_score(
            foreign_streak, inst_streak,
            foreign_total, inst_total,
            is_accelerating,
            delta_ratio=delta_ratio,
        )

        if score < 30:
            return None

        reasons = []
        if foreign_streak >= 5:
            reasons.append(f"외국인 {foreign_streak}일 연속 순매수")
        if inst_streak >= 5:
            reasons.append(f"기관 {inst_streak}일 연속 순매수")
        if foreign_streak >= 5 and inst_streak >= 5:
            reasons.append("외국인+기관 동시 매집")
        if is_accelerating:
            reasons.append("순매수 가속 중")
        if delta_ratio >= 5:
            reasons.append(f"수급 델타 폭증({delta_ratio:.1f}x)")
        elif delta_ratio >= 3:
            reasons.append(f"수급 델타 점프({delta_ratio:.1f}x)")

        return SupplyTrendStock(
            symbol=symbol,
            name=name,
            score=score,
            foreign_streak=foreign_streak,
            inst_streak=inst_streak,
            foreign_total=foreign_total,
            inst_total=inst_total,
            is_accelerating=is_accelerating,
            delta_ratio=round(delta_ratio, 2),
            reasons=reasons,
        )

    async def _build_universe(self) -> Dict[str, str]:
        """유니버스 구성: 당일 수급 상위 + 주요 종목"""
        universe = {}

        if not self._kis_market_data:
            return universe

        try:
            # 외국인/기관 순매수 상위 (코스피 + 코스닥)
            results = await asyncio.gather(
                self._kis_market_data.fetch_foreign_institution(market="0001", investor="1"),
                self._kis_market_data.fetch_foreign_institution(market="0002", investor="1"),
                self._kis_market_data.fetch_foreign_institution(market="0001", investor="2"),
                self._kis_market_data.fetch_foreign_institution(market="0002", investor="2"),
                return_exceptions=True,
            )

            for res in results:
                if isinstance(res, list):
                    for item in res[:50]:
                        symbol = item.get("symbol", "")
                        name = item.get("name", item.get("hts_kor_isnm", ""))
                        if symbol and name:
                            universe[symbol] = name

            logger.info(f"[수급추세] 수급 상위 유니버스: {len(universe)}종목")

        except Exception as e:
            logger.warning(f"[수급추세] 유니버스 구성 실패: {e}")

        return universe

    async def _analyze_supply_trend(
        self, mcp_manager, symbol: str, name: str
    ) -> Optional[SupplyTrendStock]:
        """개별 종목 수급 추세 분석"""
        # 20영업일 수급 조회
        end_date = datetime.now()
        start_date = end_date - timedelta(days=35)  # 20영업일 ≈ 28~30일

        try:
            result = await mcp_manager.call_tool(
                "pykrx",
                "get_market_trading_value_by_date",
                {
                    "fromdate": start_date.strftime("%Y%m%d"),
                    "todate": end_date.strftime("%Y%m%d"),
                    "ticker": symbol,
                },
            )
        except Exception:
            return None

        if not result:
            return None

        # 결과 파싱 (pykrx 반환 형식에 따라 적응)
        foreign_daily = []
        inst_daily = []

        if isinstance(result, list):
            for row in result:
                foreign_daily.append(float(row.get("외국인합계", row.get("foreign", 0))))
                inst_daily.append(float(row.get("기관합계", row.get("institution", 0))))
        elif isinstance(result, dict):
            # 단일 데이터
            foreign_daily.append(float(result.get("외국인합계", result.get("foreign", 0))))
            inst_daily.append(float(result.get("기관합계", result.get("institution", 0))))
        else:
            return None

        if len(foreign_daily) < 5:
            return None

        # 연속 순매수 일수 계산
        foreign_streak = self._count_consecutive_positive(foreign_daily)
        inst_streak = self._count_consecutive_positive(inst_daily)

        # 최근 10일 누적
        recent_10 = min(10, len(foreign_daily))
        foreign_total = sum(foreign_daily[-recent_10:])
        inst_total = sum(inst_daily[-recent_10:])

        # 가속 판단 (최근 5일 > 이전 5일)
        is_accelerating = False
        if len(foreign_daily) >= 10:
            recent_5 = sum(foreign_daily[-5:]) + sum(inst_daily[-5:])
            prev_5 = sum(foreign_daily[-10:-5]) + sum(inst_daily[-10:-5])
            is_accelerating = recent_5 > prev_5 > 0

        # 2026-05-10 P0-1: 수급 델타(1차 미분) 보너스
        # 가설: today 외국인 net이 5일 평균의 3배+ → 폭등 사전징후
        # 5/4 SK하이닉스 사례: 4/30~5/4 외국인 누적 412만주, 5/4 단독 289만주(평균 대비 ~3.5배)
        # 진입 차단된 종목이 +31% 폭등
        delta_ratio = 0.0
        if len(foreign_daily) >= 6:
            today_net = foreign_daily[-1] + inst_daily[-1]
            prev_5_avg = (sum(foreign_daily[-6:-1]) + sum(inst_daily[-6:-1])) / 5
            if prev_5_avg > 0:
                delta_ratio = today_net / prev_5_avg

        # 점수 산출
        score = self._calculate_trend_score(
            foreign_streak, inst_streak,
            foreign_total, inst_total,
            is_accelerating,
            delta_ratio=delta_ratio,
        )

        if score < 30:
            return None

        reasons = []
        if foreign_streak >= 5:
            reasons.append(f"외국인 {foreign_streak}일 연속 순매수")
        if inst_streak >= 5:
            reasons.append(f"기관 {inst_streak}일 연속 순매수")
        if foreign_streak >= 5 and inst_streak >= 5:
            reasons.append("외국인+기관 동시 매집")
        if is_accelerating:
            reasons.append("순매수 가속 중")
        if delta_ratio >= 5:
            reasons.append(f"수급 델타 폭증({delta_ratio:.1f}x)")
        elif delta_ratio >= 3:
            reasons.append(f"수급 델타 점프({delta_ratio:.1f}x)")

        return SupplyTrendStock(
            symbol=symbol,
            name=name,
            score=score,
            foreign_streak=foreign_streak,
            inst_streak=inst_streak,
            foreign_total=foreign_total,
            inst_total=inst_total,
            is_accelerating=is_accelerating,
            delta_ratio=round(delta_ratio, 2),
            reasons=reasons,
        )

    @staticmethod
    def _count_consecutive_positive(values: List[float]) -> int:
        """끝에서부터 연속 양수 일수"""
        count = 0
        for v in reversed(values):
            if v > 0:
                count += 1
            else:
                break
        return count

    @staticmethod
    def _calculate_trend_score(
        foreign_streak: int,
        inst_streak: int,
        foreign_total: float,
        inst_total: float,
        is_accelerating: bool,
        delta_ratio: float = 0.0,
    ) -> float:
        """수급 추세 점수 (0~100)"""
        score = 0.0

        # 외국인 연속 순매수
        if foreign_streak >= 10:
            score += 25
        elif foreign_streak >= 5:
            score += 20
        elif foreign_streak >= 3:
            score += 10

        # 기관 연속 순매수
        if inst_streak >= 10:
            score += 25
        elif inst_streak >= 5:
            score += 20
        elif inst_streak >= 3:
            score += 10

        # 외국인+기관 동시
        if foreign_streak >= 5 and inst_streak >= 5:
            score += 15

        # 누적 순매수 규모 (절대값 기준 보너스)
        total = foreign_total + inst_total
        if total > 0:
            score += min(15, total / 100000)  # 10만주당 1점, 최대 15점

        # 가속
        if is_accelerating:
            score += 10

        # 2026-05-10 P0-1: 수급 델타(1차 미분) 보너스
        # today 외국인+기관 net / 직전 5일 평균 → 3배+ 점프 시 폭등 사전징후
        if delta_ratio >= 5:
            score += 30  # 폭증 (extreme)
        elif delta_ratio >= 3:
            score += 20  # 점프 (strong)

        return min(score, 100)

    def _fallback_daily_only(self, universe: Dict[str, str]) -> List[SupplyTrendStock]:
        """pykrx 불가 시 당일 수급 데이터만으로 간이 점수"""
        # 유니버스에 있는 것 자체가 당일 순매수 상위 → 기본 점수 부여
        stocks = []
        for symbol, name in list(universe.items())[:30]:
            stocks.append(SupplyTrendStock(
                symbol=symbol,
                name=name,
                score=55,  # 당일 수급 상위이므로 기본 55점 (pykrx 미확인)
                foreign_streak=1,
                inst_streak=1,
                foreign_total=0,
                inst_total=0,
                reasons=["당일 수급 상위 (연속 데이터 미확인)"],
            ))
        return stocks

    def _save_cache(self, stocks: List[SupplyTrendStock]):
        """결과 캐시 저장 + 오래된 캐시 정리"""
        today = datetime.now().strftime("%Y%m%d")
        path = self._cache_dir / f"supply_trend_{today}.json"
        try:
            data = [s.to_dict() for s in stocks]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._cleanup_old_cache("supply_trend")
        except Exception as e:
            logger.warning(f"[수급추세] 캐시 저장 실패: {e}")

    @staticmethod
    def _cleanup_old_cache(prefix: str, max_age_days: int = 7):
        """오래된 캐시 파일 정리"""
        cache_dir = Path.home() / ".cache" / "ai_trader" / "strategic"
        cutoff = datetime.now() - timedelta(days=max_age_days)
        for f in cache_dir.glob(f"{prefix}_*.json"):
            try:
                date_str = f.stem.split("_")[-1]
                file_date = datetime.strptime(date_str, "%Y%m%d")
                if file_date < cutoff:
                    f.unlink()
            except (ValueError, OSError):
                pass

    def load_cache(self) -> List[SupplyTrendStock]:
        """오늘 캐시 로드"""
        today = datetime.now().strftime("%Y%m%d")
        path = self._cache_dir / f"supply_trend_{today}.json"
        try:
            if not path.exists():
                return []
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [SupplyTrendStock.from_dict(d) for d in data]
        except Exception as e:
            logger.debug(f"[수급추세] 캐시 로드 실패: {e}")
            return []
