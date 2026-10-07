#!/usr/bin/env python3
"""Explain a fixed journal's live-screening gates offline. No orders or source discovery."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from src.analytics.entry_evaluation_bundle import load_json_bytes
from src.analytics.entry_gate_report import build_gate_report
from src.analytics.entry_gate_trace import validate_settings
from src.analytics.entry_observation_journal import read_observation_journal


def _study(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as handle:
        before=os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size>4*1024*1024:raise ValueError('bounded regular study required')
        raw=handle.read(4*1024*1024+1);after=os.fstat(handle.fileno())
    if len(raw)>4*1024*1024 or (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
        raise ValueError('study changed or exceeded bound')
    return raw,load_json_bytes(raw)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--journal',type=Path,required=True)
    p.add_argument('--study',type=Path,required=True)
    p.add_argument('--as-of',required=True)
    p.add_argument('--max-journal-bytes',type=int,required=True)
    args=p.parse_args(argv)
    try:
        if not 1<=args.max_journal_bytes<=256*1024*1024:raise ValueError('journal limit outside 1..256MiB')
        raw,study=_study(args.study);sha=hashlib.sha256(raw).hexdigest()
        obs=read_observation_journal(args.journal,max_bytes=args.max_journal_bytes,expected_study_sha256=sha)
        if obs['evaluation_epoch']!=study.get('evaluation_epoch'):raise ValueError('study epoch mismatch')
        if (study.get('capture', {}).get('version') in ('runner-first-scan-v4', 'runner-signal-window-v1')
                or 'frame_diagnostics' in obs):
            from src.analytics.kis_frame_diagnostics import validate_study_binding, build_frame_report
            validate_study_binding(obs, study)
            build_frame_report(obs, as_of=args.as_of)
        from src.analytics.entry_observation_runtime import validate_window_contract
        validate_window_contract(study, obs)
        out=build_gate_report(obs,as_of=args.as_of)
        if out['references']:
            capture=study.get('capture',{})
            if capture.get('version') not in ('runner-first-scan-v3','runner-first-scan-v4', 'runner-signal-window-v1'):raise ValueError('gate trace requires v3/v4 study')
            settings=validate_settings(capture.get('entry_gate_trace'))
            if any(len(r['candidates']) > settings['max_candidates'] for r in obs['records'] if r.get('kind') == 'scan'):
                raise ValueError('trace exceeds declared study candidate bound')
            for key,marker in (('source_version_ref','entry_gate_source_version_ref'),
                               ('configuration_ref','entry_gate_configuration_ref')):
                if capture.get(key)!=settings[key] or out['references'][marker]!=settings[key]:
                    raise ValueError('gate source/configuration declaration mismatch')
        out.update(study_sha256=sha,journal=obs.get('journal'),study_binding_validated=True)
        print(json.dumps(out,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False))
        return 0
    except (OSError,ValueError,TypeError,KeyError,AttributeError,RecursionError):
        print('[진입 단계 관측] 입력 계약 오류',file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())
