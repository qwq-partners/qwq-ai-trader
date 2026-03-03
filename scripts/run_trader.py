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
import signal
import sys
import os
import fcntl
from datetime import datetime, date
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional, Set, List

# 프로젝트 루트를 path에 추가
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "src"))

from loguru import logger

from src.core.engine import UnifiedEngine, StrategyManager, RiskManager, is_kr_market_holiday, set_kr_market_holidays
from src.core.types import TradingConfig, Market, MarketSession
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

    # 1단계: 기존 프로세스 종료
    try:
        import psutil
        my_pid = os.getpid()
        others = []
        for proc in psutil.process_iter(['pid', 'cmdline']):
            try:
                pid = proc.info['pid']
                if pid == my_pid:
                    continue
                cmdline = ' '.join(proc.info.get('cmdline') or [])
                if 'run_trader.py' in cmdline and 'grep' not in cmdline:
                    others.append(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if others:
            logger.warning(f"기존 트레이더 프로세스 발견: {others} — 종료 시도")
            for pid in others:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            time.sleep(3)
            for pid in others:
                try:
                    if psutil.pid_exists(pid):
                        os.kill(pid, signal.SIGKILL)
                        logger.warning(f"PID {pid} SIGKILL 전송")
                except ProcessLookupError:
                    pass
            time.sleep(1)
    except ImportError:
        logger.warning("psutil 미설치 — 기존 프로세스 확인 생략")

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

        # 시장별 컴포넌트 (initialize에서 설정)
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

    async def initialize(self) -> bool:
        """컴포넌트 초기화"""
        try:
            logger.info("=== QWQ AI Trader 통합 봇 초기화 ===")
            logger.info(f"Dry Run: {self.dry_run}")
            logger.info(f"Market: {self.market}")

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

            logger.info("=== 통합 봇 초기화 완료 ===")
            return True

        except Exception as e:
            logger.exception(f"초기화 실패: {e}")
            return False

    async def _initialize_kr(self) -> bool:
        """KR 시장 초기화 (placeholder — 실제 구현 시 run_trader.py의 initialize 로직 이식)"""
        logger.info("[KR] 한국 시장 초기화 시작...")

        kr_ctx = MarketContext(
            market=Market.KRX,
            enabled=True,
            config=self.config.get("kr") or {},
        )

        # TODO: 실제 KR 초기화 로직 (브로커, 전략, 스크리너 등)
        # 이 부분은 ai-trader-v2의 TradingBot.initialize()에서 이식
        # 현재는 스텁으로 컨텍스트만 등록

        self.engine.register_context("KR", kr_ctx)
        logger.info("[KR] 한국 시장 컨텍스트 등록 완료")
        return True

    async def _initialize_us(self) -> bool:
        """US 시장 초기화 (placeholder — 실제 구현 시 LiveEngine.initialize 로직 이식)"""
        logger.info("[US] 미국 시장 초기화 시작...")

        us_ctx = MarketContext(
            market=Market.NASDAQ,
            enabled=True,
            config=self.config.get("us") or {},
        )

        # TODO: 실제 US 초기화 로직 (브로커, 전략, 스크리너 등)
        # 이 부분은 ai-trader-us의 LiveEngine.initialize()에서 이식
        # 현재는 스텁으로 컨텍스트만 등록

        self.engine.register_context("US", us_ctx)
        logger.info("[US] 미국 시장 컨텍스트 등록 완료")
        return True

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
                    # KR 스케줄러는 봇의 속성에 접근하므로 self를 전달
                    # TODO: KRScheduler(self).create_tasks() 사용
                    logger.info("[KR] KR 스케줄러 태스크 대기 (구현 예정)")

            # 3. US 스케줄러 태스크
            if self.market in ("us", "both"):
                us_ctx = self.engine.get_context("US")
                if us_ctx and us_ctx.enabled:
                    # US 스케줄러는 US 엔진에 접근
                    # TODO: USScheduler(us_engine).create_tasks() 사용
                    logger.info("[US] US 스케줄러 태스크 대기 (구현 예정)")

            # 4. 대시보드 서버
            dashboard_cfg = self.config.get("dashboard") or {}
            if dashboard_cfg.get("enabled", True):
                # TODO: DashboardServer 통합
                logger.info("[대시보드] 대시보드 서버 대기 (구현 예정)")

            # 모든 태스크 실행
            if tasks:
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
        kr_ctx = self.engine.get_context("KR")
        if kr_ctx:
            if kr_ctx.broker:
                try:
                    await kr_ctx.broker.disconnect()
                except Exception as e:
                    logger.error(f"KR 브로커 연결 해제 실패: {e}")

        # US 컨텍스트 종료
        us_ctx = self.engine.get_context("US")
        if us_ctx:
            if us_ctx.broker:
                try:
                    await us_ctx.broker.disconnect()
                except Exception as e:
                    logger.error(f"US 브로커 연결 해제 실패: {e}")

        logger.info("종료 완료")


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
            def get(self, *args, **kwargs):
                return {}

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
