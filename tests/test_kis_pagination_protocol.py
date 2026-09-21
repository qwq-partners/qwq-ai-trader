"""KIS 연속조회 요청 규약 + 형제 루프의 종료 판정 + 취소 POST 재시도 — 2026-09-21.

기준 자료: koreainvestment/open-trading-api 의 모든 연속조회 예제는 **다음 페이지 요청에
헤더 tr_cont="N"** 을 보낸다. 제품은 응답 헤더 tr_cont(F/M=다음, D/E=마지막)로 종료만
판정하고 요청에는 아무것도 싣지 않아, 2페이지 요청이 1페이지를 다시 받을 수 있었다.

고정하는 것:
1) 1페이지로 끝나는 호출은 요청이 전환 전과 완전히 같다 — 헤더에 tr_cont 키가 없고
   params 가 그대로이며 호출 1회. (세 연속조회 루프 각각, 헤더·params 전량 등식)
2) F → M → D 3페이지에서 요청 tr_cont 가 (미송신) → "N" → "N" 이고 세 페이지가 합쳐진다.
3) 세 루프가 같은 식으로 끝난다 — 응답 헤더가 D/E 면 ctx 키가 채워져 와도 다음 페이지를
   조회하지 않는다.
4) 취소 POST 는 재시도를 유지한다 — 첫 응답 HTTP 500 뒤 재전송해 성공하면 True.
   두 전송의 본문과 hashkey 헤더가 같다.

미체결 조회(get_exchange_open_orders)의 잘림 처리는 PR #81 이 같은 함수에서 다룬다 —
이 PR 은 건드리지 않는다.

모두 가짜 HTTP — 실 KIS 호출 0건.
"""
from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.types import Order, OrderSide, OrderType  # noqa: E402
from src.execution.broker import kis_kr  # noqa: E402
from src.utils import kis_rate_limit  # noqa: E402


# ── 공용 준비 ────────────────────────────────────────────────────────────────

class _Resp:
    """aiohttp 응답 흉내 — 본문과 응답 헤더 tr_cont 만 있으면 된다."""

    def __init__(self, body: dict, tr_cont: str):
        self.status = 200
        self._body = body
        self.headers = {"tr_cont": tr_cont} if tr_cont else {}

    async def json(self):
        return dict(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


@pytest.fixture
def broker(monkeypatch):
    """실 HTTP·리미터 대기 없이 진짜 _api_get 을 통과시키는 브로커.

    _get_headers 는 제품 것을 그대로 쓴다 — 요청 헤더 스냅샷이 이 시험의 근거다.
    """
    kis_rate_limit.reset()
    b = object.__new__(kis_kr.KISBroker)
    b._token = "t"
    b._token_mgr = SimpleNamespace(_access_token="t", _is_token_valid=lambda: True,
                                   invalidate=lambda: None)
    b.config = SimpleNamespace(
        base_url="http://x", account_no="12345678", account_product_cd="01", env="prod",
        app_key="k", app_secret="s",
    )
    b._balance_snapshot = None
    b._pending_orders = {}
    b._order_id_to_kis_no = {}
    b._order_id_to_orgno = {}
    monkeypatch.setattr(kis_rate_limit, "release_ledger", lambda: None)

    async def _no_limit(tr_id=None):
        return None
    b._rate_limit = _no_limit
    return b


def _install_pages(b, pages: list) -> list:
    """(요청 헤더, 요청 params) 를 기록하고 (본문, 응답 tr_cont) 를 순서대로 돌려준다."""
    sent: list = []
    seq = list(pages)

    def _get(url, headers=None, params=None):
        sent.append((dict(headers or {}), dict(params or {})))
        body, tr_cont = seq.pop(0) if seq else ({"rt_cd": "0", "output1": []}, "D")
        return _Resp(body, tr_cont)

    b._session = SimpleNamespace(closed=False, get=_get)
    return sent


_BASELINE_HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "authorization": "Bearer t",
    "appkey": "k",
    "appsecret": "s",
}

# 1페이지 요청의 기대 params — 제품 코드(origin/main)에서 그대로 옮긴 리터럴이다.
# 키가 하나라도 늘거나 줄면 이 등식이 깨진다(운영 계좌의 요청 무변경 주장의 근거).
_BALANCE_PARAMS = {
    "CANO": "12345678",
    "ACNT_PRDT_CD": "01",
    "AFHR_FLPR_YN": "N",
    "FUND_STTL_ICLD_YN": "N",
    "FNCG_AMT_AUTO_RDPT_YN": "N",
    "INQR_DVSN": "01",
    "OFL_YN": "N",
    "PRCS_DVSN": "00",
    "UNPR_DVSN": "01",
    "CTX_AREA_FK100": "",
    "CTX_AREA_NK100": "",
}
_DAILY_FILL_PARAMS = {
    "CANO": "12345678",
    "ACNT_PRDT_CD": "01",
    "INQR_STRT_DT": "20260921",
    "INQR_END_DT": "20260921",
    "SLL_BUY_DVSN_CD": "00",
    "ORD_GNO_BRNO": "",
    "CCLD_DVSN": "01",
    "INQR_DVSN": "00",
    "INQR_DVSN_1": "",
    "INQR_DVSN_3": "00",
    "EXCG_ID_DVSN_CD": "ALL",
    "CTX_AREA_FK100": "",
    "CTX_AREA_NK100": "",
    "PDNO": "",
    "ODNO": "",
}

_POS1 = {"pdno": "5930", "hldg_qty": "3", "pchs_avg_pric": "70000", "prpr": "71000",
         "prdt_name": "삼성전자"}
_POS2 = {"pdno": "660", "hldg_qty": "1", "pchs_avg_pric": "100000", "prpr": "101000",
         "prdt_name": "SK하이닉스"}
_POS3 = {"pdno": "373220", "hldg_qty": "2", "pchs_avg_pric": "400000", "prpr": "410000",
         "prdt_name": "LG에너지솔루션"}


def _balance_page(items, ctx="K1"):
    return {"rt_cd": "0", "ctx_area_fk100": ctx, "ctx_area_nk100": ctx, "output1": items}


def _fill_page(items, ctx="K1"):
    return {"rt_cd": "0", "ctx_area_fk100": ctx, "ctx_area_nk100": ctx, "output1": items}


def _connected(monkeypatch):
    monkeypatch.setattr(kis_kr.KISBroker, "is_connected", property(lambda self: True))


# ── 1) 1페이지 호출은 전환 전과 한 바이트도 다르지 않다 ──────────────────────

def test_positions_single_page_sends_no_tr_cont_header(broker, monkeypatch):
    """마지막 페이지(D) 하나로 끝나는 계좌 — 요청 헤더·params·호출 횟수가 기준선과 같다."""
    _connected(monkeypatch)
    sent = _install_pages(broker, [(_balance_page([_POS1]), "D")])
    pos = asyncio.run(broker.get_positions())
    assert pos["005930"].quantity == 3
    assert len(sent) == 1
    headers, params = sent[0]
    assert "tr_cont" not in headers
    assert {k: v for k, v in headers.items() if k != "tr_id"} == _BASELINE_HEADERS
    assert headers["tr_id"] == "TTTC8434R"
    assert params == _BALANCE_PARAMS


def test_positions_for_account_single_page_sends_no_tr_cont_header(broker, monkeypatch):
    """외부 계좌 루프도 같은 기준선 — CANO 만 다르고 나머지 헤더·params 는 동일하다."""
    _connected(monkeypatch)
    sent = _install_pages(broker, [
        (dict(_balance_page([_POS1], ctx=""), output2=[{"tot_evlu_amt": "1"}]), "D"),
    ])
    rows, summary = asyncio.run(broker.get_positions_for_account("87654321", "01"))
    assert [r["symbol"] for r in rows] == ["005930"]
    assert summary["total_equity"] == 1.0      # 마지막 페이지라도 요약은 채워진다
    assert len(sent) == 1
    headers, params = sent[0]
    assert "tr_cont" not in headers
    assert {k: v for k, v in headers.items() if k != "tr_id"} == _BASELINE_HEADERS
    assert headers["tr_id"] == "TTTC8434R"
    assert params == dict(_BALANCE_PARAMS, CANO="87654321")


def test_daily_fills_single_page_sends_no_tr_cont_header(broker, monkeypatch):
    _connected(monkeypatch)
    monkeypatch.setattr(kis_kr, "_TR_NEW", False)   # 기본 legacy 를 명시로 고정
    sent = _install_pages(broker, [(_fill_page([{"ODNO": "1", "pdno": "5930"}]), "D")])
    items = asyncio.run(broker._query_daily_fills("20260921"))
    assert len(items) == 1
    assert len(sent) == 1
    headers, params = sent[0]
    assert "tr_cont" not in headers
    assert {k: v for k, v in headers.items() if k != "tr_id"} == _BASELINE_HEADERS
    assert headers["tr_id"] == "TTTC8001R"
    assert params == _DAILY_FILL_PARAMS


# ── 2) 2페이지째부터 tr_cont="N" ─────────────────────────────────────────────

def test_positions_three_pages_send_n_from_the_second(broker, monkeypatch):
    """F → M → D. 첫 요청은 미송신, 이후는 "N" — 세 페이지가 전부 합쳐진다."""
    _connected(monkeypatch)
    sent = _install_pages(broker, [
        (_balance_page([_POS1], ctx="K1"), "F"),
        (_balance_page([_POS2], ctx="K2"), "M"),
        (_balance_page([_POS3], ctx="K3"), "D"),
    ])
    pos = asyncio.run(broker.get_positions())
    assert set(pos) == {"005930", "000660", "373220"}
    assert [h.get("tr_cont") for h, _p in sent] == [None, "N", "N"]
    assert sent[1][1]["CTX_AREA_NK100"] == "K1"     # ctx 되돌림은 그대로


def test_positions_for_account_three_pages_send_n_from_the_second(broker, monkeypatch):
    _connected(monkeypatch)
    sent = _install_pages(broker, [
        (dict(_balance_page([_POS1], ctx="K1"), output2=[{"tot_evlu_amt": "1"}]), "F"),
        (_balance_page([_POS2], ctx="K2"), "M"),
        (_balance_page([_POS3], ctx=""), "D"),
    ])
    rows, _summary = asyncio.run(broker.get_positions_for_account("87654321", "01"))
    assert {r["symbol"] for r in rows} == {"005930", "000660", "373220"}
    assert [h.get("tr_cont") for h, _p in sent] == [None, "N", "N"]


def test_daily_fills_three_pages_send_n_from_the_second(broker, monkeypatch):
    _connected(monkeypatch)
    sent = _install_pages(broker, [
        (_fill_page([{"ODNO": "1"}], ctx="K1"), "F"),
        (_fill_page([{"ODNO": "2"}], ctx="K2"), "M"),
        (_fill_page([{"ODNO": "3"}], ctx="K3"), "D"),
    ])
    items = asyncio.run(broker._query_daily_fills("20260921"))
    assert [i["ODNO"] for i in items] == ["1", "2", "3"]
    assert [h.get("tr_cont") for h, _p in sent] == [None, "N", "N"]


def test_retry_of_the_same_page_keeps_the_same_tr_cont(broker, monkeypatch):
    """같은 페이지의 3회 시도(HTTP 500 → 재시도) 안에서도 헤더가 유지된다."""
    _connected(monkeypatch)
    sent: list = []
    statuses = ["F", 500, "D"]      # 1페이지 F → 2페이지 첫 시도 500 → 재시도 성공(D)

    def _get(url, headers=None, params=None):
        sent.append(dict(headers or {}))
        nxt = statuses.pop(0)
        if nxt == 500:
            resp = _Resp({"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "유량"}, "")
            resp.status = 500
            return resp
        items = [_POS1] if len(sent) == 1 else [_POS2]
        return _Resp(_balance_page(items, ctx=f"K{len(sent)}"), nxt)

    broker._session = SimpleNamespace(closed=False, get=_get)

    async def _fast_sleep(_s):
        return None
    monkeypatch.setattr(kis_kr.asyncio, "sleep", _fast_sleep)

    pos = asyncio.run(broker.get_positions())
    assert set(pos) == {"005930", "000660"}
    assert [h.get("tr_cont") for h in sent] == [None, "N", "N"]


# ── 3) 세 루프가 같은 식으로 끝난다 ─────────────────────────────────────────

def test_positions_for_account_stops_on_header_d_even_with_filled_ctx(broker, monkeypatch):
    """KIS 는 마지막 페이지에도 ctx 키를 채워 보낸다 — 헤더가 D 면 더 묻지 않는다.

    get_positions·_query_daily_fills 는 이미 이렇게 끝난다. 이 루프만 빈 ctx 키에
    기대다가 마지막 페이지 뒤로 원장 호출을 한 번 더 내보내고 있었다.
    """
    _connected(monkeypatch)
    sent = _install_pages(broker, [
        (dict(_balance_page([_POS1], ctx="K1"), output2=[{"tot_evlu_amt": "1"}]), "D"),
    ])
    rows, summary = asyncio.run(broker.get_positions_for_account("87654321", "01"))
    assert [r["symbol"] for r in rows] == ["005930"]
    assert summary["total_equity"] == 1.0
    assert len(sent) == 1


def test_positions_for_account_two_pages_still_merge(broker, monkeypatch):
    """F → D 는 두 번 묻고 행을 합친다 — 종료 판정이 조기 종료가 되면 안 된다."""
    _connected(monkeypatch)
    sent = _install_pages(broker, [
        (dict(_balance_page([_POS1], ctx="K1"), output2=[{"tot_evlu_amt": "1"}]), "F"),
        (_balance_page([_POS2], ctx="K2"), "D"),
    ])
    rows, _summary = asyncio.run(broker.get_positions_for_account("87654321", "01"))
    assert {r["symbol"] for r in rows} == {"005930", "000660"}
    assert len(sent) == 2
    assert [h.get("tr_cont") for h, _p in sent] == [None, "N"]


# ── 4) 취소 POST 의 재시도는 유지한다 ───────────────────────────────────────

def test_cancel_post_retries_after_http_500_and_succeeds(broker, monkeypatch):
    """전량 취소는 원주문번호 하나를 겨냥한다 — 두 번 닿아도 새 노출을 만들 수 없다.
    재시도를 빼면 보호 취소(90초 SELL 폴백·10분 BUY 정리)의 성공률만 떨어진다."""
    _connected(monkeypatch)
    monkeypatch.setattr(kis_kr.kill_switch, "check", lambda side, market="KR": (True, ""))
    monkeypatch.setattr(kis_kr.audit_log, "record", lambda event, **f: None)
    monkeypatch.setattr(kis_kr.audit_log, "record_blocked", lambda **f: None)

    async def fake_hashkey(params):
        return "h"
    broker._get_hashkey = fake_hashkey

    order = Order(id="o1", symbol="005930", side=OrderSide.SELL,
                  order_type=OrderType.LIMIT, quantity=10, price=Decimal("70000"))
    broker._pending_orders["o1"] = order
    broker._order_id_to_kis_no["o1"] = "0001"
    broker._order_id_to_orgno["o1"] = "91252"

    posts: list = []
    outcomes = [500, 200]

    def _post(url, headers=None, json=None):
        posts.append((dict(headers or {}), dict(json or {})))
        status = outcomes.pop(0)
        if status == 500:
            resp = _Resp({"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "유량"}, "")
            resp.status = 500
            return resp
        return _Resp({"rt_cd": "0", "output": {"ODNO": "0001"}}, "")

    broker._session = SimpleNamespace(closed=False, get=None, post=_post)

    async def _fast_sleep(_s):
        return None
    monkeypatch.setattr(kis_kr.asyncio, "sleep", _fast_sleep)

    assert asyncio.run(broker.cancel_order("o1")) is True
    assert len(posts) == 2                       # 재전송이 실제로 나갔다
    assert posts[0][1] == posts[1][1]
    # hashkey 는 본문 무결성 검사다 — 본문이 같으면 헤더도 같아야 한다(재발급으로 달라지면
    # 두 전송이 다른 본문을 주장하는 셈이다).
    assert posts[0][0]["hashkey"] == posts[1][0]["hashkey"] == "h"
    assert posts[0][1]["QTY_ALL_ORD_YN"] == "Y"  # 원주문 전량 — 재전송해도 노출이 늘지 않는다
    assert "o1" not in broker._pending_orders
