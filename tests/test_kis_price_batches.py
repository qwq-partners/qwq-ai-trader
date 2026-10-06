"""합성 KRX/NXT 배치: 전체 파싱 검증·등록 소유권·연결 세대 경계."""
import asyncio
import json
from decimal import Decimal

import pytest

from test_quote_subscription import ack, book_frame, install, make_feed, price_frame

pytestmark = pytest.mark.asyncio


def record(symbol="005930", width=47, price=10000):
    fields = ["0"] * width
    fields[0:6] = [symbol, "100001", str(price), "2", "100", "1.25"]
    fields[7:10] = ["9900", "10200", "9800"]
    fields[13:15] = ["123", "1230000"]
    fields[33] = "20261006"
    return fields


def frame(records, tr="H0STCNT0", count=None, encrypted="0"):
    return f"{encrypted}|{tr}|{len(records) if count is None else count}|" + "^".join(
        field for fields in records for field in fields)


async def setup(managed, symbols, tr="H0STCNT0"):
    feed = make_feed()
    owner = buffer = None
    if managed:
        buffer, owner = install(feed)
        await feed.connect()
        await owner.set_operational(symbols, tr, "H0NXASP0" if tr == "H0NXCNT0" else "H0STASP0")
    events = []
    async def collect(event): events.append(event)
    feed.on_market_data(collect)
    return feed, owner, buffer, events


async def deliver(feed, owner, data):
    await feed._handle_message(data, socket=feed._ws,
                               generation=owner.generation if owner else None)


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("tr", ["H0STCNT0", "H0NXCNT0"])
@pytest.mark.parametrize("width", [46, 47])
@pytest.mark.parametrize("count", [2, 20])
async def test_batch_delivers_every_record_in_order(managed, tr, width, count):
    records = [record(f"{5930 + i:06d}", width, 10000 + i) for i in range(count)]
    feed, owner, buffer, events = await setup(managed, [r[0] for r in records], tr)
    try:
        await deliver(feed, owner, frame(records, tr))
        assert [(e.symbol, e.close) for e in events] == [
            (f"{5930 + i:06d}", Decimal(10000 + i)) for i in range(count)]
        assert all((e.open, e.high, e.low, e.volume, e.value, e.change, e.change_pct) ==
                   (Decimal(9900), Decimal(10200), Decimal(9800), 123, Decimal(1230000), Decimal(100), 1.25)
                   for e in events)
        assert feed._price_data_count == count
        if managed:
            assert feed._managed_data_count == count
            assert feed._subscribed_symbols == set()  # 데이터만으로 ACK를 대체하지 않는다.
            assert buffer.export()["complete"]
    finally:
        if managed: await feed.disconnect()


async def test_each_record_registration_is_checked_and_rest_needs_ack_and_data():
    feed, owner, _, events = await setup(True, ["000660"])
    try:
        await deliver(feed, owner, json.dumps(ack(("H0STCNT0", "000660"))))
        assert not feed._subscribed_symbols
        await deliver(feed, owner, frame([record("005930"), record("000660")]))
        assert [e.symbol for e in events] == ["000660"]
        assert feed._subscribed_symbols == {"000660"}
    finally:
        await feed.disconnect()


@pytest.mark.parametrize("defect", ["length", "mixed_width", "extra_pipe", "single_count", "wrong_count", "zero_count", "negative_count",
    "large_count", "bad_count", "encrypted", "book", "unknown", "symbol", "nan", "inf",
    "price", "change", "open", "high", "low", "volume", "value", "time", "sign"])
@pytest.mark.parametrize("managed", [False, True])
async def test_invalid_batch_has_no_partial_delivery(managed, defect):
    records = [record(), record()]
    kwargs = {}
    if defect == "length": records[1].pop()
    elif defect == "mixed_width": records[0].pop()
    elif defect == "extra_pipe": records[1][-1] = "0|extra"
    elif defect in ("single_count", "wrong_count", "zero_count", "negative_count", "large_count", "bad_count"):
        kwargs["count"] = {"single_count": "1", "wrong_count": "3", "zero_count": "0", "negative_count": "-1", "large_count": "1000", "bad_count": "bad"}[defect]
    elif defect == "encrypted": kwargs["encrypted"] = "1"
    elif defect == "book": kwargs["tr"] = "H0STASP0"
    elif defect == "unknown": kwargs["tr"] = "UNKNOWN"
    else:
        index, value = {"symbol": (0, "abc123"), "nan": (5, "NaN"), "inf": (5, "inf"),
            "price": (2, "0"), "change": (4, "broken"), "open": (7, "broken"),
            "high": (8, "broken"), "low": (9, "broken"), "volume": (13, "broken"),
            "value": (14, "broken"), "time": (1, "256199"), "sign": (3, "wrong")}[defect]
        records[1][index] = value
    feed, owner, buffer, events = await setup(managed, ["005930"])
    quotes = []
    async def collect_quote(event): quotes.append(event)
    feed.on_quote(collect_quote)
    try:
        await deliver(feed, owner, frame(records, **kwargs))
        assert events == [] and quotes == []
        assert feed._price_data_count == 0
        if managed:
            assert feed._managed_data_count == 0
            assert not feed._subscribed_symbols
            assert not buffer.export()["complete"]
            assert len([r for r in buffer.export()["records"] if r.get("status") == "connection_gap"]) == 1
    finally:
        if managed: await feed.disconnect()


@pytest.mark.parametrize("change", ["socket", "generation", "disconnected", "unsubscribe"])
async def test_callback_wait_cannot_deliver_remaining_records_from_old_connection(change):
    feed, owner, _, events = await setup(True, ["005930"])
    original_socket = feed._ws
    second_callback = []
    async def invalidate(event):
        await asyncio.sleep(0)
        if change == "socket": feed._ws = object()
        elif change == "generation": owner.generation += 1
        elif change == "disconnected": feed._connected = False
        else: await owner.set_operational([], "H0STCNT0", "H0STASP0")
    async def collect_late(event): second_callback.append(event)
    feed.on_market_data(invalidate)
    feed.on_market_data(collect_late)
    try:
        await deliver(feed, owner, frame([record(), record(price=10001)]))
        assert len(events) == 1
        assert second_callback == []
        assert feed._price_data_count == 1
    finally:
        feed._ws = original_socket
        await feed.disconnect()


@pytest.mark.parametrize("managed", [False, True])
async def test_legacy_single_price_and_book_keep_working(managed):
    feed, owner, _, events = await setup(managed, ["005930"])
    quotes = []
    async def collect(event): quotes.append(event)
    feed.on_quote(collect)
    try:
        await deliver(feed, owner, price_frame())
        await deliver(feed, owner, book_frame())
        assert len(events) == len(quotes) == 1
    finally:
        if managed: await feed.disconnect()


@pytest.mark.parametrize("managed", [False, True])
async def test_maximum_supported_count_is_bounded_and_delivered(managed):
    feed, owner, _, events = await setup(managed, ["005930"])
    try:
        await deliver(feed, owner, frame([record()] * 999))
        assert len(events) == 999
        assert feed._price_data_count == 999
    finally:
        if managed: await feed.disconnect()
