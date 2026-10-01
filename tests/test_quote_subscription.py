"""실제 계정/네트워크 없이 채널 소유권·ACK·유실 경계를 검증한다."""
import asyncio
from datetime import datetime, timezone

import pytest

from src.analytics.entry_observation import EntryObservationBuffer
from src.data.feeds.quote_subscription import QuoteSubscriptionCoordinator

PRICE = "H0STCNT0"
BOOK = "H0STASP0"


def setup_owner(cap=41, reserved=0, headroom=0, max_candidates=100):
    buffer = EntryObservationBuffer(evaluation_epoch="synthetic", capacity=10000)
    clock = [100.0]
    owner = QuoteSubscriptionCoordinator(
        buffer, registration_cap=cap, external_reserved=reserved,
        operational_headroom=headroom, max_candidates=max_candidates,
        lease_seconds=600, evidence_ref="synthetic-exclusive-session-proof",
        unsubscribe_success_messages=("SYNTHETIC UNSUB ACK",),
        unsubscribe_evidence_ref="synthetic-test-contract-only",
        monotonic=lambda: clock[0], request_interval=0,
    )
    sent = []
    async def send(action, key):
        sent.append((action, key))
    return owner, buffer, clock, sent, send


def ack(key, action="subscribe", success=True):
    return {"header": {"tr_id": key[0], "tr_key": key[1]},
            "body": {"rt_cd": "0" if success else "1",
                     "msg1": "SUBSCRIBE SUCCESS" if action == "subscribe" else "SYNTHETIC UNSUB ACK"}}


@pytest.mark.asyncio
async def test_candidate_book_request_ack_and_observed_are_separate():
    owner, buffer, _, sent, send = setup_owner()
    gen = await owner.start(send)
    await owner.enroll("scan", ["005930"])
    assert sent == [("subscribe", (BOOK, "005930"))]
    assert owner.snapshot()["acknowledged"] == 0
    assert not owner.operational_coverage()
    await owner.handle_ack(ack((BOOK, "005930")), gen)
    assert owner.snapshot()["acknowledged"] == 1
    assert owner.snapshot()["observed"] == 0
    owner.note_data((BOOK, "005930"), gen)
    assert owner.snapshot()["observed"] == 1
    assert not owner.accepts((PRICE, "005930"), gen)
    statuses = [r["status"] for r in buffer.export()["records"]]
    assert "requested" in statuses and "acknowledged" in statuses and "observed" in statuses


@pytest.mark.asyncio
@pytest.mark.parametrize("reserved,op_count,expected_books", [(0,20,1),(1,20,0),(0,21,0)])
async def test_budget_counts_channels_including_all_operational_demand(reserved, op_count, expected_books):
    owner, _, _, sent, send = setup_owner(reserved=reserved)
    await owner.set_operational([f"{i:06}" for i in range(op_count)], PRICE, BOOK)
    await owner.start(send)
    await owner.enroll("scan", ["900001", "900002"])
    assert len(sent) + reserved <= 41
    assert sum(key[1].startswith("9") for _, key in sent) == expected_books
    assert owner.snapshot()["operational_demand"] == op_count * 2
    assert owner.snapshot()["capacity_error"] is (op_count * 2 + reserved > 41)


@pytest.mark.asyncio
async def test_unsubscribe_needs_matching_success_before_slot_reuse():
    owner, _, clock, sent, send = setup_owner(cap=1)
    gen = await owner.start(send)
    key = (BOOK, "005930")
    await owner.enroll("scan", ["005930"])
    await owner.handle_ack(ack(key), gen)
    clock[0] += 601
    await owner.maintain()
    await owner.enroll("next", ["000660"])
    assert len(sent) == 2 and sent[-1] == ("unsubscribe", key)
    await owner.handle_ack(ack(key), gen)  # 늦은 SUB 성공은 해제 증거가 아니다.
    assert len(sent) == 2
    await owner.handle_ack(ack(key, "unsubscribe", False), gen)
    assert len(sent) == 2 and owner.snapshot()["occupied"] == 1
    # 거절 뒤 다시 보내지 않는다. 새 연결에서만 미확정 등록을 리셋한다.
    await owner.stop()
    await owner.start(send)
    assert sent[-1] == ("subscribe", (BOOK, "000660"))


@pytest.mark.asyncio
async def test_successful_release_admits_waiting_operational_channel():
    owner, _, _, sent, send = setup_owner(cap=2)
    gen = await owner.start(send)
    await owner.enroll("scan", ["005930", "000660"])
    for symbol in ("005930", "000660"):
        await owner.handle_ack(ack((BOOK, symbol)), gen)
    await owner.set_operational(["035420"], PRICE, BOOK)
    assert owner.snapshot()["waiting_for_release"]
    assert not owner.operational_coverage()
    await owner.handle_ack(ack((BOOK, "005930"), "unsubscribe"), gen)
    assert sent[-1] == ("subscribe", (PRICE, "035420"))
    assert owner.snapshot()["occupied"] == 2


@pytest.mark.asyncio
async def test_shared_book_lease_expiry_preserves_other_candidate_and_holding():
    owner, _, clock, sent, send = setup_owner()
    gen = await owner.start(send)
    await owner.enroll("scan1", ["005930"])
    await owner.handle_ack(ack((BOOK, "005930")), gen)
    clock[0] += 300
    await owner.enroll("scan2", ["005930"])
    clock[0] += 301
    await owner.maintain()
    assert len(sent) == 1
    await owner.set_operational(["005930"], PRICE, BOOK)
    assert sent[-1] == ("subscribe", (PRICE, "005930"))
    await owner.handle_ack(ack((PRICE, "005930")), gen)
    clock[0] += 301
    await owner.maintain()
    assert len(sent) == 2
    assert not owner.operational_coverage()
    owner.note_data((PRICE, "005930"), gen)
    assert owner.operational_coverage() == {"005930"}
    await owner.set_operational([], PRICE, BOOK)
    assert not owner.operational_coverage()


@pytest.mark.asyncio
async def test_failed_or_cancelled_send_reserves_uncertain_slot_until_reconnect():
    for exception in (RuntimeError("synthetic"), asyncio.CancelledError()):
        owner, buffer, _, sent, send = setup_owner(cap=1)
        async def fail(action, key):
            raise exception
        gen = await owner.start(fail)
        if isinstance(exception, asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await owner.enroll("scan", ["005930"])
        else:
            await owner.enroll("scan", ["005930"])
        assert owner.snapshot()["occupied"] == 1
        await owner.maintain()
        await owner.stop()
        assert buffer.export()["complete"] is False
        newgen = await owner.start(send)
        assert newgen != gen and len(sent) == 1
        await owner.handle_ack(ack((BOOK, "005930")), gen)
        assert owner.snapshot()["acknowledged"] == 0
        assert not owner.accepts((BOOK, "005930"), gen)


@pytest.mark.asyncio
async def test_pending_lease_mutation_during_send_is_not_lost():
    owner, _, _, sent, _ = setup_owner()
    entered, release = asyncio.Event(), asyncio.Event()
    async def send(action, key):
        sent.append((action, key))
        if len(sent) == 1:
            entered.set()
            await release.wait()
    await owner.start(send)
    first = asyncio.create_task(owner.enroll("a", ["005930"]))
    await entered.wait()
    second = asyncio.create_task(owner.enroll("b", ["000660"]))
    release.set()
    await asyncio.gather(first, second)
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_disconnected_withdrawals_and_expired_leases_do_not_resurrect():
    owner, _, clock, sent, send = setup_owner()
    await owner.set_operational(["005930"], PRICE, BOOK)
    await owner.enroll("scan", ["000660"])
    await owner.set_operational([], PRICE, BOOK)
    clock[0] += 601
    await owner.start(send)
    assert sent == []


@pytest.mark.parametrize("kwargs", [dict(registration_cap=42), dict(external_reserved=-1),
    dict(external_reserved=True), dict(evidence_ref=""), dict(lease_seconds=float("nan")),
    dict(max_candidates=0)])
def test_invalid_contract_rejected(kwargs):
    arguments = dict(registration_cap=41, external_reserved=0, operational_headroom=0,
                     max_candidates=100, lease_seconds=600, evidence_ref="synthetic")
    arguments.update(kwargs)
    with pytest.raises(ValueError):
        QuoteSubscriptionCoordinator(EntryObservationBuffer(evaluation_epoch="x", capacity=10), **arguments)


def make_feed():
    from types import SimpleNamespace
    from copy import deepcopy
    from src.core.types import MarketSession
    from src.data.feeds.kis_websocket import KISWebSocketFeed
    class Socket:
        closed = False
        close_code = 1000
        def __init__(self): self.sent = []
        async def send_json(self, msg): self.sent.append(deepcopy(msg))
        async def close(self): self.closed = True
    class Session:
        closed = False
        async def ws_connect(self, *a, **kw): return Socket()
        async def close(self): self.closed = True
    class Tokens:
        async def get_approval_key(self): return "synthetic-test-key"
    f = object.__new__(KISWebSocketFeed)
    f.config = SimpleNamespace(ws_url="synthetic", ping_interval=30)
    f._ws = None
    f._session = Session()
    f._token_manager = Tokens()
    f._approval_key = None
    f._running = f._connected = False
    f._should_connect = True
    for name in ("_subscribed_symbols", "_pending_subscriptions", "_priority_symbols", "_watch_symbols", "_nxt_symbols", "_regular_only_symbols"):
        setattr(f, name, set())
    f._symbol_scores = {}
    f._rolling_queue = []
    f._rolling_index = 0
    f._rolling_task = f._rebuild_task = None
    f._current_session = MarketSession.REGULAR
    f._data_callbacks = []
    f._quote_callbacks = []
    f._message_count = f._price_data_count = f._reconnect_count = 0
    f._logged_first_price = False
    return f


def install(f):
    buffer = EntryObservationBuffer(evaluation_epoch="synthetic", capacity=10000)
    f.enable_quote_observation(buffer, registration_cap=41, external_reserved=0,
        operational_headroom=0, max_candidates=100, lease_seconds=600,
        evidence_ref="synthetic-exclusive-session-proof", request_interval=0)
    return buffer, f._quote_subscription_owner


def book_frame(symbol="005930"):
    fields = ["0"] * 59
    fields[0], fields[1], fields[3], fields[13] = symbol, "100001", "10100", "10000"
    fields[23], fields[33] = "120", "230"
    return "0|H0STASP0|001|" + "^".join(fields)


@pytest.mark.asyncio
async def test_real_feed_candidate_book_does_not_emit_market_data_or_claim_rest_coverage():
    import json
    from src.analytics.entry_observation import capture_quote
    f = make_feed()
    buffer, owner = install(f)
    emitted = []
    async def market(event): emitted.append(event)
    async def quote(event): capture_quote(buffer, event)
    f.on_market_data(market)
    f.on_quote(quote)
    await owner.enroll("scan", ["005930"])
    assert await f.connect()
    ws, gen = f._ws, owner.generation
    await f._apply_subscriptions()
    assert len(ws.sent) == 1
    assert ws.sent[0]["body"]["input"]["tr_id"] == BOOK
    await f._handle_message(json.dumps(ack((BOOK, "005930"))), socket=ws, generation=gen)
    await f._handle_message(book_frame(), socket=ws, generation=gen)
    assert emitted == [] and not f._subscribed_symbols
    assert f._managed_data_count == 1
    saved = [r for r in buffer.export()["records"] if r["kind"] == "ws_quote"][0]
    assert saved["provenance"]["generation"] == gen
    # 이전 소켓 자료를 새 세대에 붙이지 않는다.
    await f._close_managed_socket(ws, gen)
    assert ws.closed and await f.connect()
    await f._handle_message(book_frame(), socket=ws, generation=gen)
    assert f._managed_data_count == 1
    await f.disconnect()
    assert f._quote_maintenance_task is None


@pytest.mark.asyncio
async def test_real_feed_disconnected_unsubscribe_does_not_revive_operational_desire():
    f = make_feed()
    _, owner = install(f)
    await f.subscribe(["005930"])
    await f.unsubscribe(["005930"])
    assert await f.connect()
    assert f._ws.sent == [] and owner.snapshot()["operational_demand"] == 0
    await f.disconnect()


@pytest.mark.asyncio
async def test_unmanaged_missing_owner_preserves_legacy_channel_trace():
    f = make_feed()
    await f.connect()
    await f.subscribe(["005930"])
    trace = lambda: [(m["header"]["tr_type"], m["body"]["input"]["tr_id"]) for m in f._ws.sent]
    assert trace() == [("1", PRICE), ("1", BOOK)]
    assert f._subscribed_symbols == {"005930"}
    await f._apply_subscriptions()
    assert len(trace()) == 2
    await f._rebuild_subscriptions()
    assert trace()[2:] == [("2", PRICE), ("2", BOOK), ("2", "H0NXCNT0"), ("2", "H0NXASP0"), ("1", PRICE), ("1", BOOK)]
    await f.unsubscribe(["005930"])
    assert not f._subscribed_symbols
    await f.disconnect()


def test_enable_requires_stopped_unoccupied_feed():
    f = make_feed()
    f._running = True
    with pytest.raises(RuntimeError): install(f)
    assert getattr(f, "_quote_subscription_owner", None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_frame", ["0|H0STASP0|bad|005930^broken",
    "0|H0STASP0|001|005930^broken", book_frame().replace("10100", "broken"),
    book_frame().replace("|001|", "|002|"), book_frame().replace("0|", "1|", 1)])
async def test_bad_first_managed_book_cannot_be_silently_skipped_for_better_later_book(bad_frame):
    f = make_feed()
    buffer, owner = install(f)
    await owner.enroll("scan", ["005930"])
    await f.connect()
    await f._handle_message(bad_frame, socket=f._ws, generation=owner.generation)
    assert buffer.export()["complete"] is False
    await f.disconnect()


@pytest.mark.asyncio
async def test_ack_bad_shape_wrong_key_action_generation_or_code_never_approves():
    owner, _, _, _, send = setup_owner()
    gen = await owner.start(send)
    await owner.enroll("scan", ["005930"])
    for message, generation in [({}, gen), ({"header": [], "body": []}, gen),
            (ack((PRICE, "005930")), gen), (ack((BOOK, "000660")), gen),
            (ack((BOOK, "005930"), "unsubscribe"), gen), (ack((BOOK, "005930")), gen - 1),
            ({"header": {"tr_id": BOOK, "tr_key": "005930"}, "body": {"rt_cd": 0, "msg1": "SUBSCRIBE SUCCESS"}}, gen)]:
        await owner.handle_ack(message, generation)
        assert owner.snapshot()["acknowledged"] == 0
    await owner.handle_ack(ack((BOOK, "005930")), gen)
    await owner.handle_ack(ack((BOOK, "005930")), gen)
    assert owner.snapshot()["acknowledged"] == 1


@pytest.mark.asyncio
async def test_rejected_unsubscribe_is_not_retried_on_maintenance():
    owner, _, clock, sent, send = setup_owner()
    gen = await owner.start(send)
    await owner.enroll("scan", ["005930"])
    await owner.handle_ack(ack((BOOK, "005930")), gen)
    clock[0] += 601
    await owner.maintain()
    await owner.handle_ack(ack((BOOK, "005930"), "unsubscribe", False), gen)
    await owner.maintain()
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_queue_is_bounded_and_expiry_progresses_without_inbound_messages():
    owner, buffer, clock, _, send = setup_owner(max_candidates=2)
    await owner.start(send)
    for i in range(9): owner.submit(str(i), ["005930"])
    assert len(owner._tasks) == 8
    assert not buffer.export()["complete"]
    await asyncio.gather(*owner._tasks)
    assert owner.snapshot()["leases"] == 2
    clock[0] += 601
    await owner.maintain()
    assert owner.snapshot()["leases"] == 0


@pytest.mark.parametrize("status", ["requested", "acknowledged", "observed", "unallocated", "connection_gap"])
def test_subscription_records_never_authorize_baseline_or_hide_gaps(status):
    from test_received_entry_shadow import observed_bundle
    from src.analytics.entry_observation import prepare_input
    context, observations, inputs = observed_bundle()
    records = observations["records"]
    records.append({"sequence": len(records) + 1, "kind": "quote_subscription", "status": status,
                    "candidate_id": "scan:MISSING", "symbol": "MISSING"})
    result = prepare_input(context, observations, inputs)
    assert len(result["payload"]["opportunities"]) == 2
    assert result["payload"]["opportunities"][1]["baseline_eligible"] is None
    if status == "connection_gap":
        assert result["capture_complete"] is False and result["ready_opportunities"] == 0
        assert result["report"]["summary"]["complete_delta_net_pnl"] is None


@pytest.mark.asyncio
async def test_unknown_unsubscribe_text_cannot_release_capacity():
    owner, _, clock, sent, send = setup_owner(cap=1)
    gen = await owner.start(send)
    key = (BOOK, "005930")
    await owner.enroll("scan", [key[1]])
    await owner.handle_ack(ack(key), gen)
    clock[0] += 601
    await owner.maintain()
    await owner.enroll("next", ["000660"])
    message = ack(key, "unsubscribe")
    message["body"]["msg1"] = "UNSUBSCRIBE UNKNOWN RESULT"
    await owner.handle_ack(message, gen)
    assert owner.snapshot()["occupied"] == 1 and len(sent) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("pause_at", ["token", "socket"])
async def test_disconnect_racing_connect_leaves_no_socket_registration_or_task(pause_at):
    f = make_feed()
    _, owner = install(f)
    entered, release = asyncio.Event(), asyncio.Event()
    obj, name = (f._token_manager, "get_approval_key") if pause_at == "token" else (f._session, "ws_connect")
    original = getattr(obj, name)
    async def delayed(*a, **kw):
        entered.set()
        await release.wait()
        return await original(*a, **kw)
    setattr(obj, name, delayed)
    connecting = asyncio.create_task(f.connect())
    await entered.wait()
    disconnecting = asyncio.create_task(f.disconnect())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(connecting, disconnecting)
    assert not f._connected and not f._running and not f._should_connect
    assert f._ws is None or f._ws.closed
    assert f._quote_maintenance_task is None and owner._send is None


def test_scanner_hook_failure_is_isolated_and_marks_buffer_incomplete():
    from types import SimpleNamespace
    from src.data.feeds.quote_subscription import observe_screen_candidates
    buffer = EntryObservationBuffer(evaluation_epoch="synthetic", capacity=10)
    def fail(*args): raise RuntimeError("synthetic enrollment failure")
    owner = SimpleNamespace(observer=buffer, submit=fail)
    observe_screen_candidates(SimpleNamespace(_quote_subscription_owner=owner), buffer,
                              "scan", [SimpleNamespace(symbol="005930")])
    assert buffer.export()["complete"] is False


def managed_bundle():
    from test_received_entry_shadow import observed_bundle
    context, observations, inputs = observed_bundle()
    rows = observations["records"]
    quote = rows.pop()
    for status in ("connected", "requested", "acknowledged", "observed"):
        rows.append({"kind": "quote_subscription", "status": status,
            "observed_at": "2026-09-30T10:00:00+09:00", "candidate_id": None,
            "generation": 1, "connection_id": "synthetic-connection",
            "tr_id": BOOK if status != "connected" else None,
            "symbol": "SYNTH1" if status != "connected" else None})
    quote["provenance"].update(generation=1, connection_id="synthetic-connection")
    rows.append(quote)
    for seq, row in enumerate(rows, 1): row["sequence"] = seq
    return context, observations, inputs


@pytest.mark.parametrize("defect", [None, "missing_ack", "missing_generation", "other_connection",
    "generation_bool", "unknown_status", "orphan_candidate", "withdrawn", "future_ack"])
def test_managed_report_requires_current_acknowledged_channel_provenance(defect):
    from src.analytics.entry_observation import prepare_input
    context, observations, inputs = managed_bundle()
    rows = observations["records"]
    if defect == "missing_ack": rows[:] = [r for r in rows if r.get("status") != "acknowledged"]
    elif defect == "missing_generation": rows[-1]["provenance"].pop("generation")
    elif defect == "other_connection": rows[-1]["provenance"]["connection_id"] = "other"
    elif defect == "generation_bool": rows[-1]["provenance"]["generation"] = True
    elif defect == "unknown_status": rows[-2]["status"] = "invented"
    elif defect == "orphan_candidate": rows[-2]["candidate_id"] = "orphan"
    elif defect == "withdrawn": rows[-2]["status"] = "unsubscribe_requested"
    elif defect == "future_ack": rows[-3]["observed_at"] = "2026-09-30T11:00:00+09:00"
    for seq, row in enumerate(rows, 1): row["sequence"] = seq
    result = prepare_input(context, observations, inputs)
    assert result["capture_complete"] is (defect is None)
    assert result["ready_opportunities"] == (1 if defect is None else 0)


@pytest.mark.asyncio
async def test_default_unsubscribe_contract_does_not_assume_unverified_success_literal():
    buffer = EntryObservationBuffer(evaluation_epoch="synthetic", capacity=100)
    clock = [0.0]
    owner = QuoteSubscriptionCoordinator(buffer, registration_cap=1, external_reserved=0,
        operational_headroom=0, max_candidates=1, lease_seconds=10, evidence_ref="synthetic",
        monotonic=lambda: clock[0], request_interval=0)
    sent = []
    async def send(action, key): sent.append((action, key))
    gen = await owner.start(send)
    await owner.enroll("a", ["005930"])
    await owner.handle_ack(ack((BOOK, "005930")), gen)
    clock[0] = 11
    await owner.maintain()
    message = ack((BOOK, "005930"), "unsubscribe")
    message["body"]["msg1"] = "UNSUBSCRIBE SUCCESS"
    await owner.handle_ack(message, gen)
    assert owner.snapshot()["occupied"] == 1
    assert owner.snapshot()["unsubscribe_ack_enabled"] is False


@pytest.mark.asyncio
async def test_expired_lease_cannot_accept_book_while_maintenance_waits():
    owner, _, clock, _, send = setup_owner()
    gen = await owner.start(send)
    await owner.enroll("scan", ["005930"])
    clock[0] += 601
    assert not owner.accepts((BOOK, "005930"), gen)


def price_frame(symbol="005930"):
    fields = ["0"] * 20
    fields[0], fields[1], fields[2], fields[3] = symbol, "100001", "10000", "2"
    return "0|H0STCNT0|001|" + "^".join(fields)


@pytest.mark.asyncio
async def test_price_callbacks_require_current_operational_owner_and_rest_requires_ack_and_data():
    import json
    f = make_feed()
    _, owner = install(f)
    received = []
    async def collect(event): received.append(event)
    f.on_market_data(collect)
    await owner.enroll("scan", ["005930"])
    await f.connect()
    ws, gen = f._ws, owner.generation
    await f._handle_message(price_frame(), socket=ws, generation=gen)
    assert not received and not f._subscribed_symbols
    await f.subscribe(["005930"])
    await f._handle_message(json.dumps(ack((PRICE, "005930"))), socket=ws, generation=gen)
    assert not f._subscribed_symbols
    await f._handle_message(price_frame(), socket=ws, generation=gen)
    assert len(received) == 1 and f._subscribed_symbols == {"005930"}
    await f._close_managed_socket(ws, gen)
    assert not f._subscribed_symbols
    await f.connect()
    await f._handle_message(price_frame(), socket=ws, generation=gen)
    assert len(received) == 1 and not f._subscribed_symbols
    await f.unsubscribe(["005930"])
    await f._handle_message(price_frame(), socket=f._ws, generation=owner.generation)
    assert len(received) == 1
    await f.disconnect()


@pytest.mark.asyncio
async def test_real_run_book_only_liveness_no_duplicate_subscribe_and_cleanup():
    import json
    import aiohttp
    from types import SimpleNamespace
    f = make_feed()
    _, owner = install(f)
    f.config.reconnect_delay = 0
    f._is_market_active = lambda: True
    f._kr_session = SimpleNamespace(get_session=lambda: f._current_session)
    invalidations = []
    f._token_manager.invalidate = lambda: invalidations.append(True)
    sockets = []
    class Socket:
        closed = False
        close_code = 1000
        def __init__(self):
            self.sent = []
            self.messages = [json.dumps(ack((BOOK, "005930"))), book_frame()]
        async def send_json(self, msg): self.sent.append(msg)
        async def close(self): self.closed = True
        def __aiter__(self): return self
        async def __anext__(self):
            if not self.messages:
                if len(sockets) == 3: f._running = False
                raise StopAsyncIteration
            return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=self.messages.pop(0))
    async def connect(*a, **kw):
        socket = Socket()
        sockets.append(socket)
        return socket
    f._session.ws_connect = connect
    await owner.enroll("scan", ["005930"])
    await f.run()
    assert len(sockets) == 3 and all(len(s.sent) == 1 and s.closed for s in sockets)
    assert not invalidations and f._managed_data_count == 3
    assert not f._subscribed_symbols and f._quote_maintenance_task is None


@pytest.mark.parametrize("defect", ["removed_subscription_records", "late_ack_after_withdraw", "late_ack_after_release"])
def test_managed_report_cannot_fall_back_to_legacy_or_revive_withdrawn_channel(defect):
    from copy import deepcopy
    from src.analytics.entry_observation import prepare_input
    context, observations, inputs = managed_bundle()
    rows = observations["records"]
    if defect == "removed_subscription_records":
        rows[:] = [r for r in rows if r["kind"] != "quote_subscription"]
    else:
        pending = deepcopy(rows[-3])
        pending["status"] = "unsubscribe_requested"
        rows.insert(-2, pending)
        if defect == "late_ack_after_release":
            released = deepcopy(pending); released["status"] = "released"
            rows.insert(-2, released)
        rows.insert(-2, deepcopy(rows[6]))  # 원래 subscribe ACK의 지연 중복
    for seq, row in enumerate(rows, 1): row["sequence"] = seq
    result = prepare_input(context, observations, inputs)
    assert result["capture_complete"] is False and result["ready_opportunities"] == 0


@pytest.mark.asyncio
async def test_real_capacity_rejection_preserves_candidate_symbol_and_valid_stream():
    from types import SimpleNamespace
    from src.analytics.entry_observation import capture_scan
    from src.analytics.received_entry_input import _subscription_stream_complete
    owner, buffer, _, _, send = setup_owner(max_candidates=1)
    sid = capture_scan(buffer, [SimpleNamespace(symbol=s) for s in ("005930", "000660")], "regular")
    await owner.start(send)
    await owner.enroll(sid, ["005930"])
    await owner.enroll(sid, ["000660"])
    records = buffer.export()["records"]
    assert _subscription_stream_complete(records, datetime.max.replace(tzinfo=timezone.utc))
    rejected = next(r for r in records if r.get("status") == "unallocated")
    assert rejected["candidate_id"] == f"{sid}:000660" and rejected["symbol"] == "000660"
