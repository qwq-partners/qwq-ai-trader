"""Strict offline explanation of the first returned cohort's actual pre-signal gates."""
from collections import Counter
from copy import deepcopy
import math
import re

from .entry_gate_trace import VERSION, POLICY_REF, STAGES, STEP_FIELDS, TRACE_FIELDS, SCAN_FIELDS
from .entry_observation import observation_population
from .entry_price_shadow import _timestamp


def _text(value):
    if type(value) is not str or not value.strip() or len(value)>200:
        raise ValueError('bounded text required')
    return value


def _reason(value, *, nullable=False):
    if nullable and value is None: return None
    if type(value) is not str or re.fullmatch(r'[a-z][a-z0-9_]{0,79}',value) is None:
        raise ValueError('fixed reason code required')
    return value


def _value(value):
    if value is None or type(value) is bool: return
    if type(value) is int and value.bit_length()<=256: return
    if type(value) is float and math.isfinite(value): return
    if type(value) is str and len(value)<=200: return
    raise ValueError('bounded finite scalar required')


def _trace(record, candidate, scan_at, report_at):
    if (set(record)!=TRACE_FIELDS|{'kind','sequence'} or record['trace_version']!=VERSION
            or record['policy_ref']!=POLICY_REF or record['symbol']!=candidate['symbol']):
        raise ValueError('gate trace schema/identity mismatch')
    at=_timestamp(record['observed_at'],'trace')
    if not scan_at<=at<=report_at: raise ValueError('gate trace time outside report')
    outcome=record['outcome'];reason=_reason(record['terminal_reason'])
    if outcome not in ('blocked','signal_created','not_reached','unknown'):
        raise ValueError('gate trace outcome invalid')
    steps=record['steps']
    if type(steps) is not list or len(steps)>len(STAGES): raise ValueError('bounded gate steps required')
    prior=-1;prior_time=scan_at;failed=[]
    for step in steps:
        if (type(step) is not dict or set(step)!=STEP_FIELDS or step['stage'] not in STAGES
                or step['status'] not in ('pass','fail','unknown','not_applicable')):
            raise ValueError('gate step schema invalid')
        index=STAGES.index(step['stage']);stamp=_timestamp(step['observed_at'],'gate step')
        if index<=prior or not prior_time<=stamp<=at or failed:
            raise ValueError('gate step order/time or post-failure record invalid')
        _reason(step['reason'],nullable=True);_value(step['value']);_value(step['threshold'])
        prior=index;prior_time=stamp
        if step['status']=='fail':failed.append(step)
    if outcome=='blocked':
        if len(failed)!=1 or reason!=(failed[0]['reason'] or failed[0]['stage']):
            raise ValueError('blocked trace requires matching actual failed predicate')
    elif failed: raise ValueError('failure conflicts with terminal outcome')
    if outcome=='signal_created':
        _text(record['signal_id'])
        if (not steps or steps[-1]['stage']!='signal_created' or steps[-1]['status']!='pass'
                or reason!='signal_created'):
            raise ValueError('actual signal creation evidence required')
    elif record['signal_id'] is not None or any(s['stage']=='signal_created' for s in steps):
        raise ValueError('signal identity conflicts with terminal outcome')
    return deepcopy(record)


def build_gate_report(observations, *, as_of):
    report_at=_timestamp(as_of,'report as_of')
    if type(observations) is not dict or type(observations.get('schema_version')) is not int or observations['schema_version']!=1:
        raise ValueError('observation schema invalid')
    epoch=_text(observations.get('evaluation_epoch'))
    records=observations.get('records')
    if type(records) is not list or len(records)>1000000: raise ValueError('bounded records required')
    journal=observations.get('journal',{})
    if type(journal) is not dict: raise ValueError('journal metadata invalid')
    evidence_end=report_at
    if journal.get('capture_closed_at') is not None:
        evidence_end=_timestamp(journal['capture_closed_at'],'journal close')
        if evidence_end>report_at: raise ValueError('future journal seal')
    for seq,row in enumerate(records,1):
        if type(row) is not dict or type(row.get('sequence')) is not int or row['sequence']!=seq:
            raise ValueError('observation sequence invalid')
        if 'observed_at' in row and _timestamp(row['observed_at'],'record')>evidence_end:
            raise ValueError('observation after report or journal seal')
    population=observation_population(records)
    scans=[r for r in records if r.get('kind')=='scan']
    if len(scans)!=1: raise ValueError('exactly one original scan required')
    scan=scans[0];scan_at=_timestamp(scan.get('observed_at'),'scan')
    if scan.get('route_origin')!='live_screening': raise ValueError('live screening scan required')
    scan_id=_text(scan.get('scan_id'))
    if type(scan.get('candidates')) is not list or len(scan['candidates'])>10000:
        raise ValueError('bounded original cohort required')
    candidates={}
    for c in scan['candidates']:
        if type(c) is not dict: raise ValueError('candidate object required')
        symbol=_text(c.get('symbol'));cid=_text(c.get('candidate_id'))
        if cid!=f'{scan_id}:{symbol}' or cid in candidates: raise ValueError('candidate identity invalid')
        candidates[cid]=c
    expected=bool(SCAN_FIELDS & set(scan))
    if expected:
        if (not SCAN_FIELDS<=set(scan) or scan['entry_gate_trace_expected'] is not True
                or scan['entry_gate_trace_version']!=VERSION or scan['entry_gate_policy_ref']!=POLICY_REF):
            raise ValueError('explicit gate trace declaration required')
        for key in ('entry_gate_source_version_ref','entry_gate_configuration_ref'):_text(scan[key])
    traces={}
    for row in records:
        if row.get('kind')!='entry_gate_trace':continue
        cid=row.get('candidate_id')
        if not expected or cid not in candidates or cid in traces or row['sequence']<=scan['sequence']:
            raise ValueError('undeclared, orphan or duplicate gate trace')
        traces[cid]=_trace(row,candidates[cid],scan_at,evidence_end)
    signal_owners={}
    for cid,trace in traces.items():
        sid=trace['signal_id']
        if sid is not None:
            if sid in signal_owners: raise ValueError('duplicate traced signal identity')
            signal_owners[sid]=cid
    for row in records:
        if row.get('kind')!='signal' or row.get('candidate_id') not in traces:continue
        trace=traces[row['candidate_id']]
        if trace['outcome']!='signal_created' or row.get('signal_id')!=trace['signal_id']:
            raise ValueError('signal record contradicts gate trace')
        created=_timestamp(trace['steps'][-1]['observed_at'],'signal creation')
        if not created<=_timestamp(row['observed_at'],'signal record')<=_timestamp(trace['observed_at'],'trace end'):
            raise ValueError('signal record contradicts gate trace time')
    counts=dict(total=len(candidates),recorded=len(traces),blocked=0,signal_created=0,not_reached=0,unknown=0,unavailable=0)
    stage_counts={s:Counter() for s in STAGES};out=[];all_accounted=True
    for cid,c in candidates.items():
        trace=traces.get(cid);outcome=trace['outcome'] if trace else ('unknown' if expected else 'unavailable')
        counts[outcome]+=1;actual={s['stage']:s for s in trace['steps']} if trace else {}
        last=max((STAGES.index(s) for s in actual),default=-1)
        stop_stage=None
        if outcome=='not_reached' and last>=0:
            terminal=actual[STAGES[last]]
            if (terminal['stage'] in ('batch_limit','signal_limit')
                    and terminal['status']=='unknown'
                    and terminal['reason']==trace['terminal_reason']):
                stop_stage=terminal['stage']
        stages={}
        for index,stage in enumerate(STAGES):
            if stage in actual:
                stages[stage]=deepcopy(actual[stage])
                # An explicit loop boundary does not evaluate a policy predicate.
                if stage==stop_stage:stages[stage]['status']='not_reached'
            else:
                status=('not_reached' if trace and outcome!='unknown' and index>last else
                        'unavailable' if not expected else 'unknown')
                stages[stage]=dict(stage=stage,status=status,observed_at=None,reason=None,value=None,threshold=None)
            stage_counts[stage][stages[stage]['status']]+=1
        failed=next((s for s in STAGES if stages[s]['status']=='fail'),None)
        before=STAGES[:STAGES.index(failed)] if failed else STAGES[:last+1]
        terminal_known=bool(trace and trace['steps'])
        if outcome=='not_reached':
            if stop_stage:
                before=STAGES[:STAGES.index(stop_stage)]
            else:
                terminal_known=bool(trace and trace['terminal_reason']=='emit_break'
                                    and last==STAGES.index('batch_limit'))
        prior_complete=terminal_known and all(stages[s]['status'] in ('pass','not_applicable') for s in before)
        coverage=bool(trace) and outcome!='unknown' and prior_complete
        all_accounted &= coverage
        out.append(dict(candidate_id=cid,symbol=c['symbol'],outcome=outcome,
            trace_status='recorded' if trace else 'missing' if expected else 'legacy_unavailable',
            terminal_reason=trace['terminal_reason'] if trace else None,
            signal_id=trace['signal_id'] if trace else None,
            first_observed_blocking_stage=failed,first_blocking_stage=failed if prior_complete else None,
            prior_evidence_complete=prior_complete,stages=stages))
    reasons=observations.get('incomplete_reasons')
    if type(reasons) is not list or any(type(r) is not str for r in reasons):raise ValueError('capture reasons required')
    source_complete=(observations.get('complete') is True and type(observations.get('dropped_records')) is int
                     and observations['dropped_records']==0 and not reasons)
    return dict(schema_version='entry-gate-report-v1',evaluation_epoch=epoch,as_of=report_at.isoformat(),
        route_origin='live_screening',population=population,counts=counts,candidates=out,
        stage_counts={k:dict(v) for k,v in stage_counts.items()},source_complete=source_complete,
        trace_coverage_complete=bool(expected and candidates and all_accounted and source_complete),
        incomplete_reasons=list(reasons),profit_comparison_available=False,production_eligible=False,
        source_authenticity_verified=False,
        references={k:scan[k] for k in SCAN_FIELDS if k in scan},
        limitations=['Signal creation is not emission, order acceptance or execution.',
            'Missing or unreached conditions are not reevaluated or treated as cash/zero profit.',
            'Only live_screening pre-signal gates are covered; configuration references are declarations.'])
