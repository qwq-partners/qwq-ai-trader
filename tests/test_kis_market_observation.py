"""Offline contract tests for KIS KR real-time market observations."""

import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
from zoneinfo import ZoneInfo

import pytest

from src.core.event import MarketDataEvent
from src.core.market_observation import MarketObservation
from src.data.feeds import kis_websocket
from src.data.feeds.kis_websocket import KISWebSocketConfig, KISWebSocketFeed


KST = ZoneInfo("Asia/Seoul")


class FakeTokenManager:
    """An external boundary fake: no approval endpoint is ever contacted."""

    def __init__(self):
        self.calls = 0

    async def get_approval_key(self):
        self.calls += 1
        return "offline-approval"


class FakeWebSocket:
    closed = False


def _record(
    *,
    symbol="005930",
    time="091530",
    price="70000",
    change_sign="2",
    change="100",
    change_pct="0.14",
    open_price="69900",
    high="70100",
    low="69800",
    volume="12345",
    value="864150000",
    date="20260918",
):
    """Make one documented 46-column KRX/NXT trade-price record."""
    fields = ["0"] * 46
    fields[0] = symbol
    fields[1] = time
    fields[2] = price
    fields[3] = change_sign
    fields[4] = change
    fields[5] = change_pct
    fields[7] = open_price
    fields[8] = high
    fields[9] = low
    fields[13] = volume
    fields[14] = value
    fields[33] = date
    return "^".join(fields)


def _frame(records, *, tr_id="H0STCNT0", count=None, marker="0", suffix=""):
    if count is None:
        count = len(records)
    return f"{marker}|{tr_id}|{count:03d}|{'^'.join(records)}{suffix}"


def _feed(monkeypatch, *, received_at=None):
    token_manager = FakeTokenManager()
    monkeypatch.setattr(kis_websocket, "get_token_manager", lambda: token_manager)
    clock = lambda: received_at or datetime(2026, 9, 18, 0, 30, tzinfo=timezone.utc)
    feed = KISWebSocketFeed(KISWebSocketConfig(ws_url="ws://offline.invalid"), clock=clock)
    return feed, token_manager


def _deliver(feed, frame):
    asyncio.run(feed._handle_message(frame))


def _observation_kwargs():
    return {
        "symbol": "005930",
        "price": Decimal("70000"),
        "open": Decimal("69900"),
        "high": Decimal("70100"),
        "low": Decimal("69800"),
        "volume": 12345,
        "value": Decimal("864150000"),
        "change_sign": "2",
        "change": Decimal("100"),
        "change_pct": Decimal("0.14"),
        "source": "kis_websocket:H0STCNT0",
        "tr_id": "H0STCNT0",
        "exchange": "KRX",
        "raw_date": "20260918",
        "raw_time": "091530",
        "received_at": datetime(2026, 9, 18, 0, 30, tzinfo=timezone.utc),
        "connection_id": "11111111-1111-4111-8111-111111111111",
        "frame_sequence": 7,
        "record_index": 1,
        "record_digest": hashlib.sha256(b"record").hexdigest(),
    }


def test_market_observation_is_frozen_and_derives_coherent_kst_as_of():
    observation = MarketObservation(**_observation_kwargs())

    assert observation.market_as_of == datetime(2026, 9, 18, 9, 15, 30, tzinfo=KST)
    assert observation.source_event_id == "11111111-1111-4111-8111-111111111111:7:1"
    with pytest.raises(FrozenInstanceError):
        observation.price = Decimal("1")
    with pytest.raises(ValueError, match="source"):
        MarketObservation(**{**_observation_kwargs(), "source": "kis_websocket:H0NXCNT0"})


def test_market_observation_canonical_json_round_trip_recomputes_derived_facts():
    observation = MarketObservation(**_observation_kwargs())

    payload = observation.to_dict()
    restored = MarketObservation.from_dict(payload)

    assert payload["price"] == "70000"
    assert payload["received_at"] == "2026-09-18T00:30:00+00:00"
    assert payload["market_as_of"] == "2026-09-18T09:15:30+09:00"
    assert restored == observation
    assert restored.to_dict() == payload


def test_market_observation_requires_a_canonical_connection_uuid():
    with pytest.raises(ValueError, match="connection"):
        MarketObservation(**{**_observation_kwargs(), "connection_id": "connection-a"})


def test_market_observation_rejects_pct_that_would_overflow_legacy_float_event():
    with pytest.raises(ValueError, match="change_pct"):
        MarketObservation(**{**_observation_kwargs(), "change_pct": Decimal("1E+10000")})


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"unknown_raw_field": "no"}),
        lambda payload: payload.update({"price": " 70000"}),
        lambda payload: payload.update({"market_as_of": "2026-09-18T09:15:31+09:00"}),
        lambda payload: payload.update({"source_event_id": "11111111-1111-4111-8111-111111111111:7:2"}),
        lambda payload: payload.update({"source": "kis_websocket:H0NXCNT0"}),
        lambda payload: payload.update({"schema_version": True}),
        lambda payload: payload.update({"schema_version": 1.0}),
    ],
)
def test_market_observation_rejects_noncanonical_or_contradictory_json(mutate):
    payload = MarketObservation(**_observation_kwargs()).to_dict()
    mutate(payload)

    with pytest.raises(ValueError):
        MarketObservation.from_dict(payload)


def test_two_record_krx_frame_preserves_original_time_and_atomic_record_identity(monkeypatch):
    feed, token_manager = _feed(monkeypatch)
    received = []

    async def receive(event):
        received.append(event)

    feed.on_market_data(receive)
    _deliver(feed, _frame([_record(), _record(symbol="000660", time="091531", price="200000")]))

    assert token_manager.calls == 0
    assert [event.symbol for event in received] == ["005930", "000660"]
    assert [event.close for event in received] == [Decimal("70000"), Decimal("200000")]
    assert feed._price_data_count == 2
    first, second = (event.observation for event in received)
    assert first.source == "kis_websocket:H0STCNT0"
    assert first.tr_id == "H0STCNT0"
    assert first.exchange == "KRX"
    assert first.market_as_of == datetime(2026, 9, 18, 9, 15, 30, tzinfo=KST)
    assert first.received_at == datetime(2026, 9, 18, 0, 30, tzinfo=timezone.utc)
    assert first.market_as_of != first.received_at
    assert first.source_event_id != second.source_event_id
    assert len(first.record_digest) == 64


def test_nxt_source_keeps_kst_market_time_when_receipt_clock_is_utc(monkeypatch):
    feed, _ = _feed(monkeypatch, received_at=datetime(2026, 9, 18, 0, 31, tzinfo=timezone.utc))
    received = []

    async def receive(event):
        received.append(event)

    feed.on_market_data(receive)
    _deliver(feed, _frame([_record(time="153001")], tr_id="H0NXCNT0"))

    observation = received[0].observation
    assert observation.exchange == "NXT"
    assert observation.source == "kis_websocket:H0NXCNT0"
    assert observation.market_as_of == datetime(2026, 9, 18, 15, 30, 1, tzinfo=KST)
    assert observation.received_at.tzinfo == timezone.utc


def test_printable_non_numeric_symbol_keeps_existing_zfill_normalization(monkeypatch):
    feed, _ = _feed(monkeypatch)
    received = []

    async def receive(event):
        received.append(event)

    feed.on_market_data(receive)
    _deliver(feed, _frame([_record(symbol="ABCD")]))

    assert received[0].symbol == "00ABCD"
    assert received[0].observation.symbol == "00ABCD"


def test_change_sign_five_preserves_existing_negative_event_and_observation_facts(monkeypatch):
    feed, _ = _feed(monkeypatch)
    received = []

    async def receive(event):
        received.append(event)

    feed.on_market_data(receive)
    _deliver(feed, _frame([_record(change_sign="5", change="100", change_pct="0.14")]))

    assert received[0].change == Decimal("-100")
    assert received[0].change_pct == -0.14
    assert received[0].observation.change == received[0].change
    assert received[0].observation.change_pct == Decimal("-0.14")


def test_change_sign_five_preserves_exact_source_decimals_under_low_precision(monkeypatch):
    feed, _ = _feed(monkeypatch)
    received = []

    async def receive(event):
        received.append(event)

    feed.on_market_data(receive)
    with localcontext() as context:
        context.prec = 3
        _deliver(feed, _frame([_record(change_sign="5", change="12345", change_pct="12.345")]))

    observation = received[0].observation
    assert observation.change == Decimal("-12345")
    assert observation.change_pct == Decimal("-12.345")
    assert received[0].change == Decimal("-12345")
    assert received[0].change_pct == -12.345


def test_change_sign_five_keeps_legacy_integer_positive_zero_and_percent_signed_zero(monkeypatch):
    feed, _ = _feed(monkeypatch)
    received = []

    async def receive(event):
        received.append(event)

    feed.on_market_data(receive)
    _deliver(feed, _frame([_record(change_sign="5", change="0", change_pct="0.0")]))

    observation = received[0].observation
    assert observation.change == Decimal("0") and not observation.change.is_signed()
    assert observation.change_pct == Decimal("-0.0") and observation.change_pct.is_signed()
    assert received[0].change == Decimal("0") and not received[0].change.is_signed()
    assert received[0].change_pct == -0.0


def test_change_sign_five_does_not_round_a_large_accepted_change(monkeypatch):
    feed, _ = _feed(monkeypatch)
    received = []
    digits = "12345678901234567890123456789"

    async def receive(event):
        received.append(event)

    feed.on_market_data(receive)
    _deliver(feed, _frame([_record(change_sign="5", change=digits)]))

    assert received[0].observation.change == Decimal("-" + digits)


@pytest.mark.parametrize("malformed", ["1__2", "_12", "12_"])
def test_float_invalid_percent_text_rejects_whole_frame_without_partial_callback(monkeypatch, malformed):
    feed, _ = _feed(monkeypatch)
    received = []

    async def receive(event):
        received.append(event)

    feed.on_market_data(receive)
    _deliver(feed, _frame([_record(), _record(change_pct=malformed)]))

    assert received == []
    assert feed._price_data_count == 0


@pytest.mark.parametrize(
    "frame",
    [
        _frame(["^".join(["0"] * 20)]),
        _frame(["^".join(["0"] * 45)]),
        _frame(["^".join(["0"] * 47)]),
        _frame([_record()], marker="1"),
        _frame([_record()], suffix="|extra"),
        _frame([_record()], tr_id="H0STCNI0"),
        _frame([_record()], count=2),
    ],
)
def test_rejects_noncanonical_or_wrong_length_price_frames_without_callbacks(monkeypatch, frame):
    feed, _ = _feed(monkeypatch)
    received = []

    async def receive(event):
        received.append(event)

    feed.on_market_data(receive)
    _deliver(feed, frame)

    assert received == []
    assert feed._price_data_count == 0


@pytest.mark.parametrize("count", ["000", "-01", "2.0", " 02", "02x"])
def test_rejects_non_positive_or_non_ascii_counts_without_callbacks(monkeypatch, count):
    feed, _ = _feed(monkeypatch)
    received = []

    async def receive(event):
        received.append(event)

    feed.on_market_data(receive)
    _deliver(feed, f"0|H0STCNT0|{count}|{_record()}")

    assert received == []
    assert feed._price_data_count == 0


@pytest.mark.parametrize(
    "bad_record",
    [
        _record(date="20250229"),
        _record(date="2026091"),
        _record(time="246001"),
        _record(price="0"),
        _record(price="-1"),
        _record(open_price="0"),
        _record(high="0"),
        _record(low="0"),
        _record(volume="-1"),
        _record(value="-1"),
        _record(change_pct="NaN"),
        _record(change_pct="Infinity"),
        _record(change_pct="1E+10000"),
    ],
)
def test_invalid_second_record_rejects_entire_frame_without_partial_statistics(monkeypatch, bad_record):
    feed, _ = _feed(monkeypatch)
    received = []

    async def receive(event):
        received.append(event)

    feed.on_market_data(receive)
    _deliver(feed, _frame([_record(), bad_record]))

    assert received == []
    assert feed._price_data_count == 0


def test_callback_failure_does_not_relabel_accepted_two_record_frame_as_atomic(monkeypatch):
    feed, _ = _feed(monkeypatch)
    delivered = []

    async def fail_first(event):
        if event.symbol == "005930":
            raise RuntimeError("test callback failure")

    async def observe(event):
        delivered.append(event.symbol)

    feed.on_market_data(fail_first)
    feed.on_market_data(observe)
    _deliver(feed, _frame([_record(), _record(symbol="000660", price="200000")]))

    assert feed._price_data_count == 2
    assert delivered == ["005930", "000660"]


def test_reconnect_resets_local_connection_identity_with_fake_websocket_and_no_auth(monkeypatch):
    feed, token_manager = _feed(monkeypatch)
    received = []

    async def receive(event):
        received.append(event)

    feed.on_market_data(receive)
    feed._activate_connection(FakeWebSocket())
    _deliver(feed, _frame([_record()]))
    first = received[-1].observation
    feed._activate_connection(FakeWebSocket())
    _deliver(feed, _frame([_record()]))
    second = received[-1].observation

    assert token_manager.calls == 0
    assert first.connection_id != second.connection_id
    assert first.frame_sequence == second.frame_sequence == 1
    assert first.source_event_id != second.source_event_id


def test_optional_observation_preserves_legacy_naive_event_heap_and_price_conversion():
    kr = MarketDataEvent(symbol="005930", close=Decimal("70000"))
    us = MarketDataEvent(symbol="AAPL", close=Decimal("200"))

    assert kr.observation is None
    assert kr.timestamp.tzinfo is None
    assert (kr < us) in (True, False)
    assert kr.to_price().timestamp is kr.timestamp
