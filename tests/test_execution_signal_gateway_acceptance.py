"""S5 wave 1 — attach 경로의 독립 인수 (하네스 + H0·A·B·C·E·F).

이 파일은 S3·S4 가 만든 시험 모듈을 하나도 import 하지 않는다. 제품 코드와 계획서
(`docs/superpowers/plans/2026-09-21-s5-independent-acceptance.md`)만 보고 하네스를 새로
조립한다. S3 이전의 조립층 중에서는 `test_execution_runtime.setup`(실제
engine·ExitManager·SQLite store·KRExecutionRuntime 을 세우는 코드) 하나만 쓴다.

**인수 GREEN 은 설치 승인이 아니다.** `KRExecutionRuntime.trading_ready` 는 제품에서 항상
False 이고(H0 이 monkeypatch 없이 단언한다) 이 파일의 모든 송신 표본은 합성 startup 허가
위에 있다. 진입 시세는 owner 의 `market_sources` 행 없이 gateway 의 `_quote` 가 첫 게시자가
되는 모양으로 고정한다 — 제품에 market source 결합이 아직 없기 때문이며, 그 결합은 10A3
factory 의 몫이다.

남긴 fake 와 이유:

- 브로커 HTTP: 실제로 KIS 로 나간다. 주문번호(ODNO)는 호출마다 증가한다(B2 가 그 요구를
  실제로 하중한다).
- `_SigLog.get`: 운영 PostgreSQL 로 나간다. `rm._log_sig` 가 아니라 그 안쪽 경계에서
  갈아끼워 제품 `_log_sig` 본문은 그대로 태운다.
- `_sector_lookup`: 운영 PostgreSQL(`kr_stock_master`)로 나간다.
- `trading_ready` property: 합성 startup 허가.
- 시계: 주입 clock 6축 + `datetime` 모듈 자신(아래 `_FrozenDatetime` 주석 참고) +
  `src.utils.macro_calendar.is_macro_event_day`.
- 사이징 오버레이 3종은 제품 스위치(`CALENDAR_SEASONALITY`/`VOL_TARGETING`/
  `TEAM_CONVICTION` = 0)로 끈다 — 모듈 상수가 import 시점에 실제 HOME 캐시 경로를 굳히므로
  `Path.home` patch 로는 닿지 않는다. 끈 결과가 배율 1.0 인 것은 H0 이 제품 함수를 직접
  불러 단언한다.
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
    Fill, MarketSession, Order, OrderSide, OrderType, RiskConfig, Signal, SignalStrength,
    StrategyType, TradingConfig,
)
from src.execution.broker.kis_kr import KISBroker, KISConfig
from src.execution.safety import risk_policy as _policy
from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.commands import CommandValidationError, RequestBoundCommands
from src.execution.safety.gateway import SignalGateway
from src.execution.safety.guards import (
    EntryAuthority, EntryOrigin, FinalEntryGuard, GuardDecision, RiskSnapshot,
)
from src.execution.safety.lifecycle import CommandStatus
from src.execution.safety.policy_snapshot import PolicyContext
from src.execution.safety.requests import KISRequestBuilder, RequestAccount, _session_at
from src.execution.safety.runtime import KRExecutionRuntime
from src.utils.stop_policy import make_entry_stop_resolver

from test_execution_runtime import setup as build_runtime

_KST = ZoneInfo('Asia/Seoul')

# 2026-09-18(금) 11:00 KST — `test_execution_runtime.setup` 이 owner risk day 를 이 날짜로
# 굳히므로 동결 날짜도 같아야 한다(다르면 `day_admission_closed`). 11:00 은 정규장이고
# CrossStrategyValidator 시간 규칙의 'other' 구간(장초반 감점·점심 보너스 모두 없음)이다.
NOW_KST = datetime(2026, 9, 18, 11, 0, tzinfo=_KST)
_CLOCK = {'kst': NOW_KST}

# 요청 본문에 실리는 계좌. 실제 KIS 계좌가 아니며 endpoint 는 제품이 강제하는 값이다.
ACCOUNT_SCOPE = 's5-accept-scope'
ACCOUNT_NO = '50123456'
PRODUCT_CD = '01'
ENDPOINT = 'https://openapi.koreainvestment.com:9443'
ORDER_PATH = '/uapi/domestic-stock/v1/trading/order-cash'
CONFIG_VERSION = 'cfg-s5-accept-1'


class _FrozenDatetime(datetime):
    """`datetime.now()` 만 동결한다. 인스턴스를 만들지 않으므로 `type(x) is datetime` 불변.

    engine·cross_validator·risk.manager·session 의 모듈 수준 `datetime` 에 꽂고, 나아가
    `datetime` 모듈 자신에도 꽂는다 — `cross_validator.validate` 는 함수 안에서
    `from datetime import datetime as _dt` 로 다시 import 해 `now_hm`(CV 시간 가드와
    `qualification._check_clock` 의 대조 축)을 읽으므로 모듈 patch 가 닿지 않는다.
    닿지 않으면 판단 버킷이 실제 벽시계에 따라 달라져 09:00~09:29 에는 자동 매수가 통째로
    차단된다. 창을 좁히기 위해 fixture teardown 이 'patch 창 동안 새 모듈 import 0' 을
    함께 단언한다.
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
    """동결 시계를 한 번에 전진시킨다(여섯 축이 같은 holder 를 읽는다)."""
    _CLOCK['kst'] = _CLOCK['kst'] + timedelta(seconds=seconds)


class _Response:
    """단회 POST 응답. 본문은 호출 시점에 만들어 ODNO 증가를 싣는다."""

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
    """실제 KISBroker 의 헤더 생성만 남기고 네트워크 경계를 대체한다.

    ODNO 는 호출마다 증가한다. `pinned_order_no` 를 세우면 같은 번호를 반복해 돌려준다
    (B2 가 쓰는 유일한 자리).
    """
    broker = object.__new__(KISBroker)
    broker.config = KISConfig(app_key='s5-accept-key', app_secret='s5-accept-secret',
                              account_no=ACCOUNT_NO, account_product_cd=PRODUCT_CD,
                              env='prod', base_url=ENDPOINT)
    broker._token = 's5-accept-token'
    broker._token_mgr = type('_Tok', (), {'_access_token': None})()
    broker._session = _Session(broker)
    broker.ack_output = {}          # ACK output 을 비우면 UNKNOWN 이 된다(B1)
    broker.pinned_order_no = None
    broker.best_bid = None
    broker.best_bid_error = None
    broker.direct_calls = []        # 취소/직접 주문 호출 기록 (E1)
    counter = {'n': 0}

    def next_body():
        if broker.ack_output is None:
            return {'rt_cd': '0', 'output': {}}
        if broker.pinned_order_no is not None:
            order_no = broker.pinned_order_no
        else:
            counter['n'] += 1
            order_no = '900000%04d' % counter['n']
        return {'rt_cd': '0', 'output': {'ODNO': order_no, 'KRX_FWDG_ORD_ORGNO': '01234',
                                         **broker.ack_output}}

    broker.next_body = next_body
    broker.peek_order_no = lambda: '900000%04d' % (counter['n'] + 1)

    async def connect():
        broker._session.closed = False
        return True

    async def ensure_token():
        broker._token = 's5-accept-token'
        return True

    async def hashkey(body):
        return 's5-accept-hash'

    async def rate_limit(tr_id):
        return None

    async def get_best_bid(symbol):
        if broker.best_bid_error is not None:
            raise broker.best_bid_error
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
    """engine 내부 RiskManager 를 `__init__` 없이 세우고 그 속성을 **명시적으로 전부** 채운다.

    `__init__` 은 LLM 매니저·TradeMemory·TradeWiki 를 만들어 네트워크/HOME 에 닿는다. 빠진
    속성은 `_try_evict_weakest_position` 의 광역 except 에 삼켜져 "보호 가드가 동작했다"로
    오독되므로 여기서 한 곳에 모아 적는다(H0 이 존재를 단언한다).
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


# engine 내부 RiskManager.__init__ 이 세우는 속성 중 attach 경로가 읽는 것들(H0).
REQUIRED_RM_ATTRS = (
    '_risk_validator', '_sector_lookup', '_cross_validator', '_resolve_entry_stop',
    '_order_fail_cooldown', '_COOLDOWN_SECONDS', '_last_signal_time',
    '_SIGNAL_COOLDOWN_SECONDS', '_LLM_CHECK_MIN', '_LLM_BYPASS_AT', '_LLM_REJECT_SIZE_MULT',
    '_REPLACEMENT_MIN_SCORE', '_REPLACEMENT_LAST_EVICT_TS', '_REPLACEMENT_COOLDOWN_SEC',
    '_last_cash_warn_time', '_pending_orders', '_pending_signal_cache', '_pending_exit_reasons',
    '_pending_timestamps', '_PENDING_TIMEOUT_SECONDS', '_pending_quantities', '_pending_sides',
    '_pending_fallback_count', '_kis_qty_mismatch_count', '_zombie_candidate_symbols',
    '_reserved_by_order', '_pending_strategy', '_exit_exempt_ref', '_stop_loss_today',
    '_pending_lock',
)


def effective_policy(risk: RiskConfig) -> _policy.EffectiveRiskPolicy:
    """owner 가 판정에 쓰는 정책. 공유 RiskConfig 에서 옮겨 적고 H0 이 그 대조를 단언한다.

    `core_allocation_pct` 는 engine `_get_core_reserve` 가 읽는
    `strategy_allocation['core_holding']` 과 같아야 두 층의 코어 예약이 갈리지 않는다.

    **`regime_min_cash_reserve_pct` 의 게시값은 쓰이지 않는다.** 이 하네스는 실제
    `MarketRegimeAdapter` 를 설치하므로 engine `get_available_cash()` 는 어댑터의
    `REGIME_PARAMS[체제]['min_cash_reserve_pct']` 를 읽고, owner 도 평가 시점에
    `build_owned_snapshot` 이 같은 표로 이 필드를 덮어쓴다. 즉 두 소비 지점은 RiskConfig 가
    아니라 레짐 표에서 만난다 — 최소 현금 축의 출처 일원화는 10A3 factory 의 몫이고, H0 은
    게시값이 아니라 **실제로 구속하는 값**을 단언한다.
    """
    return _policy.EffectiveRiskPolicy(
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
        'schema': 1, 'baseline_id': 's5-accept-1', 'account_scope': runtime.account_scope,
        'business_day': NOW_KST.date().isoformat(), 'generation': runtime._day_generation,
        'fence_id': runtime._day_fence_id,
        'evidence': {'source': 's5-accept', 'event_id': 's5-accept-1',
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


def buy_signal(symbol='005930', *, price=D('10000'), score=80.0,
               strategy=StrategyType.SEPA_TREND, indicators=True, sector=None):
    """CV 감점 0 을 목표로 한 매수 SIGNAL.

    지표를 전부 실어 '지표결손' 감점을 없애고, score 80 은 SEPA 90+ 추격매수 감점(>=90)과
    G4 LLM 이중검증 구간(85<=score<95)을 모두 피한다 — LLM 경로는 네트워크다.
    """
    metadata = {} if sector is None else {'sector': sector}
    if indicators:
        metadata['indicators'] = {'atr_pct': 3.0, 'per': 12.0, 'pbr': 1.1,
                                  'foreign_net_buy': 1000, 'inst_net_buy': 1000}
    signal = Signal(symbol=symbol, side=OrderSide.BUY, strength=SignalStrength.NORMAL,
                    strategy=strategy if strategy is not None else StrategyType.SEPA_TREND,
                    price=price, score=score, confidence=0.8,
                    reason='20일 고가 돌파, 거래량 급증', reasons=['20일 고가 돌파', '거래량 급증'],
                    metadata=dict(metadata))
    event = SignalEvent.from_signal(signal, source='s5-accept')
    event.metadata = dict(metadata)
    event.strategy = strategy
    return event


def sell_signal(symbol='005930', *, price=D('10000'), quantity=None):
    signal = Signal(symbol=symbol, side=OrderSide.SELL, strength=SignalStrength.STRONG,
                    strategy=StrategyType.SEPA_TREND, price=price, score=100.0,
                    confidence=1.0, reason='손절', metadata={})
    event = SignalEvent.from_signal(signal, source='s5-accept')
    event.metadata = {} if quantity is None else {'quantity': quantity}
    return event


async def fixture(tmp_path, monkeypatch, *, ready=True):
    """attach + gateway 설치 상태의 실큐 하네스. 반환은 locals() 사전이다."""
    monkeypatch.setenv('CALENDAR_SEASONALITY', '0')
    monkeypatch.setenv('VOL_TARGETING', '0')
    monkeypatch.setenv('TEAM_CONVICTION', '0')
    monkeypatch.setenv('ENTRY_PLAN_SHADOW', '0')
    _CLOCK['kst'] = NOW_KST
    # 시계 6축. 마지막 두 줄은 함수 안 재임포트 경계다(_FrozenDatetime 독스트링 참고).
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

    # 자동 매수의 판단 사실은 항상 'regime' 출처를 인용하고, final 의 `_recheck_regime` 은
    # 그 digest 를 state 에서 다시 유도한다 — regime_policy 가 없는 owner 는 소비 사실을
    # 통째로 거부한다(stale_regime_decision). 그래서 실제 RegimeOwner 를 설치한다.
    adapter = MarketRegimeAdapter()
    engine._regime_adapter = adapter

    def seed_policy(state):
        value = IntradayPolicyState('normal', 0.0, None, None).to_dict()
        state['intraday_policy'] = {'schema': 1, 'baseline': value,
                                    'baseline_version': runtime.owner.version + 1,
                                    'current': value.copy(), 'transitions': {}}
        state['entry_policy_effects'] = {'pending_sectors': {}, 'sidecar_active': True}
        return state

    await runtime.owner.mutate('s5-accept-prerequisite', seed_policy)
    await RegimeOwner.register_baseline(runtime, RegimeBaseline.from_dict(regime_baseline(runtime)),
                                        expected_version=runtime.owner.version)
    await runtime.owner.register_policy_generations('s5-accept-reads', POLICY_READS)

    async def missing_vix():
        return None

    regime_writer = RegimeOwner(runtime, adapter=adapter, sidecar=sidecar, vix_fetcher=missing_vix)
    vix_ticket = await regime_writer.sources.begin('s5-accept-vix', 'vix_regime')
    await regime_writer.sources.complete(
        vix_ticket, 'success', {'value': 20.0, 'fetched_at': NOW_KST.isoformat()},
        source='s5-accept-vix', source_event_id='s5-accept-vix', received_at=NOW_KST)

    async def index_price(code):
        return bullish_index(code)

    trend = await regime_writer.refresh_trend(SimpleNamespace(fetch_index_price=index_price))
    assert trend.status == 'accepted'

    authority = EntryAuthority()
    alpha = [RiskSnapshot(1, 1, 'success', NOW_KST, 'normal')]
    session = [GuardDecision(True, 's5_accept_open')]
    builder = KISRequestBuilder(RequestAccount(ACCOUNT_SCOPE, ACCOUNT_NO, PRODUCT_CD,
                                               'prod', ENDPOINT, 1))
    commands = RequestBoundCommands(
        runtime, builder=builder, authority=authority,
        entry_guard=FinalEntryGuard(authority, lambda: alpha[0], lambda: _CLOCK['kst']),
        stop_resolver=stop_resolver, session_guard=lambda request: session[0])
    context = PolicyContext(
        business_day=NOW_KST.date(), observed_at=NOW_KST,
        versions=_policy.PolicyVersions(runtime.owner.version, runtime.owner.version,
                                        runtime.owner.version, runtime.owner.version,
                                        CONFIG_VERSION, 0, 0),
        policy=effective_policy(risk),
        sync=_policy.SyncPolicySnapshot(True, 0, None, 10),
        trend=_policy.MarketTrendPolicySnapshot(False, False, False),
        macro=_policy.MacroPolicySnapshot(None, False, False))
    await commands.publish_policy_context(context, expected_version=runtime.owner.version)
    gateway = SignalGateway(runtime, commands, exit_manager=exits, config_version=CONFIG_VERSION)
    runtime.install_gateway(gateway)

    prepared = []
    original_prepare = commands.prepare

    async def prepare_spy(request, entry_context, **kwargs):
        """통과형 spy. prepare 도달과 그 뒤 주입 지점을 한 곳에 모은다."""
        result = await original_prepare(request, entry_context, **kwargs)
        prepared.append(request)
        for hook in after_prepare:
            await hook(request)
        return result

    after_prepare = []
    commands.prepare = prepare_spy

    bound_calls = []
    original_bound = commands._bound

    def bound_spy(*args, **kwargs):
        bound_calls.append(args[1].attempt_id)
        return original_bound(*args, **kwargs)

    commands._bound = bound_spy

    outcomes = []
    original_submit = gateway.submit

    async def submit_spy(event, order, evidence):
        result = await original_submit(event, order, evidence)
        outcomes.append(result)
        return result

    gateway.submit = submit_spy

    abandoned = []
    original_abandon = runtime.lifecycle.abandon_candidate

    async def abandon_spy(attempt_id, *, reason):
        abandoned.append((attempt_id, reason))
        return await original_abandon(attempt_id, reason=reason)

    runtime.lifecycle.abandon_candidate = abandon_spy

    error_events = []

    async def drive(event):
        """실제 우선순위 힙에 싣고 큐가 빌 때까지 돈다(축출의 재진입 SELL 포함)."""
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
            pass  # B2 처럼 owner 를 일부러 막은 표본은 종료 drain 이 막힌다.
        await store.close()
        late = sorted(frozenset(sys.modules) - modules_before)
        assert not late, '시계 patch 창에서 새 모듈 import: %s' % late
        assert not _conftest.VIOLATIONS, _conftest.VIOLATIONS

    return locals()


async def assert_no_direct_broker_calls(f):
    """E1: attach 경로는 broker 를 직접 부르지 않는다(취소 0·직접 주문 0)."""
    assert f['broker'].direct_calls == []


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
            # 3) 여섯 시계가 같은 순간
            naive = NOW_KST.replace(tzinfo=None)
            assert runtime._now() == NOW_KST
            assert _engine_module.datetime.now() == naive
            assert _cv_module.datetime.now() == naive
            assert _risk_module.datetime.now() == naive
            assert _session_module.datetime.now() == naive
            assert _dtmod.datetime.now() == naive
            assert _engine_module.date.today() == _risk_module.date.today() == NOW_KST.date()
            # 4) 정규장·평일·비휴장, 그리고 동결 날짜 == owner risk day
            assert engine.is_trading_hours() is True
            assert engine._get_current_session() is MarketSession.REGULAR
            assert _session_at(NOW_KST) == 'regular'
            assert NOW_KST.weekday() < 5
            assert runtime.owner.state['risk']['day'] == NOW_KST.date().isoformat()
            assert runtime.day_admission_closed is False
            # 5) 주입된 매크로 캘린더·오버레이 배율 (제품 함수를 직접 불러 확인)
            assert _macro_module.is_macro_event_day() == ('', False)
            assert _calendar_module.calendar_multiplier(NOW_KST.date(), 'kr')[0] == 1.0
            assert _vol_module.vol_targeting_multiplier('sepa_trend')[0] == 1.0
            assert _conviction_module.team_conviction_multiplier('005930')[0] == 1.0
            # 6) __init__ 속성이 전부 있고 값이 제품 기본과 같다
            for name in REQUIRED_RM_ATTRS:
                assert hasattr(rm, name), name
            assert rm._REPLACEMENT_COOLDOWN_SEC == 600
            assert rm._SIGNAL_COOLDOWN_SECONDS == 30
            assert rm._REPLACEMENT_MIN_SCORE == 85
            # 7) fake ODNO 는 호출마다 증가한다
            first, second = f['broker'].next_body(), f['broker'].next_body()
            assert first['output']['ODNO'] < second['output']['ODNO']
            # 8) 게시한 정책 임계가 공유 RiskConfig 에서 옮겨 적은 값과 같다
            published = PolicyContext.from_dict(
                runtime.owner.state['entry_policy_context']).policy
            risk = engine.config.risk
            assert published.max_positions == risk.max_positions
            assert published.max_position_pct == risk.max_position_pct
            assert published.min_cash_reserve_pct == risk.min_cash_reserve_pct
            # 최소 현금 축은 게시값이 아니라 레짐 표가 구속한다(A1 의 기대 수량이 이 값에 선다).
            regime_reserve = engine._get_regime_params()['min_cash_reserve_pct']
            assert regime_reserve == 5.0 != risk.min_cash_reserve_pct
            assert engine.get_available_cash() == (
                engine.portfolio.cash - engine.portfolio.total_equity * D('0.05'))
            assert published.daily_max_trades == risk.daily_max_trades
            assert published.max_daily_new_buys == risk.max_daily_new_buys
            assert published.max_positions_per_sector == risk.max_positions_per_sector
            assert published.core_allocation_pct == risk.strategy_allocation['core_holding']
            assert published.sizing_mode == risk.sizing_mode == 'nominal'
            assert published.hybrid_enabled is risk.hybrid.enabled is False
            assert f['broker']._session.posts == []
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


# ─────────────────────────── A — 정상 송신의 전선 값 ───────────────────────────

# A1 기대 수량 35주의 근거 (모두 제품 상수·공유 RiskConfig 에서 나온다):
#   equity = cash = 2,000,000 (setup 의 initial_capital)
#   core_reserve   = equity × strategy_allocation['core_holding'] 30% =   600,000
#   pool_equity    = equity − core_reserve                          = 1,400,000
#   base_pct       = strategy_position_pct[SEPA_TREND] 25% ,  강도 normal 배율 1.0
#   pct_value      = pool_equity × 0.25                             =   350,000
#   max_value      = equity × max_position_pct 35%                  =   700,000
#   available      = (cash − equity×레짐 표의 min_cash_reserve 5%) − 예약 0 − core_reserve
#                  = (2,000,000 − 100,000) − 600,000                = 1,300,000
#                    (출처는 RiskConfig 의 15% 가 아니라 `_get_regime_params()` — 실제
#                     MarketRegimeAdapter 를 설치했기 때문이다. H0 이 이 값을 단언한다)
#   position_value = min(350,000, 700,000, 1,300,000)               =   350,000
#   전략 예산 잔여 = equity × 42% − 보유 0 − pending 0               =   840,000 (비구속)
#   오버레이 4종(position/calendar/volatility/conviction) 전부 1.0 → 값 불변
#   quantity       = int(350,000 / 10,000) = 35 ,  시장가 여유 상한 int(1,300,000/13,000)=100
EXPECTED_BUY_QUANTITY = 35
EXPECTED_BUY_NOTIONAL = D('350000')


def test_a1_market_buy_reaches_the_wire_with_the_sized_quantity(tmp_path, monkeypatch):
    """사이징이 전선까지 갔다는 것을 건수가 아니라 본문으로 고정한다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            engine, runtime, gateway = f['engine'], f['runtime'], f['gateway']
            await f['drive'](buy_signal('005930', sector='반도체'))
            posts = f['posts']()
            assert len(posts) == 1
            url, sent = posts[0]
            assert url == ENDPOINT + ORDER_PATH
            assert sent['headers']['tr_id'] == 'TTTC0802U'           # KR 현금 매수
            assert sent['headers']['hashkey'] == 's5-accept-hash'
            assert sent['json'] == {
                'CANO': ACCOUNT_NO, 'ACNT_PRDT_CD': PRODUCT_CD, 'PDNO': '005930',
                'ORD_DVSN': '01', 'ORD_QTY': str(EXPECTED_BUY_QUANTITY), 'ORD_UNPR': '0',
                'CTAC_TLNO': '', 'SLL_TYPE': '', 'ALGO_NO': '',
            }
            # owner 에 SUBMIT 한 행, 예약은 gateway 가 보는 값과 같다.
            attempts = [row for row in runtime.owner.state['attempts'].values()
                        if row['kind'] == 'submit']
            assert len(attempts) == 1
            attempt = attempts[0]
            assert (attempt['symbol'], attempt['side'], attempt['quantity']) == ('005930', 'buy', 35)
            assert attempt['state'] == 'open' and attempt['command_status'] == 'acknowledged'
            assert attempt['request_binding']['valuation_price'] == '10000'
            assert D(attempt['request_binding']['resources']['exposure']) == EXPECTED_BUY_NOTIONAL
            assert gateway.reserved_cash() == D(attempt['reserved_cash']) > 0
            assert gateway.unresolved_symbols() == frozenset({'005930'})
            assert f['outcomes'][0].status is CommandStatus.ACKNOWLEDGED
            # H4: 송신 뒤 30초 쿨다운이 무장된다(다른 기록 지점은 사이징 0 거부뿐이다).
            assert f['rm']._last_signal_time['005930'] == NOW_KST.replace(tzinfo=None)
            # legacy 장부는 계속 비어 있다(결정 ④).
            assert f['rm']._pending_orders == set() and f['rm']._reserved_by_order == {}
            assert engine._pending_sector_map == {}
            assert ('passed', None) in f['siglog'].gates()
            assert f['errors']() == []
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_a2_limit_sell_uses_the_best_bid_and_falls_back_when_it_is_zero(tmp_path, monkeypatch):
    """실큐에서 `get_best_bid` 가지를 처음 태운다. bid=0 표본은 `event.price` 로 떨어진다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f, quantity=100)
            await seed_position(f, symbol='000660', quantity=50)
            f['broker'].best_bid = 10050
            await f['drive'](sell_signal('005930', price=D('10000'), quantity=40))
            posts = f['posts']()
            assert len(posts) == 1
            sent = posts[0][1]
            assert sent['headers']['tr_id'] == 'TTTC0801U'           # KR 현금 매도
            assert sent['json']['ORD_DVSN'] == '00' and sent['json']['SLL_TYPE'] == '01'
            assert sent['json']['ORD_UNPR'] == '10050'
            assert sent['json']['ORD_QTY'] == '40'
            # bid=0 은 호가 없음과 같게 취급돼 event.price 로 떨어진다. 같은 종목에 두 번째
            # 요청을 내면 owner 가 `unresolved_symbol_attempt` 로 먼저 막으므로 다른 종목을 쓴다.
            f['broker'].best_bid = 0
            advance(31)
            await f['drive'](sell_signal('000660', price=D('9900'), quantity=10))
            assert len(f['posts']()) == 2
            second = f['posts']()[1][1]
            assert second['json']['ORD_UNPR'] == '9900' and second['json']['ORD_QTY'] == '10'
            assert f['errors']() == []
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_a3_bid_lookup_failure_falls_back_to_the_signal_price(tmp_path, monkeypatch):
    """호가 조회 예외는 시장가로 번지지 않고 `event.price` 지정가로 떨어진다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f, quantity=100)
            f['broker'].best_bid_error = TimeoutError('synthetic quote timeout')
            await f['drive'](sell_signal('005930', price=D('9800'), quantity=20))
            assert len(f['posts']()) == 1
            sent = f['posts']()[0][1]
            assert sent['json']['ORD_DVSN'] == '00' and sent['json']['ORD_UNPR'] == '9800'
            assert sent['json']['ORD_QTY'] == '20'
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_a4_sell_without_any_price_is_refused_at_the_binding(tmp_path, monkeypatch):
    """사실 2 의 고정 — 실큐로는 MARKET SELL 이 owner 에 닿지 않는다(설치 차단 사유)."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f, quantity=100)
            before = f['runtime'].owner.version
            f['broker'].best_bid = None
            await f['drive'](sell_signal('005930', price=None, quantity=10))
            assert f['posts']() == []
            # 거부 지점이 gateway `_bind` 임을 고정한다 — prepare 도 owner 행도 없다.
            assert f['prepared'] == []
            assert f['runtime'].owner.state['attempts'] == {}
            assert f['runtime'].owner.version == before
            errors = f['errors']()
            assert len(errors) == 1 and errors[0].source == 'on_signal'
            assert 'invalid_request_price' in errors[0].message
            assert f['engine'].stats.errors_count == 1
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_a5_automatic_buy_without_evidence_publishes_nothing(tmp_path, monkeypatch):
    """전략 폴백('unknown')은 증거를 만들지 않는다 — 게시 0·prepare 0·POST 0."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            runtime = f['runtime']
            before = runtime.owner.version
            await f['drive'](buy_signal('005930', strategy=None))
            assert f['rm']._last_qualification_evidence is None
            assert f['outcomes'] == [None]
            assert f['posts']() == [] and f['prepared'] == []
            assert runtime.owner.state.get('entry_decision_facts', {}) == {}
            assert runtime.owner.state.get('entry_quotes', {}) == {}
            assert runtime.owner.version == before
            assert f['errors']() == []
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


# ─────────────────────────── B — UNKNOWN (무시험 칸) ───────────────────────────

def test_b1_unknown_ack_keeps_the_reservation_and_stops_the_attach_path(tmp_path, monkeypatch):
    """UNKNOWN 뒤에는 그 종목이 아니라 attach 경로 전체가 멈춘다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            runtime, gateway = f['runtime'], f['gateway']
            f['broker'].ack_output = None            # 주문번호 없는 ACK
            await f['drive'](buy_signal('005930'))
            assert len(f['posts']()) == 1
            assert f['outcomes'][0].status is CommandStatus.UNKNOWN
            assert f['outcomes'][0].reason_code == 'ack_identity_missing_or_invalid'
            attempt_id, attempt = next(
                (aid, row) for aid, row in runtime.owner.state['attempts'].items()
                if row['kind'] == 'submit')
            assert attempt['state'] == 'blocked_unknown'
            assert gateway.reserved_cash() > 0
            assert gateway.unresolved_symbols() == frozenset({'005930'})
            # 다른 종목의 매수도 멈춘다 — 종목별이 아니라 경로 전체다.
            f['broker'].ack_output = {}
            advance(31)
            await f['drive'](buy_signal('000660'))
            assert len(f['posts']()) == 1
            errors = f['errors']()
            assert len(errors) == 1
            assert 'unresolved_execution_evidence' in errors[0].message
            # 기동 sweep 은 이 행을 건드리지 않는다(prepared 가 아니다).
            assert f['engine'].running is False
            assert await gateway.recover_unsent() == []
            assert all(aid != attempt_id for aid, _ in f['abandoned'])
            assert runtime.owner.state['attempts'][attempt_id]['state'] == 'blocked_unknown'
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_b2_duplicate_order_number_is_refused_and_recorded_as_unknown(tmp_path, monkeypatch):
    """같은 주문번호를 두 번 받으면 두 번째 기록이 거부돼 owner 가 막힌다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            runtime = f['runtime']
            f['broker'].pinned_order_no = '9000009999'
            await f['drive'](buy_signal('005930'))
            assert f['outcomes'][0].status is CommandStatus.ACKNOWLEDGED
            advance(31)
            await f['drive'](buy_signal('000660'))
            assert len(f['posts']()) == 2
            assert f['outcomes'][1].status is CommandStatus.UNKNOWN
            assert f['outcomes'][1].reason_code == 'result_not_recorded'
            assert runtime.owner.healthy is False
            rows = [row for row in runtime.owner.state['attempts'].values()
                    if row['kind'] == 'submit']
            assert sorted(row['symbol'] for row in rows) == ['000660', '005930']
            assert [row['order_ref'] for row in rows if row['symbol'] == '000660'] == [None]
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


# ─────────────────────────── C — claim 이전 실패, 갈래별 ───────────────────────────

def test_c1_startup_barrier_releases_the_reservation_but_keeps_the_publications(tmp_path, monkeypatch):
    """② — 미송신은 예약을 전량 푼다. 그러면서 이번 판단의 게시 부산물은 남는다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch, ready=False)
        try:
            runtime = f['runtime']
            before = runtime.owner.version
            # 섹터를 실어야 아래 `_pending_sector_map == {}` 가 finally 정리(결정 ⑮)를 실제로 하중한다.
            await f['drive'](buy_signal('005930', sector='반도체'))
            assert f['posts']() == []
            result = f['outcomes'][0]
            assert result.status is CommandStatus.NOT_SENT
            assert result.reason_code == 'startup_reconciliation'
            attempt = next(row for row in runtime.owner.state['attempts'].values()
                           if row['kind'] == 'submit')
            assert attempt['state'] == 'final_rejected'
            assert attempt['command_status'] == 'not_sent'
            assert (D(attempt['reserved_cash']), attempt['reserved_quantity']) == (D('0'), 0)
            assert attempt['reserved_exposure'] == '0'
            assert attempt['reserved_planned_risk'] is None
            # 게시 부산물은 되감지 않는다(version 되감기 금지).
            facts = runtime.owner.state['entry_decision_facts']
            assert len(facts) == 1
            assert runtime.owner.state['qualification_sources']['regime']['version'] == 1
            assert runtime.owner.version > before
            assert f['engine']._pending_sector_map == {}
            # ready=True 로 바뀌면 같은 하네스가 실제로 보낸다(대조군).
            monkeypatch.setattr(KRExecutionRuntime, 'trading_ready', property(lambda self: True))
            advance(31)
            await f['drive'](buy_signal('000660'))
            assert len(f['posts']()) == 1
            assert f['outcomes'][1].status is CommandStatus.ACKNOWLEDGED
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_c2_claim_not_available_releases_the_reservation_without_posting(tmp_path, monkeypatch):
    """① — claim 이 False 면 예약 0·POST 0.

    ①의 유일한 제품 조건은 "같은 intent 의 비종료 submit sibling" 인데 gateway 의 intent 키가
    (종목·side·전략)이고 `_evaluate` 가 같은 종목의 미해결 시도를 먼저 거부하므로 실큐로는
    만들 수 없다. 그래서 여기서만 lifecycle.claim 을 False 로 세운다(인스턴스 경계).
    """
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            runtime = f['runtime']
            claims = []

            async def claim_false(attempt_id, claim_id, *, candidate_guard=None):
                claims.append(attempt_id)
                return False

            runtime.lifecycle.claim = claim_false
            await f['drive'](buy_signal('005930', sector='반도체'))
            assert f['posts']() == []
            assert len(claims) == 1
            result = f['outcomes'][0]
            assert result.status is CommandStatus.NOT_SENT
            assert result.reason_code == 'claim_not_available'
            assert f['abandoned'] == [(claims[0], 'claim_not_available')]
            attempt = runtime.owner.state['attempts'][claims[0]]
            assert attempt['state'] == 'final_rejected'
            assert (D(attempt['reserved_cash']), attempt['reserved_quantity']) == (D('0'), 0)
            assert f['gateway'].reserved_cash() == D('0')
            assert f['engine']._pending_sector_map == {}
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


@pytest.mark.parametrize('branch', ['blocked_at_claim', 'admission_boundary', 'store_failure'])
def test_c3_reservation_keeping_branches_never_post(tmp_path, monkeypatch, branch):
    """③④ — '보낼 수 없었다'가 아닌 갈래는 예약을 남긴다. POST 는 세 갈래 모두 0."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            runtime = f['runtime']
            entered = []

            async def claim_blocked(attempt_id, claim_id, *, candidate_guard=None):
                entered.append(attempt_id)
                raise ApplicationBlocked('synthetic_application_closing')

            async def claim_oserror(attempt_id, claim_id, *, candidate_guard=None):
                entered.append(attempt_id)
                raise OSError('synthetic store failure')

            if branch == 'blocked_at_claim':
                runtime.lifecycle.claim = claim_blocked
            elif branch == 'store_failure':
                runtime.lifecycle.claim = claim_oserror
            else:
                async def close_admission(request):
                    runtime._closing = True
                f['after_prepare'].append(close_admission)

            # 섹터를 실은 요청이어야 `_submit_signal` 의 finally 정리(결정 ⑮)가 실제로 하중된다.
            await f['drive'](buy_signal('005930', sector='반도체'))
            assert f['posts']() == []
            result = f['outcomes'][0]
            assert result.status is CommandStatus.NOT_SENT
            expected = ('dispatch_failed' if branch == 'store_failure'
                        else 'command_admission_closed')
            assert result.reason_code == expected
            attempt_id = f['prepared'][0].attempt_id
            attempt = runtime.owner.state['attempts'][attempt_id]
            assert attempt['state'] == 'prepared' and attempt['claim_id'] is None
            assert D(attempt['reserved_cash']) > 0 and attempt['reserved_quantity'] == 35
            assert f['abandoned'] == []
            # 분기 도달 증거: 명령 접수 경계에서 끝난 표본은 `_bound` 에 닿지도 않는다.
            if branch == 'admission_boundary':
                assert entered == [] and f['bound_calls'] == []
                # 접수가 닫힌 동안 예약 조회는 0 을 지어내지 않고 fail-closed 로 끝난다.
                with pytest.raises(ApplicationBlocked):
                    f['gateway'].reserved_cash()
                runtime._closing = False
            else:
                assert entered == [attempt_id] and f['bound_calls'] == [attempt_id]
            assert f['gateway'].reserved_cash() == D(attempt['reserved_cash'])
            assert f['engine']._pending_sector_map == {}
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_c4_cancelled_dispatch_leaves_one_row_for_the_startup_sweep(tmp_path, monkeypatch):
    """⑤ — 결과 없이 끝난 행만 `recover_unsent()` 가 쓸고, 엔진이 도는 중엔 거부한다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            runtime, gateway = f['runtime'], f['gateway']

            async def claim_cancelled(attempt_id, claim_id, *, candidate_guard=None):
                raise asyncio.CancelledError()

            runtime.lifecycle.claim = claim_cancelled
            # 섹터를 실은 요청이어야 `_submit_signal` 의 finally 정리(결정 ⑮)가 실제로 하중된다.
            await f['engine'].emit(buy_signal('005930', sector='반도체'))
            event = await f['engine']._get_next_event()
            with pytest.raises(asyncio.CancelledError):
                await f['engine']._process_event(event)
            await asyncio.sleep(0)
            assert f['posts']() == []
            assert f['engine']._pending_sector_map == {}
            attempt_id = f['prepared'][0].attempt_id
            assert runtime.owner.state['attempts'][attempt_id]['state'] == 'prepared'
            assert gateway.reserved_cash() > 0
            # 엔진이 도는 중의 sweep 은 거부된다.
            f['engine'].running = True
            with pytest.raises(CommandValidationError,
                               match='gateway_recover_requires_stopped_engine'):
                await gateway.recover_unsent()
            f['engine'].running = False
            assert await gateway.recover_unsent() == [attempt_id]
            assert f['abandoned'] == [(attempt_id, 'startup_unclaimed')]
            attempt = runtime.owner.state['attempts'][attempt_id]
            assert attempt['state'] == 'final_rejected'
            assert (D(attempt['reserved_cash']), attempt['reserved_quantity']) == (D('0'), 0)
            assert gateway.reserved_cash() == D('0')
            assert await gateway.recover_unsent() == []
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_c5_abandon_failure_preserves_the_reason_and_keeps_the_loop_alive(tmp_path, monkeypatch):
    """abandon 내부 실패는 사유를 바꾸지 않고 예약만 남기며, 다음 SIGNAL 은 정상 처리된다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            runtime = f['runtime']

            async def abandon_broken(attempt_id, *, reason):
                raise OSError('synthetic abandon failure')

            runtime.lifecycle.abandon_candidate = abandon_broken

            async def close_session(request):
                f['session'][0] = GuardDecision(False, 'synthetic_session_closed')

            f['after_prepare'].append(close_session)
            await f['drive'](buy_signal('005930', sector='반도체'))
            assert f['posts']() == []
            result = f['outcomes'][0]
            assert result.status is CommandStatus.NOT_SENT
            assert result.reason_code == 'synthetic_session_closed'
            attempt_id = f['prepared'][0].attempt_id
            attempt = runtime.owner.state['attempts'][attempt_id]
            assert attempt['state'] == 'prepared' and attempt['claim_id'] is None
            assert D(attempt['reserved_cash']) > 0
            # 루프는 살아 있다 — 다음 SIGNAL 이 실제로 나간다.
            f['session'][0] = GuardDecision(True, 's5_accept_open')
            f['after_prepare'].clear()
            runtime.lifecycle.abandon_candidate = f['original_abandon']
            advance(31)
            await f['drive'](buy_signal('000660'))
            assert len(f['posts']()) == 1
            assert f['outcomes'][1].status is CommandStatus.ACKNOWLEDGED
            assert f['engine']._pending_sector_map == {}
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


# ─────────────────────────── E·F — 취소 0 과 legacy 불변 ───────────────────────────

def test_e1_legacy_ledger_after_attach_refuses_the_signal_without_cancelling(tmp_path, monkeypatch):
    """H7 — bind 뒤 legacy 장부가 남으면 그 SIGNAL 을 거부한다(90초 stale 취소 루프 미도달).

    이 진술은 엔진 경로에 한정된다 — `KRScheduler._cleanup_stale_pending` 은 P0-4 S-D 가
    넣은 가드로 attach 에서는 취소를 내지 않는다(`tests/test_execution_p04_stale_pending.py`
    가 고정; 이 문장은 S5 당시 "attach 를 모른 채 취소를 낸다(10C)"였다 — 단언 무수정).
    """
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            rm, engine = f['rm'], f['engine']
            # 90초를 넘긴 미체결 SELL 이 남아 있는 관리자.
            rm._pending_orders.add('005930')
            rm._pending_sides['005930'] = OrderSide.SELL
            rm._pending_timestamps['005930'] = NOW_KST.replace(tzinfo=None) - timedelta(seconds=120)
            await f['drive'](buy_signal('000660'))
            errors = f['errors']()
            assert len(errors) == 1 and errors[0].error_type == 'RuntimeError'
            assert 'legacy 미체결 장부' in errors[0].message
            assert f['posts']() == []
            assert f['prepared'] == []
            await assert_no_direct_broker_calls(f)
            # legacy writer 가드 세 곳 전부 명시 거부다. 포지션·현금을 직접 쓰는 것은
            # `update_position` 하나라 그것이 빠지면 가장 비싼 가드가 무보호가 된다(독립 재현 P1).
            with pytest.raises(ApplicationBlocked):
                await rm.on_order(OrderEvent.from_order(
                    Order(symbol='005930', side=OrderSide.BUY, order_type=OrderType.MARKET,
                          quantity=1, price=D('10000')), source='legacy'))
            with pytest.raises(ApplicationBlocked):
                engine.update_position_price('005930', D('10100'))
            cash_before = engine.portfolio.cash
            with pytest.raises(ApplicationBlocked):
                engine.update_position(Fill(order_id='legacy-fill', symbol='005930',
                                            side=OrderSide.BUY, quantity=1, price=D('10000')))
            assert engine.portfolio.cash == cash_before
            assert '005930' not in engine.portfolio.positions
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_f1_legacy_engine_still_posts_through_the_broker_and_attach_refuses_the_same_order(
        tmp_path, monkeypatch):
    """legacy 경로는 불변(broker.submit_order 1회)이고 attach 는 같은 ORDER 를 거부한다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            legacy_engine = UnifiedEngine(TradingConfig(initial_capital=D('2000000')))
            legacy_broker = broker_fake()
            legacy_engine.broker = legacy_broker
            legacy_engine._market_regime = 'bull'
            legacy_exits = f['exits']
            legacy_rm = inner_risk_manager(
                legacy_engine, legacy_engine.config.risk, sidecar=None, exits=legacy_exits,
                sector_lookup=f['sector_lookup'], stop_resolver=f['stop_resolver'])
            legacy_engine.risk_manager = legacy_rm
            legacy_engine.portfolio.positions['005930'] = position_dto(quantity=100)
            await legacy_engine.emit(sell_signal('005930', price=D('9700'), quantity=30))
            order_events = []
            while legacy_engine._event_queue:
                event = await legacy_engine._get_next_event()
                order_events.append(event)
                await legacy_engine._process_event(event)
            await asyncio.sleep(0)
            submits = [call for call in legacy_broker.direct_calls if call[0] == 'submit_order']
            assert len(submits) == 1
            order = submits[0][1]
            assert (order.symbol, order.side, order.quantity) == ('005930', OrderSide.SELL, 30)
            assert (order.order_type, order.price) == (OrderType.LIMIT, D('9700'))
            assert legacy_broker._session.posts == []          # legacy 는 transport 를 쓰지 않는다
            # 대조: attach 된 엔진에 같은 모양의 ORDER 를 넣으면 조용히 폐기되지 않는다.
            before = f['engine'].stats.errors_count
            await f['engine']._process_event(OrderEvent.from_order(order, source='legacy'))
            assert f['engine'].stats.errors_count == before + 1
            # 거부는 legacy 핸들러에 닿기 전에 끝난다 — ErrorEvent 0(큐가 비어 있다)·POST 0.
            assert f['engine']._event_queue == []
            assert f['errors']() == []
            assert f['posts']() == []
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_f2_attach_is_refused_while_a_legacy_ledger_row_exists(tmp_path, monkeypatch):
    """H6 — 실제 inner RiskManager 를 붙인 엔진은 장부가 남은 채로 설치되지 않는다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            other = UnifiedEngine(TradingConfig(initial_capital=D('2000000')))
            other.broker = broker_fake()
            other_rm = inner_risk_manager(
                other, other.config.risk, sidecar=None, exits=f['exits'],
                sector_lookup=f['sector_lookup'], stop_resolver=f['stop_resolver'])
            other.risk_manager = other_rm
            other_rm._reserved_by_order['005930'] = D('1015000')
            with pytest.raises(RuntimeError, match='legacy 미체결 장부'):
                other.bind_execution_runtime(f['runtime'])
            assert other._execution_runtime is None
            # 장부를 비우면 그 가드는 지나가고 다음 가드(다른 엔진)가 잡는다 — 순서 증거.
            other_rm._reserved_by_order.clear()
            with pytest.raises(ValueError, match='다른 엔진'):
                other.bind_execution_runtime(f['runtime'])
            assert other._execution_runtime is None
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


# ─────────────────────────── 보조 (포지션 심기) ───────────────────────────

def position_dto(*, symbol='005930', quantity=100, price=D('10000'), strategy='sepa_trend'):
    from src.core.types import Position
    position = Position(symbol=symbol, quantity=quantity, avg_price=price,
                        current_price=price, strategy=strategy)
    return position


async def seed_position(f, *, symbol='005930', quantity=100, price=D('10000'),
                        strategy='sepa_trend'):
    """실제 owner 경로로 보유를 심는다(경제·보호 게시가 함께 커밋된다)."""
    runtime, exits = f['runtime'], f['exits']
    from src.execution.safety.economics import encode_portfolio
    from src.execution.safety.protection import encode_protection

    def reduce(state):
        pf = f['engine'].portfolio
        position = position_dto(symbol=symbol, quantity=quantity, price=price, strategy=strategy)
        exits.register_position(position)
        pf.positions[symbol] = position
        pf.cash -= price * quantity
        state['portfolio'] = encode_portfolio(pf)
        state['protection'] = encode_protection(exits)
        return state

    await runtime.owner.mutate('s5-accept-seed:' + symbol, reduce)


async def seed_portfolio(f, rows, *, cash=None, daily_trades=None, tag='many'):
    """여러 보유·현금·당일 거래 수를 owner 한 commit 으로 심는다(wave 2).

    `seed_position` 과 같은 경로(경제 DTO + 보호 DTO 를 같은 mutate 에서)이고, 축출 후보를
    서로 다르게 만들기 위해 진입 점수·현재가·진입 시각·섹터를 행마다 받는다. 현금은
    초기자본과 독립한 상태 값이라 그대로 적는다(기존 보유·입금의 재현이며, 예약·손익은
    owner 가 이 값으로 다시 계산한다).

    `daily_start_unrealized_pnl` 을 심은 뒤의 미실현 합으로 맞추는 이유: 전일부터 들고 있던
    손실 포지션은 장 시작 시점에 이미 그 손실을 갖고 있었다. 맞추지 않으면
    `effective_daily_pnl` 이 보유 손실 전체가 돼 sidecar 의 **일일 손실 한도**(검사 2)가
    우리가 보려는 검사 3(최대 포지션 수)보다 먼저 막는다.
    """
    runtime, exits = f['runtime'], f['exits']
    from src.execution.safety.economics import encode_portfolio
    from src.execution.safety.protection import encode_protection

    def reduce(state):
        pf = f['engine'].portfolio
        for row in rows:
            position = position_dto(symbol=row['symbol'], quantity=row['quantity'],
                                    price=row['avg_price'], strategy=row['strategy'])
            position.current_price = row.get('current_price', row['avg_price'])
            position.sector = row.get('sector')
            position.entry_time = NOW_KST - timedelta(days=row.get('entry_days_ago', 0))
            position.entry_signal_score = row.get('entry_score')
            exits.register_position(position)
            pf.positions[row['symbol']] = position
        if cash is not None:
            pf.cash = cash
        if daily_trades is not None:
            # 경제 원장의 당일 체결 수는 두 곳에 있고 owner 가 둘을 대조한다
            # (`policy_owned_day_or_counter_mismatch`) — 한쪽만 적으면 판정 자체가 거부된다.
            pf.daily_trades = daily_trades
            state['risk']['daily_stats']['trades'] = daily_trades
        pf.daily_start_unrealized_pnl = pf.total_unrealized_pnl
        state['portfolio'] = encode_portfolio(pf)
        state['protection'] = encode_protection(exits)
        return state

    await runtime.owner.mutate('s5-accept-seed-' + tag, reduce)


# ─────────────────── D·G — eviction 과 실제 게이트 (wave 2) ───────────────────

# D 의 만석 표본: 비코어 보유 5건(제품 기본 `RiskConfig.max_positions == 5`) + 코어 1건.
#   · 손실 비코어 두 건의 진입 점수가 같은 70 이고 손익이 다르다 → 정렬 키 **두 축**
#     (점수 → 손익)이 각각 하중된다. 최약 후보는 051910(점수 70·손익 -20%)이다.
#   · 006400 은 **비코어 최저 점수(60) + 수익(+10%)** 이다. '승자 킬 금지' 가드를 지우면
#     점수 60 이 정렬 1순위를 이겨 이 행이 나간다 — 그 가드에 하중을 거는 유일한 행이다.
#   · 017670 은 **코어(점수 0·손익 -50%)** 다. '코어 제외' 가드를 지우면 점수 0·최대 손실로
#     이 행이 나간다. 비코어 5건은 그대로 둬 만석(가중 5.0/5) 조건을 깨지 않는다.
#   · 진입 시각이 3일 전인 이유: sidecar 의 '일일 신규 매수 한도'(당일 진입 ≥5)가 우리가
#     보려는 '최대 포지션 수 도달'보다 먼저 막는다. 전일부터 보유한 포지션이 정상 표본이다.
#   · 전략이 gap_and_go 인 이유: 신호(sepa_trend)의 전략 예산 게이트에 걸리지 않게 한다.
# 자산: 현금 1,010,000 + 시가 990,000 = 2,000,000. 코어 예약 = 2,000,000×30% − 코어 시가
# 100,000 = 500,000 이므로 신호 사이징은 (2,000,000−500,000)×25% = 375,000 이고 최소 포지션
# 금액 200,000 을 넘는다 — 사이징 0 으로 조기 종료되지 않는다.
FULL_HOUSE = (
    {'symbol': '035420', 'quantity': 20, 'avg_price': D('10000'), 'current_price': D('9500'),
     'strategy': 'gap_and_go', 'entry_score': 70.0, 'entry_days_ago': 3},
    {'symbol': '051910', 'quantity': 20, 'avg_price': D('10000'), 'current_price': D('8000'),
     'strategy': 'gap_and_go', 'entry_score': 70.0, 'entry_days_ago': 3},
    {'symbol': '006400', 'quantity': 20, 'avg_price': D('10000'), 'current_price': D('11000'),
     'strategy': 'gap_and_go', 'entry_score': 60.0, 'entry_days_ago': 3},
    {'symbol': '105560', 'quantity': 20, 'avg_price': D('10000'), 'current_price': D('8500'),
     'strategy': 'gap_and_go', 'entry_score': 76.0, 'entry_days_ago': 3},
    {'symbol': '055550', 'quantity': 20, 'avg_price': D('10000'), 'current_price': D('7500'),
     'strategy': 'gap_and_go', 'entry_score': 78.0, 'entry_days_ago': 3},
    {'symbol': '017670', 'quantity': 20, 'avg_price': D('10000'), 'current_price': D('5000'),
     'strategy': 'core_holding', 'entry_score': 0.0, 'entry_days_ago': 3},
)
FULL_HOUSE_CASH = D('1010000')
FULL_HOUSE_EQUITY = D('2000000')
WEAKEST = '051910'          # 점수 70 중 손실이 더 큰 쪽
SECOND_WEAKEST = '035420'   # 점수 70·손실 -5%
CORE_DECOY = '017670'       # 코어 가드가 없으면 뽑힐 행(점수 0·손실 -50%)
WINNER_DECOY = '006400'     # 승자 킬 금지 가드가 없으면 뽑힐 행(점수 60·수익 +10%)
NEW_SYMBOL = '012330'       # 만석을 밀고 들어오는 신규 후보(보유에 없다)
# 축출을 부르는 조건: sidecar 가 '최대 포지션 수 도달'을 내고 점수가 `_REPLACEMENT_MIN_SCORE`
# (85) 이상. 88 은 sepa_trend 90+ 추격매수 감점(>=90)과 G4 LLM 구간을 모두 피한다.
REPLACEMENT_SCORE = 88.0


async def full_house(f):
    """만석 + 재진입 후보를 세운다. 반환은 그 신규 BUY 를 만드는 함수다."""
    await seed_portfolio(f, FULL_HOUSE, cash=FULL_HOUSE_CASH, tag='full-house')
    return lambda: buy_signal(NEW_SYMBOL, score=REPLACEMENT_SCORE)


def sell_posts(f):
    """POST 본문 중 매도만 골라 (종목, 수량, 지정가) 로 편다."""
    return [(sent['json']['PDNO'], sent['json']['ORD_QTY'], sent['json']['ORD_UNPR'])
            for _, sent in f['posts']() if sent['headers']['tr_id'] == 'TTTC0801U']


def test_d1_eviction_sells_the_weakest_candidate_through_the_gateway(tmp_path, monkeypatch):
    """만석에 막힌 고점수 BUY 가 최약 후보의 SELL 을 **전선까지** 보낸다.

    attach 에서 축출은 취소에 의존하지 않는 유일한 보호 경로다(S4 결정 ⑥). 그러므로
    "축출 시그널을 발행했다"가 아니라 POST 본문이 그 후보의 매도인지로 고정한다.
    """
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            engine, runtime, rm = f['engine'], f['runtime'], f['rm']
            new_buy = await full_house(f)
            assert engine.portfolio.total_equity == FULL_HOUSE_EQUITY
            await f['drive'](new_buy())
            # 1) 전선 값: 최약 후보의 지정가 매도 한 건뿐이다. 코어(점수 0·-50%)도 승자
            #    (점수 60·+10%)도 나가지 않는다 — 둘 다 최약 후보보다 앞서는 정렬 값이다.
            assert sell_posts(f) == [(WEAKEST, '20', '8000')]
            assert len(f['posts']()) == 1
            sent = f['posts']()[0][1]
            assert sent['json']['ORD_DVSN'] == '00' and sent['json']['SLL_TYPE'] == '01'
            await assert_no_direct_broker_calls(f)
            # 2) 원 BUY 는 같은 사이클에 G3_risk 로 끝난다(주문 0).
            assert f['siglog'].gates() == [('blocked', 'G3_risk')]
            assert '최대 포지션 수 도달' in f['siglog'].rows[0]['block_reason']
            assert NEW_SYMBOL not in engine.portfolio.positions
            # 두 미끼 행의 정렬 값이 실제로 희생자보다 앞선다(가드가 없으면 뽑힌다).
            core, winner = (engine.portfolio.positions[CORE_DECOY],
                            engine.portfolio.positions[WINNER_DECOY])
            victim = engine.portfolio.positions[WEAKEST]
            assert core.strategy == 'core_holding'
            assert core.entry_signal_score < victim.entry_signal_score
            assert float(core.unrealized_pnl_pct) < float(victim.unrealized_pnl_pct) < 0
            assert winner.entry_signal_score < victim.entry_signal_score
            assert float(winner.unrealized_pnl_pct) > 0
            # 3) owner 에는 그 SELL 한 행만 있다.
            rows = [row for row in runtime.owner.state['attempts'].values()
                    if row['kind'] == 'submit']
            assert len(rows) == 1
            assert (rows[0]['symbol'], rows[0]['side'], rows[0]['quantity']) == (WEAKEST, 'sell', 20)
            assert rows[0]['command_status'] == 'acknowledged'
            assert f['gateway'].unresolved_symbols() == frozenset({WEAKEST})
            # 4) 전역 상한이 무장된다(D2 의 전제).
            assert list(rm._REPLACEMENT_LAST_EVICT_TS) == [WEAKEST]
            assert rm._REPLACEMENT_LAST_EVICT_TS[WEAKEST] == NOW_KST.replace(tzinfo=None)
            assert f['errors']() == [] and engine._pending_sector_map == {}
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_d1_unresolved_sell_on_the_weakest_moves_the_eviction_to_the_next(tmp_path, monkeypatch):
    """H9 — 미해결 종목의 정본은 owner 다. 최약 후보가 이미 나가 있으면 다음이 나간다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            new_buy = await full_house(f)
            # 최약 후보를 실제로 한 번 내보내 owner 에 미해결 SELL 을 만든다.
            await f['drive'](sell_signal(WEAKEST, price=D('8000'), quantity=20))
            assert f['gateway'].unresolved_symbols() == frozenset({WEAKEST})
            await f['drive'](new_buy())
            assert sell_posts(f) == [(WEAKEST, '20', '8000'), (SECOND_WEAKEST, '20', '9500')]
            assert f['gateway'].unresolved_symbols() == frozenset({WEAKEST, SECOND_WEAKEST})
            assert f['siglog'].gates() == [('blocked', 'G3_risk')]
            assert f['errors']() == []
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_d2_global_replacement_cooldown_blocks_the_second_eviction(tmp_path, monkeypatch):
    """H10 ① — 쿨다운 안에는 전역 1건. 조용한 실패와 구분하려고 양성 대조를 붙인다.

    기록은 **후보 목록에 없는 제3 종목**으로 심는다. 희생자 종목으로 심으면 후보 루프의
    종목별 쿨다운이 같은 결과를 내서 전역 상한을 지워도 시험이 살아남는다.
    """
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            rm = f['rm']
            new_buy = await full_house(f)
            assert '111111' not in f['engine'].portfolio.positions
            rm._REPLACEMENT_LAST_EVICT_TS['111111'] = (
                NOW_KST.replace(tzinfo=None) - timedelta(seconds=300))
            await f['drive'](new_buy())
            # 차단 표본: 축출 0·POST 0. 분기 도달 증거는 BUY 가 G3_risk 까지 갔다는 것이다.
            assert f['posts']() == [] and f['prepared'] == []
            assert f['siglog'].gates() == [('blocked', 'G3_risk')]
            assert f['runtime'].owner.state['attempts'] == {}
            assert list(rm._REPLACEMENT_LAST_EVICT_TS) == ['111111']
            assert f['errors']() == []
            # 양성 대조: 그 기록만 지우면 같은 fixture 가 실제로 축출한다.
            rm._REPLACEMENT_LAST_EVICT_TS.clear()
            await f['drive'](new_buy())
            assert sell_posts(f) == [(WEAKEST, '20', '8000')]
            assert f['siglog'].gates() == [('blocked', 'G3_risk'), ('blocked', 'G3_risk')]
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_d2_replacement_requires_five_point_edge(tmp_path, monkeypatch):
    """H10 ② — +5 우위 경계. 88 은 84 를 밀어내지 못하고 83 은 밀어낸다.

    여섯 보유의 진입 점수를 한 값으로 맞춘다 — 경계를 보려면 최약 후보의 점수가 그 값이어야
    하고, 그러면 정렬 2순위(손실 큰 순)가 후보를 고른다(손실 -25% 의 055550). 점수를 맞추면
    코어 미끼(-50%)가 손실 1위가 되므로 이 표본은 코어 제외 가드에도 하중을 건다.
    """
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            runtime = f['runtime']
            new_buy = await full_house(f)

            def rescore(value):
                def reduce(state):
                    for row in state['portfolio']['positions'].values():
                        row['entry_signal_score'] = value
                    return state
                return reduce

            # 88 < 84 + 5 → 축출 없음.
            await runtime.owner.mutate('s5-accept-edge-84', rescore(84.0))
            await f['drive'](new_buy())
            assert f['posts']() == [] and f['prepared'] == []
            assert f['siglog'].gates() == [('blocked', 'G3_risk')]
            assert f['rm']._REPLACEMENT_LAST_EVICT_TS == {}
            # 88 >= 83 + 5 → 손실이 가장 큰 후보가 나간다(양성 대조).
            await runtime.owner.mutate('s5-accept-edge-83', rescore(83.0))
            await f['drive'](new_buy())
            assert sell_posts(f) == [('055550', '20', '7500')]
            assert f['errors']() == []
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_d3_owner_exit_exempt_symbol_is_never_evicted(tmp_path, monkeypatch):
    """(a) owner 의 `protection.exit_exempt` 종목은 축출 후보에서 빠진다.

    양성 대조는 구조가 대신한다 — 축출 자체는 일어나고 **다음** 후보가 나간다.
    """
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            runtime, rm = f['runtime'], f['rm']
            new_buy = await full_house(f)

            def exempt(state):
                state['protection']['exit_exempt'] = [WEAKEST]
                return state

            await runtime.owner.mutate('s5-accept-exempt', exempt)
            assert rm._exit_exempt_ref == {WEAKEST}
            await f['drive'](new_buy())
            assert sell_posts(f) == [(SECOND_WEAKEST, '20', '9500')]
            assert WEAKEST in f['engine'].portfolio.positions
            assert f['errors']() == []
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_d3_exit_exempt_alias_survives_publication_and_live_only_rows_do_not(
        tmp_path, monkeypatch):
    """(b) 별칭 불변식 — 사실 5 의 GREEN 고정.

    `publish_protection` 은 RiskManager 가 참조하는 **같은 set 객체**를 유지한 채 내용을
    owner DTO 로 교체한다. 그래서 런타임에 live set 에만 넣은 종목(`add_exit_exempt` 의
    모양)은 다음 게시에 사라진다 — 설치자는 추가 경로를 owner 로 옮겨야 한다.
    """
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            runtime, rm, exits = f['runtime'], f['rm'], f['exits']
            await seed_position(f, symbol='005930', quantity=20)
            before = exits._exit_exempt
            exits._exit_exempt.add('999999')      # live 에만 추가한 종목
            assert rm._exit_exempt_ref == {'999999'}

            def exempt(state):
                state['protection']['exit_exempt'] = ['005930']
                return state

            await runtime.owner.mutate('s5-accept-alias', exempt)
            assert exits._exit_exempt is before
            assert rm._exit_exempt_ref is exits._exit_exempt
            assert rm._exit_exempt_ref == {'005930'}
            assert rm._exit_exempt_ref == set(runtime.owner.state['protection']['exit_exempt'])
            assert '999999' not in rm._exit_exempt_ref
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


@pytest.mark.parametrize('consumer', ['cash_gate', 'strategy_budget_gate', 'eviction'])
def test_d4_owner_read_failure_is_fail_closed_at_every_consumer(tmp_path, monkeypatch, consumer):
    """owner 를 못 읽으면 세 소비 지점이 각각 SIGNAL 하나를 명시 거부로 끝낸다.

    0 을 지어내거나(현금·전략 예산) 축출만 건너뛴 채 통과하면(eviction) 가장 비싼 가드가
    무보호가 된다. 현금 게이트 표본은 전략 cap 을 0 으로 두어 뒤 게이트가 같은 예외를 내
    가리지 않게 하고, 나머지 둘은 앞 게이트가 실제로 통과했음을 호출 기록으로 단언한다.
    """
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            runtime, gateway, rm = f['runtime'], f['gateway'], f['rm']
            calls = []
            blocked = ApplicationBlocked('synthetic_owner_read_failure')

            def spy(name, original):
                def wrapped(*args):
                    calls.append(name)
                    return original(*args)
                return wrapped

            def broken(name):
                def wrapped(*args):
                    calls.append(name)
                    raise blocked
                return wrapped

            gateway.reserved_cash = spy('cash', gateway.reserved_cash)
            if consumer == 'cash_gate':
                # 실제 owner 장애로 읽는다 — 접수가 닫히면 `_owner_ready` 가 예외를 낸다.
                runtime._closing = True
                # 전략 예산 게이트를 통째로 건너뛰게 해 사유가 겹치지 않게 한다.
                rm.config.strategy_allocation['sepa_trend'] = 0.0
                event = buy_signal('012330')
            elif consumer == 'strategy_budget_gate':
                gateway.pending_strategy_notional = broken('budget')
                event = buy_signal('012330')
            else:
                gateway.unresolved_symbols = broken('eviction')
                await full_house(f)
                event = buy_signal(NEW_SYMBOL, score=REPLACEMENT_SCORE)
            await f['drive'](event)
            assert f['posts']() == [] and f['prepared'] == []
            assert runtime.owner.state['attempts'] == {}
            errors = f['errors']()
            assert len(errors) == 1 and errors[0].source == 'on_signal'
            assert errors[0].error_type == 'ApplicationBlocked'
            assert f['engine'].stats.errors_count == 1
            if consumer == 'cash_gate':
                assert calls == ['cash']
                # `_owner_ready` → `_require_day_admission` 의 fail-closed 사유 그대로다.
                assert errors[0].message == 'day_transition_admission_closed'
                runtime._closing = False
            else:
                # 앞 게이트는 실제로 통과했다 — 현금 게이트가 owner 를 읽고 0 을 받았다.
                assert errors[0].message == 'synthetic_owner_read_failure'
                assert calls[0] == 'cash' and calls.count('cash') >= 1
                broken_name = 'budget' if consumer == 'strategy_budget_gate' else 'eviction'
                assert calls[-1] == broken_name and calls.count(broken_name) == 1
            assert f['engine']._pending_sector_map == {}
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


# G1 의 두 표본은 **실제 sidecar `can_open_position`** 의 서로 다른 거부다.
#   · min_cash: 검사 4(최소 현금 보유 미달) — 현금 < 자산×15%(sidecar RiskConfig 기본).
#   · short_cash: 검사 6(현금 부족) — 검사 4 는 통과하고 요구 금액이 sidecar 의 가용 현금을
#     넘는다. engine 사이징은 레짐 표 5% 를 빼고 sidecar 는 자기 설정 15% 를 빼기 때문에
#     같은 자산에서 두 층의 가용 현금이 갈린다(사실 7 — 출처 일원화는 10A3).
# 두 표본 모두 코어 보유로 `_get_core_reserve()` 를 0 으로 만들어(코어 예산 초과) 앞의
# 현금 게이트·사이징이 0 으로 끝나지 않게 한다. 현금은 owner 에 그대로 심는다.
#
# 사유는 **전문**으로 적는다. '현금 부족' 은 sidecar(검사 6)와 engine `can_open_position`
# 이 같은 접두사를 쓰므로 접두사만 보면 막은 층을 구분하지 못한다(독립 재현 P2). 괄호 안
# 가용 현금이 두 층을 가른다 — sidecar 는 자기 설정 15%(1,600,000 − 10,000,000×15%
# = 100,000), engine 은 레짐 표 5%(= 1,100,000)를 뺀다. 요구액은 사이징 수량 × 10,000 ×
# 1.001 이고, 그 수량은 아래 시험이 sidecar 에 실제로 넘어간 값으로 함께 단언한다.
G1_CASES = {
    'min_cash': {'core_quantity': 900, 'cash': D('1000000'), 'quantity': 25,
                 'reason': '최소 현금 보유 미달 (1,000,000 < 1,500,000)'},
    'short_cash': {'core_quantity': 840, 'cash': D('1600000'), 'quantity': 40,
                   'reason': '현금 부족 (100,000 < 400,400)'},
}


@pytest.mark.parametrize('case', sorted(G1_CASES))
def test_g1_real_can_open_position_rejections_never_reach_the_owner(tmp_path, monkeypatch, case):
    """걷어낸 스텁 하나 — 실제 sidecar 의 거부가 G3_risk 로 끝나고 POST 0·owner 행 0."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            runtime, params = f['runtime'], G1_CASES[case]
            # 막은 층을 통과형 spy 로 고정한다 — sidecar 가 거부했고 engine 게이트는
            # 도달 0 이다(같은 '현금 부족' 접두사를 쓰는 두 층의 구분).
            sidecar_calls, engine_calls = [], []
            original_sidecar = f['sidecar'].can_open_position
            original_engine = f['engine'].can_open_position

            def sidecar_spy(*args, **kwargs):
                verdict = original_sidecar(*args, **kwargs)
                sidecar_calls.append((args[2], verdict))
                return verdict

            def engine_spy(*args, **kwargs):
                engine_calls.append(args[0])
                return original_engine(*args, **kwargs)

            f['sidecar'].can_open_position = sidecar_spy
            f['engine'].can_open_position = engine_spy
            await seed_portfolio(f, ({'symbol': '005930', 'quantity': params['core_quantity'],
                                      'avg_price': D('10000'), 'strategy': 'core_holding',
                                      'entry_days_ago': 3},),
                                 cash=params['cash'], tag='g1-' + case)
            assert f['engine'].portfolio.total_equity == D('10000000')
            assert f['rm']._get_core_reserve() == D('0')
            before = runtime.owner.version
            await f['drive'](buy_signal('012330'))
            assert f['posts']() == [] and f['prepared'] == []
            assert runtime.owner.state['attempts'] == {}
            assert runtime.owner.version == before
            gates = f['siglog'].gates()
            assert gates == [('blocked', 'G3_risk')]
            reason = f['siglog'].rows[0]['block_reason']
            assert reason == params['reason'], reason
            # 거부한 층: sidecar 가 그 사유를 돌려줬고 engine 게이트는 도달 0 이다.
            assert engine_calls == []
            assert len(sidecar_calls) == 1
            quantity, verdict = sidecar_calls[0]
            assert verdict == (False, params['reason'])
            # 사이징은 양수였다 — 거부는 '포지션 크기 0' 이 아니라 위험 게이트다.
            assert quantity == params['quantity']
            assert f['rm']._last_sizing_inputs is not None
            assert f['errors']() == []
            assert f['engine']._pending_sector_map == {}
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_g1_same_day_stop_loss_block_rides_on_the_owner_published_sidecar(tmp_path, monkeypatch):
    """당일 손절 종목 재진입 금지 — owner 가 투영한 장부로 실제 sidecar 가 막는다.

    **단일 sidecar 전제의 행위 증거**다(wave 1 은 H0 자기 단언뿐이었다). `rm._risk_validator`
    가 owner 소유 객체가 아니면 `publish_risk` 투영을 못 받아 낡은 장부로 판정하고, 같은
    BUY 가 그대로 전선까지 나간다.
    """
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            runtime, sidecar = f['runtime'], f['sidecar']

            def stopped(symbols):
                def reduce(state):
                    state['risk']['stop_loss_today'] = list(symbols)
                    return state
                return reduce

            await runtime.owner.mutate('s5-accept-stop-loss', stopped(['012330']))
            assert sidecar._stop_loss_today == {'012330'}
            assert f['rm']._risk_validator is sidecar
            await f['drive'](buy_signal('012330'))
            assert f['posts']() == [] and f['prepared'] == []
            assert runtime.owner.state['attempts'] == {}
            assert f['siglog'].gates() == [('blocked', 'G3_risk')]
            assert f['siglog'].rows[0]['block_reason'].startswith('당일 손절 종목 재진입 금지')
            # 양성 대조: owner 에서 그 종목을 지우면 같은 BUY 가 실제로 나간다.
            await runtime.owner.mutate('s5-accept-stop-loss-clear', stopped([]))
            assert sidecar._stop_loss_today == set()
            await f['drive'](buy_signal('012330'))
            assert len(f['posts']()) == 1
            assert f['posts']()[0][1]['json']['PDNO'] == '012330'
            assert f['errors']() == []
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_g2_owner_blocks_the_daily_trade_limit_that_the_engine_no_longer_counts(
        tmp_path, monkeypatch):
    """attach 에서 engine 쪽 TOCTOU 보정은 0 을 더한다 — 경계를 넘기는 BUY 는 owner 가 막는다.

    engine 의 보정은 legacy `_pending_sides` 를 세는데 attach 에서 그 장부는 비어 있다
    (결정 ④). 미해결 BUY 1건 + 당일 체결 14건이면 engine 게이트는 통과하고(그래서 이번
    신호도 'passed' 가 기록된다) owner 의 `evaluate_engine` 이 막는다.
    """
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            engine, runtime, rm = f['engine'], f['runtime'], f['rm']
            limit = engine.config.risk.daily_max_trades
            await seed_portfolio(f, (), cash=D('2000000'), daily_trades=limit - 1, tag='g2-trades')
            assert engine.portfolio.daily_trades == limit - 1
            await f['drive'](buy_signal('005930'))
            assert len(f['posts']()) == 1
            advance(31)
            await f['drive'](buy_signal('000660'))
            # engine 의 두 게이트는 통과했다(passed 2건). 보정이 살아 있었다면 여기서 막혔다.
            assert f['siglog'].gates() == [('passed', None), ('passed', None)]
            assert rm._pending_sides == {} and rm._pending_orders == set()
            assert len(f['posts']()) == 1
            errors = f['errors']()
            assert len(errors) == 1 and errors[0].error_type == 'CommandValidationError'
            assert errors[0].message == 'daily_trade_limit'
            rows = [row for row in runtime.owner.state['attempts'].values()
                    if row['kind'] == 'submit']
            assert [row['symbol'] for row in rows] == ['005930']
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_g2_owner_blocks_the_sector_limit_that_the_engine_no_longer_counts(tmp_path, monkeypatch):
    """같은 짝 — 섹터 한도. engine 의 `_pending_sector_map` 은 attach 에서 항상 비어 있다.

    owner 는 미체결 BUY 를 **두 번** 센다: `commands._evaluate` 가 pending 을 포지션 사실로
    덧붙여 `evaluate_risk_manager` 의 섹터 검사에 실리고, `evaluate_engine` 은 따로
    `pending_sectors` 를 더한다. 그래서 한쪽만 지우는 변이는 이 표본에서 살아남는다(실측:
    두 지점 각각은 생존, 둘을 함께 지우면 RED). 이 시험이 고정하는 것은 "어느 한 줄"이 아니라
    **미체결을 센 채로 owner 가 막는다**는 결과다.
    """
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            engine, runtime = f['engine'], f['runtime']
            held = ({'symbol': '005930', 'quantity': 10, 'avg_price': D('10000'),
                     'strategy': 'gap_and_go', 'sector': '반도체', 'entry_days_ago': 3},
                    {'symbol': '000660', 'quantity': 10, 'avg_price': D('10000'),
                     'strategy': 'gap_and_go', 'sector': '반도체', 'entry_days_ago': 3})
            await seed_portfolio(f, held, cash=D('1800000'), tag='g2-sector')
            assert engine.config.risk.max_positions_per_sector == 3
            await f['drive'](buy_signal('012330', sector='반도체'))
            assert len(f['posts']()) == 1
            # 미체결 섹터의 정본은 owner 의 효과 원장이다(engine 의 map 은 매번 비워진다).
            assert (runtime.owner.state['entry_policy_effects']['pending_sectors']
                    == {'012330': '반도체'})
            advance(31)
            await f['drive'](buy_signal('105560', sector='반도체'))
            assert f['siglog'].gates() == [('passed', None), ('passed', None)]
            assert engine._pending_sector_map == {}
            assert len(f['posts']()) == 1
            errors = f['errors']()
            assert len(errors) == 1 and errors[0].error_type == 'CommandValidationError'
            assert errors[0].message == 'sector_limit'
            rows = [row for row in runtime.owner.state['attempts'].values()
                    if row['kind'] == 'submit']
            assert [row['symbol'] for row in rows] == ['012330']
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


@pytest.mark.parametrize('pile', [True, False])
def test_g2_owner_pending_buys_fill_the_strategy_budget_cap(tmp_path, monkeypatch, pile):
    """같은 짝 — 전략 예산 캡. 캡의 '사용' 은 보유 + **owner 미해결 BUY** 다.

    engine 의 `_pending_strategy` 는 attach 에서 항상 비어 있으므로(결정 ④) 미체결분의
    정본은 `gateway.pending_strategy_notional` 뿐이다. 캡을 한 포지션 크기(풀 25% =
    350,000)와 같게 두면 미해결 BUY 한 건이 매수수수료만큼 캡을 넘긴다 — 사이징이 잔여
    예산으로 클램프되므로 '보유 + 미해결' 이 캡을 넘는 자리는 언제나 이 수수료분이다.
    양성 대조(pile=False)에서는 같은 BUY 가 실제로 전선까지 나간다.
    """
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            engine, runtime, rm, gateway = f['engine'], f['runtime'], f['rm'], f['gateway']
            # D4 의 현금 게이트 표본과 같은 제품 knob(전략 총예산 %)만 바꾼다.
            rm.config.strategy_allocation['sepa_trend'] = 17.5
            cap = engine.portfolio.total_equity * D('0.175')
            assert cap == EXPECTED_BUY_NOTIONAL == D('350000')
            if pile:
                await f['drive'](buy_signal('005930'))
                assert len(f['posts']()) == 1
                assert engine.portfolio.get_strategy_allocation('sepa_trend') == D('0')
                advance(31)
            await f['drive'](buy_signal('000660'))
            assert rm._pending_strategy == {} and rm._pending_orders == set()
            rows = [row for row in runtime.owner.state['attempts'].values()
                    if row['kind'] == 'submit']
            if pile:
                # 전략 예산 게이트는 owner 에 닿기 전이다 — POST 도 owner 행도 늘지 않는다.
                assert len(f['posts']()) == 1
                assert f['siglog'].gates() == [('passed', None), ('blocked', 'G5_budget')]
                assert f['siglog'].rows[1]['block_reason'].startswith('sepa_trend 전략 예산 소진')
                assert f['prepared'] == [f['prepared'][0]]
                assert [row['symbol'] for row in rows] == ['005930']
                # 그 '사용' 의 정본: 보유 0 + 미해결 BUY 한 건, 전략별 필터가 걸린 값이다.
                assert gateway.pending_strategy_notional('sepa_trend') >= cap
                assert gateway.pending_strategy_notional('gap_and_go') == D('0')
            else:
                assert len(f['posts']()) == 1
                assert f['posts']()[0][1]['json']['PDNO'] == '000660'
                assert f['siglog'].gates() == [('passed', None)]
                assert [row['symbol'] for row in rows] == ['000660']
            assert f['errors']() == []
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


# 팩터 버킷 설정은 제품 `config/default.yml`(2026-09-21 확인, 143~154행)에서 옮겨 적는다.
# 운영 기본은 `enforce: false`(shadow)다. 미러 부채 — 값이 바뀌면 이 상수도 바꿔야 한다.
FACTOR_BUDGETS = {
    'enforce': False,
    'buckets': {
        'trend': {'max_pct': 65.0,
                  'strategies': ['sepa_trend', 'gap_and_go', 'momentum_breakout',
                                 'vcp_breakout', 'strategic_swing']},
        'quality': {'max_pct': 20.0, 'strategies': ['core_holding', 'value_growth']},
        'reversion': {'max_pct': 10.0, 'strategies': ['rsi2_reversal', 'theme_chasing']},
    },
}
# reversion 버킷을 쓰는 이유: trend 버킷(65%)은 코어 예약 30%·최소 현금 5% 와 합이 100%를
# 넘어, 캡을 채우면 그 전에 현금 게이트가 먼저 막는다(초과 표본을 만들 수 없다).
# 신호는 rsi2_reversal, 보유는 같은 버킷의 **형제 전략** theme_chasing 이다 — 신호 전략
# 자신에게 심으면 전략 예산 게이트(17.5%)가 먼저 막는다.
FACTOR_SIBLING = ({'symbol': '005930', 'quantity': 20, 'avg_price': D('10000'),
                   'strategy': 'theme_chasing', 'entry_days_ago': 3},)


def factor_signal():
    return buy_signal('012330', strategy=StrategyType.RSI2_REVERSAL)


@pytest.mark.parametrize('enforce', [False, True])
def test_g3_factor_bucket_budget_blocks_only_when_enforced(tmp_path, monkeypatch, enforce):
    """걷어낸 스텁 둘 — 클래스 구현 그대로의 `_check_factor_budget`.

    같은 초과를 enforce=false 는 통과시키고(shadow) true 는 G5_factor 로 막는다.
    """
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            rm, runtime = f['rm'], f['runtime']
            await seed_portfolio(f, FACTOR_SIBLING, cash=D('1800000'), tag='g3')
            rm.config.factor_budgets = {**FACTOR_BUDGETS, 'enforce': enforce}
            equity = f['engine'].portfolio.total_equity
            assert equity == D('2000000')
            # 초과가 실제로 성립한다: 형제 전략 보유 200,000 >= 캡 10%.
            assert f['engine'].portfolio.get_strategy_allocation('theme_chasing') == D('200000')
            assert rm._check_factor_budget('rsi2_reversal') is not None
            await f['drive'](factor_signal())
            if enforce:
                assert f['posts']() == [] and f['prepared'] == []
                assert runtime.owner.state['attempts'] == {}
                assert f['siglog'].gates() == [('blocked', 'G5_factor')]
                assert "팩터 'reversion' 예산 소진" in f['siglog'].rows[0]['block_reason']
            else:
                assert len(f['posts']()) == 1
                assert f['posts']()[0][1]['json']['PDNO'] == '012330'
                assert f['siglog'].gates() == [('passed', None)]
            assert f['errors']() == []
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_g3_factor_budget_failure_is_fail_open_by_design(tmp_path, monkeypatch):
    """설정 오류는 무시하고 통과시킨다(관측용 상위 캡 — G5_budget 이 1차 방어선).

    예외 주입은 `_check_factor_budget` 구간에만 건다 — 같은 포트폴리오 조회를 쓰는 다른
    게이트까지 깨면 통과가 '예외를 삼켰다'가 아니라 '거기까지 가지도 않았다'가 된다.
    """
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            rm = f['rm']
            await seed_portfolio(f, FACTOR_SIBLING, cash=D('1800000'), tag='g3-open')
            rm.config.factor_budgets = {**FACTOR_BUDGETS, 'enforce': True,
                                        'buckets': _ExplodingBuckets()}
            assert rm._check_factor_budget('rsi2_reversal') is None
            await f['drive'](factor_signal())
            assert len(f['posts']()) == 1
            assert f['siglog'].gates() == [('passed', None)]
            assert f['errors']() == []
            await assert_no_direct_broker_calls(f)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


class _ExplodingBuckets(dict):
    """`buckets` 조회에서만 터지는 설정. 비어 있지 않다고 답해 조기 return 을 피한다."""

    def __bool__(self):
        return True

    def items(self):
        raise RuntimeError('synthetic factor budget failure')
