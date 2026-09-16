"""기존 토큰 상태 머신에 task 한정 발급 권한을 전달한다."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field

from .approval import ApprovalError
from .token_store import TokenError


@dataclass
class _Lease:
    active: bool = True
    claimed: bool = False


@dataclass(frozen=True)
class IssueContext:
    authority_hash: str
    operation: str
    deadline: float
    owner: object = field(repr=False)
    authority: object = field(repr=False)
    lease: _Lease = field(repr=False)


_ISSUE_CONTEXT = ContextVar("toss_issue_context", default=None)


def make_authorized_issuer(authority, issue):
    # 동기 진입 검사: wait_for가 만드는 자식 task는 허용하지만, 문맥을 복사한
    # 임의 자식 task가 새 issuer coroutine을 만드는 것은 차단한다.
    def issuer():
        context = _ISSUE_CONTEXT.get()
        if (context is None or context.authority is not authority
                or context.authority_hash != authority.authority_hash
                or context.owner is not asyncio.current_task()
                or not context.lease.active or context.lease.claimed):
            raise ApprovalError("approval_denied")
        context.lease.claimed = True

        async def send():
            if not context.lease.active or _ISSUE_CONTEXT.get() is not context:
                raise ApprovalError("approval_denied")
            deadline = authority.require(context.operation, deadline=context.deadline)
            return await issue(operation=context.operation, deadline=deadline)

        return send()

    return issuer


class AuthorizedTokenProvider:
    def __init__(self, manager, authority):
        self.manager, self.authority = manager, authority
        if (manager.role != authority.grant.role
                or manager.store.client_identity != authority.grant.client_identity
                or str(manager.store.directory) != authority.grant.token_directory):
            raise ApprovalError("approval_denied")

    async def _call(self, operation, action, *, deadline):
        bounded = self.authority.require("query", deadline=deadline)
        lease = _Lease()
        context = IssueContext(self.authority.authority_hash, operation, bounded,
                               asyncio.current_task(), self.authority, lease)
        reset = _ISSUE_CONTEXT.set(context)
        try:
            result = await action(bounded)
            self.authority.require("query", deadline=bounded)
            return result
        finally:
            lease.active = False
            _ISSUE_CONTEXT.reset(reset)

    async def get_token(self, *, deadline):
        return await self._call("renewal", lambda bounded: self.manager.get_token(deadline=bounded), deadline=deadline)

    def observe_revocation(self, failed_token):
        # 안전 기록에는 승인 검사를 두지 않는다. 첫 await 전 fsync 계약 유지.
        self.manager.observe_revocation(failed_token)

    async def recover(self, error_code, failed_token, *, deadline):
        if error_code == "token-revoked":
            self.observe_revocation(failed_token)
        return await self._call("renewal", lambda bounded: self.manager.recover(error_code, failed_token, deadline=bounded), deadline=deadline)

    async def bootstrap(self, *, deadline):
        bounded = self.authority.require("bootstrap", deadline=deadline)
        store = self.manager.store
        # 같은 issuer lock에서 먼저 권한을 영속 소진한다. bootstrap()은 자신의
        # lock을 획득하므로 이 블록 밖에서 호출하여 재귀 flock을 피한다.
        async with store.lock(deadline=bounded, clock=self.manager.clock):
            bounded = self.authority.require("bootstrap", deadline=bounded)
            if not store._publish_immutable("toss_bootstrap_used.json", {
                    "schema_version": 1, "authority_hash": self.authority.authority_hash,
                    "grant_id": self.authority.grant.grant_id}):
                store._sync_existing("toss_bootstrap_used.json")
                raise TokenError("approval_required")
        return await self._call("bootstrap", lambda current: self.manager.bootstrap(approved=True, deadline=current), deadline=bounded)
