"""Run actual REST→ExitManager path on/off, including failed KIS (no fallback)."""
import asyncio
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.types import Position, MarketSession
from src.schedulers import kr_scheduler as kr
from src.schedulers.kr_scheduler import KRScheduler
from src.strategies.exit_manager import ExitManager
from src.data.providers.toss.client import TossClient
from src.utils import loop_heartbeat as hb


@pytest.mark.parametrize('kis_fails', [False, True])
def test_real_rest_exit_path_identical_for_off_on_without_toss_fallback(tmp_path, monkeypatch, kis_fails):
    calls = []

    async def forbidden_toss(*args, **kwargs):
        calls.append('Toss')
        raise AssertionError('Toss must never enter REST/exit/order')

    monkeypatch.setattr(TossClient, 'get', forbidden_toss)
    monkeypatch.setattr(ExitManager, '_count_business_days', lambda *args: 0)

    def run(flag):
        monkeypatch.setenv('TOSS_API', flag)
        home = tmp_path / flag
        home.mkdir()
        monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
        position = Position(symbol='005930', quantity=10, avg_price=Decimal('10000'),
                            current_price=Decimal('10000'), entry_time=datetime.now(),
                            strategy='sepa_trend')
        exit_manager = ExitManager()
        exit_manager.register_position(position)
        events, requests = [], []

        async def get_quote(symbol):
            requests.append(symbol)
            if kis_fails:
                raise RuntimeError('synthetic KIS unavailable')
            return {'price': 9000, 'high': 10000, 'low': 8900, 'volume': 10}

        async def emit(event):
            events.append(event)

        async def noop(*args, **kwargs):
            pass

        bot = SimpleNamespace(
            broker=SimpleNamespace(get_quote=get_quote), ws_feed=None, running=True,
            engine=SimpleNamespace(portfolio=SimpleNamespace(positions={'005930': position}),
                                   risk_manager=None, emit=emit),
            exit_manager=exit_manager, _pause_resume_at=None, _exit_pending_symbols=set(),
            _exit_pending_timestamps={}, _exit_reasons={}, _sell_blocked_symbols={},
            _strategy_exit_params={},
        )
        scheduler = object.__new__(KRScheduler)
        scheduler.bot = bot
        scheduler._get_current_session = lambda: MarketSession.REGULAR
        scheduler._refresh_composite_cache = noop
        scheduler._cleanup_stale_pending = noop
        scheduler._ma5_cache = {'005930': None}
        scheduler._prev_day_low = {}
        scheduler._composite_cache_date = None

        async def sleep(seconds):
            if seconds == 20:
                bot.running = False

        monkeypatch.setattr(kr.asyncio, 'sleep', sleep)
        asyncio.run(scheduler.run_rest_price_feed())
        state = exit_manager.get_state('005930')
        return (
            requests, [(type(e).__name__, e.symbol, e.source) for e in events],
            position.quantity, str(position.current_price),
            state.current_stage.value, tuple(sorted(bot._exit_pending_symbols)),
            dict(bot._exit_reasons),
        )

    off, on = run('0'), run('1')
    assert off == on
    assert off[0] == ['005930']
    assert off[1] == ([] if kis_fails else [
        ('MarketDataEvent', '005930', 'rest_polling'),
        ('SignalEvent', '005930', 'exit_manager'),
    ])
    assert calls == []


def test_scheduler_diff_is_limited_to_observer_task_and_successful_copy():
    # Stable source boundary, in addition to the runtime tests above: no provider
    # import or observer invocation may appear inside these money-path methods.
    import inspect
    for name in ('_check_exit_signal', '_sync_portfolio', 'run_rest_price_feed', 'run_fill_check'):
        source = inspect.getsource(getattr(KRScheduler, name))
        assert 'toss' not in source.lower()
