"""
AI Trader US - Finviz Elite Data Provider (v2)

Finviz Elite API로 기관 수급 / 펀더멘털 / 기술지표 / 장중 모멘텀 데이터를 수집.
스크리너 보너스, 전략별 시그널 필터, 리스크 포지션 사이징 조정에 활용.

수집 데이터 그룹:
  Group A - 기관/내부자 수급 (29,27)     → 스크리너 보너스 최대 +30pt
  Group B - 실적 성장 (22,23,17,18,77)   → 보너스 최대 +25pt, SEPA/EarningsDrift
  Group C - 비즈니스 품질 (40,41,33)     → 보너스 최대 +20pt, SEPA 필터
  Group D - 애널리스트 컨센서스 (62,69)  → 보너스 최대 +15pt, 목표가 괴리율
  Group E - 밸류에이션/리스크 (8,48,49)  → 고평가 페널티, 포지션 사이징 보정
  Group F - 기술지표 (59,64,57,30)       → 추가 기술 확인
  Group G - 장중 모멘텀 (93,95,96,97)    → 별도 실시간 호출 (장중품질 스캐너)

API: https://elite.finviz.com/export.ashx
일일 캐시: ~/.cache/ai_trader_us/finviz_YYYY-MM-DD.json (일 1회 갱신)
장중 캐시: 별도 TTL 5분 (intraday_scan용)
"""

import asyncio
import csv
import io
import json
import os
import time
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp
from loguru import logger


CACHE_DIR = Path.home() / ".cache" / "ai_trader_us"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

ELITE_URL = "https://elite.finviz.com/export.ashx"
BATCH_SIZE = 50

# ── 컬럼 정의 (실제 검증 완료) ──────────────────────────────────────────────
# 컬럼 ID → 헤더 이름 (Finviz 실제 반환값)
# 1=Ticker
# 8=Forward P/E, 17=EPS Gr This Yr, 18=EPS Gr Next Yr, 20=EPS Gr Next 5Yr
# 22=EPS QQ, 23=Sales QQ, 27=Insider Trans, 28=Inst Own, 29=Inst Trans
# 30=Short Float, 33=ROE, 39=Gross Margin, 40=Operating Margin, 41=Profit Margin
# 48=Beta, 49=ATR, 57=52W High%, 59=RSI(14), 62=Analyst Recom
# 63=Avg Volume, 64=Rel Volume, 65=Price, 69=Target Price, 77=EPS Next Q

DAILY_COLUMNS = (
    "1,8,17,18,22,23,27,28,29,30,"   # Ticker, Valuation, Growth, Flow
    "33,39,40,41,48,49,57,59,62,"     # Quality, Risk, Technical
    "63,64,65,69,77"                  # Volume, Price, Analyst upside, Earnings
)

# 장중 실시간 컬럼 (5분/15분/30분/1시간 퍼포먼스 + 현재 RSI/RelVol)
INTRADAY_COLUMNS = "1,59,64,65,66,93,95,96,97"


def _pct(value: str) -> float:
    """'3.45%' → 3.45, '-1.2%' → -1.2, '' → 0.0"""
    if not value:
        return 0.0
    try:
        return float(value.replace("%", "").strip())
    except ValueError:
        return 0.0


def _flt(value: str) -> float:
    """'1.31' → 1.31, '' → 0.0"""
    if not value:
        return 0.0
    try:
        return float(value.strip())
    except ValueError:
        return 0.0


class FinvizProvider:
    """
    Finviz Elite 종합 데이터 프로바이더.

    사용 패턴:
        fp = FinvizProvider(token)
        await fp.refresh(universe_symbols)          # 하루 1회
        bonus    = fp.get_bonus_score("NVDA")       # 스크리너 보너스
        meta     = fp.get_meta("NVDA")              # 전체 메타데이터
        signals  = fp.get_strategy_signals("NVDA", "sepa")  # 전략 시그널
        multiplier = fp.get_risk_multiplier("NVDA") # 포지션 사이징 보정
        intraday = await fp.get_intraday_scan(["NVDA","AMD"])  # 장중 스캔
    """

    def __init__(self, api_token: str = ""):
        self._token = api_token or os.getenv("FINVIZ_API_TOKEN", "")
        self._cache: Dict[str, dict] = {}             # daily: symbol → raw dict
        self._cache_date: Optional[date] = None
        self._intraday_cache: Dict[str, dict] = {}    # intraday: symbol → data
        self._intraday_ts: float = 0.0                # intraday 캐시 타임스탬프
        self._intraday_ttl: int = 300                 # 5분 TTL
        self._session: Optional[aiohttp.ClientSession] = None
        self._timeout = aiohttp.ClientTimeout(total=20)

        if not self._token:
            logger.warning("[Finviz] API 토큰 없음 — 보너스/필터 비활성화")

    # ── 세션 ──────────────────────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={"User-Agent": "Mozilla/5.0"},
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ── 캐시 IO ───────────────────────────────────────────────────────────────

    def _cache_path(self, d: date) -> Path:
        return CACHE_DIR / f"finviz_{d.isoformat()}.json"

    def _load_cache(self, d: date) -> Optional[Dict[str, dict]]:
        path = self._cache_path(d)
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                pass
        return None

    def _save_cache(self, d: date, data: Dict[str, dict]):
        try:
            self._cache_path(d).write_text(json.dumps(data))
        except Exception as e:
            logger.debug(f"[Finviz] 캐시 저장 실패: {e}")

    @property
    def is_ready(self) -> bool:
        return bool(self._cache) and bool(self._token)

    # ── 저수준 API 호출 ────────────────────────────────────────────────────────

    async def _fetch_rows(
        self,
        columns: str,
        tickers: Optional[List[str]] = None,
        filter_str: Optional[str] = None,
    ) -> List[dict]:
        """범용 Finviz API 호출 (t= or f= 방식)"""
        if not self._token:
            return []
        session = await self._get_session()
        params: dict = {"v": "152", "c": columns, "auth": self._token}
        if tickers:
            params["t"] = ",".join(tickers)
        if filter_str:
            params["f"] = filter_str
        try:
            async with session.get(ELITE_URL, params=params) as resp:
                if resp.status != 200:
                    logger.warning(f"[Finviz] HTTP {resp.status}")
                    return []
                content = await resp.text()
                return list(csv.DictReader(io.StringIO(content)))
        except Exception as e:
            logger.error(f"[Finviz] API 오류: {e}")
            return []

    # ── 일일 갱신 ─────────────────────────────────────────────────────────────

    async def refresh(self, symbols: List[str], today: date = None) -> bool:
        """
        유니버스 전체 일일 데이터 갱신.

        전략:
          S&P500 (503종목): f=idx_sp500 → 단 1회 API 호출
          S&P400 나머지: t=batch 50종목씩 배치
        """
        if today is None:
            today = date.today()
        if not self._token:
            return False

        # 오늘 캐시 있으면 로드
        if self._cache_date == today and self._cache:
            return False

        cached = self._load_cache(today)
        if cached:
            self._cache = cached
            self._cache_date = today
            logger.info(f"[Finviz] 캐시 로드: {len(self._cache)}종목 ({today})")
            return False

        # 신규 API 조회
        logger.info(f"[Finviz] 일일 데이터 갱신 시작 ({len(symbols)}종목)...")
        new_data: Dict[str, dict] = {}

        # Step 1: S&P500 (인덱스 필터 — 1회 호출)
        sp500_rows = await self._fetch_rows(DAILY_COLUMNS, filter_str="idx_sp500")
        sp500_syms: set = set()
        for row in sp500_rows:
            sym = row.get("Ticker", "").strip()
            if sym:
                new_data[sym] = row
                sp500_syms.add(sym)
        logger.info(f"[Finviz] S&P500 로드: {len(sp500_syms)}종목")

        # Step 2: 나머지 (S&P400 등) 배치 조회
        remaining = [s for s in symbols if s not in sp500_syms]
        if remaining:
            batches = [remaining[i:i + BATCH_SIZE]
                       for i in range(0, len(remaining), BATCH_SIZE)]
            for batch in batches:
                rows = await self._fetch_rows(DAILY_COLUMNS, tickers=batch)
                for row in rows:
                    sym = row.get("Ticker", "").strip()
                    if sym:
                        new_data[sym] = row
                await asyncio.sleep(0.3)
            extra = len(new_data) - len(sp500_syms)
            logger.info(f"[Finviz] 추가 로드: {extra}종목 ({len(batches)}배치)")

        if new_data:
            self._cache = new_data
            self._cache_date = today
            self._save_cache(today, new_data)
            logger.info(f"[Finviz] 갱신 완료: {len(new_data)}종목 캐시 저장")
            return True

        logger.warning("[Finviz] 갱신 실패 — 빈 응답")
        return False

    async def discover_dynamic(self) -> List[str]:
        """
        Finviz f= 필터로 오늘의 핫 종목 동적 발견.

        3가지 필터 셋:
          A. 거래량 급증 + 상승: 장중 핫 종목
          B. 신고가 근접 + 4주 모멘텀: SEPA/추세 후보
          C. 어닝 주간 + 큰 갭: EarningsDrift 후보

        Returns:
            중복 제거된 티커 리스트 (기존 유니버스에 추가용)
        """
        if not self._token:
            return []

        filters = [
            # A: 거래량 급증 + 3% 이상 상승 (프리마켓/장중 핫 종목)
            "sh_avgvol_o500,sh_price_o10,sh_relvol_o2,ta_change_u3",
            # B: 신고가 근접 + 4주(월간) +10% 모멘텀 (추세 추종 후보)
            "sh_avgvol_o500,sh_price_o10,ta_highlow52w_nh,ta_perf_4w10o",
            # C: 어닝 주간 + 5% 이상 상승 (EarningsDrift 후보)
            "sh_avgvol_o500,sh_price_o10,ta_change_u5,earningsdate_thisweek",
        ]
        filter_names = ["거래량급증", "신고가모멘텀", "어닝갭"]

        discovered: set = set()
        for f_str, f_name in zip(filters, filter_names):
            try:
                rows = await self._fetch_rows("1,65", filter_str=f_str)
                syms = {
                    row.get("Ticker", "").strip()
                    for row in rows
                    if row.get("Ticker", "").strip()
                }
                discovered |= syms
                logger.info(f"[Finviz 동적] {f_name}: {len(syms)}종목")
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.warning(f"[Finviz 동적] {f_name} 실패: {e}")

        logger.info(f"[Finviz 동적] 총 {len(discovered)}종목 발견 (중복 제거)")
        return sorted(discovered)

    # ── 장중 실시간 스캔 ──────────────────────────────────────────────────────

    async def get_intraday_scan(self, symbols: List[str]) -> Dict[str, dict]:
        """
        장중 실시간 모멘텀 스캔 (TTL 5분 캐시).

        반환 예시:
          {
            "NVDA": {
              "rsi": 58.2,
              "rel_vol": 2.3,
              "price": 180.5,
              "change_pct": 1.2,
              "perf_5m": 0.5,
              "perf_15m": 0.8,
              "perf_30m": 1.2,
              "perf_1h": 1.8,
              "momentum_score": 72.0   # 종합 장중 모멘텀 점수 (0~100)
            }
          }
        """
        if not self._token or not symbols:
            return {}

        # TTL 내 캐시 재사용
        if time.time() - self._intraday_ts < self._intraday_ttl:
            return {s: v for s, v in self._intraday_cache.items() if s in symbols}

        result: Dict[str, dict] = {}
        # 배치 처리 (최대 100종목씩)
        batches = [symbols[i:i + 100] for i in range(0, len(symbols), 100)]
        for batch in batches:
            rows = await self._fetch_rows(INTRADAY_COLUMNS, tickers=batch)
            for row in rows:
                sym = row.get("Ticker", "").strip()
                if not sym:
                    continue
                rsi = _flt(row.get("Relative Strength Index (14)", ""))
                rel_vol = _flt(row.get("Relative Volume", ""))
                price = _flt(row.get("Price", ""))
                change_pct = _pct(row.get("Change", ""))
                p5m  = _pct(row.get("Performance (5 Minutes)", ""))
                p15m = _pct(row.get("Performance (15 Minutes)", ""))
                p30m = _pct(row.get("Performance (30 Minutes)", ""))
                p1h  = _pct(row.get("Performance (1 Hour)", ""))

                # 장중 모멘텀 점수 계산 (0~100)
                ms = self._calc_intraday_momentum(
                    rsi, rel_vol, change_pct, p5m, p15m, p30m, p1h
                )
                result[sym] = {
                    "rsi":            rsi,
                    "rel_vol":        rel_vol,
                    "price":          price,
                    "change_pct":     change_pct,
                    "perf_5m":        p5m,
                    "perf_15m":       p15m,
                    "perf_30m":       p30m,
                    "perf_1h":        p1h,
                    "momentum_score": ms,
                }
            await asyncio.sleep(0.2)

        self._intraday_cache = result
        self._intraday_ts = time.time()
        logger.debug(f"[Finviz 장중] 스캔 완료: {len(result)}종목")
        return result

    @staticmethod
    def _calc_intraday_momentum(
        rsi: float, rel_vol: float, change_pct: float,
        p5m: float, p15m: float, p30m: float, p1h: float,
    ) -> float:
        """
        장중 모멘텀 종합 점수 (0~100).

        설계 원칙:
          - 다중 시간대 일관성: 5m/15m/30m/1h 모두 양수면 강한 신호
          - RSI 40~70: 모멘텀 존재하지만 과열 아님 → 최고점
          - RelVol >= 2: 거래량 서지 동반 → 강한 확신
        """
        score = 50.0  # 기본 중립

        # ① 단기 모멘텀 (5분/15분)
        if p5m >= 1.0:
            score += 12
        elif p5m >= 0.5:
            score += 7
        elif p5m >= 0.2:
            score += 3
        elif p5m <= -0.5:
            score -= 5

        if p15m >= 1.5:
            score += 10
        elif p15m >= 0.8:
            score += 6
        elif p15m >= 0.3:
            score += 3
        elif p15m <= -0.8:
            score -= 5

        # ② 중기 모멘텀 (30분/1시간)
        if p30m >= 2.0:
            score += 8
        elif p30m >= 1.0:
            score += 5
        elif p30m >= 0.3:
            score += 2
        elif p30m <= -1.0:
            score -= 5

        if p1h >= 3.0:
            score += 8
        elif p1h >= 1.5:
            score += 5
        elif p1h >= 0.5:
            score += 2
        elif p1h <= -1.5:
            score -= 6

        # ③ 다중 시간대 일관성 보너스 (모두 양수)
        positive_count = sum(1 for p in [p5m, p15m, p30m, p1h] if p > 0)
        if positive_count == 4:
            score += 8
        elif positive_count == 3:
            score += 4

        # ④ RSI (모멘텀 존재하되 과열 아님)
        if 45 <= rsi <= 65:
            score += 5  # 최적 모멘텀 존
        elif 65 < rsi <= 75:
            score += 2  # 약간 과열
        elif rsi > 75:
            score -= 5  # 과열
        elif rsi < 35:
            score -= 3  # 하락 중

        # ⑤ 거래량 서지
        if rel_vol >= 3.0:
            score += 8
        elif rel_vol >= 2.0:
            score += 5
        elif rel_vol >= 1.5:
            score += 2
        elif rel_vol < 0.5:
            score -= 5  # 거래량 없음

        return max(0.0, min(100.0, score))

    # ── 보너스 점수 ───────────────────────────────────────────────────────────

    def get_bonus_score(self, symbol: str) -> float:
        """
        StockScreener 기본 점수에 더할 Finviz 보너스.

        점수 체계 (최대 ~+90pt, 페널티 최대 ~-25pt):

          [A] 기관/내부자 수급     max +30pt
          [B] 실적 성장 모멘텀     max +25pt
          [C] 비즈니스 품질        max +20pt
          [D] 애널리스트 컨센서스   max +15pt
          [E] 밸류에이션/리스크     max -15pt (페널티 전용)
        """
        data = self._cache.get(symbol)
        if not data:
            return 0.0

        bonus = 0.0

        # ── [A] 기관/내부자 수급 (max +30pt) ──────────────────────────────
        inst_trans = _pct(data.get("Institutional Transactions", ""))
        if inst_trans >= 5:
            bonus += 25   # 기관 대규모 매집 — 최강 신호
        elif inst_trans >= 2:
            bonus += 15   # 기관 꾸준한 매수
        elif inst_trans >= 0.5:
            bonus += 8    # 소폭 매집
        elif inst_trans <= -5:
            bonus -= 12   # 기관 대규모 이탈 — 강한 경고
        elif inst_trans <= -2:
            bonus -= 6    # 기관 이탈

        insider_trans = _pct(data.get("Insider Transactions", ""))
        if insider_trans >= 5:
            bonus += 10   # 내부자 대량 매수 (강한 확신 신호)
        elif insider_trans >= 1:
            bonus += 5    # 내부자 매수
        # 내부자 매도는 유동성 목적이 많아 페널티 없음

        # ── [B] 실적 성장 모멘텀 (max +25pt) ─────────────────────────────
        eps_qq = _pct(data.get("EPS Growth Quarter Over Quarter", ""))
        if eps_qq >= 50:
            bonus += 10   # 폭발적 분기 실적
        elif eps_qq >= 20:
            bonus += 7
        elif eps_qq >= 0:
            bonus += 3

        eps_next_yr = _pct(data.get("EPS Growth Next Year", ""))
        # 현재 분기 실적이 급락 중이면 내년 전망 신뢰도 감소 → 보너스 절반 적용
        eps_next_yr_adj = eps_next_yr * 0.5 if eps_qq <= -30 else eps_next_yr
        if eps_next_yr_adj >= 30:
            bonus += 10   # 내년 강한 성장 전망
        elif eps_next_yr_adj >= 15:
            bonus += 6
        elif eps_next_yr_adj >= 0:
            bonus += 3
        elif eps_next_yr_adj <= -20:
            bonus -= 5    # 이익 역성장 전망

        # 분기실적 + 내년성장 모두 강하면 시너지 보너스 (+5pt)
        if eps_qq >= 30 and eps_next_yr >= 20:
            bonus += 5

        # ── [C] 비즈니스 품질 (max +20pt) ────────────────────────────────
        op_margin = _pct(data.get("Operating Margin", ""))
        if op_margin >= 30:
            bonus += 8    # 세계적 수준 영업이익률 (NVDA, META, MSFT 등)
        elif op_margin >= 15:
            bonus += 5
        elif op_margin >= 5:
            bonus += 2
        elif op_margin < 0:
            bonus -= 3    # 영업 적자 (경고)

        roe = _pct(data.get("Return on Equity", ""))
        if roe >= 50:
            bonus += 7    # 탁월한 자본효율성
        elif roe >= 20:
            bonus += 4
        elif roe >= 10:
            bonus += 2

        gross_margin = _pct(data.get("Gross Margin", ""))
        if gross_margin >= 60:
            bonus += 5    # 강력한 해자 (소프트웨어/반도체 설계 등)
        elif gross_margin >= 40:
            bonus += 3
        elif gross_margin >= 20:
            bonus += 1

        # ── [D] 애널리스트 컨센서스 (max +15pt) ─────────────────────────
        recom = _flt(data.get("Analyst Recom", ""))
        if 0 < recom <= 1.5:
            bonus += 5    # 강력 매수 의견
        elif recom <= 2.5:
            bonus += 2
        elif recom >= 4.0:
            bonus -= 5    # 매도 의견

        # 목표가 괴리율 (analyst upside)
        target = _flt(data.get("Target Price", ""))
        price = _flt(data.get("Price", ""))
        if target > 0 and price > 0:
            upside = (target / price - 1) * 100
            if upside >= 40:
                bonus += 10   # 애널리스트 대규모 상승 여지
            elif upside >= 25:
                bonus += 6
            elif upside >= 10:
                bonus += 2
            elif upside < -5:
                bonus -= 3    # 애널리스트 하향 목표가

        # ── [E] 밸류에이션/리스크 페널티 ────────────────────────────────
        fwd_pe = _flt(data.get("Forward P/E", ""))
        if fwd_pe > 100:
            bonus -= 10   # 극단적 고평가 (TSLA 153x 등)
        elif fwd_pe > 60:
            bonus -= 5    # 고평가

        # 고공매도 비율 (쇼트 스퀴즈 위험 or 시장 불신)
        short_float = _pct(data.get("Short Float", ""))
        if short_float >= 20:
            bonus -= 5    # 높은 공매도 = 시장이 하락 베팅 중

        return bonus

    # ── 전략별 시그널 ─────────────────────────────────────────────────────────

    def get_strategy_signals(self, symbol: str, strategy: str) -> dict:
        """
        전략별 맞춤 시그널/필터 반환.

        Args:
            strategy: "sepa" | "momentum" | "earnings_drift"

        Returns:
            {
              "pass": bool,          # 이 전략에 적합한 종목인지
              "score_adjustment": float,  # 전략 점수에 더할 조정값
              "reasons": [str],      # 통과/탈락 이유
              "data": dict           # 전략별 관련 수치
            }
        """
        data = self._cache.get(symbol, {})
        if not data:
            return {"pass": True, "score_adjustment": 0.0, "reasons": [], "data": {}}

        if strategy == "sepa":
            return self._sepa_signals(data)
        elif strategy == "momentum":
            return self._momentum_signals(data)
        elif strategy == "earnings_drift":
            return self._earnings_drift_signals(data)
        else:
            return {"pass": True, "score_adjustment": 0.0, "reasons": [], "data": {}}

    def _sepa_signals(self, data: dict) -> dict:
        """
        SEPA 전략 Finviz 보완 시그널.

        SEPA (Stan Weinstein Trend Following)는 성장 기업의 추세 돌파를 포착.
        Finviz 데이터로 '추세 돌파할 가치 있는 기업인가' 를 검증:
          - 이익 성장 중인가? (EPS 성장)
          - 수익성 있는 비즈니스인가? (Operating Margin > 0)
          - 애널리스트 상승 여지가 있는가? (Target Price)
          - 극단적 고평가는 아닌가? (Forward P/E < 100)
        """
        reasons = []
        score_adj = 0.0
        warnings = []

        # ① 이익 성장 필터
        eps_next_yr = _pct(data.get("EPS Growth Next Year", ""))
        eps_qq = _pct(data.get("EPS Growth Quarter Over Quarter", ""))
        # 현재 분기 급락 중이면 내년 전망 신뢰도 절반
        eps_next_yr_adj = eps_next_yr * 0.5 if eps_qq <= -30 else eps_next_yr
        if eps_next_yr_adj >= 20:
            score_adj += 8
            reasons.append(f"내년 EPS 성장 {eps_next_yr:.1f}%")
        elif eps_next_yr_adj >= 0:
            score_adj += 3
        elif eps_next_yr_adj <= -20:
            score_adj -= 8
            warnings.append(f"내년 EPS 역성장 전망 {eps_next_yr:.1f}%")

        # ② 영업이익률 (soft filter — 0% 이상이면 통과)
        op_margin = _pct(data.get("Operating Margin", ""))
        if op_margin >= 20:
            score_adj += 6
            reasons.append(f"영업이익률 {op_margin:.1f}%")
        elif op_margin >= 5:
            score_adj += 2
        elif op_margin < 0:
            score_adj -= 5
            warnings.append(f"영업 적자 ({op_margin:.1f}%)")

        # ③ 목표가 상승 여지
        target = _flt(data.get("Target Price", ""))
        price = _flt(data.get("Price", ""))
        if target > 0 and price > 0:
            upside = (target / price - 1) * 100
            if upside >= 25:
                score_adj += 7
                reasons.append(f"목표가 상승여지 {upside:.1f}%")
            elif upside >= 10:
                score_adj += 3

        # ④ Forward P/E 극단 고평가 경고
        fwd_pe = _flt(data.get("Forward P/E", ""))
        if fwd_pe > 100:
            score_adj -= 8
            warnings.append(f"과도한 고평가 Fwd P/E={fwd_pe:.1f}x")

        # ⑤ 기관 매집 확인
        inst_trans = _pct(data.get("Institutional Transactions", ""))
        if inst_trans >= 2:
            score_adj += 5
            reasons.append(f"기관 매집 {inst_trans:.2f}%")

        passed = score_adj >= -5  # 심각한 마이너스가 아니면 통과

        return {
            "pass": passed,
            "score_adjustment": score_adj,
            "reasons": reasons,
            "warnings": warnings,
            "data": {
                "eps_next_yr": eps_next_yr,
                "eps_next_yr_adj": eps_next_yr_adj,
                "eps_qq": eps_qq,
                "op_margin": op_margin,
                "fwd_pe": fwd_pe,
                "target_upside": ((target / price - 1) * 100) if target > 0 and price > 0 else None,
                "inst_trans": inst_trans,
            },
        }

    def _momentum_signals(self, data: dict) -> dict:
        """
        Momentum 전략 Finviz 보완 시그널.

        모멘텀 전략은 기술적 돌파를 따르지만, Finviz로 아래를 확인:
          - 실적이 완전 추락 중인 종목 제외 (EPS QQ < -50%)
          - 애널리스트 지지 여부 (Analyst Recom)
          - 상대 거래량 확인 (Relative Volume)
        """
        reasons = []
        score_adj = 0.0
        warnings = []

        # ① EPS 급감 필터 (earnings trap 방지)
        eps_qq = _pct(data.get("EPS Growth Quarter Over Quarter", ""))
        if eps_qq >= 20:
            score_adj += 5
            reasons.append(f"EPS 성장 {eps_qq:.1f}%")
        elif eps_qq <= -50:
            score_adj -= 8
            warnings.append(f"EPS 급감 {eps_qq:.1f}% — 실적 악화 모멘텀 함정 위험")

        # ② Finviz 상대 거래량 (자체 vol_ratio 보완)
        rel_vol = _flt(data.get("Relative Volume", ""))
        if rel_vol >= 2.5:
            score_adj += 8
            reasons.append(f"RelVol {rel_vol:.1f}x 강한 거래량 서지")
        elif rel_vol >= 1.5:
            score_adj += 4
        elif rel_vol < 0.5:
            score_adj -= 5
            warnings.append(f"RelVol {rel_vol:.1f}x 거래량 부재")

        # ③ 기관 매집 방향
        inst_trans = _pct(data.get("Institutional Transactions", ""))
        if inst_trans >= 2:
            score_adj += 5
        elif inst_trans <= -3:
            score_adj -= 5
            warnings.append(f"기관 이탈 {inst_trans:.2f}%")

        # ④ 공매도 비율 (스퀴즈 포텐셜 체크)
        short_float = _pct(data.get("Short Float", ""))
        if short_float >= 15:
            score_adj += 3   # 고공매도 → 쇼트 스퀴즈 시 강한 상승
            reasons.append(f"공매도 {short_float:.1f}% — 스퀴즈 포텐셜")

        return {
            "pass": score_adj >= -5,
            "score_adjustment": score_adj,
            "reasons": reasons,
            "warnings": warnings,
            "data": {
                "eps_qq": eps_qq,
                "rel_vol": rel_vol,
                "inst_trans": inst_trans,
                "short_float": short_float,
            },
        }

    def _earnings_drift_signals(self, data: dict) -> dict:
        """
        EarningsDrift 전략 Finviz 보완 시그널.

        어닝 후 갭업 종목에 대해 Finviz로:
          - 실적의 질 검증 (EPS QQ + EPS Next Q 양수인가)
          - 기관이 결과를 어떻게 소화했는가 (Inst Trans)
          - 애널리스트 목표가 상향 여부 (Target Price)
        """
        reasons = []
        score_adj = 0.0
        warnings = []

        # ① 분기 EPS 성장 (어닝 서프라이즈 강도 proxy)
        eps_qq = _pct(data.get("EPS Growth Quarter Over Quarter", ""))
        if eps_qq >= 50:
            score_adj += 12
            reasons.append(f"EPS QQ +{eps_qq:.1f}% 강한 어닝 서프라이즈")
        elif eps_qq >= 20:
            score_adj += 7
        elif eps_qq >= 0:
            score_adj += 3
        elif eps_qq <= -20:
            score_adj -= 8
            warnings.append(f"EPS QQ {eps_qq:.1f}% — 실적 악화")

        # ② EPS Next Q (다음 분기 전망)
        eps_next_q_raw = data.get("EPS Next Q", "")
        eps_next_q = _flt(eps_next_q_raw)
        if eps_next_q > 0:
            score_adj += 5
            reasons.append(f"EPS Next Q ${eps_next_q:.2f} 양수 전망")
        elif eps_next_q < 0:
            score_adj -= 5
            warnings.append(f"EPS Next Q ${eps_next_q:.2f} 음수 — 다음 분기 손실 예상")

        # ③ 기관 반응 (어닝 발표 후 기관이 샀는가)
        inst_trans = _pct(data.get("Institutional Transactions", ""))
        if inst_trans >= 3:
            score_adj += 8
            reasons.append(f"기관 어닝 후 매집 {inst_trans:.2f}%")
        elif inst_trans >= 1:
            score_adj += 4
        elif inst_trans <= -3:
            score_adj -= 6
            warnings.append(f"기관 어닝 후 이탈 {inst_trans:.2f}%")

        # ④ 목표가 상향 여지 (애널리스트 추가 상승 여지)
        target = _flt(data.get("Target Price", ""))
        price = _flt(data.get("Price", ""))
        if target > 0 and price > 0:
            upside = (target / price - 1) * 100
            if upside >= 30:
                score_adj += 6
                reasons.append(f"애널리스트 목표가 상승여지 {upside:.1f}%")

        return {
            "pass": score_adj >= -5,
            "score_adjustment": score_adj,
            "reasons": reasons,
            "warnings": warnings,
            "data": {
                "eps_qq": eps_qq,
                "eps_next_q": eps_next_q,
                "inst_trans": inst_trans,
                "target_upside": ((target / price - 1) * 100) if target > 0 and price > 0 else None,
            },
        }

    # ── 리스크 포지션 사이징 ─────────────────────────────────────────────────

    def get_risk_multiplier(self, symbol: str) -> Tuple[float, str]:
        """
        Beta 기반 포지션 사이징 보정 계수 반환.

        RiskManager.calculate_position_size() 결과에 곱해서 사용:
          qty_adj = floor(qty * multiplier)

        Returns:
            (multiplier: float, reason: str)
              1.0 = 기본 (Beta 1.0~1.5)
              0.9 = 소폭 감소 (Beta 1.5~2.0)
              0.8 = 감소 (Beta 2.0~2.5)
              0.7 = 상당히 감소 (Beta > 2.5)
        """
        data = self._cache.get(symbol, {})
        if not data:
            return 1.0, "Finviz 데이터 없음"

        beta = _flt(data.get("Beta", ""))
        if beta <= 0:
            return 1.0, f"Beta 데이터 없음"

        if beta > 2.5:
            return 0.7, f"고위험 Beta={beta:.2f} → 포지션 30% 축소"
        elif beta > 2.0:
            return 0.8, f"Beta={beta:.2f} → 포지션 20% 축소"
        elif beta > 1.5:
            return 0.9, f"Beta={beta:.2f} → 포지션 10% 축소"
        else:
            return 1.0, f"Beta={beta:.2f} 정상"

    def get_atr(self, symbol: str) -> float:
        """
        Finviz ATR 값 반환 (달러 단위).
        손절가 정밀 설정에 활용 (예: 진입가 - 1.5×ATR).
        """
        data = self._cache.get(symbol, {})
        return _flt(data.get("Average True Range", ""))

    def get_target_upside(self, symbol: str) -> Optional[float]:
        """애널리스트 목표가 대비 현재가 상승 여지 (%). None이면 데이터 없음."""
        data = self._cache.get(symbol, {})
        target = _flt(data.get("Target Price", ""))
        price = _flt(data.get("Price", ""))
        if target > 0 and price > 0:
            return (target / price - 1) * 100
        return None

    # ── 종합 메타데이터 ───────────────────────────────────────────────────────

    def get_meta(self, symbol: str) -> dict:
        """
        전체 Finviz 메타데이터 반환 (대시보드/로그 표시용).
        """
        data = self._cache.get(symbol, {})
        if not data:
            return {}

        target = _flt(data.get("Target Price", ""))
        price = _flt(data.get("Price", ""))
        target_upside = ((target / price - 1) * 100) if target > 0 and price > 0 else None

        return {
            # 수급
            "inst_own":       _pct(data.get("Institutional Ownership", "")),
            "inst_trans":     _pct(data.get("Institutional Transactions", "")),
            "insider_trans":  _pct(data.get("Insider Transactions", "")),
            "short_float":    _pct(data.get("Short Float", "")),
            # 실적 성장
            "eps_qq":         _pct(data.get("EPS Growth Quarter Over Quarter", "")),
            "eps_this_yr":    _pct(data.get("EPS Growth This Year", "")),
            "eps_next_yr":    _pct(data.get("EPS Growth Next Year", "")),
            "eps_next_q":     _flt(data.get("EPS Next Q", "")),
            "sales_qq":       _pct(data.get("Sales Growth Quarter Over Quarter", "")),
            # 비즈니스 품질
            "gross_margin":   _pct(data.get("Gross Margin", "")),
            "op_margin":      _pct(data.get("Operating Margin", "")),
            "profit_margin":  _pct(data.get("Profit Margin", "")),
            "roe":            _pct(data.get("Return on Equity", "")),
            # 밸류에이션/리스크
            "fwd_pe":         _flt(data.get("Forward P/E", "")),
            "beta":           _flt(data.get("Beta", "")),
            "atr":            _flt(data.get("Average True Range", "")),
            # 기술지표
            "rsi":            _flt(data.get("Relative Strength Index (14)", "")),
            "rel_vol":        _flt(data.get("Relative Volume", "")),
            "pct_52w_high":   _pct(data.get("52-Week High", "")),
            # 애널리스트
            "analyst_recom":  _flt(data.get("Analyst Recom", "")),
            "target_price":   target,
            "target_upside":  target_upside,
            # 종합 보너스
            "bonus":          self.get_bonus_score(symbol),
        }

    def coverage(self) -> int:
        """현재 캐시 종목 수"""
        return len(self._cache)
