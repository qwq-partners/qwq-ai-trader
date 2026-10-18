"""
AI Trading Bot v2 - 5일 누적 수급 스코어 제공자

pykrx market-wide 수급 데이터를 5 영업일 분 축적해 종목별 정규화 점수(0~100) 산출.
기존 SupplyTrendDetector(80종목 한정)를 보완해 KOSPI+KOSDAQ 전종목 커버.

점수 구성:
  외국인 연속 순매수 일수  → 최대 +30pt (5일↑=30 / 3일↑=20 / 1일↑=8)
  기관 연속 순매수 일수    → 최대 +25pt (5일↑=25 / 3일↑=15 / 1일↑=6)
  동시 매집 보너스        → +15pt (외국인+기관 3일↑ 동시)
  규모 보너스             → 최대 +15pt (5일 누적 주수 기준)
  가속 보너스             → +15pt (최근 2일 > 이전 3일)

캐시: ~/.cache/ai_trader/supply_daily_v2_YYYYMMDD.json (날짜 검증 일별 저장)
갱신: 15:40 장 마감 후 / 또는 08:15 아침 스캔 전 (전일 데이터)
"""

import asyncio
import json
import math
import numbers
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from loguru import logger
from src.utils.atomic_io import atomic_write_json


CACHE_DIR = Path.home() / ".cache" / "ai_trader"
LOOKBACK_DAYS = 5       # 5 영업일


def _get_trading_dates(n: int) -> List[str]:
    """최근 n 영업일 날짜 목록 반환 (오늘 제외, 최신→오래된 순, 공휴일 제외)"""
    from src.core.engine import is_kr_market_holiday
    dates = []
    d = datetime.now(ZoneInfo("Asia/Seoul")).date() - timedelta(days=1)
    while len(dates) < n:
        if d.weekday() < 5 and not is_kr_market_holiday(d):
            dates.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    return dates  # 최신→오래된 순


class SupplyScoreProvider:
    """
    5일 누적 외국인/기관 수급 스코어 제공자.

    사용 예:
        provider = SupplyScoreProvider()
        await provider.ensure_loaded()          # 데이터 확보
        score = provider.get_score("005930")    # 0~100점
        meta  = provider.get_meta("005930")     # 상세 정보
    """

    def __init__(self):
        # {date_str: {symbol: {"foreign": int, "inst": int}}}
        self._daily: Dict[str, Dict[str, Dict[str, int]]] = {}
        self._expected_dates: List[str] = []
        self._loaded_dates: List[str] = []   # 로드된 날짜 목록 (최신→오래된 순)
        self._score_cache: Dict[str, float] = {}    # 계산 결과 캐시
        self._meta_cache: Dict[str, dict] = {}
        self._ready = False

    # ── 캐시 IO ───────────────────────────────────────────────────────────────

    def _cache_path(self, date_str: str) -> Path:
        if type(date_str) is not str or re.fullmatch(r"[0-9]{8}", date_str) is None:
            raise ValueError("invalid supply date")
        datetime.strptime(date_str, "%Y%m%d")
        return CACHE_DIR / f"supply_daily_v2_{date_str}.json"

    @staticmethod
    def _valid_day(data):
        from src.analytics.selection_supply_policy import validated_quantity
        if type(data) is not dict or len(data) > 10000:
            return {}
        result = {}
        for symbol, row in data.items():
            if (type(symbol) is not str or re.fullmatch(r"[0-9]{6}", symbol) is None
                    or symbol == "000000" or type(row) is not dict):
                continue
            values = {side: validated_quantity(row.get(side)) for side in ("foreign", "inst")}
            if any(value is not None for value in values.values()):
                result[symbol] = values
        return result

    def _save_day(self, date_str: str, data, *, source: str):
        # Separate names preserve old files without trusting possibly relabeled
        # snapshots written by earlier versions as verified daily history.
        if source not in ("FHKST01010900", "pykrx_exact_date"):
            raise ValueError("unverified history source")
        try:
            path = self._cache_path(date_str)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(path, {"version": "supply-daily-v2", "source_date": date_str,
                                     "source": source, "data": self._valid_day(data)})
        except Exception as e:
            logger.debug(f"[수급5일] 캐시 저장 실패 {date_str}: {type(e).__name__}")

    def _load_day(self, date_str: str) -> Optional[Dict[str, Dict[str, int]]]:
        path = self._cache_path(date_str)
        if path.exists():
            try:
                raw = json.loads(path.read_text())
                if (type(raw) is dict and raw.get("version") == "supply-daily-v2"
                        and raw.get("source_date") == date_str
                        and raw.get("source") in ("FHKST01010900", "pykrx_exact_date")):
                    return self._valid_day(raw.get("data")) or None
            except Exception:
                pass
        return None

    @staticmethod
    def _kis_day(rows, date_str):
        from src.analytics.selection_supply_policy import validated_quantity
        if type(rows) is not dict or len(rows) > 10000:
            return {}
        day = datetime.strptime(date_str, "%Y%m%d").date().isoformat()
        result = {}
        for symbol, row in rows.items():
            if type(row) is not dict or type(row.get("_supply_evidence")) is not dict:
                continue
            values = {}
            for side, evidence_side, numeric in (("foreign", "foreign", "foreign_net_buy"),
                                                  ("inst", "institution", "inst_net_buy")):
                proof = row["_supply_evidence"].get(evidence_side)
                value = None
                try:
                    stamp = datetime.fromisoformat(proof["captured_at"])
                    if (proof.get("source") == "FHKST01010900" and proof.get("status") == "observed"
                            and proof.get("source_date") == day and stamp.tzinfo is not None
                            and stamp <= datetime.now(timezone.utc)
                            and day <= stamp.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
                            and type(proof.get("value")) is int
                            and proof["value"] == validated_quantity(row.get(numeric))):
                        value = validated_quantity(proof["value"])
                except (TypeError, KeyError, ValueError, AttributeError):
                    pass
                values[side] = value
            result[symbol] = values
        return SupplyScoreProvider._valid_day(result)

    def _finish_window(self, dates):
        self._expected_dates = list(dates)
        self._daily = {day: rows for day, rows in self._daily.items() if day in dates}
        self._loaded_dates = [day for day in dates if self._daily.get(day)]
        self._score_cache.clear()
        self._meta_cache.clear()
        self._ready = bool(self._loaded_dates)

    @property
    def is_ready(self) -> bool:
        return self._ready and bool(self._daily)

    # ── 데이터 로드 / 갱신 ────────────────────────────────────────────────────

    async def ensure_loaded_from_kis(
        self, kis_market_data, symbols: List[str]
    ) -> bool:
        """
        KIS API(FHKST01010900)로 5일치 수급 데이터 로드.
        KIS 미수록 날짜는 기존 pykrx 날짜 조회로만 폴백한다.

        최적화:
        - 검증된 v2 캐시 날짜는 즉시 로드하고 KIS 호출 생략
        - 캐시 미스 날짜들은 asyncio.gather로 병렬 조회
          (concurrency=5로 KIS rate limit 준수, 총 동시 요청 ≤ 25)

        Args:
            kis_market_data: KISMarketData 인스턴스
            symbols:         조회할 종목코드 목록 (SEPA 후보 등)
        Returns:
            True if data loaded
        """
        dates = _get_trading_dates(LOOKBACK_DAYS)

        # 1) 캐시 우선 로드 (파일 있는 날짜는 KIS 호출 불필요)
        missing_dates = []
        for date_str in dates:
            if date_str in self._daily:
                continue
            cached = self._load_day(date_str)
            if cached:
                self._daily[date_str] = cached
            else:
                missing_dates.append(date_str)

        # 2) 캐시 미스 날짜를 병렬로 KIS 조회
        # fetch_stock_investor_daily가 days=30으로 전체 캐시 → 날짜별 재호출 시 캐시 히트
        # concurrency=5(날짜별) × 최대 5날짜 = 최대 25 동시 요청 (KIS 20/s 이내)
        if missing_dates:
            logger.info(
                f"[수급5일] KIS 병렬 조회: {missing_dates} ({len(symbols)}종목)"
            )

            async def _fetch_date(date_str: str):
                try:
                    kis_result = await kis_market_data.fetch_batch_investor_daily(
                        symbols, date_str, concurrency=5
                    )
                    if kis_result:
                        day_data = self._kis_day(kis_result, date_str)
                        if day_data:
                            self._daily[date_str] = day_data
                            self._save_day(date_str, day_data, source="FHKST01010900")
                        logger.info(
                            f"[수급5일] KIS 로드 완료: {date_str} {len(day_data)}종목"
                        )
                    else:
                        logger.warning(f"[수급5일] KIS {date_str} 0종목 반환")
                except Exception as e:
                    logger.warning(f"[수급5일] KIS {date_str} 조회 실패: {e}")

            await asyncio.gather(*[_fetch_date(d) for d in missing_dates])

        self._finish_window(dates)

        # KIS로 못 채운 날짜는 검증된 일별 캐시/pykrx 조회 (ensure_loaded 위임)
        still_missing = [d for d in dates if d not in self._daily]
        if still_missing:
            logger.info(
                f"[수급5일] KIS 미수록 날짜 → 일별 자료 조회: {still_missing}"
            )
            await self.ensure_loaded(_window_dates=dates)

        return self._ready

    async def ensure_loaded(self, force_refresh_today: bool = False, *, _window_dates=None) -> bool:
        """
        5일치 데이터 확보 (캐시 우선, 없으면 pykrx 호출).

        Args:
            force_refresh_today: True이면 가장 최근 완료 거래일 데이터 강제 재조회
        Returns:
            True if data is ready
        """
        # Preserve the KIS request window even if midnight passes during I/O.
        dates = list(_window_dates) if _window_dates is not None else _get_trading_dates(LOOKBACK_DAYS)
        # force_refresh_today: 가장 최신 날짜(오늘의 전일)를 재조회
        if force_refresh_today and dates:
            self._daily.pop(dates[0], None)  # 메모리에서 제거 → 재조회 유도

        for date_str in dates:
            if date_str in self._daily:
                continue  # 이미 로드됨

            # 캐시 파일 확인 (force_refresh_today=True이면 최신 날짜 캐시 무시)
            if force_refresh_today and date_str == dates[0]:
                pass  # 캐시 스킵, 바로 pykrx 조회
            else:
                cached = self._load_day(date_str)
                if cached:
                    self._daily[date_str] = cached
                    logger.debug(f"[수급5일] 캐시 로드: {date_str} {len(cached)}종목")
                    continue

            # pykrx 조회 (KRX API가 인증 차단 시 0종목 반환)
            logger.info(f"[수급5일] pykrx 조회: {date_str}")
            day_data: Dict[str, Dict[str, int]] = {}
            try:
                day_data = await asyncio.to_thread(
                    self._fetch_pykrx_day, date_str
                )
            except Exception as e:
                logger.warning(f"[수급5일] pykrx {date_str} 조회 실패: {e}")

            # Intraday snapshots cannot establish exact-date daily observations.
            # A missing date remains missing, rather than copying an older day.
            day_data = self._valid_day(day_data)

            if day_data:
                self._daily[date_str] = day_data
                self._save_day(date_str, day_data, source="pykrx_exact_date")
                logger.info(f"[수급5일] 로드 완료: {date_str} {len(day_data)}종목")
            else:
                logger.warning(f"[수급5일] {date_str} 데이터 없음 (pykrx+캐시 모두 실패)")

        self._finish_window(dates)
        if self._ready:
            logger.info(f"[수급5일] 날짜 확인 자료: {len(self._loaded_dates)}/{len(dates)}일")
        return self._ready

    @staticmethod
    def _fetch_pykrx_day(date_str: str) -> Dict[str, Dict[str, int]]:
        """
        pykrx로 특정 날짜의 KOSPI+KOSDAQ 전종목 외국인/기관 순매수 조회.
        동기 함수 — asyncio.to_thread()로 호출할 것.
        """
        import pykrx.stock as pykrx_stock
        from src.analytics.selection_supply_policy import validated_quantity

        result: Dict[str, Dict[str, int]] = {}
        configs = [
            ("KOSPI",  "외국인", "foreign"),
            ("KOSDAQ", "외국인", "foreign"),
            ("KOSPI",  "기관합계", "inst"),
            ("KOSDAQ", "기관합계", "inst"),
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
                        result[sym] = {}
                    raw = row.get("순매수거래량")
                    if isinstance(raw, numbers.Integral) and not isinstance(raw, bool):
                        raw = int(raw)
                    elif isinstance(raw, numbers.Real) and not isinstance(raw, bool):
                        # pandas promotes an integer column containing NaN to
                        # float64. Recover only exact finite integral values at
                        # this dataframe boundary; never truncate fractions.
                        raw = int(raw) if math.isfinite(raw) and float(raw).is_integer() else None
                    value = validated_quantity(raw)
                    if field in result[sym] and result[sym][field] != value:
                        result[sym][field] = None
                    elif field not in result[sym]:
                        result[sym][field] = value
            except Exception as e:
                logger.debug(f"[수급5일] pykrx {market}/{investor} {date_str}: {e}")

        return result

    # ── 점수 계산 ─────────────────────────────────────────────────────────────

    def _compute(self, symbol: str) -> Tuple[float, dict]:
        """
        종목별 5일 수급 점수 계산.

        Returns:
            (score: float, meta: dict)
        """
        if not self._loaded_dates:
            return 0.0, {}

        # 요청 거래일 순서: 최신→오래된. 자료가 없는 날짜도 제거하지 않는다.
        foreign_series = []
        inst_series = []
        for date_str in self._expected_dates:
            day = self._daily.get(date_str, {}).get(symbol, {})
            foreign_series.append(day.get("foreign"))
            inst_series.append(day.get("inst"))

        # 연속 순매수 일수 (최신부터 역산)
        def streak(series: List[int]) -> int:
            count = 0
            for v in series:
                if v is not None and v > 0:
                    count += 1
                else:
                    break
            return count

        foreign_streak = streak(foreign_series)
        inst_streak = streak(inst_series)

        # 5일 누적
        def complete_sum(series):
            return sum(series) if len(series) == LOOKBACK_DAYS and all(v is not None for v in series) else None

        foreign_5d = complete_sum(foreign_series)
        inst_5d = complete_sum(inst_series)

        # 가속 여부: 최근 2일 합 > 이전 3일 합
        is_accelerating = False
        if foreign_5d is not None:
            recent_2 = foreign_series[0] + foreign_series[1]
            prev_3 = sum(foreign_series[2:min(5, len(foreign_series))])
            if recent_2 > 0 and prev_3 > 0:
                is_accelerating = recent_2 > prev_3 * 0.8  # 최근이 과거보다 강하면

        # ── 점수 산출 ──────────────────────────────────────────────────────
        score = 0.0

        # 외국인 연속 순매수 (최대 30pt)
        if foreign_streak >= 5:
            score += 30
        elif foreign_streak >= 3:
            score += 20
        elif foreign_streak >= 1:
            score += 8

        # 기관 연속 순매수 (최대 25pt)
        if inst_streak >= 5:
            score += 25
        elif inst_streak >= 3:
            score += 15
        elif inst_streak >= 1:
            score += 6

        # 동시 매집 보너스 (최대 15pt)
        if foreign_streak >= 3 and inst_streak >= 3:
            score += 15
        elif foreign_streak >= 2 and inst_streak >= 2:
            score += 8

        # 규모 보너스: 5일 누적 (최대 15pt)
        total_5d = foreign_5d + inst_5d if foreign_5d is not None and inst_5d is not None else None
        if total_5d is not None and total_5d > 0:
            score += min(15.0, total_5d / 50_000)  # 5만 주당 1pt

        # 가속 보너스 (15pt)
        if is_accelerating:
            score += 15

        score = min(100.0, max(0.0, score))

        meta = {
            "foreign_streak": foreign_streak,
            "inst_streak":    inst_streak,
            "foreign_5d":     foreign_5d,
            "inst_5d":        inst_5d,
            "total_5d":       total_5d,
            "is_accelerating": is_accelerating,
            "score":          score,
            "days_loaded":    len(self._loaded_dates),
            "days_expected": len(self._expected_dates),
            "days_observed": {"foreign": sum(v is not None for v in foreign_series),
                              "inst": sum(v is not None for v in inst_series)},
        }
        return score, meta

    def get_score(self, symbol: str) -> float:
        """종목 5일 수급 점수 (0~100). 데이터 없으면 0."""
        if not self.is_ready:
            return 0.0
        if symbol not in self._score_cache:
            score, meta = self._compute(symbol)
            self._score_cache[symbol] = score
            self._meta_cache[symbol] = meta
        return self._score_cache[symbol]

    def get_meta(self, symbol: str) -> dict:
        """종목 5일 수급 상세 정보."""
        if not self.is_ready:
            return {}
        if symbol not in self._meta_cache:
            self.get_score(symbol)  # 계산 트리거
        return self._meta_cache.get(symbol, {})

    def get_bonus(self, symbol: str, max_bonus: float = 15.0) -> float:
        """
        스크리너/전략 점수에 더할 보너스.

        score 100 → max_bonus pt 선형 매핑.
        외국인 연속 3일↑ AND 기관 연속 3일↑ 면 동시 매집 추가 보너스.
        """
        score = self.get_score(symbol)
        if score <= 0:
            return 0.0
        meta = self._meta_cache.get(symbol, {})

        # 선형 매핑
        bonus = (score / 100.0) * max_bonus

        # 외국인+기관 동시 3일 이상이면 추가 5pt
        if meta.get("foreign_streak", 0) >= 3 and meta.get("inst_streak", 0) >= 3:
            bonus += 5.0

        return round(min(bonus, max_bonus + 5), 1)

    def top_n(self, n: int = 30) -> List[dict]:
        """점수 상위 N개 종목 반환 (대시보드/로그용)."""
        if not self.is_ready:
            return []
        # 전체 종목 집합
        all_syms: set = set()
        for day_data in self._daily.values():
            all_syms.update(day_data.keys())

        results = []
        for sym in all_syms:
            score = self.get_score(sym)
            if score > 0:
                meta = self.get_meta(sym)
                results.append({"symbol": sym, **meta})

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:n]
