"""
QWQ AI Trader - KIS 해외주식 브로커

KIS Open API를 사용하여 미국 주식 주문을 실행합니다.
v2 kis_broker.py의 HTTP 패턴 기반.

지원 기능:
- 시장가/지정가 매수/매도
- 주문 취소
- 잔고 조회
- 현재가 조회
- 체결 내역 조회

거래소 코드: NASD (NASDAQ), NYSE, AMEX
"""

from __future__ import annotations

import asyncio
import collections
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Any

import aiohttp
from loguru import logger


# 거래소 코드 매핑
EXCHANGE_MAP = {
    "NASDAQ": "NASD",
    "NMS": "NASD",
    "NGM": "NASD",
    "NASD": "NASD",
    "NYSE": "NYSE",
    "NYQ": "NYSE",
    "AMEX": "AMEX",
    "ASE": "AMEX",
    "PCX": "AMEX",  # NYSE Arca
}


@dataclass
class KISUSConfig:
    """KIS 해외주식 API 설정"""
    app_key: str = ""
    app_secret: str = ""
    account_no: str = ""
    account_product_cd: str = "01"
    env: str = "prod"
    base_url: str = field(default="")
    timeout_seconds: int = 15

    def __post_init__(self):
        if not self.base_url:
            if self.env == "prod":
                self.base_url = "https://openapi.koreainvestment.com:9443"
            else:
                self.base_url = "https://openapivts.koreainvestment.com:29443"

    @classmethod
    def from_env(cls) -> "KISUSConfig":
        return cls(
            app_key=os.getenv("KIS_APPKEY", "") or os.getenv("KIS_APP_KEY", ""),
            app_secret=os.getenv("KIS_APPSECRET", "") or os.getenv("KIS_SECRET_KEY", ""),
            account_no=os.getenv("KIS_CANO", ""),
            account_product_cd=os.getenv("KIS_ACNT_PRDT_CD", "01"),
            env=os.getenv("KIS_ENV", "prod"),
        )


class KISUSBroker:
    """
    KIS 해외주식 브로커

    미국 주식(NASDAQ, NYSE, AMEX) 주문 실행.
    """

    def __init__(self, config: Optional[KISUSConfig] = None, token_manager=None):
        self.config = config or KISUSConfig.from_env()
        self._session: Optional[aiohttp.ClientSession] = None
        self._token: Optional[str] = None
        self._token_mgr = token_manager

        # Rate limiter (초당 18건)
        self._rate_limit_lock = asyncio.Lock()
        self._api_call_times: collections.deque = collections.deque(maxlen=20)
        self._max_rps = 18

        # 검증
        if not self.config.app_key or not self.config.app_secret:
            raise ValueError("KIS_APPKEY와 KIS_APPSECRET이 설정되지 않았습니다.")
        if not self.config.account_no:
            raise ValueError("KIS_CANO(계좌번호)가 설정되지 않았습니다.")

        logger.info(
            f"KISUSBroker 초기화: env={self.config.env}, "
            f"account=****{self.config.account_no[-4:]}"
        )

    # ============================================================
    # TR ID (실전/모의 분기)
    # ============================================================

    def _tr(self, prod_id: str, dev_id: str) -> str:
        return prod_id if self.config.env == "prod" else dev_id

    @property
    def _tr_buy(self) -> str:
        return self._tr("TTTT1002U", "VTTT1002U")

    @property
    def _tr_sell(self) -> str:
        return self._tr("TTTT1006U", "VTTT1006U")

    @property
    def _tr_cancel(self) -> str:
        return self._tr("TTTT1004U", "VTTT1004U")

    @property
    def _tr_balance(self) -> str:
        """체결기준현재잔고 (CTRP6504R) — 장 마감 후 30분부터 이용 가능"""
        return self._tr("CTRP6504R", "VTRP6504R")

    @property
    def _tr_realtime_balance(self) -> str:
        """해외주식 잔고 (TTTS3012R) — 실시간, 장중 항상 이용 가능"""
        return self._tr("TTTS3012R", "VTTS3012R")

    @property
    def _tr_quote(self) -> str:
        return "HHDFS00000300"

    @property
    def _tr_ccld(self) -> str:
        return self._tr("TTTS3035R", "VTTS3035R")

    # ============================================================
    # 연결 관리
    # ============================================================

    async def connect(self) -> bool:
        try:
            if not self._session or self._session.closed:
                timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
                # keepalive_timeout=30: 30초 유휴 시 TCP 연결 만료
                # KIS 서버가 유휴 커넥션을 닫아 HTTP 500을 반환하는 문제 방지
                connector = aiohttp.TCPConnector(keepalive_timeout=30, enable_cleanup_closed=True)
                self._session = aiohttp.ClientSession(timeout=timeout, connector=connector)

            if not await self._ensure_token():
                # 60초 대기 후 1회 더 시도 (KR 엔진 캐시 토큰 대기)
                logger.warning("[토큰] 발급 실패, 60초 대기 후 캐시 토큰 재확인...")
                await asyncio.sleep(60)
                if not await self._ensure_token():
                    logger.error("KIS 토큰 발급 실패")
                    return False

            logger.info("KIS US 브로커 연결 완료")
            return True
        except Exception as e:
            logger.exception(f"KIS US 브로커 연결 실패: {e}")
            return False

    async def disconnect(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        logger.info("KIS US 브로커 연결 해제")

    @property
    def is_connected(self) -> bool:
        if self._session is None or self._session.closed:
            return False
        return (self._token is not None
                and self._token_mgr is not None
                and self._token_mgr._is_token_valid())

    # ============================================================
    # 주문
    # ============================================================

    async def submit_buy_order(self, symbol: str, exchange: str = "NASD",
                               qty: int = 0, price: float = 0) -> dict:
        """
        매수 주문.

        Args:
            symbol: 티커 (e.g., AAPL)
            exchange: NASD, NYSE, AMEX
            qty: 수량
            price: 가격 (0이면 시장가)
        """
        return await self._submit_order(symbol, exchange, qty, price, self._tr_buy, "매수")

    async def submit_sell_order(self, symbol: str, exchange: str = "NASD",
                                qty: int = 0, price: float = 0) -> dict:
        """매도 주문."""
        return await self._submit_order(symbol, exchange, qty, price, self._tr_sell, "매도")

    async def _submit_order(self, symbol: str, exchange: str, qty: int,
                            price: float, tr_id: str, side_name: str) -> dict:
        if qty <= 0:
            return {"success": False, "message": "수량은 1 이상이어야 합니다"}

        # KIS 해외주식 주문 구분:
        # ORD_DVSN="00" (지정가), OVRS_ORD_UNPR="0"이면 시장가로 처리됨
        ord_dvsn = "00"
        ord_price = f"{price:.2f}" if price > 0 else "0"

        body = {
            "CANO": self.config.account_no,
            "ACNT_PRDT_CD": self.config.account_product_cd,
            "OVRS_EXCG_CD": exchange,
            "PDNO": symbol,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": ord_price,
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": ord_dvsn,
        }

        hashkey = await self._get_hashkey(body)
        extra_headers = {"hashkey": hashkey} if hashkey else {}

        url = f"{self.config.base_url}/uapi/overseas-stock/v1/trading/order"
        data = await self._api_post(url, tr_id, body, extra_headers)

        rt_cd = data.get("rt_cd", "-1")
        if rt_cd == "0":
            output = data.get("output", {})
            order_no = output.get("ODNO", "")
            logger.info(f"[{side_name}] {symbol} {qty}주 주문 성공 (주문번호: {order_no})")
            return {
                "success": True,
                "order_no": order_no,
                "symbol": symbol,
                "qty": qty,
                "price": price,
                "message": data.get("msg1", ""),
            }
        else:
            msg = data.get("msg1", "알 수 없는 오류")
            logger.error(f"[{side_name}] {symbol} {qty}주 주문 실패: {msg}")
            return {"success": False, "message": msg}

    async def cancel_order(self, order_no: str, exchange: str = "NASD",
                           symbol: str = "", qty: int = 0) -> dict:
        """주문 취소"""
        body = {
            "CANO": self.config.account_no,
            "ACNT_PRDT_CD": self.config.account_product_cd,
            "OVRS_EXCG_CD": exchange,
            "PDNO": symbol,
            "ORGN_ODNO": order_no,
            "RVSE_CNCL_DVSN_CD": "02",  # 02=취소
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": "0",
            "ORD_SVR_DVSN_CD": "0",
        }

        hashkey = await self._get_hashkey(body)
        extra_headers = {"hashkey": hashkey} if hashkey else {}

        url = f"{self.config.base_url}/uapi/overseas-stock/v1/trading/order-rvsecncl"
        data = await self._api_post(url, self._tr_cancel, body, extra_headers)

        rt_cd = data.get("rt_cd", "-1")
        if rt_cd == "0":
            logger.info(f"[취소] 주문 {order_no} 취소 성공")
            return {"success": True, "order_no": order_no}
        else:
            msg = data.get("msg1", "알 수 없는 오류")
            logger.error(f"[취소] 주문 {order_no} 취소 실패: {msg}")
            return {"success": False, "message": msg}

    # ============================================================
    # 조회
    # ============================================================

    async def get_positions(self) -> List[dict]:
        """
        해외주식 잔고 조회 (TTTS3012R — 실시간, 장중 항상 이용 가능).

        Returns:
            [{symbol, qty, avg_price, current_price, pnl, pnl_pct, exchange, name}]
        """
        # TTTS3012R: 실시간 잔고 (장중/비장 모두 가능)
        url = f"{self.config.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
        params = {
            "CANO":            self.config.account_no,
            "ACNT_PRDT_CD":    self.config.account_product_cd,
            "OVRS_EXCG_CD":    "NASD",   # 실전: NASD=미국 전체 (NASDAQ+NYSE+AMEX)
            "TR_CRCY_CD":      "USD",
            "CTX_AREA_FK200":  "",
            "CTX_AREA_NK200":  "",
        }

        data = await self._api_get(url, self._tr_realtime_balance, params)
        if data.get("rt_cd") != "0":
            # KIS 특성: 포지션 0건일 때 rt_cd="1", msg_cd="" 반환 → 빈 결과 처리
            if not data.get("msg_cd") and not data.get("msg1"):
                logger.debug("[TTTS3012R] 포지션 없음 (빈 결과)")
                return []
            logger.error(f"잔고 조회 실패 (TTTS3012R): {data.get('msg1', '')} [{data.get('msg_cd','')}]")
            return []

        positions = []
        for item in data.get("output1", []):
            qty = int(item.get("ovrs_cblc_qty", "0") or "0")
            if qty <= 0:
                continue

            # pchs_avg_pric: USD 매입평균가격 (TTTS3012R은 직접 제공 — 역산 불필요)
            avg_price     = float(item.get("pchs_avg_pric", "0") or "0")
            current_price = float(item.get("now_pric2", "0") or "0")
            pnl           = float(item.get("frcr_evlu_pfls_amt", "0") or "0")
            pnl_pct       = float(item.get("evlu_pfls_rt", "0") or "0")

            # avg_price 이상값 방어 (API 반환 0 또는 과도한 값)
            if avg_price <= 0 and current_price > 0 and abs(pnl_pct) < 99.9:
                avg_price = current_price / (1 + pnl_pct / 100)

            positions.append({
                "symbol":        item.get("ovrs_pdno", "").strip(),
                "name":          item.get("ovrs_item_name", "").strip(),
                "qty":           qty,
                "avg_price":     avg_price,
                "current_price": current_price,
                "pnl":           pnl,
                "pnl_pct":       pnl_pct,
                "exchange":      item.get("ovrs_excg_cd", "NASD"),
            })

        return positions

    async def get_balance(self) -> dict:
        """
        잔고 + 계좌 정보 조회.

        전략:
        1. TTTS3012R (실시간 잔고) → 포지션 + P&L (장중 항상 가능)
        2. CTRP6504R (체결기준잔고) → 가용현금 (장 마감 30분 후부터 가능)
           실패 시 TTTS3012R output2로 추정값 사용

        Returns:
            {positions: [...], account: {total_equity, available_cash, ...}}
        """
        # ── 1. TTTS3012R: 실시간 포지션 조회 ───────────────────────────────
        rt_url = f"{self.config.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
        rt_params = {
            "CANO":           self.config.account_no,
            "ACNT_PRDT_CD":   self.config.account_product_cd,
            "OVRS_EXCG_CD":   "NASD",  # 실전: 미국 전체
            "TR_CRCY_CD":     "USD",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }

        rt_data = await self._api_get(rt_url, self._tr_realtime_balance, rt_params)
        logger.info(
            f"[TTTS3012R] rt_cd={rt_data.get('rt_cd')} msg_cd={repr(rt_data.get('msg_cd',''))} "
            f"msg1={repr(rt_data.get('msg1',''))[:60]} output1_len={len(rt_data.get('output1') or [])}"
        )
        if rt_data.get("rt_cd") != "0":
            # KIS 특성: 포지션 0건 / 장 마감 후 rt_cd="1", msg_cd="" 반환
            # → CTRP6504R(체결기준잔고)로 폴백 시도
            if not rt_data.get("msg_cd") and not rt_data.get("msg1"):
                logger.info("[TTTS3012R] rt_cd=1 빈 응답 → CTRP6504R 폴백 시도")
                return await self._get_balance_settle()
            logger.error(f"잔고 조회 실패 (TTTS3012R): {rt_data.get('msg1', '')} [{rt_data.get('msg_cd','')}]")
            return {}

        # 포지션 파싱
        positions = []
        for item in rt_data.get("output1", []):
            # 소수점 주식(fractional) 지원: "0.014619"처럼 소수점 포함 가능
            qty_raw = item.get("ovrs_cblc_qty", "0") or "0"
            try:
                qty = float(qty_raw)
            except (ValueError, TypeError):
                qty = 0.0
            if qty <= 0:
                continue
            avg_price     = float(item.get("pchs_avg_pric", "0") or "0")
            current_price = float(item.get("now_pric2", "0") or "0")
            pnl_pct       = float(item.get("evlu_pfls_rt", "0") or "0")
            if avg_price <= 0 and current_price > 0 and abs(pnl_pct) < 99.9:
                avg_price = current_price / (1 + pnl_pct / 100)
            positions.append({
                "symbol":        item.get("ovrs_pdno", "").strip(),
                "name":          item.get("ovrs_item_name", "").strip(),
                "qty":           qty,
                "avg_price":     avg_price,
                "current_price": current_price,
                "pnl":           float(item.get("frcr_evlu_pfls_amt", "0") or "0"),
                "pnl_pct":       pnl_pct,
                "exchange":      item.get("ovrs_excg_cd", "NASD"),
            })

        # output2: 계좌 요약 (예수금 + 총평가 포함)
        output2 = rt_data.get("output2", {})
        if isinstance(output2, list):
            output2 = output2[0] if output2 else {}

        # output2 핵심 필드:
        #   frcr_dncl_amt  = 외화 예수금 (주문 가능 USD)
        #   frcr_evlu_amt  = 보유주식 평가금 (USD)
        #   ovrs_tot_pfls  = 총 평가손익
        # 총 미국 자산 = frcr_dncl_amt + frcr_evlu_amt
        available_cash  = float(output2.get("frcr_dncl_amt", "0") or "0")
        stock_eval_amt  = float(output2.get("frcr_evlu_amt", "0") or "0")
        total_pnl       = float(output2.get("ovrs_tot_pfls", "0") or "0")
        total_equity    = available_cash + stock_eval_amt

        logger.debug(
            f"[잔고] output2: 예수금=${available_cash:.2f}, 주식평가=${stock_eval_amt:.2f}, "
            f"총자산=${total_equity:.2f}, 총손익=${total_pnl:.2f}"
        )

        # CTRP6504R 폴백 (output2에 예수금이 없을 때 — 장 마감 후 가용)
        if available_cash <= 0:
            try:
                settle_url = f"{self.config.base_url}/uapi/overseas-stock/v1/trading/inquire-present-balance"
                settle_params = {
                    "CANO":              self.config.account_no,
                    "ACNT_PRDT_CD":      self.config.account_product_cd,
                    "WCRC_FRCR_DVSN_CD": "02",
                    "NATN_CD":           "840",
                    "TR_MKET_CD":        "00",
                    "INQR_DVSN_CD":      "00",
                }
                settle_data = await self._api_get(settle_url, self._tr_balance, settle_params)
                if settle_data.get("rt_cd") == "0":
                    output3 = settle_data.get("output3", {})
                    if isinstance(output3, list):
                        output3 = output3[0] if output3 else {}
                    settle_cash = float(output3.get("FRCR_DRWG_PSBL_AMT_1", "0") or "0")
                    settle_equity = float(output3.get("FRCR_DNCL_AMT_2", "0") or "0")
                    if settle_cash > 0:
                        available_cash = settle_cash
                    if settle_equity > 0:
                        total_equity = settle_equity
                    total_pnl = float(output3.get("OVRS_TOT_PFLS", total_pnl) or total_pnl)
            except Exception:
                pass  # 장중에는 HTTP 500 → 무시

        account = {
            "available_cash":  available_cash if available_cash > 0 else None,
            "total_equity":    total_equity if total_equity > 0 else None,
            "stock_eval_amt":  stock_eval_amt,
            "total_pnl":       total_pnl,
        }

        return {"positions": positions, "account": account}

    async def _get_balance_settle(self) -> dict:
        """CTRP6504R 체결기준잔고 폴백 (TTTS3012R 실패 시)"""
        url = f"{self.config.base_url}/uapi/overseas-stock/v1/trading/inquire-present-balance"
        params = {
            "CANO":              self.config.account_no,
            "ACNT_PRDT_CD":      self.config.account_product_cd,
            "WCRC_FRCR_DVSN_CD": "02",
            "NATN_CD":           "840",
            "TR_MKET_CD":        "00",
            "INQR_DVSN_CD":      "00",
        }

        data = await self._api_get(url, self._tr_balance, params)
        if data.get("rt_cd") != "0":
            logger.warning(f"[CTRP6504R] 폴백도 실패: {data.get('msg1', '')}")
            return {}  # 빈 dict → _sync_portfolio에서 'not balance'로 스킵

        # output2: 종목별 잔고
        positions = []
        for item in data.get("output2", []):
            qty_raw = item.get("CCLD_QTY_SMTL", "0") or "0"
            try:
                qty = float(qty_raw)
            except (ValueError, TypeError):
                qty = 0.0
            if qty <= 0:
                continue
            avg_price = float(item.get("PCH_AMT", "0") or "0")
            if qty > 0 and avg_price > 0:
                avg_price = avg_price / qty
            positions.append({
                "symbol":        item.get("OVRS_PDNO", "").strip(),
                "name":          item.get("OVRS_ITEM_NAME", "").strip(),
                "qty":           qty,
                "avg_price":     avg_price,
                "current_price": float(item.get("NOW_PRIC2", "0") or "0"),
                "pnl":           float(item.get("FRCR_EVLU_PFLS_AMT", "0") or "0"),
                "pnl_pct":       float(item.get("EVLU_PFLS_RT", "0") or "0"),
                "exchange":      item.get("OVRS_EXCG_CD", "NASD"),
            })

        # output3: 계좌 요약
        output3 = data.get("output3", {})
        if isinstance(output3, list):
            output3 = output3[0] if output3 else {}

        account = {
            "available_cash": float(output3.get("FRCR_DRWG_PSBL_AMT_1", "0") or "0"),
            "total_equity":   float(output3.get("FRCR_DNCL_AMT_2", "0") or "0"),
            "total_pnl":      float(output3.get("OVRS_TOT_PFLS", "0") or "0"),
        }

        logger.info(f"[CTRP6504R] 폴백 성공: {len(positions)}종목, cash={account['available_cash']:.2f}")
        return {"positions": positions, "account": account}

    async def get_account(self) -> dict:
        """
        계좌 요약 조회.

        Returns:
            {total_equity, available_cash, total_pnl, total_pnl_pct}
        """
        url = f"{self.config.base_url}/uapi/overseas-stock/v1/trading/inquire-present-balance"
        params = {
            "CANO": self.config.account_no,
            "ACNT_PRDT_CD": self.config.account_product_cd,
            "WCRC_FRCR_DVSN_CD": "02",
            "NATN_CD": "840",
            "TR_MKET_CD": "00",
            "INQR_DVSN_CD": "00",
        }

        data = await self._api_get(url, self._tr_balance, params)
        if data.get("rt_cd") != "0":
            logger.error(f"계좌 조회 실패: {data.get('msg1', '')}")
            return {}

        # output3: 계좌 전체 요약
        output3 = data.get("output3", {})
        if isinstance(output3, list):
            output3 = output3[0] if output3 else {}

        return {
            "total_equity": float(output3.get("FRCR_DNCL_AMT_2", "0") or "0"),
            "available_cash": float(output3.get("FRCR_DRWG_PSBL_AMT_1", "0") or "0"),
            "total_pnl": float(output3.get("OVRS_TOT_PFLS", "0") or "0"),
            "total_pnl_pct": float(output3.get("TOT_EVLU_PFLS_RT", "0") or "0"),
        }

    # 거래소 코드 변환 (주문용 NASD → 시세조회용 NAS)
    _EXCD_QUOTE_MAP = {"NASD": "NAS", "NAS": "NAS", "NYSE": "NYS", "NYS": "NYS",
                        "AMEX": "AMS", "AMS": "AMS"}

    async def get_quote(self, symbol: str, exchange: str = "NAS") -> dict:
        """
        해외주식 현재가 조회.

        Returns:
            {price, change, change_pct, volume, high, low, open}
        """
        url = f"{self.config.base_url}/uapi/overseas-price/v1/quotations/price"
        # 거래소 코드 변환 (NASD→NAS, NYSE→NYS, AMEX→AMS)
        excd = self._EXCD_QUOTE_MAP.get(exchange, exchange)
        params = {
            "AUTH": "",
            "EXCD": excd,
            "SYMB": symbol,
        }

        data = await self._api_get(url, self._tr_quote, params)
        if data.get("rt_cd") != "0":
            logger.error(f"현재가 조회 실패 ({symbol}): {data.get('msg1', '')}")
            return {"symbol": symbol, "price": 0}

        output = data.get("output", {})
        return {
            "symbol": symbol,
            "price": float(output.get("last", "0") or "0"),
            "change": float(output.get("diff", "0") or "0"),
            "change_pct": float(output.get("rate", "0") or "0"),
            "volume": int(output.get("tvol", "0") or "0"),
            "high": float(output.get("high", "0") or "0"),
            "low": float(output.get("low", "0") or "0"),
            "open": float(output.get("open", "0") or "0"),
        }

    async def get_order_history(self, start_date: str = None,
                                end_date: str = None) -> List[dict]:
        """
        체결 내역 조회 (페이지네이션 지원).

        Args:
            start_date: YYYYMMDD (기본: 오늘)
            end_date: YYYYMMDD (기본: 오늘)

        Returns:
            [{order_no, symbol, side, qty, price, filled_qty, filled_price, status, time}]
        """
        today = datetime.now().strftime("%Y%m%d")
        if not start_date:
            start_date = today
        if not end_date:
            end_date = today

        url = f"{self.config.base_url}/uapi/overseas-stock/v1/trading/inquire-daily-ccld"
        ctx_fk = ""
        ctx_nk = ""
        orders = []
        max_pages = 10  # 무한 루프 방지

        for page in range(max_pages):
            params = {
                "CANO": self.config.account_no,
                "ACNT_PRDT_CD": self.config.account_product_cd,
                "PDNO": "",
                "ORD_STRT_DT": start_date,
                "ORD_END_DT": end_date,
                "SLL_BUY_DVSN": "00",   # 00=전체
                "CCLD_NCCS_DVSN": "00",  # 00=전체
                "OVRS_EXCG_CD": "",       # 빈값=전체 거래소
                "SORT_SQN": "DS",       # 내림차순
                "ORD_DT": "",
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "CTX_AREA_NK200": ctx_nk,
                "CTX_AREA_FK200": ctx_fk,
            }

            data = await self._api_get(url, self._tr_ccld, params, return_headers=True)
            if data.get("rt_cd") != "0":
                if page == 0:
                    logger.error(f"체결 내역 조회 실패: {data.get('msg1', '')}")
                break

            for item in data.get("output1", []):
                order_no = item.get("ODNO", "").strip()
                if not order_no:
                    continue

                filled_qty = int(item.get("FT_CCLD_QTY", "0") or "0")
                ord_qty = int(item.get("FT_ORD_QTY", "0") or "0")

                # 상태 판정
                if filled_qty >= ord_qty and ord_qty > 0:
                    status = "filled"
                elif filled_qty > 0:
                    status = "partial"
                else:
                    status = "pending"

                sll_buy = item.get("SLL_BUY_DVSN_CD", "")
                side = "sell" if sll_buy == "01" else "buy"

                orders.append({
                    "order_no": order_no,
                    "symbol": item.get("OVRS_PDNO", "").strip(),
                    "side": side,
                    "qty": ord_qty,
                    "price": float(item.get("FT_ORD_UNPR3", "0") or "0"),
                    "filled_qty": filled_qty,
                    "filled_price": float(item.get("FT_CCLD_UNPR3", "0") or "0"),
                    "status": status,
                    "time": item.get("ORD_TMD", ""),
                    "exchange": item.get("OVRS_EXCG_CD", ""),
                })

            # 연속조회 키 확인
            tr_cont = data.get("_tr_cont", "")
            new_fk = (data.get("ctx_area_fk200") or "").strip()
            new_nk = (data.get("ctx_area_nk200") or "").strip()

            # 다음 페이지 존재 여부: tr_cont가 "M" 또는 "F"이고 연속조회 키가 변경됨
            if tr_cont in ("M", "F") and (new_fk or new_nk):
                # IRP 중복 방지: 이전과 동일한 키면 중단
                if new_fk == ctx_fk and new_nk == ctx_nk:
                    break
                ctx_fk = new_fk
                ctx_nk = new_nk
            else:
                break

        return orders

    # ============================================================
    # Rate Limiter
    # ============================================================

    async def _rate_limit(self):
        while True:
            async with self._rate_limit_lock:
                now = time.monotonic()
                while self._api_call_times and now - self._api_call_times[0] > 1.0:
                    self._api_call_times.popleft()
                if len(self._api_call_times) < self._max_rps:
                    self._api_call_times.append(time.monotonic())
                    return
                wait_time = 1.0 - (now - self._api_call_times[0])
            if wait_time > 0:
                await asyncio.sleep(wait_time)

    # ============================================================
    # HTTP 헬퍼
    # ============================================================

    def _get_headers(self, tr_id: str) -> Dict[str, str]:
        return {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._token}",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    def _is_token_error(self, data: dict) -> bool:
        msg_cd = str(data.get("msg_cd", ""))
        return msg_cd in ("EGW00123", "EGW00121")

    async def _ensure_token(self) -> bool:
        for attempt in range(3):
            self._token = await self._token_mgr.get_access_token()
            if self._token is not None:
                return True
            delay = 2 ** attempt
            logger.warning(f"[토큰] 발급 실패 (시도 {attempt + 1}/3), {delay}초 후 재시도")
            await asyncio.sleep(delay)
        logger.error("[토큰] 3회 재시도 후에도 토큰 발급 실패")
        return False

    async def _api_get(self, url: str, tr_id: str, params: dict,
                       return_headers: bool = False) -> dict:
        if not self._session or self._session.closed:
            if not await self.connect():
                return {"rt_cd": "-1", "msg1": "세션 연결 실패"}
        if self._token is None:
            if not await self._ensure_token():
                return {"rt_cd": "-1", "msg1": "토큰 발급 실패"}

        _reconnect_after = False  # 루프 바깥에서 세션 재생성 플래그
        for attempt in range(3):
            try:
                # 이전 시도에서 500 발생 → 세션 재생성 후 재시도
                if _reconnect_after:
                    _reconnect_after = False
                    try:
                        await self._session.close()
                    except Exception:
                        pass
                    self._session = None
                    await self.connect()

                await self._rate_limit()
                headers = self._get_headers(tr_id)
                async with self._session.get(url, headers=headers, params=params) as resp:
                    if resp.status == 401 and attempt < 2:
                        logger.warning("[토큰] 401 응답, 토큰 갱신 시도")
                        await self._ensure_token()
                        continue
                    if resp.status in (429, 500, 502, 503) and attempt < 2:
                        wait = 2 ** attempt
                        logger.warning(f"[API] HTTP {resp.status}, {attempt+1}회 재시도 ({wait}초 대기)")
                        if resp.status == 500:
                            _reconnect_after = True  # async with 밖에서 세션 재생성
                        await asyncio.sleep(wait)
                        continue
                    try:
                        data = await resp.json()
                    except Exception:
                        err = {"rt_cd": "-1", "msg1": f"JSON 파싱 실패 (HTTP {resp.status})"}
                        return err
                    if self._is_token_error(data) and attempt < 2:
                        await self._ensure_token()
                        continue
                    if return_headers:
                        data["_tr_cont"] = resp.headers.get("tr_cont", "").strip()
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt < 2:
                    wait = 2 ** attempt
                    logger.warning(f"[API] 네트워크 오류, {attempt+1}회 재시도: {e}")
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"[API] GET 실패 (3회 시도): {e}")
                return {"rt_cd": "-1", "msg1": f"네트워크 오류: {e}"}
        return {"rt_cd": "-1", "msg1": "API 호출 실패 (최대 재시도 초과)"}

    async def _api_post(self, url: str, tr_id: str, json_data: dict,
                        extra_headers: Optional[dict] = None) -> dict:
        if not self._session or self._session.closed:
            if not await self.connect():
                return {"rt_cd": "-1", "msg1": "세션 연결 실패"}
        if self._token is None:
            if not await self._ensure_token():
                return {"rt_cd": "-1", "msg1": "토큰 발급 실패"}

        for attempt in range(3):
            try:
                await self._rate_limit()
                headers = self._get_headers(tr_id)
                if extra_headers:
                    headers.update(extra_headers)
                async with self._session.post(url, headers=headers, json=json_data) as resp:
                    if resp.status == 401 and attempt < 2:
                        await self._ensure_token()
                        continue
                    if resp.status in (429, 500, 502, 503) and attempt < 2:
                        wait = 2 ** attempt
                        logger.warning(f"[API] HTTP {resp.status}, {attempt+1}회 재시도 ({wait}초 대기)")
                        await asyncio.sleep(wait)
                        continue
                    try:
                        data = await resp.json()
                    except Exception:
                        return {"rt_cd": "-1", "msg1": f"JSON 파싱 실패 (HTTP {resp.status})"}
                    if self._is_token_error(data) and attempt < 2:
                        await self._ensure_token()
                        continue
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt < 2:
                    wait = 2 ** attempt
                    logger.warning(f"[API] 네트워크 오류, {attempt+1}회 재시도: {e}")
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"[API] POST 실패 (3회 시도): {e}")
                return {"rt_cd": "-1", "msg1": f"네트워크 오류: {e}"}
        return {"rt_cd": "-1", "msg1": "API 호출 실패 (최대 재시도 초과)"}

    async def _get_hashkey(self, body: dict) -> Optional[str]:
        url = f"{self.config.base_url}/uapi/hashkey"
        headers = {
            "Content-Type": "application/json",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
        }
        for attempt in range(3):
            try:
                await self._rate_limit()
                async with self._session.post(url, headers=headers, json=body) as resp:
                    if resp.status != 200:
                        if attempt < 2:
                            await asyncio.sleep(0.5 * (attempt + 1))
                            continue
                        return None
                    data = await resp.json()
                    return data.get("HASH")
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
                else:
                    logger.error(f"Hashkey 발급 실패: {e}")
        return None

    # ============================================================
    # 시세분석 / 스크리닝 API
    # ============================================================

    async def get_volume_surge(
        self,
        exchange: str = "NAS",
        minutes_ago: int = 5,
        min_volume: str = "2",
    ) -> list[dict]:
        """
        해외주식 거래량급증 조회 (HHDFS76270000)

        Parameters
        ----------
        exchange    : NAS(나스닥), NYS(뉴욕), AMS(아멕스)
        minutes_ago : 0=1분전, 1=2분전, 2=3분전, 3=5분전, 4=10분전, 5=15분전, 6=20분전
        min_volume  : 0=전체, 1=1백주이상, 2=1천주이상, 3=1만주이상, 4=10만주이상, 5=100만주이상

        Returns
        -------
        list of dict:
            symbol  : 종목코드
            name    : 종목명
            price   : 현재가 (float)
            change  : 등락율 (float, %)
            volume  : 거래량 (int)
            surge_rate: 급증율 (float, %)  ← n_rate
            exchange: 거래소코드
        """
        # 모의투자 미지원
        if self.config.env != "prod":
            logger.debug("[거래량급증] 모의투자 미지원 — skip")
            return []

        url = f"{self.config.base_url}/uapi/overseas-stock/v1/ranking/volume-surge"
        # minutes_ago → MIXN 변환
        mixn_map = {1: "0", 2: "1", 3: "2", 5: "3", 10: "4", 15: "5", 20: "6"}
        mixn = mixn_map.get(minutes_ago, "3")  # 기본 5분전

        # 거래소 코드 변환 (시세 조회용)
        excd = self._EXCD_QUOTE_MAP.get(exchange, exchange)
        params = {
            "KEYB":     "",
            "AUTH":     "",
            "EXCD":     excd,
            "MIXN":     mixn,
            "VOL_RANG": str(min_volume),
        }
        try:
            data = await self._api_get(url, "HHDFS76270000", params)
            if data.get("rt_cd") != "0":
                logger.warning(
                    f"[거래량급증] API 오류: {data.get('msg1', '')} (excd={exchange})"
                )
                return []

            rows = data.get("output2", []) or []
            result = []
            for r in rows:
                sym = r.get("symb", "").strip()
                if not sym:
                    continue
                try:
                    result.append({
                        "symbol":     sym,
                        "name":       r.get("knam", "").strip(),
                        "price":      float(r.get("last", 0) or 0),
                        "change":     float(r.get("rate", 0) or 0),
                        "volume":     int(r.get("tvol", 0) or 0),
                        "surge_rate": float(r.get("n_rate", 0) or 0),
                        "exchange":   r.get("excd", exchange),
                    })
                except (ValueError, TypeError):
                    continue

            logger.debug(
                f"[거래량급증] {exchange} {len(result)}종목 (최대급증 "
                f"{max((r['surge_rate'] for r in result), default=0):.0f}%)"
            )
            return result

        except Exception as e:
            logger.error(f"[거래량급증] 조회 오류: {e}")
            return []

    async def get_condition_search(
        self,
        exchange: str = "NAS",
        min_change_pct: float = 1.0,
        min_volume: int = 500_000,
        min_price: float = 5.0,
        max_price: float = 0.0,
    ) -> list[dict]:
        """
        해외주식 조건검색 (HHDFS76410000) — KIS 자체 필터링

        현재가·등락율·거래량 조건 복합 필터.
        최대 100건 반환 (다음조회 미지원).

        Parameters
        ----------
        exchange       : NAS, NYS, AMS
        min_change_pct : 등락율 최소 (%)
        min_volume     : 거래량 최소 (주)
        min_price      : 현재가 최소 (USD)
        max_price      : 현재가 최대 (USD, 0=제한없음)

        Returns
        -------
        list of dict:
            symbol   : 종목코드
            name     : 종목명
            price    : 현재가 (float)
            change   : 등락율 (float, %)
            volume   : 거래량 (int)
            mktcap   : 시가총액 (float, 천 단위)
            eps      : EPS (float)
            per      : PER (float)
            exchange : 거래소코드
        """
        url = f"{self.config.base_url}/uapi/overseas-price/v1/quotations/inquire-search"

        # EXCD 변환 (NAS → NAS, NYS → NYS, AMS → AMS)
        excd_map = {"NASD": "NAS", "NAS": "NAS", "NYSE": "NYS", "NYS": "NYS",
                    "AMEX": "AMS", "AMS": "AMS"}
        excd = excd_map.get(exchange.upper(), "NAS")

        params: dict = {
            "AUTH": "",
            "EXCD": excd,
            "KEYB": "",
        }

        # 등락율 조건
        if min_change_pct > 0:
            params["CO_YN_RATE"]  = "1"
            params["CO_ST_RATE"]  = str(min_change_pct)
            params["CO_EN_RATE"]  = "100"

        # 현재가 조건
        if min_price > 0:
            params["CO_YN_PRICECUR"] = "1"
            params["CO_ST_PRICECUR"] = str(min_price)
            if max_price > 0:
                params["CO_EN_PRICECUR"] = str(max_price)

        # 거래량 조건
        if min_volume > 0:
            params["CO_YN_VOLUME"] = "1"
            params["CO_ST_VOLUME"] = str(min_volume)
            params["CO_EN_VOLUME"] = "9999999999"

        try:
            data = await self._api_get(url, "HHDFS76410000", params)
            if data.get("rt_cd") != "0":
                logger.warning(
                    f"[조건검색] API 오류: {data.get('msg1', '')} (excd={excd})"
                )
                return []

            rows = data.get("output2", []) or []
            result = []
            for r in rows:
                sym = r.get("symb", "").strip()
                if not sym:
                    continue
                try:
                    result.append({
                        "symbol":   sym,
                        "name":     r.get("name", "").strip() or r.get("ename", "").strip(),
                        "price":    float(r.get("last", 0) or 0),
                        "change":   float(r.get("rate", 0) or 0),
                        "volume":   int(r.get("tvol", 0) or 0),
                        "mktcap":   float(r.get("valx", 0) or 0),
                        "eps":      float(r.get("eps", 0) or 0),
                        "per":      float(r.get("per", 0) or 0),
                        "exchange": r.get("excd", excd),
                    })
                except (ValueError, TypeError):
                    continue

            logger.info(
                f"[조건검색] {excd} {len(result)}종목 "
                f"(등락율≥{min_change_pct}%, 거래량≥{min_volume:,})"
            )
            return result

        except Exception as e:
            logger.error(f"[조건검색] 조회 오류: {e}")
            return []
