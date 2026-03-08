"""
미국증시 마감 리포트 — 차트 이미지 생성
통합 차트: 지수 카드 + 섹터 ETF 히트맵 + S&P 500 맵
단일 이미지로 통합 (16 × 20 인치, 130 DPI)
"""

from __future__ import annotations

import io
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# ── 섹터 ETF 메타 ─────────────────────────────────────────────────────────────
SECTOR_META: Dict[str, Dict] = {
    "XLK":  {"name": "Technology",     "weight": 29.0},
    "XLF":  {"name": "Financials",     "weight": 13.0},
    "XLV":  {"name": "Health Care",    "weight": 12.0},
    "XLY":  {"name": "Cons. Discret.", "weight": 11.0},
    "XLC":  {"name": "Comm. Svcs",     "weight":  9.0},
    "XLI":  {"name": "Industrials",    "weight":  8.0},
    "XLP":  {"name": "Cons. Staples",  "weight":  6.0},
    "XLE":  {"name": "Energy",         "weight":  4.0},
    "XLB":  {"name": "Materials",      "weight":  3.0},
    "XLRE": {"name": "Real Estate",    "weight":  2.5},
    "XLU":  {"name": "Utilities",      "weight":  2.5},
}

INDEX_ORDER = [
    ("^GSPC", "S&P 500"),
    ("^IXIC", "NASDAQ"),
    ("^DJI",  "DOW"),
    ("^RUT",  "Russell 2K"),
    ("^SOX",  "SOX"),
    ("^VIX",  "VIX"),
]

# ── 색상 ─────────────────────────────────────────────────────────────────────
BG       = "#0d1117"
DIVIDER  = "#30363d"
TEXT_PRI = "#e6edf3"
TEXT_SEC = "#8b949e"

# S&P500 개별 종목 (섹터당 상위 4~5개)
SP500_DISPLAY: Dict[str, list] = {
    "XLK":  [("AAPL","Apple",15.0),("MSFT","Microsoft",13.5),
              ("NVDA","NVIDIA",11.0),("AVGO","Broadcom",3.5),("ORCL","Oracle",2.0)],
    "XLF":  [("BRK-B","Berkshire",4.5),("JPM","JPMorgan",4.2),
              ("V","Visa",4.0),("MA","Mastercard",3.5),("BAC","BofA",2.0)],
    "XLV":  [("LLY","Lilly",5.0),("UNH","UnitedHlth",4.2),
              ("JNJ","J&J",2.5),("ABBV","AbbVie",2.2),("MRK","Merck",2.0)],
    "XLY":  [("AMZN","Amazon",8.5),("TSLA","Tesla",4.2),
              ("HD","Home Depot",2.2),("MCD","McDonald's",1.5)],
    "XLC":  [("GOOG","Alphabet",9.0),("META","Meta",7.5),
              ("NFLX","Netflix",2.5),("DIS","Disney",1.5)],
    "XLI":  [("GE","GE Aero",2.1),("CAT","Caterpillar",1.9),
              ("RTX","RTX",1.8),("UNP","Union Pac.",1.6),("HON","Honeywell",1.3)],
    "XLP":  [("WMT","Walmart",3.2),("COST","Costco",2.8),
              ("PG","P&G",2.6),("KO","Coca-Cola",2.0)],
    "XLE":  [("XOM","ExxonMobil",2.8),("CVX","Chevron",2.2),
              ("COP","ConocoPhil",1.3)],
    "XLB":  [("LIN","Linde",1.6),("SHW","Sherwin-W.",0.9),("APD","Air Prod.",0.7)],
    "XLRE": [("PLD","Prologis",0.9),("AMT","Amer. Tower",0.8),("EQIX","Equinix",0.6)],
    "XLU":  [("NEE","NextEra",1.0),("DUK","Duke En.",0.6),("SO","Southern",0.5)],
}


def _card_colors(pct: float):
    if pct >= 1.5:  return "#0d2818", "#2ea043", "#3fb950"
    if pct >= 0.3:  return "#0d2011", "#1a5c2a", "#26a641"
    if pct > -0.3:  return "#1c2128", "#30363d", "#8b949e"
    if pct > -1.5:  return "#2d1117", "#6e1c1c", "#f85149"
    return              "#3d0b0b",  "#da3633", "#ff7b72"


def _heat(pct: float) -> str:
    """등락률 → 레드↔그린 히트맵 색상"""
    c = max(-4.0, min(4.0, pct))
    if c >= 0:
        t = c / 4.0
        r = int(13  + (0   - 13 ) * t)
        g = int(27  + (190 - 27 ) * t)
        b = int(18  + (50  - 18 ) * t)
    else:
        t = (-c) / 4.0
        r = int(13  + (218 - 13 ) * t)
        g = int(27  + (54  - 27 ) * t)
        b = int(18  + (51  - 18 ) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _lum(h: str) -> float:
    h = h.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (0.299*r + 0.587*g + 0.114*b) / 255


def _setup_font():
    import os, matplotlib.font_manager as fm, matplotlib
    for fp in [
        "/home/user/.local/share/fonts/NotoSansKR.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]:
        if os.path.exists(fp):
            fm.fontManager.addfont(fp)
            p = fm.FontProperties(fname=fp)
            matplotlib.rcParams["font.family"] = p.get_name()
            matplotlib.rcParams["axes.unicode_minus"] = False
            return p
    return None


# ═════════════════════════════════════════════════════════════════════════════
# 통합 차트: 지수/섹터 히트맵 + S&P500 맵 — 단일 이미지
# ═════════════════════════════════════════════════════════════════════════════

def generate_combined_chart(
    quotes: Dict[str, Any],
    stock_quotes: Dict[str, Any],
    date_str: str = "",
    avg_pct: float = 0.0,
) -> Optional[io.BytesIO]:
    """
    통합 차트 생성
    - 상단 (60%): 지수 카드 6개 + 섹터 ETF 히트맵
    - 하단 (40%): S&P 500 개별 종목 맵
    """
    try:
        import matplotlib; matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mp
        import matplotlib.patheffects as pe
        import squarify

        _setup_font()

        FW, FH = 16, 22
        fig = plt.figure(figsize=(FW, FH), facecolor=BG, dpi=120)

        # ── 타이틀 ────────────────────────────────────────────────────────
        if avg_pct >= 1.0:   mood, mc = "▲  강세 마감", "#3fb950"
        elif avg_pct <= -1.0: mood, mc = "▼  약세 마감", "#ff7b72"
        else:                 mood, mc = "●  보합 마감", "#8b949e"

        fig.text(0.04, 0.990, f"US  미국증시 마감  —  {date_str}",
                 color=TEXT_PRI, fontsize=17, fontweight="bold", va="top")
        fig.text(0.96, 0.990, mood,
                 color=mc, fontsize=15, fontweight="bold", va="top", ha="right")

        fig.add_artist(plt.Line2D(
            [0.04, 0.96], [0.965, 0.965],
            transform=fig.transFigure, color=DIVIDER, linewidth=1.0))

        # ────────────────────────────────────────────────────────────────────
        # [상단] 지수 카드 (2행 × 3열)
        # 좌표계: figure 기준 (0~1)
        # 상단 영역: y = 0.63 ~ 0.96
        # ────────────────────────────────────────────────────────────────────
        CX0, CX1 = 0.04, 0.96
        CY0, CY1 = 0.645, 0.958
        COLS, ROWS = 3, 2
        PX, PY = 0.014, 0.014
        cw = (CX1 - CX0 - PX * (COLS - 1)) / COLS
        ch = (CY1 - CY0 - PY * (ROWS - 1)) / ROWS

        for i, (sym, label) in enumerate(INDEX_ORDER):
            row, col = divmod(i, COLS)
            cx = CX0 + col * (cw + PX)
            cy = CY1 - (row + 1) * ch - row * PY

            q     = quotes.get(sym, {})
            pct   = q.get("change_pct", 0.0)
            price = q.get("price", 0.0)
            bg_c, border_c, pct_c = _card_colors(pct)

            fig.add_artist(mp.FancyBboxPatch(
                (cx, cy), cw, ch,
                boxstyle="round,pad=0.004",
                transform=fig.transFigure,
                facecolor=bg_c, edgecolor=border_c,
                linewidth=1.8, clip_on=False, zorder=2,
            ))

            fig.text(cx + 0.013, cy + ch - 0.011,
                     label, color="#c9d1d9", fontsize=13, fontweight=700,
                     va="top", transform=fig.transFigure)

            sign  = "+" if pct > 0 else ""
            arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "●")
            t = fig.text(cx + cw / 2, cy + ch / 2 + ch * 0.08,
                         f"{arrow}  {sign}{pct:.2f}%",
                         color=pct_c, fontsize=26, fontweight=900,
                         ha="center", va="center", transform=fig.transFigure)
            t.set_path_effects([pe.withStroke(linewidth=3, foreground="#000000")])

            pstr = (f"{price:.2f}" if sym == "^VIX" else
                    (f"{price:,.0f}" if price >= 10000 else f"{price:,.2f}"))
            fig.text(cx + cw / 2, cy + 0.012,
                     pstr, color="#8b949e", fontsize=11, fontweight=700,
                     ha="center", va="bottom", transform=fig.transFigure,
                     fontfamily="monospace")

        # ────────────────────────────────────────────────────────────────────
        # [상단] 섹터 ETF 히트맵
        # axes 영역: x=0.04~0.96, y=0.39~0.635
        # ────────────────────────────────────────────────────────────────────
        fig.add_artist(plt.Line2D(
            [0.04, 0.96], [0.635, 0.635],
            transform=fig.transFigure, color=DIVIDER, linewidth=1.0))
        fig.text(0.04, 0.628, "S&P 500  Sector ETF",
                 color=TEXT_SEC, fontsize=10, va="top")

        ax_sec = fig.add_axes([0.04, 0.405, 0.92, 0.215], facecolor=BG)
        ax_sec.set_xlim(0, 100); ax_sec.set_ylim(0, 100); ax_sec.axis("off")

        sec_items = []
        for sym, meta in SECTOR_META.items():
            q   = quotes.get(sym, {})
            pct = q.get("change_pct", 0.0)
            sec_items.append({
                "sym": sym, "name": meta["name"],
                "weight": meta["weight"], "pct": pct,
                "color": _heat(pct),
            })

        sec_rects = squarify.squarify(
            squarify.normalize_sizes([s["weight"] for s in sec_items], 100, 100),
            x=0, y=0, dx=100, dy=100)

        # 1pt in axes coords (axes height ≈ 0.215 * 22in = 4.73in)
        PTS_SEC = 100 / (4.73 * 72)  # ≈ 0.294 data units per pt

        for rect, item in zip(sec_rects, sec_items):
            x, y, w, h = rect["x"], rect["y"], rect["dx"], rect["dy"]
            G = 0.5
            ax_sec.add_patch(mp.FancyBboxPatch(
                (x + G, y + G), w - G * 2, h - G * 2,
                boxstyle="round,pad=0.0",
                facecolor=item["color"], edgecolor=BG, linewidth=2.0, zorder=2))

            _stroke = [pe.withStroke(linewidth=5, foreground="#000000")]
            nfs   = max(8, min(42, min(w, h) * 1.35))
            pfs   = max(7, nfs / 2)
            name_h = nfs * PTS_SEC
            pct_h  = pfs * PTS_SEC
            gap    = max(0.5, min(w, h) * 0.05)
            grp_h  = name_h + gap + pct_h

            sign  = "+" if item["pct"] > 0 else ""
            pstr  = f"{sign}{item['pct']:.2f}%"

            if w > 5 and h > 5 and grp_h < h * 0.82:
                name_cy = y + h / 2 + (gap + pct_h) / 2
                pct_cy  = y + h / 2 - (name_h + gap) / 2
                t1 = ax_sec.text(x + w / 2, name_cy, item["name"],
                                 ha="center", va="center", color="#ffffff",
                                 fontsize=nfs, fontweight=900, zorder=3)
                t1.set_path_effects(_stroke)
                t2 = ax_sec.text(x + w / 2, pct_cy, pstr,
                                 ha="center", va="center", color="#ffffff",
                                 fontsize=pfs, fontweight=900, zorder=3)
                t2.set_path_effects(_stroke)
            elif w > 3 and h > 3:
                t3 = ax_sec.text(x + w / 2, y + h / 2, item["name"],
                                 ha="center", va="center", color="#ffffff",
                                 fontsize=nfs, fontweight=900, zorder=3)
                t3.set_path_effects(_stroke)

        # 섹터 히트맵 범례
        N = 40
        for i in range(N):
            ax_sec.add_patch(mp.Rectangle(
                (i * (100 / N), -7), 100 / N, 4,
                facecolor=_heat(-4.0 + i * (8.0 / N)), edgecolor="none", zorder=2))
        ax_sec.text(0,   -9, "−4%", ha="left",   va="top", color=TEXT_SEC, fontsize=9)
        ax_sec.text(50,  -9, "0",   ha="center", va="top", color=TEXT_SEC, fontsize=9)
        ax_sec.text(100, -9, "+4%", ha="right",  va="top", color=TEXT_SEC, fontsize=9)
        ax_sec.set_ylim(-14, 100)

        # ────────────────────────────────────────────────────────────────────
        # [하단] S&P 500 개별 종목 맵
        # axes 영역: x=0.01~0.99, y=0.025~0.385
        # ────────────────────────────────────────────────────────────────────
        fig.add_artist(plt.Line2D(
            [0.04, 0.96], [0.395, 0.395],
            transform=fig.transFigure, color=DIVIDER, linewidth=1.0))
        fig.text(0.04, 0.388, f"S&P 500  Map",
                 color=TEXT_PRI, fontsize=13, fontweight="bold", va="top")
        fig.text(0.96, 0.388, "size ∝ market cap   color = daily % change",
                 color=TEXT_SEC, fontsize=9, va="top", ha="right")

        ax_sp = fig.add_axes([0.01, 0.025, 0.98, 0.355], facecolor=BG)
        ax_sp.set_xlim(0, 100); ax_sp.set_ylim(0, 100); ax_sp.axis("off")

        sec_keys    = list(SP500_DISPLAY.keys())
        sec_weights = [SECTOR_META[k]["weight"] for k in sec_keys]

        sp_sec_rects = squarify.squarify(
            squarify.normalize_sizes(sec_weights, 100, 100),
            x=0, y=0, dx=100, dy=100)

        OG = 0.7
        IG = 0.4
        LH_RATIO = 0.14

        # 1pt in ax_sp coords (axes height ≈ 0.355 * 22in = 7.81in)
        PTS_SP = 100 / (7.81 * 72)   # ≈ 0.178 data units per pt

        for sec_rect, sec_key in zip(sp_sec_rects, sec_keys):
            SX  = sec_rect["x"]  + OG
            SY  = sec_rect["y"]  + OG
            SDX = sec_rect["dx"] - OG * 2
            SDY = sec_rect["dy"] - OG * 2
            if SDX < 1 or SDY < 1:
                continue

            ax_sp.add_patch(mp.FancyBboxPatch(
                (SX, SY), SDX, SDY,
                boxstyle="square,pad=0",
                facecolor="#1c2128", edgecolor="#21262d",
                linewidth=1.2, zorder=1))

            LH = max(min(SDY * LH_RATIO, 5.5), 2.8)
            ax_sp.text(SX + SDX * 0.5, SY + SDY - LH * 0.45,
                       SECTOR_META[sec_key]["name"],
                       ha="center", va="center", color=TEXT_PRI,
                       fontsize=max(6.5, min(11, SDX * 0.50)),
                       fontweight="bold", zorder=4, clip_on=True)

            stocks      = SP500_DISPLAY[sec_key]
            weights     = [w for _, _, w in stocks]
            IH          = SDY - LH
            if IH < 1:
                continue

            stock_rects = squarify.squarify(
                squarify.normalize_sizes(weights, SDX, IH),
                x=SX, y=SY, dx=SDX, dy=IH)

            _stk = [pe.withStroke(linewidth=4, foreground="#000000")]
            for sr, (sym, dname, _) in zip(stock_rects, stocks):
                IX  = sr["x"]  + IG
                IY  = sr["y"]  + IG
                IDX = sr["dx"] - IG * 2
                IDY = sr["dy"] - IG * 2
                if IDX < 0.8 or IDY < 0.8:
                    continue

                q    = stock_quotes.get(sym, {})
                pct  = q.get("change_pct", 0.0)
                clr  = _heat(pct)

                ax_sp.add_patch(mp.FancyBboxPatch(
                    (IX, IY), IDX, IDY,
                    boxstyle="round,pad=0.0",
                    facecolor=clr, edgecolor=BG,
                    linewidth=1.5, zorder=2))

                sign  = "+" if pct > 0 else ""
                pstr  = f"{sign}{pct:.1f}%"
                md    = min(IDX, IDY)

                tfs   = max(7, min(38, md * 1.35))
                pfs   = max(6, tfs / 2)
                th    = tfs * PTS_SP
                ph    = pfs * PTS_SP
                gap2  = max(0.3, md * 0.04)
                grph  = th + gap2 + ph

                if IDX > 3 and IDY > 3 and grph < IDY * 0.82:
                    name_cy = IY + IDY / 2 + (gap2 + ph) / 2
                    pct_cy  = IY + IDY / 2 - (th + gap2) / 2
                    t1 = ax_sp.text(IX + IDX / 2, name_cy, sym,
                                    ha="center", va="center", color="#ffffff",
                                    fontsize=tfs, fontweight=900, zorder=3, clip_on=True)
                    t1.set_path_effects(_stk)
                    t2 = ax_sp.text(IX + IDX / 2, pct_cy, pstr,
                                    ha="center", va="center", color="#ffffff",
                                    fontsize=pfs, fontweight=900, zorder=3, clip_on=True)
                    t2.set_path_effects(_stk)
                elif IDX > 2 and IDY > 2:
                    t3 = ax_sp.text(IX + IDX / 2, IY + IDY / 2, sym,
                                    ha="center", va="center", color="#ffffff",
                                    fontsize=tfs, fontweight=900, zorder=3, clip_on=True)
                    t3.set_path_effects(_stk)

        # S&P500 색상 범례
        N2 = 50
        for i in range(N2):
            p = -4.0 + i * (8.0 / N2)
            fig.add_axes([0.03 + i * (0.44 / N2), 0.003, 0.44 / N2, 0.016],
                         facecolor=_heat(p)).set_axis_off()
        fig.text(0.03,  0.022, "−4%", color=TEXT_SEC, fontsize=9, va="bottom")
        fig.text(0.25,  0.022, "0",   color=TEXT_SEC, fontsize=9, va="bottom", ha="center")
        fig.text(0.475, 0.022, "+4%", color=TEXT_SEC, fontsize=9, va="bottom")

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                    facecolor=BG, edgecolor="none")
        plt.close(fig)
        buf.seek(0)
        logger.info("[차트] 통합 차트 생성 완료 (지수/섹터+S&P500 맵)")
        return buf

    except Exception as e:
        logger.error(f"[차트] 통합 차트 생성 실패: {e}", exc_info=True)
        return None


# ═════════════════════════════════════════════════════════════════════════════
# 하위 호환: 개별 차트 함수 (기존 코드에서 import 시 오류 방지)
# ═════════════════════════════════════════════════════════════════════════════

def generate_us_market_chart(
    quotes: Dict[str, Any],
    date_str: str = "",
    avg_pct: float = 0.0,
) -> Optional[io.BytesIO]:
    """지수 카드 + 섹터 ETF 히트맵 (단독)"""
    try:
        import matplotlib; matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mp
        import matplotlib.patheffects as pe
        import squarify

        _setup_font()

        FW, FH = 16, 10
        fig = plt.figure(figsize=(FW, FH), facecolor=BG, dpi=130)

        if avg_pct >= 1.0:   mood, mc = "▲  강세 마감", "#3fb950"
        elif avg_pct <= -1.0: mood, mc = "▼  약세 마감", "#ff7b72"
        else:                 mood, mc = "●  보합 마감", "#8b949e"

        fig.text(0.04, 0.967, f"미국증시 마감  —  {date_str}",
                 color=TEXT_PRI, fontsize=17, fontweight="bold", va="top")
        fig.text(0.96, 0.967, mood,
                 color=mc, fontsize=15, fontweight="bold", va="top", ha="right")
        fig.add_artist(plt.Line2D([0.04, 0.96], [0.932, 0.932],
                                  transform=fig.transFigure,
                                  color=DIVIDER, linewidth=1.0))

        CX0, CX1 = 0.04, 0.96
        CY0, CY1 = 0.595, 0.925
        COLS, ROWS = 3, 2
        PX, PY = 0.014, 0.014
        cw = (CX1 - CX0 - PX * (COLS - 1)) / COLS
        ch = (CY1 - CY0 - PY * (ROWS - 1)) / ROWS

        for i, (sym, label) in enumerate(INDEX_ORDER):
            row, col = divmod(i, COLS)
            cx = CX0 + col * (cw + PX)
            cy = CY1 - (row + 1) * ch - row * PY
            q = quotes.get(sym, {})
            pct   = q.get("change_pct", 0.0)
            price = q.get("price", 0.0)
            bg_c, border_c, pct_c = _card_colors(pct)
            fig.add_artist(mp.FancyBboxPatch(
                (cx, cy), cw, ch,
                boxstyle="round,pad=0.004", transform=fig.transFigure,
                facecolor=bg_c, edgecolor=border_c, linewidth=1.8, clip_on=False, zorder=2))
            fig.text(cx + 0.013, cy + ch - 0.011, label,
                     color="#c9d1d9", fontsize=13, fontweight=700, va="top",
                     transform=fig.transFigure)
            sign  = "+" if pct > 0 else ""
            arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "●")
            t = fig.text(cx + cw/2, cy + ch/2 + ch*0.08,
                         f"{arrow}  {sign}{pct:.2f}%",
                         color=pct_c, fontsize=28, fontweight=900,
                         ha="center", va="center", transform=fig.transFigure)
            t.set_path_effects([pe.withStroke(linewidth=3, foreground="#000000")])
            pstr = (f"{price:.2f}" if sym == "^VIX" else
                    (f"{price:,.0f}" if price >= 10000 else f"{price:,.2f}"))
            fig.text(cx + cw/2, cy + 0.012, pstr,
                     color="#8b949e", fontsize=12, fontweight=700,
                     ha="center", va="bottom", transform=fig.transFigure,
                     fontfamily="monospace")

        fig.add_artist(plt.Line2D([0.04, 0.96], [0.582, 0.582],
                                  transform=fig.transFigure, color=DIVIDER, linewidth=1.0))
        fig.text(0.04, 0.572, "S&P 500  Sector ETF",
                 color=TEXT_SEC, fontsize=10, va="top")

        ax = fig.add_axes([0.04, 0.045, 0.92, 0.515], facecolor=BG)
        ax.set_xlim(0, 100); ax.set_ylim(0, 100); ax.axis("off")

        sec_items = []
        for sym, meta in SECTOR_META.items():
            q   = quotes.get(sym, {})
            pct = q.get("change_pct", 0.0)
            sec_items.append({"sym": sym, "name": meta["name"],
                               "weight": meta["weight"], "pct": pct,
                               "color": _heat(pct)})

        rects = squarify.squarify(
            squarify.normalize_sizes([s["weight"] for s in sec_items], 100, 100),
            x=0, y=0, dx=100, dy=100)

        for rect, item in zip(rects, sec_items):
            x, y, w, h = rect["x"], rect["y"], rect["dx"], rect["dy"]
            G = 0.5
            ax.add_patch(mp.FancyBboxPatch(
                (x+G, y+G), w-G*2, h-G*2,
                boxstyle="round,pad=0.0",
                facecolor=item["color"], edgecolor=BG, linewidth=2.0, zorder=2))
            sign = "+" if item["pct"] > 0 else ""
            pstr = f"{sign}{item['pct']:.2f}%"
            PTS  = 0.270
            _stroke = [pe.withStroke(linewidth=5, foreground="#000000")]
            nfs  = max(8, min(42, min(w, h) * 1.35))
            pfs  = max(7, nfs / 2)
            name_h = nfs * PTS
            pct_h  = pfs * PTS
            gap    = max(0.5, min(w, h) * 0.05)
            grp_h  = name_h + gap + pct_h
            if w > 5 and h > 5 and grp_h < h * 0.82:
                name_cy = y + h/2 + (gap + pct_h) / 2
                pct_cy  = y + h/2 - (name_h + gap) / 2
                t1 = ax.text(x+w/2, name_cy, item["name"],
                             ha="center", va="center", color="#ffffff",
                             fontsize=nfs, fontweight=900, zorder=3)
                t1.set_path_effects(_stroke)
                t2 = ax.text(x+w/2, pct_cy, pstr,
                             ha="center", va="center", color="#ffffff",
                             fontsize=pfs, fontweight=900, zorder=3)
                t2.set_path_effects(_stroke)
            elif w > 3 and h > 3:
                t3 = ax.text(x+w/2, y+h/2, item["name"],
                             ha="center", va="center", color="#ffffff",
                             fontsize=nfs, fontweight=900, zorder=3)
                t3.set_path_effects(_stroke)

        N = 40
        for i in range(N):
            ax.add_patch(mp.Rectangle(
                (i*(100/N), -7), 100/N, 4,
                facecolor=_heat(-4.0 + i*(8.0/N)), edgecolor="none", zorder=2))
        ax.text(0,   -9, "−4%", ha="left",   va="top", color=TEXT_SEC, fontsize=9)
        ax.text(50,  -9, "0",   ha="center", va="top", color=TEXT_SEC, fontsize=9)
        ax.text(100, -9, "+4%", ha="right",  va="top", color=TEXT_SEC, fontsize=9)
        ax.set_ylim(-14, 100)

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                    facecolor=BG, edgecolor="none")
        plt.close(fig)
        buf.seek(0)
        logger.info("[차트] 지수/섹터 ETF 차트 생성 완료")
        return buf

    except Exception as e:
        logger.error(f"[차트] 생성 실패: {e}", exc_info=True)
        return None


def generate_sp500_map(
    stock_quotes: Dict[str, Any],
    date_str: str = "",
) -> Optional[io.BytesIO]:
    """S&P 500 개별 종목 히트맵 (단독)"""
    try:
        import matplotlib; matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mp
        import matplotlib.patheffects as pe
        import squarify

        _setup_font()

        FW, FH = 16, 10
        fig = plt.figure(figsize=(FW, FH), facecolor=BG, dpi=130)

        fig.text(0.03, 0.97, f"S&P 500  Map  —  {date_str}",
                 color=TEXT_PRI, fontsize=17, fontweight="bold", va="top")
        fig.text(0.97, 0.97, "size ∝ market cap   color = daily % change",
                 color=TEXT_SEC, fontsize=10, va="top", ha="right")

        ax = fig.add_axes([0.01, 0.03, 0.98, 0.90], facecolor=BG)
        ax.set_xlim(0, 100); ax.set_ylim(0, 100); ax.axis("off")

        sec_keys    = list(SP500_DISPLAY.keys())
        sec_weights = [SECTOR_META[k]["weight"] for k in sec_keys]

        sec_rects = squarify.squarify(
            squarify.normalize_sizes(sec_weights, 100, 100),
            x=0, y=0, dx=100, dy=100)

        OG = 0.7; IG = 0.4; LH_RATIO = 0.14

        for sec_rect, sec_key in zip(sec_rects, sec_keys):
            SX  = sec_rect["x"]  + OG
            SY  = sec_rect["y"]  + OG
            SDX = sec_rect["dx"] - OG * 2
            SDY = sec_rect["dy"] - OG * 2
            if SDX < 1 or SDY < 1:
                continue
            ax.add_patch(mp.FancyBboxPatch(
                (SX, SY), SDX, SDY,
                boxstyle="square,pad=0",
                facecolor="#1c2128", edgecolor="#21262d",
                linewidth=1.2, zorder=1))
            LH = max(min(SDY * LH_RATIO, 5.5), 2.8)
            ax.text(SX + SDX*0.5, SY + SDY - LH*0.45,
                    SECTOR_META[sec_key]["name"],
                    ha="center", va="center", color=TEXT_PRI,
                    fontsize=max(6.5, min(11, SDX * 0.50)),
                    fontweight="bold", zorder=4, clip_on=True)

            stocks      = SP500_DISPLAY[sec_key]
            weights     = [w for _, _, w in stocks]
            IH          = SDY - LH
            if IH < 1:
                continue

            stock_rects = squarify.squarify(
                squarify.normalize_sizes(weights, SDX, IH),
                x=SX, y=SY, dx=SDX, dy=IH)

            PTS2  = 0.154
            _stk  = [pe.withStroke(linewidth=4, foreground="#000000")]
            for sr, (sym, dname, _) in zip(stock_rects, stocks):
                IX  = sr["x"]  + IG; IY  = sr["y"]  + IG
                IDX = sr["dx"] - IG * 2; IDY = sr["dy"] - IG * 2
                if IDX < 0.8 or IDY < 0.8:
                    continue
                q    = stock_quotes.get(sym, {})
                pct  = q.get("change_pct", 0.0)
                clr  = _heat(pct)
                ax.add_patch(mp.FancyBboxPatch(
                    (IX, IY), IDX, IDY,
                    boxstyle="round,pad=0.0",
                    facecolor=clr, edgecolor=BG, linewidth=1.5, zorder=2))
                sign  = "+" if pct > 0 else ""
                pstr  = f"{sign}{pct:.1f}%"
                md    = min(IDX, IDY)
                tfs   = max(7, min(38, md * 1.35))
                pfs   = max(6, tfs / 2)
                th    = tfs * PTS2; ph = pfs * PTS2
                gap2  = max(0.3, md * 0.04)
                grph  = th + gap2 + ph
                if IDX > 3 and IDY > 3 and grph < IDY * 0.82:
                    name_cy = IY + IDY/2 + (gap2 + ph) / 2
                    pct_cy  = IY + IDY/2 - (th + gap2) / 2
                    t1 = ax.text(IX+IDX/2, name_cy, sym, ha="center", va="center",
                                 color="#ffffff", fontsize=tfs, fontweight=900, zorder=3, clip_on=True)
                    t1.set_path_effects(_stk)
                    t2 = ax.text(IX+IDX/2, pct_cy, pstr, ha="center", va="center",
                                 color="#ffffff", fontsize=pfs, fontweight=900, zorder=3, clip_on=True)
                    t2.set_path_effects(_stk)
                elif IDX > 2 and IDY > 2:
                    t3 = ax.text(IX+IDX/2, IY+IDY/2, sym, ha="center", va="center",
                                 color="#ffffff", fontsize=tfs, fontweight=900, zorder=3, clip_on=True)
                    t3.set_path_effects(_stk)

        N = 50
        for i in range(N):
            p = -4.0 + i * (8.0/N)
            fig.add_axes([0.03 + i*(0.44/N), 0.005, 0.44/N, 0.018],
                         facecolor=_heat(p)).set_axis_off()
        fig.text(0.03,  0.027, "−4%", color=TEXT_SEC, fontsize=9, va="bottom")
        fig.text(0.25,  0.027, "0",   color=TEXT_SEC, fontsize=9, va="bottom", ha="center")
        fig.text(0.475, 0.027, "+4%", color=TEXT_SEC, fontsize=9, va="bottom")

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                    facecolor=BG, edgecolor="none")
        plt.close(fig)
        buf.seek(0)
        logger.info("[차트] S&P500 맵 생성 완료")
        return buf

    except Exception as e:
        logger.error(f"[차트] S&P500 맵 생성 실패: {e}", exc_info=True)
        return None
