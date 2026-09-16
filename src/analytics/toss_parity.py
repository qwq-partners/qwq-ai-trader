"""KIS ↔ 토스 대조 기록 (T12 Phase 1 — 기록 전용)

설계: docs/superpowers/plans/2026-09-15-toss-securities-fallback.md §5 Phase 1

이 모듈은 **기록과 요약만** 한다. 여기서 계산한 어떤 값도 다른 모듈이 소비하지
않는다(청산·사이징·주문·세션 판정 전부 무관). `summarize()`의 결과는 Phase 3
게이트 판정을 사람이 검토할 때 참고하는 입력일 뿐이다.

원장 레이아웃 (ledger_dir 기본 `~/.cache/ai_trader/toss_parity/`):
    price_YYYYMMDD.jsonl     — 현재가 대조 (bp 차이·지연·세션·실패)
    candle_YYYYMMDD.jsonl    — 일봉 대조 (정렬·마지막 날짜·종가 불일치)
    calendar_YYYYMMDD.jsonl  — 장운영 캘린더 대조 (utils/session.py 하드코딩 vs 토스)
"""

from __future__ import annotations

import json
import time
from datetime import date as _date, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from loguru import logger

from ..data.providers.toss.client import TossAPIError
from ..data.providers.toss.market_data import TossMarketData
from ..utils.session import KRSession, is_kr_market_holiday

LEDGER_DIR = Path.home() / ".cache" / "ai_trader" / "toss_parity"

# 가격 대조 경보 임계값 — 0.5%(설계 §1.2 실측 0.9% 차이 사례 참고)
PARITY_WARN_BP = 50.0

# 캘린더 하드코딩 경계 대조 대상 — (라벨, 토스 필드 경로, utils.session.py 하드코딩 값)
_CALENDAR_CHECKS = (
    ("pre_start", ("preMarket", "startTime"), KRSession.PRE_MARKET_START),
    ("pre_end", ("preMarket", "singlePriceAuctionStartTime"), KRSession.PRE_MARKET_END),
    ("regular_start", ("regularMarket", "startTime"), KRSession.REGULAR_START),
    ("regular_end", ("regularMarket", "singlePriceAuctionStartTime"), KRSession.REGULAR_END),
    ("next_start", ("afterMarket", "singlePriceAuctionEndTime"), KRSession.NEXT_START),
    ("next_end", ("afterMarket", "endTime"), KRSession.NEXT_END),
)

KisQuoteFn = Callable[[str], Awaitable[Optional[Dict[str, Any]]]]


# ── 공통 ─────────────────────────────────────────────────────────────────────
def _ledger_path(kind: str, day: str, ledger_dir: Optional[Path]) -> Path:
    base = Path(ledger_dir) if ledger_dir is not None else LEDGER_DIR
    return base / f"{kind}_{day.replace('-', '')}.jsonl"


def _append(kind: str, day: str, row: Dict[str, Any], ledger_dir: Optional[Path]) -> None:
    """1행 append. 실패해도 예외를 올리지 않는다 — 기록은 부가 기능이다."""
    try:
        path = _ledger_path(kind, day, ledger_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.warning(f"[토스대조] {kind} 원장 기록 실패 (무시): {e}")


def _diff_bp(kis_price: Optional[float], toss_price: Optional[float]) -> Optional[float]:
    if kis_price is None or toss_price is None or kis_price == 0:
        return None
    return (toss_price - kis_price) / kis_price * 10000.0


# ── 현재가 대조 ────────────────────────────────────────────────────────────────
async def record_price_parity(
    symbols: Sequence[str],
    kis_quote_fn: KisQuoteFn,
    toss_md: TossMarketData,
    *,
    now: datetime,
    ledger_dir: Optional[Path] = None,
) -> Dict[str, int]:
    """같은 틱에 KIS/토스 현재가를 받아 bp 차이·지연·세션을 원장에 append.

    `kis_quote_fn(symbol)` 은 호출부가 주입한다 — 이 함수는 KIS를 직접 호출하지
    않는다(캐시된 값이든 실호출이든 호출부 책임, 설계 §5 Phase1 "KIS 추가 호출 금지").
    토스는 `/prices` 다건 1콜로 배치 조회한다.

    반환값은 호출부 로그용 카운트({"compared", "toss_fail"})일 뿐 — 다른 모듈이
    소비하지 않는다.
    """
    day = now.strftime("%Y-%m-%d")
    session = KRSession().get_session(now).value
    unique = [s for s in dict.fromkeys(symbols) if s]
    if not unique:
        return {"compared": 0, "toss_fail": 0}

    t0 = time.monotonic()
    toss_map: Dict[str, Dict[str, Any]] = {}
    toss_error: Optional[str] = None
    try:
        toss_map = await toss_md.get_prices(unique)
    except TossAPIError as e:
        toss_error = str(e)
        logger.info(f"[토스대조] 현재가 조회 실패 (기록만): {e}")
    latency_ms = round((time.monotonic() - t0) * 1000, 1)

    toss_fail = 0
    for symbol in unique:
        kis_price: Optional[float] = None
        kis_as_of: Optional[datetime] = None
        try:
            kis_row = await kis_quote_fn(symbol)
        except Exception as e:
            kis_row = None
            logger.debug(f"[토스대조] {symbol} KIS 값 조회 실패 (무시): {e}")
        if kis_row:
            kis_price = kis_row.get("price")
            kis_as_of = kis_row.get("as_of")

        toss_row = toss_map.get(symbol)
        toss_price = toss_row.get("price") if toss_row else None
        toss_as_of = toss_row.get("as_of") if toss_row else None
        toss_ok = toss_error is None and toss_row is not None and toss_price is not None
        if not toss_ok:
            toss_fail += 1

        _append(
            "price",
            day,
            {
                "ts": now.isoformat(),
                "symbol": symbol,
                "session": session,
                "kis_price": kis_price,
                "kis_as_of": kis_as_of.isoformat() if kis_as_of else None,
                "toss_price": toss_price,
                "toss_as_of": toss_as_of.isoformat() if toss_as_of else None,
                "diff_bp": _diff_bp(kis_price, toss_price),
                "toss_latency_ms": latency_ms,
                "toss_ok": toss_ok,
                "error": toss_error,
            },
            ledger_dir,
        )
    return {"compared": len(unique), "toss_fail": toss_fail}


# ── 일봉 대조 ──────────────────────────────────────────────────────────────────
def _is_ascending(rows: Sequence[Dict[str, Any]]) -> bool:
    """`date` 필드가 오래된 순(비내림차순)인지 — 정렬 역전 회귀 탐지(설계 §3.3.5)"""
    dates = [r.get("date") for r in rows if r.get("date")]
    return all(dates[i] <= dates[i + 1] for i in range(len(dates) - 1))


def _close_mismatch_count(
    kis_rows: Sequence[Dict[str, Any]], toss_rows: Sequence[Dict[str, Any]], tol: float = 0.5
) -> Dict[str, Any]:
    kis_by_date = {r["date"]: r for r in kis_rows if r.get("date")}
    toss_by_date = {r["date"]: r for r in toss_rows if r.get("date")}
    common = sorted(set(kis_by_date) & set(toss_by_date))
    mismatches = 0
    for d in common:
        kc, tc = kis_by_date[d].get("close"), toss_by_date[d].get("close")
        if kc is None or tc is None or abs(kc - tc) > tol:
            mismatches += 1
    match_rate = None if not common else round(1 - mismatches / len(common), 4)
    return {"common_dates": len(common), "close_mismatch_count": mismatches, "match_rate": match_rate}


async def record_candle_parity(
    symbol: str,
    kis_candles: Sequence[Dict[str, Any]],
    toss_md: TossMarketData,
    *,
    days: int = 60,
    ledger_dir: Optional[Path] = None,
) -> None:
    """일봉 정렬·마지막 날짜·종가 일치율을 원장에 기록.

    `kis_candles` 는 호출부가 이미 가진(캐시된) KIS `get_daily_prices` 결과다 —
    이 함수는 KIS를 호출하지 않는다.
    """
    day = datetime.now().strftime("%Y-%m-%d")
    row: Dict[str, Any] = {
        "ts": datetime.now().isoformat(),
        "symbol": symbol,
        "kis_sorted_asc": _is_ascending(kis_candles),
        "kis_last_date": kis_candles[-1]["date"] if kis_candles else None,
    }
    try:
        toss_candles = await toss_md.get_candles(symbol, interval="1d", count=days)
    except TossAPIError as e:
        row.update({"toss_ok": False, "error": str(e)})
        _append("candle", day, row, ledger_dir)
        return

    toss_last_date = toss_candles[-1]["date"] if toss_candles else None
    row.update(
        {
            "toss_ok": True,
            "error": None,
            "toss_sorted_asc": _is_ascending(toss_candles),
            "toss_last_date": toss_last_date,
            "last_date_match": row["kis_last_date"] == toss_last_date,
        }
    )
    row.update(_close_mismatch_count(kis_candles, toss_candles))
    _append("candle", day, row, ledger_dir)


# ── 캘린더 대조 ────────────────────────────────────────────────────────────────
def _hm(iso_ts: Any) -> Optional[tuple]:
    if not iso_ts:
        return None
    try:
        parsed = datetime.fromisoformat(str(iso_ts))
    except ValueError:
        return None
    return (parsed.hour, parsed.minute)


async def record_calendar_parity(
    toss_md: TossMarketData,
    *,
    now: datetime,
    ledger_dir: Optional[Path] = None,
) -> None:
    """토스 장운영 캘린더 vs `utils/session.py` 하드코딩 경계를 대조·경고만 한다.

    세션 판정 자체는 바꾸지 않는다(설계 §6.3) — 불일치는 로그 WARNING + 원장 기록까지다.
    """
    day = now.strftime("%Y-%m-%d")
    our_holiday = is_kr_market_holiday(now.date())
    row: Dict[str, Any] = {"ts": now.isoformat(), "our_holiday": our_holiday}
    try:
        cal = await toss_md.get_market_calendar_kr()
    except TossAPIError as e:
        row.update({"toss_ok": False, "error": str(e)})
        _append("calendar", day, row, ledger_dir)
        return

    calendar = cal.get("calendar") or {}
    today = calendar.get("today") or {}
    integrated = today.get("integrated")
    toss_holiday = integrated is None
    mismatches: List[str] = []
    if our_holiday != toss_holiday:
        mismatches.append("holiday_mismatch")

    if not toss_holiday and isinstance(integrated, dict):
        for label, (section, field) in ((c[0], c[1]) for c in _CALENDAR_CHECKS):
            hardcoded = next(c[2] for c in _CALENDAR_CHECKS if c[0] == label)
            section_obj = integrated.get(section)
            iso_ts = section_obj.get(field) if isinstance(section_obj, dict) else None
            observed = _hm(iso_ts)
            if observed is None:
                continue  # 부분 휴장 등으로 결측 — 대조 불가, 단정하지 않는다
            if observed != hardcoded:
                mismatches.append(label)

    row.update({"toss_ok": True, "error": None, "toss_holiday": toss_holiday, "mismatches": mismatches})
    if mismatches:
        logger.warning(f"[토스대조] 캘린더 불일치 {mismatches} (세션 판정은 변경 없음)")
    _append("calendar", day, row, ledger_dir)


# ── 요약 (Phase 3 게이트 판정 입력) ─────────────────────────────────────────────
def _percentile(sorted_vals: List[float], pct: float) -> Optional[float]:
    if not sorted_vals:
        return None
    idx = max(0, min(len(sorted_vals) - 1, int(round(pct / 100 * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def summarize(ledger_dir: Optional[Path], day: str) -> Dict[str, Any]:
    """하루치 가격 대조 원장을 p50/p95 bp·초과비율·실패율·세션별 분포로 집계.

    `day`: "YYYY-MM-DD" 또는 date. 파일이 없으면 빈 집계를 반환한다(예외 없음).
    """
    day_str = day.isoformat() if isinstance(day, _date) else str(day)
    path = _ledger_path("price", day_str, ledger_dir)
    rows: List[Dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    total = len(rows)
    failed = sum(1 for r in rows if not r.get("toss_ok"))
    diffs = sorted(abs(r["diff_bp"]) for r in rows if r.get("diff_bp") is not None)
    by_session: Dict[str, int] = {}
    diffs_by_session: Dict[str, List[float]] = {}
    for r in rows:
        s = r.get("session") or "unknown"
        by_session[s] = by_session.get(s, 0) + 1
        if r.get("diff_bp") is not None:
            diffs_by_session.setdefault(s, []).append(abs(r["diff_bp"]))

    def _stats(vals: List[float]) -> Dict[str, Any]:
        vals = sorted(vals)
        return {
            "n": len(vals),
            "p50": _percentile(vals, 50),
            "p95": _percentile(vals, 95),
            "over_warn_bp_rate": round(sum(1 for d in vals if d > PARITY_WARN_BP) / len(vals), 4) if vals else None,
        }

    # Phase 3 진입 게이트(설계 §5 Phase 1)는 **정규장** 수치로만 판정한다 — 세션을 섞은 p95 는 참고값
    session_stats = {s: _stats(v) for s, v in diffs_by_session.items()}
    return {
        "day": day_str,
        "total": total,
        "failed": failed,
        "failure_rate": round(failed / total, 4) if total else None,
        "diff_bp_p50": _percentile(diffs, 50),
        "diff_bp_p95": _percentile(diffs, 95),
        "over_warn_bp_rate": round(sum(1 for d in diffs if d > PARITY_WARN_BP) / len(diffs), 4) if diffs else None,
        "by_session": by_session,
        "diff_by_session": session_stats,
        "regular": session_stats.get("regular", _stats([])),
    }
