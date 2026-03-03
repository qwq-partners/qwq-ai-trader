"""
QWQ AI Trader - KR 시장 스케줄러

ai-trader-v2의 SchedulerMixin(bot_schedulers.py)에서 추출한 독립 모듈.
모든 스케줄러 루프는 async 함수로, 엔진/봇 인스턴스를 파라미터로 받습니다.

사용법:
    from src.schedulers.kr_scheduler import KRScheduler
    scheduler = KRScheduler(bot)
    tasks = scheduler.create_tasks()
"""

import asyncio
import aiohttp
import json
import os
import re
from datetime import datetime, date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional

from loguru import logger

from ..core.engine import is_kr_market_holiday
from ..core.event import ThemeEvent, NewsEvent, FillEvent, SignalEvent, MarketDataEvent
from ..core.types import Signal, OrderSide, SignalStrength, StrategyType, MarketSession


class KRScheduler:
    """KR 시장 백그라운드 스케줄러

    ai-trader-v2의 SchedulerMixin을 독립 클래스로 변환.
    bot(TradingBot) 인스턴스의 모든 속성에 접근합니다.
    """

    _MAX_WATCH_SYMBOLS = 200

    def __init__(self, bot):
        """
        Args:
            bot: TradingBot 인스턴스 (또는 동일 인터페이스를 가진 객체)
        """
        self.bot = bot

    def create_tasks(self):
        """모든 KR 스케줄러 태스크 생성 → 리스트 반환

        Returns:
            List[asyncio.Task]: 생성된 태스크 리스트
        """
        bot = self.bot
        tasks = []

        # 테마 탐지
        if bot.theme_detector:
            tasks.append(asyncio.create_task(
                self.run_theme_detection(), name="kr_theme_detector"
            ))

        # 체결 확인
        if bot.broker:
            tasks.append(asyncio.create_task(
                self.run_fill_check(), name="kr_fill_checker"
            ))

        # 포트폴리오 동기화
        if bot.broker:
            tasks.append(asyncio.create_task(
                self.run_portfolio_sync(), name="kr_portfolio_sync"
            ))

        # 종목 스크리닝
        if bot.screener:
            tasks.append(asyncio.create_task(
                self.run_screening(), name="kr_screener"
            ))

        # REST 시세 피드
        if bot.broker:
            tasks.append(asyncio.create_task(
                self.run_rest_price_feed(), name="kr_rest_price_feed"
            ))

        # 교착 pending 정리
        tasks.append(asyncio.create_task(
            self.run_pending_cleanup(), name="kr_pending_cleanup"
        ))

        # 수급 캐시
        tasks.append(asyncio.create_task(
            self.run_supply_demand_cache(), name="kr_supply_demand_cache"
        ))

        # 일일 레포트
        tasks.append(asyncio.create_task(
            self.run_daily_report_scheduler(), name="kr_report_scheduler"
        ))

        # 진화 (LLM 리뷰)
        tasks.append(asyncio.create_task(
            self.run_evolution_scheduler(), name="kr_evolution_scheduler"
        ))

        # 주간 리밸런싱
        if bot.strategy_evolver:
            tasks.append(asyncio.create_task(
                self.run_weekly_rebalance_scheduler(), name="kr_weekly_rebalance"
            ))

        # 로그 정리
        tasks.append(asyncio.create_task(
            self.run_log_cleanup(), name="kr_log_cleanup"
        ))

        # 종목 마스터 갱신
        if bot.stock_master:
            tasks.append(asyncio.create_task(
                self.run_stock_master_refresh(), name="kr_stock_master_refresh"
            ))

        # 일봉 갱신
        if bot.broker:
            tasks.append(asyncio.create_task(
                self.run_daily_candle_refresh(), name="kr_daily_candle_refresh"
            ))

        # 배치 분석
        if bot.batch_analyzer:
            tasks.append(asyncio.create_task(
                self.run_batch_scheduler(), name="kr_batch_scheduler"
            ))

        # 헬스 모니터
        if bot.health_monitor:
            tasks.append(asyncio.create_task(
                self.run_health_monitor(), name="kr_health_monitor"
            ))

        return tasks

    # ============================================================
    # 헬퍼
    # ============================================================

    def _trim_watch_symbols(self):
        """감시 종목 리스트가 최대 수를 초과하면 오래된 비포지션 종목 제거"""
        bot = self.bot
        if len(bot._watch_symbols) <= self._MAX_WATCH_SYMBOLS:
            return
        positions = set(bot.engine.portfolio.positions.keys()) if bot.engine else set()
        config_syms = set(bot.config.get("watch_symbols") or [])
        protected = positions | config_syms
        removable = [s for s in bot._watch_symbols if s not in protected]
        excess = len(bot._watch_symbols) - self._MAX_WATCH_SYMBOLS
        if excess > 0 and removable:
            to_remove = set(removable[:excess])
            bot._watch_symbols = [s for s in bot._watch_symbols if s not in to_remove]
            logger.debug(f"[감시 종목] {len(to_remove)}개 정리 → 현재 {len(bot._watch_symbols)}개")

    def _get_current_session(self) -> MarketSession:
        """현재 세션 (봇 위임)"""
        if hasattr(self.bot, '_get_current_session'):
            return self.bot._get_current_session()
        try:
            from ..utils.session_util import SessionUtil
            return SessionUtil.get_current_session()
        except ImportError:
            return MarketSession.CLOSED

    # ============================================================
    # 스케줄러 루프 메서드 (모두 async)
    # ============================================================

    async def run_theme_detection(self):
        """테마 탐지 루프"""
        bot = self.bot
        try:
            scan_interval = bot.theme_detector.detection_interval_minutes * 60
            while bot.running:
                try:
                    themes = await bot.theme_detector.detect_themes(force=True)
                    if themes:
                        logger.info(f"[테마 탐지] {len(themes)}개 테마 감지")
                        for theme in themes:
                            event = ThemeEvent(
                                source="theme_detector",
                                name=theme.name,
                                score=theme.score,
                                keywords=theme.keywords,
                                symbols=theme.related_stocks,
                            )
                            await bot.engine.emit(event)
                        sentiments = bot.theme_detector.get_all_stock_sentiments()
                        for symbol, data in sentiments.items():
                            impact = data.get("impact", 0)
                            abs_impact = abs(impact)
                            news_threshold = (bot.config.get("scheduler") or {}).get("news_impact_threshold", 5)
                            if abs_impact >= news_threshold:
                                news_event = NewsEvent(
                                    source="theme_detector",
                                    title=data.get("reason", ""),
                                    symbols=[symbol],
                                    sentiment=impact / 10.0,
                                )
                                await bot.engine.emit(news_event)
                except Exception as e:
                    logger.warning(f"테마 스캔 오류: {e}")
                self._trim_watch_symbols()
                await asyncio.sleep(scan_interval)
        except asyncio.CancelledError:
            pass

    async def run_fill_check(self):
        """체결 확인 루프 (적응형 폴링: 미체결 유무에 따라 2초/5초)"""
        bot = self.bot
        check_interval = 5
        _fill_check_errors = 0
        try:
            while bot.running:
                try:
                    open_orders = await bot.broker.get_open_orders()
                    if open_orders:
                        fills = await bot.broker.check_fills()
                        for fill in fills:
                            logger.info(
                                f"[체결] {fill.symbol} {fill.side.value} "
                                f"{fill.quantity}주 @ {fill.price:,.0f}원"
                            )
                            event = FillEvent.from_fill(fill, source="kis_broker")
                            await bot.engine.emit(event)
                    check_interval = 2 if open_orders else 5
                    if _fill_check_errors > 0:
                        _fill_check_errors = 0
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    logger.warning(f"체결 확인 네트워크 오류: {e}")
                    _fill_check_errors += 1
                    if _fill_check_errors >= 3:
                        if bot.broker:
                            await bot.broker._ensure_token()
                        await bot._send_error_alert(
                            "ERROR",
                            f"체결 확인 연속 네트워크 오류 ({_fill_check_errors}회)",
                            str(e)
                        )
                        _fill_check_errors = 0
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"체결 확인 오류: {e}")
                    _fill_check_errors += 1
                    if _fill_check_errors >= 5:
                        await bot._send_error_alert(
                            "ERROR",
                            f"체결 확인 연속 오류 ({_fill_check_errors}회)",
                            str(e)
                        )
                        _fill_check_errors = 0
                await asyncio.sleep(check_interval)
        except asyncio.CancelledError:
            pass

    async def run_portfolio_sync(self):
        """주기적 포트폴리오 동기화 루프"""
        bot = self.bot
        await asyncio.sleep(30)
        while bot.running:
            try:
                await bot._sync_portfolio()
            except Exception as e:
                logger.error(f"동기화 루프 오류: {e}")
            await asyncio.sleep(120)

    async def run_screening(self):
        """주기적 종목 스크리닝 루프 — 봇의 _run_screening 위임"""
        bot = self.bot
        if hasattr(bot, '_run_screening'):
            await bot._run_screening()
        else:
            logger.warning("[KR 스케줄러] _run_screening 미구현 — 스킵")

    async def run_rest_price_feed(self):
        """REST 폴링 시세 피드 — 봇의 _run_rest_price_feed 위임"""
        bot = self.bot
        if hasattr(bot, '_run_rest_price_feed'):
            await bot._run_rest_price_feed()
        else:
            logger.warning("[KR 스케줄러] _run_rest_price_feed 미구현 — 스킵")

    async def run_pending_cleanup(self):
        """교착 pending 독립 정리 루프"""
        bot = self.bot
        if hasattr(bot, '_pending_cleanup_loop'):
            await bot._pending_cleanup_loop()
        else:
            logger.warning("[KR 스케줄러] _pending_cleanup_loop 미구현 — 스킵")

    async def run_supply_demand_cache(self):
        """수급 데이터 캐시 저장 루프"""
        bot = self.bot
        if hasattr(bot, '_supply_demand_cache_loop'):
            await bot._supply_demand_cache_loop()
        else:
            logger.warning("[KR 스케줄러] _supply_demand_cache_loop 미구현 — 스킵")

    async def run_daily_report_scheduler(self):
        """일일 레포트 스케줄러 — 봇의 _run_daily_report_scheduler 위임"""
        bot = self.bot
        if hasattr(bot, '_run_daily_report_scheduler'):
            await bot._run_daily_report_scheduler()
        else:
            logger.warning("[KR 스케줄러] _run_daily_report_scheduler 미구현 — 스킵")

    async def run_evolution_scheduler(self):
        """LLM 거래 리뷰 스케줄러 — 봇의 _run_evolution_scheduler 위임"""
        bot = self.bot
        if hasattr(bot, '_run_evolution_scheduler'):
            await bot._run_evolution_scheduler()
        else:
            logger.warning("[KR 스케줄러] _run_evolution_scheduler 미구현 — 스킵")

    async def run_weekly_rebalance_scheduler(self):
        """매주 토요일 00:00 전략 예산 리밸런싱"""
        bot = self.bot
        if hasattr(bot, '_run_weekly_rebalance_scheduler'):
            await bot._run_weekly_rebalance_scheduler()
        else:
            logger.warning("[KR 스케줄러] _run_weekly_rebalance_scheduler 미구현 — 스킵")

    async def run_log_cleanup(self):
        """로그/캐시 정리 스케줄러"""
        bot = self.bot
        if hasattr(bot, '_run_log_cleanup'):
            await bot._run_log_cleanup()
        else:
            logger.warning("[KR 스케줄러] _run_log_cleanup 미구현 — 스킵")

    async def run_stock_master_refresh(self):
        """종목 마스터 갱신 스케줄러"""
        bot = self.bot
        if hasattr(bot, '_run_stock_master_refresh'):
            await bot._run_stock_master_refresh()
        else:
            logger.warning("[KR 스케줄러] _run_stock_master_refresh 미구현 — 스킵")

    async def run_daily_candle_refresh(self):
        """일봉 데이터 갱신 스케줄러"""
        bot = self.bot
        if hasattr(bot, '_run_daily_candle_refresh'):
            await bot._run_daily_candle_refresh()
        else:
            logger.warning("[KR 스케줄러] _run_daily_candle_refresh 미구현 — 스킵")

    async def run_batch_scheduler(self):
        """배치 분석 스케줄러"""
        bot = self.bot
        if hasattr(bot, '_run_batch_scheduler'):
            await bot._run_batch_scheduler()
        else:
            logger.warning("[KR 스케줄러] _run_batch_scheduler 미구현 — 스킵")

    async def run_health_monitor(self):
        """헬스 모니터링 루프"""
        bot = self.bot
        if hasattr(bot, '_run_health_monitor'):
            await bot._run_health_monitor()
        else:
            logger.warning("[KR 스케줄러] _run_health_monitor 미구현 — 스킵")
