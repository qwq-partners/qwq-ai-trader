"""Quote-only KIS request metrics stay fixed-label and transport-faithful."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.execution.broker import kis_kr
from src.utils import kis_request_metrics as metrics
from src.utils import kis_rate_limit


class _Response:
    def __init__(self, body, status=200):
        self.status = status
        self._body = body
        self.headers = {}

    async def json(self):
        return dict(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


@pytest.fixture(autouse=True)
def isolated_metrics(monkeypatch):
    monkeypatch.setattr(metrics, "_recorder", metrics.RequestMetrics())
    monkeypatch.setattr(kis_kr.KISBroker, "is_connected", property(lambda self: True))
    async def no_sleep(_delay):
        return None
    monkeypatch.setattr(kis_kr.asyncio, "sleep", no_sleep)
    kis_rate_limit.reset()


@pytest.fixture
def broker(monkeypatch):
    result = object.__new__(kis_kr.KISBroker)
    result._token = "test-token"
    result._token_mgr = SimpleNamespace(_access_token="test-token", _is_token_valid=lambda: True,
                                        invalidate=lambda: None)
    result.config = SimpleNamespace(base_url="http://test", account_no="12345678",
                                    account_product_cd="01", env="prod", app_key="key", app_secret="secret")
    async def no_limit(_tr_id):
        return None
    result._rate_limit = no_limit
    monkeypatch.setattr(kis_rate_limit, "release_ledger", lambda: None)
    return result


def install(broker, responses):
    sent = []
    sequence = iter(responses)
    def get(url, headers=None, params=None):
        sent.append((url, dict(headers or {}), dict(params or {})))
        return next(sequence)
    broker._session = SimpleNamespace(closed=False, get=get)
    return sent


def http_row(operation, page=1):
    return next(row for row in metrics.snapshot()["http"]
                if row["operation"] == operation and row["page"] == page)


def test_dashboard_quote_retry_and_daily_pages_have_fixed_labels(broker):
    sent = install(broker, [
        _Response({"rt_cd": "1", "msg_cd": "EGW00201"}, status=500),
        _Response({"rt_cd": "0", "output": {"stck_prpr": "100"}}),
        _Response({"rt_cd": "0", "output": {"ovtm_untp_prpr": "101"}}),
        _Response({"rt_cd": "0", "output2": [{"stck_bsop_date": "20261016", "stck_clpr": "100"}]}),
        _Response({"rt_cd": "0", "output2": [{"stck_bsop_date": "20261015", "stck_clpr": "99"}]}),
    ])

    async def run():
        with metrics.request_source("dashboard_http_indices"):
            quote = await broker.get_quote("005930")
            overtime_quote = await broker.get_overtime_quote("005930")
            prices = await broker.get_daily_prices("005930", days=2)
        return quote, overtime_quote, prices

    quote, overtime_quote, prices = asyncio.run(run())
    assert quote["price"] == 100.0
    assert overtime_quote["price"] == 101.0
    assert [row["date"] for row in prices] == ["20261015", "20261016"]
    assert [request[1]["tr_id"] for request in sent] == [
        "FHKST01010100", "FHKST01010100", "FHPST02300000", "FHKST03010100", "FHKST03010100"]
    quote_http = http_row("quote")
    assert (quote_http["source"], quote_http["tr_id"], quote_http["attempts"],
            quote_http["retries"], quote_http["egw00201"], quote_http["http_error"],
            quote_http["success"]) == ("dashboard_http_indices", "FHKST01010100", 2, 1, 1, 1, 1)
    assert [(http_row("daily_prices", page)["tr_id"], http_row("daily_prices", page)["attempts"])
            for page in (1, 2)] == [("FHKST03010100", 1), ("FHKST03010100", 1)]
    assert (http_row("overtime_quote")["source"], http_row("overtime_quote")["tr_id"],
            http_row("overtime_quote")["success"]) == (
                "dashboard_http_indices", "FHPST02300000", 1)


def test_quote_sources_are_task_local_and_reset_to_unknown():
    async def worker(source, operation, tr_id):
        with metrics.request_source(source), metrics.logical_operation(operation):
            await asyncio.sleep(0)
            metrics.record_http_attempt(tr_id, 0)
            metrics.record_http_result(tr_id, "success")

    async def run():
        await asyncio.gather(
            worker("dashboard_sse_indices", "quote", "FHKST01010100"),
            worker("dashboard_http_indices", "overtime_quote", "FHPST02300000"),
        )
        with metrics.logical_operation("quote"):
            metrics.record_http_attempt("FHKST01010100", 0)
            metrics.record_http_result("FHKST01010100", "success")

    asyncio.run(run())
    assert {(row["source"], row["operation"], row["tr_id"], row["attempts"])
            for row in metrics.snapshot()["http"]} == {
        ("dashboard_sse_indices", "quote", "FHKST01010100", 1),
        ("dashboard_http_indices", "overtime_quote", "FHPST02300000", 1),
        ("unknown", "quote", "FHKST01010100", 1),
    }
