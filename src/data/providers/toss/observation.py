"""Read-only current-price observation and comparison diagnostics."""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
import time
from typing import Any, Mapping

from .market_data import parse_prices
from .rate_limit import RequestBudget
from .transport import TossRequestError


@dataclass(frozen=True)
class ObservationResult:
    outcome: str; degraded: bool; observation_count: int; valid_pairs: int
    ledger_complete: bool; reason: str; requested_date: str | None = None
    excluded_pairs: int = 0; comparison: object | None = None
    production_eligible: bool = False


def _aware(value): return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def select_snapshot(*, candidates, holdings, source_success_at, now, policy, kis_quotes):
    selection = policy["selection"]
    max_symbols, max_age = selection["max_symbols"], selection["max_snapshot_age_seconds"]
    valid_source = _aware(source_success_at) and _aware(now) and source_success_at <= now and (now - source_success_at).total_seconds() <= max_age
    held = tuple(dict.fromkeys(s for s in holdings if isinstance(s, str) and s))
    eligible = [] if not valid_source else [(symbol, score) for symbol, score in candidates if isinstance(symbol, str) and symbol and type(score) in (int, float) and math.isfinite(score)]
    selected = list(held)
    candidate_limit = selection.get("candidate_limit", len(eligible))
    for symbol, _ in sorted(eligible, key=lambda item: (-item[1], item[0]))[:candidate_limit]:
        if symbol not in selected: selected.append(symbol)
        if len(selected) >= max_symbols: break
    return {"snapshot_id": f"snapshot:{now.isoformat()}" if _aware(now) else "snapshot:invalid", "selected_at": now.isoformat() if _aware(now) else None,
            "source_success_at": source_success_at.isoformat() if _aware(source_success_at) else None,
            "selection_partial": not valid_source, "symbols": tuple(selected), "kis": dict(kis_quotes)}


def _pair(kis, toss, policy, now):
    if any(q is None or q.status != "ok" or q.price is None or q.currency != "KRW" or not _aware(q.observed_at) for q in (kis, toss)): return False
    expected = policy["comparison"]["expected_market_basis"]
    if kis.market_basis != expected or toss.market_basis != expected: return False
    if any(q.observed_at > now or (now - q.observed_at).total_seconds() > policy["comparison"]["max_age_seconds"] for q in (kis, toss)): return False
    if abs((kis.observed_at - toss.observed_at).total_seconds()) > policy["comparison"]["max_skew_seconds"]: return False
    delta = abs(Fraction(kis.price - toss.price) / Fraction(kis.price)) * 100
    return delta <= Fraction(str(policy["comparison"]["outlier_pct"]))


class ObservationRunner:
    def __init__(self, *, client, ledger, policy: Mapping, clock=time.monotonic, now=lambda: datetime.now(timezone.utc)):
        self.client, self.ledger, self.policy, self.clock, self.now = client, ledger, policy, clock, now

    async def prices(self, *, slot_id: str, snapshot: dict) -> ObservationResult:
        attempts = self.ledger.reserve_slot(slot_id, kind="prices", snapshot=snapshot)
        if attempts is None: return ObservationResult("duplicate", False, 0, 0, not self.ledger.summary()["incomplete"], "duplicate")
        for attempt in attempts: self.ledger.begin(attempt)
        limits = self.policy["limits"]; budget = RequestBudget(limits["job_timeout_seconds"], clock=self.clock, max_retries=limits["max_retries"], max_pages=limits["max_pages"])
        symbols, observed, valid, excluded = tuple(snapshot["symbols"]), 0, 0, 0
        for start in range(0, len(symbols), 200):
            chunk = symbols[start:start + 200]
            try:
                body = await self.client.get("/api/v1/prices", params={"symbols": ",".join(chunk)}, budget=budget)
                now = self.now(); quotes = parse_prices(body, symbols=chunk, fetched_at=now, now=now, max_age_seconds=self.policy["comparison"]["max_age_seconds"])
                for index, symbol in enumerate(chunk, start):
                    toss = quotes[symbol]; ok = toss.status == "ok" and toss.price is not None
                    if ok: observed += 1
                    if _pair(snapshot["kis"].get(symbol), toss, self.policy, now): valid += 1
                    else: excluded += 1
                    self.ledger.finish(attempts[index], reason="success" if ok else "excluded", observation={"valid_pairs": int(_pair(snapshot["kis"].get(symbol), toss, self.policy, now)), "excluded_pairs": int(not _pair(snapshot["kis"].get(symbol), toss, self.policy, now))})
            except asyncio.CancelledError:
                for index in range(start, len(symbols)):
                    self.ledger.finish(attempts[index], reason="cancelled")
                raise
            except TossRequestError as exc:
                reason = "budget_skip" if exc.code in {"timeout", "page_exhausted", "retry_exhausted", "rate_limited"} else "provider_failure"
                for index in range(start, len(symbols)):
                    self.ledger.finish(attempts[index], reason=reason)
                break
            except Exception:
                for index in range(start, len(symbols)):
                    self.ledger.finish(attempts[index], reason="provider_failure")
                break
        complete = not self.ledger.summary()["incomplete"]
        failed = self.ledger.summary()["provider_failures"]
        outcome = "success" if observed else "failure"
        return ObservationResult(outcome, bool(failed or excluded), observed, valid, complete, "ok" if observed else "provider_failure", excluded_pairs=excluded, comparison=(valid if valid else None))

    async def calendar(self, *, slot_id: str, requested_date: str) -> ObservationResult:
        from .calendar_observation import CalendarObservationRunner
        return await CalendarObservationRunner(client=self.client, ledger=self.ledger, policy=self.policy, clock=self.clock, now=self.now).calendar(slot_id=slot_id, requested_date=requested_date)
