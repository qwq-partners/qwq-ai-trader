"""
AI Trading Bot v2 - KIS 시장 데이터 조회 (조회 전용 REST API)

KIS 공식 API 5종을 모아서 관리합니다:
1. 휴장일 조회 (CTCA0903R)
2. 업종지수 조회 (FHPUP02140000)
3. 등락률 순위 (FHPST01700000)
4. 외국인/기관 매매 (FHPTJ04400000)
5. 시가총액/밸류에이션 순위 (FHPST01790000)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional, Set

import aiohttp
from loguru import logger

from src.utils.token_manager import get_token_manager


class KISMarketData:
    """KIS 시장 데이터 조회 (조회 전용 REST API)"""

    def __init__(self, token_manager=None):
        self._token_manager = token_manager or get_token_manager()
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: Dict[str, Any] = {}
        self._cache_ts: Dict[str, datetime] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _get_headers(self, tr_id: str) -> Dict[str, str]:
        token = await self._token_manager.get_access_token()
        return {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self._token_manager.app_key,
            "appsecret": self._token_manager.app_secret,
            "tr_id": tr_id,
        }

    def _is_cache_valid(self, key: str, ttl_seconds: int) -> bool:
        if key not in self._cache or key not in self._cache_ts:
            return False
        elapsed = (datetime.now() - self._cache_ts[key]).total_seconds()
        return elapsed < ttl_seconds

    def _set_cache(self, key: str, value: Any):
        self._cache[key] = value
        self._cache_ts[key] = datetime.now()

    # ============================================================
    # 1. 휴장일 조회 (CTCA0903R)
    # ============================================================

    async def fetch_holidays(self, year_month: str = "") -> Set[date]:
        """
        한국 시장 휴장일 조회

        Args:
            year_month: YYYYMM 형식 (비어 있으면 당월)

        Returns:
            휴장일 set (date 객체)

        API 주의: 1일 1회 호출 권장
        """
        if not year_month:
            year_month = datetime.now().strftime("%Y%m")

        cache_key = f"holidays_{year_month}"
        if self._is_cache_valid(cache_key, 86400):  # 1일 캐시
            return self._cache[cache_key]

        holidays: Set[date] = set()

        try:
            session = await self._get_session()
            headers = await self._get_headers("CTCA0903R")

            url = f"{self._token_manager.base_url}/uapi/domestic-stock/v1/quotations/chk-holiday"

            params = {
                "BASS_DT": f"{year_month}01",
                "CTX_AREA_NK": "",
                "CTX_AREA_FK": "",
            }

            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    logger.error(f"휴장일 조회 실패: HTTP {resp.status}")
                    return holidays

                data = await resp.json()

                if data.get("rt_cd") != "0":
                    logger.warning(f"휴장일 API 오류: {data.get('msg1')}")
                    return holidays

                output = data.get("output", [])

                for item in output:
                    bass_dt = item.get("bass_dt", "")
                    opnd_yn = item.get("opnd_yn", "")

                    if bass_dt and opnd_yn == "N":
                        try:
                            d = datetime.strptime(bass_dt, "%Y%m%d").date()
                            holidays.add(d)
                        except ValueError:
                            continue

            self._set_cache(cache_key, holidays)
            logger.info(f"[KISMarketData] 휴장일 조회 완료: {year_month} → {len(holidays)}일")
            return holidays

        except Exception as e:
            logger.error(f"휴장일 조회 오류: {e}")
            return holidays

    # ============================================================
    # 2. 업종지수 조회 (FHPUP02140000)
    # ============================================================

    async def fetch_sector_indices(self, market: str = "K") -> List[Dict]:
        """
        업종별 지수 조회

        Args:
            market: K=코스피, Q=코스닥

        Returns:
            [{업종명, 지수, 등락률, 거래량, 상승종목수, 하락종목수, ...}]
        """
        cache_key = f"sector_indices_{market}"
        if self._is_cache_valid(cache_key, 600):  # 10분 캐시
            return self._cache[cache_key]

        result: List[Dict] = []

        try:
            session = await self._get_session()
            headers = await self._get_headers("FHPUP02140000")

            url = f"{self._token_manager.base_url}/uapi/domestic-stock/v1/quotations/inquire-index-category-price"

            params = {
                "FID_COND_MRKT_DIV_CODE": "U",
                "FID_INPUT_ISCD": "0001",
                "FID_COND_SCR_DIV_CODE": "20214",
                "FID_MRKT_CLS_CODE": market,
                "FID_BLNG_CLS_CODE": "0",
            }

            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    logger.error(f"업종지수 조회 실패: HTTP {resp.status}")
                    return result

                data = await resp.json()

                if data.get("rt_cd") != "0":
                    logger.warning(f"업종지수 API 오류: {data.get('msg1')}")
                    return result

                # 업종지수 응답은 output2에 담김 (output1은 헤더 정보)
                output = data.get("output2", []) or data.get("output", [])

                for item in output:
                    sector_name = item.get("hts_kor_isnm", "").strip()
                    if not sector_name:
                        continue

                    result.append({
                        "name": sector_name,
                        "index": float(item.get("bstp_nmix_prpr", 0) or 0),
                        "change": float(item.get("bstp_nmix_prdy_vrss", 0) or 0),
                        "change_pct": float(item.get("bstp_nmix_prdy_ctrt", 0) or 0),
                        "volume": int(item.get("acml_vol", 0) or 0),
                        "trade_value": int(item.get("acml_tr_pbmn", 0) or 0),
                    })

            self._set_cache(cache_key, result)
            logger.info(f"[KISMarketData] 업종지수 조회 완료: {market} → {len(result)}개 업종")
            return result

        except Exception as e:
            logger.error(f"업종지수 조회 오류: {e}")
            return result

    # ============================================================
    # 3. 등락률 순위 (FHPST01700000)
    # ============================================================

    async def fetch_fluctuation_rank(self, limit: int = 30) -> List[Dict]:
        """
        등락률 순위 조회

        Returns:
            [{종목코드, 종목명, 현재가, 등락률, 거래량, ...}]
        """
        cache_key = "fluctuation_rank"
        if self._is_cache_valid(cache_key, 300):  # 5분 캐시
            return self._cache[cache_key][:limit]

        result: List[Dict] = []

        try:
            session = await self._get_session()
            headers = await self._get_headers("FHPST01700000")

            url = f"{self._token_manager.base_url}/uapi/domestic-stock/v1/ranking/fluctuation"

            params = {
                "fid_cond_mrkt_div_code": "J",
                "fid_cond_scr_div_code": "20170",
                "fid_input_iscd": "0000",
                "fid_rank_sort_cls_code": "0",  # 상승률순
                "fid_input_cnt_1": "0",
                "fid_prc_cls_code": "0",
                "fid_input_price_1": "",
                "fid_input_price_2": "",
                "fid_vol_cnt": "",
                "fid_trgt_cls_code": "0",
                "fid_trgt_exls_cls_code": "0",
                "fid_div_cls_code": "0",
                "fid_rsfl_rate1": "",
                "fid_rsfl_rate2": "",
            }

            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    logger.error(f"등락률 순위 조회 실패: HTTP {resp.status}")
                    return result

                data = await resp.json()

                if data.get("rt_cd") != "0":
                    logger.warning(f"등락률 순위 API 오류: {data.get('msg1')}")
                    return result

                output = data.get("output", [])

                for item in output:
                    symbol = item.get("stck_shrn_iscd", "").strip()
                    name = item.get("hts_kor_isnm", "").strip()
                    if not symbol or not name:
                        continue

                    # 비정상 symbol 필터 (숫자 6자리만 허용)
                    if not symbol.isdigit() or len(symbol) != 6:
                        continue

                    result.append({
                        "symbol": symbol,
                        "name": name,
                        "price": float(item.get("stck_prpr", 0) or 0),
                        "change_pct": float(item.get("prdy_ctrt", 0) or 0),
                        "volume": int(item.get("acml_vol", 0) or 0),
                        "trade_value": int(item.get("acml_tr_pbmn", 0) or 0),
                        "change": float(item.get("prdy_vrss", 0) or 0),
                    })

            self._set_cache(cache_key, result)
            logger.info(f"[KISMarketData] 등락률 순위 조회 완료: {len(result)}개 종목")
            return result[:limit]

        except Exception as e:
            logger.error(f"등락률 순위 조회 오류: {e}")
            return result

    # ============================================================
    # 4. 외국인/기관 매매 동향 (FHPTJ04400000)
    # ============================================================

    async def fetch_foreign_institution(
        self,
        market: str = "0001",
        investor: str = "1",
    ) -> List[Dict]:
        """
        외국인/기관 순매수 상위 종목 조회

        Args:
            market: 0001=코스피, 0002=코스닥
            investor: 1=외국인, 2=기관

        Returns:
            [{종목코드, 종목명, 순매수수량, 순매수금액, 현재가, 등락률, ...}]
        """
        investor_name = "외국인" if investor == "1" else "기관"
        cache_key = f"foreign_inst_{market}_{investor}"
        if self._is_cache_valid(cache_key, 600):  # 10분 캐시
            return self._cache[cache_key]

        result: List[Dict] = []

        try:
            session = await self._get_session()
            headers = await self._get_headers("FHPTJ04400000")

            url = f"{self._token_manager.base_url}/uapi/domestic-stock/v1/quotations/foreign-institution-total"

            params = {
                "FID_COND_MRKT_DIV_CODE": "V",
                "FID_COND_SCR_DIV_CODE": "16449",
                "FID_INPUT_ISCD": market,
                "FID_DIV_CLS_CODE": "0",
                "FID_RANK_SORT_CLS_CODE": "0",
                "FID_ETC_CLS_CODE": investor,
            }

            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    logger.error(f"{investor_name} 매매동향 조회 실패: HTTP {resp.status}")
                    return result

                data = await resp.json()

                if data.get("rt_cd") != "0":
                    logger.warning(f"{investor_name} 매매동향 API 오류: {data.get('msg1')}")
                    return result

                output = data.get("output", [])

                for item in output:
                    symbol = item.get("mksc_shrn_iscd", "").zfill(6)
                    name = item.get("hts_kor_isnm", "").strip()
                    if not symbol or not name:
                        continue

                    net_qty = int(item.get("ntby_qty", 0) or 0)

                    acml_vol = int(item.get("acml_vol", 0) or 0)
                    prdy_vol = int(item.get("prdy_vol", 0) or 0)
                    # 거래량 비율: 당일 누적 / 전일 (장 마감 후에는 정확, 장중에는 부분값)
                    volume_ratio = round(acml_vol / prdy_vol, 2) if prdy_vol > 0 else 0.0

                    result.append({
                        "symbol": symbol,
                        "name": name,
                        "net_buy_qty": net_qty,
                        "net_buy_amt": int(item.get("ntby_tr_pbmn", 0) or 0),
                        "price": float(item.get("stck_prpr", 0) or 0),
                        "change_pct": float(item.get("prdy_ctrt", 0) or 0),
                        "volume": acml_vol,
                        "prdy_vol": prdy_vol,
                        "volume_ratio": volume_ratio,
                        "investor": investor_name,
                    })

            self._set_cache(cache_key, result)
            logger.info(
                f"[KISMarketData] {investor_name} 매매동향 조회 완료: {len(result)}개 종목"
            )
            return result

        except Exception as e:
            logger.error(f"{investor_name} 매매동향 조회 오류: {e}")
            return result

    # ============================================================
    # 5. 개별 종목 PER/PBR 조회 (FHKST01010100)
    # ============================================================

    async def fetch_stock_valuation(self, symbol: str) -> Optional[Dict]:
        """
        개별 종목 밸류에이션(PER/PBR/EPS/BPS) 조회

        Args:
            symbol: 종목코드 (6자리)

        Returns:
            {symbol, per, pbr, eps, bps, price, change_pct} 또는 None
        """
        cache_key = f"valuation_{symbol}"
        if self._is_cache_valid(cache_key, 1800):  # 30분 캐시
            return self._cache[cache_key]

        try:
            session = await self._get_session()
            headers = await self._get_headers("FHKST01010100")

            url = f"{self._token_manager.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"

            params = {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
            }

            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    return None

                data = await resp.json()

                if data.get("rt_cd") != "0":
                    return None

                output = data.get("output", {})
                if not output:
                    return None

                result = {
                    "symbol": symbol,
                    "per": float(output.get("per", 0) or 0),
                    "pbr": float(output.get("pbr", 0) or 0),
                    "eps": float(output.get("eps", 0) or 0),
                    "bps": float(output.get("bps", 0) or 0),
                    "price": float(output.get("stck_prpr", 0) or 0),
                    "change_pct": float(output.get("prdy_ctrt", 0) or 0),
                }

                self._set_cache(cache_key, result)
                return result

        except Exception as e:
            logger.debug(f"종목 밸류에이션 조회 오류 ({symbol}): {e}")
            return None

    async def fetch_batch_valuations(self, symbols: List[str]) -> Dict[str, Dict]:
        """
        복수 종목 PER/PBR 일괄 조회 (캐시 우선, 미캐시 건 병렬 API 호출)

        Args:
            symbols: 종목코드 리스트

        Returns:
            {symbol: {per, pbr, eps, bps, ...}} 딕셔너리
        """
        result: Dict[str, Dict] = {}
        to_fetch: List[str] = []

        # 캐시에 있는 건 즉시 반환
        cached_count = 0
        for sym in symbols:
            cache_key = f"valuation_{sym}"
            if self._is_cache_valid(cache_key, 1800):
                result[sym] = self._cache[cache_key]
                cached_count += 1
            else:
                to_fetch.append(sym)

        # 미캐시 건은 병렬 조회 (배치 단위로 API rate limit 준수)
        fetch_count = 0
        max_symbols = 30
        batch_size = 18  # RPS 18 한도 내 동시 호출

        # 최대 30건까지만 처리
        to_fetch = to_fetch[:max_symbols]

        # 배치 단위로 병렬 처리
        for i in range(0, len(to_fetch), batch_size):
            batch = to_fetch[i:i + batch_size]

            # 배치 내 종목들을 병렬로 조회
            tasks = [self.fetch_stock_valuation(sym) for sym in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            # 결과 수집
            for sym, val in zip(batch, batch_results):
                if isinstance(val, Exception):
                    logger.debug(f"[KISMarketData] {sym} 밸류에이션 조회 오류: {val}")
                    continue
                if val:
                    result[sym] = val
                    fetch_count += 1

            # 다음 배치 전 대기 (rate limit buffer)
            if i + batch_size < len(to_fetch):
                await asyncio.sleep(0.1)

        if to_fetch:
            logger.info(
                f"[KISMarketData] 밸류에이션 일괄 조회: 캐시 {cached_count}건 + "
                f"API {fetch_count}건 (병렬 처리)"
            )
        return result

    # ============================================================
    # 유틸리티
    # ============================================================

    # ============================================================
    # 6. KOSPI200 야간선물 현재가 (FHMIF10000000)
    # ============================================================

    @staticmethod
    def get_kospi200_front_month_code() -> str:
        """
        KOSPI200 선물 근월물 종목코드 자동 계산

        코드 체계: 101 + 연도코드 + 월코드
          - 연도코드: A=2007, B=2008, ... S=2025, T=2026, U=2027 ...
          - 월코드: 3=Mar, 6=Jun, 9=Sep, C=Dec (분기 만기)
          - 만기일: 만기월 두 번째 목요일

        만기일 경과 시 자동으로 다음 분기물로 롤오버
        """
        now = datetime.now()
        year, month, day = now.year, now.month, now.day

        _QUARTER_MONTHS = [3, 6, 9, 12]
        _MONTH_CODES = {3: "3", 6: "6", 9: "9", 12: "C"}
        _YEAR_BASE = 2007  # A=2007

        def _second_thursday(y: int, m: int) -> int:
            """해당 월의 두 번째 목요일 날짜 반환"""
            # 1일의 요일 (0=Mon ... 3=Thu ... 6=Sun)
            from calendar import monthrange, weekday
            first_dow = weekday(y, m, 1)
            # 첫 번째 목요일
            first_thu = 1 + (3 - first_dow) % 7
            return first_thu + 7  # 두 번째 목요일

        # 현재 분기 만기월 찾기
        expiry_year = year
        expiry_month = None
        for qm in _QUARTER_MONTHS:
            if month < qm:
                expiry_month = qm
                break
            elif month == qm:
                # 만기일 경과 여부 확인
                second_thu = _second_thursday(year, qm)
                if day <= second_thu:
                    expiry_month = qm
                    break
                # 만기 지남 → 다음 분기

        if expiry_month is None:
            # 12월 만기도 지남 → 내년 3월
            expiry_year = year + 1
            expiry_month = 3

        year_code = chr(ord("A") + (expiry_year - _YEAR_BASE))
        code = f"101{year_code}{_MONTH_CODES[expiry_month]}"
        return code

    async def get_night_futures_quote(
        self,
        symbol: Optional[str] = None,
        cache_ttl: int = 300,
    ) -> Optional[Dict[str, Any]]:
        """
        KOSPI200 야간선물 현재가 조회 (KRX 야간거래)

        Args:
            symbol: 선물 종목코드 (None이면 근월물 자동 계산)
            cache_ttl: 캐시 유효 시간 (초, 기본 5분)

        Returns:
            dict: {price, change, change_pct, volume, high, low, open, sentiment} 또는 None
        """
        if symbol is None:
            symbol = self.get_kospi200_front_month_code()
            logger.debug(f"[KIS] KOSPI200 근월물 자동 계산: {symbol}")
        cache_key = f"ngt_futures_{symbol}"
        if self._is_cache_valid(cache_key, cache_ttl):
            return self._cache[cache_key]

        try:
            session = await self._get_session()
            headers = await self._get_headers("FHMIF10000000")
            params = {
                "FID_COND_MRKT_DIV_CODE": "F",
                "FID_INPUT_ISCD": symbol,
            }

            base_url = self._token_manager.base_url
            url = f"{base_url}/uapi/domestic-futureoption/v1/quotations/inquire-price"

            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    logger.warning(f"[KIS] 야간선물 시세 조회 실패: HTTP {resp.status}")
                    return None

                data = await resp.json()
                rt_cd = data.get("rt_cd", "")
                if rt_cd != "0":
                    msg = data.get("msg1", "")
                    logger.warning(f"[KIS] 야간선물 시세 오류: rt_cd={rt_cd} {msg}")
                    return None

                output = data.get("output1") or data.get("output", {})
                if not output:
                    return None

                # output이 리스트인 경우 첫 번째 항목 사용
                if isinstance(output, list):
                    output = output[0] if output else {}

                price = float(output.get("futs_prpr", 0) or output.get("stck_prpr", 0) or 0)
                prev_close = float(output.get("futs_sdpr", 0) or output.get("stck_sdpr", 0) or 0)
                change = float(output.get("prdy_vrss", 0) or 0)
                change_pct = float(output.get("prdy_ctrt", 0) or 0)
                volume = int(output.get("acml_vol", 0) or 0)
                high = float(output.get("stck_hgpr", 0) or output.get("futs_hgpr", 0) or 0)
                low = float(output.get("stck_lwpr", 0) or output.get("futs_lwpr", 0) or 0)
                open_price = float(output.get("stck_oprc", 0) or output.get("futs_oprc", 0) or 0)

                if price <= 0:
                    logger.debug(f"[KIS] 야간선물 시세: 가격=0 (장외시간)")
                    # 장외시간 네거티브 캐시 — 불필요한 반복 API 호출 방지
                    # TTL을 역산하여 60초 후 만료되도록 설정
                    _neg_ttl = max(cache_ttl - 60, 60)
                    self._cache[cache_key] = None
                    self._cache_ts[cache_key] = datetime.now() - timedelta(seconds=_neg_ttl)
                    return None

                result = {
                    "price": price,
                    "prev_close": prev_close,
                    "change": change,
                    "change_pct": change_pct,
                    "volume": volume,
                    "high": high,
                    "low": low,
                    "open": open_price,
                    "symbol": symbol,
                }

                # 심리 판단 (야간선물 등락률 기반)
                if change_pct <= -1.0:
                    result["sentiment"] = "bearish"
                elif change_pct >= 1.0:
                    result["sentiment"] = "bullish"
                else:
                    result["sentiment"] = "neutral"

                self._set_cache(cache_key, result)
                logger.info(
                    f"[KIS] KOSPI200 야간선물: {price:.2f} ({change_pct:+.2f}%) "
                    f"→ {result['sentiment']}"
                )
                return result

        except Exception as e:
            logger.warning(f"[KIS] 야간선물 시세 조회 오류: {e}")
            return None

    def clear_cache(self):
        """캐시 초기화"""
        self._cache.clear()
        self._cache_ts.clear()

    async def close(self):
        """리소스 정리"""
        if self._session and not self._session.closed:
            await self._session.close()


# ============================================================
# 전역 인스턴스
# ============================================================

_kis_market_data: Optional[KISMarketData] = None


def get_kis_market_data() -> KISMarketData:
    """전역 KISMarketData 인스턴스"""
    global _kis_market_data
    if _kis_market_data is None:
        _kis_market_data = KISMarketData()
    return _kis_market_data
