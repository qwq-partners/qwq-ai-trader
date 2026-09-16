"""Strict Toss KR market-calendar observer; it never changes market state."""
from __future__ import annotations

import asyncio
from datetime import date
import time
from typing import Mapping

from .observation import ObservationResult
from .rate_limit import RequestBudget


class CalendarError(Exception):
    def __init__(self, code="calendar_invalid"): self.code = code; super().__init__(code)


def _clock(value):
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":": raise CalendarError()
    try: hour, minute = map(int, value.split(":"))
    except ValueError: raise CalendarError() from None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59: raise CalendarError()
    return hour * 60 + minute


def _session(value, name):
    if value is None: return
    if not isinstance(value, Mapping) or set(value) != ({"startTime", "singlePriceAuctionStartTime", "endTime"} if name != "afterMarket" else {"startTime", "singlePriceAuctionEndTime", "endTime"}): raise CalendarError()
    auction = value.get("singlePriceAuctionEndTime", value.get("singlePriceAuctionStartTime"))
    if not _clock(value["startTime"]) <= _clock(auction) <= _clock(value["endTime"]): raise CalendarError()


def _day(value):
    if not isinstance(value, Mapping) or set(value) != {"date", "integrated"} or not isinstance(value["date"], str): raise CalendarError()
    try: parsed = date.fromisoformat(value["date"])
    except ValueError: raise CalendarError() from None
    integrated = value["integrated"]
    if integrated is None: return parsed, True
    if not isinstance(integrated, Mapping) or set(integrated) != {"preMarket", "regularMarket", "afterMarket"} or all(v is None for v in integrated.values()): raise CalendarError()
    for name, session in integrated.items(): _session(session, name)
    return parsed, False


def validate_calendar(body, *, requested_date: str):
    if not isinstance(body, Mapping) or set(body) != {"result"} or not isinstance(body["result"], Mapping) or set(body["result"]) != {"today", "previousBusinessDay", "nextBusinessDay"}: raise CalendarError()
    today, holiday = _day(body["result"]["today"]); previous, previous_holiday = _day(body["result"]["previousBusinessDay"]); following, following_holiday = _day(body["result"]["nextBusinessDay"])
    if today.isoformat() != requested_date or not previous < today < following or previous_holiday or following_holiday: raise CalendarError()
    return {"requested_date": requested_date, "holiday": holiday, "production_eligible": False}


class CalendarObservationRunner:
    def __init__(self, *, client, ledger, policy, clock=time.monotonic, now=None): self.client, self.ledger, self.policy, self.clock = client, ledger, policy, clock
    async def calendar(self, *, slot_id, requested_date):
        snap = {"snapshot_id": f"calendar:{requested_date}", "selected_at": requested_date, "source_success_at": None, "selection_partial": False, "symbols": []}
        attempts = self.ledger.reserve_slot(slot_id, kind="calendar", snapshot=snap)
        if attempts is None: return ObservationResult("duplicate", False, 0, 0, not self.ledger.summary()["incomplete"], "duplicate", requested_date)
        attempt, = attempts; self.ledger.begin(attempt)
        limits = self.policy["limits"]; budget = RequestBudget(limits["job_timeout_seconds"], clock=self.clock, max_retries=limits["max_retries"], max_pages=limits["max_pages"])
        try:
            body = await self.client.get("/api/v1/market-calendar/KR", params={"date": requested_date}, budget=budget)
            answer = validate_calendar(body, requested_date=requested_date)
            self.ledger.finish(attempt, reason="success", observation={"holiday": answer["holiday"], "requested_date": requested_date})
            return ObservationResult("success", False, 1, 0, not self.ledger.summary()["incomplete"], "holiday" if answer["holiday"] else "ok", requested_date)
        except asyncio.CancelledError:
            self.ledger.finish(attempt, reason="cancelled")
            raise
        except Exception:
            self.ledger.finish(attempt, reason="calendar_invalid")
            return ObservationResult("failure", False, 0, 0, not self.ledger.summary()["incomplete"], "calendar_invalid", requested_date)
