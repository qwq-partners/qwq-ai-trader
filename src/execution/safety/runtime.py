"""KR 경제/보호 projection과 core receipt. 전 writer 이행 전 운영 설치 금지."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from typing import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from loguru import logger

from .application import (ApplicationBlocked, FillApplicationCoordinator, FillObservation,
                          FillReduction, InboxReceipt, IngressContext, observation_from_evidence)
from .day_recovery import (DayReceipt, RolloverFence, ValuationEvidence, aware, day, text,
                           scope_reason, unresolved_reason, validate_valuation, reset_daily)
from .economics import decode_portfolio, reduce_economics, validate_risk, publish_risk
from .evidence import evidence_pages, parse_order_evidence, parser_scope
from .lifecycle import (OrderEvidence, OrderLifecycleCoordinator, OrderRef, OrderState,
                        TERMINAL_STATES)
from .initial_r import capture_initial_stop, capture_finality, reduce_finalize_initial_r
from .protection import decode_protection, publish_protection, quote_protection, reduce_protection
from .protection_recovery import RecoveryReceipt, capture_fill, capture_quote, digest, reduce_repair
from .store import ExecutionStateStore
from .market_source import (canonical_observation, observation_from_event, complete_source,
                            validate_sources, completed_duplicate, observation_market_data,
                            quote_price_view, validate_price_views)


# ── P0-2 2단계: 체결 증거 생산자(주기 task). 제품 호출자 0건·live 파일 0줄 ──────────
# 한 주기 전체를 `cycle_timeout`으로 감싼다(P-1). 최악 예산은 조회 한 번이 쓰는 시간이고
# 그것은 `request_timeout × 페이지 수`다. 수집기 기본 10페이지면 15×10=150초가 되어 손절
# 지연의 상한이 무의미해지므로 **이 단계는 페이지 상한을 3으로 낮춘다**: 15×3=45초 + 적용/
# commit 여유 15초 = 기본 60초. 같은 상한을 파서에도 넘겨 4페이지 이상 수집은 complete로
# 인정하지 않는다(더 넓은 조회가 필요하다고 판명되면 두 값을 함께 올려야 한다).
RECONCILER_MAX_PAGES = 3
RECONCILER_REQUEST_TIMEOUT = 15.0
RECONCILER_CYCLE_TIMEOUT = 60.0
RECONCILER_INTERVAL = 3.0
# 대상이 있는데 이만큼의 주기 동안 상태가 전진하지 않으면 막힌 것으로 본다(P-2).
RECONCILER_STALL_CYCLES = 5
# `economics._matching_attempt` 가 받아 주는 attempt 상태. 이 밖(또는 evidence_conflict)이면
# `reduce_economics` 가 ValueError 를 내므로 재접수는 FAILED ingress 행만 늘린다. economics.py
# 는 이 단계의 허용 파일이 아니라 같은 집합을 여기 다시 적는다 —
# `test_the_appliable_state_set_is_the_one_economics_enforces` 가 어긋남을 잡는다.
APPLIABLE_ATTEMPT_STATES = frozenset(s.value for s in (
    OrderState.OPEN, OrderState.PARTIAL, OrderState.FINAL_FILLED,
    OrderState.FINAL_CANCELLED, OrderState.FINAL_EXPIRED,
))


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
        self._command_scopes: dict[asyncio.Future, asyncio.Task] = {}
        self._command_tracking_started = False
        self._command_result_tasks: set[asyncio.Task] = set()
        self._command_results_failed = False
        self._quote_lock = asyncio.Lock()
        self._protection_failed = False
        self._closing = False
        self._intraday_writer = None
        self._regime_writer = None
        self.gateway = None
        self._reconciler_task = None
        self._reconciler_tasks: set[asyncio.Task] = set()
        self._reconciler_wakeup = None
        self._reconciler_interval = RECONCILER_INTERVAL
        self._reconciler_cycle_timeout = RECONCILER_CYCLE_TIMEOUT
        self._reconciler_started_at = None
        self._reconciler_cycle_started_at = None
        self._reconciler_complete_at = None
        self._reconciler_progress_at = None
        self._reconciler_last_reason = "not_started"
        self._reconciler_target_count = 0
        self._reconciler_skipped: dict[str, int] = {}
        self._reconciler_outcomes: dict[str, int] = {}
        self._reconciler_parked = 0
        self.owner = FillApplicationCoordinator(store, self._publish, self._reduce,
            registration_scope=self._policy_registration_scope,
            registration_guard=self._require_registration_day)
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

    @contextmanager
    def _policy_registration_scope(self):
        self._require_day_admission()
        with self.command_scope():
            yield

    def _require_registration_day(self):
        # 이미 접수된 명령은 closing에도 drain하되 새 day 경계를 넘지 않는다.
        if self.day_admission_closed:
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
        if self.risk_manager is not None:
            self.risk_manager._execution_runtime = self
        adapter = getattr(self.engine, '_regime_adapter', None)
        if adapter is not None:
            adapter._execution_runtime = self

    def install_gateway(self, gateway) -> None:
        """명시 설치. engine 은 safety 패키지를 이 한 길로만 만난다(결정 ⑪)."""
        if self.gateway is not None:
            raise ApplicationBlocked("gateway_already_installed")
        if getattr(gateway, "runtime", None) is not self:
            raise ApplicationBlocked("gateway_runtime_mismatch")
        self.gateway = gateway

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
        # 새 프로세스에서도 동일 수락 가격을 사용한다. 메모리 cache는 권위가 아니다.
        metadata = state.get('quote_price_views', {}).get(symbol)
        quote_price = Decimal(metadata['price']) if metadata else None
        if metadata is None:
            # 새 projection root가 없는 legacy checkpoint 호환. 원시각 없는 과거
            # cache를 만들어내지 않으며, 실제 신규 source proof에는 durable view가 필수다.
            explicit = state.get('latest_explicit_quote', {}).get(symbol)
            if explicit:
                metadata = {'received_at': explicit['received_at'],
                            'source_version': explicit['admission_version'],
                            'market_as_of': explicit['as_of']}
                quote_price = Decimal(explicit['price'])
            cached = self._quote_metadata.get(symbol)
            if cached and symbol in self._quotes and (metadata is None
                    or cached['source_version'] > metadata['source_version']):
                metadata, quote_price = cached, self._quotes[symbol]
        floor = self._quote_time_floor(state, symbol)
        if (metadata and metadata["source_version"] > latest_fill
                and (not valued or (metadata["source_version"] > view["rollover_version"]
                     and aware(datetime.fromisoformat(metadata["received_at"])) >= aware(datetime.fromisoformat(valuation["valuation_boundary"]))))
                and (metadata["market_as_of"] is None or floor is None
                     or aware(datetime.fromisoformat(metadata["market_as_of"])) >= floor)):
            price = quote_price
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

    def market_source_pending(self, state) -> bool:
        """접수 첫 await부터 보호 완료까지 반쯤 게시된 시세로 새 노출을 만들지 않는다."""
        return (any(not task.done() for task in self._protection_tasks)
                or bool(state.get("protection_quote_admissions")) or self._protection_failed)

    def _publish(self, state: dict, version: int) -> None:
        required = {"portfolio", "protection", "risk", "lots", "outbox",
                    "intents", "attempts", "startup_reconciliation"}
        if not required <= state.keys():
            raise ValueError("실행 checkpoint/시작 대사 기준선이 없습니다")
        self._validate_explicit_quotes(state, version)
        validate_price_views(state, version)
        validate_sources(state, version)
        from .risk_sources import validate_risk_sources
        from .intraday_owner import validate_intraday_policy
        from .policy_generations import validate_policy_generations
        validate_policy_generations(state, version)
        validate_risk_sources(state, version)
        intraday = validate_intraday_policy(state, version)
        from .regime_owner import validate_regime_policy
        regime = validate_regime_policy(state, version)
        regime_projection = (self._regime_writer.projection(state, regime)
                             if self._regime_writer is not None else None)
        intraday_horizons = (self._intraday_writer.projection(intraday)
                            if self._intraday_writer is not None else None)
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
        if self._intraday_writer is not None:
            self._intraday_writer.publish(intraday, intraday_horizons)
        if self._regime_writer is not None:
            self._regime_writer.publish(state, regime, regime_projection)
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

    # ───────────────────────── 체결 증거 생산자(P0-2 2단계) ─────────────────────────

    def start_reconciler(self, collect, *, interval: float = RECONCILER_INTERVAL,
                         cycle_timeout: float = RECONCILER_CYCLE_TIMEOUT) -> asyncio.Task:
        """명시 기동. 제품 진입점에는 아직 호출자가 없다(배선은 P0-3).

        `collect(start_date=, end_date=, exchange_scope=)`는 수집기 어댑터다. 이 runtime은
        HTTP/계좌/리미터를 갖지 않으며 조회 실패를 재시도로 덮지 않는다.
        """
        if self._closing:
            raise ApplicationBlocked("reconciler_admission_closed")
        if not callable(collect):
            raise ValueError("invalid_execution_collector")
        for value in (interval, cycle_timeout):
            if type(value) not in (int, float) or not Decimal(str(value)).is_finite() or value <= 0:
                raise ValueError("invalid_reconciler_interval")
        if self._reconciler_task is not None and not self._reconciler_task.done():
            raise ApplicationBlocked("reconciler_already_running")
        self._reconciler_wakeup = asyncio.Event()
        self._reconciler_interval = float(interval)
        self._reconciler_cycle_timeout = float(cycle_timeout)
        self._reconciler_started_at = self._now()
        self._reconciler_last_reason = "started"
        task = asyncio.create_task(self._reconcile_loop(collect))
        self._reconciler_task = task
        self._reconciler_tasks.add(task)

        def completed(done):
            self._reconciler_tasks.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(completed)
        return task

    def notify_execution_change(self) -> None:
        """새 ACK/미해결 시도를 알린다. 잠든 주기를 즉시 깨워 첫 체결 지연을 만들지 않는다."""
        wakeup = self._reconciler_wakeup
        if wakeup is not None:
            wakeup.set()

    async def _reconcile_loop(self, collect) -> None:
        """대상이 있으면 고정 interval, 없으면 조회 0회로 Event에서 잠든다(backoff 없음)."""
        while not self._closing:
            self._reconciler_wakeup.clear()
            try:
                await self.reconcile_once(collect)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 한 주기의 예외가 주기 task를 죽이지 않는다. 사유는 health가 들고 있다.
                logger.exception("[실행] 체결 대사 주기 실패")
            if self._closing:
                break
            try:
                if self._reconciler_target_count:
                    await asyncio.wait_for(self._reconciler_wakeup.wait(), self._reconciler_interval)
                else:
                    await self._reconciler_wakeup.wait()
            except asyncio.TimeoutError:
                pass

    async def reconcile_once(self, collect) -> bool:
        """한 주기. 대상 선정~조회~재접수~health **전체**를 cycle_timeout으로 감싼다(P-1)."""
        self._reconciler_cycle_started_at = self._now()
        try:
            return await asyncio.wait_for(self._reconcile_cycle(collect), self._reconciler_cycle_timeout)
        except asyncio.TimeoutError:
            self._skip_reason("cycle_timeout")
            return False

    def _reconciler_day(self, state):
        """A·B 두 집합의 공통 전제. 아니면 조회도 재처리도 하지 않는다(P-4)."""
        today = self._now().date().isoformat()
        if state.get("risk", {}).get("day") != today:
            return None, "prior_day"
        if self.day_admission_closed:
            return None, "day_admission_closed"
        return today, ""

    @staticmethod
    def _appliable(attempt) -> bool:
        """economics가 이 attempt의 체결을 적용할 수 있는가. A·B가 같은 술어를 쓴다(P1).

        밖이면 조회도 재접수도 하지 않는다 — 관측을 더 모아도 `reduce_economics`가 계속
        ValueError를 내므로 원장 TR 예산만 쓰고 FAILED ingress 행만 쌓인다. 여기서 빠지는
        비종결 상태는 `reconciling`·`blocked_unknown` 둘뿐이고, 제품에서 `lifecycle.reconcile`
        호출자는 이 주기 하나뿐이라(그 경로는 0체결 관측을 저장하지 않는다) `reconciling`은
        만들어지지 않으며 `blocked_unknown`은 order_ref가 없어 이미 대상이 아니었다.
        """
        return (type(attempt) is dict and not attempt.get("evidence_conflict")
                and attempt.get("state") in APPLIABLE_ATTEMPT_STATES)

    @staticmethod
    def _attempt_for(state, order_key: str):
        """관측의 order_key를 소유한 submit attempt. economics와 같은 단일 소유 규칙이다."""
        owners = []
        for row in state.get("attempts", {}).values():
            if type(row) is not dict or row.get("kind") != "submit" or not row.get("order_ref"):
                continue
            try:
                if OrderRef.from_dict(row["order_ref"]).key == order_key:
                    owners.append(row)
            except (TypeError, ValueError):
                continue
        return owners[0] if len(owners) == 1 else None

    @classmethod
    def _reconcile_targets(cls, state, business_day) -> dict:
        """미해결 submit만. 일자가 다른 주문은 economics의 cross-day 거부에 걸린다(F10)."""
        targets = {}
        for attempt_id, row in state.get("attempts", {}).items():
            ref = row.get("order_ref") if type(row) is dict else None
            if (not cls._appliable(row) or row.get("kind") != "submit" or ref is None
                    or ref.get("order_date") != business_day):
                continue
            if (row.get("state") not in TERMINAL_STATES
                    or row.get("observed_quantity") != row.get("applied_quantity")):
                targets[attempt_id] = row
        return targets

    @classmethod
    def _stalled_targets(cls, state, business_day) -> bool:
        """진전을 기다리는 것이 하나라도 있는가(P2-a).

        조회 대상(A)만 세면 재접수만 막힌 상태 — 미적용 inbox 행, 관측만 앞선 attempt —
        가 사유 없이 조용히 굳는다. 여기서는 적용 가능 술어로 거르지 않는다: 영원히
        적용할 수 없는 행일수록 막혔다고 말해야 한다.
        """
        if cls._reconcile_targets(state, business_day):
            return True
        for row in state.get("inbox", {}).values():
            body = row.get("observation")
            if (row.get("status") not in ("APPLIED", "SUPERSEDED")
                    and type(body) is dict and body.get("trading_day") == business_day):
                return True
        for row in state.get("attempts", {}).values():
            ref = row.get("order_ref") if type(row) is dict else None
            if (type(row) is dict and row.get("kind") == "submit" and type(ref) is dict
                    and ref.get("order_date") == business_day
                    and row.get("observed_quantity", 0) > row.get("applied_quantity", 0)):
                return True
        return False

    def _skip_reason(self, reason: str) -> None:
        self._reconciler_skipped[reason] = self._reconciler_skipped.get(reason, 0) + 1
        self._reconciler_last_reason = reason

    def _apply_outcome(self, kind: str, status: str) -> None:
        key = kind + ":" + status
        self._reconciler_outcomes[key] = self._reconciler_outcomes.get(key, 0) + 1

    async def _reconcile_cycle(self, collect) -> bool:
        state = self.owner.state
        business_day, blocked = self._reconciler_day(state)
        if business_day is None:
            self._skip_reason(blocked)
            self._reconciler_target_count = 0
            return False
        targets = self._reconcile_targets(state, business_day)
        self._reconciler_target_count = len(targets)
        chain_blocked: set[str] = set()
        progressed = False
        if targets:
            # 대상이 0이면 조회 자체를 하지 않는다(원장 TR 예산은 미해결 주문에만 쓴다).
            progressed = await self._reconcile_observed(collect, business_day, targets, chain_blocked)
        if await self._reapply_stored(business_day, chain_blocked):
            progressed = True
        # 주기 끝에 사유를 한 번 확정한다(P2-d). 막힌 주기의 blocked_reason 접미사는
        # 그 주기가 실제로 멈춘 이유여야 하고, 지난 주기 사유를 물려받으면 안 된다.
        if progressed:
            self._reconciler_progress_at = self._now()
            self._reconciler_last_reason = "progressed"
        elif not self._stalled_targets(self.owner.state, business_day):
            self._reconciler_last_reason = "idle"
        return progressed

    async def _reconcile_observed(self, collect, business_day, targets, chain_blocked) -> bool:
        """(A) 한 주기에 조회는 1회. 운영과 같은 ALL 범위로 보고 행의 거래소는 파서가 대조한다."""
        try:
            collection = await collect(start_date=business_day, end_date=business_day,
                                       exchange_scope="ALL")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[실행] 체결 조회 실패: day={}", business_day)
            self._skip_reason("collect_failed")
            return False
        if collection.complete is not True:
            # 불완전 수집은 파서를 부르지 않는다. 뒤 페이지 실패를 '없음'으로 읽지 않는다.
            self._skip_reason("incomplete_collection")
            return False
        self._reconciler_complete_at = self._now()
        try:
            pages = evidence_pages(collection)
        except (AttributeError, TypeError, ValueError):
            self._skip_reason("collection_shape")
            return False
        progressed = False
        for attempt_id, attempt in targets.items():
            try:
                if await self._reconcile_attempt(attempt_id, attempt, collection, pages, chain_blocked):
                    progressed = True
            except asyncio.CancelledError:
                raise
            except Exception:
                # 한 시도의 실패가 다른 시도의 관측을 버리지 않는다.
                logger.exception("[실행] 체결 대사 실패: attempt={}", attempt_id)
                self._skip_reason("attempt_failed")
        return progressed

    async def _reconcile_attempt(self, attempt_id, attempt, collection, pages, chain_blocked) -> bool:
        binding = attempt.get("request_binding")
        session = None if binding is None else binding.get("session")
        if type(session) is not str or not session:
            # 세션은 조회 응답에 없다. 주문 binding에 없으면 종결을 지어내지 않는다.
            # 방어층이다 — prepare가 binding 없이 attempt를 만들지 못한다. 그 계약이
            # 깨져도 여기서 파서에 빈 세션을 넘겨 provenance를 통과시키지 않는다.
            self._skip_reason("missing_session")
            return False
        evidence = parse_order_evidence(
            OrderRef.from_dict(attempt["order_ref"]), attempt["symbol"], attempt["side"], pages,
            tr_id=collection.scope.tr_id, session=session, query_kind="all",
            max_pages=RECONCILER_MAX_PAGES, observed_at=collection.completed_at,
            request_started_at=collection.started_at,
            query_scope=parser_scope(collection.scope, session=session), now=self._now())
        if evidence.chain:
            # 자식행이 보이거나 판정할 수 없으면 수량도 종결도 쓰지 않는다(F4·P-3).
            chain_blocked.add(attempt_id)
            self._skip_reason("chain")
            return False
        if not evidence.schema_valid:
            self._skip_reason(evidence.reason if evidence.reason else "schema_invalid")
            return False
        if not self._evidence_changes_attempt(attempt, evidence):
            # 같은 응답을 다시 저장하지 않는다. mutate는 그 자체로 version·게시를 전진시킨다.
            self._skip_reason(evidence.reason if evidence.reason else "no_change")
            return False
        before = attempt["observed_quantity"]
        await self.lifecycle.reconcile(attempt_id, evidence)
        stored = self.owner.state["attempts"][attempt_id]
        return stored["observed_quantity"] > before

    @staticmethod
    def _evidence_changes_attempt(attempt, evidence) -> bool:
        """이 관측이 attempt 행을 바꿀 수 있을 때만 reconcile을 부른다(P-5)."""
        # not_found 는 order_quantity 가 0 이고 주문 수량은 양수라 아래 한 줄이 이미 막는다.
        if evidence.order_quantity != attempt["quantity"]:
            return False
        if evidence.cumulative_quantity > attempt["observed_quantity"]:
            return True
        if evidence.cumulative_amount != Decimal(attempt["observed_amount"]):
            return True
        return (evidence.complete and evidence.supported_finality
                and evidence.state.value in TERMINAL_STATES
                and attempt["state"] not in TERMINAL_STATES)

    async def _reapply_stored(self, business_day, chain_blocked) -> bool:
        """(B) 조회와 무관한 저장 상태 재처리. 조회가 죽어도 저장된 체결은 적용된다."""
        progressed = False
        state = self.owner.state
        for row in state.get("inbox", {}).values():
            if row.get("status") in ("APPLIED", "SUPERSEDED", "NEEDS_RECONCILIATION"):
                continue
            body = row.get("observation")
            if type(body) is not dict or body.get("trading_day") != business_day:
                continue
            try:
                observation = FillObservation(**body)
            except (TypeError, ValueError):
                self._skip_reason("stored_observation_shape")
                continue
            # B1은 chain과 무관하게 진행한다(결정) — 그 행은 chain 판정 이전에 검증된
            # durable 관측이고, 여기서 막으면 설치 차단 사유 19가 영구화된다. 막는 것은
            # economics가 절대 적용할 수 없는 attempt뿐이다(P1).
            if not self._appliable(self._attempt_for(self.owner.state, observation.order_key)):
                self._skip_reason("unappliable_attempt")
                continue
            if await self._reapply(observation):
                progressed = True
        # B1이 inbox를 바꿨을 수 있다. 재구성은 현재 상태에서 다시 읽는다.
        state = self.owner.state
        inbox = state.get("inbox", {})
        for attempt_id, attempt in state.get("attempts", {}).items():
            ref = attempt.get("order_ref") if type(attempt) is dict else None
            if (attempt_id in chain_blocked or type(attempt) is not dict
                    or attempt.get("kind") != "submit" or ref is None
                    or ref.get("order_date") != business_day
                    or attempt.get("observed_quantity", 0) <= attempt.get("applied_quantity", 0)):
                continue
            if not self._appliable(attempt):
                self._skip_reason("unappliable_attempt")
                continue
            try:
                observation = self._stored_observation(attempt)
            except (KeyError, TypeError, ValueError):
                self._skip_reason("stored_attempt_shape")
                continue
            if observation.observation_id in inbox:
                continue  # 같은 행은 B1이 소유한다. 두 곳에서 접수하지 않는다.
            if await self._reapply(observation):
                progressed = True
        return progressed

    @staticmethod
    def _stored_observation(attempt) -> FillObservation:
        """저장 상태만으로 관측을 되살린다 — 파서 경로와 **같은 함수**라 observation_id가 같다."""
        binding = attempt.get("request_binding")
        metadata = None if binding is None else binding.get("fill_metadata")
        evidence = OrderEvidence(OrderRef.from_dict(attempt["order_ref"]), attempt["symbol"],
                                 attempt["side"], attempt["quantity"], attempt["observed_quantity"],
                                 Decimal(attempt["observed_amount"]))
        return observation_from_evidence(evidence, trading_day=attempt["order_ref"]["order_date"],
                                         metadata=metadata)

    async def _reapply(self, observation: FillObservation) -> bool:
        try:
            receipt = await self.engine.apply_execution_observation(observation)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._apply_outcome(type(exc).__name__, "raised")
            self._skip_reason("apply_failed")  # 예외 종류는 apply_outcomes가 들고 있다.
            return False
        self._apply_outcome(type(receipt).__name__, str(getattr(receipt, "status", "")))
        if isinstance(receipt, InboxReceipt):
            if not receipt.application_complete:
                self._reconciler_parked += 1
            return receipt.application_complete
        return receipt.status in ("APPLIED", "ALREADY_APPLIED")

    def reconciler_blocked_reason(self) -> str | None:
        """기다리는 것이 있는데 마지막 상태 전진 이후 k주기를 넘겼는가(P-2·P2-a).

        task.done()으로 재지 않는다 — 죽은 주기와 살아 있지만 굶은 주기를 같은 문장으로
        잰다. 조회 대상뿐 아니라 미적용 inbox 행과 관측만 앞선 attempt도 같은 경과
        기준으로 잰다. 이 단계에는 소비자가 없다(BUY 게이트는 P0-3).
        """
        state = self.owner.state
        business_day, _ = self._reconciler_day(state)
        if business_day is None:
            return None  # 일자 게이트는 그 자체가 별도 사유다. 여기서 두 번 세지 않는다.
        if not self._stalled_targets(state, business_day):
            return None
        if self._reconciler_started_at is None:
            return "reconciler_not_started"
        last = self._reconciler_progress_at
        if last is None:
            last = self._reconciler_started_at
        if (self._now() - last).total_seconds() <= RECONCILER_STALL_CYCLES * self._reconciler_interval:
            return None
        return "reconciler_no_progress:" + self._reconciler_last_reason

    @staticmethod
    def _stamp(value):
        return None if value is None else value.isoformat()

    async def apply_observation(self, observation: FillObservation, *, ingress_context=None):
        if not isinstance(observation, FillObservation):
            raise TypeError("누적체결 관측이 필요합니다")
        if self._closing:
            # 종료 중에는 새 적용을 시작하지 않는다. 진행 중 적용은 drain이 기다린다(P-7).
            raise ApplicationBlocked("execution_application_closing")
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

    async def observe_market(self, event, *, intent_id=None, market_data=None):
        """원관측을 보호·진입 가격의 같은 완료 commit으로 게시한다. 전략 송신은 별도다."""
        observation = observation_from_event(event)
        return await self.quote(observation.symbol, observation.price,
            market_data=market_data, intent_id=intent_id, market_as_of=observation.market_as_of,
            source=observation.source, source_event_id=observation.source_event_id,
            entry_observation=observation)

    async def quote(self, symbol: str, price: Decimal, *, market_data=None,
                    intent_id: str | None = None, market_as_of: datetime | None = None,
                    source: str | None = None, source_event_id: str | None = None,
                    entry_observation=None):
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
        if entry_observation is not None:
            entry_observation = canonical_observation(entry_observation, symbol=symbol, price=price,
                as_of=observed, source=source, event_id=source_event_id, now=now)
            market_data = observation_market_data(entry_observation, market_data)
        else:
            market_data = deepcopy(market_data)
        command_id = "quote:" + uuid4().hex
        request = {"symbol": symbol, "price": str(price), "market_data": market_data,
                   "intent_id": intent_id, "observed_at": now.isoformat(),
                   "market_as_of": observed.isoformat() if observed is not None else None,
                   "source": source, "source_event_id": source_event_id}
        if entry_observation is not None:
            request['entry_observation'] = entry_observation.to_dict()
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
            state.setdefault('quote_price_views', {})[symbol] = quote_price_view(request, self.owner.version + 1)
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
            if entry_observation is not None:
                complete_source(state, command_id=command_id, request=request,
                    admission_version=admission['source_version'],
                    completed_version=self.owner.version + 1, decision=decision)
            elif symbol in state.get('market_sources', {}):
                # 보호-only 입력은 과거 진입 proof를 새 시세로 재사용할 권한이 아니다.
                state['market_sources'][symbol]['invalidated_at_version'] = self.owner.version + 1
            # 보호 결과/재생 입력과 같은 commit에서만 미해결 접수를 제거한다.
            del state["protection_quote_admissions"][command_id]
            return state

        async def apply_quote():
            nonlocal admission_rejected
            # 두 owner transaction 사이에도 가격 입력 순서가 뒤집히지 않게 한다.
            async with self._quote_lock:
                if entry_observation is not None:
                    try:
                        self.owner._require_ready()
                        current = self.owner.state
                        self._require_quote_freshness(current, symbol, observed, market_quote)
                        repeated, saved_decision = completed_duplicate(current, request)
                    except (ValueError, ApplicationBlocked):
                        admission_rejected = True  # 아직 새 입력을 저장/게시한 적이 없다.
                        raise
                    if repeated:
                        return saved_decision
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
                if entry_observation is not None:
                    proof = self.owner.state['market_sources'][symbol]
                    if proof['admission_id'] != command_id or proof['request_digest'] != request_digest:
                        raise ApplicationBlocked('market_source_completion_conflict')
                    # commit-ID 재조회에서는 reducer closure가 실행되지 않을 수 있다.
                    return None if proof['decision'] is None else tuple(proof['decision'])
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

    @contextmanager
    def command_scope(self):
        """첫 await 전 접수하고 coroutine 반환까지 추적한다. caller task 전체를 기다리지 않는다."""
        if self._closing:
            raise ApplicationBlocked('command_admission_closed')
        self._command_tracking_started = True
        token = asyncio.get_running_loop().create_future()
        self._command_scopes[token] = asyncio.current_task()
        try:
            yield token
        finally:
            self._command_scopes.pop(token, None)
            if not token.done():
                token.set_result(None)

    def _command_result_completed(self, task):
        self._command_result_tasks.discard(task)
        if task.cancelled() or task.exception() is not None or task.result() is not True:
            self._command_results_failed = True
            self.owner._block()

    def start_command_result(self, token, operation):
        """종료 중에도 이미 접수한 같은 호출의 결과만 새 strong task로 등록한다."""
        if (token not in self._command_scopes
                or self._command_scopes[token] is not asyncio.current_task()):
            raise ApplicationBlocked('command_scope_required')
        task = asyncio.create_task(operation())
        self._command_result_tasks.add(task)
        task.add_done_callback(self._command_result_completed)
        return task

    def _start_command_finalizer(self, parent_token, operation):
        """Transfer an admitted cleanup to its own task-owned scope, never borrow a token."""
        if (parent_token not in self._command_scopes
                or self._command_scopes[parent_token] is not asyncio.current_task()):
            raise ApplicationBlocked('command_scope_required')
        if not callable(operation):
            raise TypeError('command_finalizer_required')
        child_token = asyncio.get_running_loop().create_future()
        def release(_=None):
            self._command_scopes.pop(child_token, None)
            if not child_token.done(): child_token.set_result(None)
        async def execute():
            try:
                return await operation(child_token)
            finally:
                release()
        task = self.start_command_result(parent_token, execute)
        self._command_scopes[child_token] = task
        # Cancellation before execute's first instruction does not run its finally.
        task.add_done_callback(release)
        return task

    async def shutdown(self) -> None:
        """admission을 닫고 명령 중 뒤늦게 생긴 결과 저장까지 fixed-point drain한다.

        drain 집합은 runtime 소유 task(수집 task 포함)뿐이다 — engine의 ingress/apply task는
        engine._shutdown의 몫이고, runtime이 engine의 사설 속성을 들여다보지 않는다. 수집
        task는 자연 종료할 때 자기가 시작한 apply를 스스로 기다리므로 그것으로 충분하다.
        제품 순서는 engine._shutdown(수락 차단 → 자기 task drain) → runtime.shutdown이며
        그 배선은 P0-3에서 고정한다.
        """
        self._closing = True
        # 주기 task는 cancel이 아니라 _closing으로 자연 종료한다. 잠들어 있으면 깨워 준다(P-7).
        self.notify_execution_change()
        if asyncio.current_task() in self._command_scopes.values():
            raise ApplicationBlocked('command_shutdown_self_wait')
        while True:
            # 이미 완료됐지만 callback 실행 전인 실패도 종료 성공으로 숨기지 않는다.
            for task in tuple(self._command_result_tasks):
                if task.done():
                    self._command_result_completed(task)
            pending = {task for task in (*self._command_scopes, *self._command_result_tasks,
                                        *self._day_tasks, *self._protection_tasks,
                                        *self._reconciler_tasks) if not task.done()}
            if not pending:
                break
            # 종료 caller 취소가 명령/저장 task 취소로 전파되지 않는다.
            await asyncio.gather(*(asyncio.shield(task) for task in pending), return_exceptions=True)
        if self._command_results_failed:
            raise ApplicationBlocked('command_result_drain_failed')
        # scope의 예외 유무만으로는 내부에서 NOT_SENT로 처리한 claim 실패를 못 본다.
        # 반대로 scope finally에서 검사하면 다른 정상 commit의 일시 _block을 오인한다.
        # 새 명령 경계를 사용한 runtime만, 전 작업 drain 뒤 최종 저장/게시를 판정한다.
        if self._command_tracking_started and (
                not self.owner.healthy or self.owner.version != self.owner.published_version
                or self.engine._execution_version != self.owner.version):
            raise ApplicationBlocked('command_state_drain_failed')

    def health(self) -> dict:
        state = self.owner.state
        return {
            "store_healthy": self.owner.healthy,
            "execution_version": self.owner.version,
            "published_version": self.owner.published_version,
            "publication_recovery_required": self.owner.publication_recovery_required,
            "protection_updates_failed": self._protection_failed,
            "protection_updates_pending": len(self._protection_tasks),
            "command_operations_pending": len(self._command_scopes),
            "command_results_pending": len(self._command_result_tasks),
            "command_results_failed": self._command_results_failed,
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
            # 생산자의 관측 창. 이름 충돌(day_admission_closed)을 피해 한 단계 아래에 둔다.
            "reconciler": {
                "reconciler_running": (self._reconciler_task is not None
                                       and not self._reconciler_task.done()),
                "last_cycle_started_at": self._stamp(self._reconciler_cycle_started_at),
                "last_complete_at": self._stamp(self._reconciler_complete_at),
                "last_progress_at": self._stamp(self._reconciler_progress_at),
                "last_reason": self._reconciler_last_reason,
                "targets": self._reconciler_target_count,
                "skipped": dict(self._reconciler_skipped),
                "apply_outcomes": dict(self._reconciler_outcomes),
                "parked": self._reconciler_parked,
                "day_admission_closed": self.day_admission_closed,
            },
        }
