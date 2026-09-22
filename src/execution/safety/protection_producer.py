"""기존 시세의 보호 결정을 같은 호출 안에서 인계하는 KR 전용 생산자.

새 피드·주기·durable 스키마를 만들지 않는다. REST/WS 전환은 기존 피드의
커버리지 정책을 따르며 REST 보호 입력의 진입 source 무효화를 유지한다.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from ...core.event import SignalEvent
from ...core.types import OrderSide, SignalStrength, StrategyType
from ...utils import loop_heartbeat as heartbeat
from .application import ApplicationBlocked
from .lifecycle import TERMINAL_STATES
from .market_source import canonical_observation, observation_from_event, observation_market_data
from .protection import quote_protection
from .requests import _session_at

PROTECTION_QUOTE_INTERVAL = 20.0
PROTECTION_REEMISSION_INTERVAL = 60.0
_KST = ZoneInfo('Asia/Seoul')
_HEARTBEAT = 'kr_protection_producer'


@dataclass
class _Episode:
    intent_id: str
    decision: tuple
    price: Decimal
    source: str
    command_id: str | None = None
    rejected_at: datetime | None = None
    reason: str | None = None


class ProtectionProducer:
    """lock은 sweep→quote→동기 submit 전체를 소유한다. 제품 등록은 설치기 소관이다."""

    def __init__(self, runtime, *, clock, indicator_source):
        self.runtime, self.engine = runtime, runtime.engine
        self.clock, self.indicator_source = clock, indicator_source
        self._lock = asyncio.Lock()
        self._tasks = set()
        self._episodes: dict[str, _Episode] = {}
        self._last_quote = {}
        self._sources = {}
        self._restart_originals = None
        self._restart_retries = {}
        self._restart_checked = set()
        self._recovery_required = {}
        self._pending_reasons = {}
        self._stats = dict(last_tick_at=None, throttled=0, pending_released=0,
                           resume_dispositions={}, reemissions=0, blocked_reason=None,
                           last_error=None, source_transitions=0, invariant_violation=None)

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError('보호 생산자 시각은 aware datetime이어야 합니다')
        return value.astimezone(_KST)

    def _completed(self, task):
        self._tasks.discard(task)
        # 호출자가 취소된 경우에도 예외를 회수한다. 실제 오류는 _run에서 기록한다.
        if not task.cancelled():
            task.exception()

    async def _run(self, event):
        try:
            # 첫 await 전에 접수한다. _protection_tasks 등록은 자기 source 장벽을 만든다.
            with self.runtime.command_scope():
                async with self._lock:
                    heartbeat.record_attempt(_HEARTBEAT)
                    self._stats['blocked_reason'] = None
                    self._pending_reasons = {}
                    now = self._now()
                    if event is not None:
                        self._stats['last_tick_at'] = now.isoformat()
                    await self._sweep(now)
                    if event is not None:
                        await self._tick(event, self._now())
                    if self._stats['blocked_reason'] is not None:
                        heartbeat.record_failure(_HEARTBEAT, self._stats['blocked_reason'])
                    elif self._pending_reasons:
                        heartbeat.record_idle(_HEARTBEAT, '보호 주문 진행 중')
                    elif not self.engine.portfolio.positions:
                        heartbeat.record_idle(_HEARTBEAT, '보유 종목 없음')
                    else:
                        heartbeat.record_success(_HEARTBEAT)
        except ApplicationBlocked as exc:
            self._stats['blocked_reason'] = str(exc)
            heartbeat.record_failure(_HEARTBEAT, str(exc))
        except BaseException as exc:
            self._stats['last_error'] = type(exc).__name__
            heartbeat.record_failure(_HEARTBEAT, type(exc).__name__)
            raise

    async def _schedule(self, event):
        task = asyncio.create_task(self._run(event))
        self._tasks.add(task)
        task.add_done_callback(self._completed)
        await asyncio.shield(task)

    async def on_market_data(self, event):
        await self._schedule(event)

    async def sweep(self):
        await self._schedule(None)

    @staticmethod
    def _price_text(value, *, optional=False):
        if value is None and optional:
            return None
        if type(value) not in (Decimal, int, float):
            raise ValueError('보호 시세 값은 유한한 양수여야 합니다')
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0:
            raise ValueError('보호 시세 값은 유한한 양수여야 합니다')
        return str(amount)

    def _market_input(self, event, now):
        indicators = self.indicator_source(event.symbol)
        if type(indicators) is not dict or not indicators.keys() <= {'ma5', 'prev_low'}:
            raise ValueError('보호 지표는 ma5/prev_low 캐시만 받습니다')
        data = {key: self._price_text(value, optional=True) for key, value in indicators.items()}
        if event.observation is None:
            data.update(high=self._price_text(event.high), low=self._price_text(event.low))
            return 'rest', data, data
        observation = observation_from_event(event)
        canonical_observation(observation, symbol=event.symbol, price=event.close,
            as_of=observation.market_as_of, source=observation.source,
            event_id=observation.source_event_id, now=now)
        # EOD도 읽기 전용 원시세 검증을 거친다. WS의 low만 보태는 기존 모양을 유지한다.
        preview_data = observation_market_data(observation, data)
        market_quote = dict(price=str(observation.price), as_of=observation.market_as_of.isoformat(),
            source=observation.source, source_event_id=observation.source_event_id,
            market_data=preview_data)
        self.runtime._require_quote_freshness(self.runtime.owner.state, event.symbol,
            observation.market_as_of, market_quote)
        return 'ws', data, preview_data

    def _blocked(self, reason, episode=None):
        self._stats['blocked_reason'] = reason
        if episode is not None:
            episode.reason = reason

    def _pending(self, symbol, reason, episode=None):
        # 다른 종목에서 이미 기록한 실패를 정상 pending으로 덮어쓰지 않는다.
        self._pending_reasons[symbol] = reason
        if episode is not None:
            episode.reason = reason

    def _resume_invariant(self, reason, commands):
        previous = self._stats['invariant_violation']
        since = previous['since'] if previous is not None and previous['reason'] == reason else self._now().isoformat()
        self._stats['invariant_violation'] = dict(reason=reason, since=since, command_ids=list(commands))

    def _new_decision_available(self, symbol):
        has_open, has_failure = False, False
        for row in self._attempts(symbol):
            if row['side'] != 'sell' or row['state'] in TERMINAL_STATES and row['reserved_quantity'] == 0:
                continue
            has_open = True
            if not (row.get('command_status') == 'acknowledged' and not row.get('evidence_conflict')
                    and row['state'] not in TERMINAL_STATES and row['state'] != 'blocked_unknown'
                    and row['observed_quantity'] == row['applied_quantity']):
                has_failure = True
        if has_open:
            # 행 순서와 관계없이 충돌/미적용 실패가 정상 ACK 대기보다 우선한다.
            if has_failure:
                self._blocked('blocked_by_open_exit')
            else:
                self._pending(symbol, 'blocked_by_open_exit')
            return False
        dto = self.runtime.owner.state['protection']
        row = dto['states'].get(symbol)
        if symbol in dto['pending_owners'] or row is not None and row['pending_stage'] is not None:
            self._pending(symbol, 'protection_pending')
            return False
        return True

    def _attempts(self, symbol):
        # 관측은 순수 읽기다. 다른 종목 inbox 때문에 _owner_ready를 호출하지 않는다.
        return [row for row in self.runtime.owner.state['attempts'].values()
                if row['symbol'] == symbol and row['kind'] == 'submit']

    @staticmethod
    def _audit_decisions(state):
        return [(key, row) for key, row in state['outbox'].items()
                if type(row) is dict and row.get('kind') == 'protection_decision'
                and row.get('effect_source') is None]

    @staticmethod
    def _valid_audit(row):
        decision = row.get('decision')
        return (all(type(row.get(key)) is str and row[key] != '' and row[key] == row[key].strip()
                    for key in ('symbol', 'intent_id'))
                and type(decision) is list and len(decision) == 3
                and decision[0] in ('sell_all', 'sell_partial')
                and type(decision[1]) is int and decision[1] > 0
                and type(decision[2]) is str and decision[2] != '')

    def _audit_recovery(self, state, now):
        previous, current = self._recovery_required, {}
        for command, row in self._audit_decisions(state):
            symbol, identity = row.get('symbol'), row.get('intent_id')
            if type(symbol) is not str or symbol == '':
                raise ApplicationBlocked('protection_decision_symbol_required')
            intent = state['intents'].get(identity) if type(identity) is str else None
            linked_symbols = {symbol}
            if intent is not None:
                linked_symbols.add(intent['symbol'])
            linked_symbols.update(key for key, episode in self._episodes.items()
                                  if episode.intent_id == identity)
            linked_symbols.update(key for key, pending in state['protection']['pending_owners'].items()
                                  if pending == identity)
            reason = None
            if not self._valid_audit(row) or len(linked_symbols) != 1:
                reason = 'protection_decision_evidence_mismatch'
            else:
                if intent is not None:
                    if (intent['symbol'] != symbol or intent['side'] != 'sell'
                            or type(intent['target_quantity']) is not int
                            or intent['target_quantity'] != row['decision'][1]):
                        reason = 'protection_decision_evidence_mismatch'
                    else:
                        continue
                else:
                    episode = self._episodes.get(symbol)
                    if episode is not None and episode.intent_id == identity:
                        if tuple(row['decision']) == episode.decision:
                            continue
                        reason = 'protection_decision_evidence_mismatch'
                    admissions = state.get('protection_quote_admissions', {})
                    if reason is None and (command in admissions or any(value.get('intent_id') == identity
                            and value.get('symbol') == symbol for value in admissions.values())):
                        continue
                    if reason is None:
                        reason = 'unsubmitted_protection_decision'
                    pending = state['protection']['pending_owners'].get(symbol)
                    if pending == identity and state['protection']['states'][symbol]['pending_target_qty'] != row['decision'][1]:
                        reason = 'protection_decision_evidence_mismatch'
            # 감사 행은 소비하지 않는다. 원 RAM/admission 없는 결정은 복구 권한이 필요하다.
            # 종목 연결 충돌은 감사·intent·현재 pending/RAM 양쪽 모두 보류한다.
            for affected in sorted(linked_symbols):
                entry = current.setdefault(affected, dict(reason=reason, intent_ids=[], command_ids=[],
                    since=previous.get(affected, {}).get('since', now.isoformat())))
                if reason == 'protection_decision_evidence_mismatch':
                    entry['reason'] = reason
                if identity not in entry['intent_ids']:
                    entry['intent_ids'].append(identity)
                entry['command_ids'].append(command)
        self._recovery_required = current
        for row in current.values():
            self._blocked(row['reason'])

    def _episode_status(self, symbol, episode):
        state = self.runtime.owner.state
        rows = [row for row in self._attempts(symbol) if row['intent_id'] == episode.intent_id]
        intent = state['intents'].get(episode.intent_id)
        if not rows:
            return 'unsubmitted' if intent is None else 'inconsistent'
        if (intent is None or set(intent['attempt_ids']) != {row['attempt_id'] for row in rows}
                or intent['target_quantity'] != episode.decision[1]):
            return 'inconsistent'
        if any(row.get('evidence_conflict') for row in rows):
            return 'conflict'
        if any(row['observed_quantity'] != row['applied_quantity'] for row in rows):
            return 'observed_unapplied'
        if any(row['state'] in TERMINAL_STATES and row['reserved_quantity'] != 0 for row in rows):
            return 'reservation_inconsistent'
        if any(row['state'] not in TERMINAL_STATES or row['reserved_quantity'] != 0 for row in rows):
            if any(row.get('command_status') == 'unknown' or row['state'] == 'blocked_unknown' for row in rows):
                return 'unknown'
            return 'pending' if all(row.get('command_status') == 'acknowledged' for row in rows) else 'unconfirmed'
        if all(row['observed_quantity'] == row['applied_quantity'] == 0
               and Decimal(row['observed_amount']) == 0
               and row.get('command_status') in ('not_sent', 'rejected')
               and row['state'] == 'final_rejected' for row in rows):
            return 'rejected'
        if (all(row['observed_quantity'] == row['applied_quantity'] for row in rows)
                and sum(row['applied_quantity'] for row in rows) > 0):
            return 'applied'
        return 'unresolved'

    def _restart_cooldowns(self, now):
        state = self.runtime.owner.state
        if self._restart_originals is None:
            pending = set(state['protection']['pending_owners'].values())
            audit = self._audit_decisions(state)
            self._restart_originals = {}
            for identity, intent in state['intents'].items():
                linked = any(self._valid_audit(row) and row['intent_id'] == identity
                    and row['symbol'] == intent['symbol'] and row['decision'][1] == intent['target_quantity']
                    for _, row in audit)
                if intent['side'] == 'sell' and (identity.startswith('pp-i-') or identity in pending or linked):
                    self._restart_originals.setdefault(intent['symbol'], []).append(identity)
        for symbol, identities in self._restart_originals.items():
            rejected = []
            for identity in identities:
                intent = state['intents'].get(identity)
                if identity in self._restart_checked or intent is None:
                    continue
                # 종결시각이 없는 정본에서 최초 관측 이후 60초를 보수적으로 센다.
                probe = _Episode(identity, ('sell_all', intent['target_quantity'], ''), Decimal('1'), '')
                if self._episode_status(symbol, probe) == 'rejected':
                    rejected.append(identity)
            if not rejected:
                self._restart_retries.pop(symbol, None)
                continue
            pending = state['protection']['pending_owners'].get(symbol)
            if pending is not None and pending not in rejected:
                # 다른 원 pending이 현재 episode임을 증명한다. 옛 거절은 재발행 대상이 아니다.
                self._restart_checked.update(rejected)
                self._restart_retries.pop(symbol, None)
                continue
            if pending is None and len(identities) != 1:
                # UUID/JSON 키 순서는 시간 근거가 아니다. full/EOD의 최신 행을 추측하지 않는다.
                self._restart_retries[symbol] = dict(reason='restart_episode_order_ambiguous',
                    intent_ids=list(identities), started_at=None)
                continue
            identity = pending if pending is not None else rejected[0]
            barrier = self._restart_retries.get(symbol)
            if barrier is None or barrier['intent_ids'] != [identity]:
                barrier = dict(reason='restart_reemission_cooldown', intent_ids=[identity], started_at=now)
                self._restart_retries[symbol] = barrier
            if (now - barrier['started_at']).total_seconds() >= PROTECTION_REEMISSION_INTERVAL:
                self._restart_checked.update(rejected)
                del self._restart_retries[symbol]
                self._stats['reemissions'] += 1

    async def _sweep(self, now):
        self._audit_recovery(self.runtime.owner.state, now)
        self._restart_cooldowns(now)
        for symbol, episode in tuple(self._episodes.items()):
            status = self._episode_status(symbol, episode)
            if status == 'applied':
                del self._episodes[symbol]
            elif status == 'rejected':
                if episode.rejected_at is None:
                    episode.rejected_at = now
                if (now - episode.rejected_at).total_seconds() >= PROTECTION_REEMISSION_INTERVAL:
                    del self._episodes[symbol]
                    self._stats['reemissions'] += 1
        # 보류 결정의 intent가 아직 미등록이어도 orphan으로 해제하면 안 된다.
        for symbol in tuple(self.runtime.owner.state['protection']['pending_owners']):
            if (symbol not in self._episodes and symbol not in self._restart_retries
                    and symbol not in self._recovery_required):
                if await self.runtime.release_protection_pending(symbol):
                    self._stats['pending_released'] += 1
        while self.runtime.owner.state.get('protection_quote_admissions', {}):
            originals = deepcopy(self.runtime.owner.state['protection_quote_admissions'])
            try:
                result = await self.runtime.resume_protection_admission()
            except ApplicationBlocked:
                self._resume_invariant('protection_resume_blocked', originals)
                raise
            if not result:
                self._resume_invariant('protection_resume_no_progress', originals)
                raise ApplicationBlocked('protection_resume_no_progress')
            if len(result) != 1:
                self._resume_invariant('protection_resume_ambiguous', originals)
                raise ApplicationBlocked('protection_resume_ambiguous')
            symbol, disposition, decision = result[0]
            matches = [(key, row) for key, row in originals.items() if row['symbol'] == symbol
                       and key not in self.runtime.owner.state.get('protection_quote_admissions', {})]
            if len(matches) != 1:
                self._resume_invariant('protection_resume_ambiguous', originals)
                raise ApplicationBlocked('protection_resume_ambiguous')
            command, original = matches[0]
            if decision is not None:
                if symbol in self._episodes:
                    self._resume_invariant('protection_resume_episode_conflict', originals)
                    raise ApplicationBlocked('protection_resume_episode_conflict')
                intent = original['intent_id']
                if type(intent) is not str or intent == '':
                    self._resume_invariant('protection_resume_intent_required', originals)
                    raise ApplicationBlocked('protection_resume_intent_required')
                # 다음 행이 실패해도 이 결정은 원 ID/가격으로 남는다.
                self._episodes[symbol] = _Episode(intent, tuple(decision),
                    Decimal(original['price']), 'exit_manager', command_id=command)
            counts = self._stats['resume_dispositions']
            counts[disposition] = counts.get(disposition, 0) + 1
        for symbol, episode in tuple(self._episodes.items()):
            await self._submit(symbol, episode, now)
        # 접수 배수와 후속 인계까지 정상 반환한 뒤 현재 복구 불변식 장애를 해제한다.
        self._stats['invariant_violation'] = None

    async def _tick(self, event, now):
        symbol = event.symbol
        position = self.engine.portfolio.positions.get(symbol)
        if position is None or self.runtime.exit_manager.is_exit_exempt(symbol):
            return
        session = _session_at(now)
        if session not in ('regular', 'closing'):
            self._blocked('unsupported_session:' + session)
            return
        if symbol in self._episodes:
            return
        if symbol in self._recovery_required:
            self._blocked(self._recovery_required[symbol]['reason'])
            return
        if symbol in self._restart_retries:
            self._blocked(self._restart_retries[symbol]['reason'])
            return
        intent = 'pp-i-' + uuid4().hex
        price = event.close
        if type(price) is not Decimal or not price.is_finite() or price <= 0:
            raise ValueError('보호 가격은 양의 유한 Decimal이어야 합니다')
        source, data, preview_data = self._market_input(event, now)
        if now.hour * 100 + now.minute >= 1510 and position.strategy in ('gap_and_go', 'theme_chasing'):
            if type(position.avg_price) is not Decimal or position.avg_price <= 0:
                self._blocked('invalid_entry_basis')
                return
            pnl = (price / position.avg_price - 1) * Decimal('100')
            threshold = Decimal('0') if position.strategy == 'gap_and_go' else Decimal('1')
            if pnl < threshold:
                if not self._new_decision_available(symbol):
                    return
                label, source = (('갭EOD', 'gap_eod') if position.strategy == 'gap_and_go'
                                 else ('테마EOD', 'theme_eod'))
                episode = _Episode(intent, ('sell_all', position.quantity,
                    f'{label}: 수익률 {pnl:+.1f}%'), price, source)
                self._episodes[symbol] = episode
                await self._submit(symbol, episode, now)
                return
        if symbol in self.runtime.owner.state['protection']['degraded']:
            self._blocked('protection_degraded')
            return
        if symbol in self._sources and self._sources[symbol] != source:
            self._stats['source_transitions'] += 1
        self._sources[symbol] = source
        # 사전 검사는 매 틱 독립 DTO를 계산한다. 중간 고점 표본 손실은 측정 대상이다.
        _, preview = quote_protection(self.runtime.owner.state['protection'], symbol=symbol,
            price=price, now=now, market_data=preview_data, intent_id=intent)
        if preview is not None and not self._new_decision_available(symbol):
            return
        previous = self._last_quote.get(symbol)
        if preview is None and previous is not None and (now - previous).total_seconds() < PROTECTION_QUOTE_INTERVAL:
            self._stats['throttled'] += 1
            return
        if source == 'ws':
            decision = await self.runtime.observe_market(event, intent_id=intent, market_data=data)
        else:
            decision = await self.runtime.quote(symbol, price, intent_id=intent, market_data=data,
                market_as_of=None, source=None, source_event_id=None)
        self._last_quote[symbol] = now
        if decision is not None:
            episode = _Episode(intent, tuple(decision), price, 'exit_manager')
            self._episodes[symbol] = episode
            await self._submit(symbol, episode, now)

    async def _submit(self, symbol, episode, now):
        status = self._episode_status(symbol, episode)
        if status == 'pending':
            self._pending(symbol, 'episode_acknowledged_pending', episode)
            return
        if status != 'unsubmitted':
            if status == 'rejected' and episode.rejected_at is None:
                episode.rejected_at = now
            self._blocked('episode_' + status, episode)
            return
        if symbol in self._recovery_required:
            self._blocked(self._recovery_required[symbol]['reason'], episode)
            return
        if symbol in self._restart_retries:
            self._blocked(self._restart_retries[symbol]['reason'], episode)
            return
        if self.runtime.day_admission_closed or self.runtime._closing:
            self._blocked('day_transition_admission_closed', episode)
            return
        session = _session_at(self._now())
        if session not in ('regular', 'closing'):
            self._blocked('unsupported_session:' + session, episode)
            return
        if self.runtime.exit_manager.is_exit_exempt(symbol):
            self._blocked('exit_exempt', episode)
            return
        position = self.engine.portfolio.positions.get(symbol)
        action, quantity, reason = episode.decision
        if (position is None or type(quantity) is not int or quantity <= 0
                or quantity > position.quantity or action not in ('sell_all', 'sell_partial')
                or (action == 'sell_all' and quantity != position.quantity)):
            self._blocked('protection_quantity_mismatch', episode)
            return
        dto = self.runtime.owner.state['protection']
        pending = dto['pending_owners'].get(symbol)
        row = dto['states'].get(symbol)
        if (row is not None and row['remaining_quantity'] != position.quantity
                or pending is not None and (pending != episode.intent_id
                    or row is None or row['pending_target_qty'] != quantity)):
            self._blocked('protection_quantity_mismatch', episode)
            return
        for attempt in self._attempts(symbol):
            if attempt['state'] not in TERMINAL_STATES or attempt['reserved_quantity'] != 0:
                self._blocked('blocked_by_open_entry' if attempt['side'] == 'buy'
                              else 'blocked_by_open_exit', episode)
                return
        strategy = next((value for value in StrategyType if value.value == position.strategy), StrategyType.SEPA_TREND)
        signal = SignalEvent(source=episode.source, symbol=symbol, side=OrderSide.SELL,
            strength=SignalStrength.STRONG, strategy=strategy, price=episode.price,
            score=100.0, confidence=1.0, reason=reason,
            metadata={'source': episode.source, 'quantity': quantity, 'exit_action': action,
                      'protection_intent_id': episode.intent_id,
                      'order_type': 'market' if session == 'regular' else 'limit'})
        try:
            await self.engine._submit_signal(signal)
        except ApplicationBlocked as exc:
            self._blocked(str(exc), episode)
            return
        status = self._episode_status(symbol, episode)
        if status == 'unsubmitted':
            self._blocked('submit_without_owner_evidence', episode)
        elif status == 'rejected':
            episode.rejected_at = self._now()
            self._blocked('episode_rejected', episode)
        elif status == 'inconsistent':
            self._blocked('episode_evidence_mismatch', episode)
        elif status in ('conflict', 'unknown', 'unconfirmed', 'unresolved',
                        'observed_unapplied', 'reservation_inconsistent'):
            self._blocked('episode_' + status, episode)
        else:
            attempts = [row for row in self._attempts(symbol) if row['intent_id'] == episode.intent_id]
            if any(row.get('command_status') == 'unknown' for row in attempts):
                self._blocked('episode_unknown', episode)
            elif any(row.get('command_status') != 'acknowledged' for row in attempts):
                self._blocked('episode_unconfirmed', episode)
            else:
                episode.reason = 'owner_attempt_recorded'

    def health(self):
        state = deepcopy(self._stats)
        state.update(producer_running=not self.runtime._closing, closing=self.runtime._closing,
            active_tasks=sum(not task.done() for task in self._tasks),
            last_quote_at={key: value.isoformat() for key, value in self._last_quote.items()},
            blocked_by_open_entry=sorted({row['symbol'] for row in self.runtime.owner.state['attempts'].values()
                if row['kind'] == 'submit' and row['side'] == 'buy'
                and (row['state'] not in TERMINAL_STATES or row['reserved_quantity'] != 0)}),
            pending_reasons=dict(self._pending_reasons),
            sources=dict(self._sources), recovery_required=deepcopy(self._recovery_required), restart_retries={symbol: {
                **row, 'started_at': row['started_at'].isoformat() if row['started_at'] is not None else None}
                for symbol, row in self._restart_retries.items()}, retained_decisions={symbol: {
                'intent_id': row.intent_id, 'decision': list(row.decision), 'price': str(row.price),
                'command_id': row.command_id, 'reason': row.reason,
                'rejected_at': row.rejected_at.isoformat() if row.rejected_at is not None else None}
                for symbol, row in self._episodes.items()})
        return state
