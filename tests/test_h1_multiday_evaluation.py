"""H1 original-input integration: all fixtures are synthetic and local."""
from copy import deepcopy
from decimal import Decimal
import importlib
import json

import pytest

from h1_multiday_fixtures import payload, rebind, dates, source_complete


def evaluate(p):
    try:
        api = importlib.import_module('src.analytics.h1_multiday_evaluation')
    except ModuleNotFoundError:
        pytest.fail('H1 multi-day capital evaluation is not implemented')
    return api.build_h1_multiday_report(p['protocol'], p['days'], analysis_as_of=p['as_of'])


def test_original_twenty_day_bundles_preserve_cohort_and_sensitivity(tmp_path):
    p=payload(tmp_path); before=deepcopy(p)
    out=evaluate(p)
    assert len(out['days']) == 20
    assert out['counts']['selected_admissions'] == 60
    assert out['counts']['all_candidates'] == 80
    assert out['quality_failures'] == []
    assert set(out['scenarios']) == {'0','10','30'}
    # Three pending orders each reserve253750; the fixed500000 strategy sleeve fits only one.
    assert all(s['paired_decisions'] == 20 for s in out['scenarios'].values())
    assert out['scenarios']['0']['arms']['A']['capital_blocked_count']==40
    assert out['production_eligible'] is False and out['profitability_pass'] is False
    assert out['account_return'] is None and out['benchmarks']['kodex200'] is None
    assert out['source_authenticity_verified'] is False
    assert p==before


def test_missing_planned_day_never_becomes_zero_return_or_shorter_window(tmp_path):
    p=payload(tmp_path,all_days=False)
    out=evaluate(p)
    assert len(out['days']) == 20
    assert len(out['quality_failures']) == 19
    assert out['economic_verdict'] == 'suppressed'
    assert out['scenarios']['0']['delta_net_pnl'] is None
    assert out['days'][1]['status'] == 'planned_missing'


@pytest.mark.parametrize('defect',['count','duplicate','weekend','epoch','policy','fixed_after_start','multiplier','rr','scenarios','extra'])
def test_preregistration_and_policy_mismatch_rejects(tmp_path,defect):
    p=payload(tmp_path,all_days=False); proto=p['protocol']
    if defect=='count': proto['calendar']['scheduled_dates'].pop()
    elif defect=='duplicate': proto['calendar']['scheduled_dates'][1]=dates()[0]
    elif defect=='weekend': proto['calendar']['scheduled_dates'][4]='2026-10-17'
    elif defect=='epoch': proto['evaluation_epoch']='other'
    elif defect=='policy': proto['study_policy_sha256']='d'*64
    elif defect=='fixed_after_start': proto['fixed_at']=dates()[0]+'T09:00:00+09:00'
    elif defect=='multiplier': proto['pending_reservation_multiplier']='1'
    elif defect=='rr': proto['min_net_rr']='1.0'
    elif defect=='scenarios': proto['slippage_bps_each']=[0]
    else: proto['promote']=True
    with pytest.raises(ValueError):evaluate(p)


def test_missing_first_selected_seed_is_not_replaced_by_later_snapshot(tmp_path):
    p=payload(tmp_path)
    first=next(r for r in p['days'][0]['observations']['records'] if r['kind']=='order_ready')
    first['capital_snapshot_status']='unavailable'
    first.pop('capital_snapshot',None); first.pop('capital_snapshot_ref',None)
    rebind(p['days'][0])
    out=evaluate(p)
    assert out['seed']['status']=='unavailable'
    assert out['scenarios']['0']['arms']['A']['net_pnl'] is None
    assert out['economic_verdict']=='suppressed'


def test_no_order_day_requires_every_scan_source_complete(tmp_path):
    p=payload(tmp_path)
    day=p['days'][0]; records=day['observations']['records']
    # Preserve original candidate denominator but remove all signal/quote activity.
    day['observations']['records']=[r for r in records if r['kind'] in ('scan','selection_basis','entry_gate_trace')]
    rebind(day)
    out=evaluate(p)
    assert out['days'][0]['status']=='verified_no_order_ready'
    assert out['scenarios']['0']['daily'][0]['a_net_pnl']=='0'
    scan=next(r for r in day['observations']['records'] if r['kind']=='scan')
    from src.analytics.selection_source_status import SCAN_FIELDS
    for k in SCAN_FIELDS:scan.pop(k,None)
    scan.update(selection_sources_expected=True,selection_sources_status='unavailable')
    rebind(day)
    out=evaluate(p)
    assert out['days'][0]['status']=='no_order_ready_unverified'
    assert out['economic_verdict']=='suppressed'


def test_late_audit_is_distinct_from_economic_cutoff_and_no_early_verdict(tmp_path):
    p=payload(tmp_path,all_days=False);p['as_of']=dates()[0]+'T16:00:00+09:00'
    out=evaluate(p)
    assert out['evaluation_status']=='quality_only'
    assert all(s['minimum_proxy_conditions_met'] is False for s in out['scenarios'].values())


def test_streaming_days_does_not_require_twenty_journals_resident(tmp_path):
    p=payload(tmp_path)
    p['days']=iter(p['days'])
    out=evaluate(p)
    assert out['counts']['selected_admissions']==60


def test_out_of_order_day_iterator_is_rejected(tmp_path):
    p=payload(tmp_path)
    p['days'][0],p['days'][1]=p['days'][1],p['days'][0]
    with pytest.raises(ValueError,match='order|duplicate'):evaluate(p)


def test_multiple_scans_select_admitted_identity_instead_of_per_scan_rank(tmp_path):
    p=payload(tmp_path);day=p['days'][0];records=day['observations']['records']
    scan=next(r for r in records if r['kind']=='scan')
    second=deepcopy(scan);second['scan_id']='synthetic-second-scan'
    second['observed_at']=day['date']+'T09:15:01+09:00'
    second['candidates']=scan['candidates'][2:];scan['candidates']=scan['candidates'][:2]
    moved={c['candidate_id']:second['scan_id']+':'+c['symbol'] for c in second['candidates']}
    for c in second['candidates']:c['candidate_id']=moved[c['candidate_id']]
    for r in records:
        if r.get('candidate_id') in moved:
            r['candidate_id']=moved[r['candidate_id']]
            if r['observed_at']<second['observed_at']:r['observed_at']=second['observed_at']
    source_complete(scan);source_complete(second)
    records.append(second)
    from datetime import datetime
    records.sort(key=lambda r:(datetime.fromisoformat(r['observed_at']),0 if r['kind']=='scan' else 1))
    rebind(day)
    out=evaluate(p)
    assert out['counts']['scans']==21
    assert out['counts']['selected_admissions']==60
    assert out['scenarios']['0']['paired_decisions']==20


def test_ambiguous_same_candidate_never_duplicates_price_result(tmp_path):
    p=payload(tmp_path);day=p['days'][0];records=day['observations']['records']
    order=deepcopy(next(r for r in records if r['kind']=='order_ready'))
    sig=deepcopy(next(r for r in records if r.get('signal_id')==order['signal_id'] and r['kind']=='signal'))
    admitted=deepcopy(next(r for r in records if r.get('reason')=='order_ready_admitted'))
    sig['signal_id']='ambiguous-signal';sig['observed_at']=day['date']+'T09:15:03.1+09:00'
    order.update(signal_id=sig['signal_id'],order_id='ambiguous-order',observed_at=day['date']+'T09:15:04.1+09:00')
    order['capital_snapshot'].update(signal_id=sig['signal_id'],order_id=order['order_id'],captured_at=order['observed_at'])
    admitted.update(signal_id=sig['signal_id'],order_id=order['order_id'],observed_at=order['observed_at'])
    records.extend([sig,order,admitted])
    from datetime import datetime
    records.sort(key=lambda r:datetime.fromisoformat(r['observed_at']))
    rebind(day)
    out=evaluate(p)
    assert out['counts']['selected_admissions']==61
    assert out['seed']['status']=='unavailable'
    assert out['economic_verdict']=='suppressed'


def test_missing_markout_does_not_turn_b_rr_skip_into_unknown(tmp_path):
    p=payload(tmp_path);day=p['days'][0]
    day['observations']['records']=[r for r in day['observations']['records']
        if not (r['kind']=='ws_quote' and r['quote_id'].startswith('synthetic-mark-SYNTH_LOSS'))]
    rebind(day)
    out=evaluate(p)
    scenario=out['scenarios']['0']
    assert scenario['arms']['A']['net_pnl'] is None
    assert scenario['arms']['B']['net_pnl'] is not None
    assert scenario['delta_net_pnl'] is None
    assert out['economic_verdict']=='suppressed'


def test_first_invalid_quote_is_not_replaced_by_later_valid_quote(tmp_path):
    p=payload(tmp_path);day=p['days'][0];records=day['observations']['records']
    first=next(r for r in records if r['kind']=='ws_quote' and 'entry-SYNTH_LOSS' in r['quote_id'])
    first['ask_size']=1
    later=deepcopy(first);later['quote_id']='synthetic-later-good';later['ask_size']=1000
    records.insert(records.index(first)+1,later);rebind(day)
    out=evaluate(p)
    assert out['scenarios']['0']['arms']['A']['net_pnl'] is None
    assert out['economic_verdict']=='suppressed'


def test_selected_day_with_failed_source_scan_suppresses_cohort_economics(tmp_path):
    p=payload(tmp_path);day=p['days'][0]
    scan=next(r for r in day['observations']['records'] if r['kind']=='scan')
    from src.analytics.selection_source_status import SCAN_FIELDS
    for k in SCAN_FIELDS:scan.pop(k,None)
    scan.update(selection_sources_expected=True,selection_sources_status='unavailable')
    rebind(day)
    out=evaluate(p)
    assert day['date'] in out['quality_failures']
    assert out['economic_verdict']=='suppressed'


def test_larger_evidenced_strategy_sleeve_preserves_losses_and_missed_gains(tmp_path):
    p=payload(tmp_path)
    for day in p['days']:
        for r in day['observations']['records']:
            if r['kind']=='order_ready':
                s=r['capital_snapshot']
                cap=str(Decimal(s['equity'])*Decimal('0.5'))
                s.update(strategy_allocation_pct='50',strategy_cap_notional=cap,
                         strategy_held_notional='0',strategy_pending_reserved='0',strategy_remaining_notional=cap)
        rebind(day)
    out=evaluate(p)
    assert out['scenarios']['0']['paired_decisions']==60
    assert Decimal(out['scenarios']['0']['delta_net_pnl'])==Decimal('-226660')
    assert out['scenarios']['0']['minimum_proxy_conditions_met'] is False
    assert out['economic_verdict']=='synthetic_diagnostic_only'


def test_early_seal_does_not_prove_the_declared_capture_window(tmp_path):
    p=payload(tmp_path);day=p['days'][0]
    day['observations']['journal']['capture_closed_at']=day['date']+'T09:44:00+09:00'
    rebind(day)
    out=evaluate(p)
    assert day['date'] in out['quality_failures']
    assert out['economic_verdict']=='suppressed'


def test_evidenced_zero_cash_is_known_blocked_not_missing_seed(tmp_path):
    p=payload(tmp_path);day=p['days'][0]
    r=next(r for r in day['observations']['records'] if r['kind']=='order_ready')
    r['capital_snapshot'].update(cash='0',cash_after_reserve='0',prior_pending_cash_reserved='0',
                                core_cash_reserved='0',cash_capacity_before='0')
    rebind(day)
    out=evaluate(p)
    assert out['seed']['status']=='validated_internal_snapshot'
    assert out['seed']['cash']=='0'
    assert Decimal(out['scenarios']['0']['arms']['A']['ending_cash'])==0
    assert out['scenarios']['0']['arms']['A']['net_pnl']=='0'
    assert out['scenarios']['0']['paired_decisions']==0


def test_late_open_does_not_prove_first_five_coverage(tmp_path):
    p=payload(tmp_path);day=p['days'][0]
    day['observations']['journal']['created_at']=day['date']+'T09:12:30+09:00'
    rebind(day)
    out=evaluate(p)
    assert day['date'] in out['quality_failures']


@pytest.mark.parametrize('bad',[None,'not-a-number','NaN'])
def test_malformed_numeric_policy_is_a_controlled_input_error(tmp_path,bad):
    from h1_multiday_fixtures import protocol
    p=payload(tmp_path,all_days=False);day=p['days'][0]
    c=json.loads(day['study_utf8']);c['policy']['max_quote_age_seconds']=bad
    rebind(day,c);p['protocol']=protocol(day)
    with pytest.raises(ValueError):evaluate(p)


@pytest.mark.parametrize('field',['cash','strategy'])
def test_valid_negative_residual_is_preserved_and_allocates_no_shadow_capital(tmp_path,field):
    p=payload(tmp_path);day=p['days'][0]
    r=next(r for r in day['observations']['records'] if r['kind']=='order_ready')
    s=r['capital_snapshot']
    if field=='cash':
        s.update(cash='0',cash_after_reserve='0',prior_pending_cash_reserved='100',
                 core_cash_reserved='0',cash_capacity_before='-100')
    else:
        s.update(strategy_held_notional=str(Decimal(s['strategy_cap_notional'])+100),
                 strategy_pending_reserved='0',strategy_remaining_notional='-100')
    rebind(day)
    out=evaluate(p)
    assert out['seed']['status']=='validated_internal_snapshot'
    projected='cash' if field=='cash' else 'strategy_capacity'
    original='cash_capacity_before' if field=='cash' else 'strategy_remaining_notional'
    assert out['seed'][projected]=='0'
    assert out['seed'][original]=='-100'
    for arm in ('A','B'):
        assert out['scenarios']['0']['arms'][arm]['net_pnl']=='0'
        assert out['scenarios']['0']['arms'][arm]['capital_blocked_count']==60
    assert out['scenarios']['0']['paired_decisions']==0


def test_original_first_quote_capital_failure_is_known_no_entry_not_missing(tmp_path):
    p=payload(tmp_path);day=p['days'][0]
    r=next(r for r in day['observations']['records'] if r['kind']=='order_ready')
    r['capital_snapshot'].update(cash='300000',cash_after_reserve='300000',
                                prior_pending_cash_reserved='0',core_cash_reserved='0',cash_capacity_before='300000')
    rebind(day)
    out=evaluate(p)
    first=out['scenarios']['0']['opportunities'][0]
    for arm in ('a','b'):
        assert first[arm+'_status']=='capital_blocked'
        assert first[arm+'_net_pnl']=='0'
        assert first[arm+'_reason']=='original_market'
    assert out['scenarios']['0']['arms']['A']['net_pnl'] is not None
    assert out['scenarios']['0']['arms']['B']['net_pnl'] is not None
