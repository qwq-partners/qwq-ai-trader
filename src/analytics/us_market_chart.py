"""미국 마감 자료를 모바일용 두 장으로 렌더링한다.

고정 720px 폭, 주요 글자 28px 이상. 미수집 값을 보합으로 채우거나
고정 비중을 실시간 시가총액 맵으로 표시하지 않는다. 외부 조회는 하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import io
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from .us_report_data import observe_us_quote

logger = logging.getLogger(__name__)

INDEX_ORDER = [("^GSPC", "S&P 500"), ("^IXIC", "나스닥"), ("^DJI", "다우"),
               ("^SOX", "반도체 SOX"), ("^VIX", "변동성 VIX")]
ETF_ORDER = [("SMH", "반도체"), ("XLK", "기술주"), ("XLV", "헬스케어"),
             ("XBI", "바이오"), ("XLE", "에너지"), ("URNM", "우라늄"),
             ("LIT", "리튬·배터리"), ("ITA", "항공·방산")]
STOCK_ORDER = [("NVDA", "엔비디아"), ("AAPL", "애플"), ("MSFT", "마이크로소프트"),
               ("GOOG", "알파벳"), ("META", "메타"), ("AMZN", "아마존"),
               ("TSLA", "테슬라"), ("AMD", "AMD")]

BG = "#F4F6FA"
INK = "#142136"
MUTED = "#526175"
UP = "#B42318"
DOWN = "#1D4ED8"
VIX_COLOR = "#6941C6"
WIDTH = 720
DPI = 100


@dataclass(frozen=True)
class _Row:
    symbol: str
    label: str
    price: Optional[float]
    pct: Optional[float]
    day: Optional[str]

    @property
    def valid(self) -> bool:
        return self.price is not None and self.price > 0 and self.pct is not None

    @property
    def basis(self) -> str:
        return self.day[5:].replace("-", "/") if self.day else "기준일 미확인"


def _rows(quotes: Dict[str, Any], order: list) -> list[_Row]:
    now = datetime.now(timezone.utc)
    result = []
    for symbol, label in order:
        observed = observe_us_quote(quotes.get(symbol), now=now)
        price = observed["price"] if observed else None
        pct = observed["pct"] if observed else None
        day = observed["date"].replace(".", "-") if observed and observed["date"] else None
        result.append(_Row(symbol, label, price, pct, day))
    return result


def _basis(rows: list[_Row]) -> str:
    days = {row.day for row in rows if row.valid and row.day}
    if len(days) > 1:
        return "시장일 혼재 · 항목별 날짜 확인"
    if not days:
        return "가격일 미확인 · 항목별 자료 확인"
    day = next(iter(days)).replace("-", ".")
    return f"가격일 {day} · 미국 동부 기준"


def _pct(row: _Row) -> str:
    if not row.valid:
        return "미확인"
    if abs(row.pct) >= 1000:
        return f"{row.pct:+.3g}%"
    return f"{row.pct:+.2f}%" if row.pct else "0.00%"


def _color(row: _Row) -> str:
    if not row.valid:
        return MUTED
    if row.symbol == "^VIX":
        return VIX_COLOR
    return UP if row.pct > 0 else DOWN if row.pct < 0 else INK


def _font():
    from matplotlib.font_manager import FontProperties
    # 실제 서버의 한글 지원 글꼴을 포함한다. 전역 rcParams는 변경하지 않는다.
    for name in (
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/home/user/.local/share/fonts/NotoSansKR.ttf",
    ):
        if Path(name).is_file():
            return FontProperties(fname=name)
    return None


class _Canvas:
    def __init__(self, height: int):
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        self.height = height
        self.figure = Figure(figsize=(WIDTH / DPI, height / DPI), dpi=DPI, facecolor=BG)
        FigureCanvasAgg(self.figure)
        self.font = _font()

    def text(self, x, y, value, size=28, color=INK, bold=False, align="left", primary=False):
        text = self.figure.text(x / WIDTH, 1 - y / self.height, value,
                                fontsize=size * 72 / DPI, color=color, va="top", ha=align,
                                fontproperties=self.font, fontweight="bold" if bold else "normal")
        if primary:
            text.set_gid("primary")
        return text

    def panel(self, x, y, width, height):
        from matplotlib.patches import FancyBboxPatch
        self.figure.add_artist(FancyBboxPatch(
            (x / WIDTH, 1 - (y + height) / self.height), width / WIDTH, height / self.height,
            boxstyle="round,pad=0.002,rounding_size=0.012", linewidth=.8,
            transform=self.figure.transFigure, facecolor="white", edgecolor="#DCE3ED"))

    def line(self, y):
        from matplotlib.lines import Line2D
        self.figure.add_artist(Line2D([36 / WIDTH, 684 / WIDTH], [1 - y / self.height] * 2,
                                     transform=self.figure.transFigure, color="#DCE3ED", linewidth=.8))

    def png(self):
        buffer = io.BytesIO()
        self.figure.savefig(buffer, format="png", dpi=DPI, facecolor=BG)
        buffer.seek(0)
        return buffer


def _overview(quotes):
    rows = _rows(quotes, INDEX_ORDER)
    canvas = _Canvas(1000)
    canvas.text(36, 30, "미국 장 마감 한눈에", 42, bold=True, primary=True)
    canvas.text(36, 90, _basis(rows), 28)
    equities = rows[:4]
    valid = [row for row in equities if row.valid]
    days = {row.day for row in valid}
    if not valid:
        summary = "지수 방향 미확인"
    elif len(days) > 1:
        summary = "시장일 달라 방향 비교 보류"
    elif None in days:
        summary = "기준일 확인 후 방향 비교"
    else:
        up = sum(row.pct > 0 for row in valid)
        down = sum(row.pct < 0 for row in valid)
        flat = len(valid) - up - down
        summary = f"주가지수 상승 {up} · 하락 {down} · 보합 {flat}"
    canvas.text(36, 143, summary, 32, bold=True, primary=True)
    canvas.text(36, 190, f"주가지수 확인 {len(valid)}/4 · VIX는 별도 변동성 지표", 26, MUTED)
    for i, row in enumerate(rows):
        top = 235 + i * 120
        canvas.panel(24, top, 672, 108)
        canvas.text(44, top + 14, row.label, 32, bold=True, primary=True)
        canvas.text(676, top + 9, _pct(row), 44 if row.valid else 32,
                    _color(row), bold=True, align="right", primary=True)
        canvas.text(44, top + 62, row.basis if row.valid else "자료 미확인", 26, MUTED)
        if row.valid:
            level = f"{row.price:,.2f}" if row.price < 1e10 else f"{row.price:.3g}"
            canvas.text(676, top + 64, f"수준 {level}", 28, MUTED, align="right")
    vix = rows[-1]
    if not vix.valid:
        vix_note = "VIX 자료 미확인 · 변동성 변화 판단 보류"
    else:
        direction = "상승" if vix.pct > 0 else "하락" if vix.pct < 0 else "보합"
        vix_note = f"VIX: 변동성 기대 {direction}"
    canvas.text(36, 865, vix_note, 28, VIX_COLOR)
    canvas.text(36, 908, "VIX 변화만으로 주가 방향을 단정하지 않습니다", 26, MUTED)
    canvas.text(36, 960, "Yahoo Finance · 등락률은 공급자 전일대비 값", 24, MUTED)
    return canvas.png()


def _detail(quotes):
    etfs, stocks = _rows(quotes, ETF_ORDER), _rows(quotes, STOCK_ORDER)
    rows = etfs + stocks
    mixed = len({row.day for row in rows if row.valid and row.day}) > 1
    canvas = _Canvas(1280)
    canvas.text(36, 27, "업종과 주요 종목", 42, bold=True, primary=True)
    canvas.text(36, 84, _basis(rows), 28)
    canvas.text(36, 129, "선정한 관찰 대상 · 전체 시장 순위 아님", 25, MUTED)
    for title, group, heading, start in (
        ("업종·테마 ETF 8개", etfs, 174, 225),
        ("대표 종목 8개", stocks, 704, 755),
    ):
        count = sum(row.valid for row in group)
        canvas.text(36, heading, title, 32, bold=True, primary=True)
        canvas.text(684, heading + 2, f"확인 {count}/8", 26, MUTED, align="right")
        for i, row in enumerate(group):
            y = start + i * 56
            show_date = row.valid and (mixed or not row.day)
            label_y = y if show_date else y + 10
            canvas.text(36, label_y, row.label, 30, primary=True)
            canvas.text(353, label_y + 1, row.symbol, 28, MUTED)
            canvas.text(684, label_y - 2, _pct(row), 34 if row.valid else 28,
                        _color(row), bold=True, align="right", primary=True)
            if show_date:
                canvas.text(36, y + 32, row.basis, 22, MUTED)
            canvas.line(y + 55)
    canvas.text(36, 1222, "+ 상승 / - 하락 · 미확인은 보합이 아닙니다", 24, MUTED)
    return canvas.png()


def generate_mobile_charts(quotes: Dict[str, Any], date_str: str = "",
                           avg_pct: float = 0.0) -> list[io.BytesIO]:
    """동일 조회 자료로 모바일 PNG 두 장. 날짜·방향은 행의 근거에서 계산한다.

    date_str/avg_pct는 호출 호환 인자다. 외부 평균이나 임의 날짜로
    자료 결측을 보합 마감으로 바꾸지 않는다.
    """
    try:
        data = quotes if isinstance(quotes, dict) else {}
        return [_overview(data), _detail(data)]
    except Exception:
        logger.exception("[차트] 모바일 미국 마감 이미지 생성 실패")
        return []


def generate_combined_chart(quotes: Dict[str, Any], stock_quotes: Dict[str, Any],
                            date_str: str = "", avg_pct: float = 0.0) -> Optional[io.BytesIO]:
    """기존 단일 이미지 소비자에는 핵심 지수 한 장을 제공한다."""
    return generate_us_market_chart(quotes, date_str, avg_pct)


def generate_us_market_chart(quotes: Dict[str, Any], date_str: str = "",
                             avg_pct: float = 0.0) -> Optional[io.BytesIO]:
    try:
        return _overview(quotes if isinstance(quotes, dict) else {})
    except Exception:
        logger.exception("[차트] 미국 지수 이미지 생성 실패")
        return None


def generate_sp500_map(stock_quotes: Dict[str, Any], date_str: str = "") -> Optional[io.BytesIO]:
    """이전 이름 호환. 고정 시총 맵 대신 관찰 대상의 실제 등락을 표시한다."""
    try:
        return _detail(stock_quotes if isinstance(stock_quotes, dict) else {})
    except Exception:
        logger.exception("[차트] 미국 관찰 종목 이미지 생성 실패")
        return None
