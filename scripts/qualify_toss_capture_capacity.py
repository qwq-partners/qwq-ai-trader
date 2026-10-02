#!/usr/bin/env python3
"""토스 유한 관측기의 합성 용량 검증. 외부 접속·운영 입력 없음."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from src.analytics.toss_capture_capacity import qualify


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--suite',choices=('smoke','full'),required=True)
    args=p.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory(prefix='qwq-synthetic-capacity-') as directory:
            result=asyncio.run(qualify(args.suite,directory=Path(directory)))
        print(json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False))
        return 0 if result['test_expectations_met'] else 1
    except (ValueError,OSError):
        print('[합성 용량] 입력/검증 오류',file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())
