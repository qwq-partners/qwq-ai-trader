"""독립 산술 사례와 읽기 전용 계좌 입력 경계."""
from copy import deepcopy
from decimal import Decimal
import hashlib
import importlib
import json
import os

import pytest


def payload():
    return dict(schema_version='account-net-return-v1', dataset_kind='synthetic',
        as_of='2026-10-02T01:00:00Z', currency='KRW', tax_basis_ref='synthetic-embedded',
        account=dict(source_ref='synthetic-account', source_sha256='a'*64, declared_complete=True,
            start=dict(at='2026-10-01T00:00:00Z', equity='1000'),
            end=dict(at='2026-10-02T00:00:00Z', equity='1080'), income_in_equity=True,
            trading_costs_in_equity=True, cashflows=[], external_operating_costs=[]), benchmarks=[])


def flow(kind='deposit', amount='1000', pre='1100', post='2100'):
    return dict(id='flow-1', at='2026-10-01T12:00:00Z', kind=kind,
                amount=amount, pre_equity=pre, post_equity=post)


def report(value):
    return importlib.import_module('src.analytics.account_net_return').build_account_report(value)


def number(value):
    return None if value is None else Decimal(value)


def test_deposit_not_income_and_external_operating_costs_separate():
    p=payload(); a=p['account']; a['cashflows']=[flow()]; a['end']['equity']='2310'
    a['external_operating_costs']=[dict(id='cost-1',at='2026-10-01T20:00:00Z',amount='10',allocation_ref='synthetic-ai')]
    out=report(p); r=out['account']
    assert number(r['gain_before_external_operating_costs'])==310
    assert number(r['net_gain'])==300
    assert number(r['twr_embedded_costs'])==Decimal('.21')
    assert r['all_costs_twr'] is None
    assert out['dataset_kind']=='synthetic'
    assert not any(out[k] for k in ('source_authenticity_verified','engine_attribution_verified','production_eligible'))


def test_withdrawal_and_transfer_have_signed_external_flow():
    p=payload();p['account']['cashflows']=[flow('withdrawal','300','1100','800')];p['account']['end']['equity']='880'
    r=report(p)['account']
    assert number(r['net_gain'])==180 and number(r['twr_embedded_costs'])==Decimal('.21')
    p['account']['cashflows'][0]['kind']='transfer_out'
    assert report(p)['account']==r


def test_embedded_dividend_and_trading_fees_not_deducted_twice():
    # 1000 원금 + 배당100 - 비용20 = 1080, 입력은 이미 평가액에 반영됨.
    r=report(payload())['account']
    assert number(r['net_gain'])==80 and number(r['all_costs_twr'])==Decimal('.08')


def test_missing_boundary_keeps_krw_gain_and_unknown_twr():
    p=payload();p['account']['cashflows']=[flow(pre=None,post=None)];p['account']['end']['equity']='2310'
    r=report(p)['account']
    assert number(r['net_gain'])==310 and r['twr_embedded_costs'] is None
    assert 'missing_flow_valuation' in r['twr_unavailable_reasons']


@pytest.mark.parametrize('start,end',[('0','80'),('-100','80'),('100','-80')])
def test_nonpositive_bases_or_negative_nav_preserve_gain_only(start,end):
    p=payload();p['account']['start']['equity']=start;p['account']['end']['equity']=end
    r=report(p)['account']
    assert number(r['net_gain'])==Decimal(end)-Decimal(start)
    assert r['twr_embedded_costs'] is None


def test_zero_end_nav_is_total_loss_but_zero_post_flow_denominator_unknown():
    p=payload();p['account']['end']['equity']='0'
    assert number(report(p)['account']['twr_embedded_costs'])==-1
    p['account']['cashflows']=[flow('withdrawal','1000','1000','0')]
    r=report(p)['account'];assert number(r['net_gain'])==0 and r['twr_embedded_costs'] is None


@pytest.mark.parametrize('field',['declared_complete','income_in_equity','trading_costs_in_equity'])
def test_incomplete_declaration_cannot_produce_economic_values(field):
    p=payload();p['account'][field]=False;r=report(p)['account']
    assert all(r[k] is None for k in ('net_gain','gain_before_external_operating_costs','twr_embedded_costs','all_costs_twr'))
    assert field in r['unavailable_reasons']


def test_two_flows_geometric_link_and_input_immutable():
    p=payload();a=p['account'];a['cashflows']=[flow(),dict(flow('withdrawal','310','2310','2000'),id='flow-2',at='2026-10-01T18:00:00Z')]
    a['end']['equity']='2200'; original=deepcopy(p)
    r=report(p)['account'];assert number(r['net_gain'])==510
    assert number(r['twr_embedded_costs'])==Decimal('.331') and p==original


@pytest.mark.parametrize('bad',['NaN','Infinity','1e3','-1e999','1.123456789','1'*25,1,True,'',' 1000'])
def test_strict_bounded_decimal_input(bad):
    p=payload();p['account']['start']['equity']=bad
    with pytest.raises(ValueError):report(p)


@pytest.mark.parametrize('defect',['future','reverse','naive','outside','same_time','duplicate_id','contradiction','one_boundary','dividend','negative_amount','extra','bad_sha','bool_flag','too_many'])
def test_rejects_inconsistent_or_unsupported_evidence(defect):
    p=payload();a=p['account'];a['cashflows']=[flow()]
    if defect=='future':p['as_of']='2026-10-01T00:00:00Z'
    elif defect=='reverse':a['end']['at']=a['start']['at']
    elif defect=='naive':a['start']['at']='2026-10-01T00:00:00'
    elif defect=='outside':a['cashflows'][0]['at']=a['end']['at']
    elif defect=='same_time':a['cashflows'].append(dict(flow(),id='flow-2'))
    elif defect=='duplicate_id':a['external_operating_costs']=[dict(id='flow-1',at='2026-10-01T20:00:00Z',amount='10',allocation_ref='cost')]
    elif defect=='contradiction':a['cashflows'][0]['post_equity']='2099'
    elif defect=='one_boundary':a['cashflows'][0]['pre_equity']=None
    elif defect=='dividend':a['cashflows'][0]['kind']='dividend'
    elif defect=='negative_amount':a['cashflows'][0]['amount']='-1'
    elif defect=='extra':a['cashflows'][0]['secret']='unrecognized'
    elif defect=='bad_sha':a['source_sha256']='bad'
    elif defect=='bool_flag':a['declared_complete']=1
    elif defect=='too_many':a['cashflows']=[flow()]*10001
    with pytest.raises(ValueError):report(p)


def benchmark(p):
    b=deepcopy(p['account']);b['source_ref']='synthetic-benchmark';b['source_sha256']='b'*64
    p['benchmarks']=[dict(name='KODEX200',methodology_ref='synthetic-total-return',portfolio=b)]
    return b


def test_matched_benchmark_uses_flows_not_ids_or_boundary_nav():
    p=payload();p['account']['cashflows']=[flow()];p['account']['end']['equity']='2310'
    b=benchmark(p);b['cashflows']=[dict(flow(pre='1200',post='2200'),id='other-id')];b['end']['equity']='2420'
    out=report(p);comp=out['benchmarks'][0]
    assert number(comp['net_gain_difference'])==-110
    assert number(comp['twr_difference'])==Decimal('-.11')
    assert out['benchmark_basis']=='matched_supplied_portfolios'


@pytest.mark.parametrize('defect',['period','capital','flow_amount','flow_kind','flow_time','duplicate','name'])
def test_rejects_unmatched_benchmark(defect):
    p=payload();p['account']['cashflows']=[flow()];b=benchmark(p)
    if defect=='period':b['end']['at']='2026-10-02T00:01:00Z'
    elif defect=='capital':b['start']['equity']='999'
    elif defect=='flow_amount':b['cashflows'][0].update(amount='999',post_equity='2099')
    elif defect=='flow_kind':b['cashflows'][0]['kind']='transfer_in'
    elif defect=='flow_time':b['cashflows'][0]['at']='2026-10-01T13:00:00Z'
    elif defect=='duplicate':p['benchmarks']*=2
    elif defect=='name':p['benchmarks'][0]['name']='unapproved'
    with pytest.raises(ValueError):report(p)


def test_missing_or_incomplete_benchmark_never_invents_comparison():
    p=payload();assert report(p)['benchmarks']==[]
    b=benchmark(p);b['declared_complete']=False
    r=report(p)['benchmarks'][0];assert r['net_gain_difference'] is None and r['twr_difference'] is None


def cli():
    return importlib.import_module('scripts.report_account_net_return')


def test_cli_roundtrip_hash_readonly(tmp_path,capsys):
    path=tmp_path/'input.json';raw=json.dumps(payload()).encode();path.write_bytes(raw)
    before=path.stat()
    assert cli().main(['--input',str(path),'--max-input-bytes','100000'])==0
    out=json.loads(capsys.readouterr().out)
    assert out['input_sha256']==hashlib.sha256(raw).hexdigest()
    assert path.read_bytes()==raw and path.stat().st_mtime_ns==before.st_mtime_ns


@pytest.mark.parametrize('kind',['symlink','fifo','directory','over_limit','duplicate_json','nonfinite_json','invalid_json','negative_limit'])
def test_cli_rejects_unsafe_input_without_echoing_private_content(tmp_path,capsys,kind):
    path=tmp_path/'PRIVATE-input';limit='100000'
    if kind=='symlink':target=tmp_path/'target';target.write_text('{}');path.symlink_to(target)
    elif kind=='fifo':os.mkfifo(path)
    elif kind=='directory':path.mkdir()
    elif kind=='over_limit':path.write_bytes(b' '*101);limit='100'
    elif kind=='duplicate_json':path.write_text('{"PRIVATE":1,"PRIVATE":2}')
    elif kind=='nonfinite_json':path.write_text('{"PRIVATE":NaN}')
    elif kind=='invalid_json':path.write_text('PRIVATE')
    else:path.write_text('{}');limit='-1'
    assert cli().main(['--input',str(path),'--max-input-bytes',limit])==2
    captured=capsys.readouterr();assert not captured.out and 'PRIVATE' not in captured.err


def test_cli_real_process_outputs_single_json(tmp_path):
    from pathlib import Path
    import subprocess
    import sys
    root=Path(__file__).resolve().parents[1]
    path=tmp_path/'input.json';path.write_text(json.dumps(payload()))
    result=subprocess.run([sys.executable,str(root/'scripts/report_account_net_return.py'),
        '--input',str(path),'--max-input-bytes','100000'],capture_output=True,text=True,timeout=15)
    assert result.returncode==0 and result.stderr==''
    assert number(json.loads(result.stdout)['account']['net_gain'])==80


def test_cli_rejects_utc_normalization_overflow(tmp_path,capsys):
    p=payload();p['account']['start']['at']='0001-01-01T00:00:00+23:00'
    path=tmp_path/'input.json';path.write_text(json.dumps(p))
    assert cli().main(['--input',str(path),'--max-input-bytes','100000'])==2
    assert not capsys.readouterr().out


def test_cli_refuses_changed_file_metadata(tmp_path,monkeypatch,capsys):
    from types import SimpleNamespace
    path=tmp_path/'input.json';path.write_text(json.dumps(payload()))
    module=cli();real=module.os.fstat;calls=[]
    def changed(fd):
        value=real(fd);calls.append(1)
        if len(calls)==1:return value
        return SimpleNamespace(st_size=value.st_size,st_mtime_ns=value.st_mtime_ns+1,st_ctime_ns=value.st_ctime_ns)
    monkeypatch.setattr(module.os,'fstat',changed)
    assert module.main(['--input',str(path),'--max-input-bytes','100000'])==2
    assert not capsys.readouterr().out
