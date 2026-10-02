"""KIS 실행 원장의 비동기 수명주기와 메모리 조회용 복구 상태.

과거 session의 execution은 여기서 Fill로 만들지 않는다. 재시작 hold는
포트폴리오 pending과 별개이며 보호 SELL 경로를 잠그지 않는다.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from loguru import logger

from .execution_ledger import ExecutionLedger


class ExecutionHistory:
    def __init__(self, path: Path, scope: str):
        self.ledger = ExecutionLedger(path, scope)
        self.session_id = uuid4().hex
        self._snapshot = {'session_id': self.session_id, 'prior_unclean': False, 'orders': {}}
        self._opened = False
        self._closed = False
        self._opening = asyncio.Lock()
        self._writing = asyncio.Lock()
        self.fault = None
        self.keys = {}

    def fail(self, reason: str):
        # 경로/응답/계좌 자료를 로그에 복사하지 않는다.
        if self.fault is None:
            self.fault = reason
            logger.error('[실행원장] 기록 경계 실패 — 신규 매수/분할 매도 보류, 명시 대사 필요')

    @property
    def session_recorded(self):
        return self._opened and not self._closed

    async def open(self):
        async with self._opening:
            if self._opened or self.fault or self._closed:
                return
            try:
                self._snapshot = await self.ledger.open(self.session_id)
                self._opened = True
            except asyncio.CancelledError:
                self.fail('open_cancelled')
                raise
            except Exception:
                self.fail('open_failed')

    async def _write(self, operation, *args, **kwargs):
        await self.open()
        async with self._writing:
            if self.fault or self._closed:
                raise RuntimeError('실행 원장 사용 불가')
            try:
                value = await getattr(self.ledger, operation)(*args, **kwargs)
                self._snapshot = await self.ledger.snapshot()
                return value
            except asyncio.CancelledError:
                self.fail('write_cancelled')
                raise
            except Exception:
                self.fail('write_failed')
                raise

    async def intent(self, order, order_date):
        if order.id in self.keys:
            # 같은 객체 재전송을 새 의도로 바꾸지 않는다.
            raise ValueError('이미 제출 의도가 기록된 주문')
        key = f'{self.session_id}:{order.id}'
        self.keys[order.id] = key
        await self._write('intent', key, {
            'local_id': order.id, 'symbol': order.symbol, 'side': order.side.value,
            'quantity': order.quantity, 'order_date': order_date,
            'strategy': order.strategy or '', 'reason': order.reason or '',
            'partial_exit': order.partial_exit is True,
        })

    async def accepted(self, order_id, odno, orgno):
        await self._write('accepted', self.keys[order_id], odno, orgno)

    async def outcome(self, order_id, status):
        key = self.keys.get(order_id)
        if key is not None:
            await self._write('outcome', key, status)

    async def observe(self, order_id, quantity, average, terminal=False):
        key = self.keys.get(order_id)
        if key is None:
            self.fail('untracked_order')
            raise ValueError('원장 주문 의도 없음')
        return await self._write('observe', key, quantity, Decimal(str(average)), terminal=terminal)

    async def receipt(self, execution_id, stage):
        await self._write('receipt', execution_id, stage)

    async def close(self):
        if not self._opened or self.fault or self._closed:
            return False
        result = await self._write('close_session')
        self._closed = result is True
        return result

    @staticmethod
    def _unresolved(record):
        if record['status'] in ('not_sent', 'rejected'):
            return False
        if record['status'] in ('unknown', 'modified'):
            return True
        terminal = record['terminal_quantity']
        return (terminal is None or terminal != record['observed_quantity']
                or any(not e['handoff_returned'] for e in record['executions']))

    def hold(self, symbol=None):
        if self.fault:
            return '실행 원장 오류: 명시 대사 필요'
        if not self._opened or self._closed:
            return '실행 원장 초기화/종료 경계'
        if self._snapshot['prior_unclean']:
            return '이전 실행 정상 종료 미확인: 과거 체결 자동 재생 금지'
        for record in self._snapshot['orders'].values():
            uncertain = (record['session_id'] != self.session_id and self._unresolved(record)
                         or record['status'] in ('unknown', 'modified'))
            if uncertain and (symbol is None or record['facts']['symbol'] == symbol):
                return '주문·체결 처리 미확인: 명시 대사 필요'
        return None

    def report(self):
        result = deepcopy(self._snapshot)
        result['status'] = ('storage_fault' if self.fault else
                            'recovery_required' if self.hold() else 'ready')
        result['reason'] = self.hold()
        result['baseline_inclusion'] = 'unverified'
        result['journal_persistence'] = 'unverified'
        result['replay_allowed'] = False
        return result

    def pending_handoffs(self, symbol=None):
        """현재 실행의 관측 체결 중 후처리 반환이 아직 저장되지 않은 개수."""
        return sum(
            not execution['handoff_returned']
            for record in self._snapshot['orders'].values()
            if record['session_id'] == self.session_id
            and (symbol is None or record['facts']['symbol'] == symbol)
            for execution in record['executions']
        )
