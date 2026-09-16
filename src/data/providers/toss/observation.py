"""Current-price observations with durable, separately qualified comparisons."""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from hashlib import sha256
from typing import Mapping

from .market_data import parse_prices
from .observation_ledger import LedgerError, _canonical, comparison_summary, quote_evidence, ratio
from .rate_limit import RequestBudget
from .transport import TossRequestError

_KST = timezone(timedelta(hours=9))
_BUDGET_ERRORS = {"timeout", "page_exhausted", "retry_exhausted", "rate_limited"}


@dataclass(frozen=True)
class ObservationResult:
    outcome: str
    degraded: bool
    observation_count: int
    valid_pairs: int
    ledger_complete: bool
    reason: str
    requested_date: str | None = None
    excluded_pairs: int = 0
    comparison: object | None = None
    production_eligible: bool = False
    provider_failures: int = 0
    budget_skips: int = 0


def _aware(value):
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _cohort(policy, now, *, calendar=False):
    session = "calendar" if calendar else "outside"
    if not calendar and _aware(now):
        minute = now.astimezone(_KST).strftime("%H:%M")
        for item in policy.get("sessions", ()):
            if item["start"] <= minute < item["end"]:
                session = item["name"]
                break
    comparison = policy.get("comparison", {})
    return {"dataset_kind": policy.get("dataset_kind", "unknown"), "session": session,
            "min_valid_pairs": comparison.get("min_valid_pairs", 1),
            "outlier_pct": str(comparison.get("outlier_pct", 0)),
            "max_age_seconds": comparison.get("max_age_seconds", 1),
            "max_skew_seconds": comparison.get("max_skew_seconds", 0),
            "expected_market_basis": comparison.get("expected_market_basis", "unknown")}


def select_snapshot(*, candidates, holdings, source_success_at, now, policy, kis_quotes):
    selection = policy["selection"]
    cap, max_age = selection["max_symbols"], selection["max_snapshot_age_seconds"]
    if type(cap) is not int or cap < 1:
        raise ValueError("invalid_selection_limit")
    valid_source = _aware(source_success_at) and _aware(now) and source_success_at <= now and (now - source_success_at).total_seconds() <= max_age
    held = tuple(dict.fromkeys(s for s in holdings if isinstance(s, str) and s))
    selected = list(held[:cap])
    overflow = held[cap:]
    selected_candidates, seen = [], set(held)
    eligible = [] if not valid_source else [(symbol, score) for symbol, score in candidates
        if isinstance(symbol, str) and symbol and type(score) in (int, float) and math.isfinite(score)]
    for symbol, _ in sorted(eligible, key=lambda item: (-item[1], item[0])):
        if len(selected) >= cap or len(selected_candidates) >= selection.get("candidate_limit", len(eligible)):
            break
        if symbol in seen:
            continue
        seen.add(symbol)
        selected.append(symbol)
        selected_candidates.append(symbol)
    metadata = {"rule": "score_desc_symbol_asc_holdings_first", "holdings": held[:cap],
                "candidates": tuple(selected_candidates), "overflow_symbols": overflow}
    selected_at = now.isoformat() if _aware(now) else None
    identity = sha256(_canonical([selected_at, selected, metadata])).hexdigest()
    return {"snapshot_id": identity, "selected_at": selected_at,
            "source_success_at": source_success_at.isoformat() if _aware(source_success_at) else None,
            "selection_partial": not valid_source or bool(overflow), "symbols": tuple(selected),
            "kis": {s: kis_quotes[s] for s in selected if s in kis_quotes},
            "selection_metadata": metadata, "cohort": _cohort(policy, now)}


def _invalid(reason, pair_id=None):
    return {"valid": False, "reason": reason, "pair_id": pair_id, "delta_pct": None, "outlier": None}


def _pair(kis, toss, policy, now):
    if kis is None or toss is None:
        return _invalid("missing_quote")
    if any(q.status != "ok" for q in (kis, toss)):
        return _invalid("status")
    if any(not isinstance(q.price, Decimal) or not q.price.is_finite() or q.price <= 0
           or abs(q.price.adjusted()) > 30 or abs(q.price.as_tuple().exponent) > 30 for q in (kis, toss)):
        return _invalid("price")
    if kis.symbol != toss.symbol:
        return _invalid("symbol")
    if kis.currency != toss.currency or kis.currency != "KRW":
        return _invalid("currency")
    expected = policy["comparison"]["expected_market_basis"]
    if expected not in {"krx", "krx_nxt"} or kis.market_basis != expected or toss.market_basis != expected:
        return _invalid("market_basis")
    if not _aware(now) or any(not _aware(q.observed_at) for q in (kis, toss)):
        return _invalid("observed_at")
    if any(not _aware(q.fetched_at) for q in (kis, toss)):
        return _invalid("fetched_at")
    if any(q.observed_at > now or q.fetched_at > now for q in (kis, toss)):
        return _invalid("future")
    if any(q.observed_at > q.fetched_at for q in (kis, toss)):
        return _invalid("fetched_at")
    if any((now - q.observed_at).total_seconds() > policy["comparison"]["max_age_seconds"]
           or (now - q.fetched_at).total_seconds() > policy["comparison"]["max_age_seconds"] for q in (kis, toss)):
        return _invalid("stale")
    if abs((kis.observed_at - toss.observed_at).total_seconds()) > policy["comparison"]["max_skew_seconds"]:
        return _invalid("skew")
    delta = 100 * abs(Fraction(toss.price) - Fraction(kis.price)) / Fraction(kis.price)
    pair_id = sha256(_canonical([kis.symbol, kis.observed_at.astimezone(timezone.utc).isoformat(),
                                toss.observed_at.astimezone(timezone.utc).isoformat()])).hexdigest()
    return {"valid": True, "reason": "ok", "pair_id": pair_id, "delta_pct": ratio(delta),
            "outlier": delta > Fraction(str(policy["comparison"]["outlier_pct"]))}


def _request_reason(exc):
    return "budget_skip" if isinstance(exc, TossRequestError) and exc.code in _BUDGET_ERRORS else "provider_failure"


class ObservationRunner:
    def __init__(self, *, client, ledger, policy: Mapping, clock=time.monotonic, now=lambda: datetime.now(timezone.utc)):
        self.client, self.ledger, self.policy, self.clock, self.now = client, ledger, policy, clock, now

    async def prices(self, *, slot_id: str, snapshot: dict) -> ObservationResult:
        limits = self.policy["limits"]
        # Created before selection copying, reservation or any synchronous fsync.
        budget = RequestBudget(limits["job_timeout_seconds"], clock=self.clock,
                               max_retries=limits["max_retries"], max_pages=limits["max_pages"])
        symbols = tuple(snapshot["symbols"])
        snapshot = dict(snapshot)
        snapshot["cohort"] = _cohort(self.policy, self.now())
        attempts, finished, comparisons = (), set(), []
        observed = failed = skipped = 0

        def close_pending(reason):
            nonlocal failed, skipped
            for attempt in attempts:
                if attempt not in finished:
                    self.ledger.finish(attempt, reason=reason)
                    finished.add(attempt)
                    failed += int(reason == "provider_failure")
                    skipped += int(reason == "budget_skip")

        try:
            attempts = self.ledger.reserve_slot(slot_id, kind="prices", snapshot=snapshot)
            if attempts is None:
                return ObservationResult("duplicate", False, 0, 0, not self.ledger.summary()["incomplete"], "duplicate")
            for attempt in attempts:
                self.ledger.begin(attempt)
            if len(symbols) > self.policy["selection"]["max_symbols"]:
                close_pending("provider_failure")
                return ObservationResult("failure", True, 0, 0, not self.ledger.summary()["incomplete"], "selection_overflow", provider_failures=failed)
            for start in range(0, len(symbols), 200):
                chunk = symbols[start:start + 200]
                try:
                    budget.remaining()
                    body = await self.client.get("/api/v1/prices", params={"symbols": ",".join(chunk)}, budget=budget)
                    budget.remaining()
                    now = self.now()
                    quotes = parse_prices(body, symbols=chunk, fetched_at=now, now=now,
                                          max_age_seconds=max(1, self.policy["comparison"]["max_age_seconds"]))
                    if self.policy["comparison"]["max_age_seconds"] == 0:
                        # The existing normalizer requires a positive TTL; keep
                        # its contract and enforce the stricter approved zero age.
                        quotes = {s: replace(q, status="stale") if q.observed_at is not None and q.observed_at < now else q
                                  for s, q in quotes.items()}
                    budget.remaining()
                    for index, symbol in enumerate(chunk, start):
                        budget.remaining()
                        toss, kis = quotes[symbol], snapshot.get("kis", {}).get(symbol)
                        comparison = _pair(kis, toss, self.policy, now)
                        if comparison["valid"] and self.ledger.pair_seen(attempt_id=attempts[index], pair_id=comparison["pair_id"]):
                            comparison = _invalid("duplicate", comparison["pair_id"])
                        ok = toss.status == "ok" and isinstance(toss.price, Decimal) and toss.price.is_finite() and toss.price > 0
                        evidence = {"valid_pairs": int(comparison["valid"]), "excluded_pairs": int(not comparison["valid"]),
                                    "kis": quote_evidence(kis), "toss": quote_evidence(toss), "comparison": comparison,
                                    "evaluated_at": now.isoformat()}
                        budget.remaining()
                        self.ledger.finish(attempts[index], reason="success" if ok else "excluded", observation=evidence)
                        finished.add(attempts[index])
                        observed += int(ok)
                        comparisons.append(comparison)
                except LedgerError:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    close_pending(_request_reason(exc))
                    break
        except asyncio.CancelledError:
            try:
                close_pending("cancelled")
            except LedgerError:
                pass  # journal remains incomplete; cancellation never becomes success
            raise
        except LedgerError:
            return ObservationResult("failure", True, observed, sum(c["valid"] for c in comparisons), False,
                                     "ledger_unavailable", provider_failures=failed, budget_skips=skipped)
        valid = sum(c["valid"] for c in comparisons)
        excluded = len(comparisons) - valid
        complete = not self.ledger.summary()["incomplete"]
        outcome = "success" if observed else "failure" if symbols else "idle"
        reason = "ok" if observed else "empty_selection" if not symbols else "budget_skip" if skipped == len(symbols) else "provider_failure"
        return ObservationResult(outcome, bool(failed or skipped or excluded or snapshot["selection_partial"]), observed, valid,
            complete, reason, excluded_pairs=excluded,
            comparison=comparison_summary(comparisons, minimum=self.policy["comparison"]["min_valid_pairs"]),
            provider_failures=failed, budget_skips=skipped)

    async def calendar(self, *, slot_id: str, requested_date: str) -> ObservationResult:
        from .calendar_observation import CalendarObservationRunner
        return await CalendarObservationRunner(client=self.client, ledger=self.ledger, policy=self.policy,
            clock=self.clock, now=self.now).calendar(slot_id=slot_id, requested_date=requested_date)
