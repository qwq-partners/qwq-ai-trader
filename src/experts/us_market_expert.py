"""us-market-expert — 미국증시 미시 (SPY/QQQ/VIX/섹터 ETF/어닝)

데이터 소스 (신규 키 0개):
- yfinance: SPY, QQQ, IWM, VIX, 섹터 ETF (XLK/XLF/XLE/XLV/XLI/XLY/XLP/XLU/XLB/XLRE/XLC)
- Finnhub: 어닝 캘린더, 컨센서스
- Perplexity: FOMC dot plot, Powell 발언
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiohttp
from loguru import logger

from .base import ExpertAgent
from .types import ExpertOpinion, RegimeBias


_INDEX_TICKERS = ["SPY", "QQQ", "IWM", "DIA"]
_SECTOR_TICKERS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]


class USMarketExpert(ExpertAgent):
    name = "us_market_expert"
    refresh_minutes = 60
    cost_per_call_usd = 0.008

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._finnhub_key = os.getenv("FINNHUB_API_KEY", "")

    async def _analyze(self) -> ExpertOpinion:
        # 병렬 fetch
        indices, sectors, vix, earnings, semis = await asyncio.gather(
            self._fetch_index_states(),
            self._fetch_sector_rs(),
            self._fetch_vix(),
            self._fetch_earnings_season(),
            self._fetch_semis_state(),
            return_exceptions=True,
        )

        indices = indices if isinstance(indices, dict) else {}
        sectors = sectors if isinstance(sectors, dict) else {}
        vix = vix if isinstance(vix, (int, float)) else None
        earnings = earnings if isinstance(earnings, dict) else {}
        semis = semis if isinstance(semis, dict) else {}

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
            findings.append(
                f"강세 섹터: {','.join(f'{s}({r:+.1f}%)' for s, r in top)}"
            )
            # Energy/Financials 강세 → 가치주 → bull
            top_sectors = [s for s, _ in top]
            if "XLE" in top_sectors or "XLF" in top_sectors:
                score += 5

        # 어닝시즌 평균 surprise
        surprise = earnings.get("avg_surprise")
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
                findings.append(f"⚠️ SOX 5일 {sox_5d:+.1f}% — 반도체 단기 약세 (KR 직격)")
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
            ),
            valid_hours=4,
        )

    # ─────────────────────────────────────────
    # 지수 상태 (SPY/QQQ/IWM/DIA)
    # ─────────────────────────────────────────
    async def _fetch_index_states(self) -> Dict[str, Dict[str, float]]:
        def _one(ticker: str):
            try:
                import yfinance as yf
                hist = yf.Ticker(ticker).history(period="220d", interval="1d", auto_adjust=False)
                if hist.empty or len(hist) < 50:
                    return ticker, {}
                close = hist["Close"]
                last = float(close.iloc[-1])
                ma50 = float(close.tail(50).mean())
                ma200 = float(close.tail(200).mean()) if len(close) >= 200 else float(close.mean())
                return ticker, {
                    "last": round(last, 2),
                    "vs_ma50_pct": round((last - ma50) / ma50 * 100, 2) if ma50 > 0 else 0.0,
                    "vs_ma200_pct": round((last - ma200) / ma200 * 100, 2) if ma200 > 0 else 0.0,
                }
            except Exception:
                return ticker, {}

        results = await asyncio.gather(
            *[asyncio.to_thread(_one, t) for t in _INDEX_TICKERS],
            return_exceptions=True,
        )
        out: Dict[str, Dict[str, float]] = {}
        for r in results:
            if isinstance(r, tuple) and len(r) == 2:
                tkr, data = r
                if data:
                    out[tkr] = data
        return out

    # ─────────────────────────────────────────
    # 섹터 ETF RS (20일 수익률)
    # ─────────────────────────────────────────
    async def _fetch_sector_rs(self) -> Dict[str, float]:
        def _one(ticker: str):
            try:
                import yfinance as yf
                hist = yf.Ticker(ticker).history(period="30d", interval="1d", auto_adjust=False)
                if hist.empty or len(hist) < 5:
                    return ticker, None
                close = hist["Close"]
                last = float(close.iloc[-1])
                if len(close) >= 20:
                    base = float(close.iloc[-20])
                else:
                    base = float(close.iloc[0])
                rs = (last - base) / base * 100 if base > 0 else 0.0
                return ticker, round(rs, 2)
            except Exception:
                return ticker, None

        results = await asyncio.gather(
            *[asyncio.to_thread(_one, t) for t in _SECTOR_TICKERS],
            return_exceptions=True,
        )
        out: Dict[str, float] = {}
        for r in results:
            if isinstance(r, tuple) and len(r) == 2 and r[1] is not None:
                out[r[0]] = r[1]
        return out

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
                last = float(close.iloc[-1])
                prev = float(close.iloc[-2])
                base_5d = float(close.iloc[-6]) if len(close) >= 6 else float(close.iloc[0])
                d1 = (last - prev) / prev * 100 if prev > 0 else 0.0
                d5 = (last - base_5d) / base_5d * 100 if base_5d > 0 else 0.0
                return {"d1": round(d1, 2), "d5": round(d5, 2), "last": round(last, 2)}
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
        if sox.get("d1") is not None:
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
                return round(float(hist["Close"].iloc[-1]), 2)
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
                    est_f = float(est)
                    # 0에 가까운 EPS estimate는 outlier 유발 → 제외
                    if abs(est_f) < 0.1:
                        continue
                    surprise = (float(actual) - est_f) / abs(est_f)
                    # ±50% 클램프 (단일 outlier가 평균 왜곡 방지)
                    surprises.append(max(-0.5, min(0.5, surprise)))
                except (ValueError, ZeroDivisionError):
                    continue
            if not surprises:
                return {"count": 0}
            avg = sum(surprises) / len(surprises)
            return {"count": len(surprises), "avg_surprise": round(avg, 4)}
        except Exception as e:
            logger.debug(f"[US시장] earnings 실패: {e}")
            return {}
