"""S10A3a-1: 로드된 설정에서 owner 정책을 만드는 부품(`safety/factory.py`)의 인수.

제품 호출자는 0건이다 — 설치 단계(10A3b)에서만 쓰인다. 게시 시험은 실제 SQLite
store·실제 runtime·실제 `RequestBoundCommands` 위에서 돌고 외부 I/O·벽시계 조회가
없다(now 는 전부 주입). 여기서 통과하는 것은 부품의 유도 규칙일 뿐 설치 승인이 아니다.
"""
import asyncio
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from src.core.market_regime import MarketRegimeAdapter
from src.core.types import HybridConfig, OrderSide, RiskConfig, TradingConfig
from src.execution.safety import risk_policy as p
from src.execution.safety.commands import CommandValidationError, RequestBoundCommands
from src.execution.safety.guards import EntryAuthority, FinalEntryGuard, GuardDecision, RiskSnapshot
from src.execution.safety.policy_snapshot import PolicyContext
from src.execution.safety.qualification import QualificationRefused, config_version
from src.execution.safety.requests import KISRequestBuilder, RequestAccount
from src.risk.manager import RiskManager
from src.strategies.exit_manager import ExitConfig
from src.utils.fee_calculator import FeeConfig
from src.utils.stop_policy import StopDecision
import src.utils.macro_calendar as macro_calendar

from src.execution.safety.factory import (
    effective_risk_policy, execution_config_version, publish_entry_policy_context)

from test_execution_runtime import NOW, setup

ENDPOINT = 'https://openapi.koreainvestment.com:9443'


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


def operating_risk(**changes):
    """운영값에 가깝게 세운 인스턴스. 유도 축 대부분이 `RiskConfig()` 기본값과 다르다."""
    value = RiskConfig(
        daily_max_loss_pct=5.0, daily_max_trades=10, max_daily_new_buys=4,
        base_position_pct=25.0, max_position_pct=28.0, max_positions=8,
        min_cash_reserve_pct=7.5, min_position_value=200000.0,
        max_positions_per_sector=2, daily_exit_cooldown_threshold=5,
        max_core_positions=4, sizing_mode='risk', risk_per_trade_pct=0.7,
        risk_max_position_pct=18.0, hybrid=HybridConfig(enabled=True),
        strategy_allocation={'core_holding': 12.5, 'sepa_trend': 40.0, 'gap_and_go': 15.0})
    return replace(value, **changes) if changes else value


def validator_block(**changes):
    """`config/default.yml` 의 중첩 `kr.validator` 블록 중 CV/G4 가 읽는 값."""
    value = {'min_pass_score': 50, 'missing_indicator_penalty_step': 2,
             'missing_indicator_penalty_cap': 8, 'rule_penalties': {'early_session': 8,
             'sepa_chase': 10, 'rsi_overbought': 5, 'supply_dual_sell': 10,
             'surge_chase': 15, 'deficit_high_pbr': 10},
             'llm_daily_max': 10, 'llm_check_score_min': 85, 'llm_bypass_score': 95,
             'llm_reject_size_mult': 0.5,
             # 축 밖의 값 — digest 를 흔들면 안 된다.
             'replacement_min_score': 85, 'replacement_cooldown_sec': 600}
    value.update(changes)
    return value


def position_table(**changes):
    """`engine.py` 의 전략별 base_pct 표를 호출자가 str 키로 정규화한 형태."""
    value = {'sepa_trend': 25.0, 'vcp_breakout': 15.0, 'gap_and_go': 15.0,
             'core_holding': 10.0, 'momentum_breakout': 0.0}
    value.update(changes)
    return value


def stop_table(**changes):
    """`run_trader.py` 의 전략별 청산 파라미터(위험 사이징 분모인 고정 SL)."""
    value = {'sepa_trend': {'stop_loss_pct': 5.0, 'trailing_stop_pct': 3.0},
             'gap_and_go': {'stop_loss_pct': 3.5, 'trailing_stop_pct': 1.5},
             'vcp_breakout': {'stop_loss_pct': 4.0, 'trailing_stop_pct': 2.5}}
    value.update(changes)
    return value


def version_inputs(**changes):
    value = dict(validator_config=validator_block(), risk=operating_risk(),
                 position_pct=position_table(), stop_params=stop_table(),
                 exit_config=ExitConfig(), experts_shadow_mode=True)
    value.update(changes)
    return value


def policy_of(risk=None, *, regime='neutral', fee_config=None):
    return effective_risk_policy(risk if risk is not None else operating_risk(),
                                 regime=regime,
                                 fee_config=fee_config if fee_config is not None else FeeConfig())


# ---------------------------------------------------------------- 정책 부품

def test_policy_fields_come_from_the_supplied_instance_not_dataclass_defaults():
    risk = operating_risk()
    policy = policy_of(risk)
    assert type(policy) is p.EffectiveRiskPolicy
    assert (policy.daily_max_loss_pct, policy.daily_max_trades, policy.max_daily_new_buys) == (5.0, 10, 4)
    assert (policy.max_positions, policy.max_position_pct, policy.min_cash_reserve_pct) == (8, 28.0, 7.5)
    assert (policy.max_positions_per_sector, policy.sizing_mode) == (2, 'risk')
    assert (policy.risk_per_trade_pct, policy.risk_max_position_pct) == (0.7, 18.0)
    # 기본값으로 만든 정책과 달라야 한다(인자를 무시하고 RiskConfig() 를 새로 만드는 변이를 죽인다).
    # 레짐/수수료는 RiskConfig 에서 오지 않고, 위험 사이징 두 축은 운영값이 기본값과 같다.
    unchanged = {'regime_min_cash_reserve_pct', 'buy_commission_rate',
                 'risk_per_trade_pct', 'risk_max_position_pct'}
    default = policy_of(RiskConfig())
    for field in fields(p.EffectiveRiskPolicy):
        if field.name in unchanged:
            assert getattr(policy, field.name) == getattr(default, field.name), field.name
        else:
            assert getattr(policy, field.name) != getattr(default, field.name), field.name


def test_loader_unread_axes_and_hybrid_keep_the_instance_value():
    """`_build_risk_config` 가 읽지 않는 두 축도 dataclass 기본값으로 되돌리지 않는다."""
    risk = operating_risk()
    assert (risk.max_core_positions, risk.daily_exit_cooldown_threshold) != (
        RiskConfig().max_core_positions, RiskConfig().daily_exit_cooldown_threshold)
    policy = policy_of(risk)
    assert policy.max_core_positions == 4
    assert policy.daily_exit_cooldown_threshold == 5
    # hybrid 출처는 하나다 — 인스턴스의 중첩 설정.
    assert policy.hybrid_enabled is True
    assert policy_of(operating_risk(hybrid=HybridConfig(enabled=False))).hybrid_enabled is False


def test_fee_rate_comes_from_fee_config_while_two_product_rates_coexist():
    """정본은 `FeeConfig` 다. `TradingConfig.buy_fee_rate` 와의 공존을 단언으로 고정한다."""
    assert type(FeeConfig().buy_commission_rate) is D
    assert type(TradingConfig().buy_fee_rate) is float
    assert D(str(TradingConfig().buy_fee_rate)) == FeeConfig().buy_commission_rate
    assert policy_of().buy_commission_rate == FeeConfig().buy_commission_rate
    # TradingConfig 에서 읽어오면 아래 명시 인자가 무시된다.
    other = FeeConfig(buy_commission_rate=D('0.000111'))
    assert policy_of(fee_config=other).buy_commission_rate == D('0.000111')


def test_core_allocation_requires_the_explicit_key():
    assert policy_of().core_allocation_pct == 12.5
    without = operating_risk(strategy_allocation={'sepa_trend': 40.0})
    with pytest.raises(ValueError):
        policy_of(without)
    with pytest.raises(ValueError):
        policy_of(operating_risk(strategy_allocation={'core_holding': None}))


def test_regime_minimum_cash_comes_from_the_regime_table():
    for regime, expected in (('bull', 5.0), ('bear', 15.0), ('sideways', 5.0), ('neutral', 5.0)):
        assert MarketRegimeAdapter.REGIME_PARAMS[regime]['min_cash_reserve_pct'] == expected
        policy = policy_of(regime=regime)
        assert policy.regime_min_cash_reserve_pct == expected
        # 설정 최소 현금(7.5%)을 복사하면 어느 레짐에서도 이 값이 나오지 않는다.
        assert policy.min_cash_reserve_pct == 7.5
    with pytest.raises(ValueError):
        policy_of(regime='unknown_regime')


def test_policy_refuses_inputs_that_are_not_the_explicit_instances():
    with pytest.raises(ValueError):
        effective_risk_policy({'daily_max_loss_pct': 5.0}, regime='bull', fee_config=FeeConfig())
    with pytest.raises(ValueError):
        effective_risk_policy(operating_risk(), regime='bull', fee_config=TradingConfig())


# ---------------------------------------------------------------- config_version

def test_config_version_wraps_the_product_hash_without_a_new_scheme():
    digest = execution_config_version(**version_inputs())
    assert type(digest) is str and len(digest) == 64
    assert digest == execution_config_version(**version_inputs())


@pytest.mark.parametrize('axis,changes', [
    ('validator', dict(validator_config=validator_block(min_pass_score=55))),
    ('llm', dict(validator_config=validator_block(llm_daily_max=11))),
    ('sizing', dict(position_pct=position_table(sepa_trend=24.0))),
    ('stops', dict(stop_params=stop_table(sepa_trend={'stop_loss_pct': 4.5, 'trailing_stop_pct': 3.0}))),
    ('experts', dict(experts_shadow_mode=False)),
])
def test_config_version_moves_when_one_axis_moves(axis, changes):
    assert execution_config_version(**version_inputs(**changes)) != execution_config_version(**version_inputs())


@pytest.mark.parametrize('axis,changes', [
    ('sizing.base_pct', dict(risk=operating_risk(base_position_pct=20.0))),
    ('sizing.sizing_mode', dict(risk=operating_risk(sizing_mode='nominal'))),
    ('sizing.allocation', dict(risk=operating_risk(
        strategy_allocation={'core_holding': 30.0, 'sepa_trend': 40.0, 'gap_and_go': 15.0}))),
    ('sizing.hybrid', dict(risk=operating_risk(hybrid=HybridConfig(enabled=False)))),
    ('stops.global', dict(exit_config=ExitConfig(stop_loss_pct=6.0))),
])
def test_config_version_moves_with_the_risk_and_exit_axes(axis, changes):
    assert execution_config_version(**version_inputs(**changes)) != execution_config_version(**version_inputs())


@pytest.mark.parametrize('outside,changes', [
    ('risk.daily_max_loss_pct', dict(risk=operating_risk(daily_max_loss_pct=4.0))),
    ('risk.max_positions', dict(risk=operating_risk(max_positions=6))),
    ('validator.replacement', dict(validator_config=validator_block(replacement_min_score=90))),
    ('exit.first_exit_pct', dict(exit_config=ExitConfig(first_exit_pct=12.0))),
])
def test_config_version_ignores_values_outside_the_five_axes(outside, changes):
    assert execution_config_version(**version_inputs(**changes)) == execution_config_version(**version_inputs())


@pytest.mark.parametrize('changes', [
    dict(position_pct={'sepa_trend': D('25')}),
    dict(stop_params={'sepa_trend': {'as_of': NOW}}),
    dict(validator_config=validator_block(min_pass_score=OrderSide.BUY)),
    dict(validator_config=validator_block(llm_reject_size_mult=float('nan'))),
])
def test_config_version_refuses_unnormalized_inputs(changes):
    with pytest.raises(QualificationRefused) as raised:
        execution_config_version(**version_inputs(**changes))
    assert raised.value.reason == 'invalid_config_version_input'


def test_config_version_refuses_a_validator_block_missing_a_read_key():
    block = validator_block()
    del block['llm_bypass_score']
    with pytest.raises(ValueError):
        execution_config_version(**version_inputs(validator_config=block))


def test_config_version_is_the_product_hash_of_the_five_axes():
    """새 해시 체계를 만들지 않는다 — 제품 `qualification.config_version` 그대로다."""
    from src.strategies.exit_manager import INTRADAY_CRASH_PARAMS
    inputs = version_inputs()
    risk, block, exit_config = inputs['risk'], inputs['validator_config'], inputs['exit_config']
    expected = config_version(
        validator={key: block[key] for key in ('min_pass_score', 'missing_indicator_penalty_step',
                                               'missing_indicator_penalty_cap', 'rule_penalties')},
        llm={key: block[key] for key in ('llm_daily_max', 'llm_check_score_min',
                                         'llm_bypass_score', 'llm_reject_size_mult')},
        sizing={'strategy_position_pct': inputs['position_pct'],
                'base_position_pct': risk.base_position_pct,
                'max_position_pct': risk.max_position_pct,
                'sizing_mode': risk.sizing_mode,
                'risk_per_trade_pct': risk.risk_per_trade_pct,
                'risk_max_position_pct': risk.risk_max_position_pct,
                'min_position_value': risk.min_position_value,
                'strategy_allocation': risk.strategy_allocation,
                'hybrid_enabled': risk.hybrid.enabled},
        stops={'strategy_exit_params': inputs['stop_params'],
               'stop_loss_pct': exit_config.stop_loss_pct,
               'min_stop_pct': exit_config.min_stop_pct,
               'max_stop_pct': exit_config.max_stop_pct,
               'intraday_crash_params': INTRADAY_CRASH_PARAMS},
        experts={'shadow_mode': inputs['experts_shadow_mode']})
    assert execution_config_version(**inputs) == expected


# ---------------------------------------------------------------- publisher

async def harness(tmp_path, *, sidecar, clock):
    engine, exits, store, runtime = await setup(tmp_path, account_scope='factory-scope',
                                                risk_manager=sidecar, clock=lambda: clock[0])
    authority = EntryAuthority()
    builder = KISRequestBuilder(RequestAccount('factory-scope', '12345678', '01', 'prod', ENDPOINT, 1))
    commands = RequestBoundCommands(runtime, builder=builder, authority=authority,
        entry_guard=FinalEntryGuard(authority, lambda: RiskSnapshot(1, 1, 'success', clock[0], 'normal'),
                                    lambda: clock[0]),
        stop_resolver=lambda strategy: StopDecision(D('5'), 'strategy', False),
        session_guard=lambda request: GuardDecision(True, 'synthetic_open'))
    return store, runtime, commands


def published(runtime):
    return PolicyContext.from_dict(runtime.owner.state['entry_policy_context'])


def test_publisher_derives_every_axis_from_the_objects_at_publication_time(tmp_path, monkeypatch):
    async def scenario():
        clock = [NOW]
        sidecar = RiskManager(RiskConfig(), D('2000000'))
        sidecar._market_trend = {'recovering': True, 'avg_pct': 0.4}
        sidecar._sidecar_active = True
        sidecar._sync_healthy = False
        sidecar._sync_fail_count = 3
        # 제품의 naive 벽시계(KST 지역시각). qualification 과 같은 규칙으로 aware 화된다.
        sidecar._sync_unhealthy_since = (NOW - timedelta(minutes=4)).replace(tzinfo=None)
        # 제품 기본값(10분)과 다른 값 — 게시본이 인스턴스에서 읽는지 고정한다.
        sidecar._sync_timeout_minutes = 7
        monkeypatch.setattr(macro_calendar, 'is_macro_event_day', lambda day=None: ('FOMC', True))
        store, runtime, commands = await harness(tmp_path, sidecar=sidecar, clock=clock)
        try:
            adapter = MarketRegimeAdapter()
            adapter._current_regime = 'bear'
            risk = operating_risk()
            inputs = version_inputs(risk=risk)
            before = runtime.owner.version
            digest = await publish_entry_policy_context(
                commands, risk=risk, sidecar=sidecar, regime_adapter=adapter,
                fee_config=FeeConfig(), now=NOW,
                **{k: v for k, v in inputs.items() if k != 'risk'})
            assert digest == execution_config_version(**inputs)
            context = published(runtime)
            assert context.business_day == NOW.date() and context.observed_at == NOW
            assert context.versions.config == digest
            assert (context.versions.execution, context.versions.quote,
                    context.versions.risk, context.versions.protection) == (before,) * 4
            # regime/macro counter 는 제품에 게시 주체가 0 건이라 0 으로 둔다(모듈 docstring 의
            # 결정). owner 는 regime 을 `regime_policy` 에서 다시 유도하고 macro 는 아무도 읽지
            # 않으므로, 여기서 owner version 을 실어 보내면 없는 생산자를 있는 것처럼 꾸민다.
            assert context.versions.regime == 0 and context.versions.macro == 0
            # 정책 전 필드가 게시 시점 객체에서 다시 유도한 값과 전건 일치한다.
            expected = effective_risk_policy(risk, regime='bear', fee_config=FeeConfig())
            for field in fields(p.EffectiveRiskPolicy):
                assert getattr(context.policy, field.name) == getattr(expected, field.name), field.name
            assert context.sync == p.SyncPolicySnapshot(False, 3, NOW - timedelta(minutes=4), 7)
            assert context.trend == p.MarketTrendPolicySnapshot(True, True, True)
            assert context.macro == p.MacroPolicySnapshot('FOMC', True, False)
            # 왕복 정규화: 게시본은 canonical DTO 다.
            assert PolicyContext.from_dict(context.to_dict()) == context
        finally:
            await store.close()
    asyncio.run(scenario())


def test_publisher_rederives_instead_of_reusing_the_previous_policy(tmp_path, monkeypatch):
    async def scenario():
        clock = [NOW]
        sidecar = RiskManager(RiskConfig(), D('2000000'))
        monkeypatch.setattr(macro_calendar, 'is_macro_event_day', lambda day=None: (None, False))
        store, runtime, commands = await harness(tmp_path, sidecar=sidecar, clock=clock)
        try:
            adapter = MarketRegimeAdapter()
            adapter._current_regime = 'bull'
            first = version_inputs(risk=operating_risk())
            await publish_entry_policy_context(commands, sidecar=sidecar, regime_adapter=adapter,
                fee_config=FeeConfig(), now=NOW, **first)
            assert published(runtime).policy.max_positions == 8
            assert published(runtime).policy.regime_min_cash_reserve_pct == 5.0
            # 갱신 전 sidecar 의 추세 dict 는 비어 있다 — legacy `_is_daily_loss_limit_hit` 의
            # truthy 판정대로 '추세 없음'(present=False)이지 '추세 있음'이 아니다.
            assert sidecar._market_trend == {} and sidecar._sidecar_active is False
            assert published(runtime).trend == p.MarketTrendPolicySnapshot(False, False, False)

            adapter._current_regime = 'bear'
            second = version_inputs(risk=operating_risk(max_positions=3, max_position_pct=12.0))
            digest = await publish_entry_policy_context(commands, sidecar=sidecar,
                regime_adapter=adapter, fee_config=FeeConfig(), now=NOW, **second)
            context = published(runtime)
            assert context.policy.max_positions == 3
            assert context.policy.max_position_pct == 12.0
            assert context.policy.regime_min_cash_reserve_pct == 15.0
            assert context.versions.config == digest == execution_config_version(**second)
        finally:
            await store.close()
    asyncio.run(scenario())


def test_publisher_reads_the_live_owner_version_and_refuses_a_day_mismatch(tmp_path, monkeypatch):
    async def scenario():
        clock = [NOW]
        sidecar = RiskManager(RiskConfig(), D('2000000'))
        monkeypatch.setattr(macro_calendar, 'is_macro_event_day', lambda day=None: (None, False))
        store, runtime, commands = await harness(tmp_path, sidecar=sidecar, clock=clock)
        try:
            adapter = MarketRegimeAdapter()
            # 고정 expected_version 으로는 통과할 수 없도록 owner 를 먼저 진행시킨다.
            for tag in ('spacer-1', 'spacer-2', 'spacer-3'):
                await runtime.owner.mutate(tag, lambda state: state)
            before = runtime.owner.version
            assert before > 1
            inputs = version_inputs()
            await publish_entry_policy_context(commands, sidecar=sidecar, regime_adapter=adapter,
                fee_config=FeeConfig(), now=NOW, **inputs)
            assert runtime.owner.version == before + 1
            assert published(runtime).versions.execution == before

            with pytest.raises(CommandValidationError) as raised:
                await publish_entry_policy_context(commands, sidecar=sidecar, regime_adapter=adapter,
                    fee_config=FeeConfig(), now=NOW + timedelta(days=1), **inputs)
            assert raised.value.reason == 'policy_context_day_or_time_mismatch'
        finally:
            await store.close()
    asyncio.run(scenario())


def test_publisher_records_a_failed_macro_lookup_instead_of_an_event(tmp_path, monkeypatch):
    async def scenario():
        clock = [NOW]
        sidecar = RiskManager(RiskConfig(), D('2000000'))
        def explode(day=None):
            raise RuntimeError('합성 calendar 실패')
        monkeypatch.setattr(macro_calendar, 'is_macro_event_day', explode)
        store, runtime, commands = await harness(tmp_path, sidecar=sidecar, clock=clock)
        try:
            await publish_entry_policy_context(commands, sidecar=sidecar,
                regime_adapter=MarketRegimeAdapter(), fee_config=FeeConfig(), now=NOW,
                **version_inputs())
            assert published(runtime).macro == p.MacroPolicySnapshot(None, False, True)
        finally:
            await store.close()
    asyncio.run(scenario())


def test_publisher_takes_the_business_day_from_kst_across_the_date_boundary(tmp_path, monkeypatch):
    """UTC-aware now 가 KST 날짜 경계를 넘는 표본 — 영업일은 KST 날짜다.

    UTC 09-17 15:30 = KST 09-18 00:30 이라 두 날짜가 다르다. now 를 KST 로 바꾸지 않으면
    영업일이 09-17 로 떨어져 owner 의 risk day(KST 09-18)와 어긋난다 — 게시 자체가 실패한다.
    """
    async def scenario():
        boundary = datetime(2026, 9, 17, 15, 30, tzinfo=timezone.utc)
        assert boundary.date() != NOW.date()
        assert boundary.astimezone(NOW.tzinfo).date() == NOW.date()
        clock = [boundary]
        sidecar = RiskManager(RiskConfig(), D('2000000'))
        monkeypatch.setattr(macro_calendar, 'is_macro_event_day', lambda day=None: (None, False))
        store, runtime, commands = await harness(tmp_path, sidecar=sidecar, clock=clock)
        try:
            await publish_entry_policy_context(commands, sidecar=sidecar,
                regime_adapter=MarketRegimeAdapter(), fee_config=FeeConfig(), now=boundary,
                **version_inputs())
            context = published(runtime)
            assert context.business_day == NOW.date()
            # 관측 시각은 원 tz 를 보존한다 — 날짜만 KST 로 유도한다.
            assert context.observed_at == boundary
        finally:
            await store.close()
    asyncio.run(scenario())


def test_publisher_refuses_a_naive_now_and_a_foreign_sidecar(tmp_path, monkeypatch):
    async def scenario():
        clock = [NOW]
        sidecar = RiskManager(RiskConfig(), D('2000000'))
        monkeypatch.setattr(macro_calendar, 'is_macro_event_day', lambda day=None: (None, False))
        store, runtime, commands = await harness(tmp_path, sidecar=sidecar, clock=clock)
        try:
            # 사유까지 고정한다 — aware 검사를 지우면 naive now 가 `astimezone` 에서 호스트
            # 지역시각으로 조용히 해석되고 거부는 DTO 의 `invalid_policy_context_time` 으로
            # 늦춰진다(같은 ValueError 라 사유 없이는 구분되지 않는다).
            with pytest.raises(ValueError, match='aware'):
                await publish_entry_policy_context(commands, sidecar=sidecar,
                    regime_adapter=MarketRegimeAdapter(), fee_config=FeeConfig(),
                    now=NOW.replace(tzinfo=None), **version_inputs())
            with pytest.raises(ValueError):
                await publish_entry_policy_context(commands, sidecar=object(),
                    regime_adapter=MarketRegimeAdapter(), fee_config=FeeConfig(),
                    now=NOW, **version_inputs())
            assert 'entry_policy_context' not in runtime.owner.state
        finally:
            await store.close()
    asyncio.run(scenario())


# ---------------------------------------------------------------- 설치 불변

def test_no_product_source_calls_the_factory():
    """10A3a 는 설치가 아니다 — 제품 호출자 0건을 시험으로 고정한다."""
    root = Path(__file__).resolve().parent.parent
    needles = ('safety.factory', 'safety import factory', 'effective_risk_policy',
               'execution_config_version', 'publish_entry_policy_context')
    hits = []
    for folder in ('src', 'scripts'):
        for path in (root / folder).rglob('*.py'):
            if path.name == 'factory.py' and path.parent.name == 'safety':
                continue
            body = path.read_text(encoding='utf-8')
            hits += [f'{path}:{needle}' for needle in needles if needle in body]
    assert hits == []
