"""Cohort persistence and report-helper ordering stay inside the report process."""
import asyncio
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest


KST = ZoneInfo("Asia/Seoul")


def _stock(dr, rank, symbol):
    return dr.RecommendedStock(rank=rank, symbol=symbol, name=symbol,
                               investment_thesis="", catalyst="")


def _generator(dr, stocks):
    generator = dr.DailyReportGenerator.__new__(dr.DailyReportGenerator)
    generator._today_recommendations = stocks
    generator._recommendation_date = date(2026, 10, 24)
    generator._report_cohort = None
    generator.toss_report_prepare = None
    generator.toss_quote_provider = None
    return generator


def test_persisted_cohort_keeps_original_aware_creation_time_and_exact_members(tmp_path, monkeypatch):
    from src.analytics import daily_report as dr

    monkeypatch.setattr(dr, "_REC_CACHE_DIR", tmp_path)
    generated_at = datetime(2026, 10, 24, 8, 25, 17, 123456, tzinfo=KST)
    stocks = [_stock(dr, 1, "005930"), _stock(dr, 2, "000660")]
    writer = _generator(dr, stocks)

    writer._set_report_cohort(date(2026, 10, 24), generated_at)
    writer._save_recommendations(date(2026, 10, 24))
    raw = json.loads((tmp_path / "morning_recs_2026-10-24.json").read_text())
    assert raw["report_cohort"]["generated_at"] == generated_at.isoformat()
    assert raw["report_cohort"]["members"] == [
        {"rank": 1, "symbol": "005930"}, {"rank": 2, "symbol": "000660"},
    ]

    recovered = _generator(dr, [])
    assert recovered._load_recommendations(date(2026, 10, 24))[0].symbol == "005930"
    assert recovered._report_cohort.generated_at == generated_at
    assert recovered._report_cohort.members == ((1, "005930"), (2, "000660"))


@pytest.mark.parametrize("members", [
    [{"rank": 1, "symbol": "005930"}],
    [{"rank": 1, "symbol": "005930"}, {"rank": 1, "symbol": "005930"}],
])
def test_recovery_keeps_legacy_stocks_but_rejects_mismatched_or_duplicate_cohort(tmp_path, monkeypatch, members):
    from src.analytics import daily_report as dr
    from src.data.providers.toss.report_protocol import ReportCohort

    monkeypatch.setattr(dr, "_REC_CACHE_DIR", tmp_path)
    stocks = [_stock(dr, 1, "005930"), _stock(dr, 2, "000660")]
    cohort = ReportCohort.create(
        date(2026, 10, 24), datetime(2026, 10, 24, 8, 25, tzinfo=KST),
        ((1, "005930"),),
    ).to_document()
    cohort["members"] = members
    raw = {
        "date": "2026-10-24",
        "stocks": [dict(stock.__dict__) for stock in stocks],
        "report_cohort": cohort,
    }
    (tmp_path / "morning_recs_2026-10-24.json").write_text(json.dumps(raw))

    recovered = _generator(dr, [])
    assert [stock.symbol for stock in recovered._load_recommendations(date(2026, 10, 24))] == [
        "005930", "000660",
    ]
    assert recovered._report_cohort is None


def test_recovery_rejects_out_of_date_cohort_but_preserves_legacy_stocks(tmp_path, monkeypatch):
    from src.analytics import daily_report as dr
    from src.data.providers.toss.report_protocol import ReportCohort

    monkeypatch.setattr(dr, "_REC_CACHE_DIR", tmp_path)
    stock = _stock(dr, 1, "005930")
    cohort = ReportCohort.create(
        date(2026, 10, 23), datetime(2026, 10, 23, 8, 25, tzinfo=KST),
        ((1, "005930"),),
    )
    raw = {"date": "2026-10-24", "stocks": [dict(stock.__dict__)],
           "report_cohort": cohort.to_document()}
    (tmp_path / "morning_recs_2026-10-24.json").write_text(json.dumps(raw))
    recovered = _generator(dr, [])
    assert len(recovered._load_recommendations(date(2026, 10, 24))) == 1
    assert recovered._report_cohort is None


@pytest.mark.asyncio
async def test_evening_prepares_valid_cohort_before_reading_quotes(monkeypatch):
    from src.analytics import daily_report as dr

    today = date(2026, 10, 24)
    generator = _generator(dr, [_stock(dr, 1, "005930")])
    generator._set_report_cohort(today, datetime(2026, 10, 24, 8, 25, tzinfo=KST))
    observed = []

    async def prepare(cohort):
        observed.append(("prepare", cohort.members))

    async def update():
        observed.append(("update", generator.toss_quote_provider("005930")))

    generator.toss_report_prepare = prepare
    generator.toss_quote_provider = lambda symbol: "new-price" if observed else "stale-price"
    generator._update_results = update
    generator._format_evening_report = lambda *_: "report"
    monkeypatch.setattr(dr, "_today", lambda: today)
    monkeypatch.setattr(dr, "date", type("FixedDate", (), {"today": staticmethod(lambda: today)}))

    assert await generator.generate_evening_report(send_telegram=False) == "report"
    assert observed == [("prepare", ((1, "005930"),)), ("update", "new-price")]


@pytest.mark.asyncio
async def test_evening_falls_back_after_prepare_error_but_propagates_cancellation(monkeypatch):
    from src.analytics import daily_report as dr

    today = date(2026, 10, 24)
    generator = _generator(dr, [_stock(dr, 1, "005930")])
    generator._set_report_cohort(today, datetime(2026, 10, 24, 8, 25, tzinfo=KST))
    generator._format_evening_report = lambda *_: "report"
    seen = []

    async def failed_prepare(_):
        raise RuntimeError("helper unavailable")

    async def update():
        seen.append("fallback")

    generator.toss_report_prepare = failed_prepare
    generator._update_results = update
    monkeypatch.setattr(dr, "date", type("FixedDate", (), {"today": staticmethod(lambda: today)}))
    assert await generator.generate_evening_report(send_telegram=False) == "report"
    assert seen == ["fallback"]

    async def cancelled(_):
        raise asyncio.CancelledError()

    generator.toss_report_prepare = cancelled
    with pytest.raises(asyncio.CancelledError):
        await generator.generate_evening_report(send_telegram=False)


@pytest.mark.asyncio
async def test_evening_uses_actual_helper_quote_only_after_prepare_completes(monkeypatch, tmp_path):
    from src.analytics import daily_report as dr
    from src.data.providers.toss import report_helper
    from src.data.providers.toss.market_types import Quote
    from src.data.providers.toss.report_protocol import ReportCohort, make_result

    today = date(2026, 10, 24)
    now = datetime(today.year, today.month, today.day, 16, 1, tzinfo=KST)
    monkeypatch.setattr(dr, "date", type("FixedDate", (), {"today": staticmethod(lambda: today)}))
    monkeypatch.setattr(dr, "_REC_CACHE_DIR", tmp_path)
    monkeypatch.setattr(report_helper, "_now", lambda: now)
    monkeypatch.setenv("TOSS_REPORT_API", "1")
    helper = report_helper.ReportHelperClient(enabled=lambda: True)

    class Process:
        def __init__(self):
            self.returncode = None
            self.stdin = None
            self.stdout = type("Stdout", (), {"read": staticmethod(lambda _size: _empty())})()
            self._transport = type("Transport", (), {"close": staticmethod(lambda: None)})()

        async def wait(self):
            self.returncode = 0
            return 0

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = 0

    async def _empty():
        return b""

    helper._process = Process()

    async def exchange(request):
        quote = Quote("005930", Decimal("101"), now - timedelta(seconds=1), now,
                      "ok", frozenset(), "unknown", "KRW")
        return make_result(request, quotes=(quote,), valid_until=now + timedelta(seconds=50))

    helper._exchange = exchange
    generator = _generator(dr, [_stock(dr, 1, "005930")])
    generator._set_report_cohort(today, now.replace(hour=8, minute=25))
    generator.toss_report_prepare = helper.prepare_report

    def bound_quote(symbol):
        current = generator._valid_report_cohort(today)
        return None if current is None else helper.get_report_quote(symbol, cohort=current)

    generator.toss_quote_provider = bound_quote
    seen = []

    async def update():
        quote = generator.toss_quote_provider("005930")
        seen.append(None if quote is None else quote.price)

    generator._update_results = update
    generator._format_evening_report = lambda *_: "report"
    assert await generator.generate_evening_report(send_telegram=False) == "report"
    assert seen == [Decimal("101")]
    different = ReportCohort.create(today, now.replace(hour=8, minute=25), ((1, "000660"),))
    assert helper.get_report_quote("005930", cohort=different) is None
    generator._today_recommendations = [_stock(dr, 1, "000660")]
    assert generator.toss_quote_provider("005930") is None

    helper._process.returncode = 0
    await helper.close()
