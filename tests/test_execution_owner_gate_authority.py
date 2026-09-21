"""S10A3a-2 — owner 게이트의 단독 결정·누적 정합 인수 (제품 0줄).

이 파일은 `tests/test_execution_signal_gateway_acceptance.py`(S5 독립 인수 37건)와 S3·S4 의
시험 모듈을 **하나도 import 하지 않는다**. autouse fixture 가 모듈 경계를 넘지 않으므로
하네스를 새로 조립하고, S3 이전의 조립층 중에서는 `test_execution_runtime.setup`(실제
engine·ExitManager·SQLite store·KRExecutionRuntime) 하나만 쓴다.

**고정하는 것:** attach 경로에서 owner 가 *단독으로* 내리는 진입 판정이다. 축마다 게시
정책값만 sidecar(`src/risk/manager.RiskManager`)의 `RiskConfig` 보다 엄격하게 두어,
sidecar 는 같은 SIGNAL 을 통과시키고(도달 증거를 spy 와 `_log_sig` 로 함께 단언한다) owner
의 `evaluate_entry_policy` 가 그 뒤에서 막는 표본을 만든다. 누적 표본은 미해결 BUY 가
예약 합·포지션 슬롯·섹터 한도·전략별 예산에 **동시에** 실린다는 것을 고정한다.

**GREEN 은 설치 승인이 아니다.** `KRExecutionRuntime.trading_ready` 는 제품에서 항상 False
이고(H0 이 monkeypatch 없이 단언한다) 이 파일의 모든 송신 표본은 합성 startup 허가 위에
있다.

남긴 fake 와 이유(걷어내면 운영 자원에 닿는다):

- 브로커 HTTP: 실제로 KIS 로 나간다. 헤더 생성은 제품 `KISBroker` 그대로 태운다.
- `_SigLog.get`: 운영 PostgreSQL 로 나간다. `rm._log_sig` 본문은 그대로 태우고 저장 경계만
  바꾼다 — 어느 게이트가 막았는지의 도달 증거가 여기서 나온다.
- `_sector_lookup`: 운영 PostgreSQL(`kr_stock_master`)로 나간다.
- `trading_ready` property: 합성 startup 허가.
- 시계: 주입 clock + `datetime` 모듈 자신(`_FrozenDatetime` 독스트링 참고) +
  `src.utils.macro_calendar.is_macro_event_day`.
- 사이징 오버레이 3종은 제품 스위치(`CALENDAR_SEASONALITY`/`VOL_TARGETING`/
  `TEAM_CONVICTION` = 0)로 끈다 — 모듈 상수가 import 시점에 HOME 캐시 경로를 굳힌다.
"""
from __future__ import annotations

import asyncio
import datetime as _dtmod
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

# 시계 patch 창 동안 새 모듈 import 가 일어나지 않도록 경로에 등장하는 모듈을 미리 묶는다.
import conftest as _conftest
import src.core.cross_validator as _cv_module
import src.core.engine as _engine_module
import src.execution.safety.qualification_publisher as _qual_module
import src.execution.safety.risk_input_seal as _seal_module
import src.risk.manager as _risk_module
import src.signals.strategic.expert_panel as _panel_module
import src.utils.session_util as _session_util_module
import src.utils.calendar_seasonality as _calendar_module
import src.utils.fee_calculator as _fee_module
import src.utils.macro_calendar as _macro_module
import src.utils.session as _session_module
import src.utils.team_conviction as _conviction_module
import src.utils.volatility_targeting as _vol_module
from src.core.cross_validator import CrossStrategyValidator
from src.core.engine import RiskManager as EngineRiskManager, UnifiedEngine
from src.core.event import ErrorEvent, OrderEvent, SignalEvent
from src.core.types import (
    OrderSide, Position, RiskConfig, Signal, SignalStrength, StrategyType, TradingConfig,
)
from src.execution.broker.kis_kr import KISBroker, KISConfig
from src.execution.safety import risk_policy as _policy
from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.commands import CommandValidationError, RequestBoundCommands
from src.execution.safety.economics import encode_portfolio, new_risk_state
from src.execution.safety.gateway import SignalGateway
from src.execution.safety.guards import (
    EntryAuthority, FinalEntryGuard, GuardDecision, RiskSnapshot,
)
from src.execution.safety.lifecycle import CommandStatus, OrderRef
from src.execution.safety.policy_snapshot import PolicyContext
from src.execution.safety.protection import encode_protection
from src.execution.safety.requests import CancelParent, KISRequestBuilder, RequestAccount, _session_at
from src.execution.safety.runtime import KRExecutionRuntime
from src.execution.safety.store import ExecutionStateStore
from src.strategies.exit_manager import ExitManager
from src.utils.stop_policy import make_entry_stop_resolver

from test_execution_runtime import setup as build_runtime

_KST = ZoneInfo('Asia/Seoul')

# 2026-09-18(금) 11:00 KST — `test_execution_runtime.setup` 이 owner risk day 를 이 날짜로
# 굳히므로 동결 날짜도 같아야 한다(다르면 `day_admission_closed`). 11:00 은 정규장이고
# CrossStrategyValidator 시간 규칙의 'other' 구간이다.
NOW_KST = datetime(2026, 9, 18, 11, 0, tzinfo=_KST)
_CLOCK = {'kst': NOW_KST}

ACCOUNT_SCOPE = 's10a3a2-scope'
ACCOUNT_NO = '50123456'
PRODUCT_CD = '01'
ENDPOINT = 'https://openapi.koreainvestment.com:9443'
ORDER_PATH = '/uapi/domestic-stock/v1/trading/order-cash'
CONFIG_VERSION = 'cfg-s10a3a2-1'
PRICE = D('10000')


class _FrozenDatetime(datetime):
    """`datetime.now()` 만 동결한다. 인스턴스를 만들지 않으므로 `type(x) is datetime` 불변.

    engine·cross_validator·risk.manager·session 의 모듈 수준 `datetime` 에 꽂고, 나아가
    `datetime` 모듈 자신에도 꽂는다 — `cross_validator.validate` 는 함수 안에서
    `from datetime import datetime as _dt` 로 다시 import 해 `now_hm` 을 읽으므로 모듈
    patch 가 닿지 않는다. 닿지 않으면 판단 버킷이 실제 벽시계를 따라가 09:00~09:29 에는
    자동 매수가 통째로 차단된다.
    """

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


def advance(seconds):
    """동결 시계를 한 번에 전진시킨다(모든 축이 같은 holder 를 읽는다)."""
    _CLOCK['kst'] = _CLOCK['kst'] + timedelta(seconds=seconds)


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
    """실제 KISBroker 의 헤더 생성만 남기고 네트워크 경계를 대체한다. ODNO 는 호출마다 증가."""
    broker = object.__new__(KISBroker)
    broker.config = KISConfig(app_key='s10a3a2-key', app_secret='s10a3a2-secret',
                              account_no=ACCOUNT_NO, account_product_cd=PRODUCT_CD,
                              env='prod', base_url=ENDPOINT)
    broker._token = 's10a3a2-token'
    broker._token_mgr = type('_Tok', (), {'_access_token': None})()
    broker._session = _Session(broker)
    broker.ack_output = {}          # None 으로 바꾸면 주문번호 없는 ACK → UNKNOWN
    broker.best_bid = None
    broker.direct_calls = []        # 취소/직접 주문 호출 기록
    counter = {'n': 0}

    def next_body():
        if broker.ack_output is None:
            return {'rt_cd': '0', 'output': {}}
        counter['n'] += 1
        return {'rt_cd': '0', 'output': {'ODNO': '910000%04d' % counter['n'],
                                         'KRX_FWDG_ORD_ORGNO': '01234', **broker.ack_output}}

    broker.next_body = next_body

    async def connect():
        broker._session.closed = False
        return True

    async def ensure_token():
        broker._token = 's10a3a2-token'
        return True

    async def hashkey(body):
        return 's10a3a2-hash'

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

    def gates(self):
        return [(row['event_type'], row['block_gate']) for row in self.rows]


def inner_risk_manager(engine, config, *, sidecar, exits, sector_lookup, stop_resolver):
    """engine 내부 RiskManager 를 `__init__` 없이 세우고 그 속성을 명시적으로 전부 채운다.

    `__init__` 은 LLM 매니저·TradeMemory·TradeWiki 를 만들어 네트워크/HOME 에 닿는다. 빠진
    속성은 광역 except 에 삼켜져 "가드가 동작했다"로 오독되므로 한 곳에 모아 적는다.
    """
    rm = object.__new__(EngineRiskManager)
    rm.engine, rm.config = engine, config
    rm._risk_validator = sidecar
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


def effective_policy(risk: RiskConfig, **stricter) -> _policy.EffectiveRiskPolicy:
    """owner 가 판정에 쓰는 정책. 기본은 공유 RiskConfig 에서 옮겨 적고 인자로만 조인다.

    `stricter` 로 덮는 축이 이 파일의 '단독 결정' 표본이다 — 같은 값이 sidecar 에는 없으므로
    sidecar 는 통과시키고 owner 만 막는다.

    `regime_min_cash_reserve_pct` 의 게시값은 쓰이지 않는다 — 실제 `MarketRegimeAdapter` 를
    설치하므로 `build_owned_snapshot` 이 평가 시점에 레짐 표로 이 필드를 덮어쓴다.
    """
    values = dict(
        daily_max_loss_pct=risk.daily_max_loss_pct,
        daily_max_trades=risk.daily_max_trades,
        max_daily_new_buys=risk.max_daily_new_buys,
        max_positions=risk.max_positions,
        max_core_positions=risk.max_core_positions,
        max_position_pct=risk.max_position_pct,
        min_cash_reserve_pct=risk.min_cash_reserve_pct,
        max_positions_per_sector=risk.max_positions_per_sector,
        daily_exit_cooldown_threshold=risk.daily_exit_cooldown_threshold,
        regime_min_cash_reserve_pct=risk.min_cash_reserve_pct,
        core_allocation_pct=risk.strategy_allocation['core_holding'],
        sizing_mode=risk.sizing_mode,
        risk_per_trade_pct=risk.risk_per_trade_pct,
        risk_max_position_pct=risk.risk_max_position_pct,
        buy_commission_rate=D('0.000140527'),
        hybrid_enabled=risk.hybrid.enabled,
    )
    values.update(stricter)
    return _policy.EffectiveRiskPolicy(**values)


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    """conftest 에 없으므로 이 파일이 직접 정의한다. 실제 HOME 에 닿지 않게 한다."""
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    _CLOCK['kst'] = NOW_KST
    yield
    _CLOCK['kst'] = NOW_KST


def regime_baseline(runtime):
    """설치자가 인계하는 레짐 기준선. 중기 추세를 bull 로 세워 SEPA 가 CV 를 통과하게 한다."""
    return {
        'schema': 1, 'baseline_id': 's10a3a2-1', 'account_scope': runtime.account_scope,
        'business_day': NOW_KST.date().isoformat(), 'generation': runtime._day_generation,
        'fence_id': runtime._day_fence_id,
        'evidence': {'source': 's10a3a2', 'event_id': 's10a3a2-1',
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


def bullish_index(code):
    """평균 등락률 +2%·시가 대비 상승 — 중기 bull 을 유지시키는 합성 지수 관측."""
    from uuid import uuid4
    values = {'price': 102.0, 'open': 100.0, 'high': 103.0, 'low': 99.0,
              'change': 2.0, 'change_pct': 2.0}
    return {**values, '_observation': {
        'schema_version': 1, 'source': 'kis', 'source_tr': 'FHPUP02100000', 'index_code': code,
        'observation_id': str(uuid4()), 'received_at': NOW_KST.isoformat(), 'market_as_of': None,
        'fields': {key: {'status': 'valid', 'value': value} for key, value in values.items()}}}


def buy_signal(symbol='005930', *, price=PRICE, score=80.0,
               strategy=StrategyType.SEPA_TREND, sector=None):
    """CV 감점 0 을 목표로 한 매수 SIGNAL.

    지표를 전부 실어 '지표결손' 감점을 없애고, score 80 은 SEPA 90+ 추격매수 감점(>=90)과
    G4 LLM 이중검증 구간(85<=score<95)을 모두 피한다 — LLM 경로는 네트워크다.
    """
    metadata = {'indicators': {'atr_pct': 3.0, 'per': 12.0, 'pbr': 1.1,
                               'foreign_net_buy': 1000, 'inst_net_buy': 1000}}
    if sector is not None:
        metadata['sector'] = sector
    signal = Signal(symbol=symbol, side=OrderSide.BUY, strength=SignalStrength.NORMAL,
                    strategy=strategy, price=price, score=score, confidence=0.8,
                    reason='20일 고가 돌파, 거래량 급증', reasons=['20일 고가 돌파', '거래량 급증'],
                    metadata=dict(metadata))
    event = SignalEvent.from_signal(signal, source='s10a3a2')
    event.metadata = dict(metadata)
    event.strategy = strategy
    return event


def sell_signal(symbol='005930', *, price=PRICE, quantity=None):
    signal = Signal(symbol=symbol, side=OrderSide.SELL, strength=SignalStrength.STRONG,
                    strategy=StrategyType.SEPA_TREND, price=price, score=100.0,
                    confidence=1.0, reason='손절', metadata={})
    event = SignalEvent.from_signal(signal, source='s10a3a2')
    event.metadata = {} if quantity is None else {'quantity': quantity}
    return event


async def fixture(tmp_path, monkeypatch, *, ready=True, policy=None):
    """attach + gateway 설치 상태의 실큐 하네스. 반환은 locals() 사전이다.

    `policy` 는 게시 정책의 축별 덮어쓰기다(owner 만 엄격하게 만드는 자리).
    """
    monkeypatch.setenv('CALENDAR_SEASONALITY', '0')
    monkeypatch.setenv('VOL_TARGETING', '0')
    monkeypatch.setenv('TEAM_CONVICTION', '0')
    monkeypatch.setenv('ENTRY_PLAN_SHADOW', '0')
    _CLOCK['kst'] = NOW_KST
    # 시계 축. 마지막 두 줄은 함수 안 재임포트 경계다(_FrozenDatetime 독스트링 참고).
    monkeypatch.setattr(_session_module, 'datetime', _FrozenDatetime)
    monkeypatch.setattr(_engine_module, 'datetime', _FrozenDatetime)
    monkeypatch.setattr(_engine_module, 'date', _FrozenDate)
    monkeypatch.setattr(_cv_module, 'datetime', _FrozenDatetime)
    monkeypatch.setattr(_risk_module, 'datetime', _FrozenDatetime)
    monkeypatch.setattr(_risk_module, 'date', _FrozenDate)
    monkeypatch.setattr(_dtmod, 'datetime', _FrozenDatetime)
    monkeypatch.setattr(_dtmod, 'date', _FrozenDate)
    monkeypatch.setattr(_macro_module, 'is_macro_event_day', lambda: ('', False))
    siglog = _SigLogRecorder()
    monkeypatch.setattr(_engine_module._SigLog, 'get', staticmethod(lambda: siglog))
    modules_before = frozenset(sys.modules)

    from src.core.market_regime import MarketRegimeAdapter
    from src.execution.safety.regime_owner import POLICY_READS, RegimeBaseline, RegimeOwner
    from src.execution.safety.risk_transition import IntradayPolicyState
    from src.risk.manager import RiskManager as SidecarRiskManager
    sidecar = SidecarRiskManager(RiskConfig(), D('2000000'), 'KR')
    broker = broker_fake()
    engine, exits, store, runtime = await build_runtime(
        tmp_path, account_scope=ACCOUNT_SCOPE, risk_manager=sidecar,
        clock=lambda: _CLOCK['kst'])
    try:
        engine.broker = broker
        engine._market_regime = 'bull'
        risk = engine.config.risk
        # CLAUDE.md 의 KR 운영값(최소 포지션 20만원). 기본 RiskConfig 의 50만원은 US 쪽 값이라
        # 2백만원 자산에서는 모든 사이징이 바닥 클램프로 0 이 된다.
        risk.min_position_value = 200000
        if ready:
            monkeypatch.setattr(KRExecutionRuntime, 'trading_ready', property(lambda self: True))

        async def sector_lookup(symbol):
            return None

        stop_resolver = make_entry_stop_resolver(runtime.exit_manager, {
            'sepa_trend': {'stop_loss_pct': 5.0},
            'gap_and_go': {'stop_loss_pct': 3.5},
            'vcp_breakout': {'stop_loss_pct': 4.0},
        })
        rm = inner_risk_manager(engine, risk, sidecar=sidecar, exits=exits,
                                sector_lookup=sector_lookup, stop_resolver=stop_resolver)
        engine.risk_manager = rm
        sidecar.set_exit_manager(exits)

        # 자동 매수의 판단 사실은 항상 'regime' 출처를 인용하고 final 의 `_recheck_regime` 이 그
        # digest 를 state 에서 다시 유도한다 — regime_policy 가 없는 owner 는 소비 사실을 통째로
        # 거부한다(stale_regime_decision). 그래서 실제 RegimeOwner 를 설치한다.
        adapter = MarketRegimeAdapter()
        engine._regime_adapter = adapter

        def seed_policy(state):
            value = IntradayPolicyState('normal', 0.0, None, None).to_dict()
            state['intraday_policy'] = {'schema': 1, 'baseline': value,
                                        'baseline_version': runtime.owner.version + 1,
                                        'current': value.copy(), 'transitions': {}}
            state['entry_policy_effects'] = {'pending_sectors': {}, 'sidecar_active': True}
            return state

        await runtime.owner.mutate('s10a3a2-prerequisite', seed_policy)
        await RegimeOwner.register_baseline(runtime, RegimeBaseline.from_dict(regime_baseline(runtime)),
                                            expected_version=runtime.owner.version)
        await runtime.owner.register_policy_generations('s10a3a2-reads', POLICY_READS)

        async def missing_vix():
            return None

        regime_writer = RegimeOwner(runtime, adapter=adapter, sidecar=sidecar, vix_fetcher=missing_vix)
        vix_ticket = await regime_writer.sources.begin('s10a3a2-vix', 'vix_regime')
        await regime_writer.sources.complete(
            vix_ticket, 'success', {'value': 20.0, 'fetched_at': NOW_KST.isoformat()},
            source='s10a3a2-vix', source_event_id='s10a3a2-vix', received_at=NOW_KST)

        async def index_price(code):
            return bullish_index(code)

        trend = await regime_writer.refresh_trend(SimpleNamespace(fetch_index_price=index_price))
        assert trend.status == 'accepted'

        authority = EntryAuthority()
        alpha = [RiskSnapshot(1, 1, 'success', NOW_KST, 'normal')]
        session = [GuardDecision(True, 's10a3a2_open')]
        builder = KISRequestBuilder(RequestAccount(ACCOUNT_SCOPE, ACCOUNT_NO, PRODUCT_CD,
                                                   'prod', ENDPOINT, 1))
        commands = RequestBoundCommands(
            runtime, builder=builder, authority=authority,
            entry_guard=FinalEntryGuard(authority, lambda: alpha[0], lambda: _CLOCK['kst']),
            stop_resolver=stop_resolver, session_guard=lambda request: session[0])
        published_policy = effective_policy(risk, **(policy or {}))
        context = PolicyContext(
            business_day=NOW_KST.date(), observed_at=NOW_KST,
            versions=_policy.PolicyVersions(runtime.owner.version, runtime.owner.version,
                                            runtime.owner.version, runtime.owner.version,
                                            CONFIG_VERSION, 0, 0),
            policy=published_policy,
            sync=_policy.SyncPolicySnapshot(True, 0, None, 10),
            trend=_policy.MarketTrendPolicySnapshot(False, False, False),
            macro=_policy.MacroPolicySnapshot(None, False, False))
        await commands.publish_policy_context(context, expected_version=runtime.owner.version)
        gateway = SignalGateway(runtime, commands, exit_manager=exits, config_version=CONFIG_VERSION)
        runtime.install_gateway(gateway)

        prepared = []
        original_prepare = commands.prepare

        async def prepare_spy(request, entry_context, **kwargs):
            result = await original_prepare(request, entry_context, **kwargs)
            prepared.append(request)
            return result

        commands.prepare = prepare_spy

        # sidecar 도달·판정의 유일한 증거. 통과형 spy 라 제품 본문을 그대로 태운다.
        sidecar_calls = []
        original_can_open = sidecar.can_open_position

        def sidecar_spy(*args, **kwargs):
            verdict = original_can_open(*args, **kwargs)
            sidecar_calls.append((args[0], args[2], verdict))
            return verdict

        sidecar.can_open_position = sidecar_spy

        outcomes = []
        original_submit = gateway.submit

        async def submit_spy(event, order, evidence):
            result = await original_submit(event, order, evidence)
            outcomes.append(result)
            return result

        gateway.submit = submit_spy

        error_events = []

        async def drive(event):
            """실제 우선순위 힙에 싣고 큐가 빌 때까지 돈다."""
            await engine.emit(event)
            processed = []
            while engine._event_queue:
                queued = await engine._get_next_event()
                if queued is None:
                    break
                processed.append(queued)
                if isinstance(queued, ErrorEvent):
                    error_events.append(queued)
                await engine._process_event(queued)
            # `_log_sig` 의 fire-and-forget 태스크를 소진시킨다(도달 증거의 결정성).
            await asyncio.sleep(0)
            return processed

        def posts():
            return broker._session.posts

        def errors():
            return list(error_events)

        async def teardown():
            try:
                await runtime.shutdown()
            except ApplicationBlocked:
                pass
            await store.close()
            late = sorted(frozenset(sys.modules) - modules_before)
            assert not late, '시계 patch 창에서 새 모듈 import: %s' % late
            assert not _conftest.VIOLATIONS, _conftest.VIOLATIONS
    except BaseException:
        # 조립 도중 실패하면 열린 store 가 남는다 — 호출자는 아직 f 를 받지 못해
        # 자기 finally 에서 닫을 수 없다.
        await store.close()
        raise

    return locals()


async def seed(f, rows, *, cash, tag):
    """보유·현금을 owner 한 commit 으로 심는다(경제 DTO + 보호 DTO 를 같은 mutate 에서).

    `daily_start_unrealized_pnl` 을 심은 뒤의 미실현 합으로 맞추는 이유: 전일부터 들고 있던
    포지션의 평가손익이 `effective_daily_pnl` 에 그대로 실리면 sidecar 의 일일 손실 한도가
    우리가 보려는 게이트보다 먼저 막는다.
    """
    runtime, exits = f['runtime'], f['exits']

    def reduce(state):
        pf = f['engine'].portfolio
        for row in rows:
            position = Position(symbol=row['symbol'], quantity=row['quantity'],
                                avg_price=row['price'], current_price=row['price'],
                                strategy=row['strategy'])
            position.sector = row.get('sector')
            position.entry_time = NOW_KST - timedelta(days=row.get('entry_days_ago', 3))
            position.entry_signal_score = row.get('entry_score', 70.0)
            exits.register_position(position)
            pf.positions[row['symbol']] = position
        pf.cash = cash
        pf.daily_start_unrealized_pnl = pf.total_unrealized_pnl
        state['portfolio'] = encode_portfolio(pf)
        state['protection'] = encode_protection(exits)
        return state

    await runtime.owner.mutate('s10a3a2-seed-' + tag, reduce)


def sent_orders(f):
    """POST 본문을 (종목, side, 수량) 으로 편다."""
    return [(sent['json']['PDNO'], 'buy' if sent['headers']['tr_id'] == 'TTTC0802U' else 'sell',
             sent['json']['ORD_QTY']) for _, sent in f['posts']()]


def submits(f):
    """owner 의 SUBMIT 행을 종목으로 찾는다 — `attempts` 사전의 순서에 기대지 않는다."""
    return {row['symbol']: row for row in f['runtime'].owner.state['attempts'].values()
            if row['kind'] == 'submit'}


def assert_owner_alone(f, reason, *, sidecar_quantity):
    """owner 가 단독으로 막았다 — sidecar 는 같은 요청을 통과시켰다.

    `sidecar_quantity` 는 sidecar 에 실제로 넘어간 수량이다(사이징 0 으로 조기 종료된 것이
    아니라 위험 게이트까지 갔다는 증거).
    """
    assert f['posts']() == [] and f['prepared'] == []
    assert f['runtime'].owner.state['attempts'] == {}
    # sidecar 는 통과시켰다.
    assert len(f['sidecar_calls']) == 1
    symbol, quantity, verdict = f['sidecar_calls'][0]
    assert verdict[0] is True, verdict
    assert quantity == sidecar_quantity
    # engine 두 게이트도 통과했다 — 기록은 'passed' 다.
    assert f['siglog'].gates() == [('passed', None)]
    # 막은 층은 owner 다.
    errors = f['errors']()
    assert len(errors) == 1 and errors[0].source == 'on_signal'
    assert errors[0].error_type == 'CommandValidationError'
    assert errors[0].message == reason
    assert f['engine'].stats.errors_count == 1
    assert f['broker'].direct_calls == []
    assert f['engine']._pending_sector_map == {}


# ─────────────────────────── H — 하네스 자기 단언 ───────────────────────────

def test_h0_harness_self_assertions(tmp_path, monkeypatch):
    """나머지 전부의 근거. monkeypatch 없는 `trading_ready is False` 를 여기서만 단언한다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch, ready=False)
        try:
            runtime, engine, exits, rm = f['runtime'], f['engine'], f['exits'], f['rm']
            # 1) 설치 승인이 아니다 — 제품 property 는 patch 없이 False 다.
            assert runtime.trading_ready is False
            assert runtime.health()['trading_ready'] is False
            # 2) 단일 sidecar·별칭
            assert rm._risk_validator is runtime.risk_manager is f['sidecar']
            assert rm._exit_exempt_ref is exits._exit_exempt
            assert engine._execution_runtime is runtime and runtime.gateway is f['gateway']
            # 3) 시계가 같은 순간
            naive = NOW_KST.replace(tzinfo=None)
            assert runtime._now() == NOW_KST
            assert _engine_module.datetime.now() == naive
            assert _cv_module.datetime.now() == naive
            assert _risk_module.datetime.now() == naive
            assert _dtmod.datetime.now() == naive
            assert _engine_module.date.today() == _risk_module.date.today() == NOW_KST.date()
            # 4) 정규장·평일, 동결 날짜 == owner risk day
            assert engine.is_trading_hours() is True
            assert _session_at(NOW_KST) == 'regular'
            assert runtime.owner.state['risk']['day'] == NOW_KST.date().isoformat()
            assert runtime.day_admission_closed is False
            # 5) 기본 게시 정책은 공유 RiskConfig 와 같다(이 파일의 표본만 이 값을 조인다).
            published = PolicyContext.from_dict(runtime.owner.state['entry_policy_context']).policy
            risk = engine.config.risk
            assert published.max_positions == risk.max_positions == 5
            assert published.min_cash_reserve_pct == risk.min_cash_reserve_pct == 15.0
            assert published.max_positions_per_sector == risk.max_positions_per_sector == 3
            assert published.core_allocation_pct == risk.strategy_allocation['core_holding']
            assert published.sizing_mode == risk.sizing_mode == 'nominal'
            # 6) sidecar 는 owner 와 같은 임계를 **자기 설정**에서 읽는다(대조의 두 출처).
            assert f['sidecar'].config.max_positions == 5
            assert f['sidecar'].config.min_cash_reserve_pct == 15.0
            assert f['sidecar'].config.max_positions_per_sector == 3
            assert f['sidecar'].config is not risk
            # 7) 사이징 오버레이는 전부 1.0 이다(제품 함수를 직접 불러 확인).
            assert _macro_module.is_macro_event_day() == ('', False)
            assert _calendar_module.calendar_multiplier(NOW_KST.date(), 'kr')[0] == 1.0
            assert _vol_module.vol_targeting_multiplier('sepa_trend')[0] == 1.0
            assert _conviction_module.team_conviction_multiplier('005930')[0] == 1.0
            assert f['posts']() == [] and f['broker'].direct_calls == []
        finally:
            await f['teardown']()
    asyncio.run(scenario())


# ────────────────── A — owner 단독 결정 4건 (게시값만 엄격하게) ──────────────────

# A1 의 자산: 코어 40만(40주) + 현금 160만 = 자산 200만.
#   코어 예약 = 200만×30% − 40만 = 20만 → 비코어 풀 = 180만
#   사이징   = min(180만×25%, 200만×35%, (160만 − 200만×5%) − 20만) = 45만 → 45주
#   sidecar 검사 4: 현금 160만 ≥ 200만×15% = 30만 → 통과
#   owner    검사 : 현금 160만 < 200만×90% = 180만 → `minimum_cash_reserve`
A1_HELD = ({'symbol': '017670', 'quantity': 40, 'price': PRICE, 'strategy': 'core_holding'},)


def test_a1_owner_minimum_cash_reserve_blocks_what_the_sidecar_passed(tmp_path, monkeypatch):
    """최소 현금 축 — 게시본 90% 가 sidecar 의 15% 를 덮지 않고 그 뒤에서 단독으로 막는다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch, policy={'min_cash_reserve_pct': 90.0})
        try:
            await seed(f, A1_HELD, cash=D('1600000'), tag='a1')
            assert f['engine'].portfolio.total_equity == D('2000000')
            await f['drive'](buy_signal('012330'))
            assert_owner_alone(f, 'minimum_cash_reserve', sidecar_quantity=45)
            # 두 층이 같은 사실에 다른 임계를 쓴다(대조의 정본은 게시본이다).
            assert f['sidecar'].config.min_cash_reserve_pct == 15.0
            snapshot = f['commands']._snapshot(f['runtime'].owner.state)
            assert snapshot.policy.min_cash_reserve_pct == 90.0
            assert snapshot.portfolio.cash == D('1600000')
        finally:
            await f['teardown']()
    asyncio.run(scenario())


# A2 의 자산: 비코어 3건 × 20만 = 60만 + 현금 140만 = 자산 200만.
#   코어 예약 = 60만(코어 보유 0) → 비코어 풀 = 140만 → 사이징 min(35만, 70만, 70만) = 35만 → 35주
#   sidecar 검사 3: 가중 3.0 < 자기 설정 5 → 통과
#   owner    검사 : 가중 3.0 >= 게시본 2 → `position_slot_limit`
A2_HELD = tuple({'symbol': symbol, 'quantity': 20, 'price': PRICE, 'strategy': 'gap_and_go'}
                for symbol in ('035420', '051910', '006400'))


def test_a2_owner_position_slot_limit_blocks_what_the_sidecar_passed(tmp_path, monkeypatch):
    """최대 포지션 수 축 — 같은 세 보유를 sidecar 는 여유로, owner 는 만석으로 읽는다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch, policy={'max_positions': 2})
        try:
            await seed(f, A2_HELD, cash=D('1400000'), tag='a2')
            assert f['engine'].portfolio.total_equity == D('2000000')
            await f['drive'](buy_signal('012330'))
            assert_owner_alone(f, 'position_slot_limit', sidecar_quantity=35)
            assert f['sidecar'].config.max_positions == 5
            snapshot = f['commands']._snapshot(f['runtime'].owner.state)
            assert snapshot.policy.max_positions == 2
            # 가중치는 두 층이 같은 보호 상태에서 만든다 — 다른 것은 임계뿐이다.
            assert sum(_policy.position_weight(fact) for fact in snapshot.portfolio.positions) == 3.0
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_a3_owner_same_day_stop_loss_blocks_what_the_live_sidecar_forgot(tmp_path, monkeypatch):
    """당일 손절 재진입 축 — 정본은 owner 장부이고 live set 의 누락이 그것을 열지 못한다.

    owner 의 `risk.stop_loss_today` 에 종목을 올린 뒤 **live sidecar 의 집합만** 비운다
    (legacy writer 가 만들 수 있는 모양이다). sidecar 는 그래서 통과시키지만 owner 는 같은
    SIGNAL 을 `rebound_missing_exit` 로 끝낸다. 그리고 그 뒤 첫 게시가 live 집합을 owner
    값으로 되돌린다 — 되돌림을 단언해 '누락이 살아남지 않는다'까지 고정한다.
    """
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            runtime, sidecar = f['runtime'], f['sidecar']

            def stopped(state):
                state['risk']['stop_loss_today'] = ['012330']
                return state

            await runtime.owner.mutate('s10a3a2-stop-loss', stopped)
            assert sidecar._stop_loss_today == {'012330'}
            sidecar._stop_loss_today = set()          # live 에만 생긴 누락
            await f['drive'](buy_signal('012330'))
            assert_owner_alone(f, 'rebound_missing_exit', sidecar_quantity=35)
            # 이번 판단 중의 첫 게시가 live 집합을 owner 값으로 되돌렸다.
            assert sidecar._stop_loss_today == {'012330'}
            assert runtime.owner.state['risk']['stop_loss_today'] == ['012330']
        finally:
            await f['teardown']()
    asyncio.run(scenario())


# A4 의 자산: 반도체 1건 20만 + 현금 180만 = 자산 200만.
#   코어 예약 60만 → 비코어 풀 140만 → 사이징 35만 → 35주
#   sidecar 검사 7: 섹터 1건 < 자기 설정 3 → 통과 / owner: 1 >= 게시본 1 → `sector_limit`
A4_HELD = ({'symbol': '005930', 'quantity': 20, 'price': PRICE, 'strategy': 'gap_and_go',
            'sector': '반도체'},)


def test_a4_owner_sector_limit_blocks_and_the_reason_cannot_name_the_gate(tmp_path, monkeypatch):
    """섹터 축 — owner 단독 거부이고, **두 판정 지점이 같은 사유 문자열**이라 구분 불가다.

    `evaluate_risk_manager` 와 `evaluate_engine` 의 섹터 거부가 모두
    `PolicyReason.SECTOR_LIMIT` 이라 호출자에게 올라오는 `CommandValidationError` 만으로는
    어느 쪽이 막았는지 알 수 없다. 같은 owner snapshot 에 두 제품 함수를 직접 걸어 그것을
    단언으로 남긴다(설치 단계의 관측 설계가 풀어야 할 부채다).
    """
    async def scenario():
        f = await fixture(tmp_path, monkeypatch, policy={'max_positions_per_sector': 1})
        try:
            await seed(f, A4_HELD, cash=D('1800000'), tag='a4')
            await f['drive'](buy_signal('012330', sector='반도체'))
            assert_owner_alone(f, 'sector_limit', sidecar_quantity=35)
            assert f['sidecar'].config.max_positions_per_sector == 3
            snapshot = f['commands']._snapshot(f['runtime'].owner.state)
            entry = _policy.EntryPolicyInput('012330', OrderSide.BUY, 35, PRICE,
                                             'sepa_trend', '반도체')
            first = _policy.evaluate_risk_manager(entry, snapshot, now=NOW_KST)
            second = _policy.evaluate_engine(entry, snapshot, reserved_cash=D('0'))
            assert first.reason is second.reason is _policy.PolicyReason.SECTOR_LIMIT
            assert first.reason.value == second.reason.value == 'sector_limit'
            # 구분되는 값은 legacy 사유뿐이고 그것은 owner 밖으로 나가지 않는다.
            assert first.legacy_reason != second.legacy_reason
        finally:
            await f['teardown']()
    asyncio.run(scenario())


# ─────────────────── B — 누적: 3종목·3섹터·2전략의 미해결 BUY ───────────────────

# 자산: 코어 60만(60주) + 비코어 30만(30주) + 현금 110만 = 200만.
#   코어 보유가 코어 예산 30% 와 같아 코어 예약 0 이고, 그때 사이징 풀은
#   equity − 코어 실점유 = 140만이다(`_calculate_position_size` 의 코어 초과 보호 분기).
#   가용 현금 = 110만 − 200만×5% = 100만 이 세 요청의 공통 출발점이다.
#   #1 sepa  005930 반도체 : min(140만×25%, 200만×35%, 100만) = 35만 → 35주, 예약 35만×1.015 = 355,250
#   #2 gap   000660 2차전지: min(140만×15%, 70만, 100만−355,250) = 21만 → 21주, 예약 213,150
#   #3 gap   035420 인터넷 : min(21만, 70만, 100만−568,400, gap 잔여) = 21만 → 21주, 예약 213,150
# 누적 예약 = 781,550 이고 남는 가용 현금은 218,450 — 네 번째 요청의 구속항이 된다.
#   #4 sepa  012330        : 가용이 비율 상한(35만)보다 작고, 시장가 여유 상한이 한 번 더
#                            걸려 수량은 int(218,450 / (10,000×1.3)) = 16 주다.
B_HELD = ({'symbol': '017670', 'quantity': 60, 'price': PRICE, 'strategy': 'core_holding'},
          # 전략 예산 표에 없는 전략이라 아래 두 전략의 잔여 예산을 건드리지 않는다.
          {'symbol': '105560', 'quantity': 30, 'price': PRICE, 'strategy': 'vcp_breakout'})
B_CASH = D('1100000')
B_PENDING = (('005930', StrategyType.SEPA_TREND, '반도체', '35', D('355250')),
             ('000660', StrategyType.GAP_AND_GO, '2차전지', '21', D('213150')),
             ('035420', StrategyType.GAP_AND_GO, '인터넷', '21', D('213150')))
B_RESERVED = D('781550')
B_AVAILABLE_LEFT = D('218450')


async def three_unresolved_buys(f):
    """세 종목·세 섹터·두 전략의 미해결 BUY 를 실큐로 만든다."""
    # gap_and_go 기본 예산 3.5%(7만)는 한 포지션도 못 만든다 — 제품 knob 하나만 올린다.
    f['rm'].config.strategy_allocation['gap_and_go'] = 42.0
    await seed(f, B_HELD, cash=B_CASH, tag='b-held')
    assert f['engine'].portfolio.total_equity == D('2000000')
    assert f['rm']._get_core_reserve() == D('0')
    for index, (symbol, strategy, sector, quantity, _) in enumerate(B_PENDING):
        if index:
            advance(31)
        await f['drive'](buy_signal(symbol, strategy=strategy, sector=sector))
    assert sent_orders(f) == [(symbol, 'buy', quantity)
                              for symbol, _, _, quantity, _ in B_PENDING]
    assert f['errors']() == []
    advance(31)


def test_b1_pending_reservations_sum_into_one_cash_bound(tmp_path, monkeypatch):
    """예약 합 — 세 미해결 BUY 의 예약이 한 합으로 다음 요청의 사이징을 구속한다.

    legacy 장부(`_reserved_by_order`)는 attach 에서 항상 비어 있으므로 그 합의 정본은
    `gateway.reserved_cash()` 뿐이다. 네 번째 BUY 의 전선 수량이 그 합을 뺀 잔여로 떨어지는
    것으로 고정한다 — 합에서 다른 attempt 가 빠지면 50주가 나간다.
    """
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            engine, gateway, rm = f['engine'], f['gateway'], f['rm']
            await three_unresolved_buys(f)
            rows = submits(f)
            assert {symbol: D(row['reserved_cash']) for symbol, row in rows.items()} == {
                symbol: reserved for symbol, _, _, _, reserved in B_PENDING}
            assert gateway.reserved_cash() == B_RESERVED == sum(
                (D(row['reserved_cash']) for row in rows.values()), D('0'))
            # 한 합의 정본이 하나다 — engine 의 예약 조회도 같은 값을 돌려준다.
            assert rm._reserved_cash == B_RESERVED
            assert rm._reserved_by_order == {} and rm._pending_strategy == {}
            # 전략별 필터: 두 전략으로 갈리고 그 합이 전체와 같다.
            assert gateway.pending_strategy_notional('sepa_trend') == D('355250')
            assert gateway.pending_strategy_notional('gap_and_go') == D('426300')
            assert gateway.pending_strategy_notional('vcp_breakout') == D('0')
            assert (gateway.pending_strategy_notional('sepa_trend')
                    + gateway.pending_strategy_notional('gap_and_go')) == B_RESERVED
            # 네 번째 BUY: 구속항은 누적 예약을 뺀 잔여다(전략 예산·비율 상한은 더 크다).
            assert engine.get_available_cash() - B_RESERVED == B_AVAILABLE_LEFT
            await f['drive'](buy_signal('012330'))
            assert sent_orders(f)[-1] == ('012330', 'buy', '16')
            assert D(submits(f)['012330']['request_binding']['resources']['exposure']) == D('160000')
            assert f['errors']() == []
            assert f['broker'].direct_calls == []
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_b2_pending_buys_fill_the_owner_position_slots(tmp_path, monkeypatch):
    """pending 포함 최대 포지션 — 비코어 보유 1건 + 미해결 BUY 3건이 슬롯 4를 채운다.

    sidecar 의 슬롯 계산은 **보유**만 센다(attach 에서 legacy pending 장부는 비어 있다).
    owner 는 미해결 BUY 를 수량 0 의 포지션 사실로 덧붙여 같은 슬롯을 만석으로 읽는다.
    """
    async def scenario():
        f = await fixture(tmp_path, monkeypatch, policy={'max_positions': 4})
        try:
            runtime = f['runtime']
            await three_unresolved_buys(f)
            f['sidecar_calls'].clear()
            f['siglog'].rows.clear()
            await f['drive'](buy_signal('012330'))
            assert_owner_alone_after_pending(f, 'position_slot_limit', sidecar_quantity=16)
            snapshot = f['commands']._snapshot(runtime.owner.state)
            assert len(snapshot.pending) == 3
            assert sorted(fact.symbol for fact in snapshot.pending) == sorted(
                symbol for symbol, _, _, _, _ in B_PENDING)
            # sidecar 가 센 것은 비코어 보유 한 건뿐이다 — 같은 순간에 1.0/5 와 4.0/4 로 갈린다.
            non_core = [fact for fact in snapshot.portfolio.positions
                        if fact.strategy != 'core_holding']
            assert len(non_core) == 1
            assert sum(_policy.position_weight(fact) for fact in non_core) == 1.0
            assert f['sidecar'].config.max_positions == 5
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_b3_pending_buys_fill_the_owner_sector_limit(tmp_path, monkeypatch):
    """pending 으로 채워지는 섹터 한도 — 같은 섹터의 미해결 BUY 한 건이 한도를 채운다.

    engine 의 `_pending_sector_map` 은 attach 에서 매번 비워지므로 미해결 섹터의 정본은
    owner 의 효과 원장이고, 판정에 실리는 것은 그 원장이 아니라 `_evaluate` 가 덧붙이는
    pending 포지션 사실이다(두 지점이 같은 섹터를 각각 센다).
    """
    async def scenario():
        f = await fixture(tmp_path, monkeypatch, policy={'max_positions_per_sector': 1})
        try:
            engine, runtime = f['engine'], f['runtime']
            await three_unresolved_buys(f)
            assert runtime.owner.state['entry_policy_effects']['pending_sectors'] == {
                symbol: sector for symbol, _, sector, _, _ in B_PENDING}
            assert engine._pending_sector_map == {}
            f['sidecar_calls'].clear()
            f['siglog'].rows.clear()
            await f['drive'](buy_signal('012330', sector='반도체'))
            assert_owner_alone_after_pending(f, 'sector_limit', sidecar_quantity=16)
            # sidecar 의 섹터 계산에는 그 한 건이 없다 — 보유 중 반도체는 0 건이다.
            assert not any(position.sector == '반도체'
                           for position in engine.portfolio.positions.values())
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def assert_owner_alone_after_pending(f, reason, *, sidecar_quantity):
    """B2·B3 의 공통 단언 — 미해결 BUY 세 건은 그대로 두고 이번 요청만 owner 가 끝냈다."""
    assert len(f['posts']()) == 3 and len(f['prepared']) == 3
    assert len(submits(f)) == 3
    assert len(f['sidecar_calls']) == 1
    symbol, quantity, verdict = f['sidecar_calls'][0]
    assert verdict[0] is True, verdict
    assert quantity == sidecar_quantity
    assert f['siglog'].gates() == [('passed', None)]
    errors = f['errors']()
    assert len(errors) == 1 and errors[0].source == 'on_signal'
    assert errors[0].error_type == 'CommandValidationError'
    assert errors[0].message == reason
    assert f['broker'].direct_calls == []


# ─────────────── C — UNKNOWN 1건의 정지 범위(차단 사유 12)와 면제 충돌 ───────────────

def test_c1_one_unknown_stops_the_protective_sell_and_the_cancel_too(tmp_path, monkeypatch):
    """`blocked_unknown` 인 **SUBMIT** 한 행 뒤에는 **보호 SELL 과 CANCEL 도** 거부된다.

    `_evaluate` 의 미해결 증거 검사는 **새 요청**의 side·kind·종목을 가리지 않는다. 다만 전역
    정지를 일으키는 **기존 행**은 SUBMIT 뿐이다 — 기존 비-SUBMIT 행은 그 검사에서 제외되고
    (같은 종목의 새 SUBMIT 만 `unresolved_child_attempt` 로 막는다), 이 표본이 만드는 것도
    BUY SUBMIT 의 UNKNOWN 이다(Codex 6차의 범위 한정). 자동 매수만 멈춘다고
    읽으면 설치 판단이 실제보다 안전해 보인다 — 여기서 그 범위를 SELL·CANCEL 로 고정한다.
    """
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            runtime, commands = f['runtime'], f['commands']
            await seed(f, ({'symbol': '035420', 'quantity': 20, 'price': PRICE,
                            'strategy': 'gap_and_go'},), cash=D('1800000'), tag='c1')
            # ① 정상 ACK 한 건 — 이것이 뒤에서 CANCEL 의 부모가 된다.
            await f['drive'](buy_signal('005930'))
            assert f['outcomes'][0].status is CommandStatus.ACKNOWLEDGED
            parent_row = submits(f)['005930']
            parent_request = f['prepared'][0]
            # ② 주문번호 없는 ACK 한 건 — owner 에 `blocked_unknown` 이 남는다.
            f['broker'].ack_output = None
            advance(31)
            await f['drive'](buy_signal('000660'))
            unknown = submits(f)['000660']
            assert unknown['state'] == 'blocked_unknown'
            assert f['outcomes'][1].reason_code == 'ack_identity_missing_or_invalid'
            posts_before = len(f['posts']())
            errors_before = len(f['errors']())
            # ③ 보호 SELL SIGNAL — 다른 종목인데도 같은 사유로 끝난다.
            f['broker'].best_bid = 9800
            advance(31)
            await f['drive'](sell_signal('035420', price=D('9800'), quantity=20))
            assert len(f['posts']()) == posts_before
            errors = f['errors']()[errors_before:]
            assert len(errors) == 1 and errors[0].error_type == 'CommandValidationError'
            assert errors[0].message == 'unresolved_execution_evidence'
            # ④ CANCEL prepare — 부모가 멀쩡해도 같은 사유다.
            cancel = f['builder'].prepare_cancel(
                intent_id=parent_request.intent_id, attempt_id='s10a3a2-cancel',
                session=parent_request.session,
                parent=CancelParent(parent_request.intent_id, parent_request.attempt_id,
                                    parent_row['version'], OrderRef.from_dict(parent_row['order_ref']),
                                    parent_request.symbol, parent_request.side,
                                    parent_request.order_type, parent_row['reserved_quantity'],
                                    parent_request.valuation_price, parent_request.strategy))
            context = commands.authority.automatic(cancel.symbol, cancel.side.value, cancel.strategy)
            with pytest.raises(CommandValidationError, match='unresolved_execution_evidence'):
                await commands.prepare(cancel, context)
            assert len(f['posts']()) == posts_before
            assert 's10a3a2-cancel' not in runtime.owner.state['attempts']
            assert f['broker'].direct_calls == []
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_c2_runtime_exit_exemption_blocks_every_command_and_the_next_publication_drops_it(
        tmp_path, monkeypatch):
    """런타임 `add_exit_exempt` 의 실제 결말 — 다음 명령은 막히고 다음 게시가 그것을 지운다.

    owner 를 거치지 않고 live `ExitManager` 에 면제를 넣으면 `_owner_ready` 의 보호 등식이
    깨져 **그 SIGNAL 하나가 통째로** `legacy_protection_writer_conflict` 로 끝난다(첫 소비
    지점은 예약 현금 조회다). 그리고 owner 의 다음 게시는 병합이 아니라 교체라서 그 면제는
    조용히 사라진다 — 설치자는 면제 추가 경로를 owner 로 옮겨야 한다.
    """
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            runtime, exits, rm = f['runtime'], f['exits'], f['rm']
            await seed(f, ({'symbol': '005930', 'quantity': 20, 'price': PRICE,
                            'strategy': 'gap_and_go'},), cash=D('1800000'), tag='c2')
            before = runtime.owner.version
            exits.add_exit_exempt('005930', reason='런타임 면제 등록')
            assert rm._exit_exempt_ref == {'005930'}
            assert runtime.owner.state['protection']['exit_exempt'] == []
            await f['drive'](buy_signal('012330'))
            assert f['posts']() == [] and f['prepared'] == []
            assert runtime.owner.state['attempts'] == {}
            assert runtime.owner.version == before
            errors = f['errors']()
            assert len(errors) == 1 and errors[0].source == 'on_signal'
            assert errors[0].error_type == 'CommandValidationError'
            assert errors[0].message == 'legacy_protection_writer_conflict'
            # 다음 게시(owner 가 다른 종목을 면제로 등록)는 live 집합을 병합하지 않고 바꾼다.
            def exempt(state):
                state['protection']['exit_exempt'] = ['017670']
                return state

            await runtime.owner.mutate('s10a3a2-owner-exempt', exempt)
            assert rm._exit_exempt_ref is exits._exit_exempt
            assert rm._exit_exempt_ref == {'017670'}
            assert '005930' not in rm._exit_exempt_ref
            # 충돌이 사라졌으므로 같은 BUY 가 이제 전선까지 간다(양성 대조).
            advance(31)
            await f['drive'](buy_signal('012330'))
            assert len(f['posts']()) == 1
            assert f['posts']()[0][1]['json']['PDNO'] == '012330'
            assert len(f['errors']()) == 1
            assert f['broker'].direct_calls == []
        finally:
            await f['teardown']()
    asyncio.run(scenario())


# ─────────────────── D — 제품 순서의 attach 거부(legacy 장부) ───────────────────

def test_d1_attach_is_refused_while_a_legacy_pending_ledger_row_exists(tmp_path, monkeypatch):
    """`engine.risk_manager` 를 attach **앞**에 세우는 제품 순서에서만 이 가드가 하중된다.

    인수 하네스는 `test_execution_runtime.setup` 안에서 attach 가 먼저라 이 가드가 무하중
    이다. 제품 기동은 반대 순서이고, 행이 남은 채로 붙으면 다음 on_signal 의 90초 stale
    SELL 루프가 owner 를 거치지 않고 broker 를 직접 부른다. 그래서 여기서만 조립을 직접 한다.
    """
    async def scenario():
        engine = UnifiedEngine(TradingConfig(initial_capital=D('2000000')))
        exits = ExitManager(persist=False, clock=lambda: NOW_KST)
        store = ExecutionStateStore(tmp_path / 'execution' / 'state.sqlite3')
        try:
            state = {'portfolio': encode_portfolio(engine.portfolio),
                     'protection': encode_protection(exits),
                     'risk': new_risk_state(NOW_KST.date().isoformat()),
                     'lots': {}, 'outbox': {}, 'intents': {}, 'attempts': {},
                     'startup_reconciliation': {'status': 'blocked', 'reason': 'synthetic_baseline'}}
            await store.commit(0, state, 's10a3a2-baseline')
            runtime = KRExecutionRuntime(store, engine, exits, clock=lambda: NOW_KST,
                                         account_scope=ACCOUNT_SCOPE)
            await runtime.restore()
            legacy = SimpleNamespace(_pending_orders={'005930'}, _pending_timestamps={},
                                     _reserved_by_order={})
            engine.risk_manager = legacy          # 제품 순서: attach 보다 앞이다
            with pytest.raises(RuntimeError, match='legacy 미체결 장부'):
                runtime.attach()
            assert engine._execution_runtime is None
            assert engine._execution_accepting is False
            # 같은 행을 비우면 같은 runtime 이 실제로 붙는다(양성 대조).
            legacy._pending_orders.clear()
            runtime.attach()
            assert engine._execution_runtime is runtime
        finally:
            await store.close()
            # 이 시험만 `fixture` 를 쓰지 않아 teardown 의 격리 단언이 없었다 — 11/11 로 맞춘다.
            assert not _conftest.VIOLATIONS, _conftest.VIOLATIONS
    asyncio.run(scenario())
