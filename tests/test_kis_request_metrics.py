"""관측 source 누출, 비밀 태그, 무한 카디널리티와 기록 실패를 방어한다."""
import asyncio
import importlib

import pytest


@pytest.fixture
def metrics(monkeypatch):
    m = importlib.import_module("src.utils.kis_request_metrics")
    monkeypatch.setattr(m, "_recorder", m.RequestMetrics())
    return m


def test_concurrent_sources_and_nested_context_reset(metrics):
    async def worker(source):
        with metrics.request_source(source):
            await asyncio.sleep(0)
            with metrics.logical_operation("positions"):
                metrics.record_cache("hit")
            with metrics.request_source("dashboard_settlement"):
                await asyncio.sleep(0)
            with metrics.logical_operation("account_summary"):
                pass

    async def run():
        await asyncio.gather(worker("portfolio_sync"), worker("fill_check"))
        with metrics.logical_operation("daily_fills"):
            pass
    asyncio.run(run())
    rows = metrics.snapshot()["logical"]
    assert {(r["source"], r["operation"], r["calls"]) for r in rows} == {
        ("portfolio_sync", "positions", 1), ("portfolio_sync", "account_summary", 1),
        ("fill_check", "positions", 1), ("fill_check", "account_summary", 1),
        ("unknown", "daily_fills", 1),
    }
    assert sum(r["cache_hits"] for r in rows) == 2


def test_untrusted_dimensions_are_bounded_and_never_retained(metrics):
    for i in range(1000):
        with metrics.request_source(f"private-account-{i}"), metrics.logical_operation(f"private-op-{i}"):
            with metrics.request_page(999 + i):
                metrics.record_http_attempt(f"private-token-{i}", 0)
                metrics.record_http_result(f"private-token-{i}", "private-message")
    snapshot = metrics.snapshot()
    assert len(snapshot["logical"]) == 1
    assert len(snapshot["http"]) == 1
    assert "private" not in str(snapshot)
    assert snapshot["http"][0]["attempts"] == 1000
    snapshot["http"][0]["attempts"] = -1
    assert metrics.snapshot()["http"][0]["attempts"] == 1000


def test_decorator_restores_source_on_cancellation(metrics):
    @metrics.with_request_source("startup")
    async def cancel():
        with metrics.logical_operation("account_summary"):
            raise asyncio.CancelledError

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await cancel()
        with metrics.logical_operation("positions"):
            pass
    asyncio.run(run())
    assert {(r["source"], r["operation"]) for r in metrics.snapshot()["logical"]} == {
        ("startup", "account_summary"), ("unknown", "positions")}


def test_recorder_failure_is_unknown_not_zero(metrics, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("private failure")
    monkeypatch.setattr(metrics._recorder, "increment", broken)
    with metrics.logical_operation("positions"):
        metrics.record_cache("miss")
        metrics.record_http_attempt("TTTC8434R", 0)
        metrics.record_http_result("TTTC8434R", "success")
    assert metrics.snapshot()["available"] is False
    assert "private" not in str(metrics.snapshot())
