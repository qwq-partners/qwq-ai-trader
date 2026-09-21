"""10A3b — 거부형 설치기(`factory.install_attached_runtime`)의 인수.

이 파일은 독립 인수(`tests/test_execution_signal_gateway_acceptance.py`)·owner 게이트
인수(`tests/test_execution_owner_gate_authority.py`)·S3/S4 시험 모듈을 **하나도 import 하지
않는다**(autouse fixture 가 모듈 경계를 넘지 않는다). 조립층 중에서는 `test_execution_runtime`
의 `setup` 하나만 쓰고, 그것도 **1차 seed 런타임**(checkpoint 를 만드는 쪽)에만 쓴다 —
그 함수가 restore·attach 까지 하기 때문이다. 설치 대상은 restore/attach 를 하지 않은 새
engine·ExitManager·store·runtime 으로 매번 새로 세운다.

**고정하는 것:** 설치 *순서*와 *명명된 거부*다. 구간 1(순수 읽기)의 거부는 live·owner 를
전혀 건드리지 않고 끝나야 하며(store 파일이 없던 경우 **생성되지 않는다**), 구간 2 의
실패는 live 가 저장본 값으로 남는다는 계약을 남긴다. `attach()` 는 되돌릴 수 없는 줄이라
`install_gateway` 바로 앞에 있다.

**GREEN 은 설치 승인이 아니다.** 제품에는 이 함수가 요구하는 checkpoint 를 만드는 코드가
없고 제품 호출자도 0건이다 — 운영에서는 항상 거부로 끝나는 것이 정상이다.
`KRExecutionRuntime.trading_ready` 는 제품에서 항상 False 이고(H0 이 monkeypatch 없이
단언한다) 성공 표본의 SIGNAL 도 그래서 송신까지 가지 않는다.

남긴 fake 와 이유(걷어내면 운영 자원에 닿는다): 브로커 HTTP, `_SigLog.get`(운영
PostgreSQL), `_sector_lookup`(운영 PostgreSQL), 시계(주입 clock + `datetime` 모듈),
`macro_calendar.is_macro_event_day`, 지수 조회. 사이징 오버레이 3종은 제품 스위치로 끈다.
"""
from __future__ import annotations

import asyncio
import datetime as _dtmod
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import src.core.cross_validator as _cv_module
import src.core.engine as _engine_module
import src.risk.manager as _risk_module
import src.utils.macro_calendar as _macro_module
import src.utils.session as _session_module
from src.core.cross_validator import CrossStrategyValidator
from src.core.engine import RiskManager as EngineRiskManager, UnifiedEngine
from src.core.event import ErrorEvent, OrderEvent, SignalEvent
from src.core.market_regime import MarketRegimeAdapter
from src.core.types import (
    OrderSide, RiskConfig, Signal, SignalStrength, StrategyType, TradingConfig,
)
from src.execution.broker.kis_kr import KISBroker, KISConfig
from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.commands import CommandValidationError, RequestBoundCommands
from src.execution.safety.economics import encode_portfolio
from src.execution.safety.factory import install_attached_runtime
from src.execution.safety.gateway import SignalGateway
from src.execution.safety.guards import (
    EntryAuthority, FinalEntryGuard, GuardDecision, RiskSnapshot,
)
from src.execution.safety.lifecycle import CommandKind, CommandResult, CommandStatus, OrderRef
from src.execution.safety.protection import encode_protection
from src.execution.safety.regime_owner import POLICY_READS, RegimeBaseline, RegimeOwner
from src.execution.safety.requests import KISRequestBuilder, RequestAccount
from src.execution.safety.risk_transition import IntradayPolicyState
from src.execution.safety.runtime import KRExecutionRuntime
from src.execution.safety.store import ExecutionStateStore
from src.risk.manager import RiskManager as SidecarRiskManager
from src.strategies.exit_manager import ExitConfig, ExitManager
from src.utils.fee_calculator import FeeConfig
from src.utils.stop_policy import make_entry_stop_resolver

from test_execution_runtime import setup as build_seed_runtime

_KST = ZoneInfo('Asia/Seoul')

# `test_execution_runtime.setup` 이 owner risk day 를 2026-09-18 로 굳히므로 동결 날짜도
# 같아야 한다. 11:00 은 정규장이고 CrossStrategyValidator 시간 규칙의 'other' 구간이다.
NOW_KST = datetime(2026, 9, 18, 11, 0, tzinfo=_KST)
_CLOCK = {'kst': NOW_KST}

SCOPE = 's10a3b-scope'
ACCOUNT_NO = '50123456'
PRODUCT_CD = '01'
ENDPOINT = 'https://openapi.koreainvestment.com:9443'
PRICE = D('10000')


class _FrozenDatetime(datetime):
    """`datetime.now()` 만 동결한다. 인스턴스를 만들지 않으므로 `type(x) is datetime` 불변."""

    @classmethod
    def now(cls, tz=None):
        return _CLOCK['kst'].astimezone(tz) if tz is not None else _CLOCK['kst'].replace(tzinfo=None)

    @classmethod
    def today(cls):
        return _CLOCK['kst'].replace(tzinfo=None)

    @classmethod
    def fromisoformat(cls, value):
        return datetime.fromisoformat(value)


class _FrozenDate(date):
    @classmethod
    def today(cls):
        return _CLOCK['kst'].date()

    @classmethod
    def fromisoformat(cls, value):
        return date.fromisoformat(value)


class _Response:
    def __init__(self, body_factory, status=200):
        self._body_factory, self.status = body_factory, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self._body_factory()


class _Session:
    def __init__(self, broker):
        self._broker, self.closed = broker, False
        self.posts = []

    def post(self, url, *, headers, json):
        self.posts.append((url, {'headers': dict(headers), 'json': dict(json)}))
        return _Response(self._broker.next_body)


def broker_fake():
    """실제 KISBroker 의 헤더 생성만 남기고 네트워크 경계를 대체한다."""
    broker = object.__new__(KISBroker)
    broker.config = KISConfig(app_key='s10a3b-key', app_secret='s10a3b-secret',
                              account_no=ACCOUNT_NO, account_product_cd=PRODUCT_CD,
                              env='prod', base_url=ENDPOINT)
    broker._token = 's10a3b-token'
    broker._token_mgr = type('_Tok', (), {'_access_token': None})()
    broker._session = _Session(broker)
    broker.best_bid = None
    broker.direct_calls = []
    counter = {'n': 0}

    def next_body():
        counter['n'] += 1
        return {'rt_cd': '0', 'output': {'ODNO': '920000%04d' % counter['n'],
                                         'KRX_FWDG_ORD_ORGNO': '01234'}}

    broker.next_body = next_body

    async def connect():
        broker._session.closed = False
        return True

    async def ensure_token():
        broker._token = 's10a3b-token'
        return True

    async def hashkey(body):
        return 's10a3b-hash'

    async def rate_limit(tr_id):
        return None

    async def get_best_bid(symbol):
        return broker.best_bid

    async def cancel_all_for_symbol(symbol):
        broker.direct_calls.append(('cancel_all_for_symbol', symbol))
        return 0

    async def submit_order(order):
        broker.direct_calls.append(('submit_order', order))
        return True, 'legacy-order-id'

    broker.connect, broker._ensure_token = connect, ensure_token
    broker._get_hashkey, broker._rate_limit = hashkey, rate_limit
    broker.get_best_bid = get_best_bid
    broker.cancel_all_for_symbol = cancel_all_for_symbol
    broker.submit_order = submit_order
    return broker


class _SigLogRecorder:
    """제품 `_log_sig` 본문은 그대로 태우고 저장 경계만 기록한다."""

    def __init__(self):
        self.rows = []

    async def log(self, **kwargs):
        self.rows.append(kwargs)
        return True


def inner_risk_manager(engine, config, *, sidecar, exits, stop_resolver):
    """engine 내부 RiskManager 를 `__init__` 없이 세우고 속성을 명시적으로 전부 채운다.

    `__init__` 은 LLM 매니저·TradeMemory·TradeWiki 를 만들어 네트워크/HOME 에 닿는다.
    """
    rm = object.__new__(EngineRiskManager)
    rm.engine, rm.config = engine, config
    rm._risk_validator = sidecar

    async def sector_lookup(symbol):
        return None

    rm._sector_lookup = sector_lookup
    rm._trade_memory = None
    rm._trade_wiki = None
    rm._cross_validator = CrossStrategyValidator(
        portfolio=engine.portfolio, risk_manager=sidecar, trade_memory=None,
        llm_manager=None, trade_wiki=None,
        max_sector_positions=config.max_positions_per_sector, expert_orchestrator=None)
    rm._resolve_entry_stop = stop_resolver
    rm._order_fail_cooldown = {}
    rm._COOLDOWN_SECONDS = 300
    rm._last_signal_time = {}
    rm._SIGNAL_COOLDOWN_SECONDS = 30
    rm._LLM_CHECK_MIN = 85
    rm._LLM_BYPASS_AT = 95
    rm._LLM_REJECT_SIZE_MULT = 0.5
    rm._REPLACEMENT_MIN_SCORE = 85
    rm._REPLACEMENT_LAST_EVICT_TS = {}
    rm._REPLACEMENT_COOLDOWN_SEC = 600
    rm._last_cash_warn_time = None
    rm._pending_orders = set()
    rm._pending_signal_cache = {}
    rm._pending_exit_reasons = {}
    rm._pending_timestamps = {}
    rm._PENDING_TIMEOUT_SECONDS = 600
    rm._pending_quantities = {}
    rm._pending_sides = {}
    rm._pending_fallback_count = {}
    rm._kis_qty_mismatch_count = {}
    rm._zombie_candidate_symbols = set()
    rm._reserved_by_order = {}
    rm._pending_strategy = {}
    # 제품(run_trader.py:854)과 같은 별칭. plain set 을 대입하면 owner 게시가 보이지 않는다.
    rm._exit_exempt_ref = exits._exit_exempt
    rm._stop_loss_today = set()
    rm._pending_lock = asyncio.Lock()
    rm._last_qualification_evidence = None
    engine.register_handler(SignalEvent().type, rm.on_signal)
    engine.register_handler(OrderEvent().type, rm.on_order)
    return rm


def validator_block():
    """`config/default.yml` 의 중첩 `kr.validator` 블록 중 CV/G4 가 읽는 값."""
    return {'min_pass_score': 50, 'missing_indicator_penalty_step': 2,
            'missing_indicator_penalty_cap': 8,
            'rule_penalties': {'early_session': 8, 'sepa_chase': 10, 'rsi_overbought': 5,
                               'supply_dual_sell': 10, 'surge_chase': 15, 'deficit_high_pbr': 10},
            'llm_daily_max': 10, 'llm_check_score_min': 85, 'llm_bypass_score': 95,
            'llm_reject_size_mult': 0.5}


def position_table():
    return {'sepa_trend': 25.0, 'vcp_breakout': 15.0, 'gap_and_go': 15.0, 'core_holding': 10.0}


def stop_table():
    return {'sepa_trend': {'stop_loss_pct': 5.0}, 'gap_and_go': {'stop_loss_pct': 3.5},
            'vcp_breakout': {'stop_loss_pct': 4.0}}


def bullish_index(code):
    """평균 등락률 +2%·시가 대비 상승 — 중기 bull 을 유지시키는 합성 지수 관측."""
    from uuid import uuid4
    values = {'price': 102.0, 'open': 100.0, 'high': 103.0, 'low': 99.0,
              'change': 2.0, 'change_pct': 2.0}
    return {**values, '_observation': {
        'schema_version': 1, 'source': 'kis', 'source_tr': 'FHPUP02100000', 'index_code': code,
        'observation_id': str(uuid4()), 'received_at': NOW_KST.isoformat(), 'market_as_of': None,
        'fields': {key: {'status': 'valid', 'value': value} for key, value in values.items()}}}


def regime_baseline(runtime):
    """설치자가 인계하는 레짐 기준선. 중기 추세 bull 로 SEPA 가 CV 를 통과하게 한다."""
    return {
        'schema': 1, 'baseline_id': 's10a3b-1', 'account_scope': runtime.account_scope,
        'business_day': NOW_KST.date().isoformat(), 'generation': runtime._day_generation,
        'fence_id': runtime._day_fence_id,
        'evidence': {'source': 's10a3b', 'event_id': 's10a3b-1',
                     'observed_at': NOW_KST.isoformat()},
        'sidecar_active': True,
        'intraday': {'baseline_version': runtime.owner.state['intraday_policy']['baseline_version'],
                     'current': runtime.owner.state['intraday_policy']['current']},
        'trend_state': {
            'mid_regime': 'bull',
            'mid_pending': {'present': False, 'target': None, 'since': None},
            'expert_pending': {'present': False, 'target': None, 'since': None},
            'market_trend': {'present': True, 'kospi_pct': 2.0, 'kosdaq_pct': 2.0,
                             'avg_pct': 2.0, 'vs_open_pct': 2.0, 'position_pct': 80.0,
                             'recovering': False, 'classified_at': NOW_KST.isoformat()},
            'regime_data': {}, 'last_update': NOW_KST.isoformat(),
            'source_refs': {'trend': None, 'vix': None, 'expert': None}},
        'engine_regime': 'bull'}


def buy_signal(symbol='012330', *, price=PRICE, score=80.0):
    """CV 감점 0 을 목표로 한 매수 SIGNAL."""
    metadata = {'indicators': {'atr_pct': 3.0, 'per': 12.0, 'pbr': 1.1,
                               'foreign_net_buy': 1000, 'inst_net_buy': 1000}}
    signal = Signal(symbol=symbol, side=OrderSide.BUY, strength=SignalStrength.NORMAL,
                    strategy=StrategyType.SEPA_TREND, price=price, score=score, confidence=0.8,
                    reason='20일 고가 돌파, 거래량 급증', reasons=['20일 고가 돌파', '거래량 급증'],
                    metadata=dict(metadata))
    event = SignalEvent.from_signal(signal, source='s10a3b')
    event.metadata = dict(metadata)
    event.strategy = StrategyType.SEPA_TREND
    return event


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    """conftest 에 없으므로 이 파일이 직접 정의한다. 실제 HOME 에 닿지 않게 한다."""
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    _CLOCK['kst'] = NOW_KST
    yield
    _CLOCK['kst'] = NOW_KST


def freeze(monkeypatch):
    monkeypatch.setenv('CALENDAR_SEASONALITY', '0')
    monkeypatch.setenv('VOL_TARGETING', '0')
    monkeypatch.setenv('TEAM_CONVICTION', '0')
    monkeypatch.setenv('ENTRY_PLAN_SHADOW', '0')
    _CLOCK['kst'] = NOW_KST
    monkeypatch.setattr(_session_module, 'datetime', _FrozenDatetime)
    monkeypatch.setattr(_engine_module, 'datetime', _FrozenDatetime)
    monkeypatch.setattr(_engine_module, 'date', _FrozenDate)
    monkeypatch.setattr(_cv_module, 'datetime', _FrozenDatetime)
    monkeypatch.setattr(_risk_module, 'datetime', _FrozenDatetime)
    monkeypatch.setattr(_risk_module, 'date', _FrozenDate)
    monkeypatch.setattr(_dtmod, 'datetime', _FrozenDatetime)
    monkeypatch.setattr(_dtmod, 'date', _FrozenDate)
    monkeypatch.setattr(_macro_module, 'is_macro_event_day', lambda day=None: ('', False))


async def seed_checkpoint(tmp_path, *, scope=SCOPE, mutate=None, with_trend=True):
    """1차 seed 런타임으로 checkpoint 를 만들고 닫는다. 설치 대상은 이 파일만 쓴다.

    `mutate` 는 checkpoint 를 마지막으로 한 번 더 바꾸는 reducer 다(잔존 행 표본 등).
    """
    sidecar = SidecarRiskManager(RiskConfig(), D('2000000'), 'KR')
    engine, exits, store, runtime = await build_seed_runtime(
        tmp_path, account_scope=scope, risk_manager=sidecar, clock=lambda: _CLOCK['kst'])
    try:
        adapter = MarketRegimeAdapter()
        engine._regime_adapter = adapter
        sidecar.set_exit_manager(exits)

        def prerequisite(state):
            value = IntradayPolicyState('normal', 0.0, None, None).to_dict()
            state['intraday_policy'] = {'schema': 1, 'baseline': value,
                                        'baseline_version': runtime.owner.version + 1,
                                        'current': value.copy(), 'transitions': {}}
            state['entry_policy_effects'] = {'pending_sectors': {}, 'sidecar_active': True}
            return state

        await runtime.owner.mutate('s10a3b-prerequisite', prerequisite)
        await RegimeOwner.register_baseline(
            runtime, RegimeBaseline.from_dict(regime_baseline(runtime)),
            expected_version=runtime.owner.version)
        await runtime.owner.register_policy_generations('s10a3b-reads', POLICY_READS)
        if with_trend:
            async def missing_vix():
                return None

            writer = RegimeOwner(runtime, adapter=adapter, sidecar=sidecar, vix_fetcher=missing_vix)

            async def index_price(code):
                return bullish_index(code)

            trend = await writer.refresh_trend(SimpleNamespace(fetch_index_price=index_price))
            assert trend.status == 'accepted'
        if mutate is not None:
            await mutate(runtime)
        await runtime.shutdown()
    finally:
        await store.close()
    return store.path


async def target(tmp_path, monkeypatch, *, scope=SCOPE, path=None, seed=True, **seed_kwargs):
    """설치 대상: restore/attach 를 하지 않은 새 부품 일습. 반환은 locals() 사전이다."""
    freeze(monkeypatch)
    store_path = path
    if seed:
        store_path = await seed_checkpoint(tmp_path, scope=scope, **seed_kwargs)
    if store_path is None:
        store_path = tmp_path / 'missing' / 'state.sqlite3'

    sidecar = SidecarRiskManager(RiskConfig(), D('2000000'), 'KR')
    broker = broker_fake()
    engine = UnifiedEngine(TradingConfig(initial_capital=D('2000000')))
    engine.broker = broker
    engine._market_regime = 'bull'
    exits = ExitManager(persist=False, clock=lambda: _CLOCK['kst'])
    sidecar.set_exit_manager(exits)
    risk = engine.config.risk
    # CLAUDE.md 의 KR 운영값(최소 포지션 20만원). 기본 RiskConfig 의 50만원은 US 쪽 값이다.
    risk.min_position_value = 200000
    siglog = _SigLogRecorder()
    monkeypatch.setattr(_engine_module._SigLog, 'get', staticmethod(lambda: siglog))

    stop_resolver = make_entry_stop_resolver(exits, stop_table())
    rm = inner_risk_manager(engine, risk, sidecar=sidecar, exits=exits, stop_resolver=stop_resolver)
    engine.risk_manager = rm
    adapter = MarketRegimeAdapter()
    engine._regime_adapter = adapter

    store = ExecutionStateStore(store_path)
    runtime = KRExecutionRuntime(store, engine, exits, clock=lambda: _CLOCK['kst'],
                                 risk_manager=sidecar, account_scope=scope)
    authority = EntryAuthority()
    builder = KISRequestBuilder(RequestAccount(scope, ACCOUNT_NO, PRODUCT_CD, 'prod', ENDPOINT, 1))
    alpha = [RiskSnapshot(1, 1, 'success', NOW_KST, 'normal')]
    commands = RequestBoundCommands(
        runtime, builder=builder, authority=authority,
        entry_guard=FinalEntryGuard(authority, lambda: alpha[0], lambda: _CLOCK['kst']),
        stop_resolver=stop_resolver, session_guard=lambda request: GuardDecision(True, 's10a3b_open'))

    async def missing_vix():
        return None

    kwargs = dict(sidecar=sidecar, regime_adapter=adapter, fee_config=FeeConfig(), risk=risk,
                  validator_config=validator_block(), position_pct=position_table(),
                  stop_params=stop_table(), exit_config=ExitConfig(),
                  experts_shadow_mode=True, now=NOW_KST, vix_fetcher=missing_vix)

    error_events = []

    async def drive(event):
        """실제 우선순위 힙에 싣고 큐가 빌 때까지 돈다."""
        await engine.emit(event)
        while engine._event_queue:
            queued = await engine._get_next_event()
            if queued is None:
                break
            if isinstance(queued, ErrorEvent):
                error_events.append(queued)
            await engine._process_event(queued)
        await asyncio.sleep(0)

    def posts():
        return broker._session.posts

    async def install(**overrides):
        return await install_attached_runtime(runtime, commands, **{**kwargs, **overrides})

    async def teardown():
        try:
            await runtime.shutdown()
        except ApplicationBlocked:
            pass
        await store.close()

    return locals()


def live_snapshot(f):
    """설치기가 건드리면 안 되는 live 세 축."""
    return (encode_portfolio(f['engine'].portfolio), encode_protection(f['exits']),
            f['sidecar']._sidecar_active, f['sidecar']._market_trend)


def assert_untouched(f, before):
    """구간 1 거부의 공통 계약 — live·owner·설치 흔적이 전부 그대로다."""
    assert live_snapshot(f) == before
    assert f['runtime'].owner.version == 0 and f['runtime'].owner.state == {}
    assert f['runtime'].owner.healthy is False
    assert f['engine']._execution_runtime is None
    assert f['runtime'].gateway is None or f['runtime'].gateway is f.get('installed_gateway')
    assert f['runtime']._regime_writer is None
    assert f['posts']() == [] and f['broker'].direct_calls == []


async def saved_state(f):
    """설치기와 같은 읽기(파일을 만들지 않는 경로는 호출자가 먼저 확인한다)."""
    probe = ExecutionStateStore(f['store'].path)
    try:
        return await probe.load()
    finally:
        await probe.close()


# ─────────────────────────── H — 하네스 자기 단언 ───────────────────────────

def test_h0_harness_self_assertions(tmp_path, monkeypatch):
    """나머지 전부의 근거. seed checkpoint 의 내용과 설치 전 대상의 상태를 고정한다."""
    async def scenario():
        f = await target(tmp_path, monkeypatch)
        try:
            runtime, engine, exits = f['runtime'], f['engine'], f['exits']
            # 1) 설치 대상은 restore/attach 전이다.
            assert runtime.owner.version == 0 and runtime.owner.state == {}
            assert engine._execution_runtime is None and runtime.gateway is None
            assert runtime._regime_writer is None
            # 2) 설치 승인이 아니다 — 제품 property 는 patch 없이 False 다.
            assert runtime.trading_ready is False
            # 3) live 는 저장본과 같은 값이다(대조 통과 표본).
            version, state = await saved_state(f)
            assert version > 0
            assert state['portfolio'] == encode_portfolio(engine.portfolio)
            assert state['protection'] == encode_protection(exits)
            assert state['risk']['day'] == NOW_KST.date().isoformat()
            # 4) seed 가 남긴 레짐·등록 근거
            assert state['regime_policy']['baseline']['supplied']['account_scope'] == SCOPE
            assert state['regime_policy']['trend_state']['source_refs']['trend'] is not None
            assert set(state['policy_generations']['selectors']) == set(POLICY_READS)
            assert state['attempts'] == {}
            # 5) 별칭·시계
            assert engine.risk_manager._exit_exempt_ref is exits._exit_exempt
            assert runtime._now() == NOW_KST
            assert _engine_module.datetime.now() == NOW_KST.replace(tzinfo=None)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


# ─────────────────────────── S — 성공 1건 ───────────────────────────

def test_s1_install_wires_the_single_path_and_the_signal_ends_unsent(tmp_path, monkeypatch):
    """설치 성공 → BUY SIGNAL 이 gateway 로 가고 `trading_ready=False` 라 송신 없이 끝난다."""
    async def scenario():
        f = await target(tmp_path, monkeypatch)
        try:
            runtime, engine = f['runtime'], f['engine']
            gateway = await f['install']()
            assert type(gateway) is SignalGateway
            # attach 와 install_gateway 가 둘 다 끝났다.
            assert engine._execution_runtime is runtime and runtime.gateway is gateway
            assert runtime._regime_writer is not None
            assert f['adapter']._execution_runtime is runtime
            assert f['sidecar']._execution_runtime is runtime
            # digest 는 기동 시 한 번 고정된다 — 게시본의 config 축과 같은 값이다.
            from src.execution.safety.policy_snapshot import PolicyContext
            published = PolicyContext.from_dict(runtime.owner.state['entry_policy_context'])
            assert published.versions.config == gateway.config_version
            outcomes = []
            original = gateway.submit

            async def spy(event, order, evidence):
                result = await original(event, order, evidence)
                outcomes.append(result)
                return result

            gateway.submit = spy
            await f['drive'](buy_signal())
            assert len(outcomes) == 1
            assert outcomes[0].status is CommandStatus.NOT_SENT
            assert outcomes[0].reason_code == 'startup_reconciliation'
            # POST 0 · 예약 0 · legacy 직접 호출 0
            assert f['posts']() == [] and f['broker'].direct_calls == []
            assert gateway.reserved_cash() == D('0')
            assert f['error_events'] == []
        finally:
            await f['teardown']()
    asyncio.run(scenario())


# ─────────────────── B — 구간 1 의 인자 모양 거부 ───────────────────

def test_b1_naive_now_is_an_argument_shape_error(tmp_path, monkeypatch):
    async def scenario():
        f = await target(tmp_path, monkeypatch)
        before = live_snapshot(f)
        try:
            with pytest.raises(ValueError):
                await f['install'](now=NOW_KST.replace(tzinfo=None))
            assert_untouched(f, before)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_b2_vix_fetcher_is_a_required_keyword(tmp_path, monkeypatch):
    """기본값 None 은 'VIX 없음'이 아니라 실제 네트워크 조회를 설치한다(§6-1)."""
    async def scenario():
        f = await target(tmp_path, monkeypatch)
        before = live_snapshot(f)
        try:
            kwargs = dict(f['kwargs'])
            kwargs.pop('vix_fetcher')
            with pytest.raises(TypeError):
                await install_attached_runtime(f['runtime'], f['commands'], **kwargs)
            assert_untouched(f, before)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_b3_commands_bound_to_another_runtime_touch_neither_store(tmp_path, monkeypatch):
    """잘못 배선된 commands 는 남의 store 에 게시한다 — 두 store 의 version 이 그대로다."""
    async def scenario():
        first = await target(tmp_path, monkeypatch)
        second = await target(tmp_path / 'second', monkeypatch)
        before = (live_snapshot(first), live_snapshot(second))
        try:
            versions = (await saved_state(first))[0], (await saved_state(second))[0]
            with pytest.raises(ValueError) as caught:
                await install_attached_runtime(first['runtime'], second['commands'],
                                               **first['kwargs'])
            assert 'invalid_command_binding' in str(caught.value)
            assert ((await saved_state(first))[0], (await saved_state(second))[0]) == versions
            assert_untouched(first, before[0])
            assert_untouched(second, before[1])
        finally:
            await first['teardown']()
            await second['teardown']()
    asyncio.run(scenario())


# ─────────────────── C — 구간 1 의 바인딩 선검사 (각각 단독) ───────────────────

def _binding_case(name, prepare):
    def run(tmp_path, monkeypatch):
        async def scenario():
            f = await target(tmp_path, monkeypatch)
            prepare(f)
            before = live_snapshot(f)
            try:
                with pytest.raises(ApplicationBlocked) as caught:
                    await f['install']()
                assert str(caught.value) == name
                assert_untouched(f, before)
            finally:
                await f['teardown']()
        asyncio.run(scenario())
    return run


def _set_running(f):
    f['engine'].running = True


def _set_bound(f):
    f['engine']._execution_runtime = SimpleNamespace(gateway=None)


def _queue_event(f):
    f['engine']._event_queue.append(buy_signal())


def _legacy_ledger(f):
    f['engine'].risk_manager._pending_orders.add('legacy-order-1')


def _installed_gateway(f):
    f['runtime'].gateway = SimpleNamespace(runtime=f['runtime'])
    f['installed_gateway'] = f['runtime'].gateway


def _regime_writer_present(f):
    f['runtime']._regime_writer = SimpleNamespace()


test_c1_engine_running = _binding_case('engine_running', _set_running)
test_c2_execution_runtime_already_bound = _binding_case('execution_runtime_already_bound', _set_bound)
test_c3_execution_queue_not_empty = _binding_case('execution_queue_not_empty', _queue_event)
test_c4_legacy_pending_orders_present = _binding_case('legacy_pending_orders_present', _legacy_ledger)
test_c5_gateway_already_installed = _binding_case('gateway_already_installed', _installed_gateway)
test_c6_regime_owner_binding_conflict = _binding_case('regime_owner_binding_conflict',
                                                      _regime_writer_present)


def test_c7_regime_adapter_must_be_the_engine_bound_object(tmp_path, monkeypatch):
    """adapter 가 engine 에 묶인 객체가 아니면 RegimeOwner 생성 전에 거부한다."""
    async def scenario():
        f = await target(tmp_path, monkeypatch)
        before = live_snapshot(f)
        try:
            with pytest.raises(ApplicationBlocked) as caught:
                await f['install'](regime_adapter=MarketRegimeAdapter())
            assert str(caught.value) == 'regime_owner_binding_conflict'
            assert_untouched(f, before)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_c8_sidecar_must_be_the_runtime_bound_object(tmp_path, monkeypatch):
    async def scenario():
        f = await target(tmp_path, monkeypatch)
        before = live_snapshot(f)
        try:
            with pytest.raises(ApplicationBlocked) as caught:
                await f['install'](sidecar=SidecarRiskManager(RiskConfig(), D('2000000'), 'KR'))
            assert str(caught.value) == 'regime_owner_binding_conflict'
            assert_untouched(f, before)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_c9_exit_exempt_alias_required(tmp_path, monkeypatch):
    """면제 별칭 주입은 호출자 몫이고 설치기는 동일성만 확인한다."""
    async def scenario():
        f = await target(tmp_path, monkeypatch)
        f['engine'].risk_manager._exit_exempt_ref = set(f['exits']._exit_exempt)
        before = live_snapshot(f)
        try:
            with pytest.raises(ApplicationBlocked) as caught:
                await f['install']()
            assert str(caught.value) == 'exit_exempt_alias_required'
            assert_untouched(f, before)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_c10_already_restored_runtime_is_refused(tmp_path, monkeypatch):
    """설치 실패 뒤의 재호출이 restore 앞 대조를 항진명제로 되살리지 못한다."""
    async def scenario():
        f = await target(tmp_path, monkeypatch)
        try:
            # 구간 2 에서 실패시킨다: 게시 시각이 다른 날이면 owner 가 거부한다.
            with pytest.raises(CommandValidationError):
                await f['install'](now=NOW_KST + timedelta(days=1))
            assert f['runtime'].owner.version > 0          # restore 는 끝났다
            assert f['engine']._execution_runtime is None  # attach 는 아직이다
            with pytest.raises(ApplicationBlocked) as caught:
                await f['install']()
            assert str(caught.value) == 'execution_runtime_already_restored'
        finally:
            await f['teardown']()
    asyncio.run(scenario())


# ─────────────────── D — 구간 1 의 checkpoint 선검사 ───────────────────

def test_d1_missing_store_file_is_refused_without_creating_it(tmp_path, monkeypatch):
    """`store.load()` 는 없는 파일을 만든다 — 읽기가 아니다. 설치기는 만들지 않는다."""
    async def scenario():
        f = await target(tmp_path, monkeypatch, seed=False)
        before = live_snapshot(f)
        try:
            assert not f['store'].path.exists()
            with pytest.raises(ApplicationBlocked) as caught:
                await f['install']()
            assert str(caught.value) == 'startup_checkpoint_required'
            assert not f['store'].path.exists()
            assert not f['store'].path.parent.exists()
            assert_untouched(f, before)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_d2_account_scope_conflict(tmp_path, monkeypatch):
    """다른 계좌의 checkpoint 가 그대로 live 에 게시되지 않는다(제품 `_publish` 에 대조 없음)."""
    async def scenario():
        path = await seed_checkpoint(tmp_path, scope='other-scope')
        f = await target(tmp_path, monkeypatch, seed=False, path=path)
        before = live_snapshot(f)
        try:
            with pytest.raises(ApplicationBlocked) as caught:
                await f['install']()
            assert str(caught.value) == 'startup_account_scope_conflict'
            assert_untouched(f, before)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_d3_day_prefilter_uses_the_runtime_clock_not_the_injected_now(tmp_path, monkeypatch):
    """일자 선필터는 제품의 입장 검사와 **같은 시계**(`runtime._now`)를 쓴다."""
    async def scenario():
        f = await target(tmp_path, monkeypatch)
        before = live_snapshot(f)
        try:
            # 주입 now 는 저장본 일자 그대로인데 runtime 시계만 다음 날로 간다 → 거부.
            _CLOCK['kst'] = NOW_KST + timedelta(days=1)
            with pytest.raises(ApplicationBlocked) as caught:
                await f['install']()
            assert str(caught.value) == 'startup_day_transition_required'
            assert_untouched(f, before)
        finally:
            _CLOCK['kst'] = NOW_KST
            await f['teardown']()
    asyncio.run(scenario())


def test_d4_day_prefilter_does_not_read_the_injected_now(tmp_path, monkeypatch):
    """반대 방향 — 주입 now 만 다른 날이면 선필터는 통과하고 뒤의 게시가 거부한다."""
    async def scenario():
        f = await target(tmp_path, monkeypatch)
        try:
            with pytest.raises(CommandValidationError) as caught:
                await f['install'](now=NOW_KST + timedelta(days=1))
            assert str(caught.value) == 'policy_context_day_or_time_mismatch'
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_d5_regime_baseline_required(tmp_path, monkeypatch):
    """설치기는 레짐 baseline 을 만들지 않는다 — 없으면 거부다."""
    async def scenario():
        sidecar = SidecarRiskManager(RiskConfig(), D('2000000'), 'KR')
        engine, exits, store, runtime = await build_seed_runtime(
            tmp_path / 'bare', account_scope=SCOPE, risk_manager=sidecar,
            clock=lambda: _CLOCK['kst'])
        await store.close()
        f = await target(tmp_path, monkeypatch, seed=False, path=store.path)
        before = live_snapshot(f)
        try:
            with pytest.raises(ApplicationBlocked) as caught:
                await f['install']()
            assert str(caught.value) == 'startup_regime_baseline_required'
            assert_untouched(f, before)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_d6_policy_generations_required(tmp_path, monkeypatch):
    """POLICY_READS 미등록이면 RegimeOwner 가 뒤에서 죽는다 — 앞에서 명시 거부한다."""
    async def scenario():
        async def skip_registration(runtime):
            return None

        # 등록만 빼고 baseline 은 만든 checkpoint.
        sidecar = SidecarRiskManager(RiskConfig(), D('2000000'), 'KR')
        engine, exits, store, runtime = await build_seed_runtime(
            tmp_path / 'noreads', account_scope=SCOPE, risk_manager=sidecar,
            clock=lambda: _CLOCK['kst'])
        try:
            adapter = MarketRegimeAdapter()
            engine._regime_adapter = adapter
            sidecar.set_exit_manager(exits)

            def prerequisite(state):
                value = IntradayPolicyState('normal', 0.0, None, None).to_dict()
                state['intraday_policy'] = {'schema': 1, 'baseline': value,
                                            'baseline_version': runtime.owner.version + 1,
                                            'current': value.copy(), 'transitions': {}}
                state['entry_policy_effects'] = {'pending_sectors': {}, 'sidecar_active': True}
                return state

            await runtime.owner.mutate('s10a3b-prerequisite', prerequisite)
            await RegimeOwner.register_baseline(
                runtime, RegimeBaseline.from_dict(regime_baseline(runtime)),
                expected_version=runtime.owner.version)
            await runtime.shutdown()
        finally:
            await store.close()
        f = await target(tmp_path, monkeypatch, seed=False, path=store.path)
        before = live_snapshot(f)
        try:
            with pytest.raises(ApplicationBlocked) as caught:
                await f['install']()
            assert str(caught.value) == 'startup_policy_generations_required'
            assert_untouched(f, before)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_d7_reconciliation_compares_before_restore(tmp_path, monkeypatch):
    """대조는 restore **앞**이다 — 1원 어긋난 live 가 저장본으로 덮여 통과하지 않는다."""
    async def scenario():
        f = await target(tmp_path, monkeypatch)
        f['engine'].portfolio.cash -= D('1')
        before = live_snapshot(f)
        try:
            with pytest.raises(ApplicationBlocked) as caught:
                await f['install']()
            assert str(caught.value) == 'startup_reconciliation_required'
            assert_untouched(f, before)
            assert f['engine'].portfolio.cash == D('1999999')
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_d8_protection_mismatch_is_also_a_reconciliation_refusal(tmp_path, monkeypatch):
    """보호 상태도 같은 정규화로 본다."""
    async def scenario():
        f = await target(tmp_path, monkeypatch)
        f['exits'].add_exit_exempt('087010')
        before = live_snapshot(f)
        try:
            with pytest.raises(ApplicationBlocked) as caught:
                await f['install']()
            assert str(caught.value) == 'startup_reconciliation_required'
            assert_untouched(f, before)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_d9_unsweepable_child_command_is_refused_before_restore(tmp_path, monkeypatch):
    """`recover_unsent()` 는 미claim 자식을 쓸지 않는다 — 잔존 검사는 kind 무관이다."""
    async def scenario():
        async def leave_child(runtime):
            ref = OrderRef(SCOPE, 'KR', NOW_KST.date().isoformat(), 'KRX', '9100001')
            await runtime.lifecycle.prepare('i1', 'a1', 10, '005930', 'buy',
                                            strategy='sepa_trend', reserved_cash='100000')
            await runtime.lifecycle.claim('a1', 'sender')
            await runtime.lifecycle.record_result('a1', 'sender', CommandResult(
                CommandStatus.ACKNOWLEDGED, 'a1', ref))
            await runtime.lifecycle.prepare('i1', 'a2', 10, '005930', 'buy',
                                            command=CommandKind.CANCEL,
                                            parent_attempt_id='a1', order_ref=ref)

        path = await seed_checkpoint(tmp_path, mutate=leave_child)
        f = await target(tmp_path, monkeypatch, seed=False, path=path)
        before = live_snapshot(f)
        try:
            version, state = await saved_state(f)
            assert state['attempts']['a2']['kind'] == 'cancel'
            assert state['attempts']['a2']['state'] == 'prepared'
            with pytest.raises(ApplicationBlocked) as caught:
                await f['install']()
            assert str(caught.value) == 'startup_unresolved_prepared_attempt'
            assert_untouched(f, before)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


# ─────────────────── E — 구간 2 의 계약 ───────────────────

def test_e1_failure_after_restore_leaves_live_at_the_stored_values(tmp_path, monkeypatch):
    """구간 2 의 실패는 live 를 저장본 값으로 남긴다 — 호출자는 legacy 로 가면 안 된다."""
    async def scenario():
        f = await target(tmp_path, monkeypatch)
        try:
            engine, runtime = f['engine'], f['runtime']
            with pytest.raises(CommandValidationError):
                await f['install'](now=NOW_KST + timedelta(days=1))
            version, state = await saved_state(f)
            assert runtime.owner.version == version
            assert encode_portfolio(engine.portfolio) == state['portfolio']
            assert encode_protection(f['exits']) == state['protection']
            # attach 는 되돌릴 수 없는 줄이라 마지막까지 일어나지 않았다.
            assert engine._execution_runtime is None and runtime.gateway is None
            assert f['posts']() == []
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_e2_unsweepable_submit_is_caught_by_the_kind_agnostic_backstop(tmp_path, monkeypatch):
    """sweep 이 끝낼 수 없는 prepared SUBMIT 은 `recover_unsent()` 뒤 backstop 이 잡는다."""
    async def scenario():
        async def leave_submit(runtime):
            ref = OrderRef(SCOPE, 'KR', NOW_KST.date().isoformat(), 'KRX', '9100002')
            # order_ref 가 채워진 SUBMIT 은 곧 송신 증거라 `abandon_candidate` 가 거부한다.
            await runtime.lifecycle.prepare('i9', 'a9', 10, '005930', 'buy',
                                            strategy='sepa_trend', reserved_cash='100000',
                                            order_ref=ref)

        path = await seed_checkpoint(tmp_path, mutate=leave_submit)
        f = await target(tmp_path, monkeypatch, seed=False, path=path)
        try:
            with pytest.raises(ApplicationBlocked) as caught:
                await f['install']()
            assert str(caught.value) == 'startup_unresolved_prepared_attempt'
            # 구간 2 의 거부다 — restore 는 끝났고 attach 는 일어나지 않았다.
            assert f['runtime'].owner.version > 0
            assert f['engine']._execution_runtime is None
            assert f['runtime'].gateway is None
            assert f['posts']() == []
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_e3_sweepable_submit_is_released_and_install_succeeds(tmp_path, monkeypatch):
    """대조 표본 — 끝낼 수 있는 prepared SUBMIT 은 sweep 이 풀고 설치가 이어진다."""
    async def scenario():
        async def leave_submit(runtime):
            await runtime.lifecycle.prepare('i8', 'a8', 10, '005930', 'buy',
                                            strategy='sepa_trend', reserved_cash='100000')

        path = await seed_checkpoint(tmp_path, mutate=leave_submit)
        f = await target(tmp_path, monkeypatch, seed=False, path=path)
        try:
            gateway = await f['install']()
            assert type(gateway) is SignalGateway
            attempt = f['runtime'].owner.state['attempts']['a8']
            assert attempt['command_status'] == 'not_sent'
            assert attempt['reason_code'] == 'startup_unclaimed'
            assert gateway.reserved_cash() == D('0')
            assert f['posts']() == []
        finally:
            await f['teardown']()
    asyncio.run(scenario())
