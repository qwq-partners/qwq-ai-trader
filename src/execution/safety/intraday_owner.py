"""기존 5분 위험 writer의 명시 owner. 운영 factory/신호 전달은 아직 미설치다.

조회 성공은 시장 시각 증명이 아니다. 보호 후보와 미전달 effect만 같은 checkpoint에
저장한다. 최초 기준선은 외부에서 증명해야 하며 live normal/0을 인계로 채택하지 않는다.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from uuid import uuid4

from .application import ApplicationBlocked
from .economics import decode_portfolio
from .protection_recovery import capture_intraday_transition, digest
from .risk_sources import RiskSourceCoordinator
from .risk_transition import IntradayPolicyState, transition_intraday, _COOLDOWN
from .stale_exit_candidate import select_preemptive_stale


def _calendar(state, today):
    """기존 메모리 휴장일 판단을 I/O 없이 명시 입력으로 고정한다."""
    from ...utils.session import is_kr_market_holiday
    positions = decode_portfolio(state['portfolio']).positions.values()
    start = min((p.entry_time.date() for p in positions if p.entry_time), default=today)
    start = min(start, today)
    dates = [start + timedelta(days=i) for i in range((today - start).days + 1)]
    return {'source': 'legacy_session_memory', 'start': start.isoformat(),
            'end': today.isoformat(),
            'holidays': [d.isoformat() for d in dates if is_kr_market_holiday(d)]}


def validate_intraday_policy(state, version):
    """source→정책 전이→outbox 연결을 live 게시 전에 확인한다."""
    root = state.get('intraday_policy')
    if root is None:
        return None
    if (type(root) is not dict or set(root) != {
            'schema', 'baseline', 'baseline_version', 'current', 'transitions'}
            or type(root['schema']) is not int or root['schema'] != 1
            or type(root['baseline_version']) is not int
            or not 0 < root['baseline_version'] <= version
            or type(root['transitions']) is not dict):
        raise ValueError('invalid_intraday_baseline')
    previous = IntradayPolicyState.from_dict(root['baseline'])
    previous_version = root['baseline_version']
    sources = state.get('risk_sources', {}).get('records', {})
    expected_effects = set()
    for operation, row in sorted(root['transitions'].items(), key=lambda x: x[1]['version']):
        if (type(row) is not dict or set(row) != {
                'version', 'before', 'after', 'outcome_digest', 'disposition', 'effects'}
                or type(row['version']) is not int or not previous_version < row['version'] <= version
                or row['before'] != previous.to_dict() or type(row['effects']) is not dict):
            raise ValueError('invalid_intraday_transition')
        source = sources.get(operation, {})
        terminal = source.get('terminal') or {}
        receipt, envelope = terminal.get('receipt', {}), terminal.get('envelope', {})
        if (source.get('ticket', {}).get('kind') != 'intraday_5m'
                or receipt.get('status') != 'accepted'
                or receipt.get('committed_version') != row['version']
                or receipt.get('outcome_digest') != row['outcome_digest']):
            raise ValueError('intraday_source_crosslink_conflict')
        after = IntradayPolicyState.from_dict(row['after'])
        if row['disposition'] == 'applied':
            payload = envelope['payload']
            classified = datetime.fromisoformat(envelope['classified_at'])
            recovery = (classified + _COOLDOWN if payload['level'] == 'normal'
                        and previous.level != 'normal' else previous.recovery_until)
            expected = IntradayPolicyState(payload['level'], payload['change_pct'], classified, recovery)
            if after != expected:
                raise ValueError('intraday_policy_source_conflict')
        elif row['disposition'] in {'duplicate_observation', 'older_observation'}:
            if after != previous or row['effects']:
                raise ValueError('ignored_intraday_changed_policy')
        else:
            raise ValueError('invalid_intraday_disposition')
        for key, effect_digest in row['effects'].items():
            effect = state['outbox'].get(key)
            if (type(effect) is not dict or digest(effect) != effect_digest
                    or effect.get('kind') != 'protection_decision'
                    or effect.get('effect_source') != 'intraday_preemptive'
                    or effect.get('source_operation') != operation
                    or effect.get('source_version') != row['version']
                    or effect.get('status') != 'pending'
                    or key != 'intraday:' + digest([operation, effect.get('symbol')])):
                raise ValueError('intraday_effect_crosslink_conflict')
            expected_effects.add(key)
        previous, previous_version = after, row['version']
    accepted = {op for op, row in sources.items() if row['ticket']['kind'] == 'intraday_5m'
                and row['ticket']['admission_version'] > root['baseline_version']
                and row['terminal'] and row['terminal']['receipt']['status'] == 'accepted'}
    if (accepted != set(root['transitions']) or previous.to_dict() != root['current']
            or previous.level != state['protection']['intraday_crash_level']
            or expected_effects != {key for key, row in state['outbox'].items()
                                   if row.get('effect_source') == 'intraday_preemptive'}):
        raise ValueError('intraday_policy_projection_conflict')
    return previous


class IntradayRiskOwner:
    """명시 설치된 실제 batch mirror와 기존 owner를 연결한다. 자동 설치 없음."""

    def __init__(self, runtime, batch):
        if (getattr(runtime, '_intraday_writer', None) is not None
                or getattr(batch, '_engine', None) is not runtime.engine
                or getattr(batch, '_exit_manager', None) is not runtime.exit_manager):
            raise ApplicationBlocked('intraday_owner_binding_conflict')
        runtime.owner._require_ready()
        policy = validate_intraday_policy(runtime.owner.state, runtime.owner.version)
        if policy is None:
            raise ApplicationBlocked('intraday_baseline_required')
        self.runtime, self.batch = runtime, batch
        self.sources = RiskSourceCoordinator(runtime, completion_reducer=self._reduce)
        self.publish(policy, self.projection(policy))
        runtime._intraday_writer = self

    def projection(self, policy):
        if policy is None:
            raise ValueError('installed_intraday_baseline_missing')
        adapter = getattr(self.runtime.engine, '_regime_adapter', None)
        if adapter is None:
            return None
        return replace(adapter._horizons, intraday_risk=policy.level,
            intraday_change_pct=policy.kospi_pct, intraday_risk_as_of=policy.updated_at)

    def publish(self, policy, horizons):
        # 全 decode는 runtime에서 이 호출 전에 완료된다. await/legacy 파일 I/O 없음.
        batch = self.batch
        batch._intraday_state = policy.level
        batch._intraday_kospi_pct = policy.kospi_pct
        batch._intraday_updated_at = policy.updated_at
        batch._intraday_recovery_until = policy.recovery_until
        adapter = getattr(self.runtime.engine, '_regime_adapter', None)
        if adapter is not None and horizons is not None:
            # 이 세 필드만 owner projection. trend/expert/LLM writer는 별도 이행 대상.
            # legacy horizon의 as_of는 분류 시각이며 시장시각으로 사용하지 않는다.
            adapter._horizons = horizons

    def snapshot(self):
        result = self.sources.snapshot()
        if result.observation_status != 'success':
            return result
        policy = IntradayPolicyState.from_dict(self.runtime.owner.state['intraday_policy']['current'])
        return replace(result, level=policy.level, recovery_until=policy.recovery_until)

    def _reduce(self, state, ticket, envelope, version):
        if ticket.kind != 'intraday_5m':
            raise ValueError('unsupported_intraday_policy_writer')
        checkpoint_before = deepcopy(state)
        root = state['intraday_policy']
        before = IntradayPolicyState.from_dict(root['current'])
        payload = envelope['payload']
        disposition, effects = 'applied', {}
        sources = state['risk_sources']['records']
        previous_envelopes = [sources[op]['terminal']['envelope'] for op in root['transitions']]
        for prior in previous_envelopes:
            if prior['source_event_id'] == envelope['source_event_id']:
                if prior['payload']['observation_json'] != payload['observation_json']:
                    raise ValueError('intraday_original_observation_conflict')
                disposition = 'duplicate_observation'
        if disposition == 'applied' and any(
                datetime.fromisoformat(p['received_at']) > datetime.fromisoformat(envelope['received_at'])
                for p in previous_envelopes):
            disposition = 'older_observation'
        after = before
        if disposition == 'applied':
            classified = datetime.fromisoformat(envelope['classified_at'])
            transition = transition_intraday(before, state['protection'],
                change_pct=payload['change_pct'], classified_at=classified)
            after = transition.state
            if after.level != payload['level']:
                raise ValueError('intraday_classification_conflict')
            if transition.preemptive_stale_required:
                calendar = payload['calendar']
                today = classified.date()
                portfolio = decode_portfolio(state['portfolio'])
                start = date.fromisoformat(calendar['start'])
                if (calendar['end'] != today.isoformat() or any(
                        p.entry_time and p.entry_time.date() < start for p in portfolio.positions.values())):
                    raise ValueError('intraday_calendar_snapshot_incomplete')
                prices = {symbol: self.runtime._view_price(state, symbol, p.current_price)
                          for symbol, p in portfolio.positions.items()}
                candidates = select_preemptive_stale(state['portfolio'], transition.protection_dto,
                    today=today, prices=prices,
                    holidays=frozenset(date.fromisoformat(d) for d in calendar['holidays']))
                for candidate in candidates:
                    key = 'intraday:' + digest([ticket.operation_id, candidate.symbol])
                    body = asdict(candidate); body['price'] = str(candidate.price)
                    effect = {'kind': 'protection_decision', 'effect_source': 'intraday_preemptive',
                        'symbol': candidate.symbol, 'intent_id': None,
                        'decision': ['sell', candidate.quantity, candidate.reason], 'candidate': body,
                        'status': 'pending', 'source_operation': ticket.operation_id,
                        'source_version': version, 'observed_at': envelope['classified_at'],
                        'provenance': {key: envelope[key] for key in (
                            'source', 'source_event_id', 'received_at', 'market_as_of')}}
                    state['outbox'][key] = effect
                    effects[key] = digest(effect)
            state['protection'] = transition.protection_dto
        root['current'] = after.to_dict()
        root['transitions'][ticket.operation_id] = {
            'before': before.to_dict(), 'after': after.to_dict(), 'version': version,
            'outcome_digest': digest(envelope), 'disposition': disposition, 'effects': effects}
        capture_intraday_transition(checkpoint_before, state, ticket=ticket, envelope=envelope,
                                    version=version, now=self.runtime._now())
        return state

    async def refresh(self, provider):
        """실제 scheduler 1회 조회. begin은 GET보다 먼저, receipt 뒤에만 mirror 갱신."""
        from .index_risk_input import normalize_index_risk
        runtime = self.runtime
        with runtime.command_scope() as token:
            ticket = await self.sources.begin('intraday-5m:' + uuid4().hex, 'intraday_5m')
            try:
                quote = await provider.fetch_index_price('0001') if provider is not None else None
            except asyncio.CancelledError:
                await self.sources.complete(ticket, 'cancelled', scope_token=token)
                raise
            except Exception:
                # 외부 예외 본문/자격/계좌 정보를 원장에 보존하지 않는다.
                return await self.sources.complete(ticket, 'failed', scope_token=token)
            now = runtime._now()
            fact = normalize_index_risk(quote, now=now, business_day=ticket.business_day)
            if fact.outcome != 'success':
                return await self.sources.complete(ticket, 'missing', scope_token=token)
            payload = {'level': fact.level, 'change_pct': fact.change_pct,
                       'observation_json': fact.observation_json,
                       'calendar': _calendar(runtime.owner.state, now.date())}
            return await self.sources.complete(ticket, 'success', payload,
                source=fact.source, source_event_id=fact.source_event_id,
                received_at=fact.received_at, market_as_of=fact.market_as_of,
                classified_at=now, scope_token=token)
