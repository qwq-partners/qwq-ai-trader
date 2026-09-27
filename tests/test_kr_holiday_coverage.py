"""KR 휴장일 커버리지 (2026-09-28) — 오프라인 가짜 세션만 사용, 실제 KIS 호출 없음.

- fetch_holidays: 한 응답이 달 중간에서 끝나면 가장 늦은 날짜 다음 날로 새 첫 조회를 다시 보낸다.
- engine.is_kr_market_holiday: 동적(KIS) ∪ fallback.
"""
import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from src.core import engine
from src.data.providers import kis_market_data as kmd


def _rows(start: date, end: date, holidays=()):
    rows, d = [], start
    while d <= end:
        closed = d.weekday() >= 5 or d in holidays
        rows.append({"bass_dt": d.strftime("%Y%m%d"), "opnd_yn": "N" if closed else "Y"})
        d += timedelta(days=1)
    return rows


class _Resp:
    def __init__(self, status, payload):
        self.status, self._payload = status, payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload


class _Session:
    """BASS_DT 순서대로 미리 준비한 응답을 돌려준다."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.bass_dts = []

    def get(self, url, headers=None, params=None):
        self.bass_dts.append(params["BASS_DT"])
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, payload = item
        return _Resp(status, payload)


def _ok(rows):
    return (200, {"rt_cd": "0", "output": rows})


def _run(monkeypatch, responses, year_month="202609"):
    acquired = []

    async def _acquire(tr_id=""):
        acquired.append(tr_id)

    monkeypatch.setattr(kmd.kis_rate_limit, "acquire", _acquire)

    async def _token():
        return "t"

    provider = kmd.KISMarketData(token_manager=SimpleNamespace(
        base_url="https://example.invalid", app_key="k", app_secret="s",
        get_access_token=_token,
    ))
    session = _Session(responses)

    async def _get_session():
        return session

    provider._get_session = _get_session
    result = asyncio.run(provider.fetch_holidays(year_month))
    return provider, session, acquired, result


CHUSEOK = {date(2026, 9, 24), date(2026, 9, 25)}


def test_second_query_covers_rest_of_month(monkeypatch):
    first = _rows(date(2026, 9, 1), date(2026, 9, 24), CHUSEOK)   # 운영 관측: 24일치에서 끝남
    rest = _rows(date(2026, 9, 25), date(2026, 10, 18), CHUSEOK)  # 다음 달이 섞여도 포함
    provider, session, acquired, result = _run(monkeypatch, [_ok(first), _ok(rest)])

    assert session.bass_dts == ["20260901", "20260925"]
    assert len(acquired) == 2  # 매 호출 전 리미터
    assert date(2026, 9, 25) in result and date(2026, 9, 24) in result
    assert date(2026, 9, 28) not in result
    assert provider._cache["holidays_202609"] == result


def test_stops_on_empty_response(monkeypatch):
    first = _rows(date(2026, 9, 1), date(2026, 9, 24), CHUSEOK)
    provider, session, _, result = _run(monkeypatch, [_ok(first), _ok([])])
    assert session.bass_dts == ["20260901", "20260925"]
    assert date(2026, 9, 24) in result
    assert "holidays_202609" not in provider._cache  # 월말 미확인 — 캐시 안 함


def test_stops_when_no_progress(monkeypatch):
    first = _rows(date(2026, 9, 1), date(2026, 9, 24), CHUSEOK)
    stale = _rows(date(2026, 9, 20), date(2026, 9, 24), CHUSEOK)  # 가장 늦은 날짜 < 커서(09-25)
    provider, session, _, result = _run(monkeypatch, [_ok(first), _ok(stale)])
    assert session.bass_dts == ["20260901", "20260925"]
    assert date(2026, 9, 24) in result and date(2026, 9, 25) not in result  # 빠진 날은 fallback 합집합이 덮는다
    assert "holidays_202609" not in provider._cache  # 월말 미확인 — 캐시 안 함


def test_stops_at_four_calls(monkeypatch):
    chunks = [
        _rows(date(2026, 9, 1), date(2026, 9, 5)),
        _rows(date(2026, 9, 6), date(2026, 9, 10)),
        _rows(date(2026, 9, 11), date(2026, 9, 15)),
        _rows(date(2026, 9, 16), date(2026, 9, 20)),
        _rows(date(2026, 9, 21), date(2026, 9, 30)),  # 호출되면 안 됨
    ]
    provider, session, acquired, _ = _run(monkeypatch, [_ok(c) for c in chunks])
    assert session.bass_dts == ["20260901", "20260906", "20260911", "20260916"]
    assert len(acquired) == 4
    assert "holidays_202609" not in provider._cache  # 09-20 까지만 확인 — 캐시 안 함


def test_single_call_when_first_response_covers_month(monkeypatch):
    provider, session, _, _ = _run(monkeypatch, [_ok(_rows(date(2026, 9, 1), date(2026, 9, 30), CHUSEOK))])
    assert session.bass_dts == ["20260901"]
    assert "holidays_202609" in provider._cache


def test_response_ending_at_cursor_is_progress(monkeypatch):
    # 커서 당일 한 줄만 와도 그날은 소비했으므로 다음 날부터 계속 조회한다
    first = _rows(date(2026, 9, 1), date(2026, 9, 24), CHUSEOK)
    one_day = _rows(date(2026, 9, 25), date(2026, 9, 25), CHUSEOK)
    rest = _rows(date(2026, 9, 26), date(2026, 9, 30), CHUSEOK)
    provider, session, _, result = _run(monkeypatch, [_ok(first), _ok(one_day), _ok(rest)])
    assert session.bass_dts == ["20260901", "20260925", "20260926"]
    assert date(2026, 9, 25) in result and "holidays_202609" in provider._cache


@pytest.mark.parametrize("second", [
    (500, {}),
    (200, {"rt_cd": "1", "msg1": "오류"}),
    RuntimeError("연결 끊김"),
])
def test_second_query_failure_keeps_first_result(monkeypatch, second):
    first = _rows(date(2026, 9, 1), date(2026, 9, 24), CHUSEOK)
    provider, session, _, result = _run(monkeypatch, [_ok(first), second])
    assert len(session.bass_dts) == 2
    assert date(2026, 9, 24) in result and date(2026, 9, 5) in result
    assert "holidays_202609" not in provider._cache  # 실패는 캐시하지 않음 (기존과 동일)


def test_first_query_failure_returns_empty(monkeypatch):
    _, session, _, result = _run(monkeypatch, [(500, {})])
    assert result == set() and session.bass_dts == ["20260901"]


def test_engine_holiday_union_with_fallback(monkeypatch):
    # 운영 관측 그대로: 동적 자료에 09-24 만 있고 09-25 는 없음
    monkeypatch.setattr(engine, "_kr_market_holidays", {date(2026, 9, 24)})
    assert engine.is_kr_market_holiday(date(2026, 9, 25)) is True
    assert engine.is_kr_market_holiday(date(2026, 12, 31)) is True
    assert engine.is_kr_market_holiday(date(2026, 12, 25)) is True
    assert engine.is_kr_market_holiday(date(2026, 9, 28)) is False


def test_engine_holiday_without_dynamic_data(monkeypatch):
    monkeypatch.setattr(engine, "_kr_market_holidays", set())
    assert engine.is_kr_market_holiday(date(2027, 12, 31)) is True
    assert engine.is_kr_market_holiday(date(2026, 9, 26)) is True  # 토요일
    assert engine.is_kr_market_holiday(date(2026, 9, 28)) is False


def test_session_fallback_has_year_end_closures():
    from src.utils import session
    assert {date(2026, 12, 31), date(2027, 12, 31)} <= session._KR_FALLBACK_HOLIDAYS


def test_fallback_is_single_list_with_corrected_dates(monkeypatch):
    # 합집합이라 fallback 에 잘못 든 개장일은 KIS 가 되돌릴 수 없다 — 2026-09-28 공식 근거로 교정한 날짜 고정
    from src.utils import session
    assert engine._FALLBACK_HOLIDAYS is session._KR_FALLBACK_HOLIDAYS
    monkeypatch.setattr(engine, "_kr_market_holidays", set())
    for d in (date(2026, 2, 17), date(2026, 5, 1), date(2026, 6, 3), date(2026, 7, 17),
              date(2027, 2, 9), date(2027, 9, 15)):
        assert engine.is_kr_market_holiday(d) is True, d
    for d in (date(2026, 1, 28), date(2027, 2, 10), date(2027, 6, 7), date(2027, 10, 14)):
        assert engine.is_kr_market_holiday(d) is False, d


def test_engine_setter_feeds_session_calendar(monkeypatch):
    # KOSPI 벤치마크 신선도·스크리너는 session 판정을 쓴다 — KIS 에만 있는 휴장일도 같이 봐야 한다
    from src.utils import session
    from src.utils.kospi_benchmark import benchmark_date_status
    from datetime import datetime
    monkeypatch.setattr(engine, "_kr_market_holidays", set())
    monkeypatch.setattr(session, "_kr_market_holidays", set())
    adhoc = date(2026, 10, 26)  # fallback 에 없는 합성 임시 휴장일(월)
    engine.set_kr_market_holidays({adhoc})
    assert engine.is_kr_market_holiday(adhoc) and session.is_kr_market_holiday(adhoc)
    # 화요일 장전: 직전 거래일은 금요일(10-23) — 금요일 봉이 fresh 여야 한다
    assert benchmark_date_status(date(2026, 10, 23), datetime(2026, 10, 27, 8, 30))[0] == "fresh"
