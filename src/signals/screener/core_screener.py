"""
QWQ AI Trader - 코어홀딩 종목 스크리너

대형 우량주 중심 중장기 보유 종목 발굴.
시총 5000억+, MA200 존재, 안정적 우상향 추세 종목 스코어링.

스코어링 (100점 만점):
    추세 안정성  30점
    펀더멘탈    30점
    수급 추세   20점
    모멘텀 품질 20점
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from loguru import logger

from src.indicators.technical import TechnicalIndicators


@dataclass
class CoreCandidate:
    """코어홀딩 후보 종목"""
    symbol: str
    name: str
    score: float = 0.0
    entry_price: Decimal = Decimal("0")
    indicators: Dict[str, Any] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)


class CoreScreener:
    """코어홀딩 종목 스크리너

    유니버스 → 기본 필터 → 일봉 지표 → 펀더멘탈 → 수급 → 스코어링
    """

    def __init__(self, broker, kis_market_data, stock_master=None, config: Optional[Dict] = None):
        self._broker = broker
        self._kis_market_data = kis_market_data
        self._stock_master = stock_master
        self._indicators = TechnicalIndicators()
        self._config = config or {}

        # 필터 설정
        self._min_market_cap_b = self._config.get("min_market_cap_b", 0.5)  # 5000억 = 0.5조
        self._min_price = self._config.get("min_price", 5000)
        self._min_avg_trading_value = self._config.get("min_avg_trading_value", 1_000_000_000)
        self._min_score = self._config.get("min_score", 70)

    async def run_full_scan(self) -> List[CoreCandidate]:
        """
        전체 코어홀딩 스캔

        Returns:
            점수 순 정렬된 CoreCandidate 리스트
        """
        logger.info("[코어스크리너] 전체 스캔 시작...")

        # 1단계: 유니버스 구축 (대형주 중심)
        universe = await self._build_universe()
        logger.info(f"[코어스크리너] 유니버스: {len(universe)}개 종목")

        if not universe:
            logger.warning("[코어스크리너] 유니버스 비어있음")
            return []

        # 2단계: 일봉 + 기술적 지표 계산
        candidates = await self._calculate_indicators(universe)
        logger.info(f"[코어스크리너] 지표 계산 완료: {len(candidates)}개")

        # 3단계: 기본 필터 (MA200 존재, PER>0 등)
        filtered = self._apply_base_filter(candidates)
        logger.info(f"[코어스크리너] 기본 필터 통과: {len(filtered)}개")

        # 4단계: 수급 데이터 보강
        await self._enrich_supply_demand(filtered)

        # 5단계: 스코어링
        scored = self._score_candidates(filtered)

        # 6단계: 점수 순 정렬
        scored.sort(key=lambda c: c.score, reverse=True)

        # 상위 로그
        for i, c in enumerate(scored[:10]):
            logger.info(
                f"[코어스크리너] #{i+1} {c.symbol} {c.name}: "
                f"{c.score:.1f}점 ({', '.join(c.reasons[:3])})"
            )

        return scored

    async def _build_universe(self) -> List[Dict[str, Any]]:
        """대형주 유니버스 구축

        StockMaster.get_top_stocks() → 종목코드 리스트
        KISMarketData.fetch_batch_valuations() → 펀더멘탈 데이터 (PER, PBR 등)
        """
        universe = []

        # StockMaster에서 상위 종목 가져오기
        symbols_with_names: Dict[str, str] = {}  # {code: name}

        if self._stock_master:
            try:
                # get_top_stocks() → ["삼성전자=005930", ...]
                top_stocks = await self._stock_master.get_top_stocks(limit=150)
                if top_stocks:
                    for item in top_stocks:
                        if "=" in item:
                            name, code = item.rsplit("=", 1)
                            symbols_with_names[code] = name
                    logger.info(f"[코어스크리너] StockMaster에서 {len(symbols_with_names)}개 종목 추출")
            except Exception as e:
                logger.warning(f"[코어스크리너] StockMaster 조회 실패: {e}")

        # StockMaster 없으면 tradeable_universe 사용
        if not symbols_with_names and self._stock_master:
            try:
                ticker_set = await self._stock_master.get_tradeable_universe()
                if ticker_set:
                    for code in ticker_set:
                        symbols_with_names[code] = ""
                    logger.info(f"[코어스크리너] tradeable_universe에서 {len(symbols_with_names)}개 종목 추출")
            except Exception as e:
                logger.warning(f"[코어스크리너] tradeable_universe 조회 실패: {e}")

        if not symbols_with_names:
            logger.warning("[코어스크리너] 유니버스 구축 실패: 종목 목록 없음")
            return []

        # ETF/ETN 제외
        filtered_symbols = {
            code: name for code, name in symbols_with_names.items()
            if not self._is_etf_etn(name)
        }

        # KIS API로 밸류에이션 데이터 조회
        symbol_list = list(filtered_symbols.keys())
        valuations: Dict[str, Dict] = {}

        if self._kis_market_data:
            try:
                valuations = await self._kis_market_data.fetch_batch_valuations(symbol_list)
                logger.info(f"[코어스크리너] 밸류에이션 조회 완료: {len(valuations)}개")
            except Exception as e:
                logger.warning(f"[코어스크리너] 밸류에이션 조회 실패: {e}")

        # 유니버스 구성
        for code in symbol_list:
            name = filtered_symbols[code]
            val = valuations.get(code, {})

            price = val.get("price", 0)
            if price is None:
                price = 0
            try:
                price = float(price)
            except (TypeError, ValueError):
                price = 0

            # 최소 가격 필터
            if price < self._min_price:
                continue

            per = val.get("per", 0) or 0
            pbr = val.get("pbr", 0) or 0

            universe.append({
                "symbol": code,
                "name": name or val.get("name", ""),
                "price": price,
                "per": float(per),
                "pbr": float(pbr),
                "eps": float(val.get("eps", 0) or 0),
                "bps": float(val.get("bps", 0) or 0),
            })

        logger.info(f"[코어스크리너] 최종 유니버스: {len(universe)}개 (가격필터 후)")
        return universe

    async def _calculate_indicators(self, universe: List[Dict]) -> List[CoreCandidate]:
        """일봉 데이터 + 기술적 지표 계산"""
        candidates = []
        batch_size = 10  # 동시 요청 제한

        for i in range(0, len(universe), batch_size):
            batch = universe[i:i + batch_size]
            tasks = [self._fetch_and_calc(item) for item in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for item, result in zip(batch, results):
                if isinstance(result, Exception):
                    logger.debug(f"[코어스크리너] {item['symbol']} 지표 계산 실패: {result}")
                    continue
                if result is not None:
                    candidates.append(result)

            # API 레이트 리밋 방지
            if i + batch_size < len(universe):
                await asyncio.sleep(0.5)

        return candidates

    async def _fetch_and_calc(self, item: Dict) -> Optional[CoreCandidate]:
        """단일 종목 일봉 조회 + 지표 계산"""
        symbol = item["symbol"]
        try:
            # broker.get_daily_prices(symbol, days=250) 사용
            candles = await self._broker.get_daily_prices(symbol, days=250)

            if candles is None or len(candles) < 200:
                return None

            closes = [float(c["close"]) for c in candles]
            highs = [float(c["high"]) for c in candles]
            lows = [float(c["low"]) for c in candles]
            volumes = [int(c.get("volume", 0)) for c in candles]

            # 이동평균 계산
            ind: Dict[str, Any] = {}
            ind["close"] = closes[-1]
            ind["ma5"] = sum(closes[-5:]) / 5 if len(closes) >= 5 else None
            ind["ma10"] = sum(closes[-10:]) / 10 if len(closes) >= 10 else None
            ind["ma20"] = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
            ind["ma50"] = sum(closes[-50:]) / 50 if len(closes) >= 50 else None
            ind["ma60"] = sum(closes[-60:]) / 60 if len(closes) >= 60 else None
            ind["ma120"] = sum(closes[-120:]) / 120 if len(closes) >= 120 else None
            ind["ma200"] = sum(closes[-200:]) / 200 if len(closes) >= 200 else None

            # 52주 고점/저점
            ind["high_52w"] = max(highs[-250:]) if len(highs) >= 250 else max(highs)
            ind["low_52w"] = min(lows[-250:]) if len(lows) >= 250 else min(lows)

            # 수익률 계산
            if len(closes) >= 21:
                ind["change_20d"] = (closes[-1] - closes[-21]) / closes[-21] * 100
            if len(closes) >= 61:
                ind["change_60d"] = (closes[-1] - closes[-61]) / closes[-61] * 100
            if len(closes) >= 126:
                ind["change_6m"] = (closes[-1] - closes[-126]) / closes[-126] * 100

            # 변동성 (20일 표준편차)
            if len(closes) >= 20:
                mean20 = sum(closes[-20:]) / 20
                var20 = sum((x - mean20) ** 2 for x in closes[-20:]) / 20
                ind["volatility_20d"] = (var20 ** 0.5) / mean20 * 100 if mean20 > 0 else 0

            # 거래대금 평균 (20일)
            if len(volumes) >= 20 and len(closes) >= 20:
                trading_values = [closes[-(20-j)] * volumes[-(20-j)] for j in range(20)]
                ind["avg_trading_value"] = sum(trading_values) / 20

            # MA 정배열 체크
            ma5 = ind.get("ma5")
            ma20 = ind.get("ma20")
            ma50 = ind.get("ma50")
            ma200 = ind.get("ma200")
            ma_aligned = False
            if all(v is not None and v > 0 for v in [ma5, ma20, ma50, ma200]):
                ma_aligned = ma5 > ma20 > ma50 > ma200
            ind["ma_aligned"] = ma_aligned
            ind["ma5_above_ma20"] = (ma5 is not None and ma20 is not None and ma5 > ma20)

            # 펀더멘탈 (유니버스에서 전달받은 값)
            ind["per"] = item.get("per", 0)
            ind["pbr"] = item.get("pbr", 0)

            candidate = CoreCandidate(
                symbol=symbol,
                name=item.get("name", ""),
                entry_price=Decimal(str(closes[-1])),
                indicators=ind,
            )
            return candidate

        except Exception as e:
            logger.debug(f"[코어스크리너] {symbol} 처리 실패: {e}")
            return None

    async def _enrich_supply_demand(self, candidates: List[CoreCandidate]) -> None:
        """수급 데이터(외인/기관 순매수) 보강"""
        if not self._kis_market_data or not candidates:
            return

        symbols = [c.symbol for c in candidates]
        today_str = datetime.now().strftime("%Y%m%d")

        try:
            # 배치 수급 조회 (5일간 합산)
            investor_data: Dict[str, Dict] = {}
            for sym in symbols:
                try:
                    daily = await self._kis_market_data.fetch_stock_investor_daily(sym, days=5)
                    if daily:
                        total_foreign = sum(d.get("foreign_net_buy", 0) for d in daily.values())
                        total_inst = sum(d.get("inst_net_buy", 0) for d in daily.values())
                        investor_data[sym] = {
                            "foreign_net_buy_5d": total_foreign,
                            "inst_net_buy_5d": total_inst,
                        }
                except Exception:
                    pass
                # 레이트 리밋
                await asyncio.sleep(0.1)

            # 후보에 수급 데이터 병합
            for c in candidates:
                sd = investor_data.get(c.symbol, {})
                c.indicators["foreign_net_buy_5d"] = sd.get("foreign_net_buy_5d")
                c.indicators["inst_net_buy_5d"] = sd.get("inst_net_buy_5d")

            logger.info(f"[코어스크리너] 수급 데이터 보강 완료: {len(investor_data)}/{len(candidates)}개")

        except Exception as e:
            logger.warning(f"[코어스크리너] 수급 데이터 조회 실패: {e}")

    def _apply_base_filter(self, candidates: List[CoreCandidate]) -> List[CoreCandidate]:
        """기본 필터: MA200 존재, PER>0, 거래대금 충분"""
        filtered = []
        for c in candidates:
            ind = c.indicators

            # MA200 존재 필수
            if ind.get("ma200") is None or ind["ma200"] <= 0:
                continue

            # 현재가 > MA200 (상승 추세)
            close = ind.get("close", 0)
            if close <= ind["ma200"]:
                continue

            # PER > 0 (적자 기업 제외)
            per = ind.get("per", 0)
            if per is not None and per <= 0:
                continue

            # 거래대금 필터
            avg_tv = ind.get("avg_trading_value", 0)
            if avg_tv is not None and avg_tv < self._min_avg_trading_value:
                continue

            filtered.append(c)
        return filtered

    def _score_candidates(self, candidates: List[CoreCandidate]) -> List[CoreCandidate]:
        """100점 만점 스코어링"""
        for c in candidates:
            score = 0.0
            reasons = []
            ind = c.indicators

            # ── 추세 안정성 (30점) ──
            trend_score = self._score_trend(ind, reasons)
            score += trend_score

            # ── 펀더멘탈 (30점) ──
            fund_score = self._score_fundamentals(ind, reasons)
            score += fund_score

            # ── 수급 추세 (20점) ──
            supply_score = self._score_supply(ind, reasons)
            score += supply_score

            # ── 모멘텀 품질 (20점) ──
            momentum_score = self._score_momentum(ind, reasons)
            score += momentum_score

            c.score = min(score, 100.0)
            c.reasons = reasons

        return candidates

    def _score_trend(self, ind: Dict, reasons: List[str]) -> float:
        """추세 안정성 (30점)"""
        score = 0.0

        # MA 정배열 (10점)
        if ind.get("ma_aligned"):
            score += 10
            reasons.append("MA정배열")

        # MA200 위 (5점)
        close = ind.get("close", 0)
        ma200 = ind.get("ma200", 0)
        if close > 0 and ma200 > 0 and close > ma200:
            score += 5
            reasons.append("MA200↑")

        # 52주 고점 -15% 이내 (5점)
        high_52w = ind.get("high_52w", 0)
        if close > 0 and high_52w > 0:
            from_high_pct = (close - high_52w) / high_52w * 100
            if from_high_pct >= -15:
                score += 5
                if from_high_pct >= -5:
                    reasons.append(f"52주고점근접({from_high_pct:+.1f}%)")

        # 6개월 수익률 > 0 (5점)
        change_6m = ind.get("change_6m")
        if change_6m is not None and change_6m > 0:
            score += 5
            reasons.append(f"6M+{change_6m:.1f}%")

        # 저변동성 (5점): 20일 변동성 < 3%
        vol20 = ind.get("volatility_20d", 999)
        if vol20 is not None and vol20 < 3.0:
            score += 5
            reasons.append("저변동")
        elif vol20 is not None and vol20 < 5.0:
            score += 3

        return score

    def _score_fundamentals(self, ind: Dict, reasons: List[str]) -> float:
        """펀더멘탈 (30점)"""
        score = 0.0

        # PER 적정 (5점)
        per = ind.get("per", 0)
        if per is not None and per > 0:
            if 5 <= per <= 15:
                score += 5
            elif per <= 25:
                score += 3
            elif per <= 40:
                score += 1

        # PBR (3점) — 좁은 범위 먼저 체크
        pbr = ind.get("pbr", 0)
        if pbr is not None and 0 < pbr < 3:
            score += 3
            reasons.append(f"PBR{pbr:.1f}")
        elif pbr is not None and 0 < pbr < 5:
            score += 2

        return score

    def _score_supply(self, ind: Dict, reasons: List[str]) -> float:
        """수급 추세 (20점)"""
        score = 0.0

        # 외인 순매수 5일 (10점)
        foreign_net = ind.get("foreign_net_buy_5d")
        if foreign_net is not None and foreign_net > 0:
            score += 10
            reasons.append("외인매수")
        elif foreign_net is not None and foreign_net == 0:
            score += 3

        # 기관 순매수 5일 (10점)
        inst_net = ind.get("inst_net_buy_5d")
        if inst_net is not None and inst_net > 0:
            score += 10
            reasons.append("기관매수")
        elif inst_net is not None and inst_net == 0:
            score += 3

        # 수급 데이터 없으면 기본 점수
        if foreign_net is None and inst_net is None:
            score += 6  # 기본 중립 점수

        return score

    def _score_momentum(self, ind: Dict, reasons: List[str]) -> float:
        """모멘텀 품질 (20점)"""
        score = 0.0

        # MRS > 0 (5점) - 있으면 사용
        mrs = ind.get("mrs")
        if mrs is not None and mrs > 0:
            score += 5
            reasons.append("MRS↑")
        elif mrs is None:
            score += 2  # MRS 데이터 없으면 중립

        # 20일 수익률 > 0 (5점)
        change_20d = ind.get("change_20d")
        if change_20d is not None and change_20d > 0:
            score += 5
        elif change_20d is not None and change_20d > -3:
            score += 2

        # 60일 수익률 > 0 (5점)
        change_60d = ind.get("change_60d")
        if change_60d is not None and change_60d > 0:
            score += 5
        elif change_60d is not None and change_60d > -5:
            score += 2

        # MA5 > MA20 (5점)
        if ind.get("ma5_above_ma20"):
            score += 5

        return score

    @staticmethod
    def _is_etf_etn(name: str) -> bool:
        """ETF/ETN 판별"""
        if not name:
            return False
        upper = name.upper()
        etf_brands = {"KODEX", "TIGER", "KOSEF", "ARIRANG", "KBSTAR", "HANARO",
                       "SOL", "ACE", "PLUS", "RISE", "BNK", "TIMEFOLIO", "WOORI"}
        etf_keywords = {"ETF", "ETN", "레버리지", "인버스", "선물", "채권"}
        for brand in etf_brands:
            if upper.startswith(brand):
                return True
        for kw in etf_keywords:
            if kw.upper() in upper:
                return True
        return False
