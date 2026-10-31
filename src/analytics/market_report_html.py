"""Web rendering of already-sent market reports: a pure view over stored rows.

The day page is a different presentation of the exact Telegram body that was
published (plus the frozen analysis JSON/evidence bundle when one exists). It
performs no market, news or model requests and never rewrites a consumed slot.
"""
import html
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
MARKET_KINDS = ("morning", "midday", "after")
_LABELS = {"morning": "장전 07:40", "midday": "장중 13:10", "after": "장후 16:40"}
_TITLES = {"morning": "한국증시 모닝브리프", "midday": "한국증시 장중시황", "after": "한국증시 마감점검"}
_STATUS = {"accepted": "API 수락", "attempted": "발송 중", "rejected": "거절", "unknown": "불명",
           "failed_validation": "검증 실패", "failed_configuration": "설정 오류", "skipped_holiday": "휴장",
           "skipped_missing_current_market_basis": "자료 보류", "skipped_missing_market_basis": "자료 보류"}
_WEEKDAY = "월화수목금토일"
_DEFAULT_BASE = "https://qwq.ai.kr"


_BASE_SHAPE = re.compile(r"^https://[A-Za-z0-9.-]+(?::\d+)?(?:/[\w./-]*)?$")


def web_base_url() -> str:
    """Empty or malformed REPORT_WEB_BASE_URL disables the link and the page files."""
    base = os.environ.get("REPORT_WEB_BASE_URL", _DEFAULT_BASE).strip().rstrip("/")
    return base if _BASE_SHAPE.match(base) else ""


def default_html_dir() -> Path:
    """Beside the delivery store, so a test store never writes under the real cache."""
    from .report_delivery import default_store_path
    return default_store_path().parent / "html"


def html_dir_for(store) -> Path:
    return Path(store.path).parent / "html"


def day_url(day: str, kind: str, base: str | None = None) -> str:
    return f"{web_base_url() if base is None else base}/reports/{day}#{kind}"


def with_web_link(text: str, day: str, kind: str) -> str:
    """Append the page link only when the whole message still passes the limits."""
    from .report_delivery import validate_report_body
    base = web_base_url()
    if not base or not isinstance(text, str):
        return text
    linked = f"{text}\n\n📄 전문·근거 보기: {day_url(day, kind, base)}"
    try:
        validate_report_body(linked)
    except ValueError:
        return text
    return linked


def report_file_name(name: str) -> str | None:
    """Only the two public file shapes; anything else is not a report page."""
    if name in ("", "index", "index.html"):
        return "index.html"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(\.html)?", name):
        return name if name.endswith(".html") else name + ".html"
    return None


# ---------------------------------------------------------------- body parsing

_TAG = re.compile(r"</?b>")
_FACT = r"(?: \[(?P<fact>F\d+)\])?$"
_KPI = re.compile(r"^• (?P<label>.+?) (?P<price>\d[\d,]*\.\d+)(?: \((?P<pct>[+-]\d+\.\d+)%\))?(?: · (?P<meta>.+?))?" + _FACT)
_PCT = re.compile(r"^• (?P<label>.+?) (?P<pct>[+-]\d+\.\d+)%" + _FACT)
_LINK_LINE = "📄 전문·근거 보기: "
_EVIDENCE = re.compile(r"^• (?P<id>E\d+) · (?P<rest>.+)$")


def _plain(line: str) -> str:
    """Telegram-HTML line → plain text (we re-escape on output)."""
    return html.unescape(_TAG.sub("", line))


def _esc(text) -> str:
    return html.escape(str(text), quote=True)


def _parse_body(body: str) -> dict:
    lines = body.split("\n")
    head = {"title": _plain(lines[0]) if lines else "", "time": _plain(lines[1]) if len(lines) > 1 else ""}
    sections, current = [], None
    for raw in lines[2:]:
        if raw.startswith("<b>") and raw.endswith("</b>"):
            current = {"heading": _plain(raw), "lines": []}
            sections.append(current)
        elif not raw.strip() or raw.startswith(_LINK_LINE):
            current = None  # Blank lines only separate sections; the page never links to itself.
        elif current is None:
            current = {"heading": "", "lines": []}  # 자료 범위/출처 trailer or lead-in text
            sections.append(current)
            current["lines"].append(_plain(raw))
        else:
            current["lines"].append(_plain(raw))
    return {"head": head, "sections": sections}


# ---------------------------------------------------------------- section views

def _card(heading: str, inner: str, extra_class: str = "") -> str:
    h2 = f"<h2>{_esc(heading)}</h2>" if heading else ""
    return f'<section class="card {extra_class}">{h2}{inner}</section>'


def _para(lines) -> str:
    return "".join(f"<p>{_esc(line)}</p>" for line in lines if line.strip())


def _badges(ids, kind: str) -> str:
    chips = []
    for ref in ids:
        ref = str(ref)
        if re.fullmatch(r"E\d+", ref):
            chips.append(f'<a class="badge" href="#{kind}-{ref}">{ref}</a>')
        elif re.fullmatch(r"F\d+", ref):
            chips.append(f'<span class="badge">{_esc(ref)} 시장 사실</span>')
    return f'<div class="badges">{"".join(chips)}</div>' if chips else ""


def _judgment(section, kind: str, analysis) -> str:
    thesis, refs, rest = None, [], []
    for line in section["lines"]:
        if line.startswith("LLM 해석: "):
            thesis = line[len("LLM 해석: "):]
        elif line.startswith("판단 근거: "):
            refs = [r.strip() for r in line[len("판단 근거: "):].split(",") if r.strip()]
        elif line.strip():
            rest.append(line)
    if thesis is None and isinstance(analysis, dict) and isinstance(analysis.get("thesis"), dict):
        thesis = str(analysis["thesis"].get("text", ""))
        refs = refs or list(analysis["thesis"].get("evidence_ids", []))
    inner = ""
    if thesis:
        inner += f'<p class="thesis">{_esc(thesis)}</p>'
        inner += _para(rest)
    else:
        if rest:
            inner += f'<p class="thesis muted">{_esc(rest[0])}</p>' + _para(rest[1:])
        if any("미확보" in line for line in rest):
            inner += '<div class="badges"><span class="badge warn">분석 미확보 · 사실 보고</span></div>'
    inner += _badges(refs, kind)
    return _card("한 줄 판단", inner)


def _issues(section, kind: str, analysis) -> str:
    issues = analysis.get("issues") if isinstance(analysis, dict) else None
    if isinstance(issues, list) and issues:
        parts = []
        for n, issue in enumerate(issues, 1):
            if not isinstance(issue, dict):
                continue
            rows = "".join(
                f'<dt class="{cls}">{label}</dt><dd>{_esc(issue.get(key, ""))}</dd>'
                for key, label, cls in (("event", "사건", ""), ("impact", "영향", ""),
                                        ("counterpoint", "반대", "c"), ("watch", "관찰", "")))
            parts.append(f'<div class="issue"><h3>{n}. {_esc(issue.get("title", ""))}</h3><dl class="flow">{rows}</dl>'
                         f'{_badges(issue.get("evidence_ids", []), kind)}</div>')
        return _card("핵심 이슈와 영향", "".join(parts))
    return _card(section["heading"], _para(section["lines"]))


def _fact_tag(fact) -> str:
    return f'<span class="tag">{_esc(fact)}</span>' if fact else ""


def _kpis(section, kind: str) -> str:
    cards, captions = [], []
    sub = section["heading"].split(" · ", 1)[1] if " · " in section["heading"] else ""
    for line in section["lines"]:
        m = _KPI.match(line)
        if m:
            pct = m.group("pct")
            cls = "up" if pct and pct.startswith("+") and float(pct) != 0 else "down" if pct and float(pct) < 0 else ""
            pct_html = f' <span class="pct {cls}">{_esc(pct)}%</span>' if pct else ""
            meta = m.group("meta") or ""
            if not pct:
                meta = f"{meta} · 전일 대비 미확보" if meta else "전일 대비 미확보"
            tag = '<span class="tag">확정 종가 아님</span>' if "분 단위" in sub or "참고" in sub else ""
            fact = _fact_tag(m.group("fact"))
            cards.append(f'<div class="kpi"><div class="l">{_esc(m.group("label"))}{tag}{fact}</div>'
                         f'<div class="v">{_esc(m.group("price"))}{pct_html}</div>'
                         f'<div class="s">{_esc(meta)}</div></div>')
            continue
        m = _PCT.match(line)
        if m:
            pct = m.group("pct")
            cls = "up" if float(pct) > 0 else "down" if float(pct) < 0 else ""
            cards.append(f'<div class="kpi"><div class="l">{_esc(m.group("label"))}{_fact_tag(m.group("fact"))}</div>'
                         f'<div class="v {cls}">{_esc(pct)}%</div><div class="s">{_esc(sub)}</div></div>')
            continue
        if line.strip():
            captions.append(line)
    inner = f'<div class="grid">{"".join(cards)}</div>' if cards else ""
    inner += "".join(f'<p class="cap">{_esc(c)}</p>' for c in captions)
    return _card("시장 지표" + (f" · {sub}" if sub else ""), inner)


def _sectors(section) -> str:
    rows, captions = [], []
    for line in section["lines"]:
        m = _PCT.match(line)
        if m:
            rows.append((m.group("label"), float(m.group("pct")), m.group("pct"), m.group("fact")))
        elif line.strip():
            captions.append(line)
    scale = max([abs(v) for _, v, _, _ in rows] + [0.01])
    bars = ""
    for label, value, text, fact in rows:
        cls = "up" if value > 0 else "down" if value < 0 else "flat"
        width = abs(value) / scale * 50
        bars += (f'<div class="n">{_esc(label)}{_fact_tag(fact)}</div><div class="t"><div class="f {cls}" style="width:{width:.1f}%"></div></div>'
                 f'<div class="v {cls}">{_esc(text)}%</div>')
    inner = f'<div class="bars" role="img" aria-label="업종 ETF 등락">{bars}</div>' if bars else ""
    inner += "".join(f'<p class="cap">{_esc(c)}</p>' for c in captions)
    if bars:
        inner += '<p class="cap">상승 빨강 / 하락 파랑</p>'
    return _card(section["heading"], inner)


def _watch(section) -> str:
    items = [line[2:] if line.startswith("• ") else line for line in section["lines"] if line.strip()]
    rows = "".join(f"<tr><td>{n}</td><td>{_esc(item)}</td></tr>" for n, item in enumerate(items, 1))
    table = f'<table class="cond"><tr><th>#</th><th>다음 세션에서 확인할 것</th></tr>{rows}</table>' if rows else ""
    return _card("확인 조건", table)


def _sources(section, kind: str) -> str:
    coverage, items, trailing = [], [], []
    for line in section["lines"]:
        if line.startswith("수집 범위: "):
            coverage += [c.strip() for c in line[len("수집 범위: "):].split(" / ") if c.strip()]
            continue
        m = _EVIDENCE.match(line)
        if m:
            parts = [p.strip() for p in m.group("rest").split(" · ")]
            items.append({"id": m.group("id"), "source": parts[0] if parts else "",
                          "title": parts[1] if len(parts) > 1 else "", "meta": parts[2:], "url": None})
            continue
        if line.startswith("원문: ") and items:
            items[-1]["url"] = line[len("원문: "):].strip()
            continue
        if line.strip():
            trailing.append(line)
    inner = ""
    if coverage:
        chips = "".join(f'<span class="badge{" warn" if any(w in c for w in ("미확보", "없음", "실패", "오류", "오래된", "준비 전")) else ""}">{_esc(c)}</span>' for c in coverage)
        inner += f'<div class="cov">{chips}</div>'
    if items:
        lis = ""
        for item in items:
            link = ""
            if item["url"] and re.match(r"^https?://[^\s<>\"']+$", item["url"]):
                link = f' · <a href="{_esc(item["url"])}" rel="noopener nofollow" target="_blank">원문</a>'
            elif item["url"] and item["url"].startswith(("http://", "https://")):
                link = f' · 원문 {_esc(item["url"])}'  # odd characters: text only, never an href; other schemes dropped'
            meta = " · ".join([item["source"], *item["meta"]])
            lis += (f'<li id="{kind}-{item["id"]}"><span class="id">{item["id"]}</span>{_esc(item["title"])}'
                    f'<span class="m">{_esc(meta)}{link}</span></li>')
        inner += f'<ul class="src">{lis}</ul>'
    inner += _para(trailing)
    return _card("근거 · 출처", inner)


def _render_slot(slot: dict, default: bool) -> str:
    kind, status, body = slot["kind"], slot.get("status"), slot.get("body")
    cls = "slot default" if default else "slot"
    label = _STATUS.get(status, "예정" if status is None else "미발송")
    if body and status != "accepted":
        label = f"{label} · 미발송 저장 본문"
    if not body or (status and status.startswith("skipped_")):
        reason = {"skipped_holiday": "휴장일이라 보고가 없습니다.",
                  "skipped_missing_current_market_basis": "원천 시점이 확인된 장중 지수 자료가 없어 보고를 생략했습니다. 수신 시각으로 대체하지 않습니다.",
                  "skipped_missing_market_basis": "원천 시점이 확인된 필수 자료가 없어 보고를 생략했습니다. 수신 시각으로 대체하지 않습니다.",
                  "failed_validation": "본문 형식·분량 검증에 실패해 발송하지 않았습니다."}.get(status, "이 슬롯의 보고는 아직 없습니다.")
        return (f'<article class="{cls}" id="{kind}"><p class="meta">{_esc(_LABELS[kind])} KST 슬롯 · {_esc(label)}</p>'
                f'{_card("보고 없음", f"<p class=empty>{_esc(reason)}</p>")}</article>')
    parsed = _parse_body(body)
    analysis = slot.get("analysis")
    out = [f'<article class="{cls}" id="{kind}">',
           f'<p class="meta">{_esc(_TITLES[kind])} · {_esc(parsed["head"]["time"])} 발송 · {_esc(label)}</p>']
    for section in parsed["sections"]:
        heading = section["heading"]
        if heading == "시장 판단":
            out.append(_judgment(section, kind, analysis))
        elif heading == "핵심 이슈와 영향":
            out.append(_issues(section, kind, analysis))
        elif heading.startswith("주요 지표"):
            out.append(_kpis(section, kind))
        elif heading.startswith("업종 흐름"):
            out.append(_sectors(section))
        elif heading == "국내 관전 포인트":
            out.append(_watch(section))
        elif heading == "근거 / 출처":
            out.append(_sources(section, kind))
        elif heading:
            out.append(_card(heading, _para(section["lines"])))
        else:
            out.append(f'<p class="note">{"<br>".join(_esc(l) for l in section["lines"] if l.strip())}</p>')
    out.append("</article>")
    return "".join(out)


_CSS = """
:root{--bg:#f6f7fa;--card:#fff;--ink:#1a1d26;--ink2:#555c6e;--mute:#8a92a6;--line:#e3e6ee;--up:#d64545;--down:#2f6fd6;--accent:#3b4bd6;--chip:#eef0f6;--hl:#fff6d6}
@media (prefers-color-scheme:dark){:root{--bg:#0f1117;--card:#171a23;--ink:#e7e9f0;--ink2:#aab1c3;--mute:#7b839a;--line:#262a37;--up:#ef6b6b;--down:#6b97f0;--accent:#8b96ff;--chip:#1f2330;--hl:#3a3410}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.65 -apple-system,"Pretendard Variable",Pretendard,"Apple SD Gothic Neo","Noto Sans KR",system-ui,sans-serif;font-feature-settings:"tnum"}
a{color:var(--accent)}.wrap{max-width:720px;margin:0 auto;padding:0 16px 48px}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line)}
.bar{max-width:720px;margin:0 auto;padding:10px 16px 0}
.brand{display:flex;justify-content:space-between;align-items:baseline;font-size:14px;color:var(--mute)}.brand b{color:var(--ink);font-size:15px}
.nav a{color:var(--mute);text-decoration:none;margin-left:10px}
h1{font-size:22px;margin:4px 0 8px;letter-spacing:-.01em}
.tabs{display:flex;gap:6px;margin:0 -16px;padding:0 16px 10px;overflow-x:auto;scrollbar-width:none}
.tabs a{flex:0 0 auto;padding:8px 14px;border-radius:999px;background:var(--chip);color:var(--ink2);text-decoration:none;font-size:15px;border:1px solid transparent}
.tabs a.off{color:var(--mute);text-decoration:line-through}.tabs a.live{border-color:var(--accent);color:var(--accent);font-weight:600}
.slot{display:none}.slot:target{display:block}.slot.default{display:block}body:has(.slot:target) .slot.default:not(:target){display:none}
section.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 18px 16px;margin:14px 0}
h2{font-size:14px;letter-spacing:.06em;color:var(--mute);margin:0 0 10px;font-weight:600}
.meta{font-size:14px;color:var(--mute);margin:10px 0 2px}
.thesis{font-size:21px;line-height:1.5;font-weight:600;margin:0 0 10px;letter-spacing:-.01em}.thesis.muted{color:var(--ink2);font-weight:500}
.badges{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.badge{font-size:12.5px;padding:2px 8px;border-radius:6px;background:var(--chip);color:var(--ink2);text-decoration:none;border:1px solid var(--line)}
.badge.warn{color:var(--up);border-color:color-mix(in srgb,var(--up) 40%,transparent)}
.issue{border-top:1px solid var(--line);padding-top:14px;margin-top:14px}.issue:first-child{border-top:0;padding-top:0;margin-top:0}
.issue h3{font-size:18px;margin:0 0 10px}
.flow{display:grid;grid-template-columns:62px 1fr;row-gap:10px;column-gap:10px;margin:0}.flow dt{font-size:13px;font-weight:700;color:var(--mute);padding-top:3px}.flow dt.c{color:var(--down)}.flow dd{margin:0}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.kpi{background:var(--chip);border-radius:10px;padding:12px}.kpi .l{font-size:13px;color:var(--mute)}.kpi .v{font-size:24px;font-weight:700;letter-spacing:-.02em;margin:2px 0}.kpi .s{font-size:13px;color:var(--ink2)}
.kpi .pct{font-size:15px;font-weight:600}.up{color:var(--up)}.down{color:var(--down)}
.tag{display:inline-block;font-size:11.5px;padding:1px 6px;border-radius:4px;background:var(--card);border:1px solid var(--line);color:var(--mute);margin-left:4px;vertical-align:middle}
.bars{display:grid;grid-template-columns:72px 1fr 68px;gap:6px 8px;align-items:center;font-size:15px}
.bars .n{text-align:right;color:var(--ink2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bars .t{position:relative;height:16px;background:var(--chip);border-radius:4px}.bars .t::before{content:"";position:absolute;left:50%;top:0;bottom:0;width:1px;background:var(--line)}
.bars .f{position:absolute;top:2px;bottom:2px;border-radius:3px}.bars .f.up{background:var(--up);left:50%}.bars .f.down{background:var(--down);right:50%}.bars .f.flat{display:none}
.bars .v{font-weight:600}
.cap{font-size:13.5px;color:var(--mute);margin:10px 0 0}
table.cond{width:100%;border-collapse:collapse;font-size:15.5px}
table.cond th{text-align:left;font-size:13px;color:var(--mute);font-weight:600;padding:0 10px 6px 0;border-bottom:1px solid var(--line)}
table.cond td{padding:10px 10px 10px 0;border-bottom:1px solid var(--line);vertical-align:top}
table.cond td:first-child,table.cond th:first-child{width:28px;color:var(--mute)}
.src{list-style:none;padding:0;margin:10px 0 0}.src li{padding:12px 0;border-top:1px solid var(--line);scroll-margin-top:130px}.src li:first-child{border-top:0}
.src li:target{background:var(--hl);margin:0 -10px;padding:12px 10px;border-radius:8px}
.src .id{font-weight:700;color:var(--accent);margin-right:6px}.src .m{display:block;font-size:13.5px;color:var(--mute);margin-top:3px}
.cov{display:flex;flex-wrap:wrap;gap:6px}
.note{font-size:14px;color:var(--mute);line-height:1.6}.empty{color:var(--mute);font-size:16px;margin:0}
section.card p{margin:0 0 8px}
table.index{width:100%;border-collapse:collapse;font-size:16px}table.index td{padding:12px 6px;border-bottom:1px solid var(--line)}table.index td:first-child a{font-weight:600;text-decoration:none}
@media (min-width:600px){.flow{grid-template-columns:72px 1fr}}
@media print{.slot{display:block!important}header{position:static}.tabs{display:none}a[href^="http"]::after{content:" (" attr(href) ")";font-size:11px;color:#777}}
"""


def _page(title: str, body: str, description: str, canonical: str) -> str:
    return (f'<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">'
            f'<title>{_esc(title)}</title><meta name="description" content="{_esc(description)}">'
            f'<meta property="og:title" content="{_esc(title)}"><meta property="og:description" content="{_esc(description)}">'
            f'<meta property="og:type" content="article"><link rel="canonical" href="{_esc(canonical)}">'
            f'<style>{_CSS}</style></head><body>{body}</body></html>')


def _day_title(day: str) -> str:
    d = datetime.strptime(day, "%Y-%m-%d").date()
    return f"{d.year}년 {d.month}월 {d.day}일 ({_WEEKDAY[d.weekday()]})"


def render_day_page(day: str, slots: list[dict], *, generated_at: datetime, neighbours: tuple[str | None, str | None] = (None, None), base: str | None = None) -> str:
    """slots: [{kind, status, body, analysis}] in MARKET_KINDS order; missing kinds allowed."""
    base = web_base_url() if base is None else base
    by_kind = {s["kind"]: s for s in slots}
    sent = [k for k in MARKET_KINDS if by_kind.get(k, {}).get("body") and by_kind[k].get("status") == "accepted"]
    shown = [k for k in MARKET_KINDS if by_kind.get(k, {}).get("body") and not str(by_kind[k].get("status", "")).startswith("skipped_")]
    default = (sent or shown or [None])[-1]
    tabs = ""
    for kind in MARKET_KINDS:
        slot = by_kind.get(kind, {"kind": kind, "status": None})
        status = slot.get("status")
        cls = "live" if kind == default else ("off" if (status is None or str(status).startswith("skipped_") or not slot.get("body")) else "")
        suffix = "" if cls != "off" else (" 휴장" if status == "skipped_holiday" else " 보류" if status else "")
        tabs += f'<a class="{cls}" href="#{kind}">{_esc(_LABELS[kind] + suffix)}</a>'
    prev_day, next_day = neighbours
    nav = ""
    if prev_day:
        nav += f'<a href="{_esc(base)}/reports/{_esc(prev_day)}">‹ {_esc(prev_day[5:].replace("-", "/"))}</a>'
    if next_day:
        nav += f'<a href="{_esc(base)}/reports/{_esc(next_day)}">{_esc(next_day[5:].replace("-", "/"))} ›</a>'
    nav += f'<a href="{_esc(base)}/reports/">아카이브</a>'
    articles = "".join(_render_slot(by_kind.get(kind, {"kind": kind, "status": None}), kind == default) for kind in MARKET_KINDS)
    stamp = generated_at.astimezone(KST).strftime("%Y/%m/%d %H:%M:%S KST")
    footer = (f'<p class="note">이 페이지는 텔레그램 시장 채널 정기 보고의 저장 본문과 동결 분석으로 자동 생성됩니다. 슬롯별 발송 상태는 각 탭 상단에 표시합니다. '
              f'투자 권유가 아니며 표시된 관측 표본과 인용 근거의 범위 안에서만 유효합니다. 생성 {stamp}.</p>')
    body = (f'<header><div class="bar"><div class="brand"><b>QWQ 시장 리포트</b><span class="nav">{nav}</span></div>'
            f'<h1>{_esc(_day_title(day))}</h1><nav class="tabs">{tabs}</nav></div></header>'
            f'<main class="wrap">{articles}{footer}</main>')
    description = "한국증시 장전·장중·장후 시장 리포트 (자동 생성, 투자 권유 아님)"
    return _page(f"QWQ 시장 리포트 · {day}", body, description, f"{base}/reports/{day}")


def render_index_page(days: list[dict], *, generated_at: datetime, base: str | None = None) -> str:
    """days: [{report_date, statuses: {kind: status}}] newest first."""
    base = web_base_url() if base is None else base
    rows = ""
    for entry in days:
        if entry["statuses"] and all(v == "skipped_holiday" for v in entry["statuses"].values()):
            continue
        chips = ""
        for kind in MARKET_KINDS:
            status = entry["statuses"].get(kind)
            label = _STATUS.get(status, "—" if status is None else "미발송")
            cls = "badge" if status == "accepted" else "badge warn" if status in ("rejected", "unknown", "failed_validation", "failed_configuration") else "badge"
            chips += f'<a class="{cls}" href="{_esc(base)}/reports/{_esc(entry["report_date"])}#{kind}">{_esc(_LABELS[kind].split()[0])} {_esc(label)}</a>'
        rows += f'<tr><td><a href="{_esc(base)}/reports/{_esc(entry["report_date"])}">{_esc(_day_title(entry["report_date"]))}</a></td><td><div class="badges" style="margin:0">{chips}</div></td></tr>'
    stamp = generated_at.astimezone(KST).strftime("%Y/%m/%d %H:%M:%S KST")
    body = (f'<header><div class="bar"><div class="brand"><b>QWQ 시장 리포트</b></div><h1>아카이브</h1></div></header>'
            f'<main class="wrap"><section class="card"><table class="index">{rows}</table></section>'
            f'<p class="note">자동 생성 · 투자 권유 아님 · 생성 {stamp}</p></main>')
    return _page("QWQ 시장 리포트 · 아카이브", body, "한국증시 시장 리포트 아카이브", f"{base}/reports/")


# ---------------------------------------------------------------- store → files

def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".html")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def collect_day(store, day: str) -> list[dict]:
    slots = []
    for kind in MARKET_KINDS:
        receipt = store.receipt("market", kind, day)
        analysis = store.analysis_receipt("market", kind, day) if receipt else None
        result = analysis.get("result") if isinstance(analysis, dict) and analysis.get("status") == "ready" else None
        slots.append({"kind": kind, "status": receipt["status"] if receipt else None,
                      "body": receipt.get("body") if receipt else None, "analysis": result})
    return slots


def write_web_pages(store, day: str, *, html_dir: Path | None = None, now: datetime | None = None) -> Path | None:
    """Regenerate the day page and archive index from stored rows only."""
    base = web_base_url()
    if not base:
        return None
    html_dir = Path(html_dir) if html_dir is not None else html_dir_for(store)
    now = now or datetime.now(KST)
    days = store.market_days()
    # Holiday-only days have no page; never link to them.
    dates = [d["report_date"] for d in days
             if not (d["statuses"] and all(v == "skipped_holiday" for v in d["statuses"].values()))]  # newest first
    index = dates.index(day) if day in dates else None
    neighbours = (dates[index + 1] if index is not None and index + 1 < len(dates) else None,
                  dates[index - 1] if index is not None and index > 0 else None)
    page = render_day_page(day, collect_day(store, day), generated_at=now, neighbours=neighbours, base=base)
    target = html_dir / f"{day}.html"
    _atomic_write(target, page)
    _atomic_write(html_dir / "index.html", render_index_page(days, generated_at=now, base=base))
    return target
