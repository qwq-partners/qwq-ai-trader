"""2026-10-02 리뷰 P1-1·P1-2 — 실행 원장 보류의 명시 해제와 원장 open 실패 시 보호 전량 SELL 허용.

P1-1: 비정상 종료 세션은 다음 세션에 `prior_unclean` 으로 영구 상속되고 그 세션은 clean close 할 수 없었다.
      운영자 `acknowledge` 이벤트(봇 정지 중 CLI)만 과거 비정상 종료·미완결 주문을 보류 사유에서 뺀다.
P1-2: 원장 open 실패(`session_recorded=False`)가 손절 전량 SELL 까지 막았다. 이제 BUY·분할 SELL 만 막는다.
전부 합성 입력 — tmp_path 원장, Path.home 패치, 가짜 HTTP 세션. 네트워크·운영 캐시 무접촉.
"""
from __future__ import annotations

import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.core.types import Order, OrderSide, OrderType  # noqa: E402
from src.execution.execution_history import ExecutionHistory, execution_ledger_location  # noqa: E402
from src.execution.execution_ledger import ExecutionLedger, ExecutionLedgerError  # noqa: E402
from scripts.ops import acknowledge_execution_ledger as cli  # noqa: E402

from test_execution_ledger import FACTS, accepted  # noqa: E402
from test_order_post_unknown import OK_BODY, _order, _submit, ub  # noqa: E402,F401
from test_risk_sizing import home  # noqa: E402,F401

SCOPE = "scope-test"


async def _unclean_history(path):
    """s1: 접수됐지만 종료 수량 미확정인 주문을 남기고 close 없이 끝난다(비정상 종료)."""
    ledger = ExecutionLedger(path, SCOPE)
    await ledger.open("s1")
    await accepted(ledger, "s1:order-1")
    assert await ledger.close_session() is False  # 미완결 주문 → clean 불가


# ── P1-1 원장 ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unclean_session_is_inherited_until_acknowledged(tmp_path):
    path = tmp_path / "executions.sqlite"
    await _unclean_history(path)

    s2 = ExecutionLedger(path, SCOPE)
    assert (await s2.open("s2"))["prior_unclean"] is True
    assert await s2.close_session() is False  # 상속된 비정상은 스스로 풀리지 않는다

    s3 = ExecutionLedger(path, SCOPE)
    assert (await s3.open("s3"))["prior_unclean"] is True  # 재시작을 거듭해도 영구

    ack = ExecutionLedger(path, SCOPE)
    assert (await ack.open("ack-1"))["prior_unclean"] is True
    await ack.acknowledge("HTS 10/02 15:40 확인, reconcile 보고서 abc123")
    after = await ack.snapshot()
    assert after["prior_unclean"] is False
    assert after["orders"]["s1:order-1"]["acknowledged"] is True
    assert after["orders"]["s1:order-1"]["status"] == "accepted"  # 과거 사실은 불변
    assert await ack.close_session() is True

    s4 = ExecutionLedger(path, SCOPE)
    snap = await s4.open("s4")
    assert snap["prior_unclean"] is False
    assert await s4.close_session() is True


@pytest.mark.asyncio
async def test_acknowledge_requires_note_and_does_not_touch_current_session_orders(tmp_path):
    path = tmp_path / "executions.sqlite"
    ledger = ExecutionLedger(path, SCOPE)
    await ledger.open("s1")
    key = await accepted(ledger, "s1:order-1")
    with pytest.raises(ExecutionLedgerError):
        await ledger.acknowledge("   ")
    await ledger.acknowledge("현재 실행 중 호출")
    order = (await ledger.snapshot())["orders"][key]
    assert "acknowledged" not in order  # 현재 실행 소유 주문은 대상이 아니다
    assert await ledger.close_session() is False


@pytest.mark.asyncio
async def test_acknowledge_event_replays_identically_on_full_reload(tmp_path):
    path = tmp_path / "executions.sqlite"
    await _unclean_history(path)
    ack = ExecutionLedger(path, SCOPE)
    await ack.open("ack-1")
    await ack.acknowledge("대사 완료")
    await ack.close_session()
    # 새 인스턴스의 open 은 전체 이벤트를 재생해 projection 과 대조한다(불일치면 _CorruptLedger)
    snap = await ExecutionLedger(path, SCOPE).open("s5")
    assert snap["prior_unclean"] is False and snap["orders"]["s1:order-1"]["acknowledged"] is True


@pytest.mark.asyncio
async def test_history_hold_clears_only_after_acknowledge(tmp_path):
    path = tmp_path / "executions.sqlite"
    await _unclean_history(path)

    blocked = ExecutionHistory(path, SCOPE)
    await blocked.open()
    assert blocked.hold() == "이전 실행 정상 종료 미확인: 과거 체결 자동 재생 금지"
    assert blocked.report()["status"] == "recovery_required"
    assert await blocked.close() is False

    ack = ExecutionLedger(path, SCOPE)
    await ack.open("ack-1")
    await ack.acknowledge("대사 완료")
    assert await ack.close_session() is True

    fresh = ExecutionHistory(path, SCOPE)
    await fresh.open()
    assert fresh.hold() is None and fresh.hold("005930") is None
    assert fresh.report()["status"] == "ready"


# ── P1-1 CLI ─────────────────────────────────────────────────────────────────

def test_cli_acknowledges_with_explicit_ledger_and_refuses_when_bot_holds_lock(tmp_path, home, capsys, monkeypatch):
    from src.utils import trader_lock
    monkeypatch.setattr(trader_lock, "_held", None)
    path = tmp_path / "executions.sqlite"
    asyncio.run(_unclean_history(path))
    try:
        assert cli.main(["--note", "HTS 확인", "--ledger", str(path), "--account-scope", SCOPE]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["before"] == {"prior_unclean": True, "unclean_sessions": 1, "unresolved_orders": 1}
        assert out["after"] == {"prior_unclean": False, "unclean_sessions": 0, "unresolved_orders": 0}
        assert out["ack_session_closed"] is True
        # 이 프로세스가 락을 쥔 채라 두 번째 호출은 봇 실행 중과 같은 거부(exit 2)다
        with pytest.raises(SystemExit) as exc:
            cli.main(["--note", "다시", "--ledger", str(path), "--account-scope", SCOPE])
        assert exc.value.code == 2
    finally:
        if trader_lock._held is not None:
            trader_lock._held.close()  # tmp 락 fd 를 다른 테스트에 남기지 않는다


def test_cli_rejects_half_specified_location_and_missing_ledger(tmp_path, home, capsys, monkeypatch):
    from src.utils import trader_lock
    monkeypatch.setattr(trader_lock, "_held", None)
    monkeypatch.setattr(cli, "hold_or_exit", lambda tool: None)
    with pytest.raises(SystemExit):
        cli.main(["--note", "x", "--ledger", str(tmp_path / "e.sqlite")])
    assert cli.main(["--note", "x", "--ledger", str(tmp_path / "none.sqlite"), "--account-scope", SCOPE]) == 2
    # 손상/범위 불일치: 다른 scope 로 열면 _CorruptLedger → 2, 경로는 메시지에 없다
    asyncio.run(_unclean_history(tmp_path / "e.sqlite"))
    assert cli.main(["--note", "x", "--ledger", str(tmp_path / "e.sqlite"), "--account-scope", "other"]) == 2
    assert str(tmp_path) not in capsys.readouterr().err


def test_ledger_location_matches_broker_formula(tmp_path):
    path, scope = execution_ledger_location("prod", "12345678", "01", base_dir=tmp_path)
    assert path == tmp_path / f"executions-{scope}.sqlite3" and len(scope) == 64
    assert "12345678" not in path.name


# ── P1-2 브로커 ──────────────────────────────────────────────────────────────

def _failed_open_history(tmp_path, monkeypatch):
    history = ExecutionHistory(tmp_path / "executions.sqlite", SCOPE)

    async def _boom(session_id):
        raise ExecutionLedgerError("디스크/잠금 실패")
    monkeypatch.setattr(history.ledger, "open", _boom)
    return history


def test_ledger_open_failure_blocks_buy_and_partial_sell_only(ub, tmp_path, monkeypatch):
    ub._execution_history = _failed_open_history(tmp_path, monkeypatch)

    ok, msg = _submit(ub, _order(OrderSide.BUY))
    assert ok is False and "실행 시작 기록 미확인" in msg
    assert ub._session.sent == []

    partial = _order(OrderSide.SELL, qty=3)
    partial.partial_exit = True
    ok, msg = _submit(ub, partial)
    assert ok is False and "실행 시작 기록 미확인" in msg
    assert ub._session.sent == []

    full = _order(OrderSide.SELL, qty=10)  # 손절·트레일링 전량
    ok, order_no = _submit(ub, full)
    assert ok is True and order_no == "0001"
    assert len(ub._session.sent) == 1 and ub._session.sent[0][1]["ORD_QTY"] == "10"
    assert ub._execution_history.fault == "open_failed"
    assert ub._execution_history.session_recorded is False  # 이 세션은 clean close 불가로 남는다


def test_post_guard_without_cancel_guard_still_blocks_when_unrecorded(ub, tmp_path, monkeypatch):
    """cancel_guard 없이 order-cash 를 치는 경로가 생겨도 기록 없는 전송은 기본 차단이다."""
    ub._execution_history = _failed_open_history(tmp_path, monkeypatch)
    asyncio.run(ub._execution_history.open())
    out = asyncio.run(ub._api_post("http://x/uapi/domestic-stock/v1/trading/order-cash", "TTTC0801U", {}, retry=False))
    assert out.get("_blocked") is True and ub._session.sent == []


# ── P1-3 브로커: 귀속 미확정 종목 한정 보류 ─────────────────────────────────

def test_unattributed_symbol_blocks_buy_and_partial_sell_for_that_symbol_only(ub):
    ub.mark_unattributed_execution(SYM_A := "005930", "장부 DB commit 미확정")
    status = ub.execution_recovery_status()
    assert status["unattributed_symbols"] == [SYM_A]

    ok, msg = _submit(ub, _order(OrderSide.BUY, symbol=SYM_A))
    assert ok is False and "귀속 미확정" in msg and ub._session.sent == []

    partial = _order(OrderSide.SELL, symbol=SYM_A, qty=3)
    partial.partial_exit = True
    ok, msg = _submit(ub, partial)
    assert ok is False and ub._session.sent == []

    assert _submit(ub, _order(OrderSide.SELL, symbol=SYM_A, qty=10))[0] is True  # 전량 보호 SELL
    assert _submit(ub, _order(OrderSide.BUY, symbol="000660"))[0] is True       # 다른 종목 BUY
    assert len(ub._session.sent) == 2
