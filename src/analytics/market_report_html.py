"""Web-native daily market report page: a pure view over stored rows.

Inputs per slot are the delivery status (``slots``), the frozen web analysis row
(``report_analyses`` audience ``web``: observed facts M#, validated analysis JSON,
public evidence E#) and, for days before the web report existed, the stored
Telegram body whose metric lines are read back as facts. No market, news or
model requests happen here and consumed rows are never rewritten.
"""
import html
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .market_report_analysis import _display_note, _display_time
from .web_report import fact_lines

KST = ZoneInfo("Asia/Seoul")
MARKET_KINDS = ("morning", "midday", "after")
_LABELS = {"morning": "장전 07:40", "midday": "장중 13:10", "after": "장후 16:40"}
_TITLES = {"morning": "모닝브리프", "midday": "장중시황", "after": "마감점검"}
_STATUS = {"accepted": "텔레그램 발송", "attempted": "발송 중", "rejected": "발송 거절", "unknown": "발송 불명",
           "failed_validation": "검증 실패", "failed_configuration": "설정 오류", "skipped_holiday": "휴장",
           "skipped_missing_current_market_basis": "자료 보류", "skipped_missing_market_basis": "자료 보류"}
_REASON = {None: "분석을 생성하지 못했습니다.",
           "research_evidence_missing": "뉴스·공시·일정 근거가 수집되지 않아 분석을 생성하지 않았습니다.",
           "invalid_evidence": "수집 근거 형식이 맞지 않아 분석을 생성하지 않았습니다.",
           "no_event_cited": "모델 응답이 공개 사건을 인용하지 않아 채택하지 않았습니다.",
           "invalid_output": "모델 응답이 근거 검증을 통과하지 못해 채택하지 않았습니다.",
           "incomplete": "모델 응답이 중간에 끊겨 채택하지 않았습니다.",
           "refused": "모델이 응답을 거절했습니다.",
           "provider_failed": "모델 호출이 실패했습니다.", "provider_exception": "모델 호출이 실패했습니다.",
           "manager_unavailable": "모델 연결이 준비되지 않았습니다.", "prompt_unavailable": "입력이 한도를 넘어 분석을 생성하지 않았습니다.",
           "interrupted": "이전 처리가 중단되어 재시도하지 않았습니다.", "cancelled": "처리가 중단되었습니다.",
           "no_web_row": "이 보고에는 웹 분석 행이 없습니다(웹 리포트 도입 전 발송이거나 생성을 건너뛴 경우). 발송 본문의 관측 자료만 보여 드립니다."}
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


# ---------------------------------------------------------------- legacy body → facts

_TAG = re.compile(r"</?b>")
_FACT_TAIL = r"(?: \[F\d+\])?$"
_KPI = re.compile(r"^• (?P<label>.+?) (?P<price>\d[\d,]*\.\d+)(?: \((?P<pct>[+-]\d+\.\d+)%\))?(?: · (?P<meta>.+?))?" + _FACT_TAIL)
_PCT = re.compile(r"^• (?P<label>.+?) (?P<pct>[+-]\d+\.\d+)%" + _FACT_TAIL)


def _plain(line: str) -> str:
    return html.unescape(_TAG.sub("", line))


def sent_time_of(body: str | None) -> str:
    lines = (body or "").split("\n")
    return _plain(lines[1]) if len(lines) > 1 else ""


def facts_from_body(body: str) -> dict:
    """Read the metric lines of a stored Telegram body back as M# facts (pre-web days only)."""
    facts = {"version": "legacy-body", "indices": [], "us": [], "sectors": [], "basis_date": None, "us_basis_date": None}
    heading, count = "", 0
    for raw in body.split("\n"):
        if raw.startswith("<b>") and raw.endswith("</b>"):
            heading = _plain(raw)
            if heading.startswith("업종 흐름 · ") and re.search(r"\d{4}-\d{2}-\d{2}$", heading):
                facts["basis_date"] = heading[-10:]
            if heading.startswith("주요 지표 · 미국 "):
                facts["us_basis_date"] = heading.rsplit(" ", 1)[-1]
            continue
        line = _plain(raw)
        if heading.startswith("주요 지표") and "미국" not in heading and (m := _KPI.match(line)):
            count += 1
            basis = "indicative_minute" if "분 단위" in heading else "provider_reference" if "참고" in heading else "close_vs_prev"
            facts["indices"].append({"id": f"M{count}", "label": m["label"], "price": float(m["price"].replace(",", "")),
                                     "pct": float(m["pct"]) if m["pct"] else None, "observed_at": None,
                                     "observed_text": m["meta"] or "", "source": "KIS", "basis": basis})
        elif heading.startswith("주요 지표 · 미국") and (m := _PCT.match(line)):
            count += 1
            facts["us"].append({"id": f"M{count}", "label": m["label"], "pct": float(m["pct"]), "date": facts["us_basis_date"], "source": "Yahoo Finance"})
        elif heading.startswith("업종 흐름") and (m := _PCT.match(line)):
            count += 1
            facts["sectors"].append({"id": f"M{count}", "name": m["label"], "pct": float(m["pct"]), "as_of": facts["basis_date"], "source": "KIS"})
    return facts


# ---------------------------------------------------------------- rendering helpers

def _esc(text) -> str:
    return html.escape(str(text), quote=True)


def _sign(value: float | None) -> str:
    if value is None:
        return "flat"
    rounded = float(f"{value:.2f}")
    return "up" if rounded > 0 else "down" if rounded < 0 else "flat"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}%"


def _when(row: dict) -> str:
    if row.get("observed_text"):
        return row["observed_text"]
    try:
        return datetime.fromisoformat(row["observed_at"]).astimezone(KST).strftime("%m/%d %H:%M KST")
    except (TypeError, ValueError, KeyError):
        return "시각 미확인"


_BASIS_TAG = {"close_vs_prev": "전일 종가 대비", "intraday": "장중 관측", "provider_reference": "제공기관 대비율 · 원천시각 미제공",
              "indicative_minute": "분 단위 참고 · 확정 종가 아님"}


def _cite(ids, kind: str) -> str:
    ids = [i for i in (ids or []) if isinstance(i, str) and re.fullmatch(r"[EM]\d+", i)]
    if not ids:
        return ""
    return '<span class="cite">' + "".join(f'<a href="#{kind}-{i}">{i}</a>' for i in ids) + "</span>"


def _p(text, ids, kind) -> str:
    return f"<p>{_esc(text)}{_cite(ids, kind)}</p>"


def _cited_ids(analysis: dict | None) -> list[str]:
    if not analysis:
        return []
    seen = []
    blocks = [analysis.get("market_view"), analysis.get("sector_view"), analysis.get("macro_view"), analysis.get("outlook"), *analysis.get("issues", [])]
    for block in blocks:
        for ref in (block or {}).get("evidence_ids", []) if isinstance(block, dict) else []:
            if ref not in seen:
                seen.append(ref)
    return seen


# ---------------------------------------------------------------- slot views

def _lead(slot: dict) -> str:
    kind, analysis = slot["kind"], slot.get("analysis")
    status = _STATUS.get(slot.get("status"), "미발송")
    kicker = f"{_TITLES[kind]} · {_LABELS[kind]} KST"
    kicker += f" · {slot['sent_at']} {status}" if slot.get("sent_at") else f" · {status}"
    if analysis:
        stance = analysis["stance"]
        cls = {"우호": "up", "경계": "down"}.get(stance, "flat")
        summary = "".join(f"<li>{_esc(item)}</li>" for item in analysis.get("summary", []))
        return (f'<header class="lead"><p class="kicker">{_esc(kicker)}</p>'
                f'<div class="stance {cls}"><span>시장 스탠스</span><b>{_esc(stance)}</b></div>'
                f'<h2 class="headline">{_esc(analysis["headline"])}</h2>'
                f'<ul class="summary">{summary}</ul></header>')
    reason = _REASON.get(slot.get("web_reason"), "분석을 생성하지 못했습니다.")
    return (f'<header class="lead"><p class="kicker">{_esc(kicker)}</p>'
            f'<div class="stance none"><span>시장 스탠스</span><b>분석 미확보</b></div>'
            f'<h2 class="headline muted">관측 자료만 제공하는 보고입니다</h2>'
            f'<p class="why">{_esc(reason)}</p></header>')


def _narrative(slot: dict) -> str:
    kind, analysis = slot["kind"], slot.get("analysis")
    if not analysis:
        facts = slot.get("facts") or {}
        counts = [f"지수 {len(facts.get('indices', []))}개" if facts.get("indices") else "",
                  f"미국 지수 {len(facts.get('us', []))}개" if facts.get("us") else "",
                  f"섹터 ETF {len(facts.get('sectors', []))}개" if facts.get("sectors") else ""]
        have = " · ".join(c for c in counts if c) or "적격 관측 없음"
        return (f'<section class="blk"><h3>이 보고에 있는 것</h3><p>{_esc(have)}. 자료 칸에 관측값과 수집 근거를 그대로 표시합니다. '
                f'해석·전망은 생성하지 않았으므로 방향 판단은 다음 보고에서 확인하십시오.</p></section>')
    out = [f'<section class="blk"><h3>시장 판단</h3>{_p(analysis["market_view"]["text"], analysis["market_view"]["evidence_ids"], kind)}</section>']
    issues = ""
    for n, issue in enumerate(analysis.get("issues", []), 1):
        issues += (f'<article class="issue"><h4><span class="n">{n}</span>{_esc(issue["title"])}</h4><dl class="flow">'
                   f'<dt>사건</dt><dd>{_esc(issue["event"])}</dd>'
                   f'<dt>영향</dt><dd>{_esc(issue["impact"])}</dd>'
                   f'<dt class="c">반론</dt><dd>{_esc(issue["counterpoint"])}</dd>'
                   f'<dt class="w">확인</dt><dd>{_esc(issue["watch"])}</dd></dl>{_cite(issue["evidence_ids"], kind)}</article>')
    out.append(f'<section class="blk"><h3>핵심 이슈</h3>{issues}</section>')
    out.append(f'<section class="blk"><h3>업종 흐름 해석</h3>{_p(analysis["sector_view"]["text"], analysis["sector_view"]["evidence_ids"], kind)}</section>')
    if analysis.get("macro_view"):
        out.append(f'<section class="blk"><h3>거시 · 해외</h3>{_p(analysis["macro_view"]["text"], analysis["macro_view"]["evidence_ids"], kind)}</section>')
    o = analysis["outlook"]
    out.append('<section class="blk"><h3>다음 세션 전망</h3><div class="outlook">'
               f'<div class="o base"><span>기본 시나리오</span><p>{_esc(o["base"])}</p></div>'
               f'<div class="o up"><span>상방 조건</span><p>{_esc(o["upside"])}</p></div>'
               f'<div class="o down"><span>하방 조건</span><p>{_esc(o["downside"])}</p></div></div>'
               f'{_cite(o["evidence_ids"], kind)}<p class="fine">조건부 시나리오이며 목표가·확률이 아닙니다.</p></section>')
    checks = "".join(f"<li>{_esc(item)}</li>" for item in analysis.get("checklist", []))
    out.append(f'<section class="blk"><h3>체크리스트</h3><ol class="check">{checks}</ol></section>')
    return "".join(out)


_COVERAGE_STATUS = {"partial": "일부", "ok": "확보", "empty": "자료 없음", "unavailable": "미확보", "error": "조회 실패",
                    "not_ready": "준비 전", "stale": "오래됨", "failed": "조회 실패", "invalid_time": "시각 오류"}
_COVERAGE_SOURCE = {"news_storage": "뉴스 저장소", "news_curator": "뉴스 수집", "dart_earnings": "공시 색인",
                    "earnings_calendar": "실적 일정", "disclosure_feed": "공시 유형", "macro_economist": "거시 지표",
                    "public_research": "공개 자료"}
_CONTENT_KIND = {"title_excerpt": "기사 발췌", "title": "제목", "index": "색인", "disclosure_index": "공시 색인",
                 "filing_index": "공시 색인", "filing_type_description": "공시 유형 설명", "article_excerpt": "기사 발췌",
                 "earnings_calendar": "실적 일정", "indicator_value": "지표 관측", "macro": "거시 관측", "calendar": "일정"}


def _link(url, text: str) -> str:
    if isinstance(url, str) and re.match(r"^https?://", url):
        return f'<a href="{_esc(url)}" rel="noopener">{text}</a>'
    return text


def _data_column(slot: dict) -> str:
    kind, facts, evidence = slot["kind"], slot.get("facts") or {}, slot.get("evidence") or {}
    out = []
    cards = ""
    for row in facts.get("indices", []):
        pct = row.get("pct")
        sub = _pct(pct) if pct is not None else "전일 대비 미확보"
        cards += (f'<div class="kpi"><span class="l">{_esc(row["label"])}</span>'
                  f'<span class="v">{row["price"]:,.2f}</span><span class="pct {_sign(pct)}">{_esc(sub)}</span>'
                  f'<span class="s">{_esc(_when(row))} · {_esc(_BASIS_TAG.get(row.get("basis"), "기준 미확인"))}</span></div>')
    for row in facts.get("us", []):
        cards += (f'<div class="kpi"><span class="l">미국 {_esc(row["label"])}</span>'
                  f'<span class="v {_sign(row["pct"])}">{_esc(_pct(row["pct"]))}</span>'
                  f'<span class="s">{_esc(row.get("date") or "기준일 미확인")} 마감</span></div>')
    if cards:
        out.append(f'<section class="dat"><h3>지수</h3><div class="kpis">{cards}</div></section>')
    sectors = facts.get("sectors", [])
    if sectors:
        scale = max(abs(float(s["pct"])) for s in sectors) or 1.0
        rows = ""
        for s in sectors:
            cls = _sign(s["pct"])
            fill = f'<i class="f {cls}" style="width:{abs(float(s["pct"])) / scale * 50:.1f}%"></i>' if cls != "flat" else ""
            rows += (f'<div class="n">{_esc(s["name"])}</div><div class="t">{fill}</div>'
                     f'<div class="v {cls}">{_esc(_pct(s["pct"]))}</div>')
        basis = facts.get("basis_date") or sectors[0].get("as_of") or "기준일 미확인"
        out.append(f'<section class="dat"><h3>섹터 ETF</h3><div class="bars">{rows}</div>'
                   f'<p class="fine">확인 {len(sectors)}개 · 기준일 {_esc(basis)} 최근 1세션 · 전체 업종 순위 아님</p></section>')
    records = evidence.get("records", []) if isinstance(evidence, dict) else []
    macro = [r for r in records if r.get("kind") == "macro"]
    if macro:
        trs = "".join(f'<tr><td>{_esc(r["title"])}</td><td class="num">{_esc(r.get("excerpt") or "—")}</td>'
                      f'<td class="m">{_esc(r.get("source") or "")} · {_esc(_display_time(r.get("event_date")))}</td></tr>' for r in macro)
        out.append(f'<section class="dat"><h3>거시 지표</h3><table class="macro">{trs}</table><p class="fine">지표 기준일 기준 · 보고서 발행일 아님</p></section>')
    events = [r for r in records if r.get("kind") in ("disclosure", "calendar")]
    if events:
        lis = ""
        for r in events[:10]:
            label = {"disclosure": "접수", "calendar": "예정"}[r["kind"]]
            lis += (f'<li>{_link(r.get("url"), _esc(r["title"]))}'
                    f'<span class="m">{_esc(r.get("source") or "")} · {label} {_esc(_display_time(r.get("event_date")))}</span></li>')
        out.append(f'<section class="dat"><h3>공시 · 일정</h3><ul class="ev">{lis}</ul></section>')
    coverage = evidence.get("coverage", []) if isinstance(evidence, dict) else []
    if coverage:
        chips = ""
        for entry in coverage:
            status = _COVERAGE_STATUS.get(entry.get("status"), "상태 미확인")
            source = _COVERAGE_SOURCE.get(entry.get("source"), entry.get("source") or "출처 미확인")
            warn = entry.get("status") not in ("ok", "partial")
            chips += f'<span class="chip{" warn" if warn else ""}">{_esc(source)} {_esc(status)}</span>'
        out.append(f'<section class="dat"><h3>자료 범위</h3><div class="chips">{chips}</div></section>')
    return "".join(out) or '<section class="dat"><p class="fine">표시할 관측 자료가 없습니다.</p></section>'


def _evidence_li(row: dict, kind: str) -> str:
    meta = []
    if row.get("published_at"):
        meta.append("발행 " + _display_time(row["published_at"]) + ("" if row.get("publication_status") == "verified" else " (검증 안됨)"))
    else:
        meta.append("발행시각 미확인")
    if row.get("collected_at"):
        meta.append("수집 " + _display_time(row["collected_at"]))
    if row.get("content_kind"):
        meta.append(_CONTENT_KIND.get(row["content_kind"], row["content_kind"]))
    if row.get("limitations"):
        meta.append("한계 " + _display_note(row["limitations"]))
    link = " · " + _link(row["url"], "원문") if isinstance(row.get("url"), str) and re.match(r"^https?://", row["url"]) else ""
    return (f'<li id="{kind}-{_esc(row["id"])}"><span class="id">{_esc(row["id"])}</span><b>{_esc(row.get("source") or "제공처 미확인")}</b> {_esc(row["title"])}'
            f'<span class="m">{_esc(" · ".join(meta))}{link}</span></li>')


def _sources(slot: dict) -> str:
    kind, facts, evidence = slot["kind"], slot.get("facts") or {}, slot.get("evidence") or {}
    records = evidence.get("records", []) if isinstance(evidence, dict) else []
    cited = _cited_ids(slot.get("analysis"))
    out = []
    if records:
        first = [r for r in records if r["id"] in cited]
        rest = [r for r in records if r["id"] not in cited]
        lis = "".join(_evidence_li(r, kind) for r in first)
        more = ""
        if rest:
            more = (f'<details><summary>인용하지 않은 수집 자료 {len(rest)}건</summary><ul class="src">'
                    + "".join(_evidence_li(r, kind) for r in rest) + "</ul></details>")
        out.append(f'<section class="blk src-blk"><h3>근거 · 출처</h3><ul class="src">{lis}</ul>{more}</section>')
    lines = fact_lines(facts) if facts else []
    if lines:
        lis = "".join(f'<li id="{kind}-{_esc(fid)}"><span class="id">{_esc(fid)}</span>{_esc(text)}</li>' for fid, text in lines)
        out.append(f'<section class="blk src-blk"><h3>관측 자료</h3><ul class="src facts">{lis}</ul></section>')
    meta = slot.get("web_meta") or {}
    model = meta.get("resolved_model") or meta.get("requested_model")
    if model or slot.get("analysis"):
        out.append(f'<p class="fine">분석 생성{" · 모델 " + _esc(model) if model else ""} · 수치·날짜는 인용 근거의 값만 사용 · 종목 추천·목표가 없음</p>')
    return "".join(out)


def _render_slot(slot: dict, default: bool) -> str:
    kind, status = slot["kind"], slot.get("status")
    cls = "slot default" if default else "slot"
    has_content = bool(slot.get("facts") or slot.get("analysis"))
    if not has_content or (status and str(status).startswith("skipped_")):
        label = _STATUS.get(status, "예정" if status is None else "미발송")
        reason = {"skipped_holiday": "휴장일이라 보고가 없습니다.",
                  "skipped_missing_current_market_basis": "원천 시점이 확인된 장중 지수 자료가 없어 보고를 생략했습니다. 수신 시각으로 대체하지 않습니다.",
                  "skipped_missing_market_basis": "원천 시점이 확인된 필수 자료가 없어 보고를 생략했습니다. 수신 시각으로 대체하지 않습니다.",
                  "failed_validation": "본문 형식·분량 검증에 실패해 발송하지 않았습니다."}.get(status, "이 슬롯의 보고는 아직 없습니다.")
        return (f'<article class="{cls}" id="{kind}"><header class="lead"><p class="kicker">{_esc(_TITLES[kind])} · {_esc(_LABELS[kind])} KST · {_esc(label)}</p>'
                f'<h2 class="headline muted">보고 없음</h2><p class="why">{_esc(reason)}</p></header></article>')
    return (f'<article class="{cls}" id="{kind}">{_lead(slot)}'
            f'<div class="layout"><div class="main">{_narrative(slot)}</div><aside class="aside">{_data_column(slot)}</aside></div>'
            f'{_sources(slot)}</article>')


_CSS = """
:root{--bg:#f4f5f8;--card:#fff;--ink:#15171f;--ink2:#4b5263;--mute:#7c8496;--line:#e2e5ec;--up:#d2403f;--down:#2d6bd2;--upbg:#fbeaea;--downbg:#e8effb;--accent:#2f3aa8;--chip:#eceef4;--hl:#fff3c4}
@media (prefers-color-scheme:dark){:root{--bg:#0e1016;--card:#161922;--ink:#e9ebf2;--ink2:#aeb5c6;--mute:#7d8599;--line:#272b38;--up:#f07070;--down:#6f9bf3;--upbg:#3a1f22;--downbg:#1b2740;--accent:#9aa4ff;--chip:#1e2230;--hl:#3b3410}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%;scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font:16.5px/1.7 -apple-system,"Apple SD Gothic Neo","Pretendard Variable",Pretendard,"Malgun Gothic","Noto Sans KR",system-ui,sans-serif;font-feature-settings:"tnum","ss05";word-break:keep-all}
a{color:var(--accent)}.wrap{max-width:1100px;margin:0 auto;padding:0 20px 72px}
.top{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 92%,transparent);backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
.bar{max-width:1100px;margin:0 auto;padding:12px 20px 0}
.brand{display:flex;justify-content:space-between;align-items:baseline;font-size:13.5px;color:var(--mute);letter-spacing:.02em}.brand b{color:var(--ink);font-size:15px;letter-spacing:0}
.nav a{color:var(--mute);text-decoration:none;margin-left:14px}.nav a:hover{color:var(--ink)}
h1{font-size:26px;margin:6px 0 10px;letter-spacing:-.02em;font-weight:700}
.tabs{display:flex;gap:8px;margin:0 -20px;padding:0 20px 12px;overflow-x:auto;scrollbar-width:none}
.tabs a{flex:0 0 auto;padding:8px 16px;border-radius:999px;background:var(--chip);color:var(--ink2);text-decoration:none;font-size:14.5px;border:1px solid transparent}
.tabs a.off{color:var(--mute);text-decoration:line-through}.tabs a.live{border-color:var(--accent);color:var(--accent);font-weight:600;background:var(--card)}
.slot{display:none;scroll-margin-top:150px}.slot [id]{scroll-margin-top:150px}.slot:target,.slot:has(:target){display:block}.slot.default{display:block}body:has(.slot:target,.slot :target) .slot.default:not(:target):not(:has(:target)){display:none}
.lead{padding:36px 0 26px;border-bottom:1px solid var(--line)}
.kicker{margin:0 0 14px;font-size:13.5px;color:var(--mute);letter-spacing:.02em}
.stance{display:inline-flex;align-items:center;gap:10px;padding:6px 12px 6px 14px;border-radius:999px;font-size:13px;background:var(--chip);color:var(--ink2);margin-bottom:16px}
.stance b{font-size:15px;color:var(--ink)}.stance.up{background:var(--upbg)}.stance.up b{color:var(--up)}.stance.down{background:var(--downbg)}.stance.down b{color:var(--down)}.stance.none b{color:var(--mute)}
.headline{font-size:30px;line-height:1.3;letter-spacing:-.025em;margin:0 0 18px;font-weight:700;max-width:22em}.headline.muted{color:var(--ink2);font-weight:600}
.summary{margin:0;padding:0;list-style:none;display:grid;gap:8px;max-width:46em}.summary li{position:relative;padding-left:18px;color:var(--ink2);font-size:17px}.summary li::before{content:"";position:absolute;left:0;top:.72em;width:8px;height:8px;border-radius:50%;background:var(--accent)}
.why{color:var(--mute);margin:0;max-width:46em}
.layout{display:grid;grid-template-columns:minmax(0,1fr);gap:28px;padding-top:8px}
@media (min-width:900px){.layout{grid-template-columns:minmax(0,1fr) 340px;gap:48px;align-items:start}.aside{position:sticky;top:132px}.headline{font-size:36px}}
.blk{padding:26px 0 6px}.blk h3,.dat h3{font-size:12.5px;letter-spacing:.12em;color:var(--mute);margin:0 0 12px;font-weight:700;text-transform:uppercase}
.blk p{margin:0 0 10px;font-size:17.5px;line-height:1.75}.blk .fine,.fine{font-size:13px;color:var(--mute);margin:8px 0 0;line-height:1.5}
.cite{display:inline-flex;gap:4px;margin-left:8px;vertical-align:2px}.cite a{font-size:11.5px;padding:1px 7px;border-radius:5px;background:var(--chip);color:var(--ink2);text-decoration:none;border:1px solid var(--line)}.cite a:hover{color:var(--accent);border-color:var(--accent)}
.issue{padding:18px 0;border-top:1px solid var(--line)}.issue:first-of-type{border-top:0;padding-top:4px}
.issue h4{font-size:19px;margin:0 0 12px;letter-spacing:-.01em;display:flex;gap:10px;align-items:baseline}.issue .n{font-size:13px;color:var(--mute);font-weight:600;min-width:1.2em}
.flow{display:grid;grid-template-columns:52px 1fr;gap:8px 14px;margin:0}.flow dt{font-size:12.5px;font-weight:700;color:var(--mute);padding-top:4px;letter-spacing:.04em}.flow dt.c{color:var(--down)}.flow dt.w{color:var(--accent)}.flow dd{margin:0;font-size:16.5px;line-height:1.65}
.outlook{display:grid;gap:10px}@media (min-width:640px){.outlook{grid-template-columns:1.3fr 1fr 1fr}}
.o{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 16px 12px}.o span{display:block;font-size:12px;letter-spacing:.08em;color:var(--mute);font-weight:700;margin-bottom:8px}.o p{font-size:15.5px;margin:0;line-height:1.6}
.o.up{border-top:3px solid var(--up)}.o.down{border-top:3px solid var(--down)}.o.base{border-top:3px solid var(--accent)}
.check{margin:0;padding-left:0;list-style:none;counter-reset:c}.check li{counter-increment:c;position:relative;padding:10px 0 10px 36px;border-top:1px solid var(--line);font-size:16.5px}.check li:first-child{border-top:0}
.check li::before{content:counter(c);position:absolute;left:0;top:10px;width:24px;height:24px;border-radius:50%;background:var(--chip);color:var(--ink2);font-size:12.5px;font-weight:700;display:grid;place-items:center}
.dat{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px 16px 14px;margin:0 0 14px}
.kpis{display:grid;grid-template-columns:1fr 1fr;gap:10px}.kpi{display:grid;gap:2px;background:var(--chip);border-radius:12px;padding:12px}
.kpi .l{font-size:12.5px;color:var(--mute)}.kpi .v{font-size:22px;font-weight:700;letter-spacing:-.02em}.kpi .pct{font-size:14px;font-weight:600}.kpi .s{font-size:11.5px;color:var(--mute);line-height:1.4}
.up{color:var(--up)}.down{color:var(--down)}.flat{color:var(--ink2)}
.bars{display:grid;grid-template-columns:minmax(56px,auto) 1fr 64px;gap:7px 10px;align-items:center;font-size:14px}
.bars .n{text-align:right;color:var(--ink2);white-space:nowrap}.bars .t{position:relative;height:14px;background:var(--chip);border-radius:4px}.bars .t::before{content:"";position:absolute;left:50%;top:0;bottom:0;width:1px;background:var(--line)}
.bars .f{position:absolute;top:2px;bottom:2px;border-radius:3px;display:block}.bars .f.up{background:var(--up);left:50%}.bars .f.down{background:var(--down);right:50%}.bars .v{font-weight:600;font-size:13.5px}
table.macro{width:100%;border-collapse:collapse;font-size:14px}table.macro td{padding:7px 0;border-top:1px solid var(--line);vertical-align:top}table.macro tr:first-child td{border-top:0}table.macro .num{font-weight:600;white-space:nowrap;padding:7px 8px}table.macro .m{color:var(--mute);font-size:12px;text-align:right;white-space:nowrap}
.ev{list-style:none;margin:0;padding:0;font-size:14px}.ev li{padding:8px 0;border-top:1px solid var(--line)}.ev li:first-child{border-top:0}.ev .m{display:block;font-size:12px;color:var(--mute)}
.chips{display:flex;flex-wrap:wrap;gap:6px}.chip{font-size:12px;padding:3px 9px;border-radius:6px;background:var(--chip);color:var(--ink2);border:1px solid var(--line)}.chip.warn{color:var(--up);border-color:color-mix(in srgb,var(--up) 35%,transparent)}
.src-blk{border-top:1px solid var(--line);margin-top:20px}.src{list-style:none;padding:0;margin:0}.src li{padding:12px 0;border-top:1px solid var(--line);font-size:15px;scroll-margin-top:150px;line-height:1.55}.src li:first-child{border-top:0}
.src li:target{background:var(--hl);border-radius:8px}
.src .id{display:inline-block;min-width:34px;font-weight:700;color:var(--accent);font-size:13px}.src b{font-weight:600;margin-right:4px}.src .m{display:block;font-size:12.5px;color:var(--mute);margin-top:2px;padding-left:34px}
.src.facts li{font-size:14px;color:var(--ink2)}
details{margin-top:10px}summary{cursor:pointer;color:var(--mute);font-size:14px;padding:6px 0}
.foot{margin-top:40px;padding-top:18px;border-top:1px solid var(--line);font-size:13px;color:var(--mute);line-height:1.6}
.archive{list-style:none;padding:0;margin:20px 0 0;display:grid;gap:12px}.archive li{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px 20px;display:grid;gap:8px}
.archive .d{font-size:14px;color:var(--mute)}.archive .d a{color:var(--ink);text-decoration:none;font-weight:700;font-size:18px;letter-spacing:-.01em}.archive .h{font-size:17px;margin:0;line-height:1.5}.archive .h a{color:var(--ink);text-decoration:none}
.archive .h .stance{margin:0 10px 0 0;padding:3px 10px;font-size:12px}.archive .h .stance b{font-size:13px}
.archive .chips a{text-decoration:none}
@media print{.slot{display:block!important}.top{position:static}.tabs{display:none}.aside{position:static}a[href^="http"]::after{content:" (" attr(href) ")";font-size:11px;color:#777}}
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
    """slots: [{kind, status, sent_at, facts, analysis, evidence, web_reason, web_meta}] in MARKET_KINDS order; missing kinds allowed."""
    base = web_base_url() if base is None else base
    by_kind = {s["kind"]: s for s in slots}

    def shown(kind):
        slot = by_kind.get(kind, {})
        return bool(slot.get("facts") or slot.get("analysis")) and not str(slot.get("status") or "").startswith("skipped_")
    sent = [k for k in MARKET_KINDS if shown(k) and by_kind[k].get("status") == "accepted"]
    visible = [k for k in MARKET_KINDS if shown(k)]
    recorded = [k for k in MARKET_KINDS if by_kind.get(k, {}).get("status")]
    default = (sent or visible or recorded or [None])[-1]   # a fully-skipped day still shows its last reason
    tabs = ""
    for kind in MARKET_KINDS:
        slot = by_kind.get(kind, {"kind": kind, "status": None})
        status = slot.get("status")
        off = not shown(kind)
        cls = "live" if kind == default else ("off" if off else "")
        suffix = (" 휴장" if status == "skipped_holiday" else " 보류" if status else "") if off else ""
        current = ' aria-current="page"' if kind == default else ''
        tabs += f'<a class="{cls}"{current} href="#{kind}">{_esc(_LABELS[kind] + suffix)}</a>'
    prev_day, next_day = neighbours
    nav = ""
    if prev_day:
        nav += f'<a href="{_esc(base)}/reports/{_esc(prev_day)}">‹ {_esc(prev_day[5:].replace("-", "/"))}</a>'
    if next_day:
        nav += f'<a href="{_esc(base)}/reports/{_esc(next_day)}">{_esc(next_day[5:].replace("-", "/"))} ›</a>'
    nav += f'<a href="{_esc(base)}/reports/">아카이브</a>'
    articles = "".join(_render_slot(by_kind.get(kind, {"kind": kind, "status": None}), kind == default) for kind in MARKET_KINDS)
    stamp = generated_at.astimezone(KST).strftime("%Y/%m/%d %H:%M:%S KST")
    footer = (f'<p class="foot">QWQ 시장 리포트는 수집된 공개 자료와 관측값만으로 자동 생성되는 데일리 리포트입니다. '
              f'투자 권유가 아니며 수치·날짜는 하단 근거에 인용된 값만 사용합니다. 종목 추천·목표가·확률 예측을 포함하지 않습니다. 페이지 생성 {stamp}.</p>')
    body = (f'<div class="top"><div class="bar"><div class="brand"><b>QWQ 시장 리포트</b><span class="nav">{nav}</span></div>'
            f'<h1>{_esc(_day_title(day))}</h1><nav class="tabs">{tabs}</nav></div></div>'
            f'<main class="wrap">{articles}{footer}</main>')
    description = "한국증시 장전·장중·장후 데일리 리포트 (자동 생성, 투자 권유 아님)"
    return _page(f"QWQ 시장 리포트 · {day}", body, description, f"{base}/reports/{day}")


def render_index_page(days: list[dict], *, generated_at: datetime, base: str | None = None) -> str:
    """days: [{report_date, statuses: {kind: status}, lead: {kind, stance, headline} | None}] newest first."""
    base = web_base_url() if base is None else base
    items = ""
    for entry in days:
        if entry["statuses"] and all(v == "skipped_holiday" for v in entry["statuses"].values()):
            continue
        href = f'{_esc(base)}/reports/{_esc(entry["report_date"])}'
        lead = entry.get("lead")
        if lead:
            cls = {"우호": "up", "경계": "down"}.get(lead["stance"], "flat")
            head = (f'<p class="h"><span class="stance {cls}"><b>{_esc(lead["stance"])}</b></span>'
                    f'<a href="{href}#{_esc(lead["kind"])}">{_esc(lead["headline"])}</a></p>')
        else:
            head = '<p class="h fine">분석 미확보 · 관측 자료만</p>'
        chips = ""
        for kind in MARKET_KINDS:
            status = entry["statuses"].get(kind)
            label = _STATUS.get(status, "—" if status is None else "미발송")
            warn = status in ("rejected", "unknown", "failed_validation", "failed_configuration")
            chips += f'<a class="chip{" warn" if warn else ""}" href="{href}#{kind}">{_esc(_LABELS[kind].split()[0])} {_esc(label)}</a>'
        items += f'<li><div class="d"><a href="{href}">{_esc(_day_title(entry["report_date"]))}</a></div>{head}<div class="chips">{chips}</div></li>'
    stamp = generated_at.astimezone(KST).strftime("%Y/%m/%d %H:%M:%S KST")
    body = (f'<div class="top"><div class="bar"><div class="brand"><b>QWQ 시장 리포트</b></div><h1>아카이브</h1></div></div>'
            f'<main class="wrap"><ul class="archive">{items}</ul>'
            f'<p class="foot">자동 생성 · 투자 권유 아님 · 생성 {stamp}</p></main>')
    return _page("QWQ 시장 리포트 · 아카이브", body, "한국증시 데일리 리포트 아카이브", f"{base}/reports/")


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


def _web_row(store, kind: str, day: str) -> dict | None:
    row = store.analysis_receipt("web", kind, day)
    return row if isinstance(row, dict) and row.get("status") != "attempted" else None


def collect_day(store, day: str) -> list[dict]:
    slots = []
    for kind in MARKET_KINDS:
        receipt = store.receipt("market", kind, day)
        body = receipt.get("body") if receipt else None
        web = _web_row(store, kind, day)
        result = web.get("result") if web and isinstance(web.get("result"), dict) else None
        facts = result.get("facts") if result else (facts_from_body(body) if body else None)
        analysis = result.get("analysis") if result and web.get("status") == "ready" else None
        slots.append({"kind": kind, "status": receipt["status"] if receipt else None, "sent_at": sent_time_of(body),
                      "facts": facts, "analysis": analysis, "evidence": web.get("public_evidence") if web else None,
                      "web_reason": (result or {}).get("reason") if web else "no_web_row",
                      "web_meta": web.get("metadata") if web else None})
    return slots


def collect_index(store, days: list[dict], *, limit: int = 60) -> list[dict]:
    out = []
    for entry in days:
        lead = None
        if len(out) < limit:
            for kind in reversed(MARKET_KINDS):
                web = _web_row(store, kind, entry["report_date"])
                analysis = (web.get("result") or {}).get("analysis") if web and web.get("status") == "ready" else None
                if analysis:
                    lead = {"kind": kind, "stance": analysis["stance"], "headline": analysis["headline"]}
                    break
        out.append({**entry, "lead": lead})
    return out


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
    _atomic_write(html_dir / "index.html", render_index_page(collect_index(store, days), generated_at=now, base=base))
    return target
