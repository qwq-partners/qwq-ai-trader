"""Bounded, offline fixed-window statistics for entry-price research.

This module consumes caller-supplied original studies and decoded journals only.  It
does not discover trading days, read the clock, authenticate sources, or promote a
research result into a trading decision.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, time
from decimal import Decimal, DecimalException
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import numpy as np

from .entry_evaluation_bundle import build_evaluation_bundle, load_json_bytes
from ..utils.fee_calculator import FeeCalculator, FeeConfig

KST = ZoneInfo("Asia/Seoul")
PROTOCOL_VERSION = "entry-window-protocol-v1"
PHASES = {
    "diagnostic": (date(2026, 10, 6), date(2026, 11, 3)),
    "confirmation": (date(2026, 11, 4), date(2026, 12, 1)),
}
SCENARIOS = (0, 10, 30)
BOOTSTRAP_SEED = 20261001
BOOTSTRAP_REPLICATES = 10_000
BLOCK_DAYS = 5
CAPTURE_INSTANCE_FIELDS = {"capture_id", "start_at", "admission_end_at", "end_at", "journal_path", "study_ref"}


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid {name}")
    return value.strip()


def _sha(value: Any, name: str) -> str:
    value = _text(value, name)
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"invalid {name}")
    return value


def _iso_date(value: Any, name: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"invalid {name}")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid {name}") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"invalid {name}")
    return parsed


def _aware(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid {name}") from exc
    else:
        raise ValueError(f"invalid {name}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"timezone-aware {name} required")
    return parsed.astimezone(KST)


def _decimal(value: Any, name: str, *, nonnegative: bool = False) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"invalid {name}")
    try:
        result = Decimal(str(value))
    except (DecimalException, ValueError) as exc:
        raise ValueError(f"invalid {name}") from exc
    if not result.is_finite() or (nonnegative and result < 0):
        raise ValueError(f"invalid {name}")
    return result


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _effective_policy(context: Mapping[str, Any], fixed_at: datetime, day_text: str,
                      study_as_of: datetime) -> str:
    """Bind every declared study policy field, with only observation time excluded.

    `fixed_at` is a controlled per-capture timestamp: each declared policy fixed
    point must precede the protocol fixed point, then it is excluded from the
    across-day identity.  No policy/config/prompt/code identifier is otherwise
    removed, so a same-named adaptive policy cannot silently pool samples.
    """
    if not isinstance(context, Mapping):
        raise ValueError("study context missing")
    for section in ("markout_policy", "capital_policy"):
        value = context.get(section)
        if not isinstance(value, Mapping) or "fixed_at" not in value:
            raise ValueError("declared policy fixed_at missing")
        if _aware(value["fixed_at"], f"{section}.fixed_at") > fixed_at:
            raise ValueError("declared policy fixed_at after protocol fixed_at")
    identity = {key: value for key, value in context.items() if key != "as_of"}
    # Remove only the two declared per-capture fixed points after validating them
    # above; an unknown `fixed_at` field remains part of the policy identity.
    for section in ("markout_policy", "capital_policy"):
        identity[section] = {key: value for key, value in context[section].items() if key != "fixed_at"}
    capture = context.get("capture")
    if capture is not None:
        if not isinstance(capture, Mapping):
            raise ValueError("invalid capture policy")
        # CapturePlan has a bounded set of per-window instance values.  Leave
        # every other capture key (code/config/source/selection/channels/etc.)
        # in the comparison so a changed acquisition policy cannot be pooled.
        schedule = {}
        for key in ("start_at", "admission_end_at", "end_at"):
            if key not in capture:
                raise ValueError("capture schedule missing")
            instant = _aware(capture[key], "capture." + key)
            if instant.date().isoformat() != day_text:
                raise ValueError("capture schedule day mismatch")
            schedule[key] = instant.timetz().isoformat()
        start = _aware(capture["start_at"], "capture.start_at")
        admission = _aware(capture["admission_end_at"], "capture.admission_end_at")
        end = _aware(capture["end_at"], "capture.end_at")
        if not start < admission < end or end != study_as_of:
            raise ValueError("capture schedule order")
        schedule["admission_seconds"] = str(Decimal(str((admission - start).total_seconds())))
        schedule["window_seconds"] = str(Decimal(str((end - start).total_seconds())))
        identity["capture"] = {key: value for key, value in capture.items()
                               if key not in CAPTURE_INSTANCE_FIELDS} | {"schedule": schedule}
    return _canonical(identity)


def _scenario_reports(bundle: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    found: dict[int, Mapping[str, Any]] = {}
    for item in bundle.get("sensitivity", []):
        if not isinstance(item, Mapping) or type(item.get("slippage_bps_each")) is not int:
            raise ValueError("invalid bundle sensitivity")
        result = item.get("result")
        report = result.get("report") if isinstance(result, Mapping) else None
        if item["slippage_bps_each"] in found or not isinstance(report, Mapping):
            raise ValueError("invalid bundle sensitivity")
        found[item["slippage_bps_each"]] = report
    if set(found) != set(SCENARIOS):
        raise ValueError("bundle must contain exactly 0/10/30bp scenarios")
    return found


def _add_unique(seen: set[str], value: str, name: str) -> None:
    if value in seen:
        raise ValueError(f"duplicate {name} across days")
    seen.add(value)


def _verified_empty_scan(scan: Mapping[str, Any]) -> bool:
    """Only the structured source-completion contract can establish a real empty run."""
    try:
        from .selection_source_status import read_scan_sources
        source = read_scan_sources(scan)
    except (TypeError, ValueError, KeyError):
        return False
    if source["status"] != "observed" or not source["snapshot"] or source["snapshot"].get("fallback_used") is not False:
        return False
    runs = source["snapshot"]["runs"]
    completed = {"completed_empty", "completed_nonempty"}
    return bool(runs) and any(run.get("outcome") in completed for run in runs) \
        and all(run.get("outcome") in completed | {"not_called"} for run in runs)


def _entry_cost(row: Mapping[str, Any], report: Mapping[str, Any]) -> Decimal | None:
    quantity = row.get("a_quantity")
    if type(quantity) is not int or quantity <= 0:
        return None
    entry = _decimal(row.get("entry_price_proxy"), "entry_price_proxy", nonnegative=True)
    if entry <= 0:
        return None
    fees = report.get("fees")
    if not isinstance(fees, Mapping):
        raise ValueError("bundle fee model missing")
    config = FeeConfig(**{key: _decimal(fees.get(key), f"fees.{key}", nonnegative=True)
                          for key in FeeConfig.__dataclass_fields__})
    amount = entry * quantity
    return amount + FeeCalculator(config).calculate_buy_fee(amount)


def _bootstrap_indices(days: int) -> np.ndarray:
    rng = np.random.Generator(np.random.PCG64(BOOTSTRAP_SEED))
    starts = rng.integers(0, days, size=(BOOTSTRAP_REPLICATES, math.ceil(days / BLOCK_DAYS)))
    offsets = np.arange(BLOCK_DAYS, dtype=np.int64)
    return ((starts[:, :, None] + offsets) % days).reshape(BOOTSTRAP_REPLICATES, -1)[:, :days]


def _linear_ci(values: list[Decimal], indices: np.ndarray) -> tuple[Decimal, Decimal]:
    vector = np.array([float(v) for v in values], dtype=float)
    draws = vector[indices].mean(axis=1)
    low, high = np.quantile(draws, [0.025, 0.975], method="linear")
    return Decimal(str(low)), Decimal(str(high))


def _number_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value.normalize()) if value != 0 else "0"


def _validate_protocol(protocol: Mapping[str, Any]) -> tuple[str, str, datetime, list[str], date, date]:
    if not isinstance(protocol, Mapping) or set(protocol) != {
        "version", "phase", "dataset_kind", "fixed_at", "evaluation_epoch", "calendar"}:
        raise ValueError("strict protocol fields required")
    if protocol["version"] != PROTOCOL_VERSION:
        raise ValueError("unsupported protocol version")
    phase = _text(protocol["phase"], "phase")
    if phase not in PHASES:
        raise ValueError("invalid phase")
    dataset_kind = _text(protocol["dataset_kind"], "dataset_kind")
    epoch = _text(protocol["evaluation_epoch"], "evaluation_epoch")
    fixed_at = _aware(protocol["fixed_at"], "fixed_at")
    start, end = PHASES[phase]
    if fixed_at >= datetime.combine(start, time.min, tzinfo=KST):
        raise ValueError("fixed_at must precede period start")
    calendar = protocol["calendar"]
    if not isinstance(calendar, Mapping) or set(calendar) != {"source_ref", "source_sha256", "scheduled_dates"}:
        raise ValueError("strict calendar fields required")
    _text(calendar["source_ref"], "calendar.source_ref")
    _sha(calendar["source_sha256"], "calendar.source_sha256")
    dates = calendar["scheduled_dates"]
    if not isinstance(dates, list) or not dates:
        raise ValueError("scheduled_dates required")
    parsed = [_iso_date(item, "scheduled_date") for item in dates]
    if dates != sorted(dates) or len(set(dates)) != len(dates):
        raise ValueError("scheduled_dates must be sorted and unique")
    if any(item < start or item > end for item in parsed):
        raise ValueError("scheduled date outside phase")
    return phase, dataset_kind, fixed_at, dates, start, end


def _day_input(day: Mapping[str, Any]) -> tuple[str, bytes, Mapping[str, Any], Any]:
    required = {"date", "study_utf8", "observations"}
    allowed = required | {"session_review"}
    if (not isinstance(day, Mapping) or not required <= set(day) or set(day) - allowed):
        raise ValueError("invalid day fields")
    date_text = _iso_date(day.get("date"), "day.date").isoformat()
    study = day.get("study_utf8")
    if not isinstance(study, str):
        raise ValueError("study_utf8 required")
    try:
        raw = study.encode("utf-8", "strict")
    except UnicodeError as exc:
        raise ValueError("invalid study_utf8") from exc
    load_json_bytes(raw)  # duplicate keys/nonfinite constants are never accepted.
    observations = day.get("observations")
    if not isinstance(observations, Mapping):
        raise ValueError("observations required")
    return date_text, raw, observations, day.get("session_review")


def build_window_report(protocol: Mapping[str, Any], days: list[Mapping[str, Any]], *, as_of: datetime) -> dict[str, Any]:
    """Build the fixed protocol window without I/O or implicit time/calendar inputs."""
    phase, dataset_kind, fixed_at, scheduled, start, end = _validate_protocol(protocol)
    report_at = _aware(as_of, "as_of")
    if not isinstance(days, list):
        raise ValueError("days must be a list")
    supplied: dict[str, Mapping[str, Any]] = {}
    for item in days:
        day_text, *_ = _day_input(item)
        if day_text in supplied:
            raise ValueError("duplicate day")
        supplied[day_text] = item
    if set(supplied) - set(scheduled):
        raise ValueError("unplanned scheduled day")

    output_days, scenario_rows = [], {bps: [] for bps in SCENARIOS}
    common_policy = None
    whole_unknown = 0
    quality_failures: list[str] = []
    original_total = common_no_entry = 0
    selected_unknown_by_scenario = {bps: 0 for bps in SCENARIOS}
    selected_unknown_union: set[tuple[str, str]] = set()
    seen_capture_ids: set[str] = set()
    seen_study_hashes: set[str] = set()
    seen_observation_hashes: set[str] = set()
    for day_text in scheduled:
        if day_text not in supplied:
            quality_failures.append(day_text)
            output_days.append({"date": day_text, "status": "planned_missing", "selected_candidates": None,
                                "capture_id": None})
            for bps in SCENARIOS:
                scenario_rows[bps].append([])
            continue
        _, raw, observations, session_review = _day_input(supplied[day_text])
        context = load_json_bytes(raw)
        if not isinstance(context, Mapping) or context.get("dataset_kind") != dataset_kind \
                or context.get("evaluation_epoch") != protocol["evaluation_epoch"]:
            raise ValueError("study dataset/epoch mismatch")
        study_as_of = _aware(context.get("as_of"), "study.as_of")
        if study_as_of.date().isoformat() != day_text:
            raise ValueError("day date/study window mismatch")
        if study_as_of > report_at:
            raise ValueError("study as_of after report as_of")
        records = observations.get("records")
        scans = [record for record in records if isinstance(record, Mapping) and record.get("kind") == "scan"] \
            if isinstance(records, list) else []
        if len(scans) > 1:
            raise ValueError("one scan per day supported")
        observed = []
        if isinstance(records, list):
            for record in records:
                if isinstance(record, Mapping) and record.get("observed_at") is not None:
                    observed.append(_aware(record["observed_at"], "record.observed_at"))
        if observed and fixed_at >= min(observed):
            raise ValueError("fixed_at must precede first observation")
        if any(value.date().isoformat() != day_text for value in observed):
            raise ValueError("day date/record timestamp mismatch")
        if any(value > report_at for value in observed):
            raise ValueError("record observed_at after report as_of")
        journal = observations.get("journal")
        capture_id = _text(journal.get("capture_id"), "journal.capture_id") if isinstance(journal, Mapping) else None
        if capture_id is None:
            raise ValueError("journal missing")
        study_hash = hashlib.sha256(raw).hexdigest()
        observation_hash = hashlib.sha256(_canonical(observations).encode("utf-8")).hexdigest()
        _add_unique(seen_capture_ids, capture_id, "capture")
        _add_unique(seen_study_hashes, study_hash, "study")
        _add_unique(seen_observation_hashes, observation_hash, "observations")
        close_date_ok = False
        if isinstance(journal, Mapping) and journal.get("capture_closed_at") is not None:
            close_at = _aware(journal["capture_closed_at"], "journal.capture_closed_at")
            close_date_ok = close_at.date().isoformat() == day_text and close_at <= report_at
        quality_ok = isinstance(journal, Mapping) and journal.get("sealed") is True \
            and journal.get("dropped_count_exact") is True and journal.get("structurally_valid") is True \
            and journal.get("scope") == "sealed_window" and journal.get("persistence_dropped_records") == 0 \
            and observations.get("complete") is True and observations.get("dropped_records") == 0 \
            and observations.get("incomplete_reasons") == [] and close_date_ok
        if not quality_ok:
            quality_failures.append(day_text)
        bundle = build_evaluation_bundle(raw, observations, study_sha256=study_hash,
                                         session_review=session_review)
        if not isinstance(bundle, Mapping) or bundle.get("version") != "entry-evaluation-bundle-v1":
            raise ValueError("invalid evaluation bundle")
        binding = bundle.get("binding")
        if bundle.get("dataset_kind") != dataset_kind or not isinstance(binding, Mapping) \
                or binding.get("evaluation_epoch") != protocol["evaluation_epoch"]:
            raise ValueError("bundle dataset/epoch mismatch")
        if session_review is not None:
            if not isinstance(session_review, Mapping) or not isinstance(session_review.get("quotes"), list):
                raise ValueError("session review schema")
            if any(_aware(item.get("reviewed_at"), "session_review.reviewed_at") > report_at
                   for item in session_review["quotes"] if isinstance(item, Mapping)):
                raise ValueError("session review after report as_of")
        identity = _effective_policy(context, fixed_at, day_text, study_as_of)
        if common_policy is None:
            common_policy = identity
        elif common_policy != identity:
            raise ValueError("effective policy changed across days")
        tasks = bundle.get("review_tasks")
        if not isinstance(tasks, list):
            raise ValueError("bundle review_tasks missing")
        selected = []
        for expected_rank, task in enumerate(tasks, 1):
            if not isinstance(task, Mapping) or task.get("returned_rank") != expected_rank:
                raise ValueError("selected ranks must preserve recorded order")
            oid = _text(task.get("opportunity_id"), "opportunity_id")
            if expected_rank <= 3:
                selected.append((expected_rank, oid))
        reports = _scenario_reports(bundle)
        original = bundle.get("original")
        original_report = original.get("report") if isinstance(original, Mapping) else None
        if not isinstance(original_report, Mapping):
            raise ValueError("bundle original report missing")
        original_rows = {row.get("opportunity_id"): row for row in original_report.get("opportunities", [])
                         if isinstance(row, Mapping)}
        original_total += len(original_rows)
        whole_unknown += sum(row.get("gate_status") == "unknown" for row in original_rows.values())
        status = "complete_scan"
        if len(scans) == 0:
            status = "no_scan"
            quality_failures.append(day_text)
        elif not selected:
            status = "verified_empty_scan" if quality_ok and _verified_empty_scan(scans[0]) else "empty_scan_unverified"
            if status == "empty_scan_unverified":
                quality_failures.append(day_text)
        elif not quality_ok:
            status = "unsealed_or_dropped"
        output_day = {"date": day_text, "status": status, "selected_candidates": len(selected),
                      "capture_id": binding.get("capture_id")}
        output_days.append(output_day)
        for bps, scenario_report in reports.items():
            by_id = {row.get("opportunity_id"): row for row in scenario_report.get("opportunities", [])
                     if isinstance(row, Mapping)}
            candidate_values = []
            for rank, oid in selected:
                row = by_id.get(oid)
                if row is None or row.get("gate_status") not in {"allow", "cash"}:
                    quality_failures.append(day_text)
                    selected_unknown_by_scenario[bps] += 1
                    selected_unknown_union.add((day_text, oid))
                    continue
                if row.get("delta_net_pnl") is None or row.get("b_net_pnl") is None:
                    quality_failures.append(day_text)
                    selected_unknown_by_scenario[bps] += 1
                    selected_unknown_union.add((day_text, oid))
                    continue
                cost = _entry_cost(row, scenario_report)
                if cost is None or cost <= 0:
                    quality_failures.append(day_text)
                    selected_unknown_by_scenario[bps] += 1
                    selected_unknown_union.add((day_text, oid))
                    continue
                candidate_values.append({"date": day_text, "rank": rank, "opportunity_id": oid,
                                         "x": _decimal(row["delta_net_pnl"], "delta_net_pnl") / cost,
                                         "b": _decimal(row["b_net_pnl"], "b_net_pnl") / cost,
                                         "price_pair": True})
            scenario_rows[bps].append(candidate_values)

    final_end = datetime.combine(end, time(15, 30), tzinfo=KST)
    quality_only = report_at < final_end
    suppress = bool(quality_failures) or quality_only
    incomplete_dates = set(quality_failures)
    indices = _bootstrap_indices(len(scheduled))
    scenarios: dict[str, Any] = {}
    for bps, day_values in scenario_rows.items():
        daily_x = [sum((item["x"] for item in values), Decimal(0)) / len(values) if values else Decimal(0)
                   for values in day_values]
        daily_b = [sum((item["b"] for item in values), Decimal(0)) / len(values) if values else Decimal(0)
                   for values in day_values]
        all_items = [item for values in day_values for item in values]
        pairs = [item for item in all_items if item["price_pair"]]
        pair_days = len({item["date"] for item in pairs})
        low, high = _linear_ci(daily_x, indices)
        b_low, b_high = _linear_ci(daily_b, indices)
        removed = sorted((item for item in all_items if item["x"] > 0),
                         key=lambda item: (-item["x"], item["date"], item["rank"], item["opportunity_id"]))[:3]
        removed_ids = {(item["date"], item["opportunity_id"]) for item in removed}
        reduced_days = [[item for item in values if (item["date"], item["opportunity_id"]) not in removed_ids]
                        for values in day_values]
        reduced_x = [sum((item["x"] for item in values), Decimal(0)) / len(values) if values else Decimal(0)
                     for values in reduced_days]
        reduced_pairs = [item for values in reduced_days for item in values if item["price_pair"]]
        reduced_mean = sum(reduced_x, Decimal(0)) / len(reduced_x)
        mean_x = sum(daily_x, Decimal(0)) / len(daily_x)
        mean_b = sum(daily_b, Decimal(0)) / len(daily_b)
        criteria = (not suppress and len(pairs) >= 30 and pair_days >= 10 and mean_x > 0 and mean_b > 0
                    and low > 0 and len(reduced_pairs) >= 27
                    and len({item["date"] for item in reduced_pairs}) >= 10 and reduced_mean > 0)
        scenarios[str(bps)] = {
            "price_pairs": len(pairs), "price_days": pair_days,
            "selected_unknown_candidates": selected_unknown_by_scenario[bps],
            "mean_delta": None if suppress else _number_text(mean_x),
            "mean_b_net": None if suppress else _number_text(mean_b),
            "bootstrap": None if suppress else {"seed": BOOTSTRAP_SEED, "replicates": BOOTSTRAP_REPLICATES,
                                                   "block_days": BLOCK_DAYS, "quantile_method": "linear",
                                                   "lower": _number_text(low), "upper": _number_text(high),
                                                   "b_net_lower": _number_text(b_low),
                                                   "b_net_upper": _number_text(b_high)},
            "daily_y": [{"date": day_text,
                           "delta": None if quality_only or day_text in incomplete_dates else _number_text(daily_x[index]),
                           "b_net": None if quality_only or day_text in incomplete_dates else _number_text(daily_b[index])}
                        for index, day_text in enumerate(scheduled)],
            "top3_positive_removed": {"status": "quality_only" if quality_only else "insufficient_data" if len(reduced_pairs) < 27
                                                   or len({item["date"] for item in reduced_pairs}) < 10
                                                   else "evaluated",
                                        "removed": [] if quality_only else [item["opportunity_id"] for item in removed],
                                        "price_pairs": None if quality_only else len(reduced_pairs),
                                        "price_days": None if quality_only else len({item["date"] for item in reduced_pairs}),
                                        "mean_delta": None if suppress else _number_text(reduced_mean)},
            "research_criteria_met": criteria,
            "research_criteria_conditional_on_supplied_input": True,
        }
    if suppress:
        verdict = "suppressed"
    elif all(scenarios[str(bps)]["research_criteria_met"] for bps in SCENARIOS):
        verdict = "conditional_research_criteria_met"
    elif any(scenarios[str(bps)]["price_pairs"] < 30 or scenarios[str(bps)]["price_days"] < 10 for bps in SCENARIOS):
        verdict = "insufficient_data"
    elif any(Decimal(scenarios[str(bps)]["mean_delta"]) <= 0 or Decimal(scenarios[str(bps)]["mean_b_net"]) <= 0
             for bps in SCENARIOS):
        verdict = "improvement_evidence_not_met"
    else:
        verdict = "advantage_unconfirmed"
    return {"version": "entry-window-report-v1", "protocol": dict(protocol), "as_of": report_at.isoformat(),
            "evaluation_status": "quality_only" if quality_only else "final", "days": output_days,
            "quality_failures": sorted(set(quality_failures)), "original_total_candidates": original_total,
            "selected_unknown_candidates": len(selected_unknown_union),
            "common_no_entry_candidates": common_no_entry,
            "whole_cohort_unknown_candidates": whole_unknown,
            "economic_verdict": verdict, "scenarios": scenarios,
            "source_authenticity_verified": False, "production_eligible": False, "account_return": None,
            "profitability_pass": False,
            "limitations": ["Calendar is caller-supplied and not inferred or authenticated.",
                            "Results are conditional on supplied original studies, journals, and reviews.",
                            "No account replay, actual-fill result, source authentication, or trading promotion."]}
