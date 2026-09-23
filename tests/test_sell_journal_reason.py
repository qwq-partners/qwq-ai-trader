"""실제 SELL 체결 루프의 주문 근거 보존 — 브로커/LLM/DB는 합성 입력만 사용."""

import asyncio
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.evolution.trade_journal import TradeJournal, TradeRecord
from src.core.types import Fill, Order, OrderSide
from src.schedulers import kr_scheduler as ks
from test_sync_portfolio_characterization import _fill_check_once, _make, _pos


SYM = "034220"
REASON = "LLM 종가점검: 긴급 손절보다 추세 약화가 청산 판단 근거"


@pytest.fixture
def harness(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    pos = _pos(SYM, qty=100, avg="10000", cur="9800", strategy="sepa_trend")
    pos.trade_id = "trade-1"
    sched, bot, sleeps = _make(monkeypatch, bot_positions=[pos], balance={}, kis_seq=[])
    bot.exit_manager = None
    bot.trade_journal = TradeJournal(str(tmp_path / "journal"))
    trade = TradeRecord(id="trade-1", symbol=SYM, entry_time=datetime.now(),
                        entry_price=10000, entry_quantity=100, entry_strategy="sepa_trend")
    bot.trade_journal._trades[trade.id] = trade
    risk_calls, events, outcomes = [], [], []
    bot.risk_manager.record_exit = lambda *a, **kw: risk_calls.append((a, kw))
    bot.engine.risk_manager._pending_exit_reasons = {}
    bot.engine.risk_manager._trade_memory = SimpleNamespace(
        record_outcome=lambda **kw: outcomes.append(kw))

    async def emit(event):
        events.append(event)

    bot.engine.emit = emit
    return SimpleNamespace(sched=sched, bot=bot, sleeps=sleeps, trade=trade,
                           risk_calls=risk_calls, events=events, outcomes=outcomes)


def drive(monkeypatch, h, *, quantity=100, reason=REASON, order_id="llm-1"):
    fill = Fill(order_id=order_id, symbol=SYM, side=OrderSide.SELL,
                quantity=quantity, price=Decimal("9800"), reason=reason)
    h.bot.broker.fills_seq = [[fill]]
    _fill_check_once(monkeypatch, h.sched, h.sleeps)
    return fill


def test_order_reason_reaches_real_journal_without_changing_risk(monkeypatch, harness):
    """Fill.reason을 무시하거나 LLM 본문을 리스크 유형으로 해석하면 실패."""
    h = harness
    fill = drive(monkeypatch, h)
    assert h.trade.exit_reason == REASON
    assert h.trade.exit_type == "llm_eod"
    saved = json.loads(next(h.bot.trade_journal.storage_dir.glob("*.json")).read_text())
    assert saved["trades"][0]["exit_reason"] == REASON
    assert h.outcomes[-1]["exit_type"] == "llm_eod"
    assert h.risk_calls == [((SYM, 9800.0), {
        "sector": None, "exit_type": "manual", "is_full_exit": True})]
    assert h.events[0].fill is fill


def test_trade_storage_keeps_explicit_llm_type_in_json_and_db_queue(monkeypatch, harness, tmp_path):
    from src.data.storage.trade_storage import TradeStorage
    h = harness
    monkeypatch.setenv("TRADE_JOURNAL_DIR", str(tmp_path / "storage"))
    storage = TradeStorage(db_url="")
    storage._trades[h.trade.id] = h.trade
    writes = []
    monkeypatch.setattr(storage, "_enqueue", lambda sql, params: writes.append((sql, params)))
    h.bot.trade_journal = storage
    reason = "LLM 종가점검: 본전 이탈 가능성 때문에 종가 청산"
    drive(monkeypatch, h, reason=reason)
    assert h.trade.exit_type == "llm_eod"
    saved = json.loads(next(storage._journal.storage_dir.glob("*.json")).read_text())
    assert saved["trades"][0]["exit_type"] == "llm_eod"
    update = next(args for sql, args in writes if "UPDATE trades SET" in sql)
    insert = next(args for sql, args in writes if "INSERT INTO trade_events" in sql)
    assert update[3:5] == (reason, "llm_eod")
    assert insert[6:8] == ("llm_eod", reason)
    assert h.outcomes[-1]["exit_type"] == "llm_eod"


def test_partial_fills_keep_order_reason_after_symbol_cache_is_popped(monkeypatch, harness):
    h = harness
    h.bot._exit_reasons[SYM] = "갭EOD: 이전 주문 캐시"
    drive(monkeypatch, h, quantity=40)
    assert h.trade.exit_reason == REASON
    assert h.bot._exit_reasons == {}
    drive(monkeypatch, h, quantity=60)
    assert h.trade.exit_quantity == 100
    assert h.trade.exit_reason == REASON
    assert [item["exit_type"] for item in h.outcomes] == ["llm_eod", "llm_eod"]
    assert [item[1]["exit_type"] for item in h.risk_calls] == ["manual", "manual"]


def test_broker_order_reason_survives_cumulative_fills_into_journal(monkeypatch, harness):
    """실제 KIS 주문번호 매칭·누적→증분 계산부터 일지까지 연결한다."""
    from src.execution.broker.kis_kr import KISBroker
    h = harness
    broker = object.__new__(KISBroker)
    order = Order(id="llm-1", symbol=SYM, side=OrderSide.SELL, quantity=100,
                  reason=REASON, price=None)
    broker._pending_orders = {order.id: order}
    broker._order_id_to_kis_no = {order.id: "synthetic-broker-1"}
    broker._order_id_to_orgno = {}
    monkeypatch.setattr(KISBroker, "is_connected", property(lambda self: True))
    cumulative = iter([40, 100])

    async def query():
        return [{"ODNO": "synthetic-broker-1", "TOT_CCLD_QTY": str(next(cumulative)),
                 "AVG_PRVS": "9800"}]

    broker._query_daily_fills = query
    h.bot.broker = broker
    _fill_check_once(monkeypatch, h.sched, h.sleeps)
    assert h.trade.exit_quantity == 40
    assert (h.trade.exit_reason, h.trade.exit_type) == (REASON, "llm_eod")
    _fill_check_once(monkeypatch, h.sched, h.sleeps)
    assert h.trade.exit_quantity == 100
    assert h.trade.exit_reason == REASON
    assert [event.fill.quantity for event in h.events] == [40, 60]
    assert broker._pending_orders == {}


def test_missing_reason_on_later_order_stays_unknown(monkeypatch, harness):
    """누적 대표 사유 보존은 범위 밖: 이전 주문 이유를 다음 주문에 전염시키지 않는다."""
    h = harness
    drive(monkeypatch, h, quantity=40)
    drive(monkeypatch, h, quantity=60, reason=None, order_id="manual-2")
    assert h.trade.exit_quantity == 100
    assert h.trade.exit_reason == "fill_detected"
    assert [outcome["exit_type"] for outcome in h.outcomes] == ["llm_eod", "manual"]


@pytest.mark.parametrize("reason", [None, "", "  ", 123])
def test_missing_reason_never_borrows_stale_symbol_intent(monkeypatch, harness, reason):
    h = harness
    h.bot._exit_reasons[SYM] = REASON
    drive(monkeypatch, h, reason=reason, order_id="unmatched-manual")
    assert h.trade.exit_reason == "fill_detected"
    assert h.trade.exit_type == "sync_detected"
    assert h.bot._exit_reasons == {}
    # 기존 캐시 기반 리스크 동작은 그대로 유지한다.
    assert h.risk_calls[0][1]["exit_type"] == "emergency_stop"


@pytest.mark.parametrize("reason,want_type", [
    ("갭EOD: 수익률 -1.2%", "manual"),
    ("손절: -5%", "stop_loss"),
    ("미체결 폴백: 시장가 전환", "manual"),
])
def test_other_order_reasons_remain_literal(monkeypatch, harness, reason, want_type):
    h = harness
    drive(monkeypatch, h, reason=reason)
    assert (h.trade.exit_reason, h.trade.exit_type) == (reason, want_type)


class FakeDB:
    """SQL 호출 경계만 대체하여 실제 스케줄러의 두 저장 payload를 검사."""
    def __init__(self):
        self.writes = []

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def fetchrow(self, query, *args):
        return {"id": "db-trade", "entry_price": 10000,
                "entry_strategy": "sepa_trend", "entry_quantity": 100,
                "exit_quantity": 0, "exit_time": None}

    async def execute(self, query, *args):
        self.writes.append((query, args))


@pytest.mark.parametrize("quantity", [40, 100])
def test_db_fallback_writes_same_order_reason(monkeypatch, harness, quantity):
    h = harness
    h.bot.engine.portfolio.positions[SYM].trade_id = None
    h.bot.trade_journal._trades.clear()
    db = FakeDB()
    h.bot.trade_journal.pool = db
    drive(monkeypatch, h, quantity=quantity)
    assert len(db.writes) == 2
    insert, update = [args for query, args in db.writes]
    assert insert[6:8] == ("llm_eod", REASON)
    if quantity == 100:
        assert update[3:5] == (REASON, "llm_eod")
    else:
        assert insert[-1] == "partial"
        assert update[0] == 40


def test_llm_decision_log_is_bounded_single_line_without_changing_signal(monkeypatch, harness):
    import src.utils.llm as llm_module
    h = harness
    reason = "추세 약화\r\n" + "가" * 1000
    messages = []

    async def complete_json(**kwargs):
        return {"positions": [{"symbol": SYM, "action": "exit_today", "reason": reason}]}

    async def alert(text):
        pass

    monkeypatch.setattr(llm_module, "get_llm_manager", lambda: SimpleNamespace(complete_json=complete_json))
    monkeypatch.setattr(ks, "send_alert", alert)
    monkeypatch.setattr(ks, "logger", SimpleNamespace(
        info=messages.append, warning=messages.append, error=messages.append))
    h.bot.batch_analyzer = None
    asyncio.run(h.sched._run_position_eod_llm_check())
    decision_logs = [msg for msg in messages if "action=exit_today" in msg]
    assert len(decision_logs) == 1
    assert "추세 약화" in decision_logs[0]
    assert "\r" not in decision_logs[0] and "\n" not in decision_logs[0]
    assert len(decision_logs[0]) <= 400
    assert h.events[0].reason == "LLM 종가점검: " + reason
