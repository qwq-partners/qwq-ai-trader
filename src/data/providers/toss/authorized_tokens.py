"""기존 토큰 상태 머신에 task 한정 발급 권한을 전달한다."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field

from .approval import ApprovalError
from .token import TokenManager
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


class _QueryOnlyTokenManager(TokenManager):
    """원 manager의 안전 상태를 공유하며 발급 역할만 제한하는 view.

    TokenManager 생성자는 안전 상태를 초기화하므로 호출하지 않는다. store/
    clock/now/enabled와 관측 digest dict는 원본을 읽고, scalar 안전 상태의
    쓰기도 원본으로 전달한다. bootstrap과 조회 사이 세대 정보가 갈라지지 않는다.
    """

    role = "reader"
    issuer = None

    def __init__(self, manager):
        self._manager = manager

    def __getattr__(self, name):
        return getattr(self._manager, name)

    @property
    def _blocked(self):
        return self._manager._blocked

    @_blocked.setter
    def _blocked(self, value):
        self._manager._blocked = value

    @property
    def _seen_generation(self):
        return self._manager._seen_generation

    @_seen_generation.setter
    def _seen_generation(self, value):
        self._manager._seen_generation = value


def _budget_available(can_issue):
    if can_issue is None:
        return True
    try:
        return can_issue() is True
    except Exception:
        return False


class _BudgetedTokenManager(_QueryOnlyTokenManager):
    """issuer lock 안의 역할 검사에 현재 worker 발급 예산을 결합한다."""

    def __init__(self, manager, can_issue):
        super().__init__(manager)
        self._can_issue = can_issue

    @property
    def role(self):
        return self._manager.role if _budget_available(self._can_issue) else "reader"

    @property
    def issuer(self):
        return self._manager.issuer

    async def _issue(self, generation, deadline):
        # 원 TokenManager._issue의 intent 저장보다 먼저 거부한다. 이 검사 뒤
        # 실제 POST 가능성이 생긴 실패는 기존 unknown 처리를 그대로 따른다.
        if not _budget_available(self._can_issue):
            raise TokenError("auth_unavailable")
        return await super()._issue(generation, deadline)


class AuthorizedTokenProvider:
    def __init__(self, manager, authority, *, can_issue=None):
        self.manager, self.authority = manager, authority
        if (manager.role != authority.grant.role
                or manager.store.client_identity != authority.grant.client_identity
                or str(manager.store.directory) != authority.grant.token_directory):
            raise ApprovalError("approval_denied")
        if can_issue is not None and not callable(can_issue):
            raise ApprovalError("approval_denied")
        self._can_issue = can_issue
        self._issuance_manager = manager if can_issue is None else _BudgetedTokenManager(manager, can_issue)
        self._query_manager = self._issuance_manager if authority.grant.capabilities["renewal"] else _QueryOnlyTokenManager(manager)

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
        return await self._call("renewal", lambda bounded: self._query_manager.get_token(deadline=bounded), deadline=deadline)

    def observe_revocation(self, failed_token):
        # 안전 기록에는 승인 검사를 두지 않는다. 첫 await 전 fsync 계약 유지.
        self.manager.observe_revocation(failed_token)

    async def recover(self, error_code, failed_token, *, deadline):
        if error_code == "token-revoked":
            self.observe_revocation(failed_token)
        return await self._call("renewal", lambda bounded: self._query_manager.recover(error_code, failed_token, deadline=bounded), deadline=deadline)

    async def bootstrap(self, *, deadline):
        bounded = self.authority.require("bootstrap", deadline=deadline)
        if not _budget_available(self._can_issue):
            raise TokenError("auth_unavailable")
        store = self.manager.store
        # 같은 issuer lock에서 먼저 권한을 영속 소진한다. bootstrap()은 자신의
        # lock을 획득하므로 이 블록 밖에서 호출하여 재귀 flock을 피한다.
        async with store.lock(deadline=bounded, clock=self.manager.clock):
            bounded = self.authority.require("bootstrap", deadline=bounded)
            if not _budget_available(self._can_issue):
                raise TokenError("auth_unavailable")
            if not store._publish_immutable("toss_bootstrap_used.json", {
                    "schema_version": 1, "authority_hash": self.authority.authority_hash,
                    "grant_id": self.authority.grant.grant_id}):
                store._sync_existing("toss_bootstrap_used.json")
                raise TokenError("approval_required")
        return await self._call("bootstrap", lambda current: self._issuance_manager.bootstrap(approved=True, deadline=current), deadline=bounded)
