"""C3 source와 application 수명. 모든 canonical 쓰기는 기존 owner가 직렬화한다."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

from .application import ApplicationBlocked
from .policy_generations import canonical, versioned_fact
from .protection_recovery import digest
from .regime_horizon import RegimeStageInput, RegimeSyncContext, RegimeApplicationReceipt, fold_horizon
from .regime_application import (CLASSIFIER_READS, HORIZON_READS, PROTECTION_READS,
    classifier_ref, append_application)
from .index_risk_input import normalize_index_risk


def require_c3(owner):
    if owner.runtime.owner.state['regime_policy']['schema'] != 2:
        raise ApplicationBlocked('regime_horizon_baseline_required')
    for name in CLASSIFIER_READS: versioned_fact(owner.runtime.owner.state, name)


def context_for(value, now):
    from .regime_owner import _time
    if type(value) is not RegimeSyncContext: raise ValueError('regime_application_request_conflict')
    result = RegimeSyncContext.from_dict(value.to_dict()).to_dict()
    when = _time(result['captured_at']); observed = result['screener']['observed_at']
    if (when > now or when.date() != now.date() or observed is not None and _time(observed) > now):
        raise ValueError('regime_application_request_conflict')
    return result


async def stage_read(owner, provider, name, *, asynchronous=True, code=None):
    from .regime_owner import _time
    try:
        fn = getattr(provider, name)
        value = fn(code) if code else fn()
        if asynchronous: value = await value
        if type(value) is not RegimeStageInput: raise ValueError('regime_stage_required')
        result = RegimeStageInput.from_dict(value.to_dict()).to_dict()
        expected = {'read_daily_bias': 'daily_bias', 'fetch_us_overnight': 'us_overnight',
                    'snapshot_screener': 'screener'}.get(name, 'index' + (code or ''))
        if result['stage'] != expected: raise ValueError('regime_stage_mismatch')
        times = [result['received_at'], result['market_as_of']]
        if expected == 'screener' and result['payload']: times.append(result['payload']['loaded_at'])
        if any(_time(t) > owner.runtime._now() for t in times if t is not None):
            raise ValueError('future_regime_stage')
        return result
    except Exception:
        stage = {'read_daily_bias': 'daily_bias', 'fetch_us_overnight': 'us_overnight',
                 'snapshot_screener': 'screener'}.get(name, 'index' + (code or ''))
        return RegimeStageInput.from_dict({'schema': 1, 'stage': stage, 'outcome': 'failed',
            'source': name, 'event_id': None, 'received_at': None, 'market_as_of': None, 'payload': None}).to_dict()


def noon_candidate(reads, fact, now):
    from .regime_owner import _time
    from ...core.market_regime import max_intraday_level
    policies = reads['versioned_policies']
    batch, horizon = policies['intraday_policy.current']['value'], policies['regime_policy.horizon']['value']
    batch_level = batch['level'] if batch['updated_at'] and _time(batch['updated_at']).date() == now.date() else None
    prior = horizon['level'] if horizon['classified_at'] and _time(horizon['classified_at']).date() == now.date() else None
    return {'level': max_intraday_level(batch_level, fact.level, prior), 'change_pct': fact.change_pct,
            'classified_at': now.isoformat()}


async def classify(owner, label, provider, llm):
    from .regime_owner import _OwnerBeginCancelled
    from .regime_classifier import prompt_inputs, projected_result
    from ...schedulers.kr_scheduler import _INTRADAY_REFRESH_FROM, _INTRADAY_REFRESH_UNTIL
    from ...utils.llm import LLMTask
    from .risk_input_seal import _context_reason
    require_c3(owner)
    runtime = owner.runtime
    with runtime.command_scope() as token:
        ticket = await owner._begin('llm-regime:' + uuid4().hex, 'llm_regime', token, require_seal=True)
        terminal_started = False
        try:
            stages = {'daily_bias': await stage_read(owner, provider, 'read_daily_bias')}
            now = runtime._now()
            stages['us_overnight'] = await stage_read(owner, provider, 'fetch_us_overnight')
            stages['screener'] = await stage_read(owner, provider, 'snapshot_screener', asynchronous=False)
            noon_ref = None
            if _INTRADAY_REFRESH_FROM <= (now.hour, now.minute) <= _INTRADAY_REFRESH_UNTIL:
                noon = await owner._begin('noon-index:' + uuid4().hex, 'noon_index', token, require_seal=True)
                try:
                    for code in ('0001', '1001'):
                        stages['index' + code] = await stage_read(owner, provider, 'fetch_index_price', code=code)
                    quote = (stages['index0001']['payload'] or {}).get('quote')
                    fact = normalize_index_risk(quote, now=runtime._now(), business_day=noon.business_day)
                    if fact.outcome != 'success' or _context_reason(runtime, runtime.owner.state, noon):
                        receipt = await owner.sources.complete(noon, 'missing', scope_token=token)
                    else:
                        reads_json = owner.sources.capture_reads(noon, versioned_policy_reads=HORIZON_READS)
                        candidate = noon_candidate(json.loads(reads_json), fact, now)
                        inputs = {'observation_json': fact.observation_json, 'candidate': candidate}
                        await owner.sources.seal(noon, versioned_policy_reads=HORIZON_READS,
                            inputs=inputs, expected_reads_json=reads_json, scope_token=token)
                        receipt = await owner.sources.complete(noon, 'success', {'level': candidate['level'],
                            'change_pct': fact.change_pct, **inputs}, source=fact.source,
                            source_event_id=fact.source_event_id, received_at=fact.received_at,
                            market_as_of=None, classified_at=now, scope_token=token)
                    noon_ref = {'operation_id': noon.operation_id, 'status': receipt.status,
                                'committed_version': receipt.committed_version, 'outcome_digest': receipt.outcome_digest}
                except asyncio.CancelledError:
                    owner._defer_cancel(noon.operation_id, noon.kind, token)
                    raise
            if noon_ref is not None and noon_ref['status'] == 'stale':
                if _context_reason(runtime, runtime.owner.state, ticket):
                    terminal_started = True
                    return await owner.sources.complete(ticket, 'missing', scope_token=token)
                from .risk_input_seal import _source_at
                reads = json.loads(owner.sources.capture_reads(ticket, source_lanes=('intraday',),
                    versioned_policy_reads=CLASSIFIER_READS))
                state = runtime.owner.state
                reads['sources']['intraday'] = _source_at(state['risk_sources']['records'][noon_ref['operation_id']],
                    noon_ref['committed_version'] + 1, state.get('risk_input_seals', {}).get('records', {}))
                await owner.sources.seal(ticket, source_lanes=('intraday',), versioned_policy_reads=CLASSIFIER_READS,
                    inputs={'noon_ref': noon_ref}, expected_reads_json=canonical(reads), scope_token=token)
                terminal_started = True
                return await owner.sources.complete(ticket, 'success', {}, scope_token=token)
            try:
                context = context_for(provider.snapshot_application_context(captured_at=runtime._now()), runtime._now())
            except (TypeError, ValueError, AttributeError, OverflowError):
                terminal_started = True
                return await owner.sources.complete(ticket, 'failed', scope_token=token)
            if _context_reason(runtime, runtime.owner.state, ticket):
                terminal_started = True
                return await owner.sources.complete(ticket, 'missing', scope_token=token)
            reads_json = owner.sources.capture_reads(ticket, source_lanes=('intraday',), versioned_policy_reads=CLASSIFIER_READS)
            if noon_ref is not None and noon_ref['status'] in {'accepted', 'stale'}:
                source = owner.sources.read_source('intraday', expected_version=noon_ref['committed_version'])
                if source.authority_status != 'current' or noon_ref['status'] == 'stale':
                    from .risk_input_seal import _source_at
                    # 실제 소비한 완료 noon 원 사실을 보존한다. 최신 사실을 옛 계산에 붙이지 않는다.
                    reads = json.loads(reads_json)
                    state = runtime.owner.state
                    reads['sources']['intraday'] = _source_at(state['risk_sources']['records'][noon_ref['operation_id']],
                        noon_ref['committed_version'] + 1, state.get('risk_input_seals', {}).get('records', {}))
                    await owner.sources.seal(ticket, source_lanes=('intraday',), versioned_policy_reads=CLASSIFIER_READS,
                        inputs={'noon_ref': noon_ref}, expected_reads_json=canonical(reads), scope_token=token)
                    terminal_started = True
                    return await owner.sources.complete(ticket, 'success', {}, scope_token=token)
            try:
                from .regime_owner import _time
                prompt, meta = prompt_inputs(label, stages, json.loads(reads_json)['versioned_policies'], now,
                    captured_at=_time(context['captured_at']))
            except (TypeError, ValueError, AttributeError, OverflowError):
                terminal_started = True
                return await owner.sources.complete(ticket, 'failed', scope_token=token)
            inputs = {'label': label, 'stages': stages, 'classified_at': now.isoformat(),
                      'context': context, 'noon_ref': noon_ref, 'prompt': prompt, 'input_meta': meta}
            await owner.sources.seal(ticket, source_lanes=('intraday',), versioned_policy_reads=CLASSIFIER_READS,
                inputs=inputs, expected_reads_json=reads_json, scope_token=token)
            try:
                raw = await asyncio.wait_for(llm.complete_json(prompt=prompt,
                    system='한국 주식시장 레짐 분류 전문가. JSON만 응답.', task=LLMTask.QUICK_ANALYSIS), timeout=15.0)
                if type(raw) is not dict or not raw or 'error' in raw: raise ValueError('invalid_classifier_response')
                raw_json = canonical(raw)
                result = projected_result(raw, meta, now)
                canonical(result)
            except Exception:
                terminal_started = True
                return await owner.sources.complete(ticket, 'failed', scope_token=token)
            terminal_started = True
            receipt = await owner.sources.complete(ticket, 'success', {'raw_result_json': raw_json, 'result': result},
                source='llm.complete_json', source_event_id=ticket.operation_id, classified_at=now, scope_token=token)
            if receipt.status == 'accepted':
                await owner._write_classifier_projection(ticket.operation_id)
            return receipt
        except asyncio.CancelledError:
            if not terminal_started: owner._defer_cancel(ticket.operation_id, ticket.kind, token)
            raise


def reduce_source(owner, state, ticket, envelope, version):
    root = state['regime_policy']; seal = state['risk_input_seals']['records'][ticket.operation_id]['original']
    if ticket.kind == 'noon_index':
        root['noon_caps'][ticket.operation_id] = {'version': version, 'candidate': envelope['payload']['candidate'],
            'request_digest': ticket.request_digest, 'outcome_digest': digest(envelope), 'seal_digest': seal['seal_digest']}
        root['horizon'] = fold_horizon(state)
    elif ticket.kind == 'llm_regime':
        append_application(state, operation='classifier:' + ticket.operation_id, kind='classifier',
            ref=classifier_ref(state, ticket.operation_id, envelope=envelope, version=version),
            request={'kind': 'classifier', 'classifier_operation_id': ticket.operation_id},
            read_bundle={'source_seal_digest': seal['seal_digest'], 'reads_json': canonical(seal['reads'])},
            context=deepcopy(seal['request']['inputs']['context']), payload=envelope['payload'],
            version=version, now=owner.runtime._now())
    return state


async def sync_protection(owner, operation, supplied_context):
    from .regime_owner import _BaselineAdmissionRejected
    from .risk_sources import _SourceAuthority
    runtime = owner.runtime
    require_c3(owner)
    if type(operation) is not str or not operation.strip() or type(supplied_context) is not RegimeSyncContext:
        raise ValueError('regime_application_request_conflict')
    supplied = RegimeSyncContext.from_dict(supplied_context.to_dict()).to_dict()
    request = {'kind': 'sync', 'supplied_context': supplied}
    def prior(state):
        row = state['regime_policy']['applications'].get(operation)
        if row is not None:
            if canonical(row['request']) != canonical(request): raise ValueError('regime_application_request_conflict')
            return RegimeApplicationReceipt(**row['receipt'])
    found = prior(runtime.owner.state)
    if found is not None: return found
    context = context_for(supplied_context, runtime._now())
    read = owner.sources.read_source('llm_regime')
    if read.authority_status != 'current' or read.terminal_status != 'accepted':
        raise ApplicationBlocked('regime_classifier_not_current')
    state = runtime.owner.state; ref = classifier_ref(state, read.operation_id)
    names = CLASSIFIER_READS if context['regime_conflict_guard_enabled'] else PROTECTION_READS
    bundle = {'captured_version': runtime.owner.version, 'captured_at': context['captured_at'],
        'classifier_ref': ref, 'versioned_policies': {name: versioned_fact(state, name) for name in names}}
    generation, fence = runtime._day_generation, runtime._day_fence_id
    payload = deepcopy(state['risk_sources']['records'][read.operation_id]['terminal']['envelope']['payload'])
    with runtime.command_scope() as token:
        rejection = None
        async def execute():
            nonlocal rejection
            def reduce(candidate):
                try:
                    if prior(candidate) is not None: return candidate
                except ValueError as exc:
                    raise _BaselineAdmissionRejected(str(exc)) from None
                authority = _SourceAuthority(candidate, runtime._now().date().isoformat(), runtime._risk_source_pending.values())
                current = authority.dependency('llm_regime', ref['committed_version'])[0] == 'current'
                stale = (not current or runtime.day_admission_closed or generation != runtime._day_generation
                    or fence != runtime._day_fence_id or any(canonical(fact) != canonical(versioned_fact(candidate, name))
                        for name, fact in bundle['versioned_policies'].items()))
                append_application(candidate, operation=operation, kind='sync', ref=ref, request=request,
                    read_bundle=bundle, context=context, payload=payload, version=runtime.owner.version + 1,
                    now=runtime._now(), stale_reason='regime_application_reads_changed' if stale else '')
                from .regime_horizon import validate_c3_history
                validate_c3_history(candidate, runtime.owner.version + 1)
                return candidate
            try: await runtime.owner.mutate('regime-application:' + operation, reduce)
            except _BaselineAdmissionRejected as exc: rejection = str(exc)
            return True
        await asyncio.shield(runtime.start_command_result(token, execute))
        if rejection: raise ValueError(rejection)
        return prior(runtime.owner.state)


async def write_projection(owner, operation):
    runtime = owner.runtime
    async with owner._classifier_projection_lock:
        state = runtime.owner.state
        row = state.get('risk_sources', {}).get('records', {}).get(operation)
        if (row is None or state['risk_sources']['latest'].get('llm_regime') != operation
                or row['ticket']['business_day'] != runtime._now().date().isoformat()
                or row['terminal'] is None or row['terminal']['receipt']['status'] != 'accepted'):
            return False
        # 이 검사와 최종 synchronous replace 사이에는 await가 없다.
        try: owner._replace_classifier_projection(deepcopy(row['terminal']['envelope']['payload']['result']))
        except OSError: return False
        return True


def replace_projection(payload):
    import os
    import tempfile
    path = Path.home() / '.cache' / 'ai_trader' / 'llm_regime_today.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as output:
            temporary = output.name
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.flush()
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary): os.unlink(temporary)
