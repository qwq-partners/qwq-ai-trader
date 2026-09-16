"""Default-off/minimal scheduler integration, scheduling and honest health."""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.schedulers.toss_shadow import (
    attach_shadow, capture_inputs, candidate_copy, due_slots, update_observation_health,
)
from src.utils import loop_heartbeat as hb

KST = ZoneInfo('Asia/Seoul')


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    for name in ('_beats', '_states', '_observers', '_observer_status'):
        monkeypatch.setattr(hb, name, {})


def test_off_has_no_factory_task_registry_or_bot_mutation(monkeypatch):
    monkeypatch.delenv('TOSS_API', raising=False)
    bot = SimpleNamespace()
    assert attach_shadow(bot) is None
    assert vars(bot) == {}
    assert not hb._observers and not hb._observer_status


def test_optional_import_failure_preserves_all_existing_kr_task_handles(monkeypatch):
    import builtins
    from src.schedulers.kr_scheduler import KRScheduler
    bot = SimpleNamespace(theme_detector=None, broker=None, screener=None,
                          strategy_evolver=None, stock_master=None,
                          batch_analyzer=None, health_monitor=None)
    scheduler = object.__new__(KRScheduler)
    scheduler.bot, scheduler._manual_buy_orders = bot, []
    created, imports = [], []
    def task(coro, *, name):
        coro.close()
        created.append(name)
        return name
    original_import = builtins.__import__
    def fail_optional(name, *args, **kwargs):
        if name == 'toss_shadow':
            imports.append(name)
            raise ImportError('synthetic optional observer unavailable')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(asyncio, 'create_task', task)
    monkeypatch.setattr(builtins, '__import__', fail_optional)
    monkeypatch.setenv('TOSS_API', '0')
    baseline = scheduler.create_tasks()
    assert len(baseline) == 12 and imports == []
    monkeypatch.setenv('TOSS_API', '1')
    assert scheduler.create_tasks() == baseline
    assert created == baseline + baseline and imports == ['toss_shadow']


def test_on_without_operator_anchor_is_unavailable_no_task(monkeypatch):
    monkeypatch.setenv('TOSS_API', '1')
    bot = SimpleNamespace()
    assert attach_shadow(bot) is None
    status = hb.loop_status()['kr_toss_prices']
    assert status['enabled'] is True
    assert status['last_success'] is None
    assert status['observation']['state'] == 'unavailable'


def test_flag_does_not_accept_truthy_typos(monkeypatch):
    monkeypatch.setenv('TOSS_API', 'true')
    assert attach_shadow(SimpleNamespace()) is None
    assert hb.loop_status()['kr_toss_prices']['observation']['state'] == 'configuration_invalid'


def test_repeated_task_creation_does_not_replace_pending_or_stuck_supervisor(monkeypatch):
    monkeypatch.setenv('TOSS_API', '1')
    existing = SimpleNamespace(state='stopping_unconfirmed')
    bot = SimpleNamespace(_toss_shadow_supervisor=existing, _toss_observation_deployment=object())
    assert attach_shadow(bot) is None
    assert bot._toss_shadow_supervisor is existing
    assert not hb._observer_status


def test_candidate_and_holdings_copy_never_fabricates_quote_timestamps():
    now = datetime(2026, 9, 16, 9, 0, tzinfo=KST)
    stocks = [SimpleNamespace(symbol='005930', score=80), SimpleNamespace(symbol='000660', score=80)]
    saved = candidate_copy(stocks, now=now)
    stocks[0].score = 0
    position = SimpleNamespace(current_price=Decimal('100'))
    bot = SimpleNamespace(engine=SimpleNamespace(portfolio=SimpleNamespace(positions={'005930': position})))
    inputs = capture_inputs(bot, saved)
    position.current_price = Decimal('1')
    assert inputs.candidates == (('005930', 80), ('000660', 80))
    assert inputs.source_success_at == now
    quote = inputs.kis_quotes['005930']
    assert quote.price == 100
    assert quote.observed_at is None and quote.fetched_at is None
    assert quote.market_basis == 'unknown'
    assert quote.status == 'partial'
    assert capture_inputs(bot, None).source_success_at is None


def test_slots_are_fixed_kst_calendar_independent_closed_and_missed():
    policy = {'dates': ('2026-09-16',), 'calendar_time': '08:00',
              'sessions': ({'name': 'regular', 'start': '09:00', 'end': '09:15'},)}
    now = datetime(2026, 9, 16, 0, 7, tzinfo=timezone.utc)
    slots = list(due_slots(policy, now))
    assert [(s.kind, s.slot_id, s.missed) for s in slots] == [
        ('calendar', '2026-09-16T08:00:00+09:00', False),
        ('prices', '2026-09-16T09:00:00+09:00', True),
        ('prices', '2026-09-16T09:05:00+09:00', False),
    ]
    assert list(due_slots(policy, now.replace(day=17)))[0].missed is True


def result(**kwargs):
    data = dict(outcome='success', degraded=False, observation_count=1, valid_pairs=0,
                ledger_complete=True, reason='ok', requested_date=None)
    data.update(kwargs)
    return SimpleNamespace(**data)


def test_health_success_requires_durable_valid_toss_not_comparison(monkeypatch):
    now = datetime(2026, 9, 16, 10, tzinfo=KST)
    monkeypatch.setattr(hb.time, 'time', lambda: now.timestamp())
    update_observation_health('prices', result(), now=now)
    status = hb.loop_status()['kr_toss_prices']
    assert status['last_success'] == now.timestamp()
    assert status['observation']['comparison'] == 'insufficient'
    monkeypatch.setattr(hb.time, 'time', lambda: now.timestamp() + 60)
    update_observation_health('prices', result(observation_count=0), now=now)
    status = hb.loop_status()['kr_toss_prices']
    assert status['last_success'] == now.timestamp()
    assert status['consecutive_failures'] == 1
    update_observation_health('prices', result(ledger_complete=False), now=now)
    assert hb.loop_status()['kr_toss_prices']['last_success'] == now.timestamp()


def test_calendar_result_after_midnight_not_new_day_success():
    now = datetime(2026, 9, 17, 0, 1, tzinfo=KST)
    update_observation_health('calendar', result(requested_date='2026-09-16'), now=now)
    assert hb.loop_status()['kr_toss_calendar']['last_success'] is None


def test_holiday_calendar_staleness_and_skip_does_not_refresh(monkeypatch):
    now = datetime(2026, 9, 13, 10, tzinfo=KST)
    hb.register_observer('kr_toss_calendar', dates=('2026-09-13',), daily_time='08:00', grace_seconds=60)
    hb.register_observer('kr_toss_prices', dates=('2026-09-13',),
                         windows=(('09:00', '15:30'),), period_seconds=300, grace_seconds=60)
    monkeypatch.setattr(hb, '_started', now.replace(hour=7).timestamp())
    monkeypatch.setattr(hb.time, 'time', lambda: now.timestamp())
    update_observation_health('prices', result(outcome='failure', reason='worker_busy'), now=now)
    stale = hb.check(now, is_holiday=lambda _: True)
    assert set(stale) == {'kr_toss_calendar', 'kr_toss_prices'}
    assert hb.loop_status()['kr_toss_prices']['last_success'] is None


def test_successful_observer_does_not_modify_existing_loop_registry():
    periods, daily = dict(hb.PERIODS), dict(hb.DAILY_SCHEDULE)
    hb.register_observer('kr_toss_prices', dates=('2026-09-16',), windows=(('09:00', '15:30'),))
    update_observation_health('prices', result(degraded=True), now=datetime.now(KST))
    assert hb.PERIODS == periods and hb.DAILY_SCHEDULE == daily
    assert hb.loop_status()['kr_toss_prices']['observation']['degraded'] is True


def test_partial_success_keeps_failure_and_exclusion_counts_visible():
    update_observation_health('prices', result(degraded=True, provider_failures=2,
        excluded_pairs=3, budget_skips=1), now=datetime.now(KST))
    status = hb.loop_status()['kr_toss_prices']['observation']
    assert status['observation_count'] == 1
    assert status['provider_failures'] == 2
    assert status['excluded_pairs'] == 3 and status['budget_skips'] == 1


def test_calendar_start_after_due_does_not_count_startup_as_success(monkeypatch):
    now = datetime(2026, 9, 16, 10, tzinfo=KST)
    hb.register_observer('kr_toss_calendar', dates=('2026-09-16',), daily_time='08:00', grace_seconds=60)
    monkeypatch.setattr(hb, '_started', now.replace(hour=9).timestamp())
    assert 'kr_toss_calendar' in hb.check(now, is_holiday=lambda _: False)


def test_observer_next_due_uses_approved_kst_grid_in_utc_process():
    hb.register_observer('kr_toss_prices', dates=('2026-09-16',), windows=(('09:02', '15:30'),))
    hb.register_observer('kr_toss_calendar', dates=('2026-09-16',), daily_time='08:00')
    now = datetime(2026, 9, 15, 22, 0, tzinfo=timezone.utc)
    rows = hb.loop_status(now)
    assert rows['kr_toss_prices']['next_due'] == '2026-09-16 09:05'
    assert rows['kr_toss_calendar']['next_due'] == '2026-09-16 08:00'


def test_next_due_uses_time_order_not_approved_array_order():
    hb.register_observer('kr_toss_prices', dates=('2026-09-16',),
                         windows=(('16:00', '18:00'), ('09:00', '15:30')))
    now = datetime(2026, 9, 16, 8, tzinfo=KST)
    assert hb.loop_status(now)['kr_toss_prices']['next_due'] == '2026-09-16 09:00'


@pytest.mark.parametrize('process_tz', ['UTC', 'Asia/Seoul'])
def test_default_health_clock_converts_host_local_time_to_kst(monkeypatch, process_tz):
    import time
    instant = datetime(2026, 9, 16, 10, tzinfo=KST)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(instant.timestamp(), tz)
    try:
        with monkeypatch.context() as patch:
            patch.setenv('TZ', process_tz)
            time.tzset()
            patch.setattr(hb, 'datetime', Clock)
            patch.setattr(hb, '_started', instant.replace(hour=7).timestamp())
            hb.register_observer('kr_toss_prices', dates=('2026-09-16',),
                                 windows=(('09:00', '15:30'),))
            hb.register_observer('kr_toss_calendar', dates=('2026-09-16',), daily_time='08:00')
            assert set(hb.check(is_holiday=lambda _: True)) == {'kr_toss_prices', 'kr_toss_calendar'}
            assert hb.loop_status()['kr_toss_prices']['next_due'] == '2026-09-16 10:05'
    finally:
        time.tzset()


@pytest.fixture
def supervisor_fakes(monkeypatch):
    import sys
    from concurrent.futures import Future
    from types import ModuleType
    from src.schedulers import toss_shadow as module
    # Supervisor boundary tests: no auth/files/HTTP, even before Task2 lands.
    observation = ModuleType('src.data.providers.toss.observation')
    observation.select_snapshot = lambda **kwargs: {}
    monkeypatch.setitem(sys.modules, observation.__name__, observation)
    monkeypatch.setenv('TOSS_API', '1')
    calls = []
    policy = {'dates': ('2026-09-16',), 'calendar_time': '08:00',
              'sessions': ({'name': 'regular', 'start': '09:00', 'end': '09:15'},),
              'limits': {'cleanup_timeout_seconds': .05, 'job_timeout_seconds': .02}}
    authority = SimpleNamespace(plan=SimpleNamespace(document=policy),
                                grant=SimpleNamespace(client_identity='synthetic'),
                                stop=lambda: calls.append('authority_stop'),
                                require=lambda *args, **kwargs: None)
    class Preflight:
        def __init__(self, loader):
            self.loader = loader
        async def run(self, **kwargs):
            return self.loader()
    class Worker:
        def __init__(self, *args, **kwargs):
            calls.append('worker_factory')
        async def start(self, **kwargs):
            calls.append('worker_start')
        def submit(self, command):
            calls.append('submit')
            return Future()  # hangs; supervisor must bound its own wait
        async def stop(self, **kwargs):
            calls.append('worker_stop')
            return 'closed'
    monkeypatch.setattr(module, 'Preflight', Preflight)
    monkeypatch.setattr(module, 'TossWorker', Worker)
    deployment = SimpleNamespace(preflight_timeout_seconds=.1, load=lambda: authority)
    return module, deployment, calls


def test_off_during_preflight_discards_approval_without_worker(monkeypatch, supervisor_fakes):
    module, deployment, calls = supervisor_fakes
    loader = deployment.load
    def turn_off():
        monkeypatch.setenv('TOSS_API', '0')
        return loader()
    deployment.load = turn_off
    supervisor = module.TossShadowSupervisor(SimpleNamespace(running=False), deployment)
    asyncio.run(supervisor.run())
    assert calls == ['authority_stop']
    assert not hb._observers
    assert supervisor.state == 'disabled'
    assert hb.loop_status()['kr_toss_prices']['enabled'] is False


def test_normal_shutdown_is_not_left_approval_pending(supervisor_fakes):
    module, deployment, calls = supervisor_fakes
    supervisor = module.TossShadowSupervisor(SimpleNamespace(running=False), deployment)
    asyncio.run(supervisor.run())
    assert calls == ['worker_factory', 'worker_start', 'authority_stop', 'worker_stop']
    assert supervisor.state == 'closed'
    status = hb.loop_status()['kr_toss_prices']
    assert status['observation']['state'] == 'closed'
    assert status['last_success'] is None


@pytest.mark.parametrize('cleanup_state', ['closed', 'stopping_unconfirmed'])
@pytest.mark.parametrize('off_value', ['0', ''])
@pytest.mark.parametrize('start_fails', [False, True])
def test_dynamic_off_disables_alarms_only_after_confirmed_stop(monkeypatch, supervisor_fakes, cleanup_state, off_value, start_fails):
    module, deployment, calls = supervisor_fakes
    class Worker(module.TossWorker):
        async def start(self, **kwargs):
            monkeypatch.setenv('TOSS_API', off_value)
            if start_fails:
                raise module.RuntimeUnavailable('stopping')
        async def stop(self, **kwargs): return cleanup_state
    monkeypatch.setattr(module, 'TossWorker', Worker)
    supervisor = module.TossShadowSupervisor(SimpleNamespace(running=True), deployment)
    asyncio.run(supervisor.run())
    for name in ('kr_toss_prices', 'kr_toss_calendar'):
        status = hb.loop_status()[name]
        assert status['enabled'] is (cleanup_state != 'closed')
        assert status['observation']['state'] == ('disabled' if cleanup_state == 'closed' else cleanup_state)
        assert status['last_success'] is None


def test_stuck_observation_wait_is_bounded_and_not_reported_closed(monkeypatch, supervisor_fakes):
    module, deployment, calls = supervisor_fakes
    monkeypatch.setattr(module, 'due_slots', lambda *args: [
        module.DueSlot('calendar', '2026-09-16T08:00:00+09:00', False, '2026-09-16')])
    supervisor = module.TossShadowSupervisor(SimpleNamespace(running=True), deployment)
    async def exercise():
        await asyncio.wait_for(supervisor.run(), .25)
    asyncio.run(exercise())
    assert calls[-2:] == ['authority_stop', 'worker_stop']
    assert supervisor.state == 'unavailable'
    assert hb.loop_status()['kr_toss_calendar']['last_success'] is None


def test_cancelled_preflight_never_stays_pending(monkeypatch, supervisor_fakes):
    import threading
    from src.data.providers.toss.runtime import Preflight
    module, deployment, calls = supervisor_fakes
    started, release = threading.Event(), threading.Event()
    def load():
        started.set()
        release.wait(1)
        return None
    deployment.load = load
    monkeypatch.setattr(module, 'Preflight', Preflight)
    supervisor = module.TossShadowSupervisor(SimpleNamespace(running=True), deployment)
    async def exercise():
        task = asyncio.create_task(supervisor.run())
        while not started.is_set():
            await asyncio.sleep(.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    try:
        asyncio.run(exercise())
    finally:
        release.set()
    assert supervisor.state == 'cancelled'
    assert hb.loop_status()['kr_toss_prices']['observation']['state'] == 'cancelled'
    assert 'worker_factory' not in calls


def test_slot_expiring_while_previous_job_runs_is_recorded_missed(monkeypatch, supervisor_fakes):
    from concurrent.futures import Future
    module, deployment, calls = supervisor_fakes
    current = [datetime(2026, 9, 16, 9, 4, 59, tzinfo=KST)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return current[0]
    bot = SimpleNamespace(running=True)
    commands = []
    class Worker:
        def __init__(self, *args, **kwargs): pass
        async def start(self, **kwargs): pass
        def submit(self, command):
            commands.append(command)
            future = Future()
            if command.kind == 'calendar':
                current[0] = current[0].replace(minute=5, second=1)
                future.set_result(result(requested_date='2026-09-16'))
            else:
                bot.running = False
                future.set_result(result(outcome='idle', observation_count=0))
            return future
        async def stop(self, **kwargs): return 'closed'
    monkeypatch.setattr(module, 'datetime', Clock)
    monkeypatch.setattr(module, 'TossWorker', Worker)
    supervisor = module.TossShadowSupervisor(bot, deployment)
    asyncio.run(supervisor.run())
    assert [command.kind for command in commands] == ['calendar', 'missed']
    assert commands[1].snapshot == {'kind': 'prices'}
