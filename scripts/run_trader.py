#!/usr/bin/env python3
"""
QWQ AI Trader - 통합 트레이더 실행 스크립트

KR + US 시장을 단일 프로세스에서 운영합니다.

사용법:
    python scripts/run_trader.py [--config CONFIG_PATH] [--dry-run]
    python scripts/run_trader.py --market kr      # KR만 실행
    python scripts/run_trader.py --market us      # US만 실행
    python scripts/run_trader.py --market both    # KR + US 동시 (기본)
"""

import argparse
import asyncio
import json
import signal
import sys
import os
import fcntl
from collections import deque
from datetime import datetime, date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional, Set, List

# 프로젝트 루트를 path에 추가
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "src"))

from loguru import logger

from src.core.engine import UnifiedEngine, StrategyManager, RiskManager, is_kr_market_holiday, set_kr_market_holidays
from src.core.types import TradingConfig, Market, MarketSession, Portfolio, RiskConfig
from src.core.event import EventType
from src.core.market_context import MarketContext


# ============================================================
# PID 파일 + flock 기반 프로세스 중복 방지
# ============================================================

PID_FILE = Path.home() / ".cache" / "ai_trader" / "unified_trader.pid"
LOCK_FILE = Path.home() / ".cache" / "ai_trader" / "unified_trader.lock"
_lock_fd = None  # 전역 파일 디스크립터 (프로세스 수명 동안 유지)


def acquire_singleton_lock() -> bool:
    """
    flock 기반 싱글톤 락 획득

    1단계: 실행 중인 다른 프로세스를 SIGTERM → SIGKILL
    2단계: flock 파일 락으로 race condition 완전 차단
    3단계: PID 파일 기록

    Returns:
        True: 락 획득 성공 (유일한 프로세스)
        False: 락 획득 실패
    """
    global _lock_fd
    import time

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)

    # 1단계: PID 파일에서 기존 프로세스 종료 (안전: PID 파일에 기록된 프로세스만 종료)
    try:
        if PID_FILE.exists():
            old_pid = int(PID_FILE.read_text().strip())
            if old_pid != os.getpid():
                try:
                    os.kill(old_pid, signal.SIGTERM)
                    logger.warning(f"기존 프로세스 PID={old_pid} SIGTERM 전송")
                    time.sleep(3)
                    try:
                        os.kill(old_pid, 0)  # 아직 살아있는지 확인
                        os.kill(old_pid, signal.SIGKILL)
                        logger.warning(f"기존 프로세스 PID={old_pid} SIGKILL 전송")
                        time.sleep(1)
                    except ProcessLookupError:
                        pass
                except ProcessLookupError:
                    pass  # 이미 종료됨
                except ValueError:
                    pass  # PID 파일 손상
    except Exception as e:
        logger.debug(f"기존 프로세스 확인 실패: {e}")

    # 2단계: flock 획득 (non-blocking)
    try:
        _lock_fd = open(LOCK_FILE, 'w')
        fcntl.flock(_lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
    except (IOError, OSError):
        logger.error("flock 획득 실패 — 다른 프로세스가 이미 락을 보유 중")
        if _lock_fd:
            _lock_fd.close()
            _lock_fd = None
        return False

    # 3단계: PID 파일 기록
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))

    logger.info(f"싱글톤 락 획득 완료 (PID: {os.getpid()})")
    return True


def release_singleton_lock():
    """락 해제 + PID 파일 제거"""
    global _lock_fd
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
    except Exception as e:
        logger.warning(f"PID 파일 제거 실패: {e}")

    try:
        if _lock_fd:
            fcntl.flock(_lock_fd.fileno(), fcntl.LOCK_UN)
            _lock_fd.close()
            _lock_fd = None
    except Exception as e:
        logger.warning(f"flock 해제 실패: {e}")

    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except Exception:
        pass


# ============================================================
# 통합 트레이딩 봇
# ============================================================

class UnifiedTradingBot:
    """KR + US 통합 트레이딩 봇

    UnifiedEngine을 중심으로 KR/US 시장 컨텍스트를 초기화하고
    각 시장의 스케줄러 태스크를 병렬 실행합니다.
    """

    def __init__(self, config, dry_run: bool = False, market: str = "both"):
        self.config = config
        self.dry_run = dry_run
        self.market = market  # "kr", "us", "both"
        self.running = False

        # 통합 엔진
        self.engine = UnifiedEngine(config.trading)

        # 공유 토큰 매니저 (KR + US 동일 KIS API 키 사용)
        self._token_manager = None

        # KR 시장 컴포넌트
        self.broker = None                    # KISBroker
        self.strategy_manager = None          # StrategyManager
        self.risk_manager = None              # RiskManager (risk/manager.py)
        self.exit_manager = None              # ExitManager
        self.screener = None                  # StockScreener (KR)
        self.theme_detector = None            # ThemeDetector
        self.stock_master = None              # StockMaster
        self.kis_market_data = None           # KISMarketData
        self.trade_journal = None             # TradeJournal / TradeStorage
        self.strategy_evolver = None          # StrategyEvolver
        self.batch_analyzer = None            # BatchAnalyzer
        self.health_monitor = None            # HealthMonitor
        self.ws_feed = None                   # KISWebSocketFeed

        # 대시보드
        self.dashboard = None                 # DashboardServer

        # US 시장 컴포넌트 (us_engine 객체로 묶어서 USScheduler에 전달)
        self._us_engine = None                # US LiveEngine-like 객체

        # KR 상태
        self._watch_symbols: List[str] = []
        self._screening_interval: int = 600
        self._screening_signal_cooldown: dict = {}
        # 재시작 생존: 당일 진입 카운터 파일 로드
        _ec_today = datetime.now().date().isoformat()
        _ec_path = Path.home() / ".cache" / "ai_trader" / f"daily_entry_count_{_ec_today}.json"
        try:
            self._daily_entry_count: Dict[str, int] = json.loads(_ec_path.read_text()) if _ec_path.exists() else {}
        except Exception:
            self._daily_entry_count: Dict[str, int] = {}
        self._strategy_exit_params: Dict[str, Dict[str, float]] = {}
        self._symbol_strategy: Dict[str, str] = {}
        self._symbol_signals: Dict[str, Any] = {}
        self._exit_pending_symbols: Set[str] = set()
        self._exit_pending_timestamps: Dict[str, datetime] = {}
        self._exit_reasons: Dict[str, str] = {}
        self._sell_blocked_symbols: Dict[str, datetime] = {}
        self._pause_resume_at: Optional[datetime] = None
        self._watch_symbols_lock = asyncio.Lock()
        self._portfolio_lock = asyncio.Lock()
        self._sector_cache: dict = {}
        self._external_accounts: list = []
        self._last_screened: list = []
        self.stock_name_cache: Dict[str, str] = {}
        self.kr_scheduler = None              # KRScheduler (WS 콜백에서 exit check용)
        self.equity_tracker = None
        self.daily_reviewer = None
        self.report_generator = None
        self._mcp_manager = None
        self._stock_validator = None

        # 엔진에 종목명 캐시 참조 연결
        self.engine._stock_name_cache = self.stock_name_cache

        # 시장별 태스크
        self._kr_tasks: List[asyncio.Task] = []
        self._us_tasks: List[asyncio.Task] = []

        # 시그널 핸들러
        self._setup_signal_handlers()

    def _setup_signal_handlers(self):
        """종료 시그널 핸들러"""
        def handle_shutdown(signum, frame):
            logger.warning(f"종료 신호 수신 ({signum})")
            self.stop()

        signal.signal(signal.SIGINT, handle_shutdown)
        signal.signal(signal.SIGTERM, handle_shutdown)

    # ============================================================
    # 초기화
    # ============================================================

    async def initialize(self) -> bool:
        """컴포넌트 초기화"""
        try:
            logger.info("=== QWQ AI Trader 통합 봇 초기화 ===")
            logger.info(f"Dry Run: {self.dry_run}")
            logger.info(f"Market: {self.market}")

            # 공유 토큰 매니저 생성 (KR + US 공통)
            from src.utils.token_manager import KISTokenManager, get_token_manager
            self._token_manager = get_token_manager()
            logger.info(f"공유 토큰 매니저 초기화: env={self._token_manager.env}")

            # KR 시장 초기화
            if self.market in ("kr", "both"):
                kr_ok = await self._initialize_kr()
                if not kr_ok:
                    logger.error("KR 시장 초기화 실패")
                    if self.market == "kr":
                        return False

            # US 시장 초기화
            if self.market in ("us", "both"):
                us_ok = await self._initialize_us()
                if not us_ok:
                    logger.error("US 시장 초기화 실패")
                    if self.market == "us":
                        return False

            # 대시보드 초기화 (KR/US 컨텍스트 모두 사용)
            dashboard_cfg = self.config.get("dashboard") or {}
            if dashboard_cfg.get("enabled", True):
                try:
                    from src.dashboard.server import DashboardServer
                    self.dashboard = DashboardServer(
                        kr_bot=self if self.market in ("kr", "both") else None,
                        us_engine=self._us_engine if self.market in ("us", "both") else None,
                        host=dashboard_cfg.get("host", "0.0.0.0"),
                        port=dashboard_cfg.get("port", 8080),
                    )
                    logger.info(f"대시보드 서버 초기화 완료 (포트: {dashboard_cfg.get('port', 8080)})")
                except Exception as e:
                    logger.warning(f"대시보드 서버 초기화 실패 (무시): {e}")
                    self.dashboard = None

            logger.info("=== 통합 봇 초기화 완료 ===")
            return True

        except Exception as e:
            logger.exception(f"초기화 실패: {e}")
            try:
                from src.utils.telegram import send_alert
                import traceback
                loop = asyncio.get_running_loop()
                loop.create_task(send_alert(
                    f"🚨 <b>[CRITICAL]</b> 통합 봇 초기화 실패\n<pre>{traceback.format_exc()[:300]}</pre>"
                ))
            except Exception:
                pass
            return False

    # ============================================================
    # KR 시장 초기화
    # ============================================================

    async def _initialize_kr(self) -> bool:
        """KR 시장 초기화 — ai-trader-v2 TradingBot.initialize()에서 이식"""
        logger.info("[KR] 한국 시장 초기화 시작...")

        try:
            kr_cfg = self.config.get("kr") or {}

            # 1. KIS 브로커 생성 및 연결
            if not self.dry_run:
                from src.execution.broker.kis_kr import KISBroker, KISConfig

                kis_config = KISConfig.from_env()
                self.broker = KISBroker(config=kis_config, token_manager=self._token_manager)

                if not await self.broker.connect():
                    logger.error("[KR] 브로커 연결 실패")
                    return False

                # 계좌 잔고 로드
                balance = await self.broker.get_account_balance()
                if balance:
                    actual_capital = balance.get('total_equity', 0)
                    available_cash = balance.get('available_cash', 0)
                    stock_value = balance.get('stock_value', 0)

                    if actual_capital > 0:
                        self.engine.portfolio.initial_capital = Decimal(str(actual_capital))
                        self.engine.portfolio.cash = Decimal(str(available_cash))
                        self.config.trading.initial_capital = Decimal(str(actual_capital))

                        logger.info(f"[KR] === 실제 계좌 잔고 ===")
                        logger.info(f"[KR]   초기자본(총자산): {actual_capital:,.0f}원")
                        logger.info(f"[KR]   주문가능금액:     {available_cash:,.0f}원")
                        logger.info(f"[KR]   주식평가금액:     {stock_value:,.0f}원")

                        # 기존 보유 종목 로드
                        await self._load_existing_positions()
                    else:
                        logger.warning("[KR] 계좌 잔고 조회 실패, 설정값 사용")
                        self.engine.portfolio.initial_capital = Decimal(str(self.config.trading.initial_capital))
                        self.engine.portfolio.cash = Decimal(str(self.config.trading.initial_capital))
                else:
                    logger.warning("[KR] 계좌 잔고 조회 실패, 설정값 사용")
                    self.engine.portfolio.initial_capital = Decimal(str(self.config.trading.initial_capital))
                    self.engine.portfolio.cash = Decimal(str(self.config.trading.initial_capital))
            else:
                logger.info(f"[KR] Dry Run 모드: 설정 자본 사용 ({self.config.trading.initial_capital:,}원)")
                self.engine.portfolio.initial_capital = Decimal(str(self.config.trading.initial_capital))
                self.engine.portfolio.cash = Decimal(str(self.config.trading.initial_capital))

            # 2. KIS 시장 데이터 클라이언트 + 동적 휴장일
            try:
                from src.data.providers.kis_market_data import KISMarketData, get_kis_market_data
                self.kis_market_data = get_kis_market_data()
                try:
                    now = datetime.now()
                    cur_month = now.strftime("%Y%m")
                    next_month = (now.replace(day=1) + timedelta(days=32)).strftime("%Y%m")
                    h1 = await self.kis_market_data.fetch_holidays(cur_month)
                    h2 = await self.kis_market_data.fetch_holidays(next_month)
                    all_holidays = h1 | h2
                    if all_holidays:
                        set_kr_market_holidays(all_holidays)
                except Exception as e:
                    logger.warning(f"[KR] 동적 휴장일 로드 실패 (주말만 체크): {e}")
                logger.info("[KR] KIS 시장 데이터 클라이언트 초기화 완료")
            except Exception as e:
                logger.warning(f"[KR] KIS 시장 데이터 초기화 실패 (무시): {e}")
                self.kis_market_data = None

            # 3. 종목 마스터 초기화
            sm_cfg = kr_cfg.get("stock_master", self.config.get("stock_master") or {})
            if sm_cfg.get("enabled", True):
                try:
                    from src.data.storage.stock_master import StockMaster, get_stock_master
                    self.stock_master = get_stock_master()
                    if await self.stock_master.connect():
                        if await self.stock_master.is_empty():
                            logger.info("[KR] [종목마스터] 빈 테이블 감지 → 초기 갱신 실행")
                            try:
                                await self.stock_master.refresh_master()
                            except Exception as e:
                                logger.warning(f"[KR] [종목마스터] 초기 갱신 실패 (무시): {e}")
                        else:
                            await self.stock_master.rebuild_cache()
                        logger.info("[KR] 종목 마스터 초기화 완료")
                    else:
                        logger.warning("[KR] 종목 마스터 DB 연결 실패 (무시)")
                        self.stock_master = None
                except Exception as e:
                    logger.warning(f"[KR] 종목 마스터 초기화 실패 (무시): {e}")
                    self.stock_master = None

            # 4. 테마 탐지기 초기화
            try:
                from src.signals.sentiment.kr_theme_detector import ThemeDetector, get_theme_detector
                theme_cfg = kr_cfg.get("theme_detector", self.config.get("theme_detector") or {})
                self.theme_detector = ThemeDetector(
                    kis_market_data=self.kis_market_data,
                    us_market_data=None,
                    stock_master=self.stock_master,
                )
                self.theme_detector.detection_interval_minutes = theme_cfg.get("scan_interval_minutes", 15)
                self.theme_detector.min_news_count = theme_cfg.get("min_news_count", 3)
                self.theme_detector.hot_theme_threshold = theme_cfg.get("min_theme_score", 70.0)

                # 전역 싱글톤 등록
                import src.signals.sentiment.kr_theme_detector as _td_mod
                _td_mod._theme_detector = self.theme_detector
                logger.info("[KR] 테마 탐지기 초기화 완료")
            except Exception as e:
                logger.warning(f"[KR] 테마 탐지기 초기화 실패 (무시): {e}")
                self.theme_detector = None

            # 5. MCP 서버 클라이언트 초기화
            try:
                from src.utils.mcp_client import get_mcp_manager
                self._mcp_manager = get_mcp_manager()
                await self._mcp_manager.initialize()
                logger.info("[KR] MCP 서버 클라이언트 초기화 완료")
            except Exception as e:
                logger.warning(f"[KR] MCP 클라이언트 초기화 실패 (무시): {e}")
                self._mcp_manager = None

            # 6. 종목 뉴스/공시 검증기 초기화
            try:
                from src.signals.fundamentals.stock_validator import get_stock_validator
                self._stock_validator = get_stock_validator()
                await self._stock_validator.initialize()
                logger.info("[KR] 종목 뉴스/공시 검증기 초기화 완료")
            except Exception as e:
                logger.warning(f"[KR] 종목 검증기 초기화 실패 (무시): {e}")
                self._stock_validator = None

            # 7. 전략 초기화 및 등록
            self.strategy_manager = StrategyManager(self.engine)

            strategies_cfg = kr_cfg.get("strategies", self.config.get("strategies") or {})

            # 모멘텀 전략
            momentum_cfg = strategies_cfg.get("momentum_breakout") or {}
            if momentum_cfg.get("enabled", True):
                try:
                    from src.strategies.kr.momentum import MomentumBreakoutStrategy, MomentumConfig
                    momentum_strategy = MomentumBreakoutStrategy(MomentumConfig(
                        min_breakout_pct=momentum_cfg.get("min_breakout_pct", 1.0),
                        volume_surge_ratio=momentum_cfg.get("volume_surge_ratio", 3.0),
                        stop_loss_pct=momentum_cfg.get("stop_loss_pct", 2.5),
                        take_profit_pct=momentum_cfg.get("take_profit_pct", 5.0),
                        trailing_stop_pct=momentum_cfg.get("trailing_stop_pct", 1.5),
                    ))
                    self.strategy_manager.register_strategy("momentum_breakout", momentum_strategy)
                    logger.info("[KR] 모멘텀 브레이크아웃 전략 등록")
                except Exception as e:
                    logger.warning(f"[KR] 모멘텀 전략 초기화 실패: {e}")

            # 테마 추종 전략
            theme_strategy_cfg = strategies_cfg.get("theme_chasing") or {}
            if theme_strategy_cfg.get("enabled", True):
                try:
                    from src.strategies.kr.theme_chasing import ThemeChasingStrategy, ThemeChasingConfig
                    theme_strategy = ThemeChasingStrategy(
                        config=ThemeChasingConfig(
                            min_theme_score=theme_strategy_cfg.get("min_theme_score", 50.0),
                            stop_loss_pct=theme_strategy_cfg.get("stop_loss_pct", 2.0),
                            take_profit_pct=theme_strategy_cfg.get("take_profit_pct", 4.0),
                            trailing_stop_pct=theme_strategy_cfg.get("trailing_stop_pct", 1.0),
                        ),
                        kis_market_data=self.kis_market_data,
                    )
                    if self.theme_detector:
                        theme_strategy.set_theme_detector(self.theme_detector)
                    self.strategy_manager.register_strategy("theme_chasing", theme_strategy)
                    logger.info("[KR] 테마 추종 전략 등록")
                except Exception as e:
                    logger.warning(f"[KR] 테마 추종 전략 초기화 실패: {e}")

            # 갭상승 추종 전략
            gap_cfg = strategies_cfg.get("gap_and_go") or {}
            if gap_cfg.get("enabled", True):
                try:
                    from src.strategies.kr.gap_and_go import GapAndGoStrategy, GapAndGoConfig
                    gap_strategy = GapAndGoStrategy(GapAndGoConfig(
                        min_gap_pct=gap_cfg.get("min_gap_pct", 2.0),
                        max_gap_pct=gap_cfg.get("max_gap_pct", 10.0),
                        entry_delay_minutes=gap_cfg.get("entry_delay_minutes", 30),
                        pullback_pct=gap_cfg.get("pullback_pct", 1.0),
                        min_volume_ratio=gap_cfg.get("min_volume_ratio", 2.0),
                        stop_loss_pct=gap_cfg.get("stop_loss_pct", 2.0),
                        take_profit_pct=gap_cfg.get("take_profit_pct", 4.0),
                        trailing_stop_pct=gap_cfg.get("trailing_stop_pct", 1.5),
                    ))
                    self.strategy_manager.register_strategy("gap_and_go", gap_strategy)
                    logger.info("[KR] 갭상승 추종 전략 등록")
                except Exception as e:
                    logger.warning(f"[KR] 갭상승 전략 초기화 실패: {e}")

            # SEPA / RSI2 스윙 전략 설정
            rsi2_cfg = strategies_cfg.get("rsi2_reversal") or {}
            sepa_cfg = strategies_cfg.get("sepa_trend") or {}

            # 전략별 청산 파라미터 (ExitManager 전달용)
            self._strategy_exit_params = {
                "momentum_breakout": {
                    "stop_loss_pct": momentum_cfg.get("stop_loss_pct", 2.5),
                    "trailing_stop_pct": momentum_cfg.get("trailing_stop_pct", 1.5),
                    "first_exit_pct": 3.0,
                    "second_exit_pct": 5.0,
                    "third_exit_pct": momentum_cfg.get("take_profit_pct", 10.0),
                },
                "theme_chasing": {
                    "stop_loss_pct": theme_strategy_cfg.get("stop_loss_pct", 2.0),
                    "trailing_stop_pct": theme_strategy_cfg.get("trailing_stop_pct", 1.0),
                    "first_exit_pct": theme_strategy_cfg.get("take_profit_pct", 8.0) * 0.3,
                    "second_exit_pct": theme_strategy_cfg.get("take_profit_pct", 8.0) * 0.6,
                    "third_exit_pct": theme_strategy_cfg.get("take_profit_pct", 8.0),
                },
                "gap_and_go": {
                    "stop_loss_pct": gap_cfg.get("stop_loss_pct", 2.0),
                    "trailing_stop_pct": gap_cfg.get("trailing_stop_pct", 1.5),
                    "first_exit_pct": gap_cfg.get("take_profit_pct", 8.0) * 0.3,
                    "second_exit_pct": gap_cfg.get("take_profit_pct", 8.0) * 0.6,
                    "third_exit_pct": gap_cfg.get("take_profit_pct", 8.0),
                },
                "rsi2_reversal": {
                    "stop_loss_pct": rsi2_cfg.get("stop_loss_pct", 5.0),
                    "trailing_stop_pct": 3.0,
                    "first_exit_pct": 5.0,
                    "second_exit_pct": 10.0,
                    "third_exit_pct": 12.0,
                },
                "sepa_trend": {
                    "stop_loss_pct": sepa_cfg.get("stop_loss_pct", 5.0),
                    "trailing_stop_pct": 3.0,
                    "first_exit_pct": 5.0,
                    "second_exit_pct": 10.0,
                    "third_exit_pct": 12.0,
                },
                "strategic_swing": {
                    "stop_loss_pct": 5.0,
                    "trailing_stop_pct": 3.0,
                    "first_exit_pct": 5.0,
                    "second_exit_pct": 10.0,
                    "third_exit_pct": 12.0,
                },
            }

            # 8. 리스크 매니저 초기화
            from src.risk.manager import RiskManager as RiskMgr
            self.risk_manager = RiskMgr(
                self.config.trading.risk,
                self.config.trading.initial_capital,
                market="KR",
            )

            # 9. ExitManager 초기화
            from src.strategies.exit_manager import ExitManager, ExitConfig
            exit_cfg = kr_cfg.get("exit_manager", self.config.get("exit_manager") or {})
            self.exit_manager = ExitManager(ExitConfig(
                enable_partial_exit=exit_cfg.get("enable_partial_exit", True),
                first_exit_pct=exit_cfg.get("first_exit_pct", 5.0),
                first_exit_ratio=exit_cfg.get("first_exit_ratio", 0.30),
                second_exit_pct=exit_cfg.get("second_exit_pct", 10.0),
                second_exit_ratio=exit_cfg.get("second_exit_ratio", 0.50),
                third_exit_pct=exit_cfg.get("third_exit_pct", 12.0),
                third_exit_ratio=exit_cfg.get("third_exit_ratio", 0.50),
                stop_loss_pct=exit_cfg.get("stop_loss_pct", 5.0),
                trailing_stop_pct=exit_cfg.get("trailing_stop_pct", 3.0),
                trailing_activate_pct=exit_cfg.get("trailing_activate_pct", 5.0),
                min_stop_pct=exit_cfg.get("min_stop_pct", 4.0),
                max_stop_pct=exit_cfg.get("max_stop_pct", 7.0),
                atr_multiplier=exit_cfg.get("atr_multiplier", 2.0),
                include_fees=exit_cfg.get("include_fees", True),
            ))
            logger.info("[KR] 분할 익절 관리자 초기화 완료")

            # 10. 기존 포지션을 ExitManager에 등록
            if self.engine.portfolio.positions:
                for symbol, position in self.engine.portfolio.positions.items():
                    price_history = self._get_price_history_for_atr(symbol)
                    exit_params = self._strategy_exit_params.get(position.strategy, {}) if position.strategy else {}
                    self.exit_manager.register_position(
                        position,
                        price_history=price_history,
                        stop_loss_pct=exit_params.get("stop_loss_pct"),
                        trailing_stop_pct=exit_params.get("trailing_stop_pct"),
                        first_exit_pct=exit_params.get("first_exit_pct"),
                        second_exit_pct=exit_params.get("second_exit_pct"),
                        third_exit_pct=exit_params.get("third_exit_pct"),
                    )
                logger.info(
                    f"[KR] 기존 포지션 {len(self.engine.portfolio.positions)}개 ExitManager 등록 완료"
                )

            # 11. 자가 진화 엔진 초기화
            evolution_cfg = kr_cfg.get("evolution", self.config.get("evolution") or {})
            if evolution_cfg.get("enabled", True):
                try:
                    from src.core.evolution import get_trade_journal, get_strategy_evolver
                    self.trade_journal = get_trade_journal()

                    # TradeStorage DB 연결
                    if hasattr(self.trade_journal, 'connect'):
                        await self.trade_journal.connect()
                        if self.broker and hasattr(self.trade_journal, 'sync_from_kis'):
                            await self.trade_journal.sync_from_kis(self.broker, engine=self.engine)
                        # DB 연결 후 포지션 전략/진입시간 복원
                        if self.engine.portfolio.positions:
                            await self._restore_position_metadata(self.engine.portfolio.positions)

                    # 일일 통계 복원
                    self.engine.restore_daily_stats()
                    _pool = getattr(self.trade_journal, 'pool', None)
                    if _pool:
                        await self.engine.restore_daily_pnl_from_db(_pool)

                    self.strategy_evolver = get_strategy_evolver()

                    # 전략 등록 (파라미터 자동 조정용)
                    for name, strategy in self.strategy_manager.strategies.items():
                        self.strategy_evolver.register_strategy(name, strategy)

                    # 컴포넌트 등록
                    if self.exit_manager:
                        self.strategy_evolver.register_component(
                            "exit_manager", self.exit_manager, "config"
                        )
                    if self.config.trading.risk:
                        self.strategy_evolver.register_component(
                            "risk_config", self.config.trading.risk
                        )

                    # 기존 포지션에 전략 정보 보강
                    if self.engine.portfolio.positions and self.trade_journal:
                        open_trades = self.trade_journal.get_open_trades()
                        trade_by_symbol = {t.symbol: t for t in open_trades}
                        for symbol, pos in self.engine.portfolio.positions.items():
                            if not pos.strategy and symbol in trade_by_symbol:
                                trade = trade_by_symbol[symbol]
                                if trade.entry_strategy:
                                    pos.strategy = trade.entry_strategy
                                    if not pos.entry_time and trade.entry_time:
                                        pos.entry_time = trade.entry_time
                                    logger.info(f"[KR]   포지션 전략 보강: {symbol} → {trade.entry_strategy}")

                    logger.info("[KR] 자가 진화 엔진 초기화 완료")
                except Exception as e:
                    logger.warning(f"[KR] 진화 엔진 초기화 실패 (무시): {e}")
                    self.trade_journal = None
                    self.strategy_evolver = None

            # 12. 자산 히스토리 추적기
            try:
                from src.analytics.equity_tracker import EquityTracker
                self.equity_tracker = EquityTracker()
                if self.trade_journal:
                    self.equity_tracker.backfill_from_journal(
                        initial_capital=float(self.engine.portfolio.initial_capital)
                    )
            except Exception as e:
                logger.warning(f"[KR] 자산 추적기 초기화 실패 (무시): {e}")
                self.equity_tracker = None

            # 13. 일일 거래 리뷰어
            try:
                from src.core.evolution.daily_reviewer import DailyReviewer
                self.daily_reviewer = DailyReviewer()
                logger.info("[KR] 일일 거래 리뷰어 초기화 완료")
            except Exception as e:
                logger.warning(f"[KR] 일일 리뷰어 초기화 실패 (무시): {e}")
                self.daily_reviewer = None

            # 14. 종목 스크리너 초기화
            try:
                from src.signals.screener.kr_screener import StockScreener, get_screener
                self.screener = get_screener()
                screener_cfg = kr_cfg.get("screener", self.config.get("screener") or {})
                self.screener.min_volume_ratio = screener_cfg.get("min_volume_ratio", 2.0)
                self.screener.min_change_pct = screener_cfg.get("min_change_pct", 1.0)
                self.screener.max_change_pct = screener_cfg.get("max_change_pct", 15.0)
                self.screener.min_trading_value = screener_cfg.get("min_trading_value", 100000000)
                self._screening_interval = screener_cfg.get("scan_interval_minutes", 10) * 60
                if self.stock_master:
                    self.screener.set_stock_master(self.stock_master)
                if self.broker:
                    self.screener.set_broker(self.broker)
                logger.info("[KR] 종목 스크리너 초기화 완료")
            except Exception as e:
                logger.warning(f"[KR] 스크리너 초기화 실패 (무시): {e}")
                self.screener = None

            # 15. 엔진에 컴포넌트 연결
            self.engine.strategy_manager = self.strategy_manager
            self.engine.broker = self.broker

            # 엔진 이벤트 핸들링용 RiskManager (SIGNAL→ORDER, FILL 추적)
            engine_risk_manager = RiskManager(
                self.engine, self.config.trading.risk,
                risk_validator=self.risk_manager,
                sector_lookup=self._get_sector,
            )
            self.engine.risk_manager = engine_risk_manager
            logger.info("[KR] 엔진 리스크 매니저 등록 완료")

            # 16. WebSocket 피드 초기화
            if not self.dry_run:
                try:
                    from src.data.feeds.kis_websocket import KISWebSocketFeed, KISWebSocketConfig
                    self.ws_feed = KISWebSocketFeed(KISWebSocketConfig.from_env())
                    self.ws_feed.on_market_data(self._on_market_data)
                    data_cfg = kr_cfg.get("data", self.config.get("data") or {})
                    realtime_source = data_cfg.get("realtime_source", "rest_polling")
                    if realtime_source == "rest_polling":
                        logger.info("[KR] REST+WS 병행 모드")
                    else:
                        logger.info("[KR] WebSocket 피드 초기화 완료")

                    # WS 생성 직후 보유 종목 우선 구독 설정
                    if self.engine.portfolio.positions:
                        pos_symbols = list(self.engine.portfolio.positions.keys())
                        self.ws_feed.set_priority_symbols(pos_symbols)
                        logger.info(f"[KR] WS 보유 종목 {len(pos_symbols)}개 우선 구독 예약 (연결 후 자동 구독)")
                except Exception as e:
                    logger.warning(f"[KR] WebSocket 초기화 실패 (무시): {e}")
                    self.ws_feed = None

            # 17. 배치 분석기 초기화
            if rsi2_cfg.get("enabled") or sepa_cfg.get("enabled"):
                try:
                    from src.core.batch_analyzer import BatchAnalyzer
                    self.batch_analyzer = BatchAnalyzer(
                        engine=self.engine,
                        broker=self.broker,
                        kis_market_data=self.kis_market_data,
                        stock_master=self.stock_master,
                        exit_manager=self.exit_manager,
                        config={
                            "rsi2_reversal": rsi2_cfg,
                            "sepa_trend": sepa_cfg,
                            "batch": kr_cfg.get("batch", self.config.get("batch") or {}),
                        },
                    )
                    batch_cfg = kr_cfg.get("batch", self.config.get("batch") or {})
                    if self.exit_manager:
                        self.exit_manager._max_holding_days = batch_cfg.get("max_holding_days", 10)
                    logger.info("[KR] 배치 분석기 초기화 완료 (스윙 모멘텀 모드)")

                    # 섹터 모멘텀 → 스크리너 연동
                    if self.screener and hasattr(self.batch_analyzer, '_sector_momentum'):
                        self.screener.set_sector_momentum(self.batch_analyzer._sector_momentum)
                        logger.info("[KR] 섹터 모멘텀 → 스크리너 연동 완료")
                except Exception as e:
                    logger.warning(f"[KR] 배치 분석기 초기화 실패 (무시): {e}")
                    self.batch_analyzer = None

            # 18. 헬스 모니터 초기화
            try:
                from src.monitoring.health_monitor import HealthMonitor
                self.health_monitor = HealthMonitor(self)
                logger.info("[KR] 헬스 모니터 초기화 완료")
            except Exception as e:
                logger.warning(f"[KR] 헬스 모니터 초기화 실패 (무시): {e}")
                self.health_monitor = None

            # 19. 외부 계좌 설정 파싱
            ext_accounts_str = os.getenv("KIS_EXT_ACCOUNTS", "")
            if ext_accounts_str:
                for entry in ext_accounts_str.split(","):
                    parts = entry.strip().split(":")
                    if len(parts) != 3:
                        logger.warning(f"[KR] 외부 계좌 형식 오류 (무시): {entry.strip()}")
                        continue
                    name, cano, acnt_prdt_cd = parts
                    if len(cano) != 8 or not cano.isdigit():
                        logger.warning(f"[KR] 외부 계좌 CANO 오류 (무시): {name}")
                        continue
                    if len(acnt_prdt_cd) != 2 or not acnt_prdt_cd.isdigit():
                        logger.warning(f"[KR] 외부 계좌 ACNT_PRDT_CD 오류 (무시): {name}")
                        continue
                    self._external_accounts.append((name, cano, acnt_prdt_cd))
                if self._external_accounts:
                    masked = [f"{a[0]}({a[1][:2]}****{a[1][-2:]})" for a in self._external_accounts]
                    logger.info(f"[KR] 외부 계좌 {len(self._external_accounts)}개 설정: {', '.join(masked)}")

            # 20. 이벤트 핸들러 등록
            self._register_event_handlers()

            # 21. 감시 종목 로드
            await self._load_watch_symbols()

            # 22. 과거 일봉 데이터 로드
            await self._preload_price_history()

            # 23. 거래 저널 종목명 보강
            await self._fill_name_cache_from_journal()

            # KR 컨텍스트 등록
            from src.utils.session import KRSession
            kr_session = KRSession()

            kr_ctx = MarketContext(
                market=Market.KRX,
                enabled=True,
                broker=self.broker,
                session=kr_session,
                portfolio=self.engine.portfolio,
                risk_mgr=self.risk_manager,
                exit_mgr=self.exit_manager,
                strategy_manager=self.strategy_manager,
                screener=self.screener,
                market_data=self.kis_market_data,
                data_feed=self.ws_feed,
                config=kr_cfg,
            )
            self.engine.register_context("KR", kr_ctx)

            logger.info("[KR] 한국 시장 초기화 완료")
            return True

        except Exception as e:
            logger.exception(f"[KR] 초기화 실패: {e}")
            return False

    # ============================================================
    # US 시장 초기화
    # ============================================================

    async def _initialize_us(self) -> bool:
        """US 시장 초기화 — ai-trader-us LiveEngine.initialize()에서 이식"""
        logger.info("[US] 미국 시장 초기화 시작...")

        try:
            us_cfg = self.config.get("us") or {}
            live_cfg = us_cfg.get("live", {})

            # US 전용 TradingConfig 생성
            from src.utils.config import create_us_trading_config
            us_trading_config = create_us_trading_config(self.config.raw)

            # US LiveEngine-like 객체 생성 (USScheduler가 접근할 속성 번들)
            us_engine = _USEngineBundle(us_cfg, us_trading_config, live_cfg)

            # 1. US 브로커 생성 및 연결
            if not self.dry_run:
                from src.execution.broker.kis_us import KISUSBroker, KISUSConfig

                kis_us_config = KISUSConfig.from_env()
                us_engine.broker = KISUSBroker(config=kis_us_config, token_manager=self._token_manager)

                if not await us_engine.broker.connect():
                    logger.error("[US] 브로커 연결 실패")
                    return False
                logger.info("[US] KIS US 브로커 연결 완료")

            # 2. US 포트폴리오 초기화
            us_engine.portfolio = Portfolio(
                cash=us_trading_config.initial_capital,
                initial_capital=us_trading_config.initial_capital,
                market=Market.NASDAQ,
                currency="USD",
            )

            # 실제 잔고 동기화
            if not self.dry_run and us_engine.broker:
                try:
                    balance = await us_engine.broker.get_balance()
                    balance = balance.get("account", {}) if balance else {}
                    if balance:
                        actual_capital = balance.get('total_equity') or 0
                        available_cash = balance.get('available_cash') or 0
                        if actual_capital is not None and actual_capital > 0:
                            us_engine.portfolio.initial_capital = Decimal(str(actual_capital))
                            us_engine.portfolio.cash = Decimal(str(available_cash))
                            logger.info(f"[US] 계좌 잔고: ${actual_capital:,.2f} (가용: ${available_cash:,.2f})")

                    # 기존 보유 종목 로드
                    positions = await us_engine.broker.get_positions()
                    if positions:
                        for pos in positions:
                            symbol = pos.get("symbol", "")
                            if symbol:
                                from src.core.types import Position, PositionSide
                                us_pos = Position(
                                    symbol=symbol,
                                    name=pos.get("name", symbol),
                                    side=PositionSide.LONG,
                                    quantity=int(pos.get("quantity", 0)),
                                    avg_price=Decimal(str(pos.get("avg_price", 0))),
                                    current_price=Decimal(str(pos.get("current_price", pos.get("avg_price", 0)))),
                                    market=Market.NASDAQ,
                                    currency="USD",
                                )
                                us_engine.portfolio.positions[symbol] = us_pos
                        logger.info(f"[US] 보유 포지션 {len(us_engine.portfolio.positions)}개 로드 완료")
                except Exception as e:
                    logger.warning(f"[US] 포트폴리오 동기화 실패 (무시): {e}")

            # 3. US 세션 관리자
            try:
                from src.utils.session import USSession
                us_engine.session = USSession()
                logger.info("[US] USSession 초기화 완료")
            except Exception as e:
                logger.warning(f"[US] USSession 초기화 실패 (무시): {e}")

            # 4. US 전략 로드
            us_strategies_cfg = us_cfg.get("strategies", {})
            us_engine.strategies = []

            try:
                from src.strategies.us.momentum import MomentumBreakoutStrategy as USMomentumStrategy
                us_mom_cfg = us_strategies_cfg.get("momentum", {})
                if us_mom_cfg.get("enabled", True):
                    us_engine.strategies.append(USMomentumStrategy(config=us_mom_cfg))
                    logger.info("[US] Momentum 전략 로드")
            except Exception as e:
                logger.warning(f"[US] Momentum 전략 로드 실패: {e}")

            try:
                from src.strategies.us.sepa_trend import SEPATrendStrategy as USSEPAStrategy
                us_sepa_cfg = us_strategies_cfg.get("sepa", us_strategies_cfg.get("sepa_trend", {}))
                if us_sepa_cfg.get("enabled", True):
                    us_engine.strategies.append(USSEPAStrategy(config=us_sepa_cfg))
                    logger.info("[US] SEPA Trend 전략 로드")
            except Exception as e:
                logger.warning(f"[US] SEPA 전략 로드 실패: {e}")

            try:
                from src.strategies.us.earnings_drift import EarningsDriftStrategy
                us_ed_cfg = us_strategies_cfg.get("earnings_drift", {})
                if us_ed_cfg.get("enabled", True):
                    us_engine.strategies.append(EarningsDriftStrategy(config=us_ed_cfg))
                    logger.info("[US] EarningsDrift 전략 로드")
            except Exception as e:
                logger.warning(f"[US] EarningsDrift 전략 로드 실패: {e}")

            logger.info(f"[US] 전략 로드: {[s.name for s in us_engine.strategies]}")

            # 5. US 리스크 매니저
            from src.risk.manager import RiskManager as RiskMgr
            us_engine.risk_manager = RiskMgr(
                us_trading_config.risk,
                us_trading_config.initial_capital,
                market="US",
            )

            # 6. US ExitManager
            from src.strategies.exit_manager import ExitManager, ExitConfig
            us_exit_cfg = us_cfg.get("exit_manager", {})
            us_engine.exit_manager = ExitManager(ExitConfig(
                enable_partial_exit=us_exit_cfg.get("enable_partial_exit", True),
                first_exit_pct=us_exit_cfg.get("first_exit_pct", 5.0),
                first_exit_ratio=us_exit_cfg.get("first_exit_ratio", 0.30),
                second_exit_pct=us_exit_cfg.get("second_exit_pct", 10.0),
                second_exit_ratio=us_exit_cfg.get("second_exit_ratio", 0.50),
                third_exit_pct=us_exit_cfg.get("third_exit_pct", 15.0),
                third_exit_ratio=us_exit_cfg.get("third_exit_ratio", 0.50),
                stop_loss_pct=us_exit_cfg.get("stop_loss_pct", 5.0),
                trailing_stop_pct=us_exit_cfg.get("trailing_stop_pct", 3.0),
                trailing_activate_pct=us_exit_cfg.get("trailing_activate_pct", 5.0),
                include_fees=False,  # US: zero-commission
                eod_close=us_exit_cfg.get("eod_close", False),
            ))

            # 7. US 스크리너 (DataStore + UniverseManager + StockScreener)
            try:
                from src.data.providers.yfinance import YFinanceProvider
                from src.data.store import DataStore
                from src.data.universe import UniverseManager
                from src.signals.screener.us_screener import StockScreener as USStockScreener

                us_engine.data_provider = YFinanceProvider()
                us_engine.data_store = DataStore()
                us_engine.universe_mgr = UniverseManager(
                    provider=us_engine.data_provider,
                    config=us_cfg.get("universe", {}),
                )

                # Finviz 프로바이더 (옵션)
                finviz_provider = None
                try:
                    from src.data.providers.finviz import FinvizProvider
                    finviz_token = os.getenv("FINVIZ_API_TOKEN", "")
                    finviz_provider = FinvizProvider(finviz_token)
                except Exception:
                    pass

                us_engine.screener = USStockScreener(
                    provider=us_engine.data_provider,
                    finviz=finviz_provider,
                )

                # 유니버스 로드
                pools = us_cfg.get("universe", {}).get("pools", ["sp500"])
                us_engine._universe = us_engine.universe_mgr.get_universe(pools)
                logger.info(f"[US] 유니버스: {len(us_engine._universe)} 종목")
            except Exception as e:
                logger.warning(f"[US] 스크리너/유니버스 초기화 실패 (무시): {e}")
                us_engine.screener = None
                us_engine._universe = []

            # 8. US TradeStorage
            try:
                from src.data.storage.trade_storage import TradeStorage
                us_engine.trade_storage = TradeStorage()
                await us_engine.trade_storage.connect()
                if us_engine.broker:
                    try:
                        await us_engine.trade_storage.sync_from_kis(us_engine.broker, engine=us_engine)
                    except Exception as e:
                        logger.warning(f"[US] TradeStorage KIS 동기화 실패: {e}")
                logger.info("[US] TradeStorage 초기화 완료")
            except Exception as e:
                logger.warning(f"[US] TradeStorage 초기화 실패 (무시): {e}")
                us_engine.trade_storage = None

            # 9. US WebSocket 피드 (Finnhub — 디스플레이 전용)
            finnhub_key = os.getenv("FINNHUB_API_KEY", "")
            if finnhub_key:
                try:
                    from src.data.feeds.finnhub_ws import FinnhubWSFeed
                    us_engine.ws_feed = FinnhubWSFeed(finnhub_key)
                    us_engine.ws_feed.on_trade(us_engine._on_ws_price)
                    if us_engine.portfolio.positions:
                        await us_engine.ws_feed.subscribe(list(us_engine.portfolio.positions.keys()))
                    logger.info(f"[US] Finnhub WS 초기화 완료 (구독 {len(us_engine.portfolio.positions)}개)")
                except Exception as e:
                    logger.warning(f"[US] Finnhub WS 초기화 실패 (무시): {e}")
                    us_engine.ws_feed = None
            else:
                us_engine.ws_feed = None

            # 10. KIS 실시간체결통보 WS
            hts_id = os.getenv("KIS_HTS_ID", "").strip()
            if (hts_id and hts_id.isalnum() and len(hts_id) >= 6
                    and not self.dry_run and us_engine.broker
                    and hasattr(us_engine.broker, 'config')
                    and us_engine.broker.config.env == "prod"):
                try:
                    from src.data.feeds.kis_us_ws import KISNotificationWS
                    us_engine.kis_ws = KISNotificationWS(
                        app_key=us_engine.broker.config.app_key,
                        app_secret=us_engine.broker.config.app_secret,
                        hts_id=hts_id,
                        is_mock=False,
                    )
                    us_engine.kis_ws.on_fill(us_engine._on_kis_fill)
                    logger.info(f"[US] KIS 체결통보 WS 초기화 완료 (HTS ID: {hts_id[:4]}****)")
                except Exception as e:
                    logger.warning(f"[US] KIS WS 초기화 실패 (무시): {e}")
                    us_engine.kis_ws = None
            else:
                us_engine.kis_ws = None

            # 11. US 테마 탐지기
            if finnhub_key:
                try:
                    from src.signals.sentiment.us_theme_detector import USThemeDetector
                    us_engine.theme_detector = USThemeDetector(finnhub_key)
                    logger.info("[US] 테마 탐지기 초기화 완료")
                except Exception as e:
                    logger.warning(f"[US] 테마 탐지기 초기화 실패 (무시): {e}")
                    us_engine.theme_detector = None
            else:
                us_engine.theme_detector = None

            # 12. 어닝 캘린더 프로바이더
            try:
                from src.data.providers.earnings import EarningsProvider
                us_engine.earnings_provider = EarningsProvider(finnhub_key)
            except Exception:
                us_engine.earnings_provider = None

            # 13. 센티멘트 스코어러
            us_engine.sentiment_scorer = None
            us_engine.news_provider = None

            # 14. 스크리너 캐시 로드
            if us_engine.screener and hasattr(us_engine.screener, 'load_cache'):
                try:
                    cached = us_engine.screener.load_cache()
                    if cached:
                        us_engine._last_screen_result = cached
                        logger.info(f"[US] 스크리너 캐시 로드: {len(cached.results)}종목")
                except Exception:
                    pass

            # 15. 헬스 모니터
            try:
                from src.monitoring.health_monitor import HealthMonitor
                us_engine.health_monitor = HealthMonitor(us_engine)
            except Exception:
                us_engine.health_monitor = None

            # 설정값 저장
            us_engine._screening_interval = live_cfg.get("screening_interval_min", 30) * 60
            us_engine._max_screen_symbols = live_cfg.get("max_screen_symbols", 100)
            us_engine._max_signals_per_cycle = live_cfg.get("max_signals_per_cycle", 3)
            us_engine._signal_cooldown_sec = live_cfg.get("signal_cooldown_sec", 300)
            us_engine._position_sync_sec = live_cfg.get("position_sync_sec", 30)
            us_engine._exit_check_sec = live_cfg.get("exit_check_sec", 60)
            us_engine._order_check_sec = live_cfg.get("order_check_sec", 10)
            us_engine._heartbeat_sec = live_cfg.get("heartbeat_sec", 300)
            us_engine._default_exchange = live_cfg.get("default_exchange", "NASD")

            # 번들 저장
            self._us_engine = us_engine

            # US 컨텍스트 등록
            us_ctx = MarketContext(
                market=Market.NASDAQ,
                enabled=True,
                broker=us_engine.broker,
                session=us_engine.session,
                portfolio=us_engine.portfolio,
                risk_mgr=us_engine.risk_manager,
                exit_mgr=us_engine.exit_manager,
                strategies=us_engine.strategies,
                screener=us_engine.screener,
                data_feed=us_engine.ws_feed,
                config=us_cfg,
            )
            self.engine.register_context("US", us_ctx)

            logger.info("[US] 미국 시장 초기화 완료")
            return True

        except Exception as e:
            logger.exception(f"[US] 초기화 실패: {e}")
            return False

    # ============================================================
    # 실행
    # ============================================================

    async def run(self):
        """봇 실행"""
        if not await self.initialize():
            return

        self.running = True
        logger.info("=== QWQ AI Trader 통합 봇 시작 ===")

        try:
            tasks = []

            # 1. 메인 엔진 실행 (이벤트 루프)
            tasks.append(asyncio.create_task(self.engine.run(), name="engine"))

            # 2. KR 스케줄러 태스크
            if self.market in ("kr", "both"):
                kr_ctx = self.engine.get_context("KR")
                if kr_ctx and kr_ctx.enabled:
                    try:
                        from src.schedulers.kr_scheduler import KRScheduler
                        kr_scheduler = KRScheduler(self)
                        self.kr_scheduler = kr_scheduler
                        kr_tasks = kr_scheduler.create_tasks()
                        tasks.extend(kr_tasks)
                        logger.info(f"[KR] 스케줄러 태스크 {len(kr_tasks)}개 시작")
                    except Exception as e:
                        logger.error(f"[KR] 스케줄러 생성 실패: {e}")

            # 3. US 스케줄러 태스크
            if self.market in ("us", "both"):
                us_ctx = self.engine.get_context("US")
                if us_ctx and us_ctx.enabled and self._us_engine:
                    try:
                        from src.schedulers.us_scheduler import USScheduler
                        us_scheduler = USScheduler(self._us_engine)
                        us_tasks = us_scheduler.create_tasks()
                        tasks.extend(us_tasks)
                        logger.info(f"[US] 스케줄러 태스크 {len(us_tasks)}개 시작")
                    except Exception as e:
                        logger.error(f"[US] 스케줄러 생성 실패: {e}")

            # 4. 대시보드 서버
            if self.dashboard:
                tasks.append(asyncio.create_task(
                    self.dashboard.run(), name="dashboard"
                ))
                logger.info("[대시보드] 서버 시작")

            # 5. WebSocket 피드 (KR)
            if self.ws_feed and not self.dry_run:
                tasks.append(asyncio.create_task(
                    self.ws_feed.run(), name="kr_ws_feed"
                ))

            # 모든 태스크 실행
            if tasks:
                logger.info(f"총 {len(tasks)}개 태스크 시작")
                results = await asyncio.gather(*tasks, return_exceptions=True)

                # 핵심 태스크 예외 검사
                for i, result in enumerate(results):
                    if isinstance(result, Exception):
                        task_name = tasks[i].get_name() if hasattr(tasks[i], 'get_name') else f"task-{i}"
                        logger.error(f"[태스크 종료] {task_name} 예외 발생: {result}")

        except Exception as e:
            logger.exception(f"실행 오류: {e}")
        finally:
            await self.shutdown()

    def stop(self):
        """봇 중지"""
        self.running = False
        self.engine.stop()

    async def shutdown(self):
        """종료 처리"""
        logger.info("=== QWQ AI Trader 통합 봇 종료 ===")
        self.running = False

        # KR 컨텍스트 종료
        if self.broker:
            try:
                await self.broker.disconnect()
            except Exception as e:
                logger.error(f"KR 브로커 연결 해제 실패: {e}")

        # US 컨텍스트 종료
        if self._us_engine and self._us_engine.broker:
            try:
                await self._us_engine.broker.disconnect()
            except Exception as e:
                logger.error(f"US 브로커 연결 해제 실패: {e}")

        # 대시보드 종료
        if self.dashboard:
            try:
                await self.dashboard.stop()
            except Exception as e:
                logger.error(f"대시보드 종료 실패: {e}")

        # WebSocket 종료
        if self.ws_feed:
            try:
                await self.ws_feed.disconnect()
            except Exception as e:
                logger.error(f"KR WebSocket 종료 실패: {e}")

        # 토큰 매니저 정리
        if self._token_manager:
            try:
                await self._token_manager.close()
            except Exception as e:
                logger.error(f"토큰 매니저 종료 실패: {e}")

        logger.info("종료 완료")

    # ============================================================
    # KR 헬퍼 메서드 (KRScheduler가 접근하는 인터페이스)
    # ============================================================

    async def _load_existing_positions(self):
        """KIS API에서 기존 보유 종목 로드"""
        if not self.broker:
            return
        try:
            positions = await self.broker.get_positions()
            if not positions:
                logger.info("[KR] 보유 종목 없음")
                return

            from src.core.types import Position, PositionSide
            # get_positions()는 Dict[str, Position] 반환 — .items()로 순회
            items = positions.items() if isinstance(positions, dict) else enumerate(positions)
            for symbol, pos_data in items:
                # pos_data가 Position 객체인 경우
                if hasattr(pos_data, 'quantity'):
                    if not symbol:
                        continue
                    quantity = pos_data.quantity
                    if quantity <= 0:
                        continue
                    avg_price = pos_data.avg_price
                    current_price = pos_data.current_price if pos_data.current_price and pos_data.current_price > 0 else avg_price
                    name = getattr(pos_data, 'name', None) or symbol
                    position = pos_data
                # pos_data가 dict인 경우 (하위 호환)
                else:
                    symbol = str(symbol).zfill(6) if str(symbol).isdigit() else pos_data.get("symbol", "").zfill(6)
                    if not symbol:
                        continue
                    quantity = int(pos_data.get("quantity", 0))
                    if quantity <= 0:
                        continue
                    avg_price = Decimal(str(pos_data.get("avg_price", 0)))
                    current_price = Decimal(str(pos_data.get("current_price", avg_price)))
                    name = pos_data.get("name", symbol)
                    position = Position(
                        symbol=symbol,
                        name=name,
                        side=PositionSide.LONG,
                        quantity=quantity,
                        avg_price=avg_price,
                        current_price=current_price if current_price > 0 else avg_price,
                        market=Market.KRX,
                        currency="KRW",
                    )
                self.engine.portfolio.positions[symbol] = position
                self.stock_name_cache[symbol] = name

            total_value = sum(p.market_value for p in self.engine.portfolio.positions.values())
            logger.info(
                f"[KR] 기존 보유 종목 {len(self.engine.portfolio.positions)}개 로드 "
                f"(평가금액: {total_value:,.0f}원)"
            )

            # WS 실시간 구독: 보유 종목을 최우선 구독
            if self.ws_feed and self.engine.portfolio.positions:
                pos_symbols = list(self.engine.portfolio.positions.keys())
                self.ws_feed.set_priority_symbols(pos_symbols)
                await self.ws_feed.subscribe(pos_symbols)
                logger.info(f"[KR] WS 보유 종목 {len(pos_symbols)}개 우선 구독 설정")
        except Exception as e:
            logger.error(f"[KR] 보유 종목 로드 실패: {e}")

    async def _restore_position_metadata(self, positions: dict):
        """DB에서 포지션 전략/진입시간 복원"""
        if not self.trade_journal or not hasattr(self.trade_journal, 'pool') or not self.trade_journal.pool:
            restored = 0
            for sym, pos in positions.items():
                if not pos.strategy and sym in self._symbol_strategy:
                    pos.strategy = self._symbol_strategy[sym]
                    restored += 1
            if restored:
                logger.info(f"[KR] 포지션 전략 복원 (메모리): {restored}개")
            return

        try:
            async with self.trade_journal.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT symbol, entry_strategy, entry_time FROM trades "
                    "WHERE exit_time IS NULL ORDER BY entry_time DESC"
                )
            restored = 0
            for row in rows:
                sym = row["symbol"]
                if sym in positions:
                    pos = positions[sym]
                    if not pos.strategy and row["entry_strategy"]:
                        pos.strategy = row["entry_strategy"]
                        restored += 1
                    if not pos.entry_time and row["entry_time"]:
                        pos.entry_time = row["entry_time"]
            if restored:
                logger.info(f"[KR] 포지션 전략 복원 (DB): {restored}개")
        except Exception as e:
            logger.warning(f"[KR] 포지션 메타데이터 복원 실패: {e}")

    def _get_price_history_for_atr(self, symbol: str) -> list:
        """ATR 계산용 가격 히스토리 조회 (전략 캐시에서)"""
        if self.strategy_manager:
            for _, strategy in self.strategy_manager.strategies.items():
                if hasattr(strategy, '_price_history') and symbol in strategy._price_history:
                    return strategy._price_history[symbol]
        return []

    async def _get_sector(self, symbol: str) -> Optional[str]:
        """종목 섹터 조회 (StockMaster DB 기반, 캐시)"""
        if symbol in self._sector_cache:
            return self._sector_cache[symbol]
        if self.stock_master and hasattr(self.stock_master, 'pool') and self.stock_master.pool:
            try:
                async with self.stock_master.pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT corp_cls FROM kr_stock_master WHERE ticker = $1", symbol)
                    if row and row["corp_cls"]:
                        if len(self._sector_cache) > 2000:
                            keys_to_del = list(self._sector_cache.keys())[:1000]
                            for k in keys_to_del:
                                del self._sector_cache[k]
                        self._sector_cache[symbol] = row["corp_cls"]
                        return row["corp_cls"]
            except Exception as e:
                logger.debug(f"[섹터] {symbol} 조회 실패: {e}")
        return None

    def _register_event_handlers(self):
        """이벤트 핸들러 등록"""
        # 이벤트 핸들러는 StrategyManager와 RiskManager가 엔진 초기화 시 이미 등록
        pass

    async def _load_watch_symbols(self):
        """감시 종목 로드"""
        watch_cfg = self.config.get("watch_symbols") or self.config.get("kr", "watch_symbols") or []
        self._watch_symbols = list(watch_cfg)

        # 보유 종목 추가
        for symbol in self.engine.portfolio.positions:
            if symbol not in self._watch_symbols:
                self._watch_symbols.append(symbol)

        logger.info(f"[KR] 감시 종목 {len(self._watch_symbols)}개 로드")

    async def _preload_price_history(self):
        """과거 일봉 데이터 사전 로드"""
        if not self.broker or not self._watch_symbols:
            return
        try:
            loaded = 0
            for symbol in self._watch_symbols[:50]:  # 최대 50종목
                try:
                    history = await self.broker.get_daily_prices(symbol, count=100)
                    if history and self.strategy_manager:
                        from src.core.types import Price
                        prices = []
                        for bar in history:
                            prices.append(Price(
                                symbol=symbol,
                                timestamp=bar.get("timestamp", datetime.now()),
                                open=Decimal(str(bar.get("open", 0))),
                                high=Decimal(str(bar.get("high", 0))),
                                low=Decimal(str(bar.get("low", 0))),
                                close=Decimal(str(bar.get("close", 0))),
                                volume=int(bar.get("volume", 0)),
                            ))
                        if prices:
                            for _, strategy in self.strategy_manager.strategies.items():
                                if hasattr(strategy, 'preload_history'):
                                    strategy.preload_history(symbol, prices)
                            loaded += 1
                except Exception:
                    pass
            if loaded:
                logger.info(f"[KR] 일봉 데이터 {loaded}종목 사전 로드 완료")
        except Exception as e:
            logger.warning(f"[KR] 일봉 데이터 사전 로드 실패: {e}")

    async def _fill_name_cache_from_journal(self):
        """거래 저널에서 종목명 캐시 보강"""
        if not self.trade_journal:
            return
        try:
            open_trades = self.trade_journal.get_open_trades()
            for trade in open_trades:
                if trade.symbol and trade.name and trade.symbol not in self.stock_name_cache:
                    self.stock_name_cache[trade.symbol] = trade.name
        except Exception:
            pass

    async def _on_market_data(self, event):
        """KR WebSocket 시장 데이터 콜백"""
        try:
            await self.engine.emit(event)

            # WS 실시간 청산 체크: 보유 종목이면 즉시 exit signal 확인
            if (self.kr_scheduler
                    and event.symbol in self.engine.portfolio.positions
                    and event.close is not None and event.close > 0):
                await self.kr_scheduler._check_exit_signal(event.symbol, event.close)
        except Exception as e:
            logger.error(f"[KR] 시장 데이터 이벤트 발행 실패: {e}")

    def _get_current_session(self) -> MarketSession:
        """현재 KR 세션 조회"""
        try:
            from src.utils.session import KRSession
            return KRSession().get_current_session()  # 인스턴스 생성 후 호출
        except Exception:
            return MarketSession.CLOSED


# ============================================================
# US 엔진 번들 (USScheduler 호환 인터페이스)
# ============================================================

class _USEngineBundle:
    """US LiveEngine-like 객체

    USScheduler가 접근하는 속성들을 모아둔 번들.
    실제 LiveEngine을 대체합니다.
    """

    def __init__(self, us_cfg: dict, trading_config: TradingConfig, live_cfg: dict):
        self.config_raw = us_cfg
        self.trading_config = trading_config
        self._live_cfg = live_cfg

        # 핵심 컴포넌트 (initialize에서 설정)
        self.broker = None
        self.portfolio = None
        self.session = None
        self.risk_manager = None
        self.exit_manager = None
        self.strategies: list = []
        self.screener = None
        self.data_provider = None
        self.data_store = None
        self.universe_mgr = None
        self.trade_storage = None
        self.health_monitor = None
        self.ws_feed = None
        self.kis_ws = None
        self.theme_detector = None
        self.earnings_provider = None
        self.sentiment_scorer = None
        self.news_provider = None

        # 상태
        self._universe: list = []
        self._pending_orders: Dict[str, dict] = {}
        self._pending_symbols: Set[str] = set()
        self._signal_cooldown: Dict[str, datetime] = {}
        self._exchange_cache: Dict[str, str] = {}
        self._sector_cache: Dict[str, str] = {}
        self._indicator_cache: Dict[str, dict] = {}
        self._tasks: List[asyncio.Task] = []
        self._running = False
        self.running = True
        self.recent_signals: deque = deque(maxlen=50)
        self._ws_last_exit_check: Dict[str, float] = {}
        self._daily_reset_done: Optional[date] = None
        self._symbol_strategy: Dict[str, str] = {}
        self._vol_surge_symbols: Set[str] = set()
        self._vol_surge_updated: Optional[datetime] = None
        self._earnings_today: Set[str] = set()
        self._earnings_last_refresh: Optional[date] = None
        self._finviz_last_refresh: Optional[date] = None
        self._last_screen_result = None
        self._last_screen_time: Optional[datetime] = None
        self._dynamic_symbols: Set[str] = set()
        self._dynamic_last_refresh: Optional[date] = None

        # 설정값 (initialize에서 설정)
        self._screening_interval: int = 1800
        self._max_screen_symbols: int = 100
        self._max_signals_per_cycle: int = 3
        self._signal_cooldown_sec: int = 300
        self._position_sync_sec: int = 30
        self._exit_check_sec: int = 60
        self._order_check_sec: int = 10
        self._heartbeat_sec: int = 300
        self._default_exchange: str = "NASD"

        # AppConfig-like 인터페이스 (USScheduler 호환)
        self.config = _USConfigProxy(us_cfg, trading_config)

    def _on_ws_price(self, *args, **kwargs):
        """Finnhub WS 가격 콜백 (placeholder)"""
        pass

    def _on_kis_fill(self, *args, **kwargs):
        """KIS 체결통보 콜백 (placeholder)"""
        pass


class _USConfigProxy:
    """USScheduler가 self.engine.config.trading/raw 접근 시 사용하는 프록시"""

    def __init__(self, raw: dict, trading: TradingConfig):
        self.raw = raw
        self.trading = trading

    def get(self, *keys, default=None):
        value = self.raw
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
            else:
                return default
            if value is None:
                return default
        return value


# ============================================================
# 진입점
# ============================================================

def parse_args():
    """명령줄 인자 파싱"""
    parser = argparse.ArgumentParser(description="QWQ AI Trader - 통합 트레이딩 봇")
    parser.add_argument(
        "--config", "-c",
        type=str,
        default=None,
        help="설정 파일 경로"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry run 모드 (실제 거래 없음)"
    )
    parser.add_argument(
        "--market",
        type=str,
        default="both",
        choices=["kr", "us", "both"],
        help="운영할 시장 (kr/us/both)"
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="로그 레벨"
    )
    return parser.parse_args()


async def main():
    """메인 함수"""
    args = parse_args()

    # 로거 설정
    try:
        from src.utils.logger import setup_logger, trading_logger
        log_dir = project_root / "logs" / datetime.now().strftime("%Y%m%d")
        setup_logger(
            log_level=args.log_level,
            log_dir=str(log_dir),
            enable_console=True,
            enable_file=True,
        )
        trading_logger.set_log_dir(str(log_dir))
    except ImportError:
        # 로거 미구현 시 기본 loguru 사용
        logger.info("기본 loguru 로거 사용")

    # 설정 로드
    try:
        from src.utils.config import AppConfig
        config = AppConfig.load(
            config_path=args.config,
            dotenv_path=str(project_root / ".env")
        )
    except ImportError:
        # AppConfig 미구현 시 빈 설정 사용
        logger.warning("AppConfig 미구현 — 빈 설정으로 시작")

        class _MockConfig:
            trading = TradingConfig()
            raw = {}
            def get(self, *args, **kwargs):
                return kwargs.get('default', {})
            def get_kr_config(self):
                return self.trading
            def get_us_config(self):
                return self.trading

        config = _MockConfig()

    # 프로세스 중복 체크
    if not acquire_singleton_lock():
        logger.error("싱글톤 락 획득 실패. 종료합니다.")
        sys.exit(1)

    # 봇 실행
    bot = UnifiedTradingBot(config, dry_run=args.dry_run, market=args.market)
    try:
        await bot.run()
    finally:
        release_singleton_lock()


if __name__ == "__main__":
    asyncio.run(main())
