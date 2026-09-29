"""봇 실행 중 주문 CLI 거부 — 봇이 기동부터 종료까지 쥐는 flock 을 CLI 가 시험 삼아 잡는다 (2026-09-29).

새 주문 CLI 는 인자 파싱 직후, 토큰·브로커를 만들기 전에 `hold_or_exit("<도구명>")` 를 부른다.
획득한 락은 프로세스가 끝날 때까지 쥔다(그동안 봇 재기동도 거부된다). 락 파일은 지우지 않는다.
설계: docs/superpowers/specs/2026-09-29-cli-refusal-orderref-design.md §2 D1
"""
from __future__ import annotations

import fcntl
import sys
from pathlib import Path

_held = None  # 획득한 fd — 프로세스 수명 동안 유지


def lock_path() -> Path:
    """봇 싱글톤 락 경로 — 호출 시점의 Path.home() 으로 계산한다(시험의 home 패치로 격리된다)."""
    return Path.home() / ".cache" / "ai_trader" / "unified_trader.lock"


def hold_or_exit(tool: str, path: Path | None = None) -> None:
    """락을 잡으면 쥔 채 돌아오고, 봇·다른 CLI 가 쥐고 있으면 안내를 stderr 에 쓰고 exit 2."""
    global _held
    p = path if path is not None else lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    f = open(p, "a")  # "w" 는 봇이 쓴 PID 를 자른다
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        sys.stderr.write(
            f"[{tool}] 봇 또는 다른 주문 CLI 가 실행 중 — 주문 CLI 거부\n"
            f"  누가 쥐었나: fuser -v ~/.cache/ai_trader/unified_trader.lock\n"
            f"  봇이면(급할 때 1순위): touch ~/.cache/ai_trader/KILL_SWITCH 후 MTS/HTS 에서 미체결 일괄취소·매도 → 완료 전 봇 정지·재확인\n"
            f"  CLI 로 하려면: KILL_SWITCH → sudo systemctl stop qwq-ai-trader → HTS 미체결 취소 확인 → 이 명령 재실행\n"
        )
        sys.exit(2)
    _held = f
