"""로드된 설정에서 owner 진입 정책을 만드는 부품 (설치 단계 전용).

제품 호출자는 0건이다 — attach 설치(10A3b)가 정해지기 전에는 아무도 이 모듈을
부르지 않는다. 시계 조회가 없고(now 는 전부 인자), 설정 파일을 직접 읽지 않으며,
정책 게시 외의 owner 상태를 건드리지 않는다.

naive 시각 규칙: sidecar 의 `_sync_unhealthy_since` 는 naive 벽시계다. 진입 판단
사실(`qualification._kst`)과 **같은 규칙**으로 naive = KST 지역시각으로 본다.

게시한 축이 owner 에게 실제로 어떻게 읽히는지(`policy_snapshot.build_owned_snapshot`):

| 게시 축 | owner 가 읽는가 | 비고 |
|---|---|---|
| versions.execution | 예 | `commands._publish_policy_context` 가 owner.version 과 대조한다(`stale_policy_source_version`). snapshot 에서는 읽은 checkpoint version 으로 덮어써진다 |
| versions.quote / risk / protection | 아니오 | snapshot 이 셋 다 checkpoint version 으로 덮어쓴다. 같은 값으로 게시해 둔다 |
| versions.config | 예 | gateway 가 자기 `config_version` 과 대조한다(`gateway.py`) |
| versions.regime | 조건부 | state 에 `regime_policy` 가 있으면 `require_current_trend` 로 덮어써진다. 제품에 이 counter 의 게시 주체가 없어 0 으로 둔다 |
| versions.macro | 아니오 | snapshot 에 그대로 실리지만 어떤 gate 도 읽지 않는다. 생산자가 없어 0 으로 둔다 |
| policy.* | 예 | 진입 gate 전부가 읽는다 |
| policy.regime_min_cash_reserve_pct | 조건부 | `regime_policy` 가 있으면 owner 의 유효 레짐으로 재유도돼 덮어써진다 |
| sync | 예 | 그대로 실린다 |
| trend.present / trend.recovering | 조건부 | `regime_policy` 가 있으면 owned `trend_state` 로 재유도된다 |
| trend.sidecar_active | 아니오 | `entry_policy_effects.sidecar_active` 가 있으면 항상 그 값으로 덮어써진다 |
| macro | 예 | 매크로 이벤트 당일 신규 매수 1건 제한이 읽는다 |
| business_day / observed_at | 예 | 게시 시 당일·비미래 검사 |
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from ...core.market_regime import MarketRegimeAdapter
from ...core.types import RiskConfig
from ...risk.manager import RiskManager
from ...strategies.exit_manager import INTRADAY_CRASH_PARAMS, ExitConfig
from ...utils import macro_calendar
from ...utils.fee_calculator import FeeConfig
from . import risk_policy as p
from .policy_snapshot import PolicyContext
from .qualification import config_version

_KST = ZoneInfo('Asia/Seoul')

# `qualification.config_version` docstring 이 정본으로 지정한 validator/llm 축의 키.
_VALIDATOR_KEYS = ('min_pass_score', 'missing_indicator_penalty_step',
                   'missing_indicator_penalty_cap', 'rule_penalties')
_LLM_KEYS = ('llm_daily_max', 'llm_check_score_min', 'llm_bypass_score', 'llm_reject_size_mult')


def effective_risk_policy(risk: RiskConfig, *, regime: str, fee_config: FeeConfig) -> p.EffectiveRiskPolicy:
    """로드된 `RiskConfig` 인스턴스와 수수료 정본에서 owner 정책을 만든다.

    전 필드를 인자 인스턴스에서 유도한다 — dataclass 기본값으로 되돌리지 않고 YAML dict
    오버로드도 받지 않는다. `core_holding` 배분 키 부재는 0 으로 뭉개지 않고 거부한다.
    매수 수수료율의 정본은 `FeeConfig` 다(`TradingConfig.buy_fee_rate` 와 이원화돼 있다).
    """
    if type(risk) is not RiskConfig:
        raise ValueError('명시 RiskConfig 인스턴스가 필요합니다')
    if type(fee_config) is not FeeConfig:
        raise ValueError('명시 FeeConfig 인스턴스가 필요합니다')
    if type(regime) is not str or regime not in MarketRegimeAdapter.REGIME_PARAMS:
        raise ValueError('알 수 없는 레짐 이름')
    allocation = risk.strategy_allocation
    if type(allocation) is not dict or 'core_holding' not in allocation:
        raise ValueError('core_holding 배분이 명시되지 않았습니다')
    return p.EffectiveRiskPolicy(
        daily_max_loss_pct=risk.daily_max_loss_pct,
        daily_max_trades=risk.daily_max_trades,
        max_daily_new_buys=risk.max_daily_new_buys,
        max_positions=risk.max_positions,
        max_core_positions=risk.max_core_positions,
        max_position_pct=risk.max_position_pct,
        min_cash_reserve_pct=risk.min_cash_reserve_pct,
        max_positions_per_sector=risk.max_positions_per_sector,
        daily_exit_cooldown_threshold=risk.daily_exit_cooldown_threshold,
        regime_min_cash_reserve_pct=MarketRegimeAdapter.REGIME_PARAMS[regime]['min_cash_reserve_pct'],
        core_allocation_pct=allocation['core_holding'],
        sizing_mode=risk.sizing_mode,
        risk_per_trade_pct=risk.risk_per_trade_pct,
        risk_max_position_pct=risk.risk_max_position_pct,
        buy_commission_rate=fee_config.buy_commission_rate,
        hybrid_enabled=risk.hybrid.enabled)


def execution_config_version(*, validator_config: dict, risk: RiskConfig, position_pct: dict,
                             stop_params: dict, exit_config: ExitConfig,
                             experts_shadow_mode) -> str:
    """`qualification.config_version` 의 얇은 어댑터. 새 해시 체계를 만들지 않는다.

    5축의 정본은 그 함수의 docstring 이고 여기서는 그 축들을 로드된 객체에서 모으기만
    한다. `position_pct` 는 `engine.py` 의 전략별 base_pct 표, `stop_params` 는
    `run_trader.py` 의 전략별 청산 파라미터를 호출자가 str 키로 정규화한 것이다.
    Decimal·datetime·enum·NaN 은 `canonical` 이 거부한다 — 정규화는 호출자 몫이고
    여기서 조용히 문자열로 바꾸지 않는다.
    """
    if type(risk) is not RiskConfig or type(exit_config) is not ExitConfig:
        raise ValueError('명시 RiskConfig/ExitConfig 인스턴스가 필요합니다')
    for value in (validator_config, position_pct, stop_params):
        if type(value) is not dict:
            raise ValueError('명시 설정 dict 가 필요합니다')
    missing = [key for key in _VALIDATOR_KEYS + _LLM_KEYS if key not in validator_config]
    if missing:
        raise ValueError(f'validator 설정 키 누락: {missing}')
    return config_version(
        validator={key: validator_config[key] for key in _VALIDATOR_KEYS},
        llm={key: validator_config[key] for key in _LLM_KEYS},
        sizing={'strategy_position_pct': position_pct,
                'base_position_pct': risk.base_position_pct,
                'max_position_pct': risk.max_position_pct,
                'sizing_mode': risk.sizing_mode,
                'risk_per_trade_pct': risk.risk_per_trade_pct,
                'risk_max_position_pct': risk.risk_max_position_pct,
                'min_position_value': risk.min_position_value,
                'strategy_allocation': risk.strategy_allocation,
                'hybrid_enabled': risk.hybrid.enabled},
        stops={'strategy_exit_params': stop_params,
               'stop_loss_pct': exit_config.stop_loss_pct,
               'min_stop_pct': exit_config.min_stop_pct,
               'max_stop_pct': exit_config.max_stop_pct,
               'intraday_crash_params': INTRADAY_CRASH_PARAMS},
        experts={'shadow_mode': experts_shadow_mode})


def _sync(sidecar: RiskManager) -> p.SyncPolicySnapshot:
    since = sidecar._sync_unhealthy_since
    if since is not None:
        if type(since) is not datetime:
            raise ValueError('명시 동기화 실패 시각이 필요합니다')
        # naive = KST 지역시각 (모듈 docstring 의 시각 규칙).
        since = since.replace(tzinfo=_KST) if since.utcoffset() is None else since.astimezone(_KST)
    return p.SyncPolicySnapshot(sidecar._sync_healthy, sidecar._sync_fail_count, since,
                                sidecar._sync_timeout_minutes)


def _trend(sidecar: RiskManager) -> p.MarketTrendPolicySnapshot:
    trend = sidecar._market_trend
    if type(trend) is not dict:
        raise ValueError('명시 시장 추세 dict 가 필요합니다')
    # legacy `_is_daily_loss_limit_hit` 의 truthy 판정을 그대로 옮긴다(빈 dict = 추세 없음).
    return p.MarketTrendPolicySnapshot(bool(trend), bool(trend.get('recovering')),
                                       sidecar._sidecar_active)


def _macro(day) -> p.MacroPolicySnapshot:
    try:
        label, is_event = macro_calendar.is_macro_event_day(day)
    except Exception:
        # legacy 도 캘린더 실패를 통과시킨다 — 실패를 '이벤트 아님'으로 위조하지 않고 표시한다.
        return p.MacroPolicySnapshot(None, False, True)
    return p.MacroPolicySnapshot(label, is_event, False)


async def publish_entry_policy_context(commands, *, risk: RiskConfig, sidecar: RiskManager,
                                       regime_adapter: MarketRegimeAdapter, fee_config: FeeConfig,
                                       validator_config: dict, position_pct: dict,
                                       stop_params: dict, exit_config: ExitConfig,
                                       experts_shadow_mode, now: datetime) -> str:
    """게시 시점 객체에서 정책 context 를 유도해 게시하고 그 config digest 를 돌려준다.

    `config_version` 을 인자로 받지 않는다 — 받으면 owner 의 대조가 한 값을 자기 자신과
    비교하게 된다. 네 owned version 과 `expected_version` 은 게시 직전 `owner.version`
    하나로 채운다(고정값이 아니다).
    """
    if type(now) is not datetime or now.utcoffset() is None:
        raise ValueError('aware 게시 시각이 필요합니다')
    if type(sidecar) is not RiskManager:
        raise ValueError('명시 sidecar RiskManager 가 필요합니다')
    if type(regime_adapter) is not MarketRegimeAdapter:
        raise ValueError('명시 MarketRegimeAdapter 가 필요합니다')
    digest = execution_config_version(validator_config=validator_config, risk=risk,
                                      position_pct=position_pct, stop_params=stop_params,
                                      exit_config=exit_config,
                                      experts_shadow_mode=experts_shadow_mode)
    local = now.astimezone(_KST)
    policy = effective_risk_policy(risk, regime=regime_adapter.regime, fee_config=fee_config)
    sync, trend, macro = _sync(sidecar), _trend(sidecar), _macro(local.date())
    version = commands.owner.version
    context = PolicyContext(
        business_day=local.date(), observed_at=now,
        versions=p.PolicyVersions(version, version, version, version, digest, 0, 0),
        policy=policy, sync=sync, trend=trend, macro=macro)
    await commands.publish_policy_context(context, expected_version=version)
    return digest
