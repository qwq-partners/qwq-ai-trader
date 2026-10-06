"""승인된 단일 후보 구간의 토스 WS 접속·로컬 후보 전달·종료 자료 저장."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
from types import SimpleNamespace

from src.data.providers.toss.approval import ApprovedAuthority, ApprovalError
from src.data.providers.toss.orderbook_stream import (TossOrderbookCapture, aware_time,
    receive_orderbooks, unique_json_keys, reject_json_constant)
from .entry_anchor_input import AnchorClient, InputInvalid, validate_projection
from .toss_positions import InputUnavailable

FAILURE_PHASES = frozenset(('none', 'startup', 'initial_scan', 'live_poll', 'finalization',
                            'cleanup', 'cancelled'))
ERROR_KINDS = frozenset(('none', 'input_unavailable', 'invalid_input', 'approval_denied',
                         'websocket_incomplete', 'input_timeout', 'grace_expired', 'cleanup_failed',
                         'cancelled', 'unexpected'))


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


class CaptureArtifact:
    """명시 경로에 시작/결과 두 행 저장. 중단된 시작 행은 완료 자료가 아니다.

    전용 관측 프로세스에서 종료 후 자료 전체를 한 번 기록한다. 중간 호가의
    디스크 보존은 보장하지 않으며, 강제 종료/쓰기 실패 시 불완전 파일을 남긴다.
    """
    def __init__(self, path, *, max_bytes, plan_hash):
        self.path = Path(path)
        if (not self.path.is_absolute() or type(max_bytes) is not int or not 1 <= max_bytes <= 67108864
                or type(plan_hash) is not str or not re.fullmatch('[0-9a-f]{64}', plan_hash)):
            raise ValueError('artifact_contract_invalid')
        self.max_bytes, self.plan_hash = max_bytes, plan_hash
        self._file = None
        self._bytes = 0
        self._previous = '0' * 64
        self._row = 0

    def _write(self, kind, payload):
        body = dict(row=self._row + 1, previous_hash=self._previous, kind=kind, payload=payload)
        digest = hashlib.sha256(_json(body)).hexdigest()
        data = _json(dict(body, hash=digest)) + b'\n'
        if self._bytes + len(data) > self.max_bytes:
            raise ValueError('artifact_limit_exceeded')
        try:
            pending = memoryview(data)
            while pending:
                written = self._file.write(pending)
                if written is None or written <= 0:
                    raise OSError('artifact_short_write')
                pending = pending[written:]
            os.fsync(self._file.fileno())
        except BaseException:
            # 실패한 result가 완성 행처럼 보이지 않도록 마지막 확정 경계로 되돌린다.
            # 복구 IO도 실패하면 파일을 신뢰할 수 없으며 서비스는 계속 실패한다.
            try:
                os.ftruncate(self._file.fileno(), self._bytes)
                self._file.seek(self._bytes)
                os.fsync(self._file.fileno())
            except OSError:
                pass
            raise
        self._bytes += len(data)
        self._row += 1
        self._previous = digest

    def open(self):
        if self._file is not None:
            raise ValueError('artifact_already_open')
        for parent in self.path.parents:
            if parent.is_symlink():
                raise ValueError('artifact_unsafe_path')
        parent = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(self.path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent)
            self._file = os.fdopen(fd, 'wb', buffering=0)
            self._write('start', {'format':'toss-candidate-artifact-v1', 'plan_hash':self.plan_hash})
            os.fsync(parent)
        except BaseException:
            self.close()
            raise
        finally:
            os.close(parent)

    def finish(self, value):
        if self._row != 1:
            raise ValueError('artifact_state_invalid')
        self._write('result', value)

    def close(self):
        if self._file is not None:
            self._file.close()
            self._file = None


def read_capture_artifact(path, *, max_bytes, plan_hash):
    if type(max_bytes) is not int or not 1 <= max_bytes <= 67108864:
        raise ValueError('artifact_read_limit_invalid')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > max_bytes:
            raise ValueError('artifact_file_invalid')
        raw = handle.read(max_bytes + 1)
    lines = raw.splitlines(keepends=True)
    if len(raw) > max_bytes or len(lines) != 2 or any(not x.endswith(b'\n') for x in lines):
        raise ValueError('artifact_unsealed')
    previous = '0' * 64
    result = None
    for i, line in enumerate(lines, 1):
        row = json.loads(line, object_pairs_hook=unique_json_keys, parse_constant=reject_json_constant)
        if type(row) is not dict or set(row) != {'row','kind','payload','hash','previous_hash'}:
            raise ValueError('artifact_row_invalid')
        digest = row.pop('hash')
        if (type(row['row']) is not int or row['row'] != i or row['previous_hash'] != previous
                or hashlib.sha256(_json(row)).hexdigest() != digest):
            raise ValueError('artifact_hash_invalid')
        previous = digest
        if i == 1:
            if row['kind'] != 'start' or row['payload'] != {'format':'toss-candidate-artifact-v1', 'plan_hash':plan_hash}:
                raise ValueError('artifact_plan_invalid')
        elif row['kind'] != 'result' or type(row['payload']) is not dict:
            raise ValueError('artifact_result_invalid')
        else:
            result = row['payload']
    return result


def build_ws_components(authority, *, stop_event):
    """기존 승인/단일 발급/송신 잠금을 재사용. 생성자는 파일·토큰을 읽지 않는다."""
    from src.data.providers.toss.authorized_tokens import AuthorizedTokenProvider, make_authorized_issuer
    from src.data.providers.toss.client import TossClient
    from src.data.providers.toss.http_body import BodyLimits
    from src.data.providers.toss.oauth import OAuthIssuer, environment_credentials
    from src.data.providers.toss.token import TokenManager
    from src.data.providers.toss.token_store import SecureTokenStore
    from src.data.providers.toss.ws_transport import TossWebSocketTransport
    limits, grant = authority.plan.document['limits'], authority.grant
    def authorize(operation, *, deadline):
        if stop_event.is_set() or os.environ.get('TOSS_API') != '1':
            raise ApprovalError('approval_denied')
        authority.require('websocket', deadline=deadline)
        return authority.require(operation, deadline=deadline)
    def credentials():
        value = environment_credentials()
        if value.client_id != grant.client_identity:
            raise ApprovalError('approval_denied')
        return value
    oauth = None
    if grant.role == 'issuer':
        oauth = OAuthIssuer(credential_loader=credentials, authorize=authorize,
            limits=BodyLimits(limits['response_max_bytes'], limits['response_max_depth'],
                limits['response_max_nodes'], limits['response_max_string'], limits['parse_timeout_seconds']),
            max_issues=limits['auth_max_issues'], clock=authority.clock)
    manager = TokenManager(SecureTokenStore(Path(grant.token_directory), grant.client_identity),
        role=grant.role, enabled=True, clock=authority.clock, now=authority.now,
        issuer=make_authorized_issuer(authority, oauth.issue) if oauth is not None else None)
    tokens = AuthorizedTokenProvider(manager, authority, can_issue=oauth.can_issue if oauth else None)
    transport = TossWebSocketTransport(authority)
    # GET 실행은 하지 않는다. 기존 송신 잠금/세션 정리 수명만 재사용한다.
    owner = TossClient(transport=transport, tokens=tokens, limiter=None, enabled=True, role='sender',
        sender_lock_path=Path(grant.sender_lock_path), circuit_failure_threshold=limits['circuit_failure_threshold'],
        circuit_open_seconds=limits['circuit_open_seconds'], clock=authority.clock)
    return SimpleNamespace(owner=owner, transport=transport, tokens=tokens, oauth=oauth)


def _input(value, policy, now, previous=None):
    value = validate_projection(value)
    if (value['evaluation_epoch'] != policy['evaluation_epoch']
            or value['study_sha256'] != policy['engine_study_sha256']
            or not 0 <= (now - aware_time(value['observed_at'])).total_seconds() <= 5):
        raise ValueError('engine_identity_or_time_invalid')
    if previous is not None:
        count = len(previous['records'])
        if (value['capture_id'] != previous['capture_id'] or value['records'][:count] != previous['records']
                or value['source_record_count'] < previous['source_record_count']
                or (previous['capture_closed'] and not value['capture_closed'])):
            raise ValueError('engine_projection_changed')
    scans = [r for r in value['records'] if r['kind'] == 'scan']
    if len(scans) > 1:
        raise ValueError('multiple_engine_scans')
    if scans:
        scan = scans[0]
        if (scan.get('route_origin') != 'live_screening'
                or not aware_time(policy['start_at']) <= aware_time(scan['observed_at']) < aware_time(policy['scan_until'])):
            raise ValueError('scan_admission_invalid')
        symbols = []
        for item in scan['candidates']:
            symbol = item['symbol']
            if (type(symbol) is not str or not re.fullmatch('[0-9]{6}', symbol)
                    or item['candidate_id'] != f"{scan['scan_id']}:{symbol}" or symbol in symbols):
                raise ValueError('candidate_identity_invalid')
            symbols.append(symbol)
        selection = value.get('selection')
        if selection is not None:
            if (len(symbols) > 3 and policy.get('candidate_selection_rule', 'whole_returned_cohort')
                    != selection['rule']):
                raise ValueError('candidate_subset_not_approved')
            symbols = symbols[:3]
        return value, symbols
    return value, None


async def run_ws_service(*, deployment, settings=None, claim_start, stop_event=None,
                         anchor_factory=None, components_factory=None):
    """기존 봉인 launcher에서만 선택적으로 호출. 봇·전략·KIS 호출 객체 없음."""
    if os.environ.get('TOSS_API', '0') in ('0', ''):
        return 0
    authority = inputs = components = artifact = capture = receiver = None
    latest = None
    reason, complete = 'capture_failed', False
    failure_phase, error_kind = 'none', 'none'
    input_unavailable = dict(live_total=0, live_consecutive=0,
                             grace_total=0, grace_consecutive=0)
    phase = 'startup'
    stop = stop_event if stop_event is not None else asyncio.Event()
    handlers = []
    cancelled = False
    stop_watcher = None

    def record_failure(where, kind, value):
        nonlocal failure_phase, error_kind, reason
        if where not in FAILURE_PHASES or kind not in ERROR_KINDS:
            raise ValueError('failure_taxonomy_invalid')
        failure_phase, error_kind, reason = where, kind, value

    def invalid_input(where):
        record_failure(where, 'invalid_input', 'engine_input_invalid')

    def unavailable_input(where):
        total, consecutive = (('live_total', 'live_consecutive') if where == 'live_poll'
                              else ('grace_total', 'grace_consecutive'))
        input_unavailable[total] += 1
        input_unavailable[consecutive] += 1
        return input_unavailable[consecutive]

    def accepted_input(value, where):
        nonlocal latest
        try:
            projected, symbols = _input(value, p, authority.now(), latest)
        except Exception:
            invalid_input(where)
            raise
        latest = projected
        if where == 'live_poll':
            input_unavailable['live_consecutive'] = 0
        elif where == 'finalization':
            input_unavailable['grace_consecutive'] = 0
        return symbols

    try:
        if os.environ.get('TOSS_API') != '1':
            raise ApprovalError('approval_denied')
        from src.data.providers.toss.runtime import Preflight
        authority = await Preflight(deployment.load).run(timeout=deployment.preflight_timeout_seconds)
        if type(authority) is not ApprovedAuthority:
            raise ApprovalError('approval_untrusted')
        p, limits = authority.plan.document.get('websocket'), authority.plan.document['limits']
        if p is None or authority.grant.capabilities.get('websocket') is not True:
            raise ApprovalError('approval_denied')
        deadline = authority.clock() + (aware_time(p['end_at']) - authority.now()).total_seconds()
        scan_deadline = authority.clock() + (aware_time(p['scan_until']) - authority.now()).total_seconds()
        def check(operation='query'):
            if stop.is_set() or os.environ.get('TOSS_API') != '1':
                raise ApprovalError('approval_denied')
            return authority.require(operation, deadline=deadline if operation == 'websocket' else authority.clock() + 2)
        check()
        service_task = asyncio.current_task()
        async def watch_stop():
            while not stop.is_set() and os.environ.get('TOSS_API') == '1':
                try:
                    await asyncio.wait_for(stop.wait(), timeout=.1)
                except TimeoutError:
                    continue
            authority.stop()
            service_task.cancel()
        stop_watcher = asyncio.create_task(watch_stop(), name='toss_candidate_stop')
        claim_start(authority)
        artifact = CaptureArtifact(authority.grant.ledger_path,
            max_bytes=limits['ledger_max_bytes'], plan_hash=authority.plan.canonical_hash)
        artifact.open()
        if stop_event is None:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, stop.set)
                handlers.append(sig)
        inputs = (anchor_factory or AnchorClient)()
        symbols = None
        phase = 'initial_scan'
        while symbols is None and authority.clock() < scan_deadline:
            check()
            try:
                fetched = await inputs.fetch()
            except InputUnavailable:
                await asyncio.sleep(min(p['poll_seconds'], max(0, scan_deadline - authority.clock())))
                continue
            except InputInvalid:
                invalid_input(phase)
                raise
            symbols = accepted_input(fetched, phase)
            if authority.clock() >= scan_deadline:
                record_failure('initial_scan', 'input_unavailable', 'first_scan_unavailable')
                raise ValueError(reason)
            if symbols is None:
                await asyncio.sleep(p['poll_seconds'])
        if symbols is None:
            record_failure('initial_scan', 'input_unavailable', 'first_scan_unavailable')
            raise ValueError(reason)
        if not symbols:
            reason = 'first_scan_empty'
            raise ValueError(reason)
        check('websocket')
        capture = TossOrderbookCapture(symbols=symbols, request_id=p['request_id'],
            evaluation_epoch=p['evaluation_epoch'], start_at=p['start_at'], end_at=p['end_at'],
            max_frames=p['max_frames'], max_source_age_seconds=p['max_source_age_seconds'])
        components = (components_factory(authority) if components_factory
                      else build_ws_components(authority, stop_event=stop))
        await components.owner.start()
        check('websocket')
        from src.data.providers.toss.token_store import TokenError
        async with asyncio.timeout(min(20, limits['job_timeout_seconds'], check('websocket') - authority.clock())):
            try:
                token = await components.tokens.get_websocket_token(deadline=check('websocket'))
            except TokenError as exc:
                if exc.code != 'auth_unavailable' or not authority.grant.capabilities['bootstrap']:
                    raise
                check('websocket')
                token = await components.tokens.bootstrap(deadline=check('websocket'))
        socket = await components.transport.connect(token, deadline=check('websocket'))
        token = None
        receiver = asyncio.create_task(receive_orderbooks(socket, capture, exclusive=True,
            now=authority.now, monotonic=authority.clock), name='toss_candidate_orderbooks')
        phase = 'live_poll'
        while not receiver.done():
            await asyncio.wait((receiver,), timeout=p['poll_seconds'])
            if receiver.done():
                break
            check()
            try:
                fetched = await inputs.fetch()
            except InputUnavailable:
                if unavailable_input(phase) >= 2:
                    record_failure(phase, 'input_unavailable', 'engine_input_unavailable')
                    raise
                await asyncio.sleep(p['poll_seconds'])
                continue
            except InputInvalid:
                invalid_input(phase)
                raise
            accepted_input(fetched, phase)
        await receiver
        if capture.stop_reason != 'window_ended' or capture._cleanup_failed:
            record_failure('live_poll', 'websocket_incomplete', 'websocket_incomplete')
            raise ValueError(reason)
        # 원천 봉인 결과만 제한된 로컬 유예 시간 동안 회수한다. WS는 이미 닫혔다.
        grace = authority.clock() + min(5, limits['cleanup_timeout_seconds'])
        sealed_fresh = False
        phase = 'finalization'
        while authority.clock() < grace:
            check()
            try:
                remaining = grace - authority.clock()
                async with asyncio.timeout(remaining):
                    fetched = await inputs.fetch()
            except TimeoutError:
                record_failure(phase, 'input_timeout', 'engine_input_grace_expired')
                raise
            except InputUnavailable:
                if unavailable_input(phase) >= 2:
                    record_failure(phase, 'input_unavailable', 'engine_input_unavailable')
                    raise
                await asyncio.sleep(min(p['poll_seconds'], max(0, grace - authority.clock())))
                continue
            except InputInvalid:
                invalid_input(phase)
                raise
            check()
            if authority.clock() >= grace:
                record_failure(phase, 'grace_expired', 'engine_input_grace_expired')
                raise ValueError(reason)
            accepted_input(fetched, phase)
            if latest['capture_closed'] and latest['journal_sealed']:
                sealed_fresh = True
                break
            await asyncio.sleep(min(p['poll_seconds'], max(0, grace - authority.clock())))
        complete = sealed_fresh and latest['complete'] is True
        reason = 'captured' if complete else 'engine_input_incomplete'
    except asyncio.CancelledError:
        cancelled = True
        record_failure('cancelled', 'cancelled', 'cancelled')
    except ApprovalError:
        complete = False
        if error_kind == 'none':
            record_failure(phase, 'approval_denied', reason)
    except Exception:
        complete = False
        if error_kind == 'none':
            record_failure(phase, 'unexpected', reason)
    finally:
        if stop_watcher is not None:
            stop_watcher.cancel()
            await asyncio.gather(stop_watcher, return_exceptions=True)
        if authority is not None:
            authority.stop()
        if receiver is not None and not receiver.done():
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
        for resource in (inputs, components.oauth if components else None, components.owner if components else None):
            if resource is not None:
                try:
                    await asyncio.wait_for(resource.close(), timeout=2)
                except asyncio.CancelledError:
                    cancelled, complete = True, False
                    record_failure('cancelled', 'cancelled', 'cancelled')
                except Exception:
                    complete = False
                    record_failure('cleanup', 'cleanup_failed', 'cleanup_failed')
        if artifact is not None:
            try:
                artifact.finish(dict(service_complete=complete, service_reason=reason,
                    engine=latest, toss=capture.export() if capture else None,
                    as_of=authority.now().isoformat(), profit_comparison_available=False,
                    failure_phase=failure_phase, error_kind=error_kind,
                    input_unavailable=input_unavailable))
            except Exception:
                complete = False
            finally:
                artifact.close()
        for sig in handlers:
            asyncio.get_running_loop().remove_signal_handler(sig)
    if cancelled:
        raise asyncio.CancelledError()
    return 0 if complete else 1
