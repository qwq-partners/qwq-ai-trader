"""Legacy 조회 수집 계약만 검증한다. 합성 페이지는 실 API 최종성 증명이 아니다."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.execution.safety.queries import (
    LegacyExecutionQueries, QueryRequest, QueryResponse,
)


NOW = datetime(2026, 9, 16, 15, 30, tzinfo=timezone.utc)


def response(rows=None, *, continuation="D", cursor=("", ""), kind="daily"):
    return QueryResponse(200, {"tr_cont": continuation}, {
        "rt_cd": "0", "output1" if kind == "daily" else "output": rows or [],
        "ctx_area_fk100": cursor[0], "ctx_area_nk100": cursor[1],
    })


async def daily(collector, **kwargs):
    args = dict(account_scope="internal-kr", account_number="SYNTHETIC_ACCOUNT",
                product_code="SYNTHETIC_PRODUCT", start_date="2026-09-17", end_date="2026-09-17")
    args.update(kwargs)
    return await collector.daily(**args)


def collect_pages(pages, **kwargs):
    requests = []
    async def fetch(request):
        requests.append(request)
        page = pages[len(requests) - 1]
        if isinstance(page, BaseException):
            raise page
        return page
    return LegacyExecutionQueries(fetch, clock=lambda: NOW, request_timeout=1, **kwargs), requests


def test_daily_uses_exact_legacy_all_side_full_query_and_no_credentials_in_result_repr():
    collector, requests = collect_pages([response([{"odno": "001", "account": "RAW_PRIVATE"}])])
    result = asyncio.run(daily(collector))
    request = requests[0]
    assert request.method == "GET"
    assert request.path == "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
    assert request.tr_id == "TTTC8001R" and request.tr_cont == ""
    assert dict(request.params) == {
        "CANO": "SYNTHETIC_ACCOUNT", "ACNT_PRDT_CD": "SYNTHETIC_PRODUCT",
        "INQR_STRT_DT": "20260917", "INQR_END_DT": "20260917",
        "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "01", "PDNO": "", "CCLD_DVSN": "00",
        "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_3": "00", "INQR_DVSN_1": "",
        "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
    }
    assert result.complete and result.reason == "complete"
    assert len(result.rows) == 1
    assert result.scope.account_scope == "internal-kr"
    assert result.scope.market == "KR" and result.scope.exchange_scope == "legacy_unspecified_all"
    assert result.started_at == NOW and result.completed_at == NOW
    assert result.business_date_kst == "2026-09-17"
    assert result.finality_supported is False and result.trading_permission is False
    for secret in ("SYNTHETIC_ACCOUNT", "SYNTHETIC_PRODUCT", "RAW_PRIVATE"):
        assert secret not in repr(request) + repr(result) + repr(result.pages)


def test_cancelable_uses_legacy_contract_without_manufactured_exchange_or_date_filter():
    collector, requests = collect_pages([response(kind="cancelable")])
    result = asyncio.run(collector.cancelable(account_scope="internal-kr", account_number="A", product_code="P"))
    request = requests[0]
    assert request.path == "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl"
    assert request.tr_id == "TTTC8036R"
    assert dict(request.params) == {"CANO": "A", "ACNT_PRDT_CD": "P", "INQR_DVSN_1": "1",
                                    "INQR_DVSN_2": "0", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    assert result.scope.start_date is None and result.scope.end_date is None
    assert result.complete and not result.trading_permission


@pytest.mark.parametrize("first,last", [("F", "D"), ("M", "E")])
def test_continuation_forwards_both_cursors_with_n_and_preserves_page_order(first, last):
    collector, requests = collect_pages([response([{"odno": "1"}], continuation=first, cursor=("F1", "N1")),
                                        response([{"odno": "2"}], continuation=last)])
    result = asyncio.run(daily(collector))
    assert result.complete and [row["odno"] for row in result.rows] == ["1", "2"]
    assert len(result.pages) == 2
    assert requests[1].tr_cont == "N"
    assert requests[1].params["CTX_AREA_FK100"] == "F1"
    assert requests[1].params["CTX_AREA_NK100"] == "N1"
    assert requests[0].params["CTX_AREA_FK100"] == ""
    assert result.pages[0].request_cont == "" and result.pages[0].request_cursor == ("", "")
    assert result.pages[0].response_cont == first and result.pages[0].next_cursor == ("F1", "N1")
    assert result.pages[1].request_cont == "N" and result.pages[1].request_cursor == ("F1", "N1")
    assert result.pages[1].response_cont == last and result.pages[1].next_cursor == ("", "")


@pytest.mark.parametrize("last", ["D", "E"])
def test_empty_last_page_is_complete_but_never_finality_or_startup_permission(last):
    collector, requests = collect_pages([response(continuation=last)])
    result = asyncio.run(daily(collector))
    assert result.complete and not result.rows and len(requests) == 1
    assert result.finality_supported is False and result.trading_permission is False


@pytest.mark.parametrize("bad,reason", [
    (QueryResponse(503, {}, {}), "http_status"),
    (QueryResponse(200, {"tr_cont": "D"}, {"rt_cd": 0, "output1": []}), "broker_status"),
    (QueryResponse(200, {"tr_cont": "D"}, {"rt_cd": "1", "msg1": "PRIVATE"}), "broker_status"),
    (QueryResponse(200, {}, {"rt_cd": "0", "output1": []}), "continuation_header"),
    (QueryResponse(200, {"tr_cont": "D", "TR_CONT": "M"}, {"rt_cd": "0", "output1": []}), "continuation_header"),
    (QueryResponse(200, {"tr_cont": "?"}, {"rt_cd": "0", "output1": []}), "continuation_header"),
    (QueryResponse(200, {"tr_cont": "D"}, {"rt_cd": "0", "output1": {}}), "rows_shape"),
    (QueryResponse(200, {"tr_cont": "D"}, {"rt_cd": "0", "output1": ["bad"]}), "rows_shape"),
    (QueryResponse(200, {"tr_cont": "D"}, {"rt_cd": "0"}), "rows_shape"),
    (QueryResponse(200, {"tr_cont": "M"}, {"rt_cd": "0", "output1": []}), "cursor_shape"),
    (QueryResponse(200, {"tr_cont": "M"}, {"rt_cd": "0", "output1": [], "ctx_area_fk100": "", "ctx_area_nk100": ""}), "cursor_loop"),
])
def test_malformed_or_failed_page_is_incomplete_with_sanitized_reason(bad, reason):
    collector, requests = collect_pages([bad])
    result = asyncio.run(daily(collector))
    assert not result.complete and result.reason == reason
    assert len(requests) == 1
    assert "PRIVATE" not in repr(result)


def test_header_case_normalization_accepts_consistent_aliases():
    collector, _ = collect_pages([QueryResponse(200, {"Tr_Cont": "D", "TR_CONT": "D"}, {"rt_cd": "0", "output1": []})])
    result = asyncio.run(daily(collector))
    assert result.complete and result.pages[0].next_cursor is None


def test_second_page_exception_preserves_first_page_without_retry_or_error_leak():
    collector, requests = collect_pages([response([{"odno": "one"}], continuation="M", cursor=("F", "N")),
                                        RuntimeError("SYNTHETIC_ACCOUNT PRIVATE")])
    result = asyncio.run(daily(collector))
    assert not result.complete and result.reason == "fetch_failed"
    assert [row["odno"] for row in result.rows] == ["one"]
    assert len(requests) == 2
    assert "PRIVATE" not in repr(result)


def test_repeated_cursor_and_page_cap_cannot_be_complete():
    page = response([{"odno": "one"}], continuation="M", cursor=("F", "N"))
    collector, requests = collect_pages([page, page])
    result = asyncio.run(daily(collector))
    assert not result.complete and result.reason == "cursor_loop" and len(requests) == 2
    collector, requests = collect_pages([page], max_pages=1)
    result = asyncio.run(daily(collector))
    assert not result.complete and result.reason == "page_limit" and len(requests) == 1


def test_request_timeout_is_bounded_and_not_retried():
    calls = []
    async def fetch(request):
        calls.append(request)
        await asyncio.Event().wait()
    collector = LegacyExecutionQueries(fetch, clock=lambda: NOW, request_timeout=.001)
    result = asyncio.run(daily(collector))
    assert not result.complete and result.reason == "timeout" and len(calls) == 1


def test_cancellation_propagates_without_converting_to_incomplete():
    async def scenario():
        entered = asyncio.Event()
        async def fetch(request):
            entered.set()
            await asyncio.Event().wait()
        collector = LegacyExecutionQueries(fetch, clock=lambda: NOW, request_timeout=1)
        task = asyncio.create_task(daily(collector))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())


def test_request_and_collected_rows_are_detached_from_mutable_sources():
    params = {"CANO": "A"}
    request = QueryRequest("/read", "TR", params)
    params["CANO"] = "B"
    assert request.params["CANO"] == "A"
    with pytest.raises(TypeError):
        request.params["CANO"] = "C"
    first_rows = [{"odno": "one", "nested": {"value": 1}}]
    calls = []
    async def fetch(request):
        calls.append(request)
        if len(calls) == 1:
            return response(first_rows, continuation="M", cursor=("F", "N"))
        first_rows[0]["nested"]["value"] = 99
        return response()
    collector = LegacyExecutionQueries(fetch, clock=lambda: NOW, request_timeout=1)
    result = asyncio.run(daily(collector))
    assert result.rows[0]["nested"]["value"] == 1


@pytest.mark.parametrize("kwargs", [{"start_date": "20260917"}, {"end_date": "2026-09-16"},
                                   {"account_scope": " "}, {"account_scope": "internal "}])
def test_invalid_query_scope_rejected_before_fetch_without_echo(kwargs):
    collector, requests = collect_pages([])
    with pytest.raises(ValueError) as exc:
        asyncio.run(daily(collector, **kwargs))
    assert not requests and "SYNTHETIC_ACCOUNT" not in str(exc.value)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_invalid_request_timeout_rejected(timeout):
    with pytest.raises(ValueError):
        LegacyExecutionQueries(lambda _: None, clock=lambda: NOW, request_timeout=timeout)


def test_naive_clock_is_rejected_before_fetch():
    calls = []
    async def fetch(request): calls.append(request)
    collector = LegacyExecutionQueries(fetch, clock=lambda: NOW.replace(tzinfo=None), request_timeout=1)
    with pytest.raises(ValueError):
        asyncio.run(daily(collector))
    assert not calls


def test_failed_attempt_records_completion_time_instead_of_previous_success_time():
    times = iter([NOW, NOW + timedelta(seconds=1), NOW + timedelta(seconds=2)])
    async def fetch(request):
        raise RuntimeError("PRIVATE")
    collector = LegacyExecutionQueries(fetch, clock=lambda: next(times), request_timeout=1)
    result = asyncio.run(daily(collector))
    assert result.reason == "fetch_failed"
    assert result.completed_at == NOW + timedelta(seconds=2)


def test_second_page_clock_failure_preserves_prior_rows_as_incomplete():
    times = iter([NOW, NOW, NOW, NOW.replace(tzinfo=None)])
    async def fetch(request):
        return response([{"odno": "one"}], continuation="M", cursor=("F", "N"))
    collector = LegacyExecutionQueries(fetch, clock=lambda: next(times), request_timeout=1)
    result = asyncio.run(daily(collector))
    assert not result.complete and result.reason == "clock_invalid"
    assert [row["odno"] for row in result.rows] == ["one"]


def test_ancient_date_is_rejected_without_switching_to_historical_tr():
    collector, requests = collect_pages([response()])
    with pytest.raises(ValueError):
        asyncio.run(daily(collector, start_date="0001-01-01"))
    assert not requests


def test_default_cap_ends_after_ten_pages_without_retry():
    pages = [response(continuation="M", cursor=("F", str(index))) for index in range(10)]
    collector, requests = collect_pages(pages)
    result = asyncio.run(daily(collector))
    assert result.reason == "page_limit" and not result.complete and len(requests) == 10


@pytest.mark.parametrize("cap", [0, -1, True, 1.5])
def test_invalid_page_limit_rejected(cap):
    with pytest.raises(ValueError):
        collect_pages([], max_pages=cap)


def test_non_utc_aware_clock_is_normalized_with_kst_business_date():
    kst = timezone(timedelta(hours=9))
    async def fetch(request): return response()
    collector = LegacyExecutionQueries(fetch, clock=lambda: NOW.astimezone(kst), request_timeout=1)
    result = asyncio.run(daily(collector))
    assert result.started_at.tzinfo is timezone.utc
    assert result.pages[0].started_at == NOW and result.business_date_kst == "2026-09-17"


@pytest.mark.parametrize("start,end,valid", [("2026-06-01", "2026-09-17", True),
                                           ("2026-05-31", "2026-09-17", False),
                                           ("2026-09-17", "2026-09-18", False)])
def test_daily_window_is_month_based_three_months_ago_through_kst_today(start, end, valid):
    collector, requests = collect_pages([response()])
    if valid:
        assert asyncio.run(daily(collector, start_date=start, end_date=end)).complete
    else:
        with pytest.raises(ValueError):
            asyncio.run(daily(collector, start_date=start, end_date=end))
        assert not requests


def test_month_window_handles_year_boundary():
    requests = []
    async def fetch(request):
        requests.append(request)
        return response()
    collector = LegacyExecutionQueries(fetch, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc), request_timeout=1)
    result = asyncio.run(daily(collector, start_date="2025-10-01", end_date="2026-01-01"))
    assert result.complete and requests[0].params["INQR_STRT_DT"] == "20251001"


def test_raw_cursor_provenance_is_hidden_from_page_and_collection_repr():
    collector, _ = collect_pages([response(continuation="F", cursor=("CURSOR_ACCOUNT_PRIVATE", "CURSOR_PRIVATE")), response()])
    result = asyncio.run(daily(collector))
    assert result.pages[1].request_cursor == ("CURSOR_ACCOUNT_PRIVATE", "CURSOR_PRIVATE")
    assert "CURSOR" not in repr(result) + repr(result.pages)


def test_month_rollover_between_pages_cannot_send_now_unsupported_range():
    before = datetime(2026, 9, 30, 14, 59, tzinfo=timezone.utc)
    after = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)
    times = iter([before, before, before, after])
    requests = []
    async def fetch(request):
        requests.append(request)
        return response([{"odno": "one"}], continuation="M", cursor=("F", "N"))
    collector = LegacyExecutionQueries(fetch, clock=lambda: next(times), request_timeout=1)
    result = asyncio.run(daily(collector, start_date="2026-06-01", end_date="2026-09-30"))
    assert not result.complete and result.reason == "date_outside_legacy_window"
    assert len(requests) == 1 and len(result.rows) == 1
