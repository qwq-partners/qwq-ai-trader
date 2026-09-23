"""실 브로커와 가짜 HTTP를 연결해 전송 규약·반환·취소 무변경을 검증한다."""
import asyncio
from types import SimpleNamespace

import pytest

from src.execution.broker import kis_kr
from src.utils import kis_request_metrics as metrics
from src.utils import kis_rate_limit
from test_kis_pagination_protocol import (
    broker, _Resp, _BASELINE_HEADERS, _BALANCE_PARAMS, _POS1, _POS2, _POS3,
    _balance_page,
)


@pytest.fixture(autouse=True)
def isolated_metrics(monkeypatch):
    monkeypatch.setattr(metrics, "_recorder", metrics.RequestMetrics())
    monkeypatch.setattr(kis_kr.KISBroker, "is_connected", property(lambda self: True))
    async def no_sleep(delay):
        pass
    monkeypatch.setattr(kis_kr.asyncio, "sleep", no_sleep)


def install(broker, responses):
    sent = []
    seq = iter(responses)
    def get(url, headers, params):
        sent.append((url, dict(headers), dict(params)))
        response = next(seq)
        if isinstance(response, BaseException):
            raise response
        return response
    broker._session = SimpleNamespace(closed=False, get=get)
    return sent


def response(body, continuation="D", status=200):
    r = _Resp(body, continuation)
    r.status = status
    return r


def row(table, operation, page=1):
    return next(r for r in metrics.snapshot()[table]
                if r["operation"] == operation and (table == "logical" or r["page"] == page))


def expected_request(params, continuation=None, tr_id="TTTC8434R", endpoint="inquire-balance"):
    headers = dict(_BASELINE_HEADERS, tr_id=tr_id)
    if continuation:
        headers["tr_cont"] = continuation
    return ("http://x/uapi/domestic-stock/v1/trading/" + endpoint, headers, params)


def test_balance_positions_cache_does_not_add_http(broker):
    sent = install(broker, [
        response(dict(_balance_page([_POS1]), output2=[{"dnca_tot_amt": "100", "scts_evlu_amt": "200"}])),
        response({"rt_cd": "0", "output": {"nrcvb_buy_amt": "90"}}),
    ])
    async def run():
        with metrics.request_source("portfolio_sync"):
            balance = await broker.get_account_balance()
            positions = await broker.get_positions()
        return balance, positions
    balance, positions = asyncio.run(run())
    assert balance["available_cash"] == 90
    assert positions["005930"].quantity == 3
    assert sent == [expected_request(_BALANCE_PARAMS), expected_request({
        "CANO": "12345678", "ACNT_PRDT_CD": "01", "PDNO": "005930", "ORD_UNPR": "0",
        "ORD_DVSN": "00", "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N",
    }, tr_id="TTTC8908R", endpoint="inquire-psbl-order")]
    assert row("logical", "account_summary")["calls"] == 1
    assert row("logical", "positions")["cache_hits"] == 1
    assert row("logical", "positions")["cache_misses"] == 0
    assert row("http", "account_summary")["attempts"] == 1
    assert row("http", "orderable_cash")["attempts"] == 1
    assert {r["source"] for r in metrics.snapshot()["http"]} == {"portfolio_sync"}


@pytest.mark.parametrize("external", [False, True])
def test_three_pages_keep_every_request_and_separate_pages(broker, external):
    sent = install(broker, [response(_balance_page([p], f"K{i}"), cont)
                           for i, (p, cont) in enumerate([(_POS1, "F"), (_POS2, "M"), (_POS3, "D")], 1)])
    result = asyncio.run(broker.get_positions_for_account("12345678", "01") if external else broker.get_positions())
    assert len(result[0] if external else result) == 3
    assert sent == [expected_request(_BALANCE_PARAMS),
                    expected_request(dict(_BALANCE_PARAMS, CTX_AREA_FK100="K1", CTX_AREA_NK100="K1"), "N"),
                    expected_request(dict(_BALANCE_PARAMS, CTX_AREA_FK100="K2", CTX_AREA_NK100="K2"), "N")]
    operation = "external_positions" if external else "positions"
    assert row("logical", operation)["calls"] == 1
    assert [(r["page"], r["attempts"], r["success"]) for r in metrics.snapshot()["http"]] == [(1, 1, 1), (2, 1, 1), (3, 1, 1)]


def test_second_page_rejection_retries_same_request_once(broker):
    sent = install(broker, [response(_balance_page([_POS1], "K1"), "F"),
                           response({"rt_cd": "1", "msg_cd": "EGW00215"}, status=500),
                           response(_balance_page([_POS2], "K2"))])
    assert len(asyncio.run(broker.get_positions())) == 2
    second = expected_request(dict(_BALANCE_PARAMS, CTX_AREA_FK100="K1", CTX_AREA_NK100="K1"), "N")
    assert sent == [expected_request(_BALANCE_PARAMS), second, second]
    stats = row("http", "positions", 2)
    assert (stats["attempts"], stats["retries"], stats["egw00215"], stats["success"], stats["http_error"]) == (2, 1, 1, 1, 1)


@pytest.mark.parametrize("failure", ["reject", "timeout", "client_error", "cancel"])
def test_exhaustion_and_cancellation_preserve_contract(broker, failure):
    if failure == "reject":
        responses = [response({"rt_cd": "1", "msg_cd": "EGW00215"}, status=500) for _ in range(3)]
    elif failure == "timeout":
        responses = [asyncio.TimeoutError("synthetic") for _ in range(3)]
    elif failure == "client_error":
        responses = [kis_kr.aiohttp.ClientError("synthetic") for _ in range(3)]
    else:
        responses = [asyncio.CancelledError()]
    sent = install(broker, responses)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(broker.get_positions())
    else:
        assert asyncio.run(broker.get_positions()) == {}
    stats = row("http", "positions")
    assert len(sent) == stats["attempts"] == (1 if failure == "cancel" else 3)
    assert stats["retries"] == (0 if failure == "cancel" else 2)
    assert stats[{"reject": "egw00215", "timeout": "network_error", "client_error": "network_error", "cancel": "cancelled"}[failure]] == len(sent)
    assert sent == [expected_request(_BALANCE_PARAMS)] * len(sent)


def test_recorder_failure_does_not_swallow_success_or_cancellation(broker, monkeypatch):
    def broken(*args):
        raise ValueError("synthetic observer failure")
    monkeypatch.setattr(metrics._recorder, "increment", broken)
    sent = install(broker, [response(_balance_page([_POS1])), asyncio.CancelledError()])
    assert len(asyncio.run(broker.get_positions())) == 1
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(broker.get_positions())
    assert sent == [expected_request(_BALANCE_PARAMS)] * 2
    assert metrics.snapshot()["available"] is False


def test_health_reads_recent_limiter_without_mutation_and_distinguishes_unknown(broker, monkeypatch):
    from src.dashboard.data_collector import DashboardDataCollector
    bot = SimpleNamespace(broker=broker, engine=SimpleNamespace(risk_manager=None), strategy_manager=None)
    collector = DashboardDataCollector(bot)
    monkeypatch.setattr(kis_rate_limit.time, "monotonic", lambda: 10.0)
    kis_rate_limit._calls.extend([8.0, 9.5, 10.0])
    assert collector.get_system_health()["broker"]["rate_limit_calls_last_sec"] == 2
    assert list(kis_rate_limit._calls) == [8.0, 9.5, 10.0]
    assert collector.get_system_health()["broker"]["kis_requests"]["available"] is True
    monkeypatch.setattr(kis_rate_limit, "_calls", None)
    assert collector.get_system_health()["broker"]["rate_limit_calls_last_sec"] is None


def test_other_logical_operation_counts_quote_without_retaining_symbol(broker):
    install(broker, [response({"rt_cd": "0", "output": {"stck_prpr": "100"}})])
    asyncio.run(broker.get_quote("005930"))
    assert row("logical", "other")["calls"] == 1
    assert row("http", "other")["attempts"] == 1
    assert "005930" not in str(metrics.snapshot())


@pytest.mark.parametrize("age,expected_hits,expected_requests", [(4.0, 1, 1), (5.0, 0, 2)])
def test_snapshot_ttl_and_single_use_unchanged(broker, monkeypatch, age, expected_hits, expected_requests):
    monkeypatch.setattr(kis_kr.time, "monotonic", lambda: 10.0)
    broker._balance_snapshot = (10.0 - age, [_POS1])
    sent = install(broker, [response(_balance_page([_POS1])), response(_balance_page([_POS1]))])
    assert len(asyncio.run(broker.get_positions())) == 1
    assert len(asyncio.run(broker.get_positions())) == 1
    assert len(sent) == expected_requests
    stats = row("logical", "positions")
    assert (stats["calls"], stats["cache_hits"], stats["cache_misses"]) == (2, expected_hits, expected_requests)


def test_rejection_in_http_200_is_counted_without_new_retry(broker):
    sent = install(broker, [response({"rt_cd": "1", "msg_cd": "EGW00215"})])
    assert asyncio.run(broker.get_positions()) == {}
    stats = row("http", "positions")
    assert (stats["attempts"], stats["retries"], stats["egw00215"], stats["api_error"]) == (1, 0, 1, 1)
    assert sent == [expected_request(_BALANCE_PARAMS)]


def test_non_kr_health_preserves_legacy_broker_counter():
    from src.dashboard.data_collector import DashboardDataCollector
    broker = SimpleNamespace(is_connected=True, _api_call_times=[1, 2, 3])
    collector = DashboardDataCollector(SimpleNamespace(
        broker=broker, engine=SimpleNamespace(risk_manager=None), strategy_manager=None))
    assert collector.get_system_health()["broker"] == {
        "connected": True, "rate_limit_calls_last_sec": 3, "pending_orders": 0}


def test_missing_recorder_does_not_change_request_or_return(broker, monkeypatch):
    monkeypatch.setattr(metrics, "_recorder", None)
    sent = install(broker, [response(_balance_page([_POS1]))])
    assert len(asyncio.run(broker.get_positions())) == 1
    assert sent == [expected_request(_BALANCE_PARAMS)]
    assert metrics.snapshot()["available"] is False


def test_cancel_during_backoff_counts_only_the_completed_rejection(broker, monkeypatch):
    async def cancel_sleep(delay):
        raise asyncio.CancelledError
    monkeypatch.setattr(kis_kr.asyncio, "sleep", cancel_sleep)
    sent = install(broker, [response({"rt_cd": "1", "msg_cd": "EGW00215"}, status=500)])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(broker.get_positions())
    stats = row("http", "positions")
    assert (stats["attempts"], stats["retries"], stats["egw00215"], stats["cancelled"]) == (1, 0, 1, 0)
    assert sent == [expected_request(_BALANCE_PARAMS)]


def test_cancel_before_transmission_does_not_count_http(broker):
    async def cancel_limit(tr_id):
        raise asyncio.CancelledError
    broker._rate_limit = cancel_limit
    sent = install(broker, [])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(broker.get_positions())
    assert metrics.snapshot()["http"] == []
    assert row("logical", "positions")["calls"] == 1
    assert sent == []


@pytest.mark.parametrize("operation,new_tr", [("daily_fills", False), ("daily_fills", True), ("open_orders", False), ("open_orders", True)])
def test_daily_and_open_order_sources_keep_both_tr_sets(broker, monkeypatch, operation, new_tr):
    monkeypatch.setattr(kis_kr, "_TR_NEW", new_tr)
    install(broker, [response({"rt_cd": "0", "output1": [], "output": []})])
    async def run():
        with metrics.request_source("fill_check"):
            if operation == "daily_fills":
                return await broker._query_daily_fills("20260923")
            return await broker.get_exchange_open_orders()
    assert asyncio.run(run()) == []
    assert row("logical", operation)["calls"] == 1
    stats = row("http", operation)
    assert stats["source"] == "fill_check"
    assert stats["tr_id"] == {
        ("daily_fills", False): "TTTC8001R", ("daily_fills", True): "TTTC0081R",
        ("open_orders", False): "TTTC8036R", ("open_orders", True): "TTTC0084R",
    }[(operation, new_tr)]
