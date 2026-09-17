"""KR 경제/보호 projection과 core receipt. 전 writer 이행 전 운영 설치 금지."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from typing import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from .application import ApplicationBlocked, FillApplicationCoordinator, FillObservation, FillReduction
from .economics import decode_portfolio, reduce_economics, validate_risk, publish_risk
from .lifecycle import OrderLifecycleCoordinator
from .protection import decode_protection, publish_protection, quote_protection, reduce_protection
from .protection_recovery import RecoveryReceipt, capture_fill, capture_quote, digest, reduce_repair
from .store import ExecutionStateStore


class KRExecutionRuntime:
    """경제·보호 후보를 하나의 checkpoint에 commit 후 함께 게시한다."""

    def __init__(self, store: ExecutionStateStore, engine, exit_manager, *,
                 clock: Callable[[], datetime], risk_manager=None):
        self.engine = engine
        self.exit_manager = exit_manager
        self.risk_manager = risk_manager
        self.clock = clock
        self._quotes: dict[str, Decimal] = {}
        self._protection_tasks: set[asyncio.Task] = set()
        self._quote_lock = asyncio.Lock()
        self._protection_failed = False
        self._closing = False
        self.owner = FillApplicationCoordinator(store, self._publish, self._reduce)
        self.lifecycle = OrderLifecycleCoordinator(self.owner, clock=clock)

    @property
    def trading_ready(self) -> bool:
        # 이 slice는 전 writer/HTTP/startup 이행 미완. 건강한 저장소 != 거래 허가.
        return False

    def _now(self) -> datetime:
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("실행 시계는 timezone-aware여야 합니다")
        return now.astimezone(ZoneInfo("Asia/Seoul"))

    async def restore(self) -> int:
        """빈 checkpoint를 정상 빈 계좌로 바꾸지 않는다."""
        return await self.owner.restore()

    def attach(self) -> None:
        """명시 설치. 운영 진입점에는 아직 호출되지 않는다."""
        if not self.owner.healthy:
            raise ApplicationBlocked("유효한 checkpoint를 먼저 게시해야 합니다")
        self.engine.bind_execution_runtime(self)

    def _publish(self, state: dict, version: int) -> None:
        required = {"portfolio", "protection", "risk", "lots", "outbox",
                    "intents", "attempts", "startup_reconciliation"}
        if not required <= state.keys():
            raise ValueError("실행 checkpoint/시작 대사 기준선이 없습니다")
        # 전체 decode를 먼저 끝낸다. live object deepcopy/legacy 파일 I/O 없음.
        portfolio = decode_portfolio(state["portfolio"])
        protection = decode_protection(state["protection"], clock=self.clock)
        risk = validate_risk(state["risk"])
        for symbol, position in portfolio.positions.items():
            if symbol in self._quotes:
                position.current_price = self._quotes[symbol]
            guarded = protection.get_state(symbol)
            degraded = state["protection"].get("degraded", {}).get(symbol)
            if guarded is None or guarded.remaining_quantity != position.quantity:
                if not degraded or degraded.get("quantity") != position.quantity:
                    raise ValueError("보유 수량과 보호/degraded 수량이 불일치합니다")
        if set(protection._states) - portfolio.positions.keys():
            raise ValueError("경제 포지션 없는 청산 상태입니다")
        # await 없는 게시: Portfolio identity는 MarketContext/validator와 공유.
        current = self.engine.portfolio
        current.cash = portfolio.cash
        current.positions = portfolio.positions
        current.initial_capital = portfolio.initial_capital
        current.market = portfolio.market
        current.currency = portfolio.currency
        current.daily_pnl = portfolio.daily_pnl
        current.daily_trades = portfolio.daily_trades
        current.daily_start_unrealized_pnl = portfolio.daily_start_unrealized_pnl
        publish_protection(self.exit_manager, state["protection"], clock=self.clock)
        if self.risk_manager is not None:
            publish_risk(self.risk_manager, risk)
        self.engine._counted_buy_order_ids = set(risk["counted_buy_orders"])
        self.engine._execution_version = version

    def _reduce(self, state, observation, delta) -> FillReduction:
        now = self._now()
        economic = reduce_economics(state, observation, delta, now=now)
        protection, status = reduce_protection(
            economic.state["protection"], before=economic.before_position,
            after=economic.after_position, observation=observation, delta=delta,
            fill_kind=economic.fill_kind, intent_id=economic.intent_id, now=now,
        )
        economic.state["protection"] = protection
        economic.state["outbox"][observation.observation_id]["source_version"] = self.owner.version + 1
        capture_fill(state, economic.state, observation, delta, fill_kind=economic.fill_kind,
                     intent_id=economic.intent_id, status=status, version=self.owner.version + 1, now=now)
        return FillReduction(economic.state, protection_status=status, journal_pending=True)

    async def apply_observation(self, observation: FillObservation):
        if not isinstance(observation, FillObservation):
            raise TypeError("누적체결 관측이 필요합니다")
        return await self.owner.apply(observation)

    async def quote(self, symbol: str, price: Decimal, *, market_data=None,
                    intent_id: str | None = None):
        """입력의 durable 접수 후 view 게시, 보호 적용 후 완료. 제안은 주문 전송이 아니다."""
        if self._closing:
            raise ApplicationBlocked("종료 중에는 새 보호 명령을 수락하지 않습니다")
        if type(symbol) is not str or not symbol or symbol != symbol.strip():
            raise ValueError("종목 식별자 오류")
        if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
            raise ValueError("현재가는 양의 유한 Decimal이어야 합니다")
        now = self._now()
        market_data = deepcopy(market_data)
        command_id = "quote:" + uuid4().hex
        request = {"symbol": symbol, "price": str(price), "market_data": market_data,
                   "intent_id": intent_id, "observed_at": now.isoformat()}
        request_digest = digest(request)  # JSON 입력 검증도 수락 전에 끝낸다.
        if intent_id is not None and (type(intent_id) is not str or not intent_id or intent_id != intent_id.strip()):
            raise ValueError("보호 intent 식별자 오류")
        decision = None

        def admit(state):
            pending = state.setdefault("protection_quote_admissions", {})
            if any(row["symbol"] == symbol for row in pending.values()):
                raise ApplicationBlocked("미해결 보호 가격 입력을 먼저 대사해야 합니다")
            pending[command_id] = {**request, "payload_digest": request_digest,
                                   "status": "RECEIVED", "source_version": self.owner.version + 1,
                                   "admitted_at": self._now().isoformat()}
            return state

        def reduce(state):
            nonlocal decision
            admission = state.get("protection_quote_admissions", {}).get(command_id)
            if (not admission or admission["status"] != "RECEIVED"
                    or admission["payload_digest"] != request_digest
                    or any(admission[key] != value for key, value in request.items())):
                raise ApplicationBlocked("보호 가격 접수 증거 불일치")
            before = deepcopy(state)
            dto, decision = quote_protection(
                state["protection"], symbol=symbol, price=price, now=now,
                market_data=market_data, intent_id=intent_id,
            )
            state["protection"] = dto
            protective = decode_protection(dto, clock=self.clock).get_state(symbol)
            if protective is not None and symbol in state["portfolio"]["positions"]:
                state["portfolio"]["positions"][symbol]["highest_price"] = str(protective.highest_price)
            if decision is not None:
                state["outbox"][command_id] = {
                    "kind": "protection_decision", "symbol": symbol,
                    "intent_id": intent_id, "decision": list(decision),
                    "status": "pending", "observed_at": now.isoformat(),
                }
            capture_quote(before, state, symbol, price=price, market_data=market_data,
                          intent_id=intent_id, command_id=command_id, decision=decision,
                          version=self.owner.version + 1, now=now)
            # 보호 결과/재생 입력과 같은 commit에서만 미해결 접수를 제거한다.
            del state["protection_quote_admissions"][command_id]
            return state

        async def apply_quote():
            # 두 owner transaction 사이에도 가격 입력 순서가 뒤집히지 않게 한다.
            async with self._quote_lock:
                await self.owner.mutate("quote-admit:" + command_id, admit)
                # 저장 실패/결과불명 때는 수락하거나 view를 게시하지 않는다.
                self._quotes[symbol] = price
                position = self.engine.portfolio.positions.get(symbol)
                if position is not None:
                    position.current_price = price
                await self.owner.mutate(command_id, reduce)
                return decision

        # 수락된 가격은 호출자의 취소와 분리한다. 강한 참조와 종료 drain으로
        # 앞선 fill commit을 기다리는 동안의 고점/손절 접촉을 버리지 않는다.
        task = asyncio.create_task(apply_quote())
        self._protection_tasks.add(task)

        def completed(done):
            self._protection_tasks.discard(done)
            if done.cancelled() or done.exception() is not None:
                self._protection_failed = True

        task.add_done_callback(completed)
        return await asyncio.shield(task)

    async def repair_protection(self, operation_id: str, symbol: str, *, expected_version: int):
        if self._closing:
            raise ApplicationBlocked("종료 중에는 새 보호 명령을 수락하지 않습니다")
        if any(type(value) is not str or not value or value != value.strip()
               for value in (operation_id, symbol)):
            raise ValueError("복구 명령/종목 식별자 오류")
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("복구 version 오류")
        request = {"kind": "protection_repair", "operation_id": operation_id,
                   "symbol": symbol, "expected_version": expected_version}

        async def apply_repair():
            applied_here = False
            def reduce(state):
                nonlocal applied_here
                applied_here = operation_id not in state.get("recovery_receipts", {})
                return reduce_repair(state, operation_id, symbol,
                                     expected_version=expected_version, state_version=self.owner.version)
            await self.owner.mutate("protection-repair:" + digest(request), reduce)
            row = self.owner.state["recovery_receipts"][operation_id]
            return RecoveryReceipt(operation_id,
                                   "ALREADY_APPLIED" if not applied_here and row["status"] == "APPLIED" else row["status"],
                                   row["reason"], row["committed_version"])

        task = asyncio.create_task(apply_repair())
        self._protection_tasks.add(task)
        def completed(done):
            self._protection_tasks.discard(done)
            if done.cancelled() or done.exception() is not None:
                self._protection_failed = True
        task.add_done_callback(completed)
        return await asyncio.shield(task)

    async def shutdown(self) -> None:
        """새 명령 수락을 닫고 이미 수락한 보호 작업을 저장/실패 확정까지 추적한다."""
        self._closing = True
        if self._protection_tasks:
            await asyncio.gather(*(asyncio.shield(task) for task in tuple(self._protection_tasks)),
                                 return_exceptions=True)

    def health(self) -> dict:
        state = self.owner.state
        return {
            "store_healthy": self.owner.healthy,
            "execution_version": self.owner.version,
            "published_version": self.owner.published_version,
            "publication_recovery_required": self.owner.publication_recovery_required,
            "protection_updates_failed": self._protection_failed,
            "protection_updates_pending": len(self._protection_tasks),
            "protection_quote_admissions_pending": len(state.get("protection_quote_admissions", {})),
            "trading_ready": False, "block_reason": "runtime_integration_incomplete",
            "protection_degraded": len(state.get("protection", {}).get("degraded", {})),
            "unapplied_inbox": sum(row.get("status") not in ("APPLIED", "SUPERSEDED")
                                    for row in state.get("inbox", {}).values()),
            "outbox_pending": sum(type(row) is not dict or row.get("status") != "delivered"
                                  for row in state.get("outbox", {}).values()),
        }
