from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

from PIL import Image

from src.analytics.audience_reports import MarketSnapshot, render_market_report
from src.analytics.market_report_image import render_market_report_image


def sample():
    now = datetime(2026, 10, 29, 13, 10, tzinfo=ZoneInfo('Asia/Seoul'))
    rows = tuple({'label': label, 'price': 3000, 'change_pct': pct, 'source': 'kis',
                  'source_observed_at': None, 'received_at': now.isoformat()}
                 for label, pct in [('KOSPI', 1.2), ('KOSDAQ', -.5)])
    sectors = tuple({'name': name, 'r1': pct, 'source': 'kis', 'as_of': '2026-10-29'}
                    for name, pct in [('반도체', 1), ('은행', -2)])
    return render_market_report('midday', MarketSnapshot(indices=rows, sectors=sectors,
                                                         kr_basis_date='2026-10-29'), now)


def test_report_creates_valid_png_and_retains_all_body_lines():
    from html import unescape
    import re
    text = sample()
    result = render_market_report_image(text)
    assert result is not None
    with Image.open(BytesIO(result.png)) as image:
        assert image.format == 'PNG'
        assert image.size == (result.width, result.height)
        assert result.width == 720
    expected = unescape(re.sub(r'</?b>', '', text)).replace('🇰🇷 ', '')
    assert result.plain_text == expected
    assert '원천시각 미제공 · 전일 기준 거래일 미확인' in result.plain_text
    assert '전체 업종 순위 아님' in result.plain_text
    assert '출처: KIS' in result.plain_text
    assert '한국증시 장중시황' in result.caption
    assert len(result.caption.encode('utf-16-le')) // 2 <= 900


def test_unsafe_unknown_or_oversized_input_falls_back():
    text = sample()
    assert render_market_report_image(text.replace('<b>시장 판단</b>', '<script>시장 판단</script>')) is None
    assert render_market_report_image(text.replace('</b>', '', 1)) is None
    assert render_market_report_image(text.replace('시장 판단', '새로운 항목')) is None
    assert render_market_report_image(text + '\n' + '가' * 10000) is None
    assert render_market_report_image(text.replace('KOSPI', '🦄')) is None


def test_minute_observation_retains_unconfirmed_close_and_source():
    now = datetime(2026, 10, 29, 16, 40, tzinfo=ZoneInfo('Asia/Seoul'))
    row = {'code': '0001', 'price': 3000, 'source': 'KIS/FHKUP03500200',
           'source_observed_at': now.isoformat(), 'received_at': now.isoformat(),
           'observation_kind': 'historical_minute', 'finality': 'unverified'}
    text = render_market_report('after', MarketSnapshot(index_observations=(row,),
                                                        kr_basis_date='2026-10-29'), now)
    result = render_market_report_image(text)
    assert result is not None
    assert '확정 종가 아님 · 해당 분 관측값이며 봉 완료 여부 미확인' in result.plain_text
    assert '전일 대비 등락·시장 방향 판단자료는 미확보' in result.plain_text
    assert '출처: KIS' in result.plain_text


def test_rendered_text_is_inside_image_and_preserves_source_words(monkeypatch):
    from matplotlib.axes import Axes
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    artists = []
    original = Axes.text
    def capture(ax, *args, **kwargs):
        artist = original(ax, *args, **kwargs)
        artists.append(artist)
        return artist
    monkeypatch.setattr(Axes, 'text', capture)
    result = render_market_report_image(sample())
    assert result is not None
    renderer = artists[0].figure.canvas.get_renderer()
    for artist in artists:
        bounds = artist.get_window_extent(renderer)
        assert 0 <= bounds.x0 < bounds.x1 <= result.width
        assert 0 <= bounds.y0 < bounds.y1 <= result.height
    bounds = [artist.get_window_extent(renderer) for artist in artists]
    for i, left in enumerate(bounds):
        for right in bounds[i + 1:]:
            assert not (min(left.x1, right.x1) > max(left.x0, right.x0) + 0.5
                        and min(left.y1, right.y1) > max(left.y0, right.y0) + 0.5)
    drawn = ' '.join(a.get_text() for a in artists)
    for phrase in ('KOSPI 3,000.00 (+1.20%)', '원천시각 미제공', '전체 업종 순위', '출처: KIS'):
        assert phrase in drawn


def test_us_report_renders_signed_values_and_difference():
    now = datetime(2026, 10, 29, 7, 40, tzinfo=ZoneInfo('Asia/Seoul'))
    stamp = datetime(2026, 10, 28, 16, tzinfo=ZoneInfo('America/New_York')).timestamp()
    quotes = {symbol: {'price': 100, 'change_pct': pct, 'market_time': stamp}
              for symbol, pct in [('^GSPC', .28), ('^IXIC', .27), ('^SOX', 1.75)]}
    text = render_market_report('morning', MarketSnapshot(us_basis_date='2026-10-28', us_quotes=quotes), now)
    result = render_market_report_image(text)
    assert result is not None
    assert 'S&P500 +0.28%' in result.plain_text
    assert 'SOX−나스닥: +1.48%p' in result.plain_text
    assert 'Yahoo Finance' in result.plain_text


def test_height_overflow_and_invalid_entities_fall_back():
    text = sample()
    text = text.replace('자료 범위:', ('• 확인할 사항입니다.\n' * 48) + '자료 범위:')
    assert render_market_report_image(text) is None
    assert render_market_report_image(sample().replace('KOSPI', '&bogus;')) is None


def test_metric_values_align_and_domestic_receipt_times_remain_visible(monkeypatch):
    from matplotlib.axes import Axes
    drawn = []
    original = Axes.text
    def capture(ax, x, y, text, *args, **kwargs):
        artist = original(ax, x, y, text, *args, **kwargs)
        drawn.append((text, x, y, artist))
        return artist
    monkeypatch.setattr(Axes, 'text', capture)
    result = render_market_report_image(sample())
    assert result is not None
    by_text = {text: (x, y, artist) for text, x, y, artist in drawn}
    for text in ('KOSPI', 'KOSDAQ', '반도체', '은행', '3,000.00 (+1.20%)',
                 '3,000.00 (-0.50%)', '+1.00%', '-2.00%', '· 수신 13:10 KST'):
        assert text in by_text
    assert by_text['KOSPI'][0] == by_text['KOSDAQ'][0] == by_text['반도체'][0]
    assert by_text['3,000.00 (+1.20%)'][0] == by_text['3,000.00 (-0.50%)'][0] == by_text['+1.00%'][0]
    assert by_text['3,000.00 (+1.20%)'][0] > by_text['KOSPI'][0]
    renderer = by_text['KOSPI'][2].figure.canvas.get_renderer()
    assert by_text['KOSPI'][2].get_window_extent(renderer).x1 < by_text['3,000.00 (+1.20%)'][2].get_window_extent(renderer).x0


def test_unrecognized_metric_row_stays_a_complete_wrapped_row(monkeypatch):
    from matplotlib.axes import Axes
    drawn = []
    original = Axes.text
    def capture(ax, x, y, text, *args, **kwargs):
        drawn.append(text)
        return original(ax, x, y, text, *args, **kwargs)
    monkeypatch.setattr(Axes, 'text', capture)
    source = sample().replace('KOSPI 3,000.00 (+1.20%) · 수신 13:10 KST', 'KOSPI 확인 불가')
    result = render_market_report_image(source)
    assert result is not None
    assert 'KOSPI 확인 불가' in drawn


def test_height_and_transport_byte_limits_are_fail_closed(monkeypatch):
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    long_line = '• 관측 자료의 기준과 제한을 확인하는 반복 표본 문장입니다.\n'
    source = sample().replace('자료 범위:', long_line * 27 + '자료 범위:')
    assert len(source.splitlines()) < 50
    assert render_market_report_image(source) is None
    def oversized_png(canvas, target, *args, **kwargs):
        target.write(b'x' * (4 * 1024 * 1024 + 1))
    monkeypatch.setattr(FigureCanvasAgg, 'print_png', oversized_png)
    assert render_market_report_image(sample()) is None
