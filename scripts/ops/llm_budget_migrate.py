#!/usr/bin/env python3
"""LLM 월 예산 상한을 명시 이관한다(L01 Task 5, D2: 설정만 바꾸면 budget_configuration_mismatch 로 막힌다).

봇이 멈춘 상태에서만 실행된다(봇 싱글톤 락을 잡는다). 사용액·예약·unknown·정책 행은 바꾸지 않고
`settings.limit_units` 와 `limit_migrations` 영수증 행만 추가한다. 이관 뒤 `config/default.yml` 의
`llm.monthly_budget` 이 새 상한과 같아야 봇이 뜬다(다르면 원장이 fail-closed 로 유료 호출을 막는다).

순서(배포 창, 장외): 새 소스(설정 200) 배포 → `sudo systemctl stop qwq-ai-trader` → 이 명령 → `sudo systemctl start`.
--check 는 읽기 전용으로 현재 상한·행 수·활성 예약만 보인다.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.llm_budget import BudgetBlocked, migrate_monthly_limit  # noqa: E402
from src.utils.trader_lock import hold_or_exit  # noqa: E402

DEFAULT_PATH = "~/.local/state/qwq-ai-trader/llm-budget.sqlite3"   # config/default.yml llm.budget_state_path 와 같다


def check(path: Path) -> dict:
    path.stat()
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.1, isolation_level=None) as db:
        db.row_factory = sqlite3.Row
        settings = db.execute("SELECT limit_units, activated_at, baseline_month FROM settings").fetchone()
        states = {r["state"]: r["n"] for r in db.execute("SELECT state, COUNT(*) AS n FROM reservations GROUP BY state")}
        migrations = (db.execute("SELECT COUNT(*) FROM limit_migrations").fetchone()[0]
                      if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='limit_migrations'").fetchone() else 0)
    return {"path": str(path), "limit_usd": settings["limit_units"] / 1e9, "activated_at": settings["activated_at"],
            "baseline_month": settings["baseline_month"], "reservation_states": states, "migrations": migrations}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--path", default=DEFAULT_PATH, help=f"원장 파일(기본 {DEFAULT_PATH})")
    parser.add_argument("--check", action="store_true", help="읽기 전용 상태 출력(이관 없음)")
    parser.add_argument("--from", dest="current", type=float, help="현재 상한 USD(원장과 일치해야 함)")
    parser.add_argument("--to", dest="new", type=float, help="새 상한 USD")
    parser.add_argument("--note", default="", help="승인 근거(사용자 결정 일자·크레딧 조건). 원장에 영구 기록")
    parser.add_argument("--missing-cost-policy", default="log_and_continue", choices=("log_and_continue", "block"),
                        help="정책 단계 재계산에 쓰는 불명 비용 정책(운영 default.yml 과 같게, 기본 log_and_continue)")
    args = parser.parse_args(argv)
    path = Path(args.path).expanduser()
    try:
        if args.check:
            print(json.dumps(check(path), ensure_ascii=False, indent=1))
            return 0
        if args.current is None or args.new is None or not args.note.strip():
            parser.error("--from, --to, --note 가 모두 필요하다(또는 --check)")
        hold_or_exit("llm_budget_migrate")   # 봇·다른 CLI 가 실행 중이면 exit 2
        receipt = migrate_monthly_limit(path, current_limit_usd=args.current, new_limit_usd=args.new, note=args.note,
                                        missing_cost_policy=args.missing_cost_policy)
        print(json.dumps(receipt, ensure_ascii=False, indent=1))
        return 0
    except BudgetBlocked as error:
        print(json.dumps({"error": error.reason, "path": str(path)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as error:
        # 독립 리뷰 2차 P2: --check 의 잠금 시한·손상 DB 도 같은 구조화 오류·exit 2 로 통일한다.
        print(json.dumps({"error": "storage_unavailable", "detail": error.__class__.__name__}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
