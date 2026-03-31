"""
QWQ AI Trader - 시그널 이벤트 저장소

매수 신호 발생 / 차단 / 통과 이력을 PostgreSQL에 기록합니다.
- passed  : 모든 게이트 통과 → 실제 주문 실행
- blocked : 특정 게이트에서 차단됨 (원칙 적용)
- penalized: 점수 감점 후 통과

차단 게이트 코드:
  G1_regime   - 마켓 레짐 필터 (약세장 진입 차단)
  G2_cross    - 크로스 검증 9개 규칙
  G3_risk     - 리스크 매니저 (일일손실/동기화/재진입/쿨다운)
  G4_llm      - LLM 이중 검증 (85점+, 비강세장)
  G5_cash     - 가용 현금 부족
  G5_budget   - 전략 예산 소진
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import asyncpg
from loguru import logger

_DB_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/ai_db")

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS signal_events (
    id          BIGSERIAL PRIMARY KEY,
    event_time  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol      VARCHAR(20)  NOT NULL,
    name        VARCHAR(100) DEFAULT '',
    strategy    VARCHAR(50)  DEFAULT '',
    score       FLOAT,
    adjusted_score FLOAT,
    side        VARCHAR(10)  DEFAULT 'buy',
    event_type  VARCHAR(20)  NOT NULL,   -- 'passed' | 'blocked' | 'penalized'
    block_gate  VARCHAR(30),             -- G1_regime / G2_cross / G3_risk / G4_llm / G5_cash / G5_budget
    block_reason TEXT,
    market_regime VARCHAR(30),
    sector      VARCHAR(50),
    metadata    JSONB        DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_se_time    ON signal_events(event_time DESC);
CREATE INDEX IF NOT EXISTS idx_se_symbol  ON signal_events(symbol);
CREATE INDEX IF NOT EXISTS idx_se_type    ON signal_events(event_type);
CREATE INDEX IF NOT EXISTS idx_se_gate    ON signal_events(block_gate);
"""


class SignalEventStorage:
    """시그널 이벤트 DB 저장소 (싱글톤)"""

    _instance: Optional["SignalEventStorage"] = None

    def __init__(self):
        self._pool: Optional[asyncpg.Pool] = None
        self._lock = asyncio.Lock()
        self._init_done = False
        # 실시간 SSE 콜백 (대시보드 push)
        self._sse_callback = None

    @classmethod
    def get(cls) -> "SignalEventStorage":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_sse_callback(self, cb):
        """SSE broadcast 콜백 등록"""
        self._sse_callback = cb

    # ────────────────────────────────────────────
    # 초기화
    # ────────────────────────────────────────────

    async def _ensure_init(self):
        if self._init_done:
            return
        async with self._lock:
            if self._init_done:
                return
            try:
                self._pool = await asyncpg.create_pool(_DB_URL, min_size=1, max_size=3,
                                                       command_timeout=10)
                async with self._pool.acquire() as conn:
                    await conn.execute(_CREATE_TABLE)
                self._init_done = True
                logger.info("[SignalEventStorage] DB 초기화 완료")
            except Exception as e:
                logger.error(f"[SignalEventStorage] DB 초기화 실패: {e}")

    # ────────────────────────────────────────────
    # 기록 메서드
    # ────────────────────────────────────────────

    async def log(
        self,
        *,
        symbol: str,
        name: str = "",
        strategy: str = "",
        score: float = 0.0,
        adjusted_score: Optional[float] = None,
        side: str = "buy",
        event_type: str,           # 'passed' | 'blocked' | 'penalized'
        block_gate: Optional[str] = None,
        block_reason: Optional[str] = None,
        market_regime: str = "",
        sector: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """비동기 이벤트 기록 — fire-and-forget"""
        asyncio.create_task(self._write(
            symbol=symbol, name=name, strategy=strategy,
            score=score, adjusted_score=adjusted_score or score,
            side=side, event_type=event_type,
            block_gate=block_gate, block_reason=block_reason,
            market_regime=market_regime, sector=sector,
            metadata=metadata or {},
        ))

    async def _write(self, **kwargs) -> None:
        try:
            await self._ensure_init()
            if not self._pool:
                return
            meta_json = json.dumps(kwargs.pop("metadata", {}), ensure_ascii=False,
                                   default=str)
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO signal_events
                        (symbol, name, strategy, score, adjusted_score,
                         side, event_type, block_gate, block_reason,
                         market_regime, sector, metadata)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                    RETURNING id, event_time
                    """,
                    kwargs["symbol"], kwargs["name"], kwargs["strategy"],
                    kwargs["score"], kwargs["adjusted_score"],
                    kwargs["side"], kwargs["event_type"],
                    kwargs.get("block_gate"), kwargs.get("block_reason"),
                    kwargs["market_regime"], kwargs["sector"],
                    meta_json,
                )
            # SSE push
            if self._sse_callback and row:
                await self._push_sse(row["id"], row["event_time"], **kwargs)
        except Exception as e:
            logger.debug(f"[SignalEventStorage] 기록 실패 (무시): {e}")

    async def _push_sse(self, row_id: int, event_time: datetime, **kwargs):
        try:
            payload = {
                "id": row_id,
                "event_time": event_time.isoformat(),
                "symbol": kwargs["symbol"],
                "name": kwargs["name"],
                "strategy": kwargs["strategy"],
                "score": kwargs["score"],
                "adjusted_score": kwargs["adjusted_score"],
                "event_type": kwargs["event_type"],
                "block_gate": kwargs.get("block_gate"),
                "block_reason": kwargs.get("block_reason"),
                "market_regime": kwargs["market_regime"],
                "sector": kwargs["sector"],
            }
            await self._sse_callback("signal_event", payload)
        except Exception:
            pass

    # ────────────────────────────────────────────
    # 조회 메서드
    # ────────────────────────────────────────────

    async def get_recent(self, limit: int = 50,
                         event_type: Optional[str] = None) -> List[Dict]:
        await self._ensure_init()
        if not self._pool:
            return []
        try:
            where = "WHERE event_type = $2" if event_type else ""
            params = [limit, event_type] if event_type else [limit]
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT id, event_time, symbol, name, strategy,
                           score, adjusted_score, side, event_type,
                           block_gate, block_reason, market_regime, sector
                    FROM signal_events
                    {where}
                    ORDER BY event_time DESC
                    LIMIT $1
                    """,
                    *params,
                )
            return [dict(r) for r in rows]
        except Exception as e:
            logger.debug(f"[SignalEventStorage] 조회 실패: {e}")
            return []

    async def get_stats(self, days: int = 30) -> Dict[str, Any]:
        """통계 집계 (최근 N일)"""
        await self._ensure_init()
        if not self._pool:
            return {}
        try:
            async with self._pool.acquire() as conn:
                # 전체 집계
                summary = await conn.fetchrow(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE side='buy')                        AS total_buy,
                        COUNT(*) FILTER (WHERE side='buy' AND event_type='passed')  AS passed,
                        COUNT(*) FILTER (WHERE side='buy' AND event_type='blocked') AS blocked,
                        COUNT(*) FILTER (WHERE side='buy' AND event_type='penalized') AS penalized
                    FROM signal_events
                    WHERE event_time >= NOW() - ($1 || ' days')::interval
                    """,
                    str(days),
                )
                # 게이트별 차단 통계
                gate_rows = await conn.fetch(
                    """
                    SELECT block_gate, COUNT(*) AS cnt
                    FROM signal_events
                    WHERE side='buy' AND event_type='blocked'
                      AND event_time >= NOW() - ($1 || ' days')::interval
                      AND block_gate IS NOT NULL
                    GROUP BY block_gate
                    ORDER BY cnt DESC
                    """,
                    str(days),
                )
                # 전략별 차단 통계
                strat_rows = await conn.fetch(
                    """
                    SELECT strategy,
                           COUNT(*) FILTER (WHERE event_type='passed')  AS passed,
                           COUNT(*) FILTER (WHERE event_type='blocked') AS blocked
                    FROM signal_events
                    WHERE side='buy'
                      AND event_time >= NOW() - ($1 || ' days')::interval
                    GROUP BY strategy
                    ORDER BY (COUNT(*) FILTER (WHERE event_type='blocked')) DESC
                    """,
                    str(days),
                )
                # 일별 추이 (최근 14일)
                daily_rows = await conn.fetch(
                    """
                    SELECT DATE(event_time AT TIME ZONE 'Asia/Seoul') AS day,
                           COUNT(*) FILTER (WHERE event_type='passed')  AS passed,
                           COUNT(*) FILTER (WHERE event_type='blocked') AS blocked
                    FROM signal_events
                    WHERE side='buy' AND event_time >= NOW() - '14 days'::interval
                    GROUP BY day
                    ORDER BY day
                    """
                )

            block_rate = 0.0
            if summary["total_buy"] and summary["total_buy"] > 0:
                block_rate = round(summary["blocked"] / summary["total_buy"] * 100, 1)

            return {
                "period_days": days,
                "total_buy": summary["total_buy"],
                "passed": summary["passed"],
                "blocked": summary["blocked"],
                "penalized": summary["penalized"],
                "block_rate_pct": block_rate,
                "by_gate": [{"gate": r["block_gate"], "count": r["cnt"]}
                            for r in gate_rows],
                "by_strategy": [{"strategy": r["strategy"],
                                 "passed": r["passed"],
                                 "blocked": r["blocked"]}
                                for r in strat_rows],
                "daily": [{"day": str(r["day"]),
                           "passed": r["passed"],
                           "blocked": r["blocked"]}
                          for r in daily_rows],
            }
        except Exception as e:
            logger.debug(f"[SignalEventStorage] 통계 조회 실패: {e}")
            return {}
