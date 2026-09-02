"""2026-09-03 운영 리뷰 수정 회귀 테스트

대상: KIS 원장 TR 호출 간격 / 모니터링 SQL 선행 주석 허용 / pykrx 업종분류 실패 백오프

실행: venv/bin/python -m pytest tests/test_review_fixes_2026_09.py -q
프로덕션 캐시·네트워크 무접촉.
"""

import asyncio
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analytics.monitoring_runner import MonitoringRunner  # noqa: E402
from src.data.providers import sector_momentum  # noqa: E402
from src.execution.broker.kis_kr import KISBroker  # noqa: E402
from src.utils import kis_rate_limit  # noqa: E402


# ── KIS 공용 리미터: 원장 TR 간격 ─────────────────────────────────────────────

def _bare_broker() -> KISBroker:
    kis_rate_limit.reset()
    return object.__new__(KISBroker)


def test_ledger_tr_calls_are_spaced_one_second():
    b = _bare_broker()

    async def run():
        t0 = time.monotonic()
        await b._rate_limit("TTTC8434R")  # 잔고
        await b._rate_limit("TTTC8908R")  # 매수가능조회 (다른 원장 TR도 같은 간격)
        return time.monotonic() - t0

    assert asyncio.run(run()) >= 1.0


def test_non_ledger_tr_is_not_spaced():
    b = _bare_broker()

    async def run():
        for _ in range(5):
            await b._rate_limit("FHKST01010100")  # 현재가
        await kis_rate_limit.acquire()             # 시세 모듈 경로(tr_id 없음)도 같은 윈도우

    asyncio.run(run())
    assert len(kis_rate_limit._calls) == 6 and kis_rate_limit._state["ledger_last"] == 0.0


def test_shared_window_blocks_burst_over_max_rps():
    kis_rate_limit.reset()

    async def run():
        t0 = time.monotonic()
        for _ in range(kis_rate_limit.MAX_RPS + 1):
            await kis_rate_limit.acquire()
        return time.monotonic() - t0

    assert asyncio.run(run()) >= 0.9  # 19번째 호출은 1초 윈도우가 지나야 통과


# ── 주문 POST 재전송 금지 / 토큰 회전 채택 ─────────────────────────────────────

class _FakeResp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def json(self):
        return self._body


class _FakeSession:
    """post() 호출 횟수를 세고, 지정한 예외/응답을 돌려주는 최소 세션"""
    closed = False

    def __init__(self, outcome):
        self.calls, self._outcome = 0, outcome

    def post(self, *a, **k):
        self.calls += 1
        outcome = self._outcome

        class _CM:
            async def __aenter__(self_inner):
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome

            async def __aexit__(self_inner, *exc):
                return False

        return _CM()


def _post_broker(session, monkeypatch):
    from types import SimpleNamespace
    from src.execution.broker import kis_kr

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(kis_kr.asyncio, "sleep", _no_sleep)
    b = _bare_broker()
    b._session = session
    b._token = "tok"
    b._token_mgr = SimpleNamespace(_access_token="tok", _is_token_valid=lambda: True,
                                   invalidate=lambda: None)
    b.config = SimpleNamespace(app_key="k", app_secret="s")
    return b


def test_order_post_is_not_resent_on_timeout(monkeypatch):
    sess = _FakeSession(asyncio.TimeoutError())
    b = _post_broker(sess, monkeypatch)
    out = asyncio.run(b._api_post("u", "TTTC0802U", {}, retry=False))
    assert out["rt_cd"] == "-1" and sess.calls == 1
    # 비주문(취소 등) 기본 경로는 3회 시도 유지
    sess2 = _FakeSession(asyncio.TimeoutError())
    b2 = _post_broker(sess2, monkeypatch)
    asyncio.run(b2._api_post("u", "TTTC0803U", {}))
    assert sess2.calls == 3


def test_order_post_is_not_resent_on_5xx(monkeypatch):
    body = {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "원장 초과"}
    sess = _FakeSession(_FakeResp(500, body))
    b = _post_broker(sess, monkeypatch)
    out = asyncio.run(b._api_post("u", "TTTC0802U", {}, retry=False))
    assert out == body and sess.calls == 1


def test_recover_token_adopts_rotated_token_without_invalidate():
    from types import SimpleNamespace
    b = _bare_broker()
    b._token = "old"
    calls = {"inv": 0}

    def _inv():
        calls["inv"] += 1

    b._token_mgr = SimpleNamespace(_access_token="new", _is_token_valid=lambda: True, invalidate=_inv)
    asyncio.run(b._recover_token())
    assert b._token == "new" and calls["inv"] == 0
    b.config = SimpleNamespace(app_key="k", app_secret="s")
    b._token_mgr._access_token = "newer"
    assert b._get_headers("X")["authorization"] == "Bearer newer"


# ── 모니터링 SQL read-only 검사 ─────────────────────────────────────────────

@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "  with x as (select 1) select * from x",
    "-- 캐시 히트율\nSELECT count(*) FROM t",
    "-- 주석 1\n-- 주석 2\n  WITH a AS (SELECT 1) SELECT * FROM a",
])
def test_read_only_sql_allows_leading_comments(sql):
    assert MonitoringRunner._is_read_only_sql(sql)


@pytest.mark.parametrize("sql", [
    "DELETE FROM t",
    "-- 주석\nUPDATE t SET a = 1",
    "-- SELECT 처럼 보이는 주석\nDROP TABLE t",
])
def test_read_only_sql_blocks_writes(sql):
    assert not MonitoringRunner._is_read_only_sql(sql)


# ── pykrx 업종분류 실패 백오프 ───────────────────────────────────────────────

def test_pykrx_sector_map_backs_off_after_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(sector_momentum, "_CACHE_DIR", tmp_path)  # 프로덕션 캐시 격리
    prov = sector_momentum.SectorMomentumProvider()
    calls = {"n": 0}

    class _FakeStock:
        @staticmethod
        def get_market_sector_classifications(*a, **k):
            calls["n"] += 1
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    import types
    fake_pykrx = types.ModuleType("pykrx")
    fake_pykrx.stock = _FakeStock
    monkeypatch.setitem(sys.modules, "pykrx", fake_pykrx)
    monkeypatch.setitem(sys.modules, "pykrx.stock", _FakeStock)

    assert asyncio.run(prov._fetch_pykrx_sector_map()) == {}
    assert calls["n"] == 2  # KOSPI + KOSDAQ 1회씩
    assert prov._pykrx_fail_until > time.monotonic()

    # 백오프 중에는 pykrx 를 다시 호출하지 않는다
    assert asyncio.run(prov._fetch_pykrx_sector_map()) == {}
    assert calls["n"] == 2
    # 점진 백오프: 첫 실패 15분 → 다음 실패 30분 (상한 6시간)
    assert prov._pykrx_backoff_sec == 2 * sector_momentum.SectorMomentumProvider._PYKRX_BACKOFF_MIN_SEC


# ── 전략 예산 캡: pending BUY 예약금 귀속 ───────────────────────────────────────

def test_pending_strategy_notional_sums_reserved_cash():
    from decimal import Decimal
    from src.core.engine import RiskManager
    rm = object.__new__(RiskManager)
    rm._reserved_by_order = {"A": Decimal("1000"), "B": Decimal("2500"), "C": Decimal("7")}
    rm._pending_strategy = {"A": "sepa_trend", "B": "sepa_trend", "C": "gap_and_go"}
    assert rm._pending_strategy_notional("sepa_trend") == Decimal("3500")
    assert rm._pending_strategy_notional("core_holding") == Decimal("0")
