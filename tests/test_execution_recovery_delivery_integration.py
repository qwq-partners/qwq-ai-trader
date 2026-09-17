"""실큐 보호 복구와 DB 원장 ACK의 교차 인수. 외부 SQL 경계만 fake."""
import asyncio
from decimal import Decimal

from src.data.storage.trade_storage import TradeStorage
from src.execution.safety.journal_delivery import OutboxDispatcher
from test_execution_journal_delivery import Pool
from test_execution_protection_recovery import failed_entry
from test_execution_runtime import observed, queued


def test_durable_journal_ack_then_repair_then_more_fills_keeps_economics_once(tmp_path, monkeypatch):
    async def scenario():
        engine, exits, store, runtime, ref, first = await failed_entry(tmp_path, monkeypatch)
        try:
            # 운영 URL/기존 JSON 생성자 없이 실제 TradeStorage의 새 DB 경계를 사용한다.
            storage = TradeStorage.__new__(TradeStorage)
            storage.pool, storage._db_available = Pool(), True
            dispatcher = OutboxDispatcher(runtime.owner, storage.execution_journal())
            before = runtime.owner.state
            assert (await dispatcher.drain()).delivered == 1
            assert not runtime.owner.state["cursors"][first.order_key]["journal_pending"]
            assert (await runtime.repair_protection(
                "after-journal", "005930", expected_version=runtime.owner.version,
            )).status == "APPLIED"
            assert exits.get_state("005930").remaining_quantity == 40
            assert runtime.owner.state["portfolio"] == before["portfolio"]
            assert runtime.owner.state["risk"] == before["risk"]
            assert runtime.owner.state["lots"] == before["lots"]

            latest = await observed(runtime, ref, 100, "1000000")
            receipt = await queued(engine, latest)
            assert (receipt.status, receipt.protection_status) == ("APPLIED", "ready")
            assert receipt.journal_pending
            assert engine.portfolio.cash == Decimal("999859")
            assert exits.get_state("005930").remaining_quantity == 100
            assert (await dispatcher.drain()).delivered == 1
            duplicate = await queued(engine, latest)
            assert duplicate.status == "ALREADY_APPLIED" and not duplicate.journal_pending
            assert storage.pool.insertions == 2
            assert runtime.health()["outbox_pending"] == 0
            assert engine.portfolio.daily_trades == 1
            assert runtime.owner.state["lots"][first.order_key]["initial_r_status"] == "pending"
            assert not runtime.trading_ready
            await runtime.restore()
            assert engine.portfolio.cash == Decimal("999859")
            assert (await dispatcher.drain()).delivered == 0
        finally:
            await store.close()
    asyncio.run(scenario())
