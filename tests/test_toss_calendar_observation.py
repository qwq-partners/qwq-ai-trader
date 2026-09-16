from __future__ import annotations

import asyncio


def valid(date="2026-09-16"):
    def integrated():
        return {"preMarket": {"startTime": "08:00", "singlePriceAuctionStartTime": "08:20", "endTime": "08:30"},
                "regularMarket": {"startTime": "09:00", "singlePriceAuctionStartTime": "09:00", "endTime": "15:30"},
                "afterMarket": {"startTime": "15:40", "singlePriceAuctionEndTime": "18:00", "endTime": "18:30"}}
    return {"result": {"today": {"date": date, "integrated": integrated()},
        "previousBusinessDay": {"date": "2026-09-15", "integrated": integrated()},
        "nextBusinessDay": {"date": "2026-09-17", "integrated": integrated()}}}


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
