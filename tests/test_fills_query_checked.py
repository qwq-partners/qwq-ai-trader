"""체결 조회 완결 판정 (설계 A §5-1, 계획 2단계 T7) — 2026-09-29.

고정하는 것:
1) 기준선: `_query_daily_fills` 의 기본 호출(status 생략·None)은 요청 params·요청 tr_cont·
   호출 횟수·반환·중복 제거·예외 전파가 변경 전과 같다 — check_fills(돈 경로)가 이 함수를 쓴다.
   status 에 dict 를 넘겨도 요청 쪽은 한 바이트도 달라지지 않는다(판정만 채운다).
2) status 판정: 모든 페이지 rt_cd=="0" 이고 마지막 페이지 응답 헤더 tr_cont 가 D/E 일 때만 완결.
3) get_fills_for_date_checked: 행 형태는 get_all_fills_for_date 와 같고 정규화 실패는 미완.
4) execution_day_status DDL 이 SCHEMA_SQL 에 있다(T8 첫 항목).

모두 가짜 _api_get/가짜 HTTP — 실 KIS 호출 0건.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from src.execution.broker import kis_kr  # noqa: E402
from test_kis_ledger_pagination_2026_09_15 import _broker  # noqa: E402
from test_kis_pagination_protocol import (  # noqa: E402,F401
    _BASELINE_HEADERS, _DAILY_FILL_PARAMS, _connected, _fill_page, _install_pages, broker,
)


def _pg(items, ctx="K1", tr_cont=None, rt_cd="0"):
    d = {"rt_cd": rt_cd, "ctx_area_fk100": ctx, "ctx_area_nk100": ctx, "output1": items}
    if tr_cont is not None:
        d["_tr_cont"] = tr_cont
    return d


def _fill(odno, qty="5", price="70000", side="01", sym="005930"):
    return {"odno": odno, "pdno": sym, "prdt_name": "삼성전자", "sll_buy_dvsn_cd": side,
            "tot_ccld_qty": qty, "avg_prvs": price, "ord_tmd": "093000"}


def _record(b, pages, raise_on=None):
    """메서드 수준 가짜 _api_get — (params 사본, 요청 tr_cont 인자) 를 기록."""
    sent = []
    seq = list(pages)

    async def fake_get(url, tr_id, params, tr_cont=None):
        sent.append((tr_id, dict(params), tr_cont))
        if raise_on is not None and len(sent) == raise_on:
            raise RuntimeError("가짜 네트워크 예외")
        return seq.pop(0) if seq else _pg([], ctx="", tr_cont="D")
    b._api_get = fake_get
    return sent


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setattr(kis_kr.KISBroker, "is_connected", property(lambda self: True))
    monkeypatch.setattr(kis_kr, "_TR_NEW", False)
    return _broker()


# ── 1) 기준선 — 기본 호출은 변경 전과 같다 ───────────────────────────────────
# 각 시나리오: (페이지 목록, 기대 호출 수, 기대 반환 odno, 기대 판정(complete, reason))
_F = [_fill(str(i)) for i in range(12)]
SCENARIOS = {
    "single_D": ([_pg([_F[0]], tr_cont="D")], 1, ["0"], (True, None)),
    "single_E": ([_pg([_F[0]], ctx="", tr_cont="E")], 1, ["0"], (True, None)),
    "F_M_D": ([_pg([_F[0]], "K1", "F"), _pg([_F[1]], "K2", "M"), _pg([_F[2]], "K3", "D")],
              3, ["0", "1", "2"], (True, None)),
    "no_header_repeated_ctx": ([_pg([_F[0]], "K1")] * 10, 2, ["0"], (False, "missing_tr_cont")),
    "no_header_empty_ctx": ([_pg([_F[0]], "")], 1, ["0"], (False, "missing_tr_cont")),
    "rt_fail_page1": ([_pg([], rt_cd="1")], 1, [], (False, "rt_cd_failed:1")),
    "rt_fail_page2": ([_pg([_F[0]], "K1", "F"), _pg([_F[1]], rt_cd="-1")], 2, ["0"],
                      (False, "rt_cd_failed:-1")),
    "F_empty_ctx": ([_pg([_F[0]], "", "F")], 1, ["0"], (False, "contradictory_continuation")),
    "F_empty_page": ([_pg([_F[0]], "K1", "F"), _pg([], "K2", "F")], 2, ["0"],
                     (False, "contradictory_continuation")),
    "F_repeated_ctx": ([_pg([_F[0]], "K1", "F"), _pg([_F[1]], "K1", "F")], 2, ["0", "1"],
                       (False, "repeated_ctx")),
    "tenth_page_D": ([_pg([_F[i]], f"K{i}", "F") for i in range(9)] + [_pg([_F[9]], "K9", "D")],
                     10, [str(i) for i in range(10)], (True, None)),
    "page_cap": ([_pg([_F[i]], f"K{i}", "F") for i in range(11)], 10,
                 [str(i) for i in range(10)], (False, "page_cap")),
    "dup_odno": ([_pg([_F[0], _F[1]], "K1", "F"), _pg([_F[1], _F[2]], "K2", "D")], 2,
                 ["0", "1", "2"], (True, None)),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_default_call_matches_baseline(conn, name):
    """status 생략·None·dict 세 호출의 요청 기록과 반환이 같고, 기대 기준선과 같다."""
    pages, n_calls, odnos, _judg = SCENARIOS[name]
    runs = []
    for kwargs in ({}, {"status": None}, {"status": {}}):
        sent = _record(conn, pages)
        out = asyncio.run(conn._query_daily_fills("20260929", **kwargs))
        runs.append((sent, out))
    assert runs[0] == runs[1] == runs[2]
    sent, out = runs[0]
    assert len(sent) == n_calls
    assert [str(i.get("odno")) for i in out] == odnos
    # 1페이지 tr_cont 미송신(인자 생략), 2페이지째부터 "N" — 기존 규약 그대로
    assert [t for _tr, _p, t in sent] == [None] + ["N"] * (n_calls - 1)
    assert all(tr == "TTTC8001R" for tr, _p, _t in sent)
    assert all(p["INQR_STRT_DT"] == "20260929" for _tr, p, _t in sent)


def test_default_call_propagates_exception_like_before(conn):
    for kwargs in ({}, {"status": None}):
        sent = _record(conn, [_pg([_F[0]], "K1", "F")], raise_on=2)
        with pytest.raises(RuntimeError):
            asyncio.run(conn._query_daily_fills("20260929", **kwargs))
        assert len(sent) == 2


def test_default_call_not_connected_returns_empty_without_request(monkeypatch):
    monkeypatch.setattr(kis_kr.KISBroker, "is_connected", property(lambda self: False))
    b = _broker()
    sent = _record(b, [_pg([_F[0]], tr_cont="D")])
    assert asyncio.run(b._query_daily_fills("20260929")) == []
    assert asyncio.run(b._query_daily_fills("20260929", status=None)) == []
    assert sent == []


def test_default_call_http_headers_and_params_unchanged(broker, monkeypatch):
    """진짜 _api_get 을 통과한 요청 헤더·params 전량 등식 — 생략·None·dict 셋 다."""
    _connected(monkeypatch)
    monkeypatch.setattr(kis_kr, "_TR_NEW", False)
    for kwargs in ({}, {"status": None}, {"status": {}}):
        sent = _install_pages(broker, [(_fill_page([{"ODNO": "1"}], ctx="K1"), "F"),
                                       (_fill_page([{"ODNO": "2"}], ctx="K2"), "D")])
        items = asyncio.run(broker._query_daily_fills("20260921", **kwargs))
        assert [i["ODNO"] for i in items] == ["1", "2"]
        assert len(sent) == 2
        (h1, p1), (h2, p2) = sent
        assert "tr_cont" not in h1 and h2["tr_cont"] == "N"
        assert {k: v for k, v in h1.items() if k != "tr_id"} == _BASELINE_HEADERS
        assert h1["tr_id"] == h2["tr_id"] == "TTTC8001R"
        assert p1 == _DAILY_FILL_PARAMS
        assert p2 == dict(_DAILY_FILL_PARAMS, CTX_AREA_FK100="K1", CTX_AREA_NK100="K1")


def test_check_fills_request_unchanged(conn):
    """돈 경로 check_fills 는 status 없이 부른다 — 요청 기록이 기준선과 같다."""
    conn._order_id_to_kis_no = {}
    conn._pending_orders = {}
    sent = _record(conn, [_pg([_F[0]], "K1", "F"), _pg([_F[1]], "K2", "D")])
    asyncio.run(conn.check_fills())
    assert [t for _tr, _p, t in sent] == [None, "N"]


# ── 2) status 판정 ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_status_judgement(conn, name):
    pages, _n, _o, (complete, reason) = SCENARIOS[name]
    _record(conn, pages)
    st: dict = {}
    asyncio.run(conn._query_daily_fills("20260929", status=st))
    assert (st["complete"], st["reason"]) == (complete, reason)


def test_status_not_connected(monkeypatch):
    monkeypatch.setattr(kis_kr.KISBroker, "is_connected", property(lambda self: False))
    b = _broker()
    st: dict = {}
    assert asyncio.run(b._query_daily_fills("20260929", status=st)) == []
    assert st == {"complete": False, "reason": "not_connected"}


def test_status_exception_marks_incomplete_and_still_raises(conn):
    _record(conn, [_pg([_F[0]], "K1", "F")], raise_on=2)
    st: dict = {}
    with pytest.raises(RuntimeError):
        asyncio.run(conn._query_daily_fills("20260929", status=st))
    assert st == {"complete": False, "reason": "exception"}


# ── 3) get_fills_for_date_checked ────────────────────────────────────────────

def test_checked_rows_match_get_all_fills_for_date(conn):
    pages = [_pg([_F[0], _fill("x", qty="0")], "K1", "F"),
             _pg([_fill("y", side="02", sym="000660", price="100000.5")], "K2", "D")]
    _record(conn, pages)
    legacy = asyncio.run(conn.get_all_fills_for_date(date(2026, 9, 29)))
    sent = _record(conn, pages)
    rows, complete, reason = asyncio.run(conn.get_fills_for_date_checked(date(2026, 9, 29)))
    assert rows == legacy and len(rows) == 2            # 수량 0 행은 양쪽 다 뺀다
    assert (complete, reason) == (True, None)
    assert sent[0][1]["INQR_STRT_DT"] == "20260929"


@pytest.mark.parametrize("bad", [{"tot_ccld_qty": "abc"}, {"tot_ccld_qty": "1.5"},
                                 {"tot_ccld_qty": None}, {"avg_prvs": "N/A"},
                                 {"avg_prvs": None}])
def test_checked_normalize_failure_is_incomplete(conn, bad):
    _record(conn, [_pg([_F[0], dict(_F[1], **bad)], tr_cont="D")])
    rows, complete, reason = asyncio.run(conn.get_fills_for_date_checked(date(2026, 9, 29)))
    assert (complete, reason) == (False, "normalize_failed")
    assert [r["odno"] for r in rows] == ["0"]            # 정규화된 행만 돌려준다


def test_checked_query_incomplete_propagates_reason(conn):
    _record(conn, [_pg([_F[0]], "K1", "F"), _pg([], rt_cd="1")])
    rows, complete, reason = asyncio.run(conn.get_fills_for_date_checked(date(2026, 9, 29)))
    assert [r["odno"] for r in rows] == ["0"]            # 부분 목록
    assert (complete, reason) == (False, "rt_cd_failed:1")


def test_checked_exception_is_incomplete(conn):
    _record(conn, [], raise_on=1)
    assert asyncio.run(conn.get_fills_for_date_checked(date(2026, 9, 29))) == \
        ([], False, "exception")


def test_checked_not_connected(monkeypatch):
    monkeypatch.setattr(kis_kr.KISBroker, "is_connected", property(lambda self: False))
    b = _broker()
    assert asyncio.run(b.get_fills_for_date_checked(date(2026, 9, 29))) == \
        ([], False, "not_connected")


def test_checked_zero_fills_complete(conn):
    _record(conn, [_pg([], ctx="", tr_cont="D")])
    assert asyncio.run(conn.get_fills_for_date_checked(date(2026, 9, 29))) == ([], True, None)


# ── 4) DDL ──────────────────────────────────────────────────────────────────

def test_schema_has_execution_day_status_table():
    from src.data.storage.trade_storage import SCHEMA_SQL
    sql = " ".join(SCHEMA_SQL.split())
    assert ("CREATE TABLE IF NOT EXISTS execution_day_status ( trade_date DATE PRIMARY KEY, "
            "status VARCHAR(12) NOT NULL, reasons TEXT, source VARCHAR(30), "
            "checked_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL )") in sql
