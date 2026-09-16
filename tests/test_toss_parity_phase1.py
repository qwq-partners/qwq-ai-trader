"""토스 대조 기록 Phase 1 테스트 (네트워크 없음, 전부 스텁)

검증 대상: docs/superpowers/plans/2026-09-15-toss-securities-fallback.md §5 Phase 1 인수 조건.
ledger_dir는 전부 tmp_path 주입 — conftest가 ~/.cache/ai_trader 를 막는다.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analytics import toss_parity
from src.data.providers.toss.client import TossAPIError
from src.schedulers.kr_scheduler import KRScheduler

KST = ZoneInfo("Asia/Seoul")


# ── 스텁 TossMarketData (duck-typed — 실제 TossClient/HTTP 없이 모듈 자체 동작만 검증) ──
class StubTossMarketData:
    def __init__(self, *, prices=None, candles=None, calendar=None, raise_on=()):
        self._prices = prices or {}
        self._candles = candles or []
        self._calendar = calendar or {}
        self._raise_on = set(raise_on)
        self.calls = []

    async def get_prices(self, symbols):
        self.calls.append(("get_prices", tuple(symbols)))
        if "get_prices" in self._raise_on:
            raise TossAPIError("토스 현재가 실패", status=500)
        return self._prices

    async def get_candles(self, symbol, interval="1d", count=100, adjusted=True):
        self.calls.append(("get_candles", symbol))
        if "get_candles" in self._raise_on:
            raise TossAPIError("토스 캔들 실패", status=500)
        return self._candles

    async def get_market_calendar_kr(self, date=None):
        self.calls.append(("get_market_calendar_kr", date))
        if "get_market_calendar_kr" in self._raise_on:
            raise TossAPIError("토스 캘린더 실패", status=500)
        return {"calendar": self._calendar, "source": "toss"}


def _kis_fn(prices: dict):
    async def fn(symbol):
        return prices.get(symbol)
    return fn


# ── 현재가 대조 ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_price_parity_diff_bp_계산_정확성(tmp_path):
    now = datetime(2026, 9, 16, 10, 0, tzinfo=KST)  # 정규장
    toss_md = StubTossMarketData(
        prices={"005930": {"price": 100500.0, "as_of": now, "currency": "KRW", "source": "toss"}}
    )
    kis_fn = _kis_fn({"005930": {"price": 100000.0, "as_of": now}})

    stats = await toss_parity.record_price_parity(
        ["005930"], kis_fn, toss_md, now=now, ledger_dir=tmp_path
    )
    assert stats == {"compared": 1, "toss_fail": 0}

    rows = _read_jsonl(tmp_path / "price_20260916.jsonl")
    assert len(rows) == 1
    row = rows[0]
    # (100500-100000)/100000 * 10000 = 50.0bp
    assert row["diff_bp"] == pytest.approx(50.0)
    assert row["kis_price"] == 100000.0
    assert row["toss_price"] == 100500.0
    assert row["toss_ok"] is True


@pytest.mark.asyncio
async def test_price_parity_세션_태깅(tmp_path):
    cases = [
        (datetime(2026, 9, 16, 8, 20, tzinfo=KST), "pre_market"),
        (datetime(2026, 9, 16, 10, 0, tzinfo=KST), "regular"),
        (datetime(2026, 9, 16, 16, 0, tzinfo=KST), "next"),
        (datetime(2026, 9, 16, 21, 0, tzinfo=KST), "closed"),
    ]
    for now, expected_session in cases:
        toss_md = StubTossMarketData(prices={})
        await toss_parity.record_price_parity(
            ["005930"], _kis_fn({}), toss_md, now=now, ledger_dir=tmp_path
        )
    rows = _read_jsonl(tmp_path / "price_20260916.jsonl")
    assert [r["session"] for r in rows] == [c[1] for c in cases]


@pytest.mark.asyncio
async def test_price_parity_토스_실패시_기록만_예외전파없음(tmp_path):
    now = datetime(2026, 9, 16, 10, 0, tzinfo=KST)
    toss_md = StubTossMarketData(raise_on={"get_prices"})
    kis_fn = _kis_fn({"005930": {"price": 100000.0, "as_of": now}})

    stats = await toss_parity.record_price_parity(
        ["005930"], kis_fn, toss_md, now=now, ledger_dir=tmp_path
    )
    assert stats == {"compared": 1, "toss_fail": 1}
    rows = _read_jsonl(tmp_path / "price_20260916.jsonl")
    assert rows[0]["toss_ok"] is False
    assert rows[0]["toss_price"] is None
    assert rows[0]["error"]


@pytest.mark.asyncio
async def test_price_parity_kis_없을때_diff_none이고_전파없음(tmp_path):
    now = datetime(2026, 9, 16, 10, 0, tzinfo=KST)
    toss_md = StubTossMarketData(prices={"005930": {"price": 100500.0, "as_of": now}})

    async def _raising_kis(symbol):
        raise RuntimeError("kis 조회 실패")

    stats = await toss_parity.record_price_parity(
        ["005930"], _raising_kis, toss_md, now=now, ledger_dir=tmp_path
    )
    assert stats["compared"] == 1
    rows = _read_jsonl(tmp_path / "price_20260916.jsonl")
    assert rows[0]["kis_price"] is None
    assert rows[0]["diff_bp"] is None


@pytest.mark.asyncio
async def test_price_parity_빈_심볼목록(tmp_path):
    now = datetime(2026, 9, 16, 10, 0, tzinfo=KST)
    stats = await toss_parity.record_price_parity(
        [], _kis_fn({}), StubTossMarketData(), now=now, ledger_dir=tmp_path
    )
    assert stats == {"compared": 0, "toss_fail": 0}
    assert not (tmp_path / "price_20260916.jsonl").exists()


# ── 캔들 대조 ──────────────────────────────────────────────────────────────────
def _candle_row(date_str, close, open_=None, high=None, low=None, volume=1000):
    return {
        "date": date_str,
        "open": open_ if open_ is not None else close,
        "high": high if high is not None else close,
        "low": low if low is not None else close,
        "close": close,
        "volume": volume,
        "value": None,
    }


@pytest.mark.asyncio
async def test_candle_parity_정렬_불일치_탐지(tmp_path):
    # KIS 캔들을 일부러 역순(최신→과거)으로 넣는다 — 정렬 회귀 재현
    kis_candles_reversed = [
        _candle_row("20260916", 105.0),
        _candle_row("20260915", 100.0),
    ]
    toss_md = StubTossMarketData(
        candles=[_candle_row("20260915", 100.0), _candle_row("20260916", 105.0)]
    )
    await toss_parity.record_candle_parity(
        "005930", kis_candles_reversed, toss_md, days=2, ledger_dir=tmp_path
    )
    rows = _read_jsonl(tmp_path / f"candle_{datetime.now():%Y%m%d}.jsonl")
    assert rows[0]["kis_sorted_asc"] is False
    assert rows[0]["toss_sorted_asc"] is True


@pytest.mark.asyncio
async def test_candle_parity_날짜_종가_일치율(tmp_path):
    kis_candles = [_candle_row("20260915", 100.0), _candle_row("20260916", 105.0)]
    toss_candles = [_candle_row("20260915", 100.0), _candle_row("20260916", 106.0)]  # 하루 불일치
    toss_md = StubTossMarketData(candles=toss_candles)
    await toss_parity.record_candle_parity("005930", kis_candles, toss_md, days=2, ledger_dir=tmp_path)
    rows = _read_jsonl(tmp_path / f"candle_{datetime.now():%Y%m%d}.jsonl")
    row = rows[0]
    assert row["last_date_match"] is True
    assert row["kis_last_date"] == "20260916"
    assert row["common_dates"] == 2
    assert row["close_mismatch_count"] == 1
    assert row["match_rate"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_candle_parity_토스_실패시_기록만(tmp_path):
    toss_md = StubTossMarketData(raise_on={"get_candles"})
    await toss_parity.record_candle_parity(
        "005930", [_candle_row("20260915", 100.0)], toss_md, ledger_dir=tmp_path
    )
    rows = _read_jsonl(tmp_path / f"candle_{datetime.now():%Y%m%d}.jsonl")
    assert rows[0]["toss_ok"] is False
    assert rows[0]["error"]


# ── 캘린더 대조 ────────────────────────────────────────────────────────────────
def _matching_calendar():
    return {
        "today": {
            "date": "2026-09-16",
            "integrated": {
                "preMarket": {
                    "startTime": "2026-09-16T08:00:00+09:00",
                    "singlePriceAuctionStartTime": "2026-09-16T08:50:00+09:00",
                    "endTime": "2026-09-16T09:00:00+09:00",
                },
                "regularMarket": {
                    "startTime": "2026-09-16T09:00:00+09:00",
                    "singlePriceAuctionStartTime": "2026-09-16T15:20:00+09:00",
                    "endTime": "2026-09-16T15:30:00+09:00",
                },
                "afterMarket": {
                    "startTime": "2026-09-16T15:30:00+09:00",
                    "singlePriceAuctionEndTime": "2026-09-16T15:40:00+09:00",
                    "endTime": "2026-09-16T20:00:00+09:00",
                },
            },
        }
    }


@pytest.mark.asyncio
async def test_calendar_parity_일치시_불일치없음(tmp_path, monkeypatch):
    now = datetime(2026, 9, 16, 10, 0, tzinfo=KST)  # 수요일, 공휴일 아님
    toss_md = StubTossMarketData(calendar=_matching_calendar())
    await toss_parity.record_calendar_parity(toss_md, now=now, ledger_dir=tmp_path)
    rows = _read_jsonl(tmp_path / "calendar_20260916.jsonl")
    assert rows[0]["mismatches"] == []
    assert rows[0]["our_holiday"] is False
    assert rows[0]["toss_holiday"] is False


@pytest.mark.asyncio
async def test_calendar_parity_경계_불일치_경고(tmp_path, caplog):
    now = datetime(2026, 9, 16, 10, 0, tzinfo=KST)
    cal = _matching_calendar()
    # 정규장 시작을 09:05로 바꿔 하드코딩(09:00)과 불일치시킨다
    cal["today"]["integrated"]["regularMarket"]["startTime"] = "2026-09-16T09:05:00+09:00"
    toss_md = StubTossMarketData(calendar=cal)
    await toss_parity.record_calendar_parity(toss_md, now=now, ledger_dir=tmp_path)
    rows = _read_jsonl(tmp_path / "calendar_20260916.jsonl")
    assert "regular_start" in rows[0]["mismatches"]


@pytest.mark.asyncio
async def test_calendar_parity_휴장일_불일치(tmp_path):
    # 우리 코드가 휴장으로 보는 날(주말)인데 토스는 영업일로 응답 → holiday_mismatch
    now = datetime(2026, 9, 19, 10, 0, tzinfo=KST)  # 토요일
    toss_md = StubTossMarketData(calendar=_matching_calendar())
    await toss_parity.record_calendar_parity(toss_md, now=now, ledger_dir=tmp_path)
    rows = _read_jsonl(tmp_path / "calendar_20260919.jsonl")
    assert "holiday_mismatch" in rows[0]["mismatches"]


@pytest.mark.asyncio
async def test_calendar_parity_토스_실패시_기록만_세션판정_불변(tmp_path):
    now = datetime(2026, 9, 16, 10, 0, tzinfo=KST)
    toss_md = StubTossMarketData(raise_on={"get_market_calendar_kr"})
    # 세션 판정 API가 여전히 하드코딩값을 반환하는지 (모듈이 바꾸지 않았는지) 확인
    from src.utils.session import KRSession, MarketSession
    before = KRSession().get_session(now)
    await toss_parity.record_calendar_parity(toss_md, now=now, ledger_dir=tmp_path)
    after = KRSession().get_session(now)
    assert before == after == MarketSession.REGULAR
    rows = _read_jsonl(tmp_path / "calendar_20260916.jsonl")
    assert rows[0]["toss_ok"] is False


# ── summarize ──────────────────────────────────────────────────────────────
def test_summarize_p50_p95_실패율_세션분포(tmp_path):
    day = "2026-09-16"
    path = tmp_path / f"price_{day.replace('-', '')}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    diffs_bp = [10, 20, 30, 40, 200]  # 200bp는 0.5%(50bp) 초과
    with path.open("w", encoding="utf-8") as f:
        for i, d in enumerate(diffs_bp):
            f.write(json.dumps({
                "diff_bp": float(d), "toss_ok": True, "session": "regular" if i < 4 else "pre_market",
            }) + "\n")
        f.write(json.dumps({"diff_bp": None, "toss_ok": False, "session": "regular"}) + "\n")

    summary = toss_parity.summarize(tmp_path, day)
    assert summary["total"] == 6
    assert summary["failed"] == 1
    assert summary["failure_rate"] == pytest.approx(1 / 6, abs=1e-4)
    assert summary["diff_bp_p50"] == 30.0
    assert summary["over_warn_bp_rate"] == pytest.approx(1 / 5)  # 5건 중 200bp 1건
    assert summary["by_session"] == {"regular": 5, "pre_market": 1}


def test_summarize_원장_없으면_빈_집계(tmp_path):
    summary = toss_parity.summarize(tmp_path, "2026-01-01")
    assert summary["total"] == 0
    assert summary["failure_rate"] is None
    assert summary["diff_bp_p50"] is None
    assert summary["by_session"] == {}


# ── 스케줄러 태스크 게이팅 (TOSS_API=0 이면 루프 진입 없이 즉시 반환) ──────────────
@pytest.mark.asyncio
async def test_run_toss_parity_scheduler_TOSS_API_0이면_즉시_반환(monkeypatch):
    monkeypatch.setenv("TOSS_API", "0")
    sched = object.__new__(KRScheduler)  # __init__ 생략 — 이 경로는 self.bot을 건드리지 않는다
    # bot 미설정 상태에서도 예외 없이 즉시 반환해야 한다 (bot 접근은 게이트 통과 후에만 일어남)
    await sched.run_toss_parity_scheduler()


@pytest.mark.asyncio
async def test_run_toss_parity_scheduler_자격증명_없으면_즉시_반환(monkeypatch):
    monkeypatch.setenv("TOSS_API", "1")
    monkeypatch.delenv("TOSS_CLIENT_ID", raising=False)
    monkeypatch.delenv("TOSS_CLIENT_SECRET", raising=False)
    sched = object.__new__(KRScheduler)
    await sched.run_toss_parity_scheduler()


# ── 유틸 ─────────────────────────────────────────────────────────────────────
def _read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ── 검토 반영 (2026-09-16) ───────────────────────────────────────────────────

def test_summarize_reports_regular_session_separately(tmp_path):
    """Phase 3 게이트는 정규장 수치로만 판정한다 — 세션 섞인 p95 와 별도로 regular 키가 있어야 한다."""
    import json
    from src.analytics import toss_parity as tp
    day = "2026-09-16"
    p = tp._ledger_path("price", day, tmp_path); p.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"toss_ok": True, "diff_bp": 5.0, "session": "regular"},
            {"toss_ok": True, "diff_bp": 90.0, "session": "next"},
            {"toss_ok": True, "diff_bp": 7.0, "session": "regular"}]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    s = tp.summarize(tmp_path, day)
    assert s["regular"]["n"] == 2 and s["regular"]["p95"] <= 7.0
    assert s["diff_by_session"]["next"]["n"] == 1
    assert s["diff_bp_p95"] >= 7.0   # 혼합 수치는 참고값


def test_scheduler_marks_heartbeat_disabled_when_toss_off(monkeypatch):
    """TOSS_API=0 이면 태스크를 안 만들 뿐 아니라 하트비트를 비활성 표기해 정체 오탐을 막는다."""
    from src.schedulers import kr_scheduler
    from src.utils import loop_heartbeat as hb
    src = __import__("inspect").getsource(kr_scheduler.KRScheduler.create_tasks)
    assert 'set_enabled("kr_toss_parity", False' in src
    src2 = __import__("inspect").getsource(kr_scheduler.KRScheduler.run_toss_parity_scheduler)
    assert 'set_enabled("kr_toss_parity", False' in src2
