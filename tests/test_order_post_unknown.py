"""주문 POST 접수 불명(UNKNOWN) 분리 — 2026-09-29 (설계 docs/superpowers/specs/2026-09-29-order-post-unknown-design.md §4).

KIS 주문 POST 가 서버에 닿은 뒤 응답을 잃으면 접수됐을 수 있다. 종전에는 확정 거절과 똑같이 처리해
BUY 는 5분 뒤 재매수, 분할 SELL 은 재발행(이중 매도)이 열렸다. 고정하는 것:

- 브로커(B1~B6): 응답을 믿을 수 없는 경우만 UNKNOWN(`(False, "[접수불명] …")`, EV_UNKNOWN, 장부 기록).
  오늘 BUY 불명이면 신규 BUY 전부 보류(머리 게이트 + 전송 직전 재확인). 성공·명시 거절 경로는 현행 그대로.
- 엔진(E0~E4): 오늘 SELL 불명 종목은 **분할** 매도만 막는다 — ExitManager 발생원·on_signal·폴백.
  전량(손절·잔량 전부인 단계 익절)은 통과한다. 좀비 카운터는 불명 종목도 현행대로 센다(회복 경로 유지).

전부 합성 입력 — 네트워크·운영 캐시 무접촉(장부는 tmp_path, Path.home 은 tmp_path). 시계는 주입·동결한다.
"""
from __future__ import annotations

import ast
import asyncio
import fcntl
import json
import os
import sys
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import aiohttp
import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.core.event import OrderEvent, SignalEvent  # noqa: E402
from src.core.types import (  # noqa: E402
    Order, OrderSide, OrderType, Position, Signal, SignalStrength, StrategyType,
)
from src.execution.broker import kis_kr  # noqa: E402
from src.risk import order_unknown  # noqa: E402
from src.risk.order_unknown import PREFIX, UnknownOrderBook  # noqa: E402
from src.strategies.exit_manager import ExitConfig, ExitManager, ExitStage  # noqa: E402
from src.utils import audit_log, kis_rate_limit  # noqa: E402

from test_review_fixes_2026_09 import _FakeResp, _FakeSession, _post_broker  # noqa: E402
from test_risk_sizing import home  # noqa: E402,F401
from test_t11_entry_plan import _freeze_clock  # noqa: E402

NOW = datetime(2026, 9, 29, 10, 30, 0)
SYM = "005930"
OTHER = "000660"
OK_BODY = {"rt_cd": "0", "msg_cd": "APBK0013", "msg1": "주문 전송 완료",
           "output": {"ODNO": "0001", "KRX_FWDG_ORD_ORGNO": "91252"}}


class _NonJson(_FakeResp):
    """본문이 JSON 이 아닌 응답(게이트웨이 HTML 등)."""

    async def json(self):
        raise aiohttp.ContentTypeError(None, (), message="text/html")


class _RecSession(_FakeSession):
    """보낸 (tr_id, 본문) 도 기록한다 — 성공 경로 요청이 바뀌지 않았음을 본다."""

    def __init__(self, outcome):
        super().__init__(outcome)
        self.sent = []

    def post(self, *a, **k):
        self.sent.append((k["headers"]["tr_id"], dict(k["json"])))
        return super().post(*a, **k)


def _order(side=OrderSide.BUY, symbol=SYM, qty=10) -> Order:
    return Order(id=f"o-{side.value}-{symbol}", symbol=symbol, side=side, order_type=OrderType.LIMIT,
                 quantity=qty, price=Decimal("70000"), strategy="sepa_trend")


@pytest.fixture
def ub(tmp_path, monkeypatch):
    """실 `_api_post` 까지 도는 브로커 — HTTP 세션만 가짜. 장부는 tmp_path, 시계는 NOW 로 동결."""
    kis_rate_limit.reset()
    _freeze_clock(monkeypatch, kis_kr, NOW)
    b = object.__new__(kis_kr.KISBroker)
    b._session = _RecSession(_FakeResp(200, OK_BODY))
    b._token = "tok"
    b._token_mgr = SimpleNamespace(_access_token="tok", _is_token_valid=lambda: True, invalidate=lambda: None)
    b.config = SimpleNamespace(base_url="http://x", account_no="12345678", account_product_cd="01",
                               env="prod", app_key="k", app_secret="s")
    b._pending_orders, b._order_id_to_kis_no, b._order_id_to_orgno = {}, {}, {}
    b._unknown_book = UnknownOrderBook(tmp_path / "order_unknown.json", now=NOW)
    b.book_path = tmp_path / "order_unknown.json"

    monkeypatch.setattr(kis_kr.KISBroker, "_get_current_market_session", lambda self: "regular")
    monkeypatch.setattr(kis_kr.kill_switch, "check", lambda side, market="KR": (True, ""))
    b.audits = []
    monkeypatch.setattr(kis_kr.audit_log, "record", lambda event, **f: b.audits.append((event, f)))
    monkeypatch.setattr(kis_kr.audit_log, "record_blocked", lambda **f: b.audits.append(("blocked", f)))
    b.alerts = []

    async def _alert(text, **_k):
        b.alerts.append(text)
    monkeypatch.setattr("src.utils.telegram.send_alert", _alert)

    async def _hashkey(params):
        return "h"
    b._get_hashkey = _hashkey
    return b


def _submit(b, order):
    """submit_order 를 돌리고, 알림 태스크(fire-and-forget)가 한 번 돌 기회를 준다."""
    async def run():
        out = await b.submit_order(order)
        await asyncio.sleep(0)
        return out
    return asyncio.run(run())


def _events(b):
    return [e for e, _ in b.audits]


# ── B1 `_api_post` 분류 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("status", [200, 403, 502])
def test_b1_non_json_body_is_unknown_regardless_of_status(monkeypatch, status):
    sess = _FakeSession(_NonJson(status, None))
    out = asyncio.run(_post_broker(sess, monkeypatch)._api_post("u", "TTTC0802U", {}, retry=False))
    assert out.get("_unknown") is True and sess.calls == 1


@pytest.mark.parametrize("exc", [asyncio.TimeoutError(), aiohttp.ClientConnectionError("끊김")])
def test_b1_no_retry_network_error_is_unknown_and_posted_once(monkeypatch, exc):
    sess = _FakeSession(exc)
    out = asyncio.run(_post_broker(sess, monkeypatch)._api_post("u", "TTTC0802U", {}, retry=False))
    assert out.get("_unknown") is True and out["rt_cd"] == "-1" and sess.calls == 1


def test_b1_session_connect_failure_is_not_unknown(monkeypatch):
    b = _post_broker(_FakeSession(asyncio.TimeoutError()), monkeypatch)
    b._session = None

    async def _no_connect():
        return False
    b.connect = _no_connect
    out = asyncio.run(b._api_post("u", "TTTC0802U", {}, retry=False))
    assert out == {"rt_cd": "-1", "msg1": "세션 연결 실패"}


def test_b1_5xx_json_body_is_returned_as_is(monkeypatch):
    body = {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수 초과"}
    sess = _FakeSession(_FakeResp(500, body))
    out = asyncio.run(_post_broker(sess, monkeypatch)._api_post("u", "TTTC0802U", {}, retry=False))
    assert out == body and sess.calls == 1


# ── B2 UNKNOWN BUY → 그날 신규 BUY 전부 보류 ────────────────────────────────

def test_b2_unknown_buy_is_reported_recorded_and_holds_later_buys(ub):
    ub._session = _RecSession(asyncio.TimeoutError())
    ok, msg = _submit(ub, _order())

    assert ok is False and msg.startswith(PREFIX + " ") and "네트워크 오류(재전송 금지)" in msg
    assert _events(ub).count(audit_log.EV_UNKNOWN) == 1 and audit_log.EV_REJECT not in _events(ub)
    assert ub._pending_orders == {} and ub._order_id_to_kis_no == {} and ub._order_id_to_orgno == {}
    assert len(ub._session.sent) == 1
    assert len(ub.alerts) == 1 and "주문 접수 불명" in ub.alerts[0] and "신규 매수 보류" in ub.alerts[0]
    saved = json.loads(ub.book_path.read_text(encoding="utf-8"))
    assert saved["date"] == "2026-09-29" and [e["side"] for e in saved["entries"]] == ["buy"]

    # 다음 BUY(다른 종목)는 POST 0회로 막히고 차단 기록이 남는다
    ub.audits.clear()
    ok2, msg2 = _submit(ub, _order(symbol=OTHER))
    assert ok2 is False and "신규 매수 보류" in msg2 and not msg2.startswith(PREFIX)
    assert len(ub._session.sent) == 1
    assert _events(ub) == ["blocked"] and ub.audits[0][1]["symbol"] == OTHER

    # SELL 은 보류 대상이 아니다 — 그대로 POST 된다
    ub._session = _RecSession(_FakeResp(200, OK_BODY))
    assert _submit(ub, _order(OrderSide.SELL)) == (True, "0001")
    assert len(ub._session.sent) == 1


# ── B3 UNKNOWN SELL → 그 종목 분할 매도 금지 표식, BUY 는 보류하지 않는다 ────

def test_b3_unknown_sell_marks_symbol_and_does_not_hold_buys(ub):
    ub._session = _RecSession(_NonJson(200, None))
    ok, msg = _submit(ub, _order(OrderSide.SELL))

    assert ok is False and msg.startswith(PREFIX)
    assert ub.has_unknown_sell(SYM) is True and ub.has_unknown_sell(OTHER) is False
    assert ub.unknown_buy_hold() is None
    assert "분할 매도 재발행 금지" in ub.alerts[0]

    ub._session = _RecSession(_FakeResp(200, OK_BODY))
    assert _submit(ub, _order(OrderSide.BUY, symbol=OTHER)) == (True, "0001")


# ── B4 POST 뒤 예외·rt_cd 무효 → UNKNOWN / POST 전 예외 → 현행 REJECT ─────────

def test_b4_exception_after_post_is_unknown(ub):
    ub._session = _RecSession(_FakeResp(200, [{"rt_cd": "0"}]))   # 본문이 dict 가 아니다
    ok, msg = _submit(ub, _order())
    assert ok is False and msg.startswith(PREFIX + " 예외:")
    assert audit_log.EV_UNKNOWN in _events(ub) and audit_log.EV_REJECT not in _events(ub)
    assert ub.unknown_buy_hold() is not None


def test_b4_exception_before_post_keeps_the_current_reject(ub):
    async def _boom(params):
        raise RuntimeError("hashkey 폭발")
    ub._get_hashkey = _boom
    assert _submit(ub, _order()) == (False, "hashkey 폭발")
    assert _events(ub) == [audit_log.EV_SUBMIT, audit_log.EV_REJECT]
    assert ub.audits[1][1]["reason"] == "예외: hashkey 폭발"
    assert ub._session.sent == [] and ub.unknown_buy_hold() is None


@pytest.mark.parametrize("body", [{}, {"output": {}}, {"rt_cd": None}, {"rt_cd": ""}])
def test_b4_json_without_valid_rt_cd_or_msg_cd_is_unknown(ub, body):
    ub._session = _RecSession(_FakeResp(200, body))
    ok, msg = _submit(ub, _order())
    assert ok is False and msg.startswith(PREFIX + " 응답에 rt_cd 없음")
    assert ub.unknown_buy_hold() is not None


def test_b4_msg_cd_without_rt_cd_is_an_explicit_reject(ub):
    ub._session = _RecSession(_FakeResp(500, {"msg_cd": "EGW00201"}))
    assert _submit(ub, _order()) == (False, "[EGW00201] 알 수 없는 오류")
    assert audit_log.EV_REJECT in _events(ub) and audit_log.EV_UNKNOWN not in _events(ub)
    assert ub.unknown_buy_hold() is None


# ── B4b 응답 대기 중 종료 취소 → 기록 후 다시 raise ─────────────────────────

class _HangSession:
    """POST 가 서버에 닿은 뒤 응답이 오지 않는다."""
    closed = False

    def __init__(self):
        self.calls = 0
        self.entered = asyncio.Event()

    def post(self, *a, **k):
        self.calls += 1
        sess = self

        class _CM:
            async def __aenter__(self_inner):
                sess.entered.set()
                await asyncio.Event().wait()

            async def __aexit__(self_inner, *exc):
                return False
        return _CM()


def test_b4b_cancel_while_waiting_for_the_response_records_and_reraises(ub):
    sess = ub._session = _HangSession()

    async def run():
        task = asyncio.create_task(ub.submit_order(_order()))
        await sess.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())

    assert sess.calls == 1 and audit_log.EV_UNKNOWN in _events(ub)
    assert ub.alerts == []                                  # 종료 중에는 알림을 보내지 않는다
    # 같은 날 재시작한 새 장부도 BUY 를 보류한다
    assert UnknownOrderBook(ub.book_path, now=NOW).buy_hold_reason(NOW) is not None


# ── B4c 전송 직전 재확인 — 머리 게이트를 지난 BUY 가 그사이 생긴 불명을 넘지 않는다 ──

def test_b4c_buy_waiting_for_hashkey_is_not_sent_after_another_buy_became_unknown(ub):
    ub._session = _RecSession(asyncio.TimeoutError())

    async def run():
        release, waiting = asyncio.Event(), asyncio.Event()

        async def _hashkey(params):
            if params["PDNO"] == OTHER:
                waiting.set()
                await release.wait()
            return "h"
        ub._get_hashkey = _hashkey
        first = asyncio.create_task(ub.submit_order(_order(symbol=OTHER)))
        await waiting.wait()                                # 머리 게이트 통과, hashkey 대기 중
        second = await ub.submit_order(_order(symbol=SYM))  # 응답 유실 → 불명
        release.set()
        return await first, second

    first, second = asyncio.run(run())
    assert second[1].startswith(PREFIX)
    assert first[0] is False and "신규 매수 보류" in first[1]
    assert [s[1]["PDNO"] for s in ub._session.sent] == [SYM]   # 대기하던 BUY 는 POST 0회
    assert [(e, f["symbol"]) for e, f in ub.audits if e == "blocked"] == [("blocked", OTHER)]


def test_b4c_gate_is_rechecked_before_the_401_resend(ub):
    ub._session = _RecSession(_FakeResp(401, {}))

    async def _recover():   # 토큰 갱신을 기다리는 사이 다른 BUY 가 불명이 됐다
        ub._unknown_book.record("buy", OTHER, 5, "시험", NOW)
    ub._recover_token = _recover

    ok, msg = _submit(ub, _order())
    assert ok is False and "신규 매수 보류" in msg
    assert len(ub._session.sent) == 1                       # 401 재전송이 나가지 않았다
    assert "blocked" in _events(ub) and audit_log.EV_UNKNOWN not in _events(ub)


# ── B5 명시적 거절·성공 경로는 현행 그대로 ──────────────────────────────────

def test_b5_explicit_reject_keeps_the_current_behaviour(ub):
    ub._session = _RecSession(_FakeResp(200, {"rt_cd": "1", "msg_cd": "APBK0919", "msg1": "주문가능금액 초과"}))
    assert _submit(ub, _order()) == (False, "[APBK0919] 주문가능금액 초과")
    assert _events(ub) == [audit_log.EV_SUBMIT, audit_log.EV_REJECT]
    assert ub.audits[1][1] == {"market": "KR", "symbol": SYM, "side": "buy", "qty": 10,
                               "reason": "[APBK0919] 주문가능금액 초과"}
    assert ub.unknown_buy_hold() is None and ub.alerts == [] and not ub.book_path.exists()


def test_b5_success_path_is_unchanged(ub):
    order = _order()
    assert _submit(ub, order) == (True, "0001")
    assert ub._session.sent == [("TTTC0802U", {
        "CANO": "12345678", "ACNT_PRDT_CD": "01", "PDNO": SYM, "ORD_DVSN": "00", "ORD_QTY": "10",
        "ORD_UNPR": "70000", "CTAC_TLNO": "", "SLL_TYPE": "", "ALGO_NO": ""})]
    assert ub._pending_orders == {order.id: order} and ub._order_id_to_kis_no == {order.id: "0001"}
    assert ub._order_id_to_orgno == {order.id: "91252"}
    assert _events(ub) == [audit_log.EV_SUBMIT, audit_log.EV_ACCEPT]
    assert ub.unknown_buy_hold() is None and not ub.book_path.exists()


def test_b5_bare_broker_without_a_book_does_not_raise():
    b = object.__new__(kis_kr.KISBroker)
    assert b.unknown_buy_hold() is None and b.has_unknown_sell(SYM) is False


# ── B6 장부 영속 ────────────────────────────────────────────────────────────

def test_b6_same_day_restart_holds_and_next_day_releases(tmp_path):
    path = tmp_path / "order_unknown.json"
    assert UnknownOrderBook(path, now=NOW).record("buy", SYM, 10, "응답 유실", NOW) is True
    assert UnknownOrderBook(path, now=NOW).buy_hold_reason(NOW) is not None
    tomorrow = NOW + timedelta(days=1)
    assert UnknownOrderBook(path, now=tomorrow).buy_hold_reason(tomorrow) is None


def test_b6_same_object_releases_when_the_date_changes(tmp_path):
    book = UnknownOrderBook(tmp_path / "u.json", now=NOW)
    book.record("buy", SYM, 10, "응답 유실", NOW)
    book.record("sell", OTHER, 5, "응답 유실", NOW)
    assert book.buy_hold_reason(NOW) is not None and book.has_unknown_sell(OTHER, NOW)
    tomorrow = NOW + timedelta(days=1)
    assert book.buy_hold_reason(tomorrow) is None and not book.has_unknown_sell(OTHER, tomorrow)


@pytest.mark.parametrize("mtime,held", [(NOW - timedelta(hours=2), True), (NOW - timedelta(days=1), False)])
def test_b6_corrupt_file_holds_only_when_modified_today(tmp_path, mtime, held):
    path = tmp_path / "order_unknown.json"
    path.write_text('{"date": "2026-09-29", "entries": [', encoding="utf-8")
    os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    book = UnknownOrderBook(path, now=NOW)
    assert (book.buy_hold_reason(NOW) is not None) is held
    assert book.has_unknown_sell(SYM, NOW) is held and book.has_unknown_sell(OTHER, NOW) is held


def test_b6_write_failure_keeps_the_hold_in_memory_and_says_so(ub, monkeypatch):
    def _fail(*_a, **_k):
        raise OSError("디스크 가득")
    monkeypatch.setattr(order_unknown, "atomic_write_json", _fail)
    ub._session = _RecSession(asyncio.TimeoutError())

    ok, msg = _submit(ub, _order())
    assert ok is False and msg.startswith(PREFIX)
    assert ub.unknown_buy_hold() is not None                # 이 프로세스 안에서는 보류
    assert "재시작 보호 없음" in ub.alerts[0]
    # 문서화된 한계 — 저장 실패 뒤 같은 날 새 장부(재시작)는 보류하지 않는다
    assert UnknownOrderBook(ub.book_path, now=NOW).buy_hold_reason(NOW) is None


def test_b6_merge_save_keeps_the_other_writers_entries(tmp_path):
    path = tmp_path / "order_unknown.json"
    bot, cli = UnknownOrderBook(path, now=NOW), UnknownOrderBook(path, now=NOW)
    cli.record("buy", OTHER, 3, "CLI 응답 유실", NOW)
    bot.record("sell", SYM, 10, "봇 응답 유실", NOW + timedelta(minutes=1))

    fresh = UnknownOrderBook(path, now=NOW)
    assert fresh.buy_hold_reason(NOW) is not None and fresh.has_unknown_sell(SYM, NOW)
    assert sorted(e["side"] for e in json.loads(path.read_text(encoding="utf-8"))["entries"]) == ["buy", "sell"]


def test_b6_merge_rereads_inside_the_lock(tmp_path):
    """두 장부가 같은 이전 상태(빈 파일)를 읽은 뒤, 첫 장부의 재읽기 직후 다른 기록자가 끼어들어도 두 항목이 남는다.
    재읽기가 잠금 안이어야 한다 — 그 순간 다른 열기로 비차단 잠금을 시도하면 막혀야 한다."""
    path = tmp_path / "order_unknown.json"
    lock_path = tmp_path / "order_unknown.json.lock"
    bot, cli = UnknownOrderBook(path, now=NOW), UnknownOrderBook(path, now=NOW)
    seen = {}
    real_read = bot._read

    def _read_then_race(today):
        out = real_read(today)
        with open(lock_path, "a") as probe:
            try:
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen["held"] = False
            except BlockingIOError:
                seen["held"] = True
        writer = threading.Thread(target=cli.record, args=("buy", OTHER, 3, "CLI 응답 유실", NOW))
        writer.start()                                      # 다른 기록자가 이 틈에 쓴다(잠금이 있으면 기다린다)
        writer.join(timeout=0.3)
        seen["writer"] = writer
        return out
    bot._read = _read_then_race

    assert bot.record("sell", SYM, 10, "봇 응답 유실", NOW) is True
    seen["writer"].join(timeout=10)
    assert seen["held"] is True
    fresh = UnknownOrderBook(path, now=NOW)
    assert fresh.buy_hold_reason(NOW) is not None and fresh.has_unknown_sell(SYM, NOW)


def test_b6_lock_failure_is_a_save_failure(tmp_path, monkeypatch):
    def _no_lock(*_a):
        raise OSError("잠금 불가")
    monkeypatch.setattr(order_unknown.fcntl, "flock", _no_lock)
    book = UnknownOrderBook(tmp_path / "order_unknown.json", now=NOW)
    assert book.record("buy", SYM, 10, "응답 유실", NOW) is False
    assert book.buy_hold_reason(NOW) is not None                     # 메모리 보류는 유지
    assert not (tmp_path / "order_unknown.json").exists()


def test_b6_real_constructor_restores_todays_book(home, monkeypatch):
    """운영 생성자가 임시 HOME 의 오늘 장부를 한 번 읽어 BUY 를 보류한다(같은 날 재시작 보호)."""
    _freeze_clock(monkeypatch, order_unknown, NOW)
    _freeze_clock(monkeypatch, kis_kr, NOW)
    path = home / ".cache" / "ai_trader" / "order_unknown.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"date": "2026-09-29", "entries": [
        {"side": "buy", "symbol": SYM, "qty": 10, "reason": "응답 유실", "at": "2026-09-29T09:10:00"}]}),
        encoding="utf-8")
    b = kis_kr.KISBroker(config=kis_kr.KISConfig(app_key="k", app_secret="s", account_no="12345678"))
    hold = b.unknown_buy_hold()
    assert isinstance(hold, str) and SYM in hold
    assert b.has_unknown_sell(SYM) is False


def test_run_trader_wires_the_partial_exit_block_next_to_the_pending_verifier():
    """배선 회귀 방지 — pending 검증자 배선 블록(브로커가 있을 때만) 안에서 브로커 메서드를 넘긴다."""
    tree = ast.parse((ROOT / "scripts" / "run_trader.py").read_text(encoding="utf-8"))
    blocks = [n for n in ast.walk(tree) if isinstance(n, ast.If)
              and ast.unparse(n.test) == "self.exit_manager and self.broker"
              and "set_pending_verifier" in ast.unparse(n)]
    assert len(blocks) == 1
    calls = [ast.unparse(c) for c in ast.walk(blocks[0]) if isinstance(c, ast.Call)
             and isinstance(c.func, ast.Attribute) and c.func.attr == "set_partial_exit_block"]
    assert calls == ["self.exit_manager.set_partial_exit_block(self.broker.has_unknown_sell)"]


def test_b6_default_path_follows_home(home):
    assert order_unknown.default_path() == home / ".cache" / "ai_trader" / "order_unknown.json"


# ── E1 엔진 BUY 조기 차단 ───────────────────────────────────────────────────

def _buy_rm(monkeypatch, broker):
    from test_risk_sizing import _em, _rm
    from test_t11_entry_plan import _order_env
    rm = _rm(monkeypatch, mode="nominal", em=_em())
    _order_env(monkeypatch, rm)
    rm.validated = []
    rm._cross_validator.validate = lambda **kw: (rm.validated.append(kw["symbol"]) or (True, kw["score"], ""))
    rm.engine.broker = broker
    return rm


def test_e1_buy_signal_is_dropped_before_cross_validation(home, monkeypatch):
    from test_t11_entry_plan import _buy_signal
    rm = _buy_rm(monkeypatch, SimpleNamespace(unknown_buy_hold=lambda: "접수 불명 보류: 시험"))
    assert asyncio.run(rm.on_signal(_buy_signal())) is None
    assert rm.validated == [] and rm._pending_orders == set()


@pytest.mark.parametrize("broker", [
    SimpleNamespace(unknown_buy_hold=lambda: None),
    SimpleNamespace(unknown_buy_hold=lambda: ""),
    SimpleNamespace(unknown_buy_hold=lambda: 1),           # str 이 아니면 차단하지 않는다
    SimpleNamespace(unknown_buy_hold="보류"),              # 호출 불가
    MagicMock(),                                           # 우연히 무엇이든 돌려주는 가짜
    None,
])
def test_e1_buy_signal_passes_without_a_real_hold_reason(home, monkeypatch, broker):
    from test_t11_entry_plan import _buy_signal
    rm = _buy_rm(monkeypatch, broker)
    assert asyncio.run(rm.on_signal(_buy_signal()))
    assert rm.validated


# ── E0 ExitManager 발생원 차단 ──────────────────────────────────────────────

def _freeze_exit_clock(monkeypatch):
    """ExitManager 의 datetime.now()·date.today() 를 NOW 로 고정한다."""
    import src.strategies.exit_manager as xm
    _freeze_clock(monkeypatch, xm, NOW)

    class _Day(date):
        @classmethod
        def today(cls):
            return NOW.date()
    monkeypatch.setattr(xm, "date", _Day)


def _em_with_position(monkeypatch, block, qty=100):
    _freeze_exit_clock(monkeypatch)
    em = ExitManager(ExitConfig(stop_loss_pct=5.0, min_stop_pct=4.0, max_stop_pct=8.0, atr_multiplier=2.0))
    em.register_position(Position(symbol=SYM, quantity=qty, avg_price=Decimal("10000"),
                                  current_price=Decimal("10000"), strategy="sepa_trend"),
                         stop_loss_pct=5.0, trailing_stop_pct=3.0, atr_pct_hint=2.5)
    if block is not None:
        em.set_partial_exit_block(block)
    return em


def test_e0_blocked_symbol_gets_no_partial_exit_but_keeps_its_stop(home, monkeypatch):
    em = _em_with_position(monkeypatch, lambda s: s == SYM)
    assert em.update_price(SYM, Decimal("11300")) is None          # +13% — 1차 익절 조건 충족
    assert em.get_state(SYM).pending_stage is None
    action, qty, reason = em.update_price(SYM, Decimal("9300"))     # 같은 흐름에서 손절선 아래로
    assert action == "sell_all" and qty == 100 and "손절" in reason


@pytest.mark.parametrize("block", [None, lambda s: False, lambda s: s == OTHER, MagicMock()])
def test_e0_partial_exit_is_unchanged_without_a_true_block(home, monkeypatch, block):
    em = _em_with_position(monkeypatch, block)
    action, qty, _ = em.update_price(SYM, Decimal("11300"))
    assert (action, qty) == ("sell_partial", 10)
    assert em.get_state(SYM).pending_stage == ExitStage.FIRST


def test_e0_blocked_partial_leaves_no_side_effects(home, monkeypatch):
    em = _em_with_position(monkeypatch, lambda s: True)
    st = em.get_state(SYM)
    history = len(st.exit_history)
    persisted = []
    monkeypatch.setattr(em, "_persist_states", lambda: persisted.append(1))
    assert em.update_price(SYM, Decimal("11300")) is None
    assert (st.pending_stage, st.pending_since, st.pending_target_qty) == (None, None, 0)
    assert len(st.exit_history) == history and persisted == []


def test_e0_stage_exit_of_the_whole_remainder_still_goes_out(home, monkeypatch):
    """잔량이 작아 단계 청산이 전량(sell_all)이면 훅이 참이어도 낸다 — 이중 발행은 매도가능수량이 막는다."""
    em = _em_with_position(monkeypatch, lambda s: True, qty=1)
    action, qty, reason = em.update_price(SYM, Decimal("11300"))
    assert (action, qty) == ("sell_all", 1) and "1차 익절" in reason
    assert em.get_state(SYM).pending_stage == ExitStage.FIRST


def _pending_fields(st):
    return (st.pending_stage, st.pending_since, st.pending_target_qty, st.pending_filled_qty)


@pytest.mark.parametrize("stage,price,label", [
    (ExitStage.FIRST, Decimal("11600"), "2차 익절"),     # +16% ≥ 2차 15%
    (ExitStage.SECOND, Decimal("12600"), "3차 익절"),    # +26% ≥ 3차 25%
])
def test_e0_later_stage_partial_is_blocked_without_side_effects(home, monkeypatch, stage, price, label):
    """구현 리뷰 2회차: 2차·3차 가드를 각각 고정한다 — 분할이면 None, pending 네 필드·이력·영속 쓰기 불변."""
    em = _em_with_position(monkeypatch, lambda s: True)
    st = em.get_state(SYM)
    st.current_stage = stage
    st.breakeven_activated = True        # 본전 이동의 영속 쓰기를 분리한다 — 이 시험은 분할 경로만 본다
    before, history = _pending_fields(st), len(st.exit_history)
    persisted = []
    monkeypatch.setattr(em, "_persist_states", lambda: persisted.append(1))
    assert em.update_price(SYM, price) is None
    assert _pending_fields(st) == before and len(st.exit_history) == history and persisted == []

    # 대조군 — 훅이 없으면 같은 가격에서 이 단계의 분할이 나간다
    em.set_partial_exit_block(None)
    action, qty, reason = em.update_price(SYM, price)
    assert (action, qty) == ("sell_partial", 50) and label in reason


@pytest.mark.parametrize("stage,price,label", [
    (ExitStage.FIRST, Decimal("11600"), "2차 익절"),
    (ExitStage.SECOND, Decimal("12600"), "3차 익절"),
])
def test_e0_later_stage_exit_of_the_whole_remainder_still_goes_out(home, monkeypatch, stage, price, label):
    em = _em_with_position(monkeypatch, lambda s: True, qty=1)
    st = em.get_state(SYM)
    st.current_stage = stage
    st.breakeven_activated = True
    action, qty, reason = em.update_price(SYM, price)
    assert (action, qty) == ("sell_all", 1) and label in reason


def test_e0_hard_expiry_of_an_old_pending_still_runs_when_blocked(home, monkeypatch):
    """구현 리뷰 2회차 처분(변경 없음 고정): `_check_partial_exit` 머리의 30분 하드 만료는 기준 동작 그대로 틱마다 돈다 —
    훅이 참이어도 31분 된 pending_stage 는 만료되고, 그 뒤의 분할 후보는 부작용 없이 걸러진다."""
    em = _em_with_position(monkeypatch, lambda s: True)

    async def _verifier(_symbol):
        return None
    em.set_pending_verifier(_verifier)   # 운영처럼 검증자가 배선돼 하드 만료는 30분
    st = em.get_state(SYM)
    st.pending_stage, st.pending_since = ExitStage.FIRST, NOW - timedelta(minutes=31)
    st.pending_target_qty = 10
    history = len(st.exit_history)
    persisted = []
    monkeypatch.setattr(em, "_persist_states", lambda: persisted.append(1))

    assert em.update_price(SYM, Decimal("11300")) is None
    assert _pending_fields(st) == (None, None, 0, 0)
    assert persisted == [1] and len(st.exit_history) == history   # 만료 영속 1회뿐 — 새 분할 후보는 흔적 없음


def test_e0_third_to_trailing_transition_is_not_blocked(home, monkeypatch):
    em = _em_with_position(monkeypatch, lambda s: True)
    em.get_state(SYM).current_stage = ExitStage.THIRD
    assert em.update_price(SYM, Decimal("12700")) is None           # +27% ≥ 3차 25% + 1
    assert em.get_state(SYM).current_stage == ExitStage.TRAILING


def _scheduler(monkeypatch, em):
    import src.schedulers.kr_scheduler as ks
    _freeze_clock(monkeypatch, ks, NOW)
    emitted = []

    async def _emit(event):
        emitted.append(event)

    async def _noop():
        return None
    sched = object.__new__(ks.KRScheduler)
    sched._cleanup_stale_pending = _noop
    position = Position(symbol=SYM, quantity=100, avg_price=Decimal("10000"),
                        current_price=Decimal("10000"), strategy="sepa_trend")
    sched.bot = SimpleNamespace(
        exit_manager=em, broker=object(), _pause_resume_at=None,
        engine=SimpleNamespace(emit=_emit, risk_manager=None,
                               portfolio=SimpleNamespace(positions={SYM: position})),
        _exit_pending_symbols=set(), _exit_pending_timestamps={}, _exit_reasons={},
        _sell_blocked_symbols={}, _strategy_exit_params={},
    )
    return sched, emitted


def test_e0_scheduler_stop_is_not_masked_by_a_dropped_partial(home, monkeypatch):
    sched, emitted = _scheduler(monkeypatch, _em_with_position(monkeypatch, lambda s: s == SYM))
    asyncio.run(sched._check_exit_signal(SYM, Decimal("11300")))
    assert emitted == [] and sched.bot._exit_pending_symbols == set()   # 분할 신호도 pending 도 없다

    asyncio.run(sched._check_exit_signal(SYM, Decimal("9300")))
    assert [e.metadata["exit_action"] for e in emitted] == ["sell_all"]
    assert SYM in sched.bot._exit_pending_symbols


def test_e0_scheduler_control_partial_pending_masks_the_next_tick(home, monkeypatch):
    """대조군 — 훅이 없으면 분할 신호가 청산 pending 을 걸고, 다음 틱의 손절 판정은 그 pending 에 가려진다."""
    sched, emitted = _scheduler(monkeypatch, _em_with_position(monkeypatch, None))
    asyncio.run(sched._check_exit_signal(SYM, Decimal("11300")))
    asyncio.run(sched._check_exit_signal(SYM, Decimal("9300")))
    assert [e.metadata["exit_action"] for e in emitted] == ["sell_partial"]


# ── E2 on_signal 분할 SELL 가드 ─────────────────────────────────────────────

def _sell_rm_unknown(monkeypatch, unknown=lambda s: s == SYM):
    from test_exit_exempt_sell_guard import _pos, _sell_rm
    rm = _sell_rm(monkeypatch, {SYM: _pos(SYM, "sepa_trend"), OTHER: _pos(OTHER, "sepa_trend")}, set())
    rm.engine.broker.has_unknown_sell = unknown
    return rm


@pytest.mark.parametrize("meta", [
    {"quantity": 10, "exit_action": "sell_partial"},
    {"quantity": 10},                                      # exit_action 없는 발행처(트림 등) — 수량 의도로 분할
    {"quantity": 100, "exit_action": "sell_partial"},      # 불명 분할 체결 → 동기화로 보유만 줄어든 뒤 같은 단계 재발행
])
def test_e2_partial_sell_on_an_unknown_symbol_is_dropped(home, monkeypatch, meta):
    from test_exit_exempt_sell_guard import _sell_event
    rm = _sell_rm_unknown(monkeypatch)
    assert asyncio.run(rm.on_signal(_sell_event(SYM, "exit_manager", StrategyType.SEPA_TREND, **meta))) is None
    assert SYM not in rm._pending_orders and SYM not in rm._partial_action_marks()


@pytest.mark.parametrize("meta", [{}, {"quantity": 100, "exit_action": "sell_all"}, {"quantity": 100}])
def test_e2_full_sell_on_an_unknown_symbol_still_goes_out(home, monkeypatch, meta):
    from test_exit_exempt_sell_guard import _sell_event
    rm = _sell_rm_unknown(monkeypatch)
    orders = asyncio.run(rm.on_signal(_sell_event(SYM, "exit_manager", StrategyType.SEPA_TREND, **meta)))
    assert orders and orders[0].order.quantity == 100


@pytest.mark.parametrize("unknown", [lambda s: s == OTHER, lambda s: "yes", MagicMock()])
def test_e2_partial_sell_passes_without_a_true_unknown(home, monkeypatch, unknown):
    from test_exit_exempt_sell_guard import _sell_event
    rm = _sell_rm_unknown(monkeypatch, unknown)
    orders = asyncio.run(rm.on_signal(_sell_event(SYM, "exit_manager", StrategyType.SEPA_TREND,
                                                  quantity=10, exit_action="sell_partial")))
    assert orders and orders[0].order.quantity == 10
    assert SYM in rm._partial_action_marks()                # 명시 액션은 폴백 가드용으로 따로 남는다


# ── E3 90초 SELL 폴백 ───────────────────────────────────────────────────────

def _fallback_rm(monkeypatch, *, partial, unknown):
    from test_stale_sell_cancel_failure import SellBroker, _engine
    broker = SellBroker(cancelled=1, tracked=[])           # 취소 ACK — 종전이면 곧바로 시장가 전환
    broker.has_unknown_sell = lambda s: unknown and s == SYM
    return _engine(monkeypatch, broker, partial=partial), broker


def test_e3_partial_fallback_on_an_unknown_symbol_submits_nothing_and_releases(monkeypatch):
    from test_engine_stale_pending_fixes import _drive
    rm, broker = _fallback_rm(monkeypatch, partial=True, unknown=True)
    _drive(rm)
    assert broker.orders == [] and "submit" not in broker.calls
    assert SYM not in rm._pending_orders


def test_e3_explicit_partial_action_is_also_guarded(monkeypatch):
    from test_engine_stale_pending_fixes import _drive
    rm, broker = _fallback_rm(monkeypatch, partial=False, unknown=True)
    rm._partial_action_marks().add(SYM)                    # 수량 == 보유량이던 명시 분할
    _drive(rm)
    assert broker.orders == [] and SYM not in rm._pending_orders
    assert SYM not in rm._partial_action_marks()            # clear_pending 이 표식도 지운다


@pytest.mark.parametrize("partial,unknown", [(False, True), (True, False)])
def test_e3_full_fallback_or_known_symbol_still_submits(monkeypatch, partial, unknown):
    from test_engine_stale_pending_fixes import _drive
    rm, broker = _fallback_rm(monkeypatch, partial=partial, unknown=unknown)
    _drive(rm)
    assert [o.quantity for o in broker.orders] == [10]


# ── E4 on_order 좀비 카운터 ─────────────────────────────────────────────────

def test_e4_qty_exceeded_is_still_counted_on_an_unknown_sell_symbol(home, monkeypatch):
    """구현 리뷰 1회차 P1-2: 불명 전량 SELL 이 실제 체결되면 동기화가 유령 제거를 미룬다 — 재발행의 APBK0400 을
    세지 않으면 강제 정리 경로가 끊긴다. 불명 종목도 현행대로 센다(잘못된 좀비 알림 한 통보다 회복 경로가 중요)."""
    async def _alert(text, **_k):
        return True
    monkeypatch.setattr("src.utils.telegram.send_alert", _alert)
    rm = _sell_rm_unknown(monkeypatch)
    rm._kis_qty_mismatch_count, rm._zombie_candidate_symbols = {}, set()

    async def _reject(order):
        return False, "[APBK0400] 주문 가능한 수량을 초과했습니다"
    rm.engine.broker.submit_order = _reject

    def _sell_order(symbol):
        rm._pending_orders.add(symbol)
        return OrderEvent.from_order(
            Order(symbol=symbol, side=OrderSide.SELL, order_type=OrderType.LIMIT, quantity=100,
                  price=Decimal("9000"), strategy="sepa_trend", reason="손절"), source="risk_manager")

    async def run():
        for _ in range(2):
            await rm.on_order(_sell_order(SYM))
            await rm.on_order(_sell_order(OTHER))
        await asyncio.sleep(0)
    asyncio.run(run())

    assert rm._kis_qty_mismatch_count == {SYM: 2, OTHER: 2}
    assert rm._zombie_candidate_symbols == {SYM, OTHER}
