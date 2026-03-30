"""
QWQ AI Trader - KR REST API 핸들러

KR(한국주식) 대시보드 API 엔드포인트.
모든 경로는 /api/* 에 마운트됩니다.
"""

import os
import time
import asyncio
from datetime import date, datetime

import aiohttp as aiohttp_client
from aiohttp import web
from loguru import logger

# 시장 지수 캐시 (5분)
_indices_cache: dict = {"data": None, "ts": 0.0}
_benchmark_cache: dict = {"data": None, "ts": 0.0, "range": ""}


def setup_kr_api_routes(app: web.Application, data_collector):
    """KR API 라우트 등록"""
    handler = KRAPIHandler(data_collector)

    app.router.add_get("/api/status", handler.get_status)
    app.router.add_get("/api/portfolio", handler.get_portfolio)
    app.router.add_get("/api/positions", handler.get_positions)
    app.router.add_get("/api/risk", handler.get_risk)
    app.router.add_get("/api/trades/today", handler.get_today_trades)
    app.router.add_get("/api/trades", handler.get_trades)
    app.router.add_get("/api/trades/stats", handler.get_trade_stats)
    app.router.add_get("/api/themes", handler.get_themes)
    app.router.add_get("/api/screening", handler.get_screening)
    app.router.add_get("/api/config", handler.get_config)
    app.router.add_get("/api/us-market", handler.get_us_market)
    app.router.add_get("/api/evolution", handler.get_evolution)
    app.router.add_get("/api/evolution/history", handler.get_evolution_history)
    app.router.add_get("/api/health", handler.get_system_health)
    app.router.add_get("/api/premarket", handler.get_premarket)
    app.router.add_get("/api/events", handler.get_events)
    app.router.add_get("/api/orders/pending", handler.get_pending_orders)
    app.router.add_get("/api/orders/history", handler.get_order_history)
    app.router.add_get("/api/equity-curve", handler.get_equity_curve)
    app.router.add_get("/api/health-checks", handler.get_health_checks)
    app.router.add_get("/api/accounts/positions", handler.get_external_accounts)
    app.router.add_get("/api/accounts/overseas", handler.get_ext_overseas)
    app.router.add_get("/api/equity-history", handler.get_equity_history)
    app.router.add_get("/api/equity-history/positions", handler.get_equity_history_positions)
    app.router.add_get("/api/daily-review", handler.get_daily_review)
    app.router.add_get("/api/daily-review/dates", handler.get_daily_review_dates)
    app.router.add_post("/api/evolution/apply", handler.apply_evolution_parameter)
    app.router.add_post("/api/signals/execute", handler.execute_pending_signals)
    app.router.add_post("/api/scan/run", handler.run_morning_scan)
    app.router.add_post("/api/sync-trades", handler.sync_trades)
    app.router.add_get("/api/trade-events", handler.get_trade_events)
    app.router.add_get("/api/daily-settlement", handler.get_daily_settlement)
    app.router.add_get("/api/app/latest", handler.get_latest_app)
    app.router.add_get("/api/market/indices", handler.get_market_indices)
    app.router.add_get("/api/core-holdings", handler.get_core_holdings)
    app.router.add_get("/api/benchmark", handler.get_benchmark)


class KRAPIHandler:
    """KR REST API 핸들러"""

    def __init__(self, data_collector):
        self.dc = data_collector

    async def get_status(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_status())

    async def get_portfolio(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_portfolio())

    async def get_positions(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_positions())

    async def get_risk(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_risk())

    async def get_today_trades(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_today_trades())

    async def get_trades(self, request: web.Request) -> web.Response:
        date_str = request.query.get("date")
        if date_str:
            try:
                trade_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                return web.json_response(
                    {"error": "Invalid date format. Use YYYY-MM-DD"},
                    status=400,
                )
        else:
            trade_date = date.today()

        return web.json_response(self.dc.get_trades_by_date(trade_date))

    async def get_trade_stats(self, request: web.Request) -> web.Response:
        try:
            days = int(request.query.get("days", "30"))
        except ValueError:
            return web.json_response({"error": "Invalid days parameter"}, status=400)
        days = max(1, min(days, 365))
        return web.json_response(await self.dc.get_trade_stats(days))

    async def get_themes(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_themes())

    async def get_screening(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_screening())

    async def get_config(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_config())

    async def get_us_market(self, request: web.Request) -> web.Response:
        return web.json_response(await self.dc.get_us_market())

    async def get_evolution(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_evolution())

    async def get_evolution_history(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_evolution_history())

    async def get_system_health(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_system_health())

    async def get_premarket(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_premarket())

    async def get_events(self, request: web.Request) -> web.Response:
        try:
            since_id = int(request.query.get("since", "0"))
        except ValueError:
            since_id = 0
        return web.json_response(self.dc.get_events(since_id))

    async def get_equity_curve(self, request: web.Request) -> web.Response:
        try:
            days = int(request.query.get("days", "30"))
        except ValueError:
            return web.json_response({"error": "Invalid days parameter"}, status=400)
        days = max(1, min(days, 90))
        return web.json_response(self.dc.get_equity_curve(days))

    async def get_pending_orders(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_pending_orders())

    async def get_order_history(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_order_history())

    async def get_health_checks(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_health_checks())

    async def get_external_accounts(self, request: web.Request) -> web.Response:
        return web.json_response(await self.dc.get_external_accounts())

    async def get_ext_overseas(self, request: web.Request) -> web.Response:
        return web.json_response(await self.dc.get_ext_overseas_positions())

    async def get_equity_history(self, request: web.Request) -> web.Response:
        date_from = request.query.get("from")
        date_to = request.query.get("to")
        if date_from and date_to:
            # 날짜 형식 검증
            try:
                datetime.strptime(date_from, "%Y-%m-%d")
                datetime.strptime(date_to, "%Y-%m-%d")
            except ValueError:
                return web.json_response(
                    {"error": "Invalid date format. Use YYYY-MM-DD"}, status=400
                )
            # 오늘 거래통계를 DB에서 비동기로 선조회 (inject 정확도 향상)
            today_stats = await self.dc._fetch_today_trade_stats_from_db()
            return web.json_response(
                self.dc.get_equity_history_range(date_from, date_to, today_stats=today_stats)
            )
        # fallback: days 파라미터
        try:
            days = int(request.query.get("days", "30"))
        except ValueError:
            return web.json_response({"error": "Invalid days parameter"}, status=400)
        days = max(1, min(days, 365))
        today_stats = await self.dc._fetch_today_trade_stats_from_db()
        return web.json_response(self.dc.get_equity_history(days, today_stats=today_stats))

    async def get_daily_review(self, request: web.Request) -> web.Response:
        date_str = request.query.get("date")
        if not date_str:
            # 기본값: 오늘
            date_str = date.today().isoformat()
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return web.json_response(
                {"error": "Invalid date format. Use YYYY-MM-DD"}, status=400
            )
        return web.json_response(self.dc.get_daily_review(date_str))

    async def get_daily_review_dates(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_daily_review_dates())

    async def get_equity_history_positions(self, request: web.Request) -> web.Response:
        date_str = request.query.get("date")
        if not date_str:
            return web.json_response({"error": "date parameter required"}, status=400)
        # 오늘 날짜 요청 시 DB에서 정확한 거래 통계 조회
        today_stats = None
        if date_str == date.today().isoformat():
            today_stats = await self.dc._fetch_today_trade_stats_from_db()
        return web.json_response(self.dc.get_equity_history_positions(date_str, today_stats=today_stats))

    async def apply_evolution_parameter(self, request: web.Request) -> web.Response:
        """
        파라미터 진화 추천 반영 + 봇 재시작

        POST /api/evolution/apply
        Body: {
            "strategy": "momentum_breakout",
            "parameter": "min_breakout_pct",
            "new_value": 0.8,
            "reason": "승률 향상을 위한 필터 강화"
        }
        """
        try:
            data = await request.json()
        except Exception as e:
            return web.json_response(
                {"success": False, "message": f"Invalid JSON: {e}"},
                status=400,
            )

        strategy = data.get("strategy")
        parameter = data.get("parameter")
        new_value = data.get("new_value")
        reason = data.get("reason", "대시보드 수동 반영")

        if not all([strategy, parameter, new_value is not None]):
            return web.json_response(
                {"success": False, "message": "strategy, parameter, new_value 필수"},
                status=400,
            )

        try:
            # 1. Config 업데이트 (evolved_config_manager 사용)
            from src.core.evolution.config_persistence import get_evolved_config_manager

            config_mgr = get_evolved_config_manager()
            config_mgr.save_override(
                component=strategy,
                param=parameter,
                value=new_value,
                source="dashboard",
            )

            logger.info(
                f"[대시보드] 파라미터 반영: {strategy}.{parameter} = {new_value} "
                f"(사유: {reason})"
            )

            # 2. 봇 재시작 예약 (3초 후)
            asyncio.create_task(self._restart_bot_delayed(3))

            return web.json_response({
                "success": True,
                "message": "파라미터가 적용되었습니다. 3초 후 봇이 재시작됩니다.",
            })

        except Exception as e:
            logger.error(f"[대시보드] 파라미터 반영 오류: {e}")
            return web.json_response(
                {"success": False, "message": str(e)},
                status=500,
            )

    async def execute_pending_signals(self, request: web.Request) -> web.Response:
        """
        대기 시그널 즉시 실행 (수동 트리거)

        POST /api/signals/execute
        """
        try:
            bot = self.dc.bot
            batch_analyzer = getattr(bot, "batch_analyzer", None)
            if batch_analyzer is None:
                return web.json_response(
                    {"success": False, "message": "batch_analyzer 미초기화"},
                    status=503,
                )
            logger.info("[대시보드] 대기 시그널 수동 실행 트리거")
            asyncio.create_task(batch_analyzer.execute_pending_signals())
            return web.json_response({"success": True, "message": "시그널 실행 시작 (비동기)"})
        except Exception as e:
            logger.error(f"[대시보드] 시그널 실행 오류: {e}")
            return web.json_response({"success": False, "message": str(e)}, status=500)

    async def run_morning_scan(self, request: web.Request) -> web.Response:
        """
        배치 스캔 즉시 실행 + 시그널 실행 (수동 풀백 트리거)

        POST /api/scan/run
        """
        try:
            bot = self.dc.bot
            batch_analyzer = getattr(bot, "batch_analyzer", None)
            if batch_analyzer is None:
                return web.json_response(
                    {"success": False, "message": "batch_analyzer 미초기화"},
                    status=503,
                )
            logger.info("[대시보드] 배치 스캔 수동 트리거 (풀백)")

            async def _run():
                await batch_analyzer.run_morning_scan()
                await batch_analyzer.execute_pending_signals()

            asyncio.create_task(_run())
            return web.json_response({"success": True, "message": "배치 스캔+실행 시작 (비동기)"})
        except Exception as e:
            logger.error(f"[대시보드] 배치 스캔 오류: {e}")
            return web.json_response({"success": False, "message": str(e)}, status=500)

    async def sync_trades(self, request: web.Request) -> web.Response:
        """
        KIS 당일 체결 기반 거래이력 동기화 (수동 트리거)

        POST /api/sync-trades
        """
        try:
            bot = self.dc.bot
            trade_journal = getattr(bot, "trade_journal", None)
            if trade_journal is None:
                return web.json_response(
                    {"success": False, "message": "trade_journal 미초기화"},
                    status=503,
                )
            if not hasattr(trade_journal, "sync_from_kis"):
                return web.json_response(
                    {"success": False, "message": "sync_from_kis 미지원"},
                    status=503,
                )
            logger.info("[대시보드] KIS 거래이력 동기화 수동 트리거")

            async def _run():
                await trade_journal.sync_from_kis(bot.broker, engine=getattr(bot, "engine", None))
                bot._last_kis_sync_date = None  # 오늘 장 마감 후 재동기화 허용
                logger.info("[대시보드] KIS 거래이력 동기화 완료")

            asyncio.create_task(_run())
            return web.json_response({"success": True, "message": "KIS 거래이력 동기화 시작 (비동기)"})
        except Exception as e:
            logger.error(f"[대시보드] 거래이력 동기화 오류: {e}")
            return web.json_response({"success": False, "message": str(e)}, status=500)

    async def get_trade_events(self, request: web.Request) -> web.Response:
        """거래 이벤트 로그 조회"""
        date_str = request.query.get("date")
        if date_str:
            try:
                target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                return web.json_response(
                    {"error": "Invalid date format. Use YYYY-MM-DD"}, status=400
                )
        else:
            target_date = date.today()

        event_type = request.query.get("type", "all")
        if event_type not in ("all", "buy", "sell"):
            event_type = "all"

        market = request.query.get("market", "all")
        if market not in ("all", "kr", "KR", "us", "US"):
            market = "all"

        events = await self.dc.get_trade_events(target_date, event_type, market=market)
        return web.json_response(events)

    async def get_daily_settlement(self, request: web.Request) -> web.Response:
        """일일 정산 (KIS 체결 기반)"""
        date_str = request.query.get("date")
        if date_str:
            try:
                target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                return web.json_response(
                    {"error": "Invalid date format. Use YYYY-MM-DD"}, status=400
                )
        else:
            target_date = date.today()
        return web.json_response(await self.dc.get_daily_settlement(target_date))

    async def get_latest_app(self, request: web.Request) -> web.Response:
        """최신 APK 파일 정보 반환"""
        from pathlib import Path
        static_dir = Path(__file__).parent / "static"
        apk_files = sorted(static_dir.glob("*.apk"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not apk_files:
            return web.json_response({"available": False})
        latest = apk_files[0]
        size_mb = round(latest.stat().st_size / (1024 * 1024), 1)
        return web.json_response({
            "available": True,
            "filename": latest.name,
            "url": f"/static/{latest.name}",
            "size_mb": size_mb,
            "modified": datetime.fromtimestamp(latest.stat().st_mtime).isoformat(),
        })

    async def get_market_indices(self, request: web.Request) -> web.Response:
        """KOSPI·KOSDAQ·S&P500·NASDAQ·DOW 지수 + KR 개별종목 (5분 캐시)
        - 지수: Yahoo Finance
        - KR 개별종목: KIS API 실시간 (프리/넥스트장 포함), 실패 시 Yahoo 폴백
        """
        global _indices_cache
        if _indices_cache["data"] and (time.time() - _indices_cache["ts"]) < 10:
            return web.json_response(_indices_cache["data"])

        from src.utils.session import KRSession
        from src.core.types import MarketSession

        # 현재 KR 세션 판단
        kr_session = KRSession().get_session()
        is_next = kr_session == MarketSession.NEXT

        index_symbols = [
            ("^KS11",  "KOSPI",  "index_kr"),
            ("^KQ11",  "KOSDAQ", "index_kr"),
            ("^GSPC",  "S&P500", "index_us"),
            ("^IXIC",  "NASDAQ", "index_us"),
            ("^DJI",   "DOW",    "index_us"),
        ]
        # 순서: 펩트론, SK하이닉스, 삼성전자
        kr_stocks = [
            ("087010", "펩트론",    "stock_kr"),
            ("000660", "SK하이닉스", "stock_kr"),
            ("005930", "삼성전자",  "stock_kr"),
        ]

        results = []

        # Yahoo Finance 세션 1회 생성, 지수 + KR 폴백에서 재사용
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        timeout = aiohttp_client.ClientTimeout(total=6)
        async with aiohttp_client.ClientSession(headers=headers, timeout=timeout) as yahoo_sess:
            # ── 1. 지수: Yahoo Finance ─────────────────────────────
            try:
                for sym, label, kind in index_symbols:
                    try:
                        # 스파크라인(1mo)이 필요한 지수는 범위 확대
                        need_spark = sym in ("^KS11", "^GSPC")
                        range_param = "1mo" if need_spark else "5d"
                        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range={range_param}"
                        async with yahoo_sess.get(url) as resp:
                            if resp.status != 200:
                                continue
                            js = await resp.json(content_type=None)
                            meta = js["chart"]["result"][0]["meta"]
                            price = meta.get("regularMarketPrice") or 0
                            raw_closes = js["chart"]["result"][0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
                            closes = [c for c in raw_closes if c is not None]
                            prev = closes[-2] if len(closes) >= 2 else (closes[-1] if closes else price)
                            chg = price - prev
                            chg_pct = (chg / prev * 100) if prev else 0
                            item = {
                                "symbol": sym, "label": label, "kind": kind,
                                "price": round(price, 2),
                                "change": round(chg, 2),
                                "change_pct": round(chg_pct, 2),
                            }
                            # 스파크라인 데이터 (KOSPI·S&P500만)
                            if need_spark and len(closes) >= 5:
                                item["sparkline"] = [round(c, 2) for c in closes]
                            results.append(item)
                    except Exception as e:
                        logger.debug(f"[지수] {sym} 오류: {e}")
            except Exception as e:
                logger.debug(f"[지수] 조회 오류: {e}")

            # ── 2. KR 개별종목: KIS API ──────────────────────────────
            broker = getattr(getattr(self.dc, 'bot', None), 'broker', None)
            for sym, label, kind in kr_stocks:
                item = None
                if broker:
                    try:
                        q = await broker.get_quote(sym)
                        if q and q.get("price", 0) > 0:
                            # 넥스트장(시간외단일가) / 일반
                            if is_next and q.get("ovtm_price", 0) > 0:
                                price    = q["ovtm_price"]
                                chg_pct  = q.get("ovtm_change_pct", 0)
                                prev     = q.get("prev_close", price)
                                chg      = price - prev
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
                        logger.debug(f"[KIS 시세] {sym} 오류: {e}")

                # KIS 실패 → Yahoo Finance 폴백 (세션 재사용)
                if not item:
                    try:
                        yf_sym = sym + ".KS"
                        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_sym}?interval=1d&range=5d"
                        async with yahoo_sess.get(url) as resp:
                            if resp.status == 200:
                                js = await resp.json(content_type=None)
                                meta = js["chart"]["result"][0]["meta"]
                                price = meta.get("regularMarketPrice") or 0
                                raw_closes = js["chart"]["result"][0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
                                closes = [c for c in raw_closes if c is not None]
                                prev = closes[-2] if len(closes) >= 2 else price
                                chg = price - prev
                                chg_pct = (chg / prev * 100) if prev else 0
                                item = {
                                    "symbol": sym, "label": label, "kind": kind,
                                    "price": round(price), "change": round(chg),
                                    "change_pct": round(chg_pct, 2), "source": "yahoo",
                                }
                    except Exception as e:
                        logger.debug(f"[Yahoo 폴백] {sym} 오류: {e}")

                if item:
                    results.append(item)

        if results:
            _indices_cache["data"] = results
            _indices_cache["ts"]   = time.time()
        elif _indices_cache["data"]:
            return web.json_response(_indices_cache["data"])
        return web.json_response(results)

    async def get_core_holdings(self, request: web.Request) -> web.Response:
        """코어홀딩 데이터"""
        return web.json_response(self.dc.get_core_holdings())

    async def get_benchmark(self, request: web.Request) -> web.Response:
        """KOSPI 벤치마크 데이터 (Yahoo Finance)"""
        global _benchmark_cache
        try:
            days = int(request.query.get("days", "30"))
        except ValueError:
            return web.json_response({"error": "Invalid days"}, status=400)
        days = max(7, min(days, 1825))

        if days <= 14:
            yf_range = "1mo"
        elif days <= 45:
            yf_range = "3mo"
        elif days <= 120:
            yf_range = "6mo"
        elif days <= 400:
            yf_range = "2y"
        else:
            yf_range = "5y"

        if (_benchmark_cache["data"]
                and _benchmark_cache["range"] == yf_range
                and (time.time() - _benchmark_cache["ts"]) < 600):
            return web.json_response(_benchmark_cache["data"])

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        timeout = aiohttp_client.ClientTimeout(total=10)
        try:
            async with aiohttp_client.ClientSession(headers=headers, timeout=timeout) as sess:
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/^KS11?interval=1d&range={yf_range}"
                async with sess.get(url) as resp:
                    if resp.status != 200:
                        return web.json_response([])
                    js = await resp.json(content_type=None)
                    result = js["chart"]["result"][0]
                    timestamps = result.get("timestamp", [])
                    closes = result["indicators"]["quote"][0].get("close", [])
                    data = []
                    for ts_val, close in zip(timestamps, closes):
                        if close is not None:
                            dt = datetime.fromtimestamp(ts_val).strftime("%Y-%m-%d")
                            data.append({"date": dt, "close": round(close, 2)})

                    # Yahoo Finance 지연 보완: KIS API로 최근 KOSPI 보충
                    today_str = datetime.now().strftime("%Y-%m-%d")
                    last_date = data[-1]["date"] if data else ""
                    if last_date < today_str:
                        broker = getattr(getattr(self.dc, 'bot', None), 'broker', None)
                        if broker:
                            try:
                                # KIS 국내지수 현재가 (KOSPI)
                                q = await broker.get_quote("0001")  # KOSPI 지수코드
                                if q and q.get("price", 0) > 0:
                                    data.append({"date": today_str, "close": round(q["price"], 2)})
                            except Exception:
                                pass
                        if data[-1]["date"] < today_str:
                            # broker 실패 시 market_indices에서 KOSPI 가져오기
                            try:
                                cached = _indices_cache.get("data") or []
                                for item in cached:
                                    if item.get("label") == "KOSPI" and item.get("price", 0) > 0:
                                        data.append({"date": today_str, "close": round(item["price"], 2)})
                                        break
                            except Exception:
                                pass

                    _benchmark_cache = {"data": data, "ts": time.time(), "range": yf_range}
                    return web.json_response(data)
        except Exception as e:
            logger.debug(f"[벤치마크] KOSPI 조회 오류: {e}")
            return web.json_response([])

    async def _restart_bot_delayed(self, delay_seconds: int):
        """봇 재시작 (지연 실행)"""
        await asyncio.sleep(delay_seconds)
        logger.warning("[대시보드] 파라미터 적용 완료 → 봇 재시작")
        import sys
        sys.exit(0)  # systemd/supervisor가 재시작 (graceful shutdown)
