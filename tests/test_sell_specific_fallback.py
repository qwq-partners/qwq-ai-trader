"""sell_specific 15초 폴백 수량 — 보유 전량이 아니라 요청 중 안 팔린 몫만 (2026-09-29).

스크립트는 import 시 load_env()·브로커 import 가 돌므로 fallback_qty 함수만 AST 로 뽑아 실행한다.
"""
import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "scripts" / "sell_specific.py"


def _load():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "fallback_qty")
    ns: dict = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SRC), "exec"), ns)
    return ns["fallback_qty"]


fallback_qty = _load()


@pytest.mark.parametrize("requested,before,now,expected", [
    (10, 100, 90, 0),     # 100 보유·10 요청 전량 체결 → 남은 90주를 팔지 않는다
    (10, 100, 96, 6),     # 10 중 4 체결 → 6
    (10, 100, 100, 10),   # 미체결 → 요청 수량 그대로
    (10, None, 100, 0),   # 스냅샷 실패(또는 스냅샷에 없던 종목) → 폴백 없음
    (150, 100, 100, 100), # 요청 > 보유 → 현재 보유
    (150, 100, 60, 60),   # 요청 > 보유, 일부 체결 → 현재 보유
    (10, 100, 0, 0),      # 현재 보유 0(조회 실패 포함) → 폴백 없음
    (10, 100, 105, 10),   # 그사이 보유가 늘어도 요청 수량까지만
])
def test_fallback_qty(requested, before, now, expected):
    assert fallback_qty(requested, before, now) == expected


def test_main_uses_fallback_qty_and_snapshots_before_first_order():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    main = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "main")
    src = ast.get_source_segment(SRC.read_text(encoding="utf-8"), main)
    assert src.index("get_positions") < src.index("submit_order")   # 첫 주문 전에 스냅샷
    assert "fallback_qty(" in src
    assert "remaining.append((sym, pos.quantity))" not in src
