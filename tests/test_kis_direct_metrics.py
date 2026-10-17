"""직접 KIS GET 관측의 고정 차원과 실패 격리를 검증한다."""
import asyncio
import importlib

import aiohttp
import pytest


class _Response:
    def __init__(self, status=200, body=None, error=None):
        self.status = status
        self._body = body
        self._error = error

    async def json(self):
        if self._error:
            raise self._error
        return self._body


@pytest.fixture
def metrics(monkeypatch):
    module = importlib.import_module("src.utils.kis_direct_metrics")
    recorder = module.DirectKISMetrics()
    monkeypatch.setattr(module, "_recorder", recorder)
    return module


def _row(snapshot, operation):
    return next(row for row in snapshot["http"] if row["operation"] == operation)


def test_success_snapshot_is_fixed_and_independent(metrics):
    async def run():
        with metrics.observe_get("market_data", "sector_indices", "FHPUP02140000") as observed:
            assert observed.observe_response(_Response()) is True
            data = await observed.json(_Response(body={"rt_cd": "0"}))
            observed.api_envelope(data)

    asyncio.run(run())
    first = metrics.snapshot()
    row = _row(first, "sector_indices")
    assert first["available"] is True
    assert (first["scope"], first["window"]) == (
        "kr_market_data_and_screener_get", "process_lifetime")
    assert set(row) == {
        "component", "operation", "tr_id", "attempts", "success", "http_error",
        "api_error", "network_error", "cancelled", "invalid_json", "other",
        "egw00201", "egw00215",
    }
    assert (row["attempts"], row["success"]) == (1, 1)
    row["success"] = 999
    assert _row(metrics.snapshot(), "sector_indices")["success"] == 1


def test_untrusted_labels_collapse_without_retaining_identity(metrics):
    for index in range(100):
        with metrics.observe_get(
            f"private-account-{index}", f"005930-{index}", f"private-token-{index}"
        ) as observed:
            observed.observe_response(_Response(status=503))
    snapshot = metrics.snapshot()
    assert len(snapshot["http"]) == 1
    row = snapshot["http"][0]
    assert (row["component"], row["operation"], row["tr_id"]) == ("other",) * 3
    assert (row["attempts"], row["http_error"]) == (100, 100)
    assert "private" not in str(snapshot)
    assert "005930" not in str(snapshot)


@pytest.mark.parametrize(
    ("status", "body", "json_error", "field", "code"),
    [
        (503, {"msg_cd": "EGW00201"}, None, "http_error", None),
        (200, {"rt_cd": "1", "msg_cd": "EGW00201"}, None, "api_error", "egw00201"),
        (200, {"rt_cd": "1", "msg_cd": "EGW00215"}, None, "api_error", "egw00215"),
        (200, None, ValueError("synthetic"), "invalid_json", None),
        (200, None, aiohttp.ContentTypeError(None, (), message="synthetic"), "invalid_json", None),
        (200, None, aiohttp.ClientPayloadError("synthetic"), "network_error", None),
    ],
)
def test_response_outcomes_are_terminal_and_http_does_not_read_body(metrics, status, body, json_error, field, code):
    response = _Response(status=status, body=body, error=json_error)

    async def run():
        with metrics.observe_get("market_data", "sector_indices", "FHPUP02140000") as observed:
            if observed.observe_response(response):
                try:
                    data = await observed.json(response)
                except Exception:
                    return
                observed.api_envelope(data)

    asyncio.run(run())
    row = _row(metrics.snapshot(), "sector_indices")
    assert row["attempts"] == 1
    assert row[field] == 1
    assert sum(row[key] for key in ("success", "http_error", "api_error", "network_error", "cancelled", "invalid_json", "other")) == 1
    if code:
        assert row[code] == 1
    else:
        assert row["egw00201"] == row["egw00215"] == 0


def test_transport_cancellation_and_recorder_failure_do_not_change_control_flow(metrics, monkeypatch):
    async def cancelled():
        with metrics.observe_get("market_data", "sector_indices", "FHPUP02140000"):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancelled())
    assert _row(metrics.snapshot(), "sector_indices")["cancelled"] == 1

    def broken(*_args, **_kwargs):
        raise RuntimeError("observer unavailable")

    monkeypatch.setattr(metrics._recorder, "increment", broken)
    async def normal():
        with metrics.observe_get("market_data", "sector_indices", "FHPUP02140000") as observed:
            observed.observe_response(_Response())
            observed.api_envelope({"rt_cd": "0"})

    asyncio.run(normal())
    assert metrics.snapshot()["available"] is False
