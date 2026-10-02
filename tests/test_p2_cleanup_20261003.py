"""48차 P2 정리 — 현금 미검증 시 포지션 대사 계속, 장부 영수증 확정 조회/재제출, 원장 전용 스레드, 저널 NaN 정리, health 노출.

전부 합성 입력. 네트워크·운영 캐시 무접촉.
"""
from __future__ import annotations

import asyncio
import json
import math
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.core.types import OrderSide  # noqa: E402
from src.data.storage.execution_journal import ExecutionWriteReceipt  # noqa: E402
from src.execution.execution_ledger import ExecutionLedger  # noqa: E402

from test_journal_commit_handoff import _unattributed, enqueue, identified, setup  # noqa: E402
from test_sync_portfolio_characterization import _make, _pos, _run  # noqa: E402


# ── (A) 현금 미검증이면 현금만 보류, 포지션 대사는 진행 ───────────────────────

def test_unverified_cash_keeps_cash_but_still_reconciles_positions(monkeypatch):
    sched, bot, sleeps = _make(
        monkeypatch, bot_positions=[_pos("005930"), _pos("000660")],
        balance={"stock_value": 105_000, "available_cash": 78_000, "available_cash_verified": False},
        kis_seq=[{"005930": _pos("005930")}, {"005930": _pos("005930")}], cash="123456",
    )
    statuses = []
    bot.risk_manager = SimpleNamespace(set_sync_status=lambda ok: statuses.append(ok))
    _run(sched)
    assert set(bot.engine.portfolio.positions) == {"005930"}        # 유령 000660 정리됨
    assert bot.engine.portfolio.cash == Decimal("123456")             # 현금은 보류
    assert statuses == [False]                                        # 매수 건강성은 실패로 센다
    from src.utils import loop_heartbeat as _hb
    assert _hb._state("kr_portfolio_sync").consecutive_failures >= 1  # 성공 기록으로 덮이지 않는다(정체 경보 유지)


# ── (C) 장부 영수증 확정 조회/재제출 ─────────────────────────────────────────

def _with_resolver(bot, sequence):
    calls = []

    async def resolve(scope, execution_id):
        calls.append(execution_id)
        bot.trade_journal.status = sequence.pop(0)
        return ExecutionWriteReceipt(bot.trade_journal.status, execution_id, "synthetic")
    bot.trade_journal.resolve_execution_receipt = resolve
    return calls


def test_unknown_receipt_is_resolved_from_db_before_unattributed(monkeypatch, tmp_path):
    sched, bot, receipts, acks, failures = setup(monkeypatch, tmp_path)
    bot.trade_journal.status = "unknown"
    calls = _with_resolver(bot, ["pending", "committed"])

    async def run():
        fill = identified()
        await enqueue(sched, bot, fill)
        await sched._drain_fill_handoffs(wait=False)   # unknown → 조회 → pending
        assert calls == [fill.execution_id] and not acks and sched._pending_fill_handoffs
        await sched._drain_fill_handoffs(wait=False)   # pending → 대기(재조회 없음)
        assert calls == [fill.execution_id] and not acks
        bot.trade_journal.status = "committed"           # DB writer 가 commit 을 확정한 뒤
        await sched._drain_fill_handoffs(wait=False)
        assert acks == [(fill.order_id, 3)] and receipts[-1] == (fill.execution_id, "handoff_returned")
        assert _unattributed(tmp_path) == [] and not failures
    asyncio.run(run())


def test_lookup_failed_during_db_outage_keeps_retrying_within_budget(monkeypatch, tmp_path):
    sched, bot, receipts, acks, failures = setup(monkeypatch, tmp_path)
    bot.trade_journal.status = "unknown"
    calls = []

    async def resolve(scope, execution_id):
        calls.append(execution_id)
        return ExecutionWriteReceipt("unknown", execution_id, "database_lookup_failed")
    bot.trade_journal.resolve_execution_receipt = resolve
    bot.broker.mark_unattributed_execution = lambda *a: None

    async def run():
        fill = identified()
        await enqueue(sched, bot, fill)
        await sched._drain_fill_handoffs(wait=False)
        assert calls == [fill.execution_id] and not acks and sched._pending_fill_handoffs  # 예산 안에서 대기
        await sched._drain_fill_handoffs(wait=False)
        await sched._drain_fill_handoffs(wait=False)
        assert len(calls) == sched.JOURNAL_RESOLVE_ATTEMPTS and acks and len(_unattributed(tmp_path)) == 1
    asyncio.run(run())


def test_unavailable_receipt_exhausts_bounded_retries_then_records_unattributed(monkeypatch, tmp_path):
    sched, bot, receipts, acks, failures = setup(monkeypatch, tmp_path)
    bot.trade_journal.status = "unavailable"
    calls = _with_resolver(bot, ["unavailable", "unavailable", "unavailable"])
    bot.broker.mark_unattributed_execution = lambda *a: None

    async def run():
        fill = identified()
        await enqueue(sched, bot, fill)
        for _ in range(3):
            await sched._drain_fill_handoffs(wait=False)
        assert len(calls) == sched.JOURNAL_RESOLVE_ATTEMPTS                # 유한 횟수
        assert acks == [(fill.order_id, 3)] and len(_unattributed(tmp_path)) == 1
    asyncio.run(run())


@pytest.mark.asyncio
async def test_storage_resolve_requeues_only_uncommitted_batches(tmp_path, monkeypatch):
    from src.data.storage import trade_storage as ts
    from src.data.storage.execution_journal import ExecutionBatch, ExecutionIdentity
    from datetime import date
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    storage = ts.TradeStorage.__new__(ts.TradeStorage)
    storage._execution_receipts, storage._execution_batches = {}, {}
    storage._closing, storage._db_available, storage.pool = False, True, object()
    storage._write_queue = asyncio.Queue()
    identity = ExecutionIdentity("scope", date(2026, 10, 2), "000123", "s:o:3")
    key = ts.execution_key("scope", "s:o:3")
    batch = SimpleNamespace(identity=identity, signature="sig")
    storage._execution_batches[key] = batch

    async def lookup(scope, execution_id):
        storage._execution_receipts[key] = ExecutionWriteReceipt("unknown", execution_id, "not_committed")
        return storage._execution_receipts[key]
    storage.lookup_execution_receipt = lookup

    storage._execution_receipts[key] = ExecutionWriteReceipt("unknown", "s:o:3", "commit_unconfirmed")
    out = await storage.resolve_execution_receipt("scope", "s:o:3")
    assert out.status == "pending" and out.reason == "requeued" and storage._write_queue.qsize() == 1

    storage._execution_receipts[key] = ExecutionWriteReceipt("failed", "s:o:3", "transaction_rejected")
    out = await storage.resolve_execution_receipt("scope", "s:o:3")
    assert out.status == "failed" and storage._write_queue.qsize() == 1          # 확정 거절은 재시도 안 함

    storage._execution_receipts[key] = ExecutionWriteReceipt("failed", "s:o:3", "predecessor_uncommitted")
    out = await storage.resolve_execution_receipt("scope", "s:o:3")
    assert out.status == "pending" and storage._write_queue.qsize() == 2          # 앞 batch 미확정 거절은 재큐

    storage._execution_receipts[key] = ExecutionWriteReceipt("committed", "s:o:3", "ok")
    assert (await storage.resolve_execution_receipt("scope", "s:o:3")).status == "committed"


# ── (B) 원장 전용 단일 스레드 ────────────────────────────────────────────────

def test_ledger_uses_dedicated_single_worker_executor(tmp_path):
    ledger = ExecutionLedger(tmp_path / "e.sqlite", "scope")
    assert ledger._executor._max_workers == 1 and ledger._executor._thread_name_prefix == "execution-ledger"
    asyncio.run(ledger.open("s1"))
    names = {t.name for t in ledger._executor._threads}
    assert names and all(n.startswith("execution-ledger") for n in names)


# ── (F) 저널 NaN 정리 ────────────────────────────────────────────────────────

def test_journal_sanitizes_non_finite_metadata_so_daily_file_still_saves(tmp_path, monkeypatch):
    from src.core.evolution.trade_journal import TradeJournal
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    journal = TradeJournal(storage_dir=tmp_path / "journal")
    rec = journal.record_entry(trade_id="t-nan", symbol="005930", name="삼성", entry_price=70000.0, entry_quantity=1,
                               entry_reason="t", entry_strategy="sepa_trend", signal_score=80.0,
                               indicators={"rsi": float("nan"), "atr": 1.5, "nested": {"x": float("inf")}},
                               market_context={"vol": float("-inf")}, market="KR")
    assert rec.indicators_at_entry == {"rsi": None, "atr": 1.5, "nested": {"x": None}}
    files = list((tmp_path / "journal").glob("trades_*.json"))
    assert len(files) == 1
    saved = json.loads(files[0].read_text())
    assert saved["trades"][0]["market_context"] == {"vol": None}
    assert not any(isinstance(v, float) and not math.isfinite(v) for v in saved["trades"][0]["indicators_at_entry"].values())


# ── (D) health 노출 ──────────────────────────────────────────────────────────

def test_health_exposes_execution_recovery_subset():
    from src.dashboard import data_collector as dc_mod
    dc = dc_mod.DashboardDataCollector.__new__(dc_mod.DashboardDataCollector)
    bot = SimpleNamespace(
        broker=SimpleNamespace(is_connected=True, _pending_orders={}, execution_recovery_status=lambda: {
            'status': 'journal_unattributed', 'reason': 'r', 'journal_pending_count': 0,
            'unattributed_symbols': ['005930'], 'prior_unclean': False, 'orders': {'secret': 1}}),
        engine=SimpleNamespace(risk_manager=None, strategies={}, _watch_symbols=[]),
        strategy_manager=None, stock_name_cache={}, _watch_symbols=[],
    )
    dc.bot = bot
    out = dc.get_system_health()
    assert out["execution_recovery"] == {'status': 'journal_unattributed', 'reason': 'r', 'journal_pending_count': 0,
                                         'unattributed_symbols': ['005930'], 'prior_unclean': False}
    assert 'orders' not in out["execution_recovery"]
