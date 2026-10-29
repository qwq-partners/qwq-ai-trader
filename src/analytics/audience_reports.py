"""Small audience-specific reports from existing, source-dated inputs only."""
from dataclasses import dataclass, field
from datetime import datetime
from html import escape, unescape
import math
import re
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True)
class MarketSnapshot:
    indices: tuple[dict, ...] = ()
    sectors: tuple[dict, ...] = ()
    us_quotes: dict = field(default_factory=dict)
    kr_basis_date: str | None = None
    us_basis_date: str | None = None
    index_observations: tuple[dict, ...] = ()


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def timestamp(value):
    try:
        result = datetime.fromisoformat(value)
        return result.astimezone(KST) if result.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _text(value, limit=150):
    plain = " ".join(str(value).split())[:limit]
    while len(escape(plain, quote=False)) > limit:
        plain = plain[:-1]
    return escape(plain, quote=False)


def current_indices(snapshot: MarketSnapshot, now: datetime, *, midday: bool):
    valid = []
    for row in snapshot.indices:
        price, pct = number(row.get("price")), number(row.get("change_pct"))
        observed, received = timestamp(row.get("source_observed_at")), timestamp(row.get("received_at"))
        if price is None or price <= 0 or pct is None or not observed or not received:
            continue
        if not row.get("source") or observed > now or received > now or observed > received:
            continue
        if midday:
            if observed.date() != now.date() or not all(0 <= (now-t).total_seconds() <= 300 for t in (observed, received)):
                continue
        else:
            if observed.date().isoformat() != snapshot.kr_basis_date or (observed.hour, observed.minute) < (15, 30):
                continue
        valid.append((row, price, pct, observed))
    return valid


def indicative_indices(snapshot: MarketSnapshot, now: datetime):
    """Same-row prices are references, never substitutes for daily returns/close."""
    result, seen = [], set()
    for row in snapshot.index_observations:
        if not isinstance(row, dict):
            continue
        code = row.get("code")
        price = number(row.get("price"))
        observed, received = timestamp(row.get("source_observed_at")), timestamp(row.get("received_at"))
        if (code not in {"0001", "1001"} or code in seen or price is None or price <= 0
                or row.get("source") != "KIS/FHKUP03500200"
                or row.get("observation_kind") != "historical_minute" or row.get("finality") != "unverified"
                or not observed or not received or not observed <= received <= now
                or observed.date() != now.date() or received.date() != now.date()
                or observed.date().isoformat() != snapshot.kr_basis_date
                or (observed.hour, observed.minute) < (15, 30)):
            continue
        seen.add(code)
        result.append(("KOSPI" if code == "0001" else "KOSDAQ", price, observed))
    return result


def _midday_provider_references(snapshot: MarketSnapshot, now: datetime):
    """Use recent KIS receipts only as provider references, not source freshness."""
    if now.weekday() >= 5 or not (9 * 60 <= now.hour * 60 + now.minute < 15 * 60 + 30):
        return []
    rows = [row for row in snapshot.indices if isinstance(row, dict)]
    groups = {label: [row for row in rows if row.get("label") == label]
              for label in ("KOSPI", "KOSDAQ")}
    result = []
    for label, group in groups.items():
        if not group or any(row != group[0] for row in group[1:]):
            continue
        row = group[0]
        if row.get("source") != "kis":
            continue
        if "source_observed_at" not in row or row["source_observed_at"] is not None:
            continue
        price, pct = number(row.get("price")), number(row.get("change_pct"))
        received = timestamp(row.get("received_at"))
        if (price is None or price <= 0 or pct is None or not received
                or received.date() != now.date() or not 0 <= (now - received).total_seconds() <= 300):
            continue
        result.append((label, price, pct, received))
    return result


def _direction(values):
    values = [float(f"{value:.2f}") for value in values]
    if all(value > 0 for value in values):
        return "동반 상승"
    if all(value < 0 for value in values):
        return "동반 하락"
    return "혼조·보합"


def _provider(source):
    source = str(source)
    if source.lower() == "kis" or source.upper().startswith(("KIS/", "KIS:")):
        return "KIS"
    return _text(source, 36) + " (제공처 미확인)"


def render_market_report(kind: str, snapshot: MarketSnapshot, now: datetime) -> str | None:
    """Describe only eligible displayed observations, without inferring missing data."""
    now = now.astimezone(KST)
    labels = {"morning": "한국증시 모닝브리프", "midday": "한국증시 장중시황", "after": "한국증시 마감점검"}
    if kind not in labels:
        raise ValueError("unknown market slot")
    indices = current_indices(snapshot, now, midday=kind == "midday")[:2]
    references = _midday_provider_references(snapshot, now) if kind == "midday" and not indices else []
    if kind == "midday" and not indices and not references:
        return None
    observations = indicative_indices(snapshot, now) if kind == "after" and not indices else []
    try:
        basis = datetime.strptime(snapshot.kr_basis_date, "%Y-%m-%d").date()
        valid_sector_date = basis.isoformat() == snapshot.kr_basis_date and basis <= now.date()
    except (TypeError, ValueError):
        valid_sector_date = False
    sectors = []
    for row in snapshot.sectors:
        pct = number(row.get("r1"))
        if valid_sector_date and pct is not None and row.get("as_of") == snapshot.kr_basis_date and row.get("source") not in (None, "", "unknown"):
            sectors.append((row, pct))
    sectors.sort(key=lambda pair: pair[1], reverse=True)
    selected = sectors[:2] + (sectors[-2:] if len(sectors) > 3 else sectors[2:])
    us = []
    if kind == "morning" and snapshot.us_basis_date:
        from .us_report_data import observe_us_quote
        for symbol, label in (("^GSPC", "S&P500"), ("^IXIC", "나스닥"), ("^SOX", "미 반도체 SOX")):
            value = observe_us_quote(snapshot.us_quotes.get(symbol), now=now)
            if value and (value.get("date") or "").replace(".", "-") == snapshot.us_basis_date:
                us.append((symbol, label, value['pct']))
    if not indices and not observations and not references and not sectors and not us:
        return None
    lines = [f"🇰🇷 <b>{labels[kind]}</b>", f"{now:%m/%d %H:%M} KST", "", "<b>시장 판단</b>"]
    if indices:
        if len(indices) == 1:
            row, _, pct, _ = indices[0]
            lines.append(f"확인된 지수 {_text(row.get('label'), 20)} {pct:+.2f}%입니다.")
        else:
            lines.append(f"확인된 지수 {len(indices)}개는 {_direction([v[2] for v in indices])}입니다.")
    elif references:
        lines.append("검증된 시장 방향 자료는 미확보입니다.")
    elif observations:
        lines.append("시점이 확인된 지수 참고 가격입니다. 전일 대비 등락·시장 방향 판단자료는 미확보입니다.")
    elif us:
        if len(us) == 1:
            lines.append(f"미국 {us[0][1]} {us[0][2]:+.2f}%가 관측됐습니다.")
        else:
            lines.append(f"미국 관측 {len(us)}개 지수 {_direction([v[2] for v in us])}입니다.")
    else:
        lines.append("국내 섹터 ETF의 최근 1세션 관측 표본을 확인합니다.")
    if us:
        values = {symbol: pct for symbol, _, pct in us}
        if '^SOX' in values and '^IXIC' in values:
            difference = values['^SOX'] - values['^IXIC']
            if float(f"{abs(difference):.2f}") > 0:
                relation = "웃돌아" if difference > 0 else "밑돌아"
                lines.append(f"SOX는 나스닥 등락률을 {relation} {abs(difference):.2f}%p 차이를 보였습니다.")
    sources = []
    def add_source(source):
        provider = _provider(source)
        if provider not in sources:
            sources.append(provider)
    if indices:
        lines += ["", "<b>주요 지표 · 국내 지수 전일 종가 대비</b>"]
        for row, price, pct, observed in indices:
            lines.append(f"• {_text(row.get('label'), 20)} {price:,.2f} ({pct:+.2f}%) · {observed:%m/%d %H:%M}")
            add_source(row['source'])
    if references:
        lines += ["", "<b>주요 지표 · KIS 응답 참고 · 제공기관 대비율</b>"]
        lines += [f"• {label} {price:,.2f} ({pct:+.2f}%) · 수신 {received:%H:%M} KST" for label, price, pct, received in references]
        lines += ["원천시각 미제공 · 전일 기준 거래일 미확인"]
        add_source('kis')
    if observations:
        lines += ["", "<b>주요 지표 · 국내 지수 분 단위 참고 관측</b>"]
        lines += [f"• {label} {price:,.2f} · {observed:%m/%d %H:%M} KST" for label, price, observed in observations]
        lines += ["확정 종가 아님 · 해당 분 관측값이며 봉 완료 여부 미확인"]
        add_source('KIS/FHKUP03500200')
    if us:
        lines += ["", f"<b>주요 지표 · 미국 {snapshot.us_basis_date}</b>"]
        lines += [f"• {label} {pct:+.2f}%" for _, label, pct in us]
        sources.append('Yahoo Finance')
    if sectors:
        lines += ["", f"<b>업종 흐름 · 한국 상장 섹터 ETF {snapshot.kr_basis_date}</b>", f"확인 {len(sectors)}개 중 최근 1세션 등락 (전체 업종 순위 아님)"]
        if len(sectors) > 1 and float(f"{sectors[0][1]:.2f}") > float(f"{sectors[-1][1]:.2f}"):
            lines.append(f"관측 표본 상대 상위: {_text(sectors[0][0].get('name'), 24)} {sectors[0][1]:+.2f}%")
        for row, pct in selected:
            lines.append(f"• {_text(row.get('name'), 24)} {pct:+.2f}%")
            add_source(row['source'])
    watch = {
        'morning': '개장 후 국내 지수와 반도체·다른 업종의 움직임이 이어지는지 확인합니다.',
        'midday': '이후 국내 지수와 업종 움직임, 개별 종목 거래대금·수급을 함께 확인합니다.',
        'after': '다음 거래일 국내 지수와 업종 움직임의 지속 여부를 확인합니다.',
    }
    lines += ["", "<b>국내 관전 포인트</b>", watch[kind]]
    if references:
        lines.append("원천시각과 전일 기준 거래일을 추가 확인합니다.")
    lines += ["", "자료 범위: 표시된 관측 표본; 시장 전체 종목 수·당일 누적 수급은 미확보.", "출처: " + " / ".join(sources)]
    return "\n".join(lines)


def _excerpt(text: str, limit: int) -> str:
    plain = unescape(re.sub(r"<[^>]*>", "", str(text)))
    lines = [line.strip() for line in plain.splitlines() if line.strip() and not set(line.strip()) <= set("━─-= ")]
    return _text(" / ".join(lines), limit)


def render_engine_digest(records: list[dict], context: str, now: datetime, *, deliveries=()) -> str:
    weekly = now.weekday() == 5
    lines = [f"⚙️ <b>엔진 {'주간·일일' if weekly else '일일'} 요약</b>", f"{now:%m/%d %H:%M} KST", "", "<b>현재 대응</b>"]
    lines.extend(_excerpt(line, 100) for line in context.splitlines()[:3])
    if deliveries:
        status_labels = {"attempted": "처리 중단·결과 미확인", "unknown": "접수 여부 미확인", "rejected": "발송 거절", "failed_validation": "본문 검증 실패", "failed_configuration": "발송 설정 누락", "skipped_missing_current_market_basis": "장중 유효 지수·참고자료 미확보", "skipped_missing_market_basis": "시장 기준자료 미확보"}
        slot_labels = {"morning": "장전", "midday": "장중", "after": "장후", "daily": "개인 요약"}
        lines += ["", "<b>보고 상태 · 최근 2일</b>"]
        for row in deliveries[:4]:
            lines.append(f"• {row['report_date'][5:]} {slot_labels[row['kind']]}: {status_labels.get(row['status'], '상태 확인 필요')}")
        if len(deliveries) > 4:
            lines.append(f"추가 {len(deliveries)-4}건은 비공개 보고 저장소에 보존")
    priority = {name: n for n, name in enumerate(("report_quality", "position_review", "daily_review", "morning_candidates", "evening_candidates", "batch_watchlist", "expert_quality", "llm_migration"))}
    grouped = {}
    for row in records:
        count, _ = grouped.get(row['kind'], (0, None))
        grouped[row['kind']] = (count+1, row)
    groups = sorted(grouped.items(), key=lambda item: priority.get(item[0], 99))
    lines += ["", "<b>유형별 최근 결과 · 전일 21시~당일 21시</b>"]
    if not records:
        lines.append("정기 결과 기록 없음 — 무거래·정상 가동을 뜻하지 않습니다.")
    for _, (count, row) in groups[:6]:
        suffix = f" ({count}건 중 최신)" if count > 1 else ""
        lines.append(f"• {_excerpt(row['text'], 110)}{suffix}")
    if len(groups) > 6:
        lines.append(f"추가 {len(groups)-6}개 유형과 전체 기록은 비공개 보고 저장소에 보존했습니다.")
    lines += ["", "<b>다음 확인</b>", "후보·판단은 주문/체결과 다릅니다. 실제 진입 전 현금·가격·위험 조건을 재확인합니다.", "주문·체결·중요 장애는 이 요약과 별도로 즉시 알립니다."]
    return "\n".join(lines)
