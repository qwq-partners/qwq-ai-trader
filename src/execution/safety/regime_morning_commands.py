"""Morning lifetime: source admission precedes optional I/O and model work."""
import asyncio
from copy import deepcopy
import json
from uuid import uuid4

from .application import ApplicationBlocked
from .day_recovery import scope_reason
from .policy_generations import canonical, versioned_fact
from .protection_recovery import digest
from .regime_morning import (RegimeMorningBaseline, RegimeMorningStageInput,
    RegimeMorningResult, READS, KIND, RULE, prompt_for, calculation)


async def register_baseline(runtime, baseline, expected_version):
    from .regime_owner import _BaselineAdmissionRejected, _time, validate_regime_policy
    if type(baseline) is not RegimeMorningBaseline or type(expected_version) is not int or expected_version < 0:
        raise ValueError('invalid_morning_registration')
    supplied = RegimeMorningBaseline.from_dict(baseline.to_dict()).to_dict()
    def check(state, *, admitted=False):
        def reject(reason, *, blocked=False):
            if admitted: raise _BaselineAdmissionRejected(reason, blocked=blocked)
            raise (ApplicationBlocked if blocked else ValueError)(reason)
        if admitted:
            if runtime.day_admission_closed: reject('day_transition_admission_closed', blocked=True)
        else: runtime._require_day_admission()
        root = state.get('regime_policy')
        if root is None or root.get('schema') not in (2, 3):
            reject('regime_horizon_baseline_required', blocked=True)
        now = runtime._now()
        if (supplied['account_scope'] != runtime.account_scope or scope_reason(state, runtime.account_scope)
                or supplied['business_day'] != now.date().isoformat()
                or supplied['generation'] != runtime._day_generation or supplied['fence_id'] != runtime._day_fence_id):
            reject('morning_baseline_context_conflict')
        times = (supplied['evidence']['observed_at'], supplied['morning']['open_expectation_as_of'])
        if any(_time(t) > now for t in times if t is not None): reject('future_morning_baseline')
        if supplied['morning']['assessment_day'] and supplied['morning']['assessment_day'] > now.date().isoformat():
            reject('future_morning_baseline')
        if root.get('morning_baseline') is not None:
            if canonical(root['morning_baseline']['supplied']) != canonical(supplied):
                reject('morning_baseline_conflict')
            return root['morning_baseline']['baseline_version']
        if runtime.owner.version != expected_version: reject('morning_baseline_version_conflict')
        if (supplied['regime_baseline_version'] != root['baseline']['baseline_version']
                or supplied['horizon_baseline_version'] != root['horizon_baseline']['baseline_version']):
            reject('morning_baseline_prerequisite_conflict')
    runtime.owner._require_ready()
    prior = check(runtime.owner.state)
    if prior is not None: return prior
    with runtime.command_scope() as token:
        rejection = None
        async def execute():
            nonlocal rejection
            def reduce(state):
                if check(state, admitted=True) is not None: return state
                version = runtime.owner.version + 1
                state['regime_policy'].update(schema=3,
                    morning_baseline={'supplied': supplied, 'digest': digest(supplied), 'baseline_version': version},
                    morning={**supplied['morning'], 'source_ref': None})
                validate_regime_policy(state, version)
                return state
            try: await runtime.owner.mutate('regime-morning-baseline:' + digest(supplied), reduce)
            except _BaselineAdmissionRejected as exc: rejection = exc
            return True
        await asyncio.shield(runtime.start_command_result(token, execute))
        try:
            if rejection is not None: raise rejection
            result = check(runtime.owner.state, admitted=True)
            if result is None: raise ValueError('morning_baseline_result_missing')
            return result
        except _BaselineAdmissionRejected as exc:
            raise (ApplicationBlocked if exc.blocked else ValueError)(str(exc)) from None


async def read_stage(owner, provider, method, stage, *, symbol=None, asynchronous=False):
    from .regime_owner import _time
    try:
        fn = getattr(provider, method)
        value = fn(symbol) if symbol is not None else fn()
        if asynchronous: value = await value
        if type(value) is not RegimeMorningStageInput: raise ValueError('morning_stage_required')
        result = RegimeMorningStageInput.from_dict(value.to_dict()).to_dict()
        if result['stage'] != stage: raise ValueError('morning_stage_mismatch')
        if any(_time(result[k]) > owner.runtime._now() for k in ('received_at', 'market_as_of') if result[k]):
            raise ValueError('future_morning_stage')
        if stage == 'overtime' and result['outcome'] == 'success' and result['payload']['symbol'] != symbol:
            raise ValueError('morning_symbol_mismatch')
        return result
    except Exception:
        return RegimeMorningStageInput.from_dict({'schema': 1, 'stage': stage, 'outcome': 'failed',
            'source': method, 'event_id': None, 'received_at': None, 'market_as_of': None, 'payload': None}).to_dict()


async def diagnose(owner, provider, llm):
    from .regime_owner import _OwnerBeginCancelled
    from .risk_input_seal import _context_reason
    from ...utils.llm import LLMTask
    runtime = owner.runtime
    runtime._require_day_admission(); runtime.owner._require_ready()
    state = runtime.owner.state; root = state['regime_policy']
    if root.get('schema') != 3: raise ApplicationBlocked('morning_baseline_required')
    if root['morning']['knowledge'] != 'known': raise ApplicationBlocked('morning_baseline_unknown')
    now = runtime._now(); day = now.date().isoformat()
    if root['morning']['assessment_day'] == day: return RegimeMorningResult('already_done', 'accepted_today')
    if not (8, 48) <= (now.hour, now.minute) <= (8, 55): return RegimeMorningResult('outside_window', 'morning_window')
    if owner._morning_running or KIND in runtime._risk_source_pending.values():
        return RegimeMorningResult('pending', 'morning_inflight')
    for row in state.get('risk_sources', {}).get('records', {}).values():
        if row['ticket']['kind'] == KIND and row['ticket']['business_day'] == day and row['terminal'] is None:
            return RegimeMorningResult('pending', 'morning_unresolved')
    for name in READS: versioned_fact(state, name)
    ref = root['trend_state']['source_refs']['trend']
    read = owner.sources.read_source('index_trend', expected_version=None if ref is None else ref['committed_version'])
    if ref is None or read.authority_status != 'current' or read.terminal_status != 'accepted':
        raise ApplicationBlocked('regime_source_not_current')
    owner._morning_running = True
    try:
        with runtime.command_scope() as token:
            ticket = await owner._begin('morning:' + uuid4().hex, KIND, token, require_seal=True,
                dependencies={'index_trend': ref['committed_version']})
            terminal_started = False
            async def finish(outcome, payload=None, **metadata):
                nonlocal terminal_started
                terminal_started = True
                receipt = await owner.sources.complete(ticket, outcome, payload, scope_token=token, **metadata)
                return RegimeMorningResult('completed', receipt.reason, receipt)
            try:
                if _context_reason(runtime, runtime.owner.state, ticket): return await finish('missing')
                read = owner.sources.read_source('index_trend', expected_version=ref['committed_version'])
                if read.authority_status != 'current' or read.terminal_status != 'accepted':
                    return await finish('missing')
                # Capture the actual accepted index source before optional awaits. The
                # same captured policies build the prompt and completion adjustment.
                reads_json = owner.sources.capture_reads(ticket, source_lanes=('index_trend',), versioned_policy_reads=READS)
                stages = {'theme': await read_stage(owner, provider, 'snapshot_themes', 'theme')}
                if stages['theme']['outcome'] == 'failed': return await finish('failed')
                try:
                    symbols = provider.snapshot_symbols()
                    if (type(symbols) is not tuple or len(symbols) > 5
                            or any(type(s) is not str or not s for s in symbols)):
                        raise ValueError('invalid_morning_symbols')
                except Exception: return await finish('failed')
                stages['overtime'] = []
                for symbol in symbols:
                    stages['overtime'].append(await read_stage(owner, provider, 'fetch_overtime_price',
                        'overtime', symbol=symbol, asynchronous=True))
                stages['news'] = await read_stage(owner, provider, 'snapshot_news', 'news')
                if stages['news']['outcome'] == 'failed': return await finish('failed')
                reads = json.loads(reads_json); policies = reads['versioned_policies']
                before = policies['regime_policy.trend_state']['value']
                # Legacy formats regime and overtime fields before macro I/O.
                # Validate with an absent macro locally; only the real stage below
                # is sealed, and no optional request is added after a format error.
                absent_macro = {'schema': 1, 'stage': 'macro', 'outcome': 'missing',
                    'source': 'fetch_macro_context', 'event_id': None, 'received_at': None,
                    'market_as_of': None, 'payload': None}
                try: prompt_for(before, {**stages, 'macro': absent_macro}, list(symbols))
                except (TypeError, ValueError, OverflowError): return await finish('failed')
                stages['macro'] = await read_stage(owner, provider, 'fetch_macro_context', 'macro', asynchronous=True)
                if _context_reason(runtime, runtime.owner.state, ticket): return await finish('missing')
                try: prompt = prompt_for(before, stages, list(symbols))
                except (TypeError, ValueError, OverflowError): return await finish('failed')
                inputs = {'stages': stages, 'symbols': list(symbols), 'prompt': prompt,
                    'task': 'MARKET_ANALYSIS', 'max_tokens': 150, 'rule_digest': RULE}
                seal = await owner.sources.seal(ticket, source_lanes=('index_trend',), versioned_policy_reads=READS,
                    inputs=inputs, expected_reads_json=reads_json, scope_token=token)
                if seal.status != 'sealed': return await finish('missing')
                try:
                    response = await llm.complete(prompt, task=LLMTask.MARKET_ANALYSIS, max_tokens=150)
                except Exception: return await finish('failed')
                if (getattr(response, 'success', None) is not True or type(getattr(response, 'content', None)) is not str
                        or not response.content.strip()):
                    return await finish('failed')
                raw = response.content
                when = runtime._now()
                after, engine = calculation(before, policies['regime_policy.horizon']['value'], raw.strip(), when)
                morning_before = deepcopy(runtime.owner.state['regime_policy']['morning'])
                payload = {'raw_text': raw, 'assessment': raw.strip(), 'before': before, 'after': after,
                    'engine_regime': engine, 'morning_before': morning_before,
                    'morning_after': {'knowledge': 'known', 'assessment': raw.strip(), 'assessment_day': ticket.business_day,
                        'open_expectation': raw.strip(), 'open_expectation_as_of': when.isoformat(),
                        'source_ref': {'operation_id': ticket.operation_id, 'committed_version': None}}}
                return await finish('success', payload, source='llm.complete', source_event_id=ticket.operation_id,
                    received_at=when, classified_at=when)
            except asyncio.CancelledError:
                if not terminal_started: owner._defer_cancel(ticket.operation_id, ticket.kind, token)
                raise
    finally: owner._morning_running = False
