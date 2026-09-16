"""Strict Toss KR market-calendar observer; it never changes market state."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
import re
import time
from typing import Mapping

from .observation import ObservationResult, _request_reason, _cohort
from .observation_ledger import LedgerError
from .rate_limit import RequestBudget


class CalendarError(Exception):
    def __init__(self, code="calendar_invalid"): self.code = code; super().__init__(code)


def _clock(value, day):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\+09:00", value):
        raise CalendarError()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise CalendarError() from None
    if parsed.utcoffset() != timedelta(hours=9) or parsed.date() != day:
        raise CalendarError()
    return parsed


def _session(value, name, day):
    if value is None: return
    auction_field = "singlePriceAuctionEndTime" if name == "afterMarket" else "singlePriceAuctionStartTime"
    if not isinstance(value, Mapping) or not {"startTime", "endTime"} <= set(value) or set(value) - {"startTime", "endTime", auction_field}: raise CalendarError()
    auction = value.get(auction_field)
    start, end = _clock(value["startTime"], day), _clock(value["endTime"], day)
    if start >= end or (auction is not None and not start <= _clock(auction, day) <= end): raise CalendarError()
    return start, end


def _day(value):
    if not isinstance(value, Mapping) or set(value) != {"date", "integrated"} or not isinstance(value["date"], str): raise CalendarError()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value["date"]): raise CalendarError()
    try: parsed = date.fromisoformat(value["date"])
    except ValueError: raise CalendarError() from None
    integrated = value["integrated"]
    if integrated is None: return parsed, True
    if not isinstance(integrated, Mapping) or set(integrated) != {"preMarket", "regularMarket", "afterMarket"} or all(v is None for v in integrated.values()): raise CalendarError()
    previous_end = None
    for name in ("preMarket", "regularMarket", "afterMarket"):
        interval = _session(integrated[name], name, parsed)
        if interval is not None:
            if previous_end is not None and interval[0] < previous_end: raise CalendarError()
            previous_end = interval[1]
    return parsed, False


def validate_calendar(body, *, requested_date: str):
    if not isinstance(body, Mapping) or set(body) != {"result"} or not isinstance(body["result"], Mapping) or set(body["result"]) != {"today", "previousBusinessDay", "nextBusinessDay"}: raise CalendarError()
    today, holiday = _day(body["result"]["today"]); previous, previous_holiday = _day(body["result"]["previousBusinessDay"]); following, following_holiday = _day(body["result"]["nextBusinessDay"])
    if today.isoformat() != requested_date or not previous < today < following or previous_holiday or following_holiday: raise CalendarError()
    return {"requested_date": requested_date, "holiday": holiday, "production_eligible": False}


class CalendarObservationRunner:
    def __init__(self, *, client, ledger, policy, clock=time.monotonic, now=None):
        self.client, self.ledger, self.policy, self.clock = client, ledger, policy, clock
        self.now = now or (lambda: datetime.now(timezone.utc))
    async def calendar(self, *, slot_id, requested_date):
        limits = self.policy["limits"]; budget = RequestBudget(limits["job_timeout_seconds"], clock=self.clock, max_retries=limits["max_retries"], max_pages=limits["max_pages"])
        now = self.now()
        snap = {"snapshot_id": f"calendar:{requested_date}", "selected_at": now.isoformat(), "source_success_at": None,
                "selection_partial": False, "symbols": [], "cohort": _cohort(self.policy, now, calendar=True)}
        attempt = None
        try:
            attempts = self.ledger.reserve_slot(slot_id, kind="calendar", snapshot=snap)
            if attempts is None: return ObservationResult("duplicate", False, 0, 0, not self.ledger.summary()["incomplete"], "duplicate", requested_date)
            attempt, = attempts
            self.ledger.begin(attempt)
            try:
                budget.remaining()
                body = await self.client.get("/api/v1/market-calendar/KR", params={"date": requested_date}, budget=budget)
                budget.remaining()
                answer = validate_calendar(body, requested_date=requested_date)
                budget.remaining()
            except Exception as exc:
                reason = "calendar_invalid" if isinstance(exc, CalendarError) else _request_reason(exc)
                self.ledger.finish(attempt, reason=reason)
                return ObservationResult("failure", True, 0, 0, not self.ledger.summary()["incomplete"], reason, requested_date,
                    provider_failures=int(reason == "provider_failure"), budget_skips=int(reason == "budget_skip"))
            self.ledger.finish(attempt, reason="success", observation={"holiday": answer["holiday"], "requested_date": requested_date})
            return ObservationResult("success", False, 1, 0, not self.ledger.summary()["incomplete"], "holiday" if answer["holiday"] else "ok", requested_date)
        except asyncio.CancelledError:
            if attempt is not None:
                try: self.ledger.finish(attempt, reason="cancelled")
                except LedgerError: pass
            raise
        except LedgerError:
            return ObservationResult("failure", True, 0, 0, False, "ledger_unavailable", requested_date)
