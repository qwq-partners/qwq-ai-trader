"""KIS 원장 TR 초당 한도 초과(EGW00215) 조사 후속 (2026-09-15).

고정하는 동작:
1) 잔고/포지션/체결 연속조회 루프가 헤더 tr_cont(D/E) 또는 동일 ctx 키에서 종료한다
   (보유 1종목 계좌에서 8434R 을 10페이지까지 호출하던 결함).
2) 잔고 스냅샷 재사용이 헤더 D/E 에서 실제로 발동해 동기화 1틱당 8434R 이 1회가 된다.
3) _api_get 의 3번째 500 도 리미터 계측을 거치고 실패로 판정되는 dict 를 반환한다(호출자 분기 불변).
4) 동기화 루프가 장외 CLOSED 에서 300초로 늘어나되 전환 직후 1회는 30초, 08:00~15:40 은 항상 30초.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils import kis_rate_limit  # noqa: E402
from test_review_fixes_2026_09 import _bare_broker  # noqa: E402

_POS = {"pdno": "5930", "hldg_qty": "3", "pchs_avg_pric": "70000", "prpr": "71000", "prdt_name": "삼성전자"}
_POS2 = {"pdno": "660", "hldg_qty": "1", "pchs_avg_pric": "100000", "prpr": "101000", "prdt_name": "SK하이닉스"}
_OUT2 = [{"dnca_tot_amt": "1000", "scts_evlu_amt": "213000", "evlu_pfls_smtl_amt": "3000",
          "pchs_amt_smtl_amt": "210000", "tot_evlu_amt": "214000"}]


def _broker():
    b = _bare_broker()
    b._session = SimpleNamespace(closed=False)
    b._token = "t"
    b._token_mgr = SimpleNamespace(_access_token="t", _is_token_valid=lambda: True, invalidate=lambda: None)
    b.config = SimpleNamespace(base_url="http://x", account_no="1", account_product_cd="01", env="prod")
    b._balance_snapshot = None
    return b


def _page(items, ctx="K1", tr_cont=None, output2=None):
    d = {"rt_cd": "0", "ctx_area_fk100": ctx, "ctx_area_nk100": ctx, "output1": items}
    if output2 is not None:
        d["output2"] = output2
    if tr_cont is not None:
        d["_tr_cont"] = tr_cont
    return d


def _install(b, pages):
    """페이지 응답 시퀀스를 순서대로 돌려주는 가짜 _api_get — 호출 tr_id 기록."""
    calls = []
    seq = list(pages)

    async def fake_get(url, tr_id, params, tr_cont=""):
        calls.append(tr_id)
        if tr_id == "TTTC8908R":
            return {"rt_cd": "0", "output": {"nrcvb_buy_amt": "1000"}}
        return seq.pop(0) if seq else _page([], ctx="", tr_cont="D")
    b._api_get = fake_get
    return calls


# ── 1) 페이지 루프 종료 ──────────────────────────────────────────────────────

def test_positions_single_page_stops_on_header_last():
    """KIS 가 ctx 키를 채워 보내도 헤더 D 면 1회로 끝난다(이전: 10회)."""
    b = _broker()
    calls = _install(b, [_page([_POS], ctx="K1", tr_cont="D")] + [_page([_POS], ctx="K1", tr_cont="D")] * 9)
    pos = asyncio.run(b.get_positions())
    assert pos["005930"].quantity == 3
    assert calls.count("TTTC8434R") == 1


def test_positions_single_page_stops_on_repeated_ctx_without_header():
    """헤더가 없으면(프록시·구 응답) 같은 ctx 키가 반복될 때 2회로 끝난다(이전: 10회)."""
    b = _broker()
    calls = _install(b, [_page([_POS], ctx="K1")] * 10)
    pos = asyncio.run(b.get_positions())
    assert pos["005930"].quantity == 3
    assert calls.count("TTTC8434R") == 2


def test_positions_two_real_pages_are_merged():
    """헤더 F(다음 있음)는 계속 읽고 합친다 — 새 종료 조건(D/E·동일 ctx)이 진짜 다음 페이지를
    끊지 않는지와 ctx 키가 2회차 요청에 되돌려지는지 고정. 요청 헤더 tr_cont 규약은
    가짜 _api_get 을 지나쳐 가므로 tests/test_kis_pagination_protocol.py 가 고정한다."""
    b = _broker()
    seen_params = []
    calls = _install(b, [_page([_POS], ctx="K1", tr_cont="F"), _page([_POS2], ctx="K2", tr_cont="D")])
    _orig = b._api_get

    async def _spy(url, tr_id, params, tr_cont=""):
        seen_params.append(dict(params))
        return await _orig(url, tr_id, params, tr_cont)
    b._api_get = _spy
    pos = asyncio.run(b.get_positions())
    assert set(pos) == {"005930", "000660"}
    assert calls.count("TTTC8434R") == 2
    assert seen_params[1]["CTX_AREA_NK100"] == "K1"     # 2회차 요청에 1페이지 ctx 되돌림


def test_daily_fills_loop_uses_same_termination(monkeypatch):
    from src.execution.broker.kis_kr import KISBroker
    b = _broker()
    monkeypatch.setattr(KISBroker, "is_connected", property(lambda self: True))
    fill = {"ODNO": "1", "pdno": "5930"}
    calls = _install(b, [_page([fill], ctx="K1")] * 10)
    items = asyncio.run(b._query_daily_fills("20260915"))
    assert len(items) == 1
    assert calls.count("TTTC8001R") == 2
    calls2 = _install(b, [_page([fill], ctx="K1", tr_cont="E")] * 3)
    asyncio.run(b._query_daily_fills("20260915"))
    assert calls2.count("TTTC8001R") == 1


# ── 2) 잔고 스냅샷 재사용이 실제로 발동 ─────────────────────────────────────

def test_balance_snapshot_reused_when_header_says_last_even_with_ctx_keys():
    """ctx 키가 채워져 있어도 헤더 D 면 스냅샷을 보관 → 동기화 1틱당 8434R 1회."""
    b = _broker()
    calls = _install(b, [_page([_POS], ctx="K1", tr_cont="D", output2=_OUT2)])
    bal = asyncio.run(b.get_account_balance())
    assert bal and b._balance_snapshot is not None
    pos = asyncio.run(b.get_positions())
    assert pos["005930"].quantity == 3
    assert calls.count("TTTC8434R") == 1


def test_balance_snapshot_not_kept_when_more_pages():
    b = _broker()
    _install(b, [_page([_POS], ctx="K1", tr_cont="F", output2=_OUT2)])
    asyncio.run(b.get_account_balance())
    assert b._balance_snapshot is None


def test_balance_snapshot_falls_back_to_ctx_keys_without_header():
    """헤더가 없으면 기존 키 기반(보수적) 판정 그대로 — 키가 있으면 보관하지 않는다."""
    b = _broker()
    _install(b, [_page([_POS], ctx="K1", output2=_OUT2)])
    asyncio.run(b.get_account_balance())
    assert b._balance_snapshot is None
    _install(b, [_page([_POS], ctx="", output2=_OUT2)])
    asyncio.run(b.get_account_balance())
    assert b._balance_snapshot is not None


# ── 3) 마지막 시도의 500 도 계측 ─────────────────────────────────────────────

def test_api_get_last_attempt_500_is_instrumented_and_returns_failure(monkeypatch):
    b = _broker()
    noted = []
    monkeypatch.setattr(kis_rate_limit, "note_ledger_rejection", lambda tr: noted.append(tr))
    monkeypatch.setattr(kis_rate_limit, "release_ledger", lambda: None)

    async def _no_limit(tr_id):
        return None
    b._rate_limit = _no_limit
    b._get_headers = lambda tr_id: {}

    class _Resp:
        status = 500
        headers = {}
        async def json(self):
            return {"rt_cd": "1", "msg_cd": "EGW00215", "msg1": "원장 초과"}
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    b._session = SimpleNamespace(closed=False, get=lambda url, headers=None, params=None: _Resp())

    async def _fast_sleep(_s):
        return None
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    data = asyncio.run(b._api_get("http://x", "TTTC8434R", {}))
    assert str(data.get("rt_cd")) != "0"
    assert noted == ["TTTC8434R"] * 3      # 3번째 시도도 계측된다(이전: 2회)


def test_api_get_attaches_tr_cont_header_to_data(monkeypatch):
    b = _broker()
    monkeypatch.setattr(kis_rate_limit, "release_ledger", lambda: None)

    async def _no_limit(tr_id):
        return None
    b._rate_limit = _no_limit
    b._get_headers = lambda tr_id: {}

    class _Resp:
        status = 200
        headers = {"tr_cont": "d"}
        async def json(self):
            return {"rt_cd": "0", "output1": []}
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    b._session = SimpleNamespace(closed=False, get=lambda url, headers=None, params=None: _Resp())
    data = asyncio.run(b._api_get("http://x", "TTTC8434R", {}))
    assert data["_tr_cont"] == "D"


# ── 4) 동기화 루프 세션 게이트 ───────────────────────────────────────────────

def test_sync_loop_sleeps_300s_only_after_second_consecutive_closed(monkeypatch):
    from src.core.types import MarketSession
    from src.schedulers import kr_scheduler
    from src.schedulers.kr_scheduler import KRScheduler

    sessions = [MarketSession.REGULAR, MarketSession.REGULAR, MarketSession.CLOSED,
                MarketSession.CLOSED, MarketSession.CLOSED]
    sleeps = []
    sched = KRScheduler.__new__(KRScheduler)
    sched.bot = SimpleNamespace(running=True, risk_manager=None)
    sched._get_current_session = lambda: sessions.pop(0) if sessions else MarketSession.CLOSED

    async def _sync():
        if not sessions:
            sched.bot.running = False
    sched._sync_portfolio = _sync

    async def _rec(s):
        sleeps.append(s)
    monkeypatch.setattr(kr_scheduler.asyncio, "sleep", _rec)

    # 장외 시각(21:00 거래일)으로 고정 — 08:00~15:40 이면 CLOSED 여도 30초 유지되므로
    from datetime import datetime as _dt
    sched._portfolio_sync_interval = (lambda closed, prev, now=None, _f=KRScheduler._portfolio_sync_interval:
                                      _f(sched, closed, prev, _dt(2026, 9, 15, 21, 0)))
    asyncio.run(sched.run_portfolio_sync())
    # 초기 30 → REGULAR 30, 30 → CLOSED 전환 직후 1회 30 → 연속 CLOSED 300 …
    assert sleeps == [30, 30, 30, 30, 300, 300, 300]


@pytest.mark.parametrize("hhmm, expect", [
    ((8, 55), 30),     # 장전 동시호가 — KRSession 은 CLOSED 지만 주문 접수 시간대 → 30초
    ((15, 25), 30),    # 마감 동시호가 — 15:30 체결 정합을 위해 30초
    ((15, 39), 30),
    ((15, 41), 300),   # NXT 세션(NEXT)이면 closed=False 라 실제로는 30초지만, 함수 단위로는 300 허용
    ((21, 0), 300),
    ((7, 30), 300),
])
def test_sync_interval_keeps_30s_inside_orderable_window(hhmm, expect):
    from datetime import datetime as _dt
    from src.schedulers.kr_scheduler import KRScheduler
    sched = KRScheduler.__new__(KRScheduler)
    now = _dt(2026, 9, 15, *hhmm)   # 화요일 거래일
    assert sched._portfolio_sync_interval(True, True, now) == expect
    assert sched._portfolio_sync_interval(True, False, now) == 30    # 전환 직후 1회는 항상 30
    assert sched._portfolio_sync_interval(False, True, now) == 30


def test_sync_interval_holiday_is_closed_all_day():
    from datetime import datetime as _dt
    from src.schedulers.kr_scheduler import KRScheduler
    sched = KRScheduler.__new__(KRScheduler)
    assert sched._portfolio_sync_interval(True, True, _dt(2026, 9, 13, 10, 0)) == 300   # 일요일
