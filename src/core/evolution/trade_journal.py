"""
AI Trading Bot v2 - 거래 저널 (Trade Journal)

모든 거래를 구조화하여 기록하고, 복기 및 학습에 활용합니다.
"""

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Any

import asyncio
from loguru import logger


@dataclass
class TradeRecord:
    """거래 기록"""
    # 기본 정보
    id: str                              # 거래 고유 ID
    symbol: str                          # 종목코드
    name: str = ""                       # 종목명

    # 진입 정보
    entry_time: datetime = None          # 진입 시간
    entry_price: float = 0               # 진입가
    entry_quantity: int = 0              # 진입 수량
    entry_reason: str = ""               # 진입 사유 (요약 문자열)
    entry_strategy: str = ""             # 사용 전략 (의무)
    entry_signal_score: float = 0        # 진입 신호 점수
    entry_tags: List[str] = field(default_factory=list)  # 진입근거 태그 (3개 이상 의무)

    # 청산 정보
    exit_time: Optional[datetime] = None # 청산 시간
    exit_price: float = 0                # 청산가
    exit_quantity: int = 0               # 청산 수량
    exit_reason: str = ""                # 청산 사유
    exit_type: str = ""                  # 청산 유형 (take_profit, stop_loss, trailing, manual)

    # 결과
    pnl: float = 0                       # 손익 (원)
    pnl_pct: float = 0                   # 손익률 (%)
    holding_minutes: int = 0             # 보유 시간 (분)

    # 컨텍스트 (진화 학습용)
    market_context: Dict[str, Any] = field(default_factory=dict)  # 시장 상황
    indicators_at_entry: Dict[str, float] = field(default_factory=dict)  # 진입 시 지표
    indicators_at_exit: Dict[str, float] = field(default_factory=dict)   # 청산 시 지표
    theme_info: Dict[str, Any] = field(default_factory=dict)     # 테마 정보

    # 복기 메모 (LLM이 작성)
    review_notes: str = ""               # 복기 노트
    lesson_learned: str = ""             # 교훈
    improvement_suggestion: str = ""     # 개선 제안

    # 메타데이터
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    @property
    def is_win(self) -> bool:
        """승리 여부"""
        return self.pnl > 0

    @property
    def is_sync(self) -> bool:
        """동기화/복구 기반 포지션 여부 (전략 의사결정 없는 정합성 이벤트)"""
        _sync_exit_types = ("kis_sync", "sync_reconcile", "sync_closed", "sync_partial")
        return (
            self.entry_reason == "sync_detected"
            or self.id.startswith("SYNC_")
            or self.id.startswith("KIS_SYNC_")
            or (self.exit_type or "") in _sync_exit_types
        )

    @property
    def is_closed(self) -> bool:
        """청산 완료 여부 (전량 매도 시에만 True)"""
        if self.exit_time is None:
            return False
        return self.entry_quantity > 0 and (self.exit_quantity or 0) >= self.entry_quantity

    def to_dict(self) -> Dict:
        """딕셔너리로 변환 (JSON 저장용)"""
        from decimal import Decimal as _Decimal

        d = asdict(self)
        for key, value in d.items():
            if isinstance(value, datetime):
                d[key] = value.isoformat() if value else None
            elif isinstance(value, _Decimal):
                # fee_calculator 등 내부에서 Decimal이 필드에 들어올 수 있음
                d[key] = float(value)
        return d

    @classmethod
    def from_dict(cls, data: Dict) -> "TradeRecord":
        """딕셔너리에서 생성"""
        # datetime 파싱 (개별 필드 실패 시 None 처리)
        for key in ["entry_time", "exit_time", "created_at", "updated_at"]:
            if data.get(key) and isinstance(data[key], str):
                try:
                    data[key] = datetime.fromisoformat(data[key])
                except (ValueError, TypeError):
                    data[key] = None
        return cls(**data)


class TradeJournal:
    """
    거래 저널

    모든 거래를 기록하고 조회하는 저장소입니다.
    파일 기반으로 영구 저장되며, 복기 및 학습에 활용됩니다.
    """

    def __init__(self, storage_dir: str = None):
        self.storage_dir = Path(storage_dir or os.getenv(
            "TRADE_JOURNAL_DIR",
            os.path.expanduser("~/.cache/ai_trader/journal")
        ))
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        # 메모리 캐시
        self._trades: Dict[str, TradeRecord] = {}
        self._today_trades: List[str] = []

        # 로드
        self._load_recent_trades()

        logger.info(f"TradeJournal 초기화: {self.storage_dir}")

    def _get_file_path(self, trade_date: date) -> Path:
        """날짜별 저장 파일 경로"""
        return self.storage_dir / f"trades_{trade_date.strftime('%Y%m%d')}.json"

    def _load_recent_trades(self, days: int = 30):
        """최근 거래 로드"""
        today = date.today()

        for i in range(days):
            trade_date = today - timedelta(days=i)
            file_path = self._get_file_path(trade_date)

            if file_path.exists():
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)

                    for trade_dict in data.get("trades", []):
                        try:
                            trade = TradeRecord.from_dict(trade_dict)
                            self._trades[trade.id] = trade

                            if trade_date == today:
                                self._today_trades.append(trade.id)
                        except Exception as te:
                            logger.warning(f"거래 레코드 파싱 실패 (건너뜀): {te}")

                except json.JSONDecodeError as e:
                    logger.error(f"거래 파일 손상 ({file_path}): {e}")
                except Exception as e:
                    logger.warning(f"거래 로드 실패 ({file_path}): {e}")

        logger.info(f"거래 저널 로드: {len(self._trades)}건 (최근 {days}일)")

    def _recover_trade_from_db_sync(self, trade_id: str) -> Optional["TradeRecord"]:
        """DB(trades 테이블)에서 단일 거래 복원 (동기 래핑)

        record_exit()이 동기 메서드이므로 이벤트 루프가 있으면
        asyncio.to_thread로 블로킹 DB 호출을 수행합니다.
        """
        try:
            import asyncpg
            db_url = os.getenv("DATABASE_URL")
            if not db_url:
                return None

            loop = None
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

            if loop and loop.is_running():
                # 이미 이벤트 루프 안 → 새 스레드에서 실행
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(self._fetch_trade_from_db, db_url, trade_id)
                    return future.result(timeout=10)
            else:
                return asyncio.run(self._async_fetch_trade(db_url, trade_id))
        except Exception as e:
            logger.warning(f"[저널] DB 복원 실패 (trade_id={trade_id}): {e}")
            return None

    def _fetch_trade_from_db(self, db_url: str, trade_id: str) -> Optional["TradeRecord"]:
        """별도 스레드에서 DB 조회 (새 이벤트루프 생성)"""
        return asyncio.run(self._async_fetch_trade(db_url, trade_id))

    async def _async_fetch_trade(self, db_url: str, trade_id: str) -> Optional["TradeRecord"]:
        """단일 거래 DB 조회"""
        import asyncpg
        conn = await asyncpg.connect(db_url)
        try:
            row = await conn.fetchrow(
                """SELECT id, symbol, name, entry_time, entry_price, entry_quantity,
                          entry_reason, entry_strategy, entry_signal_score,
                          exit_time, exit_price, exit_quantity, exit_reason, exit_type,
                          pnl, pnl_pct, holding_minutes
                   FROM trades WHERE id = $1""",
                trade_id,
            )
            if not row:
                return None
            return self._row_to_trade_record(row)
        finally:
            await conn.close()

    @staticmethod
    def _row_to_trade_record(row) -> "TradeRecord":
        """DB 행 → TradeRecord 변환"""
        return TradeRecord(
            id=row["id"],
            symbol=row["symbol"],
            name=row["name"] or "",
            entry_time=row["entry_time"],
            entry_price=float(row["entry_price"]),
            entry_quantity=int(row["entry_quantity"]),
            entry_reason=row["entry_reason"] or "",
            entry_strategy=row["entry_strategy"] or "",
            entry_signal_score=float(row["entry_signal_score"] or 0),
            exit_time=row["exit_time"],
            exit_price=float(row["exit_price"] or 0),
            exit_quantity=int(row["exit_quantity"] or 0),
            exit_reason=row["exit_reason"] or "",
            exit_type=row["exit_type"] or "",
            pnl=float(row["pnl"] or 0),
            pnl_pct=float(row["pnl_pct"] or 0),
            holding_minutes=int(row["holding_minutes"] or 0),
        )

    async def sync_from_db(self, days: int = 7):
        """DB의 trades 테이블에서 JSON에 누락된 거래를 보강

        strategy_evolver.rebalance_strategy_allocation() 호출 전에 실행하여
        메모리 dict(_trades)를 DB 기준으로 보강합니다.
        """
        import asyncpg
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            logger.debug("[저널] DATABASE_URL 미설정 → DB 동기화 스킵")
            return

        pool = None
        try:
            pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
            cutoff = datetime.now() - timedelta(days=days)

            # 청산 완료된 거래만 조회 (exit_time IS NOT NULL)
            rows = await pool.fetch(
                """SELECT id, symbol, name, entry_time, entry_price, entry_quantity,
                          entry_reason, entry_strategy, entry_signal_score,
                          exit_time, exit_price, exit_quantity, exit_reason, exit_type,
                          pnl, pnl_pct, holding_minutes
                   FROM trades
                   WHERE entry_time >= $1 AND exit_time IS NOT NULL
                   ORDER BY entry_time""",
                cutoff,
            )

            synced = 0
            for row in rows:
                tid = row["id"]

                if tid in self._trades:
                    existing = self._trades[tid]
                    # exit 정보가 없는 레코드 보강 (부분 누락)
                    if not existing.is_closed and row["pnl"] is not None:
                        existing.exit_time = row["exit_time"]
                        existing.exit_price = float(row["exit_price"] or 0)
                        existing.exit_quantity = int(row["exit_quantity"] or 0)
                        existing.pnl = float(row["pnl"] or 0)
                        existing.pnl_pct = float(row["pnl_pct"] or 0)
                        existing.exit_type = row["exit_type"] or ""
                        existing.exit_reason = row["exit_reason"] or ""
                        existing.holding_minutes = int(row["holding_minutes"] or 0)
                        synced += 1
                    continue

                # _trades에 없으면 새로 생성
                trade = self._row_to_trade_record(row)
                self._trades[tid] = trade
                synced += 1

            if synced > 0:
                logger.info(f"[저널] DB 동기화: {synced}건 보강 (JSON 누락분)")
            else:
                logger.debug(f"[저널] DB 동기화: 보강 대상 없음 ({len(rows)}건 조회)")

        except Exception as e:
            logger.warning(f"[저널] DB 동기화 실패: {e}")
        finally:
            if pool:
                await pool.close()

    def _save_trades(self, trade_date: date):
        """해당 날짜 거래 저장"""
        file_path = self._get_file_path(trade_date)

        # 해당 날짜 거래 필터
        trades = [
            t for t in self._trades.values()
            if t.entry_time and t.entry_time.date() == trade_date
        ]

        data = {
            "date": trade_date.isoformat(),
            "count": len(trades),
            "trades": [t.to_dict() for t in trades],
            "updated_at": datetime.now().isoformat(),
        }

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[거래저널] 저장 실패: {file_path} — {e}")

        logger.debug(f"거래 저장: {file_path} ({len(trades)}건)")

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
        entry_tags: List[str] = None,
        market: str = "KR",
    ) -> TradeRecord:
        """
        진입 기록

        매수 체결 시 호출합니다.

        의무 규칙:
        - entry_strategy: 반드시 실제 전략명 (unknown/empty 불가 → 경고 후 fallback)
        - entry_tags: 3개 이상 진입근거 (미달 시 경고 후 저장은 허용)
        """
        now = datetime.now()

        # ── 전략 태그 의무 검증 ─────────────────────────────────
        _strategy = entry_strategy or ""
        if not _strategy or _strategy in ("unknown", ""):
            logger.warning(
                f"[저널] {symbol} 전략 태그 누락 (entry_strategy='{_strategy}') "
                f"→ 'unclassified'로 기록. 진입 근거 재확인 필요."
            )
            _strategy = "unclassified"

        # ── 진입근거 3항목 의무 검증 ──────────────────────────────
        _tags: List[str] = list(entry_tags or [])
        if len(_tags) < 3:
            logger.warning(
                f"[저널] {symbol} 진입근거 {len(_tags)}개 (최소 3개 필요) "
                f"→ tags={_tags}. 시그널 메타데이터 확인 필요."
            )

        trade = TradeRecord(
            id=trade_id,
            symbol=symbol,
            name=name,
            entry_time=now,
            entry_price=entry_price,
            entry_quantity=entry_quantity,
            entry_reason=entry_reason,
            entry_strategy=_strategy,
            entry_signal_score=signal_score,
            entry_tags=_tags,
            indicators_at_entry=indicators or {},
            market_context=market_context or {},
            theme_info=theme_info or {},
        )

        self._trades[trade_id] = trade

        if now.date() == date.today() and trade_id not in self._today_trades:
            self._today_trades.append(trade_id)

        # 저장
        self._save_trades(now.date())

        logger.info(
            f"[저널] 진입 기록: {symbol} {name} "
            f"{entry_quantity}주 @ {entry_price:,.0f}원 ({entry_strategy})"
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
        # 폴백용 optional 파라미터 (메모리에 trade가 없을 때 최소 레코드 생성)
        symbol: str = None,
        name: str = None,
        entry_price: float = None,
        entry_strategy: str = None,
    ) -> Optional[TradeRecord]:
        """
        청산 기록

        매도 체결 시 호출합니다.
        avg_entry_price: 포트폴리오 평균단가 (KIS 일치용). None이면 개별 trade.entry_price 사용.

        폴백: trade_id가 메모리에 없을 때, symbol 등 optional 파라미터가 있으면
        최소 TradeRecord를 생성하여 청산 기록을 보존합니다.
        """
        trade = self._trades.get(trade_id)
        if not trade:
            # DB에서 복원 시도
            trade = self._recover_trade_from_db_sync(trade_id)
            if trade:
                self._trades[trade_id] = trade
                logger.info(f"[저널] 거래 ID {trade_id} DB에서 복원 성공")
            elif symbol:
                # 최소 레코드 생성 (폴백)
                _ep = entry_price or avg_entry_price or exit_price
                trade = TradeRecord(
                    id=trade_id,
                    symbol=symbol,
                    name=name or "",
                    entry_time=datetime.now(),  # 정확한 시간 불명 → 현재 시간
                    entry_price=_ep,
                    entry_quantity=exit_quantity,
                    entry_reason="recovered_at_exit",
                    entry_strategy=entry_strategy or "unknown",
                )
                self._trades[trade_id] = trade
                logger.warning(
                    f"[저널] 거래 ID {trade_id} 메모리 미존재 → 최소 레코드 생성 "
                    f"(symbol={symbol}, entry_price={_ep})"
                )
            else:
                logger.warning(f"[저널] 거래 ID 없음: {trade_id} (복원 실패, symbol 미제공)")
                return None

        now = exit_time or datetime.now()

        # 청산 정보 업데이트
        trade.exit_time = now
        trade.exit_price = exit_price
        trade.exit_quantity = (trade.exit_quantity or 0) + exit_quantity  # 누적
        trade.exit_reason = exit_reason
        trade.exit_type = exit_type
        trade.indicators_at_exit = indicators or {}

        # 손익 계산 (수수료 포함, 누적: 부분 매도 시 += 방식)
        # 포트폴리오 평균단가 우선 사용 (KIS와 일치)
        entry_price_for_pnl = avg_entry_price or trade.entry_price
        if entry_price_for_pnl > 0:
            from ...utils.fee_calculator import calculate_net_pnl
            partial_pnl, _ = calculate_net_pnl(entry_price_for_pnl, exit_price, exit_quantity)
            trade.pnl += partial_pnl  # += 누적 (수수료 포함)
            # 총 투자원금 기준 손익률
            invested = entry_price_for_pnl * trade.entry_quantity
            trade.pnl_pct = float(trade.pnl / invested * 100) if invested > 0 else 0.0

        # 보유 시간 계산
        if trade.entry_time:
            delta = now - trade.entry_time
            trade.holding_minutes = int(delta.total_seconds() / 60)

        trade.updated_at = now

        # 오늘 청산된 거래를 _today_trades에 추가 (어제 진입→오늘 청산 케이스 포함)
        if now.date() == date.today() and trade_id not in self._today_trades:
            self._today_trades.append(trade_id)

        # 저장
        self._save_trades(trade.entry_time.date())

        emoji = "+" if trade.pnl > 0 else ""
        logger.info(
            f"[저널] 청산 기록: {trade.symbol} {trade.name} "
            f"{exit_quantity}주 @ {exit_price:,.0f}원 "
            f"({exit_type}) -> {emoji}{trade.pnl:,.0f}원 ({trade.pnl_pct:+.1f}%)"
        )

        return trade

    def get_trade(self, trade_id: str) -> Optional[TradeRecord]:
        """거래 조회"""
        return self._trades.get(trade_id)

    def get_today_trades(self) -> List[TradeRecord]:
        """오늘 거래 목록"""
        return [self._trades[tid] for tid in self._today_trades if tid in self._trades]

    def get_trades_by_date(self, trade_date: date) -> List[TradeRecord]:
        """날짜별 거래 목록"""
        return [
            t for t in self._trades.values()
            if t.entry_time and t.entry_time.date() == trade_date
        ]

    def get_trades_by_strategy(self, strategy: str, days: int = 30) -> List[TradeRecord]:
        """전략별 거래 목록"""
        cutoff = datetime.now() - timedelta(days=days)
        return [
            t for t in self._trades.values()
            if t.entry_strategy == strategy and t.entry_time and t.entry_time > cutoff
        ]

    def get_closed_trades(self, days: int = 30) -> List[TradeRecord]:
        """청산된 거래 목록"""
        cutoff = datetime.now() - timedelta(days=days)
        return [
            t for t in self._trades.values()
            if t.is_closed and t.entry_time and t.entry_time > cutoff
        ]

    def get_open_trades(self) -> List[TradeRecord]:
        """미청산 거래 목록"""
        return [t for t in self._trades.values() if not t.is_closed]

    def get_recent_trades(self, days: int = 7) -> List[TradeRecord]:
        """최근 N일 거래 목록 (청산/미청산 모두)"""
        cutoff = datetime.now() - timedelta(days=days)
        return [
            t for t in self._trades.values()
            if t.entry_time and t.entry_time > cutoff
        ]

    def update_review(
        self,
        trade_id: str,
        review_notes: str = "",
        lesson_learned: str = "",
        improvement_suggestion: str = "",
    ):
        """복기 노트 업데이트 (LLM 분석 결과)"""
        trade = self._trades.get(trade_id)
        if not trade:
            return

        trade.review_notes = review_notes
        trade.lesson_learned = lesson_learned
        trade.improvement_suggestion = improvement_suggestion
        trade.updated_at = datetime.now()

        # 저장
        if trade.entry_time:
            self._save_trades(trade.entry_time.date())

    def get_statistics(self, days: int = 30, exclude_sync: bool = True) -> Dict[str, Any]:
        """거래 통계 (exclude_sync=True: 동기화 포지션 제외)"""
        trades = self.get_closed_trades(days)
        if exclude_sync:
            trades = [t for t in trades if not t.is_sync]

        if not trades:
            return {
                "total_trades": 0,
                "win_rate": 0,
                "avg_pnl_pct": 0,
                "total_pnl": 0,
                "avg_holding_minutes": 0,
                "best_trade": None,
                "worst_trade": None,
            }

        wins = [t for t in trades if t.is_win]
        losses = [t for t in trades if not t.is_win]

        total_pnl = sum(float(t.pnl) for t in trades)
        avg_pnl_pct = sum(float(t.pnl_pct) for t in trades) / len(trades)
        avg_holding = sum(t.holding_minutes for t in trades) / len(trades)

        best = max(trades, key=lambda t: t.pnl_pct) if trades else None
        worst = min(trades, key=lambda t: t.pnl_pct) if trades else None

        return {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(trades) * 100 if trades else 0,
            "avg_pnl_pct": avg_pnl_pct,
            "total_pnl": total_pnl,
            "avg_holding_minutes": avg_holding,
            "best_trade": best.to_dict() if best else None,
            "worst_trade": worst.to_dict() if worst else None,
            "by_strategy": self._get_stats_by_strategy(trades),
            "by_exit_type": self._get_stats_by_exit_type(trades),
        }

    def _get_stats_by_strategy(self, trades: List[TradeRecord]) -> Dict[str, Dict]:
        """전략별 통계"""
        stats = {}

        for trade in trades:
            strategy = trade.entry_strategy or "unknown"
            if strategy not in stats:
                stats[strategy] = {"trades": 0, "wins": 0, "total_pnl": 0}

            stats[strategy]["trades"] += 1
            if trade.is_win:
                stats[strategy]["wins"] += 1
            stats[strategy]["total_pnl"] += float(trade.pnl)

        # 승률 계산
        for strategy, s in stats.items():
            s["win_rate"] = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0

        return stats

    def _get_stats_by_exit_type(self, trades: List[TradeRecord]) -> Dict[str, Dict]:
        """청산 유형별 통계"""
        stats = {}

        for trade in trades:
            exit_type = trade.exit_type or "unknown"
            if exit_type not in stats:
                stats[exit_type] = {"trades": 0, "wins": 0, "avg_pnl_pct": 0, "total_pnl_pct": 0}

            stats[exit_type]["trades"] += 1
            if trade.is_win:
                stats[exit_type]["wins"] += 1
            stats[exit_type]["total_pnl_pct"] += float(trade.pnl_pct)

        # 평균 계산
        for exit_type, s in stats.items():
            s["avg_pnl_pct"] = s["total_pnl_pct"] / s["trades"] if s["trades"] > 0 else 0
            del s["total_pnl_pct"]

        return stats


# 싱글톤 인스턴스
_trade_journal: Optional[TradeJournal] = None


def get_trade_journal() -> TradeJournal:
    """TradeJournal 인스턴스 반환 (TradeStorage 우선, 실패 시 JSON 폴백)"""
    global _trade_journal
    if _trade_journal is None:
        try:
            from ...data.storage.trade_storage import get_trade_storage
            _trade_journal = get_trade_storage()
            logger.info("[거래저널] TradeStorage(DB+JSON) 모드 초기화")
        except Exception as e:
            logger.warning(f"[거래저널] TradeStorage 초기화 실패, JSON 폴백: {e}")
            _trade_journal = TradeJournal()
    return _trade_journal
