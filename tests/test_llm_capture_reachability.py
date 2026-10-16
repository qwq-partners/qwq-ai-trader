"""Connected synthetic screening proof; real gates, no accounts or providers.

Catches a lost scheduler/engine hook, an early cross-validation boundary, a
changed LLM band, or a broker path that bypasses the persistent BUY halt.
"""
import asyncio
import datetime as dt
import json
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from src.analytics import entry_observation as obs, entry_gate_trace
from src.analytics.entry_observation_runtime import _WindowBuffer, validate_window_contract
from src.analytics.entry_observation_journal import ObservationJournal, read_observation_journal
from src.analytics.entry_markout import MarkoutPolicy, select_markout_quote
from src.core.engine import UnifiedEngine, RiskManager
from src.core.types import TradingConfig, RiskConfig
from src.execution.broker import kis_kr
from src.risk.order_unknown import UnknownOrderBook
from src.schedulers import kr_scheduler
from test_entry_observation_runtime import load
from test_llm_window_observation import llm_plan
from test_quote_subscription import ack
from src.data.feeds.quote_subscription import QuoteSubscriptionCoordinator
from test_risk_sizing import home, _em
from test_entry_llm_context import RecordingLLM
from src.core.adversarial_validator import AdversarialValidator
from src.analytics import llm_decision_record as decisions


@pytest.fixture(autouse=True)
def decision_recorder(tmp_path, monkeypatch):
    recorder = decisions.DecisionRecorder(tmp_path / 'decisions', enabled=True)
    monkeypatch.setattr(decisions, '_recorder', recorder)
    yield recorder
    assert recorder.close()


SYMBOL = '900001'


def study(context):
    llm_plan(context)
    context['as_of'] = '2026-10-16T09:56:00+09:00'
    context['markout_policy'].update(horizon_seconds=900, policy_ref='synthetic-15min-v1')
    context['capture'].update(start_at='2026-10-16T09:30:00+09:00',
        admission_end_at='2026-10-16T09:40:00+09:00', end_at=context['as_of'])
    context['capture']['channels']['lease_seconds'] = 1560


async def connected_case(tmp_path, monkeypatch, *, raw_score=93, regime='neutral',
                         scan_time='09:30:00', process_time=None, unknown=False, manager_available=True, sector_bonus=False):
    plan, context = load(tmp_path, study)
    clock = [dt.datetime.fromisoformat('2026-10-16T' + scan_time + '+09:00')]
    real_datetime = dt.datetime

    class Frozen(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    # CrossStrategyValidator also imports datetime inside validate().
    monkeypatch.setattr(dt, 'datetime', Frozen)
    for module in ('src.core.engine', 'src.core.cross_validator',
                   'src.utils.session',
                   'src.schedulers.kr_scheduler', 'src.execution.broker.kis_kr'):
        monkeypatch.setattr(module + '.datetime', Frozen)
    monkeypatch.setattr(obs, '_now', lambda: clock[0].isoformat())
    monkeypatch.setattr(entry_gate_trace, '_now', lambda: clock[0].isoformat())
    monkeypatch.setattr('src.analytics.entry_observation_journal._timestamp', lambda: clock[0].isoformat())
    monkeypatch.setattr('src.core.evolution.trade_wiki.WIKI_DIR', tmp_path / 'wiki')
    class SyntheticLLM(RecordingLLM):
        async def complete_with(self, prompt, *, provider, **kwargs):
            # Admission is recorded before either synthetic provider is awaited.
            assert any(r['kind'] == 'llm_check_admission' for r in buffer.export()['records'])
            return await super().complete_with(prompt, provider=provider, **kwargs)

    llm = SyntheticLLM(bull='APPROVE synthetic', bear='ACCEPT synthetic') if manager_available else None
    monkeypatch.setattr('src.utils.llm.get_llm_manager', lambda: llm)
    monkeypatch.setattr('src.core.adversarial_validator.get_adversarial_validator', AdversarialValidator)
    monkeypatch.setattr('src.data.providers.disclosure_feed.get_symbol_disclosure_context', lambda *a: '')
    monkeypatch.setattr(decisions, 'now_kst', lambda: clock[0])
    monkeypatch.setenv('RUNTIME_WIKI', '0')
    for key in ('CALENDAR_SEASONALITY', 'VOL_TARGETING', 'TEAM_CONVICTION'):
        monkeypatch.setenv(key, '0')
    async def log(**kwargs): pass
    monkeypatch.setattr(kr_scheduler._SigLog, 'get', lambda: NS(log=log))
    monkeypatch.setattr(kr_scheduler, 'trading_logger', NS(log_screening=lambda **kw: None))
    monkeypatch.setattr(kr_scheduler, '_hb', NS(**{name: lambda *a, **k: None for name in
        ('record_attempt', 'record_idle', 'record_success', 'record_failure')}))

    buffer = _WindowBuffer(plan, lambda: clock[0])
    journal = await ObservationJournal.open(buffer, plan.settings['journal_path'],
        study_ref=plan.settings['study_ref'], study_sha256=plan.study_sha256,
        queue_capacity=1000, batch_size=10, max_bytes=plan.settings['max_bytes'],
        max_record_bytes=plan.settings['max_record_bytes'])
    subscriptions = QuoteSubscriptionCoordinator(buffer, **plan.settings['channels'], request_interval=0)
    buffer.llm_quote_submit = subscriptions.submit_llm_check
    sent = []
    async def send(action, key): sent.append((action, key))
    generation = await subscriptions.start(send)
    engine = UnifiedEngine(TradingConfig(initial_capital=Decimal('10000000'),
        risk=RiskConfig(min_position_value=200000, strategy_allocation={'gap_and_go': 40, 'core_holding': 30})))
    engine._market_regime = regime
    rm = RiskManager(engine, engine.config.risk)
    engine.risk_manager = rm
    # External panel source absent; retain all actual cross-validator rules.
    monkeypatch.setattr(rm._cross_validator, '_load_panel_outlook', lambda: None)
    broker = object.__new__(kis_kr.KISBroker)
    broker._unknown_book = UnknownOrderBook(tmp_path / 'unknown.json', now=clock[0].replace(tzinfo=None))
    if unknown:
        broker._unknown_book.record('buy', '900002', 1, 'synthetic lost response', clock[0].replace(tzinfo=None))
    http_attempts = []
    async def forbidden_http(*args, **kwargs):
        http_attempts.append((args, kwargs))
        raise AssertionError('broker HTTP must remain unreachable')
    broker._api_post = forbidden_http
    broker._api_get = forbidden_http
    broker._get_hashkey = forbidden_http
    blocked = []
    monkeypatch.setattr(kis_kr.audit_log, 'record_blocked', lambda **kwargs: blocked.append(kwargs))
    monkeypatch.setattr(kis_kr.kill_switch, 'CACHE_DIR', tmp_path)
    monkeypatch.setattr(kis_kr.kill_switch, '_cache', {})
    halt = tmp_path / 'KILL_SWITCH_KR'
    halt.write_text('synthetic BUY halt')
    engine.broker = broker
    async def quote(symbol):
        if symbol in ('069500', '229200'): return {'change_pct': 0}
        return dict(price=10000, high=11000, change_pct=2, open=9900, volume=100)
    broker.get_quote = quote
    async def night(): return None
    async def sector(symbol): return 'synthetic-sector' if sector_bonus else None
    candidate = NS(symbol=SYMBOL, name='synthetic', score=raw_score, price=10000,
        change_pct=2, reasons=['MA20+3%', 'RSI:50', '기관 순매수'], atr_pct=4,
        volume_ratio=3, per=10, pbr=1)
    async def screen_all(**kwargs): return [candidate]
    # Synthetic supply input is consumed by the actual scheduler metadata builder.
    cache = tmp_path / '.cache' / 'ai_trader'
    cache.mkdir(parents=True, exist_ok=True)
    (cache / 'supply_demand_20261016.json').write_text(json.dumps({SYMBOL: {'foreign_net_buy': 1, 'inst_net_buy': 1}}))
    if sector_bonus:
        (cache / 'supply_daily_20261016.json').write_text(json.dumps({SYMBOL: {'foreign': 1}}))
    bot = NS(running=True, market='kr', engine=engine, broker=broker,
        screener=NS(screen_all=screen_all), theme_detector=None, _stock_validator=None,
        kis_market_data=NS(get_night_futures_quote=night),
        strategy_manager=NS(enabled_strategies=['gap_and_go']),
        config=NS(get=lambda *args: {}), risk_manager=rm,
        _watch_symbols_lock=asyncio.Lock(), _watch_symbols=[], _screening_interval=300,
        _screening_signal_cooldown={}, _daily_entry_count={}, _get_sector=sector,
        _entry_price_observer=buffer, exit_manager=_em(), _strategy_exit_params={})
    rm._entry_observation_owner = bot
    real_sleep = asyncio.sleep
    async def scheduler_sleep(seconds):
        if seconds == 300: bot.running = False
        await real_sleep(0)
    # Replace only this module's timer, not asyncio globally or screening branches.
    monkeypatch.setattr(kr_scheduler, 'asyncio', NS(sleep=scheduler_sleep,
        gather=asyncio.gather, create_task=asyncio.create_task, CancelledError=asyncio.CancelledError))
    await kr_scheduler.KRScheduler(bot).run_screening()
    signal = await engine._get_next_event()
    assert signal is not None, buffer.export()
    if process_time:
        clock[0] = real_datetime.fromisoformat('2026-10-16T' + process_time + '+09:00')
    await engine._process_event(signal)
    if subscriptions._tasks:
        await asyncio.gather(*list(subscriptions._tasks))
    for action, key in sent:
        await subscriptions.handle_ack(ack(key), generation)
    order_event = await engine._get_next_event()
    if order_event is not None:
        assert SYMBOL in rm._pending_orders
        await engine._process_event(order_event)
        assert SYMBOL not in rm._pending_orders
        assert SYMBOL in rm._order_fail_cooldown
    assert engine.stats.errors_count == 0
    assert http_attempts == []
    assert halt.read_text() == 'synthetic BUY halt'
    assert kis_kr.kill_switch.check('buy', 'KR')[0] is False
    return NS(plan=plan, context=context, buffer=buffer, journal=journal, subscriptions=subscriptions,
        clock=clock, signal=signal, order=order_event, blocked=blocked, engine=engine, llm=llm)


@pytest.mark.asyncio
async def test_0930_scheduler_reaches_llm_order_ready_and_real_broker_halt(home, tmp_path, monkeypatch):
    case = await connected_case(tmp_path, monkeypatch)
    try:
        rows = case.buffer.export()['records']
        assert case.signal.score == 85  # raw 93 minus actual early-session 8
        assert any(r['kind'] == 'llm_check_started' for r in rows)
        decision = case.signal.metadata['llm_decision']
        assert decision['stage'] == 'adversarial' and decision['attempts'] == 2
        recorder = decisions.get_decision_recorder()
        assert recorder.close()
        persisted = [json.loads(line) for path in recorder.directory.glob('*.jsonl') for line in path.read_text().splitlines()]
        assert len(persisted) == 1
        assert persisted[0]['signal_id'] == case.signal.id
        assert persisted[0]['result']['approved'] is True
        assert [a['outcome'] for a in persisted[0]['attempts']] == ['success', 'success']
        assert [r['kind'] for r in rows if r['kind'] in ('signal', 'llm_check_started',
            'llm_check_admission', 'order_ready')] == ['signal', 'llm_check_started',
            'llm_check_admission', 'order_ready']
        admission = next(r for r in rows if r['kind'] == 'llm_check_admission')
        ready = next(r for r in rows if r['kind'] == 'order_ready')
        assert admission['reason'] == 'llm_check_admitted'
        assert admission['signal_id'] == ready['signal_id'] == case.signal.id
        assert case.order.order.quantity == ready['requested_quantity'] > 0
        assert ready['transport_status'] == 'not_observed'
        assert len(case.blocked) == 1 and 'KILL_SWITCH_KR' in case.blocked[0]['reason']
        assert case.subscriptions.snapshot()['leases'] == 1
        assert case.subscriptions.snapshot()['acknowledged'] == 3
        assert not case.engine.portfolio.positions
        validate_window_contract(case.context, case.buffer.export())
    finally:
        await case.journal.close()
        await case.subscriptions.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('raw_score,regime,sector_bonus,expected,eligible', [
    (92, 'neutral', False, 84, False), (97, 'neutral', True, 94, True),
    (98, 'neutral', True, 95, False), (93, 'bull', False, 85, False),
])
async def test_real_adjusted_score_band_and_bull_bypass(home, tmp_path, monkeypatch, raw_score, regime, sector_bonus, expected, eligible):
    case = await connected_case(tmp_path, monkeypatch, raw_score=raw_score, regime=regime, sector_bonus=sector_bonus)
    try:
        assert case.signal.score == expected
        assert sum(r['kind'] == 'llm_check_started' for r in case.buffer.export()['records']) == int(eligible)
        assert case.subscriptions.snapshot()['leases'] == int(eligible)
        assert len(case.llm.prompts) == (2 if eligible else 0)
        assert case.order is not None and len(case.blocked) == 1
    finally:
        await case.journal.close()
        await case.subscriptions.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('options', [{'scan_time': '09:29:59'}, {'unknown': True}])
async def test_time_and_unknown_buy_hold_stop_before_llm(home, tmp_path, monkeypatch, options):
    case = await connected_case(tmp_path, monkeypatch, **options)
    try:
        assert case.order is None and not case.blocked
        assert case.llm.prompts == []
        assert case.engine.risk_manager._cross_validator._stats['total'] == (0 if options.get('unknown') else 1)
        assert not any(r['kind'] in ('llm_check_started', 'order_ready') for r in case.buffer.export()['records'])
        assert case.subscriptions.snapshot()['leases'] == 0
    finally:
        await case.journal.close()
        await case.subscriptions.stop()


def add_quote(case, quote_id, timestamp, *, bid):
    case.clock[0] = dt.datetime.fromisoformat(timestamp)
    obs.capture_quote(case.buffer, NS(id=quote_id, symbol=SYMBOL,
        ask_price=Decimal(bid) + 10, bid_price=Decimal(bid), ask_size=1000, bid_size=1000,
        metadata={'tr_id': 'H0STASP0', 'message_count': 1, 'source_as_of': None,
                  'received_at': timestamp, 'generation': case.subscriptions.generation,
                  'connection_id': case.subscriptions.connection_id}))


async def economics(case):
    rows = case.buffer.export()['records']
    signal = next(r for r in rows if r['kind'] == 'signal')
    context = dict(case.context)
    # The real order-ready capital snapshot and an explicit synthetic session
    # review are separate from ACK/transport evidence.
    inputs = [{'opportunity_id': signal['candidate_id'],
        'policy_inputs_at': signal['observed_at'],
        'quote_session': {'quote_id': 'entry', 'session': 'KRX_REGULAR_CONTINUOUS',
                          'evidence_ref': 'synthetic-session-review'},
        'markout_session': {'quote_id': 'markout', 'session': 'KRX_REGULAR_CONTINUOUS',
                            'evidence_ref': 'synthetic-session-review'}}]
    case.clock[0] = case.plan.end_at
    await case.journal.close()
    observations = read_observation_journal(case.plan.settings['journal_path'],
        max_bytes=case.plan.settings['max_bytes'], expected_study_sha256=case.plan.study_sha256)
    return obs.prepare_input(context, observations, inputs)


@pytest.mark.asyncio
@pytest.mark.parametrize('late_first_quote', [False, True])
async def test_first_quote_anchors_900_seconds_and_late_quote_cannot_create_profit(
        home, tmp_path, monkeypatch, late_first_quote):
    case = await connected_case(tmp_path, monkeypatch, scan_time='09:39:59')
    try:
        assert case.signal.metadata['llm_decision']['stage'] == 'adversarial'
        records = case.buffer.export()['records']
        assert next(r for r in records if r['kind'] == 'llm_check_admission')['reason'] == 'llm_check_admitted'
        assert next(r for r in records if r['kind'] == 'order_ready')['signal_id'] == case.signal.id
        assert len(case.blocked) == 1 and 'KILL_SWITCH_KR' in case.blocked[0]['reason']
        add_quote(case, 'entry', '2026-10-16T09:41:01+09:00' if late_first_quote
                  else '2026-10-16T09:40:00+09:00', bid='9990')
        # Early price cannot be substituted for the fixed horizon even if better.
        add_quote(case, 'too-early', '2026-10-16T09:54:59+09:00', bid='12000')
        add_quote(case, 'markout', '2026-10-16T09:56:01+09:00' if late_first_quote
                  else '2026-10-16T09:55:00+09:00', bid='10500')
        quotes = [r for r in case.buffer.export()['records'] if r['kind'] == 'ws_quote']
        selected = select_markout_quote(quotes[0], quotes, MarkoutPolicy.from_dict(case.context['markout_policy']))
        if late_first_quote:
            assert selected is None
            assert [q['quote_id'] for q in quotes] == ['entry', 'too-early']
        else:
            assert selected['quote_id'] == 'markout'
        result = await economics(case)
        row = result['report']['opportunities'][0]
        assert result['report']['production_eligible'] is False
        assert result['report']['account_return'] is None
        if late_first_quote:
            assert row['outcome_status'] == 'unknown'
            assert result['report']['summary']['complete_delta_net_pnl'] is None
        else:
            assert result['capture_complete'] is True
            assert row['outcome_status'] == 'complete_markout_proxy', result
            assert row['evaluation_at'] == '2026-10-16T00:55:00+00:00'
            assert Decimal(row['a_net_pnl']) > 0
    finally:
        await case.journal.close()
        await case.subscriptions.stop()


@pytest.mark.asyncio
async def test_queue_delay_to_0940_cannot_backfill_llm_admission(home, tmp_path, monkeypatch):
    case = await connected_case(tmp_path, monkeypatch, scan_time='09:39:59', process_time='09:40:00')
    try:
        rows = case.buffer.export()['records']
        admission = next(r for r in rows if r['kind'] == 'llm_check_admission')
        assert admission['reason'] == 'llm_check_admission_closed'
        assert case.subscriptions.snapshot()['leases'] == 0
        assert case.order is not None and len(case.blocked) == 1
        result = await economics(case)
        assert result['report']['summary']['complete_delta_net_pnl'] is None
        assert result['report']['performance_status'] == 'incomplete'
        assert result['report']['production_eligible'] is False
    finally:
        await case.journal.close()
        await case.subscriptions.stop()


@pytest.mark.asyncio
async def test_missing_manager_is_started_evidence_not_model_approval(home, tmp_path, monkeypatch):
    case = await connected_case(tmp_path, monkeypatch, manager_available=False)
    try:
        assert case.signal.metadata['llm_decision']['stage'] == 'unavailable'
        assert any(r['kind'] == 'llm_check_started' for r in case.buffer.export()['records'])
        assert case.order is not None and len(case.blocked) == 1
    finally:
        await case.journal.close()
        await case.subscriptions.stop()
