#!/usr/bin/env python3
"""명시한 engine/toss/as_of JSON만 읽어 후보 호가 품질 보고서를 출력한다.

인증·웹소켓 접속·운영 파일 탐색·손익 계산 없음. 입력은 수정하지 않는다.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analytics.toss_candidate_observation import build_toss_candidate_report  # noqa: E402
from src.data.providers.toss.orderbook_stream import reject_json_constant, unique_json_keys  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--input", type=Path)
    sources.add_argument("--artifact", type=Path)
    parser.add_argument("--plan-sha256")
    args = parser.parse_args(argv)
    try:
        if args.artifact:
            from src.observation.toss_ws_service import read_capture_artifact
            value = read_capture_artifact(args.artifact, max_bytes=64 * 1024 * 1024,
                                          plan_hash=args.plan_sha256)
            if (type(value.get('service_complete')) is not bool
                    or type(value.get('service_reason')) is not str
                    or value.get('profit_comparison_available') is not False):
                raise ValueError('서비스 완료 상태 필요')
        else:
            if args.plan_sha256 is not None:
                raise ValueError('artifact 입력에만 plan hash 사용')
            with args.input.open("rb") as stream:
                data = stream.read(64 * 1024 * 1024 + 1)
            if len(data) > 64 * 1024 * 1024:
                raise ValueError("입력 상한 초과")
            value = json.loads(data, object_pairs_hook=unique_json_keys, parse_constant=reject_json_constant)
        report = build_toss_candidate_report(value["engine"], value["toss"], as_of=value["as_of"])
        if args.artifact:
            report.update(service_complete=value['service_complete'], service_reason=value['service_reason'],
                          approved_plan_sha256=args.plan_sha256)
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        print("[토스 후보 관측] 입력 계약 오류", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
