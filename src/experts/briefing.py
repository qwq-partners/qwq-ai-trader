"""Pure, bounded Telegram presentation of expert evidence (no data acquisition)."""
from datetime import datetime
from html import escape
import math
from zoneinfo import ZoneInfo

from .orchestrator import ExpertOrchestrator

KST = ZoneInfo("Asia/Seoul")
ORDER = ("weekend_signal_expert", "macro_economist", "kr_market_expert",
         "us_market_expert", "kr_economy_expert", "global_micro_expert",
         "news_curator", "earnings_expert")
NAMES = dict(zip(ORDER, ("야간 지표", "거시", "KR시장", "US시장", "한국경제", "글로벌", "뉴스", "실적")))
STATUS = {"ok": "필수입력 확보", "partial": "부분 결측", "insufficient": "자료 부족", "unknown": "자료상태 미확인"}
SCOPE = {
    "weekend_signal_expert": "선물·환율 등 / 직전 관측구간",
    "macro_economist": "금리·환율 등 수준 / 미국 반도체 5일",
    "kr_market_expert": "한국 수급·공매도·지수",
    "us_market_expert": "미국 지수 추세·섹터 ETF / 한국 당일 등락 아님",
    "kr_economy_expert": "원/달러 수준·한국 거시 참고",
    "global_micro_expert": "미국 ETF 5일·원자재 수준",
    "news_curator": "기사 방향 분류 / 수익률 예측 아님",
    "earnings_expert": "US EPS 최근 7달력일 / KR 공시 건수",
}


def _text(value, limit=100):
    # Clip before escaping: never split a Telegram HTML entity or trust source HTML.
    text = " ".join(str(value).split())
    return escape(text[:limit] + ("…" if len(text) > limit else ""), quote=False)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _pct(value):
    return f"{value:+.2f}%" if _number(value) else "미확인"


def _date(value):
    try:
        return datetime.fromisoformat(str(value)).date().isoformat()
    except (ValueError, TypeError):
        return None


def _time(value):
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(KST)
        return stamp.strftime("%m-%d %H:%M")
    except (ValueError, TypeError, AttributeError):
        return "미확인"


def _basis(name, op):
    raw = op.raw_evidence or {}
    if name == "macro_economist":
        evidence = (raw.get("indicator_evidence") or {}).get("us10y") or {}
        if evidence:
            return f"{evidence.get('source', '미확인')} · US10Y 기준 {_time(evidence.get('as_of'))}"
    if name == "us_market_expert":
        periods = raw.get("sector_periods") or {}
        dates = sorted({_date(p.get("as_of")) for p in periods.values() if isinstance(p, dict) and _date(p.get("as_of"))})
        if dates:
            return f"yfinance 미국 ETF · 가격일 {'~'.join(dict.fromkeys((dates[0], dates[-1])))}"
    if name == "news_curator":
        collected, classified = raw.get("item_count"), raw.get("classified_count")
        if isinstance(collected, int) and isinstance(classified, int):
            return f"수집 {collected}건 중 분류 {classified}건 · 중립 {raw.get('neutral_count', 0)}건"
    if name == "weekend_signal_expert":
        signals = raw.get("signals") or {}
        source = signals.get("kr_futures_source") or "원천 미확인"
        stamp = signals.get("kr_futures_as_of")
        session = "야간 세션" if signals.get("kr_futures_session") == "night" else "대체지표·관측구간"
        return f"{source} · {session} 기준 {_time(stamp)} (체결시각과 구분)"
    return "자료 기준시각 미확인"


def render_expert_briefing(label, ops, agg, bias, bear_consensus, summary, *,
                           now=None, threshold_confidence=.7, min_count=2):
    """Render one complete expert section; caller may append other report sections."""
    now = now or datetime.now(KST)
    today = now.astimezone(KST).date().isoformat() if now.tzinfo else now.date().isoformat()
    contributors = set(summary.get("contributors", ()))
    counts = summary.get("market_counts", {})
    count_keys = ("ok", "partial", "insufficient", "unknown", "unavailable")
    counts_known = (isinstance(counts, dict)
                    and all(type(counts.get(k)) is int and counts[k] >= 0 for k in count_keys)
                    and sum(counts[k] for k in count_keys) == 6)
    count_label = (f"확보 {counts['ok']} / 부분 {counts['partial']} / 부족 {counts['insufficient']} / "
                   f"미확인·만료 {counts['unknown'] + counts['unavailable']}"
                   if counts_known else "자료 상태별 수: 미확인")
    distribution = {b: sum(1 for n in contributors if n in ops and ops[n].regime_bias.value == b)
                    for b in ("bull", "neutral", "bear")}
    eligible_n = summary.get("valid_n")
    coverage = f"{eligible_n}/6" if isinstance(eligible_n, int) else "미확인/6"
    short = summary.get("insufficient_coverage", True)
    lines = [
        f"🧠 <b>{_text(label, 35)} 시장 근거 브리핑</b>",
        f"작성 {_time(now)} KST · 분석시각과 원자료 시각은 다릅니다",
        f"<b>시장 종합</b>: {_text(bias.upper(), 12)} · 체제 보정 {agg:+d}/±30",
        f"반영 {coverage} · {count_label}",
        f"반영 의견: 🟢{distribution['bull']} ⚪{distribution['neutral']} 🔴{distribution['bear']}",
        f"<b>약세 합의 조건</b>: {'충족' if bear_consensus else '미충족'} · 매매 가능 여부와 별개",
        f"기준: 시장 입력 ≥4개, 내부 가중치 ≥{threshold_confidence:g}인 약세 ≥{min_count}개",
        "<i>점수·가중치는 규칙값이며 예측 확률이 아닙니다. 중립은 보합 예측이 아닙니다.</i>",
    ]
    if short:
        lines.append("⚠️ 자료 범위 부족/미확인 — 종합 방향 판단 보류")
    lines.append("\n<b>시장 입력과 참고 자료</b>")
    for name in ORDER:
        op = ops.get(name)
        if op is None or not op.is_valid:
            lines.append(f"• <b>{NAMES[name]}</b>: 미수집·만료 — 미반영")
            continue
        status = STATUS.get(op.data_status, STATUS["unknown"])
        tag = "시장 반영" if name in contributors else "참고·시장 미반영"
        score = f"{op.score:+d}" if isinstance(op.score, int) else "미확인"
        lines.append(f"• <b>{NAMES[name]}</b> {score} · {status} · {tag}")
        lines.append(f"  {_text(SCOPE[name], 65)}")
        finding = next((f for f in op.key_findings if f), "판단 근거 미확보")
        lines.append(f"  {_text(finding, 110)}")
        lines.append(f"  {_text(_basis(name, op), 100)} · 분석 {_time(op.issued_at)}")
        if name == "news_curator":
            representatives = (op.raw_evidence or {}).get("representative_articles") or []
            article = representatives[0] if representatives and isinstance(representatives[0], dict) else {}
            if article:
                lines.append(f"  출처 {_text(article.get('source') or '미확인', 40)} · "
                             f"수집 {_time(article.get('fetched_at'))} · 발행시각 미확인")
                lines.append(f"  기사 {_text(article.get('url') or 'URL 미확인', 300)}")
            else:
                lines.append("  대표 기사 출처·시각 미확인")
        if op.missing_inputs:
            lines.append(f"  결측: {_text(', '.join(op.missing_inputs), 75)}")

    sector = ops.get("sector_council")
    if sector is not None and sector.is_valid:
        scores = (sector.raw_evidence or {}).get("sector_scores") or {}
        if scores:
            # Show daily leaders as well as weakest multi-day scores so a rebound
            # cannot disappear behind the saturated -80 score.
            daily = sorted(((n, v) for n, v in scores.items() if _number(v.get("r1"))),
                           key=lambda item: -item[1]["r1"])
            trend = sorted(scores.items(), key=lambda item: item[1].get("score", 0))
            selected = dict(daily[:3])
            for name, values in trend[:3]:
                selected.setdefault(name, values)
            lines.append("\n<b>섹터: ETF 등락 / 가격·뉴스 근거</b>")
            for name, values in selected.items():
                as_of = _date(values.get("as_of"))
                day_label = "당일" if as_of == today else "최근 1세션"
                r1 = _pct(values.get("r1"))
                periods = []
                for key, intended in (("r5", 5), ("r20", 20)):
                    sessions = values.get(f"{key}_sessions")
                    period = f"{sessions}세션" if isinstance(sessions, int) and sessions > 0 else f"{intended}일값(기간 미확인)"
                    periods.append(f"{period} {_pct(values.get(key))}")
                lines.append(f"• <b>{_text(name, 20)}</b> {day_label} {r1} / {' / '.join(periods)}")
                tags = " · 가격자료만" if values.get("quant_only") else ""
                if values.get("score_saturated"):
                    tags += " · 정량점수 한도 도달"
                if values.get("qual_only"):
                    score_label = "뉴스 단독 점수"
                    tags = " · 가격 추세 미확인"
                elif values.get("quant_only"):
                    score_label = "누적 추세점수"
                else:
                    score_label = "섹터 종합점수(구성 미확인)" if "quant_only" not in values else "가격·뉴스 혼합점수"
                lines.append(f"  {score_label} {_text(values.get('score', '미확인'), 8)}{tags}")
                lines.append(f"  {_text(values.get('source', '원천 미확인'), 30)} · 가격 기준 {as_of or '미확인'}")
    lines.append("\n종목 선정·진입 판단에는 현재 가격·거래량·수급을 별도로 확인합니다.")
    return "\n".join(lines)
