"""단일 발급자/reader 상태 머신. HTTP 발급은 주입한 callable만 담당한다."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import math
import time

from .token_store import MAX_GENERATION, MAX_LIFETIME, TokenError, TokenRecord


def utc_now():
    return datetime.now(timezone.utc)


class TokenManager:
    def __init__(self, store, *, role="reader", issuer=None, enabled=False,
                 clock=time.monotonic, now=utc_now):
        if role not in {"reader", "issuer"}:
            raise TokenError("auth_unavailable")
        self.store = store
        self.role = role
        self.issuer = issuer
        self.enabled = enabled
        self.clock = clock
        self.now = now
        self._blocked = None
        self._seen_generation = 0
        self._token_generations = {}

    def _check(self, deadline):
        if not self.enabled:
            raise TokenError("disabled")
        remaining = deadline - self.clock()
        if not math.isfinite(remaining) or remaining <= 0:
            raise TokenError("deadline_exceeded")
        if self._blocked:
            raise TokenError(self._blocked)
        return remaining

    def _load(self):
        try:
            return self.store.load()
        except TokenError as error:
            if error.code == "invalid_cache":
                return None
            raise

    def _valid(self, record):
        return record is not None and record.issued_at <= self.now() < record.expires_at

    @staticmethod
    def _digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def observe_revocation(self, failed_token):
        """호출자가 401을 해석한 직후, 첫 await/deadline 검사보다 먼저 호출한다."""
        if not self.enabled:
            raise TokenError("disabled")
        try:
            digest = self._digest(failed_token)
            self.store.observe_revocation(digest, self._token_generations.get(digest, 0))
        except Exception:
            self._blocked = "auth_unavailable"
            try:
                self.store.block_revocations()
            except Exception:
                pass
            raise TokenError("auth_unavailable") from None

    def _revocation_gate(self, record):
        """issuer lock 안에서 정확한 관측 스냅샷만 유효 최신 캐시로 해결한다."""
        try:
            observations = self.store.load_revocations()
            resolutions = self.store.load_revocation_resolutions()
            if not set(resolutions).issubset(observations):
                raise TokenError("auth_unavailable")
            if not observations:
                return
            if record is None or record.issued_at > self.now():
                raise TokenError("auth_unavailable")
            digest = self._digest(record.access_token)
            if any(item["failed_digest"] == digest for item in observations.values()):
                raise TokenError("auth_unavailable")
            updated = dict(resolutions)
            for observation_id, observation in observations.items():
                proof = resolutions.get(observation_id)
                if (self._valid(record) and record.generation > observation["generation"]
                        and record.generation >= self._seen_generation
                        and (proof is None or record.generation > proof["generation"]
                             or (record.generation == proof["generation"] and digest == proof["cache_digest"]))):
                    updated[observation_id] = {"cache_digest": digest, "generation": record.generation}
                elif (proof is None or proof["cache_digest"] != digest
                      or proof["generation"] != record.generation
                      or proof["generation"] <= observation["generation"]):
                    raise TokenError("auth_unavailable")
            if updated != resolutions:
                self.store.save_revocation_resolutions(updated)
        except TokenError:
            raise TokenError("auth_unavailable") from None

    def _state(self, record):
        state = self.store.load_state()
        if state is None:
            return
        if state["kind"] == "ready":
            if record is None or record.generation < state["generation"]:
                raise TokenError("auth_unavailable")
            return
        if state["kind"] == "issuance_unknown":
            raise TokenError("issuance_unknown")
        if (self._valid(record) and record.generation > state["generation"]
                and self._digest(record.access_token) != state["failed_digest"]):
            self.store.save_state("ready", record.generation)
            return
        raise TokenError("auth_unavailable")

    def _return(self, record, deadline):
        self._check(deadline)
        self._seen_generation = max(self._seen_generation, record.generation)
        digest = self._digest(record.access_token)
        self._token_generations[digest] = max(self._token_generations.get(digest, 0), record.generation)
        return record.access_token

    def _due(self, record):
        now = self.now()
        age = (now - record.issued_at).total_seconds()
        return ((record.expires_at - now).total_seconds() <= 1800
                and age >= 60)

    async def get_token(self, *, deadline):
        self._check(deadline)
        async with self.store.lock(deadline=deadline, clock=self.clock):
            self._check(deadline)
            record = self._load()
            self._state(record)
            self._revocation_gate(record)
            if self._valid(record) and (self.role == "reader" or not self._due(record)):
                return self._return(record, deadline)
            if (record is None or (self.now() - record.issued_at).total_seconds() < 60
                    or self.role != "issuer" or self.issuer is None):
                raise TokenError("auth_unavailable")
            return await self._issue(record.generation + 1, deadline)

    async def bootstrap(self, *, approved, deadline):
        self._check(deadline)
        if approved is not True:
            raise TokenError("approval_required")
        if self.role != "issuer" or self.issuer is None:
            raise TokenError("auth_unavailable")
        async with self.store.lock(deadline=deadline, clock=self.clock):
            self._check(deadline)
            # 손상 파일은 초기 부재가 아니므로 bootstrap으로 우회하지 않는다.
            self._state(self._load())
            record = self.store.load()
            self._revocation_gate(record)
            if record is not None:
                if self._valid(record):
                    return self._return(record, deadline)
                raise TokenError("auth_unavailable")
            return await self._issue(1, deadline)

    async def recover(self, error_code, failed_token, *, deadline):
        if error_code == "token-revoked":
            self.observe_revocation(failed_token)
        self._check(deadline)
        if error_code not in {"token-revoked", "expired-token"}:
            raise TokenError("auth_unavailable")
        async with self.store.lock(deadline=deadline, clock=self.clock):
            self._check(deadline)
            record = self._load()
            state = self.store.load_state()
            if state and state["kind"] == "issuance_unknown":
                raise TokenError("issuance_unknown")
            if error_code == "expired-token":
                self._state(record)
                self._revocation_gate(record)
                if self._valid(record) and record.access_token != failed_token:
                    return self._return(record, deadline)
                if (record is None or self.role != "issuer" or self.issuer is None
                        or (self.now() - record.issued_at).total_seconds() < 60):
                    raise TokenError("auth_unavailable")
                return await self._issue(record.generation + 1, deadline)
            failed_digest = self._digest(failed_token)
            failed_generation = self._token_generations.get(failed_digest, 0)
            if (self._valid(record) and record.access_token != failed_token
                    and record.generation > failed_generation
                    and record.generation >= self._seen_generation):
                self._state(record)
                self._revocation_gate(record)
                return self._return(record, deadline)
            generation = max(self._seen_generation, record.generation if record else 0,
                             state["generation"] if state else 0)
            try:
                self.store.save_state("auth_unavailable", generation, failed_digest)
            except TokenError:
                self._blocked = "auth_unavailable"
                raise TokenError("auth_unavailable") from None
            raise TokenError("auth_unavailable")

    async def _issue(self, generation, deadline):
        self._check(deadline)
        if not 1 <= generation <= MAX_GENERATION:
            raise TokenError("auth_unavailable")
        record = self._load()
        self._state(record)
        self._revocation_gate(record)
        # intent 게시 성공 전에는 외부 발급자를 절대 호출하지 않는다.
        self.store.save_state("issuance_unknown", generation)
        try:
            # admission scan 시작 전에 durable 게시된 관측은 모두 검사한다.
            # scan 중/후 관측은 이미 허용한 외부 발급을 취소하지 못하며,
            # 새 토큰 게시/이후 반환·갱신에서 다시 검사한다.
            self._revocation_gate(record)
            remaining = self._check(deadline)
            response = await asyncio.wait_for(self.issuer(), timeout=remaining)
            if not isinstance(response, dict):
                raise TokenError("issuance_unknown")
            ttl = response.get("expires_in")
            if (type(ttl) not in {int, float} or not math.isfinite(ttl)
                    or not 0 < ttl <= MAX_LIFETIME or response.get("token_type") != "Bearer"):
                raise TokenError("issuance_unknown")
            issued_at = self.now()
            record = TokenRecord(access_token=response.get("access_token"), issued_at=issued_at,
                expires_at=issued_at + timedelta(seconds=ttl), generation=generation,
                client_identity=self.store.client_identity)
            self._check(deadline)
            self.store.save(record)
            self._revocation_gate(record)
            self.store.save_state("ready", generation)
            return self._return(record, deadline)
        except asyncio.CancelledError:
            self._blocked = "issuance_unknown"
            raise
        except Exception:
            self._blocked = "issuance_unknown"
            # ready의 replace 후 directory fsync 실패도 재시작 시 발급 금지.
            # 디스크 자체가 쓰기를 전혀 허용하지 않으면 현재 프로세스만 차단할 수 있다.
            try:
                self.store.save_state("issuance_unknown", generation)
            except Exception:
                pass
            raise TokenError("issuance_unknown") from None
