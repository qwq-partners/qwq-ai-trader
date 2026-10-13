#!/usr/bin/env python3
"""Explicit file-only paired comparison report; no providers, credentials or orders."""
import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analytics.decision_quote_join import DEFAULT_MAX_BYTES, _read_lines
from src.analytics.entry_observation_journal import read_observation_journal
from src.analytics.model_comparison_daily_report import build_daily_report
from src.analytics.model_comparison_runner import report_pairs


def _jsonl(path, max_bytes):
    return [json.loads(line) for line in _read_lines(path, max_bytes=max_bytes) if line.strip()] if path else []


def main(argv=None):
    parser = argparse.ArgumentParser(description='동일 역할 비교 기록과 봉인 관측 원장을 연결한다. 순수익/체결을 인증하지 않는다.')
    parser.add_argument('--pairs', required=True, help='명시 JSONL 비교 결과 파일')
    parser.add_argument('--ledger', help='봉인된 관측 원장')
    parser.add_argument('--study-sha256', help='관측 계획의 SHA256; --ledger와 함께 필수')
    parser.add_argument('--max-bytes', type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument('--max-delay-seconds', type=float, default=5.0)
    parser.add_argument('--max-state-age-seconds', type=float, default=5.0)
    parser.add_argument('--daily', metavar='YYYY-MM-DD', help='KST 하루의 일별 보고(v1/v2 행). 텔레그램은 미리보기 문자열만')
    parser.add_argument('--envelopes', help='--daily 용 입력 봉투 JSONL (제공 분모·선정)')
    parser.add_argument('--exclusions', help='--daily 용 제외 JSONL ({decision_id, reason})')
    args = parser.parse_args(argv)
    try:
        if args.max_bytes <= 0:
            raise ValueError('max-bytes must be positive')
        pairs = [json.loads(line) for line in _read_lines(args.pairs, max_bytes=args.max_bytes) if line.strip()]
        if args.daily:
            result = build_daily_report(pairs, day_kst=args.daily,
                                        offered_envelopes=_jsonl(args.envelopes, args.max_bytes),
                                        exclusions=_jsonl(args.exclusions, args.max_bytes))
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        payloads, status = [], None
        if bool(args.ledger) != bool(args.study_sha256):
            raise ValueError('--ledger and --study-sha256 must be supplied together')
        if args.ledger:
            if re.fullmatch('[0-9a-f]{64}', args.study_sha256) is None:
                raise ValueError('invalid study hash')
            verified = read_observation_journal(args.ledger, max_bytes=args.max_bytes, expected_study_sha256=args.study_sha256)
            payloads = [r for r in verified['records'] if isinstance(r, dict) and r.get('kind') in ('ws_quote', 'market_state')]
            status = {'verified': True, 'complete': verified['complete'], 'incomplete_reasons': verified['incomplete_reasons']}
        result = report_pairs(pairs, payloads=payloads, ledger_status=status,
                              max_delay_seconds=args.max_delay_seconds, max_state_age_seconds=args.max_state_age_seconds)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ValueError, TypeError, KeyError, OSError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
