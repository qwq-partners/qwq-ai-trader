"""재시작 조회 결과를 성공으로 추정하거나 계좌 체결을 봇 거래로 만들지 않는다."""
import asyncio
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.data.storage import trade_storage as module


def fill(symbol="005930", side="02", odno="synthetic-1", **patch):
    return dict(symbol=symbol, name="합성", sll_buy_dvsn_cd=side,
                odno=odno, tot_ccld_qty=3, avg_prvs=11000.0,
                ord_tmd="090100", **patch)


class Broker:
    def __init__(self, rows, complete=True, reason=None, error=None):
        self.rows, self.complete, self.reason, self.error = rows, complete, reason, error

    async def get_fills_for_date_checked(self, day):
        assert day == date.today()
        if self.error:
            raise self.error
        return deepcopy(self.rows), self.complete, self.reason


class Pool:
    def __init__(self, rows=(), error=None):
        self.rows, self.error = list(rows), error
        self.writes = []

    async def fetch(self, sql, *params):
        if self.error:
            raise self.error
        return deepcopy(self.rows)

    async def execute(self, sql, *params):
        self.writes.append((sql, params))


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("TRADE_JOURNAL_DIR", str(tmp_path / "journal"))
    storage = module.TradeStorage(db_url="synthetic-unused")
    storage.pool = Pool()
    storage._db_available = True
    storage._write_queue = asyncio.Queue()
    engine = SimpleNamespace(portfolio=SimpleNamespace(
        positions={"005930": SimpleNamespace(strategy="sepa_trend", quantity=10)},
        cash=Decimal("100000"), daily_pnl=Decimal("123")))
    return SimpleNamespace(storage=storage, engine=engine)


def run_untouched(case, broker):
    before_trades = deepcopy(case.storage._trades)
    before_engine = deepcopy(case.engine)
    before_files = {p: p.read_bytes() for p in case.storage._journal.storage_dir.glob("*.json")}
    before_queue = case.storage._write_queue.qsize()
    result = asyncio.run(case.storage.sync_from_kis(broker, case.engine))
    assert case.storage._trades == before_trades
    assert case.engine == before_engine
    assert {p: p.read_bytes() for p in case.storage._journal.storage_dir.glob("*.json")} == before_files
    assert case.storage._write_queue.qsize() == before_queue
    assert case.storage.pool.writes == []
    assert type(result.complete) is bool
    assert result.recovered_count == 0
    return result


@pytest.mark.parametrize("complete", [False, None, 1, "true"])
def test_incomplete_query_returns_noncomplete_without_mutation(case, complete):
    result = run_untouched(case, Broker([fill()], complete, "partial_page"))
    assert result.status == "incomplete"
    assert result.complete is False
    assert "partial_page" in result.reason


def test_verified_empty_result_is_complete(case):
    result = run_untouched(case, Broker([]))
    assert result.status == "verified_empty"
    assert result.complete is True


def test_unsupported_adapter_is_not_success(case):
    result = run_untouched(case, SimpleNamespace())
    assert result.status == "unsupported"
    assert result.complete is False


def test_query_exception_returns_error(case):
    result = run_untouched(case, Broker([], error=RuntimeError("synthetic-query")))
    assert result.status == "error"
    assert result.complete is False


@pytest.mark.parametrize("side", ["01", "02"])
def test_two_orders_same_symbol_preflight_all_rows_before_writing(case, side):
    rows = [fill(symbol="000660"), fill(side=side), fill(side=side, odno="synthetic-2")]
    result = run_untouched(case, Broker(rows))
    assert result.status == "ambiguous"
    assert result.complete is False


def test_unknown_buy_never_infers_strategy_from_position(case):
    result = run_untouched(case, Broker([fill()]))
    assert result.status == "ambiguous"
    assert result.complete is False


def test_database_failure_cannot_fall_back_to_inferred_recovery(case):
    case.storage.pool.error = RuntimeError("synthetic-db-lookup")
    result = run_untouched(case, Broker([fill()]))
    assert result.status == "error"
    assert result.complete is False


def test_retry_unknown_order_does_not_create_first_or_duplicate_write(case):
    for _ in range(2):
        assert run_untouched(case, Broker([fill()])).status == "ambiguous"


def event(symbol="005930", side="BUY", odno="synthetic-1", **patch):
    row = dict(trade_id="synthetic-entry", symbol=symbol, event_type=side,
               kis_order_no=odno, quantity=3, price=Decimal("11000.00"))
    row.update(patch)
    return row


def test_exact_persisted_identity_comparison_is_complete_without_replay(case):
    case.storage.pool.rows = [event()]
    for _ in range(2):
        result = run_untouched(case, Broker([fill()]))
        assert result.status == "reconciled"
        assert result.complete is True


def test_buy_sell_comparison_requires_same_trade_identity(case):
    case.storage.pool.rows = [event(), event(side="SELL", odno="sell-1", trade_id="other")]
    result = run_untouched(case, Broker([fill(), fill(side="01", odno="sell-1")]))
    assert result.status == "ambiguous"


@pytest.mark.parametrize("patch", [
    {"kis_order_no": None}, {"kis_order_no": "other"}, {"symbol": "000660"},
    {"event_type": "SELL"}, {"trade_id": ""},
])
def test_missing_or_conflicting_persisted_identity_is_ambiguous(case, patch):
    case.storage.pool.rows = [event(**patch)]
    result = run_untouched(case, Broker([fill()]))
    assert result.status == "ambiguous"
    assert result.complete is False


@pytest.mark.parametrize("patch", [{"quantity": 2}, {"price": Decimal("10000")}])
def test_persisted_quantity_or_price_mismatch_is_incomplete(case, patch):
    case.storage.pool.rows = [event(**patch)]
    result = run_untouched(case, Broker([fill()]))
    assert result.status == "incomplete"
    assert result.complete is False


def test_extra_database_event_is_not_silently_ignored(case):
    case.storage.pool.rows = [event(), event(symbol="000660", odno="other")]
    result = run_untouched(case, Broker([fill()]))
    assert result.status == "incomplete"


@pytest.mark.parametrize("rows", [None, {}, "", [None], [{}], [fill(odno="")],
                                    [fill(), fill()]])
def test_malformed_or_duplicate_query_is_not_verified_empty_or_reconciled(case, rows):
    result = run_untouched(case, Broker(rows))
    assert result.complete is False
    assert result.status in {"incomplete", "ambiguous"}


def test_no_database_cannot_prove_durable_identity(case):
    case.storage._db_available = False
    result = run_untouched(case, Broker([fill()]))
    assert result.status == "unsupported"


def test_pending_write_is_not_a_persistence_receipt(case):
    case.storage.pool.rows = [event()]
    case.storage._write_queue.put_nowait(("synthetic", (), 0))
    result = run_untouched(case, Broker([fill()]))
    assert result.status == "incomplete"


@pytest.mark.parametrize("status", ["incomplete", "unsupported", "ambiguous", "error", "partial", "unknown"])
def test_noncomplete_result_does_not_become_true_with_recovered_count(status):
    result_type = getattr(module, "KISSyncResult", None)
    assert result_type is not None
    result = result_type(status, "synthetic", recovered_count=1)
    assert result.complete is False


@pytest.mark.parametrize("field,value", [
    ("tot_ccld_qty", 0), ("tot_ccld_qty", -1), ("tot_ccld_qty", 1.5),
    ("tot_ccld_qty", True), ("tot_ccld_qty", "NaN"),
    ("avg_prvs", 0), ("avg_prvs", "NaN"), ("avg_prvs", "Infinity"),
])
def test_invalid_numeric_fills_never_reconcile_or_mutate(case, field, value):
    row = fill()
    row[field] = value
    result = run_untouched(case, Broker([row]))
    assert result.status == "incomplete"


def test_valid_first_match_then_bad_database_row_does_not_partially_replay(case):
    case.storage.pool.rows = [event(), {"symbol": "000660"}]
    result = run_untouched(case, Broker([fill()]))
    assert result.status == "error"
    assert result.complete is False


def test_duplicate_database_events_are_not_summed_into_success(case):
    case.storage.pool.rows = [event(), event()]
    result = run_untouched(case, Broker([fill()]))
    assert result.status == "ambiguous"


def test_same_trade_buy_and_sell_explicit_identities_can_be_compared(case):
    case.storage.pool.rows = [event(), event(side="SELL", odno="sell-1")]
    result = run_untouched(case, Broker([fill(), fill(side="01", odno="sell-1")]))
    assert result.status == "reconciled"


def test_inflight_queue_item_does_not_become_success_when_queue_is_empty(case):
    case.storage.pool.rows = [event()]
    case.storage._write_queue.put_nowait(("synthetic", (), 0))
    case.storage._write_queue.get_nowait()
    assert case.storage._write_queue.empty()
    result = run_untouched(case, Broker([fill()]))
    assert result.status == "incomplete"


def test_database_query_scopes_all_events_to_kr_and_checked_date(case):
    class ScopedPool(Pool):
        async def fetch(self, sql, *params):
            assert params == (date.today(),)
            assert "JOIN trades t ON t.id = e.trade_id" in sql
            assert "e.event_time::date = $1" in sql
            assert "t.market = 'KR'" in sql
            assert "e.event_type IN ('BUY', 'SELL')" in sql
            return [event()]

    case.storage.pool = ScopedPool()
    assert run_untouched(case, Broker([fill()])).complete is True
