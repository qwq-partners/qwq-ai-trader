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
from .market_source import observation_from_event, observation_market_data
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
        self._stats = dict(last_tick_at=None, throttled=0, pending_released=0,
                           resume_dispositions={}, reemissions=0, blocked_reason=None,
                           last_error=None, source_transitions=0)

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
                    now = self._now()
                    if event is not None:
                        self._stats['last_tick_at'] = now.isoformat()
                    await self._sweep(now)
                    if event is not None:
                        await self._tick(event, self._now())
                    if self._stats['blocked_reason'] is not None:
                        heartbeat.record_failure(_HEARTBEAT, self._stats['blocked_reason'])
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

    def _blocked(self, reason, episode=None):
        self._stats['blocked_reason'] = reason
        if episode is not None:
            episode.reason = reason

    def _attempts(self, symbol):
        # 관측은 순수 읽기다. 다른 종목 inbox 때문에 _owner_ready를 호출하지 않는다.
        return [row for row in self.runtime.owner.state['attempts'].values()
                if row['symbol'] == symbol and row['kind'] == 'submit']

    def _episode_status(self, symbol, episode):
        state = self.runtime.owner.state
        rows = [row for row in self._attempts(symbol) if row['intent_id'] == episode.intent_id]
        intent = state['intents'].get(episode.intent_id)
        if not rows:
            return 'unsubmitted' if intent is None else 'inconsistent'
        if (intent is None or set(intent['attempt_ids']) != {row['attempt_id'] for row in rows}
                or intent['target_quantity'] != episode.decision[1]):
            return 'inconsistent'
        if any(row['state'] not in TERMINAL_STATES or row.get('evidence_conflict')
               or row['reserved_quantity'] != 0 for row in rows):
            return 'unresolved'
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
            self._restart_originals = {}
            for identity, intent in state['intents'].items():
                if intent['side'] == 'sell' and (identity.startswith('pp-i-') or identity in pending):
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
            if symbol not in self._episodes and symbol not in self._restart_retries:
                if await self.runtime.release_protection_pending(symbol):
                    self._stats['pending_released'] += 1
        while self.runtime.owner.state.get('protection_quote_admissions', {}):
            originals = deepcopy(self.runtime.owner.state['protection_quote_admissions'])
            result = await self.runtime.resume_protection_admission()
            if not result:
                raise ApplicationBlocked('protection_resume_no_progress')
            if len(result) != 1:
                raise ApplicationBlocked('protection_resume_ambiguous')
            symbol, disposition, decision = result[0]
            matches = [(key, row) for key, row in originals.items() if row['symbol'] == symbol
                       and key not in self.runtime.owner.state.get('protection_quote_admissions', {})]
            if len(matches) != 1:
                raise ApplicationBlocked('protection_resume_ambiguous')
            command, original = matches[0]
            if decision is not None:
                if symbol in self._episodes:
                    raise ApplicationBlocked('protection_resume_episode_conflict')
                intent = original['intent_id']
                if type(intent) is not str or intent == '':
                    raise ApplicationBlocked('protection_resume_intent_required')
                # 다음 행이 실패해도 이 결정은 원 ID/가격으로 남는다.
                self._episodes[symbol] = _Episode(intent, tuple(decision),
                    Decimal(original['price']), 'exit_manager', command_id=command)
            counts = self._stats['resume_dispositions']
            counts[disposition] = counts.get(disposition, 0) + 1
        for symbol, episode in tuple(self._episodes.items()):
            await self._submit(symbol, episode, now)

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
        if symbol in self._restart_retries:
            self._blocked(self._restart_retries[symbol]['reason'])
            return
        intent = 'pp-i-' + uuid4().hex
        price = event.close
        if type(price) is not Decimal or not price.is_finite() or price <= 0:
            raise ValueError('보호 가격은 양의 유한 Decimal이어야 합니다')
        if now.hour * 100 + now.minute >= 1510 and position.strategy in ('gap_and_go', 'theme_chasing'):
            if type(position.avg_price) is not Decimal or position.avg_price <= 0:
                self._blocked('invalid_entry_basis')
                return
            pnl = (price / position.avg_price - 1) * Decimal('100')
            threshold = Decimal('0') if position.strategy == 'gap_and_go' else Decimal('1')
            if pnl < threshold:
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
        indicators = self.indicator_source(symbol)
        if type(indicators) is not dict or not indicators.keys() <= {'ma5', 'prev_low'}:
            raise ValueError('보호 지표는 ma5/prev_low 캐시만 받습니다')
        data = {key: str(value) if isinstance(value, Decimal) else value
                for key, value in indicators.items()}
        source = 'ws' if event.observation is not None else 'rest'
        if source == 'ws':
            observation = observation_from_event(event)
            preview_data = observation_market_data(observation, data)
        else:
            data.update(high=str(event.high), low=str(event.low))
            preview_data = data
        if symbol in self._sources and self._sources[symbol] != source:
            self._stats['source_transitions'] += 1
        self._sources[symbol] = source
        # 사전 검사는 매 틱 독립 DTO를 계산한다. 중간 고점 표본 손실은 측정 대상이다.
        _, preview = quote_protection(self.runtime.owner.state['protection'], symbol=symbol,
            price=price, now=now, market_data=preview_data, intent_id=intent)
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
        if status != 'unsubmitted':
            if status == 'rejected' and episode.rejected_at is None:
                episode.rejected_at = now
            self._blocked('episode_' + status, episode)
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
                if row['kind'] == 'submit' and row['side'] == 'buy' and row['state'] not in TERMINAL_STATES}),
            sources=dict(self._sources), restart_retries={symbol: {
                **row, 'started_at': row['started_at'].isoformat() if row['started_at'] is not None else None}
                for symbol, row in self._restart_retries.items()}, retained_decisions={symbol: {
                'intent_id': row.intent_id, 'decision': list(row.decision), 'price': str(row.price),
                'command_id': row.command_id, 'reason': row.reason,
                'rejected_at': row.rejected_at.isoformat() if row.rejected_at is not None else None}
                for symbol, row in self._episodes.items()})
        return state
