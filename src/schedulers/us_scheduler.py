"""
QWQ AI Trader - US 시장 스케줄러

ai-trader-us의 LiveEngine에서 추출한 백그라운드 태스크 모듈.
USScheduler는 US LiveEngine 인스턴스를 파라미터로 받아
모든 스케줄러 태스크를 독립 관리합니다.

사용법:
    from src.schedulers.us_scheduler import USScheduler
    scheduler = USScheduler(us_engine)
    tasks = scheduler.create_tasks()
"""

import asyncio
import random
import uuid
from datetime import datetime, date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Set

from loguru import logger


class USScheduler:
    """US 시장 백그라운드 스케줄러

    ai-trader-us LiveEngine의 백그라운드 태스크를 독립 클래스로 추출.
    engine 인스턴스의 모든 속성에 접근합니다.

    태스크:
    1. _screening_loop (15분) — 유니버스 스캔 → 전략 시그널 → 주문
    2. _exit_check_loop (15초) — 보유 포지션 청산 체크 [KIS REST 실시간 기준]
    3. _portfolio_sync_loop (30초) — KIS 잔고 ↔ 로컬 Portfolio 동기화
    4. _order_check_loop (10초) — 미체결 주문 상태 폴링
    5. _eod_close_loop (30초) — 마감 15분 전 DAY 포지션 청산
    6. _heartbeat_loop (5분) — 상태 로깅
    7. _screener_loop (60분) — S&P500+400 전종목 점수 계산 (pool 갱신)
    8. _watchlist_loop (5분) — 상위 25 + 보유 종목 Finviz 실시간 모니터링
    9. _volume_surge_loop (15분) — KIS 거래량급증 API
    10. _theme_detection_loop (30분) — US 테마 탐지
    """

    def __init__(self, engine):
        """
        Args:
            engine: US LiveEngine 인스턴스
        """
        self.engine = engine

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

        # Finnhub WS (디스플레이 전용)
        if eng.ws_feed:
            tasks.append(asyncio.create_task(eng.ws_feed.start(), name="us_finnhub_ws"))

        # KIS 실시간체결통보 WS
        if eng.kis_ws:
            tasks.append(asyncio.create_task(eng.kis_ws.start(), name="us_kis_ws"))

        # 거래량급증 루프
        if hasattr(eng.broker, "get_volume_surge"):
            tasks.append(asyncio.create_task(self.volume_surge_loop(), name="us_vol_surge"))

        return tasks

    # ============================================================
    # 태스크 1: 스크리닝 루프
    # ============================================================

    async def screening_loop(self):
        """유니버스 스캔 → 전략 시그널 → 주문 — engine._screening_loop 위임"""
        eng = self.engine
        if hasattr(eng, '_screening_loop'):
            await eng._screening_loop()
        else:
            logger.warning("[US 스케줄러] _screening_loop 미구현")

    # ============================================================
    # 태스크 2: 청산 체크 루프
    # ============================================================

    async def exit_check_loop(self):
        """보유 포지션 청산 체크 — engine._exit_check_loop 위임"""
        eng = self.engine
        if hasattr(eng, '_exit_check_loop'):
            await eng._exit_check_loop()
        else:
            logger.warning("[US 스케줄러] _exit_check_loop 미구현")

    # ============================================================
    # 태스크 3: 포트폴리오 동기화
    # ============================================================

    async def portfolio_sync_loop(self):
        """KIS 잔고 ↔ 로컬 Portfolio 동기화 — engine._portfolio_sync_loop 위임"""
        eng = self.engine
        if hasattr(eng, '_portfolio_sync_loop'):
            await eng._portfolio_sync_loop()
        else:
            logger.warning("[US 스케줄러] _portfolio_sync_loop 미구현")

    # ============================================================
    # 태스크 4: 주문 상태 체크
    # ============================================================

    async def order_check_loop(self):
        """미체결 주문 상태 폴링 — engine._order_check_loop 위임"""
        eng = self.engine
        if hasattr(eng, '_order_check_loop'):
            await eng._order_check_loop()
        else:
            logger.warning("[US 스케줄러] _order_check_loop 미구현")

    # ============================================================
    # 태스크 5: EOD 청산
    # ============================================================

    async def eod_close_loop(self):
        """마감 15분 전 DAY 포지션 청산 — engine._eod_close_loop 위임"""
        eng = self.engine
        if hasattr(eng, '_eod_close_loop'):
            await eng._eod_close_loop()
        else:
            logger.warning("[US 스케줄러] _eod_close_loop 미구현")

    # ============================================================
    # 태스크 6: Heartbeat
    # ============================================================

    async def heartbeat_loop(self):
        """상태 로깅 + 헬스 모니터링 — engine._heartbeat_loop 위임"""
        eng = self.engine
        if hasattr(eng, '_heartbeat_loop'):
            await eng._heartbeat_loop()
        else:
            logger.warning("[US 스케줄러] _heartbeat_loop 미구현")

    # ============================================================
    # 태스크 7: 스크리너 루프
    # ============================================================

    async def screener_loop(self):
        """유니버스 스크리닝 (60분 주기) — engine._screener_loop 위임"""
        eng = self.engine
        if hasattr(eng, '_screener_loop'):
            await eng._screener_loop()
        else:
            logger.warning("[US 스케줄러] _screener_loop 미구현")

    # ============================================================
    # 태스크 8: 워치리스트 루프
    # ============================================================

    async def watchlist_loop(self):
        """상위 후보 + 보유 포지션 모니터링 — engine._watchlist_loop 위임"""
        eng = self.engine
        if hasattr(eng, '_watchlist_loop'):
            await eng._watchlist_loop()
        else:
            logger.warning("[US 스케줄러] _watchlist_loop 미구현")

    # ============================================================
    # 태스크 9: 거래량급증 루프
    # ============================================================

    async def volume_surge_loop(self):
        """KIS 거래량급증 API 조회 — engine._volume_surge_loop 위임"""
        eng = self.engine
        if hasattr(eng, '_volume_surge_loop'):
            await eng._volume_surge_loop()
        else:
            logger.warning("[US 스케줄러] _volume_surge_loop 미구현")

    # ============================================================
    # 태스크 10: 테마 탐지 루프
    # ============================================================

    async def theme_detection_loop(self):
        """US 테마 탐지 — engine._theme_detection_loop 위임"""
        eng = self.engine
        if hasattr(eng, '_theme_detection_loop'):
            await eng._theme_detection_loop()
        else:
            logger.warning("[US 스케줄러] _theme_detection_loop 미구현")
