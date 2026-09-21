"""KIS 구 TR → 신 TR 전환 스위치 (KIS_TR_SET) 고정 — 2026-09-21.

기준 자료: koreainvestment/open-trading-api@b4e6249 의 examples_llm
(명세: docs/integrations/kis-tr-migration-spec-2026-09-21.md).

고정하는 것:
1) 두 모드 × 5 경로의 요청 헤더 tr_id 가 명세 §1 표와 정확히 일치한다.
2) legacy 모드의 요청 본문이 전환 전과 완전히 같다 — 신 TR 전용 키가 새지 않는다.
3) new 모드 본문에만 EXCG_ID_DVSN_CD(+ order-cash 의 CNDT_PRIC)가 실린다.
4) new 모드 취소가능조회가 rmn_qty 부재/공백 시 psbl_qty 를 읽고, 둘 다 없거나
   비어 있거나 음수면 조용한 0 대신 판단 불가(None)를 돌려준다. legacy 분기는 무변경.
5) 신 TR 두 개도 계좌 원장 간격으로 직렬화된다.
6) check_fills 가 읽는 응답 키(odno·tot_ccld_qty·avg_prvs)는 그대로다.
7) 신 TR·신 본문은 정규장 주문에만 — NXT 세션(pre_market·next_market) 접수는
   new 모드에서도 legacy tr_id·본문 그대로다.
8) 접수·정정 POST 는 두 모드 모두 retry=False (비멱등 재전송 금지).
9) KIS_TR_SET 해석 — 정확히 "new" 일 때만 신 TR.

모두 가짜 HTTP — 실 KIS 호출 0건. 9)만 자식 프로세스(네트워크·.env 없음)를 쓴다.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analytics import tca  # noqa: E402
from src.core.types import Order, OrderSide, OrderType  # noqa: E402
from src.execution.broker import kis_kr  # noqa: E402
from src.utils import kis_rate_limit  # noqa: E402


# ── 공용 준비 ────────────────────────────────────────────────────────────────

@pytest.fixture
def broker(monkeypatch):
    """실 HTTP·킬스위치 파일·감사 원장을 건드리지 않는 브로커."""
    kis_rate_limit.reset()
    b = object.__new__(kis_kr.KISBroker)
    b._session = SimpleNamespace(closed=False)
    b._token = "t"
    b._token_mgr = SimpleNamespace(_access_token="t", _is_token_valid=lambda: True)
    b.config = SimpleNamespace(
        base_url="http://x", account_no="12345678", account_product_cd="01", env="prod",
        app_key="k", app_secret="s",
    )
    b._pending_orders = {}
    b._order_id_to_kis_no = {}
    b._order_id_to_orgno = {}

    # 세션 의존 분기 동결 — 벽시계와 무관하게 정규장으로 고정
    monkeypatch.setattr(kis_kr.KISBroker, "_get_current_market_session", lambda self: "regular")
    monkeypatch.setattr(kis_kr.kill_switch, "check", lambda side, market="KR": (True, ""))
    monkeypatch.setattr(kis_kr.audit_log, "record", lambda event, **f: None)
    monkeypatch.setattr(kis_kr.audit_log, "record_blocked", lambda **f: None)
    # 체결 계측은 운영 캐시에 쓴다 — 오프라인 시험에서는 호출만 삼킨다
    monkeypatch.setattr(tca, "record_fill_tca", lambda order, fill, market="KR": None)

    async def fake_hashkey(params):
        return "h"
    b._get_hashkey = fake_hashkey
    return b


def _use_new(monkeypatch, new: bool) -> None:
    """모듈 상수만 바꾼다 — 제품은 호출 시점에 이 값을 읽는다."""
    monkeypatch.setattr(kis_kr, "_TR_NEW", new)


def _capture_post(b) -> list:
    """(tr_id, 본문, retry) 를 기록하는 가짜 _api_post — 접수 성공 응답."""
    sent = []

    async def fake_post(url, tr_id, json_data, extra_headers=None, retry=True):
        sent.append((tr_id, dict(json_data), retry))
        return {"rt_cd": "0", "output": {"ODNO": "0001", "KRX_FWDG_ORD_ORGNO": "91252"}}
    b._api_post = fake_post
    return sent


def _capture_get(b, response: dict) -> list:
    """(tr_id, 파라미터) 를 기록하는 가짜 _api_get."""
    sent = []

    async def fake_get(url, tr_id, params):
        sent.append((tr_id, dict(params)))
        return response
    b._api_get = fake_get
    return sent


def _order(side=OrderSide.BUY) -> Order:
    return Order(id="o1", symbol="005930", side=side, order_type=OrderType.LIMIT,
                 quantity=10, price=Decimal("70000"))


def _pending(b, side=OrderSide.SELL) -> Order:
    o = _order(side)
    b._pending_orders[o.id] = o
    b._order_id_to_kis_no[o.id] = "0001"
    b._order_id_to_orgno[o.id] = "91252"
    return o


_DAILY_OK = {"rt_cd": "0", "output1": [], "_tr_cont": "D"}
_CANCELABLE_OK = {"rt_cd": "0", "output": []}


# ── 1) TR 매핑 스냅샷: 두 모드 × 5 경로 ──────────────────────────────────────

@pytest.mark.parametrize("new,expected", [
    (False, {"buy": "TTTC0802U", "sell": "TTTC0801U", "cancel": "TTTC0803U",
             "modify": "TTTC0803U", "daily": "TTTC8001R", "cancelable": "TTTC8036R"}),
    (True, {"buy": "TTTC0012U", "sell": "TTTC0011U", "cancel": "TTTC0013U",
            "modify": "TTTC0013U", "daily": "TTTC0081R", "cancelable": "TTTC0084R"}),
])
def test_tr_id_snapshot_for_every_path(broker, monkeypatch, new, expected):
    """명세 §1 표와 한 글자도 다르지 않아야 한다."""
    _use_new(monkeypatch, new)
    seen = {}

    posted = _capture_post(broker)
    asyncio.run(broker.submit_order(_order(OrderSide.BUY)))
    seen["buy"] = posted[-1][0]
    broker._pending_orders.clear()
    asyncio.run(broker.submit_order(_order(OrderSide.SELL)))
    seen["sell"] = posted[-1][0]

    _pending(broker)
    assert asyncio.run(broker.cancel_order("o1")) is True
    seen["cancel"] = posted[-1][0]

    _pending(broker)
    assert asyncio.run(broker.modify_order("o1", new_price=Decimal("71000"))) is True
    seen["modify"] = posted[-1][0]

    daily = _capture_get(broker, _DAILY_OK)
    asyncio.run(broker._query_daily_fills("20260921"))
    seen["daily"] = daily[-1][0]

    cancelable = _capture_get(broker, _CANCELABLE_OK)
    asyncio.run(broker.get_exchange_open_orders())
    seen["cancelable"] = cancelable[-1][0]

    assert seen == expected


# ── 2) legacy 본문 = 전환 전과 동일 ─────────────────────────────────────────

_LEGACY_ORDER_BODY = {
    "CANO": "12345678", "ACNT_PRDT_CD": "01", "PDNO": "005930", "ORD_DVSN": "00",
    "ORD_QTY": "10", "ORD_UNPR": "70000", "CTAC_TLNO": "", "SLL_TYPE": "", "ALGO_NO": "",
}
_LEGACY_CANCEL_BODY = {
    "CANO": "12345678", "ACNT_PRDT_CD": "01", "KRX_FWDG_ORD_ORGNO": "91252",
    "ORGN_ODNO": "0001", "ORD_DVSN": "00", "RVSE_CNCL_DVSN_CD": "02",
    "ORD_QTY": "10", "ORD_UNPR": "0", "QTY_ALL_ORD_YN": "Y",
}
_LEGACY_MODIFY_BODY = {
    "CANO": "12345678", "ACNT_PRDT_CD": "01", "KRX_FWDG_ORD_ORGNO": "91252",
    "ORGN_ODNO": "0001", "ORD_DVSN": "00", "RVSE_CNCL_DVSN_CD": "01",
    "ORD_QTY": "10", "ORD_UNPR": "71000", "QTY_ALL_ORD_YN": "N",
}


def test_legacy_order_body_is_byte_identical(broker, monkeypatch):
    """기본 모드에서 order-cash 본문은 9키 그대로 — 신 TR 키가 새면 안 된다."""
    _use_new(monkeypatch, False)
    posted = _capture_post(broker)
    asyncio.run(broker.submit_order(_order(OrderSide.BUY)))
    assert posted[0][1] == _LEGACY_ORDER_BODY


def test_legacy_cancel_and_modify_bodies_are_byte_identical(broker, monkeypatch):
    _use_new(monkeypatch, False)
    posted = _capture_post(broker)
    _pending(broker)
    asyncio.run(broker.cancel_order("o1"))
    assert posted[0][1] == _LEGACY_CANCEL_BODY
    _pending(broker)
    asyncio.run(broker.modify_order("o1", new_price=Decimal("71000")))
    assert posted[1][1] == _LEGACY_MODIFY_BODY


def test_legacy_daily_query_keeps_excg_all(broker, monkeypatch):
    """일별조회의 EXCG_ID_DVSN_CD='ALL' 은 두 모드 모두 현행 그대로."""
    _use_new(monkeypatch, False)
    got = _capture_get(broker, _DAILY_OK)
    asyncio.run(broker._query_daily_fills("20260921"))
    assert got[0][1]["EXCG_ID_DVSN_CD"] == "ALL"
    _use_new(monkeypatch, True)
    asyncio.run(broker._query_daily_fills("20260921"))
    assert got[1][1]["EXCG_ID_DVSN_CD"] == "ALL"


def test_legacy_cancelable_query_body_unchanged(broker, monkeypatch):
    """정정취소가능조회는 두 모드 모두 6키 — 거래소 파라미터가 없다."""
    _use_new(monkeypatch, False)
    got = _capture_get(broker, _CANCELABLE_OK)
    asyncio.run(broker.get_exchange_open_orders())
    _use_new(monkeypatch, True)
    asyncio.run(broker.get_exchange_open_orders())
    expected = {"CANO": "12345678", "ACNT_PRDT_CD": "01", "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "", "INQR_DVSN_1": "0", "INQR_DVSN_2": "0"}
    assert got[0][1] == expected and got[1][1] == expected


# ── 3) new 모드에서만 붙는 키 ────────────────────────────────────────────────

def test_new_order_body_adds_excg_and_cndt_pric(broker, monkeypatch):
    """저장소 신 TR 은 EXCG_ID_DVSN_CD 를 필수로 받고 CNDT_PRIC 키를 항상 보낸다."""
    _use_new(monkeypatch, True)
    posted = _capture_post(broker)
    asyncio.run(broker.submit_order(_order(OrderSide.BUY)))
    body = posted[0][1]
    assert body["EXCG_ID_DVSN_CD"] == "KRX"
    assert body["CNDT_PRIC"] == ""
    # 기존 키는 하나도 사라지지 않는다
    assert {k: body[k] for k in _LEGACY_ORDER_BODY} == _LEGACY_ORDER_BODY


def test_new_cancel_and_modify_bodies_add_excg(broker, monkeypatch):
    _use_new(monkeypatch, True)
    posted = _capture_post(broker)
    _pending(broker)
    asyncio.run(broker.cancel_order("o1"))
    _pending(broker)
    asyncio.run(broker.modify_order("o1", new_price=Decimal("71000")))
    assert posted[0][1]["EXCG_ID_DVSN_CD"] == "KRX"
    assert posted[1][1]["EXCG_ID_DVSN_CD"] == "KRX"
    assert {k: posted[0][1][k] for k in _LEGACY_CANCEL_BODY} == _LEGACY_CANCEL_BODY
    assert {k: posted[1][1][k] for k in _LEGACY_MODIFY_BODY} == _LEGACY_MODIFY_BODY


def test_new_order_body_hashkey_covers_the_added_keys(broker, monkeypatch):
    """hashkey 는 신 TR 키가 붙은 뒤의 본문으로 발급돼야 한다."""
    _use_new(monkeypatch, True)
    hashed = {}

    async def fake_hashkey(params):
        hashed.update(params)
        return "h"
    broker._get_hashkey = fake_hashkey
    _capture_post(broker)
    asyncio.run(broker.submit_order(_order(OrderSide.BUY)))
    assert hashed.get("EXCG_ID_DVSN_CD") == "KRX" and "CNDT_PRIC" in hashed


# ── 4) 취소가능조회 수량 키 ─────────────────────────────────────────────────

_ROW = {"pdno": "005930", "sll_buy_dvsn_cd": "01"}


def test_new_cancelable_falls_back_to_psbl_qty(broker, monkeypatch):
    """신 TR output 에는 rmn_qty 가 없다 — psbl_qty 로 읽는다."""
    _use_new(monkeypatch, True)
    _capture_get(broker, {"rt_cd": "0", "output": [dict(_ROW, psbl_qty="7")]})
    rows = asyncio.run(broker.get_exchange_open_orders())
    assert rows == [{"symbol": "005930", "side": "sell", "qty": 7}]


def test_new_cancelable_prefers_rmn_qty_when_present(broker, monkeypatch):
    _use_new(monkeypatch, True)
    _capture_get(broker, {"rt_cd": "0", "output": [dict(_ROW, rmn_qty="3", psbl_qty="7")]})
    rows = asyncio.run(broker.get_exchange_open_orders())
    assert rows[0]["qty"] == 3


def test_new_cancelable_blank_rmn_qty_still_reads_psbl_qty(broker, monkeypatch):
    """rmn_qty 가 빈 문자열이어도 유효한 psbl_qty 가 있으면 그 값을 쓴다 (조용한 0 금지)."""
    _use_new(monkeypatch, True)
    _capture_get(broker, {"rt_cd": "0", "output": [dict(_ROW, rmn_qty="", psbl_qty="7")]})
    rows = asyncio.run(broker.get_exchange_open_orders())
    assert rows == [{"symbol": "005930", "side": "sell", "qty": 7}]


def test_new_cancelable_blank_both_quantities_is_undecidable(broker, monkeypatch):
    """대체 뒤에도 비어 있으면 0 이 아니라 None 이다."""
    _use_new(monkeypatch, True)
    _capture_get(broker, {"rt_cd": "0", "output": [dict(_ROW, rmn_qty="", psbl_qty="  ")]})
    assert asyncio.run(broker.get_exchange_open_orders()) is None


def test_new_cancelable_without_any_qty_key_is_undecidable(broker, monkeypatch):
    """둘 다 없으면 조용한 0 이 아니라 None — 호출측이 pending 을 유지한다."""
    _use_new(monkeypatch, True)
    _capture_get(broker, {"rt_cd": "0", "output": [dict(_ROW)]})
    assert asyncio.run(broker.get_exchange_open_orders()) is None


def test_new_cancelable_negative_qty_is_undecidable(broker, monkeypatch):
    """음수 수량은 응답을 해석할 수 없다는 뜻 — 그대로 올리지 않고 None 이다."""
    _use_new(monkeypatch, True)
    _capture_get(broker, {"rt_cd": "0", "output": [dict(_ROW, psbl_qty="-1")]})
    assert asyncio.run(broker.get_exchange_open_orders()) is None


def test_legacy_cancelable_negative_qty_keeps_current_behaviour(broker, monkeypatch):
    """legacy 분기는 무변경이 이 PR 의 계약 — 음수도 전환 전처럼 그대로 실린다."""
    _use_new(monkeypatch, False)
    _capture_get(broker, {"rt_cd": "0", "output": [dict(_ROW, rmn_qty="-1")]})
    rows = asyncio.run(broker.get_exchange_open_orders())
    assert rows == [{"symbol": "005930", "side": "sell", "qty": -1}]


def test_legacy_cancelable_missing_rmn_qty_keeps_current_behaviour(broker, monkeypatch):
    """legacy 는 현행 그대로 — 키가 없으면 0 (전환 전 동작을 바꾸지 않는다)."""
    _use_new(monkeypatch, False)
    _capture_get(broker, {"rt_cd": "0", "output": [dict(_ROW)]})
    rows = asyncio.run(broker.get_exchange_open_orders())
    assert rows == [{"symbol": "005930", "side": "sell", "qty": 0}]


# ── 5) 리미터: 신 TR 도 원장 간격 ───────────────────────────────────────────

@pytest.mark.parametrize("tr_id", ["TTTC0081R", "TTTC0084R"])
def test_new_ledger_trs_are_serialized(tr_id):
    """신 조회 TR 도 계좌 원장(EGW00215) 간격을 지킨다."""
    kis_rate_limit.reset()
    assert kis_rate_limit.is_ledger(tr_id)

    async def run():
        t0 = time.monotonic()
        await kis_rate_limit.acquire(tr_id)
        kis_rate_limit.release_ledger()
        await kis_rate_limit.acquire("TTTC8434R")
        return time.monotonic() - t0

    assert asyncio.run(run()) >= kis_rate_limit.LEDGER_MIN_INTERVAL


def test_legacy_ledger_trs_stay_registered():
    """롤백해도 구 TR 의 원장 보호가 끊기지 않는다."""
    for tr_id in ("TTTC8001R", "TTTC8036R", "TTTC8434R", "TTTC8908R"):
        assert kis_rate_limit.is_ledger(tr_id)


# ── 6) 파서 계약: check_fills 가 읽는 키 ────────────────────────────────────

@pytest.mark.parametrize("new", [False, True])
def test_check_fills_reads_the_same_response_keys(broker, monkeypatch, new):
    """TR 을 바꿔도 odno·tot_ccld_qty·avg_prvs 세 키로 체결을 읽는다."""
    _use_new(monkeypatch, new)
    _pending(broker, OrderSide.SELL)
    _capture_get(broker, {
        "rt_cd": "0", "_tr_cont": "D",
        "output1": [{"odno": "0001", "tot_ccld_qty": "10", "avg_prvs": "70500"}],
    })
    fills = asyncio.run(broker.check_fills())
    assert len(fills) == 1
    assert fills[0].quantity == 10 and fills[0].price == Decimal("70500")


# ── 7) 세션별: 신 TR 은 정규장 주문에만 ─────────────────────────────────────

@pytest.mark.parametrize("new,session,expected_tr,new_keys", [
    (False, "regular", "TTTC0802U", False),
    (False, "pre_market", "TTTC0802U", False),
    (False, "next_market", "TTTC0802U", False),
    (True, "regular", "TTTC0012U", True),
    (True, "pre_market", "TTTC0802U", False),
    (True, "next_market", "TTTC0802U", False),
])
def test_order_tr_and_body_by_session(broker, monkeypatch, new, session, expected_tr, new_keys):
    """공식 저장소에 NXT 주문 예제가 없어 EXCG_ID_DVSN_CD 값을 확정할 수 없다 —
    그 세션 접수는 new 모드에서도 구 TR·구 본문으로 나간다."""
    _use_new(monkeypatch, new)
    monkeypatch.setattr(kis_kr.KISBroker, "_get_current_market_session", lambda self: session)

    async def fake_nxt(self):
        return ["005930"]
    monkeypatch.setattr(kis_kr.KISBroker, "get_nxt_symbols", fake_nxt)

    posted = _capture_post(broker)
    asyncio.run(broker.submit_order(_order(OrderSide.BUY)))
    tr_id, body, _ = posted[-1]
    assert tr_id == expected_tr
    assert ("EXCG_ID_DVSN_CD" in body) is new_keys
    assert ("CNDT_PRIC" in body) is new_keys


# ── 8) 비멱등 POST 는 재전송하지 않는다 ─────────────────────────────────────

@pytest.mark.parametrize("new", [False, True])
def test_order_and_modify_posts_are_pinned_to_no_retry(broker, monkeypatch, new):
    """접수·정정은 응답 유실 시 재전송하면 중복 주문이다 (2026-09-03 P0)."""
    _use_new(monkeypatch, new)
    posted = _capture_post(broker)
    asyncio.run(broker.submit_order(_order(OrderSide.BUY)))
    assert posted[0][2] is False
    _pending(broker)
    assert asyncio.run(broker.modify_order("o1", new_price=Decimal("71000"))) is True
    assert posted[1][2] is False


# ── 9) KIS_TR_SET 해석 ──────────────────────────────────────────────────────

@pytest.mark.parametrize("env_value,expected", [
    (None, "TTTC0802U"),        # 미설정
    ("", "TTTC0802U"),
    ("legacy", "TTTC0802U"),
    ("NEW", "TTTC0802U"),       # 대소문자 구분 — 오타가 조용히 전환하면 안 된다
    ("New", "TTTC0802U"),
    ("1", "TTTC0802U"),
    ("true", "TTTC0802U"),
    ("new", "TTTC0012U"),       # 유일한 전환 값
])
def test_kis_tr_set_env_parsing(env_value, expected):
    """자식 프로세스에서 모듈을 새로 import 한다 — 네트워크·.env 없이 env 만 준다."""
    env = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(ROOT),
    }
    if env_value is not None:
        env["KIS_TR_SET"] = env_value
    done = subprocess.run(
        [sys.executable, "-c",
         "from src.execution.broker import kis_kr; print(kis_kr._tr_id('buy'))"],
        env=env, cwd=str(ROOT), capture_output=True, text=True, timeout=180,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == expected
