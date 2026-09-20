"""S2-3: final 동기 구간의 regime 재유도 대조.

실제 SQLite owner·실제 RegimeOwner 설치 + 합성 fake HTTP·주입 시계다. 실제 송신
경로나 운영 승격 근거가 아니며 `trading_ready` 는 제품 코드에서 계속 False 다.
"""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.core.market_regime import MarketRegimeAdapter
from src.core.types import Order, OrderSide, OrderType, RiskConfig
from src.execution.safety import risk_policy as p
from src.execution.safety.commands import RequestBoundCommands
from src.execution.safety.decisions import ConsumedSource
from src.execution.safety.guards import EntryAuthority, FinalEntryGuard, GuardDecision, RiskSnapshot
from src.execution.safety.lifecycle import CommandStatus
from src.execution.safety.policy_generations import canonical
from src.execution.safety.policy_snapshot import PolicyContext
from src.execution.safety.protection_recovery import digest as sha256
from src.execution.safety.regime_owner import effective_regime
from src.execution.safety.requests import RequestSession
from src.execution.safety.risk_transition import IntradayPolicyState
from src.execution.safety.runtime import KRExecutionRuntime
from src.execution.safety.transport import GuardedKISTransport
from src.risk.manager import RiskManager
from src.utils.stop_policy import StopDecision

from test_execution_command_owner import fixture as command_fixture
from test_execution_decision_facts import make_facts, nominal, publish
from test_execution_regime_owner import baseline_json
from test_execution_risk_policy import NOW, snapshot as policy_snapshot
from test_execution_runtime import setup
from test_kr_final_dispatch import Response
from test_kr_prepared_dispatch import fixture as broker_fixture


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


def bullish(code):
    """평균 등락률 +2%·시가 대비 상승 — 중기 bull 을 유지시키는 합성 지수 관측."""
    values = {'price': 102.0, 'open': 100.0, 'high': 103.0, 'low': 99.0,
              'change': 2.0, 'change_pct': 2.0}
    return {**values, '_observation': {'schema_version': 1, 'source': 'kis',
        'source_tr': 'FHPUP02100000', 'index_code': code, 'observation_id': str(uuid4()),
        'received_at': NOW.isoformat(), 'market_as_of': None,
        'fields': {key: {'status': 'valid', 'value': value} for key, value in values.items()}}}


def regime_digest(regime):
    """계약 2 의 식을 시험이 독립으로 다시 적는다(제품 헬퍼를 경유하지 않는다)."""
    return sha256(canonical({'effective_regime': regime}))


async def fixture(tmp_path, monkeypatch, *, mid_regime='bull'):
    """command owner 배선에 실제 regime owner 를 설치한 형태."""
    from src.execution.safety.regime_owner import POLICY_READS, RegimeBaseline, RegimeOwner
    clock = [NOW]
    broker, builder, calls = broker_fixture(Response(data={'rt_cd': '0', 'output': {
        'ODNO': '1234567890', 'KRX_FWDG_ORD_ORGNO': '12345'}}))
    sidecar = RiskManager(RiskConfig(), D('2000000'))
    engine, exits, store, runtime = await setup(tmp_path, account_scope='test-scope',
                                                risk_manager=sidecar, clock=lambda: clock[0])
    # 명시 합성 startup 허가. 실제 runtime property 는 계속 False 다.
    monkeypatch.setattr(KRExecutionRuntime, 'trading_ready', property(lambda self: True))
    adapter = MarketRegimeAdapter()
    engine._regime_adapter = adapter
    def seed(state):
        value = IntradayPolicyState('normal', 0.0, None, None).to_dict()
        state['intraday_policy'] = {'schema': 1, 'baseline': value,
            'baseline_version': runtime.owner.version + 1, 'current': value.copy(), 'transitions': {}}
        state['entry_policy_effects'] = {'pending_sectors': {}, 'sidecar_active': True}
        return state
    await runtime.owner.mutate('known-prerequisite', seed)
    supplied = baseline_json(runtime)
    supplied['account_scope'] = runtime.account_scope
    supplied['trend_state']['mid_regime'] = supplied['engine_regime'] = mid_regime
    await RegimeOwner.register_baseline(runtime, RegimeBaseline.from_dict(supplied),
                                        expected_version=runtime.owner.version)
    await runtime.owner.register_policy_generations('known-reads', POLICY_READS)
    async def missing_vix(): return None
    writer = RegimeOwner(runtime, adapter=adapter, sidecar=sidecar, vix_fetcher=missing_vix)
    ticket = await writer.sources.begin('fresh-vix', 'vix_regime')
    await writer.sources.complete(ticket, 'success', {'value': 20.0, 'fetched_at': NOW.isoformat()},
        source='synthetic-vix', source_event_id='fresh-vix', received_at=NOW)
    async def refresh():
        async def index(code): return bullish(code)
        return await writer.refresh_trend(SimpleNamespace(fetch_index_price=index))
    # 지수 출처가 current 여야 owner snapshot 이 진입을 평가한다.
    assert (await refresh()).status == 'accepted'
    authority = EntryAuthority()
    alpha = [RiskSnapshot(1, 1, 'success', NOW, 'normal')]
    session = [GuardDecision(True, 'synthetic_open')]
    stop = [StopDecision(D('5'), 'strategy', False)]
    commands = RequestBoundCommands(runtime, builder=builder, authority=authority,
        entry_guard=FinalEntryGuard(authority, lambda: alpha[0], lambda: clock[0]),
        stop_resolver=lambda strategy: stop[0], session_guard=lambda request: session[0])
    ctx = PolicyContext.from_snapshot(policy_snapshot(p))
    ctx = replace(ctx, policy=nominal(), versions=replace(ctx.versions, execution=runtime.owner.version))
    await commands.publish_policy_context(ctx, expected_version=runtime.owner.version)
    def request(aid='A', *, symbol='005930', price=D('10000')):
        return builder.prepare_submit(Order(symbol=symbol, side=OrderSide.BUY, quantity=10,
            price=price, order_type=OrderType.LIMIT, strategy='sepa_trend'), intent_id='I-'+aid,
            attempt_id=aid, session=RequestSession(NOW.date().isoformat(), NOW, 'regular'),
            valuation_price=price)
    def entry(req):
        return authority.automatic(req.symbol, req.side.value, req.strategy)
    async def market_quote(req):
        await commands.observe_entry_quote(req.symbol, req.valuation_price, as_of=clock[0],
            source='synthetic-market', event_id='quote-'+req.attempt_id,
            expected_version=runtime.owner.version)
    async def send(req):
        return await commands.dispatch(req, entry(req),
                                       GuardedKISTransport(broker, request_builder=builder))
    async def intraday(level, when):
        """장중 급락 레벨만 바꾼다. 중기 추세 문자열은 건드리지 않는다.

        batch mirror 를 설치하지 않았으므로 기준선 자체를 옮긴다(관측 재생이 아니다).
        """
        value = IntradayPolicyState(level, -5.0, when, None).to_dict()
        def reduce(state):
            state['intraday_policy']['baseline'] = value
            state['intraday_policy']['current'] = dict(value)
            state['protection']['intraday_crash_level'] = level
            return state
        exits._intraday_crash_level = level
        await runtime.owner.mutate('synthetic-intraday:' + level + str(when), reduce)
    return locals()


async def cite_regime(f, regime):
    """S2-5 어댑터의 idempotent 재사용: digest 가 같으면 재게시하지 않는다."""
    commands, runtime = f['commands'], f['runtime']
    value = regime_digest(regime)
    current = commands.read_qualification_source('regime')
    if current is None or current['digest'] != value:
        await commands.publish_qualification_source('regime', as_of=f['clock'][0], digest=value,
                                                    expected_version=runtime.owner.version)
        current = commands.read_qualification_source('regime')
    return ConsumedSource('regime', current['version'],
                          datetime.fromisoformat(current['as_of']), current['digest'])


async def bound(f, aid='A', *, symbol='005930', regime='bull'):
    req = f['request'](aid, symbol=symbol)
    await f['market_quote'](req)
    source = await cite_regime(f, regime)
    await publish(f, make_facts(req, sources=(source,)), sources=False)
    await f['commands'].prepare(req, f['entry'](req))
    return req


def test_intraday_crash_after_prepare_blocks_the_post_and_names_the_stale_axis(tmp_path, monkeypatch):
    """판단~송신 사이 유효 레짐이 바뀌면 재유도 대조가 POST 를 막아야 한다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            req = await bound(f)
            state = f['runtime'].owner.state
            assert effective_regime(state, f['clock'][0]) == 'bull'
            await f['intraday']('crash', f['clock'][0])
            state = f['runtime'].owner.state
            # 중기 추세 문자열은 그대로이고 유효 레짐만 강등된다.
            assert state['regime_policy']['trend_state']['mid_regime'] == 'bull'
            assert effective_regime(state, f['clock'][0]) == 'sideways'
            result = await f['send'](req)
            assert result.status is CommandStatus.NOT_SENT
            assert f['broker']._session.posts == []
            # 같은 출처를 다시 인용한 새 요청은 정확한 사유로 거부된다.
            other = f['request']('B', symbol='000660')
            await f['market_quote'](other)
            source = ConsumedSource('regime', 1, f['clock'][0], regime_digest('bull'))
            await publish(f, make_facts(other, sources=(source,)), sources=False)
            with pytest.raises(ValueError, match='stale_regime_decision'):
                await f['commands'].prepare(other, f['entry'](other))
            assert 'B' not in f['runtime'].owner.state['attempts']
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


def test_intraday_crash_dated_before_today_does_not_demote_the_decision(tmp_path, monkeypatch):
    """당일 게이트가 닫힌 급락 관측은 유효 레짐을 바꾸지 않는다 — 과도 stale 방지."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            req = await bound(f)
            await f['intraday']('crash', f['clock'][0] - timedelta(days=1))
            assert effective_regime(f['runtime'].owner.state, f['clock'][0]) == 'bull'
            result = await f['send'](req)
            assert result.status is CommandStatus.ACKNOWLEDGED
            assert len(f['broker']._session.posts) == 1
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


def test_new_regime_commit_with_the_same_string_still_sends(tmp_path, monkeypatch):
    """regime_policy 가 다시 커밋돼도 유효 레짐 문자열이 같으면 과도 stale 이 아니다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            req = await bound(f)
            before = f['runtime'].owner.version
            assert (await f['refresh']()).status == 'accepted'
            state = f['runtime'].owner.state
            assert f['runtime'].owner.version > before
            assert state['regime_policy']['trend_state']['mid_regime'] == 'bull'
            result = await f['send'](req)
            assert result.status is CommandStatus.ACKNOWLEDGED
            assert len(f['broker']._session.posts) == 1
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


def test_unrelated_owner_version_bump_still_sends(tmp_path, monkeypatch):
    """소비하지 않은 관측으로 owner.version 만 올라간 경우는 stale 이 아니다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            req = await bound(f)
            before = f['runtime'].owner.version
            await f['commands'].observe_entry_quote('000660', D('10000'), as_of=f['clock'][0],
                source='synthetic-market', event_id='unrelated-quote', expected_version=before)
            assert f['runtime'].owner.version > before
            result = await f['send'](req)
            assert result.status is CommandStatus.ACKNOWLEDGED
            assert len(f['broker']._session.posts) == 1
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


def test_same_digest_is_reused_without_republishing_and_keeps_the_first_request(tmp_path, monkeypatch):
    """같은 digest 재게시는 앞선 in-flight 판단을 헛되이 stale 로 만든다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            first = await bound(f, 'A')
            published = f['commands'].read_qualification_source('regime')
            assert published['version'] == 1
            await bound(f, 'B', symbol='000660')
            assert f['commands'].read_qualification_source('regime') == published
            assert f['runtime'].owner.state['attempts']['B']['request_binding'] is not None
            # 뒤 판단이 같은 출처를 다시 게시했다면 앞 요청의 version 1 이 stale 이 된다.
            assert (await f['send'](first)).status is CommandStatus.ACKNOWLEDGED
            assert len(f['broker']._session.posts) == 1
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


def test_missing_regime_policy_rejects_a_facts_row_that_consumed_regime(tmp_path, monkeypatch):
    """재유도가 불가능한 state 에서는 소비 사실을 받지 않는다(fail-closed)."""
    async def scenario():
        f = await command_fixture(tmp_path, monkeypatch, origin='automatic', policy=nominal())
        try:
            assert 'regime_policy' not in f['runtime'].owner.state
            req = f['request']()
            await f['quote'](req)
            source = ConsumedSource('regime', 1, f['clock'][0], regime_digest('bull'))
            await publish(f, make_facts(req, sources=(source,)))
            with pytest.raises(ValueError, match='stale_regime_decision'):
                await f['commands'].prepare(req, f['entry'](req))
            assert f['runtime'].owner.state['attempts'] == {}
            assert f['broker']._session.posts == []
        finally:
            await f['store'].close()
    asyncio.run(scenario())


def test_rederivation_uses_the_owner_clock_not_the_published_decision_time(tmp_path, monkeypatch):
    """판단 시각은 게시본이 정한다. 그 값으로 당일 게이트를 재유도하면 급락이 가려진다.

    같은 순간을 UTC 표기로 담은 `decided_at` 은 게시 검사(astimezone 대조)를 통과하지만
    naive `.date()` 는 전일이라, 장중 급락 관측의 당일 게이트가 열린 것처럼 보인다.
    """
    async def scenario():
        from datetime import timezone
        f = await fixture(tmp_path, monkeypatch)
        try:
            # 이 시험이 두 시각을 구분하는 근거는 schema1 경로의 naive `.date()` 대조다.
            # schema3(horizon)는 양쪽을 KST 로 정규화해 이 변이가 동치가 된다 — fixture 가
            # schema3 으로 넘어가면 조용히 무력화되지 말고 여기서 깨져 다른 축으로 다시 세우게 한다.
            assert f['runtime'].owner.state['regime_policy'].get('schema', 1) == 1
            await f['intraday']('crash', f['clock'][0])
            assert effective_regime(f['runtime'].owner.state, f['clock'][0]) == 'sideways'
            decided = f['clock'][0].replace(hour=8).astimezone(timezone.utc)
            assert decided.date() != f['clock'][0].date()
            req = f['request']()
            await f['market_quote'](req)
            await f['commands'].publish_qualification_source('regime', as_of=decided,
                digest=regime_digest('bull'), expected_version=f['runtime'].owner.version)
            row = f['commands'].read_qualification_source('regime')
            source = ConsumedSource('regime', row['version'],
                                    datetime.fromisoformat(row['as_of']), row['digest'])
            await publish(f, make_facts(req, sources=(source,), decided_at=decided), sources=False)
            with pytest.raises(ValueError, match='stale_regime_decision'):
                await f['commands'].prepare(req, f['entry'](req))
            assert f['runtime'].owner.state['attempts'] == {}
            assert f['broker']._session.posts == []
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


def test_read_qualification_source_hands_out_a_detached_copy(tmp_path, monkeypatch):
    """반환값 변형이 게시본에 닿으면 어댑터가 stale 대조를 조용히 무력화한다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            req = await bound(f)
            copy = f['commands'].read_qualification_source('regime')
            copy['digest'] = regime_digest('bear')
            copy['version'] = 99
            copy['as_of'] = (f['clock'][0] - timedelta(hours=1)).isoformat()
            again = f['commands'].read_qualification_source('regime')
            assert again['digest'] == regime_digest('bull')
            assert (again['version'], again['as_of']) == (1, f['clock'][0].isoformat())
            assert f['runtime'].owner.state['qualification_sources']['regime'] == again
            assert f['commands'].read_qualification_source('panel_outlook') is None
            result = await f['send'](req)
            assert result.status is CommandStatus.ACKNOWLEDGED
            assert len(f['broker']._session.posts) == 1
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())
