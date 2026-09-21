"""로드된 설정에서 owner 진입 정책을 만드는 부품 (설치 단계 전용).

제품 호출자는 0건이다 — attach 설치(10A3b)가 정해지기 전에는 아무도 이 모듈을
부르지 않는다. 이 모듈 **자신**은 시계를 부르지 않고(now 는 전부 인자), 설정 파일을
직접 읽지 않으며, 정책 게시 외의 owner 상태를 건드리지 않는다.

시계가 주입으로 닫히지는 **않는다**: `publish_entry_policy_context` 가 읽는
`regime_adapter.regime` 은 제품 속성이고, 그 뒤의 `effective_regime` 은 장중 위험의
당일 게이트에서 host 벽시계(`src/core/market_regime.py` 의 `_now`)를 읽는다. 게시 시점의
레짐은 그래서 주입 now 가 아니라 벽시계 날짜에 걸린다 — 그 경로까지 주입 시계로 묶는
것은 10A3b 의 범위다.

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
from .application import ApplicationBlocked
from .commands import RequestBoundCommands
from .day_recovery import scope_reason
from .economics import decode_portfolio, encode_portfolio
from .gateway import SignalGateway
from .policy_generations import versioned_fact
from .policy_snapshot import PolicyContext
from .protection import encode_protection
from .qualification import config_version
from .regime_owner import POLICY_READS, RegimeOwner
from .store import ExecutionStateStore

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

    탐지력의 구멍 둘(이 digest 가 못 잡는 설정 변경):

    - experts 축은 `shadow_mode` 하나만 덮는다 — `experts.enabled`·`fail_open` 변경은
      검출되지 않는다(10A3b 호출자 명세의 결정 사항).
    - stops 축의 `INTRADAY_CRASH_PARAMS` 는 모듈 리터럴 상수라 설정 변경 탐지력을 더하지
      않는다. digest 에는 실리지만 어떤 설정으로도 움직이지 않는다.
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

    거부(전부 게시 **전**이라 owner 상태는 움직이지 않는다 — fail-closed):

    - naive `now`, 명시 인스턴스가 아닌 sidecar/regime_adapter.
    - sidecar 의 `_sync_unhealthy_since` 가 `now` 보다 나중이면 `PolicyContext` 가
      `ValueError('future_policy_sync_fact')` 를 던진다. 주기 재게시 호출자는 그래서
      `now` 를 게시 직전에 잡아야 한다 — 오래된 `now` 를 들고 오면 그 사이 갱신된 동기화
      사실이 미래로 보여 게시가 통째로 막힌다.
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


# `_publish` 가 요구하는 checkpoint root. 하나라도 없으면 복구 자체가 불가능하다.
_REQUIRED_ROOTS = frozenset({'portfolio', 'protection', 'risk', 'lots', 'outbox',
                             'intents', 'attempts', 'startup_reconciliation'})
# `engine.bind_execution_runtime` 이 attach 시점에 보는 legacy 미체결 장부 3종.
_LEGACY_LEDGERS = ('_pending_orders', '_pending_timestamps', '_reserved_by_order')


async def install_attached_runtime(runtime, commands, *, sidecar: RiskManager,
                                   regime_adapter: MarketRegimeAdapter, fee_config: FeeConfig,
                                   risk: RiskConfig, validator_config: dict, position_pct: dict,
                                   stop_params: dict, exit_config: ExitConfig,
                                   experts_shadow_mode, now: datetime, vix_fetcher) -> SignalGateway:
    """기동 시 한 번 attach 런타임을 세우거나 **명명된 사유로 거부한다**.

    제품 호출자는 0건이다. 제품에는 이 함수가 요구하는 checkpoint(포트폴리오·보호 대사 +
    레짐 baseline + policy generation 등록)를 만드는 코드가 없으므로 **운영에서는 항상
    거부로 끝나는 것이 정상**이다. 기동 시 단일 호출자가 전제이며 계좌 lease 를 잡지 않는다.

    구간 1(아래 1~9)은 순수 읽기다 — live·owner·기존 store 내용을 하나도 바꾸지 않고, store
    파일이 없으면 **만들지도 않는다**(`store.load()` 는 없는 파일을 생성·초기화한다).
    여기서 끝나면 호출자는 legacy 로 계속 가도 된다.

    구간 2(10~14)부터 live 는 저장본 값이다 — `runtime.restore()` 의 게시는 롤백 없는 순차
    대입이라 중간 실패가 혼합 상태를 남긴다. **이 뒤의 실패에서 호출자는 legacy 로 계속
    가면 안 된다**(프로세스를 세우거나 거래를 멈춘다). store 는 닫지 않는다 — 수명은
    호출자 소유다.

    예외 관례: 인자 모양은 `ValueError`, 상태 거부는 `ApplicationBlocked(<사유>)`, store
    장애(`StoreError`)와 `attach()` 의 `RuntimeError` 는 재작명하지 않고 그대로 올린다.
    `vix_fetcher` 에 기본값을 두지 않는 이유: None 은 "VIX 없음"이 아니라 실제 네트워크
    조회를 설치한다.
    """
    # ── 1. 인자 모양 ──────────────────────────────────────────────
    if type(now) is not datetime or now.utcoffset() is None:
        raise ValueError('aware 설치 시각이 필요합니다')
    if type(commands) is not RequestBoundCommands:
        raise ValueError('명시 RequestBoundCommands 가 필요합니다')
    if commands.runtime is not runtime or commands.owner is not runtime.owner:
        # 잘못 배선된 commands 는 남의 store 에 게시한다.
        raise ValueError('invalid_command_binding')
    # 정책 인자의 모양 결함을 live 를 건드리기 전으로 당긴다(결과는 버린다).
    execution_config_version(validator_config=validator_config, risk=risk,
                             position_pct=position_pct, stop_params=stop_params,
                             exit_config=exit_config, experts_shadow_mode=experts_shadow_mode)
    effective_risk_policy(risk, regime=regime_adapter.regime, fee_config=fee_config)

    # ── 2. 바인딩 선검사 ──────────────────────────────────────────
    engine = runtime.engine
    if engine.running:
        raise ApplicationBlocked('engine_running')
    if engine._execution_runtime is not None:
        raise ApplicationBlocked('execution_runtime_already_bound')
    if engine._event_queue:
        raise ApplicationBlocked('execution_queue_not_empty')
    if engine.risk_manager is not None and any(
            len(getattr(engine.risk_manager, ledger, ())) > 0 for ledger in _LEGACY_LEDGERS):
        # 제품 순서에서 처음 하중되는 가드를 restore 앞으로 당긴다.
        raise ApplicationBlocked('legacy_pending_orders_present')
    if runtime.gateway is not None:
        raise ApplicationBlocked('gateway_already_installed')
    # 실패 뒤의 재호출에서 8번 대조가 항진명제로 되살아나는 것을 구조로 막는다. 레짐 배선
    # 검사보다 **앞**이다 — 구간 2 의 실패는 `_regime_writer` 를 남기고 끝날 수 있어서
    # 뒤에 두면 재호출이 배선 충돌로 가려진다.
    if runtime.owner.version != 0 or runtime.owner.state:
        raise ApplicationBlocked('execution_runtime_already_restored')
    if (runtime._regime_writer is not None or sidecar is not runtime.risk_manager
            or regime_adapter is not getattr(engine, '_regime_adapter', None)):
        raise ApplicationBlocked('regime_owner_binding_conflict')

    # ── 3. 면제 별칭(주입은 호출자 몫, 설치기는 확인만 한다) ──────
    exit_manager = runtime.exit_manager
    if (engine.risk_manager is None
            or getattr(engine.risk_manager, '_exit_exempt_ref', None) is not exit_manager._exit_exempt):
        raise ApplicationBlocked('exit_exempt_alias_required')

    # ── 4. checkpoint 읽기(없는 파일을 만들지 않는다) ─────────────
    store = runtime.owner.store
    if type(store) is not ExecutionStateStore or not store.path.exists():
        raise ApplicationBlocked('startup_checkpoint_required')
    version, state = await store.load()
    if version <= 0 or not _REQUIRED_ROOTS <= state.keys():
        raise ApplicationBlocked('startup_checkpoint_required')

    # ── 5. 계좌 scope(제품 `_publish` 에는 scope 대조가 없다) ─────
    regime = state.get('regime_policy')
    if scope_reason(state, runtime.account_scope) or (
            regime is not None
            and regime['baseline']['supplied']['account_scope'] != runtime.account_scope):
        raise ApplicationBlocked('startup_account_scope_conflict')

    # ── 6. 일자 선필터(제품 입장 검사와 같은 시계·같은 식) ────────
    # 주입 `now` 는 정책 게시 시각 전용이다. 설치기는 일자 전환을 하지 않는다.
    if state['risk'].get('day') != runtime._now().date().isoformat():
        raise ApplicationBlocked('startup_day_transition_required')

    # ── 7. 레짐 baseline·등록(만들지 않는다) ──────────────────────
    if regime is None:
        raise ApplicationBlocked('startup_regime_baseline_required')
    for name in POLICY_READS:
        try:
            versioned_fact(state, name)
        except ValueError:
            raise ApplicationBlocked('startup_policy_generations_required') from None

    # ── 8. restore 앞 대조(restore 뒤는 게시본 대 게시본의 항진명제다) ──
    try:
        saved = decode_portfolio(state['portfolio'])
        for symbol, position in saved.positions.items():
            position.current_price = runtime._view_price(state, symbol, position.current_price)
        reconciled = (encode_portfolio(saved) == encode_portfolio(engine.portfolio)
                      and encode_protection(exit_manager) == state['protection'])
    except Exception:
        # live 인코딩 실패도 "대사되지 않았다"는 같은 결론이다.
        reconciled = False
    if not reconciled:
        raise ApplicationBlocked('startup_reconciliation_required')

    # ── 9. 잔존 prepared 선필터 ───────────────────────────────────
    # `recover_unsent()` 는 `kind=='submit'` 만 순회한다 — 미claim 자식 명령은 기동 sweep 이
    # 끝낼 수 없고 남으면 같은 종목의 모든 새 SUBMIT 을 영구 차단한다.
    for attempt in state['attempts'].values():
        if attempt.get('state') == 'prepared' and attempt.get('kind') != 'submit':
            raise ApplicationBlocked('startup_unresolved_prepared_attempt')

    # ── 구간 2 ────────────────────────────────────────────────────
    await runtime.restore()
    runtime._require_day_admission()
    writer = RegimeOwner(runtime, adapter=regime_adapter, sidecar=sidecar, vix_fetcher=vix_fetcher)
    if runtime._regime_writer is not writer or 'regime_policy' not in runtime.owner.state:
        raise ApplicationBlocked('regime_owner_binding_conflict')
    # digest 는 기동 시 한 번 고정한다 — 재게시와 함께 덮어쓰면 다시 자기 인증이 된다.
    digest = await publish_entry_policy_context(
        commands, risk=risk, sidecar=sidecar, regime_adapter=regime_adapter,
        fee_config=fee_config, validator_config=validator_config, position_pct=position_pct,
        stop_params=stop_params, exit_config=exit_config,
        experts_shadow_mode=experts_shadow_mode, now=now)
    gateway = SignalGateway(runtime, commands, exit_manager=exit_manager, config_version=digest)
    await gateway.recover_unsent()
    # backstop: sweep 이 끝내지 못한 행은 kind 를 가리지 않고 거부한다.
    for attempt in runtime.owner.state['attempts'].values():
        if attempt.get('state') == 'prepared':
            raise ApplicationBlocked('startup_unresolved_prepared_attempt')
    # attach 만 되고 gateway 가 없는 구간의 SIGNAL 은 조용히 폐기된다 — 인접한 두 줄로 줄인다.
    runtime.attach()
    runtime.install_gateway(gateway)
    return gateway
