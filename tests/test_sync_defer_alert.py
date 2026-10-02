"""48차 P1-4 — 잔고 동기화 보류가 시한을 넘기면 실패로 승격하고 한 번 경보한다.

종전에는 실패로 표시되지 않은 보류(해소 안 되는 취소 관측·적용 전 예외 등)가 `record_idle` 만 남겨
정체 경보도 매수 차단도 작동하지 않았다. 합성 입력·시계 동결, 네트워크 무접촉.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.schedulers import kr_scheduler  # noqa: E402
from src.utils import loop_heartbeat as _hb  # noqa: E402
from test_fill_reconciliation import case  # noqa: E402
from test_t11_entry_plan import _freeze_clock  # noqa: E402

T0 = datetime(2026, 10, 6, 10, 0, 0)


async def _drain():
    """fire-and-forget 경보 태스크를 실행한다 — 픽스처가 asyncio.sleep 을 전역 패치하므로 sleep(0) 은 양보하지 않는다."""
    others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if others:
        await asyncio.gather(*others)


def _wire(monkeypatch, tmp_path):
    sched, bot = case(monkeypatch, tmp_path)
    statuses, alerts = [], []
    bot.risk_manager = type("R", (), {"set_sync_status": lambda self, ok: statuses.append(ok)})()

    async def alert(text, **_k):
        alerts.append(text)
    monkeypatch.setattr(kr_scheduler, "send_alert", alert)
    return sched, statuses, alerts


def test_short_deferral_stays_idle_without_block(monkeypatch, tmp_path):
    sched, statuses, alerts = _wire(monkeypatch, tmp_path)
    _freeze_clock(monkeypatch, kr_scheduler, T0)

    async def run():
        sched._defer_portfolio_sync("로컬 주문/체결 처리 중")
        _freeze_clock(monkeypatch, kr_scheduler, T0 + timedelta(minutes=14))
        sched._defer_portfolio_sync("로컬 주문/체결 처리 중")
        await _drain()
    asyncio.run(run())
    assert statuses == [] and alerts == []
    assert _hb._state("kr_portfolio_sync").idle_reason == "로컬 주문/체결 처리 중"


def test_long_deferral_escalates_once_and_resets_on_success(monkeypatch, tmp_path):
    sched, statuses, alerts = _wire(monkeypatch, tmp_path)
    _freeze_clock(monkeypatch, kr_scheduler, T0)

    async def run():
        sched._defer_portfolio_sync("취소 주문 최종 체결 미확인")
        _freeze_clock(monkeypatch, kr_scheduler, T0 + timedelta(minutes=15))
        sched._defer_portfolio_sync("취소 주문 최종 체결 미확인")
        await _drain()
        _freeze_clock(monkeypatch, kr_scheduler, T0 + timedelta(minutes=16))
        sched._defer_portfolio_sync("취소 주문 최종 체결 미확인")
        await _drain()
        assert statuses == [False, False]
        assert len(alerts) == 1 and "잔고 동기화 정지" in alerts[0] and "15분 초과" in alerts[0]
        assert _hb._state("kr_portfolio_sync").consecutive_failures >= 2
        # 성공 경로가 하는 초기화 뒤에는 새 보류가 다시 0부터 센다
        sched._portfolio_sync_deferred_since = None
        sched._portfolio_sync_defer_alerted = False
        _freeze_clock(monkeypatch, kr_scheduler, T0 + timedelta(minutes=30))
        sched._defer_portfolio_sync("로컬 주문/체결 처리 중")
        await _drain()
        assert statuses == [False, False] and len(alerts) == 1
    asyncio.run(run())


def test_failed_handoff_deferral_alerts_immediately(monkeypatch, tmp_path):
    sched, statuses, alerts = _wire(monkeypatch, tmp_path)
    _freeze_clock(monkeypatch, kr_scheduler, T0)
    sched._pending_fill_handoffs = {"e1": {"failed": True}}

    async def run():
        sched._defer_portfolio_sync("체결 적용 확인 불가")
        await _drain()
    asyncio.run(run())
    assert statuses == [False] and len(alerts) == 1 and "후처리 실패" in alerts[0]


def test_successful_sync_resets_deferral_clock_and_alert_flag(monkeypatch, tmp_path):
    """실제 `_sync_portfolio` 성공 경로가 보류 시작 시각·경보 플래그를 초기화한다(테스트의 수동 초기화 아님)."""
    from test_sync_portfolio_characterization import _make, _SYNC_PARAMS
    sched, bot, _ = _make(monkeypatch, bot_positions=[], balance={"stock_value": 0, "available_cash": 100000},
                         kis_seq=[{}], exit_params={"_sync": dict(_SYNC_PARAMS)})
    sched._portfolio_sync_deferred_since = T0
    sched._portfolio_sync_defer_alerted = True
    asyncio.run(sched._sync_portfolio())
    assert sched._portfolio_sync_deferred_since is None
    assert sched._portfolio_sync_defer_alerted is False

