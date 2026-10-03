"""
AI Trading Bot v2 - PostgreSQL 거래 저장소

TradeJournal 인터페이스 100% 호환 + DB 영속화 + trade_events 이벤트 로그.
DB 연결 실패 시 JSON 전용 모드로 자동 폴백.
"""

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

import asyncpg
from loguru import logger

from src.core.evolution.trade_journal import TradeJournal, TradeRecord
from src.utils.fee_calculator import FeeConfig
from src.data.storage.execution_journal import (ExecutionIdentity, ExecutionWriteReceipt,
    ExecutionBatch, execution_key, validate_batch, EXECUTION_SCHEMA_SQL)


# ── SQL 스키마 ──────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id              VARCHAR(80) PRIMARY KEY,
    symbol          VARCHAR(10)  NOT NULL,
    name            VARCHAR(100) NOT NULL DEFAULT '',
    market          VARCHAR(5)   NOT NULL DEFAULT 'KR',
    entry_time      TIMESTAMP    NOT NULL,
    entry_price     NUMERIC(12,2) NOT NULL,
    entry_quantity  INTEGER      NOT NULL,
    entry_reason    TEXT         DEFAULT '',
    entry_strategy  VARCHAR(50)  DEFAULT '',
    entry_signal_score NUMERIC(6,2) DEFAULT 0,
    exit_time       TIMESTAMP    NULL,
    exit_price      NUMERIC(12,2) DEFAULT 0,
    exit_quantity   INTEGER      DEFAULT 0,
    exit_reason     TEXT         DEFAULT '',
    exit_type       VARCHAR(30)  DEFAULT '',
    pnl             NUMERIC(14,2) DEFAULT 0,
    pnl_pct         NUMERIC(8,4)  DEFAULT 0,
    holding_minutes INTEGER       DEFAULT 0,
    market_context       JSONB DEFAULT '{}',
    indicators_at_entry  JSONB DEFAULT '{}',
    indicators_at_exit   JSONB DEFAULT '{}',
    theme_info           JSONB DEFAULT '{}',
    review_notes            TEXT DEFAULT '',
    lesson_learned          TEXT DEFAULT '',
    improvement_suggestion  TEXT DEFAULT '',
    kis_order_no VARCHAR(20) NULL,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time DESC);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(entry_strategy);
CREATE INDEX IF NOT EXISTS idx_trades_entry_date ON trades((entry_time::date));
CREATE INDEX IF NOT EXISTS idx_trades_open ON trades(exit_time) WHERE exit_time IS NULL;

CREATE TABLE IF NOT EXISTS trade_events (
    id              BIGSERIAL PRIMARY KEY,
    trade_id        VARCHAR(80) NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
    symbol          VARCHAR(10) NOT NULL,
    name            VARCHAR(100) DEFAULT '',
    event_type      VARCHAR(10) NOT NULL,
    event_time      TIMESTAMP   NOT NULL,
    price           NUMERIC(12,2) NOT NULL,
    quantity        INTEGER     NOT NULL,
    exit_type       VARCHAR(30) NULL,
    exit_reason     TEXT        NULL,
    pnl             NUMERIC(14,2) NULL,
    pnl_pct         NUMERIC(8,4)  NULL,
    strategy        VARCHAR(50) DEFAULT '',
    signal_score    NUMERIC(6,2) DEFAULT 0,
    kis_order_no    VARCHAR(20) NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'holding',
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_te_event_time ON trade_events(event_time DESC);
CREATE INDEX IF NOT EXISTS idx_te_trade_id ON trade_events(trade_id);
CREATE INDEX IF NOT EXISTS idx_te_type ON trade_events(event_type);
CREATE INDEX IF NOT EXISTS idx_te_date ON trade_events((event_time::date));

-- 거래일 기록 대사 결과(설계 A §5-1, 2026-09-29). complete = '20:30 대사가 불일치를 찾지 못했다'
-- (완전성 증명 아님). trade_date 는 event_time 과 같은 호스트 로컬 날짜.
CREATE TABLE IF NOT EXISTS execution_day_status (
    trade_date  DATE PRIMARY KEY,
    status      VARCHAR(12) NOT NULL,
    reasons     TEXT,
    source      VARCHAR(30),
    checked_at  TIMESTAMP NOT NULL,
    updated_at  TIMESTAMP NOT NULL
);
"""


@dataclass(frozen=True)
class KISSyncResult:
    """Journal comparison result, not a portfolio or persistence receipt."""

    status: str
    reason: str
    recovered_count: int = 0

    @property
    def complete(self) -> bool:
        return self.status in ("verified_empty", "reconciled")


async def sync_kis_journal(owner, *, close_day: bool = False) -> KISSyncResult:
    """Expose journal evidence separately from portfolio readiness.

    A startup empty query must not suppress the end-of-day comparison.
    Unsupported legacy adapters and failed comparisons remain retryable.
    """
    today = date.today()
    method = getattr(getattr(owner, 'trade_journal', None), 'sync_from_kis', None)
    try:
        if not callable(method) or getattr(owner, 'broker', None) is None:
            result = KISSyncResult("unsupported", "journal_sync_unavailable")
        else:
            result = await method(owner.broker, engine=getattr(owner, 'engine', None))
            if not isinstance(result, KISSyncResult):
                result = KISSyncResult("unsupported", "journal_sync_receipt_unavailable")
        if date.today() != today:
            result = KISSyncResult("incomplete", "comparison_date_changed")
    except Exception as exc:
        result = KISSyncResult("error", f"journal_sync_failed:{type(exc).__name__}")
    owner._kis_journal_sync_result = result
    if result.complete:
        if close_day:
            owner._last_kis_sync_date = today
        logger.info(f"[KIS동기화] 확인 결과: {result.status} ({result.reason}) — Portfolio 재적용 없음")
    else:
        logger.warning(f"[KIS동기화] 미완료·재확인 필요: {result.status} ({result.reason})")
    return result


class TradeStorage:
    """
    PostgreSQL 기반 거래 저장소.

    - TradeJournal과 동일한 동기 인터페이스 제공 (인메모리 캐시)
    - DB 쓰기는 asyncio.Queue → 백그라운드 writer 코루틴으로 비동기 처리
    - JSON 백업은 내부 TradeJournal 인스턴스에 위임
    """

    def __init__(self, db_url: str = None):
        self.db_url = db_url or os.getenv("DATABASE_URL", "")
        self.pool: Optional[asyncpg.Pool] = None
        self._db_available = False

        # JSON 백업 전담
        self._journal = TradeJournal()

        # 인메모리 캐시 (TradeJournal에서 가져옴)
        self._trades = self._journal._trades
        self._today_trades = self._journal._today_trades

        # DB 비동기 쓰기 큐
        self._write_queue: Optional[asyncio.Queue] = None
        self._writer_task: Optional[asyncio.Task] = None
        self._execution_receipts = {}
        self._execution_batches = {}
        self._closing = False

    # ── 라이프사이클 ──────────────────────────────────────

    async def connect(self):
        """DB 연결 + 스키마 생성 + writer 시작"""
        if not self.db_url:
            logger.warning("[TradeStorage] DATABASE_URL 미설정, JSON 전용 모드")
            return

        try:
            self.pool = await asyncpg.create_pool(
                self.db_url, min_size=1, max_size=5, command_timeout=30
            )
            await self._ensure_tables()
            self._db_available = True

            # writer 코루틴 시작
            self._write_queue = asyncio.Queue()
            self._writer_task = asyncio.create_task(self._db_writer())

            logger.info("[TradeStorage] DB 연결 완료, 듀얼 라이트 모드")
        except Exception as e:
            logger.error(f"[TradeStorage] DB 연결 실패, JSON 폴백: {e}")
            self._db_available = False

    async def disconnect(self):
        """큐 drain + DB 연결 종료"""
        self._closing = True
        # writer 중지 — 큐 크기 기반 동적 timeout (대량 청산 시 데이터 손실 방지)
        if self._writer_task and not self._writer_task.done():
            if self._write_queue:
                await self._write_queue.put(None)  # sentinel
                # 항목당 0.1초 + base 10초, 최대 60초 (지나친 셧다운 지연 방지)
                qsize = self._write_queue.qsize()
                drain_timeout = max(10.0, min(60.0, 10.0 + qsize * 0.1))
                try:
                    await asyncio.wait_for(self._writer_task, timeout=drain_timeout)
                except asyncio.TimeoutError:
                    remaining = self._write_queue.qsize()
                    self._writer_task.cancel()
                    logger.warning(
                        f"[TradeStorage] writer 타임아웃({drain_timeout:.0f}s), 강제 종료 "
                        f"— 미처리 큐 {remaining}건 (데이터 손실 가능)"
                    )

        for key, receipt in list(self._execution_receipts.items()):
            if receipt.status == "pending":
                self._execution_receipts[key] = ExecutionWriteReceipt("unknown", receipt.execution_id, "shutdown_unconfirmed")
        self._db_available = False
        if self.pool:
            await self.pool.close()
            self.pool = None
            logger.info("[TradeStorage] DB 연결 종료")

    async def _ensure_tables(self):
        """테이블 + 인덱스 생성 + 마이그레이션"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(SCHEMA_SQL)
                # 마이그레이션: market 컬럼 추가 (기존 DB 호환)
                await conn.execute(
                    "ALTER TABLE trades ADD COLUMN IF NOT EXISTS market VARCHAR(5) NOT NULL DEFAULT 'KR'"
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market)"
                )
                # 마이그레이션: trade_events.market (2026-08-05 — us_scheduler 직접 INSERT
                # 2곳이 이 컬럼을 참조하는데 스키마에 없어 US SELL 이벤트 기록이 무음 실패)
                await conn.execute(
                    "ALTER TABLE trade_events ADD COLUMN IF NOT EXISTS market VARCHAR(5) NOT NULL DEFAULT 'KR'"
                )
                await conn.execute(EXECUTION_SCHEMA_SQL)
        logger.info("[TradeStorage] 테이블 확인/생성 완료")

    @staticmethod
    def _refine_exit_type(exit_type: str, exit_reason: str) -> str:
        """exit_reason에 구체적 정보가 있으면 exit_type 세분화"""
        # LLM 본문의 위험 표현은 규칙 청산이 아니다. 주문 출처 분류를 보존한다.
        if exit_type == "llm_eod":
            return exit_type
        if not exit_reason:
            return exit_type
        r = exit_reason.lower()
        # 본전 이탈: reason에 "1차 익절" 등이 언급돼도 breakeven 우선
        if "본전 이탈" in r:
            return "breakeven"
        # 횡보/추세 무효화
        if exit_type in ("take_profit", "unknown", "", "manual") and ("횡보 청산" in r or "추세 무효화" in r):
            return "stale"
        # take_profit → first/second/third 세분화
        if exit_type in ("take_profit", "unknown", ""):
            if "1차 익절" in r or "1차익절" in r:
                return "first_take_profit"
            if "2차 익절" in r or "2차익절" in r:
                return "second_take_profit"
            if "3차 익절" in r or "3차익절" in r:
                return "third_take_profit"
        # reason에서 추론 가능한데 exit_type이 unknown인 경우
        if exit_type == "unknown":
            if "손절" in r:
                return "stop_loss"
            if "트레일링" in r:
                return "trailing"
            if "본전" in r:
                return "breakeven"
            if "익절" in r:
                return "take_profit"
        return exit_type

    # ── DB 비동기 Writer ──────────────────────────────────

    async def _db_writer(self):
        """한 batch의 재시도를 끝내기 전에 뒤 항목으로 진행하지 않는다."""
        while True:
            item = await self._write_queue.get()
            if item is None:
                self._write_queue.task_done()
                return
            try:
                if isinstance(item, ExecutionBatch):
                    key = execution_key(item.identity.account_scope, item.identity.execution_id)
                    try:
                        await self._commit_execution_batch(item)
                    except asyncio.CancelledError:
                        self._execution_receipts[key] = ExecutionWriteReceipt("unknown", item.identity.execution_id, "write_canceled")
                        raise
                    except (ValueError, asyncpg.IntegrityConstraintViolationError) as exc:
                        # 앞선 batch 미확정으로 거절된 것은 앞 batch 복구 뒤 재시도할 수 있게 사유를 분리한다 (48차 P2)
                        reason = "predecessor_uncommitted" if str(exc) == "predecessor_uncommitted" else "transaction_rejected"
                        self._execution_receipts[key] = ExecutionWriteReceipt("failed", item.identity.execution_id, reason)
                    except Exception:
                        self._execution_receipts[key] = ExecutionWriteReceipt("unknown", item.identity.execution_id, "commit_unconfirmed")
                    else:
                        self._execution_receipts[key] = ExecutionWriteReceipt("committed", item.identity.execution_id, "transaction_committed")
                else:
                    sql, params, retries_left = item
                    for attempt in range(retries_left + 1):
                        try:
                            async with self.pool.acquire() as conn:
                                await conn.execute(sql, *params)
                            break
                        except Exception:
                            if attempt == retries_left:
                                logger.error("[TradeStorage] 기존 DB 쓰기 최종 실패")
                            else:
                                await asyncio.sleep(attempt + 1)
            finally:
                self._write_queue.task_done()

    def _enqueue(self, sql: str, params: tuple):
        """DB 쓰기 큐에 추가 (동기 호출 가능)"""
        if self._closing or not self._db_available or self._write_queue is None:
            return
        try:
            self._write_queue.put_nowait((sql, params, 3))
        except Exception as e:
            logger.warning(f"[TradeStorage] 큐 추가 실패: {e}")

    # ── TradeJournal 호환 인터페이스 (동기) ────────────────

    def record_entry(
        self,
        trade_id: str,
        symbol: str,
        name: str,
        entry_price: float,
        entry_quantity: int,
        entry_reason: str,
        entry_strategy: str,
        signal_score: float = 0,
        indicators: Dict[str, float] = None,
        market_context: Dict[str, Any] = None,
        theme_info: Dict[str, Any] = None,
        market: str = "KR",
        entry_tags: list = None,
        entry_reasons: list = None,
        score_breakdown: Dict[str, float] = None,
        execution_identity: ExecutionIdentity = None,
        execution_time: datetime = None,
    ) -> TradeRecord:
        """진입 기록: 캐시 + JSON + DB큐"""
        if execution_identity is not None:
            return self._record_execution("BUY", locals())
        # 1) 캐시 + JSON (동기)
        _journal_kwargs = dict(
            trade_id=trade_id,
            symbol=symbol,
            name=name,
            entry_price=entry_price,
            entry_quantity=entry_quantity,
            entry_reason=entry_reason,
            entry_strategy=entry_strategy,
            signal_score=signal_score,
            indicators=indicators,
            market_context=market_context,
            theme_info=theme_info,
            market=market,
        )
        if entry_tags is not None:
            _journal_kwargs['entry_tags'] = entry_tags
        if entry_reasons is not None:
            _journal_kwargs['entry_reasons'] = entry_reasons
        if score_breakdown is not None:
            _journal_kwargs['score_breakdown'] = score_breakdown
        trade = self._journal.record_entry(**_journal_kwargs)

        # 2) DB 큐 — trades INSERT
        self._enqueue(
            """INSERT INTO trades
               (id, symbol, name, market, entry_time, entry_price, entry_quantity,
                entry_reason, entry_strategy, entry_signal_score,
                market_context, indicators_at_entry, theme_info, created_at, updated_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
               ON CONFLICT (id) DO NOTHING""",
            (
                trade.id, trade.symbol, trade.name, market,
                trade.entry_time, float(trade.entry_price), trade.entry_quantity,
                trade.entry_reason, trade.entry_strategy, float(trade.entry_signal_score),
                json.dumps(trade.market_context, default=str, ensure_ascii=False),
                json.dumps(trade.indicators_at_entry, default=str, ensure_ascii=False),
                json.dumps(trade.theme_info, default=str, ensure_ascii=False),
                trade.created_at, trade.updated_at,
            ),
        )

        # 3) DB 큐 — trade_events BUY INSERT
        self._enqueue(
            """INSERT INTO trade_events
               (trade_id, symbol, name, event_type, event_time, price, quantity,
                strategy, signal_score, status)
               VALUES ($1,$2,$3,'BUY',$4,$5,$6,$7,$8,'holding')""",
            (
                trade.id, trade.symbol, trade.name,
                trade.entry_time, float(trade.entry_price), trade.entry_quantity,
                trade.entry_strategy, float(trade.entry_signal_score),
            ),
        )

        return trade

    def record_exit(
        self,
        trade_id: str,
        exit_price: float,
        exit_quantity: int,
        exit_reason: str,
        exit_type: str,
        indicators: Dict[str, float] = None,
        exit_time: datetime = None,
        avg_entry_price: float = None,
        # 폴백용 optional (메모리에 trade 없을 때 최소 레코드 생성)
        symbol: str = None,
        name: str = None,
        entry_price: float = None,
        entry_strategy: str = None,
        execution_identity: ExecutionIdentity = None,
        execution_time: datetime = None,
    ) -> Optional[TradeRecord]:
        """청산 기록: 캐시 + JSON + DB큐"""
        if execution_identity is not None:
            return self._record_execution("SELL", locals())
        # exit_type 세분화: reason에 구체적 정보가 있으면 재분류
        exit_type = self._refine_exit_type(exit_type, exit_reason)

        # 누적 PnL 캡처 (이번 매도분 PnL 계산용)
        prev_pnl = 0.0
        prev_trade = self._journal.get_trade(trade_id)
        if prev_trade:
            prev_pnl = float(prev_trade.pnl)

        # 1) 캐시 + JSON (폴백 파라미터 전달)
        trade = self._journal.record_exit(
            trade_id=trade_id,
            exit_price=exit_price,
            exit_quantity=exit_quantity,
            exit_reason=exit_reason,
            exit_type=exit_type,
            indicators=indicators,
            exit_time=exit_time,
            avg_entry_price=avg_entry_price,
            symbol=symbol,
            name=name,
            entry_price=entry_price,
            entry_strategy=entry_strategy,
        )
        if not trade:
            return None

        # 이번 매도분 PnL (누적 - 이전)
        this_sell_pnl = float(trade.pnl) - prev_pnl
        entry_price_for_pct = avg_entry_price or float(trade.entry_price)
        invested_this = entry_price_for_pct * exit_quantity
        this_sell_pnl_pct = (this_sell_pnl / invested_this * 100) if invested_this > 0 else 0.0

        # 상태 결정
        total_exited = trade.exit_quantity or 0
        is_fully_closed = total_exited >= trade.entry_quantity
        status = exit_type if is_fully_closed else "partial"

        # 2) DB 큐 — trades UPSERT (누락된 KIS_SYNC 등 부모 레코드 보장 후 UPDATE)
        # Step 2a: 부모 레코드가 없을 경우에만 INSERT (FK 위반 방지)
        self._enqueue(
            """INSERT INTO trades
               (id, symbol, name, entry_time, entry_price, entry_quantity,
                entry_reason, entry_strategy, entry_signal_score,
                market_context, indicators_at_entry, theme_info, created_at, updated_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
               ON CONFLICT (id) DO NOTHING""",
            (
                trade.id, trade.symbol, trade.name,
                trade.entry_time, float(trade.entry_price), trade.entry_quantity,
                trade.entry_reason, trade.entry_strategy, float(trade.entry_signal_score),
                json.dumps(trade.market_context, default=str, ensure_ascii=False),
                json.dumps(trade.indicators_at_entry, default=str, ensure_ascii=False),
                json.dumps(trade.theme_info, default=str, ensure_ascii=False),
                trade.created_at, trade.updated_at,
            ),
        )
        # Step 2b: 청산 필드 UPDATE (ON CONFLICT DO NOTHING 이후 항상 실행)
        self._enqueue(
            """UPDATE trades SET
               exit_time=$1, exit_price=$2, exit_quantity=$3,
               exit_reason=$4, exit_type=$5, pnl=$6, pnl_pct=$7,
               holding_minutes=$8, indicators_at_exit=$9, updated_at=$10
               WHERE id=$11""",
            (
                trade.exit_time, float(trade.exit_price), trade.exit_quantity,
                trade.exit_reason, trade.exit_type,
                round(float(trade.pnl)), float(trade.pnl_pct),
                trade.holding_minutes,
                json.dumps(trade.indicators_at_exit, default=str, ensure_ascii=False),
                trade.updated_at, trade.id,
            ),
        )

        # 3) DB 큐 — trade_events SELL INSERT (이번 매도분 PnL)
        self._enqueue(
            """INSERT INTO trade_events
               (trade_id, symbol, name, event_type, event_time, price, quantity,
                exit_type, exit_reason, pnl, pnl_pct, strategy, signal_score, status)
               VALUES ($1,$2,$3,'SELL',$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)""",
            (
                trade.id, trade.symbol, trade.name,
                trade.exit_time, float(exit_price), exit_quantity,
                exit_type, exit_reason,
                round(float(this_sell_pnl)), float(this_sell_pnl_pct),
                trade.entry_strategy, float(trade.entry_signal_score),
                status,
            ),
        )

        # 4) 미청산 BUY 이벤트 상태 업데이트
        if is_fully_closed:
            self._enqueue(
                """UPDATE trade_events SET status=$1
                   WHERE trade_id=$2 AND event_type='BUY'""",
                (status, trade.id),
            )

        return trade

    def _record_execution(self, side, values):
        identity = values["execution_identity"]
        if not isinstance(identity, ExecutionIdentity):
            raise ValueError("invalid_execution_identity")
        key = execution_key(identity.account_scope, identity.execution_id)
        args = {name: value for name, value in values.items() if name != "self"}
        try:
            if side == "SELL":
                args["exit_type"] = self._refine_exit_type(args["exit_type"], args["exit_reason"])
                trade = self._journal.record_exit(**args)
            else:
                trade = self._journal.record_entry(**args)
        except Exception:
            self._execution_receipts[key] = ExecutionWriteReceipt("failed", identity.execution_id, "journal_record_failed")
            raise
        record = trade.execution_records[key]
        batch = ExecutionBatch(identity, record["signature"],
            json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False), record["predecessor"])
        try:
            validate_batch(batch)
        except Exception:
            self._execution_receipts[key] = ExecutionWriteReceipt("failed", identity.execution_id, "execution_payload_corrupt")
            raise
        existing = self._execution_receipts.get(key)
        self._execution_batches[key] = batch
        if existing and existing.status in {"pending", "committed"}:
            return trade
        if self._closing or not self._db_available or self.pool is None or self._write_queue is None:
            self._execution_receipts[key] = ExecutionWriteReceipt("unavailable", identity.execution_id, "database_unavailable")
            return trade
        self._execution_receipts[key] = ExecutionWriteReceipt("pending", identity.execution_id, "queued")
        try:
            self._write_queue.put_nowait(batch)
        except Exception:
            self._execution_receipts[key] = ExecutionWriteReceipt("failed", identity.execution_id, "queue_rejected")
        return trade

    def get_execution_receipt(self, account_scope, execution_id):
        return self._execution_receipts.get(execution_key(account_scope, execution_id),
            ExecutionWriteReceipt("unknown", execution_id, "not_observed"))

    async def resolve_execution_receipt(self, account_scope, execution_id):
        """불명/일시 장애 영수증을 DB 행으로 확정한다. 행이 없고 보관 batch 가 있으면 한 번 다시 큐에 넣는다.

        확정 거절(서명 충돌·payload 손상·저널 기록 실패)은 다시 시도하지 않는다. 재삽입은 유일 인덱스·서명
        검사로 멱등이라 중복 행을 만들지 않는다 (48차 P2).
        """
        key = execution_key(account_scope, execution_id)
        receipt = self._execution_receipts.get(key)
        if receipt is None or receipt.status in ("pending", "committed"):
            return receipt
        retryable = (receipt.status in ("unknown", "unavailable")
                     or receipt.reason in ("queue_rejected", "predecessor_uncommitted"))
        if not retryable:
            return receipt
        looked = await self.lookup_execution_receipt(account_scope, execution_id)
        batch = self._execution_batches.get(key)
        if (looked.status == "unknown" and looked.reason == "not_committed" and batch is not None
                and not self._closing and self._db_available and self.pool is not None and self._write_queue is not None):
            self._execution_receipts[key] = ExecutionWriteReceipt("pending", execution_id, "requeued")
            try:
                self._write_queue.put_nowait(batch)
            except Exception:
                self._execution_receipts[key] = ExecutionWriteReceipt("failed", execution_id, "queue_rejected")
            return self._execution_receipts[key]
        return looked

    async def lookup_execution_receipt(self, account_scope, execution_id):
        key = execution_key(account_scope, execution_id)
        if not self._db_available or self.pool is None:
            return ExecutionWriteReceipt("unavailable", execution_id, "database_unavailable")
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("SELECT execution_signature, execution_payload_digest FROM trade_events WHERE account_scope=$1 AND execution_id=$2", account_scope, execution_id)
            expected = self._execution_batches.get(key)
            if row is None:
                result = ExecutionWriteReceipt("unknown", execution_id, "not_committed")
            elif expected is not None and (row["execution_signature"] != expected.signature
                    or row["execution_payload_digest"] != validate_batch(expected)["payload_digest"]):
                result = ExecutionWriteReceipt("failed", execution_id, "execution_signature_conflict")
            else:
                result = ExecutionWriteReceipt("committed", execution_id, "database_row_confirmed")
        except Exception:
            result = ExecutionWriteReceipt("unknown", execution_id, "database_lookup_failed")
        self._execution_receipts[key] = result
        return result

    async def _commit_execution_batch(self, batch):
        data = validate_batch(batch)
        summary, event = data["summary"], data["event"]
        identity = batch.identity
        key = execution_key(identity.account_scope, identity.execution_id)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL synchronous_commit = on")
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", summary["id"])
                known = await conn.fetchrow("SELECT execution_signature, execution_payload_digest FROM trade_events WHERE account_scope=$1 AND execution_id=$2", identity.account_scope, identity.execution_id)
                if known is not None:
                    if known["execution_signature"] != batch.signature or known["execution_payload_digest"] != data["payload_digest"]:
                        raise ValueError("execution_signature_conflict")
                    return
                parent = await conn.fetchrow("SELECT symbol, execution_scope, execution_head FROM trades WHERE id=$1 FOR UPDATE", summary["id"])
                if parent and (parent["symbol"] != summary["symbol"] or parent["execution_scope"] not in (None, identity.account_scope)):
                    raise ValueError("execution_trade_conflict")
                if (parent["execution_head"] if parent else None) != batch.predecessor:
                    raise ValueError("predecessor_uncommitted")
                columns = ["id", "symbol", "name", "entry_time", "entry_price", "entry_quantity",
                    "entry_reason", "entry_strategy", "entry_signal_score", "exit_time", "exit_price",
                    "exit_quantity", "exit_reason", "exit_type", "pnl", "pnl_pct", "holding_minutes",
                    "market_context", "indicators_at_entry", "indicators_at_exit", "theme_info", "created_at", "updated_at"]
                numeric = {"entry_price", "entry_signal_score", "exit_price", "pnl", "pnl_pct"}
                timestamps = {"entry_time", "exit_time", "created_at", "updated_at"}
                json_fields = {"market_context", "indicators_at_entry", "indicators_at_exit", "theme_info"}
                parameters = []
                for name in columns:
                    value = summary[name]
                    if name in numeric:
                        value = Decimal(str(value))
                    elif name in timestamps:
                        value = datetime.fromisoformat(value) if value is not None else None
                    elif name in json_fields:
                        value = json.dumps(value, ensure_ascii=False, allow_nan=False)
                    parameters.append(value)
                columns += ["market", "execution_scope", "execution_head"]
                parameters += ["KR", identity.account_scope, key]
                placeholders = ",".join(f"${i}" for i in range(1, len(columns)+1))
                updates = ",".join(f"{column}=EXCLUDED.{column}" for column in columns if column != "id")
                await conn.execute(f"INSERT INTO trades ({','.join(columns)}) VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {updates}", *parameters)
                await conn.execute("""INSERT INTO trade_events
                    (trade_id,symbol,name,market,event_type,event_time,price,quantity,
                     exit_type,exit_reason,pnl,pnl_pct,strategy,status,kis_order_no,
                     account_scope,order_date,execution_id,execution_signature,execution_payload_digest,signal_score)
                    VALUES ($1,$2,$3,'KR',$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)""",
                    summary["id"], event["symbol"], event["name"], event["side"],
                    datetime.fromisoformat(event["event_time"]), Decimal(event["price"]), event["quantity"],
                    event["exit_type"] or None, event["reason"],
                    Decimal(str(event["pnl"])) if event["pnl"] is not None else None,
                    Decimal(str(event["pnl_pct"])) if event["pnl_pct"] is not None else None,
                    event["strategy"], event["status"], identity.kis_order_no,
                    identity.account_scope, identity.order_date, identity.execution_id, batch.signature, data["payload_digest"], Decimal(str(event["signal_score"])))
                if event["side"] == "SELL" and summary["exit_quantity"] >= summary["entry_quantity"]:
                    await conn.execute("UPDATE trade_events SET status=$1 WHERE trade_id=$2 AND event_type='BUY'", event["status"], summary["id"])

    def get_trade(self, trade_id: str) -> Optional[TradeRecord]:
        return self._journal.get_trade(trade_id)

    def update_market_context(self, trade_id: str, patch: Dict[str, Any]) -> bool:
        """JSON 저널 + DB(JSONB) 양쪽의 `market_context` 를 병합 갱신 (2026-09-14 T3 A 배선).

        부분체결 주문이 뒤늦게 완결됐을 때 이미 기록된 레코드의 `entry_risk` 를 확정값으로
        바꾸는 용도다. 기존 `record_entry`/`record_exit` 시그니처는 그대로 둔다.
        """
        if not self._journal.update_market_context(trade_id, patch):
            return False
        trade = self._journal.get_trade(trade_id)
        if trade is None:
            return False
        self._enqueue(
            "UPDATE trades SET market_context=$1, updated_at=$2 WHERE id=$3",
            (
                json.dumps(trade.market_context, default=str, ensure_ascii=False),
                trade.updated_at,
                trade_id,
            ),
        )
        return True

    def get_today_trades(self) -> List[TradeRecord]:
        return self._journal.get_today_trades()

    def get_trades_by_date(self, trade_date: date) -> List[TradeRecord]:
        return self._journal.get_trades_by_date(trade_date)

    def get_trades_by_strategy(self, strategy: str, days: int = 30) -> List[TradeRecord]:
        return self._journal.get_trades_by_strategy(strategy, days)

    def get_closed_trades(self, days: int = 30) -> List[TradeRecord]:
        return self._journal.get_closed_trades(days)

    async def sync_from_db(self, days: int = 7):
        """DB에서 JSON 누락 거래를 _trades에 보강 (리밸런싱용)"""
        return await self._journal.sync_from_db(days)

    def get_open_trades(self) -> List[TradeRecord]:
        return self._journal.get_open_trades()

    def recover_trade(self, trade_id: str) -> bool:
        return self._journal.recover_trade(trade_id)

    async def recover_trade_async(self, trade_id: str) -> bool:
        return await self._journal.recover_trade_async(trade_id)

    def get_recent_trades(self, days: int = 7) -> List[TradeRecord]:
        return self._journal.get_recent_trades(days)

    def get_statistics(self, days: int = 30) -> Dict[str, Any]:
        return self._journal.get_statistics(days)

    async def get_statistics_from_db(self, days: int = 30) -> Dict[str, Any]:
        """DB 기반 거래 통계 (JSON 대체, 청산 완료 건만)"""
        if not self._db_available or not self.pool:
            return self._journal.get_statistics(days)

        # 풀 상태 검증 — 풀이 닫혀있으면 재연결 시도
        try:
            if self.pool._closed:
                logger.warning("[TradeStorage] DB 풀 닫힘 — 재연결 시도")
                await self.connect()
                if not self._db_available:
                    return self._journal.get_statistics(days)
        except AttributeError:
            pass

        cutoff = datetime.now() - timedelta(days=days)
        try:
            async with self.pool.acquire() as conn:
                # 1) 기본 통계
                row = await conn.fetchrow("""
                    SELECT COUNT(*) as total,
                           COUNT(*) FILTER (WHERE pnl > 0) as wins,
                           COUNT(*) FILTER (WHERE pnl <= 0) as losses,
                           COALESCE(SUM(pnl), 0) as total_pnl,
                           COALESCE(AVG(pnl_pct), 0) as avg_pnl_pct,
                           COALESCE(AVG(holding_minutes), 0) as avg_holding
                    FROM trades
                    WHERE exit_time IS NOT NULL
                      AND entry_time >= $1
                """, cutoff)

                total = row['total']
                if total == 0:
                    return {
                        "total_trades": 0, "win_rate": 0, "avg_pnl_pct": 0,
                        "total_pnl": 0, "avg_holding_minutes": 0,
                        "best_trade": None, "worst_trade": None,
                        "by_strategy": {}, "by_exit_type": {},
                    }

                # 2) 전략별
                strat_rows = await conn.fetch("""
                    SELECT entry_strategy, COUNT(*) as trades,
                           COUNT(*) FILTER (WHERE pnl > 0) as wins,
                           COALESCE(SUM(pnl), 0) as total_pnl,
                           COALESCE(AVG(pnl_pct), 0) as avg_pnl_pct
                    FROM trades
                    WHERE exit_time IS NOT NULL AND entry_time >= $1
                    GROUP BY entry_strategy
                """, cutoff)

                # 3) 청산유형별
                exit_rows = await conn.fetch("""
                    SELECT exit_type, COUNT(*) as trades,
                           COUNT(*) FILTER (WHERE pnl > 0) as wins,
                           COALESCE(AVG(pnl_pct), 0) as avg_pnl_pct
                    FROM trades
                    WHERE exit_time IS NOT NULL AND entry_time >= $1
                    GROUP BY exit_type
                """, cutoff)

                # 4) best/worst
                best = await conn.fetchrow("""
                    SELECT symbol, name, pnl_pct FROM trades
                    WHERE exit_time IS NOT NULL AND entry_time >= $1
                    ORDER BY pnl_pct DESC LIMIT 1
                """, cutoff)
                worst = await conn.fetchrow("""
                    SELECT symbol, name, pnl_pct FROM trades
                    WHERE exit_time IS NOT NULL AND entry_time >= $1
                    ORDER BY pnl_pct ASC LIMIT 1
                """, cutoff)

            # dict 구성 (기존 JSON 응답 구조 호환)
            by_strategy = {}
            for sr in strat_rows:
                key = sr['entry_strategy'] or 'unknown'
                trades_cnt = sr['trades']
                by_strategy[key] = {
                    "trades": trades_cnt,
                    "wins": sr['wins'],
                    "total_pnl": float(sr['total_pnl']),
                    "avg_pnl_pct": float(sr['avg_pnl_pct']),
                    "win_rate": sr['wins'] / trades_cnt * 100 if trades_cnt > 0 else 0,
                }

            by_exit_type = {}
            for er in exit_rows:
                key = er['exit_type'] or 'unknown'
                by_exit_type[key] = {
                    "trades": er['trades'],
                    "wins": er['wins'],
                    "avg_pnl_pct": float(er['avg_pnl_pct']),
                }

            best_dict = None
            if best:
                best_dict = {"symbol": best['symbol'], "name": best['name'],
                             "pnl_pct": float(best['pnl_pct'])}
            worst_dict = None
            if worst:
                worst_dict = {"symbol": worst['symbol'], "name": worst['name'],
                              "pnl_pct": float(worst['pnl_pct'])}

            return {
                "total_trades": total,
                "wins": row['wins'],
                "losses": row['losses'],
                "win_rate": row['wins'] / total * 100 if total > 0 else 0,
                "avg_pnl_pct": float(row['avg_pnl_pct']),
                "total_pnl": float(row['total_pnl']),
                "avg_holding_minutes": float(row['avg_holding']),
                "best_trade": best_dict,
                "worst_trade": worst_dict,
                "by_strategy": by_strategy,
                "by_exit_type": by_exit_type,
            }

        except Exception as e:
            logger.warning(f"[TradeStorage] DB 통계 조회 실패, JSON 폴백: {type(e).__name__}: {e}")
            return self._journal.get_statistics(days)

    def update_review(
        self,
        trade_id: str,
        review_notes: str = "",
        lesson_learned: str = "",
        improvement_suggestion: str = "",
    ):
        self._journal.update_review(
            trade_id, review_notes, lesson_learned, improvement_suggestion
        )
        self._enqueue(
            """UPDATE trades SET review_notes=$1, lesson_learned=$2,
               improvement_suggestion=$3, updated_at=$4 WHERE id=$5""",
            (review_notes, lesson_learned, improvement_suggestion,
             datetime.now(), trade_id),
        )

    # ── 새 메서드: trade_events DB 쿼리 ──────────────────

    async def get_trade_events(
        self,
        target_date: date = None,
        event_type: str = "all",
        limit: int = 200,
        market: str = "all",
    ) -> List[Dict]:
        """
        trade_events 테이블에서 이벤트 로그 조회.

        DB 미연결 시 캐시에서 이벤트 구성.
        market: 'all' | 'KR' | 'US'
        """
        if not self._db_available:
            return self._get_events_from_cache(target_date, event_type)

        target_date = target_date or date.today()
        try:
            sql = """
                SELECT te.*, t.entry_price, t.entry_quantity
                FROM trade_events te
                JOIN trades t ON te.trade_id = t.id
                WHERE te.event_time::date = $1
                  -- KIS_SYNC BUY 중복 제거: 봇 BUY가 있으면 KIS_SYNC BUY 숨김
                  AND NOT (
                    te.trade_id LIKE 'KIS_SYNC_%%'
                    AND EXISTS (
                      SELECT 1 FROM trade_events te2
                      WHERE te2.symbol = te.symbol
                        AND te2.event_type = te.event_type
                        AND te2.event_time::date = te.event_time::date
                        AND te2.trade_id NOT LIKE 'KIS_SYNC_%%'
                    )
                  )
            """
            params = [target_date]

            if market and market.upper() in ("KR", "US"):
                sql += f" AND t.market = ${len(params) + 1}"
                params.append(market.upper())

            if event_type and event_type != "all":
                sql += f" AND te.event_type = ${len(params) + 1}"
                params.append(event_type.upper())

            sql += " ORDER BY te.event_time DESC LIMIT $" + str(len(params) + 1)
            params.append(limit)

            async with self.pool.acquire() as conn:
                rows = await conn.fetch(sql, *params)

            return [self._row_to_event_dict(row) for row in rows]
        except Exception as e:
            logger.warning(f"[TradeStorage] trade_events 쿼리 실패, 캐시 폴백: {e}")
            return self._get_events_from_cache(target_date, event_type)

    def _row_to_event_dict(self, row) -> Dict:
        """asyncpg Row → dict 변환"""
        d = dict(row)
        # Decimal → float, datetime → isoformat
        for k, v in d.items():
            if isinstance(v, Decimal):
                d[k] = float(v)
            elif isinstance(v, datetime):
                d[k] = v.isoformat()
        return d

    def _get_events_from_cache(
        self, target_date: date = None, event_type: str = "all"
    ) -> List[Dict]:
        """DB 미사용 시 캐시 기반 이벤트 목록 구성"""
        target_date = target_date or date.today()
        events = []

        for trade in self._trades.values():
            if not trade.entry_time:
                continue

            # 진입 이벤트
            if trade.entry_time.date() == target_date:
                if event_type in ("all", "buy"):
                    exit_qty = trade.exit_quantity or 0
                    is_closed = exit_qty >= trade.entry_quantity
                    status = trade.exit_type if is_closed else "holding"
                    events.append({
                        "trade_id": trade.id,
                        "symbol": trade.symbol,
                        "name": trade.name,
                        "event_type": "BUY",
                        "event_time": trade.entry_time.isoformat(),
                        "price": float(trade.entry_price),
                        "quantity": trade.entry_quantity,
                        "strategy": trade.entry_strategy,
                        "signal_score": float(trade.entry_signal_score),
                        "status": status,
                        "entry_price": float(trade.entry_price),
                        "entry_quantity": trade.entry_quantity,
                    })

            # 청산 이벤트
            if trade.exit_time and trade.exit_time.date() == target_date:
                if event_type in ("all", "sell"):
                    events.append({
                        "trade_id": trade.id,
                        "symbol": trade.symbol,
                        "name": trade.name,
                        "event_type": "SELL",
                        "event_time": trade.exit_time.isoformat(),
                        "price": float(trade.exit_price),
                        "quantity": trade.exit_quantity or 0,
                        "exit_type": trade.exit_type,
                        "exit_reason": trade.exit_reason,
                        "pnl": float(trade.pnl),
                        "pnl_pct": float(trade.pnl_pct),
                        "strategy": trade.entry_strategy,
                        "signal_score": float(trade.entry_signal_score),
                        "status": trade.exit_type or "closed",
                        "entry_price": float(trade.entry_price),
                        "entry_quantity": trade.entry_quantity,
                    })

        # 역시간순 정렬
        events.sort(key=lambda e: e["event_time"], reverse=True)
        return events

    # ── KIS 동기화 ────────────────────────────────────────

    # 수수료/세금 상수 — FeeCalculator 기준값 사용 (fee_calculator.py 단일 소스)
    _fee_config = FeeConfig()
    _BUY_FEE_RATE = _fee_config.buy_commission_rate    # Decimal 매수 수수료
    _SELL_FEE_RATE = _fee_config.sell_commission_rate   # Decimal 매도 수수료
    _SELL_TAX_RATE = _fee_config.sell_tax_rate          # Decimal 증권거래세
    # float 호환 (기존 코드 하위호환)
    BUY_FEE_RATE = float(_fee_config.buy_commission_rate)
    SELL_FEE_RATE = float(_fee_config.sell_commission_rate)
    SELL_TAX_RATE = float(_fee_config.sell_tax_rate)

    @staticmethod
    def _parse_kis_time(ord_tmd: str, base_date: date) -> Optional[datetime]:
        """KIS ord_tmd (HHMMSS) → datetime 변환"""
        if not ord_tmd or len(ord_tmd) < 6:
            return None
        try:
            h, m, s = int(ord_tmd[:2]), int(ord_tmd[2:4]), int(ord_tmd[4:6])
            return datetime(base_date.year, base_date.month, base_date.day, h, m, s)
        except (ValueError, TypeError):
            return None

    @classmethod
    def calc_pnl(cls, entry_price: float, exit_price: float, quantity: int) -> tuple:
        """KIS 체결 기반 정확한 PnL 계산 (수수료+세금 포함, Decimal 정밀 연산).

        Returns:
            (pnl, pnl_pct) — 원 단위 손익(int), 퍼센트 수익률(float)
        """
        d_entry = Decimal(str(entry_price))
        d_exit = Decimal(str(exit_price))
        d_qty = Decimal(str(quantity))

        buy_amount = d_entry * d_qty
        sell_amount = d_exit * d_qty
        buy_fee = buy_amount * cls._BUY_FEE_RATE
        sell_fee = sell_amount * cls._SELL_FEE_RATE
        sell_tax = sell_amount * cls._SELL_TAX_RATE
        cost = buy_amount + buy_fee
        net = sell_amount - sell_fee - sell_tax
        pnl = net - cost
        pnl_pct = (pnl / cost * Decimal("100")) if cost > 0 else Decimal("0")
        return int(pnl.to_integral_value(rounding=ROUND_HALF_UP)), float(pnl_pct.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

    @classmethod
    def calc_pnl_us(cls, entry_price: float, exit_price: float, quantity: int) -> tuple:
        """US 체결 기반 PnL 계산 (zero-commission).

        Returns:
            (pnl_usd: float, pnl_pct: float) — USD 손익, 퍼센트 수익률
        """
        d_entry = Decimal(str(entry_price))
        d_exit = Decimal(str(exit_price))
        d_qty = Decimal(str(quantity))

        buy_amount = d_entry * d_qty
        sell_amount = d_exit * d_qty
        pnl = sell_amount - buy_amount  # zero-commission
        pnl_pct = (pnl / buy_amount * Decimal("100")) if buy_amount > 0 else Decimal("0")

        pnl_rounded = float(pnl.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        pct_rounded = float(pnl_pct.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        return pnl_rounded, pct_rounded

    def _find_recovery_target(self, symbol: str, today: date,
                              db_trades: Dict[str, 'TradeRecord'] = None) -> Optional[TradeRecord]:
        """매도 복구 대상 trade 찾기 (우선순위: 오늘 미청산 → 오늘 부분청산 → 오늘 최근 → 전체 미청산)

        캐시(JSON)가 손상/비어있을 수 있으므로 db_trades도 함께 검색합니다.
        """
        # 캐시 + DB 통합 (trade_id 기준 중복 제거, 캐시 우선)
        combined = {}
        if db_trades:
            combined.update(db_trades)
        combined.update(self._journal._trades)
        all_trades = list(combined.values())

        # 1. 오늘 진입한 미청산 거래
        today_open = [
            t for t in all_trades
            if t.symbol == symbol and not t.is_closed
            and t.entry_time and t.entry_time.date() == today
        ]
        if today_open:
            today_open.sort(key=lambda t: t.entry_time)
            return today_open[0]

        # 2. 오늘 진입한 거래 중 exit_qty < entry_qty (부분만 기록된 거래)
        today_partial = [
            t for t in all_trades
            if t.symbol == symbol
            and t.entry_time and t.entry_time.date() == today
            and (t.exit_quantity or 0) < t.entry_quantity
        ]
        if today_partial:
            today_partial.sort(key=lambda t: t.entry_time)
            return today_partial[0]

        # 3. 오늘 진입한 거래 중 가장 최근 (이미 closed라도)
        today_all = [
            t for t in all_trades
            if t.symbol == symbol
            and t.entry_time and t.entry_time.date() == today
        ]
        if today_all:
            today_all.sort(key=lambda t: t.entry_time, reverse=True)
            return today_all[0]

        # 4. 전체 미청산 거래 (FIFO)
        all_open = [
            t for t in all_trades
            if t.symbol == symbol and not t.is_closed
        ]
        if all_open:
            all_open.sort(key=lambda t: t.entry_time)
            return all_open[0]

        # 5. 2026-04-22 추가: 잔여 수량이 남은 부분청산 거래 (is_closed=True라도 포함)
        #   케이스: 어제 진입 → 오늘 1차 익절로 exit_time 세팅됨 → 2차/3차 매도 복구 대상
        #   entry_time 날짜 제한 없음(최근 N일 이내 FIFO).
        partial_any = [
            t for t in all_trades
            if t.symbol == symbol
            and (t.exit_quantity or 0) < t.entry_quantity
            and t.entry_time
        ]
        if partial_any:
            partial_any.sort(key=lambda t: t.entry_time, reverse=True)
            return partial_any[0]

        return None

    async def sync_from_kis(self, broker, engine=None) -> KISSyncResult:
        """Compare checked KR fills against durable, explicitly identified events.

        Read-only comparison, not restart execution replay. Legacy symbol-only
        attribution cannot distinguish manual/account trades from bot orders.
        Missing identity requires operator reconciliation or a future durable
        execution ledger. Neither journal nor Portfolio is modified here.
        ``complete`` covers this comparison only, not portfolio health or a
        persistence receipt for queued journal writes.
        """
        try:
            checked_query = getattr(broker, "get_fills_for_date_checked", None)
            if not callable(checked_query):
                logger.warning("[TradeStorage] broker에 get_fills_for_date_checked 없음, 동기화 건너뜀")
                return KISSyncResult("unsupported", "checked_query_unavailable")

            today = date.today()
            fills, complete, reason = await checked_query(today)
            if complete is not True:
                logger.warning(
                    f"[TradeStorage] KIS 당일 체결 조회 미완결, 동기화 건너뜀: "
                    f"{reason or '완결 여부 불명'}"
                )
                return KISSyncResult("incomplete", str(reason or "query_completeness_unknown"))
            if not isinstance(fills, list):
                return KISSyncResult("incomplete", "invalid_fill_list")
            if date.today() != today:
                return KISSyncResult("incomplete", "query_date_changed")

            # Preflight the whole account response. Even identical repeated rows
            # are not assumed to be harmless pagination duplicates.
            expected = {}
            symbol_sides = set()
            order_numbers = set()
            for fill in fills:
                if not isinstance(fill, dict):
                    return KISSyncResult("incomplete", "invalid_fill_row")
                symbol = fill.get("symbol")
                side = {"02": "BUY", "01": "SELL"}.get(fill.get("sll_buy_dvsn_cd"))
                odno = fill.get("odno")
                if not isinstance(symbol, str) or not symbol.strip() or side is None:
                    return KISSyncResult("ambiguous", "fill_identity_missing")
                if not isinstance(odno, str) or not odno.strip():
                    return KISSyncResult("ambiguous", "fill_order_number_missing")
                symbol, odno = symbol.strip(), odno.strip()
                if (symbol, side) in symbol_sides or odno in order_numbers:
                    return KISSyncResult("ambiguous", "multiple_or_duplicate_symbol_orders")
                symbol_sides.add((symbol, side))
                order_numbers.add(odno)
                values = self._kis_sync_values(fill.get("tot_ccld_qty"), fill.get("avg_prvs"))
                if values is None:
                    return KISSyncResult("incomplete", "invalid_fill_quantity_or_price")
                expected[(symbol, side, odno)] = values

            if not self._db_available or self.pool is None:
                return KISSyncResult("unsupported", "durable_order_identity_unavailable")
            if self._write_queue is not None and self._write_queue._unfinished_tasks:
                return KISSyncResult("incomplete", "journal_writes_pending")

            # All persisted KR BUY/SELL events for the checked local date, in one
            # SELECT snapshot. Filtering only matching ODNOs hides legacy NULLs
            # and extra events. Existing queue status is not persistence proof.
            rows = await self.pool.fetch(
                """SELECT e.trade_id, e.symbol, e.event_type, e.kis_order_no,
                          e.quantity, e.price
                   FROM trade_events e
                   JOIN trades t ON t.id = e.trade_id
                   WHERE e.event_time::date = $1 AND t.market = 'KR'
                     AND e.event_type IN ('BUY', 'SELL')""",
                today,
            )
            recorded = {}
            trade_by_symbol = {}
            for row in rows:
                symbol, side = row['symbol'], row['event_type']
                odno, trade_id = row['kis_order_no'], row['trade_id']
                if (not isinstance(symbol, str) or not symbol.strip()
                        or side not in ("BUY", "SELL")
                        or not isinstance(odno, str) or not odno.strip()
                        or not isinstance(trade_id, str) or not trade_id.strip()):
                    return KISSyncResult("ambiguous", "persisted_order_identity_missing")
                symbol, odno = symbol.strip(), odno.strip()
                key = (symbol, side, odno)
                if key in recorded:
                    return KISSyncResult("ambiguous", "multiple_persisted_order_events")
                if symbol in trade_by_symbol and trade_by_symbol[symbol] != trade_id:
                    return KISSyncResult("ambiguous", "multiple_symbol_trade_owners")
                trade_by_symbol[symbol] = trade_id
                values = self._kis_sync_values(row['quantity'], row['price'])
                if values is None:
                    return KISSyncResult("incomplete", "invalid_persisted_quantity_or_price")
                recorded[key] = values

            if expected.keys() - recorded.keys():
                return KISSyncResult("ambiguous", "unattributed_account_fill")
            if recorded.keys() - expected.keys():
                return KISSyncResult("incomplete", "persisted_events_absent_from_query")
            if recorded != expected:
                return KISSyncResult("incomplete", "persisted_fill_values_differ")
            if date.today() != today:
                return KISSyncResult("incomplete", "query_date_changed")
            if self._write_queue is not None and self._write_queue._unfinished_tasks:
                return KISSyncResult("incomplete", "journal_writes_pending")
            if not fills:
                logger.info("[TradeStorage] KIS 당일 체결 0건, DB 이벤트도 0건 확인")
                return KISSyncResult("verified_empty", "checked_query_and_persisted_events_empty")
            return KISSyncResult("reconciled", "persisted_order_events_match_no_replay")
        except Exception as exc:
            # No writes occur here: no hidden partial recovery and no fallback
            # to symbol/quantity attribution or a guessed strategy.
            logger.error(f"[TradeStorage] KIS 장부 비교 실패: {exc}")
            return KISSyncResult("error", f"comparison_failed:{type(exc).__name__}")

    @staticmethod
    def _kis_sync_values(quantity, price):
        """Finite, positive comparison values; never round missing fills away."""
        if isinstance(quantity, bool) or isinstance(price, bool):
            return None
        try:
            qty, value = Decimal(str(quantity)), Decimal(str(price))
            if (not qty.is_finite() or not value.is_finite()
                    or qty <= 0 or qty != qty.to_integral_value() or value <= 0):
                return None
            return int(qty), value
        except (ValueError, ArithmeticError):
            return None

    async def sync_from_kis_us(self, broker, engine=None):
        """
        KIS 해외주식 당일 체결 내역과 캐시/DB 동기화.

        1) 누락 매수/매도 복구
        2) 청산 거래 PnL 보정 (zero-commission)
        절대 예외를 전파하지 않습니다.
        """
        try:
            if not hasattr(broker, "get_all_fills_for_date"):
                logger.debug("[TradeStorage-US] broker에 get_all_fills_for_date 없음, 동기화 건너뜀")
                return

            today = date.today()
            fills = await broker.get_all_fills_for_date(today)
            if not fills:
                logger.info("[TradeStorage-US] KIS 당일 체결 0건, 동기화 불필요")
                return

            # 체결을 종목별 매수/매도 그룹화
            buys = {}   # symbol → list of fills
            sells = {}  # symbol → list of fills
            for f in fills:
                side = f.get("sll_buy_dvsn_cd", "")
                sym = f.get("symbol", "")
                if not sym:
                    continue
                if side == "02":  # 매수
                    buys.setdefault(sym, []).append(f)
                elif side == "01":  # 매도
                    sells.setdefault(sym, []).append(f)

            synced = 0

            # DB에서 당일 US 이벤트 조회
            db_buy_symbols: set = set()
            db_sell_qty_by_symbol: Dict[str, int] = {}
            db_trades_map: Dict[str, Any] = {}

            if self._db_available and self.pool:
                try:
                    buy_rows = await self.pool.fetch(
                        "SELECT DISTINCT te.symbol FROM trade_events te "
                        "JOIN trades t ON te.trade_id = t.id "
                        "WHERE te.event_type='BUY' AND te.event_time::date=$1 AND t.market='US'",
                        today,
                    )
                    db_buy_symbols = {r['symbol'] for r in buy_rows}

                    sell_rows = await self.pool.fetch(
                        "SELECT te.symbol, COALESCE(SUM(te.quantity), 0) as total_qty "
                        "FROM trade_events te "
                        "JOIN trades t ON te.trade_id = t.id "
                        "WHERE te.event_type='SELL' AND te.event_time::date=$1 AND t.market='US' "
                        "GROUP BY te.symbol",
                        today,
                    )
                    db_sell_qty_by_symbol = {r['symbol']: int(r['total_qty']) for r in sell_rows}

                    trade_rows = await self.pool.fetch(
                        "SELECT id, symbol, name, entry_time, entry_price, entry_quantity, "
                        "exit_time, exit_price, exit_quantity, entry_strategy, entry_signal_score, "
                        "pnl, pnl_pct "
                        "FROM trades WHERE market='US' AND ("
                        "  entry_time::date = $1 OR (exit_time IS NULL AND entry_time::date < $1)"
                        ")",
                        today,
                    )
                    for tr in trade_rows:
                        rec = TradeRecord(
                            id=tr['id'], symbol=tr['symbol'], name=tr['name'] or '',
                            entry_time=tr['entry_time'],
                            entry_price=Decimal(str(tr['entry_price'])),
                            entry_quantity=tr['entry_quantity'],
                            entry_reason='', entry_strategy=tr['entry_strategy'] or '',
                            entry_signal_score=Decimal(str(tr['entry_signal_score'] or 0)),
                        )
                        if tr['exit_time']:
                            rec.exit_time = tr['exit_time']
                            rec.exit_price = Decimal(str(tr['exit_price'] or 0))
                            rec.exit_quantity = tr['exit_quantity'] or 0
                            rec.pnl = Decimal(str(tr['pnl'] or 0))
                            rec.pnl_pct = Decimal(str(tr['pnl_pct'] or 0))
                        db_trades_map[rec.id] = rec
                    if db_trades_map:
                        logger.debug(f"[TradeStorage-US] DB에서 당일 거래 {len(db_trades_map)}건 로드")
                except Exception as e:
                    logger.warning(f"[TradeStorage-US] DB 이벤트 조회 실패, 캐시 폴백: {e}")

            # 캐시의 당일 거래 (market 미분리 — symbol로만 비교)
            cache_trades = {t.symbol: t for t in self.get_today_trades()}

            # ── 누락 매수 복구 ──
            for sym, buy_fills in buys.items():
                if sym in db_buy_symbols or sym in cache_trades:
                    continue

                f = buy_fills[0]
                qty = int(f.get("tot_ccld_qty", 0))
                price = float(f.get("avg_prvs", 0))
                if qty <= 0 or price <= 0:
                    continue

                trade_id = f"KIS_SYNC_US_{sym}_{today.strftime('%Y%m%d')}"
                name = f.get("name", "") or sym

                strategy = "momentum"
                if engine:
                    pos = engine.portfolio.positions.get(sym)
                    if pos and getattr(pos, 'strategy', None):
                        s = pos.strategy
                        strategy = s.value if hasattr(s, 'value') else str(s)

                self.record_entry(
                    trade_id=trade_id,
                    symbol=sym,
                    name=name,
                    entry_price=price,
                    entry_quantity=qty,
                    entry_reason="KIS 동기화 복구",
                    entry_strategy=strategy,
                    market="US",
                )
                synced += 1
                logger.info(f"[TradeStorage-US] KIS 동기화 매수 복구: {sym} {qty}주 @ ${price:.4f}")

            # ── 누락 매도 복구 ──
            for sym, sell_fills in sells.items():
                kis_total_sold = sum(int(f.get("tot_ccld_qty", 0)) for f in sell_fills)

                db_total = db_sell_qty_by_symbol.get(sym, 0)
                cache_total = sum(
                    t.exit_quantity or 0
                    for t in self._journal._trades.values()
                    if t.symbol == sym and t.entry_time and t.entry_time.date() == today
                )
                already_sold = max(db_total, cache_total)

                if already_sold >= kis_total_sold:
                    logger.debug(
                        f"[TradeStorage-US] {sym} 매도 이미 기록됨 "
                        f"(KIS={kis_total_sold}, DB={db_total}, 캐시={cache_total})"
                    )
                    continue

                missing_qty = kis_total_sold - already_sold

                target_trade = self._find_recovery_target(sym, today, db_trades=db_trades_map)
                if not target_trade:
                    logger.warning(
                        f"[TradeStorage-US] {sym} 매도 복구 대상 trade 없음 (누락 {missing_qty}주)"
                    )
                    continue

                remaining = target_trade.entry_quantity - (target_trade.exit_quantity or 0)
                if missing_qty > remaining:
                    logger.warning(
                        f"[TradeStorage-US] {sym} 매도수량 클램핑: "
                        f"missing={missing_qty} > remaining={remaining}"
                    )
                    missing_qty = max(remaining, 0)
                    if missing_qty <= 0:
                        continue

                last_fill = sell_fills[-1]
                price = float(last_fill.get("avg_prvs", 0))
                if price <= 0:
                    continue

                actual_time = self._parse_kis_time(last_fill.get("ord_tmd", ""), today)

                result = self.record_exit(
                    trade_id=target_trade.id,
                    exit_price=price,
                    exit_quantity=missing_qty,
                    exit_reason="KIS 동기화 복구",
                    exit_type="kis_sync",
                    exit_time=actual_time,
                )

                # 캐시 손상 → DB 직접 기록
                if result is None and self._db_available:
                    exit_time = actual_time or datetime.now()
                    entry_price = float(target_trade.entry_price)
                    pnl, pnl_pct = self.calc_pnl_us(entry_price, price, missing_qty)
                    total_exit_qty = (target_trade.exit_quantity or 0) + missing_qty
                    is_fully_closed = total_exit_qty >= target_trade.entry_quantity

                    self._enqueue(
                        """UPDATE trades SET exit_time=$1, exit_price=$2, exit_quantity=$3,
                           exit_reason=$4, exit_type=$5, pnl=$6, pnl_pct=$7, updated_at=$8
                           WHERE id=$9""",
                        (exit_time, price, total_exit_qty, "KIS 동기화 복구", "kis_sync",
                         float(pnl), float(pnl_pct), datetime.now(), target_trade.id),
                    )
                    self._enqueue(
                        """INSERT INTO trade_events
                           (trade_id, symbol, name, event_type, event_time, price, quantity,
                            exit_type, exit_reason, pnl, pnl_pct, strategy, signal_score, status)
                           VALUES ($1,$2,$3,'SELL',$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)""",
                        (target_trade.id, target_trade.symbol, target_trade.name,
                         exit_time, price, missing_qty, "kis_sync", "KIS 동기화 복구",
                         float(pnl), float(pnl_pct), target_trade.entry_strategy,
                         float(target_trade.entry_signal_score),
                         "kis_sync" if is_fully_closed else "partial"),
                    )
                    logger.info(
                        f"[TradeStorage-US] DB 직접 기록: {sym} {missing_qty}주 pnl=${pnl:+.2f}"
                    )

                synced += 1
                logger.info(
                    f"[TradeStorage-US] KIS 동기화 매도 복구: {sym} {missing_qty}주 @ ${price:.4f} "
                    f"(trade={target_trade.id})"
                )

            if synced > 0:
                logger.info(f"[TradeStorage-US] KIS 동기화 완료: {synced}건 복구")
            else:
                logger.info("[TradeStorage-US] KIS 동기화 완료: 누락 없음")

            # PnL 보정 전 DB 큐 drain 대기
            if self._write_queue:
                await asyncio.sleep(0.5)
            await self._reconcile_pnl_us(today, sells)

        except Exception as e:
            logger.error(f"[TradeStorage-US] KIS 동기화 실패 (무시): {e}")

    async def _reconcile_pnl_us(self, target_date: date, kis_sells: Dict[str, list]):
        """
        US 당일 청산 거래의 PnL을 KIS 체결가 기준으로 보정 (zero-commission).
        """
        if not self._db_available or not self.pool:
            return

        try:
            rows = await self.pool.fetch("""
                SELECT id, symbol, entry_price, exit_price, entry_quantity,
                       exit_quantity, pnl, pnl_pct
                FROM trades
                WHERE market = 'US'
                  AND exit_time::date = $1
                  AND exit_time IS NOT NULL
            """, target_date)

            if not rows:
                return

            corrected = 0
            for row in rows:
                sym = row['symbol']
                entry_price = float(row['entry_price'])
                exit_qty = row['exit_quantity'] or 0

                if entry_price <= 0 or exit_qty <= 0:
                    continue

                # KIS 가중평균 매도가 계산
                kis_exit_price = float(row['exit_price'])
                if sym in kis_sells:
                    total_qty = 0
                    total_amt = 0.0
                    for f in kis_sells[sym]:
                        q = int(f.get("tot_ccld_qty", 0))
                        p = float(f.get("avg_prvs", 0))
                        total_qty += q
                        total_amt += q * p
                    if total_qty > 0:
                        kis_exit_price = total_amt / total_qty

                correct_pnl, correct_pct = self.calc_pnl_us(entry_price, kis_exit_price, exit_qty)
                old_pnl = float(row['pnl'] or 0)

                # $0.01 이하 차이 무시
                if abs(correct_pnl - old_pnl) < 0.01:
                    continue

                # 분할매도 체크 (trades 테이블 보정 건너뜀)
                sell_count = await self.pool.fetchval(
                    "SELECT COUNT(*) FROM trade_events "
                    "WHERE trade_id=$1 AND event_type='SELL' AND event_time::date=$2",
                    row['id'], target_date,
                )
                if sell_count and sell_count > 1:
                    logger.debug(f"[PnL보정-US] {sym} 분할매도 {sell_count}건 — 보정 건너뜀")
                    continue

                await self.pool.execute("""
                    UPDATE trades SET pnl=$1, pnl_pct=$2, exit_price=$3,
                                      updated_at=CURRENT_TIMESTAMP
                    WHERE id=$4
                """, correct_pnl, correct_pct, kis_exit_price, row['id'])

                await self.pool.execute("""
                    UPDATE trade_events SET pnl=$1, pnl_pct=$2, price=$3
                    WHERE trade_id=$4 AND event_type='SELL' AND event_time::date=$5
                """, correct_pnl, correct_pct, kis_exit_price, row['id'], target_date)

                cached = self._journal.get_trade(row['id'])
                if cached:
                    cached.pnl = Decimal(str(correct_pnl))
                    cached.pnl_pct = Decimal(str(correct_pct))

                corrected += 1
                logger.info(
                    f"[KIS보정-US] {sym} PnL 보정: ${old_pnl:+.2f} → ${correct_pnl:+.2f} "
                    f"(entry=${entry_price:.4f} exit=${kis_exit_price:.4f} qty={exit_qty})"
                )

            if corrected > 0:
                logger.info(f"[KIS보정-US] 당일 PnL 보정 완료: {corrected}건")

        except Exception as e:
            logger.error(f"[KIS보정-US] PnL 보정 실패 (무시): {e}")


# ── 싱글톤 팩토리 ──────────────────────────────────────────

_trade_storage: Optional[TradeStorage] = None


def get_trade_storage() -> TradeStorage:
    """TradeStorage 싱글톤 인스턴스 반환"""
    global _trade_storage
    if _trade_storage is None:
        _trade_storage = TradeStorage()
    return _trade_storage
