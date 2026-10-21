import asyncio
from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace

from src.analytics import daily_report as dr


class _Notifier:
    report_chat_id = 123

    def __init__(self):
        self.sent = []

    async def send_report(self, text):
        self.sent.append(text)
        return True


class _USData:
    def __init__(self, quotes):
        self.quotes = quotes
        self.sp500_calls = 0

    async def fetch_us_market_summary(self, force_refresh=False):
        assert force_refresh is True
        return self.quotes

    async def get_sector_signals(self):
        return {
            "AI/반도체": {
                "boost": 15,
                "us_avg_pct": 1.4,
                "top_movers": ["NVDA(+2.0%)"],
            }
        }

    async def fetch_sp500_stocks(self):
        self.sp500_calls += 1
        raise AssertionError("mobile report must not fetch a second stock universe")


def _generator(monkeypatch, notifier, quotes):
    monkeypatch.setattr(dr, "get_telegram_notifier", lambda: notifier)
    monkeypatch.setattr(dr, "get_screener", lambda: SimpleNamespace())
    monkeypatch.setattr(dr, "get_theme_detector", lambda: SimpleNamespace())
    monkeypatch.setattr(dr, "NewsCollector", lambda: SimpleNamespace())
    monkeypatch.setattr(dr, "save_morning_brief", lambda *_args, **_kwargs: None)
    generator = dr.DailyReportGenerator()
    generator._us_market_data = _USData(quotes)
    return generator


def _quote(price, pct, stamp):
    return {"price": price, "change_pct": pct, "market_time": stamp}


def test_mobile_report_sends_two_snapshot_images_then_full_accessible_text(monkeypatch):
    close = datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(
        dr, "_now", lambda: datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc),
    )
    quotes = {
        "^GSPC": _quote(6888.0, 0.55, close),
        "^IXIC": _quote(23100.0, 0.91, close),
        "^DJI": _quote(47100.0, 0.12, close),
        "^SOX": _quote(7100.0, 1.24, close),
        "^VIX": _quote(18.4, -2.1, close),
        "SMH": _quote(350.0, 1.8, close),
        "XLK": _quote(290.0, 0.8, close),
        "NVDA": _quote(205.0, 2.0, close),
        "AAPL": _quote(265.0, -0.3, close),
    }
    notifier = _Notifier()
    generator = _generator(monkeypatch, notifier, quotes)

    import src.analytics.us_market_chart as chart
    chart_calls = []

    def mobile_charts(**kwargs):
        chart_calls.append(kwargs)
        return [BytesIO(b"overview"), BytesIO(b"watchlist")]

    monkeypatch.setattr(chart, "generate_mobile_charts", mobile_charts, raising=False)
    import src.utils.telegram as telegram
    photo_calls = []

    async def send_photo(buf, **kwargs):
        photo_calls.append((buf.read(), kwargs))
        return True

    monkeypatch.setattr(telegram, "send_photo", send_photo)

    report = asyncio.run(generator.generate_us_market_report(send_telegram=True))

    assert chart_calls == [{
        "quotes": quotes,
        "date_str": "2026.10.21",
        "avg_pct": (0.55 + 0.91 + 0.12 + 1.24) / 4,
    }]
    assert [body for body, _ in photo_calls] == [b"overview", b"watchlist"]
    assert all(call["chat_id"] == 123 and call["parse_mode"] == "HTML" for _, call in photo_calls)
    assert all(len(call["caption"]) <= 1024 for _, call in photo_calls)
    assert notifier.sent == [report]
    assert "■ 주요 ETF" in report and "SMH" in report and "XLK" in report
    assert "■ 대표 종목" in report and "NVDA" in report and "AAPL" in report
    assert "반도체 ETF(SMH) +1.80% · 10/21" in report
    assert "기술 ETF(XLK) +0.80% · 10/21" in report
    assert "주요 ETF (2/8 확인)" in report
    assert "대표 종목 (2/8 확인)" in report
    assert "NVDA(NVDA)" not in report
    assert "SOX·SMH·NVDA 같은 방향 상승" in report
    assert "VIX 18.4 · -2.10%" in report
    assert "확인 지수 4/4: 상승 4 · 하락 0 · 보합 0" in report
    assert "미국 지수 평균" not in report
    assert report.index("■ 핵심 사실") < report.index("■ 주요 지수")
    assert "개장 후 관련 업종 거래대금·외국인/기관 수급 확인" in report
    assert "부스트" not in report
    assert generator._us_market_data.sp500_calls == 0


def test_mobile_report_attempts_both_images_and_sends_text_when_one_photo_fails(monkeypatch):
    close = datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(
        dr, "_now", lambda: datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc),
    )
    quotes = {"^GSPC": _quote(6888.0, 0.55, close)}
    notifier = _Notifier()
    generator = _generator(monkeypatch, notifier, quotes)

    import src.analytics.us_market_chart as chart
    monkeypatch.setattr(
        chart, "generate_mobile_charts",
        lambda **_kwargs: [BytesIO(b"first"), BytesIO(b"second")],
        raising=False,
    )
    import src.utils.telegram as telegram
    attempts = []

    async def send_photo(buf, **_kwargs):
        attempts.append(buf.read())
        if len(attempts) == 1:
            raise RuntimeError("first upload failed")
        return True

    monkeypatch.setattr(telegram, "send_photo", send_photo)

    report = asyncio.run(generator.generate_us_market_report(send_telegram=True))

    assert attempts == [b"first", b"second"]
    assert notifier.sent == [report]


def test_mobile_photo_captions_do_not_assign_index_date_to_other_quotes(monkeypatch):
    now = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)
    latest = datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc).timestamp()
    older = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(dr, "_now", lambda: now)
    generator = _generator(monkeypatch, _Notifier(), {
        "^GSPC": _quote(6888.0, 0.55, latest),
        "^VIX": _quote(18.4, -2.1, older),
        "SMH": _quote(350.0, 1.8, older),
    })
    import src.analytics.us_market_chart as chart
    import src.utils.telegram as telegram
    monkeypatch.setattr(chart, "generate_mobile_charts", lambda **_: [
        BytesIO(b"overview"), BytesIO(b"watchlist"),
    ])
    captions = []

    async def send_photo(_buf, **kwargs):
        captions.append(kwargs["caption"])
        return True

    monkeypatch.setattr(telegram, "send_photo", send_photo)
    asyncio.run(generator.generate_us_market_report(send_telegram=True))

    assert len(captions) == 2
    assert all("2026.10.21" not in caption for caption in captions)
    assert all("항목별" in caption for caption in captions)


def test_semiconductor_trio_all_zero_is_flat_not_divergent(monkeypatch):
    close = datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(
        dr, "_now", lambda: datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc),
    )
    generator = _generator(monkeypatch, _Notifier(), {
        "^SOX": _quote(7100.0, 0.0, close),
        "SMH": _quote(350.0, 0.0, close),
        "NVDA": _quote(205.0, 0.0, close),
    })
    report = asyncio.run(generator.generate_us_market_report(send_telegram=False))

    assert "SOX·SMH·NVDA 모두 보합" in report
    assert "SOX·SMH·NVDA 방향 엇갈림" not in report


def test_small_changes_keep_two_decimal_precision_in_report_and_cached_brief(monkeypatch):
    close = datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(
        dr, "_now", lambda: datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc),
    )
    quotes = {
        "^GSPC": _quote(6888.0, -0.02, close),
        "^VIX": _quote(18.4, -0.02, close),
        "SMH": _quote(350.0, -0.02, close),
        "NVDA": _quote(205.0, 0.02, close),
    }
    generator = _generator(monkeypatch, _Notifier(), quotes)
    report = asyncio.run(generator.generate_us_market_report(send_telegram=False))
    record = asyncio.run(generator.build_morning_brief(
        quotes=quotes, sector_signals={}, avg_pct=-0.02,
        us_date_str="2026.10.21", mood="보합",
    ))

    assert "VIX 18.4 · -0.02%" in report
    for text in (report, record["text"]):
        assert "반도체 ETF(SMH) -0.02%" in text
        assert "NVDA +0.02%" in text
        assert "-0.0%" not in text


def test_report_discloses_mixed_market_days_and_never_predicts_korean_open(monkeypatch):
    first = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc).timestamp()
    second = datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc).timestamp()
    quotes = {
        "^GSPC": _quote(6888.0, 0.55, second),
        "^IXIC": _quote(22900.0, -0.40, first),
        "SMH": _quote(350.0, 1.8, second),
    }
    monkeypatch.setattr(
        dr, "_now", lambda: datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc),
    )
    generator = _generator(monkeypatch, _Notifier(), quotes)

    report = asyncio.run(generator.generate_us_market_report(
        send_telegram=False,
        kr_inputs=[{
            "name": "야간선물", "value": "+1.0%",
            "as_of": "2026-10-22T06:30:00+09:00", "source": "kis",
        }],
    ))

    assert "시장일 혼재" in report
    assert "직접 비교" in report
    assert "한국 개장 방향은 이 자료만으로 판단하지 않음" in report
    for unsupported in ("갭업 가능성", "긍정적", "하방 압력", "보수적 접근"):
        assert unsupported not in report


def test_index_basis_does_not_borrow_known_date_from_etf(monkeypatch):
    close = datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(
        dr, "_now", lambda: datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc),
    )
    quotes = {
        "^GSPC": {"price": 6888.0, "change_pct": 0.55},
        "^IXIC": {"price": 23100.0, "change_pct": 0.91},
        "SMH": _quote(350.0, 1.8, close),
    }
    generator = _generator(monkeypatch, _Notifier(), quotes)

    report = asyncio.run(generator.generate_us_market_report(send_telegram=False))

    assert "미국 지수 평균" not in report
    assert "주요 지수 (2/4 확인)" in report
    assert report.count("기준일 미확인") >= 3
    assert "2026.10.21 시장 자료" not in report


def test_future_and_malformed_index_rows_are_withheld(monkeypatch):
    now = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)
    past = datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc).timestamp()
    future = datetime(2026, 10, 23, 20, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(dr, "_now", lambda: now)
    quotes = {
        "^GSPC": _quote(6888.0, 9.99, future),
        "^IXIC": "malformed",
        "^DJI": _quote(47100.0, 0.12, past),
        "SMH": {"price": float("nan"), "change_pct": 1.8},
    }
    generator = _generator(monkeypatch, _Notifier(), quotes)

    report = asyncio.run(generator.generate_us_market_report(send_telegram=False))

    assert "6,888" not in report and "+9.99%" not in report
    assert "주요 지수 (1/4 확인)" in report
    assert "다우" in report and "2026.10.21" in report
    assert "주요 ETF (0/8 확인)" in report
    assert "ETF 자료 미확인" in report


def test_brief_excludes_future_quotes_and_unverified_sector_aggregate(monkeypatch):
    now = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)
    future = datetime(2026, 10, 23, 20, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(dr, "_now", lambda: now)
    generator = _generator(monkeypatch, _Notifier(), {})

    record = asyncio.run(generator.build_morning_brief(
        quotes={
            "^GSPC": _quote(6888.0, 9.99, future),
            "^IXIC": {"price": 23100.0, "change_pct": 0.91},
        },
        sector_signals={
            "AI/반도체": {
                "boost": 15, "us_avg_pct": 1.4, "top_movers": ["NVDA(+2.0%)"],
            }
        },
        avg_pct=0.0,
        us_date_str="기준일 미확인",
        mood="기준일 미확인",
    ))

    assert "9.99%" not in record["text"]
    assert "NASDAQ: +0.91%" in record["text"]
    assert "AI/반도체" not in record["text"]


def test_brief_uses_dated_raw_rows_not_sector_boost_with_valid_looking_date(monkeypatch):
    now = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)
    close = datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc).timestamp()
    older = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(dr, "_now", lambda: now)
    generator = _generator(monkeypatch, _Notifier(), {})

    record = asyncio.run(generator.build_morning_brief(
        quotes={
            "^GSPC": _quote(6888.0, 0.55, close),
            "SMH": {"price": 350.0, "change_pct": 1.8},
            "SOXX": _quote(305.0, 1.2, older),
            "NVDA": _quote(205.0, 2.0, close),
        },
        sector_signals={
            "AI/반도체": {
                "boost": 15, "us_avg_pct": 1.4, "top_movers": ["NVDA(+2.0%)"],
            }
        },
        avg_pct=0.55,
        us_date_str="2026.10.21",
        mood="보합",
    ))

    assert "AI/반도체" not in record["text"] and "부스트" not in record["text"]
    assert "S&P500: +0.55% (6,888.0) · 10/21" in record["text"]
    assert "NASDAQ: 가격·등락 미수집" in record["text"]
    assert "NVDA +2.00% · 10/21" in record["text"]
    assert "AAPL 자료 미확인" in record["text"]
    assert "반도체 ETF(SMH) +1.80% · 기준일 미확인" in record["text"]


def test_optional_sector_signal_failure_does_not_block_report(monkeypatch):
    close = datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(
        dr, "_now", lambda: datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc),
    )
    notifier = _Notifier()
    generator = _generator(monkeypatch, notifier, {"^GSPC": _quote(6888.0, 0.55, close)})

    async def fail_signals():
        raise TypeError("malformed sector contributor")

    generator._us_market_data.get_sector_signals = fail_signals
    report = asyncio.run(generator.generate_us_market_report(send_telegram=False))

    assert "미국시장 아침 스냅샷" in report
    assert "S&P500" in report


def test_trio_mixed_dates_says_market_days_mixed_not_alignment(monkeypatch):
    now = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)
    close = datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc).timestamp()
    older = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(dr, "_now", lambda: now)
    quotes = {
        "^SOX": _quote(7100.0, 1.2, close),
        "SMH": _quote(350.0, 1.8, older),
        "NVDA": _quote(205.0, 2.0, close),
    }
    generator = _generator(monkeypatch, _Notifier(), quotes)

    report = asyncio.run(generator.generate_us_market_report(send_telegram=False))

    assert "SOX·SMH·NVDA 확인 3/3 · 시장일 혼재" in report
    assert "정합 3/3" not in report


def test_shared_quote_observer_rejects_bool_time_and_treats_naive_now_as_kst():
    from src.analytics.us_report_data import observe_us_quote

    quote = {"price": 100.0, "change_pct": 1.0, "market_time": True}
    assert observe_us_quote(quote, now=datetime(2026, 10, 22, 8, 0)) is None

    close = datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc).timestamp()
    observed = observe_us_quote(
        {"price": 100.0, "change_pct": 1.0, "market_time": close},
        now=datetime(2026, 10, 22, 8, 0),
    )
    assert observed["date"] == "2026.10.21"
