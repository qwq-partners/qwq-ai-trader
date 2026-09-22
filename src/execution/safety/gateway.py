"""SIGNAL 한 건의 단일 송신로: 게시 → prepare → dispatch (S3-5).

engine 을 import 하지 않는다 — 입력은 SignalEvent·Order·증거뿐이고, 설치 지점은
`runtime.install_gateway` 하나다. owner 의 판정을 복제하지 않는다: 가격·수량·세션·경제·
섹터는 `_evaluate` 가 보고 이 모듈은 순서와 식별자만 맡는다.

실패는 삼키지 않는다. 게시~prepare 실패는 reducer 예외라 commit 0 이고 그대로 호출자에게
올라간다(S3-6a 의 호출부가 결정 ②로 흡수한다). `CommandResult` 를 돌려준 요청만 dispatch
까지 간 것이며 NOT_SENT 도 결과다 — 송신 성공이 아니다. "출처만 게시·판단 사실 실패"의
부분 상태는 되돌리지 않는다(version 되감기 금지).
"""
from __future__ import annotations

from decimal import Decimal
from math import isfinite
from uuid import uuid4

from loguru import logger

from ...core.types import Order, OrderSide
from ...utils.exit_types import classify_exit_type
from .commands import RequestBoundCommands, _require, _text
from .lifecycle import CommandResult
from .policy_snapshot import PolicyContext
from .qualification_publisher import QualificationEvidence, publish_qualification
from .requests import RequestSession, _session_at
from .transport import GuardedKISTransport


class SignalGateway:
    """후보 판단을 통과한 요청 하나를 owner 의 한 길로 보낸다."""

    def __init__(self, runtime, commands, *, exit_manager, config_version):
        _require(type(commands) is RequestBoundCommands and commands.runtime is runtime,
                 'invalid_gateway_commands')
        # 판단~final 의 손절 축이 owner 가 보는 보호 상태와 갈라지지 않게 같은 객체를 요구한다.
        _require(exit_manager is runtime.exit_manager, 'invalid_gateway_exit_manager')
        _text(config_version)
        self.runtime, self.commands = runtime, commands
        self.exit_manager, self.config_version = exit_manager, config_version
        # 같은 종목·side·전략의 재시도는 같은 intent 를 쓴다(계약 4). 목표 수량은 lifecycle 소관.
        self._intents: dict[tuple, str] = {}

    async def submit(self, event, order, evidence) -> CommandResult | None:
        """결정 ⑫의 순서. 자동 BUY 는 증거가 없으면 게시도 prepare 도 하지 않는다."""
        _require(type(order) is Order, 'invalid_gateway_order')
        buying = order.side is OrderSide.BUY
        if buying and evidence is None:
            logger.warning('[게이트웨이] 증거 없는 자동 매수는 게시하지 않습니다: 종목={}',
                           order.symbol)
            return None
        if buying:
            _require(type(evidence) is QualificationEvidence, 'invalid_qualification_evidence')
        now = self.runtime._now()
        request, context = self._bind(event, order, now)
        # 전송 객체는 예약이 생기기 전에 만든다 — prepare 뒤에 실패하면 예약이 남는다.
        transport = GuardedKISTransport(self.runtime.engine.broker,
                                        request_builder=self.commands.builder)
        self._policy_context(buying)
        if buying:
            await self._quote(event, request, now)
            facts = await publish_qualification(
                self.commands, evidence, intent_id=request.intent_id,
                config_version=self.config_version, decided_at=now)
            # S3-4 가 전일 판단 사실을 지우므로 이 한 줄이 유일한 감사 흔적이다(결정 ⑩·⑬).
            logger.info('[게이트웨이] 판단 사실 게시: intent={} 종목={} 전략={} digest={} '
                        'config={} 만료={}', facts.intent_id, facts.symbol, facts.strategy,
                        facts.digest, facts.config_version, facts.expires_at.isoformat())
        await self.commands.prepare(request, context, fill_metadata=self._fill_metadata(event, order))
        # 같은 attempt 로 재시도하지 않는다(계약 6). create_task 로 감싸면 명령 스코프가 깨진다.
        return await self.commands.dispatch(request, context, transport)

    @staticmethod
    def _fill_metadata(event, order):
        """체결 관측이 읽을 값은 여기서 주문 intent에 묶는다(브로커 응답에서 추측 0건).

        진입 점수는 `Order.signal_score`가 정본이고 없으면 신호가 실어 온 점수다. 만석 교체의
        점수 비교는 돈 경로라 결측을 허용하지 않는다 — 없으면 보유가 전부 0점으로 보인다.
        섹터는 `request_binding['sector']`가 이미 정본이다(economics의 `_entry_sector`).

        SELL 의 `exit_type`(P0-3 G6): legacy 는 `run_fill_check` 가 `record_exit` 에 이 태그를
        넘겨 당일 손절·청산 카운트를 세운다. attach 에서 None 을 돌려주면 그 태그가 영원히
        빈 문자열이라 재진입 억제가 fail-open 으로 죽는다. 분류는 legacy 와 **같은 함수**를
        쓴다(`src/utils/exit_types.py`). reason 의 정본은 engine 이 이미 실어 둔
        `Order.reason` 이고(engine.py 의 `event.reason` 대입) 없으면 신호에서 읽는다.

        BUY 의 `name`(P0-3 G7): 신호가 실어 온 후보명만 쓴다 — 없거나 공백뿐이면 **키 자체를
        싣지 않고** 있으면 앞뒤 공백을 떼어 싣는다(빈 문자열도 공백 낀 문자열도 prepare 의
        `invalid_fill_metadata_value` 이고, 종목코드를 이름으로 위조하지도 않는다).
        event 는 duck-typing 으로 읽는다: 신호 객체의 종류가 호출자마다 다르다.
        """
        if order.side is not OrderSide.BUY:
            reason = order.reason
            if reason is None or reason == '':
                reason = getattr(event, 'reason', '')
            return {'exit_type': classify_exit_type(reason)}
        score = order.signal_score if order.signal_score is not None else event.score
        _require(type(score) in (int, float) and isfinite(score), 'entry_signal_score_required')
        metadata = {'entry_signal_score': score}
        # 이 `or` 는 dict 결측 폴백이고 falsy 판정이 아니다(빈 dict 도 같은 결론이다).
        source = getattr(event, 'metadata', None) or {}
        name = source.get('candidate_name') if type(source) is dict else None
        if name is None and type(source) is dict:
            name = source.get('name')
        if type(name) is str and name.strip() != '':
            # prepare 의 검사기(`commands._fill_metadata`)와 **같은 정규화**다. 그쪽은
            # `item == item.strip()` 을 요구하므로 앞뒤 공백을 그대로 실으면 그 BUY 가
            # `invalid_fill_metadata_value` 로 통째로 죽는다(공백뿐인 이름은 이름이 아니다).
            metadata['name'] = name.strip()
        return metadata

    def reserved_cash(self) -> Decimal:
        """owner 미해결 attempt 의 예약 현금 합. `evaluate_entry_policy` 와 같은 식이다."""
        return sum((fact.reserved_cash for fact in self._pending()), Decimal('0'))

    def unresolved_symbols(self) -> frozenset:
        """owner 미해결 attempt 의 종목. attach 의 교체 후보 제외에 쓴다(S4-2 H9)."""
        return frozenset(fact.symbol for fact in self._pending())

    def pending_strategy_notional(self, strategy) -> Decimal:
        """전략별 pending 매수 예약. `recompose_quantity` 의 잔여 계산과 같은 필터다."""
        return sum((fact.reserved_cash for fact in self._pending()
                    if fact.side == 'buy' and fact.strategy == strategy), Decimal('0'))

    async def recover_unsent(self) -> list[str]:
        """기동 전용 sweep: 송신을 시작한 적 없는 prepared SUBMIT 을 끝내고 예약을 푼다.

        prepare 와 claim 사이의 취소·종료·crash 는 같은 호출 안에서 정리할 수 없다. 재시작하면
        그 요청 객체도 사라져 아무도 보낼 수 없으므로, 남은 행은 예약과 일자 전환만 막는다.
        누가 끝낼 수 있는지는 `abandon_candidate` 의 가드가 정한다(여기서 다시 판정하지 않는다).
        엔진이 도는 중에는 진행 중인 제출과 겹치므로 거부한다.
        """
        _require(not self.runtime.engine.running, 'gateway_recover_requires_stopped_engine')
        swept = []
        for attempt_id, attempt in list(self.commands.owner.state.get('attempts', {}).items()):
            if attempt['kind'] == 'submit' and attempt['state'] == 'prepared':
                if await self.runtime.lifecycle.abandon_candidate(attempt_id,
                                                                  reason='startup_unclaimed'):
                    swept.append(attempt_id)
        if swept:
            logger.warning('[게이트웨이] 미송신 시도 {}건을 기동 시 정리했습니다', len(swept))
        return swept

    def _pending(self):
        # snapshot 을 못 만드는 상태에서 0 을 지어내면 예약이 없는 것처럼 읽힌다(fail-closed).
        # 저장과 게시가 어긋난 owner 의 메모리 state 는 낡았을 수 있으므로 먼저 준비 상태를 본다.
        state = self.commands.owner.state
        self.commands._owner_ready(state)
        return self.commands._snapshot(state).pending

    def _bind(self, event, order, now):
        metadata = getattr(event, 'metadata', None)
        protection_intent_id = (metadata.get('protection_intent_id')
                                if type(metadata) is dict else None)
        if (order.side is OrderSide.SELL and type(protection_intent_id) is str
                and protection_intent_id != ''):
            # 보호 에피소드의 목표 수량과 pending owner를 같은 ID로 묶는다.
            # 기존 자동 intent 캐시는 읽지도 쓰지도 않는다. 검증은 builder가 맡는다.
            intent_id = protection_intent_id
        else:
            key = (order.symbol, order.side, order.strategy)
            intent_id = self._intents.get(key)
            if intent_id is None:
                intent_id = 'gw-i-' + uuid4().hex
                self._intents[key] = intent_id
        # 시장가에는 지정가가 없다 — 평가 가격은 신호가 실어 온 값이다.
        valuation = order.price if order.price is not None else event.price
        request = self.commands.builder.prepare_submit(
            order, intent_id=intent_id, attempt_id='gw-a-' + uuid4().hex,
            session=RequestSession(now.date().isoformat(), now, _session_at(now)),
            valuation_price=valuation)
        context = self.commands.authority.automatic(request.symbol, request.side.value,
                                                    request.strategy)
        return request, context

    async def _quote(self, event, request, now):
        if request.symbol in self.commands.owner.state.get('market_sources', {}):
            # 실제 원관측이 이미 진입 가격을 게시했다. 같은 종목에 두 번째 출처를 세우지 않는다.
            return
        await self.commands.observe_entry_quote(
            request.symbol, request.valuation_price, as_of=now, source='signal',
            event_id=event.id, expected_version=self.commands.owner.version)

    def _policy_context(self, buying):
        """정책 맥락은 게시자(10A3 factory)의 것이다 — gateway 는 쓰지 않고 읽기만 한다.

        주입 `config_version` 으로 게시본의 config 축을 덮어쓰면 owner 의
        `stale_decision_config_version` 이 한 값을 자기 자신과 비교하게 된다. 그래서 덮어쓰지
        않고, 매수는 판단 사실을 게시하기 **전에** 같은 조건을 미리 본다(어긋난 채 게시하면
        그날의 판단 행만 남는다). 매도는 사이징 설정과 무관하므로 막지 않는다.
        """
        row = self.commands.owner.state.get('entry_policy_context')
        _require(row is not None, 'entry_policy_context_required')
        if buying:
            _require(PolicyContext.from_dict(row).versions.config == self.config_version,
                     'stale_decision_config_version')
