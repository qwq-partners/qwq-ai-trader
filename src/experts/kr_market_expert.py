"""kr-market-expert — 한국증시 미시 (수급·체결·신용·옵션)

데이터 소스 (신규 키 0개):
- pykrx: 외국인/기관 매매, 공매도, 신용잔고
- yfinance: KOSPI 지수
- Perplexity: 시황·옵션 시그널
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List

from loguru import logger

from .base import ExpertAgent
from .types import ExpertOpinion, RegimeBias


class KRMarketExpert(ExpertAgent):
    name = "kr_market_expert"
    refresh_minutes = 60
    cost_per_call_usd = 0.008

    async def _analyze(self) -> ExpertOpinion:
        # 1) 수급 + 지수 + 공매도 + 야간선물 병렬 fetch
        flows, kospi_state, short_balance, futures_state = await asyncio.gather(
            self._fetch_investor_flows(),
            self._fetch_kospi_state(),
            self._fetch_short_balance(),
            self._fetch_kospi200_futures(),
            return_exceptions=True,
        )

        flows = flows if isinstance(flows, dict) else {}
        kospi_state = kospi_state if isinstance(kospi_state, dict) else {}
        short_balance = short_balance if isinstance(short_balance, dict) else {}
        futures_state = futures_state if isinstance(futures_state, dict) else {}

        # 2) 점수 계산
        score = 0
        findings: List[str] = []

        # 외국인 순매수 (5일 누적)
        foreign_5d = flows.get("foreign_5d_billion")
        if isinstance(foreign_5d, (int, float)):
            if foreign_5d > 2000:        # +2조원 이상
                score += 20
                findings.append(f"외국인 5일 +{foreign_5d:,.0f}억 (강한 매수)")
            elif foreign_5d > 500:
                score += 10
                findings.append(f"외국인 5일 +{foreign_5d:,.0f}억")
            elif foreign_5d < -2000:
                score -= 20
                findings.append(f"외국인 5일 {foreign_5d:,.0f}억 (대규모 매도)")
            elif foreign_5d < -500:
                score -= 10
                findings.append(f"외국인 5일 {foreign_5d:,.0f}억")

        # 기관 순매수
        institutional = flows.get("institutional_5d_billion")
        if isinstance(institutional, (int, float)):
            if institutional > 1000:
                score += 10
            elif institutional < -1000:
                score -= 10

        # KOSPI 모멘텀
        ma20_dist = kospi_state.get("vs_ma20_pct")
        if isinstance(ma20_dist, (int, float)):
            if ma20_dist > 2.0:
                score += 8
                findings.append(f"KOSPI MA20 +{ma20_dist:.1f}%")
            elif ma20_dist < -3.0:
                score -= 10
                findings.append(f"KOSPI MA20 {ma20_dist:.1f}% (이격 과다)")

        # 공매도 비율
        short_ratio = short_balance.get("avg_short_ratio")
        if isinstance(short_ratio, (int, float)):
            if short_ratio > 2.0:
                score -= 8
                findings.append(f"공매도 잔고 {short_ratio:.2f}% (경계)")
            elif short_ratio < 1.0:
                score += 3

        # KOSPI200 야간선물 (월요일 갭 risk + 야간 spillover 캡처)
        # 2026-06-07 추가: KS200=F 또는 KOSPI futures 대용 지표
        nf_chg = futures_state.get("overnight_chg_pct")
        if isinstance(nf_chg, (int, float)):
            if nf_chg <= -2.0:
                score -= 18
                findings.append(f"⚠️ KOSPI200 야간선물 {nf_chg:+.2f}% (갭다운 위험)")
            elif nf_chg <= -1.0:
                score -= 10
                findings.append(f"KOSPI200 야간선물 {nf_chg:+.2f}% (약세)")
            elif nf_chg >= 2.0:
                score += 15
                findings.append(f"🟢 KOSPI200 야간선물 {nf_chg:+.2f}% (갭업 기대)")
            elif nf_chg >= 1.0:
                score += 8
                findings.append(f"KOSPI200 야간선물 {nf_chg:+.2f}% (강세)")

        # 3) Perplexity 시황 (옵션·테마)
        market_ctx = await self._perplexity_search(
            "오늘 한국 주식시장 (KOSPI/KOSDAQ) 핵심 수급·테마 동향 3가지를 "
            "각각 한 줄로 한국어로 요약. 옵션 만기·풋콜 비율·외국인 자금 흐름 포함.",
            max_tokens=300,
        )
        if market_ctx:
            findings.append(f"시황: {market_ctx[:120]}")

        bias = (
            RegimeBias.BULL if score > 15
            else RegimeBias.BEAR if score < -15
            else RegimeBias.NEUTRAL
        )
        confidence = min(0.85, 0.4 + len(findings) * 0.08)

        sectors = await self._detect_rotation_sectors()

        return self._build_opinion(
            score=score,
            bias=bias,
            confidence=confidence,
            findings=findings,
            sectors=sectors,
            raw=dict(
                flows=flows,
                kospi=kospi_state,
                short_balance=short_balance,
                futures=futures_state,
                has_market_ctx=bool(market_ctx),
            ),
            valid_hours=4,
        )

    # ─────────────────────────────────────────
    # pykrx — 외국인/기관 5일 누적 (단위: 억원)
    # ─────────────────────────────────────────
    async def _fetch_investor_flows(self) -> Dict[str, float]:
        def _sync_fetch():
            try:
                from pykrx import stock as pyk
                # 영업일 5일 데이터 (간단히 calendar 5일)
                end = datetime.now().strftime("%Y%m%d")
                start = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
                df = pyk.get_market_trading_value_by_investor(start, end, "KOSPI")
                if df is None or df.empty:
                    return {}
                # KRX 컬럼명: '외국인합계', '기관합계' 등 (열거형 다양)
                foreign_col = next((c for c in df.columns if "외국인" in c), None)
                inst_col = next((c for c in df.columns if "기관" in c), None)
                result = {}
                if foreign_col is not None:
                    val = float(df[foreign_col].sum()) / 1e8   # 원 → 억원
                    result["foreign_5d_billion"] = round(val, 1)
                if inst_col is not None:
                    val = float(df[inst_col].sum()) / 1e8
                    result["institutional_5d_billion"] = round(val, 1)
                return result
            except Exception as e:
                logger.debug(f"[KR시장] flows 실패: {e}")
                return {}

        return await asyncio.to_thread(_sync_fetch)

    # ─────────────────────────────────────────
    # KOSPI 지수 상태
    # ─────────────────────────────────────────
    async def _fetch_kospi_state(self) -> Dict[str, float]:
        def _sync_fetch():
            try:
                import yfinance as yf
                hist = yf.Ticker("^KS11").history(period="40d", interval="1d", auto_adjust=False)
                if hist.empty:
                    return {}
                close = hist["Close"]
                last = float(close.iloc[-1])
                ma20 = float(close.tail(20).mean())
                vs_ma20 = (last - ma20) / ma20 * 100 if ma20 > 0 else 0.0
                # 5일 변동률
                if len(close) >= 6:
                    chg_5d = (last - float(close.iloc[-6])) / float(close.iloc[-6]) * 100
                else:
                    chg_5d = 0.0
                return {
                    "last": round(last, 2),
                    "ma20": round(ma20, 2),
                    "vs_ma20_pct": round(vs_ma20, 2),
                    "chg_5d_pct": round(chg_5d, 2),
                }
            except Exception as e:
                logger.debug(f"[KR시장] KOSPI 실패: {e}")
                return {}

        return await asyncio.to_thread(_sync_fetch)

    # ─────────────────────────────────────────
    # 공매도 잔고
    # ─────────────────────────────────────────
    async def _fetch_short_balance(self) -> Dict[str, float]:
        # P1-8 (2026-05-29 리뷰): 휴일 회피 — 가장 최근 영업일 사용
        from src.utils.session import is_kr_market_holiday
        target = datetime.now().date() - timedelta(days=1)
        # 최대 10일 거슬러 올라가며 영업일 찾기
        for _ in range(10):
            if not is_kr_market_holiday(target) and target.weekday() < 5:
                break
            target = target - timedelta(days=1)
        target_str = target.strftime("%Y%m%d")

        def _sync_fetch():
            try:
                from pykrx import stock as pyk
                df = pyk.get_shorting_balance_by_ticker(target_str, market="KOSPI")
                if df is None or df.empty:
                    return {}
                ratio_col = next((c for c in df.columns if "비중" in c or "비율" in c), None)
                if ratio_col is None:
                    return {}
                avg = float(df[ratio_col].mean())
                return {"avg_short_ratio": round(avg, 3)}
            except Exception as e:
                logger.debug(f"[KR시장] 공매도 실패: {e}")
                return {}

        return await asyncio.to_thread(_sync_fetch)

    # ─────────────────────────────────────────
    # KOSPI200 야간선물 (월요일 갭 risk 캡처용, 2026-06-07 추가)
    # ─────────────────────────────────────────
    async def _fetch_kospi200_futures(self) -> Dict[str, Any]:
        """KOSPI200 야간선물 변동률.

        1) KIS 야간선물 직접 시세 (A01609/CM, 주간 종가 대비 밤사이 변동률 — 2026-08-06~)
           ※ yfinance KS200=F는 상장폐지(404), ^KS200은 시간봉 부족으로 항상 실패라
             기존 체인은 사실상 NKD 프록시만 동작했음
        2) yfinance 폴백: KS200=F/^KS200 → NKD=F (니케이 spillover proxy ×0.7)
        """
        # 1) KIS 직접 시세 — kr_scheduler 아침 스크리닝과 동일 경로 재사용
        try:
            from src.data.providers.kis_market_data import get_kis_market_data
            q = await get_kis_market_data().get_night_futures_quote()
            if q and q.get("change_pct") is not None:
                return {
                    "overnight_chg_pct": q["change_pct"],
                    "source": f"KIS:{q.get('symbol')}",
                    "last": q.get("price"),
                }
        except Exception as e:
            logger.debug(f"[KR시장] KIS 야간선물 실패 → yfinance 폴백: {e}")

        def _sync_fetch():
            import yfinance as yf
            result: Dict[str, Any] = {}

            # 변동률은 모두 24시간(=24h 봉) 기준으로 통일.
            # period=3d/interval=1h → ~72봉. iloc[-24]가 24시간 전.
            def _24h_chg(ticker: str):
                hist = yf.Ticker(ticker).history(period="3d", interval="1h", auto_adjust=False)
                if hist.empty or len(hist) < 24:
                    return None
                last = float(hist["Close"].iloc[-1])
                prev = float(hist["Close"].iloc[-24])
                if prev <= 0:
                    return None
                return last, round((last - prev) / prev * 100, 2)

            # 1) KS200=F (KOSPI 200 야간선물, 가용성 불확실)
            for ticker in ("KS200=F", "^KS200"):
                try:
                    pair = _24h_chg(ticker)
                    if pair is not None:
                        last, chg = pair
                        result["overnight_chg_pct"] = chg
                        result["source"] = ticker
                        result["last"] = round(last, 2)
                        return result
                except Exception:
                    continue

            # 2) NKD=F 폴백 (니케이 야간선물 → KOSPI 상관성 활용, 0.7 계수)
            try:
                pair = _24h_chg("NKD=F")
                if pair is not None:
                    last, chg = pair
                    result["overnight_chg_pct"] = round(chg * 0.7, 2)
                    result["source"] = "NKD=F(proxy×0.7)"
                    result["last"] = round(last, 2)
                    return result
            except Exception:
                pass

            return result

        try:
            return await asyncio.to_thread(_sync_fetch)
        except Exception as e:
            logger.debug(f"[KR시장] 야간선물 실패: {e}")
            return {}

    # ─────────────────────────────────────────
    # 섹터 로테이션 (Perplexity 자연어)
    # ─────────────────────────────────────────
    async def _detect_rotation_sectors(self) -> List[str]:
        text = await self._perplexity_search(
            "오늘 한국 증시(KOSPI/KOSDAQ)에서 가장 강한 섹터/테마 5개를 쉼표로 나열하세요. "
            "(다른 설명 없이 명사만)",
            max_tokens=100,
        )
        if not text:
            return []
        items = [s.strip() for s in text.replace("\n", ",").split(",") if s.strip()]
        return items[:5]
