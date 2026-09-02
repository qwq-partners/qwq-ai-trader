"""2026-09-03 운영 리뷰 수정 회귀 테스트

대상: KIS 원장 TR 호출 간격 / 모니터링 SQL 선행 주석 허용 / pykrx 업종분류 실패 백오프

실행: venv/bin/python -m pytest tests/test_review_fixes_2026_09.py -q
프로덕션 캐시·네트워크 무접촉.
"""

import asyncio
import collections
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


# ── KIS 원장 TR 간격 ─────────────────────────────────────────────────────────

def _bare_broker() -> KISBroker:
    b = object.__new__(KISBroker)
    b._rate_limit_lock = asyncio.Lock()
    b._api_call_times = collections.deque(maxlen=20)
    b._max_rps = 18
    b._ledger_last_call = 0.0
    return b


def test_ledger_tr_calls_are_spaced_one_second():
    b = _bare_broker()

    async def run():
        t0 = time.monotonic()
        await b._rate_limit("TTTC8434R")  # 잔고
        await b._rate_limit("TTTC8434R")  # 포지션 (동일 원장 TR 연속 호출)
        return time.monotonic() - t0

    assert asyncio.run(run()) >= 1.0


def test_non_ledger_tr_is_not_spaced():
    b = _bare_broker()

    async def run():
        t0 = time.monotonic()
        for _ in range(5):
            await b._rate_limit("FHKST01010100")  # 현재가
        return time.monotonic() - t0

    assert asyncio.run(run()) < 0.5


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

def test_pykrx_sector_map_backs_off_after_failure(monkeypatch):
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
