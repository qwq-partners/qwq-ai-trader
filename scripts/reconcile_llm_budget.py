#!/usr/bin/env python3
"""Preview/apply an operator-reviewed cost reconciliation. No API calls or restart."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.utils.llm_budget_reconciliation import reconcile_budget
from src.utils.llm_budget import BudgetBlocked


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ledger',type=Path,required=True)
    p.add_argument('--limit-usd',type=float,required=True)
    p.add_argument('--review',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True)
    p.add_argument('--apply',action='store_true',help='explicit operator application of the reviewed dry-run')
    p.add_argument('--expected-state-sha256')
    a=p.parse_args()
    if a.apply and not a.expected_state_sha256:
        p.error('--apply requires the reviewed --expected-state-sha256')
    try:
        if a.review.stat().st_size>1024*1024:
            p.error('review exceeds 1 MiB')
        result=reconcile_budget(a.ledger,a.limit_usd,json.loads(a.review.read_text()),a.evidence,
                                apply=a.apply,expected_state_sha256=a.expected_state_sha256)
    except (ValueError, BudgetBlocked, OSError) as exc:
        p.error(str(exc))
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))


if __name__=='__main__':
    main()
