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
        for symbol, pos in self.engine.portfolio.positions.items():
            entry_time = getattr(pos, "entry_time", None)
            positions.append({
                "symbol": symbol,
                "name": getattr(pos, "name", ""),
                "quantity": pos.quantity,
                "avg_price": float(pos.avg_price),
                "current_price": float(pos.current_price),
                "pnl": float(pos.unrealized_pnl),
                "pnl_pct": round(pos.unrealized_pnl_pct, 2),
                "strategy": pos.strategy or "",
                "stage": getattr(pos, "stage", ""),
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
        """거래 내역 반환 (DB 우선, CSV 폴백)"""
        # DB 사용 가능 시 TradeStorage에서 조회
        ts = getattr(self.engine, 'trade_storage', None)
        if ts and ts._db_available:
            days = int(request.rel_url.query.get("days", "30"))
            closed = ts.get_closed_trades(days=days)
            trades = []
            for t in closed:
                trades.append({
                    "timestamp": t.exit_time.isoformat() if t.exit_time else "",
                    "symbol": t.symbol,
                    "side": "sell",
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "quantity": t.entry_quantity,
                    "pnl": round(t.pnl, 2),
                    "pnl_pct": round(t.pnl_pct, 2),
                    "strategy": t.entry_strategy,
                    "reason": t.exit_reason,
                    "exit_type": t.exit_type,
                    "holding_minutes": t.holding_minutes,
                    "trade_id": t.id,
                    "market": "US",
                })
            trades.sort(key=lambda x: x["timestamp"], reverse=True)
            return web.json_response(trades[:200])

        # CSV 폴백
        date_str = request.rel_url.query.get("date", "")
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
