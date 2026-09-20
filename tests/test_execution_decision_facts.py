"""S1(B2a): 요청에 묶인 진입 판단 사실과 최종 kernel 재검사.

전부 합성 fake HTTP + 주입 시계다. 실제 송신 경로·운영 승격 근거가 아니며
`trading_ready` 는 제품 코드에서 계속 False 다.
"""
import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import importlib
from pathlib import Path

import pytest

from src.core.types import OrderSide, StrategyType
from src.execution.safety import risk_policy as p
from src.execution.safety.economics import decode_portfolio, encode_portfolio
from src.execution.safety.lifecycle import CommandStatus
from src.execution.safety.transport import GuardedKISTransport
from src.utils.fee_calculator import get_fee_calculator
from src.utils.sizing import atr_position_multiplier

from test_execution_command_owner import fixture as command_fixture, held
from test_execution_risk_policy import NOW, snapshot as policy_snapshot
from test_execution_sizing_characterization import (  # noqa: F401 — pytest fixture 재사용
    EQUITY, PRICE, external_factors, _config, _manager, _signal, _stop_recorder,
)


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


def api():
    return importlib.import_module('src.execution.safety.decisions')


def nominal():
    """kernel 재검사만으로 상한이 정해지는 구성(위험 cap 미발동)."""
    return replace(policy_snapshot(p).policy, sizing_mode='nominal')


# 고정 fixture 경제(자산 200만·가격 1만·기본 25%)에서 kernel 이 정당화하는 최대 수량.
JUSTIFIED = 50


def make_facts(req, *, sector=None, stop=None, **changes):
    module = api()
    values = dict(
        intent_id=req.intent_id, symbol=req.symbol, side=req.side.value, strategy=req.strategy,
        origin='automatic', sector=sector, config_version='effective-config',
        decided_at=NOW, expires_at=NOW + timedelta(minutes=30), base_pct=0.25,
        strategy_allocation_pct=None, min_position_value=D('200000'), strength_multiplier=1.0,
        position_multiplier=1.0, calendar_multiplier=1.0, volatility_multiplier=1.0,
        conviction_multiplier=1.0, atr_pct=None,
        stop_pct=None if stop is None else stop.stop_pct,
        stop_source=None if stop is None else stop.source,
        stop_crash_capped=None if stop is None else stop.crash_capped,
        qualification=module.QualificationFacts(72.0, 72.0, 70.0, ('rule_11',), 'allow'),
        sources=(module.ConsumedSource('trade_memory', 1, NOW, 'synthetic-memory-digest'),))
    values.update(changes)
    return module.EntryDecisionFacts(**values)


async def publish(f, facts, *, sources=True):
    commands, runtime = f['commands'], f['runtime']
    if sources:
        for source in facts.sources:
            await commands.publish_qualification_source(source.name, as_of=source.as_of,
                digest=source.digest, expected_version=runtime.owner.version)
    await commands.publish_decision_facts(facts, expected_version=runtime.owner.version)
    return facts


async def economy(f, **changes):
    """owner checkpoint 와 legacy portfolio 를 함께 움직여 writer 충돌을 만들지 않는다."""
    def reduce(state):
        portfolio = decode_portfolio(state['portfolio'])
        for key, value in changes.items():
            setattr(portfolio, key, value)
        state['portfolio'] = encode_portfolio(portfolio)
        return state
    await f['runtime'].owner.mutate('synthetic-economy:'+''.join(changes), reduce)
    for key, value in changes.items():
        setattr(f['engine'].portfolio, key, value)


@pytest.mark.parametrize('route', ['automatic', 'user', 'safe_asset', 'sell', 'cancel'])
def test_only_automatic_buy_requires_published_decision_facts(tmp_path, monkeypatch, route):
    async def scenario():
        origin = 'automatic' if route in ('automatic', 'sell', 'cancel') else route
        f = await command_fixture(tmp_path, monkeypatch, origin=origin, policy=nominal())
        try:
            if route == 'sell':
                await held(f)
            req = f['request'](side=OrderSide.SELL if route == 'sell' else OrderSide.BUY,
                               strategy='safe_asset' if route == 'safe_asset' else None)
            if route != 'sell':
                await f['quote'](req)
            if route == 'automatic':
                with pytest.raises(ValueError):
                    await f['commands'].prepare(req, f['entry'](req))
                assert f['runtime'].owner.state['attempts'] == {}
                assert f['broker']._session.posts == []
                return
            if route == 'cancel':
                await publish(f, make_facts(req))
            attempt = await f['commands'].prepare(req, f['entry'](req))
            assert attempt['request_binding']['fingerprint'] == req.fingerprint
            result = await f['commands'].dispatch(req, f['entry'](req),
                GuardedKISTransport(f['broker'], request_builder=f['builder']))
            assert result.status is CommandStatus.ACKNOWLEDGED
            if route != 'cancel':
                return
            # 취소는 자동 매수 판단을 다시 요구하지 않는다(청산 판단은 protection owner 소관).
            from src.execution.safety.lifecycle import OrderRef
            from src.execution.safety.requests import CancelParent
            parent = f['runtime'].owner.state['attempts']['A']
            cancel = f['builder'].prepare_cancel(intent_id=req.intent_id, attempt_id='C',
                session=req.session, parent=CancelParent(req.intent_id, req.attempt_id,
                    parent['version'], OrderRef.from_dict(parent['order_ref']), req.symbol,
                    req.side, req.order_type, parent['reserved_quantity'],
                    req.valuation_price, req.strategy))
            await f['commands'].prepare(cancel, f['entry'](cancel))
            cancelled = await f['commands'].dispatch(cancel, f['entry'](cancel),
                GuardedKISTransport(f['broker'], request_builder=f['builder']))
            assert cancelled.status is CommandStatus.ACKNOWLEDGED
            assert len(f['broker']._session.posts) == 2
        finally: await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('mismatch', ['symbol', 'strategy', 'quantity', 'stop'])
def test_published_facts_must_describe_the_actual_request(tmp_path, monkeypatch, mismatch):
    async def scenario():
        policy = nominal() if mismatch != 'stop' else policy_snapshot(p).policy
        f = await command_fixture(tmp_path, monkeypatch, origin='automatic', policy=policy)
        try:
            quantity = JUSTIFIED + 1 if mismatch == 'quantity' else 10
            req = f['request'](quantity=quantity)
            await f['quote'](req)
            changes = {}
            if mismatch == 'symbol': changes['symbol'] = '000660'
            if mismatch == 'strategy': changes['strategy'] = 'gap_and_go'
            stop = None if mismatch != 'stop' else replace(f['stop'][0], stop_pct=D('4'))
            await publish(f, make_facts(req, stop=stop, **changes))
            with pytest.raises(ValueError):
                await f['commands'].prepare(req, f['entry'](req))
            assert f['runtime'].owner.state['attempts'] == {}
            assert f['broker']._session.posts == []
        finally: await f['store'].close()
    asyncio.run(scenario())


async def paused_dispatch(f, req, *, boundary):
    reached, release = asyncio.Event(), asyncio.Event()
    async def pause(*args):
        reached.set()
        await release.wait()
        f['broker']._session.closed = False
        return 'synthetic-hash' if boundary == 'hashkey' else True
    if boundary == 'connect': f['broker']._session.closed = True
    setattr(f['broker'], {'connect': 'connect', 'hashkey': '_get_hashkey'}[boundary], pause)
    task = asyncio.create_task(f['commands'].dispatch(req, f['entry'](req),
        GuardedKISTransport(f['broker'], request_builder=f['builder'])))
    await asyncio.wait_for(reached.wait(), 2)
    return task, release


async def apply_change(f, req, facts, change):
    commands, runtime = f['commands'], f['runtime']
    if change == 'cash':
        await economy(f, cash=D('600000'))
    elif change == 'reservation':
        other = f['request']('B', symbol='000660', quantity=140, strategy='manual')
        await f['quote'](other)
        await commands.prepare(other, f['authority'].user_order(other.symbol, 'buy'))
    elif change == 'daily_loss':
        await economy(f, daily_pnl=D('-60000'))
    elif change == 'source':
        source = facts.sources[0]
        await commands.publish_qualification_source(source.name, as_of=source.as_of,
            digest='synthetic-memory-digest-2', expected_version=runtime.owner.version)
    elif change == 'config':
        context = f['ctx']
        context = replace(context, versions=replace(context.versions,
            execution=runtime.owner.version, config='effective-config-2'))
        await commands.publish_policy_context(context, expected_version=runtime.owner.version)
    elif change == 'expired':
        f['clock'][0] = NOW + timedelta(minutes=45)
    else:
        raise AssertionError('unknown synthetic change')


@pytest.mark.parametrize('change', ['cash', 'reservation', 'daily_loss', 'source', 'config', 'expired'])
def test_final_recheck_blocks_post_after_the_network_await(tmp_path, monkeypatch, change):
    async def scenario():
        f = await command_fixture(tmp_path, monkeypatch, origin='automatic', policy=nominal())
        try:
            req = f['request'](quantity=JUSTIFIED)
            await f['quote'](req)
            facts = await publish(f, make_facts(req))
            prepared = await f['commands'].prepare(req, f['entry'](req))
            assert (prepared['reserved_cash'], prepared['reserved_quantity']) == ('507500.000', 50)
            task, release = await paused_dispatch(f, req, boundary='connect')
            await apply_change(f, req, facts, change)
            release.set()
            result = await task
            assert result.status in (CommandStatus.NOT_SENT, CommandStatus.UNKNOWN)
            assert f['broker']._session.posts == []
            # 미송신은 예약을 새로 잡지도 남기지도 않는다(누수 없이 전량 해제).
            current = f['runtime'].owner.state['attempts']['A']
            assert (D(current['reserved_cash']), D(current['reserved_exposure']),
                    current['reserved_quantity']) == (D('0'), D('0'), 0)
        finally: await f['store'].close()
    asyncio.run(scenario())


def test_final_recheck_also_covers_the_hashkey_await_boundary(tmp_path, monkeypatch):
    async def scenario():
        f = await command_fixture(tmp_path, monkeypatch, origin='automatic', policy=nominal())
        try:
            req = f['request'](quantity=JUSTIFIED)
            await f['quote'](req)
            facts = await publish(f, make_facts(req))
            await f['commands'].prepare(req, f['entry'](req))
            task, release = await paused_dispatch(f, req, boundary='hashkey')
            await apply_change(f, req, facts, 'source')
            release.set()
            assert (await task).status in (CommandStatus.NOT_SENT, CommandStatus.UNKNOWN)
            assert f['broker']._session.posts == []
        finally: await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('unrelated', ['other_quote', 'other_source', 'policy_context'])
def test_unrelated_owner_version_growth_is_not_stale(tmp_path, monkeypatch, unrelated):
    async def scenario():
        f = await command_fixture(tmp_path, monkeypatch, origin='automatic', policy=nominal())
        commands, runtime = f['commands'], f['runtime']
        try:
            req = f['request'](quantity=JUSTIFIED)
            await f['quote'](req)
            await publish(f, make_facts(req))
            await commands.prepare(req, f['entry'](req))
            before = runtime.owner.version
            if unrelated == 'other_quote':
                await commands.observe_entry_quote('000660', D('20000'), as_of=f['clock'][0],
                    source='synthetic-market', event_id='unrelated-quote',
                    expected_version=runtime.owner.version)
            elif unrelated == 'other_source':
                await commands.publish_qualification_source('sector_council', as_of=f['clock'][0],
                    digest='synthetic-council-digest', expected_version=runtime.owner.version)
            else:
                context = replace(f['ctx'], versions=replace(f['ctx'].versions,
                                                             execution=runtime.owner.version))
                await commands.publish_policy_context(context, expected_version=runtime.owner.version)
            assert runtime.owner.version > before
            result = await commands.dispatch(req, f['entry'](req),
                GuardedKISTransport(f['broker'], request_builder=f['builder']))
            assert result.status is CommandStatus.ACKNOWLEDGED
            assert len(f['broker']._session.posts) == 1
        finally: await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['future_as_of', 'unknown_source', 'republish_conflict', 'republish_same'])
def test_publication_pins_day_identity_and_immutability(tmp_path, monkeypatch, fault):
    async def scenario():
        f = await command_fixture(tmp_path, monkeypatch, origin='automatic', policy=nominal())
        commands, runtime = f['commands'], f['runtime']
        try:
            req = f['request'](quantity=JUSTIFIED)
            await f['quote'](req)
            if fault == 'future_as_of':
                with pytest.raises(ValueError):
                    await commands.publish_qualification_source('trade_memory',
                        as_of=NOW + timedelta(minutes=1), digest='synthetic-memory-digest',
                        expected_version=runtime.owner.version)
                assert 'qualification_sources' not in runtime.owner.state
                return
            if fault == 'unknown_source':
                with pytest.raises(ValueError):
                    await publish(f, make_facts(req), sources=False)
                assert 'entry_decision_facts' not in runtime.owner.state
                return
            facts = await publish(f, make_facts(req))
            if fault == 'republish_conflict':
                with pytest.raises(ValueError):
                    await commands.publish_decision_facts(
                        make_facts(req, base_pct=0.20), expected_version=runtime.owner.version)
            else:
                await commands.publish_decision_facts(facts, expected_version=runtime.owner.version)
            assert (runtime.owner.state['entry_decision_facts'][req.intent_id]
                    == facts.to_dict())
            attempt = await commands.prepare(req, f['entry'](req))
            assert attempt['request_binding']['decision_facts_digest'] == facts.digest
        finally: await f['store'].close()
    asyncio.run(scenario())


def test_synthetic_startup_permission_absent_never_posts(tmp_path, monkeypatch):
    async def scenario():
        f = await command_fixture(tmp_path, monkeypatch, ready=False, origin='automatic',
                                  policy=nominal())
        try:
            req = f['request'](quantity=JUSTIFIED)
            await f['quote'](req)
            await publish(f, make_facts(req))
            await f['commands'].prepare(req, f['entry'](req))
            result = await f['commands'].dispatch(req, f['entry'](req),
                GuardedKISTransport(f['broker'], request_builder=f['builder']))
            assert result.status is CommandStatus.NOT_SENT
            assert f['broker']._session.posts == []
            assert f['runtime'].trading_ready is False
        finally: await f['store'].close()
    asyncio.run(scenario())


def _parity_snapshot(*, mode, core_pct, cash=EQUITY, positions=()):
    policy = replace(policy_snapshot(p).policy, sizing_mode=mode, core_allocation_pct=core_pct,
        regime_min_cash_reserve_pct=0.0, max_position_pct=28.0, daily_max_loss_pct=5.0,
        risk_per_trade_pct=0.7, risk_max_position_pct=18.0,
        buy_commission_rate=get_fee_calculator('KR').config.buy_commission_rate)
    return replace(policy_snapshot(p), policy=policy,
        portfolio=p.PortfolioPolicySnapshot(D(cash), EQUITY, D('0'), 0, positions))


def test_final_kernel_quantity_equals_the_actual_legacy_wrapper(external_factors):
    """실제 wrapper 와 같은 표본에서 수량이 일치해야 한다(hybrid 는 범위 밖)."""
    module = api()
    stop_calls = []
    atr = atr_position_multiplier(6.0)
    cases = [
        ('nominal', 175, _manager(), _signal(), _parity_snapshot(mode='nominal', core_pct=30.0),
         dict(base_pct=0.25, strategy_allocation_pct=42.0)),
        ('risk', 139, _manager(config=_config(mode='risk'), stop=_stop_recorder(stop_calls)),
         _signal(metadata={'atr_pct': 6.0, 'position_multiplier': atr}),
         _parity_snapshot(mode='risk', core_pct=30.0),
         dict(base_pct=0.25, strategy_allocation_pct=42.0, atr_pct=6.0, position_multiplier=atr,
              stop_pct=D('5'), stop_source='strategy', stop_crash_capped=False)),
        ('core', 100, _manager(config=_config(mode='risk'), stop=_stop_recorder(stop_calls)),
         _signal(strategy=StrategyType.CORE_HOLDING),
         _parity_snapshot(mode='risk', core_pct=30.0),
         dict(strategy='core_holding', base_pct=0.10, strategy_allocation_pct=30.0)),
        # wrapper는 get_available_cash를 직접 눌러 1.3M을 만들고, owner는 현금 1.3M +
        # 무관 전략 보유 8.7M으로 같은 자산/가용현금을 구성한다.
        ('market_affordability', 100, _manager(config=_config(core_pct=0.0), available=D('1300000')),
         _signal(), _parity_snapshot(mode='nominal', core_pct=0.0, cash=D('1300000'),
             positions=(p.PositionPolicyFact('000660', 'gap_and_go', None, 87, D('8700000'),
                                             NOW.date(), None, None, None, False),)),
         dict(base_pct=0.25, strategy_allocation_pct=42.0)),
    ]
    for name, expected, manager, event, snapshot, changes in cases:
        legacy = manager._calculate_position_size(event)
        facts = module.EntryDecisionFacts(
            intent_id='I-parity', symbol='005930', side='buy',
            strategy=changes.pop('strategy', 'sepa_trend'), origin='automatic', sector=None,
            config_version='effective-config', decided_at=NOW,
            expires_at=NOW + timedelta(minutes=30), base_pct=changes.pop('base_pct'),
            strategy_allocation_pct=changes.pop('strategy_allocation_pct'),
            min_position_value=D('200000'), strength_multiplier=1.0,
            position_multiplier=changes.pop('position_multiplier', 1.0), calendar_multiplier=1.0,
            volatility_multiplier=1.0, conviction_multiplier=1.0,
            atr_pct=changes.pop('atr_pct', None), stop_pct=changes.pop('stop_pct', None),
            stop_source=changes.pop('stop_source', None),
            stop_crash_capped=changes.pop('stop_crash_capped', None),
            qualification=module.QualificationFacts(72.0, 72.0, 70.0, (), None), sources=())
        assert not changes
        assert legacy == expected, name
        assert module.recompose_quantity(facts, snapshot, price=PRICE).quantity == legacy, name
