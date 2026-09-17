"""저장된 송신권을 받은 KR 거래 전용 전송 경계. 재전송하지 않는다.

이 모듈은 intent를 만들지 않는다. 호출자는 durable claim을 먼저 저장하고
finally에서 결과를 기록해야 한다. 취소/프로세스 종료 시 claim은 UNKNOWN으로
대사해야 하며 이 함수를 재호출해 복구하지 않는다.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
import json
from typing import Callable

from .guards import GuardDecision


def _payload_snapshot(payload: dict) -> dict:
    """첫 await 전에 전송 대상을 고정하고 JSON 변환의 손실/NaN을 거부한다."""
    def validate(value):
        if value is None or type(value) in (str, int, bool, float):
            return
        if type(value) is list:
            for item in value:
                validate(item)
            return
        if type(value) is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError('payload 키는 문자열이어야 합니다')
                validate(item)
            return
        raise ValueError('payload는 JSON 값만 지원합니다')
    if type(payload) is not dict:
        raise ValueError('payload는 dict여야 합니다')
    snapshot = deepcopy(payload)
    validate(snapshot)
    # 기본 JSON 인코더가 tuple/key 변환과 비유한 숫자를 허용하는 경로를 닫는다.
    return json.loads(json.dumps(snapshot, allow_nan=False))


class TransportStatus(str, Enum):
    NOT_SENT = 'not_sent'
    ACKNOWLEDGED = 'acknowledged'
    REJECTED = 'rejected'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class TransportResult:
    status: TransportStatus
    reason: str
    data: dict = field(default_factory=dict, repr=False)


class GuardedKISTransport:
    """실제 KISBroker 연결/헤더/공용 limiter를 재사용하는 단일 POST.

    guard는 정상 승인 시 process-local permit도 동기 소비해야 한다.
    aiohttp 내부 대기 또는 거래소 접수 시점까지 위험 불변을 보장하지 않는다.
    """

    _PATHS = {'submit': 'order-cash', 'cancel': 'order-rvsecncl', 'modify': 'order-rvsecncl'}

    def __init__(self, broker):
        self._broker = broker

    async def send(self, command: str, tr_id: str, payload: dict,
                   guard: Callable[[], GuardDecision]) -> TransportResult:
        if command not in self._PATHS:
            return TransportResult(TransportStatus.NOT_SENT, 'unknown_command')
        broker = self._broker
        dispatched = False
        try:
            prepared_payload = _payload_snapshot(payload)
            if not broker._session or broker._session.closed:
                if not await broker.connect():
                    return TransportResult(TransportStatus.NOT_SENT, 'connection_unavailable')
            if broker._token is None and not await broker._ensure_token():
                return TransportResult(TransportStatus.NOT_SENT, 'token_unavailable')
            hashkey = await broker._get_hashkey(prepared_payload)
            if not isinstance(hashkey, str) or not hashkey:
                return TransportResult(TransportStatus.NOT_SENT, 'hashkey_unavailable')
            await broker._rate_limit(tr_id)
            headers = dict(broker._get_headers(tr_id))
            if any(type(key) is not str or type(value) is not str
                   for key, value in headers.items()):
                return TransportResult(TransportStatus.NOT_SENT, 'invalid_headers')
            headers['hashkey'] = hashkey
            url = f'{broker.config.base_url}/uapi/domestic-stock/v1/trading/{self._PATHS[command]}'
            # 여기부터 session.post 호출까지 application await 금지.
            decision = guard()
            if not isinstance(decision, GuardDecision) or not decision.allowed:
                reason = decision.reason if isinstance(decision, GuardDecision) else 'invalid_guard_result'
                return TransportResult(TransportStatus.NOT_SENT, reason)
            dispatched = True
            async with broker._session.post(url, headers=headers, json=prepared_payload) as response:
                if response.status != 200:
                    return TransportResult(TransportStatus.UNKNOWN, 'http_response_unconfirmed')
                data = await response.json()
                if not isinstance(data, dict):
                    return TransportResult(TransportStatus.UNKNOWN, 'invalid_response')
                if data.get('rt_cd') == '0':
                    return TransportResult(TransportStatus.ACKNOWLEDGED, 'command_acknowledged', data)
                if data.get('rt_cd') == '1' and isinstance(data.get('msg_cd'), str) and data['msg_cd']:
                    return TransportResult(TransportStatus.REJECTED, 'command_rejected', data)
                return TransportResult(TransportStatus.UNKNOWN, 'invalid_response')
        except asyncio.CancelledError:
            # durable claim은 호출자/store에 남는다. 종료 신호는 삼키지 않는다.
            raise
        except Exception:
            # 예외/본문/계좌/자격 원문은 상태 사유에 넣지 않는다.
            status = TransportStatus.UNKNOWN if dispatched else TransportStatus.NOT_SENT
            return TransportResult(status, 'dispatch_unconfirmed' if dispatched else 'preparation_failed')
