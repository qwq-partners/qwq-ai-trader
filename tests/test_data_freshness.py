"""데이터 신선도 유틸 테스트 (계획서 T9 요청 4 — C 담당, 2026-09-14)

`src/utils/data_freshness.py`의 DataPoint/is_fresh/freshness_label/missing 단위 테스트.
네트워크·DB·운영 캐시 접근 없음.

실행: venv/bin/python -m pytest tests/test_data_freshness.py -q
"""

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.data_freshness import (  # noqa: E402
    DataPoint,
    is_fresh,
    freshness_label,
    missing,
    CONFIDENCE_CAP_INSUFFICIENT,
    CONFIDENCE_CAP_PARTIAL,
)

NOW = datetime(2026, 9, 14, 12, 0, 0)


def test_fresh_within_ttl():
    dp = DataPoint(value=1.0, as_of=NOW - timedelta(minutes=10), source="x", ttl_seconds=3600)
    assert is_fresh(dp, NOW) is True


def test_stale_beyond_ttl():
    dp = DataPoint(value=1.0, as_of=NOW - timedelta(hours=4), source="x", ttl_seconds=3600)
    assert is_fresh(dp, NOW) is False


def test_no_ttl_means_no_expiry_but_needs_as_of():
    dp = DataPoint(value=1.0, as_of=NOW - timedelta(days=30), source="x", ttl_seconds=None)
    assert is_fresh(dp, NOW) is True


def test_missing_is_never_fresh():
    dp = missing(source="kospi", reason="조회 실패")
    assert dp.is_missing is True
    assert dp.as_of is None
    assert is_fresh(dp, NOW) is False


def test_as_of_none_without_missing_is_never_fresh():
    # 값은 있는데 기준시각이 없는 경우도 신선도 판정 불가 취급
    dp = DataPoint(value=1.0, as_of=None, source="x", ttl_seconds=3600)
    assert is_fresh(dp, NOW) is False


def test_freshness_label_fresh_has_no_expired_marker():
    dp = DataPoint(value=1.0, as_of=NOW - timedelta(hours=1), source="x", ttl_seconds=3600 * 6)
    label = freshness_label(dp, NOW)
    assert "as_of 11:00" in label
    assert "1h" in label or "1.0h" in label
    assert "만료" not in label


def test_freshness_label_stale_has_expired_marker():
    dp = DataPoint(value=1.0, as_of=NOW - timedelta(hours=3), source="x", ttl_seconds=3600)
    label = freshness_label(dp, NOW)
    assert "만료" in label


def test_freshness_label_missing_shows_reason():
    dp = missing(source="kr_market", reason="수급 원자료 결측")
    label = freshness_label(dp, NOW)
    assert "결측" in label
    assert "수급 원자료 결측" in label


def test_confidence_caps_are_ordered():
    # insufficient가 partial보다 더 엄격해야 함(상수 관계 회귀 방지)
    assert 0.0 < CONFIDENCE_CAP_INSUFFICIENT < CONFIDENCE_CAP_PARTIAL < 1.0


# ─────────────────────────────────────────────────────────────────
# us_market_data — VIX 수집 + 정규화 키 (F10, HTTP mock)
# ─────────────────────────────────────────────────────────────────
class _FakeResp:
    def __init__(self, status: int, data: dict):
        self.status = status
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._data


class _FakeSession:
    def __init__(self, responder):
        self._responder = responder
        self.closed = False

    def get(self, url, params=None, headers=None, timeout=None):
        status, data = self._responder(url, params)
        return _FakeResp(status, data)

    async def close(self):
        self.closed = True


def _quote(symbol: str, price: float, change_pct: float) -> dict:
    return {
        "symbol": symbol,
        "regularMarketPrice": price,
        "regularMarketChange": round(price * change_pct / 100, 2),
        "regularMarketChangePercent": change_pct,
        "shortName": symbol,
        "regularMarketVolume": 1000,
    }


def test_vix_is_collected_and_normalized_key_stable():
    from src.data.providers.us_market_data import USMarketData, US_INDEX_KEYS

    assert set(US_INDEX_KEYS.values()) == {"SP500", "NASDAQ", "DOW", "SOX", "VIX"}

    umd = USMarketData()

    def _responder(url, params):
        # F10 재현 방지: VIX가 실제로 Yahoo 요청 대상에 포함돼 있어야 한다
        symbols = params["symbols"].split(",")
        assert "^VIX" in symbols
        quotes = [
            _quote("^GSPC", 6500.0, 1.2),
            _quote("^IXIC", 21000.0, -0.5),
            _quote("^SOX", 5200.0, 2.0),
            _quote("^DJI", 41000.0, 0.3),
            _quote("^VIX", 18.5, -3.0),
        ]
        return 200, {"quoteResponse": {"result": quotes}}

    async def fake_get_session():
        return _FakeSession(_responder)

    umd._get_session = fake_get_session  # type: ignore[assignment]

    signal = asyncio.run(umd.get_overnight_signal())

    norm = signal["indices_normalized"]
    assert norm["VIX"]["price"] == 18.5
    assert norm["VIX"]["missing"] is False
    assert norm["VIX"]["as_of"] is not None
    assert norm["SP500"]["change_pct"] == 1.2
    assert norm["SOX"]["change_pct"] == 2.0
    # 기존 표시명 기반 indices/심리 평균은 그대로(VIX는 등락 심리에 안 섞임 — 레벨 지표라
    # 등락률을 방향성으로 합산하면 왜곡됨)
    assert "VIX" not in signal["indices"]


def test_vix_missing_is_none_not_zero():
    from src.data.providers.us_market_data import USMarketData

    umd = USMarketData()

    def _responder(url, params):
        # VIX 조회 실패 상황을 흉내 — 응답에 아예 없음
        return 200, {"quoteResponse": {"result": [_quote("^GSPC", 6500.0, 1.0)]}}

    async def fake_get_session():
        return _FakeSession(_responder)

    umd._get_session = fake_get_session  # type: ignore[assignment]

    signal = asyncio.run(umd.get_overnight_signal())
    vix = signal["indices_normalized"]["VIX"]

    assert vix["missing"] is True
    assert vix["price"] is None
    assert vix["as_of"] is None
    assert vix["reason"]


# ─────────────────────────────────────────────────────────────────
# kis_market_data — 야간선물 as_of 보존 + 동일값 반복 감지 (F12)
# ─────────────────────────────────────────────────────────────────
def _futures_json(price: float, change_pct: float) -> dict:
    return {
        "rt_cd": "0",
        "msg1": "",
        "output1": {
            "futs_prpr": str(price),
            "futs_sdpr": "408.0",
            "futs_prdy_vrss": "2.5",
            "futs_prdy_ctrt": str(change_pct),
            "acml_vol": "1000",
            "stck_hgpr": "411",
            "stck_lwpr": "407",
            "stck_oprc": "408",
        },
    }


def _make_kis_provider():
    from src.data.providers.kis_market_data import KISMarketData

    provider = KISMarketData(token_manager=SimpleNamespace(
        base_url="http://fake", app_key="k", app_secret="s",
        get_access_token=lambda: asyncio.sleep(0, result="tok"),
    ))

    async def fake_headers(tr_id):
        return {}

    provider._get_headers = fake_headers  # type: ignore[assignment]
    return provider


def test_night_futures_as_of_is_query_time_not_kis_tick():
    provider = _make_kis_provider()

    def _responder(url, params):
        return 200, _futures_json(410.5, 0.61)

    async def fake_get_session():
        return _FakeSession(_responder)

    provider._get_session = fake_get_session  # type: ignore[assignment]

    before = datetime.now()
    quote = asyncio.run(provider.get_night_futures_quote(symbol="TEST01", cache_ttl=0))
    after = datetime.now()

    assert quote is not None
    as_of = datetime.fromisoformat(quote["as_of"])
    assert before <= as_of <= after
    assert quote["value_changed_at"] is not None
    assert quote["value_unchanged_minutes"] == 0.0


def test_night_futures_repeated_value_keeps_original_changed_at():
    provider = _make_kis_provider()

    def _responder(url, params):
        return 200, _futures_json(410.5, 0.61)  # 매번 동일 값

    async def fake_get_session():
        return _FakeSession(_responder)

    provider._get_session = fake_get_session  # type: ignore[assignment]

    q1 = asyncio.run(provider.get_night_futures_quote(symbol="TEST02", cache_ttl=0))
    q2 = asyncio.run(provider.get_night_futures_quote(symbol="TEST02", cache_ttl=0))

    # 값(가격+등락률)이 그대로면 "새로 바뀐 시각"은 갱신되지 않아야
    # 고착 여부를 나중에 unchanged_minutes로 판단할 수 있다(0으로 리셋되면 구분 불가)
    assert q1["value_changed_at"] == q2["value_changed_at"]
    assert q2["as_of"] != q1["as_of"]  # 조회 자체는 매번 갱신
    assert q2["value_unchanged_minutes"] >= 0.0


def test_night_futures_changed_value_updates_changed_at():
    provider = _make_kis_provider()
    prices = iter([410.5, 415.0])

    def _responder(url, params):
        return 200, _futures_json(next(prices), 0.61)

    async def fake_get_session():
        return _FakeSession(_responder)

    provider._get_session = fake_get_session  # type: ignore[assignment]

    q1 = asyncio.run(provider.get_night_futures_quote(symbol="TEST03", cache_ttl=0))
    q2 = asyncio.run(provider.get_night_futures_quote(symbol="TEST03", cache_ttl=0))

    assert q1["price"] != q2["price"]
    assert q2["value_changed_at"] != q1["value_changed_at"]
