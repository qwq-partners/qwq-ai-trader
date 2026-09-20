"""S3-4: 일자 전환이 전일 판단 사실만 정리한다 (결정 ⑩).

전환은 합성 dict 조작이 아니라 실제 진입점(`prepare_day_rollover` →
`accept_valuation_evidence` → `rollover_day` → `resume_after_rollover`)을 통과하고,
출처·판단 사실은 실제 `RequestBoundCommands.publish_qualification_source` /
`publish_decision_facts` 가 게시한다. 시계는 전부 주입한다.

돈 경로가 아니다. facts 를 지우면 저장소에 흔적이 남지 않으므로 이 단계를
'감사 보존'으로 읽지 않는다 — 게시 시점 로그는 S3-5 의 몫이다.
S3 는 설치가 아니다 · 제품 attach 호출자 0건 · 합성 startup 허가 위의 GREEN.

실행: venv/bin/python -m pytest tests/test_execution_decision_facts_gc.py -q -p no:cacheprovider
"""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from src.core.types import Order, OrderSide, OrderType
from src.execution.safety.commands import CommandValidationError
from src.execution.safety.day_recovery import ValuationEvidence, portfolio_fingerprint
from src.execution.safety.decisions import ConsumedSource
from src.execution.safety.requests import RequestSession
from src.execution.safety.store import encode_state

from src.execution.safety.lifecycle import CommandStatus

from test_execution_command_owner import fixture as command_fixture
from test_execution_day_recovery import prepare as prepare_rollover
from test_execution_dispatch_reasons import prepared as traded, send

# 전환이 건드리면 안 되는 뿌리. portfolio·risk 는 reset_daily 의 원래 소관이라 따로 본다.
UNTOUCHED = ('protection', 'lots', 'outbox', 'intents', 'attempts', 'qualification_sources')
# 일자 전환이 쓰는 뿌리는 이것뿐이다. 나머지는 손으로 열거하지 않고 '전환 직전의 전 뿌리'에서
# 유도해 대조한다 — 이 writer 에 뿌리 삭제가 하나 더 끼어들면 자동으로 잡힌다.
ROLLOVER_OWNED = {'portfolio', 'risk', 'entry_decision_facts',
                  'day_transition', 'day_valuations', 'day_valuation_view', 'recovery_receipts'}
TO_DAY = '2026-09-19'


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


async def rollover(f, *, measure=False):
    """실제 일자 전환 4단계. measure 면 rollover_day 직전/직후 checkpoint 크기를 돌려준다."""
    runtime = f['runtime']
    fence = await prepare_rollover(runtime, f['clock'])
    assert fence.status == 'PREPARED', fence.reason
    evidence = ValuationEvidence(runtime.account_scope, 'KR', fence.fence_id,
                                 portfolio_fingerprint(runtime.owner.state), f['clock'][0], ())
    receipt = await runtime.accept_valuation_evidence('valuation', evidence,
                                                      expected_version=runtime.owner.version)
    assert receipt.status == 'APPLIED', receipt.reason
    before = len(encode_state(runtime.owner.state)) if measure else 0
    roll = await runtime.rollover_day('roll', expected_version=runtime.owner.version,
                                      fence_id=fence.fence_id,
                                      valuation_evidence_id=receipt.evidence_id)
    assert roll.status == 'APPLIED', roll.reason
    after = len(encode_state(runtime.owner.state)) if measure else 0
    resumed = await runtime.resume_after_rollover('resume', expected_version=runtime.owner.version,
                                                  fence_id=fence.fence_id)
    assert resumed.status == 'APPLIED', resumed.reason
    return before, after


async def source(f, name, *, digest='digest-'):
    """출처 한 행을 실제 게시 함수로 올리고 facts 가 인용할 형태로 돌려준다."""
    await f['commands'].publish_qualification_source(
        name, as_of=f['clock'][0], digest=digest + name,
        expected_version=f['runtime'].owner.version)
    row = f['commands'].read_qualification_source(name)
    return ConsumedSource(name, row['version'], f['clock'][0], row['digest'])


async def decided(f, aid, cited):
    """자동 BUY 한 건의 판단 사실. 실제 publish_decision_facts 가 받는다."""
    from src.execution.safety.decisions import EntryDecisionFacts, QualificationFacts
    req = f['request'](aid, symbol='005930' if aid == 'A' else '000660')
    # 손절 축은 fixture 의 실제 resolver 값을 그대로 싣는다(risk 모드에서만 소비된다).
    stop = f['stop'][0] if f['ctx'].policy.sizing_mode == 'risk' else None
    facts = EntryDecisionFacts(
        intent_id=req.intent_id, symbol=req.symbol, side=req.side.value, strategy=req.strategy,
        origin='automatic', sector=None, config_version=f['ctx'].versions.config,
        hybrid_enabled=False, decided_at=f['clock'][0],
        expires_at=f['clock'][0] + timedelta(minutes=30), base_pct=0.25,
        strategy_allocation_pct=None, min_position_value=D('200000'), strength_multiplier=1.0,
        position_multiplier=1.0, calendar_multiplier=1.0, volatility_multiplier=1.0,
        conviction_multiplier=1.0, atr_pct=None,
        stop_pct=None if stop is None else stop.stop_pct,
        stop_source=None if stop is None else stop.source,
        stop_crash_capped=None if stop is None else stop.crash_capped,
        qualification=QualificationFacts(72.0, 72.0, 70.0, ('rule_11',), 'allow'),
        sources=tuple(cited))
    await f['commands'].publish_decision_facts(facts, expected_version=f['runtime'].owner.version)
    return req


async def published(f):
    """당일 판단 사실 3건 + 출처 4행. 넷째 출처는 아무 facts 도 인용하지 않는다."""
    rows = [await source(f, name) for name in
            ('trade_memory', 'panel_outlook:005930', 'panel_outlook:000660', 'vix_regime')]
    requests = [await decided(f, 'A', rows[0:2]), await decided(f, 'B', rows[0:1]),
                await decided(f, 'C', [rows[2]])]
    assert list(f['runtime'].owner.state['entry_decision_facts']) == ['I-A', 'I-B', 'I-C']
    return requests


def test_rollover_clears_only_the_prior_day_decision_facts(tmp_path, monkeypatch):
    """전환 뒤 facts 는 비고, 출처 4행은 바이트 단위로 그대로다."""
    async def scenario():
        f = await command_fixture(tmp_path, monkeypatch, origin='automatic')
        try:
            await published(f)
            state = f['runtime'].owner.state
            sources = encode_state({'rows': deepcopy(state['qualification_sources'])})
            frozen = {key: encode_state({'rows': deepcopy(state[key])})
                      for key in state if key not in ROLLOVER_OWNED}
            assert set(UNTOUCHED) <= set(frozen)
            await rollover(f)
            state = f['runtime'].owner.state
            assert state['entry_decision_facts'] == {}
            assert encode_state({'rows': state['qualification_sources']}) == sources
            assert set(state) - ROLLOVER_OWNED == set(frozen)   # 뿌리를 새로 만들지도 없애지도 않는다
            assert {key: encode_state({'rows': state[key]}) for key in frozen} == frozen
            # 사실 15: 지운 판단은 checkpoint 어디에도 흔적이 없다.
            assert 'I-A' not in encode_state(state)
        finally:
            await f['store'].close()
    asyncio.run(scenario())


def test_rollover_hands_the_money_roots_to_their_own_writers(tmp_path, monkeypatch):
    """facts 정리가 reset_daily 의 기존 reset 의미를 바꾸지 않는다."""
    async def scenario():
        f = await command_fixture(tmp_path, monkeypatch, origin='automatic')
        try:
            await published(f)
            positions = deepcopy(f['runtime'].owner.state['portfolio']['positions'])
            await rollover(f)
            state = f['runtime'].owner.state
            assert state['portfolio']['positions'] == positions
            assert state['portfolio']['daily_pnl'] == '0' and state['portfolio']['daily_trades'] == 0
            assert state['risk']['day'] == state['risk']['daily_exit_count_date'] == TO_DAY
            assert state['risk']['daily_stats']['date'] == TO_DAY
            assert state['risk']['exited_today'] == {} and state['risk']['stop_loss_today'] == []
        finally:
            await f['store'].close()
    asyncio.run(scenario())


def test_rollover_shrinks_the_checkpoint_it_carries(tmp_path, monkeypatch):
    """rollover_day 사이 구간의 순증감 — 전환 자체가 더하는 행보다 지우는 facts 가 크다."""
    async def scenario():
        f = await command_fixture(tmp_path, monkeypatch, origin='automatic')
        try:
            await published(f)
            before, after = await rollover(f, measure=True)
            assert after < before
        finally:
            await f['store'].close()
    asyncio.run(scenario())


def test_source_counter_continues_after_the_rollover(tmp_path, monkeypatch):
    """출처는 이름으로 키잉된 유계 행이다 — 전환은 counter 를 되돌리지 않는다."""
    async def scenario():
        f = await command_fixture(tmp_path, monkeypatch, origin='automatic')
        try:
            await published(f)
            assert f['commands'].read_qualification_source('trade_memory')['version'] == 1
            await rollover(f)
            again = await source(f, 'trade_memory', digest='next-')
            assert again.version == 2
            assert f['commands'].read_qualification_source('trade_memory')['as_of'] \
                == f['clock'][0].isoformat()
        finally:
            await f['store'].close()
    asyncio.run(scenario())


def test_rollover_without_published_facts_adds_no_key(tmp_path, monkeypatch):
    """키가 없는 state 에 새 키를 만들지 않는다(KeyError 도 아니다)."""
    async def scenario():
        f = await command_fixture(tmp_path, monkeypatch, origin='automatic')
        try:
            assert 'entry_decision_facts' not in f['runtime'].owner.state
            await rollover(f)
            assert 'entry_decision_facts' not in f['runtime'].owner.state
            assert f['runtime'].owner.state['risk']['day'] == TO_DAY
        finally:
            await f['store'].close()
    asyncio.run(scenario())


def test_prior_day_intent_cannot_prepare_on_the_new_day(tmp_path, monkeypatch):
    """전일 판단은 새 날의 같은 intent 를 통과시키지 못한다 — 사유는 '부재'다.

    전환 전에 prepare 된 attempt 는 전환 자체를 `unresolved_submit` 으로 막으므로
    (실측), 사실 부재 사유는 전환 뒤 재 prepare 경로로 고정한다. 예약 단언은
    S3-3 병렬이라 넣지 않는다(S3-5 의 통합 시험 몫).
    """
    async def scenario():
        f = await command_fixture(tmp_path, monkeypatch, origin='automatic')
        try:
            requests = await published(f)
            await rollover(f)
            runtime, commands = f['runtime'], f['commands']
            # 새 날의 정책 맥락을 다시 게시해야 판단 사실 관문까지 간다.
            ctx = replace(f['ctx'], business_day=f['clock'][0].date(), observed_at=f['clock'][0],
                          versions=replace(f['ctx'].versions, execution=runtime.owner.version))
            await commands.publish_policy_context(ctx, expected_version=runtime.owner.version)
            again = f['builder'].prepare_submit(
                Order(symbol=requests[0].symbol, side=OrderSide.BUY, quantity=10, price=D('10000'),
                      order_type=OrderType.LIMIT, strategy=requests[0].strategy),
                intent_id=requests[0].intent_id, attempt_id='A2',
                session=RequestSession(TO_DAY, f['clock'][0], 'regular'), valuation_price=D('10000'))
            await commands.observe_entry_quote(
                again.symbol, again.valuation_price, as_of=f['clock'][0], source='synthetic-market',
                event_id='quote-A2', expected_version=runtime.owner.version)
            with pytest.raises(CommandValidationError, match='decision_facts_required'):
                await commands.prepare(again, f['entry'](again))
            assert 'A2' not in runtime.owner.state['attempts']
            assert f['broker']._session.posts == []
        finally:
            await f['store'].close()
    asyncio.run(scenario())


def test_rollover_clears_facts_on_a_day_that_actually_traded(tmp_path, monkeypatch):
    """운영 전환은 그날 거래한 뒤에 온다 — attempt·intent 가 남은 state 에서도 facts 는 빈다."""
    async def scenario():
        f, req, _ = await traded(tmp_path, monkeypatch, ready=False)
        try:
            assert (await send(f, req)).status is CommandStatus.NOT_SENT
            state = f['runtime'].owner.state
            assert state['attempts'] and state['intents'] and state['entry_decision_facts']
            attempts = encode_state({'rows': deepcopy(state['attempts'])})
            await rollover(f)
            state = f['runtime'].owner.state
            assert state['entry_decision_facts'] == {}
            assert encode_state({'rows': state['attempts']}) == attempts
        finally:
            await f['store'].close()
    asyncio.run(scenario())
