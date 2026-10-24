import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "ops" / "kis_health_delta.py"


def api():
    spec = importlib.util.spec_from_file_location("kis_health_delta", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


IDENTITY = {
    "ActiveState": "active",
    "MainPID": "123",
    "InvocationID": "11111111111111111111111111111111",
    "ExecMainStartTimestampMonotonic": "9000",
    "NRestarts": "2",
}


def health(*, attempts=3, direct_attempts=4, requests=5, entries=2):
    return {
        "timestamp": "2026-10-26T00:25:00+00:00",
        "broker": {
            "reserved_cash": "never-copy",
            "kis_requests": {
                "available": True, "scope": "kr_broker_get", "window": "process_lifetime",
                "logical": [{"source": "startup", "operation": "positions", "calls": 1,
                             "cache_hits": 0, "cache_misses": 1}],
                "http": [{"source": "startup", "operation": "positions", "tr_id": "TTTC8434R",
                          "page": 1, "attempts": attempts, "retries": 0, "egw00201": 0,
                          "egw00215": 0, "success": attempts, "http_error": 0, "api_error": 0,
                          "network_error": 0, "cancelled": 0, "invalid_json": 0, "other": 0}],
            },
            "kis_direct_requests": {
                "available": True, "scope": "kr_market_data_and_screener_get", "window": "process_lifetime",
                "http": [{"component": "kr_screener", "operation": "volume_surge", "tr_id": "FHPST01710000",
                          "attempts": direct_attempts, "success": direct_attempts, "http_error": 0,
                          "api_error": 0, "network_error": 0, "cancelled": 0, "invalid_json": 0,
                          "other": 0, "egw00201": 0, "egw00215": 0}],
            },
            "analysis_daily_requests": {"scope": "analysis_daily_prices", "window": "broker_event_loop_lifetime",
                                        "requests": requests, "cache_hits": 1, "coalesced": 0,
                                        "provider_calls": 1, "entries": entries},
        },
        "loop_status": {"kr_screener": {"enabled": True, "last_attempt": 1761438240.0,
                                             "last_success": 1761438240.0,
                                             "consecutive_failures": 0, "idle_reason": None,
                                             "next_due": None}},
    }


def snap(a, data, captured="2026-10-26T00:25:01+00:00"):
    return a.build_snapshot(data, IDENTITY, captured, captured)


def test_snapshot_projects_only_whitelisted_metrics_and_marks_absence():
    a = api()
    result = snap(a, health())
    encoded = json.dumps(result)
    assert result["metrics"]["kis_requests"]["status"] == "present"
    assert result["metrics"]["analysis_daily_requests"]["value"]["entries"] == 2
    assert result["loop_heartbeat"]["status"] == "present"
    assert "reserved_cash" not in encoded
    assert "never-copy" not in encoded
    missing = snap(a, {"broker": {}})
    assert missing["metrics"]["kis_requests"] == {"status": "missing"}


def test_diff_keeps_each_metric_dimension_separate_and_does_not_sum():
    a = api()
    before = snap(a, health(attempts=3, direct_attempts=4, requests=5))
    after = snap(a, health(attempts=7, direct_attempts=9, requests=8), "2026-10-26T00:50:01+00:00")
    result = a.diff_snapshots(before, after)
    broker = result["metrics"]["kis_requests"]["http"][0]
    direct = result["metrics"]["kis_direct_requests"]["http"][0]
    assert broker["delta"]["attempts"] == 4
    assert direct["delta"]["attempts"] == 5
    assert result["metrics"]["analysis_daily_requests"]["delta"]["requests"] == 3
    assert result["metrics"]["analysis_daily_requests"]["entries"] == {"before": 2, "after": 2}


def test_diff_marks_reset_missing_and_event_loop_limit_unavailable():
    a = api()
    before = snap(a, health(attempts=5))
    reset = snap(a, health(attempts=4), "2026-10-26T00:50:01+00:00")
    reset_result = a.diff_snapshots(before, reset)
    row = reset_result["metrics"]["kis_requests"]["http"][0]
    assert row["status"] == "unavailable"
    assert row["reason"] == "counter_decreased"
    assert reset_result["metrics"]["kis_requests"]["status"] == "partial"
    assert reset_result["summary"] == {"complete": False, "comparable": False, "status": "partial"}
    missing = snap(a, {"broker": {}}, "2026-10-26T00:50:01+00:00")
    assert a.diff_snapshots(before, missing)["metrics"]["kis_requests"]["status"] == "unavailable"
    assert a.diff_snapshots(before, reset)["limitations"]["analysis_daily_event_loop_epoch"] == "unverified"


def test_analysis_entries_ttl_eviction_is_a_gauge_not_a_counter_reset():
    a = api()
    before = snap(a, health(entries=2))
    after = snap(a, health(entries=1), "2026-10-26T00:50:01+00:00")
    result = a.diff_snapshots(before, after)
    assert result["metrics"]["analysis_daily_requests"]["status"] == "available"
    assert result["metrics"]["analysis_daily_requests"]["entries"] == {"before": 2, "after": 1}


def test_snapshot_rejects_duplicate_metric_dimensions_instead_of_combining_them():
    a = api()
    data = health()
    data["broker"]["kis_requests"]["http"].append(
        dict(data["broker"]["kis_requests"]["http"][0])
    )
    with pytest.raises(ValueError, match="duplicate_metric_row"):
        snap(a, data)


def test_bounded_json_rejects_symlink_and_hardlink(tmp_path):
    a = api()
    source = tmp_path / "one.json"
    source.write_text("{}")
    symlink = tmp_path / "two.json"
    symlink.symlink_to(source)
    with pytest.raises(ValueError, match="input_path_unsafe"):
        a.read_snapshot(symlink)
    hardlink = tmp_path / "three.json"
    os.link(source, hardlink)
    with pytest.raises(ValueError, match="input_path_unsafe"):
        a.read_snapshot(source)


def test_capture_rejects_identity_change_without_writing(tmp_path):
    a = api()
    private = tmp_path / "out"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    output = private / "snapshot.json"
    calls = iter([IDENTITY, IDENTITY | {"MainPID": "124"}])
    with pytest.raises(ValueError, match="process_identity_changed"):
        a.capture_snapshot(output, health_reader=lambda: health(), service_reader=lambda: next(calls), listener_reader=lambda: "123",
                           now=lambda: datetime(2026, 10, 26, tzinfo=timezone.utc))
    assert not output.exists()


def test_capture_surfaces_local_http_failure_without_output(tmp_path):
    a = api()
    private = tmp_path / "out"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    output = private / "snapshot.json"
    with pytest.raises(ValueError, match="local_health_unavailable"):
        a.capture_snapshot(output, health_reader=lambda: (_ for _ in ()).throw(ValueError("local_health_unavailable")),
                           service_reader=lambda: IDENTITY, listener_reader=lambda: "123",
                           now=lambda: datetime(2026, 10, 26, tzinfo=timezone.utc))
    assert not output.exists()


def test_private_output_is_exclusive_and_never_serializes_other_health_fields(tmp_path):
    a = api()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    output = private / "snapshot.json"
    a.write_private(output, snap(a, health()))
    assert output.stat().st_mode & 0o777 == 0o600
    assert "never-copy" not in output.read_text()
    with pytest.raises(ValueError, match="output_create_failed"):
        a.write_private(output, {})


def test_local_health_rejects_non_200_response_without_host_access():
    a = api()

    class Response:
        status = 503

        def read(self, _limit):
            return b'{"reserved_cash":"never-copy"}'

    class Connection:
        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, *_args, **_kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    original = a.http.client.HTTPConnection
    a.http.client.HTTPConnection = Connection
    try:
        with pytest.raises(ValueError, match="local_health_unavailable"):
            a.local_health()
    finally:
        a.http.client.HTTPConnection = original


def test_capture_rejects_health_served_by_another_process(tmp_path, monkeypatch):
    a = api()
    monkeypatch.setattr(a, "dashboard_listener_pid", lambda: "999", raising=False)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    output = private / "snapshot.json"
    with pytest.raises(ValueError, match="dashboard_owner_mismatch"):
        a.capture_snapshot(output, health_reader=lambda: health(), service_reader=lambda: IDENTITY)
    assert not output.exists()


@pytest.mark.parametrize("flag", [0, 1])
def test_availability_is_strict_boolean(flag):
    a = api()
    data = health()
    data["broker"]["kis_requests"]["available"] = flag
    with pytest.raises(ValueError, match="invalid_metric_schema"):
        snap(a, data)


@pytest.mark.parametrize("value", [123, True, None, "not-a-service-invocation"])
def test_invalid_invocation_id_is_cleanly_rejected(value):
    a = api()
    with pytest.raises(ValueError, match="invalid_process_identity"):
        a.build_snapshot(health(), IDENTITY | {"InvocationID": value},
                         "2026-10-26T00:25:00+00:00", "2026-10-26T00:25:01+00:00")


def test_capture_rejects_listener_changed_after_response(tmp_path):
    a = api()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    output = private / "snapshot.json"
    owners = iter(["123", "999"])
    with pytest.raises(ValueError, match="dashboard_owner_mismatch"):
        a.capture_snapshot(output, health_reader=lambda: health(), service_reader=lambda: IDENTITY,
                           listener_reader=lambda: next(owners))
    assert not output.exists()


@pytest.mark.parametrize("stdout,rc,accepted", [("123\n123\n", 0, True), ("123\n999\n", 0, False), ("", 1, False)])
def test_listener_lookup_requires_one_pid(monkeypatch, stdout, rc, accepted):
    a = api()
    from types import SimpleNamespace
    def command(argv, **kwargs):
        assert argv == ["/usr/bin/lsof", "-nP", "-iTCP:8080", "-sTCP:LISTEN", "-t"]
        assert kwargs["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        return SimpleNamespace(stdout=stdout, returncode=rc)
    monkeypatch.setattr(a.subprocess, "run", command)
    if accepted:
        assert a.dashboard_listener_pid() == "123"
    else:
        with pytest.raises(ValueError, match="dashboard_owner_unavailable"):
            a.dashboard_listener_pid()
