"""조회 완결 전에는 실제 저널·JSON·DB 큐를 수정하지 않는 기동 대사 회귀."""
import asyncio
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from loguru import logger

from src.data.storage.trade_storage import TradeStorage


def fill(side="02"):
    return {
        "symbol": "005930", "name": "합성 종목", "sll_buy_dvsn_cd": side,
        "tot_ccld_qty": 3, "avg_prvs": 11000.0, "odno": "synthetic-1",
        "ord_tmd": "",
    }


class Broker:
    def __init__(self, rows, complete=True, reason=None, error=None):
        self.rows = rows
        self.complete = complete
        self.reason = reason
        self.error = error
        self.checked_dates = []
        self.unchecked_dates = []

    async def get_fills_for_date_checked(self, day):
        self.checked_dates.append(day)
        if self.error:
            raise self.error
        return deepcopy(self.rows), self.complete, self.reason

    async def get_all_fills_for_date(self, day):
        self.unchecked_dates.append(day)
        return deepcopy(self.rows)


class Pool:
    def __init__(self):
        self.reads = []
        self.writes = []

    async def fetch(self, sql, *args):
        self.reads.append((sql, args))
        return []

    async def execute(self, sql, *args):
        self.writes.append((sql, args))


@pytest.fixture
def case(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("TRADE_JOURNAL_DIR", str(tmp_path / "journal"))
    storage = TradeStorage(db_url="synthetic-unused")
    storage.pool = Pool()
    storage._db_available = True
    storage._write_queue = asyncio.Queue()
    engine = SimpleNamespace(portfolio=SimpleNamespace(
        positions={"005930": SimpleNamespace(strategy="sepa_trend", quantity=10)},
        daily_pnl=Decimal("123"), cash=Decimal("100000"),
    ))
    messages = []
    sink = logger.add(lambda message: messages.append(message.record["message"]))
    yield SimpleNamespace(storage=storage, engine=engine, messages=messages,
                          engine_before=deepcopy(engine))
    logger.remove(sink)


def assert_untouched(case):
    assert case.storage._trades == {}
    assert list(case.storage._journal.storage_dir.glob("*.json")) == []
    assert case.storage._write_queue.empty()
    assert case.storage.pool.reads == []
    assert case.storage.pool.writes == []
    assert case.engine == case.engine_before


@pytest.mark.parametrize("complete", [False, None, 1, "true"])
def test_partial_nonempty_or_unknown_completeness_never_writes(case, complete):
    broker = Broker([fill()], complete=complete, reason="partial_page")
    asyncio.run(case.storage.sync_from_kis(broker, case.engine))
    assert_untouched(case)
    assert broker.checked_dates == [date.today()]
    assert broker.unchecked_dates == []
    assert any("partial_page" in message for message in case.messages)
    assert not any("체결 0건" in message for message in case.messages)


def test_incomplete_empty_does_not_claim_verified_zero(case):
    broker = Broker([], complete=False, reason="not_connected")
    asyncio.run(case.storage.sync_from_kis(broker, case.engine))
    assert_untouched(case)
    assert broker.checked_dates == [date.today()]
    assert broker.unchecked_dates == []
    assert any("not_connected" in message for message in case.messages)
    assert not any("체결 0건" in message for message in case.messages)


def test_checked_query_exception_never_falls_back_or_writes(case):
    broker = Broker([fill()], error=RuntimeError("synthetic-query-failure"))
    asyncio.run(case.storage.sync_from_kis(broker, case.engine))
    assert_untouched(case)
    assert broker.checked_dates == [date.today()]
    assert broker.unchecked_dates == []
    assert any("synthetic-query-failure" in message for message in case.messages)
    assert not any("체결 0건" in message for message in case.messages)


@pytest.mark.parametrize("noncallable", [False, True])
def test_legacy_only_adapter_is_skipped_without_unchecked_query(case, noncallable):
    source = Broker([fill()])
    broker = SimpleNamespace(get_all_fills_for_date=source.get_all_fills_for_date)
    if noncallable:
        broker.get_fills_for_date_checked = None
    asyncio.run(case.storage.sync_from_kis(broker, case.engine))
    assert_untouched(case)
    assert source.unchecked_dates == []
    assert any("get_fills_for_date_checked" in message for message in case.messages)


def test_complete_empty_is_normal_zero_without_writes(case):
    broker = Broker([])
    asyncio.run(case.storage.sync_from_kis(broker, case.engine))
    assert_untouched(case)
    assert broker.checked_dates == [date.today()]
    assert broker.unchecked_dates == []
    assert any("체결 0건" in message for message in case.messages)


def test_complete_buy_preserves_real_cache_json_and_db_queue_pipeline(case):
    broker = Broker([fill()])
    asyncio.run(case.storage.sync_from_kis(broker, case.engine))
    trade = case.storage.get_today_trades()[0]
    assert (trade.symbol, trade.entry_quantity, trade.entry_price) == (
        "005930", 3, Decimal("11000.0"))
    assert trade.entry_strategy == "sepa_trend"
    assert len(list(case.storage._journal.storage_dir.glob("*.json"))) == 1
    assert case.storage._write_queue.qsize() == 2
    assert case.storage.pool.reads
    assert case.engine == case.engine_before
    assert broker.checked_dates == [date.today()]
    assert broker.unchecked_dates == []


def test_complete_sell_preserves_existing_recovery_pipeline(case):
    trade = case.storage.record_entry(
        trade_id="synthetic-entry", symbol="005930", name="합성 종목",
        entry_price=10000, entry_quantity=10, entry_reason="합성 진입",
        entry_strategy="sepa_trend",
    )
    case.storage._write_queue = asyncio.Queue()
    broker = Broker([fill("01")])
    asyncio.run(case.storage.sync_from_kis(broker, case.engine))
    assert trade.exit_quantity == 3
    assert trade.exit_price == Decimal("11000.0")
    assert trade.pnl > 0
    assert case.storage._write_queue.qsize() == 3
    assert case.engine == case.engine_before
    assert broker.checked_dates == [date.today()]
    assert broker.unchecked_dates == []
