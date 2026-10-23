"""고정 trusted helper의 보고 가격만 읽는 부모. 거래/발급 권한을 만들지 않는다."""
from __future__ import annotations

import asyncio
from datetime import datetime
import math
import os
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from .report_protocol import (
    MAX_AGE_SECONDS, REQUEST_MAX_BYTES, REQUEST_TIMEOUT_SECONDS, RESPONSE_MAX_BYTES,
    canonical_bytes, make_request, strict_loads, validate_result,
)

_ARGV = ('/usr/bin/python3', '-I', '-S', '/usr/libexec/qwq-toss-report/launcher.py')
_clock = time.monotonic


def _now():
    return datetime.now(ZoneInfo('Asia/Seoul'))


class ReportHelperClient:
    def __init__(self, *, enabled):
        self._enabled = enabled
        self._process = self._spawn_task = self._close_task = None
        self._started = self._closed = self._busy = False
        self._seen = set()
        self._quotes = {}
        self._cohort_id = None
        self._valid_until = self._published_at = None
        self._expires = 0.0
        self._late_reapers = set()

    def _allowed(self):
        try:
            return not self._closed and os.environ.get('TOSS_REPORT_API') == '1' and self._enabled() is True
        except Exception:
            return False

    def _clear(self):
        self._quotes = {}
        self._cohort_id = None
        self._valid_until = self._published_at = None
        self._expires = 0.0

    def _spawn_finished(self, task):
        # shielded spawn가 호출자 취소 뒤 완료해도 자식 소유권을 놓치지 않는다.
        try:
            self._process = task.result()
        except BaseException:
            return
        if self._closed and self._process.returncode is None:
            try:
                self._process.kill()
            except ProcessLookupError:
                pass
            reaper = asyncio.create_task(self._reap_process(self._process))
            self._late_reapers.add(reaper)
            reaper.add_done_callback(self._late_reapers.discard)

    async def _exchange(self, request):
        if not self._started:
            self._started = True
            # 전체 환경·PYTHONPATH·다른 공급자 자격을 자식에 넘기지 않는다.
            env = dict(PATH='/usr/bin:/bin', LANG='C.UTF-8', TOSS_API='1')
            for name in ('TOSS_CLIENT_ID', 'TOSS_CLIENT_SECRET'):
                value = os.environ.get(name)
                if value is not None:
                    env[name] = value
            self._spawn_task = asyncio.create_task(asyncio.create_subprocess_exec(
                *_ARGV, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=env, limit=RESPONSE_MAX_BYTES,
            ))
            self._spawn_task.add_done_callback(self._spawn_finished)
            self._process = await asyncio.shield(self._spawn_task)
        if not self._allowed() or self._process is None or self._process.returncode is not None:
            raise RuntimeError('helper_unavailable')
        payload = canonical_bytes(request)
        if len(payload) > REQUEST_MAX_BYTES:
            raise ValueError('request_too_large')
        self._process.stdin.write(payload + b'\n')
        await self._process.stdin.drain()
        raw = await self._process.stdout.readuntil(b'\n')
        return strict_loads(raw[:-1], maximum=RESPONSE_MAX_BYTES)

    async def _bounded_exchange(self, request, deadline):
        exchange = asyncio.create_task(self._exchange(request))
        try:
            while True:
                if not self._allowed():
                    raise RuntimeError('helper_disabled')
                remaining = deadline - _clock()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                done, _ = await asyncio.wait((exchange,), timeout=min(.1, remaining))
                if exchange in done:
                    return exchange.result()
        finally:
            if not exchange.done():
                exchange.cancel()
            await asyncio.gather(exchange, return_exceptions=True)

    async def prepare_report(self, cohort, *, timeout=REQUEST_TIMEOUT_SECONDS):
        self._clear()
        if not self._allowed() or self._busy:
            return False
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            return False
        timeout = min(timeout, REQUEST_TIMEOUT_SECONDS)
        started_at = _clock()
        requested_at = _now()
        deadline = started_at + timeout
        try:
            request = make_request(cohort, request_id=uuid4().hex, now=requested_at)
        except Exception:
            return False
        key = cohort.report_date.isoformat(), cohort.cohort_id
        # 같은 날짜의 새 cohort도 이미 실행한16:00 슬롯을 다시 조회하지 않는다.
        if any(day == key[0] for day, _ in self._seen):
            return False
        self._seen.add(key)
        self._busy = True
        try:
            response = await self._bounded_exchange(request, deadline)
            now = _now()
            quotes, valid_until = validate_result(response, request=request, now=now)
            if (not self._allowed() or _clock() >= deadline or self._process is None
                    or self._process.returncode is not None):
                raise RuntimeError('helper_unavailable')
            if not quotes:
                return False
            self._quotes, self._valid_until, self._published_at = quotes, valid_until, now
            self._cohort_id = cohort.cohort_id
            # 요청 중 벽시계가 역행해도 이미 소비한 시간은 회복하지 않는다.
            self._expires = min(
                _clock() + (valid_until - now).total_seconds(),
                started_at + (valid_until - requested_at).total_seconds(),
            )
            return True
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception:
            await self.close()
            return False
        finally:
            self._busy = False

    def get_report_quote(self, symbol, *, cohort=None):
        now = _now()
        if (not self._allowed() or self._process is None or self._process.returncode is not None
                or self._valid_until is None or self._published_at is None
                or not self._published_at <= now < self._valid_until or _clock() >= self._expires):
            self._clear()
            return None
        if cohort is not None and cohort.cohort_id != self._cohort_id:
            return None
        quote = self._quotes.get(symbol)
        if (quote is not None and quote.observed_at <= quote.fetched_at <= now
                and (now - quote.observed_at).total_seconds() <= MAX_AGE_SECONDS):
            return quote
        return None

    @staticmethod
    async def _reap_process(process):
        # StreamReader 상한 오류 뒤 멈춘 pipe도 배출해야 Process.wait가 끝난다.
        # communicate()와 달리 전체 출력을 메모리에 모으지 않는다.
        async def discard():
            if process.stdout is not None:
                while await process.stdout.read(16384):
                    pass

        drain = asyncio.create_task(discard())
        try:
            if process.stdin is not None:
                process.stdin.close()
            for action in (None, process.terminate, process.kill):
                if action is not None and process.returncode is None:
                    try:
                        action()
                    except ProcessLookupError:
                        pass
                try:
                    await asyncio.wait_for(process.wait(), 2.0)
                    return
                except asyncio.TimeoutError:
                    continue
        finally:
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)
            # 자식 외 프로세스가 pipe를 붙잡는 비정상 경우에도 transport를 남기지 않는다.
            # asyncio Process에는 공개 close API가 없어 CPython transport를 닫는다.
            process._transport.close()

    async def _close_resources(self):
        if self._spawn_task is not None and not self._spawn_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._spawn_task), 2.0)
            except (Exception, asyncio.CancelledError):
                # _spawn_finished owns a late completion; never spawn again.
                pass
        if self._spawn_task is not None and self._spawn_task.done() and not self._spawn_task.cancelled():
            try:
                # done callback가 아직 실행 전이어도 완료된 spawn의 소유권을 직접 회수한다.
                self._process = self._spawn_task.result()
            except Exception:
                pass
        process = self._process
        if process is not None:
            if not self._late_reapers:
                await self._reap_process(process)
        if self._late_reapers:
            await asyncio.gather(*tuple(self._late_reapers), return_exceptions=True)

    async def close(self):
        self._closed = True
        self._clear()
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_resources())
        await asyncio.shield(self._close_task)
