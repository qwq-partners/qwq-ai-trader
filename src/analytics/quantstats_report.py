"""
quantstats 기반 성과 tear sheet 생성기

EquityTracker가 매일 저장하는 자산 스냅샷(equity_YYYYMMDD.json)의
daily_pnl_pct를 일별 수익률 시계열로 변환해 quantstats HTML 리포트를 만든다.

- 수익률 원천: equity 곡선 차분이 아니라 스냅샷의 daily_pnl_pct를 쓴다.
  입출금/외부계좌 편입으로 total_equity가 계단식으로 뛰어도 수익률이 왜곡되지 않는다.
- 벤치마크: KOSPI(1001) 종가 수익률. pykrx 실패 시 벤치마크 없이 생성한다.
- 결과물: ~/.cache/ai_trader/reports/quantstats_kr.html
  대시보드 /api/performance/quantstats 가 이 파일을 서빙한다 (기본 6시간 캐시).
"""

import glob
import json
import os
import time
from datetime import datetime
from pathlib import Path

from loguru import logger

JOURNAL_DIR = Path(os.getenv(
    "EQUITY_TRACKER_DIR",
    os.path.expanduser("~/.cache/ai_trader/journal")
))
REPORT_DIR = Path(os.path.expanduser("~/.cache/ai_trader/reports"))
REPORT_PATH = REPORT_DIR / "quantstats_kr.html"

MIN_SAMPLES = 20            # 최소 거래일 수 — 미만이면 통계가 무의미하다
DEFAULT_MAX_AGE_SEC = 6 * 3600


def _load_daily_returns():
    """equity 스냅샷 → 일별 수익률 Series (DatetimeIndex)"""
    import pandas as pd

    rows = []
    for fp in sorted(glob.glob(str(JOURNAL_DIR / "equity_*.json"))):
        try:
            with open(fp, encoding="utf-8") as f:
                snap = json.load(f)
        except Exception:
            continue
        d = snap.get("date")
        equity = snap.get("total_equity")
        pnl_pct = snap.get("daily_pnl_pct")
        if not d or equity is None or pnl_pct is None:
            continue
        # 재시작 직후 동기화 전 비정상 스냅샷 방어
        if float(equity) <= 0:
            continue
        rows.append((d, float(pnl_pct) / 100.0))

    if not rows:
        return pd.Series(dtype=float)

    s = pd.Series(
        [r[1] for r in rows],
        index=pd.to_datetime([r[0] for r in rows]),
        name="QWQ-KR",
    )
    # 같은 날짜 중복 저장 시 마지막 값(장마감 스냅샷) 사용
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s


def _load_kospi_benchmark(start, end):
    """KOSPI 지수 일별 수익률 — 실패해도 리포트는 만든다.

    pykrx는 KRX 인증(KRX_ID/PW) 없이는 지수 조회가 깨지는 환경이 있어
    (2026-04-21 stock_master 사고와 동일) FDR을 1차 소스로 쓴다.
    """
    import pandas as pd

    try:
        import FinanceDataReader as fdr
        df = fdr.DataReader("KS11", start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        if df is not None and not df.empty:
            bench = df["Close"].pct_change().dropna()
            bench.index = pd.to_datetime(bench.index)
            bench.name = "KOSPI"
            return bench
    except Exception as e:
        logger.warning(f"[성과리포트] FDR KOSPI 조회 실패 — pykrx 폴백: {e}")

    try:
        from pykrx import stock as pykrx_stock
        df = pykrx_stock.get_index_ohlcv_by_date(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "1001"
        )
        if df is None or df.empty:
            return None
        bench = df["종가"].pct_change().dropna()
        bench.index = pd.to_datetime(bench.index)
        bench.name = "KOSPI"
        return bench
    except Exception as e:
        logger.warning(f"[성과리포트] KOSPI 벤치마크 조회 실패 — 벤치마크 없이 생성: {e}")
        return None


def generate_quantstats_report(
    force: bool = False,
    max_age_sec: int = DEFAULT_MAX_AGE_SEC,
) -> Path:
    """
    tear sheet HTML 생성 (동기 — 호출자가 asyncio.to_thread로 감쌀 것).

    Returns:
        생성된(또는 캐시된) HTML 파일 경로

    Raises:
        ValueError: 표본 부족(MIN_SAMPLES 미만)
    """
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    if (
        not force
        and REPORT_PATH.exists()
        and (time.time() - REPORT_PATH.stat().st_mtime) < max_age_sec
    ):
        return REPORT_PATH

    # quantstats가 내부에서 matplotlib을 쓴다 — 헤드리스 백엔드 강제
    import matplotlib
    matplotlib.use("Agg")
    import quantstats as qs

    returns = _load_daily_returns()
    if len(returns) < MIN_SAMPLES:
        raise ValueError(
            f"표본 부족: 자산 스냅샷 {len(returns)}일 < 최소 {MIN_SAMPLES}일"
        )

    benchmark = _load_kospi_benchmark(returns.index[0], returns.index[-1])

    started = time.time()
    tmp_path = REPORT_PATH.with_suffix(".tmp.html")
    qs.reports.html(
        returns,
        benchmark=benchmark,
        output=str(tmp_path),
        title="QWQ AI Trader — KR 성과 리포트",
        download_filename="quantstats_kr.html",
    )
    # 생성 도중 서빙되는 일이 없도록 원자적 교체
    os.replace(tmp_path, REPORT_PATH)

    logger.info(
        f"[성과리포트] tear sheet 생성 완료: {len(returns)}일, "
        f"벤치마크={'KOSPI' if benchmark is not None else '없음'}, "
        f"{time.time() - started:.1f}s → {REPORT_PATH}"
    )
    return REPORT_PATH


def report_status() -> dict:
    """리포트 존재/신선도 조회 (대시보드 표시용)"""
    if not REPORT_PATH.exists():
        return {"exists": False}
    mtime = REPORT_PATH.stat().st_mtime
    return {
        "exists": True,
        "generated_at": datetime.fromtimestamp(mtime).isoformat(),
        "age_minutes": round((time.time() - mtime) / 60, 1),
    }
