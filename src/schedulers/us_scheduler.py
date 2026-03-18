"""
QWQ AI Trader - US 시장 스케줄러

ai-trader-us의 LiveEngine에서 추출한 백그라운드 태스크 모듈.
USScheduler는 _USEngineBundle 인스턴스를 파라미터로 받아
모든 스케줄러 태스크를 독립 관리합니다.

사용법:
    from src.schedulers.us_scheduler import USScheduler
    scheduler = USScheduler(us_engine)
    tasks = scheduler.create_tasks()
"""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from collections import deque
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from loguru import logger

from src.core.types import (
    Portfolio, Position, Signal, TradeResult, OrderSide,
    StrategyType, TimeHorizon, PositionSide, MarketSession,
)
from src.execution.broker.kis_us import EXCHANGE_MAP
from src.indicators.technical import compute_indicators
from src.utils.telegram import send_alert


class USScheduler:
    """US 시장 백그라운드 스케줄러

    ai-trader-us LiveEngine의 백그라운드 태스크를 독립 클래스로 추출.
    engine 인스턴스(_USEngineBundle)의 모든 속성에 접근합니다.

    태스크:
    1. screening_loop (15분) — 유니버스 스캔 → 전략 시그널 → 주문
    2. exit_check_loop (15초) — 보유 포지션 청산 체크 [KIS REST 실시간 기준]
    3. portfolio_sync_loop (30초) — KIS 잔고 ↔ 로컬 Portfolio 동기화
    4. order_check_loop (10초) — 미체결 주문 상태 폴링
    5. eod_close_loop (30초) — 마감 15분 전 DAY 포지션 청산
    6. heartbeat_loop (5분) — 상태 로깅
    7. screener_loop (60분) — S&P500+400 전종목 점수 계산 (pool 갱신)
    8. watchlist_loop (5분) — 상위 25 + 보유 종목 Finviz 실시간 모니터링
    9. volume_surge_loop (15분) — KIS 거래량급증 API
    10. theme_detection_loop (30분) — US 테마 탐지
    """

    def __init__(self, engine):
        """
        Args:
            engine: _USEngineBundle 인스턴스
        """
        self.engine = engine
        # 매도 실패 쿨다운 (종목 → 실패시각)
        self._sell_fail_cooldown: Dict[str, datetime] = {}
        # 현재가 조회 실패 카운터 (3회 이상 → 세션 내 스킵)
        self._quote_fail_count: Dict[str, int] = {}
        # 이전 sync 수량 스냅샷 (수량 변화 감지용)
        self._prev_qty_snapshot: Dict[str, int] = {}
        # exit 체크 per-symbol 락 (WS 콜백 + REST 동시 실행 방지)
        self._exit_locks: Dict[str, asyncio.Lock] = {}

    # ============================================================
    # 태스크 생성
    # ============================================================

    def create_tasks(self) -> List[asyncio.Task]:
        """모든 US 스케줄러 태스크 생성 → 리스트 반환"""
        eng = self.engine
        tasks = []

        tasks.append(asyncio.create_task(self.screening_loop(), name="us_screening"))
        tasks.append(asyncio.create_task(self.exit_check_loop(), name="us_exit_check"))
        tasks.append(asyncio.create_task(self.portfolio_sync_loop(), name="us_portfolio_sync"))
        tasks.append(asyncio.create_task(self.order_check_loop(), name="us_order_check"))
        tasks.append(asyncio.create_task(self.eod_close_loop(), name="us_eod_close"))
        tasks.append(asyncio.create_task(self.heartbeat_loop(), name="us_heartbeat"))

        # 테마 탐지
        if eng.theme_detector:
            tasks.append(asyncio.create_task(self.theme_detection_loop(), name="us_theme_detect"))

        # 스크리너
        tasks.append(asyncio.create_task(self.screener_loop(), name="us_screener"))

        # 워치리스트
        tasks.append(asyncio.create_task(self.watchlist_loop(), name="us_watchlist"))

        # KIS 해외주식 실시간가 WS (HDFSCNT0)
        # ws_market_loop가 장 시작 전 사전 연결 + 포지션 구독 + 체결통보(H0GSCNI0) 통합 관리
        # KIS approval_key 1개 제약: 단일 WS에서 가격+체결통보 동시 처리
        if eng.us_price_ws:
            eng.us_price_ws.on_price(self._on_us_ws_price)
            tasks.append(asyncio.create_task(self.ws_market_loop(), name="us_ws_market"))
        elif eng.kis_ws:
            # us_price_ws가 없을 때만 별도 체결통보 WS 사용 (폴백)
            tasks.append(asyncio.create_task(eng.kis_ws.start(), name="us_kis_ws"))

        # 거래량급증 루프
        if hasattr(eng.broker, "get_volume_surge"):
            tasks.append(asyncio.create_task(self.volume_surge_loop(), name="us_vol_surge"))

        return tasks

    # ============================================================
    # 헬퍼: Finviz 프로바이더 접근
    # ============================================================

    @property
    def _finviz(self):
        """Finviz 프로바이더 접근 (screener._finviz 경유)"""
        if self.engine.screener and hasattr(self.engine.screener, '_finviz'):
            return self.engine.screener._finviz
        return None

    @property
    def _finviz_ready(self) -> bool:
        """Finviz 프로바이더 준비 여부"""
        fz = self._finviz
        return fz is not None and fz.is_ready

    # ============================================================
    # 헬퍼: highest_price 영속화
    # ============================================================

    @staticmethod
    def _hp_cache_path() -> Path:
        p = Path.home() / ".cache" / "ai_trader_us"
        p.mkdir(parents=True, exist_ok=True)
        return p / "highest_prices.json"

    def _load_highest_prices(self) -> dict:
        """캐시에서 highest_price 로드 {symbol: float}"""
        try:
            path = self._hp_cache_path()
            if path.exists():
                raw = json.loads(path.read_text())
                if isinstance(raw, dict) and "highest_prices" in raw:
                    return raw.get("highest_prices", {})
                return raw
        except Exception as e:
            logger.debug(f"[US 캐시] highest_prices 로드 실패: {e}")
        return {}

    def _load_exit_stages(self) -> dict:
        """캐시에서 exit_stages 로드"""
        try:
            path = self._hp_cache_path()
            if path.exists():
                raw = json.loads(path.read_text())
                if isinstance(raw, dict) and "exit_stages" in raw:
                    return raw.get("exit_stages", {})
        except Exception as e:
            logger.debug(f"[US 캐시] exit_stages 로드 실패: {e}")
        return {}

    def _load_cached_metadata(self) -> tuple:
        """캐시에서 entry_times, strategies 로드"""
        entry_times = {}
        strategies = {}
        try:
            path = self._hp_cache_path()
            if path.exists():
                raw = json.loads(path.read_text())
                if isinstance(raw, dict):
                    entry_times = raw.get("entry_times", {})
                    strategies = raw.get("strategies", {})
        except Exception as e:
            logger.debug(f"[US 캐시] metadata 로드 실패: {e}")
        return entry_times, strategies

    def _save_highest_prices(self):
        """현재 포지션의 highest_price + exit_stages + entry_times + strategies → 캐시 저장"""
        eng = self.engine
        try:
            hp = {
                sym: float(pos.highest_price)
                for sym, pos in eng.portfolio.positions.items()
                if pos.highest_price is not None
            }
            entry_times = {
                sym: pos.entry_time.isoformat()
                for sym, pos in eng.portfolio.positions.items()
                if pos.entry_time is not None
            }
            strategies = {
                sym: pos.strategy
                for sym, pos in eng.portfolio.positions.items()
                if pos.strategy
            }
            data = {
                "highest_prices": hp,
                "exit_stages": eng.exit_manager.get_stages(),
                "entry_times": entry_times,
                "strategies": strategies,
            }
            self._hp_cache_path().write_text(json.dumps(data))
        except Exception as e:
            logger.debug(f"[US 동기화] 상태 캐시 저장 실패: {e}")

    async def _reconcile_ghost_us_trades(self, kis_symbols: set, kis_positions: list = None):
        """봇 재시작 시 portfolio에 없고 KIS에도 없는 미결 US 거래 자동 청산 기록.

        원인: 포지션이 KIS에서 매도됐지만 재시작으로 portfolio가 초기화돼
        sync_closed 루프가 거치지 않는 경우.

        kis_positions: KIS 실제 포지션 리스트 (수량 불일치 처리에 사용)
        """
        eng = self.engine
        ts = getattr(eng, 'trade_storage', None)
        if not ts or not ts._db_available or not ts.pool:
            return
        try:
            today = datetime.now().date()
            rows = await ts.pool.fetch(
                """SELECT id, symbol, name, entry_price, entry_quantity,
                          entry_strategy
                   FROM trades
                   WHERE market='US' AND exit_time IS NULL
                     AND entry_time::date >= (CURRENT_DATE - INTERVAL '7 days')""",
            )
            if not rows:
                return

            portfolio_symbols = set(eng.portfolio.positions.keys())
            pending_symbols   = set(eng._pending_symbols)

            for row in rows:
                sym = row['symbol']
                # KIS에 있거나, portfolio에 있거나, pending이면 건너뜀
                if sym in kis_symbols or sym in portfolio_symbols or sym in pending_symbols:
                    continue

                trade_id = row['id']
                entry_price = float(row['entry_price'] if row['entry_price'] is not None else 0)

                # KIS 현재가 조회 시도 (없으면 entry_price 사용)
                exit_price = entry_price
                try:
                    q = await eng.broker.get_quote(sym)
                    if q and q.get("price", 0) > 0:
                        exit_price = float(q["price"])
                except Exception as e:
                    logger.debug(f"[US 동기화] {sym} 현재가 조회 실패: {e}")

                qty = row['entry_quantity'] if row['entry_quantity'] is not None else 1
                pnl_est = round((exit_price - entry_price) * qty, 2)
                pnl_pct_est = ((exit_price - entry_price) / entry_price * 100) if entry_price > 0 else 0

                # exit_type 추론 (sync_reconcile 대신 실제 이유 근사)
                if pnl_pct_est <= -3.5:
                    inferred_type = "stop_loss"
                    inferred_reason = f"sync_inferred: stop_loss ({pnl_pct_est:.1f}%)"
                elif pnl_pct_est >= 5.0:
                    inferred_type = "trailing"
                    inferred_reason = f"sync_inferred: trailing ({pnl_pct_est:.1f}%)"
                elif pnl_pct_est >= 2.5:
                    inferred_type = "take_profit"
                    inferred_reason = f"sync_inferred: take_profit ({pnl_pct_est:.1f}%)"
                else:
                    inferred_type = "sync_reconcile"
                    inferred_reason = f"sync_reconcile (재시작 복구, {pnl_pct_est:.1f}%)"

                result = ts.record_exit(
                    trade_id=trade_id,
                    exit_price=exit_price,
                    exit_quantity=qty,
                    exit_reason=inferred_reason,
                    exit_type=inferred_type,
                    exit_time=datetime.now(),
                    avg_entry_price=entry_price,
                )
                if result is None:
                    # journal 캐시 없는 경우 DB 직접 기록
                    ts._enqueue(
                        """UPDATE trades SET exit_time=$1, exit_price=$2, exit_quantity=$3,
                           exit_reason=$4, exit_type=$5, pnl=$6, pnl_pct=$7, updated_at=$8
                           WHERE id=$9""",
                        (datetime.now(), exit_price, qty,
                         inferred_reason, inferred_type,
                         round(pnl_est, 2), round(pnl_pct_est, 2), datetime.now(), trade_id),
                    )
                    ts._enqueue(
                        """INSERT INTO trade_events
                           (trade_id, symbol, name, event_type, event_time, price, quantity,
                            exit_type, exit_reason, pnl, pnl_pct, strategy, signal_score, status)
                           VALUES ($1,$2,$3,'SELL',$4,$5,$6,$7,$8,$9,$10,$11,0,$12)
                           ON CONFLICT DO NOTHING""",
                        (trade_id, sym, row['name'] or sym,
                         datetime.now(), exit_price, qty,
                         inferred_type, inferred_reason,
                         round(pnl_est, 2), round(pnl_pct_est, 2),
                         row['entry_strategy'] or '', inferred_type),
                    )
                logger.info(
                    f"[US Reconcile] {sym} ghost 거래 청산 기록: "
                    f"trade={trade_id} exit=${exit_price:.2f}"
                )

            # ── 2. 수량 불일치 처리: KIS qty < DB total qty (재시작 누적 SYNC_ 레코드 정리) ──
            # 예: ADEA 3개 레코드(1+1+4=6주) → KIS 1주 → 오래된 레코드 순서로 close
            if kis_positions:
                kis_qty_map = {kp["symbol"]: int(kp["qty"]) for kp in kis_positions}
                # symbol별 DB 오픈 레코드 그룹화
                sym_db_map: dict = {}
                for row in rows:
                    sym = row['symbol']
                    if sym not in kis_symbols:
                        continue  # 이미 1번(ghost)에서 처리됨
                    sym_db_map.setdefault(sym, []).append(row)

                for sym, sym_rows in sym_db_map.items():
                    if len(sym_rows) <= 1:
                        continue  # 레코드 1개면 else branch에서 정상 처리
                    db_total = sum(int(r['entry_quantity'] or 0) for r in sym_rows)
                    kis_qty = kis_qty_map.get(sym, 0)
                    if db_total <= kis_qty:
                        continue  # 수량 일치 또는 DB가 더 적으면 스킵

                    # 오래된 레코드부터 close (가장 최근 레코드는 else branch에서 관리)
                    sym_rows_sorted = sorted(sym_rows, key=lambda r: r['entry_time'])
                    remaining_to_close = db_total - kis_qty
                    _now = datetime.now()
                    for row in sym_rows_sorted:
                        if remaining_to_close <= 0:
                            break
                        if row['symbol'] in portfolio_symbols:
                            continue  # 현재 엔진이 추적 중인 레코드는 건드리지 않음
                        trade_id = row['id']
                        entry_price = float(row['entry_price'] or 0)
                        row_qty = int(row['entry_quantity'] or 0)
                        close_qty = min(row_qty, remaining_to_close)
                        # 현재가 조회 시도
                        _exit_price = entry_price
                        try:
                            q = await eng.broker.get_quote(sym)
                            if q and q.get("price", 0) > 0:
                                _exit_price = float(q["price"])
                        except Exception:
                            pass
                        _pnl = round((_exit_price - entry_price) * close_qty, 2)
                        _pct = round((_exit_price - entry_price) / entry_price * 100, 2) if entry_price > 0 else 0
                        ts._enqueue(
                            """UPDATE trades SET exit_time=$1, exit_price=$2, exit_quantity=$3,
                               exit_reason=$4, exit_type=$5, pnl=$6, pnl_pct=$7, updated_at=$8
                               WHERE id=$9 AND exit_time IS NULL""",
                            (_now, _exit_price, close_qty,
                             "sync_qty_reconcile (중복 SYNC_ 레코드 정리)", "sync_reconcile",
                             _pnl, _pct, _now, trade_id),
                        )
                        remaining_to_close -= close_qty
                        logger.info(
                            f"[US Reconcile] {sym} 수량 초과 레코드 정리: "
                            f"trade={trade_id} close={close_qty}주 (db_total={db_total}, kis={kis_qty})"
                        )

        except Exception as e:
            logger.warning(f"[US Reconcile] ghost 거래 reconcile 오류: {e}")

    async def _preload_strategies_from_db(self):
        """재시작 시 DB의 미결 US 거래에서 전략명을 _symbol_strategy에 복원.

        최우선 소스: DB trades.entry_strategy (실제 주문 전략명)
        SYNC 포지션이 strat_cache 없어도 전략 표시 가능.
        """
        eng = self.engine
        ts = getattr(eng, 'trade_storage', None)
        pool = getattr(ts, 'pool', None) if ts else None
        if not pool:
            return
        try:
            rows = await pool.fetch(
                """SELECT DISTINCT ON (symbol) symbol, entry_strategy
                   FROM trades
                   WHERE market = 'US' AND exit_time IS NULL
                     AND entry_strategy IS NOT NULL AND entry_strategy != '' AND entry_strategy != 'unknown'
                   ORDER BY symbol, entry_time DESC""",
            )
            loaded = 0
            for row in rows:
                sym  = row['symbol']
                strat = row['entry_strategy']
                # _symbol_strategy에 없거나 비어있을 때만 덮어쓰기
                if not eng._symbol_strategy.get(sym):
                    eng._symbol_strategy[sym] = strat
                    # 이미 포지션이 portfolio에 있으면 즉시 반영
                    pos = eng.portfolio.positions.get(sym)
                    if pos and not pos.strategy:
                        pos.strategy = strat
                        for strat_obj in eng.strategies:
                            if strat_obj.strategy_type.value == strat:
                                pos.time_horizon = strat_obj.time_horizon
                                break
                    loaded += 1
            if loaded:
                logger.info(f"[US 전략 복원] DB에서 {loaded}개 종목 전략 로드 완료")
        except Exception as e:
            logger.warning(f"[US 전략 복원] DB 조회 실패: {e}")

    # ============================================================
    # 헬퍼: 히스토리/ATR/거래소
    # ============================================================

    async def _get_history(self, symbol: str):
        """종목 히스토리 로드 (캐시 → yfinance, 동기 IO는 to_thread로 래핑)"""
        eng = self.engine
        if not eng.data_store or not eng.data_provider:
            return None

        cached = eng.data_store.load(symbol)
        today = eng.session.now_et().date()

        if cached is not None and not cached.empty:
            last_date = cached.index[-1]
            if hasattr(last_date, 'date'):
                last_date = last_date.date()

            if last_date >= today - timedelta(days=1):
                return cached

            try:
                new_data = await asyncio.to_thread(
                    eng.data_provider.get_daily_bars,
                    symbol,
                    last_date + timedelta(days=1),
                    today,
                )
                if not new_data.empty:
                    eng.data_store.update(symbol, new_data)
                    return eng.data_store.load(symbol)
            except Exception as e:
                logger.debug(f"[US 히스토리] {symbol} 증분 갱신 실패: {e}")

            return cached

        # 전체 다운로드 (500일)
        try:
            start = today - timedelta(days=500)
            df = await asyncio.to_thread(
                eng.data_provider.get_daily_bars, symbol, start, today,
            )
            if not df.empty:
                eng.data_store.save(symbol, df)
                return df
        except Exception as e:
            logger.debug(f"[US 히스토리] {symbol} 다운로드 실패: {e}")

        return None

    async def _get_atr(self, symbol: str) -> Optional[float]:
        """ATR 조회 (캐시된 히스토리 + 인디케이터 캐시)"""
        eng = self.engine
        if symbol in eng._indicator_cache:
            return eng._indicator_cache[symbol].get("atr")

        if not eng.data_store:
            return None

        history = eng.data_store.load(symbol)
        if history is None or len(history) < 20:
            return None

        try:
            indicators = compute_indicators(history)
            eng._indicator_cache[symbol] = indicators
            return indicators.get("atr")
        except Exception as e:
            logger.debug(f"[US ATR] {symbol} 인디케이터 계산 실패: {e}")
            return None

    async def _get_exchange(self, symbol: str) -> str:
        """종목의 거래소 코드 조회 (캐시, yfinance 동기 IO → to_thread)"""
        eng = self.engine
        if symbol in eng._exchange_cache:
            return eng._exchange_cache[symbol]

        try:
            info = await asyncio.to_thread(eng.data_provider.get_info, symbol)
            raw_exchange = info.get("exchange", "") or ""
            exchange = EXCHANGE_MAP.get(raw_exchange.upper(), eng._default_exchange)
            sector = info.get("sector", "") or ""
            if sector:
                eng._sector_cache[symbol] = sector
        except Exception as e:
            logger.debug(f"[US 거래소] {symbol} 조회 실패: {e}")
            exchange = eng._default_exchange

        eng._exchange_cache[symbol] = exchange
        return exchange

    def _is_in_cooldown(self, symbol: str) -> bool:
        """시그널 쿨다운 체크"""
        eng = self.engine
        last = eng._signal_cooldown.get(symbol)
        if last is None:
            return False
        elapsed = (datetime.now() - last).total_seconds()
        return elapsed < eng._signal_cooldown_sec

    # ============================================================
    # 태스크 1: 스크리닝 루프
    # ============================================================

    async def screening_loop(self):
        """유니버스 스캔 → 전략 시그널 → 주문"""
        await asyncio.sleep(5)  # 초기 대기
        eng = self.engine

        while eng.running:
            try:
                if not eng.session.is_market_open():
                    logger.debug("[US 스크리닝] 장 마감 — skip")
                    await asyncio.sleep(60)
                    continue

                # 일일 통계 리셋 (장 시작 시 1회)
                today = eng.session.now_et().date()
                if eng._daily_reset_done != today:
                    eng._daily_reset_done = today
                    eng.portfolio.reset_daily()
                    # 당일 청산 종목 블랙리스트 초기화 (새 거래일)
                    if hasattr(eng, "_stopped_today"):
                        eng._stopped_today = set()
                    # 당일 청산 파일 복원 (봇 재시작 시 재매수 차단 유지)
                    try:
                        import json as _json
                        from pathlib import Path as _Path
                        _cache_dir = _Path.home() / ".cache" / "ai_trader_us"
                        _today_str = today.strftime("%Y%m%d")
                        _stopped_file = _cache_dir / f"stopped_today_{_today_str}.json"
                        if _stopped_file.exists():
                            _data = _json.loads(_stopped_file.read_text())
                            _loaded = set(_data.get("symbols", []))
                            if _loaded:
                                if not hasattr(eng, "_stopped_today"):
                                    eng._stopped_today = set()
                                eng._stopped_today.update(_loaded)
                                logger.info(f"[US 재매수차단] 파일 복원: {sorted(_loaded)}")
                    except Exception as _e:
                        logger.warning(f"[US 재매수차단] 파일 로드 실패 (무시): {_e}")
                    if hasattr(eng, "_order_fail_blacklist"):
                        eng._order_fail_blacklist = set()
                    self._quote_fail_count = {}
                    logger.info("[US 엔진] 일일 통계 리셋")

                # 어닝 캘린더 갱신 (1일 1회)
                if eng.earnings_provider and eng._earnings_last_refresh != today:
                    try:
                        eng._earnings_today = await eng.earnings_provider.get_today_earnings(today)
                        eng._earnings_last_refresh = today
                    except Exception as e:
                        logger.warning(f"[US Earnings] 갱신 실패: {e}")

                # Finviz 수급 데이터 갱신 (1일 1회, 장 시작 후)
                fz = self._finviz
                if fz and eng._finviz_last_refresh != today:
                    try:
                        refreshed = await fz.refresh(eng._universe, today)
                        eng._finviz_last_refresh = today
                        if refreshed:
                            logger.info(
                                f"[US Finviz] 갱신 완료: {fz.coverage()}종목"
                            )
                    except Exception as e:
                        logger.warning(f"[US Finviz] 갱신 실패: {e}")

                # Finviz 동적 유니버스 갱신 (1일 1회)
                if fz and eng._dynamic_last_refresh != today:
                    try:
                        await asyncio.sleep(5)  # Rate limit 방지
                        dynamic = await fz.discover_dynamic()
                        eng._dynamic_last_refresh = today
                        if dynamic:
                            _cfg_excl = set(
                                eng.config_raw.get("us", {}).get("universe", {})
                                .get("excluded_symbols", [])
                            ) if hasattr(eng, "config_raw") else set()
                            new_syms = set(dynamic) - set(eng._universe) - _cfg_excl
                            eng._dynamic_symbols = new_syms
                            if new_syms:
                                logger.info(
                                    f"[US Finviz 동적] 신규 {len(new_syms)}종목 보강 "
                                    f"(기존 유니버스 외)"
                                )
                        else:
                            logger.debug("[US Finviz 동적] 오늘 발견 종목 없음")
                    except Exception as e:
                        logger.warning(f"[US Finviz 동적] 갱신 실패: {e}")

                await self._run_screening()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[US 스크리닝] 오류: {e}")

            await asyncio.sleep(eng._screening_interval)

    async def _run_screening(self):
        """한 사이클의 스크리닝 + 시그널 처리"""
        eng = self.engine

        # RS Ranking용 벤치마크 주입 (한 번만)
        if not getattr(self, '_benchmark_loaded', False):
            try:
                from src.data.store import DataStore
                store = DataStore()
                spy_df = store.load("SPY", "daily")
                if spy_df is not None and len(spy_df) >= 252:
                    for strat in eng.strategies:
                        if hasattr(strat, 'set_benchmark'):
                            strat.set_benchmark(spy_df['close'])
                    self._benchmark_loaded = True
                    logger.info("[RS] SPY 벤치마크 전략에 주입 완료")
            except Exception as e:
                logger.debug(f"[RS] 벤치마크 주입 실패: {e}")

        # 포지션 여유 없으면 스크리닝 스킵 (pending 포함)
        _effective_pos = len(eng.portfolio.positions) + len(
            eng._pending_symbols - set(eng.portfolio.positions.keys())
        )
        _max_pos = eng.risk_manager.config.max_positions if eng.risk_manager else 4
        if _effective_pos >= _max_pos:
            logger.debug(
                f"[US 스크리닝] 포지션 여유 없음 ({_effective_pos}/{_max_pos}) — 스킵"
            )
            return

        logger.info(f"[US 스크리닝] 시작 — {len(eng._universe)} 종목 중 "
                     f"최대 {eng._max_screen_symbols}개 스캔")

        # 보유 종목 캐시 보존, 나머지 정리
        held = set(eng.portfolio.positions.keys())
        eng._indicator_cache = {
            k: v for k, v in eng._indicator_cache.items() if k in held
        }

        # 쿨다운 만료 항목 정리
        now = datetime.now()
        expired = [s for s, t in eng._signal_cooldown.items()
                   if (now - t).total_seconds() > eng._signal_cooldown_sec]
        for s in expired:
            del eng._signal_cooldown[s]

        # ── StockScreener 결과 기반 후보 우선 사용 ──────────────────
        held = set(eng.portfolio.positions.keys())
        screen_candidates: List[str] = []

        if eng._last_screen_result and eng._last_screen_result.results:
            screen_candidates = [
                r.symbol for r in eng._last_screen_result.results
                if r.symbol not in held
            ][:150]
            logger.debug(
                f"[US 스크리닝] StockScreener 상위 {len(screen_candidates)}개 후보 사용"
            )

        # ── 프리마켓 갭 스캔 삽입 ─────────────────────────────────
        if eng.screener and self._finviz and self._finviz_ready:
            try:
                gap_results = await eng.screener.scan_premarket_gap(
                    eng._universe[:200], min_gap_pct=2.0, limit=15
                )
                if gap_results:
                    gap_symbols = [
                        r.symbol for r in gap_results
                        if r.symbol not in held and r.symbol not in eng._signal_cooldown
                        and r.symbol not in eng._pending_symbols
                    ]
                    if gap_symbols:
                        existing = set(screen_candidates)
                        new_gap = [s for s in gap_symbols if s not in existing]
                        screen_candidates = new_gap + screen_candidates
                        logger.info(
                            f"[US 프리마켓] 갭 {len(new_gap)}종목 최우선 삽입"
                        )
            except Exception as e:
                logger.debug(f"[US 프리마켓] 갭 스캔 오류: {e}")

        # ── 거래량급증 종목 최우선 삽입 ───────────────────────────
        if eng._vol_surge_symbols and eng._vol_surge_updated:
            surge_age = (datetime.now() - eng._vol_surge_updated).total_seconds()
            if surge_age < 1800:  # 30분 이내 데이터만 유효
                surge_candidates = [
                    s for s in eng._vol_surge_symbols
                    if s not in held and s not in eng._signal_cooldown
                    and s not in eng._pending_symbols
                ]
                if surge_candidates:
                    existing = set(screen_candidates)
                    new_surge = [s for s in surge_candidates if s not in existing]
                    screen_candidates = new_surge + screen_candidates
                    logger.info(
                        f"[US 스크리닝] 거래량급증 {len(new_surge)}종목 최우선 삽입 "
                        f"→ 총 {len(screen_candidates)}개 후보"
                    )

        # Finviz 동적 발견 종목을 후보 상위에 삽입
        if eng._dynamic_symbols:
            dynamic_candidates = [
                s for s in eng._dynamic_symbols
                if s not in held and s not in eng._signal_cooldown
                and s not in eng._pending_symbols
            ]
            if dynamic_candidates:
                existing = set(screen_candidates)
                new_dynamic = [s for s in dynamic_candidates if s not in existing]
                screen_candidates = new_dynamic + screen_candidates
                logger.debug(
                    f"[US 스크리닝] 동적 {len(new_dynamic)}종목 삽입 → "
                    f"총 {len(screen_candidates)}개 후보"
                )

        if screen_candidates:
            candidates = screen_candidates[:eng._max_screen_symbols]
        else:
            # 폴백: 랜덤 셔플 (StockScreener 결과 없을 때)
            logger.debug("[US 스크리닝] StockScreener 결과 없음 — 랜덤 샘플 폴백")
            candidates = [s for s in eng._universe if s not in held]
            random.shuffle(candidates)
            candidates = candidates[:eng._max_screen_symbols]

        # ── 동적 max_price (가용 현금 × max_position_pct%) ─────────────
        uni_cfg = eng.config_raw.get("universe")
        if uni_cfg is None:
            uni_cfg = {}
        uni_max_price = float(uni_cfg.get("max_price", 0))
        risk_cfg = eng.risk_manager.config
        max_pos_pct = getattr(risk_cfg, 'max_position_pct', 25.0)
        dynamic_max_price = float(eng.portfolio.cash) * (max_pos_pct / 100)
        effective_max_price = uni_max_price if uni_max_price > 0 else dynamic_max_price

        signals: List[Signal] = []
        processed = 0

        for symbol in candidates:
            if not eng.running:
                break

            # 기보유 종목 스킵 (추가 매수 방지)
            if symbol in eng.portfolio.positions:
                continue

            # 쿨다운 체크
            if self._is_in_cooldown(symbol):
                continue

            # 이미 주문 중이면 스킵
            if symbol in eng._pending_symbols:
                continue

            try:
                # 히스토리 로드 (캐시 + yfinance)
                history = await self._get_history(symbol)
                if history is None or len(history) < 50:
                    continue

                last_close = float(history['close'].iloc[-1])

                # 동적 max_price 필터
                if effective_max_price > 0 and last_close > effective_max_price:
                    continue

                # 인디케이터 사전 계산 → 캐시
                try:
                    eng._indicator_cache[symbol] = compute_indicators(history)
                except Exception as e:
                    logger.debug(f"[US 스크리닝] {symbol} 인디케이터 캐시 실패: {e}")

                # ── 전략 선택 필터 ──────────────────────────────────
                for strategy in eng.strategies:
                    if (
                        strategy.name == "earnings_drift"
                        and eng._earnings_today
                        and symbol not in eng._earnings_today
                    ):
                        continue

                    signal = strategy.evaluate(symbol, history, eng.portfolio)
                    if signal:
                        # ── Finviz 전략별 시그널 보정 ────────────────────
                        fz = self._finviz
                        if fz and self._finviz_ready:
                            fz_result = fz.get_strategy_signals(symbol, strategy.name)
                            if not fz_result["pass"]:
                                logger.info(
                                    f"[US Finviz 필터] {symbol} {strategy.name} 제외 "
                                    f"— {'; '.join(fz_result.get('warnings', []))}"
                                )
                                signal = None
                            else:
                                adj = fz_result["score_adjustment"]
                                if adj != 0:
                                    old_score = signal.score
                                    signal.score = max(0.0, signal.score + adj)
                                    logger.debug(
                                        f"[US Finviz] {symbol} 점수 {old_score:.1f} "
                                        f"→ {signal.score:.1f} ({adj:+.1f}pt)"
                                    )
                                if fz_result["reasons"]:
                                    signal.reason = (
                                        signal.reason + " | " +
                                        ", ".join(fz_result["reasons"][:2])
                                    )
                    if signal:
                        signals.append(signal)
                        break  # 한 종목당 하나의 시그널

                processed += 1
            except Exception as e:
                logger.debug(f"[US 스크리닝] {symbol} 평가 실패: {e}")

        # ── Finviz 실시간 가격 사전 필터 (프리마켓 가격 괴리 제거) ──────
        fz = self._finviz
        if fz and self._finviz_ready and signals:
            try:
                sig_symbols = [s.symbol for s in signals]
                fz_snap = await fz.get_intraday_scan(sig_symbols)
                filtered_signals = []
                for sig in signals:
                    snap = fz_snap.get(sig.symbol)
                    if not snap:
                        filtered_signals.append(sig)  # Finviz 데이터 없으면 통과
                        continue
                    fz_chg = snap.get("change_pct", 0.0)
                    fz_price = snap.get("price", 0.0)
                    # 당일 변동률 -3% 이하 → 하락 종목 제외
                    if fz_chg <= -3.0:
                        logger.info(
                            f"[US Finviz 가격필터] {sig.symbol} 제외: "
                            f"당일 {fz_chg:+.1f}% (Finviz ${fz_price:.2f})"
                        )
                        continue
                    # Finviz 실시간 가격 vs 시그널 가격 괴리 5% 이상 → 제외
                    if sig.price and float(sig.price) > 0 and fz_price > 0:
                        gap = abs(fz_price - float(sig.price)) / float(sig.price) * 100
                        if gap >= 5.0:
                            logger.info(
                                f"[US Finviz 가격필터] {sig.symbol} 제외: "
                                f"가격괴리 {gap:.1f}% (시그널=${float(sig.price):.2f} vs Finviz=${fz_price:.2f})"
                            )
                            continue
                    filtered_signals.append(sig)
                if len(filtered_signals) < len(signals):
                    logger.info(
                        f"[US Finviz 가격필터] {len(signals) - len(filtered_signals)}개 시그널 제거 "
                        f"(원본 {len(signals)} → {len(filtered_signals)})"
                    )
                signals = filtered_signals
            except Exception as e:
                logger.debug(f"[US Finviz 가격필터] 조회 실패 (무시): {e}")

        # 시그널 스코어 순 정렬 → 성공 N개까지 시도
        signals.sort(key=lambda s: s.score, reverse=True)
        submitted = 0
        _consecutive_fund_fail = 0  # 연속 자금 부족 실패 카운터

        for sig in signals:
            if submitted >= eng._max_signals_per_cycle:
                break
            # 연속 3건 자금 부족 → 나머지 시그널 조기 중단 (API 호출 낭비 방지)
            if _consecutive_fund_fail >= 3:
                logger.info(
                    f"[US 스크리닝] 자금 부족 연속 {_consecutive_fund_fail}건 → "
                    f"나머지 {len(signals) - signals.index(sig)}건 스킵"
                )
                break
            success = await self._process_signal(sig)
            if success:
                submitted += 1
                _consecutive_fund_fail = 0
            elif self._last_reject_reason == "insufficient_funds":
                _consecutive_fund_fail += 1
            else:
                _consecutive_fund_fail = 0  # 다른 사유 실패는 리셋

        earnings_count = len(eng._earnings_today) if eng._earnings_today else 0
        logger.info(
            f"[US 스크리닝] 완료 — 스캔: {processed}, 시그널: {len(signals)}, "
            f"주문: {submitted} | earnings 대상: {earnings_count}개"
        )

    async def _process_signal(self, signal: Signal) -> bool:
        """시그널을 주문으로 변환"""
        self._last_reject_reason = ""  # 실패 사유 추적 (screening_loop 조기 중단용)
        eng = self.engine
        symbol = signal.symbol

        # 당일 손절/트레일링 청산 종목 재매수 차단
        if symbol in getattr(eng, "_stopped_today", set()):
            logger.debug(f"[US 시그널] {symbol} — 당일 청산 종목, 재매수 차단")
            return False

        # 주문 영구실패 블랙리스트 (ETP 미신청, 매수불가 등)
        if symbol in getattr(eng, "_order_fail_blacklist", set()):
            logger.debug(f"[US 시그널] {symbol} — 주문 실패 블랙리스트, 스킵")
            return False

        # config excluded_symbols (해외ETP 미신청 등 영구 제외 종목)
        _cfg_excluded = set(
            (eng.config_raw.get("us", {}) if hasattr(eng, "config_raw") else {})
            .get("universe", {}).get("excluded_symbols", [])
        )
        if symbol in _cfg_excluded:
            logger.debug(f"[US 시그널] {symbol} — config 영구 제외 종목, 스킵")
            return False

        # pending 포함 실효 포지션 수로 max_positions 조기 차단
        effective_pos_count = len(eng.portfolio.positions) + len(
            eng._pending_symbols - set(eng.portfolio.positions.keys())
        )
        max_pos = eng.risk_manager.config.max_positions
        if symbol not in eng.portfolio.positions and effective_pos_count >= max_pos:
            logger.info(
                f"[US 시그널] {symbol} — max_positions 초과 "
                f"(보유={len(eng.portfolio.positions)}, pending={len(eng._pending_symbols)}, "
                f"한도={max_pos})"
            )
            return False

        # 현재가 조회 실패 누적 종목 스킵 (3회 이상)
        _QUOTE_FAIL_LIMIT = 3
        if self._quote_fail_count.get(symbol, 0) >= _QUOTE_FAIL_LIMIT:
            logger.debug(f"[US 시그널] {symbol} — 현재가 반복실패 블랙리스트, 스킵")
            return False

        # 현재가 조회 (리스크 체크에 필요)
        exchange = await self._get_exchange(symbol)
        quote = await eng.broker.get_quote(symbol, exchange)
        price = quote.get("price", 0)
        if price <= 0:
            self._quote_fail_count[symbol] = self._quote_fail_count.get(symbol, 0) + 1
            if self._quote_fail_count[symbol] >= _QUOTE_FAIL_LIMIT:
                logger.warning(
                    f"[US 시그널] {symbol} — 현재가 {_QUOTE_FAIL_LIMIT}회 실패, 세션 제외"
                )
            else:
                logger.warning(f"[US 시그널] {symbol} — 현재가 조회 실패")
            return False
        # 조회 성공 시 실패 카운터 초기화
        self._quote_fail_count.pop(symbol, None)

        # 시그널 평가가격 vs 실시간 현재가 갭 체크 (프리마켓 가격 괴리 방지)
        if signal.price and float(signal.price) > 0:
            signal_price = float(signal.price)
            gap_pct = (price - signal_price) / signal_price * 100
            # 현재가가 시그널 가격 대비 3% 이상 하락 → 진입 보류
            if gap_pct <= -3.0:
                logger.warning(
                    f"[US 시그널] {symbol} — 가격 괴리 차단: "
                    f"시그널=${signal_price:.2f} → 현재=${price:.2f} ({gap_pct:+.1f}%)"
                )
                return False
            # 현재가가 시그널 가격 대비 5% 이상 상승 → 추격 매수 방지
            if gap_pct >= 5.0:
                logger.warning(
                    f"[US 시그널] {symbol} — 추격매수 차단: "
                    f"시그널=${signal_price:.2f} → 현재=${price:.2f} ({gap_pct:+.1f}%)"
                )
                return False

        # 포지션 사이징 (allow_min_one=True — 금액 기준 최소 1주 보장)
        qty = eng.risk_manager.calculate_position_size(
            eng.portfolio, Decimal(str(price)), allow_min_one=True
        )
        if qty <= 0:
            logger.info(f"[US 시그널] {symbol} — 사이징 0주 (자금 부족)")
            self._last_reject_reason = "insufficient_funds"
            return False

        # 리스크 체크 (섹터 다각화 포함)
        sector = eng._sector_cache.get(symbol)
        can_open, reject_reason = eng.risk_manager.can_open_position(
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=qty,
            price=Decimal(str(price)),
            portfolio=eng.portfolio,
            strategy_type=signal.strategy.value if hasattr(signal.strategy, 'value') else str(signal.strategy),
            signal=signal,
            sector=sector,
        )
        if not can_open:
            logger.info(f"[US 시그널] {symbol} — 리스크 체크 실패: {reject_reason}")
            return False

        # ── Finviz Beta 기반 포지션 리스크 보정 ──────────────────────────
        fz = self._finviz
        if fz and self._finviz_ready and qty > 1:
            multiplier, risk_reason = fz.get_risk_multiplier(symbol)
            if multiplier < 1.0:
                adjusted = max(1, int(qty * multiplier))
                if adjusted < qty:
                    logger.info(
                        f"[US Finviz 리스크] {symbol} {qty}→{adjusted}주 "
                        f"({risk_reason})"
                    )
                    qty = adjusted

        # ── Finviz 장중 모멘텀 최종 확인 (매수 직전 게이트) ─────────────
        if fz and self._finviz_ready:
            try:
                intraday = await fz.get_intraday_scan([symbol])
                d = intraday.get(symbol, {})
                perf_1h = d.get("perf_1h", 0.0)
                perf_30m = d.get("perf_30m", 0.0)
                ms = d.get("momentum_score", 50.0)
                # 하락 지속 중: 1시간 -2% 이상 + 30분도 -1% 이상
                if perf_1h <= -2.0 and perf_30m <= -1.0:
                    logger.info(
                        f"[US 장중확인] {symbol} 하락 지속 → 진입 보류 "
                        f"(1h={perf_1h:+.2f}%, 30m={perf_30m:+.2f}%, ms={ms:.0f})"
                    )
                    return False
                if ms < 40:
                    logger.info(
                        f"[US 장중확인] {symbol} 장중 모멘텀 약세 → 진입 보류 "
                        f"(ms={ms:.0f}, 1h={perf_1h:+.2f}%)"
                    )
                    return False
            except Exception as _ie:
                logger.debug(f"[US 장중확인] {symbol} Finviz 조회 실패 → 스킵: {_ie}")

        # 매수 주문 제출 (지정가: 현재가 + 0.2% 허용)
        limit_price = float(
            (Decimal(str(price)) * Decimal("1.002")).quantize(Decimal("0.01"))
        )
        result = await eng.broker.submit_buy_order(symbol, exchange, qty, price=limit_price)

        if result.get("success"):
            order_no = result.get("order_no", "").strip()
            if not order_no:
                order_no = f"local-{uuid.uuid4().hex[:12]}"
                logger.warning(f"[US 매수 주문] {symbol} — KIS 주문번호 미반환, 폴백 사용: {order_no}")

            strategy_val = signal.strategy.value if hasattr(signal.strategy, "value") else str(signal.strategy)
            eng._pending_orders[order_no] = {
                "symbol": symbol,
                "side": "buy",
                "qty": qty,
                "price": price,
                "strategy": strategy_val,
                "signal_score": signal.score,
                "exchange": exchange,
                "submitted_at": datetime.now(),
            }
            eng._pending_symbols.add(symbol)
            eng._signal_cooldown[symbol] = datetime.now()
            eng._symbol_strategy[symbol] = strategy_val

            eng.recent_signals.append({
                "symbol": signal.symbol,
                "strategy": strategy_val,
                "score": float(signal.score) if signal.score else 0.0,
                "side": signal.side.value if hasattr(signal.side, "value") else str(signal.side),
                "timestamp": datetime.now().isoformat(),
                "reason": signal.reason or "",
            })

            logger.info(
                f"[US 매수 주문] {symbol} {qty}주 @ ${limit_price:.2f} (지정가) "
                f"({strategy_val}, score={signal.score:.0f})"
            )
            return True
        else:
            fail_msg = result.get("message", "")
            logger.warning(f"[US 매수 주문] {symbol} 실패: {fail_msg}")
            # 자금 부족 → 스크리닝 조기 종료용 사유 기록
            if "주문가능금액" in fail_msg or "잔고부족" in fail_msg:
                self._last_reject_reason = "insufficient_funds"
            # 영구 실패 코드 → 당일 블랙리스트 (재시도 차단)
            _PERMANENT_FAIL_KEYWORDS = [
                "해외ETP 거래 미신청",
                "매수불가 종목",
                "취소주문만 가능",
            ]
            if any(kw in fail_msg for kw in _PERMANENT_FAIL_KEYWORDS):
                blacklist = getattr(eng, "_order_fail_blacklist", set())
                blacklist.add(symbol)
                logger.info(f"[US 블랙리스트] {symbol} 당일 매수 차단 — {fail_msg[:40]}")
            return False

    # ============================================================
    # KIS 해외주식 실시간가 WS 콜백 (HDFSCNT0)
    # ============================================================

    async def ws_market_loop(self):
        """KIS HDFSCNT0 WS 시장 생명주기 관리

        - 미국 정규장 시작 10분 전 WS 사전 연결 (포지션 진입 즉시 subscribe 가능)
        - 기존 보유 포지션 있으면 시작 시 바로 subscribe
        - 정규장 종료 후 30분 대기 → WS 종료 (불필요한 연결 해제)
        - 루프 주기: 30초 (시장 상태 감지)
        - 체결통보(H0GSCNI0): 가격 WS와 동일 연결에서 동시 구독 (KIS WS 1개 제약 해결)
          KR WS는 09:00~15:20 KST에만 연결 → US WS(22:20~05:30) 와 시간대 불겹침
        """
        eng = self.engine
        if not eng.us_price_ws:
            return

        # 체결통보 콜백 설정 (hts_id 있을 때만)
        import os as _os
        _hts_id = _os.getenv("KIS_HTS_ID", "").strip()
        if (_hts_id and _hts_id.isalnum() and len(_hts_id) >= 6
                and hasattr(eng, '_on_kis_fill')):
            eng.us_price_ws.setup_fill(_hts_id)
            eng.us_price_ws.on_fill(eng._on_kis_fill)
            logger.info(f"[KIS US WS] 체결통보 구독 설정 완료 (HTS ID: {_hts_id[:4]}****)")

        _ws_prestarted = False  # 사전 연결 완료 여부

        # 서비스 시작 시: 미국 장 시간에만 WS 연결 (KIS approval_key 1개 공유 제약)
        # KR 정규장(09:00~15:20) 중에는 KR WS가 approval_key를 점유 → US WS 연결 금지
        await asyncio.sleep(5)  # 초기화 대기
        try:
            _mto = eng.session.minutes_to_open()
            _initial_open_soon = (_mto is not None and _mto <= 10)
            _initial_is_open = eng.session.is_market_open()
            if eng.portfolio.positions and (_initial_open_soon or _initial_is_open):
                logger.info("[KIS US WS] 초기 포지션 감지 + 미국 장 시간 → WS 사전 연결")
                await self._ensure_us_price_ws_running()
                await asyncio.sleep(3)
                for symbol in list(eng.portfolio.positions.keys()):
                    exchange = eng._exchange_cache.get(symbol, eng._default_exchange)
                    await eng.us_price_ws.subscribe([symbol], exchange=exchange)
                logger.info(f"[KIS US WS] 초기 구독: {list(eng.portfolio.positions.keys())}")
                _ws_prestarted = True
            elif eng.portfolio.positions:
                logger.info(
                    "[KIS US WS] 초기 포지션 있으나 미국 장 외 시간 → WS 연결 대기 "
                    "(KIS approval_key 1개 제약: KR WS 우선)"
                )
        except Exception as e:
            logger.warning(f"[ws_market_loop] 초기화 오류 (루프 계속): {e}")

        while eng.running:
            try:
                et_now = eng.session.now_et()
                # 미국 정규장 시작 10분 전 ~ 장 중: WS 사전 연결
                _mto = eng.session.minutes_to_open()
                market_open_soon = (_mto is not None and _mto <= 10)
                is_open = eng.session.is_market_open()

                if (market_open_soon or is_open) and not _ws_prestarted:
                    logger.info("[KIS US WS] 미국장 시작 전 WS 사전 연결")
                    await self._ensure_us_price_ws_running()
                    _ws_prestarted = True

                # WS 연결된 상태에서 포지션 구독 동기화
                if eng.us_price_ws.is_connected and eng.portfolio.positions:
                    for symbol in list(eng.portfolio.positions.keys()):
                        if symbol not in eng.us_price_ws._subscribed and symbol not in eng.us_price_ws._pending_sub:
                            exchange = eng._exchange_cache.get(symbol, eng._default_exchange)
                            await eng.us_price_ws.subscribe([symbol], exchange=exchange)

                # 장 종료 후 30분 경과 + 포지션 없음 → WS 종료
                if _ws_prestarted and not is_open:
                    mins_after_close = eng.session.minutes_since_close()
                    if mins_after_close is not None and mins_after_close >= 30 and not eng.portfolio.positions:
                        logger.info("[KIS US WS] 장 종료 30분 경과, 포지션 없음 → WS 종료")
                        await self._maybe_stop_us_price_ws()
                        _ws_prestarted = False

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[ws_market_loop] 오류 (무시): {e}")

            await asyncio.sleep(30)

    async def _init_ws_price_subs(self):
        """[DEPRECATED] ws_market_loop로 대체됨"""
        pass

    async def _ensure_us_price_ws_running(self):
        """보유 포지션이 생겼을 때 WS 태스크 시작 (이미 실행 중이면 스킵)

        KIS approval_key는 계정당 1개 → KR 정규장 중 KR WS가 점유.
        미국 정규장 시간(KST 22:30~05:00 / ET 08:20~16:00)에만 US WS 연결.
        """
        eng = self.engine
        if not eng.us_price_ws:
            return

        # ── KIS approval_key 1개 제약: 미국 장 시간에만 WS 허용 ──────────────
        is_open = eng.session.is_market_open()
        _mto = eng.session.minutes_to_open()
        open_soon = (_mto is not None and _mto <= 10)
        if not (is_open or open_soon):
            logger.debug("[KIS US WS] 미국 장 외 시간 → WS 연결 스킵 (REST polling 사용)")
            return

        # 이미 연결됐거나 태스크 실행 중이면 스킵
        if eng.us_price_ws.is_connected:
            return
        task = getattr(eng, '_us_price_ws_task', None)
        if task and not task.done():
            return
        eng._us_price_ws_task = asyncio.create_task(
            eng.us_price_ws.start(), name="us_kis_price_ws"
        )
        logger.info("[KIS US WS] WS 시작 (보유 포지션 진입)")

    async def _maybe_stop_us_price_ws(self):
        """보유 포지션이 모두 청산됐을 때 WS 종료"""
        eng = self.engine
        if not eng.us_price_ws:
            return
        if eng.portfolio.positions:
            return  # 아직 포지션 남아 있음
        if not eng.us_price_ws.is_connected:
            # 태스크만 남은 경우 취소
            task = getattr(eng, '_us_price_ws_task', None)
            if task and not task.done():
                task.cancel()
            eng._us_price_ws_task = None
            return
        await eng.us_price_ws.stop()
        task = getattr(eng, '_us_price_ws_task', None)
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=3)
            except Exception:
                pass
        eng._us_price_ws_task = None
        logger.info("[KIS US WS] WS 종료 (보유 포지션 없음)")

    async def _on_us_ws_price(self, symbol: str, price: float, volume: int):
        """
        KIS 해외주식 실시간가 WS 콜백 (HDFSCNT0)

        1. position.current_price / highest_price 즉시 갱신
        2. ExitManager.update_price() 호출 → exit 시그널 발생 시 매도 실행
        """
        eng = self.engine
        pos = eng.portfolio.positions.get(symbol)
        if not pos or symbol in eng._pending_symbols:
            return

        cur = Decimal(str(price))
        pos.current_price = cur
        if pos.highest_price is None or cur > pos.highest_price:
            pos.highest_price = cur

        # per-symbol 락 (REST exit_check과 동시 실행 방지)
        lock = self._exit_locks.setdefault(symbol, asyncio.Lock())
        if lock.locked():
            return  # REST 체크가 이미 진행 중 → 스킵 (다음 틱에서 재처리)

        async with lock:
            # ExitManager 실시간 체크
            exit_signal = eng.exit_manager.update_price(symbol, cur)
            if not exit_signal:
                return

            # pending 재확인 (update_price 처리 중 상태 변경 가능)
            if symbol in eng._pending_symbols:
                return

            action, qty, reason = exit_signal
            exchange = await self._get_exchange(symbol)
            ratio = qty / pos.quantity if pos.quantity > 0 else 1.0
            logger.info(
                f"[KIS US WS] exit 시그널 → {symbol} {action} {ratio:.0%} "
                f"@ ${price:.2f} ({reason})"
            )
            await self._execute_exit(
                symbol, pos,
                {"action": action, "ratio": ratio, "reason": reason, "qty": qty},
                exchange,
            )

    # ============================================================
    # 태스크 2: 청산 체크 루프
    # ============================================================

    async def exit_check_loop(self):
        """보유 포지션 → ExitManager → 매도 (KIS REST 실시간 가격 기준)

        KIS 해외주식 실시간가 WS(HDFSCNT0)가 연결된 경우:
          WS 콜백(_on_us_ws_price)이 실시간으로 exit 체크 → REST는 백업 역할로 주기 완화(60s)
        WS 미연결 / 비정규장:
          기존 REST 폴링 주기(기본 30초) 유지
        """
        eng = self.engine
        _rest_interval_no_ws = 15   # WS 미연결 시 REST 폴링 주기 (빠른 손절 대응)
        _ws_backup_interval  = 60   # WS 연결 시 REST 백업 주기

        while eng.running:
            try:
                # stage 복원 완료 전까지 exit 체크 보류 (중복 익절 방지)
                if not getattr(self, '_exit_stages_restored', False):
                    await asyncio.sleep(5)
                    continue

                if not eng.session.is_market_open():
                    await asyncio.sleep(180)  # 비정규장: 3분
                    continue

                await self._check_exits()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[US 청산 체크] 오류: {e}")

            # WS 연결 시 REST 폴링 완화 (WS가 실시간 exit 처리)
            ws_ok = eng.us_price_ws and eng.us_price_ws.is_connected
            await asyncio.sleep(_ws_backup_interval if ws_ok else _rest_interval_no_ws)

    async def _check_exits(self):
        """보유 포지션 순회 → KIS REST 실시간 가격 → 청산 시그널 체크"""
        eng = self.engine
        positions = list(eng.portfolio.positions.items())
        if not positions:
            return
        logger.debug(f"[US 청산 체크] {len(positions)}개 포지션 체크")

        for symbol, position in positions:
            if symbol in eng._pending_symbols:
                continue

            # per-symbol 락 (WS 콜백과 동시 실행 방지)
            lock = self._exit_locks.setdefault(symbol, asyncio.Lock())
            if lock.locked():
                continue  # WS 콜백이 이미 처리 중 → 스킵

            async with lock:
                try:
                    # KIS REST 실시간 현재가 (primary)
                    exchange = await self._get_exchange(symbol)
                    quote = await eng.broker.get_quote(symbol, exchange)
                    price = quote.get("price", 0)
                    if price <= 0:
                        continue

                    position.current_price = Decimal(str(price))

                    # 최고가 갱신
                    if position.highest_price is None or position.current_price > position.highest_price:
                        position.highest_price = position.current_price

                    # 전략별 커스텀 exit 체크 (SEPA MA50 이탈 등)
                    strategy_exit_submitted = False
                    if position.strategy:
                        for strat in eng.strategies:
                            if strat.strategy_type.value == position.strategy:
                                if eng.data_store:
                                    history = eng.data_store.load(symbol)
                                    if history is not None and len(history) >= 50:
                                        custom_reason = strat.check_exit(symbol, history, position)
                                        if custom_reason:
                                            logger.info(f"[US 전략 청산] {symbol} — {custom_reason}")
                                            exit_ok = await self._execute_exit(
                                                symbol, position,
                                                {'action': 'close', 'ratio': 1.0, 'reason': custom_reason},
                                                exchange,
                                            )
                                            strategy_exit_submitted = bool(exit_ok)  # 매도 주문 성공 시에만 True
                                break

                    # ExitManager 체크 (전략 exit 미발동 또는 주문 실패 시 폴백 — 손절 안전망)
                    if not strategy_exit_submitted and symbol not in eng._pending_symbols:
                        exit_signal = eng.exit_manager.update_price(symbol, Decimal(str(price)))
                        if exit_signal:
                            action, exit_qty, reason = exit_signal
                            ratio = exit_qty / position.quantity if position.quantity > 0 else 1.0
                            await self._execute_exit(
                                symbol, position,
                                {'action': action, 'ratio': ratio, 'reason': reason, 'qty': exit_qty},
                                exchange,
                            )

                except Exception as e:
                    logger.warning(f"[US 청산 체크] {symbol} 오류: {e}")

    async def _execute_exit(self, symbol: str, position: Position,
                            exit_signal: dict, exchange: str) -> bool:
        """매도 주문 실행. 성공 시 True, 실패/스킵 시 False 반환."""
        eng = self.engine

        # 레이스 컨디션 방지
        if symbol in eng._pending_symbols:
            logger.debug(f"[US 매도 주문] {symbol} — 이미 pending, 스킵")
            return False

        # 매도 실패 쿨다운 (5분)
        last_fail = self._sell_fail_cooldown.get(symbol)
        if last_fail and (datetime.now() - last_fail).total_seconds() < 300:
            return False

        action = exit_signal.get("action", "close")
        ratio = exit_signal.get("ratio", 1.0)
        reason = exit_signal.get("reason", "")

        # ExitManager가 직접 수량을 제공한 경우 사용
        sell_qty = int(exit_signal.get("qty", 0))
        if sell_qty <= 0:
            sell_qty = int(position.quantity * ratio)

        if sell_qty <= 0:
            if ratio < 1.0:
                # 분할매도인데 최소 1주도 안 됨 → 스킵 (전량매도 방지)
                logger.debug(
                    f"[US 매도 주문] {symbol} — 분할매도 스킵 (보유 {position.quantity}주, "
                    f"ratio={ratio:.0%} → 0주)"
                )
                return False
            sell_qty = position.quantity  # 전량매도만 fallback

        sell_price = round(float(position.current_price), 2)
        if sell_price <= 0:
            logger.error(f"[US 매도 주문] {symbol} — 현재가 0, 주문 취소 (시장가 오발주 방지)")
            return False

        result = await eng.broker.submit_sell_order(symbol, exchange, sell_qty, price=sell_price)

        if result.get("success"):
            self._sell_fail_cooldown.pop(symbol, None)  # 성공 시 쿨다운 해제
            order_no = result.get("order_no", "").strip()
            if not order_no:
                order_no = f"local-{uuid.uuid4().hex[:12]}"
                logger.warning(f"[US 매도 주문] {symbol} — KIS 주문번호 미반환, 폴백 사용: {order_no}")

            # exit_type 추론
            exit_type = "unknown"
            if reason:
                rl = reason.lower()
                if "stop_loss" in rl or "손절" in rl:
                    exit_type = "stop_loss"
                elif "trailing" in rl or "트레일링" in rl:
                    exit_type = "trailing"
                elif "first_exit" in rl or "1차" in rl:
                    exit_type = "first_take_profit"
                elif "second_exit" in rl or "2차" in rl:
                    exit_type = "second_take_profit"
                elif "third_exit" in rl or "3차" in rl:
                    exit_type = "third_take_profit"
                elif "eod" in rl:
                    exit_type = "eod_close"
                elif "breakeven" in rl or "본전" in rl:
                    exit_type = "breakeven"

            eng._pending_orders[order_no] = {
                "symbol": symbol,
                "side": "sell",
                "qty": sell_qty,
                "orig_qty": position.quantity,
                "price": float(position.current_price),
                "strategy": position.strategy or "",
                "reason": reason,
                "exit_type": exit_type,
                "exchange": exchange,
                "submitted_at": datetime.now(),
            }
            eng._pending_symbols.add(symbol)

            # 당일 매도 종목 → 재매수 차단 + 파일 영속화 (익절/손절 무관)
            if True:
                stopped = getattr(eng, "_stopped_today", set())
                stopped.add(symbol)
                try:
                    import json as _json
                    from pathlib import Path as _Path
                    _cache_dir = _Path.home() / ".cache" / "ai_trader_us"
                    _cache_dir.mkdir(parents=True, exist_ok=True)
                    _today_str = datetime.now().strftime("%Y%m%d")
                    _stopped_file = _cache_dir / f"stopped_today_{_today_str}.json"
                    _stopped_file.write_text(_json.dumps({"symbols": sorted(stopped)}))
                    logger.info(f"[US 재매수차단] {symbol} 당일 청산 등록 — {sorted(stopped)}")
                except Exception as _e:
                    logger.warning(f"[US 재매수차단] 파일 저장 실패 (무시): {_e}")

            logger.info(
                f"[US 매도 주문] {symbol} {sell_qty}/{position.quantity}주 — {reason}"
            )
            return True
        else:
            self._sell_fail_cooldown[symbol] = datetime.now()
            # 매도 실패 시 ExitManager stage 롤백 (stage만 올라가고 실제 매도 안 된 상태 방지)
            if eng.exit_manager:
                eng.exit_manager.rollback_stage(symbol)
            logger.warning(f"[US 매도 주문] {symbol} 실패 (5분 쿨다운, stage 롤백): {result.get('message')}")
            return False

    # ============================================================
    # 태스크 3: 포트폴리오 동기화
    # ============================================================

    async def portfolio_sync_loop(self):
        """KIS 잔고 ↔ 로컬 Portfolio 동기화 (비정규장 주기 축소)"""
        eng = self.engine

        while eng.running:
            try:
                session = eng.session.get_session()
                if session in (MarketSession.CLOSED, MarketSession.PRE_MARKET,
                               MarketSession.AFTER_HOURS):
                    # 비정규장: 5분 간격으로 동기화
                    await self._sync_portfolio()
                    await asyncio.sleep(300)
                    continue

                # 일일 통계 리셋은 screening_loop에서 1회 수행 (중복 방지)

                await self._sync_portfolio()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[US 동기화] 오류: {e}")

            await asyncio.sleep(eng._position_sync_sec)

    async def _sync_portfolio(self):
        """KIS 잔고와 로컬 포트폴리오 동기화 (단일 API 호출)"""
        eng = self.engine
        if not eng.broker:
            return

        balance = await eng.broker.get_balance()
        if not balance:
            return

        # 계좌 정보 동기화
        account_info = balance.get("account", {})
        if account_info:
            cash_val = account_info.get("available_cash")
            if cash_val is not None and float(cash_val) >= 0:  # 0도 유효 (전액 투자 상태)
                eng.portfolio.cash = Decimal(str(cash_val))
            equity_val = account_info.get("total_equity")
            if equity_val is not None and float(equity_val) > 0:
                # initial_capital은 최초 1회만 설정 (이후 덮어쓰기 방지)
                if eng.portfolio.initial_capital is None or eng.portfolio.initial_capital == Decimal("0"):
                    eng.portfolio.initial_capital = Decimal(str(equity_val))
                    logger.info(f"[US 동기화] initial_capital 설정: ${equity_val}")

        # highest_price 캐시 로드
        hp_cache = self._load_highest_prices()
        et_cache, strat_cache = self._load_cached_metadata()
        _need_restore_stages = not getattr(self, '_exit_stages_restored', False)

        # 포지션
        kis_positions = balance.get("positions", [])
        kis_symbols = {kp["symbol"] for kp in kis_positions}

        # ── DB 미결 US 거래 reconcile (봇 재시작 후 portfolio 공백 보완) ──
        # KIS에 없고 portfolio에도 없고 pending도 아닌 open trade → sync_reconcile 청산
        if not getattr(self, '_ghost_reconcile_done', False):
            self._ghost_reconcile_done = True
            await self._reconcile_ghost_us_trades(kis_symbols, kis_positions)

        # ── 전략 프리로드 (재시작 후 _symbol_strategy 빈 경우 DB에서 복원) ──
        if not getattr(self, '_strategy_preload_done', False):
            self._strategy_preload_done = True
            await self._preload_strategies_from_db()

        for kp in kis_positions:
            symbol = kp["symbol"]
            kis_symbols.add(symbol)

            if symbol in eng.portfolio.positions:
                # 기존 포지션 업데이트
                pos = eng.portfolio.positions[symbol]
                new_qty = int(kp["qty"])
                old_qty = self._prev_qty_snapshot.get(symbol, pos.quantity)

                if symbol not in eng._pending_symbols:
                    pos.quantity = new_qty

                    # 수량 감소 감지 → 부분/전량 매도 기록 (체결 누락 보완)
                    if new_qty < old_qty and eng.trade_storage:
                        sold_qty = old_qty - new_qty
                        exit_price = float(kp["current_price"])
                        trade_id = getattr(pos, 'trade_id', None)
                        if trade_id:
                            eng.trade_storage.record_exit(
                                trade_id=trade_id,
                                exit_price=exit_price,
                                exit_quantity=sold_qty,
                                exit_reason="sync_detected",
                                exit_type="sync_partial",
                                avg_entry_price=float(pos.avg_price),
                            )
                        logger.info(
                            f"[US 동기화] {symbol} 수량 감소 감지: "
                            f"{old_qty}→{new_qty}주 (매도 {sold_qty}주 기록)"
                        )
                        # 매도 실패 쿨다운 해제 (실제로 체결됨)
                        self._sell_fail_cooldown.pop(symbol, None)

                pos.avg_price = Decimal(str(kp["avg_price"]))
                pos.current_price = Decimal(str(kp["current_price"]))
                eng._exchange_cache[symbol] = kp.get("exchange", eng._default_exchange)

                # ExitManager에 미등록된 기존 포지션 등록 (재시작 후 누락 방지)
                if symbol not in eng.exit_manager._states:
                    try:
                        eng.exit_manager.register_position(pos)
                        logger.info(f"[US 동기화] {symbol} ExitManager 재등록 (재시작 복구)")
                    except Exception as e:
                        logger.warning(f"[US 동기화] {symbol} ExitManager 재등록 실패: {e}")
            else:
                # 새 포지션 (외부 진입 또는 체결 반영)
                cached_hp = hp_cache.get(symbol, 0.0)
                cur_price = float(kp["current_price"])
                restored_hp = max(cached_hp, cur_price)

                # ── 기존 오픈 DB 레코드 정리 (재시작 중복 SYNC_ 누적 방지) ──
                # 동일 종목의 미결 레코드를 모두 close 후 신규 SYNC_ 1건 생성
                if eng.trade_storage and eng.trade_storage._db_available and eng.trade_storage.pool:
                    try:
                        async with eng.trade_storage.pool.acquire() as _conn:
                            _old_recs = await _conn.fetch(
                                """SELECT id, entry_price, entry_quantity FROM trades
                                   WHERE market='US' AND symbol=$1 AND exit_time IS NULL""",
                                symbol,
                            )
                        if _old_recs:
                            _now = datetime.now()
                            _exit_p = cur_price or float(kp.get("avg_price") or 0)
                            for _rec in _old_recs:
                                _ep = float(_rec["entry_price"] or 0)
                                _eq = int(_rec["entry_quantity"] or 0)
                                _pnl = round((_exit_p - _ep) * _eq, 2)
                                _pct = round((_exit_p - _ep) / _ep * 100, 2) if _ep > 0 else 0
                                eng.trade_storage._enqueue(
                                    """UPDATE trades SET exit_time=$1, exit_price=$2,
                                       exit_quantity=$3, exit_reason=$4, exit_type=$5,
                                       pnl=$6, pnl_pct=$7, updated_at=$8
                                       WHERE id=$9 AND exit_time IS NULL""",
                                    (_now, _exit_p, _eq,
                                     "sync_qty_reconcile (재시작 중복 레코드 정리)", "sync_reconcile",
                                     _pnl, _pct, _now, _rec["id"]),
                                )
                            logger.info(
                                f"[US 동기화] {symbol} 기존 오픈 레코드 {len(_old_recs)}건 정리 후 신규 SYNC_ 생성"
                            )
                    except Exception as _e:
                        logger.warning(f"[US 동기화] {symbol} 기존 레코드 정리 오류: {_e}")

                sync_trade_id = f"SYNC_{symbol}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
                # entry_time 복원 (캐시 → 메모리 캐시 → 현재)
                restored_et = None
                if symbol in et_cache:
                    try:
                        restored_et = datetime.fromisoformat(et_cache[symbol])
                    except Exception as e:
                        logger.debug(f"[US 동기화] {symbol} entry_time 파싱 실패: {e}")
                if not restored_et:
                    restored_et = datetime.now()
                eng.portfolio.positions[symbol] = Position(
                    symbol=symbol,
                    name=kp.get("name", ""),
                    side=PositionSide.LONG,
                    quantity=int(kp["qty"]),
                    avg_price=Decimal(str(kp["avg_price"])),
                    current_price=Decimal(str(cur_price)),
                    highest_price=Decimal(str(restored_hp)),
                    entry_time=restored_et,
                    trade_id=sync_trade_id,
                )
                # 전략 복원 (메모리 캐시 → 파일 캐시)
                restored_strat = eng._symbol_strategy.get(symbol)
                if restored_strat is None:
                    restored_strat = strat_cache.get(symbol)
                if restored_strat:
                    pos = eng.portfolio.positions[symbol]
                    pos.strategy = restored_strat
                    eng._symbol_strategy[symbol] = restored_strat
                    for strat in eng.strategies:
                        if strat.strategy_type.value == pos.strategy:
                            pos.time_horizon = strat.time_horizon
                            break
                    logger.info(f"[US 동기화] {symbol} 전략 복원: {pos.strategy}")
                if cached_hp > cur_price:
                    logger.info(
                        f"[US 동기화] {symbol} highest_price 복원: "
                        f"${cached_hp:.2f} (현재가 ${cur_price:.2f})"
                    )
                eng._exchange_cache[symbol] = kp.get("exchange", eng._default_exchange)

                # ExitManager에 포지션 등록 (sync 포지션은 보수적 리스크 적용)
                new_pos = eng.portfolio.positions[symbol]
                try:
                    # sync_detected 포지션: 타이트한 손절/짧은 유예
                    eng.exit_manager.register_position(
                        new_pos,
                        stop_loss_pct=3.0,
                        trailing_stop_pct=2.0,
                        first_exit_pct=3.0,
                        second_exit_pct=5.0,
                        third_exit_pct=8.0,
                        stale_high_days=2,
                    )
                except Exception as e:
                    logger.debug(f"[US 동기화] {symbol} ExitManager 등록 실패: {e}")

                # TradeStorage entry 기록 (체결 누락 보완)
                if eng.trade_storage and symbol not in self._prev_qty_snapshot:
                    # pending 주문에서 signal_score/strategy 복원 (fill_ws=off 상황 보완)
                    _sync_score = 0
                    _sync_strat = restored_strat or "unknown"
                    for _pend in eng._pending_orders.values():
                        if _pend.get("symbol") == symbol and _pend.get("side") == "buy":
                            _sync_score = _pend.get("signal_score", 0)
                            if _pend.get("strategy"):
                                _sync_strat = _pend["strategy"]
                            break
                    eng.trade_storage.record_entry(
                        trade_id=sync_trade_id,
                        symbol=symbol,
                        name=kp.get("name", ""),
                        entry_price=float(kp["avg_price"]),
                        entry_quantity=int(kp["qty"]),
                        entry_reason="sync_detected",
                        entry_strategy=_sync_strat,
                        signal_score=_sync_score,
                        indicators=None,
                        market="US",
                    )
                    logger.info(f"[US 동기화] {symbol} 신규 포지션 entry 기록 (sync)")

        # exit_stages 복원 (포지션 등록 후 실행 — _states 채워진 뒤 stage 복원)
        if _need_restore_stages:
            stages_cache = self._load_exit_stages()
            if stages_cache:
                eng.exit_manager.restore_stages(stages_cache)
                logger.info(f"[US 동기화] exit_stages 최초 복원: {len(stages_cache)}개")
            self._exit_stages_restored = True

        # highest_price 캐시 저장 (재시작 대비)
        self._save_highest_prices()

        # API 실패 방어: 빈 응답으로 기존 포지션이 잘못 삭제되는 것을 방지
        local_count = len(eng.portfolio.positions)
        if not kis_positions and local_count > 0:
            if not account_info:
                logger.warning(
                    f"[US 동기화] API 실패 추정 (포지션 0건 + 계좌정보 없음) — "
                    f"로컬 {local_count}개 포지션 보존"
                )
                return
            # 로컬에 2개 이상 있는데 KIS가 0건 → API 일시 오류 가능성
            if local_count >= 2:
                logger.warning(
                    f"[US 동기화] 포지션 급감 의심 (KIS 0건 vs 로컬 {local_count}개) — "
                    f"이번 사이클 보존 (다음 동기화에서 재확인)"
                )
                return

        # KIS에 없는 포지션 → 청산 처리
        for symbol in list(eng.portfolio.positions.keys()):
            if symbol not in kis_symbols:
                if symbol in eng._pending_symbols:
                    # 매수 pending만 보존 (매도 assumed-filled는 포지션 정리 진행)
                    has_pending_buy = any(
                        o.get("side") == "buy" and o.get("symbol") == symbol
                        for o in eng._pending_orders.values()
                    )
                    if has_pending_buy:
                        logger.debug(f"[US 동기화] {symbol} — KIS에 없지만 pending 매수 주문 있어 유지")
                        continue
                    # pending_buy 없음 → 매도 assumed-filled 상태, 포지션 정리 진행
                pos = eng.portfolio.positions.pop(symbol)
                # daily_pnl은 아래 trade.pnl에서 한 번만 가산 (이중 가산 방지)
                eng.exit_manager.on_position_closed(symbol)
                eng._pending_symbols.discard(symbol)
                eng._ws_last_exit_check.pop(symbol, None)

                # 해당 종목 pending 매도 주문 즉시 제거 (history=0 로 2분 대기하던 문제 해결)
                # _sync_portfolio가 KIS 잔고에서 제거됨을 확인 → 매도 체결로 간주
                _sell_pend_nos = [
                    no for no, o in list(eng._pending_orders.items())
                    if o.get("symbol") == symbol and o.get("side") == "sell"
                ]
                for _no in _sell_pend_nos:
                    eng._pending_orders.pop(_no, None)
                    logger.info(f"[US 동기화] {symbol} 매도 pending 즉시 해제 (주문번호={_no})")

                # WS 구독 해제 → 포지션 없으면 WS 종료
                if eng.us_price_ws:
                    await eng.us_price_ws.unsubscribe([symbol])
                if eng.ws_feed:
                    await eng.ws_feed.unsubscribe([symbol])
                await self._maybe_stop_us_price_ws()
                logger.info(f"[US 동기화] {symbol} 포지션 청산 확인 (KIS에서 제거됨)")

                # 거래 기록
                trade = TradeResult(
                    symbol=symbol,
                    side=OrderSide.SELL,
                    entry_price=pos.avg_price,
                    exit_price=pos.current_price,
                    quantity=pos.quantity,
                    entry_time=pos.entry_time or datetime.now(),
                    exit_time=datetime.now(),
                    strategy=pos.strategy or "unknown",
                    reason="sync_closed",
                )
                # TradeStorage DB 기록 (journal.record_exit도 내부에서 호출됨)
                trade_id = getattr(pos, 'trade_id', None)
                if trade_id and eng.trade_storage:
                    eng.trade_storage.record_exit(
                        trade_id=trade_id,
                        exit_price=float(pos.current_price),
                        exit_quantity=pos.quantity,
                        exit_reason="sync_closed",
                        exit_type="sync_closed",
                        avg_entry_price=float(pos.avg_price),
                    )

                # daily_pnl 갱신
                eng.portfolio.daily_pnl += trade.pnl
                eng.portfolio.daily_trades += 1

        # 수량 스냅샷 갱신 (다음 sync에서 변화 감지용)
        self._prev_qty_snapshot = {
            sym: pos.quantity
            for sym, pos in eng.portfolio.positions.items()
        }

    # ============================================================
    # 태스크 4: 주문 상태 체크
    # ============================================================

    async def order_check_loop(self):
        """미체결 주문 상태 폴링"""
        eng = self.engine

        # 시작 시 KIS 미체결 주문 복원 (1회)
        try:
            await self._recover_pending_orders()
        except Exception as e:
            logger.warning(f"[US 주문 복원] 실패: {e}")

        while eng.running:
            try:
                if eng._pending_orders:
                    await self._check_orders()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[US 주문 체크] 오류: {e}")

            await asyncio.sleep(eng._order_check_sec)

    async def _recover_pending_orders(self):
        """재시작 시 KIS 미체결 주문 복원 → _pending_orders에 등록, 고아 주문 취소"""
        eng = self.engine
        if not eng.broker:
            return

        # KIS API는 KST 날짜 기준
        now_kst = datetime.now()
        today_kst = now_kst.strftime("%Y%m%d")
        yesterday_kst = (now_kst - timedelta(days=1)).strftime("%Y%m%d")

        # 1차: inquire-ccnl (체결/미체결 통합 조회)
        history = await eng.broker.get_order_history(
            start_date=yesterday_kst, end_date=today_kst
        )

        # 2차 폴백: inquire-nccs (미체결 전용 조회) — history 비어있을 때
        if not history:
            logger.info("[US 주문 복원] inquire-ccnl 빈 결과 → inquire-nccs 폴백 시도")
            try:
                outstanding = await eng.broker.get_outstanding_orders()
                if outstanding:
                    history = outstanding
            except Exception as e:
                logger.warning(f"[US 주문 복원] inquire-nccs 조회 실패: {e}")

        if not history:
            logger.info("[US 주문 복원] 미체결 주문 없음")
            return

        recovered = 0
        for h in history:
            if h["status"] not in ("pending", "partial"):
                continue
            order_no = h["order_no"]
            if order_no in eng._pending_orders:
                continue

            symbol = h["symbol"]
            side = h["side"]

            # 매수 주문인데 이미 포지션에 있으면 체결된 것 → 복원 불필요, 취소 시도
            if side == "buy" and symbol in eng.portfolio.positions:
                logger.info(
                    f"[US 주문 복원] {order_no} 매수 {symbol} — "
                    f"포지션에 이미 존재, 고아 주문 취소 시도"
                )
                try:
                    await eng.broker.cancel_order(
                        order_no, h.get("exchange", eng._default_exchange),
                        symbol, h["qty"],
                    )
                except Exception as e:
                    logger.debug(f"[US 주문 복원] {order_no} 고아 주문 취소 실패: {e}")
                continue

            # 매도 주문인데 포지션이 아직 원래 수량 → 미체결 매도, 취소 + stage 롤백
            if side == "sell":
                logger.warning(
                    f"[US 주문 복원] {order_no} 매도 {symbol} {h['qty']}주 — "
                    f"고아 매도 주문 발견, 취소 시도"
                )
                try:
                    cancel_result = await eng.broker.cancel_order(
                        order_no, h.get("exchange", eng._default_exchange),
                        symbol, h["qty"],
                    )
                    if cancel_result.get("success"):
                        logger.info(f"[US 주문 복원] {symbol} 고아 매도 취소 완료")
                        # stage 롤백 (매도가 체결되지 않았으므로)
                        if eng.exit_manager:
                            eng.exit_manager.rollback_stage(symbol)
                            logger.info(f"[US 주문 복원] {symbol} ExitManager stage 롤백")
                    else:
                        logger.warning(
                            f"[US 주문 복원] {symbol} 매도 취소 실패 "
                            f"(이미 체결 추정) — pending에 등록"
                        )
                        # 취소 실패 = 체결 중 → pending에 등록하여 추적
                        eng._pending_orders[order_no] = {
                            "symbol": symbol,
                            "side": side,
                            "qty": h["qty"],
                            "price": h.get("price", 0),
                            "strategy": eng._symbol_strategy.get(symbol, ""),
                            "reason": "recovered",
                            "exit_type": "",
                            "exchange": h.get("exchange", eng._default_exchange),
                            "submitted_at": now_kst,
                        }
                        eng._pending_symbols.add(symbol)
                        recovered += 1
                except Exception as e:
                    logger.warning(f"[US 주문 복원] {symbol} 취소 시도 예외: {e}")
                continue

            # 주문 시각 복원 (KIS ORD_TMD는 KST HHMMSS)
            submitted_at = datetime.now()
            order_time_str = h.get("time", "")
            if len(order_time_str) >= 6:
                try:
                    hh, mm, ss = int(order_time_str[:2]), int(order_time_str[2:4]), int(order_time_str[4:6])
                    today = now_kst.date()
                    submitted_at = datetime(today.year, today.month, today.day, hh, mm, ss)
                    # 미래 시각으로 설정되면 하루 전으로 보정
                    if submitted_at > now_kst:
                        submitted_at = submitted_at - timedelta(days=1)
                except Exception as e:
                    logger.debug(f"[US 주문 복원] 주문시각 파싱 실패: {e}")

            eng._pending_orders[order_no] = {
                "symbol": symbol,
                "side": side,
                "qty": h["qty"],
                "price": h.get("price", 0),
                "strategy": eng._symbol_strategy.get(symbol, ""),
                "reason": "recovered",
                "exit_type": "",
                "exchange": h.get("exchange", eng._default_exchange),
                "submitted_at": submitted_at,
            }
            eng._pending_symbols.add(symbol)
            recovered += 1
            logger.info(
                f"[US 주문 복원] {order_no} {side} {symbol} "
                f"{h['qty']}주 @ ${h.get('price', 0)} ({h['status']})"
            )

        if recovered:
            logger.info(f"[US 주문 복원] 총 {recovered}건 미체결 주문 복원 완료")

    async def _check_orders(self):
        """체결 내역 조회 → 체결 처리"""
        eng = self.engine

        # KIS API는 KST 날짜 기준 (ET 날짜와 불일치 가능)
        # 미국장은 KST 22:30~05:00으로 날짜가 걸침 → 전일 포함 조회
        now_kst = datetime.now()
        today_kst = now_kst.strftime("%Y%m%d")
        yesterday_kst = (now_kst - timedelta(days=1)).strftime("%Y%m%d")
        pending_count = len(eng._pending_orders)
        history = await eng.broker.get_order_history(start_date=yesterday_kst, end_date=today_kst)
        logger.info(
            f"[US 주문 체크] pending={pending_count}, history={len(history) if history else 0}, date={yesterday_kst}~{today_kst}"
        )

        # history가 비어있어도 pending 타임아웃은 반드시 체크해야 함
        filled_map = {h["order_no"]: h for h in (history or [])}

        for order_no in list(eng._pending_orders.keys()):
            pending = eng._pending_orders.get(order_no)
            if not pending:
                continue

            info = filled_map.get(order_no)

            # 폴백 order_no(local-xxx)는 KIS 이력에 없음 → 타임아웃 전용 처리
            is_local = order_no.startswith("local-")

            if not info or is_local:
                elapsed = (datetime.now() - pending["submitted_at"]).total_seconds()
                symbol = pending["symbol"]
                side = pending["side"]

                # 포트폴리오 기반 체결 감지: 매수 주문인데 이미 포지션에 있으면 체결된 것
                if side == "buy" and symbol in eng.portfolio.positions and elapsed > 30:
                    logger.info(
                        f"[US 주문 체크] {order_no} ({symbol}) "
                        f"매수 주문 — 포지션에 이미 존재, 체결로 간주하여 pending 정리"
                    )
                    eng._pending_symbols.discard(symbol)
                    del eng._pending_orders[order_no]
                    continue

                # 포트폴리오 기반 매도 체결 감지: 부분/전량 매도 후 수량 감소 확인
                if side == "sell" and elapsed > 30:
                    pos = eng.portfolio.positions.get(symbol)
                    expected_qty = pending.get("qty", 0)
                    if pos is None:
                        # 전량 매도 체결 — 포지션 소멸
                        logger.info(
                            f"[US 주문 체크] {order_no} ({symbol}) "
                            f"매도 주문 — 포지션 소멸, 체결로 간주하여 pending 정리"
                        )
                        eng._pending_symbols.discard(symbol)
                        del eng._pending_orders[order_no]
                        continue
                    elif expected_qty and pos.quantity < pending.get("orig_qty", pos.quantity + expected_qty):
                        # 부분 매도 체결 — 수량 감소 확인 (orig_qty가 있을 때만)
                        logger.info(
                            f"[US 주문 체크] {order_no} ({symbol}) "
                            f"매도 주문 — 수량 감소 확인 ({pos.quantity}주 남음), 체결로 간주"
                        )
                        eng._pending_symbols.discard(symbol)
                        del eng._pending_orders[order_no]
                        continue

                # 타임아웃: 매도 2분 / 매수 10분 (local 주문은 5분)
                if is_local:
                    timeout_sec = 300
                else:
                    timeout_sec = 120 if side == "sell" else 600
                side_label = "매도" if side == "sell" else "매수"

                if elapsed > timeout_sec:
                    cancel_ok = False
                    # 실주문(non-local)은 취소 시도
                    if not is_local:
                        try:
                            cancel_result = await eng.broker.cancel_order(
                                order_no, pending.get("exchange", eng._default_exchange),
                                symbol, pending.get("qty", 0),
                            )
                            cancel_ok = bool(cancel_result.get("success"))
                            if cancel_ok:
                                logger.warning(
                                    f"[US 주문 체크] {order_no} ({symbol}) "
                                    f"{side_label} 이력 미확인 {int(timeout_sec / 60)}분 경과 — 취소 완료"
                                )
                            else:
                                # 취소 실패 = 이미 체결됨 (다음 포트폴리오 동기화에서 반영)
                                logger.warning(
                                    f"[US 주문 체크] {order_no} ({symbol}) "
                                    f"{side_label} 취소 실패 (이미 체결 추정) — pending 제거"
                                )
                        except Exception as e:
                            logger.warning(
                                f"[US 주문 체크] {order_no} ({symbol}) 취소 시도 예외: {e}"
                            )
                    else:
                        cancel_ok = True
                        logger.warning(
                            f"[US 주문 체크] {order_no} ({symbol}) "
                            f"폴백주문 타임아웃 ({int(timeout_sec / 60)}분) — 제거"
                        )
                    # 매도 assumed-filled: _pending_symbols 유지 → _exit_check_loop 재진입 차단
                    # _sync_portfolio가 KIS 포지션 확인 후 _pending_symbols discard
                    if side == "sell" and not cancel_ok:
                        del eng._pending_orders[order_no]
                        # _pending_symbols는 유지 (재매도 이중 주문 차단)
                    else:
                        eng._pending_symbols.discard(symbol)
                        del eng._pending_orders[order_no]

                    # 매도 주문 취소 시 ExitManager stage 롤백 + 적극 지정가 재시도
                    if side == "sell" and cancel_ok:
                        if eng.exit_manager:
                            eng.exit_manager.rollback_stage(symbol)
                            logger.info(
                                f"[US 주문 체크] {symbol} 매도 취소 → stage 롤백"
                            )
                        # 폴백 재시도 횟수 제한 (최대 3회)
                        _retry_key = f"sell_fallback_{symbol}"
                        _retry_cnt = getattr(self, '_sell_retry_count', {})
                        if not hasattr(self, '_sell_retry_count'):
                            self._sell_retry_count = {}
                            _retry_cnt = self._sell_retry_count
                        _retry_cnt[symbol] = _retry_cnt.get(symbol, 0) + 1

                        if _retry_cnt[symbol] > 3:
                            logger.error(
                                f"[US 주문 체크] {symbol} 매도 폴백 {_retry_cnt[symbol]}회 초과 → "
                                f"폴백 중단 (수동 확인 필요)"
                            )
                            self._sell_fail_cooldown[symbol] = datetime.now()
                        else:
                            # 정규장이면 적극적 지정가로 재시도 (KIS US는 시장가 미지원)
                            p_exchange = pending.get("exchange", eng._default_exchange)
                            p_qty = pending.get("qty", 0)
                            if eng.session.is_market_open() and p_qty > 0:
                                # 현재가 조회 후 -2% 지정가 (빠른 체결 유도)
                                try:
                                    _quote = await eng.broker.get_quote(symbol, p_exchange)
                                    _fallback_price = _quote.get("price", 0)
                                except Exception:
                                    _fallback_price = 0
                                if _fallback_price <= 0:
                                    _fallback_price = pending.get("price", 0)
                                _fallback_price = round(_fallback_price * 0.98, 2)  # -2% 적극적 지정가
                                logger.warning(
                                    f"[US 주문 체크] {symbol} 매도 적극지정가 폴백 "
                                    f"({_retry_cnt[symbol]}/3) — {p_qty}주 @ ${_fallback_price:.2f}"
                                )
                                try:
                                    fallback = await eng.broker.submit_sell_order(
                                        symbol, p_exchange, p_qty, price=_fallback_price,
                                    )
                                    if fallback.get("success"):
                                        fb_order_no = fallback.get("order_no", "").strip()
                                        if not fb_order_no:
                                            fb_order_no = f"local-{uuid.uuid4().hex[:12]}"
                                        eng._pending_orders[fb_order_no] = {
                                            "symbol": symbol,
                                            "side": "sell",
                                            "qty": p_qty,
                                            "price": _fallback_price,
                                            "strategy": pending.get("strategy", ""),
                                            "reason": f"sell_fallback_{_retry_cnt[symbol]}({pending.get('reason', '')})",
                                            "exchange": p_exchange,
                                            "submitted_at": datetime.now(),
                                        }
                                        eng._pending_symbols.add(symbol)
                                    else:
                                        logger.error(
                                            f"[US 주문 체크] {symbol} 적극지정가 폴백 실패: "
                                            f"{fallback.get('message')}"
                                        )
                                except Exception as e:
                                    logger.error(f"[US 주문 체크] {symbol} 적극지정가 폴백 예외: {e}")
                continue

            if info["status"] == "filled":
                await self._on_order_filled(order_no, info)
            elif info["status"] == "partial":
                elapsed = (datetime.now() - pending["submitted_at"]).total_seconds()
                logger.debug(
                    f"[US 주문 체크] {order_no} 부분체결 "
                    f"({info['filled_qty']}/{info['qty']}, {int(elapsed)}초 경과)"
                )
                # 부분체결 타임아웃: 매도 3분, 매수 15분
                partial_timeout = 180 if pending["side"] == "sell" else 900
                if elapsed > partial_timeout:
                    logger.warning(
                        f"[US 주문 체크] {order_no} ({pending['symbol']}) "
                        f"부분체결 타임아웃 ({int(partial_timeout / 60)}분) — 잔여 취소"
                    )
                    cancel_result = await eng.broker.cancel_order(
                        order_no, pending.get("exchange", eng._default_exchange),
                        pending["symbol"], pending.get("qty", 0),
                    )
                    if cancel_result.get("success"):
                        # 체결된 분량 반영 (filled로 처리)
                        info["status"] = "filled"
                        info["filled_qty"] = info.get("filled_qty", 0)
                        info["filled_price"] = info.get("filled_price", pending.get("price", 0))
                        if info["filled_qty"] > 0:
                            await self._on_order_filled(order_no, info)
                        else:
                            eng._pending_symbols.discard(pending["symbol"])
                            del eng._pending_orders[order_no]
            elif info["status"] == "pending":
                elapsed = (datetime.now() - pending["submitted_at"]).total_seconds()
                # 매도(손절)는 2분, 매수는 10분 타임아웃
                timeout_sec = 120 if pending["side"] == "sell" else 600
                side_label = "매도" if pending["side"] == "sell" else "매수"

                if elapsed > timeout_sec:
                    logger.warning(
                        f"[US 주문 체크] {order_no} ({pending['symbol']}) "
                        f"{side_label} 미체결 {int(timeout_sec / 60)}분 경과 — 자동 취소"
                    )
                    cancel_result = await eng.broker.cancel_order(
                        order_no, pending.get("exchange", eng._default_exchange),
                        pending["symbol"], pending.get("qty", 0),
                    )
                    if cancel_result.get("success"):
                        eng._pending_symbols.discard(pending["symbol"])
                        del eng._pending_orders[order_no]
                        logger.info(f"[US 주문 체크] {order_no} 취소 완료")

                        # 매도 취소 후 적극지정가 폴백 재주문 (정규장에서만, 최대 3회)
                        if pending["side"] == "sell":
                            symbol = pending["symbol"]
                            p_exchange = pending.get("exchange", eng._default_exchange)
                            p_qty = pending.get("qty", 0)

                            # 폴백 재시도 횟수 제한
                            if not hasattr(self, '_sell_retry_count'):
                                self._sell_retry_count = {}
                            self._sell_retry_count[symbol] = self._sell_retry_count.get(symbol, 0) + 1

                            if not eng.session.is_market_open():
                                logger.warning(
                                    f"[US 주문 체크] {symbol} 정규장 아님 → 적극지정가 폴백 스킵"
                                )
                                eng._pending_symbols.discard(symbol)
                            elif self._sell_retry_count[symbol] > 3:
                                logger.error(
                                    f"[US 주문 체크] {symbol} 매도 폴백 "
                                    f"{self._sell_retry_count[symbol]}회 초과 → 중단 (수동 확인 필요)"
                                )
                                eng._pending_symbols.discard(symbol)
                                self._sell_fail_cooldown[symbol] = datetime.now()
                                asyncio.create_task(send_alert(
                                    f"[US] 긴급: 매도 폴백 3회 초과\n"
                                    f"{symbol} {p_qty}주 — 수동 확인 필요"
                                ))
                            else:
                                # 현재가 조회 후 -2% 적극적 지정가 (KIS US 시장가 미지원)
                                try:
                                    _q = await eng.broker.get_quote(symbol, p_exchange)
                                    _fp = _q.get("price", 0)
                                except Exception:
                                    _fp = 0
                                if _fp <= 0:
                                    _fp = pending.get("price", 0)
                                _fp = round(_fp * 0.98, 2)
                                logger.warning(
                                    f"[US 주문 체크] {symbol} 매도 적극지정가 폴백 "
                                    f"({self._sell_retry_count[symbol]}/3) — {p_qty}주 @ ${_fp:.2f}"
                                )
                                fallback = await eng.broker.submit_sell_order(
                                    symbol, p_exchange, p_qty, price=_fp,
                                )
                                if fallback.get("success"):
                                    fb_order_no = fallback.get("order_no", "").strip()
                                    if not fb_order_no:
                                        fb_order_no = f"local-{uuid.uuid4().hex[:12]}"
                                    eng._pending_orders[fb_order_no] = {
                                        "symbol": symbol,
                                        "side": "sell",
                                        "qty": p_qty,
                                        "price": _fp,
                                        "strategy": pending.get("strategy", ""),
                                        "reason": f"sell_fallback_{self._sell_retry_count[symbol]}({pending.get('reason', '')})",
                                        "exchange": p_exchange,
                                        "submitted_at": datetime.now(),
                                    }
                                    eng._pending_symbols.add(symbol)
                                else:
                                    logger.error(
                                        f"[US 주문 체크] {symbol} 적극지정가 폴백 실패: "
                                        f"{fallback.get('message')}"
                                    )
                                    # 긴급 알림
                                    asyncio.create_task(send_alert(
                                        f"[US] 긴급: 매도 실패\n"
                                        f"{symbol} {p_qty}주 — 지정가 취소 + 폴백 모두 실패\n"
                                        f"수동 확인 필요"
                                    ))
                    else:
                        logger.error(
                            f"[US 주문 체크] {order_no} 취소 실패: "
                            f"{cancel_result.get('message')}"
                        )
                        # 취소 실패해도 pending 해제 (이미 체결되었을 수 있음)
                        eng._pending_symbols.discard(pending["symbol"])
                        del eng._pending_orders[order_no]
                        logger.warning(f"[US 주문 체크] {order_no} — 취소 실패, pending 강제 해제")

    async def _on_order_filled(self, order_no: str, fill_info: dict):
        """주문 체결 처리"""
        eng = self.engine
        pending = eng._pending_orders.pop(order_no, None)
        if not pending:
            return

        symbol = pending["symbol"]
        side = pending["side"]
        _fp = fill_info.get("filled_price", 0)
        filled_price = float(_fp if _fp is not None else 0)
        _fq = fill_info.get("filled_qty", 0)
        filled_qty = int(_fq if _fq is not None else 0)

        eng._pending_symbols.discard(symbol)

        if side == "buy":
            logger.info(
                f"[US 체결] 매수 {symbol} {filled_qty}주 @ ${filled_price:.2f} "
                f"(전략: {pending.get('strategy', '')})"
            )
            # trade_id 생성 + TradeStorage 기록
            trade_id = f"{symbol}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"

            # 포지션에 전략/시간지평 세팅
            pos = eng.portfolio.positions.get(symbol)
            if not pos:
                # sync 미실행 시 포지션이 아직 없음 → 직접 생성
                pos = Position(
                    symbol=symbol,
                    name=pending.get("name", ""),
                    side=PositionSide.LONG,
                    quantity=filled_qty,
                    avg_price=Decimal(str(filled_price)),
                    current_price=Decimal(str(filled_price)),
                    highest_price=Decimal(str(filled_price)),
                    entry_time=datetime.now(),
                )
                eng.portfolio.positions[symbol] = pos
                logger.info(f"[US 체결] {symbol} — sync 전 포지션 직접 생성")
            else:
                # sync에서 먼저 생성된 포지션 — 체결 정보로 수량/평균가 갱신
                if pos.quantity != filled_qty or pos.avg_price != Decimal(str(filled_price)):
                    logger.info(
                        f"[US 체결] {symbol} 포지션 갱신: "
                        f"{pos.quantity}→{filled_qty}주, "
                        f"${pos.avg_price}→${filled_price:.2f}"
                    )
                    pos.quantity = filled_qty
                    pos.avg_price = Decimal(str(filled_price))
                    pos.current_price = Decimal(str(filled_price))

            pos.trade_id = trade_id
            pos.strategy = pending.get("strategy", "")
            # 메모리 캐시에 기록 (재시작 후 sync 복원용)
            if pos.strategy:
                eng._symbol_strategy[symbol] = pos.strategy
            # 섹터 설정 (섹터 다각화 체크용)
            if symbol in eng._sector_cache:
                pos.sector = eng._sector_cache[symbol]
            # highest_price 초기화
            if pos.highest_price is None:
                pos.highest_price = pos.current_price
            # 전략의 time_horizon 찾기
            for strat in eng.strategies:
                if strat.strategy_type.value == pos.strategy:
                    pos.time_horizon = strat.time_horizon
                    break

            # ExitManager에 포지션 등록
            try:
                eng.exit_manager.register_position(pos)
            except Exception as e:
                logger.debug(f"[US 체결] {symbol} ExitManager 등록 실패: {e}")

            # TradeStorage DB + 캐시 기록
            if eng.trade_storage:
                _raw_score = pending.get("signal_score", 0)
                _score = float(_raw_score if _raw_score is not None else 0)
                _strat = pending.get("strategy", "")
                # sync 경로가 이미 SYNC_ entry를 만들었으면 score/strategy 업데이트 (중복 방지)
                _sync_trade_id = None
                if eng.trade_storage.pool:
                    try:
                        _row = await eng.trade_storage.pool.fetchrow(
                            """SELECT id FROM trades
                               WHERE symbol=$1 AND market='US' AND exit_time IS NULL
                                 AND entry_reason='sync_detected'
                               ORDER BY entry_time DESC LIMIT 1""",
                            symbol,
                        )
                        if _row:
                            _sync_trade_id = _row["id"]
                    except Exception as _e:
                        logger.debug(
                            f"[US 체결] {symbol} SYNC_ entry 조회 실패: {_e} → 신규 entry 생성"
                        )

                if _sync_trade_id:
                    # SYNC_ 레코드를 실제 체결 정보로 보강 (id PK 유지)
                    eng.trade_storage._enqueue(
                        """UPDATE trades
                           SET entry_signal_score=$1, entry_strategy=$2,
                               entry_price=$3, entry_quantity=$4,
                               entry_reason=$5, updated_at=NOW()
                           WHERE id=$6""",
                        (_score, _strat or "sepa_trend",
                         float(filled_price), filled_qty,
                         pending.get("reason", "fill_confirmed"),
                         _sync_trade_id),
                    )
                    trade_id = _sync_trade_id  # 기존 SYNC_ id로 통일
                    logger.debug(
                        f"[US 체결] {symbol} SYNC_ entry 보강: score={_score:.0f}, "
                        f"strat={_strat}, id={_sync_trade_id}"
                    )
                else:
                    eng.trade_storage.record_entry(
                        trade_id=trade_id,
                        symbol=symbol,
                        name=pos.name,
                        entry_price=float(filled_price),
                        entry_quantity=filled_qty,
                        entry_reason=pending.get("reason", ""),
                        entry_strategy=_strat,
                        signal_score=_score,
                        indicators=eng._indicator_cache.get(symbol),
                        market="US",
                    )
                # pos에 trade_id 반영
                pos.trade_id = trade_id

            # WS 시작 (없을 경우) + 구독
            exchange = await self._get_exchange(symbol)
            if eng.us_price_ws:
                await self._ensure_us_price_ws_running()
                await asyncio.sleep(0.5)  # 연결 대기 (짧게)
                await eng.us_price_ws.subscribe([symbol], exchange=exchange)
            if eng.ws_feed:
                await eng.ws_feed.subscribe([symbol])

            # 텔레그램 매수 체결 알림
            asyncio.create_task(send_alert(
                f"[US] 매수 체결\n"
                f"{symbol} {filled_qty}주 @ ${filled_price:.2f}\n"
                f"전략: {pending.get('strategy', '')}\n"
                f"점수: {pending.get('signal_score', 0):.0f}",
            ))

        else:
            pos = eng.portfolio.positions.get(symbol)

            logger.info(
                f"[US 체결] 매도 {symbol} {filled_qty}주 @ ${filled_price:.2f} "
                f"(사유: {pending.get('reason', '')})"
            )
            # 매도 폴백 재시도 카운터 초기화
            if hasattr(self, '_sell_retry_count'):
                self._sell_retry_count.pop(symbol, None)

            # 텔레그램 매도 체결 알림
            entry_price = float(pos.avg_price) if pos else 0
            pnl_pct = ((filled_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
            asyncio.create_task(send_alert(
                f"[US] 매도 체결\n"
                f"{symbol} {filled_qty}주 @ ${filled_price:.2f}\n"
                f"수익률: {pnl_pct:+.2f}%\n"
                f"사유: {pending.get('reason', '')}",
            ))
            if pos:
                trade = TradeResult(
                    symbol=symbol,
                    side=OrderSide.SELL,
                    entry_price=pos.avg_price,
                    exit_price=Decimal(str(filled_price)),
                    quantity=filled_qty,
                    entry_time=pos.entry_time or datetime.now(),
                    exit_time=datetime.now(),
                    strategy=pos.strategy or pending.get("strategy", ""),
                    reason=pending.get("reason", ""),
                )
                eng.risk_manager.record_trade_result(is_win=trade.is_win)

                # TradeStorage DB 기록
                trade_id = getattr(pos, 'trade_id', None)
                if trade_id and eng.trade_storage:
                    eng.trade_storage.record_exit(
                        trade_id=trade_id,
                        exit_price=float(filled_price),
                        exit_quantity=filled_qty,
                        exit_reason=pending.get("reason", ""),
                        exit_type=pending.get("exit_type", "unknown"),
                        exit_time=datetime.now(),
                        avg_entry_price=float(pos.avg_price),
                    )

                # daily_pnl 갱신
                eng.portfolio.daily_pnl += trade.pnl
                eng.portfolio.daily_trades += 1

                # ExitManager remaining_quantity 즉시 갱신
                eng.exit_manager.on_fill(
                    symbol, filled_qty, Decimal(str(filled_price))
                )

                # 부분매도 시 수량 차감, 전량 매도 시 포지션 정리
                if filled_qty >= pos.quantity:
                    eng.exit_manager.on_position_closed(symbol)
                    eng.portfolio.positions.pop(symbol, None)
                    eng._ws_last_exit_check.pop(symbol, None)
                    if eng.us_price_ws:
                        await eng.us_price_ws.unsubscribe([symbol])
                    if eng.ws_feed:
                        await eng.ws_feed.unsubscribe([symbol])
                    await self._maybe_stop_us_price_ws()
                else:
                    pos.quantity -= filled_qty
                    logger.info(
                        f"[US 체결] {symbol} 부분매도 {filled_qty}주 → 잔여 {pos.quantity}주"
                    )

    # ============================================================
    # 태스크 5: EOD 청산
    # ============================================================

    async def eod_close_loop(self):
        """마감 15분 전 DAY 포지션 청산 + 마감 후 일일 리포트"""
        eng = self.engine
        _daily_report_sent: Optional[date] = None
        _eod_close_done: Optional[date] = None
        _us_sync_done: Optional[date] = None

        while eng.running:
            try:
                if eng.session.is_market_open():
                    minutes_left = eng.session.minutes_to_close()
                    today = eng.session.now_et().date()
                    if 0 < minutes_left <= 15 and _eod_close_done != today:
                        await self._eod_close()
                        _eod_close_done = today
                else:
                    # 장 마감 후 일일 리포트 (1일 1회)
                    today = eng.session.now_et().date()
                    if _daily_report_sent != today and eng.session.is_trading_day():
                        now_et = eng.session.now_et()
                        if now_et.hour == 16 and now_et.minute >= 10:
                            _daily_report_sent = today
                            try:
                                from src.analytics.daily_report import get_report_generator
                                reporter = get_report_generator()
                                await reporter.generate_evening_report(send_telegram=True)
                                logger.info("[US EOD] 일일 리포트 발송 완료")
                            except Exception as e:
                                logger.error(f"[US EOD] 일일 리포트 실패: {e}")

                    # 장 마감 후 KIS 체결 동기화 (16:20 ET 이후, 1일 1회)
                    if _us_sync_done != today and eng.session.is_trading_day():
                        now_et = eng.session.now_et()
                        if now_et.hour == 16 and now_et.minute >= 20:
                            _us_sync_done = today
                            try:
                                if eng.trade_storage and eng.broker:
                                    await eng.trade_storage.sync_from_kis_us(
                                        eng.broker, engine=eng
                                    )
                                    logger.info("[US EOD] KIS 체결 동기화 완료")
                            except Exception as e:
                                logger.error(f"[US EOD] KIS 체결 동기화 실패: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[US EOD] 오류: {e}")

            await asyncio.sleep(30)

    async def _eod_close(self):
        """DAY 타임호라이즌 포지션 전량 청산 (aggressive limit)"""
        eng = self.engine
        day_strategies = {s.strategy_type.value for s in eng.strategies
                         if s.time_horizon == TimeHorizon.DAY}

        for symbol, pos in list(eng.portfolio.positions.items()):
            if symbol in eng._pending_symbols:
                continue

            if pos.strategy in day_strategies or pos.time_horizon == TimeHorizon.DAY:
                exchange = await self._get_exchange(symbol)

                # KIS US API는 시장가(price=0) 미지원 → aggressive limit (-2%)
                sell_price = float(pos.current_price) if pos.current_price and pos.current_price > 0 else 0
                if sell_price <= 0:
                    quote = await eng.broker.get_quote(symbol, exchange)
                    sell_price = quote.get("price", 0)
                if sell_price <= 0:
                    logger.error(f"[US EOD] {symbol} 현재가 조회 실패 → 청산 스킵")
                    continue
                sell_price = round(sell_price * 0.98, 2)  # -2% aggressive limit
                logger.info(f"[US EOD] {symbol} DAY 포지션 청산 @ ${sell_price:.2f} (aggressive limit)")

                result = await eng.broker.submit_sell_order(
                    symbol, exchange, pos.quantity, price=sell_price
                )
                if result.get("success"):
                    order_no = result.get("order_no", "").strip()
                    if not order_no:
                        order_no = f"local-{uuid.uuid4().hex[:12]}"
                    eng._pending_orders[order_no] = {
                        "symbol": symbol, "side": "sell", "qty": pos.quantity,
                        "price": sell_price, "strategy": pos.strategy or "",
                        "reason": "eod_close", "exchange": exchange,
                        "submitted_at": datetime.now(),
                    }
                    eng._pending_symbols.add(symbol)
                else:
                    logger.error(f"[US EOD] {symbol} 청산 실패: {result.get('message')}")

    # ============================================================
    # 태스크 6: Heartbeat
    # ============================================================

    async def heartbeat_loop(self):
        """상태 로깅 + 일일 손실 경고"""
        eng = self.engine
        _daily_loss_alerted = False
        _daily_loss_alert_date: Optional[date] = None

        while eng.running:
            try:
                session_status = eng.session.get_session()
                metrics = eng.risk_manager.get_risk_metrics(eng.portfolio)

                price_ws_status = (
                    f"ok({eng.us_price_ws.subscribed_count})"
                    if (eng.us_price_ws and eng.us_price_ws.is_connected)
                    else "off"
                )
                # fill WS: us_price_ws에 통합(H0GSCNI0 동시구독) — kis_ws는 fallback 참조
                _price_ws_connected = eng.us_price_ws and getattr(eng.us_price_ws, '_connected', False)
                _fill_hts_set = eng.us_price_ws and getattr(eng.us_price_ws, '_fill_hts_id', None)
                fill_ws_status = "ok" if (_price_ws_connected and _fill_hts_set) else (
                    "ok" if (eng.kis_ws and getattr(eng.kis_ws, '_connected', False)) else "off"
                )
                logger.info(
                    f"[US Heartbeat] session={session_status.value} | "
                    f"equity=${eng.portfolio.total_equity:.2f} | "
                    f"cash=${eng.portfolio.cash:.2f} | "
                    f"positions={len(eng.portfolio.positions)} | "
                    f"pending={len(eng._pending_orders)} | "
                    f"price_ws={price_ws_status} | "
                    f"fill_ws={fill_ws_status} | "
                    f"daily_pnl=${metrics.daily_loss:.2f} ({metrics.daily_loss_pct:.1f}%)"
                )

                # 헬스 모니터 (있는 경우)
                if eng.health_monitor and hasattr(eng.health_monitor, 'run_loop'):
                    # HealthMonitor는 별도 태스크로 실행되므로 여기서는 pass
                    pass

                # 일일 손실 경고 (한도의 67% 도달 시, 1일 1회)
                today = eng.session.now_et().date()
                if _daily_loss_alert_date != today:
                    _daily_loss_alerted = False
                    _daily_loss_alert_date = today

                risk_cfg = eng.risk_manager.config
                warn_threshold = float(getattr(risk_cfg, 'daily_max_loss_pct', 3.0)) * 0.67
                if not _daily_loss_alerted and metrics.daily_loss_pct <= -warn_threshold:
                    _daily_loss_alerted = True
                    asyncio.create_task(send_alert(
                        f"[US] 일일 손실 경고\n"
                        f"일일 PnL: ${metrics.daily_loss:.2f} ({metrics.daily_loss_pct:.1f}%)\n"
                        f"보유: {len(eng.portfolio.positions)}개\n"
                        f"현금: ${eng.portfolio.cash:.2f}",
                    ))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[US Heartbeat] 오류: {e}")

            await asyncio.sleep(eng._heartbeat_sec)

    # ============================================================
    # 태스크 7: 스크리너 루프
    # ============================================================

    async def screener_loop(self):
        """유니버스 스크리닝 (60분 주기, 장중만, 순환 스캔)"""
        await asyncio.sleep(30)  # 초기 대기
        eng = self.engine
        _scan_offset = 0  # 순환 오프셋 (알파벳 편향 방지)

        while eng.running:
            try:
                if eng.session.is_market_open():
                    if eng.screener:
                        # 어닝스 종목 screener에 주입
                        if eng._earnings_today:
                            eng.screener.set_earnings_symbols(eng._earnings_today)

                        # 순환 스캔: 매 사이클마다 다음 300개 종목
                        batch_size = 300
                        total = len(eng._universe)
                        if total <= batch_size:
                            symbols = eng._universe
                        else:
                            end = _scan_offset + batch_size
                            if end <= total:
                                symbols = eng._universe[_scan_offset:end]
                            else:
                                symbols = eng._universe[_scan_offset:] + eng._universe[:end - total]
                            _scan_offset = (_scan_offset + batch_size) % total
                        if symbols:
                            result = await asyncio.to_thread(
                                eng.screener.scan, symbols,
                            )
                            eng._last_screen_result = result
                            eng._last_screen_time = datetime.now()
                            logger.info(
                                f"[US 스크리너] 완료 — {len(result.results)}/{result.total_scanned} 통과"
                            )
                            try:
                                eng.screener.save_cache(result)
                            except Exception as e:
                                logger.warning(f"[US 스크리너] 캐시 저장 실패: {e}")

                            # 동적 유니버스 확장: 상위 스크리너 결과를 유니버스에 편입
                            if result.results:
                                top_symbols = {
                                    r.symbol for r in result.results[:50]
                                    if r.total_score >= 60
                                }
                                new_additions = top_symbols - set(eng._universe)
                                if new_additions:
                                    eng._universe.extend(list(new_additions)[:30])
                                    logger.info(
                                        f"[US 유니버스] 동적 확장 +{len(new_additions)}종목 "
                                        f"→ 총 {len(eng._universe)}개"
                                    )
                else:
                    logger.debug("[US 스크리너] 장 마감 — skip")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[US 스크리너] 오류: {e}")

            await asyncio.sleep(3600)  # 60분

    # ============================================================
    # 태스크 8: 워치리스트 루프
    # ============================================================

    async def watchlist_loop(self):
        """
        상위 후보 + 보유 포지션 Finviz 실시간 모니터링 (5분 주기).

        목적:
          - 상위 후보(StockScreener Top 25): 강한 장중 모멘텀 감지 시
            15분 스캔 사이클 대기 없이 즉시 전략 평가 → 시그널 발행
          - 보유 포지션: 모멘텀 급락(ms<25, 1h<=2.5%) 감지 시 exit check 즉시 트리거

        Finviz get_intraday_scan() TTL=5분이므로 주기와 정합.
        진입 조건: momentum_score >= 75 AND perf_1h >= 0.5%
        워치리스트 쿨다운: 15분 (스크리닝 메인 쿨다운과 별도)
        """
        await asyncio.sleep(150)  # 초기 대기 (스크리닝 루프와 시간 분산)
        eng = self.engine

        _wl_cooldown: Dict[str, datetime] = {}
        _WL_COOLDOWN_SEC = 900  # 15분

        while eng.running:
            try:
                fz = self._finviz
                if not eng.session.is_market_open() or not fz or not self._finviz_ready:
                    await asyncio.sleep(300)
                    continue

                # 쿨다운 만료 항목 정리 (메모리 누수 방지)
                now = datetime.now()
                expired_wl = [s for s, t in _wl_cooldown.items()
                              if (now - t).total_seconds() > _WL_COOLDOWN_SEC * 2]
                for s in expired_wl:
                    del _wl_cooldown[s]

                held = set(eng.portfolio.positions.keys())

                # 모니터링 대상: StockScreener 상위 25 + 보유 종목
                top_candidates: List[str] = []
                if eng._last_screen_result and eng._last_screen_result.results:
                    top_candidates = [
                        r.symbol for r in eng._last_screen_result.results[:25]
                        if r.symbol not in held
                    ]

                watch_symbols = list(set(top_candidates) | held)
                if not watch_symbols:
                    await asyncio.sleep(300)
                    continue

                # Finviz 장중 배치 스캔 (TTL 5분 캐시 재사용)
                intraday = await fz.get_intraday_scan(watch_symbols)

                # ── 보유 포지션: 모멘텀 급락 → exit check 즉시 ─────────────
                for sym in list(held):
                    d = intraday.get(sym, {})
                    ms = d.get("momentum_score", 50.0)
                    p1h = d.get("perf_1h", 0.0)
                    if ms < 25 and p1h <= -2.5:
                        logger.warning(
                            f"[US Watchlist] {sym} 보유 모멘텀 급락 "
                            f"(ms={ms:.0f}, 1h={p1h:+.2f}%) — exit 즉시 체크"
                        )
                        await self._check_exits()
                        break  # 한 번만 트리거

                # ── 상위 후보: 강한 모멘텀 → 즉시 전략 평가 ──────────────
                now = datetime.now()
                for sym in top_candidates:
                    d = intraday.get(sym, {})
                    ms = d.get("momentum_score", 50.0)
                    p1h = d.get("perf_1h", 0.0)
                    p30m = d.get("perf_30m", 0.0)

                    # 강한 장중 모멘텀 기준
                    if ms < 75 or p1h < 0.5:
                        continue

                    # 워치리스트 쿨다운 체크 (15분)
                    last_wl = _wl_cooldown.get(sym)
                    if last_wl and (now - last_wl).total_seconds() < _WL_COOLDOWN_SEC:
                        continue

                    # 기존 스크리닝 쿨다운 + 주문 중 확인
                    if self._is_in_cooldown(sym) or sym in eng._pending_symbols:
                        continue

                    logger.info(
                        f"[US Watchlist] {sym} 강한 모멘텀 감지 "
                        f"(ms={ms:.0f}, 1h={p1h:+.2f}%, 30m={p30m:+.2f}%) "
                        f"→ 즉시 전략 평가"
                    )
                    _wl_cooldown[sym] = now
                    await self._evaluate_watchlist_candidate(sym)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[US Watchlist] 오류: {e}")

            await asyncio.sleep(300)  # 5분

    async def _evaluate_watchlist_candidate(self, symbol: str):
        """워치리스트 후보 즉시 전략 평가 (단일 종목)"""
        eng = self.engine
        fz = self._finviz

        try:
            history = await self._get_history(symbol)
            if history is None or len(history) < 50:
                return

            last_close = float(history["close"].iloc[-1])
            uni_cfg = eng.config_raw.get("universe")
            if uni_cfg is None:
                uni_cfg = {}
            uni_max_price = float(uni_cfg.get("max_price", 0))
            if uni_max_price > 0 and last_close > uni_max_price:
                return

            try:
                eng._indicator_cache[symbol] = compute_indicators(history)
            except Exception:
                pass

            for strategy in eng.strategies:
                if (
                    strategy.name == "earnings_drift"
                    and eng._earnings_today
                    and symbol not in eng._earnings_today
                ):
                    continue

                signal = strategy.evaluate(symbol, history, eng.portfolio)
                if signal and fz and self._finviz_ready:
                    fz_result = fz.get_strategy_signals(symbol, strategy.name)
                    if not fz_result["pass"]:
                        signal = None
                    else:
                        signal.score = max(0.0, signal.score + fz_result["score_adjustment"])
                        if fz_result["reasons"]:
                            signal.reason += " | " + ", ".join(fz_result["reasons"][:2])
                        signal.reason = "[WL] " + signal.reason

                if signal:
                    logger.info(
                        f"[US Watchlist] {symbol} 즉시 시그널: "
                        f"{strategy.name} score={signal.score:.1f}"
                    )
                    await self._process_signal(signal)
                    break

        except Exception as e:
            logger.debug(f"[US Watchlist] {symbol} 평가 실패: {e}")

    # ============================================================
    # 태스크 9: 거래량급증 루프 (HHDFS76270000)
    # ============================================================

    async def volume_surge_loop(self):
        """
        KIS 거래량급증 API 15분 주기 조회 → _vol_surge_symbols 갱신.

        surge 종목은 _run_screening에서 우선 평가 대상으로 반영.
        실전 계좌 전용 (모의투자 미지원).
        """
        await asyncio.sleep(60)  # 초기 대기 (브로커 연결 후)
        eng = self.engine

        while eng.running:
            try:
                if not eng.session.is_market_open():
                    await asyncio.sleep(300)
                    continue

                new_surge: Set[str] = set()
                # 나스닥 + 뉴욕 + 아멕스 3거래소 조회
                for excd in ("NAS", "NYS", "AMS"):
                    hits = await eng.broker.get_volume_surge(
                        exchange=excd,
                        minutes_ago=5,
                        min_volume="2",  # 1천주 이상
                    )
                    for h in hits:
                        sym = h["symbol"]
                        # 유니버스 필터 + 최소 급증율 10% 이상만
                        if sym in eng._universe and h.get("surge_rate", 0) >= 10:
                            new_surge.add(sym)

                prev_count = len(eng._vol_surge_symbols)
                eng._vol_surge_symbols = new_surge
                eng._vol_surge_updated = datetime.now()

                if new_surge:
                    logger.info(
                        f"[US 거래량급증] {len(new_surge)}종목 감지 "
                        f"(이전: {prev_count}) — {', '.join(sorted(new_surge)[:10])}"
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[US 거래량급증] 루프 오류: {e}")

            await asyncio.sleep(900)  # 15분

    # ============================================================
    # 태스크 10: 테마 탐지 루프
    # ============================================================

    async def theme_detection_loop(self):
        """US 테마 탐지 (30분 주기)"""
        await asyncio.sleep(10)  # 초기 대기
        eng = self.engine

        while eng.running:
            try:
                if eng.theme_detector:
                    themes = await eng.theme_detector.detect_themes()
                    if themes:
                        logger.info(
                            f"[US 테마] 활성 테마 {len(themes)}개: "
                            f"{', '.join(t.name for t in themes[:5])}"
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[US 테마] 탐지 오류: {e}")

            await asyncio.sleep(1800)  # 30분
