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
# CV 판독 시각(runtime._now)은 decided_at 과 다른 시각이다 — 그 사이에 LLM·섹터 조회 await 가 있다.
OBSERVED = NOW - timedelta(minutes=1)
PANEL_CREATED = '2026-09-13T21:00:00'
# 출처 이름은 소비 범위까지 담는다(F1). 전역 한 행이면 종목·전략별 digest 가 서로를 밀어낸다.
MEMORY_SOURCE = 'trade_memory:sepa_trend:' + SECTOR
PANEL_SOURCE = 'panel_outlook:' + SYM


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


def with_panel(validator, convictions, *, loaded_at):
    """같은 패널 한 장을 심는다 — 종목별 conviction 이 다르면 보너스·digest 도 달라진다.

    `_load_panel_outlook` 은 성공 6시간 lock 이라 `_panel_loaded_at` 을 함께 심으면 파일을
    읽지 않는다(운영 캐시 접근 0). loaded_at 은 freeze 가 돌려준 naive 벽시계 그대로다.
    """
    validator._panel_outlook = SimpleNamespace(created_at=PANEL_CREATED)
    validator._panel_recommended = {symbol: SimpleNamespace(symbol=symbol, conviction=value)
                                    for symbol, value in convictions.items()}
    validator._panel_loaded_at = loaded_at
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
        # on_signal 이 runtime 에서 읽는 것은 판독 시각(_now) 하나다.
        rm.engine._execution_runtime = SimpleNamespace(_now=lambda: OBSERVED)
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
            assert evidence.observed_at == OBSERVED
            assert evidence.sizing_inputs == rm._last_sizing_inputs
            assert evidence.sizing_inputs['position_multiplier'] == atr_position_multiplier(2.5)
            assert (evidence.regime_used, evidence.sector, evidence.strategy) == (
                'bull', SECTOR, 'sepa_trend')

            req = f['request']('A')
            await f['market_quote'](req)
            facts = await publish(f, evidence, aid='A')
            # 기여한 출처만 인용한다 — regime(항상) + memory_adj 가 붙은 trade_memory.
            assert tuple(source.name for source in facts.sources) == ('regime', MEMORY_SOURCE)
            assert facts.sources[0].digest == regime_digest('bull')
            # 출처 as_of 는 CV 판독 시각이다 — 판단 시각(decided_at)으로 덮지 않는다(계약 3).
            assert facts.decided_at == f['clock'][0] and f['clock'][0] != OBSERVED
            assert [source.as_of for source in facts.sources] == [OBSERVED, OBSERVED]
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
            assert source_row(f, MEMORY_SOURCE) is None
            facts = await publish(f, evidence, aid='A')
            row = source_row(f, MEMORY_SOURCE)
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
            published = {name: source_row(f, name) for name in ('regime', MEMORY_SOURCE)}
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


# ── F1: 종목·전략별 digest 는 전역 한 행에 실을 수 없다 ─────────────────

def test_f1_two_symbols_with_different_panel_convictions_do_not_stale_each_other(
        tmp_path, monkeypatch, freeze):
    """같은 패널이라도 digest 는 종목별이다 — 전역 'panel_outlook' 이면 둘째가 첫째를 밀어낸다."""
    async def scenario():
        f = await owner_fixture(tmp_path, monkeypatch)
        try:
            stamp = freeze(11, 0, day=18)
            validator = with_panel(cv(), {SYM: 0.9, '000660': 0.4}, loaded_at=stamp)
            rm = manager(monkeypatch, validator)
            first_req = f['request']('A')
            await f['market_quote'](first_req)
            _, first = await capture(rm, buy(SYM))
            _, second = await capture(rm, buy('000660'))
            # 두 판단의 패널 값이 실제로 다르다(같은 digest 표본이면 이 결함을 못 본다).
            assert first.cv_decision['panel']['conviction'] == 0.9
            assert second.cv_decision['panel']['conviction'] == 0.4
            assert first.cv_decision['panel']['bonus'] != second.cv_decision['panel']['bonus']

            first_facts = await publish(f, first, aid='A')
            second_facts = await publish(f, second, aid='B')
            assert {'I-A', 'I-B'} <= set(f['runtime'].owner.state['entry_decision_facts'])
            # 둘째 게시가 첫 요청의 인용 version 을 흔들지 않는다.
            await f['commands'].prepare(first_req, f['entry'](first_req))
            assert (await f['send'](first_req)).status is CommandStatus.ACKNOWLEDGED
            assert PANEL_SOURCE in {source.name for source in first_facts.sources}
            assert 'panel_outlook:000660' in {source.name for source in second_facts.sources}
            assert source_row(f, PANEL_SOURCE)['version'] == 1
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


def test_f1_two_strategies_with_different_memory_scopes_do_not_stale_each_other(
        tmp_path, monkeypatch, freeze):
    """memory 보정 digest 는 전략·섹터별이다 — 전역 'trade_memory' 면 서로를 stale 로 만든다."""
    async def scenario():
        f = await owner_fixture(tmp_path, monkeypatch)
        try:
            freeze(11, 0, day=18)
            rm = manager(monkeypatch, cv())
            first_req = f['request']('A')
            await f['market_quote'](first_req)
            _, first = await capture(rm, buy(SYM))
            _, second = await capture(rm, buy('000660', strategy=StrategyType.VCP_BREAKOUT,
                                              sector='2차전지'))
            assert (second.strategy, second.sector) == ('vcp_breakout', '2차전지')

            first_facts = await publish(f, first, aid='A')
            second_facts = await publish(f, second, aid='B')
            assert {'I-A', 'I-B'} <= set(f['runtime'].owner.state['entry_decision_facts'])
            await f['commands'].prepare(first_req, f['entry'](first_req))
            assert (await f['send'](first_req)).status is CommandStatus.ACKNOWLEDGED
            assert MEMORY_SOURCE in {source.name for source in first_facts.sources}
            assert 'trade_memory:vcp_breakout:2차전지' in {
                source.name for source in second_facts.sources}
            assert source_row(f, MEMORY_SOURCE)['version'] == 1
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


# ── F4: 전일 게시본은 재사용하지 않는다 (계약 5 의 '당일' 절) ────────────

def test_f4_a_source_published_yesterday_is_republished_instead_of_reused(
        tmp_path, monkeypatch, freeze):
    """전일 게시본을 재사용하면 publish_decision_facts 가 매번 stale 로 거부한다(영구 fail-closed)."""
    async def scenario():
        f = await owner_fixture(tmp_path, monkeypatch)
        try:
            _, evidence = await evidence_for(monkeypatch, freeze)
            await publish(f, evidence, aid='A')
            assert source_row(f, 'regime')['version'] == 1

            # 시계를 다음 날로 옮기면 day admission 이 먼저 막는다 — 게시본 쪽을 전일로 심는다.
            yesterday = (OBSERVED - timedelta(days=1)).isoformat()
            def age(state):
                for row in state['qualification_sources'].values():
                    row['as_of'] = yesterday
                return state
            await f['runtime'].owner.mutate('synthetic-aged-sources', age)

            facts = await publish(f, evidence, aid='B')
            assert [source.version for source in facts.sources] == [2, 2]
            assert source_row(f, 'regime')['as_of'] == OBSERVED.isoformat()
            assert source_row(f, MEMORY_SOURCE)['version'] == 2
            assert 'I-B' in f['runtime'].owner.state['entry_decision_facts']
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


# ── F7: 증거 타입 가드 ───────────────────────────────────────────────────

@pytest.mark.parametrize('kind', ['dict', 'namespace'])
def test_f7_a_look_alike_evidence_object_publishes_nothing(tmp_path, monkeypatch, freeze, kind):
    """필드가 같아도 QualificationEvidence 가 아니면 게시본 0 이다(deepcopy 방어가 통째로 빠진다)."""
    async def scenario():
        f = await owner_fixture(tmp_path, monkeypatch)
        try:
            _, evidence = await evidence_for(monkeypatch, freeze)
            fields = {name: getattr(evidence, name) for name in evidence.__slots__}
            bogus = fields if kind == 'dict' else SimpleNamespace(**fields)
            with pytest.raises(ValueError, match='invalid_qualification_evidence'):
                await publish(f, bogus, aid='A')
            state = f['runtime'].owner.state
            assert state.get('entry_decision_facts', {}) == {}
            assert state.get('qualification_sources', {}) == {}
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


def test_f3_an_intruder_carrying_its_own_token_still_leaves_no_evidence(tmp_path, monkeypatch, freeze):
    """token 은 **동등** 비교다 — 존재 검사로 약화하면 남의 token 을 내 증거로 싣는다."""
    async def scenario():
        freeze(11, 0, day=18)
        holder = {}

        async def complete(prompt, task=None, max_tokens=None):
            # 내 LLM 을 기다리는 사이 같은 symbol/strategy/regime 의 다른 요청이 채널을 덮는다.
            passed, _, _ = holder['cv'].validate(
                symbol=SYM, side='buy', strategy='vcp_breakout', score=90.0,
                metadata={'indicators': dict(FULL_INDICATORS), 'sector': SECTOR},
                market_regime='neutral', request_token='other', count_stats=False)
            assert passed and holder['cv'].last_decision['token'] == 'other'
            return SimpleNamespace(success=True, content='YES 진입 타당', error='')

        holder['cv'] = cv(memory=None, llm=complete)
        rm = manager(monkeypatch, holder['cv'], regime='neutral')
        orders, found = await capture(
            rm, buy(SYM, score=90.0, strategy=StrategyType.VCP_BREAKOUT))
        assert orders, '증거가 없어도 legacy 판정은 그대로다'
        # 침입자의 판정은 내 것과 symbol/strategy/regime 이 같다 — token 만이 구분 수단이다.
        assert holder['cv'].last_decision['token'] == 'other'
        assert holder['cv'].last_decision['symbol'] == SYM
        assert found is None
    asyncio.run(scenario())


def test_f5_regime_is_the_value_cross_validator_received_not_a_later_lookup(
        tmp_path, monkeypatch, freeze):
    """섹터 조회 await 동안 레짐이 바뀌어도 증거는 CV 가 받은 값을 싣는다."""
    async def scenario():
        freeze(11, 0, day=18)
        rm = manager(monkeypatch, cv(), regime='bull')

        async def sector_lookup(symbol):
            rm.engine._market_regime = 'bear'
            return SECTOR

        rm._sector_lookup = sector_lookup
        event = buy(SYM)
        event.metadata.pop('sector')
        orders, found = await capture(rm, event)
        assert orders and found is not None
        assert rm._resolve_market_regime() == 'bear', '레짐이 실제로 바뀌어야 의미가 있다'
        assert found.regime_used == 'bull'
        assert found.cv_decision['regime'] == 'bull'
    asyncio.run(scenario())


def test_f6_a_nested_panel_mutation_during_the_await_never_reaches_the_evidence(
        tmp_path, monkeypatch, freeze):
    """캡처 시점 복사가 얕으면 중첩 panel dict 가 섹터 조회 창 동안 제자리에서 바뀐다."""
    async def scenario():
        stamp = freeze(11, 0, day=18)
        validator = with_panel(cv(), {SYM: 0.9}, loaded_at=stamp)
        rm = manager(monkeypatch, validator)

        async def sector_lookup(symbol):
            # 증거 조립 전, CV 의 살아있는 판정 dict 안쪽이 다음 판단 값으로 덮인다.
            validator.last_decision['panel']['conviction'] = 0.1
            validator.last_decision['panel']['bonus'] = 99
            return SECTOR

        rm._sector_lookup = sector_lookup
        event = buy(SYM)
        event.metadata.pop('sector')
        orders, found = await capture(rm, event)
        assert orders and found is not None
        assert validator.last_decision['panel']['conviction'] == 0.1, '변형이 실제로 일어나야 한다'
        assert found.cv_decision['panel']['conviction'] == 0.9
        assert found.cv_decision['panel']['bonus'] != 99
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
        assert found.cv_decision['adjusted'] == 69.0
        assert found.sizing_inputs['base_pct'] == 0.25
        found.cv_decision['symbol'] = '999999'
        found.sizing_inputs['base_pct'] = 8.8
        again = QualificationEvidence(
            token=found.token, symbol=found.symbol, side=found.side, strategy=found.strategy,
            origin=found.origin, sector=found.sector, cv_decision=found.cv_decision,
            llm_reason=found.llm_reason, sizing_inputs=found.sizing_inputs,
            regime_used=found.regime_used, observed_at=found.observed_at)
        found.cv_decision['symbol'] = '000000'
        assert again.cv_decision['symbol'] == '999999'
        assert again.sizing_inputs['base_pct'] == 8.8
    asyncio.run(scenario())
