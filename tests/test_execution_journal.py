"""식별 체결의 JSON 멱등성·증분·실패 경계."""
from copy import deepcopy
from datetime import date, datetime, timezone

import pytest

from src.core.evolution.trade_journal import TradeJournal
from src.data.storage.execution_journal import ExecutionIdentity


def ident(n="e1"):
    return ExecutionIdentity("synthetic-scope", date.today(), "000123", n)


def buy(journal, n="e1", **overrides):
    values = dict(trade_id="t1", symbol="005930", name="합성", entry_price=100,
                  entry_quantity=2, entry_reason="entry", entry_strategy="gap",
                  execution_identity=ident(n))
    values.update(overrides)
    return journal.record_entry(**values)


def sell(journal, n="s1", **overrides):
    values = dict(trade_id="t1", exit_price=120, exit_quantity=1, exit_reason="exit",
                  exit_type="take_profit", avg_entry_price=100, execution_identity=ident(n))
    values.update(overrides)
    return journal.record_exit(**values)


def test_buy_deltas_accumulate_and_duplicate_survives_json_restart(tmp_path):
    journal = TradeJournal(str(tmp_path))
    buy(journal)
    buy(journal, "e2", entry_price=130, entry_quantity=1)
    assert journal.get_trade("t1").entry_quantity == 3
    assert journal.get_trade("t1").entry_price == 110
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    buy(journal)
    reopened = TradeJournal(str(tmp_path))
    buy(reopened, "e2", entry_price=130, entry_quantity=1)
    assert reopened.get_trade("t1").entry_quantity == 3
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_sell_retry_does_not_increment_quantity_or_pnl(tmp_path):
    journal = TradeJournal(str(tmp_path))
    buy(journal)
    sell(journal)
    before = deepcopy(journal.get_trade("t1").to_dict())
    sell(journal)
    assert journal.get_trade("t1").to_dict() == before
    reopened = TradeJournal(str(tmp_path))
    sell(reopened)
    assert reopened.get_trade("t1").to_dict() == before


@pytest.mark.parametrize("patch", [{"entry_quantity": 3}, {"entry_price": 101},
                                    {"symbol": "000660"}, {"trade_id": "other"}])
def test_same_execution_conflict_rejected_before_memory_change(tmp_path, patch):
    journal = TradeJournal(str(tmp_path))
    buy(journal)
    before = deepcopy(journal.get_trade("t1").to_dict())
    with pytest.raises(ValueError):
        buy(journal, **patch)
    assert journal.get_trade("t1").to_dict() == before


def test_identified_sell_without_exact_trade_never_recovers_by_symbol(tmp_path):
    journal = TradeJournal(str(tmp_path))
    with pytest.raises(ValueError):
        sell(journal, symbol="005930", entry_price=100)
    assert journal._trades == {}


def test_json_write_failure_is_not_applied_or_silently_successful(tmp_path, monkeypatch):
    journal = TradeJournal(str(tmp_path))
    import os
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("synthetic")))
    with pytest.raises(OSError):
        buy(journal)
    assert journal.get_trade("t1") is None


def test_explicit_aware_execution_time_is_preserved_in_kst(tmp_path):
    journal = TradeJournal(str(tmp_path))
    trade = buy(journal, execution_time=datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc))
    assert trade.entry_time == datetime(2026, 10, 2, 10, 0)


@pytest.mark.parametrize("args", [("",date.today(),"1","e"),("s",datetime.now(),"1","e"),
                                  ("s",date.today(),"","e"),("s",date.today(),"1","")])
def test_identity_is_strict(args):
    with pytest.raises(ValueError):
        ExecutionIdentity(*args)


@pytest.mark.asyncio
async def test_db_summary_sync_cannot_rewind_identified_memory(tmp_path, monkeypatch):
    journal = TradeJournal(str(tmp_path))
    buy(journal)
    sell(journal)
    before = deepcopy(journal.get_trade("t1").to_dict())
    row = dict(before, pnl=0, exit_quantity=0, exit_time=datetime.now(), exit_type="", exit_reason="")
    class Pool:
        async def fetch(self, *args): return [row]
        async def close(self): pass
    import asyncpg
    async def pool(*args, **kwargs): return Pool()
    monkeypatch.setenv("DATABASE_URL", "synthetic-unused")
    monkeypatch.setattr(asyncpg, "create_pool", pool)
    await journal.sync_from_db()
    assert journal.get_trade("t1").to_dict() == before


def test_price_signature_does_not_round_distinct_decimal_inputs(tmp_path):
    from decimal import Decimal
    journal = TradeJournal(str(tmp_path))
    buy(journal,entry_price=Decimal("100.00000000000000000000000000001"))
    with pytest.raises(ValueError):
        buy(journal,entry_price=Decimal("100.00000000000000000000000000002"))


@pytest.mark.parametrize("section,field,value", [
    ("summary","entry_quantity",999), ("summary","entry_price",999),
    ("summary","pnl",777), ("event","pnl",888), ("event","status","forged"),
    ("event","event_time","2000-01-01T00:00:00"),
])
def test_full_execution_payload_corruption_rejected_on_repeat_and_restart(tmp_path,section,field,value):
    import json
    journal = TradeJournal(str(tmp_path))
    buy(journal)
    record = next(iter(journal.get_trade("t1").execution_records.values()))
    record[section][field] = value
    with pytest.raises(ValueError):
        buy(journal)
    path = next(tmp_path.glob("trades_*.json"))
    content = json.loads(path.read_text())
    next(iter(content["trades"][0]["execution_records"].values()))[section][field] = value
    path.write_text(json.dumps(content))
    with pytest.raises(ValueError):
        TradeJournal(str(tmp_path))


@pytest.mark.parametrize("basis", [None, 0, -1, float("nan"), float("inf")])
def test_identified_sell_requires_explicit_finite_positive_remaining_basis(tmp_path, basis):
    journal = TradeJournal(str(tmp_path))
    buy(journal)
    before = deepcopy(journal.get_trade("t1").to_dict())
    with pytest.raises(ValueError):
        sell(journal, avg_entry_price=basis)
    assert journal.get_trade("t1").to_dict() == before


@pytest.mark.parametrize("failure", ["dump", "replace"])
def test_metadata_save_failure_preserves_atomic_json_and_rolls_back_memory(tmp_path, monkeypatch, failure):
    import json, os
    journal = TradeJournal(str(tmp_path))
    buy(journal)
    before = deepcopy(journal.get_trade("t1").to_dict())
    path = next(tmp_path.glob("trades_*.json"))
    original_bytes = path.read_bytes()
    with monkeypatch.context() as patch:
        if failure == "dump":
            def partial_dump(value, stream, *args, **kwargs):
                stream.write("{")
                raise OSError("synthetic partial write")
            patch.setattr(json,"dump",partial_dump)
        else:
            patch.setattr(os,"replace",lambda *args: (_ for _ in ()).throw(OSError("synthetic replace")))
        assert journal.update_market_context("t1",{"new":"metadata"}) is False
    assert path.read_bytes() == original_bytes
    assert journal.get_trade("t1").to_dict() == before
    assert TradeJournal(str(tmp_path)).get_trade("t1").to_dict() == before


@pytest.mark.parametrize('field,value', [('entry_quantity',999),('entry_price',777),('pnl',777),
    ('pnl_pct',99),('exit_quantity',999),('exit_price',888),('entry_time','2020-01-01T00:00:00'),
    ('exit_time','2020-01-01T00:00:00'),('entry_strategy','other'),('id','other'),('symbol','000660')])
def test_outer_accounting_corruption_rejected_on_load(tmp_path,field,value):
    import json
    from src.data.storage.execution_journal import ExecutionPayloadError
    journal = TradeJournal(str(tmp_path))
    buy(journal)
    sell(journal)
    path = next(tmp_path.glob('trades_*.json'))
    data = json.loads(path.read_text())
    data['trades'][0][field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(ExecutionPayloadError):
        TradeJournal(str(tmp_path))


@pytest.mark.parametrize('execution',['e1','e2'])
def test_mutated_returned_accounting_cannot_be_reused(tmp_path,execution):
    from src.data.storage.execution_journal import ExecutionPayloadError
    journal = TradeJournal(str(tmp_path))
    trade = buy(journal)
    trade.entry_quantity = 999
    with pytest.raises(ExecutionPayloadError):
        buy(journal,execution)


@pytest.mark.parametrize('change',['key','reorder','predecessor'])
def test_registry_chain_must_match_keys_and_order(tmp_path,change):
    from src.data.storage.execution_journal import execution_payload_digest,ExecutionPayloadError
    journal = TradeJournal(str(tmp_path))
    buy(journal)
    trade = buy(journal,'e2')
    records = trade.execution_records
    keys = list(records)
    if change == 'key':
        records['wrong'] = records.pop(keys[1])
    elif change == 'reorder':
        trade.execution_records = dict(reversed(list(records.items())))
    else:
        records[keys[1]]['predecessor'] = None
        records[keys[1]]['payload_digest'] = execution_payload_digest(records[keys[1]])
    with pytest.raises(ExecutionPayloadError):
        buy(journal,'e3')


@pytest.mark.parametrize('side',['BUY','SELL'])
def test_legacy_mutation_of_identified_trade_is_rejected(tmp_path,side):
    from src.data.storage.execution_journal import ExecutionPayloadError
    journal = TradeJournal(str(tmp_path))
    buy(journal)
    before = journal.get_trade('t1').to_dict()
    with pytest.raises(ExecutionPayloadError):
        (buy if side == 'BUY' else sell)(journal,execution_identity=None)
    assert journal.get_trade('t1').to_dict() == before


def test_mutable_metadata_does_not_invalidate_accounting(tmp_path):
    journal = TradeJournal(str(tmp_path))
    buy(journal)
    assert journal.update_market_context('t1',{'synthetic':'context'})
    restarted = TradeJournal(str(tmp_path))
    buy(restarted)
    buy(restarted,'e2')
    assert restarted.get_trade('t1').entry_quantity == 4
