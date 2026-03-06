"""
QWQ AI Trader - US REST API 핸들러

US(미국주식) 대시보드 API 엔드포인트.
모든 경로는 /api/us/* 에 마운트됩니다.
"""

from __future__ import annotations

import csv
from datetime import datetime, date as date_type
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web
from loguru import logger

if TYPE_CHECKING:
    from src.core.us_live_engine import LiveEngine

VERSION = "1.0.0"


def setup_us_api_routes(app: web.Application, engine):
    """US API 라우트 등록"""
    handler = USAPIHandler(engine)

    app.router.add_get("/api/us/health", handler.handle_health)
    app.router.add_get("/api/us/status", handler.handle_status)
    app.router.add_get("/api/us/portfolio", handler.handle_portfolio)
    app.router.add_get("/api/us/positions", handler.handle_positions)
    app.router.add_get("/api/us/signals", handler.handle_signals)
    app.router.add_get("/api/us/orders", handler.handle_orders)
    app.router.add_get("/api/us/trades", handler.handle_trades)
    app.router.add_get("/api/us/themes", handler.handle_themes)
    app.router.add_get("/api/us/screening", handler.handle_screening)
    app.router.add_get("/api/us/risk", handler.handle_risk)
    app.router.add_get("/api/us/statistics", handler.handle_statistics)
    app.router.add_get("/api/us/trade-events", handler.handle_trade_events)
    app.router.add_get("/api/us/equity-history", handler.handle_equity_history)


class USAPIHandler:
    """US REST API 핸들러"""

    def __init__(self, engine):
        self.engine = engine

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def handle_status(self, request: web.Request) -> web.Response:
        engine = self.engine
        session = getattr(engine, "session", None)
        session_value = "closed"
        if session:
            try:
                session_value = session.get_session().value
            except Exception:
                pass

        # 브로커 및 운영 환경 정보 (대시보드 TEST 배지 표시용)
        live_cfg = getattr(engine, "_live_cfg", {}) or {}
        broker_name = live_cfg.get("broker", "kis")
        env = live_cfg.get("env", "prod")
        is_paper = broker_name == "alpaca_paper" or env == "dev"

        return web.json_response({
            "running": getattr(engine, "_running", False),
            "session": session_value,
            "timestamp": datetime.now().isoformat(),
            "version": VERSION,
            "broker": broker_name,
            "env": env,
            "paper_trading": is_paper,   # True → 대시보드에서 TEST 배지 표시
        })

    async def handle_portfolio(self, request: web.Request) -> web.Response:
        portfolio = self.engine.portfolio
        total_value = float(portfolio.total_equity)
        positions_value = float(portfolio.total_position_value)
        daily_pnl = float(portfolio.effective_daily_pnl)
        initial = float(portfolio.initial_capital)
        daily_pnl_pct = (daily_pnl / initial * 100) if initial else 0.0

        return web.json_response({
            "cash": float(portfolio.cash),
            "total_value": total_value,
            "positions_value": positions_value,
            "daily_pnl": daily_pnl,
            "daily_pnl_pct": round(daily_pnl_pct, 2),
            "positions_count": len(portfolio.positions),
        })

    async def handle_positions(self, request: web.Request) -> web.Response:
        positions = []
        exit_mgr = getattr(self.engine, "exit_manager", None)
        for symbol, pos in self.engine.portfolio.positions.items():
            entry_time = getattr(pos, "entry_time", None)
            # ExitManager에서 stage 조회
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
        return web.json_response(positions)

    async def handle_signals(self, request: web.Request) -> web.Response:
        signals = list(getattr(self.engine, "recent_signals", []))
        return web.json_response(signals)

    async def handle_orders(self, request: web.Request) -> web.Response:
        orders = []
        for order_no, info in dict(getattr(self.engine, "_pending_orders", {})).items():
            orders.append({
                "order_no": order_no,
                "symbol": info.get("symbol", ""),
                "side": info.get("side", ""),
                "quantity": info.get("qty", 0),
                "price": float(info.get("price", 0)),
                "status": "pending",
                "timestamp": info.get("submitted_at", datetime.now()).isoformat(),
            })
        return web.json_response(orders)

    async def handle_trades(self, request: web.Request) -> web.Response:
        """거래 내역 반환 (KR 방식과 동일: trade_events + trades 테이블 통합)

        - trade_events: BUY/SELL 이벤트 (event_time 기준)
        - trades: SELL이 trade_events에 없는 청산 거래 보완
        - 미청산 BUY: 현재가 보강
        """
        from datetime import date as _date

        ts = getattr(self.engine, 'trade_storage', None)
        date_str = request.rel_url.query.get("date", "")
        target_date = None
        if date_str:
            try:
                target_date = _date.fromisoformat(date_str)
            except ValueError:
                pass

        events: list[dict] = []

        if ts and ts._db_available and ts.pool:
            try:
                async with ts.pool.acquire() as conn:

                    # ── 1. trade_events 기반 조회 (event_time 기준, market='US') ──
                    if target_date:
                        te_rows = await conn.fetch(
                            """SELECT te.event_type, te.symbol, te.name, te.quantity, te.price,
                                      te.event_time, te.strategy, te.pnl, te.pnl_pct,
                                      te.exit_type, te.exit_reason, te.trade_id, te.status
                               FROM trade_events te
                               JOIN trades t ON te.trade_id = t.id
                               WHERE te.event_time::date = $1 AND t.market = 'US'
                               ORDER BY te.event_time DESC LIMIT 300""",
                            target_date,
                        )
                    else:
                        te_rows = await conn.fetch(
                            """SELECT te.event_type, te.symbol, te.name, te.quantity, te.price,
                                      te.event_time, te.strategy, te.pnl, te.pnl_pct,
                                      te.exit_type, te.exit_reason, te.trade_id, te.status
                               FROM trade_events te
                               JOIN trades t ON te.trade_id = t.id
                               WHERE te.event_time >= NOW() - INTERVAL '7 days' AND t.market = 'US'
                               ORDER BY te.event_time DESC LIMIT 300""",
                        )

                    te_sell_trade_ids: set = set()
                    for r in te_rows:
                        evt = r["event_type"].upper()
                        side = "buy" if evt == "BUY" else "sell"
                        if evt == "SELL":
                            te_sell_trade_ids.add(r["trade_id"])
                        events.append({
                            "timestamp": r["event_time"].isoformat() if r["event_time"] else "",
                            "symbol": r["symbol"],
                            "name": r["name"] or "",
                            "side": side,
                            "entry_price": float(r["price"]) if side == "buy" else 0,
                            "exit_price": float(r["price"]) if side == "sell" else 0,
                            "quantity": int(r["quantity"]),
                            "pnl": round(float(r["pnl"] or 0), 2),
                            "pnl_pct": round(float(r["pnl_pct"] or 0), 2),
                            "strategy": r["strategy"] or "",
                            "reason": r["exit_reason"] or "",
                            "exit_type": r["exit_type"] or evt,
                            "holding_minutes": 0,
                            "trade_id": r["trade_id"] or "",
                            "market": "US",
                            "status": r["status"] or "",
                        })

                    # ── 2. trades 테이블: SELL이 trade_events에 없는 청산 보완 ──
                    # (분할매도가 아닌데 trade_events SELL이 누락된 케이스 커버)
                    if target_date:
                        closed_rows = await conn.fetch(
                            """SELECT id, symbol, name, entry_time, entry_price, entry_quantity,
                                      exit_time, exit_price, exit_quantity, exit_type, exit_reason,
                                      pnl, pnl_pct, entry_strategy
                               FROM trades
                               WHERE market = 'US'
                                 AND exit_time IS NOT NULL
                                 AND (entry_time::date = $1 OR exit_time::date = $1)
                               ORDER BY exit_time DESC""",
                            target_date,
                        )
                    else:
                        closed_rows = await conn.fetch(
                            """SELECT id, symbol, name, entry_time, entry_price, entry_quantity,
                                      exit_time, exit_price, exit_quantity, exit_type, exit_reason,
                                      pnl, pnl_pct, entry_strategy
                               FROM trades
                               WHERE market = 'US'
                                 AND exit_time IS NOT NULL
                                 AND exit_time >= NOW() - INTERVAL '7 days'
                               ORDER BY exit_time DESC""",
                        )

                    for r in closed_rows:
                        if r["id"] in te_sell_trade_ids:
                            continue  # trade_events에 이미 존재
                        exit_qty = r["exit_quantity"] or 0
                        if exit_qty <= 0:
                            continue
                        # trade_events 누락 SELL 보완
                        events.append({
                            "timestamp": r["exit_time"].isoformat() if r["exit_time"] else "",
                            "symbol": r["symbol"],
                            "name": r["name"] or "",
                            "side": "sell",
                            "entry_price": 0,
                            "exit_price": float(r["exit_price"] or 0),
                            "quantity": int(exit_qty),
                            "pnl": round(float(r["pnl"] or 0), 2),
                            "pnl_pct": round(float(r["pnl_pct"] or 0), 2),
                            "strategy": r["entry_strategy"] or "",
                            "reason": r["exit_reason"] or "",
                            "exit_type": r["exit_type"] or "closed",
                            "holding_minutes": 0,
                            "trade_id": r["id"],
                            "market": "US",
                            "status": r["exit_type"] or "closed",
                        })

                # ── 3. 미청산 BUY: 현재가/평가손익 보강 ──
                portfolio = self.engine.portfolio
                for ev in events:
                    if ev["side"] == "buy" and ev.get("status", "") == "holding":
                        pos = portfolio.positions.get(ev["symbol"])
                        if pos and pos.avg_price:
                            ev["current_price"] = float(pos.current_price)
                            qty = ev["quantity"]
                            ev["pnl"] = round(float(pos.current_price - pos.avg_price) * qty, 2)
                            ev["pnl_pct"] = round(
                                float((pos.current_price - pos.avg_price) / pos.avg_price * 100), 2
                            )

                events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
                return web.json_response(events[:200])

            except Exception as e:
                logger.warning(f"[US API] trades DB 조회 실패: {e}")

        # ── CSV 폴백 ──
        journal_path = Path(__file__).parent.parent.parent / "data" / "journal" / "trades.csv"
        trades: list[dict] = []
        if journal_path.exists():
            with open(journal_path, newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if date_str and not row.get("timestamp", "").startswith(date_str):
                        continue
                    trades.append({
                        "timestamp": row.get("timestamp", ""),
                        "symbol": row.get("symbol", ""),
                        "name": row.get("name", ""),
                        "side": row.get("side", ""),
                        "entry_price": float(row.get("entry_price", 0) or 0),
                        "exit_price": float(row.get("exit_price", 0) or 0),
                        "quantity": float(row.get("quantity", 0) or 0),
                        "pnl": float(row.get("pnl", 0) or 0),
                        "pnl_pct": float(row.get("pnl_pct", 0) or 0),
                        "strategy": row.get("strategy", ""),
                        "reason": row.get("reason", ""),
                        "holding_minutes": float(row.get("holding_minutes", 0) or 0),
                        "market": "US",
                    })
        return web.json_response(list(reversed(trades[-200:])))

    async def handle_themes(self, request: web.Request) -> web.Response:
        """US 테마 목록 반환"""
        detector = getattr(self.engine, "theme_detector", None)
        if not detector:
            return web.json_response([])
        return web.json_response(detector.to_dict_list())

    async def handle_risk(self, request: web.Request) -> web.Response:
        """US 리스크 정보"""
        engine = self.engine
        rm = engine.risk_manager
        metrics = rm.get_risk_metrics(engine.portfolio)

        # WS 구독 수
        ws_sub = 0
        ws_feed = getattr(engine, "ws_feed", None)
        if ws_feed:
            ws_sub = len(getattr(ws_feed, "_subscribed", set()))

        # 신호 생성 수
        signals_count = len(getattr(engine, "recent_signals", []))

        return web.json_response({
            "can_trade": metrics.can_trade,
            "daily_loss_pct": round(metrics.daily_loss_pct, 2),
            "daily_loss_limit_pct": rm.config.daily_max_loss_pct,
            "daily_trades": metrics.daily_trades,
            "daily_max_trades": 999,
            "position_count": len(engine.portfolio.positions),
            "max_positions": rm.config.max_positions,
            "consecutive_losses": metrics.consecutive_losses,
            "signals_generated": signals_count,
            "ws_subscribed": ws_sub,
        })

    async def handle_statistics(self, request: web.Request) -> web.Response:
        """거래 통계 (DB 우선, 캐시 폴백)"""
        days = int(request.rel_url.query.get("days", "30"))
        ts = getattr(self.engine, 'trade_storage', None)
        if ts and ts._db_available:
            stats = await ts.get_statistics_from_db(days=days)
        elif ts:
            stats = ts.get_statistics(days=days)
        else:
            stats = self.engine.journal.get_summary()
        return web.json_response(stats)

    async def handle_trade_events(self, request: web.Request) -> web.Response:
        """거래 이벤트 로그 (분할매도 추적)"""
        ts = getattr(self.engine, 'trade_storage', None)
        if not ts:
            return web.json_response([])
        date_str = request.rel_url.query.get("date", "")
        event_type = request.rel_url.query.get("type", "all")
        target_date = None
        if date_str:
            try:
                parts = date_str.split("-")
                target_date = date_type(int(parts[0]), int(parts[1]), int(parts[2]))
            except (ValueError, IndexError):
                pass
        events = await ts.get_trade_events(
            target_date=target_date, event_type=event_type
        )
        return web.json_response(events)

    async def handle_screening(self, request: web.Request) -> web.Response:
        """스크리너 결과 반환 (상위 50개)"""
        result = getattr(self.engine, "_last_screen_result", None)
        if not result:
            return web.json_response([])

        def _safe_round(val, digits=2):
            return round(val, digits) if val is not None else 0

        items = []
        for r in result.results[:50]:
            items.append({
                "symbol": r.symbol,
                "price": r.close if r.close is not None else 0,
                "change_pct": _safe_round(r.change_1d),
                "change_5d": _safe_round(r.change_5d),
                "volume": r.volume if r.volume is not None else 0,
                "avg_volume": r.avg_volume if r.avg_volume is not None else 0,
                "vol_ratio": _safe_round(r.vol_ratio),
                "rsi": _safe_round(r.rsi, 1),
                "pct_from_52w_high": _safe_round(r.pct_from_52w_high, 1),
                "atr_pct": _safe_round(r.atr_pct),
                "score": _safe_round(r.score, 1),
                "total_score": _safe_round(r.total_score, 1),
                "finviz_bonus": _safe_round(r.finviz_bonus, 1),
                "finviz_meta": r.finviz_meta if r.finviz_meta else {},
                "flags": r.flags if r.flags is not None else [],
            })
        return web.json_response(items)

    async def handle_equity_history(self, request: web.Request) -> web.Response:
        """US 일별 자산 히스토리 (KR equity-history와 동일한 포맷)"""
        import asyncpg
        from decimal import Decimal

        eng = self.engine
        ts = getattr(eng, 'trade_storage', None)
        pool = getattr(ts, 'pool', None) if ts else None

        # ── 날짜 파라미터 ──
        today_str = date_type.today().isoformat()
        date_from = request.rel_url.query.get('from', None)
        date_to   = request.rel_url.query.get('to', today_str)
        days_param = request.rel_url.query.get('days', None)
        if days_param and not date_from:
            try:
                from datetime import timedelta
                ndays = min(int(days_param), 9999)
                date_from = (date_type.today() - timedelta(days=ndays)).isoformat()
            except ValueError:
                date_from = '2020-01-01'
        if not date_from:
            date_from = '2020-01-01'

        snapshots = []
        if pool:
            try:
                # 날짜별 집계: 매수/매도 건수, 당일 실현손익
                rows = await pool.fetch("""
                    SELECT
                        te.event_time::date AS day,
                        COUNT(CASE WHEN te.event_type='BUY'  THEN 1 END) AS buys,
                        COUNT(CASE WHEN te.event_type='SELL' THEN 1 END) AS sells,
                        COALESCE(SUM(CASE WHEN te.event_type='SELL' THEN te.pnl ELSE 0 END), 0) AS daily_pnl,
                        COALESCE(SUM(CASE WHEN te.event_type='SELL' AND te.pnl > 0 THEN 1 END), 0) AS wins
                    FROM trade_events te
                    JOIN trades t ON te.trade_id = t.id
                    WHERE t.market = 'US'
                      AND te.event_time::date BETWEEN $1 AND $2
                    GROUP BY te.event_time::date
                    ORDER BY day
                """, date_type.fromisoformat(date_from), date_type.fromisoformat(date_to))

                # 미결 포지션 수: 특정 날짜에 buy 있고 exit_time 없는 것
                all_trades = await pool.fetch("""
                    SELECT symbol, name, entry_time::date AS entry_day,
                           exit_time::date AS exit_day, entry_price, entry_quantity,
                           exit_price, exit_quantity, pnl
                    FROM trades
                    WHERE market = 'US'
                      AND entry_time::date BETWEEN $1 AND $2
                    ORDER BY entry_time
                """, date_type.fromisoformat(date_from), date_type.fromisoformat(date_to))

                # 초기자산: engine.portfolio.initial_capital (USD)
                initial_capital = float(getattr(eng.portfolio, 'initial_capital', None) or 0)
                if initial_capital <= 0:
                    # 실제 current total이 최선의 추정치
                    initial_capital = float(
                        (eng.portfolio.cash or 0) +
                        sum(float(p.avg_price) * p.quantity for p in eng.portfolio.positions.values())
                    )

                # 날짜별 누적 PnL 계산
                cum_pnl = 0.0
                prev_equity = initial_capital
                for row in rows:
                    day_str = row['day'].isoformat()
                    dpnl = float(row['daily_pnl'])
                    sells = int(row['sells'])
                    wins = int(row['wins'])
                    cum_pnl += dpnl

                    # 해당 날짜 보유 포지션 수 (당일까지 매수됐고 아직 안 닫힌 것)
                    pos_count = sum(
                        1 for t in all_trades
                        if str(t['entry_day']) <= day_str
                        and (t['exit_day'] is None or str(t['exit_day']) > day_str)
                    )

                    # 오늘이면 live portfolio 값 사용
                    if day_str == today_str:
                        live_total = float(
                            eng.portfolio.total_equity
                            if hasattr(eng.portfolio, 'total_equity') and eng.portfolio.total_equity
                            else (eng.portfolio.cash or 0) +
                                 sum(float(p.current_price) * p.quantity for p in eng.portfolio.positions.values())
                        )
                        # KR 방식: 오늘 daily_pnl_pct = 어제 대비
                        day_equity = live_total
                        dpnl = day_equity - prev_equity
                    else:
                        day_equity = prev_equity + dpnl

                    pnl_pct = (dpnl / prev_equity * 100) if prev_equity > 0 else 0

                    # 오늘 포지션 상세: live positions
                    positions_detail = []
                    if day_str == today_str:
                        for sym, p in eng.portfolio.positions.items():
                            cur = float(p.current_price)
                            avg = float(p.avg_price)
                            qty = int(p.quantity)
                            upnl = (cur - avg) * qty
                            upnl_pct = (cur - avg) / avg * 100 if avg > 0 else 0
                            positions_detail.append({
                                "symbol": sym,
                                "name": sym,
                                "quantity": qty,
                                "avg_price": round(avg, 4),
                                "current_price": round(cur, 4),
                                "market_value": round(cur * qty, 2),
                                "pnl": round(upnl, 2),
                                "pnl_pct": round(upnl_pct, 2),
                            })

                    snapshots.append({
                        "date": day_str,
                        "total_equity": round(day_equity, 2),
                        "daily_pnl": round(dpnl, 2),
                        "daily_pnl_pct": round(pnl_pct, 2),
                        "cash": round(float(eng.portfolio.cash or 0), 2) if day_str == today_str else None,
                        "position_count": len(eng.portfolio.positions) if day_str == today_str else pos_count,
                        "trades_count": int(row['sells']),
                        "win_rate": (wins / sells * 100) if sells > 0 else 0.0,
                        "positions": positions_detail,
                        "currency": "USD",
                    })
                    prev_equity = day_equity

            except Exception as e:
                logger.warning(f"[US equity-history] DB 조회 실패: {e}")

        # 요약 통계
        summary: dict = {"oldest_date": snapshots[0]["date"] if snapshots else today_str}
        if snapshots:
            first_eq = snapshots[0]["total_equity"]
            last_eq  = snapshots[-1]["total_equity"]
            period_return = (last_eq - first_eq) / first_eq * 100 if first_eq > 0 else 0
            pnls = [s["daily_pnl"] for s in snapshots]
            avg_pnl = sum(pnls) / len(pnls) if pnls else 0
            # 최대 낙폭
            peak = snapshots[0]["total_equity"]
            max_dd = 0.0
            for s in snapshots:
                if s["total_equity"] > peak:
                    peak = s["total_equity"]
                dd = (peak - s["total_equity"]) / peak * 100 if peak > 0 else 0
                if dd > max_dd:
                    max_dd = dd
            summary.update({
                "period_return_pct": round(period_return, 2),
                "max_drawdown_pct":  round(-max_dd, 2),
                "avg_daily_pnl":     round(avg_pnl, 2),
                "oldest_date":       snapshots[0]["date"],
                "currency": "USD",
            })

        return web.json_response({"snapshots": snapshots, "summary": summary})
