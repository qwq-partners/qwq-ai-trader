"""S2-5: on_signal 증거 캡처(부품 B) + 게시 함수(부품 A).

둘을 잇는 호출은 S3 gateway 가 한다 — 여기서는 캡처와 게시를 각각 증명한다.
증거는 합성 dict 가 아니라 **실제** `CrossStrategyValidator` 와 **실제**
`RiskManager._calculate_position_size` 가 만든 값이고, 게시는 실제
`RequestBoundCommands`(합성 startup 허가 fixture) 가 받는다. 실제 송신 경로나 운영 승격
근거가 아니며 제품의 `trading_ready` 는 계속 False 다.

시계는 전부 주입한다. CV 는 `validate()` 안에서 지역 재임포트를 하므로
`datetime.datetime` 과 `cross_validator.datetime` 을 함께 얼리는 S2-1 의 freeze fixture 를
그대로 쓴다(종료 시 `_scrub_frozen` 이 지연 import 누수를 거둔다).

실행: venv/bin/python -m pytest tests/test_execution_qualification_publishers.py -q -p no:cacheprovider
"""
import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.cross_validator import CrossStrategyValidator
from src.core.event import SignalEvent
from src.core.types import (
    OrderSide, Position, Signal, SignalStrength, StrategyType,
)
from src.execution.safety.commands import CommandValidationError
from src.execution.safety.lifecycle import CommandStatus
from src.execution.safety.qualification import QualificationRefused, regime_digest
from src.execution.safety.qualification_publisher import (
    QualificationEvidence, publish_qualification,
)
from src.utils.sizing import atr_position_multiplier

from test_cross_validator_characterization import FULL_INDICATORS, freeze  # noqa: F401
from test_execution_regime_recheck import fixture as owner_fixture
from test_execution_risk_policy import NOW
from test_risk_sizing import PRICE, SYM, _em, _rm
from test_t11_entry_plan import _order_env


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    """CV 의 규칙11/12 로그·패널 파일이 운영 캐시(~/.cache/ai_trader)를 건드리지 않게 한다."""
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


MEMORY_ADJ = -3
SECTOR = '반도체'


def cv(*, memory=MEMORY_ADJ, llm=None):
    """실제 CV 인스턴스. 적대검증기는 끄고(단일 LLM 경로) 나머지 산식은 제품 그대로다."""
    kwargs = {}
    if memory is not None:
        kwargs['trade_memory'] = SimpleNamespace(
            get_score_adjustment=lambda strategy, sector: memory)
    if llm is not None:
        kwargs['llm_manager'] = SimpleNamespace(complete=llm)
    validator = CrossStrategyValidator(market='KR', **kwargs)
    validator._adversarial = None
    return validator


def buy(symbol=SYM, *, score=72.0, strategy=StrategyType.SEPA_TREND, sector=SECTOR):
    """전략이 실제로 싣는 metadata(atr_pct·position_multiplier·indicators·sector)를 가진 매수 신호."""
    metadata = {'atr_pct': 2.5, 'position_multiplier': atr_position_multiplier(2.5),
                'indicators': dict(FULL_INDICATORS), 'sector': sector}
    signal = Signal(symbol=symbol, side=OrderSide.BUY, strength=SignalStrength.NORMAL,
                    strategy=strategy, price=PRICE, score=score, metadata=metadata)
    return SignalEvent.from_signal(signal, source='test')


def manager(monkeypatch, validator, *, regime='bull', attached=True):
    """on_signal 을 실제로 도는 최소 RiskManager(기존 하네스 재사용 + 실제 CV)."""
    rm = _rm(monkeypatch, mode='nominal', em=_em())
    _order_env(monkeypatch, rm)
    rm._cross_validator = validator
    rm._PENDING_TIMEOUT_SECONDS = 600
    rm.engine._market_regime = regime
    if attached:
        # on_signal 은 runtime 의 속성을 읽지 않는다 — 설치 여부만 본다.
        rm.engine._execution_runtime = object()
    return rm


async def capture(rm, event):
    """on_signal 한 번 = 판단 한 건. 캡처는 판정·주문에 관여하지 않는다."""
    orders = await rm.on_signal(event)
    return orders, getattr(rm, '_last_qualification_evidence', None)


async def evidence_for(monkeypatch, freeze_clock, *, symbol=SYM, regime='bull'):
    """실제 CV·실제 사이징이 만든 증거 하나."""
    freeze_clock(11, 0, day=18)
    rm = manager(monkeypatch, cv(), regime=regime)
    orders, found = await capture(rm, buy(symbol))
    assert orders, '증거 캡처는 주문을 막지 않는다'
    assert found is not None
    return rm, found


async def publish(f, evidence, *, aid='A', decided_at=None, **changes):
    return await publish_qualification(
        f['commands'], evidence, intent_id='I-' + aid,
        config_version=f['ctx'].versions.config,
        decided_at=f['clock'][0] if decided_at is None else decided_at, **changes)


def source_row(f, name):
    return f['commands'].read_qualification_source(name)


# ── R29: 실제 증거 → 게시 → prepare 통과 ────────────────────────────────

def test_r29_real_evidence_publishes_facts_that_prepare_accepts(tmp_path, monkeypatch, freeze):
    """합성 dict 가 아니라 실제 CV/사이징 값이 owner 의 판단 사실이 된다."""
    async def scenario():
        f = await owner_fixture(tmp_path, monkeypatch)
        try:
            rm, evidence = await evidence_for(monkeypatch, freeze)
            # 캡처된 증거가 실제 판정·사이징 값 그대로다(스텁 흔적 0).
            assert evidence.cv_decision['original'] == 72.0
            assert evidence.cv_decision['adjusted'] == 69.0
            assert evidence.cv_decision['memory_adj'] == MEMORY_ADJ
            assert evidence.cv_decision['now_hm'] == 1100
            assert evidence.llm_reason == 'not_required'
            assert evidence.sizing_inputs == rm._last_sizing_inputs
            assert evidence.sizing_inputs['position_multiplier'] == atr_position_multiplier(2.5)
            assert (evidence.regime_used, evidence.sector, evidence.strategy) == (
                'bull', SECTOR, 'sepa_trend')

            req = f['request']('A')
            await f['market_quote'](req)
            facts = await publish(f, evidence, aid='A')
            # 기여한 출처만 인용한다 — regime(항상) + memory_adj 가 붙은 trade_memory.
            assert tuple(source.name for source in facts.sources) == ('regime', 'trade_memory')
            assert facts.sources[0].digest == regime_digest('bull')
            assert facts.qualification.applied_rule_ids == ('trade_memory_adjust',)
            assert facts.qualification.llm_verdict == 'not_required'
            assert (facts.base_pct, facts.strategy_allocation_pct) == (0.25, 40.0)
            assert facts.min_position_value == D('200000')
            assert (facts.atr_pct, facts.stop_pct) == (None, None)
            assert facts.expires_at == f['clock'][0].replace(hour=12, minute=30)

            attempt = await f['commands'].prepare(req, f['entry'](req))
            assert attempt['request_binding']['decision_facts_digest'] == facts.digest
            assert (await f['send'](req)).status is CommandStatus.ACKNOWLEDGED
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


# ── R30: 출처 전부 → 판단 사실 (계약 6 의 순서) ──────────────────────────

def test_r30_every_source_is_published_before_the_facts_row(tmp_path, monkeypatch, freeze):
    """순서가 뒤집히면 owner 가 미게시 출처 인용으로 거부한다 — 그 사유를 함께 못 박는다."""
    async def scenario():
        f = await owner_fixture(tmp_path, monkeypatch)
        try:
            _, evidence = await evidence_for(monkeypatch, freeze)
            assert source_row(f, 'trade_memory') is None
            facts = await publish(f, evidence, aid='A')
            row = source_row(f, 'trade_memory')
            assert row is not None and row['version'] == 1
            assert row['digest'] == facts.sources[1].digest
            assert 'I-A' in f['runtime'].owner.state['entry_decision_facts']

            # 대조: 같은 사실을 출처 없이 먼저 굳히려 하면 정확히 이 사유로 막힌다.
            unpublished = replace(facts, intent_id='I-B', sources=(
                facts.sources[0], replace(facts.sources[1], name='panel_outlook')))
            with pytest.raises(CommandValidationError, match='stale_qualification_source'):
                await f['commands'].publish_decision_facts(
                    unpublished, expected_version=f['runtime'].owner.version)
            assert 'I-B' not in f['runtime'].owner.state['entry_decision_facts']
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


# ── R31: 같은 digest 는 재게시하지 않는다 (계약 5) ───────────────────────

def test_r31_same_digest_is_reused_and_keeps_the_earlier_request_sendable(tmp_path, monkeypatch, freeze):
    """재게시는 앞선 in-flight 판단의 version 을 헛되이 stale 로 만든다."""
    async def scenario():
        f = await owner_fixture(tmp_path, monkeypatch)
        try:
            freeze(11, 0, day=18)
            rm = manager(monkeypatch, cv())
            first_req = f['request']('A')
            await f['market_quote'](first_req)
            _, first = await capture(rm, buy(SYM))
            await publish(f, first, aid='A')
            published = {name: source_row(f, name) for name in ('regime', 'trade_memory')}
            assert [row['version'] for row in published.values()] == [1, 1]

            # 같은 레짐·같은 전략/섹터/보정의 다음 종목 — digest 가 같으니 재게시하지 않는다.
            second_req = f['request']('B', symbol='000660')
            await f['market_quote'](second_req)
            _, second = await capture(rm, buy('000660'))
            assert second.cv_decision['symbol'] == '000660'
            await publish(f, second, aid='B')
            assert {name: source_row(f, name) for name in published} == published

            # 앞 요청이 인용한 version 1 이 그대로라 송신까지 간다.
            await f['commands'].prepare(first_req, f['entry'](first_req))
            assert (await f['send'](first_req)).status is CommandStatus.ACKNOWLEDGED
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


# ── R32: runtime 미설치면 캡처 분기가 돌지 않는다 ────────────────────────

def test_r32_detached_runtime_makes_the_same_order_without_any_evidence(tmp_path, monkeypatch, freeze):
    """legacy 경로의 주문은 그대로이고 증거는 만들지 않는다."""
    async def scenario():
        freeze(11, 0, day=18)
        attached = manager(monkeypatch, cv())
        detached = manager(monkeypatch, cv(), attached=False)
        assert getattr(detached.engine, '_execution_runtime', None) is None
        bound_orders, bound = await capture(attached, buy(SYM))
        legacy_orders, legacy = await capture(detached, buy(SYM))
        assert bound is not None and legacy is None
        assert [(order.order.symbol, order.order.quantity, order.order.strategy)
                for order in legacy_orders] == [(order.order.symbol, order.order.quantity,
                                                 order.order.strategy) for order in bound_orders]
        assert detached._last_sizing_inputs == attached._last_sizing_inputs
    asyncio.run(scenario())


# ── R33·R33b: 증거 채널의 읽는 순서 (계약 13) ────────────────────────────

def test_r33_shadow_validate_during_my_llm_await_leaves_no_evidence(tmp_path, monkeypatch, freeze):
    """내 LLM 을 기다리는 사이 다른 판단이 채널을 덮으면 증거가 없다(token 대조)."""
    async def scenario():
        freeze(11, 0, day=18)
        holder = {}

        async def complete(prompt, task=None, max_tokens=None):
            # 팀심의 shadow 가 같은 인스턴스로 끼어든다(token 없음·통계 미집계).
            passed, _, _ = holder['cv'].validate(
                symbol='000660', side='buy', strategy='team', score=70.0,
                metadata={'indicators': dict(FULL_INDICATORS), 'sector': SECTOR},
                market_regime='neutral', count_stats=False)
            assert passed and holder['cv'].last_decision['symbol'] == '000660'
            return SimpleNamespace(success=True, content='YES 진입 타당', error='')

        holder['cv'] = cv(memory=None, llm=complete)
        rm = manager(monkeypatch, holder['cv'], regime='neutral')
        # 비강세장 + 85~95 점수라야 내 판단이 실제로 LLM 을 기다린다(VCP 는 레짐 게이트 밖).
        orders, found = await capture(
            rm, buy(SYM, score=90.0, strategy=StrategyType.VCP_BREAKOUT))
        assert orders, '증거가 없어도 legacy 판정은 그대로다'
        assert holder['cv'].last_llm_reason == 'approved'
        assert holder['cv'].last_decision['token'] is None
        assert found is None
    asyncio.run(scenario())


def test_r33b_a_delayed_other_llm_cannot_relabel_my_reason(tmp_path, monkeypatch, freeze):
    """'B validate → 지연된 A 의 _llm()' 은 token B + A 의 사유를 만든다 — 함께 복사해야 한다."""
    async def scenario():
        freeze(11, 0, day=18)
        gate = asyncio.Event()

        async def slow(prompt, task=None, max_tokens=None):
            await gate.wait()
            return SimpleNamespace(success=True, content='NO 과열 구간', error='')

        shared = cv(memory=None, llm=slow)
        # A: 다른 요청이 같은 인스턴스에서 LLM 응답을 기다리는 중이다.
        shared.validate(symbol='000660', side='buy', strategy='sepa_trend', score=90.0,
                        metadata={'indicators': dict(FULL_INDICATORS), 'sector': SECTOR},
                        market_regime='bull', request_token='A')
        pending = asyncio.create_task(shared.llm_second_check(
            symbol='000660', strategy='sepa_trend', score=90.0, indicators={},
            market_regime='neutral', sector=SECTOR))
        await asyncio.sleep(0)

        rm = manager(monkeypatch, shared, regime='bull')

        async def sector_lookup(symbol):
            # B 의 sector 조회 await 동안 A 의 _llm() 이 끝나 사유만 덮어쓴다.
            gate.set()
            for _ in range(3):
                await asyncio.sleep(0)
            return SECTOR

        rm._sector_lookup = sector_lookup
        event = buy(SYM, score=72.0)
        event.metadata.pop('sector')
        orders, found = await capture(rm, event)
        assert await pending is False
        assert shared.last_llm_reason == 'rejected_soft', 'A 가 실제로 채널을 덮어야 의미가 있다'
        assert orders and found is not None
        assert found.cv_decision['symbol'] == SYM
        assert found.llm_reason == 'not_required'
        assert found.sector == SECTOR
    asyncio.run(scenario())


# ── R34: 사이징 입력은 이번 요청의 성공한 사이징에서만 (계약 14) ─────────

def test_r34_sizing_inputs_belong_to_this_request_only(tmp_path, monkeypatch, freeze):
    """조기 return 은 증거 0, 다음 성공은 앞 요청 값을 물려받지 않는다."""
    async def scenario():
        freeze(11, 0, day=18)
        rm = manager(monkeypatch, cv())
        _, first = await capture(rm, buy(SYM))
        assert first is not None and first.sizing_inputs['base_pct'] == 0.25

        # 비활성 전략(0%)은 사이징이 0 을 돌려주고 주문도 만들어지지 않는다.
        orders, blocked = await capture(
            rm, buy('000660', strategy=StrategyType.MOMENTUM_BREAKOUT))
        assert orders is None and blocked is None

        # 다음 성공 판단은 자기 사이징 값을 싣는다(VCP 15%).
        _, third = await capture(rm, buy('035420', strategy=StrategyType.VCP_BREAKOUT))
        assert third is not None
        assert third.sizing_inputs['base_pct'] == 0.15
        assert third.sizing_inputs == rm._last_sizing_inputs
        assert third.strategy == 'vcp_breakout'
    asyncio.run(scenario())


# ── R35: 매도는 증거를 만들지 않는다 ─────────────────────────────────────

def test_r35_sell_signal_leaves_no_evidence(tmp_path, monkeypatch, freeze):
    async def scenario():
        freeze(11, 0, day=18)
        rm = manager(monkeypatch, cv())
        rm.engine.portfolio.positions = {SYM: Position(
            symbol=SYM, quantity=10, avg_price=PRICE, current_price=PRICE,
            strategy='sepa_trend')}

        async def sell_price(symbol, price):
            return None

        rm._get_sell_price = sell_price
        signal = Signal(symbol=SYM, side=OrderSide.SELL, strength=SignalStrength.NORMAL,
                        strategy=StrategyType.SEPA_TREND, price=PRICE, score=0.0,
                        metadata={})
        orders, found = await capture(rm, SignalEvent.from_signal(signal, source='test'))
        assert orders and orders[0].order.side is OrderSide.SELL
        assert found is None
    asyncio.run(scenario())


# ── R36: owner 재유도 digest 와 builder digest 는 같은 값 ────────────────

@pytest.mark.parametrize('regime', ['bull', 'sideways', 'neutral', 'trending_bull'])
def test_r36_regime_digest_matches_the_owner_rederivation(regime):
    """commands 의 final 재유도와 builder 가 다른 값을 내면 정상 주문이 조용히 거부된다."""
    from src.execution.safety.commands import _digest, canonical
    assert _digest(canonical({'effective_regime': regime})) == regime_digest(regime)


# ── R37: 거부는 삼키지 않는다 (계약 15) ──────────────────────────────────

@pytest.mark.parametrize('change, reason', [
    ('hybrid', 'unsupported_hybrid_sizing'),
    ('zero_multiplier', 'non_positive_multiplier'),
    ('sepa_deadline', 'sepa_entry_deadline_passed'),
])
def test_r37_refused_qualification_publishes_no_facts(tmp_path, monkeypatch, freeze, change, reason):
    async def scenario():
        f = await owner_fixture(tmp_path, monkeypatch)
        try:
            _, evidence = await evidence_for(monkeypatch, freeze)
            decided_at = f['clock'][0]
            if change == 'hybrid':
                evidence = replace(evidence, sizing_inputs={
                    **evidence.sizing_inputs, 'hybrid_enabled': True})
            elif change == 'zero_multiplier':
                evidence = replace(evidence, sizing_inputs={
                    **evidence.sizing_inputs, 'conviction_multiplier': 0.0})
            else:
                decided_at = decided_at + timedelta(hours=3, minutes=31)
                f['clock'][0] = decided_at
                evidence = replace(evidence, cv_decision={
                    **evidence.cv_decision, 'now_hm': 1431})
            with pytest.raises(QualificationRefused, match=reason):
                await publish(f, evidence, aid='A', decided_at=decided_at)
            assert f['runtime'].owner.state.get('entry_decision_facts', {}) == {}
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


# ── 증거 dict 는 호출자의 이후 변형과 끊는다 ─────────────────────────────

def test_evidence_detaches_the_caller_dicts(tmp_path, monkeypatch, freeze):
    """CV 의 last_decision 과 사이징 반출 dict 는 다음 판단이 덮는 살아있는 값이다."""
    async def scenario():
        freeze(11, 0, day=18)
        rm = manager(monkeypatch, cv())
        _, found = await capture(rm, buy(SYM))
        rm._cross_validator.last_decision['adjusted'] = 1.0
        rm._last_sizing_inputs['base_pct'] = 9.9
        found.cv_decision['symbol'] = '999999'
        found.sizing_inputs['base_pct'] = 8.8
        again = QualificationEvidence(
            token=found.token, symbol=found.symbol, side=found.side, strategy=found.strategy,
            origin=found.origin, sector=found.sector, cv_decision=found.cv_decision,
            llm_reason=found.llm_reason, sizing_inputs=found.sizing_inputs,
            regime_used=found.regime_used)
        found.cv_decision['symbol'] = '000000'
        assert again.cv_decision['symbol'] == '999999'
        assert again.sizing_inputs['base_pct'] == 8.8
    asyncio.run(scenario())
