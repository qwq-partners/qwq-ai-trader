"""Installed two-minute regime owner: actual scheduler, KIS adapter and SQLite boundary.

These are deliberately separate from the writer's unit tests.  A regression in
the installed loop (for example, restoring the legacy ``if kospi or kosdaq``
path) must fail here even when a direct owner test still passes.
"""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from src.core.market_regime import MarketRegimeAdapter
from src.core.engine import UnifiedEngine
from src.core.types import Order, OrderSide, OrderType, TradingConfig
from src.data.providers.kis_market_data import KISMarketData
from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.guards import EntryAuthority, FinalEntryGuard, GuardDecision, RiskSnapshot
from src.execution.safety.policy_snapshot import PolicyContext, build_owned_snapshot
from src.execution.safety.requests import KISRequestBuilder, RequestSession
from src.execution.safety.risk_transition import IntradayPolicyState
from src.execution.safety import risk_policy
from src.risk.manager import RiskManager
import src.risk.manager as risk_manager_module
from src.strategies.exit_manager import ExitManager
from test_execution_risk_policy import snapshot
from test_execution_requests import account
from test_execution_runtime import NOW, setup
from test_kr_final_dispatch import Response
from test_kr_prepared_dispatch import fixture as broker_fixture
from test_kis_index_observation import raw
from test_regime_transition_parity import _risk_config
from src.utils.stop_policy import StopDecision


def _api():
    # Kept late so this file is collectable on the frozen pre-owner base.  Once
    # the product overlay is installed, every assertion below calls its public
    # interface rather than writer-private helpers.
    from src.execution.safety.regime_owner import RegimeBaseline, RegimeOwner
    return RegimeBaseline, RegimeOwner


class _Heartbeat:
    def __init__(self):
        self.attempts = []
        self.successes = []
        self.failures = []
        self.idles = []

    def record_attempt(self, name): self.attempts.append(name)
    def record_success(self, name): self.successes.append(name)
    def record_failure(self, name, reason): self.failures.append((name, reason))
    def record_idle(self, name, reason): self.idles.append((name, reason))


def _bullish_output():
    value = raw()
    value.update({
        'bstp_nmix_prpr': '105.0', 'bstp_nmix_prdy_vrss': '1.5',
        'bstp_nmix_prdy_ctrt': '1.5', 'bstp_nmix_oprc': '100.0',
        'bstp_nmix_hgpr': '106.0', 'bstp_nmix_lwpr': '99.0',
    })
    return value


async def _actual_kis(monkeypatch, outputs, *, entered=None, release=None):
    """Use KISMarketData's real headers, response decoding and cache/limiter path."""
    calls = {'get': [], 'limiter': 0}

    class Response:
        def __init__(self, code): self.code = code; self.status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def json(self):
            if entered is not None:
                entered[self.code].set()
            if release is not None:
                await release.wait()
            return {'rt_cd': '0', 'output': outputs[self.code]}

    class Session:
        closed = False
        def get(self, url, *, headers, params):
            assert url == 'https://synthetic.invalid/uapi/domestic-stock/v1/quotations/inquire-index-price'
            assert headers['tr_id'] == 'FHPUP02100000'
            assert params['FID_COND_MRKT_DIV_CODE'] == 'U'
            code = params['FID_INPUT_ISCD']
            assert code in {'0001', '1001'}
            calls['get'].append(code)
            return Response(code)

    async def headers(tr_id):
        assert tr_id == 'FHPUP02100000'
        return {'tr_id': tr_id}

    async def acquire():
        calls['limiter'] += 1

    provider = KISMarketData(token_manager=SimpleNamespace(base_url='https://synthetic.invalid'))
    provider._session = Session()
    monkeypatch.setattr(provider, '_get_headers', headers)
    monkeypatch.setattr(provider, '_index_clock', lambda: NOW, raising=False)
    monkeypatch.setattr('src.data.providers.kis_market_data.kis_rate_limit.acquire', acquire)
    return provider, calls


def _current_context(runtime, trend):
    """The policy helper's fixture clock is 11:00; this owner is at its own clock."""
    base = snapshot(risk_policy)
    now = runtime._now()
    return PolicyContext.from_snapshot(replace(base, business_day=now.date(), captured_at=now,
        versions=replace(base.versions, execution=runtime.owner.version), trend=trend))


def _scoped_broker():
    """Reuse only the fake HTTP boundary, not its unrelated test-scope identity."""
    broker, _, calls = broker_fixture(Response(data={'rt_cd': '0', 'output': {
        'ODNO': '1234567890', 'KRX_FWDG_ORD_ORGNO': '12345'}}))
    return broker, KISRequestBuilder(replace(account(), account_scope='scope')), calls


def _baseline(runtime, *, intraday_version, vix_ref=None):
    return {
        'schema': 1,
        'baseline_id': 'independent-two-minute-known-state',
        'account_scope': 'scope',
        'business_day': NOW.date().isoformat(),
        'generation': runtime._day_generation, 'fence_id': runtime._day_fence_id,
        'evidence': {'source': 'independent-acceptance', 'event_id': 'known-state',
                     'observed_at': NOW.isoformat()},
        'sidecar_active': True,
        'intraday': {'baseline_version': intraday_version,
                     'current': IntradayPolicyState('normal', 0.0, None, None).to_dict()},
        'trend_state': {
            'mid_regime': 'sideways',
            'mid_pending': {'present': False, 'target': None, 'since': None},
            'expert_pending': {'present': False, 'target': None, 'since': None},
            'market_trend': {'present': True, 'kospi_pct': -1.0, 'kosdaq_pct': -1.0,
                             'avg_pct': -1.0, 'vs_open_pct': -0.5, 'position_pct': 20.0,
                             'recovering': False, 'classified_at': NOW.isoformat()},
            'regime_data': {}, 'last_update': NOW.isoformat(),
            'source_refs': {'trend': None, 'vix': vix_ref, 'expert': None},
        },
        'engine_regime': 'sideways',
    }


async def _installed(tmp_path, monkeypatch, *, fresh_vix=True, vix_fetcher=None, clock=None):
    """Explicit synthetic handoff; it is not a production bootstrap API."""
    RegimeBaseline, RegimeOwner = _api()
    monkeypatch.setattr(risk_manager_module.Path, 'home', lambda: tmp_path)
    sidecar = RiskManager(_risk_config(), Decimal('2000000'))
    sidecar._sidecar_active = True
    sidecar._market_trend = {
        'kospi_pct': -1.0, 'kosdaq_pct': -1.0, 'avg_pct': -1.0,
        'vs_open_pct': -0.5, 'position_pct': 20.0, 'recovering': False,
        'ts': NOW,
    }
    clock = clock or (lambda: NOW)
    engine, exits, store, runtime = await setup(
        tmp_path, account_scope='scope', clock=clock, risk_manager=sidecar)
    adapter = MarketRegimeAdapter()
    engine._regime_adapter = adapter

    def seed(state):
        intraday = IntradayPolicyState('normal', 0.0, None, None).to_dict()
        state['intraday_policy'] = {'schema': 1, 'baseline': intraday,
            'baseline_version': runtime.owner.version + 1,
            'current': intraday.copy(), 'transitions': {}}
        state['entry_policy_effects'] = {'pending_sectors': {}, 'sidecar_active': True}
        return state

    await runtime.owner.mutate('independent-two-minute-prerequisites', seed)
    vix_ref = None
    if fresh_vix:
        # This is a real accepted source receipt, not an in-memory VIX cache.
        from src.execution.safety.risk_sources import RiskSourceCoordinator
        sources = RiskSourceCoordinator(runtime)
        ticket = await sources.begin('independent-fresh-vix', 'vix_regime')
        receipt = await sources.complete(ticket, 'success', {
            'value': 14.0, 'fetched_at': NOW.isoformat(),
        })
        vix_ref = {'operation_id': receipt.operation_id, 'committed_version': receipt.committed_version}
    baseline = RegimeBaseline.from_dict(_baseline(runtime,
        intraday_version=runtime.owner.state['intraday_policy']['baseline_version'], vix_ref=vix_ref))
    await RegimeOwner.register_baseline(runtime, baseline, expected_version=runtime.owner.version)
    await runtime.owner.register_policy_generations('independent-two-minute-reads', (
        'regime_policy.trend_state', 'entry_policy_effects.sidecar_active', 'intraday_policy.current'))
    owner = RegimeOwner(runtime, adapter=adapter, sidecar=sidecar, vix_fetcher=vix_fetcher)
    return engine, exits, store, runtime, sidecar, adapter, owner


async def _reopen_installed(tmp_path, monkeypatch, store, runtime, *, clock):
    """Cold process objects; only SQLite carries the installed writer state across."""
    _, RegimeOwner = _api()
    path = store.path
    await runtime.shutdown()
    await store.close()
    monkeypatch.setattr(risk_manager_module.Path, 'home', lambda: tmp_path)
    sidecar = RiskManager(_risk_config(), Decimal('2000000'))
    engine = UnifiedEngine(TradingConfig(initial_capital=Decimal('2000000')))
    exits = ExitManager(persist=False, clock=clock)
    from src.execution.safety.runtime import KRExecutionRuntime
    from src.execution.safety.store import ExecutionStateStore
    reopened = ExecutionStateStore(path)
    restored = KRExecutionRuntime(reopened, engine, exits, clock=clock,
        account_scope='scope', risk_manager=sidecar)
    await restored.restore()
    restored.attach()
    adapter = MarketRegimeAdapter()
    engine._regime_adapter = adapter
    owner = RegimeOwner(restored, adapter=adapter, sidecar=sidecar)
    return engine, exits, reopened, restored, sidecar, adapter, owner


async def _run_one_loop(scheduler, bot, monkeypatch, heartbeat):
    import src.schedulers.kr_scheduler as scheduler_module
    sleeps, real_sleep = [], asyncio.sleep

    async def controlled_sleep(seconds):
        sleeps.append(seconds)
        if seconds == 120:
            bot.running = False
        await real_sleep(0)

    monkeypatch.setattr(scheduler_module, '_hb', heartbeat)
    monkeypatch.setattr(scheduler_module.asyncio, 'sleep', controlled_sleep)
    await scheduler.run_market_trend_monitor()
    return sleeps


def test_installed_loop_commits_two_kis_indexes_and_owned_snapshot_without_protection_replay(tmp_path, monkeypatch):
    """Fails if the installed loop restores a legacy memory writer or partial-index success."""
    async def scenario():
        from src.schedulers.kr_scheduler import KRScheduler, MarketSession
        engine, exits, store, runtime, sidecar, adapter, _ = await _installed(tmp_path, monkeypatch)
        entered = {'0001': asyncio.Event(), '1001': asyncio.Event()}
        release = asyncio.Event()
        provider, calls = await _actual_kis(monkeypatch,
            {'0001': _bullish_output(), '1001': _bullish_output()}, entered=entered, release=release)
        heartbeat, applied = _Heartbeat(), []
        monkeypatch.setattr(exits, 'apply_regime_params',
                            lambda *args, **kwargs: applied.append((args, kwargs)))
        bot = SimpleNamespace(running=True, engine=engine, risk_manager=sidecar,
            kis_market_data=provider, expert_orchestrator=None,
            _get_current_session=lambda: MarketSession.REGULAR)
        scheduler = object.__new__(KRScheduler); scheduler.bot = bot
        task = asyncio.create_task(_run_one_loop(scheduler, bot, monkeypatch, heartbeat))
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 2)
            # Source admission precedes provider I/O; accepted policy/heartbeat do not.
            index_id = runtime.owner.state['risk_sources']['latest']['index_trend']
            assert runtime.owner.state['risk_sources']['records'][index_id]['terminal'] is None
            assert heartbeat.successes == []
            release.set()
            assert await asyncio.wait_for(task, 2) == [60, 120]
            state = runtime.owner.state
            index_id = state['risk_sources']['latest']['index_trend']
            terminal = state['risk_sources']['records'][index_id]['terminal']['receipt']
            assert terminal['status'] == 'accepted'
            transition = state['regime_policy']['transitions'][index_id]
            assert transition['version'] == terminal['committed_version']
            assert state['entry_policy_effects']['sidecar_active'] is False
            assert sidecar._sidecar_active is False and sidecar._market_trend['recovering'] is True
            assert adapter._current_regime == state['regime_policy']['trend_state']['mid_regime']
            assert engine._market_regime == state['regime_policy']['engine_projection']['regime']
            assert calls == {'get': ['0001', '1001'], 'limiter': 2}
            assert heartbeat.successes == ['kr_market_trend'] and heartbeat.failures == []
            assert applied == []
            assert (await store.load())[1] == state

            optimistic = _current_context(runtime,
                risk_policy.MarketTrendPolicySnapshot(True, False, True))
            owned = build_owned_snapshot(state, context=optimistic, version=runtime.owner.version,
                now=NOW, prices={})
            assert owned.trend.sidecar_active is False
            assert owned.trend.recovering is True
            assert owned.versions.regime == terminal['committed_version']
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_installed_loop_missing_one_index_only_records_source_failure_and_never_heartbeats_success(tmp_path, monkeypatch):
    """Fails if one valid index is again treated as a successful market recovery."""
    async def scenario():
        from src.schedulers.kr_scheduler import KRScheduler, MarketSession
        engine, exits, store, runtime, sidecar, adapter, _ = await _installed(tmp_path, monkeypatch)
        missing = _bullish_output(); del missing['bstp_nmix_hgpr']
        provider, calls = await _actual_kis(monkeypatch,
            {'0001': _bullish_output(), '1001': missing})
        before = deepcopy(runtime.owner.state)
        heartbeat, applied = _Heartbeat(), []
        monkeypatch.setattr(exits, 'apply_regime_params',
                            lambda *args, **kwargs: applied.append((args, kwargs)))
        bot = SimpleNamespace(running=True, engine=engine, risk_manager=sidecar,
            kis_market_data=provider, expert_orchestrator=None,
            _get_current_session=lambda: MarketSession.REGULAR)
        scheduler = object.__new__(KRScheduler); scheduler.bot = bot
        try:
            assert await _run_one_loop(scheduler, bot, monkeypatch, heartbeat) == [60, 120]
            state = runtime.owner.state
            row = state['risk_sources']['records'][state['risk_sources']['latest']['index_trend']]
            assert row['terminal']['receipt']['status'] == 'missing'
            assert state['regime_policy']['trend_state'] == before['regime_policy']['trend_state']
            assert state['entry_policy_effects'] == before['entry_policy_effects']
            assert sidecar._sidecar_active is True and adapter._current_regime == 'sideways'
            assert calls == {'get': ['0001', '1001'], 'limiter': 2}
            assert heartbeat.successes == [] and len(heartbeat.failures) == 1
            assert applied == []
        finally:
            await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_absent_vix_starts_only_after_index_terminal_and_finishes_as_tracked_source_without_reclassification(tmp_path, monkeypatch):
    """Fails if VIX starts before trend publication or its completion reruns index classification."""
    async def scenario():
        started, release_vix = asyncio.Event(), asyncio.Event()

        async def held_vix():
            started.set()
            await release_vix.wait()
            return 18.0

        _, _, store, runtime, _, _, owner = await _installed(
            tmp_path, monkeypatch, fresh_vix=False, vix_fetcher=held_vix)
        indexes_entered = {'0001': asyncio.Event(), '1001': asyncio.Event()}
        release_indexes = asyncio.Event()
        provider, calls = await _actual_kis(monkeypatch,
            {'0001': _bullish_output(), '1001': _bullish_output()},
            entered=indexes_entered, release=release_indexes)
        task = asyncio.create_task(owner.refresh_trend(provider, expert_orchestrator=None))
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in indexes_entered.values())), 2)
            assert not started.is_set()
            release_indexes.set()
            receipt = await asyncio.wait_for(task, 2)
            assert receipt.status == 'accepted'
            await asyncio.wait_for(started.wait(), 2)
            vix_id = runtime.owner.state['risk_sources']['latest']['vix_regime']
            assert runtime.owner.state['risk_sources']['records'][vix_id]['terminal'] is None
            before = deepcopy(runtime.owner.state['regime_policy'])
            release_vix.set()
            await runtime.shutdown()  # drains the actual owner-tracked VIX worker
            state = runtime.owner.state
            assert state['risk_sources']['records'][vix_id]['terminal']['receipt']['status'] == 'accepted'
            assert state['regime_policy'] == before
            assert calls == {'get': ['0001', '1001'], 'limiter': 2}
        finally:
            release_indexes.set(); release_vix.set()
            await asyncio.gather(task, return_exceptions=True)
            if not runtime._closing:
                await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_regime_owner_requires_explicit_baseline_before_it_can_install_a_live_writer(tmp_path, monkeypatch):
    """Fails if the owner silently derives a neutral baseline from empty live objects."""
    async def scenario():
        _, RegimeOwner = _api()
        monkeypatch.setattr(risk_manager_module.Path, 'home', lambda: tmp_path)
        sidecar = RiskManager(_risk_config(), Decimal('2000000'))
        engine, _, store, runtime = await setup(tmp_path, account_scope='scope', risk_manager=sidecar)
        engine._regime_adapter = MarketRegimeAdapter()
        try:
            with pytest.raises(ApplicationBlocked, match='baseline'):
                RegimeOwner(runtime, adapter=engine._regime_adapter, sidecar=sidecar)
            assert getattr(runtime, '_regime_writer', None) is None
        finally:
            await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_cold_reopen_restores_installed_regime_mirrors_and_current_source_without_reregistering_baseline(tmp_path, monkeypatch):
    """Fails if restore derives a new baseline or publishes a different live mirror from SQLite."""
    async def scenario():
        _, _, store, runtime, _, _, owner = await _installed(tmp_path, monkeypatch)
        provider, _ = await _actual_kis(monkeypatch,
            {'0001': _bullish_output(), '1001': _bullish_output()})
        restored = None
        try:
            receipt = await owner.refresh_trend(provider, expert_orchestrator=None)
            assert receipt.status == 'accepted'
            durable, version = deepcopy(runtime.owner.state), runtime.owner.version
            _, _, store, restored, sidecar, adapter, _ = await _reopen_installed(
                tmp_path, monkeypatch, store, runtime, clock=lambda: NOW)
            runtime = None
            state = restored.owner.state
            assert restored.owner.version == version and state == durable
            assert sidecar._sidecar_active is state['entry_policy_effects']['sidecar_active']
            trend = state['regime_policy']['trend_state']
            assert sidecar._market_trend['recovering'] is trend['market_trend']['recovering']
            assert adapter._current_regime == trend['mid_regime']
            assert restored.engine._market_regime == state['regime_policy']['engine_projection']['regime']
            from src.execution.safety.risk_sources import RiskSourceCoordinator
            source = RiskSourceCoordinator(restored).read_source('index_trend')
            assert source.authority_status == 'current'
            assert json.loads(source.envelope_json)['market_as_of'] is None
            assert state['regime_policy']['baseline'] == durable['regime_policy']['baseline']
        finally:
            if runtime is not None:
                await runtime.shutdown()
            elif restored is not None:
                await restored.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_next_day_rollover_keeps_historical_baseline_and_blocks_entry_until_new_index_source(tmp_path, monkeypatch):
    """Fails if rollover restamps supplied baseline facts or lets yesterday's trend authorize entry."""
    async def scenario():
        from test_execution_day_recovery import prepare, valued
        times = [NOW]
        _, _, store, runtime, _, _, owner = await _installed(
            tmp_path, monkeypatch, clock=lambda: times[0])
        provider, _ = await _actual_kis(monkeypatch,
            {'0001': _bullish_output(), '1001': _bullish_output()})
        restored = None
        try:
            assert (await owner.refresh_trend(provider, expert_orchestrator=None)).status == 'accepted'
            historical = deepcopy(runtime.owner.state['regime_policy']['baseline'])
            fence = await prepare(runtime, times)
            evidence_id = await valued(runtime, fence, times)
            assert (await runtime.rollover_day('independent-regime-roll', expected_version=runtime.owner.version,
                fence_id=fence.fence_id, valuation_evidence_id=evidence_id)).status == 'APPLIED'
            assert (await runtime.resume_after_rollover('independent-regime-resume',
                expected_version=runtime.owner.version, fence_id=fence.fence_id)).status == 'APPLIED'
            _, _, store, restored, _, _, _ = await _reopen_installed(
                tmp_path, monkeypatch, store, runtime, clock=lambda: times[0])
            runtime = None
            state = restored.owner.state
            assert state['regime_policy']['baseline'] == historical
            assert NOW.date().isoformat() in json.dumps(historical)
            from src.execution.safety.risk_sources import RiskSourceCoordinator
            # Historical index provenance survives, but has no current-day entry authority.
            assert RiskSourceCoordinator(restored).read_source('index_trend').authority_status != 'current'
            assert state['risk']['day'] == times[0].date().isoformat()
            from src.execution.safety.commands import RequestBoundCommands
            broker, builder, _ = _scoped_broker()
            authority = EntryAuthority()
            commands = RequestBoundCommands(restored, builder=builder, authority=authority,
                entry_guard=FinalEntryGuard(authority,
                    lambda: RiskSnapshot(1, 1, 'success', times[0], 'normal'), lambda: times[0]),
                stop_resolver=lambda _strategy: StopDecision(Decimal('5'), 'synthetic', False),
                session_guard=lambda _request: GuardDecision(True, 'synthetic-open'))
            await commands.publish_policy_context(_current_context(restored,
                risk_policy.MarketTrendPolicySnapshot(True, True, False)),
                expected_version=restored.owner.version)
            request = builder.prepare_submit(Order(symbol='005930', side=OrderSide.BUY, quantity=10,
                price=Decimal('10000'), order_type=OrderType.LIMIT, strategy='manual'), intent_id='I-next-day',
                attempt_id='next-day-no-source', session=RequestSession(times[0].date().isoformat(), times[0], 'regular'),
                valuation_price=Decimal('10000'))
            await commands.observe_entry_quote('005930', Decimal('10000'), as_of=times[0],
                source='independent-next-day-quote', event_id='next-day-no-source-quote',
                expected_version=restored.owner.version)
            with pytest.raises(ValueError, match='regime_source_not_current'):
                await commands.prepare(request, authority.user_order('005930', 'buy'))
            assert broker._session.posts == [] and not restored.owner.state['attempts']
        finally:
            if runtime is not None:
                await runtime.shutdown()
            elif restored is not None:
                await restored.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ('policy_aba', 'vix_latest', 'unrelated_commit'))
def test_captured_two_minute_reads_reject_relevant_aba_but_ignore_unrelated_owner_commit(tmp_path, monkeypatch, change):
    """Fails if a schema3 seal recomputes reads after its capture instead of comparing them."""
    async def scenario():
        from src.execution.safety.risk_sources import RiskSourceCoordinator
        _, _, store, runtime, _, _, owner = await _installed(tmp_path, monkeypatch)
        provider, _ = await _actual_kis(monkeypatch,
            {'0001': _bullish_output(), '1001': _bullish_output()})
        entered, release = asyncio.Event(), asyncio.Event()
        original_seal = RiskSourceCoordinator.seal

        async def hold_before_index_seal(coordinator, ticket, *args, **kwargs):
            if ticket.kind == 'index_trend' and not entered.is_set():
                entered.set()
                await release.wait()
            return await original_seal(coordinator, ticket, *args, **kwargs)

        # This is before seal acquires the owner reducer lock.  Holding
        # lookup_commit would deadlock the real concurrent owner mutation.
        monkeypatch.setattr(RiskSourceCoordinator, 'seal', hold_before_index_seal)
        before_policy = deepcopy(runtime.owner.state['regime_policy'])
        refresh = asyncio.create_task(owner.refresh_trend(provider, expert_orchestrator=None))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if change == 'policy_aba':
                def to_b(state):
                    state['entry_policy_effects']['sidecar_active'] = False
                    return state
                def back_to_a(state):
                    state['entry_policy_effects']['sidecar_active'] = True
                    return state
                await runtime.owner.mutate('independent-captured-policy-b', to_b)
                await runtime.owner.mutate('independent-captured-policy-a', back_to_a)
            elif change == 'vix_latest':
                source = RiskSourceCoordinator(runtime)
                ticket = await source.begin('independent-new-vix', 'vix_regime')
                assert (await source.complete(ticket, 'success', {
                    'value': 22.0, 'fetched_at': NOW.isoformat(),
                })).status == 'accepted'
            else:
                def unrelated(state):
                    state['entry_policy_effects']['pending_sectors']['OTHER'] = 'synthetic'
                    return state
                await runtime.owner.mutate('independent-unrelated-entry-effect', unrelated)
            release.set()
            receipt = await asyncio.wait_for(refresh, 2)
            row = runtime.owner.state['risk_sources']['records'][receipt.operation_id]
            seal = runtime.owner.state['risk_input_seals']['records'][receipt.operation_id]['original']
            assert seal['request']['schema'] == 3
            assert type(seal['request']['expected_reads']) is dict
            assert seal['request']['expected_reads']
            if change == 'unrelated_commit':
                assert receipt.status == 'accepted'
                assert row['terminal']['receipt']['status'] == 'accepted'
            else:
                assert (receipt.status, receipt.reason) == ('stale', 'input_seal')
                assert (seal['status'], seal['reason']) == ('stale', 'captured_reads_changed')
                assert row['terminal']['receipt']['status'] == 'stale'
                assert runtime.owner.state['regime_policy'] == before_policy
            assert runtime.owner.healthy and not runtime.health()['command_results_failed']
        finally:
            release.set()
            await asyncio.gather(refresh, return_exceptions=True)
            await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('source_fact', ('pending', 'failed'))
def test_installed_command_prepare_rejects_current_nonaccepted_regime_source_despite_optimistic_context(tmp_path, monkeypatch, source_fact):
    """Fails if replacing an accepted trend with pending/failed still permits prepare."""
    async def scenario():
        from src.execution.safety.commands import RequestBoundCommands
        from src.execution.safety.risk_sources import RiskSourceCoordinator
        _, _, store, runtime, _, _, owner = await _installed(tmp_path, monkeypatch)
        provider, _ = await _actual_kis(monkeypatch,
            {'0001': _bullish_output(), '1001': _bullish_output()})
        broker, builder, _ = _scoped_broker()
        authority = EntryAuthority()
        commands = RequestBoundCommands(runtime, builder=builder, authority=authority,
            entry_guard=FinalEntryGuard(authority,
                lambda: RiskSnapshot(1, 1, 'success', NOW, 'normal'), lambda: NOW),
            stop_resolver=lambda _strategy: StopDecision(Decimal('5'), 'synthetic', False),
            session_guard=lambda _request: GuardDecision(True, 'synthetic-open'))
        try:
            accepted = await owner.refresh_trend(provider, expert_orchestrator=None)
            assert accepted.status == 'accepted'
            assert owner.sources.read_source('index_trend',
                expected_version=accepted.committed_version).authority_status == 'current'
            await commands.publish_policy_context(_current_context(runtime,
                risk_policy.MarketTrendPolicySnapshot(True, True, False)),
                expected_version=runtime.owner.version)
            request = builder.prepare_submit(Order(symbol='005930', side=OrderSide.BUY, quantity=10,
                price=Decimal('10000'), order_type=OrderType.LIMIT, strategy='manual'), intent_id='I-regime',
                attempt_id='regime-source', session=RequestSession(NOW.date().isoformat(), NOW, 'regular'),
                valuation_price=Decimal('10000'))
            await commands.observe_entry_quote('005930', Decimal('10000'), as_of=NOW,
                source='independent-quote', event_id='regime-source-quote',
                expected_version=runtime.owner.version)
            sources = RiskSourceCoordinator(runtime)
            ticket = await sources.begin('independent-current-index', 'index_trend')
            if source_fact == 'failed':
                assert (await sources.complete(ticket, 'failed')).status == 'failed'
            context = authority.user_order('005930', 'buy')
            with pytest.raises(ValueError, match='regime_source_not_current'):
                await commands.prepare(request, context)
            assert broker._session.posts == [] and not runtime.owner.state['attempts']
        finally:
            await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_prepared_dispatch_rechecks_replaced_current_regime_source_before_any_http_post(tmp_path, monkeypatch):
    """Fails if dispatch trusts a prepare-time source snapshot after its index lane is replaced."""
    async def scenario():
        from src.execution.safety.commands import RequestBoundCommands
        from src.execution.safety.runtime import KRExecutionRuntime
        from src.execution.safety.risk_sources import RiskSourceCoordinator
        from src.execution.safety.transport import GuardedKISTransport
        _, _, store, runtime, _, _, owner = await _installed(tmp_path, monkeypatch)
        provider, _ = await _actual_kis(monkeypatch,
            {'0001': _bullish_output(), '1001': _bullish_output()})
        broker, builder, _ = _scoped_broker()
        authority = EntryAuthority()
        commands = RequestBoundCommands(runtime, builder=builder, authority=authority,
            entry_guard=FinalEntryGuard(authority,
                lambda: RiskSnapshot(1, 1, 'success', NOW, 'normal'), lambda: NOW),
            stop_resolver=lambda _strategy: StopDecision(Decimal('5'), 'synthetic', False),
            session_guard=lambda _request: GuardDecision(True, 'synthetic-open'))
        try:
            assert (await owner.refresh_trend(provider, expert_orchestrator=None)).status == 'accepted'
            await commands.publish_policy_context(_current_context(runtime,
                risk_policy.MarketTrendPolicySnapshot(True, True, False)),
                expected_version=runtime.owner.version)
            request = builder.prepare_submit(Order(symbol='005930', side=OrderSide.BUY, quantity=10,
                price=Decimal('10000'), order_type=OrderType.LIMIT, strategy='manual'), intent_id='I-dispatch',
                attempt_id='dispatch-source-replaced', session=RequestSession(NOW.date().isoformat(), NOW, 'regular'),
                valuation_price=Decimal('10000'))
            context = authority.user_order('005930', 'buy')
            await commands.observe_entry_quote('005930', Decimal('10000'), as_of=NOW,
                source='independent-dispatch-quote', event_id='dispatch-source-quote',
                expected_version=runtime.owner.version)
            await commands.prepare(request, context)
            prepared = deepcopy(runtime.owner.state['attempts'][request.attempt_id])
            replacement = await RiskSourceCoordinator(runtime).begin(
                'independent-dispatch-index-replacement', 'index_trend')
            assert replacement.lane == 'index_trend'
            observed_snapshot_errors = []
            original_snapshot = commands._snapshot

            def observe_snapshot(*args, **kwargs):
                try:
                    return original_snapshot(*args, **kwargs)
                except ValueError as exc:
                    observed_snapshot_errors.append(str(exc))
                    raise

            # Observation only: real _snapshot still owns its source read and
            # exact failure.  _dispatch intentionally maps it to not_sent.
            monkeypatch.setattr(commands, '_snapshot', observe_snapshot)
            # The test admits only the dispatch startup gate; source revalidation
            # remains the real _bound → _evaluate → _snapshot path.
            monkeypatch.setattr(KRExecutionRuntime, 'trading_ready', property(lambda _self: True))
            result = await commands.dispatch(request, context,
                GuardedKISTransport(broker, request_builder=builder))
            assert result.status.value == 'not_sent'
            assert observed_snapshot_errors == ['regime_source_not_current']
            # S3-3: claim 이전 실패는 예약을 남기지 않고 시도를 끝낸다(행이 prepared 로 남던 현행 갱신).
            current = runtime.owner.state['attempts'][request.attempt_id]
            assert (current['state'], current['reason_code']) == ('final_rejected',
                                                                  'regime_source_not_current')
            assert (current['reserved_quantity'], current['reserved_cash']) == (0, '0')
            assert current['request_binding'] == prepared['request_binding']
            assert runtime.owner.healthy
            assert broker._session.posts == []
        finally:
            await runtime.shutdown(); await store.close()
    asyncio.run(scenario())
