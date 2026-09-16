from __future__ import annotations

import asyncio
import pytest


def valid(date="2026-09-16"):
    def integrated(day):
        at = lambda clock: f"{day}T{clock}:00+09:00"
        return {"preMarket": {"startTime": at("08:00"), "singlePriceAuctionStartTime": at("08:20"), "endTime": at("08:30")},
                "regularMarket": {"startTime": at("09:00"), "singlePriceAuctionStartTime": at("09:00"), "endTime": at("15:30")},
                "afterMarket": {"startTime": at("15:40"), "singlePriceAuctionEndTime": at("18:00"), "endTime": at("18:30")}}
    return {"result": {"today": {"date": date, "integrated": integrated(date)},
        "previousBusinessDay": {"date": "2026-09-15", "integrated": integrated("2026-09-15")},
        "nextBusinessDay": {"date": "2026-09-17", "integrated": integrated("2026-09-17")}}}


def test_calendar_accepts_explicit_integrated_null_as_holiday():
    from src.data.providers.toss.calendar_observation import validate_calendar
    body = valid(); body["result"]["today"]["integrated"] = None
    answer = validate_calendar(body, requested_date="2026-09-16")
    assert answer["holiday"] is True and answer["requested_date"] == "2026-09-16"


def test_calendar_rejects_wrong_date_empty_and_invalid_session_order():
    from src.data.providers.toss.calendar_observation import CalendarError, validate_calendar
    import pytest
    with pytest.raises(CalendarError, match="calendar_invalid"):
        validate_calendar({"result": {}}, requested_date="2026-09-16")
    with pytest.raises(CalendarError, match="calendar_invalid"):
        validate_calendar(valid("2026-09-15"), requested_date="2026-09-16")
    body = valid(); body["result"]["today"]["integrated"]["regularMarket"]["endTime"] = "08:00"
    with pytest.raises(CalendarError, match="calendar_invalid"):
        validate_calendar(body, requested_date="2026-09-16")


def test_calendar_runner_queries_exact_path_and_wrong_date_never_succeeds(tmp_path):
    from src.data.providers.toss.calendar_observation import CalendarObservationRunner
    from src.data.providers.toss.observation_ledger import ObservationLedger
    class Client:
        def __init__(self): self.calls = []
        async def get(self, path, *, params, budget): self.calls.append((path, params)); return valid("2026-09-15")
    ledger = ObservationLedger(tmp_path / "ledger", plan_hash="a" * 64, max_bytes=100_000); ledger.open()
    client = Client()
    result = asyncio.run(CalendarObservationRunner(client=client, ledger=ledger,
        policy={"limits": {"job_timeout_seconds": 5, "max_retries": 1, "max_pages": 1}}).calendar(slot_id="slot", requested_date="2026-09-16"))
    assert client.calls == [("/api/v1/market-calendar/KR", {"date": "2026-09-16"})]
    assert result.outcome == "failure" and result.requested_date == "2026-09-16"


def test_official_aware_business_day_is_valid():
    from src.data.providers.toss.calendar_observation import validate_calendar
    result = validate_calendar(valid(), requested_date="2026-09-16")
    assert result == {"requested_date": "2026-09-16", "holiday": False, "production_eligible": False}


@pytest.mark.parametrize("form", ["missing", "null"])
def test_official_optional_auction_boundaries_are_not_invented(form):
    from src.data.providers.toss.calendar_observation import validate_calendar
    body = valid()
    for day in body["result"].values():
        for name, session in day["integrated"].items():
            field = "singlePriceAuctionEndTime" if name == "afterMarket" else "singlePriceAuctionStartTime"
            if form == "missing": del session[field]
            else: session[field] = None
    assert validate_calendar(body, requested_date="2026-09-16")["holiday"] is False


@pytest.mark.parametrize("bad", ["09:00", "2026-09-16T09:00:00", "2026-09-16T09:00:00+00:00",
    "2026-09-15T09:00:00+09:00", "20260916T090000+09:00", "2026-09-16T09:00:00+08:00"])
def test_wrong_session_clock_contract_is_rejected(bad):
    from src.data.providers.toss.calendar_observation import CalendarError, validate_calendar
    body = valid()
    body["result"]["today"]["integrated"]["regularMarket"]["startTime"] = bad
    with pytest.raises(CalendarError): validate_calendar(body, requested_date="2026-09-16")


@pytest.mark.parametrize("mutation", ["overlap", "null_neighbors", "all_null", "missing_integrated", "compact_date"])
def test_holiday_and_inter_session_structure_is_strict(mutation):
    from src.data.providers.toss.calendar_observation import CalendarError, validate_calendar
    body = valid()
    today = body["result"]["today"]
    if mutation == "overlap":
        today["integrated"]["preMarket"]["endTime"] = "2026-09-16T10:00:00+09:00"
    elif mutation == "null_neighbors": body["result"]["previousBusinessDay"]["integrated"] = None
    elif mutation == "all_null": today["integrated"] = dict.fromkeys(("preMarket", "regularMarket", "afterMarket"))
    elif mutation == "missing_integrated": del today["integrated"]
    else: today["date"] = "20260916"
    with pytest.raises(CalendarError): validate_calendar(body, requested_date="2026-09-16")


@pytest.mark.parametrize("error,reason", [("timeout", "budget_skip"), ("page_exhausted", "budget_skip"),
    ("circuit_open", "provider_failure"), ("private", "provider_failure"), ("invalid", "calendar_invalid")])
def test_calendar_failures_retain_exclusive_classification(tmp_path, error, reason):
    from src.data.providers.toss.calendar_observation import CalendarObservationRunner
    from src.data.providers.toss.observation_ledger import ObservationLedger
    from src.data.providers.toss.transport import TossRequestError
    class Client:
        async def get(self, *args, **kwargs):
            if error == "invalid": return {"result": {}}
            if error == "private": raise RuntimeError("private transport")
            raise TossRequestError(error)
    ledger = ObservationLedger(tmp_path / "ledger", plan_hash="a" * 64, max_bytes=100000)
    ledger.open()
    result = asyncio.run(CalendarObservationRunner(client=Client(), ledger=ledger,
        policy={"limits": {"job_timeout_seconds": 5, "max_retries": 1, "max_pages": 1}}).calendar(slot_id="s", requested_date="2026-09-16"))
    assert result.outcome == "failure" and result.reason == reason and result.ledger_complete
    assert ledger.summary()["terminal_reasons"][reason] == 1


def test_calendar_deadline_includes_fsync(tmp_path, monkeypatch):
    from src.data.providers.toss.calendar_observation import CalendarObservationRunner
    from src.data.providers.toss.observation_ledger import ObservationLedger
    elapsed, calls = [0], []
    class Client:
        async def get(self, *args, **kwargs): calls.append(1); return valid()
    ledger = ObservationLedger(tmp_path / "ledger", plan_hash="a" * 64, max_bytes=100000)
    ledger.open()
    reserve = ledger.reserve_slot
    def slow(*args, **kwargs):
        result = reserve(*args, **kwargs); elapsed[0] += 6; return result
    monkeypatch.setattr(ledger, "reserve_slot", slow)
    result = asyncio.run(CalendarObservationRunner(client=Client(), ledger=ledger, clock=lambda: elapsed[0],
        policy={"limits": {"job_timeout_seconds": 5, "max_retries": 1, "max_pages": 1}}).calendar(slot_id="s", requested_date="2026-09-16"))
    assert calls == [] and result.reason == "budget_skip" and result.degraded


@pytest.mark.parametrize("action", ["cancel", "terminal_ack", "midnight"])
def test_calendar_cancel_ack_and_midnight_keep_requested_day_authoritative(tmp_path, monkeypatch, action):
    import os
    from datetime import datetime, timedelta, timezone
    from src.data.providers.toss.calendar_observation import CalendarObservationRunner
    from src.data.providers.toss.observation_ledger import ObservationLedger
    now = [datetime(2026, 9, 16, 23, 59, 59, tzinfo=timezone(timedelta(hours=9)))]
    class Client:
        async def get(self, *args, **kwargs):
            if action == "cancel": raise asyncio.CancelledError()
            now[0] += timedelta(seconds=2)
            return valid()
    ledger = ObservationLedger(tmp_path / "ledger", plan_hash="a" * 64, max_bytes=100000)
    ledger.open()
    real_sync, calls = os.fsync, [0]
    if action == "terminal_ack":
        def sync(fd):
            calls[0] += 1; real_sync(fd)
            if calls[0] == 3: raise OSError("durable ACK lost")
        monkeypatch.setattr(os, "fsync", sync)
    runner = CalendarObservationRunner(client=Client(), ledger=ledger, now=lambda: now[0],
        policy={"limits": {"job_timeout_seconds": 5, "max_retries": 1, "max_pages": 1}})
    if action == "cancel":
        with pytest.raises(asyncio.CancelledError): asyncio.run(runner.calendar(slot_id="s", requested_date="2026-09-16"))
        assert ledger.summary()["cancelled"] == 1
    else:
        result = asyncio.run(runner.calendar(slot_id="s", requested_date="2026-09-16"))
        assert result.requested_date == "2026-09-16"
        assert result.outcome == ("failure" if action == "terminal_ack" else "success")
        assert result.ledger_complete is (action != "terminal_ack")
