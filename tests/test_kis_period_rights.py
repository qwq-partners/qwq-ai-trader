"""66차 — CTRGA011R 수집 로직: 월 분할·전체 페이지·반복/상한/오류/빈 응답 구분·집계 (합성 응답)."""
import asyncio
from datetime import date
from decimal import Decimal

from src.analytics.kis_period_rights import classify_page, collect_window, month_windows, summarize_rights


def _page(rows, cont, fk="", nk="", rt="0"):
    return {"rt_cd": rt, "msg_cd": "MCA00000", "msg1": "정상", "output": rows, "_tr_cont": cont,
            "ctx_area_fk100": fk, "ctx_area_nk100": nk}


def _row(t="01", amt="1000", pdno="005930"):
    return {"rght_type_cd": t, "last_alct_amt": amt, "pdno": pdno, "bass_dt": "20250105"}


def _fetcher(script):
    calls = []

    async def fetch(start, end, *, fk100, nk100, tr_cont):
        calls.append({"fk": fk100, "nk": nk100, "tr_cont": tr_cont})
        return script[min(len(calls) - 1, len(script) - 1)]
    fetch.calls = calls
    return fetch


def test_month_windows_clip_to_requested_range():
    w = month_windows(date(2025, 1, 15), date(2025, 3, 10))
    assert w == [("20250115", "20250131"), ("20250201", "20250228"), ("20250301", "20250310")]
    assert month_windows(date(2026, 10, 1), date(2026, 10, 1)) == [("20261001", "20261001")]
    assert len(month_windows(date(2025, 1, 1), date(2026, 10, 1))) == 22


def test_multi_page_window_completes_with_next_keys():
    fetch = _fetcher([_page([_row()], "F", fk="f1", nk="n1"), _page([_row(amt="2000")], "M", fk="f2", nk="n2"),
                      _page([_row(amt="3000")], "D")])
    r = asyncio.run(collect_window(fetch, "20250101", "20250131"))
    assert r["status"] == "complete" and r["api_calls"] == 3 and len(r["rows"]) == 3
    assert fetch.calls[0] == {"fk": "", "nk": "", "tr_cont": ""}
    assert fetch.calls[1] == {"fk": "f1", "nk": "n1", "tr_cont": "N"} and fetch.calls[2]["fk"] == "f2"


def test_repeat_keys_and_max_pages_are_partial_not_complete():
    rep = _fetcher([_page([_row()], "F", fk="same", nk="same")])
    r = asyncio.run(collect_window(rep, "20250101", "20250131", max_pages=10))
    assert r["status"] == "partial_page_repeat" and r["api_calls"] == 2        # 2페이지째 같은 키 → 중단
    cap = _fetcher([_page([_row()], "F", fk=f"f{i}", nk=f"n{i}") for i in range(5)])
    r2 = asyncio.run(collect_window(cap, "20250101", "20250131", max_pages=3))
    assert r2["status"] == "partial_max_pages" and r2["api_calls"] == 3 and len(r2["rows"]) == 3


def test_error_empty_and_malformed_pages():
    err = asyncio.run(collect_window(_fetcher([_page([], "", rt="1")]), "20250101", "20250131"))
    assert err["status"] == "error" and err["pages"][0]["rt_cd"] == "1"
    empty = asyncio.run(collect_window(_fetcher([_page([], "D")]), "20250101", "20250131"))
    assert empty["status"] == "empty" and empty["rows"] == []
    nohdr = asyncio.run(collect_window(_fetcher([_page([_row()], "")]), "20250101", "20250131"))
    assert nohdr["status"] == "complete" and nohdr["tr_cont_header_missing"] is True
    bad = asyncio.run(collect_window(_fetcher([{"rt_cd": "0"}]), "20250101", "20250131"))
    assert bad["status"] == "malformed"
    assert classify_page("x") == "malformed" and classify_page({"rt_cd": "0", "output1": []}) == "empty"


def test_summary_aggregates_decimal_strings_without_interpreting():
    windows = [
        {"status": "complete", "rows": [_row("01", "1000.50"), _row("01", "abc"), _row("02", "7", pdno="000660")]},
        {"status": "partial_max_pages", "rows": [_row("02", "3", pdno="000660")]},
        {"status": "empty", "rows": []},
    ]
    s = summarize_rights(windows)
    assert s["window_statuses"] == {"complete": 1, "partial_max_pages": 1, "empty": 1} and s["complete_windows"] == 2
    assert s["by_right_type"]["01"] == {"rows": 2, "last_alct_amt_sum": "1000.50", "unparsed_amount_rows": 1, "symbol_count": 1}
    assert s["by_right_type"]["02"]["last_alct_amt_sum"] == "10" and s["unparsed_amount_rows"] == 1
    assert Decimal(s["by_right_type"]["01"]["last_alct_amt_sum"]) == Decimal("1000.50")
    assert any("소득 확정" in c for c in s["caveats"])
