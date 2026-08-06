"""weekend-signal-expert — 주말·야간 갭 risk 평가 (2026-06-07 신규)

목적:
  금요일 종가 ~ 월요일 개장 사이 KR 시장이 받을 영향 평가.
  평일에도 야간(미국장 진행 중) 캡처용으로 동작.

데이터 소스 (신규 키 0개):
  - KIS: KOSPI200 야간선물 직접 시세 (A01609/CM, 2026-08-06~ — KR 갭 직접)
  - yfinance:
    · ES=F, NQ=F : S&P/NASDAQ 선물 (KR 갭 1차 동력)
    · NKD=F : 니케이 야간선물 (KIS 실패 시 ×0.7 프록시 폴백)
    · KRW=X : 원/달러 (외환 risk)
    · ^VIX : 변동성
    · BTC-USD : risk sentiment proxy
    · ZB=F : 미국 30년 채권 선물 (안전자산 수요)

점수 룰:
  · 모든 risk 항목 합산 → -100 ~ +100 clamp
  · BEAR 우선 (월요일 갭다운 위험에 민감)
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger

from .base import ExpertAgent
from .types import ExpertOpinion, RegimeBias


class WeekendSignalExpert(ExpertAgent):
    name = "weekend_signal_expert"
    refresh_minutes = 30           # 자주 갱신 (야간/주말 신호 빠르게 반영)
    cost_per_call_usd = 0.002      # yfinance만 사용

    async def _analyze(self) -> ExpertOpinion:
        signals = await self._fetch_all_signals()

        score = 0
        findings: List[str] = []

        # 1) US 야간선물 (ES/NQ) — KR 갭 1차 동력
        es = signals.get("es_pct")
        nq = signals.get("nq_pct")
        if isinstance(es, (int, float)):
            if es <= -1.5:
                score -= 20
                findings.append(f"⚠️ S&P 야간선물 {es:+.2f}% — 위험회피")
            elif es <= -0.5:
                score -= 8
                findings.append(f"S&P 야간선물 {es:+.2f}% (약세)")
            elif es >= 1.0:
                score += 10
                findings.append(f"🟢 S&P 야간선물 {es:+.2f}% (강세)")
        if isinstance(nq, (int, float)):
            if nq <= -2.0:
                score -= 15
                findings.append(f"⚠️ NASDAQ 야간선물 {nq:+.2f}% — IT 약세")
            elif nq <= -0.7:
                score -= 6
                findings.append(f"NASDAQ 야간선물 {nq:+.2f}% (약세)")
            elif nq >= 1.5:
                score += 8
                findings.append(f"🟢 NASDAQ 야간선물 {nq:+.2f}% (강세)")

        # 2) KOSPI200/니케이 야간선물 — KR 갭 직접
        kr = signals.get("kr_futures_pct")
        if isinstance(kr, (int, float)):
            if kr <= -2.0:
                score -= 25
                findings.append(f"⚠️ KR/JP 야간선물 {kr:+.2f}% — 월요일 갭다운 큰 위험")
            elif kr <= -1.0:
                score -= 12
                findings.append(f"KR/JP 야간선물 {kr:+.2f}% — 갭다운 주의")
            elif kr >= 1.5:
                score += 12
                findings.append(f"🟢 KR/JP 야간선물 {kr:+.2f}% — 갭업 기대")

        # 3) 원/달러 (외환 risk)
        krw_pct = signals.get("krw_pct")
        krw_last = signals.get("krw_last")
        if isinstance(krw_pct, (int, float)) and isinstance(krw_last, (int, float)):
            if krw_pct >= 1.0 or krw_last >= 1450:
                score -= 12
                findings.append(f"⚠️ KRW {krw_last:.0f}원 ({krw_pct:+.2f}%) — 외환 압박")
            elif krw_pct >= 0.5:
                score -= 5
                findings.append(f"KRW {krw_last:.0f}원 ({krw_pct:+.2f}%) — 약세")
            elif krw_pct <= -0.5:
                score += 5
                findings.append(f"KRW {krw_last:.0f}원 ({krw_pct:+.2f}%) — 원화 강세")

        # 4) VIX 절대치
        vix = signals.get("vix_last")
        if isinstance(vix, (int, float)):
            if vix >= 25:
                score -= 15
                findings.append(f"VIX {vix:.1f} — 변동성 체제")
            elif vix >= 20:
                score -= 6
                findings.append(f"VIX {vix:.1f} — 경계")
            elif vix <= 14:
                score += 5
                findings.append(f"VIX {vix:.1f} — 안정")

        # 5) BTC (risk sentiment proxy)
        btc_pct = signals.get("btc_pct")
        if isinstance(btc_pct, (int, float)):
            if btc_pct <= -5.0:
                score -= 10
                findings.append(f"BTC {btc_pct:+.2f}% — 글로벌 risk-off")
            elif btc_pct >= 5.0:
                score += 5
                findings.append(f"BTC {btc_pct:+.2f}% — risk-on")

        # 6) 30년 채권 (안전자산 수요 — bond up + equity down 동조)
        zb_pct = signals.get("zb_pct")
        if isinstance(zb_pct, (int, float)):
            if zb_pct >= 1.0:
                score -= 5
                findings.append(f"미 30년 채권 +{zb_pct:.2f}% — 안전자산 수요")

        # clamp
        score = max(-100, min(100, score))

        # 평일에는 임계를 조금 더 엄격하게 (현물 데이터로 보강된다고 가정)
        is_weekend = datetime.now().weekday() >= 5
        bull_thr, bear_thr = (10, -10) if is_weekend else (15, -15)
        bias = (
            RegimeBias.BULL if score > bull_thr
            else RegimeBias.BEAR if score < bear_thr
            else RegimeBias.NEUTRAL
        )

        # 데이터 수집 신뢰도
        valid_signals = sum(
            1 for k in ("es_pct", "nq_pct", "kr_futures_pct", "krw_pct",
                        "vix_last", "btc_pct", "zb_pct")
            if isinstance(signals.get(k), (int, float))
        )
        confidence = min(0.85, 0.30 + valid_signals * 0.08)

        # findings 부재 ≠ 데이터 부족 — 전 신호 중립 구간이면 findings가 비므로
        # 수집 성공 여부로 문구를 구분한다 (2026-08-06, 브리핑 오표기 수정)
        if not findings:
            findings = (
                [f"야간 신호 중립 (수집 {valid_signals}/7, 특이사항 없음)"]
                if valid_signals >= 3
                else ["야간/주말 신호 데이터 부족"]
            )

        return self._build_opinion(
            score=int(score),
            bias=bias,
            confidence=confidence,
            findings=findings,
            sectors=[],
            raw=dict(signals=signals, is_weekend=is_weekend),
            valid_hours=2,   # 빠르게 만료 (시장 변동 빠름)
        )

    # ─────────────────────────────────────────
    # yfinance 통합 fetch
    # ─────────────────────────────────────────
    async def _fetch_all_signals(self) -> Dict[str, Any]:
        async def _pct(ticker: str, period: str = "5d", interval: str = "1h") -> Dict[str, Any]:
            def _sync():
                try:
                    import yfinance as yf
                    hist = yf.Ticker(ticker).history(
                        period=period, interval=interval, auto_adjust=False
                    )
                    if hist.empty or len(hist) < 2:
                        return {"last": None, "pct": None}
                    close = hist["Close"]
                    last = float(close.iloc[-1])
                    # 24시간 전 가격 (interval=1h → 24봉 전)
                    if interval == "1h" and len(close) >= 24:
                        prev = float(close.iloc[-24])
                    else:
                        prev = float(close.iloc[0])
                    pct = (last - prev) / prev * 100 if prev > 0 else 0.0
                    return {"last": round(last, 2), "pct": round(pct, 2)}
                except Exception:
                    return {"last": None, "pct": None}

            try:
                return await asyncio.to_thread(_sync)
            except Exception:
                return {"last": None, "pct": None}

        # 병렬 fetch (KS200=F는 야후 상장폐지로 제거 — 2026-08-06)
        es, nq, nkd, krw, vix, btc, zb = await asyncio.gather(
            _pct("ES=F"),
            _pct("NQ=F"),
            _pct("NKD=F"),
            _pct("KRW=X", period="5d", interval="1h"),
            _pct("^VIX", period="5d", interval="1d"),
            _pct("BTC-USD", period="3d", interval="1h"),
            _pct("ZB=F", period="3d", interval="1d"),
            return_exceptions=True,
        )

        def _safe(r, key):
            if isinstance(r, dict):
                return r.get(key)
            return None

        out: Dict[str, Any] = {}
        out["es_pct"] = _safe(es, "pct")
        out["es_last"] = _safe(es, "last")
        out["nq_pct"] = _safe(nq, "pct")
        out["nq_last"] = _safe(nq, "last")

        # KR 야간선물: KIS 직접 시세 우선 (주간 종가 대비 밤사이 변동률, 2026-08-06~)
        # 실패 시 NKD=F × 0.7 프록시 폴백 (프록시는 실제 대비 신호가 희석됨)
        try:
            from src.data.providers.kis_market_data import get_kis_market_data
            q = await get_kis_market_data().get_night_futures_quote()
            if q and q.get("change_pct") is not None:
                out["kr_futures_pct"] = q["change_pct"]
                out["kr_futures_source"] = f"KIS:{q.get('symbol')}"
        except Exception as e:
            logger.debug(f"[갭risk] KIS 야간선물 실패 → NKD 프록시: {e}")
        if "kr_futures_pct" not in out:
            nkd_pct = _safe(nkd, "pct")
            if isinstance(nkd_pct, (int, float)):
                out["kr_futures_pct"] = round(nkd_pct * 0.7, 2)
                out["kr_futures_source"] = "NKD=F(proxy×0.7)"

        out["krw_pct"] = _safe(krw, "pct")
        out["krw_last"] = _safe(krw, "last")
        out["vix_last"] = _safe(vix, "last")
        out["btc_pct"] = _safe(btc, "pct")
        out["zb_pct"] = _safe(zb, "pct")
        return out

    async def close(self) -> None:
        """orchestrator.close_all 호환 — yfinance만 사용하므로 no-op"""
        return
