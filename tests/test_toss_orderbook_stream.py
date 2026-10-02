"""토스 호가 계약·유한 수신·후보 연결. 외부 소켓/자격증명 사용 없음."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib
import json

import pytest


START = datetime(2026, 10, 2, 1, tzinfo=timezone.utc)


def module():
    name = "src.data.providers.toss.orderbook_stream"
    assert importlib.util.find_spec(name) is not None, "토스 호가 수신 경로 미구현"
    return importlib.import_module(name)


def capture(**kw):
    return module().TossOrderbookCapture(
        symbols=["005930", "000660"], request_id="probe-1", evaluation_epoch="epoch-1",
        start_at=START, end_at=START + timedelta(seconds=120), **kw)


def ack(**kw):
    return {"type": "subscriptions", "id": "probe-1",
            "subscribed": ["orderbook:kr:005930", "orderbook:kr:000660"],
            "rejected": [], **kw}


def quote(symbol="005930", **kw):
    return {"type": "message", "topic": f"orderbook:kr:{symbol}", "data": {
        "timestamp": (START + timedelta(seconds=2)).isoformat(), "currency": "KRW",
        "asks": [{"price": "10000", "volume": "10"}],
        "bids": [{"price": "9990", "volume": "20"}], **kw}}


def feed(c, data, seconds):
    c.feed(json.dumps(data), received_at=START + timedelta(seconds=seconds))


def observed(c=None, **data):
    c = c or capture()
    feed(c, ack(), 1)
    feed(c, quote(**data), 2)
    return c


def test_declaration_is_fixed_market_only_and_ack_is_not_a_quote():
    c = capture()
    assert c.declaration() == [{"id": "probe-1"},
        {"type": "orderbook:kr", "codes": ["005930", "000660"]}]
    feed(c, ack(), 1)
    result = c.export()
    assert result["acknowledged_symbols"] == ["005930", "000660"]
    assert result["records"] == []
    assert result["stream_complete"] is None


def test_decimal_book_has_explicit_consolidated_lossy_provenance_and_no_raw_fields():
    c = observed()
    r = c.export()["records"][0]
    assert (r["ask"], r["bid"], r["ask_size"], r["bid_size"]) == ("10000", "9990", "10", "20")
    assert r["market_basis"] == "KRX_NXT_CONSOLIDATED"
    assert r["source"] == "TOSS_WS_ORDERBOOK_KR"
    assert r["quality_issues"] == []
    assert r["source_sequence"] is None
    assert r["received_index"] == 2
    assert r["kis_executable"] is False
    assert "tr_id" not in r
    changed = c.export()
    changed["records"][0]["ask"] = "1"
    assert c.export()["records"][0]["ask"] == "10000"


@pytest.mark.parametrize("timestamp,reason", [
    (None, "source_time_missing"), ("2026-10-02T01:00:02", "source_time_invalid"),
    ((START - timedelta(seconds=1)).isoformat(), "source_time_stale"),
    ((START + timedelta(seconds=3)).isoformat(), "source_time_future")])
def test_missing_stale_or_future_source_time_is_retained_as_unknown(timestamp, reason):
    c = observed(timestamp=timestamp)
    assert reason in c.export()["records"][0]["quality_issues"]


@pytest.mark.parametrize("data", [
    {"currency": "USD"}, {"asks": []}, {"bids": []},
    {"asks": [{"price": "NaN", "volume": "1"}]},
    {"asks": [{"price": 10000, "volume": "1"}]},
    {"asks": [{"price": "1e999999", "volume": "1"}]},
    {"asks": [{"price": "10000", "volume": "0"}]},
    {"asks": [{"price": "10000", "volume": "-1"}]},
    {"asks": [{"price": "10001", "volume": "1"}, {"price": "10000", "volume": "1"}]},
    {"bids": [{"price": "10010", "volume": "1"}]},
])
def test_invalid_book_is_not_skipped_as_if_the_next_quote_were_first(data):
    c = observed(**data)
    feed(c, quote(timestamp=(START + timedelta(seconds=3)).isoformat()), 3)
    rows = c.export()["records"]
    assert len(rows) == 2
    assert rows[0]["quality_issues"]
    assert rows[1]["quality_issues"] == []


@pytest.mark.parametrize("payload", [ack(id="other"), ack(subscribed=[]),
    ack(subscribed=["trade:kr:005930"]),
    ack(subscribed=["orderbook:kr:005930"] * 2)])
def test_invalid_or_uncorrelated_ack_stops_without_confirming_subscription(payload):
    c = capture()
    feed(c, payload, 1)
    assert c.export()["stop_reason"] == "invalid_ack"
    assert c.export()["acknowledged_symbols"] == []


def test_partial_rejection_keeps_denominator_and_does_not_retry_or_log_message():
    c = capture()
    feed(c, ack(subscribed=["orderbook:kr:005930"], rejected=[{
        "target": "orderbook:kr:000660", "code": "stock-not-found", "message": "private-data"}]), 1)
    feed(c, quote(), 2)
    result = c.export()
    assert result["rejected_symbols"] == ["000660"]
    assert result["acknowledged_symbols"] == ["005930"]
    assert len(result["records"]) == 1
    assert "private-data" not in json.dumps(result)


def test_data_before_ack_or_from_unrequested_topic_is_a_visible_capture_failure():
    c = capture()
    feed(c, quote(), 1)
    assert c.export()["stop_reason"] == "unacknowledged_topic"
    c = observed()
    feed(c, quote(symbol="035420"), 3)
    assert c.export()["stop_reason"] == "unacknowledged_topic"


def test_message_budget_counts_pongs_and_late_or_reversed_clock_stops():
    c = capture(max_frames=2)
    feed(c, ack(), 1)
    feed(c, {"type": "pong"}, 2)
    assert c.export()["stop_reason"] == "frame_limit"
    assert c.export()["records"] == []
    c = observed()
    feed(c, quote(), 1)
    assert c.export()["stop_reason"] == "receive_time_reversed"
    c = capture()
    feed(c, ack(), 120)
    assert c.export()["stop_reason"] == "window_ended"
    assert c.export()["acknowledged_symbols"] == []


def test_duplicate_source_timestamp_is_visible_without_claiming_full_tick_sequence():
    c = observed()
    feed(c, quote(), 2.2)
    assert "source_time_not_increasing" in c.export()["records"][1]["quality_issues"]


def test_error_and_oversize_frames_close_with_sanitized_reason():
    c = capture()
    feed(c, {"type": "error", "error": {"code": "server-shutdown", "message": "private-data"}}, 1)
    assert c.export()["stop_reason"] == "server_error"
    assert "private-data" not in json.dumps(c.export())
    c = capture()
    c.feed("x" * 65537, received_at=START)
    assert c.export()["stop_reason"] == "frame_too_large"


class Socket:
    """외부 I/O만 대체하고 수신 상태/ACK 처리는 실제 코드를 통과한다."""
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []
        self.closed = False

    async def send_str(self, text):
        self.sent.append(text)

    async def receive_str(self):
        if not self.frames:
            raise ConnectionError("never expose transport details")
        return json.dumps(self.frames.pop(0))

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_injected_socket_run_declares_once_closes_and_records_disconnect():
    c = capture()
    ws = Socket([ack(), quote()])
    await module().receive_orderbooks(ws, c, exclusive=True, now=lambda: START + timedelta(seconds=2))
    result = c.export()
    assert len(result["records"]) == 1
    assert result["stop_reason"] == "transport_error"
    assert ws.closed
    assert json.loads(ws.sent[0]) == c.declaration()
    assert len(ws.sent) == 1
    assert "never expose" not in json.dumps(result)


@pytest.mark.asyncio
async def test_shared_socket_is_rejected_before_any_send_or_close():
    ws = Socket([])
    with pytest.raises(ValueError):
        await module().receive_orderbooks(ws, capture(), exclusive=False, now=lambda: START)
    assert ws.sent == [] and not ws.closed


@pytest.mark.asyncio
async def test_cancellation_closes_owned_socket_and_preserves_cancel():
    ws = Socket([])
    async def cancelled():
        raise asyncio.CancelledError()
    ws.receive_str = cancelled
    c = capture()
    with pytest.raises(asyncio.CancelledError):
        await module().receive_orderbooks(ws, c, exclusive=True, now=lambda: START)
    assert c.export()["stop_reason"] == "cancelled" and ws.closed


def engine_records():
    return {"schema_version": 1, "evaluation_epoch": "epoch-1", "complete": True,
        "dropped_records": 0, "incomplete_reasons": [], "records": [
        {"kind": "scan", "sequence": 1, "scan_id": "scan-1", "observed_at": START.isoformat(),
         "route_origin": "live_screening", "population_scope": "returned_screen_candidates",
         "candidates": [{"candidate_id": "scan-1:005930", "symbol": "005930"},
                        {"candidate_id": "scan-1:000660", "symbol": "000660"}]},
        {"kind": "signal", "sequence": 2, "candidate_id": "scan-1:005930", "signal_id": "signal-1",
         "observed_at": (START + timedelta(seconds=0.5)).isoformat()},
        {"kind": "order_ready", "sequence": 3, "signal_id": "signal-1", "symbol": "005930",
         "order_id": "order-1", "requested_quantity": 3,
         "observed_at": (START + timedelta(seconds=1)).isoformat()}]}


def report(engine=None, c=None):
    name = "src.analytics.toss_candidate_observation"
    assert importlib.util.find_spec(name) is not None, "후보별 토스 관측 연결 미구현"
    c = c or observed()
    c.stop("window_ended")
    return importlib.import_module(name).build_toss_candidate_report(
        engine_records() if engine is None else engine, c.export(), as_of=START + timedelta(seconds=121))


def test_report_joins_exact_ids_keeps_unselected_candidate_and_never_computes_pnl():
    r = report()
    assert len(r["candidates"]) == 2
    row, missing = r["candidates"]
    assert row["candidate_id"] == "scan-1:005930"
    assert row["requested_quantity"] == 3
    assert row["decision_at"] == (START + timedelta(seconds=1)).isoformat()
    assert row["first_after_decision"]["spread_bps"] == "10"
    assert row["first_after_decision"]["received_delay_ms"] == "1000"
    assert row["first_after_decision"]["status"] == "observed_snapshot"
    assert missing["decision_at"] is None and missing["first_after_scan"]["status"] == "unknown"
    assert missing["engine_stage"] == "no_signal_observed"
    assert r["profit_comparison_available"] is False
    assert r["kis_execution_evidence"] is False


def test_first_bad_quote_is_unknown_even_if_later_quote_is_good():
    c = observed(timestamp=None)
    feed(c, quote(timestamp=(START + timedelta(seconds=3)).isoformat()), 3)
    row = report(c=c)["candidates"][0]
    assert row["first_after_decision"]["status"] == "unknown"
    assert "source_time_missing" in row["first_after_decision"]["reasons"]


@pytest.mark.parametrize("mutate", [
    lambda e: e.update(evaluation_epoch="other"),
    lambda e: e["records"][0]["candidates"][0].update(candidate_id="wrong:005930"),
    lambda e: e["records"].append(deepcopy(e["records"][0])),
    lambda e: e["records"][2].update(symbol="000660"),
    lambda e: e["records"][2].update(requested_quantity=True),
    lambda e: e["records"][2].update(observed_at=(START - timedelta(seconds=1)).isoformat()),
])
def test_mismatched_identity_duplicate_population_or_decision_is_rejected(mutate):
    e = engine_records()
    mutate(e)
    with pytest.raises(ValueError):
        report(e)


def test_missing_engine_records_and_capture_gap_prevent_usable_comparison():
    e = engine_records()
    e["complete"] = False
    row = report(e)["candidates"][0]
    assert row["first_after_decision"]["status"] == "unknown"
    c = observed()
    c.stop("transport_error")
    row = report(c=c)["candidates"][0]
    assert row["first_after_decision"]["status"] == "unknown"


def test_report_accepts_terminal_frame_limit_before_planned_end_and_reports_coverage():
    c = observed(capture(max_frames=2))
    assert c.export()["stop_reason"] == "frame_limit"

    result = importlib.import_module("src.analytics.toss_candidate_observation").build_toss_candidate_report(
        engine_records(), c.export(), as_of=START + timedelta(seconds=3))

    assert result["coverage"] == {
        "planned_start_at": START.isoformat(),
        "planned_end_at": (START + timedelta(seconds=120)).isoformat(),
        "reported_as_of": (START + timedelta(seconds=3)).isoformat(),
        "last_received_quote_at": (START + timedelta(seconds=2)).isoformat(),
        "stop_reason": "frame_limit",
        "received_frames": 2,
        "max_frames": 2,
        "quote_count": 1,
        "cleanup_failed": False,
    }
    assert "toss_capture_incomplete" in result["candidates"][0]["first_after_decision"]["reasons"]
    assert result["profit_comparison_available"] is False
    assert result["kis_execution_evidence"] is False


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(acknowledged_at=(START + timedelta(seconds=4)).isoformat()),
    lambda value: value["records"][0].update(received_at=(START + timedelta(seconds=4)).isoformat()),
])
def test_report_rejects_ack_or_quote_observed_after_early_report_time(mutate):
    c = observed(capture(max_frames=2))
    data = c.export()
    mutate(data)

    with pytest.raises(ValueError):
        importlib.import_module("src.analytics.toss_candidate_observation").build_toss_candidate_report(
            engine_records(), data, as_of=START + timedelta(seconds=3))


def test_report_requires_frame_limit_to_match_the_frame_budget():
    c = capture(max_frames=1)
    feed(c, ack(), 1)
    data = c.export()
    assert data["stop_reason"] == "frame_limit"
    data["received_frames"] = 0

    with pytest.raises(ValueError):
        importlib.import_module("src.analytics.toss_candidate_observation").build_toss_candidate_report(
            engine_records(), data, as_of=START + timedelta(seconds=3))


@pytest.mark.parametrize("stop_reason", [None, "window_ended"])
def test_report_rejects_nonterminal_or_claimed_window_end_before_planned_end(stop_reason):
    c = observed()
    if stop_reason is None:
        c = capture()
    else:
        c.stop(stop_reason)

    with pytest.raises(ValueError):
        importlib.import_module("src.analytics.toss_candidate_observation").build_toss_candidate_report(
            engine_records(), c.export(), as_of=START + timedelta(seconds=3))


@pytest.mark.parametrize("symbols", [[], ["005930"] * 2, ["5930"], ["005930", "000660", "035420", "035720"]])
def test_invalid_cohort_never_silently_truncates(symbols):
    with pytest.raises(ValueError):
        module().TossOrderbookCapture(symbols=symbols, request_id="probe", evaluation_epoch="epoch",
                                     start_at=START, end_at=START + timedelta(seconds=60))


def test_json_duplicate_keys_are_not_last_value_wins():
    c = capture()
    c.feed('{"type":"error","type":"pong"}', received_at=START)
    assert c.export()["stop_reason"] == "invalid_frame"


def test_report_rejects_engine_cohort_from_outside_capture_window():
    e = engine_records()
    e["records"][0]["observed_at"] = (START - timedelta(days=1)).isoformat()
    with pytest.raises(ValueError):
        report(e)


def test_report_rechecks_quote_time_instead_of_trusting_empty_quality_flags():
    c = observed()
    c.stop("window_ended")
    exported = c.export()
    exported["records"][0]["source_as_of"] = (START + timedelta(seconds=3)).isoformat()
    m = importlib.import_module("src.analytics.toss_candidate_observation")
    with pytest.raises(ValueError):
        m.build_toss_candidate_report(engine_records(), exported, as_of=START + timedelta(seconds=121))


@pytest.mark.asyncio
async def test_used_capture_cannot_redeclare_a_new_session_into_the_same_dataset():
    c = observed()
    ws = Socket([])
    with pytest.raises(ValueError):
        await module().receive_orderbooks(ws, c, exclusive=True, now=lambda: START + timedelta(seconds=3))
    assert ws.sent == [] and not ws.closed


@pytest.mark.asyncio
async def test_expired_window_closes_owned_socket_without_subscribing():
    c = capture()
    ws = Socket([])
    await module().receive_orderbooks(ws, c, exclusive=True, now=lambda: START + timedelta(seconds=121))
    assert ws.sent == [] and ws.closed
    assert c.export()["stop_reason"] == "window_ended"


def test_cli_builds_report_from_explicit_files_without_modifying_inputs(tmp_path, capsys):
    script = "scripts.report_toss_candidate_observation"
    assert importlib.util.find_spec(script) is not None, "파일 기반 후보 품질 보고서 미구현"
    c = observed()
    c.stop("window_ended")
    path = tmp_path / "inputs.json"
    raw = json.dumps({"engine": engine_records(), "toss": c.export(),
                      "as_of": (START + timedelta(seconds=121)).isoformat()})
    path.write_text(raw)
    result = importlib.import_module(script).main(["--input", str(path)])
    assert result == 0
    report_data = json.loads(capsys.readouterr().out)
    assert len(report_data["candidates"]) == 2
    assert report_data["profit_comparison_available"] is False
    assert path.read_text() == raw


@pytest.mark.asyncio
async def test_cancellation_arriving_during_close_is_not_swallowed():
    started = asyncio.Event()
    ws = Socket([])
    async def close():
        started.set()
        await asyncio.Event().wait()
    ws.close = close
    c = capture()
    task = asyncio.create_task(module().receive_orderbooks(
        ws, c, exclusive=True, now=lambda: START + timedelta(seconds=121)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_capture_is_claimed_before_first_await():
    started = asyncio.Event()
    ws = Socket([])
    async def send(text):
        started.set()
        await asyncio.Event().wait()
    ws.send_str = send
    c = capture()
    task = asyncio.create_task(module().receive_orderbooks(ws, c, exclusive=True, now=lambda: START))
    await started.wait()
    second = Socket([])
    try:
        with pytest.raises(ValueError):
            await module().receive_orderbooks(second, c, exclusive=True, now=lambda: START)
        assert second.sent == [] and not second.closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("value", [True, 1, "NaN", "1e999999", "9" * 1000])
def test_replay_invalid_price_or_size_cannot_be_a_usable_snapshot(value):
    c = observed()
    c.stop("window_ended")
    data = c.export()
    data["records"][0].update(ask=value, bid=value, ask_size=value, bid_size=value)
    m = importlib.import_module("src.analytics.toss_candidate_observation")
    row = m.build_toss_candidate_report(engine_records(), data, as_of=START + timedelta(seconds=121))["candidates"][0]
    assert row["first_after_decision"]["status"] == "unknown"


@pytest.mark.asyncio
async def test_heartbeat_send_time_is_deducted_from_remaining_receive_budget(monkeypatch):
    # 시계와 외부 비동기 대기만 대체한다. 선언/heartbeat/수신 루프는 실제 코드다.
    clock = [0.0]
    waits = []
    c = module().TossOrderbookCapture(symbols=["005930", "000660"], request_id="probe-1",
        evaluation_epoch="epoch-1", start_at=START, end_at=START + timedelta(seconds=65))
    ws = Socket([])
    calls = [0]
    async def send(text):
        ws.sent.append(text)
        if text == "PING":
            clock[0] += 4
    async def receive():
        calls[0] += 1
        if calls[0] == 1:
            clock[0] = 59
            return json.dumps(ack())
        if calls[0] == 2:
            clock[0] = 60
            return json.dumps(quote(timestamp=(START + timedelta(seconds=60)).isoformat()))
        raise ConnectionError()
    async def bounded(coroutine, *, timeout):
        if coroutine.cr_code.co_name == "receive":
            waits.append((clock[0], timeout))
        return await coroutine
    ws.send_str, ws.receive_str = send, receive
    monkeypatch.setattr(module().asyncio, "wait_for", bounded)
    await module().receive_orderbooks(ws, c, exclusive=True,
        now=lambda: START + timedelta(seconds=clock[0]), monotonic=lambda: clock[0])
    assert waits[-1] == (64, 1)
    assert ws.sent[1] == "PING"


def test_leap_day_capture_uses_real_previous_timestamp_only(monkeypatch):
    import sys
    monkeypatch.setattr(sys.modules[__name__], "START", datetime(2028, 2, 29, 1, tzinfo=timezone.utc))
    assert report()["candidates"][0]["first_after_decision"]["status"] == "observed_snapshot"


def test_replay_enforces_same_one_hour_window_limit_as_collector():
    c = observed()
    c.stop("window_ended")
    data = c.export()
    data["end_at"] = (START + timedelta(hours=2)).isoformat()
    m = importlib.import_module("src.analytics.toss_candidate_observation")
    with pytest.raises(ValueError):
        m.build_toss_candidate_report(engine_records(), data, as_of=START + timedelta(hours=3))
