#!/usr/bin/env python3
"""명시한 JSON 입력만 읽는 가격 조건 비교. 주문/네트워크/운영 상태 접근 없음.

사용: python scripts/compare_entry_price_shadow.py --input snapshots.json
결과는 stdout JSON, 구조 오류는 stderr 및 exit 2. 입력 파일은 수정하지 않는다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analytics.entry_price_shadow import build_report  # noqa: E402
from src.analytics.entry_observation import prepare_input  # noqa: E402
from src.analytics.entry_observation_journal import read_observation_journal  # noqa: E402


def _reject_constant(value):
    raise ValueError("JSON의 NaN/Infinity는 허용하지 않는다")


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 객체의 중복 키")
        result[key] = value
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--input", type=Path, help="명시적으로 준비한 후보 JSON")
    inputs.add_argument("--observations", type=Path, help="context/observations/evaluation_inputs를 담은 명시 JSON")
    inputs.add_argument("--journal", type=Path, help="명시 관측 원장(JSONL), 원본 무변경")
    parser.add_argument("--study", type=Path, help="수집 시 지문을 고정한 context JSON")
    parser.add_argument("--evaluation-inputs", type=Path, help="후보별 명시 보충자료 JSON 배열")
    parser.add_argument("--max-journal-bytes", type=int, help="원장 읽기 상한(바이트)")
    parser.add_argument("--analysis-as-of", help="aware audit timestamp for received observations only")
    args = parser.parse_args(argv)
    if args.input is not None and args.analysis_as_of is not None:
        parser.error("--analysis-as-of requires --observations or --journal")
    supplements = (args.study, args.evaluation_inputs, args.max_journal_bytes)
    if args.journal is not None and any(v is None for v in supplements):
        parser.error("--journal에는 --study, --evaluation-inputs, --max-journal-bytes가 모두 필요")
    if args.journal is None and any(v is not None for v in supplements):
        parser.error("원장 보충 인자는 --journal과만 사용 가능")
    try:
        if args.journal is not None:
            study_bytes = args.study.read_bytes()
            context = json.loads(study_bytes, parse_constant=_reject_constant, object_pairs_hook=_unique_keys)
            evaluation_inputs = json.loads(args.evaluation_inputs.read_bytes(), parse_constant=_reject_constant,
                                          object_pairs_hook=_unique_keys)
            observations = read_observation_journal(args.journal, max_bytes=args.max_journal_bytes,
                expected_study_sha256=hashlib.sha256(study_bytes).hexdigest())
            result = prepare_input(context, observations, evaluation_inputs, analysis_as_of=args.analysis_as_of)
            result["journal"] = observations["journal"]
        else:
            path = args.input if args.input is not None else args.observations
            payload = json.loads(path.read_text(encoding="utf-8"),
                                 parse_constant=_reject_constant, object_pairs_hook=_unique_keys)
            if args.observations is not None:
                result = prepare_input(payload["context"], payload["observations"], payload["evaluation_inputs"],
                                       analysis_as_of=args.analysis_as_of)
            else:
                result = build_report(payload)
        output = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    except (OSError, ValueError, UnicodeError, KeyError, TypeError, AttributeError) as exc:
        print(f"[가격조건 비교] 입력 오류: {exc}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
