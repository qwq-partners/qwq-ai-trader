"""
게이트 성능 분석 — 차단한 신호가 실제로 어떻게 됐는지 사후 추적한다.

주간 post-exit review는 "판 뒤 올랐나"를 본다.
이 모듈은 그 반대편, **사지 않은 것**을 본다.

engine.on_signal은 매수 신호를 게이트별로 차단하고 그 사실을 signal_events에 남긴다
(G1_regime, G2_cross, G3_risk, G4_llm, G5_budget/cash, G_intraday).
차단 자체는 기록되지만, 그 차단이 옳았는지는 지금까지 아무도 검증하지 않았다.

여기서 계산하는 것:
    차단 시점 종가 → N영업일 뒤 종가 수익률
    게이트별로 집계해 "회피 성공"과 "기회 손실"을 나눈다

판정 (post_exit_review와 동일한 ±3% 밴드 사용):
    +3% 이상  → 기회 손실 (막지 말았어야 함)
    -3% 이하  → 회피 성공 (막은 게 옳음)
    그 사이   → 중립

통과(passed) 신호도 같은 방식으로 계산해 대조군으로 쓴다.
게이트의 평균 사후 수익률이 통과 신호보다 높다면, 그 게이트는 수익을 버리고 있다는 뜻이다.

KR 종목(6자리 숫자)만 대상으로 한다 — US는 가격 소스가 달라 별도 처리가 필요하다.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

CACHE_DIR = Path.home() / ".cache" / "ai_trader"
RESULT_DIR = CACHE_DIR / "gate_performance"

# 판정 밴드 (post_exit_review와 동일)
OPPORTUNITY_THRESHOLD = 3.0   # +3% 이상 → 기회 손실
AVOIDANCE_THRESHOLD = -3.0    # -3% 이하 → 회피 성공

# 분석 기본값
DEFAULT_HORIZON_DAYS = 20     # 사후 추적 기간 (영업일)
DEFAULT_LOOKBACK_DAYS = 90    # 조회 범위 (달력일)
MIN_SAMPLES_PER_GATE = 5      # 게이트별 최소 표본 (미만이면 통계 무의미)


def _is_kr_symbol(symbol: str) -> bool:
    """KR 종목 코드(6자리 숫자) 여부"""
    s = (symbol or "").strip()
    return len(s) == 6 and s.isdigit()


class GatePerformanceAnalyzer:
    """게이트별 차단 신호의 사후 성과 분석"""

    def __init__(self, horizon_days: int = DEFAULT_HORIZON_DAYS):
        self.horizon_days = horizon_days
        RESULT_DIR.mkdir(parents=True, exist_ok=True)

    # ── 데이터 조회 ────────────────────────────────────────
    async def _fetch_signals(self, lookback_days: int) -> List[Dict[str, Any]]:
        """signal_events에서 분석 대상 신호를 가져온다"""
        dsn = os.getenv("DATABASE_URL")
        if not dsn:
            logger.warning("[게이트분석] DATABASE_URL 없음")
            return []

        try:
            import asyncpg
        except ImportError:
            logger.warning("[게이트분석] asyncpg 미설치")
            return []

        # horizon 만큼 지난 신호만 대상 (아직 결과가 안 나온 건 제외)
        # 영업일 → 달력일 여유분 1.5배
        cutoff_recent = datetime.now() - timedelta(days=int(self.horizon_days * 1.5))
        cutoff_old = datetime.now() - timedelta(days=lookback_days)

        conn = None
        try:
            conn = await asyncpg.connect(dsn, timeout=15)
            rows = await conn.fetch(
                """
                SELECT symbol, name, strategy, score, adjusted_score,
                       event_type, block_gate, block_reason,
                       market_regime, event_time
                FROM signal_events
                WHERE side = 'buy'
                  AND event_type IN ('blocked', 'passed')
                  AND event_time BETWEEN $1 AND $2
                ORDER BY event_time
                """,
                cutoff_old, cutoff_recent,
            )
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"[게이트분석] 신호 조회 실패: {e}")
            return []
        finally:
            if conn is not None:
                await conn.close()

    def _load_prices(self, symbols: List[str], start: str, end: str) -> Dict[str, Any]:
        """종목별 OHLCV를 한 번씩만 조회 (동기 — to_thread로 감싸 호출)"""
        from pykrx import stock as pykrx_stock

        prices: Dict[str, Any] = {}
        for i, sym in enumerate(symbols):
            try:
                df = pykrx_stock.get_market_ohlcv(start, end, sym)
                if df is not None and not df.empty:
                    prices[sym] = df
            except Exception as e:
                logger.debug(f"[게이트분석] {sym} 가격 조회 실패: {e}")
            # KRX 과부하 방지
            if (i + 1) % 20 == 0:
                import time
                time.sleep(0.3)
        return prices

    # ── 수익률 계산 ────────────────────────────────────────
    def _forward_return(self, df, event_time: datetime) -> Optional[float]:
        """
        신호 시점 종가 대비 horizon 영업일 뒤 종가 수익률(%).

        신호 당일 종가를 기준으로 삼는다 — 실제로 샀다면 T+1 시가겠지만,
        게이트 간 상대 비교가 목적이므로 일관된 기준이면 충분하다.
        """
        try:
            import pandas as pd

            ts = pd.Timestamp(event_time.date())
            idx = df.index
            # 신호일 이후 첫 거래일
            pos_candidates = idx[idx >= ts]
            if len(pos_candidates) == 0:
                return None
            base_date = pos_candidates[0]
            base_pos = idx.get_loc(base_date)
            target_pos = base_pos + self.horizon_days
            if target_pos >= len(idx):
                return None

            base_price = float(df.iloc[base_pos]["종가"])
            target_price = float(df.iloc[target_pos]["종가"])
            if base_price <= 0:
                return None
            return (target_price - base_price) / base_price * 100.0
        except Exception as e:
            logger.debug(f"[게이트분석] 수익률 계산 실패: {e}")
            return None

    # ── 메인 ───────────────────────────────────────────────
    async def analyze(self, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> Dict[str, Any]:
        """
        게이트별 사후 성과를 분석한다.

        Returns:
            {"generated_at", "horizon_days", "gates": {gate: {...}}, "verdicts": [...]}
        """
        signals = await self._fetch_signals(lookback_days)
        kr_signals = [s for s in signals if _is_kr_symbol(s.get("symbol", ""))]

        if not kr_signals:
            logger.info("[게이트분석] 분석 대상 신호 없음")
            return {"error": "분석 대상 신호 없음", "total_signals": len(signals)}

        logger.info(
            f"[게이트분석] 대상 {len(kr_signals)}건 "
            f"(전체 {len(signals)}건 중 KR), horizon={self.horizon_days}영업일"
        )

        # 가격 조회 (종목당 1회)
        symbols = sorted({s["symbol"] for s in kr_signals})
        oldest = min(s["event_time"] for s in kr_signals)
        start = (oldest - timedelta(days=10)).strftime("%Y%m%d")
        end = datetime.now().strftime("%Y%m%d")

        logger.info(f"[게이트분석] 가격 조회: {len(symbols)}종목 ({start}~{end})")
        prices = await asyncio.to_thread(self._load_prices, symbols, start, end)

        # 게이트별 집계
        buckets: Dict[str, List[Dict]] = defaultdict(list)
        for s in kr_signals:
            df = prices.get(s["symbol"])
            if df is None:
                continue
            ret = self._forward_return(df, s["event_time"])
            if ret is None:
                continue

            if s["event_type"] == "passed":
                gate = "PASSED(대조군)"
            else:
                gate = s.get("block_gate") or "UNKNOWN"

            buckets[gate].append({
                "symbol": s["symbol"],
                "name": s.get("name") or "",
                "strategy": s.get("strategy") or "",
                "score": s.get("score"),
                "regime": s.get("market_regime") or "",
                "reason": (s.get("block_reason") or "")[:80],
                "event_time": s["event_time"].isoformat(),
                "forward_return": round(ret, 2),
            })

        gates: Dict[str, Any] = {}
        for gate, items in buckets.items():
            rets = [i["forward_return"] for i in items]
            n = len(rets)
            opportunity = [r for r in rets if r >= OPPORTUNITY_THRESHOLD]
            avoided = [r for r in rets if r <= AVOIDANCE_THRESHOLD]

            gates[gate] = {
                "samples": n,
                "avg_return": round(sum(rets) / n, 2) if n else 0.0,
                "median_return": round(sorted(rets)[n // 2], 2) if n else 0.0,
                "opportunity_loss_cnt": len(opportunity),
                "opportunity_loss_pct": round(len(opportunity) / n * 100, 1) if n else 0.0,
                "avoided_cnt": len(avoided),
                "avoided_pct": round(len(avoided) / n * 100, 1) if n else 0.0,
                "best": max(items, key=lambda x: x["forward_return"]) if items else None,
                "worst": min(items, key=lambda x: x["forward_return"]) if items else None,
            }

        result = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "horizon_days": self.horizon_days,
            "lookback_days": lookback_days,
            "total_analyzed": sum(g["samples"] for g in gates.values()),
            "gates": gates,
            "verdicts": self._build_verdicts(gates),
        }

        self._save(result)
        return result

    def _build_verdicts(self, gates: Dict[str, Any]) -> List[str]:
        """게이트별 판정 문구 — 대조군(통과 신호) 대비 상대 평가"""
        verdicts: List[str] = []
        control = gates.get("PASSED(대조군)")
        control_avg = control["avg_return"] if control and control["samples"] >= MIN_SAMPLES_PER_GATE else None

        for gate, g in sorted(gates.items(), key=lambda kv: -kv[1]["samples"]):
            if gate.startswith("PASSED"):
                continue
            n = g["samples"]
            if n < MIN_SAMPLES_PER_GATE:
                verdicts.append(f"{gate}: 표본 부족 ({n}건) — 판단 보류")
                continue

            avg = g["avg_return"]
            opp = g["opportunity_loss_pct"]

            if avg > 0 and (control_avg is None or avg > control_avg):
                verdicts.append(
                    f"⚠️ {gate}: 차단한 신호가 평균 {avg:+.2f}% 상승 "
                    f"(기회손실 {opp:.0f}%, {n}건) — 게이트가 수익을 버리고 있음. 완화 검토"
                )
            elif avg <= AVOIDANCE_THRESHOLD:
                verdicts.append(
                    f"✅ {gate}: 차단한 신호가 평균 {avg:+.2f}% 하락 "
                    f"(회피성공 {g['avoided_pct']:.0f}%, {n}건) — 게이트가 제 역할 중"
                )
            else:
                verdicts.append(
                    f"➖ {gate}: 평균 {avg:+.2f}% ({n}건) — 유의미한 효과 불명확"
                )

        if control_avg is not None:
            verdicts.insert(0, f"[대조군] 통과 신호 평균 {control_avg:+.2f}% ({control['samples']}건)")
        return verdicts

    def _save(self, result: Dict[str, Any]) -> None:
        try:
            path = RESULT_DIR / f"gate_perf_{datetime.now():%Y%m%d}.json"
            path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            logger.info(f"[게이트분석] 저장: {path}")
        except Exception as e:
            logger.warning(f"[게이트분석] 저장 실패: {e}")

    @staticmethod
    def format_report(result: Dict[str, Any]) -> str:
        """텔레그램/로그용 요약 리포트"""
        if result.get("error"):
            return f"[게이트 성능 분석] {result['error']}"

        lines = [
            "📊 게이트 성능 분석",
            f"기간: 최근 {result['lookback_days']}일 / 추적 {result['horizon_days']}영업일",
            f"분석 신호: {result['total_analyzed']}건",
            "",
        ]
        for v in result.get("verdicts", []):
            lines.append(v)

        lines.append("")
        lines.append("— 게이트별 상세 —")
        for gate, g in sorted(result["gates"].items(), key=lambda kv: -kv[1]["samples"]):
            lines.append(
                f"{gate}: {g['samples']}건 | 평균 {g['avg_return']:+.2f}% | "
                f"기회손실 {g['opportunity_loss_pct']:.0f}% | 회피성공 {g['avoided_pct']:.0f}%"
            )
        return "\n".join(lines)


_analyzer: Optional[GatePerformanceAnalyzer] = None


def get_gate_analyzer(horizon_days: int = DEFAULT_HORIZON_DAYS) -> GatePerformanceAnalyzer:
    global _analyzer
    if _analyzer is None:
        _analyzer = GatePerformanceAnalyzer(horizon_days=horizon_days)
    return _analyzer
