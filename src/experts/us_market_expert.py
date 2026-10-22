"""us-market-expert — 미국증시 미시 (SPY/QQQ/VIX/섹터 ETF/어닝)

데이터 소스 (신규 키 0개):
- yfinance: SPY, QQQ, IWM, VIX, 섹터 ETF (XLK/XLF/XLE/XLV/XLI/XLY/XLP/XLU/XLB/XLRE/XLC)
- Finnhub: 어닝 캘린더, 컨센서스
- Perplexity: FOMC dot plot, Powell 발언
"""

from __future__ import annotations

import asyncio
import math
import os
from datetime import datetime, timedelta
from numbers import Real
from typing import Any, Dict, List, Optional

import aiohttp
from loguru import logger

from .base import ExpertAgent
from .types import ExpertOpinion, RegimeBias


_INDEX_TICKERS = ["SPY", "QQQ", "IWM", "DIA"]
_SECTOR_TICKERS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]


def _finite_number(value: Any, *, positive: bool = False) -> Optional[float]:
    """불리언·비유한 수치는 결측, 가격은 양수만 인정한다."""
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(number) or (positive and number <= 0):
        return None
    return number


def _price_return(last: Any, base: Any) -> Optional[float]:
    """정해진 두 가격이 유효할 때만 수익률을 계산한다."""
    last = _finite_number(last, positive=True)
    base = _finite_number(base, positive=True)
    if last is None or base is None:
        return None
    result = _finite_number((last - base) / base * 100)
    return round(result, 2) if result is not None else None


def _price_mean(window: Any) -> Optional[float]:
    """고정 구간의 결측 봉을 생략해 다른 평균을 만들지 않는다."""
    if any(_finite_number(value, positive=True) is None for value in window):
        return None
    return _finite_number(window.mean(skipna=False), positive=True)


def _bar_date(value: Any) -> Optional[str]:
    """Use the history index's actual bar date; never substitute the fetch time."""
    try:
        return value.date().isoformat()
    except AttributeError:
        try:
            return value.isoformat()
        except AttributeError:
            return None


class USMarketExpert(ExpertAgent):
    name = "us_market_expert"
    refresh_minutes = 60
    cost_per_call_usd = 0.008

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._finnhub_key = os.getenv("FINNHUB_API_KEY", "")

    async def _analyze(self) -> ExpertOpinion:
        # 병렬 fetch
        indices, sector_result, vix, earnings, semis = await asyncio.gather(
            self._fetch_index_states(),
            self._fetch_sector_rs(),
            self._fetch_vix(),
            self._fetch_earnings_season(),
            self._fetch_semis_state(),
            return_exceptions=True,
        )

        indices = {
            ticker: {
                **state,
                **{key: _finite_number(state[key], positive=key == "last")
                   for key in ("last", "vs_ma50_pct", "vs_ma200_pct") if key in state},
            }
            for ticker, state in (indices.items() if isinstance(indices, dict) else [])
            if isinstance(state, dict)
        }
        # The fetcher returns numeric values plus their independently observed
        # daily-bar period.  Older test doubles/private callers may still
        # return only the numeric map; never describe those as 20 days.
        if isinstance(sector_result, tuple) and len(sector_result) == 2:
            sectors, sector_periods = sector_result
            sectors = sectors if isinstance(sectors, dict) else {}
            sector_periods = sector_periods if isinstance(sector_periods, dict) else {}
        else:
            sectors = sector_result if isinstance(sector_result, dict) else {}
            sector_periods = {
                ticker: {"label": "구간 미확인"} for ticker in sectors
            }
        # 수집기 외부에서 들어온 값도 순위·점수·결측 판정 전에 검증한다.
        sectors = {ticker: number for ticker, value in sectors.items()
                   if (number := _finite_number(value)) is not None}
        sector_periods = {ticker: period for ticker, period in sector_periods.items()
                          if ticker in sectors}
        vix = _finite_number(vix, positive=True)
        earnings = earnings if isinstance(earnings, dict) else {}
        semis = semis if isinstance(semis, dict) else {}
        semis = {
            **semis,
            **{key: _finite_number(semis[key], positive=key == "sox_last")
               for key in ("sox_1d_pct", "sox_5d_pct", "sox_last", "smh_5d_pct")
               if key in semis},
        }

        score = 0
        findings: List[str] = []

        # SPY MA50 위치
        spy = indices.get("SPY", {})
        spy_ma50 = spy.get("vs_ma50_pct")
        if isinstance(spy_ma50, (int, float)):
            if spy_ma50 > 2.0:
                score += 15
                findings.append(f"SPY MA50 +{spy_ma50:.1f}% — 강세 추세")
            elif spy_ma50 < -3.0:
                score -= 15
                findings.append(f"SPY MA50 {spy_ma50:.1f}% — 약세")
            elif spy_ma50 > 0:
                score += 5

        # SPY MA200
        spy_ma200 = spy.get("vs_ma200_pct")
        if isinstance(spy_ma200, (int, float)) and spy_ma200 < 0:
            score -= 20
            findings.append(f"SPY MA200 하회 {spy_ma200:.1f}% — 베어 마켓 위험")

        # VIX
        if isinstance(vix, (int, float)):
            if vix > 25:
                score -= 20
                findings.append(f"VIX {vix:.1f} — 위험회피")
            elif vix < 15:
                score += 10
                findings.append(f"VIX {vix:.1f} — 안정")

        # 섹터 로테이션
        if sectors:
            top = sorted(sectors.items(), key=lambda x: -x[1])[:3]
            bot = sorted(sectors.items(), key=lambda x: x[1])[:2]
            labels = set()
            for ticker, _ in top:
                period = sector_periods.get(ticker) or {}
                count = period.get("session_count") if isinstance(period, dict) else None
                labels.add(f"{count}세션" if type(count) is int and count > 0 else "구간 미확인")
            period_label = next(iter(labels)) if len(labels) == 1 else "구간 미확인"
            findings.append(
                f"수익률 상위 섹터 ({period_label}): {','.join(f'{s}({r:+.1f}%)' for s, r in top)}"
            )
            # Energy/Financials 강세 → 가치주 → bull
            top_sectors = [s for s, _ in top]
            if "XLE" in top_sectors or "XLF" in top_sectors:
                score += 5

        # 어닝시즌 평균 surprise
        surprise = _finite_number(earnings.get("avg_surprise"))
        if isinstance(surprise, (int, float)):
            if surprise > 0.05:
                score += 10
                findings.append(f"어닝 surprise 평균 +{surprise*100:.1f}%")
            elif surprise < 0:
                score -= 8
                findings.append(f"어닝 surprise 평균 {surprise*100:.1f}%")

        # 반도체 단기 약세 / 강세 (2026-06-07 추가)
        # 한국 시장 직접 영향 (삼전·하닉 60% spillover)
        sox_5d = semis.get("sox_5d_pct")
        if isinstance(sox_5d, (int, float)):
            if sox_5d <= -3.0:
                score -= 18
                findings.append(f"⚠️ SOX 5일 {sox_5d:+.1f}% — 미국 반도체 단기 약세")
            elif sox_5d <= -1.5:
                score -= 8
                findings.append(f"SOX 5일 {sox_5d:+.1f}% — 반도체 부진")
            elif sox_5d >= 3.0:
                score += 10
                findings.append(f"🟢 SOX 5일 {sox_5d:+.1f}% — 반도체 강세")
        sox_1d = semis.get("sox_1d_pct")
        if isinstance(sox_1d, (int, float)) and sox_1d <= -2.0:
            score -= 10
            findings.append(f"SOX 당일 {sox_1d:+.1f}% — 당일 급락")

        bias = (
            RegimeBias.BULL if score > 15
            else RegimeBias.BEAR if score < -15
            else RegimeBias.NEUTRAL
        )
        confidence = min(0.85, 0.35 + len(findings) * 0.08)

        # 영향 섹터 — 강세 섹터 top 3
        affected = []
        if sectors:
            top = sorted(sectors.items(), key=lambda x: -x[1])[:3]
            affected = [s for s, _ in top]

        # 2026-09-15 (T10 F16): score가 실제로 참조하는 핵심 입력(SPY 지수·VIX·SOX
        # 5일 추세) 3종이 전부 결측이면 "모른다"는 뜻 — insufficient로 표시한다.
        # 섹터 RS/어닝 surprise는 score에 대한 기여가 부수적(있으면 가점, 없어도
        # 나머지 3종만으로 판단 가능)이라 insufficient 판정 기준(core)에서는 뺐다.
        core_missing: List[str] = []
        if not isinstance(spy_ma50, (int, float)) and not isinstance(spy_ma200, (int, float)):
            core_missing.append("SPY 지수(MA50/MA200)")
        if not isinstance(vix, (int, float)):
            core_missing.append("VIX")
        if not isinstance(sox_5d, (int, float)):
            core_missing.append("반도체(SOX) 5일 추세")

        # 2026-09-15 (T10 B 리뷰 반영·2차 advisory): 섹터 RS(XLE/XLF 강세 +5)도
        # score를 실제로 움직이는 입력이므로(macro_economist의 cpi_yoy/semis_basket과
        # 동일한 "부수적이지만 score에 기여" 성격), core 3종이 다 있어도 섹터 RS가
        # 없으면 ok가 아니라 partial로 남긴다 — 6명 사이의 "score 기여 필드는 판정
        # 대상" 기준을 맞춘다. 어닝 surprise는 비수기에 정상적으로 비므로(연중 상시
        # 수집 불가) 판정 대상에서 계속 제외한다.
        bonus_missing: List[str] = []
        if not sectors:
            bonus_missing.append("섹터 RS")
        missing_inputs = core_missing + bonus_missing

        if len(core_missing) == 3:
            data_status = "insufficient"
        elif missing_inputs:
            data_status = "partial"
        else:
            data_status = "ok"

        return self._build_opinion(
            score=score,
            bias=bias,
            confidence=confidence,
            findings=findings,
            sectors=affected,
            raw=dict(
                indices=indices,
                vix=vix,
                top_sector=affected[0] if affected else None,
                avg_surprise=surprise,
                semis=semis,
                sector_returns=sectors,
                sector_periods=sector_periods,
            ),
            valid_hours=4,
            data_status=data_status,
            missing_inputs=missing_inputs,
        )

    # ─────────────────────────────────────────
    # 지수 상태 (SPY/QQQ/IWM/DIA)
    # ─────────────────────────────────────────
    async def _fetch_index_states(self) -> Dict[str, Dict[str, Any]]:
        def _one(ticker: str):
            try:
                import yfinance as yf
                hist = yf.Ticker(ticker).history(period="220d", interval="1d", auto_adjust=False)
                if hist.empty or len(hist) < 50:
                    return ticker, {}
                close = hist["Close"]
                last = _finite_number(close.iloc[-1], positive=True)
                if last is None:
                    return ticker, {}
                ma50 = _price_mean(close.tail(50))
                ma200 = _price_mean(close.tail(200))
                return ticker, {
                    "last": round(last, 2),
                    "vs_ma50_pct": _price_return(last, ma50),
                    "vs_ma200_pct": _price_return(last, ma200),
                    "as_of": _bar_date(close.index[-1]),
                    "source": f"yfinance:{ticker}",
                }
            except Exception:
                return ticker, {}

        results = await asyncio.gather(
            *[asyncio.to_thread(_one, t) for t in _INDEX_TICKERS],
            return_exceptions=True,
        )
        out: Dict[str, Dict[str, Any]] = {}
        for r in results:
            if isinstance(r, tuple) and len(r) == 2:
                tkr, data = r
                if data:
                    out[tkr] = data
        return out

    # ─────────────────────────────────────────
    # 섹터 ETF RS (20일 수익률)
    # ─────────────────────────────────────────
    async def _fetch_sector_rs(self):
        def _one(ticker: str):
            try:
                import yfinance as yf
                hist = yf.Ticker(ticker).history(period="30d", interval="1d", auto_adjust=False)
                if hist.empty or len(hist) < 5:
                    return ticker, None, None
                close = hist["Close"]
                base_idx = -20 if len(close) >= 20 else 0
                rs = _price_return(close.iloc[-1], close.iloc[base_idx])
                if rs is None:
                    return ticker, None, None
                return ticker, rs, {
                    "as_of": _bar_date(close.index[-1]),
                    "source": f"yfinance:{ticker}",
                    "start": _bar_date(close.index[base_idx]),
                    "session_count": min(19, len(close) - 1),
                }
            except Exception:
                return ticker, None, None

        results = await asyncio.gather(
            *[asyncio.to_thread(_one, t) for t in _SECTOR_TICKERS],
            return_exceptions=True,
        )
        out: Dict[str, float] = {}
        periods: Dict[str, Dict[str, Any]] = {}
        for r in results:
            if isinstance(r, tuple) and len(r) == 3 and r[1] is not None:
                out[r[0]] = r[1]
                periods[r[0]] = r[2]
        return out, periods

    # ─────────────────────────────────────────
    # 반도체 단기 추세 (2026-06-07 추가, 월요일 KR 갭 risk 캡처)
    # SOX 5일/1일 변동률 + SMH 보강
    # ─────────────────────────────────────────
    async def _fetch_semis_state(self) -> Dict[str, Any]:
        def _one(ticker: str) -> Dict[str, Optional[float]]:
            try:
                import yfinance as yf
                hist = yf.Ticker(ticker).history(period="10d", interval="1d", auto_adjust=False)
                if hist.empty or len(hist) < 2:
                    return {}
                close = hist["Close"]
                last = _finite_number(close.iloc[-1], positive=True)
                if last is None:
                    return {}
                d1 = _price_return(last, close.iloc[-2])
                d5 = _price_return(last, close.iloc[-6]) if len(close) >= 6 else None
                return {
                    "d1": d1,
                    "d5": d5,
                    "last": round(last, 2),
                }
            except Exception:
                return {}

        results = await asyncio.gather(
            asyncio.to_thread(_one, "^SOX"),
            asyncio.to_thread(_one, "SMH"),
            return_exceptions=True,
        )
        sox = results[0] if isinstance(results[0], dict) else {}
        smh = results[1] if isinstance(results[1], dict) else {}
        out: Dict[str, Any] = {}
        if sox:
            out["sox_1d_pct"] = sox["d1"]
            out["sox_5d_pct"] = sox["d5"]
            out["sox_last"] = sox["last"]
        if smh.get("d5") is not None:
            out["smh_5d_pct"] = smh["d5"]
        return out

    # ─────────────────────────────────────────
    # VIX
    # ─────────────────────────────────────────
    async def _fetch_vix(self) -> Optional[float]:
        def _sync():
            try:
                import yfinance as yf
                hist = yf.Ticker("^VIX").history(period="2d", interval="1d", auto_adjust=False)
                if hist.empty:
                    return None
                last = _finite_number(hist["Close"].iloc[-1], positive=True)
                return round(last, 2) if last is not None else None
            except Exception:
                return None
        return await asyncio.to_thread(_sync)

    # ─────────────────────────────────────────
    # 어닝시즌 평균 surprise (Finnhub)
    # ─────────────────────────────────────────
    async def _fetch_earnings_season(self) -> Dict[str, Any]:
        if not self._finnhub_key:
            return {}
        try:
            today = datetime.now().date()
            week_ago = today - timedelta(days=7)
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    "https://finnhub.io/api/v1/calendar/earnings",
                    params={
                        "from": week_ago.isoformat(),
                        "to": today.isoformat(),
                        "token": self._finnhub_key,
                    },
                ) as resp:
                    if resp.status != 200:
                        return {}
                    data = await resp.json()
            calendar = data.get("earningsCalendar", []) or []
            surprises = []
            for it in calendar:
                actual = it.get("epsActual")
                est = it.get("epsEstimate")
                if actual is None or est is None:
                    continue
                try:
                    # 기존 숫자 문자열 입력은 유지하되 bool/NaN/inf는 제외한다.
                    est_f = _finite_number(float(est) if isinstance(est, str) else est)
                    actual_f = _finite_number(float(actual) if isinstance(actual, str) else actual)
                    if est_f is None or actual_f is None:
                        continue
                    # 0에 가까운 EPS estimate는 outlier 유발 → 제외
                    if abs(est_f) < 0.1:
                        continue
                    surprise = _finite_number((actual_f - est_f) / abs(est_f))
                    if surprise is None:
                        continue
                    # ±50% 클램프 (단일 outlier가 평균 왜곡 방지)
                    surprises.append(max(-0.5, min(0.5, surprise)))
                except (ValueError, TypeError, OverflowError, ZeroDivisionError):
                    continue
            if not surprises:
                return {"count": 0}
            avg = sum(surprises) / len(surprises)
            return {"count": len(surprises), "avg_surprise": round(avg, 4)}
        except Exception as e:
            logger.debug(f"[US시장] earnings 실패: {e}")
            return {}
