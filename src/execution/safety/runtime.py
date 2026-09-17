"""KR 경제/보호 projection과 core receipt. 전 writer 이행 전 운영 설치 금지."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from typing import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from .application import ApplicationBlocked, FillApplicationCoordinator, FillObservation, FillReduction, IngressContext
from .day_recovery import (DayReceipt, RolloverFence, ValuationEvidence, aware, day, text,
                           scope_reason, unresolved_reason, validate_valuation, reset_daily)
from .economics import decode_portfolio, reduce_economics, validate_risk, publish_risk
from .lifecycle import OrderLifecycleCoordinator
from .initial_r import capture_initial_stop, capture_finality, reduce_finalize_initial_r
from .protection import decode_protection, publish_protection, quote_protection, reduce_protection
from .protection_recovery import RecoveryReceipt, capture_fill, capture_quote, digest, reduce_repair
from .store import ExecutionStateStore


class KRExecutionRuntime:
    """경제·보호 후보를 하나의 checkpoint에 commit 후 함께 게시한다."""

    def __init__(self, store: ExecutionStateStore, engine, exit_manager, *,
                 clock: Callable[[], datetime], risk_manager=None, account_scope: str | None = None):
        self.engine = engine
        self.exit_manager = exit_manager
        self.risk_manager = risk_manager
        self.clock = clock
        self.account_scope = None if account_scope is None else text(account_scope)
        self._day_closed = False
        self._day_generation = 0
        self._day_fence_id = None
        self._day_tasks: set[asyncio.Task] = set()
        self._quotes: dict[str, Decimal] = {}
        self._quote_metadata: dict[str, dict] = {}
        self._published_day = None
        self._protection_tasks: set[asyncio.Task] = set()
        self._quote_lock = asyncio.Lock()
        self._protection_failed = False
        self._closing = False
        self.owner = FillApplicationCoordinator(store, self._publish, self._reduce)
        self.lifecycle = OrderLifecycleCoordinator(self.owner, clock=clock,
                                                   admission_guard=self._require_day_admission,
                                                   finality_recorder=capture_finality)

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
        version = await self.owner.restore()
        transition = self.owner.state.get("day_transition", {})
        self._day_generation = max(self._day_generation, transition.get("admission_generation", 0))
        self._day_fence_id = transition.get("fence_id", self._day_fence_id)
        if transition and transition["phase"] != "RESUMED":
            self._day_closed = True
        return version

    @property
    def day_admission_closed(self):
        state = self.owner.state
        transition = state.get("day_transition", {})
        return (self._day_closed or (bool(transition) and transition["phase"] != "RESUMED")
                or any(row.get("ingress_context", {}).get("defer_reason")
                       and row.get("status") not in ("APPLIED", "SUPERSEDED")
                       for row in state.get("inbox", {}).values())
                or state.get("risk", {}).get("day") != self._now().date().isoformat())

    def _require_day_admission(self):
        if self._closing or self.day_admission_closed:
            raise ApplicationBlocked("day_transition_admission_closed")

    def ingress_context(self, ticket, *, replay_fence_id=None):
        return IngressContext(ticket, self._day_generation, self._now(), self._day_fence_id,
                              "day_transition_parked" if self.day_admission_closed else "", replay_fence_id)

    def _require_replay(self, observation, fence_id):
        state = self.owner.state
        transition = state.get("day_transition", {})
        if (transition.get("fence_id") != fence_id or transition.get("phase") != "ROLLED_OVER"
                or observation.trading_day != state["risk"]["day"]
                or observation.trading_day != self._now().date().isoformat()
                or observation.account_scope != self.account_scope or observation.market != "KR"):
            raise ApplicationBlocked("parked_replay_scope_day_or_fence_mismatch")

    async def replay_parked_observation(self, observation_id, *, fence_id):
        row = self.owner.state.get("inbox", {}).get(text(observation_id))
        if row is None:
            raise ApplicationBlocked("durable_observation_required")
        observation = FillObservation(**row["observation"])
        if observation.observation_id != observation_id:
            raise ApplicationBlocked("durable_observation_digest_conflict")
        self._require_replay(observation, fence_id)
        return await self.engine._ingress_execution_observation(observation, replay_fence_id=fence_id)

    def _quiescence_reason(self, state):
        if not self.owner.healthy or self.engine._execution_version != self.owner.version:
            return "publication_mismatch"
        if any(row["state"] not in ("SETTLED", "PARKED", "RESOLVED") for row in self.engine._execution_ingress.values()):
            return "ingress_not_settled"
        if any(event.type.name == "EXECUTION_FILL" for event in self.engine._event_queue):
            return "execution_queue_not_empty"
        if any(not task.done() for task in self.engine._execution_apply_tasks):
            return "execution_owner_wait"
        if any(not task.done() for task in self._protection_tasks):
            return "protection_tasks_pending"
        if self._protection_failed:
            return "protection_update_failed"
        return scope_reason(state, self.account_scope) or unresolved_reason(state)

    def _drained_ticket(self):
        drained = 0
        for ticket, row in self.engine._execution_ingress.items():
            if row["state"] not in ("SETTLED", "RESOLVED"):
                break
            drained = ticket
        return drained

    async def _day_command(self, request, reducer, *, fence_receipt=False):
        if self._closing:
            raise ApplicationBlocked("종료 중에는 새 일자 명령을 수락하지 않습니다")
        text(request["operation_id"])
        if type(request["expected_version"]) is not int or request["expected_version"] < 0:
            raise ValueError("invalid_execution_version")
        request_digest = digest(request)
        async def execute():
            def reduce(state):
                receipts = state.setdefault("recovery_receipts", {})
                previous = receipts.get(request["operation_id"])
                if previous is not None:
                    if previous["request_digest"] != request_digest:
                        raise ValueError("recovery_operation_id_conflict")
                    return state
                result = reducer(state, "" if self.owner.version == request["expected_version"]
                                 else "stale_execution_version")
                receipts[request["operation_id"]] = {
                    "request": request, "request_digest": request_digest,
                    "operation_id": request["operation_id"], "committed_version": self.owner.version + 1,
                    **result}
                return state
            await self.owner.mutate("day:" + request_digest, reduce)
            row = self.owner.state["recovery_receipts"][request["operation_id"]]
            cls = RolloverFence if fence_receipt else DayReceipt
            return cls(**{key: row[key] for key in cls.__dataclass_fields__ if key in row})
        task = asyncio.create_task(execute())
        self._day_tasks.add(task)
        def completed(done):
            self._day_tasks.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(completed)
        return await asyncio.shield(task)

    async def prepare_day_rollover(self, operation_id, *, expected_version, from_day, to_day, valuation_boundary):
        boundary = aware(valuation_boundary)
        request = {"kind": "prepare_day", "operation_id": text(operation_id), "expected_version": expected_version,
                   "from_day": day(from_day), "to_day": day(to_day), "account_scope": self.account_scope,
                   "market": "KR", "valuation_boundary": boundary.isoformat()}
        if from_day >= to_day or to_day != self._now().date().isoformat() or boundary.date().isoformat() != to_day or boundary > self._now():
            raise ValueError("invalid_rollover_day_or_boundary")
        previous = self.owner.state.get("recovery_receipts", {}).get(operation_id)
        if previous and previous["request_digest"] != digest(request):
            raise ValueError("recovery_operation_id_conflict")
        if not previous:
            was_closed = self._day_closed
            self._day_closed = True
            if not was_closed and (not self.owner.state.get("day_transition") or self.owner.state["day_transition"]["phase"] == "RESUMED"):
                self._day_generation += 1
                self._day_fence_id = "day:" + digest(request)
        def reduce(state, reason):
            reason = reason or scope_reason(state, self.account_scope)
            transition = state.get("day_transition")
            if transition and transition["phase"] != "RESUMED":
                reason = reason or "day_transition_already_active"
            elif not scope_reason(state, self.account_scope):
                transition = {"fence_id": self._day_fence_id, "account_scope": self.account_scope, "market": "KR",
                              "from_day": from_day, "to_day": to_day, "valuation_boundary": boundary.isoformat(),
                              "admission_generation": self._day_generation, "phase": "PREPARED",
                              "drained_ticket": self._drained_ticket(), "drained_version": self.owner.version + 1}
                state["day_transition"] = transition
            reason = reason or ("from_day_mismatch" if state["risk"]["day"] != from_day else "")
            reason = reason or self._quiescence_reason(state)
            fields = {key: value for key, value in (transition or {}).items() if key != "phase"}
            return {"status": "BLOCKED" if reason else "PREPARED", "reason": reason, **fields}
        return await self._day_command(request, reduce, fence_receipt=True)

    async def accept_valuation_evidence(self, operation_id, evidence, *, expected_version):
        if not isinstance(evidence, ValuationEvidence):
            raise TypeError("ValuationEvidence가 필요합니다")
        payload = evidence.to_dict()
        evidence_id = digest(payload)
        request = {"kind": "valuation", "operation_id": operation_id,
                   "expected_version": expected_version, "evidence": payload}
        def reduce(state, reason):
            if not reason:
                try:
                    transition = state["day_transition"]
                    validate_valuation(state, payload, transition, self._now())
                    if transition["phase"] != "PREPARED":
                        raise ValueError("valuation_requires_prepared_fence")
                    state.setdefault("day_valuations", {}).setdefault(evidence_id, {
                        "payload": payload, "digest": evidence_id, "accepted_at": self._now().isoformat()})
                except (ValueError, KeyError, TypeError) as exc:
                    reason = str(exc)
            return {"status": "BLOCKED" if reason else "APPLIED", "reason": reason,
                    "fence_id": evidence.fence_id, "evidence_id": evidence_id if not reason else ""}
        return await self._day_command(request, reduce)

    async def rollover_day(self, operation_id, *, expected_version, fence_id, valuation_evidence_id):
        request = {"kind": "rollover_day", "operation_id": operation_id, "expected_version": expected_version,
                   "fence_id": text(fence_id), "valuation_evidence_id": text(valuation_evidence_id)}
        def reduce(state, reason):
            transition = state.get("day_transition", {})
            reason = reason or self._quiescence_reason(state)
            if not reason and (transition.get("fence_id") != fence_id or transition.get("phase") != "PREPARED"
                               or transition.get("admission_generation") != self._day_generation):
                reason = "invalid_rollover_fence"
            if not reason and state["risk"]["day"] != transition["from_day"]:
                reason = "from_day_mismatch"
            if not reason:
                try:
                    row = state.get("day_valuations", {}).get(valuation_evidence_id)
                    if row is None or digest(row["payload"]) != row["digest"] or row["digest"] != valuation_evidence_id:
                        raise ValueError("valuation_evidence_missing_or_conflicting")
                    prices, unrealized = validate_valuation(state, row["payload"], transition, self._now())
                    reset_daily(state, prices, unrealized, transition["to_day"])
                    transition.update(phase="ROLLED_OVER", rollover_version=self.owner.version + 1,
                                      valuation_evidence_id=valuation_evidence_id,
                                      drained_ticket=self._drained_ticket(), drained_version=self.owner.version)
                    state["day_valuation_view"] = {"evidence_id": valuation_evidence_id,
                                                   "rollover_version": self.owner.version + 1}
                except (ValueError, KeyError, TypeError) as exc:
                    reason = str(exc)
            return {"status": "BLOCKED" if reason else "APPLIED", "reason": reason, "fence_id": fence_id}
        return await self._day_command(request, reduce)

    async def resume_after_rollover(self, operation_id, *, expected_version, fence_id):
        request = {"kind": "resume_day", "operation_id": operation_id,
                   "expected_version": expected_version, "fence_id": text(fence_id)}
        def reduce(state, reason):
            transition = state.get("day_transition", {})
            reason = reason or self._quiescence_reason(state)
            if not reason and (transition.get("fence_id") != fence_id or transition.get("phase") != "ROLLED_OVER"
                               or state["risk"]["day"] != self._now().date().isoformat()):
                reason = "rollover_not_complete"
            if not reason:
                transition["phase"] = "RESUMED"
            return {"status": "BLOCKED" if reason else "APPLIED", "reason": reason, "fence_id": fence_id}
        receipt = await self._day_command(request, reduce)
        if receipt.status == "APPLIED" and not self._quiescence_reason(self.owner.state):
            self._day_closed = False
        return receipt

    def attach(self) -> None:
        """명시 설치. 운영 진입점에는 아직 호출되지 않는다."""
        if not self.owner.healthy:
            raise ApplicationBlocked("유효한 checkpoint를 먼저 게시해야 합니다")
        self.engine.bind_execution_runtime(self)

    def _view_price(self, state, symbol, fallback):
        """평가·증분 체결·수락 시세의 동일 우선순위를 모든 게시에서 사용한다."""
        view = state.get("day_valuation_view", {})
        valuation = state.get("day_valuations", {}).get(view.get("evidence_id"), {}).get("payload")
        valued = next((row for row in valuation["prices"] if row["symbol"] == symbol), None) if valuation else None
        fill = max((row for row in state["outbox"].values()
                    if row.get("observation", {}).get("symbol") == symbol),
                   key=lambda row: row.get("source_version", 0), default=None)
        latest_fill = fill.get("source_version", 0) if fill else 0
        price = fallback
        if valued:
            price = Decimal(valued["price"])
        if latest_fill and (not valued or latest_fill > view["rollover_version"]):
            # SELL DTO의 옛 현재가나 누적 평균 대신 실제 증분 체결가를 복원한다.
            price = Decimal(fill["amount"]) / fill["quantity"]
        metadata = self._quote_metadata.get(symbol)
        floor = self._quote_time_floor(state, symbol)
        if (symbol in self._quotes and metadata and metadata["source_version"] > latest_fill
                and (not valued or (metadata["source_version"] > view["rollover_version"]
                     and aware(datetime.fromisoformat(metadata["received_at"])) >= aware(datetime.fromisoformat(valuation["valuation_boundary"]))))
                and (metadata["market_as_of"] is None or floor is None
                     or aware(datetime.fromisoformat(metadata["market_as_of"])) >= floor)):
            price = self._quotes[symbol]
        return price

    @staticmethod
    def _market_quote_payload(symbol, row):
        # 시장 사실의 동일성에는 호출자 intent와 새 수신 시각을 포함하지 않는다.
        return {"symbol": symbol, **{key: row[key] for key in (
            "price", "as_of", "source", "source_event_id", "market_data")}}

    def _validate_explicit_quotes(self, state, version):
        rows = state.get("latest_explicit_quote", {})
        if type(rows) is not dict:
            raise ValueError("invalid_explicit_quote_watermarks")
        fields = {"price", "as_of", "source", "source_event_id", "market_data",
                  "received_at", "admission_version", "payload_digest"}
        for symbol, row in rows.items():
            text(symbol)
            if type(row) is not dict or row.keys() != fields:
                raise ValueError("invalid_explicit_quote_watermark")
            text(row["source"])
            text(row["source_event_id"])
            if type(row["price"]) is not str:
                raise ValueError("invalid_explicit_quote_price")
            price = Decimal(row["price"])
            if not price.is_finite() or price <= 0:
                raise ValueError("invalid_explicit_quote_price")
            observed = aware(datetime.fromisoformat(row["as_of"]))
            received = aware(datetime.fromisoformat(row["received_at"]))
            if observed > received:
                raise ValueError("future_explicit_quote")
            if type(row["admission_version"]) is not int or not 0 < row["admission_version"] <= version:
                raise ValueError("invalid_explicit_quote_version")
            if row["payload_digest"] != digest(self._market_quote_payload(symbol, row)):
                raise ValueError("explicit_quote_digest_conflict")

    def _quote_time_floor(self, state, symbol):
        view = state.get("day_valuation_view", {})
        valuation = state.get("day_valuations", {}).get(view.get("evidence_id"), {}).get("payload")
        valued = next((row for row in valuation["prices"] if row["symbol"] == symbol), None) if valuation else None
        watermark = state.get("latest_explicit_quote", {}).get(symbol)
        return max((aware(datetime.fromisoformat(row["as_of"])) for row in (valued, watermark) if row), default=None)

    def _require_quote_freshness(self, state, symbol, observed, market_quote=None):
        if observed is None:
            return
        floor = self._quote_time_floor(state, symbol)
        if floor is not None and observed < floor:
            raise ApplicationBlocked("stale_market_quote")
        previous = state.get("latest_explicit_quote", {}).get(symbol)
        if (market_quote is not None and previous is not None
                and (previous["source"], previous["source_event_id"]) == (market_quote["source"], market_quote["source_event_id"])
                and previous["payload_digest"] != digest(self._market_quote_payload(symbol, market_quote))):
            raise ApplicationBlocked("market_quote_event_conflict")

    def _publish(self, state: dict, version: int) -> None:
        required = {"portfolio", "protection", "risk", "lots", "outbox",
                    "intents", "attempts", "startup_reconciliation"}
        if not required <= state.keys():
            raise ValueError("실행 checkpoint/시작 대사 기준선이 없습니다")
        self._validate_explicit_quotes(state, version)
        # 전체 decode를 먼저 끝낸다. live object deepcopy/legacy 파일 I/O 없음.
        portfolio = decode_portfolio(state["portfolio"])
        protection = decode_protection(state["protection"], clock=self.clock)
        risk = validate_risk(state["risk"])
        for symbol, position in portfolio.positions.items():
            position.current_price = self._view_price(state, symbol, position.current_price)
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
        if self._published_day is not None and self._published_day != risk["day"]:
            from ...core.types import RiskMetrics
            self.engine.risk_metrics = RiskMetrics()
        self._published_day = risk["day"]
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
        capture_initial_stop(state, economic.state, observation, fill_kind=economic.fill_kind,
                             status=status, version=self.owner.version + 1, now=now)
        capture_fill(state, economic.state, observation, delta, fill_kind=economic.fill_kind,
                     intent_id=economic.intent_id, status=status, version=self.owner.version + 1, now=now)
        return FillReduction(economic.state, protection_status=status, journal_pending=True)

    async def apply_observation(self, observation: FillObservation, *, ingress_context=None):
        if not isinstance(observation, FillObservation):
            raise TypeError("누적체결 관측이 필요합니다")
        def gate():
            if ingress_context and ingress_context.replay_fence_id:
                self._require_replay(observation, ingress_context.replay_fence_id)
            elif self.day_admission_closed:
                transition = self.owner.state.get("day_transition", {})
                if (ingress_context is None or ingress_context.defer_reason
                        or ingress_context.generation >= transition.get("admission_generation", 0)
                        or transition.get("phase") != "PREPARED"):
                    raise ApplicationBlocked("day_transition_application_closed")
        return await self.owner.apply(observation, application_gate=gate)

    async def quote(self, symbol: str, price: Decimal, *, market_data=None,
                    intent_id: str | None = None, market_as_of: datetime | None = None,
                    source: str | None = None, source_event_id: str | None = None):
        """입력의 durable 접수 후 view 게시, 보호 적용 후 완료. 제안은 주문 전송이 아니다."""
        if self._closing:
            raise ApplicationBlocked("종료 중에는 새 보호 명령을 수락하지 않습니다")
        self._require_day_admission()
        if type(symbol) is not str or not symbol or symbol != symbol.strip():
            raise ValueError("종목 식별자 오류")
        if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
            raise ValueError("현재가는 양의 유한 Decimal이어야 합니다")
        now = self._now()
        if any(value is not None for value in (market_as_of, source, source_event_id)):
            observed = aware(market_as_of)
            text(source)
            text(source_event_id)
            if observed > now:
                raise ValueError("future_market_quote")
        else:
            observed = None
        market_data = deepcopy(market_data)
        command_id = "quote:" + uuid4().hex
        request = {"symbol": symbol, "price": str(price), "market_data": market_data,
                   "intent_id": intent_id, "observed_at": now.isoformat(),
                   "market_as_of": observed.isoformat() if observed is not None else None,
                   "source": source, "source_event_id": source_event_id}
        request_digest = digest(request)  # JSON 입력 검증도 수락 전에 끝낸다.
        market_quote = {"price": request["price"], "as_of": request["market_as_of"],
                        "source": source, "source_event_id": source_event_id, "market_data": market_data}
        self._require_quote_freshness(self.owner.state, symbol, observed, market_quote)
        if intent_id is not None and (type(intent_id) is not str or not intent_id or intent_id != intent_id.strip()):
            raise ValueError("보호 intent 식별자 오류")
        decision = None
        admission_rejected = False

        def admit(state):
            nonlocal admission_rejected
            # owner 대기 중 기준이 바뀔 수 있다. durable 접수 전에만 재검사한다.
            try:
                self._require_quote_freshness(state, symbol, observed, market_quote)
            except ApplicationBlocked:
                admission_rejected = True
                raise
            pending = state.setdefault("protection_quote_admissions", {})
            if any(row["symbol"] == symbol for row in pending.values()):
                raise ApplicationBlocked("미해결 보호 가격 입력을 먼저 대사해야 합니다")
            pending[command_id] = {**request, "payload_digest": request_digest,
                                   "status": "RECEIVED", "source_version": self.owner.version + 1,
                                   "admitted_at": self._now().isoformat()}
            if observed is not None:
                # 종목당 한 행만 보관하며 시각 없는 후속 입력은 이 근거를 지우지 않는다.
                state.setdefault("latest_explicit_quote", {})[symbol] = {
                    **market_quote, "received_at": request["observed_at"],
                    "admission_version": self.owner.version + 1,
                    "payload_digest": digest(self._market_quote_payload(symbol, market_quote))}
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
                    "provenance": {"market_as_of": request["market_as_of"], "source": source,
                                   "source_event_id": source_event_id, "received_at": request["observed_at"]},
                }
            capture_quote(before, state, symbol, price=price, market_data=market_data,
                          intent_id=intent_id, command_id=command_id, decision=decision,
                          version=self.owner.version + 1, now=now,
                          provenance={"market_as_of": request["market_as_of"], "source": source,
                                      "source_event_id": source_event_id, "received_at": request["observed_at"]})
            # 보호 결과/재생 입력과 같은 commit에서만 미해결 접수를 제거한다.
            del state["protection_quote_admissions"][command_id]
            return state

        async def apply_quote():
            # 두 owner transaction 사이에도 가격 입력 순서가 뒤집히지 않게 한다.
            async with self._quote_lock:
                await self.owner.mutate("quote-admit:" + command_id, admit)
                # 저장 실패/결과불명 때는 수락하거나 view를 게시하지 않는다.
                self._quotes[symbol] = price
                self._quote_metadata[symbol] = {"received_at": now.isoformat(),
                                               "source_version": self.owner.version,
                                               "market_as_of": request["market_as_of"],
                                               "source": source, "source_event_id": source_event_id}
                position = self.engine.portfolio.positions.get(symbol)
                if position is not None:
                    position.current_price = self._view_price(self.owner.state, symbol, position.current_price)
                await self.owner.mutate(command_id, reduce)
                return decision

        # 수락된 가격은 호출자의 취소와 분리한다. 강한 참조와 종료 drain으로
        # 앞선 fill commit을 기다리는 동안의 고점/손절 접촉을 버리지 않는다.
        task = asyncio.create_task(apply_quote())
        self._protection_tasks.add(task)

        def completed(done):
            self._protection_tasks.discard(done)
            if done.cancelled() or (done.exception() is not None and not admission_rejected):
                self._protection_failed = True

        task.add_done_callback(completed)
        return await asyncio.shield(task)

    async def repair_protection(self, operation_id: str, symbol: str, *, expected_version: int):
        if self._closing:
            raise ApplicationBlocked("종료 중에는 새 보호 명령을 수락하지 않습니다")
        self._require_day_admission()
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

    async def finalize_initial_r(self, operation_id: str, order_key: str, *, expected_version: int,
                                 initial_stop_evidence_id: str, finality_evidence_id: str):
        """검증된 첫 진입 R만 확정한다. 원장 전달/거래 허가와 별개의 receipt다."""
        self._require_day_admission()
        for value in (operation_id, order_key, initial_stop_evidence_id, finality_evidence_id):
            text(value)
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("invalid_execution_version")
        request = {"kind": "initial_r_finalize", "operation_id": operation_id, "order_key": order_key,
                   "initial_stop_evidence_id": initial_stop_evidence_id,
                   "finality_evidence_id": finality_evidence_id, "expected_version": expected_version}

        async def execute():
            def reduce(state):
                # owner 대기 중 일자 fence가 닫혔다면 명시 거부하고 재시도를 요구한다.
                self._require_day_admission()
                return reduce_finalize_initial_r(
                    state, operation_id, order_key, initial_stop_evidence_id=initial_stop_evidence_id,
                    finality_evidence_id=finality_evidence_id, expected_version=expected_version,
                    state_version=self.owner.version, now=self._now())
            await self.owner.mutate("initial-r:" + digest(request), reduce)
            row = self.owner.state["recovery_receipts"][operation_id]
            return RecoveryReceipt(operation_id, row["status"], row["reason"], row["committed_version"])

        task = asyncio.create_task(execute())
        self._protection_tasks.add(task)
        def completed(done):
            self._protection_tasks.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(completed)
        return await asyncio.shield(task)

    async def shutdown(self) -> None:
        """새 명령 수락을 닫고 이미 수락한 보호 작업을 저장/실패 확정까지 추적한다."""
        self._closing = True
        if self._day_tasks:
            await asyncio.gather(*(asyncio.shield(task) for task in tuple(self._day_tasks)), return_exceptions=True)
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
            "day_admission_closed": self.day_admission_closed,
            "ingress_pending": sum(row["state"] not in ("SETTLED", "PARKED", "FAILED", "RESOLVED")
                                   for row in self.engine._execution_ingress.values()),
            "protection_quote_admissions_pending": len(state.get("protection_quote_admissions", {})),
            "trading_ready": False, "block_reason": "runtime_integration_incomplete",
            "protection_degraded": len(state.get("protection", {}).get("degraded", {})),
            "unapplied_inbox": sum(row.get("status") not in ("APPLIED", "SUPERSEDED")
                                    for row in state.get("inbox", {}).values()),
            "outbox_pending": sum(type(row) is not dict or row.get("status") != "delivered"
                                  for row in state.get("outbox", {}).values()),
        }
