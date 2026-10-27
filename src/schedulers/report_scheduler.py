"""One routine publishing loop; collection and urgent trading loops stay separate."""
import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from functools import lru_cache
import hashlib
import json
import uuid
from zoneinfo import ZoneInfo
from loguru import logger

from ..analytics.audience_reports import MarketSnapshot, render_engine_digest, render_market_report
from ..analytics.report_delivery import ReportStore

KST = ZoneInfo("Asia/Seoul")
SLOTS = {"morning": (7, 40), "midday": (13, 10), "after": (16, 40), "daily": (21, 0)}


async def _durable_thread(function, *args):
    """Drain a started SQLite writer before releasing the surrounding file lock."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()  # retrieve any failure; cancellation remains the caller outcome
        raise


class ReportSchedule:
    def __init__(self, store, notifier, dispatch_recorder=None, *, index_fetcher=None, clock=None):
        self.store, self.notifier = store, notifier
        self.dispatch_recorder = dispatch_recorder
        self.index_fetcher = index_fetcher
        self.clock = clock or (lambda: datetime.now(KST))

    async def tick(self, now, trading_day, market, engine_context):
        now = now.astimezone(KST)
        kind = next((k for k, (h, m) in SLOTS.items() if now.hour == h and m <= now.minute < m+10), None)
        if kind is None:
            return None
        audience = "engine" if kind == "daily" else "market"
        day = now.date().isoformat()
        if await asyncio.to_thread(self.store.receipt, audience, kind, day):
            return "already_consumed"
        if kind == "after" and trading_day:
            with self.store.input_guard(day) as acquired:
                if not acquired:
                    return "input_collecting"
                if await asyncio.to_thread(self.store.receipt, audience, kind, day):
                    return "already_consumed"
                owner = uuid.uuid4().hex
                if self.index_fetcher is not None and await _durable_thread(self.store.claim_inputs, day, owner):
                    try:
                        rows = await self.index_fetcher(now.date())
                    except Exception as error:
                        logger.warning("[대상별 보고] 지수 참고 관측 미확보: {}", type(error).__name__)
                        rows = ()
                    await _durable_thread(self.store.complete_inputs, day, owner, rows)
                    now = self.clock().astimezone(KST)
                rows = await asyncio.to_thread(self.store.load_inputs, day)
                market = replace(market, index_observations=rows)
                return await self._publish(now, trading_day, market, engine_context, kind, audience, day)
        return await self._publish(now, trading_day, market, engine_context, kind, audience, day)

    async def _publish(self, now, trading_day, market, engine_context, kind, audience, day):
        snapshot_id = hashlib.sha256(json.dumps(asdict(market), sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()
        status = None
        if audience == "market":
            if not trading_day:
                status = "skipped_holiday"
                text = None
            else:
                text = render_market_report(kind, market, now)
                if text is None:
                    status = "skipped_missing_current_market_basis" if kind == "midday" else "skipped_missing_market_basis"
        else:
            records = await asyncio.to_thread(self.store.records, day)
            deliveries = await asyncio.to_thread(self.store.delivery_issues, day)
            text = render_engine_digest(records, engine_context, now, deliveries=deliveries)
            snapshot_id = hashlib.sha256((json.dumps([records, deliveries], sort_keys=True)+engine_context).encode()).hexdigest()
        if status:
            if not await asyncio.to_thread(self.store.claim, audience, kind, day, snapshot_id):
                return "already_consumed"
            await asyncio.to_thread(self.store.finish, audience, kind, day, status)
            if status != "skipped_holiday":
                await asyncio.to_thread(self.store.record, "report_quality", f"시장 {kind} 보고 보류: 원천 시점이 확인된 필수 자료 없음. 수신시각으로 대체하지 않음.", now=now)
            return status
        result = await self.notifier.publish_report(audience, kind, day, snapshot_id, text, store=self.store)
        if kind == "morning" and result == "accepted" and self.dispatch_recorder:
            await asyncio.to_thread(self.dispatch_recorder, kr_date=day, sent_text=text,
                                    expert_consensus=None, status="sent", dispatched_brief_text=None)
        return result


@lru_cache(maxsize=8)
def _us_session_basis(day):
    """07:40 KST is after the preceding US close, including early closes."""
    try:
        import exchange_calendars as xcals
        import pandas as pd
        calendar = xcals.get_calendar("XNYS")
        at = datetime.combine(day, datetime.min.time(), KST).replace(hour=7, minute=40)
        sessions = calendar.sessions_in_range(pd.Timestamp(day)-pd.Timedelta(days=12), pd.Timestamp(day))
        closed = [s for s in sessions if calendar.session_close(s).to_pydatetime() <= at]
        return closed[-1].date().isoformat() if closed else None
    except Exception:
        return None


def market_snapshot(bot, now, is_holiday, *, us_basis_date=None):
    """Allowlisted, defensive cache copies. No provider refresh or bot passed to formatter."""
    from copy import deepcopy
    day = now.date()
    basis = day-timedelta(days=1) if now.hour < 9 else day
    for _ in range(14):
        if not is_holiday(basis):
            break
        basis -= timedelta(days=1)
    kis = getattr(bot, "kis_market_data", None)
    peek = getattr(kis, "peek_report_indices", None)
    indices = peek() if callable(peek) else ()
    orchestrator = getattr(bot, "expert_orchestrator", None)
    opinions = orchestrator.snapshot() if orchestrator is not None else {}
    council = opinions.get("sector_council")
    evidence = getattr(council, "raw_evidence", {}) or {}
    sectors = tuple({"name": name, **{k: row.get(k) for k in ("r1", "r5", "as_of", "source")}} for name, row in evidence.get("sector_scores", {}).items() if isinstance(row, dict))
    umd = getattr(bot, "us_market_data", None)
    if umd is None:
        from ..data.providers.us_market_data import get_us_market_data
        umd = get_us_market_data()
    # Existing US cache only. fetch/get_overnight methods may initiate requests.
    quotes = deepcopy(getattr(umd, "_cache", {}))
    return MarketSnapshot(tuple(indices), sectors, quotes, basis.isoformat(), us_basis_date)


async def run_audience_reports(bot):
    from ..core.engine import is_kr_market_holiday
    from ..utils.telegram import get_telegram_notifier
    from ..analytics.report_context import render_runtime_context
    from ..analytics.daily_report import record_morning_brief_dispatch
    from ..utils import loop_heartbeat as heartbeat
    fetcher = getattr(getattr(bot, "kis_market_data", None), "fetch_report_index_observations", None)
    schedule = ReportSchedule(ReportStore(), get_telegram_notifier(), record_morning_brief_dispatch,
                              index_fetcher=fetcher)
    while bot.running:
        try:
            now = datetime.now(KST)
            if any(now.hour == h and m <= now.minute < m+10 for h, m in SLOTS.values()):
                heartbeat.record_attempt("kr_audience_reports")
                us_basis = await asyncio.to_thread(_us_session_basis, now.date()) if now.hour < 9 else None
                # Cache owners run on this event loop. Copy without yielding;
                # only the calendar calculation runs in a worker thread.
                snapshot = market_snapshot(bot, now, is_kr_market_holiday, us_basis_date=us_basis)
                result = await schedule.tick(now, not is_kr_market_holiday(now.date()), snapshot, render_runtime_context(bot, now=now))
                if result in {"unknown", "rejected", "failed_configuration", "failed_validation"}:
                    heartbeat.record_failure("kr_audience_reports", result)
                else:
                    heartbeat.record_idle("kr_audience_reports", result or "보고 창 대기")
                if result and result != "already_consumed":
                    logger.info("[대상별 보고] {}", result)
            else:
                heartbeat.record_idle("kr_audience_reports", "보고 창 밖")
        except Exception as error:
            heartbeat.record_failure("kr_audience_reports", type(error).__name__)
            logger.exception("[대상별 보고] 생성/기록 실패 — 다음 루프에서 소비 상태 확인")
        await asyncio.sleep(60)
