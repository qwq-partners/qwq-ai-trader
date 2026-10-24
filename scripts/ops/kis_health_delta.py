#!/usr/bin/env python3
"""Read-only, local KIS health snapshots and offline counter differences.

Only process-lifetime request counters and a narrow service identity are stored.
This tool never reads configuration, credentials, account values, or broker APIs.
"""
import argparse
from datetime import datetime, timezone
import http.client
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys


MAX_INPUT_BYTES = 256 * 1024
MAX_HEALTH_BYTES = 1024 * 1024
HTTP_TIMEOUT_SECONDS = 3
SCHEMA_VERSION = "kis-health-delta-snapshot-v1"
DIFF_SCHEMA_VERSION = "kis-health-delta-diff-v1"
IDENTITY_FIELDS = ("ActiveState", "MainPID", "InvocationID",
                   "ExecMainStartTimestampMonotonic", "NRestarts")

_KIS_LOGICAL_DIMENSIONS = ("source", "operation")
_KIS_LOGICAL_COUNTERS = ("calls", "cache_hits", "cache_misses")
_KIS_HTTP_DIMENSIONS = ("source", "operation", "tr_id", "page")
_KIS_HTTP_COUNTERS = ("attempts", "retries", "egw00201", "egw00215", "success",
                      "http_error", "api_error", "network_error", "cancelled",
                      "invalid_json", "other")
_DIRECT_HTTP_DIMENSIONS = ("component", "operation", "tr_id")
_DIRECT_HTTP_COUNTERS = ("attempts", "success", "http_error", "api_error",
                         "network_error", "cancelled", "invalid_json", "other",
                         "egw00201", "egw00215")
_ANALYSIS_COUNTERS = ("requests", "cache_hits", "coalesced", "provider_calls")
_KIS_SOURCES = frozenset({"startup", "portfolio_sync", "portfolio_sync_consistency_retry",
                          "fill_check", "batch_guard", "dashboard_external_accounts",
                          "dashboard_settlement", "dashboard_sse_indices",
                          "dashboard_http_indices", "unknown"})
_KIS_OPERATIONS = frozenset({"account_summary", "positions", "external_positions", "daily_fills",
                             "open_orders", "orderable_cash", "quote", "overtime_quote",
                             "daily_prices", "other"})
_KIS_TR_IDS = frozenset({"TTTC8434R", "TTTC8908R", "TTTC8001R", "TTTC8036R", "TTTC0081R",
                         "TTTC0084R", "TTTS3012R", "VTTS3012R", "CTRGA011R", "FHKST01010100",
                         "FHPST02300000", "FHKST03010100", "other"})
_DIRECT_COMPONENTS = frozenset({"market_data", "kr_screener", "other"})
_DIRECT_OPERATIONS = frozenset({"holidays", "sector_indices", "fluctuation_rank", "foreign_institution",
                                "frgnmem_trade_estimate", "stock_investor_daily", "investor_trend_estimate",
                                "stock_valuation", "night_futures_quote", "index_price", "volume_surge",
                                "new_highs", "other"})
_DIRECT_TR_IDS = frozenset({"CTCA0903R", "FHPUP02140000", "FHPST01700000", "FHPTJ04400000",
                            "FHKST644100C0", "FHKST01010900", "HHPTJ04160200", "FHKST01010100",
                            "FHMIF10000000", "FHPUP02100000", "FHPST01710000", "FHPST01720000", "other"})


def _nonnegative_int(value):
    return type(value) is int and value >= 0


def _aware_iso(value):
    if type(value) is not str:
        raise ValueError("invalid_capture_time")
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid_capture_time") from exc
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("invalid_capture_time")
    return stamp.astimezone(timezone.utc)


def _now_iso(now=None):
    value = datetime.now(timezone.utc) if now is None else now
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("invalid_capture_time")
    return value.astimezone(timezone.utc).isoformat()


def _health_timestamp(value):
    if type(value) is not str or not value or len(value) > 80:
        return {"status": "missing"}
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return {"status": "missing"}
    return {"status": "present", "value": value,
            "timezone": "aware" if stamp.tzinfo is not None and stamp.utcoffset() is not None else "unspecified"}


def _safe_absolute(path, *, output=False):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("path_unsafe")
    # Do not resolve: resolving would follow the link we mean to reject.
    for candidate in (path, *path.parents):
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("path_unsafe")
    if output:
        parent = path.parent
        try:
            info = os.lstat(parent)
        except OSError as exc:
            raise ValueError("private_output_required") from exc
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError("private_output_required")
    return path


def read_bounded(path, limit=MAX_INPUT_BYTES):
    """Read one immutable regular file without following links."""
    try:
        path = _safe_absolute(path)
    except ValueError as exc:
        raise ValueError("input_path_unsafe") from exc
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ValueError("input_path_unsafe") from exc
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size > limit):
            raise ValueError("input_path_unsafe")
        data = os.read(fd, limit + 1)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (len(data) > limit or before.st_dev != after.st_dev or before.st_ino != after.st_ino
            or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns
            or after.st_nlink != 1):
        raise ValueError("input_changed_or_oversize")
    return data


def _json_loads_strict(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid_json")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid_json") from exc


def _validate_identity(identity):
    if not isinstance(identity, dict) or set(identity) != set(IDENTITY_FIELDS):
        raise ValueError("invalid_process_identity")
    if identity["ActiveState"] != "active":
        raise ValueError("invalid_process_identity")
    for key in ("MainPID", "ExecMainStartTimestampMonotonic", "NRestarts"):
        value = identity[key]
        if type(value) is not str or not value.isascii() or not value.isdecimal():
            raise ValueError("invalid_process_identity")
    if int(identity["MainPID"]) <= 0 or int(identity["ExecMainStartTimestampMonotonic"]) <= 0:
        raise ValueError("invalid_process_identity")
    invocation = identity["InvocationID"]
    if (type(invocation) is not str or len(invocation) != 32
            or any(c not in "0123456789abcdef" for c in invocation)):
        raise ValueError("invalid_process_identity")
    return {key: identity[key] for key in IDENTITY_FIELDS}


def service_identity():
    """Read the fixed service identity with no inherited environment."""
    try:
        done = subprocess.run(
            ["/usr/bin/systemctl", "show", "qwq-ai-trader", "--no-pager",
             "--property=" + ",".join(IDENTITY_FIELDS)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=5, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("service_identity_unavailable") from exc
    if done.returncode != 0 or len(done.stdout) > 4096:
        raise ValueError("service_identity_unavailable")
    result = {}
    for line in done.stdout.splitlines():
        if "=" not in line:
            raise ValueError("service_identity_unavailable")
        key, value = line.split("=", 1)
        if key not in IDENTITY_FIELDS or key in result:
            raise ValueError("service_identity_unavailable")
        result[key] = value
    return _validate_identity(result)


def local_health():
    """Fetch the sole fixed local health endpoint; redirects are never followed."""
    connection = http.client.HTTPConnection("127.0.0.1", 8080, timeout=HTTP_TIMEOUT_SECONDS)
    try:
        connection.request("GET", "/api/health", headers={"Accept": "application/json", "Connection": "close"})
        response = connection.getresponse()
        raw = response.read(MAX_HEALTH_BYTES + 1)
        if response.status != 200 or len(raw) > MAX_HEALTH_BYTES:
            raise ValueError("local_health_unavailable")
        data = _json_loads_strict(raw)
        if not isinstance(data, dict):
            raise ValueError("local_health_unavailable")
        return data
    except (OSError, http.client.HTTPException, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc) == "local_health_unavailable":
            raise
        raise ValueError("local_health_unavailable") from exc
    finally:
        connection.close()


def dashboard_listener_pid():
    """Require one known owner of the fixed health port; never query a broker."""
    try:
        done = subprocess.run(
            ["/usr/bin/lsof", "-nP", "-iTCP:8080", "-sTCP:LISTEN", "-t"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=5, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("dashboard_owner_unavailable") from exc
    owners = set(done.stdout.splitlines())
    if done.returncode != 0 or len(done.stdout) > 4096 or len(owners) != 1:
        raise ValueError("dashboard_owner_unavailable")
    owner = owners.pop()
    if not owner.isascii() or not owner.isdecimal() or int(owner) <= 0:
        raise ValueError("dashboard_owner_unavailable")
    return owner


def _rows(value, dimensions, counters, allowed_labels):
    if not isinstance(value, list):
        raise ValueError("invalid_metric_schema")
    expected = set(dimensions + counters)
    seen, output = set(), []
    for row in value:
        if not isinstance(row, dict) or set(row) != expected:
            raise ValueError("invalid_metric_schema")
        if any(type(row[key]) is not str or row[key] not in allowed_labels[key]
               for key in dimensions if key != "page"):
            raise ValueError("invalid_metric_schema")
        if "page" in dimensions and (type(row["page"]) is not int or not 0 <= row["page"] <= 10):
            raise ValueError("invalid_metric_schema")
        if any(not _nonnegative_int(row[key]) for key in counters):
            raise ValueError("invalid_metric_schema")
        key = tuple(row[field] for field in dimensions)
        if key in seen:
            raise ValueError("duplicate_metric_row")
        seen.add(key)
        output.append({field: row[field] for field in (*dimensions, *counters)})
    return sorted(output, key=lambda row: tuple(row[field] for field in dimensions))


def _project_kis(value):
    if not isinstance(value, dict) or set(value) != {"available", "scope", "window", "logical", "http"}:
        raise ValueError("invalid_metric_schema")
    if type(value["available"]) is not bool or value["scope"] != "kr_broker_get" or value["window"] != "process_lifetime":
        raise ValueError("invalid_metric_schema")
    return {"available": value["available"], "scope": value["scope"], "window": value["window"],
            "logical": _rows(value["logical"], _KIS_LOGICAL_DIMENSIONS, _KIS_LOGICAL_COUNTERS,
                             {"source": _KIS_SOURCES, "operation": _KIS_OPERATIONS}),
            "http": _rows(value["http"], _KIS_HTTP_DIMENSIONS, _KIS_HTTP_COUNTERS,
                          {"source": _KIS_SOURCES, "operation": _KIS_OPERATIONS, "tr_id": _KIS_TR_IDS})}


def _project_direct(value):
    if not isinstance(value, dict) or set(value) != {"available", "scope", "window", "http"}:
        raise ValueError("invalid_metric_schema")
    if type(value["available"]) is not bool or value["scope"] != "kr_market_data_and_screener_get" or value["window"] != "process_lifetime":
        raise ValueError("invalid_metric_schema")
    return {"available": value["available"], "scope": value["scope"], "window": value["window"],
            "http": _rows(value["http"], _DIRECT_HTTP_DIMENSIONS, _DIRECT_HTTP_COUNTERS,
                          {"component": _DIRECT_COMPONENTS, "operation": _DIRECT_OPERATIONS,
                           "tr_id": _DIRECT_TR_IDS})}


def _project_analysis(value):
    if not isinstance(value, dict) or set(value) != {"scope", "window", *_ANALYSIS_COUNTERS, "entries"}:
        raise ValueError("invalid_metric_schema")
    if value["scope"] != "analysis_daily_prices" or value["window"] != "broker_event_loop_lifetime":
        raise ValueError("invalid_metric_schema")
    if any(not _nonnegative_int(value[key]) for key in (*_ANALYSIS_COUNTERS, "entries")):
        raise ValueError("invalid_metric_schema")
    return {key: value[key] for key in ("scope", "window", *_ANALYSIS_COUNTERS, "entries")}


def _state(raw, projector):
    return {"status": "missing"} if raw is None else {"status": "present", "value": projector(raw)}


def _project_heartbeat(value):
    if not isinstance(value, dict) or "kr_screener" not in value or not isinstance(value["kr_screener"], dict):
        return {"status": "missing"}
    row = value["kr_screener"]
    # loop_status() exposes Unix-second attempts/successes, not ISO datetimes.
    # Omit diagnostic reason/note strings: they can carry arbitrary exception text.
    allowed = ("enabled", "last_attempt", "last_success", "consecutive_failures", "next_due")
    projected = {key: row.get(key) for key in allowed}
    if (type(projected["enabled"]) is not bool or not _nonnegative_int(projected["consecutive_failures"])
            or any(projected[key] is not None and (isinstance(projected[key], bool)
                                                    or not isinstance(projected[key], (int, float))
                                                    or not math.isfinite(projected[key]) or projected[key] < 0)
                   for key in ("last_attempt", "last_success"))
            or projected["next_due"] is not None and type(projected["next_due"]) is not str):
        return {"status": "missing"}
    return {"status": "present", "value": projected}


def build_snapshot(health, identity, capture_start, capture_end):
    """Create a strict, privacy-minimised snapshot from already-read values."""
    start, end = _aware_iso(capture_start), _aware_iso(capture_end)
    if end < start or not isinstance(health, dict):
        raise ValueError("invalid_snapshot")
    identity = _validate_identity(identity)
    broker = health.get("broker")
    if broker is not None and not isinstance(broker, dict):
        raise ValueError("invalid_snapshot")
    broker = broker or {}
    timestamp_state = _health_timestamp(health.get("timestamp"))
    return {
        "schema_version": SCHEMA_VERSION,
        "capture_started_at": start.isoformat(), "capture_finished_at": end.isoformat(),
        "process_identity": identity, "health_timestamp": timestamp_state,
        "metrics": {
            "kis_requests": _state(broker.get("kis_requests"), _project_kis),
            "kis_direct_requests": _state(broker.get("kis_direct_requests"), _project_direct),
            "analysis_daily_requests": _state(broker.get("analysis_daily_requests"), _project_analysis),
        },
        "loop_heartbeat": _project_heartbeat(health.get("loop_status")),
    }


def write_private(path, value):
    path = _safe_absolute(path, output=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                         separators=(",", ":")).encode("utf-8") + b"\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise ValueError("output_create_failed") from exc
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("output_write_failed")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def capture_snapshot(output, *, health_reader=local_health, service_reader=service_identity,
                     listener_reader=None, now=None):
    """Read health between two identical fixed service identities, then write once."""
    _safe_absolute(output, output=True)
    clock = (lambda: datetime.now(timezone.utc)) if now is None else now
    listener_reader = dashboard_listener_pid if listener_reader is None else listener_reader
    started = _now_iso(clock())
    before = _validate_identity(service_reader())
    if listener_reader() != before["MainPID"]:
        raise ValueError("dashboard_owner_mismatch")
    health = health_reader()
    after = _validate_identity(service_reader())
    if before != after:
        raise ValueError("process_identity_changed")
    if listener_reader() != after["MainPID"]:
        raise ValueError("dashboard_owner_mismatch")
    finished = _now_iso(clock())
    snapshot = build_snapshot(health, before, started, finished)
    write_private(output, snapshot)
    return snapshot


def _validate_snapshot(data):
    if not isinstance(data, dict) or set(data) != {"schema_version", "capture_started_at", "capture_finished_at",
                                                    "process_identity", "health_timestamp", "metrics", "loop_heartbeat"}:
        raise ValueError("invalid_snapshot")
    if data["schema_version"] != SCHEMA_VERSION:
        raise ValueError("invalid_snapshot")
    timestamp = data["health_timestamp"]
    if not isinstance(timestamp, dict) or timestamp.get("status") == "missing" and set(timestamp) != {"status"}:
        raise ValueError("invalid_snapshot")
    if timestamp.get("status") == "present":
        if (set(timestamp) != {"status", "value", "timezone"} or type(timestamp["value"]) is not str
                or timestamp["timezone"] not in {"aware", "unspecified"}):
            raise ValueError("invalid_snapshot")
    elif timestamp.get("status") != "missing":
        raise ValueError("invalid_snapshot")
    metrics = data["metrics"]
    names = ("kis_requests", "kis_direct_requests", "analysis_daily_requests")
    if not isinstance(metrics, dict) or set(metrics) != set(names):
        raise ValueError("invalid_snapshot")
    for name in names:
        state = metrics[name]
        if not isinstance(state, dict) or state.get("status") == "missing" and set(state) != {"status"}:
            raise ValueError("invalid_snapshot")
        if state.get("status") == "present":
            if set(state) != {"status", "value"} or not isinstance(state["value"], dict):
                raise ValueError("invalid_snapshot")
        elif state.get("status") != "missing":
            raise ValueError("invalid_snapshot")
    heartbeat = data["loop_heartbeat"]
    if not isinstance(heartbeat, dict) or heartbeat.get("status") == "missing" and set(heartbeat) != {"status"}:
        raise ValueError("invalid_snapshot")
    if heartbeat.get("status") == "present":
        if set(heartbeat) != {"status", "value"} or not isinstance(heartbeat["value"], dict):
            raise ValueError("invalid_snapshot")
    elif heartbeat.get("status") != "missing":
        raise ValueError("invalid_snapshot")
    return build_snapshot({
        "timestamp": timestamp.get("value") if timestamp.get("status") == "present" else None,
        "broker": {key: state.get("value") for key, state in metrics.items() if state.get("status") == "present"},
        "loop_status": {"kr_screener": heartbeat.get("value")}
        if heartbeat.get("status") == "present" else {},
    }, data["process_identity"], data["capture_started_at"], data["capture_finished_at"])


def read_snapshot(path):
    return _validate_snapshot(_json_loads_strict(read_bounded(path)))


def _diff_rows(before, after, dimensions, counters):
    old = {tuple(row[field] for field in dimensions): row for row in before}
    new = {tuple(row[field] for field in dimensions): row for row in after}
    result = []
    for key in sorted(set(old) | set(new)):
        base, current = old.get(key), new.get(key)
        row = {field: value for field, value in zip(dimensions, key)}
        if current is None:
            row.update(status="unavailable", reason="missing_after_row")
        elif base is None:
            row.update(status="available", baseline="new_row_baseline_zero",
                       delta={field: current[field] for field in counters})
        elif any(current[field] < base[field] for field in counters):
            row.update(status="unavailable", reason="counter_decreased")
        else:
            row.update(status="available", delta={field: current[field] - base[field] for field in counters})
        result.append(row)
    return result


def _diff_metric(before, after, name):
    if before["status"] != "present" or after["status"] != "present":
        return {"status": "unavailable", "reason": "missing_metric"}
    old, new = before["value"], after["value"]
    if (old["scope"], old["window"]) != (new["scope"], new["window"]):
        return {"status": "unavailable", "reason": "scope_or_window_changed"}
    if name == "kis_requests":
        if not old["available"] or not new["available"]:
            return {"status": "unavailable", "reason": "metric_unavailable"}
        result = {"scope": old["scope"], "window": old["window"],
                  "logical": _diff_rows(old["logical"], new["logical"], _KIS_LOGICAL_DIMENSIONS, _KIS_LOGICAL_COUNTERS),
                  "http": _diff_rows(old["http"], new["http"], _KIS_HTTP_DIMENSIONS, _KIS_HTTP_COUNTERS)}
        return _row_status(result)
    if name == "kis_direct_requests":
        if not old["available"] or not new["available"]:
            return {"status": "unavailable", "reason": "metric_unavailable"}
        return _row_status({"scope": old["scope"], "window": old["window"],
                            "http": _diff_rows(old["http"], new["http"], _DIRECT_HTTP_DIMENSIONS, _DIRECT_HTTP_COUNTERS)})
    if any(new[field] < old[field] for field in _ANALYSIS_COUNTERS):
        return {"status": "unavailable", "reason": "counter_decreased"}
    return {"status": "available", "scope": old["scope"], "window": old["window"],
            "entries": {"before": old["entries"], "after": new["entries"]},
            "delta": {field: new[field] - old[field] for field in _ANALYSIS_COUNTERS}}


def _row_status(result):
    rows = [row for key in ("logical", "http") for row in result.get(key, [])]
    unavailable = sum(row.get("status") == "unavailable" for row in rows)
    if unavailable == 0:
        status = "available"
    elif unavailable == len(rows):
        status = "unavailable"
    else:
        status = "partial"
    return {"status": status, **result}


def diff_snapshots(before, after):
    before, after = _validate_snapshot(before), _validate_snapshot(after)
    if before["process_identity"] != after["process_identity"]:
        raise ValueError("process_identity_changed")
    if _aware_iso(after["capture_started_at"]) < _aware_iso(before["capture_finished_at"]):
        raise ValueError("capture_windows_unordered")
    metrics = {name: _diff_metric(before["metrics"][name], after["metrics"][name], name)
               for name in ("kis_requests", "kis_direct_requests", "analysis_daily_requests")}
    statuses = [metric["status"] for metric in metrics.values()]
    complete = all(status == "available" for status in statuses)
    return {
        "schema_version": DIFF_SCHEMA_VERSION,
        "before_capture_finished_at": before["capture_finished_at"],
        "after_capture_started_at": after["capture_started_at"],
        "process_identity": before["process_identity"],
        "summary": {"complete": complete, "comparable": complete,
                    "status": "complete" if complete else "unavailable" if "unavailable" in statuses else "partial"},
        "metrics": metrics,
        "limitations": {"analysis_daily_event_loop_epoch": "unverified",
                        "reason": "same_service_pid_does_not_prove_broker_event_loop_epoch"},
    }


def _error(reason):
    # Do not include host exceptions, file contents, command output, or URL input.
    print(json.dumps({"status": "error", "reason": reason}), file=sys.stderr)
    return 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--output", required=True)
    diff = commands.add_parser("diff")
    diff.add_argument("--before", required=True)
    diff.add_argument("--after", required=True)
    diff.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            result = capture_snapshot(args.output)
            print(json.dumps({"status": "captured", "capture_finished_at": result["capture_finished_at"]}))
        else:
            result = diff_snapshots(read_snapshot(args.before), read_snapshot(args.after))
            if args.output:
                write_private(args.output, result)
                print(json.dumps({"status": "diff_written"}))
            else:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0
    except (ValueError, OSError):
        return _error("readout_rejected")


if __name__ == "__main__":
    raise SystemExit(main())
