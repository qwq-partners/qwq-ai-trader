"""토스 프로바이더 Phase 1 인프라 테스트 (네트워크 없음, 전부 스텁)

검증 대상은 설계 §4.2(토큰 상호 무효화)·§4.3(리미터)·§4.4(반환 계약)·§6.6(서킷)이다.
캐시·락 경로는 전부 tmp_path 주입 — 운영 `~/.cache/ai_trader` 는 conftest 가 막는다.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime

import pytest

from src.data.providers.toss import rate_limit
from src.data.providers.toss.client import TossAPIError, TossCircuitOpen, TossClient, create_toss_client
from src.data.providers.toss.market_data import TossMarketData
from src.data.providers.toss.token import TossTokenCache


# ── 스텁 ────────────────────────────────────────────────────────────────────
class FakeResponse:
    def __init__(self, status: int, body, headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def json(self, content_type=None):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """aiohttp.ClientSession 중 TossClient 가 쓰는 표면만 흉내낸다"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, params=None, headers=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        if not self.responses:
            raise AssertionError(f"예상치 못한 추가 호출: {url} {params}")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def ok(result, headers=None):
    return FakeResponse(200, {"result": result}, headers)


def err(status, code, headers=None):
    return FakeResponse(
        status, {"error": {"code": code, "message": "", "requestId": "req-1"}}, headers
    )


def make_cache(tmp_path, fetcher, **kwargs):
    return TossTokenCache(
        "cid", "csec",
        cache_path=tmp_path / "toss_token.json",
        lock_path=tmp_path / "toss_token.lock",
        token_fetcher=fetcher,
        **kwargs,
    )


def counting_fetcher(delay: float = 0.0, expires_in: int = 86399, counter=None):
    counter = {"n": 0} if counter is None else counter

    async def _fetch(client_id, client_secret):
        counter["n"] += 1
        if delay:
            await asyncio.sleep(delay)
        return {"access_token": f"tok-{counter['n']}", "token_type": "Bearer",
                "expires_in": expires_in}

    _fetch.counter = counter
    return _fetch


def make_client(tmp_path, session, **kwargs):
    cache = make_cache(tmp_path, counting_fetcher())
    return TossClient("cid", "csec", token_cache=cache, session=session, **kwargs)


@pytest.fixture(autouse=True)
def _reset_limiter():
    rate_limit.reset()
    yield
    rate_limit.reset()


# ── 토큰 ────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_token_cache_path_injection_and_structure(tmp_path):
    """캐시 경로 주입 + 구조 + 0600"""
    cache = make_cache(tmp_path, counting_fetcher())
    token = await cache.get_token()

    path = tmp_path / "toss_token.json"
    assert token == "tok-1"
    assert path.exists()
    entry = json.loads(path.read_text())
    assert set(entry) == {"access_token", "issued_at", "expires_at"}
    assert entry["expires_at"] - entry["issued_at"] == pytest.approx(86399, abs=1)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"

    assert await cache.get_token() == "tok-1"   # 캐시 히트 — 재발급 없음
    assert cache.issue_count == 1


@pytest.mark.asyncio
async def test_concurrent_requests_issue_once(tmp_path):
    """동시 2요청 → 발급 1회 (in-process double-check)"""
    fetcher = counting_fetcher(delay=0.02)
    cache = make_cache(tmp_path, fetcher)

    tokens = await asyncio.gather(cache.get_token(), cache.get_token())

    assert tokens == ["tok-1", "tok-1"]
    assert fetcher.counter["n"] == 1


@pytest.mark.asyncio
async def test_two_processes_double_check_inside_file_lock(tmp_path):
    """캐시를 공유하는 별도 인스턴스(≈다른 프로세스) 2개 → flock 안 double-check 로 발급 1회

    flock 은 open file description 단위라 같은 프로세스의 서로 다른 fd 끼리도 실제로 경합한다.
    """
    counter = {"n": 0}
    a = make_cache(tmp_path, counting_fetcher(delay=0.05, counter=counter))
    b = make_cache(tmp_path, counting_fetcher(delay=0.05, counter=counter))

    tokens = await asyncio.gather(a.get_token(), b.get_token())

    assert counter["n"] == 1
    assert tokens[0] == tokens[1] == "tok-1"


@pytest.mark.asyncio
async def test_token_revoked_rereads_cache_instead_of_reissuing(tmp_path):
    """token-revoked: 캐시에 남이 발급한 새 토큰이 있으면 재발급하지 않고 그 토큰을 쓴다"""
    fetcher = counting_fetcher()
    cache = make_cache(tmp_path, fetcher)
    now = time.time()
    (tmp_path / "toss_token.json").write_text(json.dumps(
        {"access_token": "tok-new", "issued_at": now, "expires_at": now + 86399}
    ))

    token = await cache.refresh_after_error("tok-old", "token-revoked")

    assert token == "tok-new"
    assert fetcher.counter["n"] == 0          # 재발급 금지
    assert (tmp_path / "toss_token.json").exists()   # 캐시 삭제 금지


@pytest.mark.asyncio
async def test_token_revoked_reissues_once_when_cache_matches_failed_token(tmp_path):
    """캐시 토큰이 방금 실패한 토큰과 같을 때만 1회 발급"""
    fetcher = counting_fetcher()
    cache = make_cache(tmp_path, fetcher)
    now = time.time()
    (tmp_path / "toss_token.json").write_text(json.dumps(
        {"access_token": "tok-old", "issued_at": now, "expires_at": now + 86399}
    ))

    token = await cache.refresh_after_error("tok-old", "token-revoked")

    assert token == "tok-1"
    assert fetcher.counter["n"] == 1


@pytest.mark.asyncio
async def test_expired_token_reissues(tmp_path):
    """expired-token 은 재발급 경로 — 만료 시각이 남아 있어도 실패 토큰을 되돌려주지 않는다"""
    fetcher = counting_fetcher()
    cache = make_cache(tmp_path, fetcher)
    now = time.time()
    (tmp_path / "toss_token.json").write_text(json.dumps(
        {"access_token": "tok-old", "issued_at": now, "expires_at": now + 86399}
    ))

    token = await cache.refresh_after_error("tok-old", "expired-token")

    assert token == "tok-1"
    assert fetcher.counter["n"] == 1


@pytest.mark.asyncio
async def test_refresh_margin_triggers_preemptive_reissue(tmp_path):
    """만료 30분 이내면 선제 갱신"""
    fetcher = counting_fetcher()
    cache = make_cache(tmp_path, fetcher)
    now = time.time()
    (tmp_path / "toss_token.json").write_text(json.dumps(
        {"access_token": "tok-stale", "issued_at": now - 80000, "expires_at": now + 600}
    ))

    assert await cache.get_token() == "tok-1"
    assert fetcher.counter["n"] == 1


# ── 리미터 ──────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_rate_limit_spaces_calls_per_group():
    """버킷 소진 후에는 초당 상한 간격만큼 대기한다"""
    rate_limit.note_limit_header("MARKET_DATA", {"X-RateLimit-Limit": "4"})
    assert rate_limit.current_limit("MARKET_DATA") == 4

    start = time.monotonic()
    for _ in range(5):                        # 4개는 즉시, 5번째는 1/4초 대기
        await rate_limit.acquire("MARKET_DATA")
    elapsed = time.monotonic() - start

    assert elapsed >= 0.2
    assert rate_limit.current_limit("MARKET_DATA_CHART") == 20   # 다른 그룹은 독립


def test_rate_limit_header_lowers_only():
    """서버 헤더로 상한을 낮추기만 하고 올리지는 않는다"""
    rate_limit.note_limit_header("STOCK", {"X-RateLimit-Limit": "2"})
    assert rate_limit.current_limit("STOCK") == 2
    rate_limit.note_limit_header("STOCK", {"X-RateLimit-Limit": "50"})
    assert rate_limit.current_limit("STOCK") == 2
    rate_limit.note_limit_header("STOCK", {"X-RateLimit-Limit": "bogus"})
    assert rate_limit.current_limit("STOCK") == 2


def test_retry_delay_prefers_retry_after():
    assert rate_limit.parse_retry_after({"Retry-After": "0.7"}) == 0.7
    assert rate_limit.parse_retry_after({}) is None
    assert rate_limit.retry_delay(0, 0.7) == 0.7
    backoff = rate_limit.retry_delay(1)                       # 백오프 + jitter
    assert 1.0 <= backoff <= 1.0 + rate_limit.BACKOFF_JITTER_SEC


# ── 클라이언트 ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_429_honors_retry_after_then_succeeds(tmp_path, monkeypatch):
    slept = []

    async def fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    session = FakeSession([
        err(429, "rate-limit-exceeded", {"Retry-After": "0.7"}),
        ok([{"symbol": "005930", "lastPrice": "72000", "currency": "KRW"}]),
    ])
    client = make_client(tmp_path, session)

    body = await client.get("/api/v1/prices", {"symbols": "005930"}, group="MARKET_DATA")

    assert body["result"][0]["lastPrice"] == "72000"
    assert 0.7 in slept
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_429_gives_up_after_two_retries(tmp_path, monkeypatch):
    async def fake_sleep(delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    session = FakeSession([err(429, "rate-limit-exceeded")] * 3)
    client = make_client(tmp_path, session)

    with pytest.raises(TossAPIError):
        await client.get("/api/v1/prices", {"symbols": "005930"}, group="MARKET_DATA")
    assert len(session.calls) == 3          # 최초 1 + 재시도 2


@pytest.mark.asyncio
async def test_401_token_error_refreshes_and_retries_once(tmp_path):
    fetcher = counting_fetcher()
    cache = make_cache(tmp_path, fetcher)
    session = FakeSession([err(401, "expired-token"), ok([])])
    client = TossClient("cid", "csec", token_cache=cache, session=session)

    await client.get("/api/v1/prices", {"symbols": "005930"}, group="MARKET_DATA")

    assert fetcher.counter["n"] == 2        # 최초 발급 + 401 후 재발급
    assert session.calls[0]["headers"]["Authorization"] == "Bearer tok-1"
    assert session.calls[1]["headers"]["Authorization"] == "Bearer tok-2"


@pytest.mark.asyncio
async def test_401_non_token_code_does_not_reissue(tmp_path):
    """HTTP 401 만으로 분기하지 않는다 — 본문 code 가 토큰 에러가 아니면 재발급 없음"""
    fetcher = counting_fetcher()
    cache = make_cache(tmp_path, fetcher)
    session = FakeSession([err(401, "unauthorized-client")])
    client = TossClient("cid", "csec", token_cache=cache, session=session)

    with pytest.raises(TossAPIError) as exc:
        await client.get("/api/v1/prices", {"symbols": "005930"}, group="MARKET_DATA")

    assert exc.value.code == "unauthorized-client"
    assert fetcher.counter["n"] == 1


@pytest.mark.asyncio
async def test_circuit_breaker_opens_and_recovers(tmp_path):
    session = FakeSession([err(500, "internal-error"), err(500, "internal-error")])
    client = make_client(tmp_path, session, fail_threshold=2, circuit_open_sec=0.05)

    for _ in range(2):
        with pytest.raises(TossAPIError):
            await client.get("/api/v1/prices", {"symbols": "A"}, group="MARKET_DATA")
    assert client.circuit_state()["open"] is True

    with pytest.raises(TossCircuitOpen):                 # 호출 자체를 건너뛴다
        await client.get("/api/v1/prices", {"symbols": "A"}, group="MARKET_DATA")
    assert len(session.calls) == 2

    await asyncio.sleep(0.06)
    session.responses.append(ok([]))
    await client.get("/api/v1/prices", {"symbols": "A"}, group="MARKET_DATA")
    assert client.circuit_state() == {"open": False, "failures": 0, "remaining_sec": 0.0}


def test_factory_disabled_by_env():
    assert create_toss_client(env={"TOSS_API": "0", "TOSS_CLIENT_ID": "a",
                                   "TOSS_CLIENT_SECRET": "b"}) is None
    assert create_toss_client(env={"TOSS_API": "1"}) is None          # 자격증명 없음
    client = create_toss_client(env={"TOSS_CLIENT_ID": "a", "TOSS_CLIENT_SECRET": "b"})
    assert isinstance(client, TossClient)


# ── 시세 정규화 ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_prices_chunks_by_200_and_null_timestamp(tmp_path):
    symbols = [f"{i:06d}" for i in range(250)]
    page1 = [{"symbol": s, "timestamp": "2026-03-25T09:30:00.123+09:00",
              "lastPrice": "72000.5", "currency": "KRW"} for s in symbols[:200]]
    page2 = [{"symbol": s, "timestamp": None, "lastPrice": "1000", "currency": "KRW"}
             for s in symbols[200:]]
    session = FakeSession([ok(page1), ok(page2)])
    md = TossMarketData(make_client(tmp_path, session))

    prices = await md.get_prices(symbols)

    assert len(session.calls) == 2
    assert len(session.calls[0]["params"]["symbols"].split(",")) == 200
    assert len(session.calls[1]["params"]["symbols"].split(",")) == 50
    assert prices["000000"]["price"] == 72000.5
    assert prices["000000"]["as_of"].isoformat() == "2026-03-25T09:30:00.123000+09:00"
    assert prices["000000"]["source"] == "toss"
    assert prices["000200"]["as_of"] is None          # timestamp null → 지어내지 않는다


@pytest.mark.asyncio
async def test_candles_normalized_to_kis_contract(tmp_path):
    """최신순 → 오래된 순, YYYYMMDD(KST), 문자열 → float, value=None"""
    candles = [
        {"timestamp": "2026-03-25T00:00:00+09:00", "openPrice": "71600", "highPrice": "72300",
         "lowPrice": "71500", "closePrice": "72000", "volume": "3521000", "currency": "KRW"},
        {"timestamp": "2026-03-24T00:00:00+09:00", "openPrice": "71000", "highPrice": "71800",
         "lowPrice": "70900", "closePrice": "71600", "volume": "2100000", "currency": "KRW"},
        {"timestamp": "2026-03-23T00:00:00+09:00", "openPrice": "70500", "highPrice": "71200",
         "lowPrice": "70100", "closePrice": "71000", "volume": "1800000", "currency": "KRW"},
    ]
    session = FakeSession([ok({"candles": candles, "nextBefore": None})])
    md = TossMarketData(make_client(tmp_path, session))

    rows = await md.get_candles("005930", interval="1d", count=100)

    assert [r["date"] for r in rows] == ["20260323", "20260324", "20260325"]   # 정렬 역전
    assert rows[-1] == {
        "date": "20260325", "open": 71600.0, "high": 72300.0, "low": 71500.0,
        "close": 72000.0, "volume": 3521000, "value": None,
        "timestamp": rows[-1]["timestamp"],
    }
    assert all(isinstance(r["close"], float) for r in rows)
    assert all(isinstance(r["volume"], int) for r in rows)
    assert all(r["value"] is None for r in rows)       # 거래대금 미제공 — 0 금지
    assert isinstance(rows[-1]["timestamp"], datetime)
    assert session.calls[0]["params"] == {
        "symbol": "005930", "interval": "1d", "count": "100", "adjusted": "true",
    }


@pytest.mark.asyncio
async def test_candles_paginate_and_dedupe_boundary(tmp_path):
    """200봉 초과는 before 페이징 — before 가 inclusive라 겹치는 경계 봉을 중복 제거"""
    def bar(day):
        return {"timestamp": f"2026-03-{day:02d}T00:00:00+09:00", "openPrice": "1",
                "highPrice": "1", "lowPrice": "1", "closePrice": str(day),
                "volume": "10", "currency": "KRW"}

    page1 = [bar(d) for d in (5, 4, 3)]
    page2 = [bar(d) for d in (3, 2, 1)]          # 경계 봉 3일 중복
    session = FakeSession([
        ok({"candles": page1, "nextBefore": "2026-03-03T00:00:00+09:00"}),
        ok({"candles": page2, "nextBefore": None}),
    ])
    md = TossMarketData(make_client(tmp_path, session))

    rows = await md.get_candles("005930", interval="1d", count=6)

    assert [r["date"] for r in rows] == [
        "20260301", "20260302", "20260303", "20260304", "20260305"
    ]
    assert session.calls[1]["params"]["before"] == "2026-03-03T00:00:00+09:00"


@pytest.mark.asyncio
async def test_orderbook_price_limits_calendar_and_index(tmp_path):
    session = FakeSession([
        ok({"timestamp": "2026-03-25T09:30:00+09:00", "currency": "KRW",
            "asks": [{"price": "72100", "volume": "8500"}],
            "bids": [{"price": "72000", "volume": "1200"}]}),
        ok({"timestamp": "2026-03-25T09:30:00+09:00", "currency": "KRW",
            "upperLimitPrice": "93000", "lowerLimitPrice": None}),
        ok({"today": {"date": "2026-03-25", "integrated": None}}),
        ok([{"symbol": "KOSPI", "timestamp": None, "lastPrice": "2812.45"}]),
    ])
    md = TossMarketData(make_client(tmp_path, session))

    book = await md.get_orderbook("005930")
    limits = await md.get_price_limits("005930")
    calendar = await md.get_market_calendar_kr()
    index = await md.get_index_prices(["KOSPI"])

    assert book["asks"] == [{"price": 72100.0, "volume": 8500}]
    assert book["bids"][0]["price"] == 72000.0
    assert book["source"] == "toss"
    assert limits["upper_limit"] == 93000.0
    assert limits["lower_limit"] is None          # 미제공 → None (0 금지)
    assert calendar["calendar"]["today"]["date"] == "2026-03-25"
    assert index["KOSPI"]["price"] == 2812.45
    assert index["KOSPI"]["as_of"] is None        # 지수 timestamp null 실측 (§3.3-2)
    assert session.calls[3]["url"].endswith("/api/v1/market-indicators/prices")


# ── 검토 반영 (2026-09-16) — 되돌리면 실패하는 회귀 가드 ─────────────────────────

@pytest.mark.asyncio
async def test_half_open_allows_only_one_trial_call(tmp_path):
    """반열림 중 시험 호출은 1건만 통과, 동시 호출은 거부된다."""
    session = FakeSession([err(500, "internal-error"), err(500, "internal-error")])
    client = make_client(tmp_path, session, fail_threshold=2, circuit_open_sec=0.05)
    for _ in range(2):
        with pytest.raises(TossAPIError):
            await client.get("/api/v1/prices", {"symbols": "A"}, group="MARKET_DATA")
    await asyncio.sleep(0.06)
    session.responses.append(err(500, "internal-error"))   # 시험 호출도 실패시킨다
    with pytest.raises(TossAPIError):
        await client.get("/api/v1/prices", {"symbols": "A"}, group="MARKET_DATA")
    # 실패 → 다시 개방. 개방 중에는 호출 자체가 생략된다(세션 호출 수 불변)
    n = len(session.calls)
    with pytest.raises(TossCircuitOpen):
        await client.get("/api/v1/prices", {"symbols": "A"}, group="MARKET_DATA")
    assert len(session.calls) == n


@pytest.mark.asyncio
async def test_non_json_5xx_body_is_counted_as_failure(tmp_path):
    """게이트웨이 HTML 5xx(비-JSON)도 에러 매핑·서킷 계수를 탄다."""
    class _HtmlResp(FakeResponse):
        async def json(self, content_type=None):
            raise ValueError("not json")
    resp = _HtmlResp(502, {}, None)
    client = make_client(tmp_path, FakeSession([resp]), fail_threshold=1, circuit_open_sec=1)
    with pytest.raises(TossAPIError) as ei:
        await client.get("/api/v1/prices", {"symbols": "A"}, group="MARKET_DATA")
    assert ei.value.status == 502
    assert client.circuit_state()["open"] is True


@pytest.mark.asyncio
async def test_token_fetch_transport_error_is_wrapped(tmp_path):
    """토큰 발급 중 aiohttp 예외가 TossTokenError → TossAPIError 로 감싸이고 서킷에 계수된다."""
    async def boom(cid, csec):
        raise ConnectionError("dns")
    cache = make_cache(tmp_path, boom)
    client = TossClient("cid", "csec", token_cache=cache, session=FakeSession([]),
                        fail_threshold=1, circuit_open_sec=1)
    with pytest.raises(TossAPIError) as ei:
        await client.get("/api/v1/prices", {"symbols": "A"}, group="MARKET_DATA")
    assert "csec" not in str(ei.value) and "cid" not in str(ei.value)
    assert client.circuit_state()["open"] is True


@pytest.mark.asyncio
async def test_short_expires_in_does_not_reissue_every_call(tmp_path):
    """expires_in 이 갱신 마진 이하여도 캐시 히트가 보장돼 호출마다 재발급되지 않는다."""
    calls = []
    async def fetch(cid, csec):
        calls.append(1)
        return {"access_token": f"t{len(calls)}", "expires_in": 600}   # 10분 < 마진 30분
    cache = make_cache(tmp_path, fetch)
    a = await cache.get_token(); b = await cache.get_token(); c = await cache.get_token()
    assert a == b == c and len(calls) == 1


@pytest.mark.asyncio
async def test_prices_chunk_failure_keeps_other_chunks(tmp_path):
    from src.data.providers.toss.market_data import TossMarketData
    symbols = [f"{i:06d}" for i in range(250)]
    session = FakeSession([ok([{"symbol": "000000", "timestamp": None, "lastPrice": "10", "currency": "KRW"}]),
                           err(500, "internal-error")])
    md = TossMarketData(make_client(tmp_path, session, fail_threshold=10))
    out = await md.get_prices(symbols)
    assert "000000" in out and out["000000"]["data_status"] == "partial"   # timestamp null → partial
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_candle_paging_stops_when_before_does_not_advance(tmp_path):
    from src.data.providers.toss.market_data import TossMarketData
    row = {"timestamp": "2026-09-15T00:00:00+09:00", "openPrice": "1", "highPrice": "1", "lowPrice": "1",
           "closePrice": "1", "volume": "1", "currency": "KRW"}
    same = {"candles": [row], "nextBefore": "2026-09-15T00:00:00+09:00"}
    session = FakeSession([ok(same), ok(same), ok(same), ok(same)])
    md = TossMarketData(make_client(tmp_path, session))
    await md.get_candles("A", count=200)
    assert len(session.calls) == 2, "before 가 전진하지 않으면 두 번째 페이지에서 멈춰야 한다"


@pytest.mark.asyncio
async def test_orderbook_levels_are_sorted_best_first(tmp_path):
    from src.data.providers.toss.market_data import TossMarketData
    body = {"timestamp": None, "currency": "KRW",
            "asks": [{"price": "102", "volume": "1"}, {"price": "100", "volume": "1"}, {"price": "101", "volume": "1"}],
            "bids": [{"price": "98", "volume": "1"}, {"price": "99", "volume": "1"}]}
    md = TossMarketData(make_client(tmp_path, FakeSession([ok(body)])))
    ob = await md.get_orderbook("A")
    assert [a["price"] for a in ob["asks"]] == [100.0, 101.0, 102.0]
    assert [b["price"] for b in ob["bids"]] == [99.0, 98.0]
