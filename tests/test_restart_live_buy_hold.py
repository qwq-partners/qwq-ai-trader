"""재시작 시 거래소에 살아 있는 BUY → 그날 신규 BUY 보류 (2026-09-29).

재시작하면 엔진 pending·브로커 추적이 비어 재시작 전에 접수된 지정가 BUY 를 모른 채 같은 종목을 다시 살 수
있었다. 기동 직후 KR 거래일 08:00~20:00 에만 거래소 미체결을 한 번 조회해 접수 불명 장부(order_unknown)에
보류를 기록한다. 조회 실패·불완전·예외는 전면 보류(fail-closed), SELL 만·없음은 무기록, 장외·휴장일은 조회 0회.

전부 합성 입력 — 장부는 tmp_path, 시계는 동결, 브로커 조회는 가짜.
"""
from __future__ import annotations

import ast
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.execution.broker import kis_kr  # noqa: E402
from src.risk.order_unknown import UnknownOrderBook  # noqa: E402

from test_order_post_unknown import NOW, OTHER, SYM, _events, _order, _submit, ub  # noqa: E402,F401
from test_t11_entry_plan import _freeze_clock  # noqa: E402

# ub 픽스처는 세션 판정을 "regular" 로 고정한다 — 시각 창을 보는 시험은 실제 판정 함수로 되돌린다
_REAL_SESSION = kis_kr.KISBroker._get_current_market_session


def _fake_open_orders(b, result):
    """get_exchange_open_orders 를 가짜로 — 호출 인자를 기록하고 result(예외면 raise)를 돌려준다."""
    b.queries = []

    async def fake(symbol=None, *, require_complete=False):
        b.queries.append((symbol, require_complete))
        if isinstance(result, Exception):
            raise result
        return result
    b.get_exchange_open_orders = fake


def _run(b):
    async def go():
        await b.hold_buys_for_live_orders_at_restart()
        await asyncio.sleep(0)   # 알림 태스크가 한 번 돌 기회
    asyncio.run(go())


def test_live_buy_holds_that_day_and_the_next_buy_is_blocked(ub):
    _fake_open_orders(ub, [{"symbol": SYM, "side": "buy", "qty": 7}, {"symbol": OTHER, "side": "sell", "qty": 3}])
    _run(ub)
    assert ub.queries == [(None, True)]
    hold = ub.unknown_buy_hold()
    assert hold is not None and f"재시작 시 거래소 미체결 BUY {SYM} 7" in hold
    assert ub.has_unknown_sell(OTHER) is False            # BUY 보류만 — 분할 매도는 막지 않는다
    assert [(e, f["symbol"], f["side"], f["qty"]) for e, f in ub.audits] == [("unknown", SYM, "buy", 7)]
    assert len(ub.alerts) == 1 and SYM in ub.alerts[0]
    # 영속 — 같은 날 다시 재시작해도 보류
    assert UnknownOrderBook(ub.book_path, now=NOW).buy_hold_reason(NOW) is not None
    ok, msg = _submit(ub, _order(qty=7))
    assert ok is False and msg == hold and ub._session.calls == 0


@pytest.mark.parametrize("rows", [[], [{"symbol": OTHER, "side": "sell", "qty": 3}]])
def test_no_buy_rows_record_nothing(ub, rows):
    _fake_open_orders(ub, rows)
    _run(ub)
    assert ub.queries == [(None, True)]
    assert ub.unknown_buy_hold() is None and ub.audits == [] and ub.alerts == []
    assert not ub.book_path.exists()


@pytest.mark.parametrize("result", [None, RuntimeError("boom")])
def test_query_failure_or_exception_holds_all_buys_and_returns(ub, result):
    _fake_open_orders(ub, result)
    _run(ub)                                               # 예외가 밖으로 나오지 않는다(기동 계속)
    hold = ub.unknown_buy_hold()
    assert hold is not None and "재시작 시 거래소 미체결 조회 실패/불완전" in hold
    assert _events(ub) == ["unknown"] and len(ub.alerts) == 1
    ok, _ = _submit(ub, _order(symbol=OTHER))              # 다른 종목도 보류
    assert ok is False and ub._session.calls == 0


def _api_get_returning(b, tr_cont, rows):
    async def fake(url, tr_id, params, tr_cont_req=""):
        return {"rt_cd": "0", "_tr_cont": tr_cont,
                "output": [{"pdno": s, "sll_buy_dvsn_cd": "02" if side == "buy" else "01", "rmn_qty": q}
                           for s, side, q in rows]}
    b._api_get = fake


def test_incomplete_first_page_holds_all_buys(ub):
    _api_get_returning(ub, "F", [(OTHER, "sell", 3)])      # 첫 페이지에 BUY 없음 + 다음 페이지 남음
    _run(ub)
    hold = ub.unknown_buy_hold()
    assert hold is not None and "조회 실패/불완전" in hold


@pytest.mark.parametrize("tr_cont", ["F", "M"])
def test_existing_callers_keep_the_first_page_rows(ub, tr_cont):
    """require_complete 기본값(False)은 종전 그대로 — symbol 미지정 호출은 첫 페이지 행을 돌려준다."""
    _api_get_returning(ub, tr_cont, [(OTHER, "sell", 3)])
    rows = asyncio.run(ub.get_exchange_open_orders())
    assert rows == [{"symbol": OTHER, "side": "sell", "qty": 3}]
    assert asyncio.run(ub.get_exchange_open_orders(require_complete=True)) is None


@pytest.mark.parametrize("when", [
    datetime(2026, 9, 29, 7, 0),     # 장전 배포
    datetime(2026, 9, 29, 20, 45),   # 장후 배포
    datetime(2026, 10, 3, 10, 30),   # 토요일 장중 시각
])
def test_off_hours_or_holiday_queries_nothing(ub, monkeypatch, when):
    monkeypatch.setattr(kis_kr.KISBroker, "_get_current_market_session", _REAL_SESSION)
    _freeze_clock(monkeypatch, kis_kr, when)
    _fake_open_orders(ub, None)
    _run(ub)
    assert ub.queries == [] and ub.unknown_buy_hold() is None and ub.audits == []


def test_fixed_holiday_on_a_weekday_queries_nothing(ub, monkeypatch):
    monkeypatch.setattr(kis_kr, "is_kr_market_holiday", lambda d: True)
    _fake_open_orders(ub, None)
    _run(ub)
    assert ub.queries == []


@pytest.mark.parametrize("when", [datetime(2026, 9, 29, 8, 0), datetime(2026, 9, 29, 8, 55),
                                  datetime(2026, 9, 29, 15, 25), datetime(2026, 9, 29, 19, 59)])
def test_whole_0800_2000_window_including_gaps_queries(ub, monkeypatch, when):
    monkeypatch.setattr(kis_kr.KISBroker, "_get_current_market_session", _REAL_SESSION)
    _freeze_clock(monkeypatch, kis_kr, when)
    _fake_open_orders(ub, [])
    _run(ub)
    assert ub.queries == [(None, True)]


def test_next_day_releases(ub, monkeypatch):
    _fake_open_orders(ub, [{"symbol": SYM, "side": "buy", "qty": 7}])
    _run(ub)
    assert ub.unknown_buy_hold() is not None
    tomorrow = NOW + timedelta(days=1)
    _freeze_clock(monkeypatch, kis_kr, tomorrow)
    assert ub.unknown_buy_hold() is None
    assert UnknownOrderBook(ub.book_path, now=tomorrow).buy_hold_reason(tomorrow) is None


def test_run_trader_calls_it_once_inside_a_try_after_holidays():
    """배선 회귀 방지 — KR 초기화에서 휴장일 로드 뒤 try 안에서 한 번 부른다(예외가 기동을 멈추지 않게)."""
    src = (ROOT / "scripts" / "run_trader.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_initialize_kr")
    tries = [t for t in ast.walk(fn) if isinstance(t, ast.Try)
             and any(isinstance(s, ast.Expr) and "hold_buys_for_live_orders_at_restart" in ast.unparse(s)
                     for s in t.body)]
    assert len(tries) == 1 and tries[0].handlers
    assert ast.unparse(fn).count("hold_buys_for_live_orders_at_restart") == 1
    assert src.index("set_kr_market_holidays(all_holidays)") < src.index("hold_buys_for_live_orders_at_restart")
