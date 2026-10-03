"""합성 브로커로 실제 KR 기동·현금 검증·보호 포지션 경로를 검증한다."""

import asyncio
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import run_trader
from src.core.types import OrderSide, Portfolio, Position, PositionSide, RiskConfig
from src.risk.manager import RiskManager


@pytest.fixture
def start_kr(monkeypatch, tmp_path):
    # 생성자는 운영 캐시 대신 테스트 전용 경로만 사용한다.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from src.execution.broker import kis_kr
    from src.data.providers import kis_market_data
    from src.signals.fundamentals import stock_validator
    from src.signals.sentiment import kr_theme_detector
    from src.core import evolution
    from src.data.storage import trade_storage
    from src.analytics import equity_tracker
    from src.core.evolution import daily_reviewer
    from src.utils import macro_calendar

    monkeypatch.setattr(macro_calendar, "is_macro_event_day", lambda: ("", False))

    monkeypatch.setattr(kis_kr.KISConfig, "from_env", lambda: None)
    monkeypatch.setattr(kis_market_data, "get_kis_market_data", lambda: SimpleNamespace(
        fetch_holidays=AsyncMock(return_value=set())))
    monkeypatch.setattr(stock_validator, "get_stock_validator", lambda: SimpleNamespace(
        initialize=AsyncMock()))
    monkeypatch.setattr(kr_theme_detector, "ThemeDetector", lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(kr_theme_detector, "_theme_detector", None)
    monkeypatch.setattr(evolution, "get_trade_journal", lambda: SimpleNamespace())
    monkeypatch.setattr(trade_storage, "sync_kis_journal", AsyncMock())

    class ReachedProtection(BaseException):
        pass

    def stop_after_protection():
        raise ReachedProtection

    real_equity_tracker = equity_tracker.EquityTracker
    monkeypatch.setattr(equity_tracker, "EquityTracker", stop_after_protection)

    def start(balance, *, positions_error=None, positions="held", dry_run=False, through_equity=False):
        if through_equity:
            monkeypatch.setattr(real_equity_tracker, "STORAGE_DIR", tmp_path / "equity")
            monkeypatch.setattr(equity_tracker, "EquityTracker", real_equity_tracker)
            monkeypatch.setattr(daily_reviewer, "DailyReviewer", stop_after_protection)
        held = Position(symbol="005930", quantity=2, side=PositionSide.LONG,
                        avg_price=Decimal("10000"), current_price=Decimal("10000"))
        broker = SimpleNamespace(
            connect=AsyncMock(return_value=True),
            get_account_balance=AsyncMock(
                return_value=balance if not isinstance(balance, Exception) else None,
                side_effect=balance if isinstance(balance, Exception) else None),
            get_positions=AsyncMock(return_value={held.symbol: held} if positions == "held" else positions,
                                    side_effect=positions_error),
            hold_buys_for_live_orders_at_restart=AsyncMock(),
        )
        broker.set_cash_verification = lambda verified: setattr(broker, "cash_verified", verified)
        monkeypatch.setattr(kis_kr, "KISBroker", lambda **kwargs: broker)

        class Config(dict):
            pass

        config = Config(kr={
            "stock_master": {"enabled": False},
            "strategies": {name: {"enabled": False} for name in (
                "momentum_breakout", "theme_chasing", "gap_and_go")},
            "trading_team": {"enabled": False},
            "evolution": {"enabled": False},
        })
        config.raw = {"experts": {"enabled": False}}
        config.trading = SimpleNamespace(initial_capital=Decimal("10000000"), risk=RiskConfig())
        bot = object.__new__(run_trader.UnifiedTradingBot)
        bot.config = config
        bot.dry_run = dry_run
        bot._token_manager = None
        bot.risk_manager = None
        bot.broker = None
        bot.stock_master = None
        bot.stock_name_cache = {}
        bot.ws_feed = None
        bot.engine = SimpleNamespace(portfolio=Portfolio(cash=Decimal("10000000")),
                                     register_handler=lambda *args: None)
        bot._get_price_history_for_atr = lambda symbol: []
        with pytest.raises(ReachedProtection):
            asyncio.run(bot._initialize_kr())
        return bot

    return start


def buy_decision(bot):
    return bot.risk_manager.can_open_position(
        symbol="000660", side=OrderSide.BUY, quantity=1, price=Decimal("10000"),
        portfolio=bot.engine.portfolio)


@pytest.mark.parametrize("balance", [
    {"total_equity": 9000000, "available_cash": 8000000, "available_cash_verified": False},
    {"total_equity": 9000000},
    {"total_equity": 9000000, "available_cash": "NaN"},
    {"total_equity": 9000000, "available_cash": "Infinity"},
    {"total_equity": 9000000, "available_cash": -1},
    {"total_equity": 9000000, "available_cash": None},
    {},
    RuntimeError("synthetic balance failure"),
])
def test_unverified_startup_keeps_positions_and_never_invents_cash(start_kr, balance):
    bot = start_kr(balance)
    assert bot.engine.portfolio.cash == 0
    assert bot.engine.portfolio.initial_capital == 0
    assert bot.config.trading.initial_capital == 0
    assert bot.risk_manager.initial_capital == 0
    assert bot.engine.portfolio.positions["005930"].quantity == 2
    assert "005930" in bot.exit_manager._states
    # 다른 경로에서 현금이 늘어도 현금 검증 전 신규 BUY는 명시적으로 거부한다.
    bot.engine.portfolio.cash = Decimal("1000000")
    allowed, reason = buy_decision(bot)
    assert not allowed
    assert "현금" in reason and "검증" in reason
    assert bot.broker.cash_verified is False


@pytest.mark.parametrize("cash, capital", [(0, 20000), (100000, 120000)])
def test_verified_cash_including_zero_sets_baseline_after_positions(start_kr, cash, capital):
    bot = start_kr({"available_cash": cash, "available_cash_verified": True,
                    "total_equity": 9000000, "stock_value": 20000})
    assert bot.engine.portfolio.cash == cash
    assert bot.engine.portfolio.initial_capital == capital
    assert bot.config.trading.initial_capital == capital
    assert bot.risk_manager.initial_capital == capital
    assert not bot._kr_capital_baseline_pending
    assert bot._kr_cash_verified


def test_cash_hold_survives_timeout_daily_reset_and_sync_recovery(start_kr):
    bot = start_kr({"total_equity": 1000000, "available_cash": 900000,
                    "available_cash_verified": False})
    risk = bot.risk_manager
    bot.engine.portfolio.cash = Decimal("1000000")
    for _ in range(3):
        risk.set_sync_status(False)
    risk._sync_unhealthy_since = datetime.now() - timedelta(days=2)
    risk.daily_stats.date = date.today() - timedelta(days=1)
    risk.reset_daily_stats()
    assert not buy_decision(bot)[0]
    risk.set_sync_status(True)
    allowed, reason = buy_decision(bot)
    assert not allowed and "현금" in reason


def test_first_verified_sync_confirms_baseline_once_and_releases_hold(start_kr):
    bot = start_kr({})
    bot.engine.portfolio.positions["005930"].quantity = 3
    bot._confirm_kr_cash(Decimal("1000000"))
    assert bot.engine.portfolio.cash == 1000000
    assert bot.engine.portfolio.initial_capital == 1030000
    assert bot.config.trading.initial_capital == 1030000
    assert bot.risk_manager.initial_capital == 1030000
    assert bot.risk_manager.daily_stats.peak_equity == 1030000
    assert buy_decision(bot)[0]
    assert bot.broker.cash_verified is True
    bot._confirm_kr_cash(Decimal("2000000"))
    assert bot.engine.portfolio.cash == 2000000
    assert bot.engine.portfolio.initial_capital == 1030000
    assert bot.config.trading.initial_capital == 1030000
    assert bot.risk_manager.initial_capital == 1030000


def test_first_confirmation_preserves_restored_daily_loss_and_peak(start_kr, tmp_path):
    stats_path = tmp_path / ".cache" / "ai_trader" / "daily_stats.json"
    stats_path.parent.mkdir(parents=True)
    stats_path.write_text(json.dumps({
        "date": date.today().isoformat(), "trades": 4, "losses": 3,
        "total_pnl": "-200000", "peak_equity": "2000000",
    }))
    bot = start_kr({})
    bot._confirm_kr_cash(Decimal("1000000"))
    assert bot.risk_manager.initial_capital == 1020000
    assert bot.risk_manager.daily_stats.peak_equity == 2000000
    assert bot.risk_manager.daily_stats.total_pnl == -200000
    assert bot.risk_manager.daily_stats.trades == 4
    assert bot.risk_manager.daily_stats.losses == 3


def test_positions_failure_defers_baseline_even_with_valid_cash(start_kr):
    bot = start_kr({"total_equity": 1000000, "available_cash": 1000000},
                   positions_error=RuntimeError("synthetic positions failure"))
    assert bot.engine.portfolio.initial_capital == 0
    assert bot._kr_capital_baseline_pending
    assert not buy_decision(bot)[0]


def test_empty_positions_against_positive_balance_stock_value_defers_baseline(start_kr):
    bot = start_kr({"available_cash": 1000000, "stock_value": 20000}, positions={})
    assert bot.engine.portfolio.initial_capital == 0
    assert bot._kr_capital_baseline_pending
    bot.engine.portfolio.cash = Decimal("1000000")
    assert not buy_decision(bot)[0]


def test_zero_cash_and_empty_holdings_are_a_valid_zero_capital_account(start_kr):
    bot = start_kr({"available_cash": 0, "stock_value": 0}, positions={})
    assert bot.engine.portfolio.cash == 0
    assert bot.engine.portfolio.initial_capital == 0
    assert bot.config.trading.initial_capital == 0
    assert bot.risk_manager.initial_capital == 0
    assert bot.engine.portfolio.total_pnl_pct == 0
    assert bot._kr_cash_verified
    assert not bot._kr_capital_baseline_pending


def test_unknown_positions_defer_cash_confirmation(start_kr):
    bot = start_kr({"available_cash": 1000000}, positions=None)
    assert bot.engine.portfolio.initial_capital == 0
    assert bot._kr_capital_baseline_pending
    assert not buy_decision(bot)[0]


@pytest.mark.parametrize("cash", [Decimal("NaN"), Decimal("Infinity"), Decimal("-1")])
def test_confirmation_cannot_release_hold_with_invalid_cash(start_kr, cash):
    bot = start_kr({})
    with pytest.raises(ValueError):
        bot._confirm_kr_cash(cash)
    assert bot.engine.portfolio.cash == 0
    assert bot.engine.portfolio.initial_capital == 0
    bot.engine.portfolio.cash = Decimal("1000000")
    assert not buy_decision(bot)[0]


def test_cash_verification_hold_does_not_block_sell(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    risk = RiskManager(RiskConfig(), Decimal("1000000"))
    portfolio = Portfolio(cash=Decimal("1000000"), initial_capital=Decimal("1000000"))
    risk.set_cash_verification(False)
    allowed, reason = risk.can_open_position(
        "005930", OrderSide.SELL, 1, Decimal("10000"), portfolio)
    assert allowed, reason


def test_dry_run_retains_explicit_simulated_capital(start_kr):
    bot = start_kr({}, dry_run=True)
    assert bot.engine.portfolio.cash == 10000000
    assert bot.engine.portfolio.initial_capital == 10000000
    assert "현금" not in buy_decision(bot)[1]


@pytest.mark.parametrize("verified", [False, True])
def test_startup_never_backfills_equity_with_unconfirmed_baseline(start_kr, tmp_path, verified):
    folder = tmp_path / "equity"
    folder.mkdir()
    (folder / "trades_20260105.json").write_text(json.dumps({"trades": [
        {"exit_time": "2026-01-05T15:00:00", "pnl": 50000}]}))
    bot = start_kr({"available_cash": 1000000, "available_cash_verified": verified,
                    "stock_value": 20000}, through_equity=True)
    target = folder / "equity_20260105.json"
    if verified:
        assert target.exists()
        assert json.loads(target.read_text())["total_equity"] == 1070000
    else:
        assert bot._kr_capital_baseline_pending is True
        assert not list(folder.glob("equity_*.json"))


@pytest.mark.parametrize("positions_recover", [False, True])
def test_first_sync_requires_positions_when_balance_reports_stock_value(monkeypatch, tmp_path, positions_recover):
    from types import MethodType
    from test_sync_portfolio_characterization import _make, _pos
    from src.utils import macro_calendar
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(macro_calendar, "is_macro_event_day", lambda: ("", False))
    held = _pos("005930", qty=2, avg="10000", cur="10000")
    sched, bot, sleeps = _make(monkeypatch, bot_positions=[], cash="0",
        balance={"available_cash": 1000000, "available_cash_verified": True, "stock_value": 20000},
        kis_seq=[{}, {held.symbol: held} if positions_recover else {}])
    bot.engine.portfolio.initial_capital = Decimal("0")
    bot.config = SimpleNamespace(trading=SimpleNamespace(initial_capital=Decimal("0")))
    bot.risk_manager = RiskManager(RiskConfig(), Decimal("0"))
    bot.risk_manager.set_cash_verification(False)
    bot._kr_capital_baseline_pending, bot._kr_cash_verified = True, False
    bot._confirm_kr_cash = MethodType(run_trader.UnifiedTradingBot._confirm_kr_cash, bot)
    asyncio.run(sched._sync_portfolio())
    assert bot.broker.get_positions_calls == 2
    if positions_recover:
        assert bot.engine.portfolio.initial_capital == Decimal("1020000")
        assert bot._kr_capital_baseline_pending is False
        assert buy_decision(bot)[0]
    else:
        assert bot.engine.portfolio.cash == bot.engine.portfolio.initial_capital == 0
        assert bot.config.trading.initial_capital == bot.risk_manager.initial_capital == 0
        assert bot._kr_capital_baseline_pending is True
        assert not buy_decision(bot)[0]

@pytest.mark.parametrize('reported', [40000, 0, 'NaN', None])
def test_partial_or_invalid_holdings_summary_never_confirms_startup(start_kr, reported):
    bot = start_kr({'available_cash': 1000000, 'stock_value': reported})
    assert bot.engine.portfolio.positions['005930'].quantity == 2
    assert '005930' in bot.exit_manager._states
    assert bot._kr_capital_baseline_pending
    assert bot.engine.portfolio.initial_capital == 0
    assert not buy_decision(bot)[0]


@pytest.mark.parametrize('initially_loaded', [False, True])
def test_partial_first_sync_preserves_hold_until_complete_response(monkeypatch, tmp_path, initially_loaded):
    from types import MethodType
    from test_sync_portfolio_characterization import _make, _pos
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    first = _pos('005930', qty=2, avg='10000', cur='10000')
    second = _pos('000660', qty=2, avg='10000', cur='10000')
    sched, bot, _ = _make(monkeypatch, bot_positions=[first] if initially_loaded else [], cash='0',
        balance={'available_cash': 1000000, 'available_cash_verified': True, 'stock_value': 40000},
        kis_seq=[{first.symbol: first}, {first.symbol: first, second.symbol: second}])
    bot.engine.portfolio.initial_capital = Decimal('0')
    bot.config = SimpleNamespace(trading=SimpleNamespace(initial_capital=Decimal('0')))
    bot.risk_manager = RiskManager(RiskConfig(), Decimal('0'))
    bot.risk_manager.set_cash_verification(False)
    bot._kr_capital_baseline_pending, bot._kr_cash_verified = True, False
    bot._confirm_kr_cash = MethodType(run_trader.UnifiedTradingBot._confirm_kr_cash, bot)
    asyncio.run(sched._sync_portfolio())
    assert bot._kr_capital_baseline_pending
    assert bot.engine.portfolio.cash == bot.engine.portfolio.initial_capital == 0
    assert bot.engine.portfolio.positions[first.symbol].quantity == 2
    if not initially_loaded:
        assert bot.exit_manager.registered[0][0].symbol == first.symbol
    assert not buy_decision(bot)[0]
    asyncio.run(sched._sync_portfolio())
    assert bot.engine.portfolio.initial_capital == 1040000
    assert not bot._kr_capital_baseline_pending
    assert bot._kr_cash_verified
