"""보고서에 붙이는 현재 제약과 발송 중복 보호. 주문 판단을 변경하지 않는다."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path


def render_runtime_context(bot, *, kill_status=None):
    """현재 메모리의 제약만 요약한다. 금액·종목·주문 권한은 발행하지 않는다."""
    if kill_status is None:
        try:
            from ..risk.kill_switch import status
            kill_status = status("KR")
        except Exception:
            kill_status = {}
    if not isinstance(kill_status, dict):
        kill_status = {}
    if kill_status.get("halt_all") is True:
        mode = "매수·매도 중지 — 보유 위험 확인 우선"
    elif kill_status.get("halt_buy") is True:
        mode = "신규 매수 중지 — 보유 관리·후보 관찰 우선"
    elif kill_status.get("halt_buy") is False:
        mode = "매수 중지 플래그 없음 — 주문 전 별도 위험 검사 필요"
    else:
        mode = "매수 중지 상태 미확인"
    cash_note = "현금 상태 미확인 — 신규 진입 판단 보류"
    try:
        # 잔고 API를 새로 부르지 않으며, 미검증 메모리 값은 사용하지 않는다.
        if getattr(getattr(bot, "risk_manager", None), "_cash_verified", None) is True:
            cash = Decimal(str(bot.engine.get_available_cash()))
            if cash.is_finite() and cash >= 0:
                cash_note = ("가용현금 없음 — 후보는 관찰용" if cash == 0 else
                             "엔진 현금 여유 있음 — 예약·최소 주문 조건은 진입 때 재확인")
    except Exception:
        pass
    return f"현재 대응: {mode}\n{cash_note}\n보유 판단: 개별 공시·수급 변화와 기존 청산 조건을 확인"


def claim_weekly_report(now: datetime, directory: Path) -> bool:
    """토요일 슬롯의 ISO 연도·주차당 한 번 시도. 실패·재시작에도 자동 재호출하지 않는다."""
    if now.weekday() != 5 or now.hour != 0 or not 5 <= now.minute < 10:
        return False
    year, week, _ = now.isocalendar()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"principles-{year}-W{week:02d}.json"
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump({"attempted_at": now.isoformat(), "status": "attempted"}, handle)
    except FileExistsError:
        return False
    return True


def current_morning_brief(data, now):
    """같은 한국 날짜의 과거 6시간 캐시만 결합한다. 옛 naive 시각은 KST로 해석."""
    from zoneinfo import ZoneInfo
    kst = ZoneInfo("Asia/Seoul")
    try:
        current = now.replace(tzinfo=kst) if now.tzinfo is None else now.astimezone(kst)
        generated = datetime.fromisoformat(data["generated_at"])
        generated = generated.replace(tzinfo=kst) if generated.tzinfo is None else generated.astimezone(kst)
        text = data.get("text")
        if (isinstance(text, str) and text.strip()
                and generated.date() == current.date()
                and data.get("kr_date", current.date().isoformat()) == current.date().isoformat()
                and 0 <= (current - generated).total_seconds() < 6 * 3600):
            return text
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    return None


def _plain(value, limit=120):
    from html import escape
    return escape(' '.join(str(value).split())[:limit], quote=False)


def _price(value):
    try:
        number = Decimal(str(value))
        return f'{number:,.0f}원' if number.is_finite() and number > 0 else '미확인'
    except (TypeError, ValueError, InvalidOperation):
        return '미확인'


def render_pending_watchlist(pending, *, now=None):
    """배치가 만든 계획값을 그대로 표시한다. 신규 주문·계획값 생성은 하지 않는다."""
    now = now or datetime.now()
    lines = ['🔎 <b>스윙 후보와 확인 조건</b>', f'작성 {now:%Y-%m-%d %H:%M} KST',
             f'현재 대기 후보 {len(pending)}개 · 주문 승인 아님',
             '계획가는 현재 호가·체결가가 아닙니다. 실행 전 현금·시장 방어·호가 조건을 다시 검사합니다.', '']
    if not pending:
        lines.append('시장 전체에 기회가 없다는 뜻은 아님 — 다음 스캔에서 선정 근거와 제외 사유 확인')
    for item in pending[:10]:
        lines.append(f'• <b>{_plain(item.name, 30)}</b> <code>{_plain(item.symbol, 12)}</code> · {_plain(item.strategy, 30)}')
        lines.append(f'  선정 근거: {_plain(item.reason or "근거 미기록")}')
        lines.append(f'  계획 기준 {_price(item.entry_price)} · 진입 상한 {_price(item.max_entry_price)}')
        if getattr(item, 'entry_mode', '') == 'breakout':
            lines.append(f'  확인 조건: 돌파 기준 {_price(getattr(item, "breakout_trigger", None))} 도달 후 추가 검사')
        else:
            lines.append('  확인 조건: 실행 시 허용 가격대·수급·거래량 재검사')
        lines.append(f'  기존 계획: 목표 {_price(item.target_price)} / 손절 {_price(item.stop_price)} · 체결·도달 여부 미평가')
        lines.append(f'  생성 {_plain(item.created_at, 32)} · 유효기한 {_plain(item.expires_at, 32)}')
        lines.append('  제외 조건: 유효기한 경과·상한 초과·필수 입력 결측 또는 위험 검사 차단')
    if len(pending) > 10:
        lines.append(f'표시 10/{len(pending)}개 — 나머지는 다음 후보 기록에서 확인')
    return '\n'.join(lines)
