import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from src.analytics.report_index_observations import parse_index_observation
from src.data.providers.kis_market_data import KISMarketData
from src.utils import kis_direct_metrics


RECEIVED = datetime(2026, 10, 27, 7, 0, tzinfo=timezone.utc)  # 16:00 KST
REPORT_DATE = date(2026, 10, 27)


def _payload(rows, output1=None):
    return {"rt_cd": "0", "output1": output1 or {"bstp_nmix_prpr": "5638.34"}, "output2": rows}


def _row(clock="153200", price="5638.35", day="20261027"):
    return {"stck_bsop_date": day, "stck_cntg_hour": clock, "bstp_nmix_prpr": price}


def test_parser_uses_only_latest_valid_output2_bar_and_exact_allowlist():
    item = parse_index_observation(_payload([
        _row("999999", "999999"), _row("888888", "888888"),
        _row("153000", "5638.30"), _row("153200", "5638.35"),
    ]), index_code="0001", report_date=REPORT_DATE, received_at=RECEIVED)

    assert item == {
        "code": "0001", "label": "KOSPI", "price": 5638.35,
        "source_observed_at": "2026-10-27T15:32:00+09:00",
        "received_at": "2026-10-27T07:00:00+00:00",
        "source": "KIS/FHKUP03500200", "observation_kind": "historical_minute",
        "finality": "unverified",
    }
    assert parse_index_observation(_payload([_row()]), index_code="9999", report_date=REPORT_DATE, received_at=RECEIVED) is None


@pytest.mark.parametrize("payload, received", [
    (_payload([]), RECEIVED),
    (_payload([_row("152959")]), RECEIVED),
    (_payload([_row("240000")]), RECEIVED),
    (_payload([_row(price="nan")]), RECEIVED),
    (_payload([_row(price=True)]), RECEIVED),
    (_payload([_row(day="20261026")]), RECEIVED),
    (_payload([_row("153200"), _row("153200", "5639")]), RECEIVED),
    (_payload([_row("160100")]), RECEIVED),
])
def test_parser_rejects_missing_invalid_stale_conflicting_or_future_bar(payload, received):
    assert parse_index_observation(payload, index_code="0001", report_date=REPORT_DATE, received_at=received) is None


def test_parser_rejects_naive_or_wrong_received_calendar_and_output1_only():
    assert parse_index_observation({"rt_cd": "0", "output1": {"bstp_nmix_prpr": "9999"}}, index_code="0001", report_date=REPORT_DATE, received_at=RECEIVED) is None
    assert parse_index_observation(_payload([_row()]), index_code="0001", report_date=REPORT_DATE, received_at=datetime(2026, 10, 27, 16, 0)) is None
    assert parse_index_observation(_payload([_row()]), index_code="0001", report_date=date(2026, 10, 26), received_at=RECEIVED) is None


class _Response:
    status = 200

    def __init__(self, payload=None, error=None):
        self.payload, self.error = payload, error

    async def json(self):
        if self.error:
            raise self.error
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class _Observed:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def inspect_response(self, response):
        return response.status == 200

    async def json(self, response):
        return await response.json()


def _provider(monkeypatch, responses):
    provider = KISMarketData(token_manager=SimpleNamespace(base_url="https://example.invalid"))
    session = _Session(responses)

    async def headers(tr_id):
        assert tr_id == "FHKUP03500200"
        return {"tr_id": tr_id}

    async def acquire(*_args):
        return None

    monkeypatch.setattr(provider, "_get_session", lambda: _async_value(session))
    monkeypatch.setattr(provider, "_get_headers", headers)
    monkeypatch.setattr("src.data.providers.kis_market_data.kis_rate_limit.acquire", acquire)
    monkeypatch.setattr("src.data.providers.kis_market_data.kis_direct_metrics.observe_get", lambda *_args: _Observed())
    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return RECEIVED if tz is None else RECEIVED.astimezone(tz)
    monkeypatch.setattr("src.data.providers.kis_market_data.datetime", _FixedDatetime)
    return provider, session


async def _async_value(value):
    return value


def test_provider_makes_two_sequential_gets_and_never_mutates_shared_cache(monkeypatch):
    provider, session = _provider(monkeypatch, [_Response(_payload([_row()])), _Response(_payload([_row(price="700" )]))])
    provider._cache["keep"] = {"x": 1}
    provider._cache_ts["keep"] = datetime.now()
    cache_ts = dict(provider._cache_ts)

    rows = asyncio.run(provider.fetch_report_index_observations(REPORT_DATE))

    assert [row["code"] for row in rows] == ["0001", "1001"]
    assert len(session.calls) == 2
    assert [call[1]["params"]["FID_INPUT_ISCD"] for call in session.calls] == ["0001", "1001"]
    for _, kwargs in session.calls:
        assert kwargs["allow_redirects"] is False
        assert kwargs["params"] == {
            "FID_COND_MRKT_DIV_CODE": "U", "FID_ETC_CLS_CODE": "0",
            "FID_INPUT_ISCD": kwargs["params"]["FID_INPUT_ISCD"],
            "FID_INPUT_HOUR_1": "60", "FID_PW_DATA_INCU_YN": "N",
        }
    assert provider._cache == {"keep": {"x": 1}}
    assert provider._cache_ts == cache_ts


def test_provider_keeps_second_observation_after_first_error(monkeypatch):
    provider, session = _provider(monkeypatch, [_Response(error=ValueError("bad response")), _Response(_payload([_row(price="700")]))])

    rows = asyncio.run(provider.fetch_report_index_observations(REPORT_DATE))

    assert [row["code"] for row in rows] == ["1001"]
    assert len(session.calls) == 2


def test_provider_propagates_cancellation_without_second_request(monkeypatch):
    provider, session = _provider(monkeypatch, [_Response(error=asyncio.CancelledError()), _Response(_payload([_row()]))])

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(provider.fetch_report_index_observations(REPORT_DATE))
    assert len(session.calls) == 1


def test_direct_metrics_allow_report_index_bar_dimension():
    assert "report_index_bar" in kis_direct_metrics.OPERATIONS
    assert "FHKUP03500200" in kis_direct_metrics.TR_IDS
