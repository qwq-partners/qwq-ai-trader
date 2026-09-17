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
from .requests import KISRequestBuilder, PreparedTradeRequest, RequestValidationError


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

    def __init__(self, broker, *, request_builder: KISRequestBuilder | None = None):
        self._broker = broker
        if request_builder is not None and type(request_builder) is not KISRequestBuilder:
            raise ValueError('invalid_request_builder')
        self._request_builder = request_builder

    def _broker_scope_matches(self, request: PreparedTradeRequest) -> bool:
        config, account = self._broker.config, request.account
        pairs = ((config.account_no, account.account_no),
                 (config.account_product_cd, account.account_product_cd),
                 (config.env, account.environment), (config.base_url, account.endpoint))
        return all(type(actual) is str and actual == expected for actual, expected in pairs)

    async def send_prepared(self, request: PreparedTradeRequest,
                            guard: Callable[[PreparedTradeRequest], GuardDecision]) -> TransportResult:
        """실제 준비 요청을 최종 guard에 전달한다. owner 예약/claim 검사는 호출자 책임이다.

        기존 raw send와 별도인 opt-in 경계이며 운영 broker에는 아직 설치하지 않는다.
        hashkey helper의 사본 변형도 거부하고 마지막 검증 이후 단회 POST만 수행한다.
        """
        builder, broker = self._request_builder, self._broker
        if builder is None:
            return TransportResult(TransportStatus.NOT_SENT, 'request_builder_required')
        dispatched = False
        try:
            prepared = deepcopy(request)
            builder.validate(prepared)
            fingerprint = prepared.fingerprint
            if not self._broker_scope_matches(prepared):
                return TransportResult(TransportStatus.NOT_SENT, 'request_broker_scope_changed')
            if not broker._session or broker._session.closed:
                if not await broker.connect():
                    return TransportResult(TransportStatus.NOT_SENT, 'connection_unavailable')
            if not self._broker_scope_matches(prepared):
                return TransportResult(TransportStatus.NOT_SENT, 'request_broker_scope_changed')
            if broker._token is None and not await broker._ensure_token():
                return TransportResult(TransportStatus.NOT_SENT, 'token_unavailable')
            if not self._broker_scope_matches(prepared):
                return TransportResult(TransportStatus.NOT_SENT, 'request_broker_scope_changed')
            hash_payload = prepared.body()
            hashkey = await broker._get_hashkey(hash_payload)
            if _payload_snapshot(hash_payload) != prepared.body():
                return TransportResult(TransportStatus.NOT_SENT, 'hashkey_payload_changed')
            if type(hashkey) is not str or not hashkey:
                return TransportResult(TransportStatus.NOT_SENT, 'hashkey_unavailable')
            if not self._broker_scope_matches(prepared):
                return TransportResult(TransportStatus.NOT_SENT, 'request_broker_scope_changed')
            await broker._rate_limit(prepared.tr_id)
            # 마지막 application await 이후: 요청/config/header와 실제 owner guard를 대조한다.
            builder.validate(prepared)
            if not self._broker_scope_matches(prepared):
                return TransportResult(TransportStatus.NOT_SENT, 'request_broker_scope_changed')
            headers = dict(broker._get_headers(prepared.tr_id))
            if (any(type(key) is not str or type(value) is not str for key, value in headers.items())
                    or headers.get('tr_id') != prepared.tr_id):
                return TransportResult(TransportStatus.NOT_SENT, 'header_request_mismatch')
            headers['hashkey'] = hashkey
            decision = guard(prepared)
            if (type(decision) is not GuardDecision or type(decision.allowed) is not bool
                    or type(decision.reason) is not str):
                return TransportResult(TransportStatus.NOT_SENT, 'invalid_guard_result')
            if not decision.allowed:
                return TransportResult(TransportStatus.NOT_SENT, decision.reason)
            builder.validate(prepared)
            if prepared.fingerprint != fingerprint:
                return TransportResult(TransportStatus.NOT_SENT, 'prepared_request_changed')
            if not self._broker_scope_matches(prepared):
                return TransportResult(TransportStatus.NOT_SENT, 'request_broker_scope_changed')
            payload = prepared.body()
            url = prepared.account.endpoint + prepared.path
            dispatched = True
            async with broker._session.post(url, headers=headers, json=payload) as response:
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
            raise
        except RequestValidationError:
            return TransportResult(TransportStatus.UNKNOWN if dispatched else TransportStatus.NOT_SENT,
                                   'dispatch_unconfirmed' if dispatched else 'invalid_prepared_request')
        except Exception:
            return TransportResult(TransportStatus.UNKNOWN if dispatched else TransportStatus.NOT_SENT,
                                   'dispatch_unconfirmed' if dispatched else 'preparation_failed')

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
