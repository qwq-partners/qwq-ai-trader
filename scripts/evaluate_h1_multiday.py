#!/usr/bin/env python3
"""Read an explicit H1 manifest, one original journal at a time; emit JSON only."""
from __future__ import annotations

import argparse
from decimal import DecimalException
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analytics.entry_evaluation_bundle import load_json_bytes  # noqa: E402
from src.analytics.entry_observation_journal import read_observation_journal  # noqa: E402
from src.analytics.h1_multiday_evaluation import build_h1_multiday_report  # noqa: E402

MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_JOURNAL_BYTES = 512 * 1024 * 1024
MAX_REVIEW_BYTES = 256 * 1024 * 1024


def _read(path,maximum=MAX_JSON_BYTES):
    fd=os.open(path,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as handle:
        info=os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size>maximum:
            raise ValueError('explicit input must be a regular file within byte limit')
        raw=handle.read(maximum+1)
    if len(raw)>maximum:raise ValueError('input byte limit exceeded')
    return raw,load_json_bytes(raw)


def _path(value):
    if not isinstance(value,str) or not value or not Path(value).is_absolute():
        raise ValueError('explicit absolute input paths required')
    return Path(value)


def _days(items):
    if not isinstance(items,list) or len(items)>20:
        raise ValueError('at most twenty explicit day manifests required')
    required={'date','study_path','journal_path','max_journal_bytes'}
    for item in items:
        if not isinstance(item,dict) or not required <= set(item) or set(item)-required-{'session_review_path','max_review_bytes'}:
            raise ValueError('strict day manifest fields required')
        maximum=item['max_journal_bytes']
        if type(maximum) is not int or not 0<maximum<=MAX_JOURNAL_BYTES:
            raise ValueError('max_journal_bytes must be 1..536870912')
        raw,_=_read(_path(item['study_path']))
        sha=hashlib.sha256(raw).hexdigest()
        observations=read_observation_journal(_path(item['journal_path']),max_bytes=maximum,
                                              expected_study_sha256=sha)
        day=dict(date=item['date'],study_utf8=raw.decode('utf-8'),observations=observations)
        if 'session_review_path' in item:
            review_limit=item.get('max_review_bytes')
            if type(review_limit) is not int or not 0<review_limit<=MAX_REVIEW_BYTES:
                raise ValueError('explicit max_review_bytes must be 1..268435456')
            day['session_review']=_read(_path(item['session_review_path']),review_limit)[1]
        elif 'max_review_bytes' in item:
            raise ValueError('review limit requires review path')
        yield day
        del raw,observations,day


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True,help='explicit H1 protocol/day path manifest; at most4MiB')
    args=parser.parse_args(argv)
    try:
        _,payload=_read(args.input)
        if not isinstance(payload,dict) or set(payload)!={'protocol','days','as_of'}:
            raise ValueError('strict manifest fields required')
        report=build_h1_multiday_report(payload['protocol'],_days(payload['days']),analysis_as_of=payload['as_of'])
        output=json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)
    except (OSError,ValueError,UnicodeError,KeyError,TypeError,AttributeError,RecursionError,DecimalException) as exc:
        print(f'[H1 다일 평가] 입력 오류: {exc}',file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
