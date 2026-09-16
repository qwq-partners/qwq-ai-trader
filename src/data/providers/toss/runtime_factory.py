"""Explicit operator deployment + lazy worker-only assembly (no installed grant)."""
from __future__ import annotations

from dataclasses import dataclass
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path
import os
import re
import socket
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class StartupAttestation:
    """Trusted launcher evidence captured at code loading, NOT current git HEAD.

    This module does not manufacture or install launcher evidence. Deployment
    must separately provide it and its approved artifact digest; absence blocks
    live use. Tests use explicitly synthetic launch evidence only.
    """
    release_id: str
    config_hash: str
    artifact_sha256: str
    host_identity: str
    service_uid: int


def validate_attestation(attestation, identity, *, expected_artifact_sha256):
    from .runtime import RuntimeUnavailable
    if (type(attestation) is not StartupAttestation
            or type(expected_artifact_sha256) is not str
            or re.fullmatch(r'[0-9a-f]{64}', expected_artifact_sha256) is None
            or attestation.artifact_sha256 != expected_artifact_sha256
            or attestation.service_uid != os.geteuid()
            or attestation.host_identity != socket.gethostname()
            or any(getattr(identity, name, None) != getattr(attestation, name)
                   for name in ('release_id', 'config_hash', 'host_identity', 'service_uid'))):
        raise RuntimeUnavailable('approval_unavailable')


@dataclass(frozen=True)
class Deployment:
    """Supplied by an approved deployment, never assembled from arbitrary env.

    No default instance is provided. RegistryTrust/ExecutionIdentity are explicit
    frozen inputs; load_authority checks their types and every grant binding.
    preflight_timeout_seconds bounds reading the plan that contains its own cap.
    """
    registry_path: Path
    plan_path: Path
    grant_id: str
    trust: object
    identity: object
    preflight_timeout_seconds: float
    startup_attestation: StartupAttestation | None = None
    expected_artifact_sha256: str | None = None

    def load(self):
        validate_attestation(self.startup_attestation, self.identity,
                              expected_artifact_sha256=self.expected_artifact_sha256)
        from .approval import load_authority, ApprovalError
        authority = load_authority(registry_path=self.registry_path, plan_path=self.plan_path,
                                   grant_id=self.grant_id, trust=self.trust, identity=self.identity)
        if self.preflight_timeout_seconds > authority.plan.document['limits']['preflight_timeout_seconds']:
            raise ApprovalError()
        return authority


def build_app(authority, stop_event):
    """Runs in worker thread. Constructors create no files/session/network."""
    from .runtime import RuntimeUnavailable
    if stop_event.is_set() or os.environ.get('TOSS_API', '0') != '1':
        raise RuntimeUnavailable('stopping')
    from .authorized_tokens import AuthorizedTokenProvider, make_authorized_issuer
    from .client import TossClient
    from .http_body import BodyLimits
    from .oauth import OAuthIssuer, environment_credentials
    from .observation import ObservationRunner
    from .observation_ledger import ObservationLedger
    from .rate_limit import GroupRateLimiter
    from .token import TokenManager
    from .token_store import SecureTokenStore
    from .transport import AiohttpTransport

    def authorize(operation, *, deadline):
        if stop_event.is_set() or os.environ.get('TOSS_API', '0') != '1':
            raise RuntimeUnavailable('stopping')
        scope = query_scope.get()
        if scope is not None:
            command, original_deadline = scope
            # GET and a renewal induced by that GET share the same cutoff.
            # Re-check wall time on every send, but never extend the original
            # monotonic cap when the wall clock moves backwards.
            current_deadline = command_deadline(command, authority.plan.document,
                                                now=authority.now(), clock=authority.clock)
            deadline = min(deadline, original_deadline, current_deadline)
        return authority.require(operation, deadline=deadline)

    query_scope = ContextVar('toss_query_scope', default=None)

    def query_authorize(*, deadline):
        if query_scope.get() is None:
            raise RuntimeUnavailable('invalid_command')
        return authorize('query', deadline=deadline)

    policy, grant = authority.plan.document, authority.grant
    limits = policy['limits']
    body_limits = BodyLimits(limits['response_max_bytes'], limits['response_max_depth'],
                             limits['response_max_nodes'], limits['response_max_string'],
                             limits['parse_timeout_seconds'])
    oauth = None
    if grant.role == 'issuer':
        def approved_credentials():
            from .transport import TossRequestError
            credentials = environment_credentials()
            if credentials.client_id != grant.client_identity:
                raise TossRequestError('auth_unavailable')
            return credentials
        oauth = OAuthIssuer(credential_loader=approved_credentials, authorize=authorize,
                            limits=body_limits, max_issues=limits['auth_max_issues'], clock=authority.clock)
    store = SecureTokenStore(Path(grant.token_directory), grant.client_identity)
    manager = TokenManager(store, role=grant.role, enabled=True, clock=authority.clock, now=authority.now,
                           issuer=make_authorized_issuer(authority, oauth.issue) if oauth is not None else None)
    tokens = AuthorizedTokenProvider(manager, authority,
                                     can_issue=oauth.can_issue if oauth is not None else None)
    transport = AiohttpTransport(body_limits=body_limits,
                                 authorize=query_authorize,
                                 clock=authority.clock)
    groups = limits['groups']
    limiter = GroupRateLimiter(limits={'MARKET_DATA': groups['PRICES'],
                                       'MARKET_DATA_CHART': groups['CANDLES'],
                                       'MARKET_INFO': groups['MARKET_INFO']}, clock=authority.clock)
    client = TossClient(transport=transport, tokens=tokens, limiter=limiter, enabled=True,
                        role='sender', sender_lock_path=Path(grant.sender_lock_path),
                        circuit_failure_threshold=limits['circuit_failure_threshold'],
                        circuit_open_seconds=limits['circuit_open_seconds'], clock=authority.clock)
    ledger = ObservationLedger(Path(grant.ledger_path), plan_hash=authority.plan.canonical_hash,
                               max_bytes=limits['ledger_max_bytes'])
    runner = ObservationRunner(client=client, ledger=ledger, policy=policy,
                                clock=authority.clock, now=authority.now)
    return ObservationApp(authority, stop_event, client, oauth, tokens, ledger, runner, query_scope)


def command_deadline(command, policy, *, now, clock):
    """Per-command date/session/slot authorization; calendar has its own window."""
    from .runtime import RuntimeUnavailable
    kst = ZoneInfo('Asia/Seoul')
    try:
        slot = datetime.fromisoformat(command.slot_id)
        if slot.utcoffset() != timedelta(hours=9) or now.tzinfo is None:
            raise ValueError
        local = now.astimezone(kst)
        day = slot.date().isoformat()
        if day not in policy['dates'] or day != local.date().isoformat() or slot > local:
            raise ValueError
        if slot.second or slot.microsecond:
            raise ValueError
        if command.kind == 'calendar':
            if command.requested_date != day or slot.strftime('%H:%M') != policy['calendar_time']:
                raise ValueError
            end = slot.replace(hour=0, minute=0) + timedelta(days=1)
        elif command.kind == 'prices' and slot.minute % 5 == 0:
            session = next(s for s in policy['sessions'] if s['start'] <= slot.strftime('%H:%M') < s['end'])
            hour, minute = map(int, session['end'].split(':'))
            end = min(slot + timedelta(minutes=5), slot.replace(hour=hour, minute=minute))
        else:
            raise ValueError
        if local >= end:
            raise ValueError
        return clock() + min(policy['limits']['job_timeout_seconds'], (end - local).total_seconds())
    except (TypeError, ValueError, KeyError, StopIteration, AttributeError):
        raise RuntimeUnavailable('invalid_command') from None


class ObservationApp:
    """Owns the complete worker lifecycle; no broker/engine references."""
    def __init__(self, authority, stop_event, client, oauth, tokens, ledger, runner, query_scope=None):
        self.authority, self.stop_event = authority, stop_event
        self.client, self.oauth, self.tokens = client, oauth, tokens
        self.ledger, self.runner = ledger, runner
        self.query_scope = query_scope or ContextVar('toss_query_scope', default=None)

    def _check(self):
        from .runtime import RuntimeUnavailable
        if self.stop_event.is_set() or os.environ.get('TOSS_API', '0') != '1':
            raise RuntimeUnavailable('stopping')
        return self.authority.require('query', deadline=self.authority.clock() +
                                       self.authority.plan.document['limits']['job_timeout_seconds'])

    async def start(self):
        self._check()
        # Sender ownership precedes ledger/store/key access. Existing client is
        # the authority for safe lock acquisition; never bypass it here.
        await self.client.start()
        self._check()
        self.ledger.open()
        self.ledger.configure_plan(self.authority.plan.document)
        self._check()
        from .token_store import TokenError
        try:
            await self.tokens.get_token(deadline=self._check())
        except TokenError as error:
            # Unknown issuance is never bootstrap/recovery authority. A reused
            # grant with a healthy cache simply reads it; no second bootstrap.
            if error.code != 'auth_unavailable' or not self.authority.grant.capabilities['bootstrap']:
                raise
            await self.tokens.bootstrap(deadline=self._check())

    async def run(self, command):
        from .observation import ObservationResult
        from .runtime import RuntimeUnavailable
        self._check()
        if command.kind == 'missed' and command.snapshot['kind'] in {'prices', 'calendar'}:
            self.ledger.record_missed(command.slot_id, kind=command.snapshot['kind'])
            return ObservationResult('idle', False, 0, 0, True, 'missed_slot')
        deadline = command_deadline(command, self.authority.plan.document,
                                     now=self.authority.now(), clock=self.authority.clock)
        token = self.query_scope.set((command, deadline))
        try:
            if command.kind == 'prices':
                return await self.runner.prices(slot_id=command.slot_id, snapshot=command.snapshot)
            if command.kind == 'calendar':
                return await self.runner.calendar(slot_id=command.slot_id, requested_date=command.requested_date)
            raise RuntimeUnavailable('invalid_command')
        finally:
            self.query_scope.reset(token)

    async def close(self):
        # Worker has already cancelled/gathered all startup/POST/GET tasks.
        self.authority.stop()
        if self.oauth is not None:
            await self.oauth.close()
        await self.client.close()
        self.ledger.close()
