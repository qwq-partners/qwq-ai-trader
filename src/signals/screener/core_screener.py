"""
QWQ AI Trader - 코어홀딩 종목 스크리너

대형 우량주 중심 중장기 추세 캐처 (3~6개월 +30~50% 노림).
시총 5000억+, MA200 존재, 모멘텀 가속 종목 스코어링.

2026-05-11 A안 재정의: "장기 추세 캐처"
  - 박스권 대형주(통신/저변동) 자동 배제
  - 모멘텀 가중치 ↑, 펀더/추세 가중치 ↓
  - RS 등급(MRS) 신규 추가

스코어링 (100점 만점):
    추세 안정성  20점 (이전 30, ↓)
    펀더멘탈    20점 (이전 30, ↓)
    수급 추세   20점
    모멘텀 품질 30점 (이전 20, ↑ + 60일+10% 신고가 가중)
    RS 등급     10점 (신규)

진입 필터 (_apply_base_filter):
    - MA200 위 (기존)
    - 60일 수익률 ≥ +5% (신규, 박스권 배제)
    - 신고가 80% 이내 (신규, from_52w_high ≥ -20%)
    - PER > 0, 거래대금 충족 (기존)
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from loguru import logger



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

        # 4.5단계: 총자산 증가율 보강 (Asset Growth 퀄리티 감점용, 2026-08-03)
        await self._enrich_asset_growth(filtered)

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

        # 대시보드 종목명 조회용 캐시 (data_collector._build_name_cache step7에서 사용)
        self._last_candidates = scored

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
            logger.error("[코어스크리너] 유니버스 구축 실패: 종목 목록 없음 (StockMaster 장애 가능)")
            return []

        # ETF/ETN 제외
        filtered_symbols = {
            code: name for code, name in symbols_with_names.items()
            if not self._is_etf_etn(name)
        }

        # 시총 필터: StockMaster DB에서 시총(억원) 조회
        symbol_list = list(filtered_symbols.keys())
        market_caps = await self._fetch_market_caps(symbol_list)
        if market_caps:
            min_cap_eok = int(self._min_market_cap_b * 10000)  # 조→억 변환
            before = len(symbol_list)
            symbol_list = [s for s in symbol_list if market_caps.get(s, 0) >= min_cap_eok]
            filtered_symbols = {s: filtered_symbols[s] for s in symbol_list}
            logger.info(f"[코어스크리너] 시총 필터: {before}개→{len(symbol_list)}개 (>= {min_cap_eok}억원)")

        # KIS API로 밸류에이션 데이터 조회
        valuations: Dict[str, Dict] = {}

        if self._kis_market_data:
            try:
                # 30건씩 배치 처리 (API 제한 대응)
                batch_size = 30
                for bi in range(0, len(symbol_list), batch_size):
                    batch = symbol_list[bi:bi + batch_size]
                    batch_vals = await self._kis_market_data.fetch_batch_valuations(batch)
                    valuations.update(batch_vals)
                    if bi + batch_size < len(symbol_list):
                        await asyncio.sleep(0.5)
                logger.info(f"[코어스크리너] 밸류에이션 조회 완료: {len(valuations)}개")
            except Exception as e:
                logger.warning(f"[코어스크리너] 밸류에이션 조회 실패: {e}")

        # 시총 기준 정렬 → rank 부여 (실제 시총 순위)
        sorted_symbols = sorted(
            symbol_list,
            key=lambda s: market_caps.get(s, 0),
            reverse=True,
        )
        rank_map = {sym: idx for idx, sym in enumerate(sorted_symbols)}

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

            _per = val.get("per")
            per = float(_per) if _per is not None else 0.0
            _pbr = val.get("pbr")
            pbr = float(_pbr) if _pbr is not None else 0.0
            _eps = val.get("eps")
            eps = float(_eps) if _eps is not None else 0.0
            _bps = val.get("bps")
            bps = float(_bps) if _bps is not None else 0.0

            universe.append({
                "symbol": code,
                "name": name or val.get("name", ""),
                "price": price,
                "per": per,
                "pbr": pbr,
                "eps": eps,
                "bps": bps,
                "rank": rank_map.get(code, 999),  # 실제 시총 기준 순위
                "market_cap_eok": market_caps.get(code, 0),
            })

        logger.info(f"[코어스크리너] 최종 유니버스: {len(universe)}개 (가격필터 후)")
        return universe

    async def _fetch_market_caps(self, symbols: List[str]) -> Dict[str, int]:
        """StockMaster DB에서 시총(억원) 조회"""
        if not self._stock_master:
            return {}
        pool = getattr(self._stock_master, 'pool', None)
        if pool is None:
            return {}
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT ticker, market_cap FROM kr_stock_master WHERE ticker = ANY($1::text[])",
                    symbols,
                )
                return {row['ticker']: int(row['market_cap'] or 0) for row in rows}
        except Exception as e:
            logger.warning(f"[코어스크리너] 시총 DB 조회 실패: {e}")
            return {}

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

            # 일별 수익률 변동성 (60일) — 저변동성 팩터 감점용 (2026-08-03)
            # volatility_20d는 가격 수준의 분산이라 추세 종목에서 과대평가된다.
            # 급락형 종목 배제에는 일수익률 σ가 맞다.
            if len(closes) >= 61:
                rets = [
                    (closes[i] - closes[i - 1]) / closes[i - 1] * 100
                    for i in range(len(closes) - 60, len(closes))
                    if closes[i - 1] > 0
                ]
                if len(rets) >= 30:
                    mean_r = sum(rets) / len(rets)
                    var_r = sum((r - mean_r) ** 2 for r in rets) / len(rets)
                    ind["ret_vol_60d"] = var_r ** 0.5

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

            # MA200 연속 하회 일수 (코어홀딩 교체 판단용)
            # 각 날짜별 rolling MA200을 계산하여 비교
            ma200_below_days = 0
            if ma200 is not None and ma200 > 0 and len(closes) >= 201:
                for ci in range(len(closes) - 1, max(len(closes) - 31, 199), -1):
                    # ci 시점의 MA200 = closes[ci-199:ci+1]의 평균
                    rolling_ma200 = sum(closes[ci - 199:ci + 1]) / 200
                    if closes[ci] < rolling_ma200:
                        ma200_below_days += 1
                    else:
                        break
            ind["ma200_below_days"] = ma200_below_days

            # 펀더멘탈 (유니버스에서 전달받은 값)
            ind["per"] = item.get("per", 0)
            ind["pbr"] = item.get("pbr", 0)
            ind["eps"] = item.get("eps", 0)
            ind["bps"] = item.get("bps", 0)
            ind["rank"] = item.get("rank", 999)
            ind["market_cap_eok"] = item.get("market_cap_eok", 0)

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

        try:
            # 배치 수급 조회 (5일간 합산, 10건씩 병렬)
            investor_data: Dict[str, Dict] = {}
            batch_size = 10
            for bi in range(0, len(symbols), batch_size):
                batch = symbols[bi:bi + batch_size]
                tasks = [self._kis_market_data.fetch_stock_investor_daily(sym, days=5) for sym in batch]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for sym, daily in zip(batch, results):
                    if isinstance(daily, Exception) or not daily:
                        continue
                    total_foreign = sum(d.get("foreign_net_buy", 0) for d in daily.values())
                    total_inst = sum(d.get("inst_net_buy", 0) for d in daily.values())
                    investor_data[sym] = {
                        "foreign_net_buy_5d": total_foreign,
                        "inst_net_buy_5d": total_inst,
                    }
                if bi + batch_size < len(symbols):
                    await asyncio.sleep(0.5)

            # 후보에 수급 데이터 병합
            for c in candidates:
                sd = investor_data.get(c.symbol, {})
                c.indicators["foreign_net_buy_5d"] = sd.get("foreign_net_buy_5d")
                c.indicators["inst_net_buy_5d"] = sd.get("inst_net_buy_5d")

            logger.info(f"[코어스크리너] 수급 데이터 보강 완료: {len(investor_data)}/{len(candidates)}개")

        except Exception as e:
            logger.warning(f"[코어스크리너] 수급 데이터 조회 실패: {e}")

    # 자산증가율 보강 가드 — DART 장애 시 스캔 전체가 늘어지는 것 방지
    _AG_TIME_BUDGET_SEC = 90.0    # 첫 스캔(캐시 없음)도 이 안에 끝나야 한다
    _AG_MAX_CONSEC_FAIL = 5

    async def _enrich_asset_growth(self, candidates: List[CoreCandidate]) -> None:
        """DART 총자산 증가율 보강 — 실패해도 스캔은 계속 (감점만 생략됨)"""
        try:
            from ..fundamentals.asset_growth import get_asset_growth_provider
            provider = get_asset_growth_provider()
        except Exception as e:
            logger.debug(f"[코어스크리너] 자산증가율 프로바이더 로드 실패 (생략): {e}")
            return

        import time as _time
        started = _time.monotonic()
        enriched = 0
        consec_fail = 0
        for i, c in enumerate(candidates):
            if _time.monotonic() - started > self._AG_TIME_BUDGET_SEC:
                logger.warning(
                    f"[코어스크리너] 자산증가율 시간 예산 초과 — "
                    f"{i}/{len(candidates)}개에서 중단 (나머지는 감점 생략)"
                )
                break
            try:
                growth = await provider.get_asset_growth(c.symbol)
                consec_fail = 0
            except Exception as e:
                logger.debug(f"[코어스크리너] {c.symbol} 자산증가율 조회 실패: {e}")
                consec_fail += 1
                if consec_fail >= self._AG_MAX_CONSEC_FAIL:
                    logger.warning(
                        f"[코어스크리너] 자산증가율 연속 {consec_fail}회 실패 — 중단"
                    )
                    break
                continue
            if growth is not None:
                c.indicators["asset_growth_pct"] = growth
                enriched += 1
        if candidates:
            logger.info(
                f"[코어스크리너] 자산증가율 보강: {enriched}/{len(candidates)}개 "
                f"({_time.monotonic() - started:.1f}s)"
            )

    def _apply_base_filter(self, candidates: List[CoreCandidate]) -> List[CoreCandidate]:
        """기본 필터 + 진입 모멘텀 필터 (2026-05-11 A안)

        기존: MA200 존재, MA200 위, PER>0, 거래대금
        신규: 60일 수익률 ≥ +5%, 신고가 80% 이내 — 박스권 대형주 자동 배제
        """
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

            # PER > 0 (적자 기업 제외: PER=0은 적자 또는 미제공)
            per = ind.get("per")
            if per is not None and per <= 0:
                continue

            # 거래대금 필터
            avg_tv = ind.get("avg_trading_value", 0)
            if avg_tv is not None and avg_tv < self._min_avg_trading_value:
                continue

            # ── 2026-05-11 A안: 진입 모멘텀 필터 ─────────────────
            # 박스권 대형주(통신/식음료/저변동) 자동 배제
            change_60d = ind.get("change_60d")
            if change_60d is None or change_60d < 5.0:
                continue  # 60일 +5% 미달 → 박스권

            high_52w = ind.get("high_52w", 0)
            if high_52w > 0 and close > 0:
                from_high_pct = (close - high_52w) / high_52w * 100
                if from_high_pct < -20:
                    continue  # 신고가 80% 미달 → 약세 추세

            filtered.append(c)
        return filtered

    def _score_candidates(self, candidates: List[CoreCandidate]) -> List[CoreCandidate]:
        """100점 만점 스코어링 (2026-05-11 A안: 모멘텀 가중치 ↑)"""
        for c in candidates:
            score = 0.0
            reasons = []
            ind = c.indicators

            # ── 추세 안정성 (20점, 이전 30) ──
            trend_score = self._score_trend(ind, reasons)
            score += trend_score

            # ── 펀더멘탈 (20점, 이전 30) ──
            fund_score = self._score_fundamentals(ind, reasons)
            score += fund_score

            # ── 수급 추세 (20점) ──
            supply_score = self._score_supply(ind, reasons)
            score += supply_score

            # ── 모멘텀 품질 (30점, 이전 20) ──
            momentum_score = self._score_momentum(ind, reasons)
            score += momentum_score

            # ── RS 등급 (10점, 신규) ──
            rs_score = self._score_rs_rating(ind, reasons)
            score += rs_score

            # ── 저변동성 감점 (0 ~ -10, 2026-08-03) ──
            # Low Volatility Factor (Sharpe 0.717) 응용 — 가점 없이 감점만 둔다.
            # 코어홀딩은 장기 보유라 급등락형이 들어오면 stale/손절 사고로 이어진다
            # (2026-06-04 -263k 사례). 기존 min_score 보정을 흔들지 않도록 감점 전용.
            vol_penalty = self._score_low_vol_penalty(ind, reasons)
            score += vol_penalty

            # ── 자산 확장 감점 (0 ~ -5, 2026-08-03) ──
            # Asset Growth Effect (Sharpe 0.835) 응용 — 총자산 급증 기업 감점.
            ag_penalty = self._score_asset_growth_penalty(ind, reasons)
            score += ag_penalty

            c.score = max(0.0, min(score, 100.0))
            c.reasons = reasons

        return candidates

    @staticmethod
    def _score_asset_growth_penalty(ind: Dict, reasons: List[str]) -> float:
        """총자산 증가율(전년 대비) 기반 감점.

        논문: 자산 증가율 하위 기업이 상위 기업을 장기 아웃퍼폼.
        증자·차입·인수로 몸집을 급격히 불린 기업의 후속 수익률이 나쁘다.
        데이터 없으면 감점하지 않는다 (DART 미커버 종목 등).
        """
        growth = ind.get("asset_growth_pct")
        if growth is None:
            return 0.0
        if growth >= 50.0:
            penalty = -5.0
        elif growth >= 30.0:
            penalty = -3.0
        else:
            return 0.0
        reasons.append(f"자산급증 +{growth:.0f}%({penalty:+.0f})")
        return penalty

    @staticmethod
    def _score_low_vol_penalty(ind: Dict, reasons: List[str]) -> float:
        """일별 수익률 변동성(60일 σ) 기반 감점.

        KR 대형주 일수익률 σ는 보통 1.5~2.5%. 3% 이상은 테마성 급등락 구간이다.
        데이터 없으면 감점하지 않는다 (신규 상장 등 — 다른 필터가 거른다).
        """
        vol = ind.get("ret_vol_60d")
        if vol is None:
            return 0.0
        if vol >= 4.0:
            penalty = -10.0
        elif vol >= 3.0:
            penalty = -6.0
        elif vol >= 2.5:
            penalty = -3.0
        else:
            return 0.0
        reasons.append(f"고변동성 σ{vol:.1f}%({penalty:+.0f})")
        return penalty

    def _score_trend(self, ind: Dict, reasons: List[str]) -> float:
        """추세 안정성 (20점, 2026-05-11 A안 축소)

        축소: 저변동성/52주고점 보너스 제거 → 모멘텀으로 이동
        """
        score = 0.0

        # MA 정배열 (7점)
        if ind.get("ma_aligned"):
            score += 7
            reasons.append("MA정배열")

        # MA200 위 (3점)
        close = ind.get("close", 0)
        ma200 = ind.get("ma200", 0)
        if close > 0 and ma200 > 0 and close > ma200:
            score += 3
            reasons.append("MA200↑")

        # 6개월 수익률 > 0 (5점)
        change_6m = ind.get("change_6m")
        if change_6m is not None and change_6m > 0:
            score += 5
            reasons.append(f"6M+{change_6m:.1f}%")

        # 변동성 적정 (5점): 너무 낮으면 박스권 신호 → 3~6%가 추세 캐치 sweet spot
        vol20 = ind.get("volatility_20d", 999)
        if vol20 is not None and 3.0 <= vol20 <= 6.0:
            score += 5
        elif vol20 is not None and vol20 < 8.0:
            score += 2

        return min(score, 20.0)

    def _score_fundamentals(self, ind: Dict, reasons: List[str]) -> float:
        """펀더멘탈 (20점, 2026-05-11 A안 축소)

        축소 배경: 코어 역할 재정의 — 펀더보다 모멘텀 우선.
        펀더는 "최소 적자 회피" 수준으로 단순화.
        """
        score = 0.0

        # PER 적정 (4점)
        per = ind.get("per")
        if per is not None and per > 0:
            if 5 <= per <= 20:
                score += 4
                reasons.append(f"PER{per:.0f}")
            elif per <= 35:
                score += 2

        # PBR (4점)
        pbr = ind.get("pbr")
        if pbr is not None and 0 < pbr < 1.5:
            score += 4
            reasons.append(f"PBR{pbr:.1f}")
        elif pbr is not None and 0 < pbr < 3:
            score += 2

        # EPS > 0 이익 기업 (4점)
        eps = ind.get("eps")
        if eps is not None and eps > 0:
            score += 4

        # ROE 추정 (4점)
        bps = ind.get("bps")
        if eps is not None and bps is not None and bps > 0:
            roe_est = eps / bps * 100
            if roe_est >= 15:
                score += 4
                reasons.append(f"ROE~{roe_est:.0f}%")
            elif roe_est >= 10:
                score += 2

        # 시총 순위 (4점)
        rank = ind.get("rank", 999)
        if rank <= 20:
            score += 4
        elif rank <= 50:
            score += 2

        return min(score, 20.0)

    def _score_supply(self, ind: Dict, reasons: List[str]) -> float:
        """수급 추세 (20점) — 금액 기반 구간별 배점"""
        score = 0.0
        close = ind.get("close", 0)

        # 외인 순매수 5일 (10점) — 주수×현재가로 대략적 금액 환산
        foreign_net = ind.get("foreign_net_buy_5d")
        if foreign_net is not None and close > 0:
            foreign_amt = foreign_net * close  # 대략적 금액(원)
            if foreign_net > 0:
                if foreign_amt >= 50_000_000_000:  # 500억+
                    score += 10
                    reasons.append(f"외인매수{foreign_amt/1e8:,.0f}억")
                elif foreign_amt >= 10_000_000_000:  # 100억+
                    score += 8
                    reasons.append("외인매수")
                elif foreign_amt >= 3_000_000_000:  # 30억+
                    score += 6
                else:
                    score += 4
            else:
                # 순매도: 규모에 따라 차등 감점
                abs_amt = abs(foreign_amt)
                if abs_amt >= 50_000_000_000:
                    score += 0  # 대규모 매도
                elif abs_amt >= 10_000_000_000:
                    score += 1
                else:
                    score += 2  # 소규모 매도
        elif foreign_net is not None:
            # close=0 (가격 미조회): 주수 기반 fallback
            score += 3 if foreign_net > 0 else 1
        else:
            score += 2  # 데이터 없음 = 순매도 소규모와 동일 (역설 해소)

        # 기관 순매수 5일 (10점)
        inst_net = ind.get("inst_net_buy_5d")
        if inst_net is not None and close > 0:
            inst_amt = inst_net * close
            if inst_net > 0:
                if inst_amt >= 50_000_000_000:
                    score += 10
                    reasons.append(f"기관매수{inst_amt/1e8:,.0f}억")
                elif inst_amt >= 10_000_000_000:
                    score += 8
                    reasons.append("기관매수")
                elif inst_amt >= 3_000_000_000:
                    score += 6
                else:
                    score += 4
            else:
                abs_amt = abs(inst_amt)
                if abs_amt >= 50_000_000_000:
                    score += 0
                elif abs_amt >= 10_000_000_000:
                    score += 1
                else:
                    score += 2
        elif inst_net is not None:
            score += 3 if inst_net > 0 else 1
        else:
            score += 2  # 데이터 없음

        return min(score, 20.0)

    def _score_momentum(self, ind: Dict, reasons: List[str]) -> float:
        """모멘텀 품질 (30점, 2026-05-11 A안 강화)

        강화: 60일 모멘텀 단계별 가중, 신고가 근접, MA5>MA20 + 가속 추가.
        장기 추세 캐치 핵심 지표.
        """
        score = 0.0

        # 20일 수익률 (5점)
        change_20d = ind.get("change_20d")
        if change_20d is not None:
            if change_20d >= 10:
                score += 5
                reasons.append(f"20D+{change_20d:.0f}%")
            elif change_20d > 0:
                score += 3

        # 60일 수익률 — 추세 캐처 핵심 (10점, 단계별)
        change_60d = ind.get("change_60d")
        if change_60d is not None:
            if change_60d >= 20:
                score += 10
                reasons.append(f"60D+{change_60d:.0f}%★")
            elif change_60d >= 10:
                score += 7
                reasons.append(f"60D+{change_60d:.0f}%")
            elif change_60d > 5:
                score += 4

        # MA5 > MA20 (5점)
        if ind.get("ma5_above_ma20"):
            score += 5

        # 신고가 근접 (5점) — 52주 고점 95% 이상
        close = ind.get("close", 0)
        high_52w = ind.get("high_52w", 0)
        if close > 0 and high_52w > 0:
            from_high_pct = (close - high_52w) / high_52w * 100
            if from_high_pct >= -5:
                score += 5
                reasons.append("신고가권")
            elif from_high_pct >= -10:
                score += 3

        # 모멘텀 가속 (5점) — 20일 모멘텀 > 60일 평균 모멘텀의 1/3
        if (change_20d is not None and change_60d is not None
                and change_60d > 0 and change_20d > change_60d / 3 * 1.5):
            score += 5
            reasons.append("모멘텀 가속")

        return min(score, 30.0)

    def _score_rs_rating(self, ind: Dict, reasons: List[str]) -> float:
        """RS 등급 (10점, 2026-05-11 신규)

        MRS(시장상대강도) 또는 rs_rating을 등급화.
        IBD RS Rating 컨셉: 시장 대비 상대 수익률 백분위.
        """
        score = 0.0
        rs = ind.get("rs_rating")
        if rs is None:
            rs = ind.get("mrs")
        if rs is None:
            return 0.0  # 데이터 없으면 0점 (변별력 보존)

        if rs >= 80:
            score = 10
            reasons.append(f"RS{rs:.0f}★")
        elif rs >= 60:
            score = 7
            reasons.append(f"RS{rs:.0f}")
        elif rs >= 40:
            score = 4
        elif rs >= 20:
            score = 1

        return min(score, 10.0)

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
