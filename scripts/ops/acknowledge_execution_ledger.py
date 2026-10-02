#!/usr/bin/env python3
"""실행 원장의 과거 비정상 종료·미완결 주문을 운영자 확인(acknowledge)으로 보류 사유에서 제외한다.

봇이 멈춘 상태에서만 실행된다(봇 싱글톤 락을 잡는다). 과거 기록은 지우지 않고 `acknowledged`
표시만 남기며, 체결 재생·장부/잔고 수정·KIS 호출은 하지 않는다. 실행 전에 HTS 미체결·체결과
`scripts/reconcile_execution_evidence.py` 대사를 끝낸 뒤 그 근거를 --note 에 적는다.
설계: docs/reviews/codex-recent-work-review-2026-10-02.md P1-1.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.execution.execution_ledger import ExecutionLedger, ExecutionLedgerError, order_unresolved  # noqa: E402
from src.execution.execution_history import execution_ledger_location  # noqa: E402
from src.utils.trader_lock import hold_or_exit  # noqa: E402


def _summary(snapshot: dict, sessions: dict) -> dict:
    orders = snapshot["orders"]
    current = snapshot["session_id"]  # 확인 세션 자신은 아직 열려 있어 비정상으로 세지 않는다
    return {
        "prior_unclean": snapshot["prior_unclean"],
        "unclean_sessions": sum(1 for k, v in sessions.items()
                                if k != current and not v["clean"] and v.get("acknowledged") is not True),
        "unresolved_orders": sum(1 for o in orders.values() if order_unresolved(o) and o.get("acknowledged") is not True),
    }


async def _run(path: Path, scope: str, note: str) -> dict:
    ledger = ExecutionLedger(path, scope)
    session = f"ack-{uuid4().hex}"
    before = await ledger.open(session)
    before_sessions = (await _sessions(ledger))
    await ledger.acknowledge(note)
    after = await ledger.snapshot()
    closed = await ledger.close_session()
    return {
        "before": _summary(before, before_sessions),
        "after": _summary(after, await _sessions(ledger)),
        "ack_session_closed": closed,
    }


async def _sessions(ledger: ExecutionLedger) -> dict:
    # snapshot 은 sessions 를 돌려주지 않으므로 projection 을 직접 읽는다(읽기 전용 쿼리, 원장 연산 사이에서만).
    with contextlib.closing(sqlite3.connect(ledger.path, isolation_level=None)) as connection:
        return {k: json.loads(v) for k, v in connection.execute("SELECT session_id, payload FROM sessions")}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--note", required=True, help="대사 근거(HTS 확인 시각·reconcile 보고서 지문 등). 원장에 영구 기록된다.")
    parser.add_argument("--ledger", type=Path, help="원장 파일(기본: 운영 KIS 설정으로 계산)")
    parser.add_argument("--account-scope", help="--ledger 와 함께 쓰는 계좌 범위 해시")
    args = parser.parse_args(argv)
    if not args.note.strip():
        parser.error("--note 는 비울 수 없다")
    if (args.ledger is None) != (args.account_scope is None):
        parser.error("--ledger 와 --account-scope 는 함께 준다")
    hold_or_exit("acknowledge_execution_ledger")
    try:
        if args.ledger is not None:
            path, scope = args.ledger, args.account_scope
        else:
            from src.execution.broker.kis_kr import KISConfig
            cfg = KISConfig.from_env()
            if not cfg.account_no:
                print("KIS 계좌 설정이 없어 원장 위치를 정할 수 없다(.env 로드 확인)", file=sys.stderr)
                return 2
            path, scope = execution_ledger_location(cfg.env, cfg.account_no, cfg.account_product_cd)
        if not path.exists():
            print("실행 원장 파일이 없다 — 해제할 보류가 없다", file=sys.stderr)
            return 2
        result = asyncio.run(_run(path, scope, args.note))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ack_session_closed"] else 1
    except (ExecutionLedgerError, sqlite3.Error, OSError, ValueError, TypeError):
        # 경로·계좌 범위·원장 내용은 오류 메시지에 쓰지 않는다.
        print("실행 원장 확인 실패: 원장 손상/잠금/범위 불일치. 독립 복사본으로 reconcile_execution_evidence.py 를 먼저 실행하라.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
