"""KOSPI 일봉 공용 로더 — 날짜·이력 검증을 한 곳에서 한다 (2026-09-28).

swing_screener 에 있던 검증을 옮겼다. 스크리너 레짐·변동성 타게팅·수확 shadow 가 같은
계약을 쓴다: 당일 부분봉 또는 직전 KR 거래일 봉만 fresh, 50행 이상·날짜 엄격 증가·
종가 유한 양수. 정렬/보간/dropna 로 자료를 보정하지 않는다.

원천 순서: FDR "YAHOO:^KS11" → "KS11". FDR 0.9.110 의 "KS11" 은 GitHub 캐시 CSV 를
읽으며 09-17 장중 부분봉에서 멈춘 채 예외 없이 오래된 프레임을 돌려줬다(09-28 진단).
FALLBACK_SOURCES 는 끝에 KODEX200(069500)을 붙인다 — 변동성 타게팅(09-28)·수확 shadow·스크리너
레짐(09-29, Yahoo 가 09-28 봉을 빠뜨려 NaN 거부 + KS11 정지로 두 원천이 모두 막힌 날).
한계: 상류가 장중 부분봉을 직전 거래일 날짜로 남기면 KS11 도 날짜 검사는 통과한다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from math import isfinite
from typing import Callable, Optional

from loguru import logger

from src.utils.session import KST, is_kr_market_holiday
from src.utils.volatility_targeting import RET_OUTLIER_ABS

SOURCES = ("YAHOO:^KS11", "KS11")
# KOSPI 원천이 모두 신선하지 않을 때만 쓰는 KODEX200 최후 대체 — 같은 검증
FALLBACK_SOURCES = SOURCES + ("069500",)


def proxy_outlier(closes, dates, lookback: int) -> Optional[str]:
    """대용(069500) 종가의 끝 lookback 개 일수익률 중 |r| > RET_OUTLIER_ABS 첫 건 ("날짜 +24.2%") 또는 None.

    FDR 069500 에는 +24.2%/일 오염 실측 이력이 있다. 봉을 지우거나 보간하지 않는다 — 소비 창 안이면
    호출부가 원천을 채택하지 않고, 창 밖의 오래된 오염은 보지 않는다 (2026-09-29).
    """
    for i in range(max(1, len(closes) - lookback), len(closes)):
        ret = closes[i] / closes[i - 1] - 1
        if abs(ret) > RET_OUTLIER_ABS:
            return f"{dates[i]} {ret:+.1%}"
    return None


def benchmark_date_status(last_bar_date: Optional[date], now: datetime):
    """당일 부분봉 또는 직전 한국 거래일 봉만 허용한다."""
    if not isinstance(last_bar_date, date):
        return "unknown", "last_bar_date_unknown"
    today = now.date()
    if last_bar_date > today:
        return "future", "last_bar_in_future"
    if is_kr_market_holiday(last_bar_date):
        return "unknown", "last_bar_not_kr_session"
    previous = today - timedelta(days=1)
    while is_kr_market_holiday(previous):
        previous -= timedelta(days=1)
    if last_bar_date < previous:
        return "stale", "older_than_previous_kr_session"
    return "fresh", "current_day_partial" if last_bar_date == today else "previous_kr_session"


def validate_benchmark(data, source: str, now: datetime):
    """자료를 정렬/보간으로 보정하지 않고 날짜·가격 계약을 검증한다.

    Returns: (closes, dates, status) — fresh 가 아니면 closes/dates 는 빈 리스트.
    """
    import pandas as pd

    result = {"status": "missing", "source": source, "last_bar_date": None,
              "loaded_at": None, "reason": "no_data"}
    if data is None or len(data) == 0:
        return [], [], result
    result.update(status="unknown", reason="invalid_history")
    try:
        if len(data) < 50 or "Close" not in data:
            return [], [], result
        dates = []
        for value in data.index:
            if pd.isna(value) or not isinstance(value, date):
                return [], [], result
            if getattr(value, "tzinfo", None) is not None:
                value = value.astimezone(KST)
            dates.append(value.date() if hasattr(value, "date") else value)
        result["last_bar_date"] = dates[-1]
        if any(a >= b for a, b in zip(dates, dates[1:])):
            result["reason"] = "unordered_or_duplicate_dates"
            return [], [], result
        closes = [float(value) for value in data["Close"]]
        if any(not isfinite(value) or value <= 0 for value in closes):
            result["reason"] = "invalid_close_history"
            return [], [], result
        result["status"], result["reason"] = benchmark_date_status(dates[-1], now)
        if result["status"] == "fresh":
            result["loaded_at"] = now
            return closes, dates, result
    except (TypeError, ValueError, OverflowError):
        result.update(status="unknown", reason="invalid_history")
    return [], [], result


def keep_failure(previous: Optional[dict], status: dict) -> dict:
    """대체 소스 결측으로 먼저 확인한 stale/invalid 근거를 덮지 않는다."""
    if previous is None or status["status"] != "missing":
        return status
    return previous


def fetch_failed_status(source: str) -> dict:
    return {"status": "missing", "source": source, "last_bar_date": None,
            "loaded_at": None, "reason": "fetch_failed"}


def _fdr_fetch(symbol: str, start: str):
    import FinanceDataReader as fdr
    return fdr.DataReader(symbol, start)


def load_kospi_daily(start: str, now: datetime, *, sources=SOURCES,
                     fetch: Optional[Callable] = None):
    """원천을 순서대로 읽어 첫 fresh 종가를 돌려준다 (동기 — 호출부가 스레드로 감싼다).

    Returns: (closes Series(날짜 인덱스) | None, status dict)
    """
    import pandas as pd

    fetch = fetch if fetch is not None else _fdr_fetch
    if now.tzinfo is not None:
        now = now.astimezone(KST).replace(tzinfo=None)
    failure = None
    for symbol in sources:
        source = f"FDR:{symbol}"
        try:
            closes, dates, status = validate_benchmark(fetch(symbol, start), source, now)
        except Exception as exc:
            status = fetch_failed_status(source)
            logger.warning(f"[KOSPI벤치마크] {source} 조회 실패: {type(exc).__name__}")
        if status["status"] == "fresh":
            return pd.Series(closes, index=pd.DatetimeIndex(dates)), status
        failure = keep_failure(failure, status)
        logger.warning(
            f"[KOSPI벤치마크] 자료 제외: source={source}, status={status['status']}, "
            f"마지막 봉={status['last_bar_date']}, reason={status['reason']}"
        )
    return None, failure


def _fdr_fetch_range(symbol: str, start: str, end: Optional[str]):
    import FinanceDataReader as fdr
    return fdr.DataReader(symbol, start, end)


def load_kospi_history(start: str, end: Optional[str] = None, *, sources=SOURCES,
                       fetch: Optional[Callable] = None, now: Optional[datetime] = None):
    """백테스트·분석용 KOSPI 일봉 프레임(과거 구간) — 종가가 전부 유한 양수인 첫 원천. 신선도로 거르지는 않는다.

    end 는 포함한다. FDR Yahoo 리더는 end 를 **로컬 자정** 기준 period2 로 넘겨 KST 에선 end 가 빠지고
    UTC 에선 다음 거래일이 섞인다 → 하루 더 조회한 뒤 end 이후 행을 잘라 시간대와 무관하게 맞춘다.
    end 가 없으면(=오늘까지) 마지막 봉이 직전 KR 거래일보다 오래됐을 때 경고한다 — KS11 캐시가 09-17 에서
    예외 없이 멈춘 것 같은 정지를 드러내려고(2026-09-28, scripts/ 백테스트 벤치마크 교체).
    Returns: (DataFrame | None, "FDR:<기호>" | None)
    """
    import numpy as np
    import pandas as pd

    fetch = fetch if fetch is not None else _fdr_fetch_range
    for symbol in sources:
        stop = end
        if end is not None and symbol.startswith("YAHOO:"):
            stop = (date.fromisoformat(end[:10]) + timedelta(days=1)).isoformat()
        try:
            df = fetch(symbol, start, stop)
        except Exception as exc:
            logger.warning(f"[KOSPI벤치마크] FDR:{symbol} 조회 실패: {type(exc).__name__}")
            continue
        if df is None or len(df) == 0:
            continue
        if end is not None:
            idx = df.index
            if getattr(idx, "tz", None) is not None:   # 시간대가 붙은 인덱스는 KST 거래일로 맞춘다
                idx = idx.tz_convert(KST).tz_localize(None)
            df = df[idx.normalize() <= pd.Timestamp(end[:10])]
            if len(df) == 0:
                continue
        # 반환 구간 종가가 비유한/0 이하면 원천째 건너뛴다 — dropna·보간 금지(09-28 계약). 09-29 Yahoo 09-28 NaN 행이
        # 그대로 백테스트 레짐(MA200 NaN → NEUTRAL)과 영구 레짐 캐시로 들어갈 수 있었다.
        close = pd.to_numeric(df["Close"], errors="coerce") if "Close" in df else None
        if close is None or not np.isfinite(close.to_numpy(dtype=float)).all() or (close <= 0).any():
            logger.warning(f"[KOSPI벤치마크] FDR:{symbol} 종가에 NaN/inf/0 이하 값 — 원천 제외")
            continue
        if end is None:
            last = df.index[-1]
            last = last.date() if hasattr(last, "date") else last
            ref = now if now is not None else datetime.now(KST).replace(tzinfo=None)
            if benchmark_date_status(last, ref)[0] == "stale":
                logger.warning(f"[KOSPI벤치마크] FDR:{symbol} 마지막 봉 {last} — 직전 거래일보다 오래됨(원천 정지 의심)")
        return df, f"FDR:{symbol}"
    return None, None
