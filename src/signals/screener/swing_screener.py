"""
AI Trading Bot v2 - 스윙 모멘텀 스크리너

장 마감 후 배치 스캔: 유니버스 선정 → FDR 일봉 → 기술적 지표 → 전략별 필터 → 복합 점수.
기존 stock_screener.py(단기 급등 필터)와 독립.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set

from loguru import logger

from src.indicators.technical import TechnicalIndicators


@dataclass
class SwingCandidate:
    """스윙 매매 후보 종목"""
    symbol: str
    name: str
    strategy: str  # "rsi2_reversal" | "sepa_trend"
    score: float  # 0-100
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    indicators: Dict[str, Any] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)


class SwingScreener:
    """스윙 모멘텀 종목 스크리너"""

    def __init__(self, broker, kis_market_data, stock_master=None, config: Optional[Dict] = None):
        self._broker = broker
        self._kis_market_data = kis_market_data
        self._stock_master = stock_master
        self._indicators = TechnicalIndicators()
        self._kospi_closes: List[float] = []  # 벤치마크 KOSPI 종가 (MRS용)
        # 5일 수급 스코어 (싱글턴 — 스캔 사이클마다 재생성하지 않도록 인스턴스 변수)
        from src.data.providers.supply_score import SupplyScoreProvider
        self._supply5d = SupplyScoreProvider()

        # ── VCP 독립 발굴 라인 설정 (2026-08-03) ──
        _cfg = config or {}
        _vcp_cfg = _cfg.get("vcp_breakout", {}) if isinstance(_cfg, dict) else {}
        self._vcp_line_enabled = bool(_vcp_cfg.get("enabled", True))
        self._vcp_min_score = float(_vcp_cfg.get("min_score", 60.0))
        self._vcp_breakout_buffer_pct = float(_vcp_cfg.get("breakout_buffer_pct", 0.5))

        # ── 선행 유니버스 통로 설정 (2026-08-03) ──
        _uni_cfg = _cfg.get("universe_leading", {}) if isinstance(_cfg, dict) else {}
        self._leading_enabled = bool(_uni_cfg.get("enabled", True))
        self._leading_limit = int(_uni_cfg.get("limit_per_channel", 60))
        # 시총 상위 조회 한도 — 기존 200. 200위 밖 종목은 급등/수급 순위에 들어야만
        # 유니버스에 진입해서 구조적으로 "오르기 전"에 잡을 수 없었다.
        self._top_cap_limit = int(_uni_cfg.get("top_cap_limit", 350))

    async def run_full_scan(self) -> List[SwingCandidate]:
        """
        전체 스캔: 유니버스 → 지표 → 필터 → 점수

        Returns:
            점수 순 정렬된 SwingCandidate 리스트
        """
        logger.info("[스윙스크리너] 전체 스캔 시작...")

        # 0단계: 벤치마크 지수(KOSPI) 로드 (MRS 계산용)
        await self._load_benchmark_index()

        # 1단계: 유니버스 선정
        universe = await self._build_universe()
        logger.info(f"[스윙스크리너] 유니버스: {len(universe)}개 종목")

        if not universe:
            logger.warning("[스윙스크리너] 유니버스 비어있음")
            return []

        # 2단계: FDR 일봉 + 기술적 지표 계산
        candidates_data = await self._calculate_all_indicators(universe)
        logger.info(f"[스윙스크리너] 지표 계산 완료: {len(candidates_data)}개 종목")

        # 3단계: 전략별 필터
        rsi2_candidates = self._filter_rsi2_reversal(candidates_data)
        sepa_candidates = self._filter_sepa_trend(candidates_data)
        logger.info(
            f"[스윙스크리너] 필터 결과: RSI2={len(rsi2_candidates)}개, SEPA={len(sepa_candidates)}개"
        )

        # 3.5단계: VCP 변동성수축 패턴 탐지 (FDR 데이터 재사용 → 캐시 저장)
        # 2026-08-03: 오버레이 가점 전용이었던 VCP를 독립 발굴 라인으로 승격.
        # 복합 점수 계산 전에 실행해야 VCP 후보도 수급/재무 점수를 받는다.
        vcp_results: List[Any] = []
        vcp_candidates: List[SwingCandidate] = []
        try:
            from src.signals.strategic.vcp_detector import VCPDetector
            vcp_detector = VCPDetector()
            # 300종목 이상 numpy 연산 → 동기 호출 시 이벤트 루프가 그만큼 멈춘다.
            # (유니버스를 200→350으로 늘리면서 블로킹 시간도 함께 늘었다)
            vcp_results = await asyncio.to_thread(
                vcp_detector.detect_all, candidates_data
            )
            logger.info(f"[스윙스크리너] VCP 탐지: {len(vcp_results)}종목")

            if self._vcp_line_enabled:
                _existing = {c.symbol for c in (rsi2_candidates + sepa_candidates)}
                vcp_candidates = self._filter_vcp_breakout(
                    candidates_data,
                    vcp_results,
                    exclude=_existing,
                    min_score=self._vcp_min_score,
                    breakout_buffer_pct=self._vcp_breakout_buffer_pct,
                )
                if vcp_candidates:
                    logger.info(
                        f"[스윙스크리너] VCP 독립 후보: {len(vcp_candidates)}종목 "
                        f"(RSI2/SEPA 미포착분)"
                    )
        except Exception as e:
            logger.warning(f"[스윙스크리너] VCP 탐지 실패 (무시): {e}")

        # 4단계: 수급/재무 점수 + LCI z-score 계산
        # dedupe: 동일 종목이 RSI2(역추세) + SEPA(추세) 양쪽 통과 시 score 높은 전략 하나만 유지
        # — 같은 종목에 두 전략 동시 진입은 사실상 단일 사건의 이중 노출(allocation 50%+)
        _merged: Dict[str, SwingCandidate] = {}
        for _c in rsi2_candidates + sepa_candidates + vcp_candidates:
            _prev = _merged.get(_c.symbol)
            if _prev is None or _c.score > _prev.score:
                _merged[_c.symbol] = _c
        _dup_count = (
            len(rsi2_candidates) + len(sepa_candidates) + len(vcp_candidates)
        ) - len(_merged)
        if _dup_count > 0:
            logger.info(f"[스윙스크리너] 중복 제거: {_dup_count}건 (동일 종목 복수 전략 동시 통과)")
        all_candidates = list(_merged.values())
        scored = await self._apply_composite_score(all_candidates)
        self._compute_lci_zscore(scored)  # 수급 데이터 주입 후 LCI 계산

        # 5단계: 전략적 오버레이 (3계층 전략적 신호)
        scored = await self._apply_strategic_overlay(scored)

        # 점수 순 정렬
        scored.sort(key=lambda c: c.score, reverse=True)

        logger.info(f"[스윙스크리너] 최종 후보: {len(scored)}개 종목")
        for c in scored[:5]:
            logger.info(
                f"  {c.symbol} {c.name}: 점수={c.score:.0f} 전략={c.strategy} "
                f"진입={c.entry_price:,.0f} 손절={c.stop_price:,.0f}"
            )

        return scored

    async def _build_universe(self) -> List[Dict[str, str]]:
        """
        1단계: 유니버스 선정

        후행 통로 (이미 움직인 종목):
        - 등락률 상위 / 외국인 순매수 / 기관 순매수 (각 50)

        선행 통로 (아직 안 움직인 종목):
        - 시총 상위 N개 (기본 350 — 2026-08-03 200에서 확대)
        - 수급 누적 (SupplyTrendDetector: 연속 순매수 20일 분석)
        - 직전 스캔 VCP 수축 종목 (순위 API에 안 잡혀 유니버스에서 탈락하는 것 방지)

        필터: 거래대금 30일 평균 10억+, ETF 제외, 가격 2000원+
        """
        universe = {}  # symbol → {"symbol", "name"}

        # KOSPI200 + KOSDAQ150 (StockMaster) — 200종목으로 확대
        if self._stock_master:
            try:
                top_stocks = await self._stock_master.get_top_stocks(limit=self._top_cap_limit)
                for entry in top_stocks:
                    # get_top_stocks() 반환값: "종목명=코드" 형식
                    parts = entry.split("=")
                    if len(parts) == 2:
                        name, symbol = parts[0], parts[1]
                    else:
                        symbol = entry
                        name = await self._stock_master.get_name(symbol) or symbol
                    # ETF/ETN/파생상품 제외 (이름 기반)
                    if self._should_exclude(name):
                        logger.debug(f"[스크리닝] ETF/ETN 제외: {name}({symbol})")
                        continue
                    universe[symbol] = {"symbol": symbol, "name": name}
            except Exception as e:
                logger.warning(f"[스윙스크리너] StockMaster 조회 실패: {e}")

        # 등락률 순위
        if self._kis_market_data:
            try:
                ranked = await self._kis_market_data.fetch_fluctuation_rank(limit=50)
                for item in ranked:
                    symbol = item.get("symbol", item.get("stck_shrn_iscd", ""))
                    if not symbol:
                        continue
                    name = item.get("name", item.get("hts_kor_isnm", symbol))
                    price = float(item.get("price", item.get("stck_prpr", 0)))
                    # ETF/ETN/파생상품 제외
                    if self._should_exclude(name):
                        logger.debug(f"[스크리닝] ETF/ETN 제외: {name}({symbol})")
                        continue
                    if price < 2000:
                        continue
                    universe[symbol] = {"symbol": symbol, "name": name}
            except Exception as e:
                logger.warning(f"[스윙스크리너] 등락률 순위 조회 실패: {e}")

            # 외국인 순매수 (코스피 + 코스닥)
            try:
                foreign_kospi = await self._kis_market_data.fetch_foreign_institution(market="0001", investor="1")
                foreign_kosdaq = await self._kis_market_data.fetch_foreign_institution(market="0002", investor="1")
                for item in (foreign_kospi + foreign_kosdaq)[:50]:
                    symbol = item.get("symbol", item.get("stck_shrn_iscd", ""))
                    name = item.get("name", item.get("hts_kor_isnm", symbol))
                    if symbol and not self._should_exclude(name):
                        universe[symbol] = {"symbol": symbol, "name": name}
                    elif symbol and self._should_exclude(name):
                        logger.debug(f"[스크리닝] ETF/ETN 제외: {name}({symbol})")
            except Exception as e:
                logger.debug(f"[스윙스크리너] 외국인 순매수 조회 실패: {e}")

            # 기관 순매수 (코스피 + 코스닥)
            try:
                inst_kospi = await self._kis_market_data.fetch_foreign_institution(market="0001", investor="2")
                inst_kosdaq = await self._kis_market_data.fetch_foreign_institution(market="0002", investor="2")
                for item in (inst_kospi + inst_kosdaq)[:50]:
                    symbol = item.get("symbol", item.get("stck_shrn_iscd", ""))
                    name = item.get("name", item.get("hts_kor_isnm", symbol))
                    if symbol and not self._should_exclude(name):
                        universe[symbol] = {"symbol": symbol, "name": name}
                    elif symbol and self._should_exclude(name):
                        logger.debug(f"[스크리닝] ETF/ETN 제외: {name}({symbol})")
            except Exception as e:
                logger.debug(f"[스윙스크리너] 기관 순매수 조회 실패: {e}")

        # ── 선행 통로 (2026-08-03) ──────────────────────────────────────────
        # 위 4개 통로는 전부 "시총 상위" 아니면 "이미 오른/이미 수급 들어온" 종목이다.
        # 시총 200위 밖 종목은 급등하거나 수급 순위에 들어야만 유니버스에 진입하므로
        # 구조적으로 "오르기 전"에 잡을 수 없었다. 아직 안 움직인 종목만 걸리는
        # 통로를 추가한다 (SupplyTrendDetector 캐시 재사용 — 추가 API 호출 없음).
        if self._leading_enabled:
            before_leading = len(universe)
            added_by_channel = await self._add_leading_universe(universe)
            if len(universe) > before_leading:
                logger.info(
                    f"[스윙스크리너] 선행 통로 +{len(universe) - before_leading}종목 "
                    f"({added_by_channel})"
                )

        return list(universe.values())

    async def _add_leading_universe(self, universe: Dict[str, Dict[str, str]]) -> str:
        """선행 통로 3개로 유니버스 보강 (2026-08-03)

        1) 수급 누적: 외국인/기관 연속 순매수 N일 — 단일일 순위가 아닌 '누적'
        2) 거래량 수축: 최근 거래량이 20일 평균 대비 낮음 = 매물 소화 진행
        3) 신고가 근접 미돌파: 52주 고점 3~10% 이내 횡보 = 돌파 대기

        (2)(3)은 FDR 조회가 필요해 여기서는 이전 스캔에서 저장한 VCP 캐시를
        재사용한다. VCP 캐시 자체가 "수축 + MA정배열 + 고점근접" 종목 목록이라
        같은 성격의 선행 후보이며, 추가 네트워크 비용이 0이다.

        Returns:
            채널별 추가 건수 요약 문자열
        """
        counts = {"수급누적": 0, "VCP캐시": 0}
        limit = self._leading_limit

        # 1) 수급 누적 (SupplyTrendDetector 캐시)
        try:
            for st in self._load_supply_trends()[:limit]:
                sym = getattr(st, "symbol", "")
                name = getattr(st, "name", "") or sym
                if not sym or sym in universe or self._should_exclude(name):
                    continue
                universe[sym] = {"symbol": sym, "name": name}
                counts["수급누적"] += 1
        except Exception as e:
            logger.debug(f"[스윙스크리너] 선행 통로(수급누적) 스킵: {e}")

        # 2)+3) 수축/고점근접 (직전 스캔의 VCP 캐시)
        try:
            for vcp in self._load_vcp_candidates()[:limit]:
                sym = getattr(vcp, "symbol", "")
                name = getattr(vcp, "name", "") or sym
                if not sym or sym in universe or self._should_exclude(name):
                    continue
                universe[sym] = {"symbol": sym, "name": name}
                counts["VCP캐시"] += 1
        except Exception as e:
            logger.debug(f"[스윙스크리너] 선행 통로(VCP캐시) 스킵: {e}")

        return ", ".join(f"{k} {v}" for k, v in counts.items() if v)

    async def _calculate_all_indicators(
        self, universe: List[Dict[str, str]]
    ) -> List[Dict[str, Any]]:
        """
        2단계: FDR 일봉 1년 조회 + 기술적 지표 계산

        FDR(FinanceDataReader)로 1년치 일봉 조회 (KIS API 60일 제한 우회).
        Semaphore(10) + wait_for(10초) 로 병렬 조회.
        """
        start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        sem = asyncio.Semaphore(10)

        async def fetch_one(stock: Dict[str, str]) -> Optional[Dict[str, Any]]:
            symbol = stock["symbol"]
            name = stock["name"]
            loop = asyncio.get_running_loop()

            df = None
            for attempt in range(2):
                try:
                    async with sem:
                        df = await asyncio.wait_for(
                            loop.run_in_executor(None, self._fetch_fdr_data, symbol, start_date),
                            timeout=15.0,
                        )
                    break  # 성공 시 루프 탈출
                except asyncio.TimeoutError:
                    if attempt == 0:
                        logger.debug(f"[스윙스크리너] {symbol} FDR 조회 타임아웃(15초), 1회 재시도")
                        continue
                    logger.warning(f"[스윙스크리너] {symbol} FDR 조회 타임아웃(15초x2), 스킵")
                    return None
                except Exception as e:
                    logger.debug(f"[스윙스크리너] {symbol} FDR 조회 실패: {e}")
                    return None

            try:
                if df is None or len(df) < 50:
                    logger.debug(f"[스윙스크리너] {symbol} 데이터 부족 ({len(df) if df is not None else 0}일)")
                    return None

                # DataFrame → List[Dict]
                daily_data = []
                for _, row in df.iterrows():
                    daily_data.append({
                        "date": row.name.strftime("%Y%m%d") if hasattr(row.name, 'strftime') else str(row.name),
                        "open": float(row.get("Open", 0)),
                        "high": float(row.get("High", 0)),
                        "low": float(row.get("Low", 0)),
                        "close": float(row.get("Close", 0)),
                        "volume": int(row.get("Volume", 0)),
                    })

                # 거래대금 필터: 30일 평균 10억원 이상
                recent_30 = daily_data[-30:] if len(daily_data) >= 30 else daily_data
                avg_trade_value = sum(
                    d["close"] * d["volume"] for d in recent_30
                ) / len(recent_30)
                if avg_trade_value < 1_000_000_000:
                    logger.debug(
                        f"[스윙스크리너] {symbol} 거래대금 부족: "
                        f"{avg_trade_value/1e8:.0f}억 (<10억)"
                    )
                    return None

                # 기술적 지표 계산
                indicators = self._indicators.calculate_all(symbol, daily_data)
                if not indicators:
                    return None

                # MRS 계산 (벤치마크 데이터 있을 경우)
                if self._kospi_closes:
                    stock_closes = [float(d["close"]) for d in daily_data]
                    mrs_result = self._indicators.calculate_mrs(
                        stock_closes, self._kospi_closes, period=20
                    )
                    if mrs_result:
                        indicators["mrs"] = mrs_result["mrs"]
                        indicators["mrs_slope"] = mrs_result["mrs_slope"]

                return {
                    "symbol": symbol,
                    "name": name,
                    "indicators": indicators,
                    "daily_data": daily_data,
                }

            except Exception as e:
                logger.debug(f"[스윙스크리너] {symbol} 지표 계산 실패: {e}")
                return None

        raw_results = await asyncio.gather(
            *[fetch_one(s) for s in universe], return_exceptions=True
        )

        results = []
        for r in raw_results:
            if isinstance(r, dict):
                results.append(r)
            elif isinstance(r, Exception):
                logger.debug(f"[스윙스크리너] 병렬 조회 예외: {r}")

        return results

    @staticmethod
    def _fetch_fdr_data(symbol: str, start_date: str):
        """FDR 일봉 조회 (동기)"""
        try:
            import FinanceDataReader as fdr
            df = fdr.DataReader(symbol, start_date)
            return df
        except Exception as e:
            logger.debug(f"[FDR] {symbol} 조회 실패: {e}")
            return None

    def _filter_rsi2_reversal(
        self, candidates_data: List[Dict[str, Any]]
    ) -> List[SwingCandidate]:
        """3단계: RSI-2 역추세 필터"""
        results = []

        for data in candidates_data:
            ind = data["indicators"]

            # RSI-2 진입 조건 체크
            rsi2_pass, rsi_val, reason = self._indicators.check_rsi2_entry(ind)
            if not rsi2_pass:
                continue

            close = Decimal(str(ind.get("close", 0)))
            if close <= 0:
                continue

            # 손절: -5%
            stop_price = close * Decimal("0.95")
            # 목표: RSI(2) > 70 도달 시 (보통 +3~8%)
            target_price = close * Decimal("1.05")

            candidate = SwingCandidate(
                symbol=data["symbol"],
                name=data["name"],
                strategy="rsi2_reversal",
                score=0,  # 4단계에서 계산
                entry_price=close,
                stop_price=stop_price,
                target_price=target_price,
                indicators=ind,
                reasons=[reason],
            )
            results.append(candidate)

        return results

    def _filter_sepa_trend(
        self, candidates_data: List[Dict[str, Any]]
    ) -> List[SwingCandidate]:
        """3단계: SEPA 트렌드 필터"""
        results = []

        for data in candidates_data:
            ind = data["indicators"]

            # SEPA 조건 체크
            sepa_pass = ind.get("sepa_pass", False)
            sepa_reasons = ind.get("sepa_reasons", [])
            if not sepa_pass:
                continue

            # MA5 > MA20: 필수 아닌 보너스 (단기 눌림목에서도 진입 기회 확보)
            # → sepa_trend.py 점수 계산에서 가점으로 반영

            close = Decimal(str(ind.get("close", 0)))
            if close <= 0:
                continue

            # 손절: -5%
            stop_price = close * Decimal("0.95")
            # 목표: +10%
            target_price = close * Decimal("1.10")

            candidate = SwingCandidate(
                symbol=data["symbol"],
                name=data["name"],
                strategy="sepa_trend",
                score=0,
                entry_price=close,
                stop_price=stop_price,
                target_price=target_price,
                indicators=ind,
                reasons=sepa_reasons,
            )
            results.append(candidate)

        return results

    def _filter_vcp_breakout(
        self,
        candidates_data: List[Dict[str, Any]],
        vcp_results: List[Any],
        exclude: Set[str],
        min_score: float = 60.0,
        breakout_buffer_pct: float = 0.5,
    ) -> List[SwingCandidate]:
        """VCP 독립 발굴 라인 (2026-08-03)

        기존 구조에서 VCP는 RSI2/SEPA 통과자에 대한 가점(오버레이)으로만 쓰였다.
        그런데 VCP는 "아직 안 움직였고 거래량도 줄어드는" 패턴이라 정의상
        RSI2(과매도 반전)나 SEPA(추세 진행)의 필터를 통과하기 어렵다.
        실측 2026-07-31: VCP 12종목 탐지 → 오버레이 반영 2종목.
        선행 신호를 후행 게이트에 묶지 않도록 독립 후보 라인으로 분리한다.

        진입은 수축 완료 후 "돌파 확인" 시점 — 최근 20일 고점 + buffer를 진입가로
        삼아 batch_analyzer가 조건부 주문(돌파 시 진입)으로 처리한다.

        Args:
            candidates_data: _calculate_all_indicators() 결과
            vcp_results: VCPDetector.detect_all() 결과
            exclude: 이미 다른 전략 후보로 잡힌 종목 (중복 진입 방지)
            min_score: VCP 점수 하한
            breakout_buffer_pct: 돌파 확인 버퍼 (%)
        """
        if not vcp_results:
            return []

        by_symbol = {d["symbol"]: d for d in candidates_data}
        results: List[SwingCandidate] = []
        # 탈락 사유 집계 — "왜 후보가 안 나오는가"를 추측이 아니라 수치로 본다
        drops = {"중복": 0, "우선주": 0, "점수미달": 0, "MA비정배열": 0,
                 "거래량미감소": 0, "수축부족": 0, "데이터없음": 0}

        for vcp in vcp_results:
            sym = getattr(vcp, "symbol", None)
            if not sym:
                continue
            if sym in exclude:
                drops["중복"] += 1
                continue
            # 우선주 제외 (종목코드 끝자리가 0이 아니면 우선주/신주인수권 등)
            # 실측 2026-08-03 VCP 목록에 삼성화재우·NH투자증권우가 섞였는데,
            # 유동성이 낮아 돌파 진입/청산 슬리피지가 크다.
            if len(sym) == 6 and not sym.endswith("0"):
                drops["우선주"] += 1
                continue
            # 품질 게이트: 점수 + MA 정배열 + 거래량 감소 + 수축 2회 이상
            if getattr(vcp, "score", 0) < min_score:
                drops["점수미달"] += 1
                continue
            if not getattr(vcp, "ma_aligned", False):
                drops["MA비정배열"] += 1
                continue
            if not getattr(vcp, "vol_declining", False):
                drops["거래량미감소"] += 1
                continue
            if getattr(vcp, "contraction_count", 0) < 2:
                drops["수축부족"] += 1
                continue

            data = by_symbol.get(sym)
            if not data:
                drops["데이터없음"] += 1
                continue
            ind = data["indicators"]
            close = float(ind.get("close") or 0)
            high_20d = float(ind.get("high_20d") or 0)
            if close <= 0 or high_20d <= 0:
                continue

            # 진입: 20일 고점 돌파 확인가.
            # 종가가 이미 그 위면 종가를 트리거로 쓴다 — 트리거가 현재가보다 낮으면
            # "돌파 확인"이 아니라 즉시 시장가 매수가 되어 버리기 때문이다.
            # 따라서 실효 트리거는 max(20일고점×(1+버퍼), 종가)이다.
            entry = max(high_20d * (1 + breakout_buffer_pct / 100), close)
            entry_price = Decimal(str(round(entry, 2)))

            # 손절: 수축 구간 저점(20일 최저) — 단, 최대 -8%로 제한
            low_20d = float(ind.get("low_20d") or 0)
            floor_stop = entry * 0.92
            stop = max(low_20d, floor_stop) if low_20d > 0 else floor_stop
            if stop >= entry:  # 이상 데이터 방어
                stop = entry * 0.95
            stop_price = Decimal(str(round(stop, 2)))
            target_price = Decimal(str(round(entry * 1.15, 2)))

            reasons = list(getattr(vcp, "reasons", []) or [])
            reasons.insert(
                0,
                f"VCP 수축 {getattr(vcp, 'contraction_count', 0)}회 "
                f"(52주고점 {getattr(vcp, 'high_proximity', 0) * 100:.0f}% 지점)",
            )
            reasons.append(f"돌파 진입 {entry_price:,.0f} (20일고점 +{breakout_buffer_pct}%)")

            candidate = SwingCandidate(
                symbol=sym,
                name=getattr(vcp, "name", "") or data.get("name", sym),
                strategy="vcp_breakout",
                score=0,
                entry_price=entry_price,
                stop_price=stop_price,
                target_price=target_price,
                indicators=ind,
                reasons=reasons,
            )
            # 조건부 진입 메타 (batch_analyzer가 돌파 주문으로 처리)
            candidate.indicators["vcp_score"] = float(getattr(vcp, "score", 0))
            candidate.indicators["breakout_trigger"] = float(entry_price)
            candidate.indicators["entry_mode"] = "breakout"
            results.append(candidate)

        if vcp_results:
            _drop_txt = ", ".join(f"{k} {v}" for k, v in drops.items() if v)
            logger.info(
                f"[스윙스크리너] VCP 독립 라인: {len(vcp_results)}종목 중 채택 {len(results)}개"
                + (f" (탈락: {_drop_txt})" if _drop_txt else "")
            )
        return results

    async def _apply_composite_score(
        self, candidates: List[SwingCandidate]
    ) -> List[SwingCandidate]:
        """
        4단계: 복합 점수 (0-100)

        | 카테고리 | 비중 | 내용 |
        |---------|------|------|
        | 기술적 | 40% | RSI 위치, MA 정렬, BB 위치 |
        | 수급 | 30% | 외국인+기관 순매수 |
        | 재무 | 20% | PER/PBR/ROE |
        | 섹터 | 10% | 섹터 모멘텀 |
        """
        # ── 수급 데이터 일괄 조회 (LCI용) ──
        # 1차: KIS 실시간 API (장중/마감 후)
        # 2차: pykrx 전일 수급 (프리장 등 KIS 데이터 없는 경우 자동 폴백)
        supply_demand: Dict[str, Dict[str, int]] = {}  # symbol -> {foreign_net_buy, inst_net_buy}
        # ── 0차: 외국계 추정 가집계 (장중 10시 이후만, TR: FHKST644100C0) ──
        # 외국계 브로커 기반 추정치 — 결제 전 데이터라 장중에만 유효.
        # 1차(fetch_foreign_institution)와 병합해 커버리지 최대화.
        _now_hour = datetime.now().hour
        if self._kis_market_data and _now_hour >= 10:
            try:
                _est_results = await asyncio.gather(
                    self._kis_market_data.fetch_frgnmem_trade_estimate(market="1001", sort_cls="0"),  # 코스피 외국계 매수
                    self._kis_market_data.fetch_frgnmem_trade_estimate(market="2001", sort_cls="0"),  # 코스닥 외국계 매수
                    return_exceptions=True,
                )
                for _est_res in _est_results:
                    if isinstance(_est_res, list):
                        for _item in _est_res:
                            _sym = _item.get("symbol", "")
                            if _sym:
                                if _sym not in supply_demand:
                                    supply_demand[_sym] = {"foreign_net_buy": 0, "inst_net_buy": 0}
                                supply_demand[_sym]["foreign_net_buy"] += _item.get("net_buy_qty", 0)
                if supply_demand:
                    logger.info(f"[스윙스크리너] 0차 외국계가집계: {len(supply_demand)}종목")
            except Exception as _e:
                logger.debug(f"[스윙스크리너] 0차 외국계가집계 실패: {_e}")
        if self._kis_market_data:
            try:
                fi_results = await asyncio.gather(
                    self._kis_market_data.fetch_foreign_institution(market="0001", investor="1"),
                    self._kis_market_data.fetch_foreign_institution(market="0002", investor="1"),
                    self._kis_market_data.fetch_foreign_institution(market="0001", investor="2"),
                    self._kis_market_data.fetch_foreign_institution(market="0002", investor="2"),
                    return_exceptions=True,
                )
                # 외국인 (index 0,1)
                for res in fi_results[:2]:
                    if isinstance(res, list):
                        for item in res:
                            sym = item.get("symbol", "")
                            if sym not in supply_demand:
                                supply_demand[sym] = {"foreign_net_buy": 0, "inst_net_buy": 0}
                            supply_demand[sym]["foreign_net_buy"] += item.get("net_buy_qty", 0)
                # 기관 (index 2,3)
                for res in fi_results[2:]:
                    if isinstance(res, list):
                        for item in res:
                            sym = item.get("symbol", "")
                            if sym not in supply_demand:
                                supply_demand[sym] = {"foreign_net_buy": 0, "inst_net_buy": 0}
                            supply_demand[sym]["inst_net_buy"] += item.get("net_buy_qty", 0)
                logger.info(f"[스윙스크리너] 수급 데이터 조회: {len(supply_demand)}종목")
            except Exception as e:
                logger.warning(f"[스윙스크리너] 수급 데이터 조회 실패: {e}")

        # KIS 성공 시 캐시 저장 (다음날 08:20 아침 스캔 폴백용)
        supply_data_age = 0   # 0=당일, 1=전일(T-1), 2=캐시(T-2+)
        if supply_demand:
            self._save_supply_demand_cache(supply_demand)

        # ── 2~4차 폴백: 1차 성공 시 prev_date_str/candidate_symbols 계산 자체를 건너뜀 ──
        if not supply_demand:
            # prev_date_str: 직전 영업일 (주말 스킵)
            _prev = datetime.now().date() - timedelta(days=1)
            while _prev.weekday() >= 5:
                _prev -= timedelta(days=1)
            prev_date_str = _prev.strftime("%Y%m%d")
            candidate_symbols = [c.symbol for c in candidates]

            # ── 2차: KIS 종목별 전일 투자자 API (FHKST01010900) ──
            # pykrx(KRX 인증 차단)를 완전 대체. 장전 08:20에도 T-1 확정 데이터 정상 반환.
            if self._kis_market_data:
                try:
                    kis_investor = await self._kis_market_data.fetch_batch_investor_daily(
                        candidate_symbols, prev_date_str, concurrency=10
                    )
                    if kis_investor:
                        supply_demand = kis_investor
                        supply_data_age = 1
                        logger.info(
                            f"[스윙스크리너] 수급 T-1: KIS 종목별 API "
                            f"({len(supply_demand)}/{len(candidate_symbols)}종목, {prev_date_str})"
                        )
                except Exception as e:
                    logger.warning(f"[스윙스크리너] KIS 종목별 투자자 조회 실패: {e}")

            # ── 3차: supply_demand 캐시 폴백 ──
            if not supply_demand:
                supply_demand = self._load_prev_day_supply_from_cache(prev_date_str)
                if supply_demand:
                    supply_data_age = 1
                    logger.info(
                        f"[스윙스크리너] 수급 T-1 캐시 폴백: {len(supply_demand)}종목"
                    )

            # ── 4차: T-2+ 캐시 최종 폴백 ──
            if not supply_demand:
                cached, cache_age = self._load_supply_demand_cache_with_age()
                if cached:
                    supply_demand = cached
                    supply_data_age = min(cache_age, 2)

        # ── 후보별 수급 데이터 주입 ──
        for candidate in candidates:
            sd = supply_demand.get(candidate.symbol, {})
            candidate.indicators["foreign_net_buy"] = sd.get("foreign_net_buy", 0)
            candidate.indicators["inst_net_buy"] = sd.get("inst_net_buy", 0)
            candidate.indicators["supply_data_age"] = supply_data_age  # 신선도 추적

        # 밸류에이션 일괄 조회 (배치 API 활용)
        if self._kis_market_data:
            try:
                symbols = [c.symbol for c in candidates]
                valuations = await self._kis_market_data.fetch_batch_valuations(symbols)
                for candidate in candidates:
                    val = valuations.get(candidate.symbol)
                    if val:
                        candidate.indicators["per"] = val.get("per", 0)
                        candidate.indicators["pbr"] = val.get("pbr", 0)
                        candidate.indicators["roe"] = val.get("roe", 0)
                logger.debug(f"[스윙스크리너] 밸류에이션 일괄 조회: {len(valuations)}종목")
            except Exception as e:
                logger.debug(f"[스윙스크리너] 밸류에이션 일괄 조회 실패: {e}")

        for candidate in candidates:
            # 점수는 전략의 generate_batch_signals에서 계산하므로
            # 여기서는 기본 기술적 점수만 설정
            score = self._base_technical_score(candidate.indicators, candidate.strategy)
            candidate.score = score

        return candidates

    def _base_technical_score(self, ind: Dict[str, Any], strategy: str) -> float:
        """기본 기술적 점수 (전략에서 상세 점수 재계산)"""
        score = 50.0  # 기본값

        if strategy == "rsi2_reversal":
            rsi_2 = ind.get("rsi_2")
            if rsi_2 is not None:
                if rsi_2 < 5:
                    score += 20
                elif rsi_2 < 10:
                    score += 10

            ma200 = ind.get("ma200")
            close = ind.get("close", 0)
            if ma200 is not None and ma200 > 0 and close is not None and close > ma200:
                score += 10

        elif strategy == "vcp_breakout":
            # 수축 품질(VCP 점수)과 52주 고점 근접도로 기본 점수를 잡는다.
            vcp_score = ind.get("vcp_score")
            if vcp_score is not None:
                # 편입 하한이 60이므로 기준점도 60이어야 한다.
                # (기준을 70으로 두면 60~69점 후보가 편입 직후 감점되는 모순)
                score += max(0.0, min((vcp_score - 60) * 0.375, 15))  # 60점=+0, 100점=+15
            high_52w = ind.get("high_52w")
            close = ind.get("close")
            if high_52w and close and high_52w > 0:
                from_high = (close - high_52w) / high_52w * 100
                if from_high >= -10:
                    score += 10
                elif from_high >= -20:
                    score += 5
            # MA200 과확장 감점 (수축 패턴인데 이미 과열이면 실패 확률 상승)
            ma200_dist = ind.get("ma200_distance_pct")
            if ma200_dist is not None and ma200_dist > 40:
                score -= 10

        elif strategy == "sepa_trend":
            if ind.get("sepa_pass"):
                score += 15

            # 52주 고점 근접
            high_52w = ind.get("high_52w", 0)
            close = ind.get("close", 0)
            if high_52w is not None and high_52w > 0 and close is not None and close > 0:
                from_high = (close - high_52w) / high_52w * 100
                if from_high >= -10:
                    score += 10

            # MA200 과확장 감점 (60일 급등 후행 추격 방지)
            ma200_dist = ind.get("ma200_distance_pct")
            if ma200_dist is not None:
                if ma200_dist > 50:
                    score -= 10
                elif ma200_dist > 30:
                    score -= 5

        return min(score, 100)

    @staticmethod
    def _should_exclude(name: str) -> bool:
        """ETF/ETN/관리종목/정리매매 제외 판단

        ETF 운용사 브랜드는 종목명 앞에서 시작하는 경우만 제외
        (ACE, SOL, BNK 등 일반 기업명과 구분)
        """
        upper = name.upper()

        # ETF 운용사 브랜드: 종목명이 해당 키워드로 시작할 때만 제외
        # (ex: "ACE 코스피200" ✅ 제외 / "에이스기술" ❌ 통과)
        etf_brand_prefixes = [
            "KODEX", "TIGER", "KBSTAR", "ARIRANG", "KOSEF",
            "HANARO", "SOL ", "KINDEX", "ACE ", "PLUS ", "RISE ",
            "BNK ", "TIMEFOLIO", "WOORI ", "FOCUS ", "TREX ",
            "SMART ", "MASTER ",
        ]
        if any(upper.startswith(kw) for kw in etf_brand_prefixes):
            return True

        # ETF/ETN 키워드: 종목명 어디에든 포함되면 제외 (명확한 상품 유형 표기)
        etf_type_keywords = ["ETF", "ETN"]
        if any(kw in upper for kw in etf_type_keywords):
            return True

        # 파생상품 키워드 (한글 — 기업명에 포함될 가능성 낮음)
        derivative_keywords = ["인버스", "레버리지", "선물", "채권", "원유", "금선물"]
        if any(kw in name for kw in derivative_keywords):
            return True

        # 관리종목/정리매매 상태 (원문 포함 검사)
        status_keywords = ["정리매매", "투자주의", "투자경고", "투자위험"]
        if any(kw in name for kw in status_keywords):
            return True

        return False

    def get_market_regime(self) -> str:
        """KOSPI 종가 기반 시장 레짐 판단

        Returns: "bull" | "caution" | "bear" | "neutral"

        기준:
          bear:    5일 변화율 ≤ -3% OR 20일 변화율 ≤ -5%
          caution: 5일 변화율 ≤ -1.5% OR 20일 변화율 ≤ -2.5%
          bull:    5일 변화율 ≥ +1% AND 20일 변화율 ≥ 0%
          neutral: 그 외
        """
        closes = self._kospi_closes
        if not closes or len(closes) < 6:
            return "neutral"

        c5  = (closes[-1] - closes[-6])  / closes[-6]  * 100 if len(closes) >= 6  else 0.0
        c20 = (closes[-1] - closes[-21]) / closes[-21] * 100 if len(closes) >= 21 else 0.0

        if c5 <= -3.0 or c20 <= -5.0:
            return "bear"
        elif c5 <= -1.5 or c20 <= -2.5:
            return "caution"
        elif c5 >= 1.0 and c20 >= 0.0:
            return "bull"
        return "neutral"

    def get_kospi_change(self) -> dict:
        """레짐 판단에 사용된 수치 반환 (로깅/알림용)"""
        closes = self._kospi_closes
        if not closes or len(closes) < 6:
            return {"c5": 0.0, "c20": 0.0, "level": 0.0}
        c5  = (closes[-1] - closes[-6])  / closes[-6]  * 100 if len(closes) >= 6  else 0.0
        c20 = (closes[-1] - closes[-21]) / closes[-21] * 100 if len(closes) >= 21 else 0.0
        return {"c5": round(c5, 2), "c20": round(c20, 2), "level": round(closes[-1], 2)}

    async def _load_benchmark_index(self):
        """벤치마크 지수(KOSPI) 1년치 로드 (MRS 계산용)

        1차: FDR (FinanceDataReader)
        2차: KIS API 폴백 (KOSPI 지수 최근 20일)
        """
        # 1차: FDR
        try:
            loop = asyncio.get_running_loop()
            start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
            kospi_df = await loop.run_in_executor(
                None, self._fetch_fdr_data, "KS11", start_date
            )
            if kospi_df is not None and len(kospi_df) >= 50:
                self._kospi_closes = [float(row["Close"]) for _, row in kospi_df.iterrows()]
                logger.info(f"[스윙스크리너] KOSPI 벤치마크 로드: {len(self._kospi_closes)}일")
                return
        except Exception as e:
            logger.warning(f"[스윙스크리너] KOSPI 벤치마크 FDR 로드 오류: {e}")

        # 2차: KIS API 폴백 (KOSPI 지수 최근 20일)
        self._kospi_closes = []
        if self._broker:
            try:
                history = await self._broker.get_daily_prices("0001", days=20)
                if history and len(history) >= 10:
                    self._kospi_closes = [
                        float(bar.get("close", 0)) for bar in history
                        if float(bar.get("close", 0)) > 0
                    ]
                    logger.info(
                        f"[스윙스크리너] KOSPI 벤치마크 KIS API 폴백: {len(self._kospi_closes)}일"
                    )
                else:
                    logger.warning("[스윙스크리너] KOSPI 벤치마크 KIS API 데이터 부족")
            except Exception as e2:
                logger.warning(f"[스윙스크리너] KOSPI 벤치마크 KIS API 폴백 실패: {e2}")
        else:
            logger.warning("[스윙스크리너] KOSPI 벤치마크 로드 실패 (FDR + KIS 모두 불가)")

    def _save_supply_demand_cache(self, data: Dict[str, Dict[str, int]]) -> None:
        """수급 데이터를 날짜별 캐시 파일로 저장 (KIS API 성공 시)."""
        import json
        from pathlib import Path
        from datetime import datetime
        try:
            cache_dir = Path.home() / ".cache" / "ai_trader"
            cache_dir.mkdir(parents=True, exist_ok=True)
            date_str = datetime.now().strftime("%Y%m%d")
            cache_path = cache_dir / f"supply_demand_{date_str}.json"
            cache_path.write_text(json.dumps(data))
            logger.debug(f"[스윙스크리너] 수급 캐시 저장: {date_str} ({len(data)}종목)")
        except Exception as e:
            logger.debug(f"[스윙스크리너] 수급 캐시 저장 실패: {e}")

    def _load_supply_demand_cache(self) -> Dict[str, Dict[str, int]]:
        """가장 최근 수급 캐시 로드 (최대 7일 탐색).

        KIS + pykrx 모두 실패 시 (장전 08:20, KRX 차단 등) 사용.
        전 거래일 장중에 저장된 캐시를 읽어 LCI 계산에 활용.
        """
        import json
        from pathlib import Path
        from datetime import datetime, timedelta
        cache_dir = Path.home() / ".cache" / "ai_trader"
        for days_ago in range(1, 8):
            target = (datetime.now() - timedelta(days=days_ago)).strftime("%Y%m%d")
            cache_path = cache_dir / f"supply_demand_{target}.json"
            if cache_path.exists():
                try:
                    data = json.loads(cache_path.read_text())
                    if data:
                        logger.info(
                            f"[스윙스크리너] 수급 캐시 로드: {target} ({len(data)}종목) "
                            f"← KIS/pykrx 모두 실패로 폴백"
                        )
                        return data
                except Exception as e:
                    logger.debug(f"[스윙스크리너] 수급 캐시 로드 실패 ({target}): {e}")
        logger.warning("[스윙스크리너] 수급 캐시 없음 → LCI=None (폴백 불가)")
        return {}

    def _load_supply_demand_cache_with_age(self):
        """
        가장 최근 수급 캐시 로드 + 실제 영업일 기준 age 반환.

        주말/공휴일을 건너뛰어 실제 거래일 gap으로 age를 계산합니다.
        예) 월요일 08:20 조회 → 금요일 캐시(캘린더 days_ago=3) → 영업일 gap=1 → age=1

        Returns:
            (data: Dict, age: int) - age는 영업일 기준 경과일 (최대 2)
            데이터 없으면 ({}, 99)
        """
        import json as _json
        from pathlib import Path as _Path
        cache_dir = _Path.home() / ".cache" / "ai_trader"
        today = datetime.now().date()
        for days_ago in range(1, 8):
            target_date = today - timedelta(days=days_ago)
            target = target_date.strftime("%Y%m%d")
            cache_path = cache_dir / f"supply_demand_{target}.json"
            if cache_path.exists():
                try:
                    data = _json.loads(cache_path.read_text())
                    if data:
                        # 영업일 기준 age: today와 target_date 사이의 평일(월~금) 수만 카운트
                        # 예) 월요일 조회 → 금요일 캐시(calendar days_ago=3) → biz_days=1 → age=1
                        biz_days_ago = sum(
                            1 for i in range(1, days_ago + 1)
                            if (today - timedelta(days=i)).weekday() < 5
                        )
                        age = min(biz_days_ago, 2)  # 3일↑ 이상은 age=2로 처리
                        logger.info(
                            f"[스윙스크리너] 수급 캐시 로드: {target} ({len(data)}종목) "
                            f"age={age}(영업일) ← KIS 모두 실패로 폴백"
                        )
                        return data, age
                except Exception as e:
                    logger.debug(f"[스윙스크리너] 수급 캐시 로드 실패 ({target}): {e}")
        logger.warning("[스윙스크리너] 수급 캐시 없음 → LCI=None (폴백 불가)")
        return {}, 99

    def _fetch_prev_day_supply_pykrx(self) -> Dict[str, Dict[str, int]]:
        """pykrx로 전일 외국인/기관 순매수 데이터 조회 (동기 함수, asyncio.to_thread 필요)

        KIS 실시간 API가 0종목일 때(프리장, 주말 등) 자동 폴백으로 사용.
        KOSPI + KOSDAQ 외국인 + 기관합계 4번 호출 → 약 0.8초.

        Returns:
            {symbol: {"foreign_net_buy": int, "inst_net_buy": int}, ...}
        """
        import pykrx.stock as pykrx_stock
        from datetime import datetime, timedelta

        # 전일 날짜 계산 (주말이면 금요일)
        target = datetime.now().date() - timedelta(days=1)
        while target.weekday() >= 5:  # 토(5), 일(6) → 금요일로
            target -= timedelta(days=1)
        date_str = target.strftime("%Y%m%d")

        result: Dict[str, Dict[str, int]] = {}

        # KOSPI + KOSDAQ, 외국인 + 기관합계
        configs = [
            ("KOSPI", "외국인", "foreign_net_buy"),
            ("KOSDAQ", "외국인", "foreign_net_buy"),
            ("KOSPI", "기관합계", "inst_net_buy"),
            ("KOSDAQ", "기관합계", "inst_net_buy"),
        ]

        for market, investor, field in configs:
            try:
                df = pykrx_stock.get_market_net_purchases_of_equities(
                    date_str, date_str, market, investor
                )
                if df is None or df.empty:
                    continue
                for ticker, row in df.iterrows():
                    sym = str(ticker).zfill(6)
                    if sym not in result:
                        result[sym] = {"foreign_net_buy": 0, "inst_net_buy": 0}
                    _raw_net_qty = row.get("순매수거래량", 0)
                    net_qty = int(_raw_net_qty) if _raw_net_qty is not None else 0
                    result[sym][field] += net_qty
            except Exception as e:
                logger.debug(f"[스윙스크리너] pykrx {market} {investor} 조회 실패: {e}")

        logger.info(f"[스윙스크리너] pykrx 전일 수급({date_str}): {len(result)}종목 로드")
        return result

    @staticmethod
    def _load_prev_day_supply_from_cache(date_str: str) -> Dict[str, Dict[str, int]]:
        """
        KIS/pykrx 실패 시 supply_demand_YYYYMMDD.json 스냅샷 캐시로 폴백.

        supply_demand 캐시 스키마: {sym: {"foreign_net_buy": N, "inst_net_buy": N}}
        date_str 날짜 우선 탐색, 없으면 최대 7일 이내 최신 파일 탐색 (연휴 커버).
        """
        import json as _json
        from pathlib import Path as _Path
        cache_dir = _Path.home() / ".cache" / "ai_trader"

        try:
            dt = datetime.strptime(date_str, "%Y%m%d")
        except Exception:
            dt = datetime.now() - timedelta(days=1)

        for delta in range(0, 8):
            target = (dt - timedelta(days=delta)).strftime("%Y%m%d")
            path = cache_dir / f"supply_demand_{target}.json"
            if path.exists():
                try:
                    raw = _json.loads(path.read_text())
                    if raw:
                        logger.info(
                            f"[스윙스크리너] supply_demand 캐시 폴백: "
                            f"{target} ({len(raw)}종목)"
                        )
                        return raw
                except Exception:
                    pass
        return {}

    def _compute_lci_zscore(self, candidates: List[SwingCandidate]):
        """
        전체 후보의 외국인/기관 순매수 → z-score → LCI 계산

        LCI = 0.5 * z(foreign) + 0.5 * z(inst)
        """
        if not candidates:
            return

        all_foreign = [c.indicators.get("foreign_net_buy") if c.indicators.get("foreign_net_buy") is not None else 0 for c in candidates]
        all_inst = [c.indicators.get("inst_net_buy") if c.indicators.get("inst_net_buy") is not None else 0 for c in candidates]

        def zscore_list(values: List[float]) -> List[float]:
            n = len(values)
            if n < 2:
                return [0.0] * n
            mean = sum(values) / n
            variance = sum((v - mean) ** 2 for v in values) / n
            std = variance ** 0.5
            if std < 1e-10:
                return [0.0] * n
            return [(v - mean) / std for v in values]

        z_foreign = zscore_list(all_foreign)
        z_inst = zscore_list(all_inst)

        # ── P0-2: 시장 전체 수급 상태 절대값 게이트 ──
        # LCI는 후보 집합 내 상대 z-score만 측정. 시장 전체가 수급 악화 중이어도
        # "상대적으로 덜 나쁜" 종목이 높은 LCI를 받는 구조.
        # 게이트: 후보 집합의 60% 이상이 외국인 순매도 → 시장 수급 압박 → LCI 50% 할인
        n_neg_foreign = sum(1 for v in all_foreign if v < 0)
        market_sell_pressure = (n_neg_foreign / len(all_foreign)) > 0.60 if all_foreign else False
        lci_market_mult = 0.5 if market_sell_pressure else 1.0
        if market_sell_pressure:
            logger.warning(
                f"[스윙스크리너] 시장 수급 압박 감지: 외국인 순매도 종목 "
                f"{n_neg_foreign}/{len(all_foreign)}개 → LCI 50% 할인 적용"
            )

        # 수급 데이터 전무(std≈0) 시 z-score 전부 0 → LCI=None으로 설정하여 폴백 경로 활성화
        all_zero = all(z == 0.0 for z in z_foreign) and all(z == 0.0 for z in z_inst)
        for i, c in enumerate(candidates):
            if all_zero:
                c.indicators["lci"] = None
                c.indicators["market_sell_pressure"] = market_sell_pressure
            else:
                lci = 0.5 * z_foreign[i] + 0.5 * z_inst[i]

                # 수급 가속도: 외국인+기관 동시 순매수 시 LCI 부스트
                if all_foreign[i] > 0 and all_inst[i] > 0:
                    raw_accel = 0.3 + (z_foreign[i] + z_inst[i]) * 0.1
                    accel = max(0, min(raw_accel, 0.5))  # 0~0.5 클램프
                    lci += accel
                    c.indicators["supply_accel"] = round(accel, 3)

                # 시장 수급 압박 할인 적용
                lci *= lci_market_mult
                c.indicators["lci"] = round(lci, 3)
                c.indicators["market_sell_pressure"] = market_sell_pressure

    async def _apply_strategic_overlay(
        self, candidates: List[SwingCandidate]
    ) -> List[SwingCandidate]:
        """5단계: 3계층 전략적 신호로 점수 보정

        Layer 1: 전문가 패널 추천 → +최대 25점
        Layer 2: 수급 추세 → +최대 20점
        Layer 3: VCP 패턴 → +최대 15점
        다층 중첩 보너스: 2계층 +8, 3계층 +15
        """
        # 1) 전문가 추천 로드
        outlook = self._load_strategic_outlook()
        recommended = {}
        # 신선도 할인 계산: 오래될수록 보너스 감소 (7일에 50%, 14일에 0%)
        panel_freshness = 1.0
        if outlook:
            try:
                _created = datetime.fromisoformat(outlook.created_at)
                _days_old = (datetime.now() - _created).days
                panel_freshness = max(0.3, 1.0 - _days_old / 14.0)
                if _days_old >= 3:
                    logger.info(
                        f"[스윙스크리너] 전문가패널 {_days_old}일 경과 "
                        f"→ 신선도={panel_freshness:.1%}"
                    )
            except Exception:
                panel_freshness = 1.0
            recommended = {s.symbol: s for s in outlook.recommended_stocks}
            logger.info(f"[스윙스크리너] 전문가 추천 {len(recommended)}종목 로드")

        # 2) 수급 추세 로드 (SupplyTrendDetector — 심층 20일, ~80종목)
        supply_trends = self._load_supply_trends()
        trending = {s.symbol: s for s in supply_trends}
        if trending:
            logger.info(f"[스윙스크리너] 수급 추세 {len(trending)}종목 로드")

        # 2b) 5일 수급 스코어 (SupplyScoreProvider)
        # 우선순위: KIS 종목별 API → supply_demand 캐시 폴백 → pykrx(실질적으로 항상 실패)
        # ensure_loaded_from_kis 내부에서 KIS 실패 날짜를 ensure_loaded로 자동 폴백.
        supply5d = self._supply5d
        try:
            symbols_for_supply = [c.symbol for c in candidates]
            if self._kis_market_data and symbols_for_supply:
                await supply5d.ensure_loaded_from_kis(
                    self._kis_market_data, symbols_for_supply
                )
            else:
                await supply5d.ensure_loaded()
            logger.info(f"[스윙스크리너] 5일수급 준비: {len(supply5d._loaded_dates)}일치")
        except Exception as _e:
            logger.warning(f"[스윙스크리너] 5일수급 로드 실패: {_e}")
            supply5d = None

        # 3) VCP 후보 로드
        vcp_candidates = self._load_vcp_candidates()
        vcp_map = {v.symbol: v for v in vcp_candidates}
        if vcp_map:
            logger.info(f"[스윙스크리너] VCP 후보 {len(vcp_map)}종목 로드")

        if not recommended and not trending and not vcp_map:
            logger.debug("[스윙스크리너] 전략적 오버레이 데이터 없음, 스킵")
            return candidates

        OVERLAY_MAX_BONUS = 25   # 오버레이 총합 캡: 기본 점수 체계 무력화 방지
        OVERLAY_MIN_BASE = 50    # 기본 점수가 너무 낮으면 오버레이 미적용

        overlay_applied = 0
        for candidate in candidates:
            sym = candidate.symbol
            layers_matched = 0

            # 기본 점수 미달 시 오버레이 스킵 (저품질 신호 진입 차단)
            if candidate.score < OVERLAY_MIN_BASE:
                candidate.indicators["strategic_layers"] = 0
                continue

            pre_overlay_score = candidate.score  # 오버레이 전 점수 기록

            # Layer 1: 전문가 추천 보너스 (신선도 할인 적용)
            if sym in recommended:
                pick = recommended[sym]
                raw_bonus = int(pick.conviction * 25)
                bonus = max(3, int(raw_bonus * panel_freshness))  # 최소 3pt 보장
                candidate.score += bonus
                freshness_note = (
                    f" [{panel_freshness:.0%}신선도]" if panel_freshness < 0.9 else ""
                )
                candidate.reasons.append(
                    f"전문가패널 추천 (확신도 {pick.conviction:.0%}){freshness_note}"
                )
                layers_matched += 1

            # Layer 2: 수급 추세 보너스 (SupplyTrendDetector — 심층 20일)
            if sym in trending:
                trend = trending[sym]
                bonus = min(int(trend.score * 0.2), 20)  # 최대 +20
                candidate.score += bonus
                candidate.reasons.append(
                    f"수급추세 {trend.foreign_streak}일외국인+{trend.inst_streak}일기관"
                )
                # 2026-05-11 P0-1 계측: delta_ratio 영속화 (효과 검증 트레이스)
                _dr = getattr(trend, "delta_ratio", 0.0) or 0.0
                if _dr > 0:
                    candidate.indicators["supply_delta_ratio"] = round(float(_dr), 2)
                    if _dr >= 3:
                        candidate.reasons.append(
                            f"수급 델타 {'폭증' if _dr >= 5 else '점프'}({_dr:.1f}x)"
                        )
                layers_matched += 1
            elif supply5d and supply5d.is_ready:
                # SupplyTrendDetector 미수록 종목 → 5일 수급 스코어로 보완
                bonus5d = supply5d.get_bonus(sym, max_bonus=15.0)
                if bonus5d >= 5.0:
                    meta5d = supply5d.get_meta(sym)
                    candidate.score += bonus5d
                    f_streak = meta5d.get("foreign_streak", 0)
                    i_streak = meta5d.get("inst_streak", 0)
                    desc = (
                        f"5일수급 외{f_streak}일+기{i_streak}일"
                        + (" 가속" if meta5d.get("is_accelerating") else "")
                    )
                    candidate.reasons.append(desc)
                    if bonus5d >= 10.0:
                        layers_matched += 1  # 강한 신호만 계층 카운트

            # Layer 3: VCP 패턴 보너스
            # vcp_breakout 후보는 VCP 자체가 진입 근거이므로 이중 가점하지 않는다
            if sym in vcp_map and candidate.strategy != "vcp_breakout":
                vcp = vcp_map[sym]
                bonus = min(int(vcp.score * 0.15), 15)  # 최대 +15
                candidate.score += bonus
                candidate.reasons.append(f"VCP 변동성수축 (점수 {vcp.score:.0f})")
                layers_matched += 1

            # 다층 중첩 보너스
            if layers_matched >= 3:
                candidate.score += 10
                candidate.reasons.append("★ 3계층 복합신호 (전문가+수급+VCP)")
            elif layers_matched >= 2:
                candidate.score += 5
                candidate.reasons.append("2계층 복합신호")

            # 오버레이 총합 캡 적용 (최대 +25점)
            overlay_added = candidate.score - pre_overlay_score
            if overlay_added > OVERLAY_MAX_BONUS:
                candidate.score = pre_overlay_score + OVERLAY_MAX_BONUS
                overlay_added = OVERLAY_MAX_BONUS
                logger.debug(
                    f"[스윙스크리너] {sym} 오버레이 캡 적용: "
                    f"+{overlay_added:.0f} → +{OVERLAY_MAX_BONUS}점"
                )

            # 구조화된 메타데이터 (batch_analyzer에서 문자열 파싱 대신 사용)
            candidate.indicators["strategic_layers"] = layers_matched
            # generate_batch_signals가 indicators에서 재계산하므로
            # overlay bonus를 별도 필드에 저장 → 전략 점수에 가산
            candidate.indicators["overlay_bonus"] = round(overlay_added, 1)

            if layers_matched > 0:
                overlay_applied += 1

        if overlay_applied > 0:
            logger.info(f"[스윙스크리너] 전략적 오버레이 적용: {overlay_applied}종목")

        return candidates

    @staticmethod
    def _load_strategic_outlook():
        """전문가 패널 결과 캐시 로드"""
        try:
            from src.signals.strategic.expert_panel import ExpertPanel
            panel = ExpertPanel()
            return panel.load_outlook()
        except Exception as e:
            logger.debug(f"[스윙스크리너] 전문가 패널 캐시 로드 실패: {e}")
            return None

    @staticmethod
    def _load_supply_trends():
        """수급 추세 캐시 로드"""
        try:
            from src.signals.strategic.supply_trend import SupplyTrendDetector
            detector = SupplyTrendDetector()
            return detector.load_cache()
        except Exception as e:
            logger.debug(f"[스윙스크리너] 수급 추세 캐시 로드 실패: {e}")
            return []

    @staticmethod
    def _load_vcp_candidates():
        """VCP 후보 캐시 로드"""
        try:
            from src.signals.strategic.vcp_detector import VCPDetector
            detector = VCPDetector()
            return detector.load_cache()
        except Exception as e:
            logger.debug(f"[스윙스크리너] VCP 캐시 로드 실패: {e}")
            return []
