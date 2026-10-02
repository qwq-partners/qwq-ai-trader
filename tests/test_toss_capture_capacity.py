"""실제 유한 receiver/collector/artifact의 오프라인 용량 계약."""
from copy import deepcopy
import importlib
import json

import pytest


def api():
    try:return importlib.import_module('src.analytics.toss_capture_capacity')
    except ModuleNotFoundError:pytest.fail('capacity qualifier missing')


def case(**changes):
    return dict(name='synthetic-small',duration_seconds=12.0,frame_count=40,
        max_frames=100,distribution='pilot',timing='uniform',levels=10,
        horizon_seconds=10.0,expected_stop='window_ended',artifact_max_bytes=1048576,
        **changes)


@pytest.mark.asyncio
async def test_real_receiver_storage_and_all_candidates_are_measured(tmp_path):
    c=case();original=deepcopy(c)
    r=await api().run_case(c,directory=tmp_path)
    assert c==original
    assert r['test_expectation_met'] and r['resource_budget_met']
    assert r['window_coverage_met'] and r['all_symbols_horizon_quote']
    assert r['received_frames']==40 and r['quote_count']==39
    assert sum(r['quote_counts_by_symbol'].values())==39
    assert len(r['quote_counts_by_symbol'])==3 and min(r['quote_counts_by_symbol'].values())>0
    assert r['stop_reason']=='window_ended' and r['cleanup_failed'] is False
    assert r['artifact_roundtrip_verified'] and r['artifact_bytes']>r['serialized_result_bytes']
    assert r['metrics']['python_peak_bytes']>0 and r['metrics']['wall_seconds']>0
    assert r['synthetic'] and not r['production_eligible'] and not r['profit_comparison_available']
    assert r['stream_complete'] is None and len(r['artifact_sha256'])==64
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize('frames',[15,20,50])
async def test_exact_or_excess_cap_remains_incomplete_even_if_expected(tmp_path,frames):
    c=case();c.update(frame_count=frames,max_frames=15,expected_stop='frame_limit')
    r=await api().run_case(c,directory=tmp_path)
    assert r['received_frames']==15 and r['quote_count']==14
    assert r['test_expectation_met'] and not r['window_coverage_met']
    assert r['stop_reason']=='frame_limit'


@pytest.mark.asyncio
async def test_single_symbol_pressure_preserves_missing_other_symbols(tmp_path):
    c=case();c['distribution']='one_symbol'
    r=await api().run_case(c,directory=tmp_path)
    assert r['window_coverage_met'] and not r['all_symbols_horizon_quote']
    assert sorted(r['quote_counts_by_symbol'].values())==[0,0,39]
    assert r['resource_budget_met']


@pytest.mark.asyncio
async def test_burst_has_same_denominator_but_no_invented_horizon_quote(tmp_path):
    c=case();c['timing']='burst'
    r=await api().run_case(c,directory=tmp_path)
    assert r['received_frames']==40 and r['window_coverage_met']
    assert not r['all_symbols_horizon_quote']
    assert r['last_quote_offset_seconds']<c['horizon_seconds']


@pytest.mark.asyncio
async def test_file_limit_is_visible_not_sealed_success(tmp_path):
    c=case();c['artifact_max_bytes']=1024
    r=await api().run_case(c,directory=tmp_path)
    assert not r['artifact_roundtrip_verified'] and not r['resource_budget_met']
    assert r['artifact_error']=='artifact_write_or_read_failed'
    assert r['artifact_bytes']<=1024
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize('key,value',[
    ('frame_count',True),('frame_count',100001),('max_frames',50001),
    ('duration_seconds',float('nan')),('duration_seconds',float('inf')),
    ('duration_seconds',0),('duration_seconds',3601),('horizon_seconds',12),
    ('distribution','unbounded'),('timing','other'),('levels',65),('levels',True),
    ('artifact_max_bytes',67108865),('name','../../outside'),('extra','raw'),
])
async def test_invalid_case_rejected_before_files(tmp_path,key,value):
    c=case();c[key]=value
    with pytest.raises(ValueError):await api().run_case(c,directory=tmp_path)
    assert not list(tmp_path.iterdir())


def test_fixed_full_suite_separates_markout_and_whole_window():
    cases=api().qualification_cases('full')
    assert any(c['duration_seconds']==901 for c in cases)
    assert any(c['duration_seconds']==1659.665122 for c in cases)
    assert any(c['duration_seconds']==1800 and c['frame_count']==82694 for c in cases)
    assert all(c['max_frames']<=50000 for c in cases)
    assert any(c['frame_count']==c['max_frames'] for c in cases)
    with pytest.raises(ValueError):api().qualification_cases('unbounded')


def test_smoke_cli_outputs_synthetic_report(capsys):
    try:m=importlib.import_module('scripts.qualify_toss_capture_capacity')
    except ModuleNotFoundError:pytest.fail('capacity CLI missing')
    assert m.main(['--suite','smoke'])==0
    r=json.loads(capsys.readouterr().out)
    assert r['synthetic'] and not r['production_eligible']
    assert r['test_expectations_met'] and r['suite']=='smoke'
    assert r['cases']
