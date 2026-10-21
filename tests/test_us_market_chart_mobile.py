"""모바일 PNG의 결측·시점·글자 크기 계약을 실제 렌더링으로 확인한다."""
from datetime import datetime, timezone
import io

import pytest
from PIL import Image
from matplotlib.figure import Figure

from src.analytics import us_market_chart as chart


def quote(pct=0.0, price=100.0, day=20):
    stamp = datetime(2026, 10, day, 20, tzinfo=timezone.utc).timestamp()
    return {"price": price, "change_pct": pct, "market_time": stamp}


@pytest.fixture
def rendered(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 22, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(chart, "datetime", FixedDateTime)
    figures = []
    save = Figure.savefig

    def capture(self, *args, **kwargs):
        save(self, *args, **kwargs)
        figures.append(self)

    monkeypatch.setattr(Figure, "savefig", capture)
    return figures


def words(figure):
    return "\n".join(text.get_text() for text in figure.texts)


def test_existing_entrypoint_never_invents_zero_for_missing_quotes(rendered):
    result = chart.generate_combined_chart({"^GSPC": quote()}, {}, "2026.10.20")
    assert result is not None
    text = words(rendered[0])
    assert "미확인" in text
    assert "Russell" not in text
    assert "0.00%" in text  # 실제 0은 결측과 구분한다.
    assert text.count("0.00%") == 1


def test_two_phone_sized_cards_keep_primary_type_readable(rendered):
    render = getattr(chart, "generate_mobile_charts", None)
    assert callable(render), "모바일 두 장 생성 경로가 필요하다"
    data = {sym: quote(1.25) for sym in (
        "^GSPC", "^IXIC", "^DJI", "^SOX", "^VIX", "SMH", "XLK", "XLV",
        "XBI", "XLE", "URNM", "LIT", "ITA", "NVDA", "AAPL", "MSFT",
        "GOOG", "META", "AMZN", "TSLA", "AMD",
    )}
    images = render(data, "2026.10.20")
    assert len(images) == 2
    for buf, figure in zip(images, rendered):
        assert isinstance(buf, io.BytesIO) and buf.tell() == 0
        picture = Image.open(buf)
        assert picture.format == "PNG"
        assert picture.width == 720 and picture.height <= 1280
        renderer = figure.canvas.get_renderer()
        for text in figure.texts:
            bounds = text.get_window_extent(renderer)
            assert bounds.x0 >= -1 and bounds.x1 <= 721, text.get_text()
            assert bounds.y0 >= -1 and bounds.y1 <= picture.height + 1, text.get_text()
            if text.get_gid() == "primary":
                # 360px 폭으로 보아도 주요 숫자·이름의 글자 높이가 14px 이상.
                assert text.get_fontsize() * figure.dpi / 72 * .5 >= 14
    assert "대표 종목 8개" in words(rendered[1])
    assert "S&P 500 Map" not in words(rendered[1])


def test_invalid_values_and_future_quotes_are_unavailable(rendered):
    render = getattr(chart, "generate_mobile_charts", None)
    assert callable(render)
    data = {"^GSPC": quote(float("nan")), "^IXIC": quote(True),
            "^DJI": quote(4, price=0), "^SOX": None,
            "^VIX": {"price": 20, "change_pct": -3, "market_time": 999999999999},
            "SMH": {"price": 10, "change_pct": float("inf")},
            "XLK": {"price": 10 ** 1000, "change_pct": 3}}
    assert len(render(data)) == 2
    text = words(rendered[0])
    assert "지수 방향 미확인" in text
    assert "0.00%" not in text and "-3.00%" not in text
    assert "자료 미확인" in text


def test_mixed_market_days_are_visible_and_not_averaged(rendered):
    render = getattr(chart, "generate_mobile_charts", None)
    assert callable(render)
    assert len(render({"^GSPC": quote(4, day=20), "^IXIC": quote(-4, day=19)})) == 2
    text = words(rendered[0])
    assert "시장일 혼재" in text
    assert "10/20" in text and "10/19" in text
    assert "보합 마감" not in text


def test_vix_decline_is_not_presented_as_equity_loss(rendered):
    render = getattr(chart, "generate_mobile_charts", None)
    assert callable(render)
    render({"^VIX": quote(-6.13, 17.31)})
    text = words(rendered[0])
    assert "17.31" in text and "-6.13%" in text
    assert "변동성 기대 하락" in text
    assert "약세 마감" not in text


def test_explicit_bad_timestamps_are_not_treated_as_absent(rendered):
    data = {symbol: dict(quote(7.77), market_time=stamp) for symbol, stamp in (
        ("^GSPC", True), ("^IXIC", "bad"), ("^DJI", float("nan")),
        ("^SOX", -1), ("^VIX", "999999999999"),
    )}
    assert len(chart.generate_mobile_charts(data)) == 2
    assert "+7.77%" not in words(rendered[0])
    assert "지수 방향 미확인" in words(rendered[0])


def test_numeric_source_strings_follow_same_text_report_contract(rendered):
    data = {"^GSPC": {key: str(value) for key, value in quote(.28).items()}}
    assert len(chart.generate_mobile_charts(data)) == 2
    assert "+0.28%" in words(rendered[0])
    assert "2026.10.20" in words(rendered[0])
