"""명시 설치 전용 KR 요청 owner. 기본 startup 장벽/legacy writer를 열지 않는다.

예약과 claim은 기존 owner에만 저장하고, process-local permit은 durable claim의
성공한 호출에만 존재한다. 마지막 동기 검사의 보장은 로컬 POST 시작까지다.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
from decimal import Decimal
import math
from uuid import uuid4

from loguru import logger

from ...core.types import OrderSide
from . import risk_policy as p
from .application import ApplicationBlocked
from .decisions import EntryDecisionFacts, recompose_quantity
from .market_source import source_is_current
from .economics import decode_portfolio, encode_portfolio
from .guards import EntryAuthority, EntryOrigin, FinalEntryGuard, GuardDecision
from .lifecycle import CommandKind, CommandResult, CommandStatus, OrderRef, TERMINAL_STATES
from .policy_generations import canonical
from .policy_snapshot import PolicyContext, build_owned_snapshot
from .protection import encode_protection
from .protection_recovery import digest as _digest
from .regime_owner import effective_regime
from .requests import KISRequestBuilder, PreparedTradeRequest, _session_at
from .reservations import has_remaining_reservation
from .resources import calculate_resources, remaining_resource_amount
from .transport import GuardedKISTransport, TransportStatus


class CommandValidationError(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _require(condition, reason):
    if not condition:
        raise CommandValidationError(reason)


def _text(value):
    _require(type(value) is str and bool(value) and value == value.strip(), 'invalid_command_identity')


# 체결 관측 metadata의 허용 키. economics의 화이트리스트(reduce_economics)와 같은 집합이며
# registration_params는 보호 등록기가 만드는 값이라 주문 intent가 실을 수 없다.
FILL_METADATA_FIELDS = ('name', 'sector', 'entry_signal_score', 'exit_type')


def _fill_metadata(value):
    """체결 시점이 아니라 prepare 시점에 거부한다.

    metadata는 `observation_id`에 들어가므로 체결 시점에 거부되면 그 주문의 체결은 영영
    적용되지 않는다(`encode_state`가 Decimal을 거부한다). 점수는 None 또는 float만 싣는다.
    """
    if value is None:
        return {}
    _require(type(value) is dict, 'invalid_fill_metadata')
    _require(set(value) <= set(FILL_METADATA_FIELDS), 'unknown_fill_metadata_field')
    row = {}
    for key, item in value.items():
        if key == 'entry_signal_score':
            _require(item is None or (type(item) in (int, float) and math.isfinite(item)),
                     'invalid_fill_metadata_value')
            row[key] = None if item is None else float(item)
        else:
            _require(type(item) is str and bool(item) and item == item.strip(),
                     'invalid_fill_metadata_value')
            row[key] = item
    return row


def _amount(value):
    _require(type(value) is str, 'invalid_reservation')
    amount = Decimal(value)
    _require(amount.is_finite() and amount >= 0, 'invalid_reservation')
    return amount


class RequestBoundCommands:
    def __init__(self, runtime, *, builder, authority, entry_guard, stop_resolver, session_guard):
        from .runtime import KRExecutionRuntime
        _require(isinstance(runtime, KRExecutionRuntime), 'invalid_runtime')
        _require(type(builder) is KISRequestBuilder and type(authority) is EntryAuthority
                 and type(entry_guard) is FinalEntryGuard and entry_guard.authority is authority,
                 'invalid_command_authority')
        _require(callable(stop_resolver) and callable(session_guard), 'invalid_command_callbacks')
        self.runtime, self.owner = runtime, runtime.owner
        self.builder, self.authority, self.entry_guard = builder, authority, entry_guard
        self.stop_resolver, self.session_guard = stop_resolver, session_guard
        self._permits = {}
        self._result_tasks = set()

    def _owner_ready(self, state, *, dispatch=False):
        _require(self.owner.healthy and self.owner.version == self.owner.published_version,
                 'store_or_publication_unhealthy')
        self.runtime._require_day_admission()
        _require(self.runtime.engine._execution_version == self.owner.version, 'publication_mismatch')
        _require(all(row.get('status') in ('APPLIED', 'SUPERSEDED')
                     for row in state.get('inbox', {}).values()), 'unapplied_execution_observation')
        _require(all(row.get('state') in ('SETTLED', 'RESOLVED')
                     for row in self.runtime.engine._execution_ingress.values()), 'unapplied_execution_ingress')
        if dispatch:
            _require(self.runtime.trading_ready is True, 'startup_reconciliation')
        pf = decode_portfolio(state['portfolio'])
        for symbol, position in pf.positions.items():
            position.current_price = self.runtime._view_price(state, symbol, position.current_price)
        _require(encode_portfolio(pf) == encode_portfolio(self.runtime.engine.portfolio),
                 'legacy_portfolio_writer_conflict')
        _require(encode_protection(self.runtime.exit_manager) == state['protection'],
                 'legacy_protection_writer_conflict')

    def _request(self, request, context, *, session=True):
        self.builder.validate(request)
        prepared = deepcopy(request)
        _require(self.authority.owns(context), 'untrusted_entry_context')
        context = replace(context)
        _require((context.symbol, context.side, context.strategy) ==
                 (prepared.symbol, prepared.side.value, prepared.strategy), 'entry_context_mismatch')
        _require(prepared.account.account_scope == self.runtime.account_scope, 'account_scope_mismatch')
        if session:
            self._session(prepared)
        return prepared, context

    def _session(self, request):
        now = self.runtime._now()
        _require(request.session.business_date_kst == now.date().isoformat()
                 and request.session.observed_at <= now, 'request_day_mismatch')
        _require(request.session.session == _session_at(now), 'request_session_changed')
        decision = self.session_guard(request)
        _require(type(decision) is GuardDecision and type(decision.allowed) is bool
                 and type(decision.reason) is str,
                 'invalid_session_decision')
        _require(decision.allowed, decision.reason)

    def _version(self, expected):
        _require(type(expected) is int and expected == self.owner.version, 'stale_execution_version')

    async def publish_policy_context(self, context, *, expected_version):
        with self.runtime.command_scope():
            return await self._publish_policy_context(context, expected_version=expected_version)

    async def _publish_policy_context(self, context, *, expected_version):
        _require(type(context) is PolicyContext, 'invalid_policy_context')
        context = PolicyContext.from_dict(context.to_dict())
        def reduce(state):
            self._owner_ready(state)
            self._version(expected_version)
            now = self.runtime._now()
            _require(context.business_day == now.date() and context.observed_at <= now,
                     'policy_context_day_or_time_mismatch')
            _require(context.versions.execution == self.owner.version, 'stale_policy_source_version')
            state['entry_policy_context'] = context.to_dict()
            state.setdefault('entry_policy_effects', {'pending_sectors': {}})
            return state
        await self.owner.mutate('entry-policy:'+uuid4().hex, reduce)
        return self.owner.version

    async def observe_entry_quote(self, symbol, price, *, as_of, source, event_id, expected_version):
        with self.runtime.command_scope():
            return await self._observe_entry_quote(symbol, price, as_of=as_of, source=source,
                                                   event_id=event_id, expected_version=expected_version)

    async def _observe_entry_quote(self, symbol, price, *, as_of, source, event_id, expected_version):
        for value in (symbol, source, event_id): _text(value)
        _require(type(price) is Decimal and price.is_finite() and price > 0, 'invalid_entry_quote')
        _require(type(as_of) is datetime and as_of.utcoffset() is not None, 'invalid_entry_quote_time')
        row = dict(symbol=symbol, price=str(price), as_of=as_of.isoformat(), source=source, event_id=event_id)
        def reduce(state):
            self._owner_ready(state)
            self._version(expected_version)
            _require(symbol not in state.get('market_sources', {}), 'bound_market_source_required')
            now = self.runtime._now()
            _require(as_of.astimezone(now.tzinfo).date() == now.date() and as_of <= now,
                     'entry_quote_day_or_time_mismatch')
            quotes = state.setdefault('entry_quotes', {})
            previous = quotes.get(symbol)
            # 현재 보존한 원관측의 ID만 대조한다. 전체 과거 replay 검출 원장이 아니다.
            if previous is not None and (previous['source'], previous['event_id']) == (source, event_id):
                _require(previous == row, 'entry_quote_event_conflict')
            _require(previous is None or datetime.fromisoformat(previous['as_of']) <= as_of,
                     'stale_entry_quote')
            quotes[symbol] = deepcopy(row)
            return state
        await self.owner.mutate('entry-quote:'+uuid4().hex, reduce)
        return self.owner.version

    async def publish_qualification_source(self, name, *, as_of, digest, expected_version):
        with self.runtime.command_scope():
            return await self._publish_qualification_source(name, as_of=as_of, digest=digest,
                                                            expected_version=expected_version)

    async def _publish_qualification_source(self, name, *, as_of, digest, expected_version):
        for value in (name, digest): _text(value)
        _require(type(as_of) is datetime and as_of.utcoffset() is not None,
                 'invalid_qualification_source_time')
        def reduce(state):
            self._owner_ready(state)
            self._version(expected_version)
            now = self.runtime._now()
            _require(as_of.astimezone(now.tzinfo).date() == now.date() and as_of <= now,
                     'qualification_source_day_or_time_mismatch')
            sources = state.setdefault('qualification_sources', {})
            previous = sources.get(name)
            _require(previous is None or datetime.fromisoformat(previous['as_of']) <= as_of,
                     'stale_qualification_source_observation')
            # 출처별 단조 counter다. 무관한 fill/ACK로 커지는 owner.version과 구분한다.
            version = 1 if previous is None else previous['version'] + 1
            sources[name] = dict(version=version, as_of=as_of.isoformat(), digest=digest)
            return state
        await self.owner.mutate('qualification-source:'+uuid4().hex, reduce)
        return self.owner.version

    async def publish_decision_facts(self, facts, *, expected_version):
        with self.runtime.command_scope():
            return await self._publish_decision_facts(facts, expected_version=expected_version)

    async def _publish_decision_facts(self, facts, *, expected_version):
        _require(type(facts) is EntryDecisionFacts, 'invalid_decision_facts')
        facts = EntryDecisionFacts.from_dict(facts.to_dict())
        row = facts.to_dict()
        def reduce(state):
            self._owner_ready(state)
            self._version(expected_version)
            now = self.runtime._now()
            _require(facts.decided_at.astimezone(now.tzinfo).date() == now.date()
                     and facts.expires_at.astimezone(now.tzinfo).date() == now.date()
                     and facts.decided_at <= now < facts.expires_at,
                     'decision_facts_day_or_time_mismatch')
            self._consumed_sources(state, facts)
            published = state.setdefault('entry_decision_facts', {})
            previous = published.get(facts.intent_id)
            # 같은 intent로 다른 내용을 다시 굳힐 수 없다. 동일 재게시는 무해하다.
            _require(previous is None or self.owner._same(previous, row), 'decision_facts_conflict')
            published[facts.intent_id] = deepcopy(row)
            return state
        await self.owner.mutate('decision-facts:'+uuid4().hex, reduce)
        return self.owner.version

    def _consumed_sources(self, state, facts):
        """소비했다고 기록한 출처만 현재 게시본과 대조한다. 미소비 출처 변화는 stale이 아니다.

        version·digest만으로는 게시 시각이 구속되지 않는다. 게시본 as_of와 facts의
        source.as_of가 같아야 하고, 그 게시본이 당일(KST)이어야 하며, 판단 시점보다
        미래의 출처를 소비했다고 적을 수 없다.
        """
        published = state.get('qualification_sources', {})
        now = self.runtime._now()
        for source in facts.sources:
            current = published.get(source.name)
            _require(current is not None and current['version'] == source.version
                     and current['digest'] == source.digest, 'stale_qualification_source')
            published_at = datetime.fromisoformat(current['as_of'])
            _require(published_at == source.as_of
                     and published_at.astimezone(now.tzinfo).date() == now.date()
                     and source.as_of <= facts.decided_at, 'stale_qualification_source')

    def read_qualification_source(self, name):
        """게시본의 읽기 전용 사본. 어댑터의 idempotent 재사용이 이것만 본다."""
        return deepcopy(self.owner.state.get('qualification_sources', {}).get(name))

    def _recheck_regime(self, state, facts):
        """게시자 자기신고가 아닌 유일한 stale 축이다. 지금 다시 유도해 소비 digest와 대조한다.

        effective_regime은 I/O도 await도 없는 순수 함수라 final 동기 구간에서 부를 수 있다.
        regime_policy가 없는 state에서는 재유도 자체가 불가능하므로 소비 사실을 거부한다.
        """
        consumed = next((source for source in facts.sources if source.name == 'regime'), None)
        if consumed is None:
            return
        _require('regime_policy' in state, 'stale_regime_decision')
        # 판단 시각이 아니라 현재 시각으로 유도한다. 당일 게이트가 지금 기준으로 열려야 한다.
        current = effective_regime(state, self.runtime._now())
        _require(_digest(canonical({'effective_regime': current})) == consumed.digest,
                 'stale_regime_decision')

    def _decision_facts(self, state, request, context, snapshot, resources, sector):
        """자동 매수만 소비한다. 경제값은 전부 현재 snapshot에서 다시 읽는다."""
        row = state.get('entry_decision_facts', {}).get(request.intent_id)
        _require(row is not None, 'decision_facts_required')
        facts = EntryDecisionFacts.from_dict(deepcopy(row))
        _require((facts.intent_id, facts.symbol, facts.side, facts.strategy, facts.origin)
                 == (request.intent_id, request.symbol, request.side.value, request.strategy,
                     context.origin.value), 'decision_facts_mismatch')
        _require(sector is None or sector == facts.sector, 'decision_facts_sector_mismatch')
        _require((facts.stop_pct, facts.stop_source, facts.stop_crash_capped)
                 == (resources.stop_pct, resources.stop_source, resources.stop_crash_active),
                 'decision_stop_changed')
        now = self.runtime._now()
        _require(facts.expires_at.astimezone(now.tzinfo).date() == now.date() and now < facts.expires_at,
                 'decision_facts_expired')
        _require(facts.config_version == snapshot.versions.config, 'stale_decision_config_version')
        self._consumed_sources(state, facts)
        self._recheck_regime(state, facts)
        result = recompose_quantity(facts, snapshot, price=resources.valuation_price)
        _require(result.reason is None and request.quantity <= result.quantity,
                 'decision_quantity_unjustified')
        # 설정 hybrid 축의 유일한 대조 상대다. 자기신고만 믿으면 '설정 on·신고 off'가 통과한다.
        # 신고 on 은 재구성 자체가 거부하므로(unsupported_hybrid_sizing) 그 뒤에서 본다.
        _require(facts.hybrid_enabled == snapshot.policy.hybrid_enabled, 'decision_hybrid_mismatch')
        return facts

    def _snapshot(self, state, *, exclude_attempt=None):
        if 'regime_policy' in state:
            writer = getattr(self.runtime, '_regime_writer', None)
            _require(writer is not None, 'regime_owner_not_installed')
            ref = state['regime_policy']['trend_state']['source_refs']['trend']
            _require(ref is not None, 'regime_source_not_current')
            read = writer.sources.read_source('index_trend', expected_version=ref['committed_version'])
            _require(read.authority_status == 'current' and read.terminal_status == 'accepted',
                     'regime_source_not_current')
        context = PolicyContext.from_dict(state['entry_policy_context'])
        prices = {symbol: self.runtime._view_price(state, symbol, Decimal(row['current_price']))
                  for symbol, row in state['portfolio']['positions'].items()}
        return build_owned_snapshot(state, context=context, version=self.owner.version,
            now=self.runtime._now(), prices=prices, exclude_attempt=exclude_attempt)

    def _parent(self, state, request):
        parent = request.parent
        _require(parent is not None, 'cancel_parent_required')
        current = state['attempts'].get(parent.attempt_id)
        _require(current is not None and current.get('request_binding') is not None,
                 'cancel_parent_binding_required')
        binding = current['request_binding']
        _require(current['version'] == parent.version and current['kind'] == 'submit'
                 and current['state'] not in TERMINAL_STATES and not current.get('evidence_conflict')
                 and current['intent_id'] == parent.intent_id == request.intent_id
                 and current['order_ref'] == parent.order_ref.to_dict()
                 and current['symbol'] == parent.symbol == request.symbol
                 and current['side'] == parent.side.value == request.side.value
                 and current['strategy'] == parent.strategy == request.strategy
                 and binding['order_type'] == parent.order_type.value
                 and Decimal(binding['valuation_price']) == parent.valuation_price
                 and current['reserved_quantity'] == parent.remaining_quantity == request.quantity
                 and current['quantity'] - current['applied_quantity'] == parent.remaining_quantity
                 and current['observed_quantity'] == current['applied_quantity'], 'cancel_parent_changed')
        _amount(current['reserved_cash'])
        _amount(current['reserved_exposure'])
        if current['reserved_planned_risk'] is not None: _amount(current['reserved_planned_risk'])
        resources = binding['resources']
        for field, basis in (('reserved_cash', 'cash'), ('reserved_exposure', 'exposure'),
                             ('reserved_planned_risk', 'planned_risk')):
            original = resources[basis]
            if original is None:
                _require(current[field] is None, 'cancel_parent_reservation_changed')
            else:
                original, remaining = _amount(original), _amount(current[field])
                minimum = remaining_resource_amount(original, before_quantity=current['quantity'],
                                                    after_quantity=parent.remaining_quantity)
                _require(minimum <= remaining <= original, 'cancel_parent_reservation_changed')
        return current

    def _evaluate(self, state, request, context, sector, *, exclude_attempt=None):
        self._owner_ready(state)
        if request.command is CommandKind.SUBMIT:
            _require(not self.runtime.market_source_pending(state), 'market_source_pending')
        self._session(request)
        _require(self.authority.owns(context), 'untrusted_entry_context')
        _require(request.command is not CommandKind.MODIFY, 'unsupported_modify_contract')
        snapshot = self._snapshot(state, exclude_attempt=exclude_attempt)
        active = []
        for aid, attempt in state['attempts'].items():
            if aid == exclude_attempt: continue
            if attempt['kind'] != 'submit':
                if request.command is CommandKind.SUBMIT and attempt['symbol'] == request.symbol:
                    _require(attempt.get('command_status') in ('not_sent', 'rejected'), 'unresolved_child_attempt')
                continue
            _require(attempt['observed_quantity'] == attempt['applied_quantity']
                     and not attempt.get('evidence_conflict')
                     and attempt['state'] != 'blocked_unknown', 'unresolved_execution_evidence')
            remaining = has_remaining_reservation(attempt)
            if attempt['state'] in TERMINAL_STATES:
                _require(not remaining, 'terminal_reservation_remaining')
                continue
            _require(attempt.get('request_binding') is not None, 'unbound_active_attempt')
            if request.command is CommandKind.SUBMIT:
                _require(attempt['symbol'] != request.symbol, 'unresolved_symbol_attempt')
            active.append(attempt)
        if request.command is CommandKind.CANCEL:
            self._parent(state, request)
        elif request.side is OrderSide.BUY:
            _require(source_is_current(state, request.symbol), 'current_market_source_required')
            quote = state.get('entry_quotes', {}).get(request.symbol)
            now = self.runtime._now()
            _require(quote is not None and Decimal(quote['price']) == request.valuation_price
                     and datetime.fromisoformat(quote['as_of']).astimezone(now.tzinfo).date() == now.date()
                     and datetime.fromisoformat(quote['as_of']) <= now, 'current_entry_quote_required')
        stop = None
        if (request.command is CommandKind.SUBMIT and request.side is OrderSide.BUY
                and context.origin is EntryOrigin.AUTOMATIC and request.strategy != 'core_holding'
                and snapshot.policy.sizing_mode == 'risk'):
            stop = self.stop_resolver(request.strategy)
        resources = calculate_resources(request, builder=self.builder, policy=snapshot.policy,
            equity=snapshot.portfolio.equity, origin=context.origin, stop_decision=stop)
        if request.command is CommandKind.CANCEL:
            return resources, None, snapshot, None
        facts = None
        if request.side is OrderSide.BUY and context.origin is EntryOrigin.AUTOMATIC:
            facts = self._decision_facts(state, request, context, snapshot, resources, sector)
            sector = facts.sector
        if request.side is OrderSide.BUY:
            reserved = sum((_amount(a['reserved_cash']) for a in active if a['side'] == 'buy'), Decimal('0'))
            _require(snapshot.portfolio.cash - reserved >= resources.cash, 'reserved_cash_insufficient')
        else:
            held = next((position.quantity for position in snapshot.portfolio.positions
                         if position.symbol == request.symbol), 0)
            reserved = sum(a['reserved_quantity'] for a in active
                           if a['side'] == 'sell' and a['symbol'] == request.symbol)
            _require(held - reserved >= request.quantity, 'reserved_quantity_insufficient')
        # Pending BUY는 기존 pre-candidate 슬롯의 현재 점유다. 후보 자신을 +1하지 않는다.
        positions = list(snapshot.portfolio.positions)
        held_symbols = {position.symbol for position in positions}
        for pending in snapshot.pending:
            if pending.side == 'buy' and pending.symbol not in held_symbols:
                positions.append(p.PositionPolicyFact(pending.symbol, pending.strategy, pending.sector,
                    0, Decimal('0'), snapshot.business_day, None, None, None, False))
                held_symbols.add(pending.symbol)
        gated = replace(snapshot, portfolio=replace(snapshot.portfolio, positions=tuple(positions)))
        entry = p.EntryPolicyInput(request.symbol, request.side, request.quantity,
                                   resources.valuation_price, request.strategy, sector)
        decision = p.evaluate_entry_policy(entry, gated, now=self.runtime._now(), origin=context.origin)
        _require(decision.allowed, decision.reason.value)
        alpha = self.entry_guard.evaluate(context)
        _require(type(alpha) is GuardDecision and alpha.allowed is True, alpha.reason)
        if context.origin is EntryOrigin.AUTOMATIC and request.side is OrderSide.BUY:
            _require(not state['protection']['degraded'], 'protection_degraded')
        return resources, decision, snapshot, facts

    @staticmethod
    def _effects(state, request, decision, *, commit):
        root = state.get('entry_policy_effects', {})
        if decision is None: return
        for effect in decision.effects:
            if effect.kind is p.EffectKind.SIDECAR_SET:
                if commit: root['sidecar_active'] = effect.value
                else: _require(root.get('sidecar_active') == effect.value, 'policy_effect_pending')
            elif effect.kind is p.EffectKind.PENDING_SECTOR_SET:
                if commit: root['pending_sectors'][request.symbol] = effect.value
                else: _require(root.get('pending_sectors', {}).get(request.symbol) == effect.value,
                               'policy_effect_pending')
            else:
                raise CommandValidationError('policy_effect_pending')

    async def prepare(self, request, context, *, sector=None, fill_metadata=None):
        with self.runtime.command_scope():
            return await self._prepare(request, context, sector=sector, fill_metadata=fill_metadata)

    async def _prepare(self, request, context, *, sector=None, fill_metadata=None):
        request, context = self._request(request, context)
        if sector is not None: _text(sector)
        # 자동 BUY의 점수 존재 강제는 여기가 아니라 게이트웨이에 있다 — prepare에 걸면 기존
        # 직접 호출자(시험 46건)의 기대값이 바뀐다. 게이트 이관은 P0-3.
        metadata = _fill_metadata(fill_metadata)
        def reduce(state):
            resources, decision, snapshot, facts = self._evaluate(state, request, context, sector)
            # sector 정본은 facts다. 호출자 kwarg는 같거나 None만 허용한다.
            bound_sector = sector if facts is None else facts.sector
            parent = request.parent
            self.runtime.lifecycle.prepare_candidate(state, request.intent_id, request.attempt_id,
                request.quantity, request.symbol, request.side.value, command=request.command,
                parent_attempt_id=None if parent is None else parent.attempt_id,
                order_ref=None if parent is None else parent.order_ref, reserved_cash=str(resources.cash),
                origin=context.origin.value, strategy=request.strategy)
            attempt = state['attempts'][request.attempt_id]
            attempt['sector'] = bound_sector
            attempt['reserved_exposure'] = str(resources.exposure)
            attempt['reserved_planned_risk'] = None if resources.planned_risk is None else str(resources.planned_risk)
            attempt['request_binding'] = {
                'fingerprint': request.fingerprint, 'account_scope': request.account.account_scope,
                'business_day': request.session.business_date_kst, 'command': request.command.value,
                'symbol': request.symbol, 'side': request.side.value, 'order_type': request.order_type.value,
                'strategy': request.strategy, 'quantity': request.quantity,
                'valuation_price': str(request.valuation_price), 'wire_price': str(request.wire_price),
                'origin': context.origin.value, 'sector': bound_sector,
                # 체결 관측이 읽을 두 값. 세션은 응답에 없고 metadata는 응답에서 추측하지 않는다.
                'session': request.session.session, 'fill_metadata': deepcopy(metadata),
                'source_versions': asdict(snapshot.versions),
                'decision_facts_digest': None if facts is None else facts.digest,
                'resources': resources.to_dict(), 'parent_version': None if parent is None else parent.version,
            }
            self._effects(state, request, decision, commit=True)
            return state
        await self.owner.mutate('bound-prepare:'+uuid4().hex, reduce)
        return self.owner.state['attempts'][request.attempt_id]

    def _bound(self, state, request, context, binding, *, claim=None, version=None):
        self._owner_ready(state, dispatch=True)
        attempt = state['attempts'].get(request.attempt_id)
        _require(attempt is not None and self.owner._same(attempt.get('request_binding'), binding)
                 and binding['fingerprint'] == request.fingerprint
                 and binding['origin'] == context.origin.value, 'request_binding_changed')
        expected = {'fingerprint': request.fingerprint, 'account_scope': request.account.account_scope,
            'business_day': request.session.business_date_kst, 'command': request.command.value,
            'symbol': request.symbol, 'side': request.side.value, 'order_type': request.order_type.value,
            'strategy': request.strategy, 'quantity': request.quantity,
            'valuation_price': str(request.valuation_price), 'wire_price': str(request.wire_price),
            'origin': context.origin.value,
            'parent_version': None if request.parent is None else request.parent.version}
        _require(all(self.owner._same(binding.get(key), value) for key, value in expected.items()),
                 'request_binding_changed')
        _require(type(attempt['quantity']) is int and type(attempt['reserved_quantity']) is int
                 and type(attempt['version']) is int and attempt['quantity'] == request.quantity
                 and attempt['origin'] == context.origin.value and attempt['symbol'] == request.symbol
                 and attempt['side'] == request.side.value and attempt['strategy'] == request.strategy
                 and attempt['intent_id'] == request.intent_id and attempt['kind'] == request.command.value
                 and attempt['observed_quantity'] == attempt['applied_quantity'] == 0,
                 'request_attempt_changed')
        if claim is not None:
            _require(attempt['claim_id'] == claim and attempt['version'] == version
                     and attempt['command_status'] is None
                     and attempt['state'] in ('submitting', 'cancel_requested'), 'sender_claim_invalid')
        resources, decision, _, facts = self._evaluate(state, request, context, binding['sector'],
                                                       exclude_attempt=request.attempt_id)
        _require(self.owner._same(resources.to_dict(), binding['resources']), 'current_resources_changed')
        _require(self.owner._same(binding.get('decision_facts_digest'),
                                  None if facts is None else facts.digest), 'decision_facts_changed')
        _require(attempt['reserved_quantity'] == (request.quantity if request.command is CommandKind.SUBMIT else 0)
                 and _amount(attempt['reserved_cash']) == resources.cash
                 and _amount(attempt['reserved_exposure']) == resources.exposure
                 and attempt['reserved_planned_risk'] == (None if resources.planned_risk is None
                                                          else str(resources.planned_risk)), 'reservation_changed')
        self._effects(state, request, decision, commit=False)

    def _ack(self, request, response):
        if response.status is not TransportStatus.ACKNOWLEDGED:
            return CommandResult(CommandStatus(response.status.value), request.attempt_id, reason_code=response.reason)
        try:
            output = response.data['output']
            order_no = output.get('ODNO', output.get('odno'))
            org_no = output.get('KRX_FWDG_ORD_ORGNO', output.get('ORGNO'))
            _text(order_no)
            _text(org_no)
            ref = OrderRef(request.account.account_scope, request.market,
                request.session.business_date_kst, 'KRX', order_no, org_no,
                '' if request.parent is None else request.parent.order_ref.order_no)
            return CommandResult(CommandStatus.ACKNOWLEDGED, request.attempt_id, ref, response.reason)
        except (KeyError, TypeError, AttributeError, ValueError):
            return CommandResult(CommandStatus.UNKNOWN, request.attempt_id, reason_code='ack_identity_missing_or_invalid')

    async def _record(self, request, claim, version, result, command_token):
        task = self.runtime.start_command_result(command_token,
            lambda: self.runtime.lifecycle.record_result(request.attempt_id, claim, result,
                expected_attempt_version=version))
        self._result_tasks.add(task)
        def done(task):
            self._result_tasks.discard(task)
            if task.cancelled() or task.exception() is not None or task.result() is not True:
                self.owner._block()
        task.add_done_callback(done)
        accepted = await asyncio.shield(task)
        if not accepted:
            self.owner._block()
            return CommandResult(CommandStatus.UNKNOWN, request.attempt_id, reason_code='result_not_recorded')
        return result

    async def _unsent(self, request, reason):
        """claim 이전에 끝난 요청. SUBMIT 은 같은 호출 안에서 예약까지 푼다(결정 ⑦·⑧).

        자식 명령(cancel/modify)도 같은 길로 보낸다(S4-1b). 누가 실제로 끝날 수 있는지는
        lifecycle 의 abandon 가드 한 곳이 정한다 — 여기서 kind 로 다시 판정하면 '이미
        보냈을 수 있는' 행의 판단이 두 곳으로 갈린다. claim 이후 실패는 여기 오지 않는다.
        """
        released = False
        try:
            released = await self.runtime.lifecycle.abandon_candidate(request.attempt_id,
                                                                      reason=reason)
        except ApplicationBlocked:
            raise
        except Exception:
            # 저장된 행이 깨져 있어도 '보내지 못했다'는 결과는 그대로다. 삼키지 않고 남긴다.
            logger.exception('[실행] 미송신 시도 해제 예외: attempt={} 사유={}',
                             request.attempt_id, reason)
        if released is not True:
            logger.warning('[실행] 미송신 시도 예약 유지: attempt={} 사유={}',
                           request.attempt_id, reason)
        return CommandResult(CommandStatus.NOT_SENT, request.attempt_id, reason_code=reason)

    async def dispatch(self, request, context, transport):
        try:
            with self.runtime.command_scope() as command_token:
                return await self._dispatch(request, context, transport, command_token)
        except ApplicationBlocked:
            # 공개 결과 타입 유지. 종료 중에는 claim/POST를 새로 시작하지 않는다.
            return CommandResult(CommandStatus.NOT_SENT, request.attempt_id,
                                 reason_code='command_admission_closed')

    async def _dispatch(self, request, context, transport, command_token):
        # 第一 await 앞에 request/권한을 검증한다. 다른 실행 경로에 permit을 주지 않는다.
        # 신원·권한 위반은 호출자 결함이라 그대로 던진다. 세션은 여기서 보지 않는다 — prepare 뒤에
        # 장 경계가 닫힌 요청은 아래 `_bound`→`_evaluate` 의 재검사에서 걸려 예약까지 끝나야 한다.
        request, context = self._request(request, context, session=False)
        _require(type(transport) is GuardedKISTransport, 'invalid_prepared_transport')
        state = self.owner.state
        attempt = state.get('attempts', {}).get(request.attempt_id)
        binding = None if attempt is None else deepcopy(attempt.get('request_binding'))
        claim = uuid4().hex
        try:
            _require(binding is not None, 'bound_attempt_required')
            self._bound(state, request, context, binding)
            claimed = await self.runtime.lifecycle.claim(request.attempt_id, claim,
                candidate_guard=lambda candidate: self._bound(candidate, request, context, binding))
            if not claimed:
                return await self._unsent(request, 'claim_not_available')
        except asyncio.CancelledError:
            raise
        except ApplicationBlocked:
            # 종료·일자 전환 중에는 새 종료 전이도 시작하지 않는다. 바깥이 사유를 정한다.
            raise
        except CommandValidationError as exc:
            return await self._unsent(request, exc.reason)
        except Exception:
            # 저장/전송 장애는 '보낼 수 없었다'는 판정이 아니다. 예약은 그대로 둔다.
            return CommandResult(CommandStatus.NOT_SENT, request.attempt_id, reason_code='dispatch_failed')
        version = self.owner.state['attempts'][request.attempt_id]['version']
        self._permits[claim] = (request.fingerprint, version)
        def guard(actual):
            try:
                self.builder.validate(actual)
                _require(actual.fingerprint == request.fingerprint, 'request_fingerprint_changed')
                _require(self._permits.get(claim) == (request.fingerprint, version), 'sender_permit_missing')
                self._bound(self.owner.state, actual, context, binding, claim=claim, version=version)
                del self._permits[claim]
                return GuardDecision(True, 'bound_request_permit_consumed')
            except Exception:
                return GuardDecision(False, 'current_request_guard_rejected')
        try:
            response = await transport.send_prepared(request, guard)
            result = self._ack(request, response)
        except asyncio.CancelledError as cancelled:
            self._permits.pop(claim, None)
            result = CommandResult(CommandStatus.UNKNOWN, request.attempt_id, reason_code='caller_cancelled')
            try:
                await self._record(request, claim, version, result, command_token)
            except BaseException:
                self.owner._block()
            finally:
                raise cancelled
        except Exception:
            result = CommandResult(CommandStatus.UNKNOWN, request.attempt_id, reason_code='transport_failed')
        finally:
            self._permits.pop(claim, None)
        try:
            return await self._record(request, claim, version, result, command_token)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.owner._block()
            return CommandResult(CommandStatus.UNKNOWN, request.attempt_id, reason_code='result_record_failed')
