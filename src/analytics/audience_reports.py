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


def render_market_report(kind: str, snapshot: MarketSnapshot, now: datetime) -> str | None:
    """No bot/account argument. Missing source time is never filled from receipt."""
    now = now.astimezone(KST)
    labels = {"morning": "장전 체크", "midday": "장중 핵심 변화", "after": "장후 점검"}
    if kind not in labels:
        raise ValueError("unknown market slot")
    indices = current_indices(snapshot, now, midday=kind == "midday")
    if kind == "midday" and not indices:
        return None
    lines = [f"🇰🇷 <b>한국시장 · {labels[kind]}</b>", f"{now:%m/%d %H:%M} KST", ""]
    if indices:
        positive = sum(pct > 0 for _, _, pct, _ in indices)
        direction = "상승" if positive == len(indices) else "하락" if all(pct < 0 for _, _, pct, _ in indices) else "혼조·보합"
        lines.extend([f"<b>핵심</b> 확인된 지수 {len(indices)}개는 {direction} 흐름입니다.", "전체 시장 폭·수급과는 구분해 보세요.", "", "<b>국내 지수 · 전일 종가 대비</b>"])
        for row, price, pct, observed in indices[:2]:
            lines.append(f"• {_text(row.get('label'), 20)} {price:,.2f} ({pct:+.2f}%) · {observed:%m/%d %H:%M}")

    sectors = []
    for row in snapshot.sectors:
        pct = number(row.get("r1"))
        if pct is not None and row.get("as_of") == snapshot.kr_basis_date and row.get("source") not in (None, "", "unknown"):
            sectors.append((row, pct))
    if sectors:
        sectors.sort(key=lambda pair: pair[1], reverse=True)
        lines.extend(["", f"<b>한국 상장 섹터 ETF · {snapshot.kr_basis_date}</b>", f"확인 {len(sectors)}개 중 최근 1세션 등락 (전체 업종 순위 아님)"])
        selected = sectors[:2] + (sectors[-2:] if len(sectors) > 3 else sectors[2:])
        for row, pct in selected:
            lines.append(f"• {_text(row.get('name'), 24)} {pct:+.2f}%")
        if not indices:
            lines[3:3] = ["<b>핵심</b> 국내 섹터 ETF의 확인된 최근 움직임부터 살펴봅니다.", "당일 시장 전체 방향은 지수·수급 확인 뒤 판단하세요."]

    us = []
    if kind == "morning" and snapshot.us_basis_date:
        from .us_report_data import observe_us_quote
        for symbol, label in (("^GSPC", "S&P500"), ("^IXIC", "나스닥"), ("^SOX", "미 반도체 SOX")):
            value = observe_us_quote(snapshot.us_quotes.get(symbol), now=now)
            if value and (value.get("date") or "").replace(".", "-") == snapshot.us_basis_date:
                us.append(f"• {label} {value['pct']:+.2f}%")
        if us:
            if not indices and not sectors:
                lines += ["<b>핵심</b> 밤사이 확인된 미국 지수 변화입니다.", "한국 시가·섹터 강세를 예측하는 수치는 아닙니다."]
            lines += ["", f"<b>국내 개장 전 참고 · 미국 {snapshot.us_basis_date}</b>", *us]
    if not indices and not sectors and not us:
        return None
    lines += ["", "<b>다음 확인</b>", "• 지수 방향이 개별 종목 거래대금·수급과 함께 이어지는지 확인", "• 반도체 대형주와 그 외 업종의 움직임을 나눠 확인", "", "자료 범위: 확인 시점이 있는 값만 표시. 시장 전체 상승·하락 종목 수와 당일 누적 수급은 미확보.", "출처: KIS 지수 / 한국 상장 ETF 일봉 / Yahoo 미국 지수 중 표시 자료"]
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
        status_labels = {"attempted": "처리 중단·결과 미확인", "unknown": "접수 여부 미확인", "rejected": "발송 거절", "failed_validation": "본문 검증 실패", "failed_configuration": "발송 설정 누락", "skipped_missing_current_market_basis": "장중 원천시각 미확보", "skipped_missing_market_basis": "시장 기준자료 미확보"}
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
