#!/usr/bin/env python3
"""Read an explicit original journal and optional session review; emit offline diagnostics.

No market connection, file mutation, operating-state discovery or automatic session claims.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analytics.entry_evaluation_bundle import build_evaluation_bundle, load_json_bytes  # noqa: E402
from src.analytics.entry_exit_comparison import build_exit_comparison  # noqa: E402
from src.analytics.entry_observation_journal import read_observation_journal  # noqa: E402

MAX_INPUT_BYTES = 4 * 1024 * 1024


def _read_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_INPUT_BYTES:
            raise ValueError('input must be a regular file within 4MiB')
        raw = handle.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError('input exceeds 4MiB')
    return raw, load_json_bytes(raw)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal', type=Path, required=True)
    parser.add_argument('--study', type=Path, required=True)
    parser.add_argument('--max-journal-bytes', type=int, required=True)
    parser.add_argument('--session-review', type=Path)
    parser.add_argument('--exit-policy', type=Path,
                        help='strict same-entry initial-stop comparison policy JSON')
    args = parser.parse_args(argv)
    try:
        study_bytes, _ = _read_json(args.study)
        sha = hashlib.sha256(study_bytes).hexdigest()
        observations = read_observation_journal(args.journal, max_bytes=args.max_journal_bytes,
                                                expected_study_sha256=sha)
        review = _read_json(args.session_review)[1] if args.session_review is not None else None
        result = build_evaluation_bundle(study_bytes, observations, study_sha256=sha, session_review=review)
        if args.exit_policy is not None:
            comparison_policy = _read_json(args.exit_policy)[1]
            result['exit_comparison'] = build_exit_comparison(
                study_bytes, observations, study_sha256=sha, comparison_policy=comparison_policy,
                session_review=review)
        output = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    except (OSError, ValueError, UnicodeError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        print(f'[평가입력 준비] 입력 오류: {exc}', file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
