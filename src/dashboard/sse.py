"""
QWQ AI Trader - SSE (Server-Sent Events) 스트림 관리 (통합)

KR + US 실시간 데이터를 브라우저에 푸시합니다.

이벤트 타입:
  KR: status, portfolio, positions, risk, events, pending_orders
  US: us_status, us_portfolio, us_positions, us_risk
"""

import asyncio
import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from aiohttp import web
from loguru import logger


class SSEManager:
    """SSE 클라이언트 관리 및 브로드캐스트 (KR + US 통합)"""

    def __init__(self, data_collector=None, us_engine=None):
        """
        Args:
            data_collector: KR DashboardDataCollector (None이면 KR 이벤트 비활성)
            us_engine: US LiveEngine 인스턴스 (None이면 US 이벤트 비활성)
        """
        self.data_collector = data_collector
        self.us_engine = us_engine
        self._clients: Set[web.StreamResponse] = set()
        self._running = False
        # 지수 전광판 내부 캐시
        self._indices_cache_data: Optional[List[Dict]] = None
        self._indices_last_fetch: float = 0

    async def handle_stream(self, request: web.Request) -> web.StreamResponse:
        """SSE 스트림 핸들러"""
        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Access-Control-Allow-Origin": "*",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        self._clients.add(response)
        logger.info(f"[SSE] 클라이언트 연결 (총 {len(self._clients)}명)")

        try:
            # 연결 유지 (클라이언트 끊길 때까지)
            while True:
                await asyncio.sleep(15)  # 15초마다 heartbeat
                try:
                    await response.write(b": heartbeat\n\n")
                except (ConnectionResetError, ConnectionError,
                        BrokenPipeError, OSError):
                    break
        except asyncio.CancelledError:
            pass
        finally:
            self._clients.discard(response)
            logger.info(f"[SSE] 클라이언트 연결 해제 (남은 {len(self._clients)}명)")

        return response

    async def broadcast(self, event_type: str, data: Any):
        """모든 연결된 클라이언트에 이벤트 전송"""
        if not self._clients:
            return

        payload = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        payload_bytes = payload.encode("utf-8")

        disconnected = set()
        for client in list(self._clients):  # 스냅샷 (await 중 add/discard 방지)
            try:
                await client.write(payload_bytes)
            except (ConnectionResetError, ConnectionError,
                    BrokenPipeError, OSError):
                disconnected.add(client)
            except Exception:
                disconnected.add(client)

        # 끊긴 클라이언트 제거
        if disconnected:
            self._clients -= disconnected
            logger.debug(f"[SSE] 끊긴 클라이언트 {len(disconnected)}명 정리 (남은 {len(self._clients)}명)")

    async def run_broadcast_loop(self):
        """주기적 데이터 브로드캐스트 (KR + US 통합)"""
        self._running = True
        dc = self.data_collector
        us = self.us_engine

        # 각 이벤트별 마지막 전송 시간
        last_sent: Dict[str, float] = {}

        # KR 이벤트별 주기 (초)
        kr_intervals = {
            "status": 5,
            "portfolio": 5,
            "positions": 2,
            "risk": 10,
            "events": 2,
            "pending_orders": 2,
        }

        # US 이벤트별 주기 (초)
        us_intervals = {
            "us_status": 5,
            "us_portfolio": 5,
            "us_positions": 2,
            "us_risk": 10,
        }

        # 이벤트 로그 커서
        last_event_id = 0
        # pending_orders 이전 상태 추적 (빈→빈 반복 skip용)
        _had_pending = False
        # US 에러 중복 로깅 방지
        us_error_logged: Dict[str, str] = {}

        logger.info("[SSE] 브로드캐스트 루프 시작")

        try:
            while self._running:
                now = time.time()

                # --- KR 이벤트 브로드캐스트 ---
                if dc:
                    for event_type, interval in kr_intervals.items():
                        if now - last_sent.get(event_type, 0) >= interval:
                            try:
                                if event_type == "status":
                                    data = dc.get_status()
                                elif event_type == "portfolio":
                                    data = dc.get_portfolio()
                                elif event_type == "positions":
                                    data = dc.get_positions()
                                elif event_type == "risk":
                                    data = dc.get_risk()
                                elif event_type == "events":
                                    new_events = dc.get_events(last_event_id)
                                    if not new_events:
                                        last_sent[event_type] = now
                                        continue
                                    data = new_events
                                    last_event_id = new_events[-1].get("id", last_event_id)
                                elif event_type == "pending_orders":
                                    data = dc.get_pending_orders()
                                    if not data:
                                        if _had_pending:
                                            # 이전에 데이터가 있었으면 빈 배열 한 번 전송 (카드 숨김용)
                                            _had_pending = False
                                        else:
                                            last_sent[event_type] = now
                                            continue
                                    else:
                                        _had_pending = True
                                else:
                                    continue

                                await self.broadcast(event_type, data)
                                last_sent[event_type] = now

                            except Exception as e:
                                logger.error(f"[SSE] {event_type} 브로드캐스트 오류: {type(e).__name__}: {e}")

                # --- US 이벤트 브로드캐스트 ---
                if us:
                    for event_type, interval in us_intervals.items():
                        if now - last_sent.get(event_type, 0) >= interval:
                            try:
                                data = self._collect_us_data(event_type)
                                if data is not None:
                                    await self.broadcast(event_type, data)
                                    last_sent[event_type] = now
                            except Exception as e:
                                if us_error_logged.get(event_type) != str(e):
                                    us_error_logged[event_type] = str(e)
                                    logger.exception(f"[SSE] {event_type} 브로드캐스트 오류")

                # --- 지수 전광판 (KR + US 통합, 10초 주기) ---
                if now - last_sent.get("market_indices", 0) >= 10:
                    try:
                        indices = await self._fetch_market_indices()
                        if indices:
                            await self.broadcast("market_indices", indices)
                            last_sent["market_indices"] = now
                    except Exception as e:
                        logger.debug(f"[SSE] market_indices 오류: {e}")

                await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            logger.info("[SSE] 브로드캐스트 루프 종료")

    async def _fetch_market_indices(self) -> Optional[List[Dict]]:
        """KOSPI·KOSDAQ 지수 + 미국 3대지수 + KR 개별종목 (펩트론·하이닉스·삼성전자) 조회.

        우선순위:
          - KR 지수   : KIS fetch_index_price (FHPUP02100000) — 장중 실시간
          - KR 개별주  : KIS get_quote — 프리/넥스트장 포함, 실패 시 Yahoo 폴백
          - US 지수   : Yahoo Finance (장중/장외 모두)
        결과는 kr_api의 /api/market/indices 캐시도 갱신해 HTTP 요청과 동기화.
        """
        # 10초 내부 캐시 (네트워크 호출 최소화)
        now = time.time()
        if now - self._indices_last_fetch < 10 and self._indices_cache_data:
            return self._indices_cache_data

        dc = self.data_collector
        bot = getattr(dc, "bot", None)
        kis_md = getattr(bot, "kis_market_data", None)
        broker = getattr(bot, "broker", None)

        results: List[Dict] = []

        # ── 1. KIS 실시간 지수 (KOSPI / KOSDAQ) ─────────────────
        if kis_md:
            try:
                for code, label in [("0001", "KOSPI"), ("1001", "KOSDAQ")]:
                    item = await kis_md.fetch_index_price(code)
                    if item:
                        results.append(item)
            except Exception as e:
                logger.debug(f"[SSE 지수] KIS 조회 오류: {e}")

        # ── 2. Yahoo Finance (US 지수 + KR 지수 폴백) ───────────
        kis_labels = {r["label"] for r in results}
        yahoo_indices = [
            ("^KS11",  "KOSPI",  "index_kr"),
            ("^KQ11",  "KOSDAQ", "index_kr"),
            ("^GSPC",  "S&P500", "index_us"),
            ("^IXIC",  "NASDAQ", "index_us"),
            ("^DJI",   "DOW",    "index_us"),
        ]
        yahoo_needed = [t for t in yahoo_indices if t[1] not in kis_labels]

        if yahoo_needed:
            try:
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                timeout = aiohttp.ClientTimeout(total=6)
                async with aiohttp.ClientSession(headers=headers, timeout=timeout) as sess:
                    for sym, lbl, kind in yahoo_needed:
                        try:
                            url = (
                                f"https://query1.finance.yahoo.com/v8/finance/chart/"
                                f"{sym}?interval=1d&range=5d"
                            )
                            async with sess.get(url) as resp:
                                if resp.status != 200:
                                    continue
                                js = await resp.json(content_type=None)
                                meta = js["chart"]["result"][0]["meta"]
                                price = meta.get("regularMarketPrice") or 0
                                raw_closes = (
                                    js["chart"]["result"][0]
                                    .get("indicators", {})
                                    .get("quote", [{}])[0]
                                    .get("close", [])
                                )
                                closes = [c for c in raw_closes if c is not None]
                                prev = closes[-2] if len(closes) >= 2 else price
                                chg = price - prev
                                chg_pct = (chg / prev * 100) if prev else 0
                                results.append({
                                    "symbol": sym,
                                    "label": lbl,
                                    "kind": kind,
                                    "price": round(price, 2),
                                    "change": round(chg, 2),
                                    "change_pct": round(chg_pct, 2),
                                    "source": "yahoo",
                                })
                        except Exception:
                            pass
            except Exception as e:
                logger.debug(f"[SSE 지수] Yahoo 조회 오류: {e}")

        # ── 3. KR 개별종목: KIS get_quote (프리/넥스트장 포함) ──
        KR_STOCKS = [
            ("087010", "펩트론",     "stock_kr"),
            ("000660", "SK하이닉스", "stock_kr"),
            ("005930", "삼성전자",   "stock_kr"),
        ]
        try:
            from src.utils.session import KRSession
            from src.core.types import MarketSession
            kr_session = KRSession().get_session()
            is_next = kr_session == MarketSession.NEXT
        except Exception:
            is_next = False

        for sym, label, kind in KR_STOCKS:
            item: Optional[Dict] = None

            # KIS get_quote 시도
            if broker:
                try:
                    q = await broker.get_quote(sym)
                    if q and q.get("price", 0) > 0:
                        if is_next and q.get("ovtm_price", 0) > 0:
                            price   = q["ovtm_price"]
                            chg_pct = q.get("ovtm_change_pct", 0)
                            prev    = q.get("prev_close", price)
                            chg     = price - prev
                        else:
                            price   = q["price"]
                            chg_pct = q.get("change_pct", 0)
                            chg     = q.get("change", 0)
                        item = {
                            "symbol": sym, "label": label, "kind": kind,
                            "price": round(price),
                            "change": round(chg),
                            "change_pct": round(chg_pct, 2),
                            "source": "kis",
                        }
                except Exception as e:
                    logger.debug(f"[SSE 개별주] {sym} KIS 오류: {e}")

            # Yahoo Finance 폴백
            if not item:
                try:
                    yf_sym = sym + ".KS"
                    headers_yf = {"User-Agent": "Mozilla/5.0"}
                    timeout_yf = aiohttp.ClientTimeout(total=5)
                    async with aiohttp.ClientSession(
                        headers=headers_yf, timeout=timeout_yf
                    ) as sess:
                        url = (
                            f"https://query1.finance.yahoo.com/v8/finance/chart/"
                            f"{yf_sym}?interval=1d&range=5d"
                        )
                        async with sess.get(url) as resp:
                            if resp.status == 200:
                                js = await resp.json(content_type=None)
                                meta = js["chart"]["result"][0]["meta"]
                                price = meta.get("regularMarketPrice") or 0
                                raw_closes = (
                                    js["chart"]["result"][0]
                                    .get("indicators", {})
                                    .get("quote", [{}])[0]
                                    .get("close", [])
                                )
                                closes = [c for c in raw_closes if c is not None]
                                prev = closes[-2] if len(closes) >= 2 else price
                                chg = price - prev
                                chg_pct = (chg / prev * 100) if prev else 0
                                item = {
                                    "symbol": sym, "label": label, "kind": kind,
                                    "price": round(price),
                                    "change": round(chg),
                                    "change_pct": round(chg_pct, 2),
                                    "source": "yahoo",
                                }
                except Exception as e:
                    logger.debug(f"[SSE 개별주] {sym} Yahoo 폴백 오류: {e}")

            if item:
                results.append(item)

        if not results:
            return self._indices_cache_data  # 이전 캐시 유지

        # 순서 고정: KOSPI → KOSDAQ → S&P500 → NASDAQ → DOW → 개별주(순서 유지)
        order = ["KOSPI", "KOSDAQ", "S&P500", "NASDAQ", "DOW", "펩트론", "SK하이닉스", "삼성전자"]
        results.sort(key=lambda x: order.index(x["label"]) if x["label"] in order else 99)

        self._indices_cache_data = results
        self._indices_last_fetch = now

        # kr_api 공유 캐시 갱신 (HTTP /api/market/indices도 동기화)
        try:
            from src.dashboard.kr_api import _indices_cache as _kapi_cache
            _kapi_cache["data"] = results
            _kapi_cache["ts"] = now
        except Exception:
            pass

        return results

    def _collect_us_data(self, event_type: str) -> Any:
        """US 엔진에서 SSE 데이터 수집"""
        engine = self.us_engine
        if not engine:
            return None

        if event_type == "us_status":
            session = getattr(engine, "session", None)
            session_value = "closed"
            if session:
                try:
                    session_value = session.get_session().value
                except Exception:
                    pass
            live_cfg = getattr(engine, "_live_cfg", {}) or {}
            broker_name = live_cfg.get("broker", "kis")
            env = live_cfg.get("env", "prod")
            is_paper = broker_name == "alpaca_paper" or env == "dev"
            return {
                "running": getattr(engine, "_running", False),
                "session": session_value,
                "timestamp": datetime.now().isoformat(),
                "broker": broker_name,
                "env": env,
                "paper_trading": is_paper,
            }

        elif event_type == "us_portfolio":
            portfolio = engine.portfolio
            total_value = float(portfolio.total_equity)
            daily_pnl = float(portfolio.effective_daily_pnl)
            initial = float(portfolio.initial_capital)
            daily_pnl_pct = (daily_pnl / initial * 100) if initial else 0.0
            return {
                "cash": float(portfolio.cash),
                "total_value": total_value,
                "positions_value": float(portfolio.total_position_value),
                "daily_pnl": daily_pnl,
                "daily_pnl_pct": round(daily_pnl_pct, 2),
                "positions_count": len(portfolio.positions),
            }

        elif event_type == "us_positions":
            positions = []
            exit_mgr = getattr(engine, "exit_manager", None)
            for symbol, pos in engine.portfolio.positions.items():
                entry_time = getattr(pos, "entry_time", None)
                # ExitManager._states에서 stage 조회 (pos.stage 속성 없음)
                stage = ""
                if exit_mgr and hasattr(exit_mgr, "_states"):
                    state = exit_mgr._states.get(symbol)
                    if state:
                        stage = state.current_stage.value
                positions.append({
                    "symbol": symbol,
                    "name": getattr(pos, "name", ""),
                    "quantity": pos.quantity,
                    "avg_price": float(pos.avg_price),
                    "current_price": float(pos.current_price),
                    "pnl": float(pos.unrealized_pnl),
                    "pnl_pct": round(pos.unrealized_pnl_pct, 2),
                    "strategy": pos.strategy or "",
                    "stage": stage,
                    "market_value": float(pos.market_value),
                    "entry_time": entry_time.isoformat() if entry_time else None,
                })
            return positions

        elif event_type == "us_risk":
            rm = engine.risk_manager
            metrics = rm.get_risk_metrics(engine.portfolio)
            ws_sub = 0
            ws_feed = getattr(engine, "ws_feed", None)
            if ws_feed:
                ws_sub = len(getattr(ws_feed, "_subscribed", set()))
            signals_count = len(getattr(engine, "recent_signals", []))
            return {
                "can_trade": metrics.can_trade,
                "daily_loss_pct": round(metrics.daily_loss_pct, 2),
                "daily_loss_limit_pct": rm.config.daily_max_loss_pct,
                "daily_trades": metrics.daily_trades,
                "position_count": len(engine.portfolio.positions),
                "max_positions": rm.config.max_positions,
                "consecutive_losses": metrics.consecutive_losses,
                "signals_generated": signals_count,
                "ws_subscribed": ws_sub,
            }

        return None

    def stop(self):
        """브로드캐스트 중지"""
        self._running = False
