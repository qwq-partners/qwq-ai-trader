"""Run the real offline CLI against explicit temporary files only."""
import json
from pathlib import Path
import subprocess
import sys

import pytest
from h1_multiday_fixtures import payload

CLI=Path(__file__).parents[1]/'scripts/evaluate_h1_multiday.py'


def run(path,*args):
    return subprocess.run([sys.executable,str(CLI),'--input',str(path),*map(str,args)],
                          capture_output=True,text=True,timeout=15)


def test_empty_explicit_manifest_reports_twenty_missing_days_without_io_discovery(tmp_path):
    p=payload(tmp_path,all_days=False);p['days']=[]
    source=tmp_path/'input.json';source.write_text(json.dumps(p))
    result=run(source)
    assert result.returncode==0,result.stderr
    report=json.loads(result.stdout)
    assert len(report['quality_failures'])==20
    assert report['economic_verdict']=='suppressed'
    assert source.read_text()==json.dumps(p)


@pytest.mark.parametrize('case',['duplicate_json','symlink','fifo','oversize','wrong_fields','relative_path'])
def test_cli_rejects_unsafe_or_ambiguous_explicit_inputs_without_output(tmp_path,case):
    p=payload(tmp_path,all_days=False);p['days']=[]
    source=tmp_path/'input.json';source.write_text(json.dumps(p))
    if case=='duplicate_json':source.write_text('{"days":[],"days":[]}')
    elif case=='symlink':
        link=tmp_path/'link.json';link.symlink_to(source);source=link
    elif case=='fifo':
        import os
        source.unlink();os.mkfifo(source)
    elif case=='oversize':source.write_bytes(b' '* (4*1024*1024+1))
    elif case=='wrong_fields':p['restart']=True;source.write_text(json.dumps(p))
    else:
        p['days']=[dict(date=p['protocol']['calendar']['scheduled_dates'][0],
                       study_path='relative.json',journal_path='relative.jsonl',max_journal_bytes=1048576)]
        source.write_text(json.dumps(p))
    result=run(source)
    assert result.returncode==2
    assert result.stdout==''


def test_real_sealed_journal_is_read_with_its_original_study_and_review(tmp_path):
    import asyncio
    from copy import deepcopy
    import hashlib
    from unittest.mock import patch
    from src.analytics.entry_observation import EntryObservationBuffer
    from src.analytics.entry_observation_journal import ObservationJournal,read_observation_journal
    from h1_multiday_fixtures import rebind
    p=payload(tmp_path,all_days=False);day=p['days'][0]
    study=tmp_path/'original-study.json';study.write_text(day['study_utf8'])
    sha=hashlib.sha256(study.read_bytes()).hexdigest()
    journal=tmp_path/'original-journal.jsonl'
    async def write():
        b=EntryObservationBuffer(evaluation_epoch=p['protocol']['evaluation_epoch'],capacity=1000,
            frame_diagnostics_settings={'version':'kis-frame-diagnostics-v1'})
        with patch('src.analytics.entry_observation_journal._timestamp',return_value=day['date']+'T09:11:59+09:00'):
            j=await ObservationJournal.open(b,journal,study_ref='synthetic-only',study_sha256=sha,
                queue_capacity=1000,batch_size=100,max_bytes=1048576,max_record_bytes=65536)
        for record in day['observations']['records']:
            r=deepcopy(record);r.pop('sequence');assert b.publish(r)
        with patch('src.analytics.entry_observation_journal._timestamp',return_value=day['date']+'T09:45:00+09:00'):
            closed=await j.close()
        assert closed['sealed'] and closed['fsync_confirmed']
    asyncio.run(write())
    day['observations']=read_observation_journal(journal,max_bytes=1048576,expected_study_sha256=sha)
    assert day['observations']['journal']['durability_confirmed'] is False
    rebind(day)
    review=tmp_path/'review.json';review.write_text(json.dumps(day['session_review']))
    p['days']=[dict(date=day['date'],study_path=str(study),journal_path=str(journal),
                   max_journal_bytes=1048576,session_review_path=str(review),max_review_bytes=1048576)]
    path=tmp_path/'manifest.json';path.write_text(json.dumps(p))
    before={f:hashlib.sha256(f.read_bytes()).hexdigest() for f in (study,journal,review,path)}
    result=run(path)
    assert result.returncode==0,result.stderr
    out=json.loads(result.stdout)
    assert out['days'][0]['status']=='complete_selected_window'
    assert out['days'][0]['selected_admissions']==3
    assert out['scenarios']['0']['daily'][0]['a_net_pnl'] is not None
    assert before=={f:hashlib.sha256(f.read_bytes()).hexdigest() for f in before}
