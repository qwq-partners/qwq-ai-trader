"""실패·불명 상태가 추가 매도 허용량을 만들지 않는 생명주기 시험."""
import asyncio
from copy import deepcopy
from decimal import Decimal
from datetime import datetime, timezone, timedelta

import pytest

from src.execution.safety.lifecycle import (
    CommandKind, CommandResult, CommandStatus, OrderEvidence,
    OrderLifecycleCoordinator, OrderRef, OrderState,
)


class MemoryOwner:
    """저장 경계만 대체하며 생명주기 전이와 직렬화는 실제 실행한다."""
    def __init__(self):
        self.state = {}
        self.lock = asyncio.Lock()
        self.version = 0

    async def mutate(self, command_id, reducer):
        async with self.lock:
            self.state = reducer(deepcopy(self.state))
            self.version += 1
            return self.version


REF = OrderRef("account-1", "KR", "2026-09-17", "KRX", "000123", "branch")
NOW = datetime(2026, 9, 17, 3, tzinfo=timezone.utc)
SCOPE = {"account_scope": "account-1", "market": "KR", "exchange": "KRX",
         "start_date": "2026-09-17", "end_date": "2026-09-17", "tr_id": "TTTC0081R",
         "query_kind": "all", "session": "regular"}


async def opened():
    owner = MemoryOwner()
    lifecycle = OrderLifecycleCoordinator(owner, clock=lambda: NOW)
    await lifecycle.prepare("exit-1", "new-1", 10, "005930", "sell")
    assert await lifecycle.claim("new-1", "sender-1")
    await lifecycle.record_result("new-1", "sender-1", CommandResult(
        CommandStatus.ACKNOWLEDGED, "new-1", REF))
    return owner, lifecycle


@pytest.mark.parametrize("status", list(CommandStatus))
def test_cancel_result_never_releases_original_sell_reservation(status):
    async def scenario():
        owner, life = await opened()
        await life.prepare("exit-1", "cancel-1", 10, "005930", "sell",
                           command=CommandKind.CANCEL, parent_attempt_id="new-1", order_ref=REF)
        assert await life.claim("cancel-1", "cancel-owner")
        await life.record_result("cancel-1", "cancel-owner", CommandResult(status, "cancel-1"))
        assert owner.state["attempts"]["new-1"]["reserved_quantity"] == 10
        assert life.replacement_quantity("exit-1") == 0
    asyncio.run(scenario())


def terminal(qty, *, supported=True, complete=True):
    # 생명주기 경계에 명시적 합성 증거를 주입한다.
    # 이 시험은 실제 KIS 취소 종결 계약을 입증했다는 뜻이 아니다.
    return OrderEvidence(REF, "005930", "sell", 10, qty, Decimal(qty * 100),
                         0, 10-qty, OrderState.FINAL_FILLED if qty == 10 else OrderState.FINAL_CANCELLED,
                         complete=complete, supported_finality=supported, source_contract="synthetic-unit",
                         observed_at=NOW, request_started_at=NOW-timedelta(seconds=1), query_scope=SCOPE)


@pytest.mark.parametrize("qty,remaining", [(5, 5), (10, 0)])
def test_replacement_requires_terminal_evidence_and_applied_fills(qty, remaining):
    async def scenario():
        owner, life = await opened()
        await life.reconcile("new-1", terminal(qty))
        assert life.replacement_quantity("exit-1") == 0
        await life.reconcile("new-1", terminal(qty), applied_quantity=qty)
        assert life.replacement_quantity("exit-1") == remaining
        if remaining:
            await life.prepare("exit-1", "new-2", remaining, "005930", "sell")
            assert owner.state["intents"]["exit-1"]["target_quantity"] == 10
            assert life.replacement_quantity("exit-1") == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("supported,complete", [(False, True), (True, False)])
def test_incomplete_or_unsupported_evidence_preserves_reservation(supported, complete):
    async def scenario():
        owner, life = await opened()
        await life.reconcile("new-1", terminal(5, supported=supported, complete=complete), applied_quantity=5)
        assert owner.state["attempts"]["new-1"]["reserved_quantity"] == 10
        assert life.replacement_quantity("exit-1") == 0
    asyncio.run(scenario())


def test_concurrent_claims_and_duplicate_prepare_have_one_sender():
    async def scenario():
        owner = MemoryOwner()
        life = OrderLifecycleCoordinator(owner)
        await life.prepare("exit", "a", 10, "005930", "sell")
        answers = await asyncio.gather(life.claim("a", "one"), life.claim("a", "two"))
        assert sorted(answers) == [False, True]
        assert not await life.claim("a", "one")
        with pytest.raises(ValueError):
            await life.prepare("exit", "b", 10, "005930", "sell")
        assert len(owner.state["attempts"]) == 1
    asyncio.run(scenario())


def test_not_sent_new_releases_only_its_reservation_and_unknown_is_not_boolean():
    async def scenario():
        owner = MemoryOwner()
        life = OrderLifecycleCoordinator(owner)
        await life.prepare("entry", "a", 10, "005930", "buy", reserved_cash="1001")
        await life.claim("a", "sender")
        result = CommandResult(CommandStatus.NOT_SENT, "a")
        with pytest.raises(TypeError):
            bool(result)
        await life.record_result("a", "sender", result)
        assert owner.state["attempts"]["a"]["reserved_quantity"] == 0
        assert owner.state["attempts"]["a"]["reserved_cash"] == "0"
    asyncio.run(scenario())


def test_late_or_wrong_sender_result_cannot_release_new_attempt():
    async def scenario():
        owner, life = await opened()
        before = deepcopy(owner.state)
        assert not await life.record_result("new-1", "wrong", CommandResult(CommandStatus.NOT_SENT, "new-1"))
        assert not await life.record_result("new-1", "sender-1", CommandResult(CommandStatus.NOT_SENT, "other"))
        assert not await life.record_result("new-1", "sender-1", CommandResult(CommandStatus.NOT_SENT, "new-1"), expected_attempt_version=0)
        assert owner.state == before
    asyncio.run(scenario())


@pytest.mark.parametrize("field,value", [("account_scope", "other"), ("order_date", "2026-09-16"),
                                        ("exchange", "NXT"), ("parent_order_no", "888")])
def test_evidence_other_scope_never_matches(field, value):
    async def scenario():
        owner, life = await opened()
        evidence = terminal(10)
        from dataclasses import replace
        evidence = replace(evidence, ref=replace(REF, **{field: value}))
        assert not await life.reconcile("new-1", evidence, applied_quantity=10)
        assert life.replacement_quantity("exit-1") == 0
    asyncio.run(scenario())


def test_replay_after_replacement_does_not_reset_target_or_release_second_order():
    async def scenario():
        owner, life = await opened()
        await life.reconcile("new-1", terminal(5), applied_quantity=5)
        await life.prepare("exit-1", "new-2", 5, "005930", "sell")
        await life.reconcile("new-1", terminal(5), applied_quantity=5)
        assert owner.state["attempts"]["new-2"]["reserved_quantity"] == 5
        assert life.replacement_quantity("exit-1") == 0
    asyncio.run(scenario())


def test_late_fill_contradicting_final_cancellation_blocks_replacement():
    async def scenario():
        owner, life = await opened()
        await life.reconcile("new-1", terminal(5), applied_quantity=5)
        assert life.replacement_quantity("exit-1") == 5
        from dataclasses import replace
        later = replace(terminal(5), cumulative_quantity=6,
                        cumulative_amount=Decimal("600"), cancelled_quantity=4,
                        supported_finality=False)
        assert not await life.reconcile("new-1", later, applied_quantity=6)
        assert life.replacement_quantity("exit-1") == 0
        assert owner.state["attempts"]["new-1"]["applied_quantity"] == 5
    asyncio.run(scenario())


def test_parent_finishes_while_cancel_prepared_then_cancel_claim_is_denied():
    async def scenario():
        owner, life = await opened()
        await life.prepare("exit-1", "cancel-1", 10, "005930", "sell",
                           command=CommandKind.CANCEL, parent_attempt_id="new-1", order_ref=REF)
        await life.reconcile("new-1", terminal(10), applied_quantity=10)
        assert not await life.claim("cancel-1", "cancel-owner")
    asyncio.run(scenario())


def test_previous_terminal_conflict_invalidates_prepared_replacement_claim():
    async def scenario():
        owner, life = await opened()
        await life.reconcile("new-1", terminal(5), applied_quantity=5)
        await life.prepare("exit-1", "new-2", 5, "005930", "sell")
        from dataclasses import replace
        await life.reconcile("new-1", replace(terminal(5), cumulative_amount=Decimal("499")))
        assert not await life.claim("new-2", "sender-2")
    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [
    {"observed_at": None}, {"observed_at": NOW.replace(tzinfo=None)},
    {"observed_at": NOW+timedelta(seconds=1)},
    {"query_scope": dict(SCOPE, start_date="2026-09-16", end_date="2026-09-16")},
    {"query_scope": dict(SCOPE, account_scope="other")},
])
def test_untrusted_provenance_does_not_finalize_order(changes):
    async def scenario():
        owner, life = await opened()
        from dataclasses import replace
        assert not await life.reconcile("new-1", replace(terminal(10), **changes), applied_quantity=10)
        assert owner.state["attempts"]["new-1"]["state"] == "open"
    asyncio.run(scenario())


def test_older_observation_cannot_install_terminal_verdict():
    async def scenario():
        owner, life = await opened()
        await life.reconcile("new-1", terminal(5, supported=False), applied_quantity=5)
        from dataclasses import replace
        older = replace(terminal(5), observed_at=NOW-timedelta(seconds=1), request_started_at=NOW-timedelta(seconds=2))
        assert not await life.reconcile("new-1", older, applied_quantity=5)
        assert life.replacement_quantity("exit-1") == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [
    {"cumulative_amount": Decimal("0")}, {"cancelled_quantity": 1}, {"rejected_quantity": 1},
])
def test_public_finality_flag_does_not_bypass_terminal_conservation(changes):
    async def scenario():
        owner, life = await opened()
        from dataclasses import replace
        await life.reconcile("new-1", replace(terminal(10), **changes), applied_quantity=10)
        assert owner.state["attempts"]["new-1"]["state"] not in ("final_filled", "final_cancelled")
        assert owner.state["attempts"]["new-1"]["reserved_quantity"] == 10
    asyncio.run(scenario())


@pytest.mark.parametrize("qty", [0, -1, True, 1.5])
def test_invalid_order_quantity_is_never_prepared(qty):
    async def scenario():
        owner = MemoryOwner()
        with pytest.raises(ValueError):
            await OrderLifecycleCoordinator(owner).prepare("entry", "a", qty, "005930", "buy")
        assert owner.state == {}
    asyncio.run(scenario())


def test_real_store_restart_preserves_claim_and_unresolved_reservation(tmp_path):
    from src.execution.safety.store import ExecutionStateStore
    from src.execution.safety.application import FillApplicationCoordinator, FillObservation

    async def scenario():
        path = tmp_path / "execution.sqlite3"
        store = ExecutionStateStore(path)
        owner = FillApplicationCoordinator(store, lambda state, version: None)
        await owner.restore()
        life = OrderLifecycleCoordinator(owner, clock=lambda: NOW)
        await life.prepare("exit", "new", 10, "005930", "sell")
        assert await life.claim("new", "sender")
        await store.close()
        reopened = ExecutionStateStore(path)
        restored = FillApplicationCoordinator(reopened, lambda state, version: None)
        await restored.restore()
        recovered = OrderLifecycleCoordinator(restored, clock=lambda: NOW)
        assert not await recovered.claim("new", "sender")
        assert recovered.replacement_quantity("exit") == 0
        assert restored.state["attempts"]["new"]["reserved_quantity"] == 10
        observation = FillObservation("account-1", "KR", "2026-09-17", "KRX", "000123",
                                      "005930", "SELL", 10, Decimal("1000"), org_no="branch")
        assert observation.order_key == REF.key
        await reopened.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("status", [CommandStatus.NOT_SENT, CommandStatus.REJECTED])
def test_late_failure_cannot_erase_observed_and_applied_partial_fill(status):
    async def scenario():
        owner = MemoryOwner()
        life = OrderLifecycleCoordinator(owner, clock=lambda: NOW)
        await life.prepare("exit", "new", 10, "005930", "sell", order_ref=REF,
                           reserved_cash="1000")
        await life.claim("new", "sender")
        # ACK 저장 전 도착한 실제 체결 관측을 먼저 반영한다.
        await life.reconcile("new", terminal(5, supported=False), applied_quantity=5)
        assert not await life.record_result("new", "sender", CommandResult(status, "new"))
        attempt = owner.state["attempts"]["new"]
        assert attempt["state"] == "blocked_unknown"
        assert attempt["command_status"] == "unknown"
        assert attempt["reserved_quantity"] == 10
        assert attempt["reserved_cash"] == "1000"
        assert attempt["observed_quantity"] == attempt["applied_quantity"] == 5
        assert attempt["order_ref"] == REF.to_dict()
        assert life.replacement_quantity("exit") == 0
        # 뒤따른 ACK 하나로 이미 기록한 명령/체결 모순을 자동 해제하지 않는다.
        assert not await life.record_result("new", "sender", CommandResult(CommandStatus.ACKNOWLEDGED, "new", REF))
        assert owner.state["attempts"]["new"]["state"] == "blocked_unknown"
    asyncio.run(scenario())


def test_future_kst_order_day_cannot_be_finalized_before_that_day():
    async def scenario():
        from dataclasses import replace
        owner = MemoryOwner()
        before = datetime(2026, 9, 16, 14, 59, tzinfo=timezone.utc)  # KST 09/16 23:59
        life = OrderLifecycleCoordinator(owner, clock=lambda: before)
        await life.prepare("exit", "new", 10, "005930", "sell", order_ref=REF)
        await life.claim("new", "sender")
        evidence = replace(terminal(10), observed_at=before,
                           request_started_at=before-timedelta(seconds=1))
        assert not await life.reconcile("new", evidence, applied_quantity=10)
        assert owner.state["attempts"]["new"]["reserved_quantity"] == 10
    asyncio.run(scenario())
