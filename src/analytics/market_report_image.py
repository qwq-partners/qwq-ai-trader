"""Offline, fail-closed visual rendering of the existing market-report body."""
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
import re

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.font_manager import FontProperties, fontManager
from matplotlib.ft2font import FT2Font
from matplotlib.patches import FancyBboxPatch

from .us_market_chart import _font


@dataclass(frozen=True)
class MarketReportImage:
    png: bytes
    caption: str
    width: int
    height: int
    plain_text: str


class _BodyParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.in_bold = False
        self.bad = False

    def handle_starttag(self, tag, attrs):
        if tag != 'b' or attrs or self.in_bold:
            self.bad = True
        self.in_bold = True
        self.parts.append(('open', ''))

    def handle_endtag(self, tag):
        if tag != 'b' or not self.in_bold:
            self.bad = True
        self.in_bold = False
        self.parts.append(('close', ''))

    def handle_data(self, data):
        self.parts.append(('data', data))

    def handle_comment(self, data):
        self.bad = True

    def handle_decl(self, decl):
        self.bad = True

    def unknown_decl(self, data):
        self.bad = True


def _parse(text):
    if not isinstance(text, str) or len(text) > 6000 or '\r' in text:
        return None
    # The producer emits only these entities. Unknown entities are not silently repaired.
    if re.search(r'&(?!amp;|lt;|gt;)', text):
        return None
    parser = _BodyParser()
    try:
        parser.feed(text)
        parser.close()
    except (ValueError, AssertionError):
        return None
    if parser.bad or parser.in_bold:
        return None
    # Reconstruct exact markup to reject stray angle brackets, malformed tags and attributes.
    from html import escape
    rebuilt = ''.join('<b>' if k == 'open' else '</b>' if k == 'close'
                      else escape(v, quote=False) for k, v in parser.parts)
    if rebuilt != text:
        return None
    lines = text.split('\n')
    if len(lines) > 50 or len(lines) < 8:
        return None
    titles = ('한국증시 모닝브리프', '한국증시 장중시황', '한국증시 마감점검')
    if lines[0] not in [f'🇰🇷 <b>{title}</b>' for title in titles]:
        return None
    if not re.fullmatch(r'\d{2}/\d{2} \d{2}:\d{2} KST', lines[1]):
        return None
    if lines[3] != '<b>시장 판단</b>' or '<b>국내 관전 포인트</b>' not in lines:
        return None
    if not lines[-2].startswith('자료 범위: ') or not lines[-1].startswith('출처: '):
        return None
    headings = [line[3:-4] for line in lines[3:] if line.startswith('<b>') and line.endswith('</b>')]
    if any(not (h in ('시장 판단', '국내 관전 포인트') or h.startswith('주요 지표 · ') or h.startswith('업종 흐름 · ')) for h in headings):
        return None
    if headings.count('시장 판단') != 1 or headings.count('국내 관전 포인트') != 1:
        return None
    # Bold is exclusively used for complete headings.
    if any(('<b>' in line or '</b>' in line) and not (line.startswith('<b>') and line.endswith('</b>')) for line in lines[1:]):
        return None
    from html import unescape
    return [unescape(line.replace('<b>', '').replace('</b>', '')) for line in lines]


def render_market_report_image(text: str) -> MarketReportImage | None:
    """Return an image only when every source line can be displayed faithfully."""
    lines = _parse(text)
    if lines is None:
        return None
    lines[0] = lines[0].removeprefix('🇰🇷 ')
    plain = '\n'.join(lines)
    try:
        font = _font()
        if font is None or not font.get_file():
            return None
        if not any(entry.fname == font.get_file() for entry in fontManager.ttflist):
            fontManager.addfont(font.get_file())
        prop = FontProperties(family=[font.get_name(), 'DejaVu Sans'])
        faces = [FT2Font(path) for path in fontManager._find_fonts_by_props(prop)]
        if any(not any(face.get_char_index(ord(c)) for face in faces)
               for c in plain if c not in '\n\t•'):
            return None
        judgment = []
        for line in lines[4:]:
            if not line:
                break
            judgment.append(line)
        if not judgment:
            return None
        caption = '\n'.join([lines[0], lines[1], *judgment])
        if len(caption.encode('utf-16-le')) // 2 > 900:
            return None
        fig = Figure(figsize=(7.2, 1), dpi=100)
        canvas = FigureCanvasAgg(fig)
        renderer = canvas.get_renderer()

        def measure(s, px):
            measured_prop = prop.copy()
            measured_prop.set_size(px * 72 / 100)
            return renderer.get_text_width_height_descent(s, measured_prop, False)[0]

        # Lines are measured before creating the figure, so no source text is clipped.
        def wrap(s, px, available):
            words = s.split(' ')
            out, current = [], ''
            for word in words:
                candidate = (current + ' ' + word) if current else word
                if measure(candidate, px) <= available:
                    current = candidate
                else:
                    if not current or measure(word, px) > available:
                        return None
                    out.append(current)
                    current = word
            if current:
                out.append(current)
            return out

        entries = []
        y = 42
        section = ''
        check_number = 0
        for i, line in enumerate(lines):
            if not line:
                y += 15
                continue
            if i == 0:
                kind, size, x, color = 'title', 40, 42, '#142943'
            elif i == 1:
                kind, size, x, color = 'date', 28, 42, '#52677d'
            elif line == '시장 판단' or line == '국내 관전 포인트' or line.startswith(('주요 지표 · ', '업종 흐름 · ')):
                kind, size, x, color = 'heading', 30, 42, '#194b71'
                section = line
                y += 12
            else:
                kind, size, x, color = 'body', 28, 52, '#172c40'
                if section == '국내 관전 포인트' and line.startswith('• '):
                    check_number += 1
                    x = 80
                if line.startswith(('자료 범위:', '출처:')):
                    color = '#465c6c'
            bullet = line.startswith('• ')
            display_line = line[2:] if bullet else line
            if bullet:
                x += 17
            wrapped = wrap(display_line, size, 720 - x - 42)
            if wrapped is None:
                return None
            height = len(wrapped) * (size + 10) + (12 if kind == 'heading' else 8)
            entries.append((kind, x, y, size, color, wrapped, bullet, section, check_number if section == '국내 관전 포인트' and line.startswith('• ') else 0))
            y += height
        height = y + 35
        if height > 3000:
            return None
        fig.set_size_inches(7.2, height / 100)
        ax = fig.add_axes([0, 0, 1, 1], xlim=(0, 720), ylim=(height, 0))
        ax.axis('off')
        fig.patch.set_facecolor('#f5f8fa')
        ax.add_patch(FancyBboxPatch((18, 18), 684, height - 36, boxstyle='round,pad=0,rounding_size=16', facecolor='white', edgecolor='#dce6ec', linewidth=1))
        for kind, x, top, size, color, wrapped, bullet, section, number in entries:
            if kind == 'body' and (bullet or section == '시장 판단'):
                box_height = len(wrapped) * (size + 10) + 4
                ax.add_patch(FancyBboxPatch((40, top - 4), 640, box_height,
                    boxstyle='round,pad=0,rounding_size=7',
                    facecolor='#f1f7fa' if section == '시장 판단' else '#f7fafc',
                    edgecolor='none'))
            if kind == 'heading':
                ax.plot([42, 678], [top - 5, top - 5], color='#dae6ed', linewidth=1)
            if bullet:
                ax.scatter([x - 11], [top + 13], s=15, color=color)
            if number:
                ax.text(47, top, str(number), fontsize=20 * 72 / 100, fontproperties=prop, color='#257f95', va='top')
            for j, part in enumerate(wrapped):
                ax.text(x, top + j * (size + 10), part, fontsize=size * 72 / 100,
                        fontproperties=prop, color=color, va='top')
        buffer = BytesIO()
        canvas.print_png(buffer)
        png = buffer.getvalue()
        if len(png) > 5_000_000:
            return None
        return MarketReportImage(png, caption, 720, height, plain)
    except (OSError, RuntimeError, ValueError, OverflowError):
        return None
