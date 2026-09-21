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

from ...risk import kill_switch
from ...utils import audit_log
from .guards import GuardDecision
from .lifecycle import CommandKind
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


def _record_outcome(result: TransportResult, fields: dict) -> TransportResult:
    """응답/예외 뒤의 결과를 원장에 남긴다. 기록 실패는 결과를 바꾸지 않는다.

    ACK를 UNKNOWN으로 만들면 실제로 접수된 주문이 미확인으로 뒤집히므로 삼킨다.
    아래 try/except 는 방어로 남겨 둔 것이다 — 실제 audit_log.record 는 내부에서
    모든 예외를 삼키므로(src/utils/audit_log.py) 여기까지 올라오는 예외는 없다.
    """
    try:
        audit_log.record(
            audit_log.EV_ACCEPT if result.status is TransportStatus.ACKNOWLEDGED else audit_log.EV_REJECT,
            reason=result.reason,
            unconfirmed=True if result.status is TransportStatus.UNKNOWN else None,
            **fields,
        )
    except Exception:
        pass
    return result


class GuardedKISTransport:
    """실제 KISBroker 연결/헤더/공용 limiter를 재사용하는 단일 POST.

    guard는 정상 승인 시 process-local permit도 동기 소비해야 한다.
    aiohttp 내부 대기 또는 거래소 접수 시점까지 위험 불변을 보장하지 않는다.

    send_prepared 는 운영 submit_order 와 같은 두 안전장치를 통과한다 — 킬스위치
    (SUBMIT/MODIFY 만, CANCEL 제외)와 감사 원장(시도·결과). raw send 에는 둘 다 없다.
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

        guard 승인 뒤 POST 직전(동기 구간)에:
          - 킬스위치: SUBMIT/MODIFY 만 검사한다(CANCEL 은 위험을 줄이는 명령이라 제외 —
            현행 KISBroker.cancel_order 와 같다). 차단이면 원문은 EV_BLOCKED 행에만 남기고
            상태는 NOT_SENT/'kill_switch_blocked' 다.
          - 감사 원장: 시도를 EV_SUBMIT(취소는 EV_CANCEL)로 먼저 남긴다.
        고정되는 것은 두 호출의 **위치**다(마지막 await 뒤·POST 앞). 실제 audit_log.record 는
        예외를 삼키므로 원장 쓰기 실패는 주문을 막지 않는다 — 현행 submit_order 와 같은
        best-effort 이고, 원장 때문에 보호 주문을 막는 쪽이 더 위험하다. 킬스위치도 플래그
        디렉터리 접근 실패 시 허용한다(fail-open, kill_switch._read_flag) — attach 설치 시
        그 디렉터리의 가용성이 긴급 정지의 전제다.
        응답/예외 뒤에는 ACK→EV_ACCEPT, REJECTED/UNKNOWN→EV_REJECT(UNKNOWN 은
        unconfirmed=True)를 한 시도당 한 번만 남기되, 그 기록 실패는 TransportResult 를
        바꾸지 않는다. POST 뒤 CancelledError 면 결과 행 없이 submit/cancel 행만 남는다 —
        모르는 결과를 단정하지 않는다.
        원장 필드는 attach 구분(path='attach')과 owner 행 연결용 attempt_id/fingerprint 를
        포함하고 계좌번호·hashkey·토큰·헤더는 포함하지 않는다.
        """
        builder, broker = self._request_builder, self._broker
        if builder is None:
            return TransportResult(TransportStatus.NOT_SENT, 'request_builder_required')
        dispatched, result = False, None
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
            # 킬스위치·감사 원장 — 운영 submit_order 와 같은 두 안전장치. 둘 다 동기 함수라
            # guard와 POST 사이에 새 application await를 만들지 않는다.
            side = prepared.side.value
            audit_fields = {
                'market': prepared.market, 'symbol': prepared.symbol, 'side': side,
                'qty': prepared.quantity, 'price': format(prepared.wire_price, 'f'),
                'order_type': prepared.order_type.value, 'strategy': prepared.strategy,
                'path': 'attach', 'attempt_id': prepared.attempt_id, 'fingerprint': fingerprint,
            }
            if prepared.command is not CommandKind.CANCEL:
                # 취소는 위험을 줄이는 명령이라 현행 cancel_order 와 같이 검사하지 않는다.
                allowed, block_reason = kill_switch.check(side, market=prepared.market)
                if not allowed:
                    audit_log.record_blocked(reason=block_reason, **audit_fields)
                    # 차단 원문은 원장에만 남긴다 (상태 사유에 원문 금지).
                    return TransportResult(TransportStatus.NOT_SENT, 'kill_switch_blocked')
            audit_log.record(
                audit_log.EV_CANCEL if prepared.command is CommandKind.CANCEL else audit_log.EV_SUBMIT,
                **audit_fields,
            )
            dispatched = True
            async with broker._session.post(url, headers=headers, json=payload) as response:
                if response.status != 200:
                    result = TransportResult(TransportStatus.UNKNOWN, 'http_response_unconfirmed')
                else:
                    data = await response.json()
                    if not isinstance(data, dict):
                        result = TransportResult(TransportStatus.UNKNOWN, 'invalid_response')
                    elif data.get('rt_cd') == '0':
                        result = TransportResult(TransportStatus.ACKNOWLEDGED, 'command_acknowledged', data)
                    elif data.get('rt_cd') == '1' and isinstance(data.get('msg_cd'), str) and data['msg_cd']:
                        result = TransportResult(TransportStatus.REJECTED, 'command_rejected', data)
                    else:
                        result = TransportResult(TransportStatus.UNKNOWN, 'invalid_response')
            # 결과 행은 응답 컨텍스트를 닫은 뒤에 쓴다 — __aexit__ 가 터져도 한 시도당 한 행이다.
            return _record_outcome(result, audit_fields)
        except asyncio.CancelledError:
            raise
        except RequestValidationError:
            if dispatched:
                return _record_outcome(
                    TransportResult(TransportStatus.UNKNOWN, 'dispatch_unconfirmed'), audit_fields)
            return TransportResult(TransportStatus.NOT_SENT, 'invalid_prepared_request')
        except Exception:
            if result is not None:
                # 응답 본문으로 확인한 결과를 컨텍스트 종료 실패가 뒤집지 않는다.
                # 접수된 주문을 UNKNOWN 으로 되돌리면 attach 의 전역 정지를 부른다.
                return _record_outcome(result, audit_fields)
            if dispatched:
                return _record_outcome(
                    TransportResult(TransportStatus.UNKNOWN, 'dispatch_unconfirmed'), audit_fields)
            return TransportResult(TransportStatus.NOT_SENT, 'preparation_failed')

    async def send(self, command: str, tr_id: str, payload: dict,
                   guard: Callable[[], GuardDecision]) -> TransportResult:
        """킬스위치·감사 원장을 거치지 않는다 — 제품에서 쓰지 말 것(호출자 0건)."""
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
