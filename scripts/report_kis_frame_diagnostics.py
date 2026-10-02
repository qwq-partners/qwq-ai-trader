#!/usr/bin/env python3
"""명시 원장/연구 파일에서 KIS 오류 범위를 읽기 전용으로 보고한다."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.report_entry_gate_trace import _study
from src.analytics.entry_observation_journal import read_observation_journal
from src.analytics.kis_frame_diagnostics import build_frame_report, validate_study_binding


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal', type=Path, required=True)
    parser.add_argument('--study', type=Path, required=True)
    parser.add_argument('--as-of', required=True)
    parser.add_argument('--max-journal-bytes', type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.max_journal_bytes <= 256 * 1024 * 1024:
            raise ValueError('원장 읽기 한도 위반')
        raw, study = _study(args.study)
        sha = hashlib.sha256(raw).hexdigest()
        obs = read_observation_journal(args.journal, max_bytes=args.max_journal_bytes, expected_study_sha256=sha)
        validate_study_binding(obs, study)
        out = build_frame_report(obs, as_of=args.as_of)
        out.update(study_sha256=sha, study_binding_validated=True)
        print(json.dumps(out, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        print('[KIS 프레임 진단] 입력 계약 오류', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
