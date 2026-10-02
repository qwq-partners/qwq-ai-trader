"""장중 가격 비교용 수동 주입 관측 접점. 주문·파일·네트워크·정책 재판정 없음.

기본 런타임에는 버퍼를 설치하지 않는다. 명시한 평가 세대와 용량으로 생성한
버퍼만 기존 흐름을 복사한다. export는 메모리 복사이며 영속 저장이나 수집기 기동이 아니다.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from typing import Any, Mapping
from uuid import uuid4

from loguru import logger

from .entry_price_shadow import build_report, _timestamp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scalar(value: Any) -> Any:
    """원 객체/자격증명을 전달하지 않고 허용 필드의 단순 값만 복사한다."""
    if value is None or type(value) in (str, int, bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value, datetime):
        return value.isoformat()
    return None


class EntryObservationBuffer:
    """한 이벤트 루프용 유한 메모리 버퍼. 가득 차면 새 기록을 버리고 결손을 보존한다."""

    def __init__(self, *, evaluation_epoch: str, capacity: int, scan_scope="all", scan_admission_ref=None,
                 selection_basis_settings=None, entry_gate_trace_settings=None):
        if not isinstance(evaluation_epoch, str) or not evaluation_epoch.strip():
            raise ValueError("evaluation_epoch 필요")
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("양의 정수 capacity 필요")
        if scan_scope not in ("all", "first"):
            raise ValueError("지원하지 않는 scan_scope")
        if ((scan_scope == "first" and (not isinstance(scan_admission_ref, str)
                or not scan_admission_ref.strip() or len(scan_admission_ref) > 200))
                or (scan_scope == "all" and scan_admission_ref is not None)):
            raise ValueError("첫 스캔 범위에는 명시한 사전 규약 참조 필요")
        self.scan_scope = scan_scope
        self.scan_admission_ref = scan_admission_ref
        from .selection_basis import validate_settings
        self.selection_basis_settings = (validate_settings(selection_basis_settings)
                                         if selection_basis_settings is not None else None)
        from .entry_gate_trace import validate_settings as validate_gate_settings
        self.entry_gate_trace_settings = (validate_gate_settings(entry_gate_trace_settings)
                                         if entry_gate_trace_settings is not None else None)
        if self.entry_gate_trace_settings is not None and scan_scope != 'first':
            raise ValueError('entry gate trace requires first scan scope')
        self._entry_gate_trace_started = False
        self._first_scan_id = None
        self._first_scan_recorded = False
        self._cohort_candidates: set[str] = set()
        self._cohort_signals: set[str] = set()
        self.evaluation_epoch = evaluation_epoch
        self.capacity = capacity
        self._records: list[dict] = []
        self._dropped = 0
        self._incomplete_reasons: set[str] = set()
        self._journal_sink = None
        self._capture_closed = False

    @property
    def selection_capture_enabled(self):
        return (self.selection_basis_settings is not None and not self._capture_closed
                and (self.scan_scope != 'first' or self._first_scan_id is None))

    @property
    def selection_sources_capture_enabled(self):
        return (self.selection_capture_enabled
                and self.selection_basis_settings['version'] == 'selection-basis-v2')

    def begin_scan(self):
        """복사 전에 첫 시도를 예약한다. 빈 결과/복사 실패 뒤 재선정하지 않는다."""
        if self._capture_closed or (self.scan_scope == "first" and self._first_scan_id is not None):
            return None
        scan_id = uuid4().hex
        if self.scan_scope == "first":
            self._first_scan_id = scan_id
        return scan_id

    def accepts_order_signal(self, signal_id):
        return self.scan_scope == "all" or signal_id in self._cohort_signals

    def mark_incomplete(self, reason: str):
        # 알려진 수신 공백은 버린 레코드 개수와 구분한다. 메모리 사용은 제한한다.
        if not self._capture_closed and len(self._incomplete_reasons) < 16:
            self._incomplete_reasons.add(reason)

    def publish(self, record: dict) -> bool:
        if self._capture_closed:
            return False
        kind = record.get("kind")
        if self.scan_scope == "first":
            if kind == "scan":
                if record.get("scan_id") != self._first_scan_id or self._first_scan_recorded:
                    return False
                record = {**record, "population_scope": "first_returned_scan_candidates",
                          "scan_admission_ref": self.scan_admission_ref}
            elif kind in ("rest_quote", "signal", "emit_result", "selection_basis", "entry_gate_trace"):
                if record.get("candidate_id") not in self._cohort_candidates:
                    return False
            elif kind == "order_ready" and not self.accepts_order_signal(record.get("signal_id")):
                return False
        if len(self._records) >= self.capacity:
            self._dropped += 1
            return False
        copied = deepcopy(record)
        copied["sequence"] = len(self._records) + 1
        self._records.append(copied)
        if self.scan_scope == "first":
            if kind == "scan":
                self._cohort_candidates = {c["candidate_id"] for c in copied["candidates"]}
                self._first_scan_recorded = True
            elif kind == "signal":
                self._cohort_signals.add(copied["signal_id"])
        if self._journal_sink is not None:
            try:
                self._journal_sink.offer(copied)
            except Exception:
                self.mark_incomplete("journal_offer_failed")
        return True

    def capture_status(self) -> dict:
        reasons = set(self._incomplete_reasons)
        if self.scan_scope == "first" and not self._first_scan_recorded:
            reasons.add("first_scan_not_recorded")
        if self._journal_sink is not None and self._journal_sink.failure_reason:
            reasons.add(self._journal_sink.failure_reason)
        return {"schema_version": 1, "evaluation_epoch": self.evaluation_epoch,
                "complete": self._dropped == 0 and not reasons,
                "incomplete_reasons": sorted(reasons), "dropped_records": self._dropped}

    def export(self) -> dict:
        return {**self.capture_status(), "records": deepcopy(self._records)}


def _capture_failed(observer):
    # 데이터 생성/저장 예외도 유실이다. 운영 흐름을 멈추지 않고 완전성은 낮춘다.
    if isinstance(observer, EntryObservationBuffer) and not observer._capture_closed:
        observer._dropped += 1
    logger.warning("[가격관측] 복사 실패 — 매매 흐름은 계속 진행")


def _publish(observer, record: dict) -> bool:
    if observer is None:
        return False
    try:
        return observer.publish(record) is True
    except Exception:
        _capture_failed(observer)
        return False


def capture_scan(observer, stocks, session: str) -> str | None:
    if observer is None:
        return None
    try:
        scan_id = observer.begin_scan() if isinstance(observer, EntryObservationBuffer) else uuid4().hex
        if scan_id is None:
            return None
        fields = ("symbol", "price", "score", "screened_at", "change_pct", "volume", "atr_pct")
        candidates = [{**{k: _scalar(getattr(s, k, None)) for k in fields},
                       "candidate_id": f"{scan_id}:{s.symbol}"} for s in stocks]
        record = {"kind": "scan", "scan_id": scan_id, "observed_at": _now(),
                  "session": session, "route_origin": "live_screening",
                  "population_scope": "returned_screen_candidates", "candidates": candidates}
        if isinstance(observer, EntryObservationBuffer) and observer.entry_gate_trace_settings is not None:
            from .entry_gate_trace import scan_fields
            record.update(scan_fields(observer.entry_gate_trace_settings))
        selection = observer.selection_basis_settings if isinstance(observer, EntryObservationBuffer) else None
        if selection is not None:
            record.update(selection_basis_expected=True, selection_basis_max_candidates=selection['max_candidates'],
                          selection_basis_max_terms=selection['max_source_terms'])
            if selection['version'] == 'selection-basis-v2':
                record.update(selection_sources_expected=True, selection_sources_status='unavailable')
                try:
                    from .selection_source_status import validate_snapshot, snapshot_fields
                    snapshot = getattr(stocks, 'selection_sources', None)
                    if snapshot is not None:
                        snapshot = validate_snapshot(snapshot, observed_at=record['observed_at'])
                        record.update(snapshot_fields(snapshot), selection_sources_status='observed')
                except Exception:
                    observer.mark_incomplete('selection_sources_invalid')
        if not _publish(observer, record):
            return None
        if selection is not None:
            _capture_selection_basis(observer, scan_id, stocks, selection, record['observed_at'])
        return scan_id
    except Exception:
        _capture_failed(observer)
        return None


def _capture_selection_basis(observer, scan_id, stocks, settings, scan_at):
    from .selection_basis import validate_basis
    if len(stocks) > settings['max_candidates']:
        observer.mark_incomplete('selection_candidate_limit')
        return
    for rank, stock in enumerate(stocks, 1):
        try:
            record = {'kind': 'selection_basis', 'candidate_id': f'{scan_id}:{stock.symbol}',
                      'symbol': stock.symbol, 'observed_at': _now(), 'basis_status': 'unavailable'}
            basis = getattr(stock, 'selection_basis', None)
            if basis is not None:
                record.update(validate_basis(basis, score=stock.score, rank=rank,
                    observed_at=scan_at, max_terms=settings['max_source_terms']))
                record['basis_status'] = 'observed'
            if not _publish(observer, record):
                observer.mark_incomplete('selection_basis_publish_failed')
        except Exception:
            _capture_failed(observer)


def capture_rest_quote(observer, scan_id, symbol, quote, *, requested_at=None):
    if observer is None or scan_id is None:
        return
    try:
        fields = ("price", "open", "high", "low", "volume", "change_pct")
        _publish(observer, {"kind": "rest_quote", "candidate_id": f"{scan_id}:{symbol}",
                            "observed_at": _now(), "requested_at": requested_at,
                            "source": "KIS_FHKST01010100", "source_as_of": None,
                            "quote": {k: _scalar(quote.get(k)) for k in fields}})
    except Exception:
        _capture_failed(observer)


def capture_signal(observer, scan_id, event):
    if observer is None or scan_id is None:
        return
    try:
        _publish(observer, {"kind": "signal", "candidate_id": f"{scan_id}:{event.symbol}",
                            "signal_id": event.id, "observed_at": _now(),
                            "event_timestamp": _scalar(event.timestamp),
                            "strategy": event.strategy.value, "price": _scalar(event.price),
                            "signal_target_price": _scalar(event.target_price),
                            "signal_stop_price": _scalar(event.stop_price)})
    except Exception:
        _capture_failed(observer)


def capture_emit_result(observer, scan_id, symbol, signal_id, emitted: bool):
    if observer is not None and scan_id is not None:
        _publish(observer, {"kind": "emit_result", "candidate_id": f"{scan_id}:{symbol}",
                            "signal_id": signal_id, "observed_at": _now(), "emitted": emitted})


async def emit_with_observation(engine, event, observer, scan_id):
    """관측 실패는 격리하되 기존 emit 반환/예외와 이벤트 객체는 그대로 보존한다."""
    capture_signal(observer, scan_id, event)
    try:
        result = await engine.emit(event)
    except Exception:
        capture_emit_result(observer, scan_id, event.symbol, event.id, False)
        raise
    capture_emit_result(observer, scan_id, event.symbol, event.id, True)
    return result


def capture_quote(observer, event):
    if observer is None:
        return
    try:
        fields = ("tr_id", "exchange_time", "hour_class_code", "received_at", "message_count", "source_as_of",
                  "connection_id", "generation")
        _publish(observer, {"kind": "ws_quote", "quote_id": event.id,
                            "symbol": event.symbol, "observed_at": _now(),
                            "ask": _scalar(event.ask_price), "bid": _scalar(event.bid_price),
                            "ask_size": event.ask_size, "bid_size": event.bid_size,
                            "provenance": {k: _scalar(event.metadata.get(k)) for k in fields}})
    except Exception:
        _capture_failed(observer)


def freeze_pre_pending_capital(owner, risk_manager, event, order):
    """pending lock 안에서 내부 장부만 복사. publish/정책 재판정/외부 조회 없음.

    G3 통과와 lock 획득 사이 상태가 바뀔 수 있어 게이트가 사용한 입력이나
    증권사 매수 가능액이 아니다. 실패는 None이며 주문/예약을 변경하지 않는다.
    """
    try:
        observer = getattr(owner, "_entry_price_observer", None)
        if observer is None or event.source != "live_screening" or order.side.value != "buy":
            return None
        if isinstance(observer, EntryObservationBuffer) and (
                observer._capture_closed or (observer.scan_scope == "first"
                    and event.id not in observer._cohort_signals)):
            return None

        def number(value):
            if isinstance(value, bool) or not isinstance(value, (Decimal, str, int, float)):
                raise ValueError("capital scalar required")
            result = Decimal(str(value))
            if not result.is_finite():
                raise ValueError("finite capital required")
            return result

        engine = risk_manager.engine
        portfolio = engine.portfolio
        strategy = order.strategy or event.strategy.value
        equity = number(portfolio.total_equity)
        cash = number(portfolio.cash)
        after_reserve = number(engine.get_available_cash())
        pending = number(risk_manager._reserved_cash)
        core = (Decimal("0") if strategy == "core_holding"
                else number(risk_manager._get_core_reserve()))
        allocation = number(risk_manager.config.strategy_allocation.get(strategy, 0))
        held = number(portfolio.get_strategy_allocation(strategy))
        strategy_pending = number(risk_manager._pending_strategy_notional(strategy))
        cap = equity * Decimal(str(float(allocation) / 100)) if allocation > 0 else None
        values = {
            "equity": equity, "cash": cash, "cash_after_reserve": after_reserve,
            "prior_pending_cash_reserved": pending, "core_cash_reserved": core,
            "cash_capacity_before": after_reserve - pending - core,
            "strategy_allocation_pct": allocation, "strategy_held_notional": held,
            "strategy_pending_reserved": strategy_pending, "strategy_cap_notional": cap,
            "strategy_remaining_notional": None if cap is None else cap - held - strategy_pending,
            "reference_price": number(order.price),
        }
        return {"version": "pre-pending-capacity-v1", "basis": "engine_memory_not_broker_balance",
                "stage": "before_current_pending_registration", "captured_at": _now(),
                "signal_id": event.id, "order_id": order.id, "symbol": order.symbol,
                "strategy": strategy, "requested_quantity": order.quantity,
                "current_order_reservation_included": False,
                **{key: None if value is None else str(value) for key, value in values.items()}}
    except Exception:
        # lock 안에서 observer/logger 콜백을 호출하지 않는다.
        return None


def capture_order_ready(owner, event, order, *, capital_snapshot=None):
    """최종 pending 등록 뒤 생성된 BUY 주문만 복사. 접수/체결 승인으로 해석하지 않는다."""
    observer = getattr(owner, "_entry_price_observer", None)
    if observer is None:
        return
    try:
        if isinstance(observer, EntryObservationBuffer) and not observer.accepts_order_signal(event.id):
            return
        if event.source != "live_screening" or order.side.value != "buy":
            return
        strategy = order.strategy or event.strategy.value
        params = owner._strategy_exit_params.get(strategy, {})
        is_core = bool(params.get("is_core", False)) or strategy == "core_holding"
        kwargs = dict(dynamic_stop_pct=None, fixed_stop_pct=params.get("stop_loss_pct"), is_core=is_core)
        risk_stop = owner.exit_manager.resolve_stop(**kwargs, apply_crash_cap=False)
        effective_stop = owner.exit_manager.resolve_stop(**kwargs)
        snapshot = {"risk_stop_pct": str(risk_stop.stop_pct),
                    "effective_stop_pct": str(effective_stop.stop_pct),
                    "base_stop_source": effective_stop.source,
                    "effective_stop_source": "crash_cap" if effective_stop.crash_capped else effective_stop.source,
                    "crash_cap_applied": effective_stop.crash_capped,
                    "is_core": is_core, "strategy": strategy}
        # 초기 손절 해석의 식별자일 뿐 전체 청산 정책/미래 fill 손절의 해시가 아니다.
        ref = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
        capital_ref = (hashlib.sha256(json.dumps(capital_snapshot, sort_keys=True).encode()).hexdigest()
                       if capital_snapshot is not None else None)
        _publish(observer, {"kind": "order_ready", "signal_id": event.id, "symbol": order.symbol,
                            "order_id": order.id, "capital_snapshot": capital_snapshot,
                            "capital_snapshot_ref": capital_ref,
                            "capital_snapshot_status": "recorded" if capital_snapshot is not None else "unavailable",
                            "observed_at": _now(), "requested_quantity": order.quantity,
                            "order_reference_price": _scalar(order.price), "order_type": order.order_type.value,
                            **snapshot, "stop_snapshot_ref": ref, "capital_budget": None,
                            "stop_basis": "net_pnl", "stop_resolved_at_stage": "order_ready",
                            "fill_applied": False,
                            "transport_status": "not_observed"})
    except Exception:
        _capture_failed(observer)


def observation_population(records):
    """첫 스캔 선언을 손익 계산과 분리해 검증한다. 여러 스캔을 사후 고르지 않는다."""
    scans = [r for r in records if r.get("kind") == "scan"]
    scopes = {r.get("population_scope", "returned_screen_candidates") for r in scans}
    known = {"returned_screen_candidates", "first_returned_scan_candidates"}
    if scopes - known or len(scopes) > 1:
        raise ValueError("관측 모집단 범위 혼합/미지원")
    if "first_returned_scan_candidates" in scopes:
        if len(scans) != 1:
            raise ValueError("첫 스캔 모집단에는 정확히 한 scan 필요")
        ref = scans[0].get("scan_admission_ref")
        if not isinstance(ref, str) or not ref.strip() or len(ref) > 200:
            raise ValueError("첫 스캔 사전 규약 참조 필요")
        return {"population_scope": "first_returned_scan_candidates", "scan_admission_ref": ref}
    if any("scan_admission_ref" in r for r in scans):
        raise ValueError("전체 스캔과 첫 스캔 선언 혼합")
    return {"population_scope": "returned_screen_candidates"}


def prepare_input(context: Mapping, observations: Mapping, evaluation_inputs: list) -> dict:
    """후보 분모와 명시적 평가 입력을 ID로만 결합. 관측으로 승인/시각/수량을 추정하지 않는다."""
    if context.get("data_basis") == "received_snapshot":
        from .received_entry_input import prepare_received_input
        return prepare_received_input(context, observations, evaluation_inputs)
    payload = deepcopy(dict(context))
    payload["opportunities"] = []
    build_report(payload)  # 전역 비용·시점·정책 누락을 임의 기본값으로 채우지 않는다.
    if observations.get("schema_version") != 1 or observations.get("evaluation_epoch") != payload["evaluation_epoch"]:
        raise ValueError("관측 schema/평가 세대 불일치")
    records = observations.get("records")
    if not isinstance(records, list) or not isinstance(evaluation_inputs, list):
        raise ValueError("records/evaluation_inputs 배열 필요")
    population = observation_population(records)
    candidates = {}
    observed_at = {}
    report_at = _timestamp(payload["as_of"], "보고 시각")
    for record in records:
        if record.get("kind") != "scan":
            continue
        scan_at = _timestamp(record.get("observed_at"), "후보 관측 시각")
        if scan_at > report_at:
            raise ValueError("후보 관측 시각이 보고 시각보다 늦음")
        for item in record["candidates"]:
            cid = item["candidate_id"]
            if cid in candidates or cid != f"{record['scan_id']}:{item['symbol']}":
                raise ValueError("후보 연결 ID 중복/불일치")
            candidates[cid] = {"opportunity_id": cid, "symbol": item["symbol"],
                               "route_origin": "live_screening", "strategy": None,
                               "evaluation_epoch": payload["evaluation_epoch"],
                               "decision_at": None, "baseline_eligible": None}
            observed_at[cid] = scan_at
    seen = set()
    for item in evaluation_inputs:
        cid = item.get("opportunity_id")
        if cid not in candidates or cid in seen:
            raise ValueError("평가 입력의 미연결/중복 후보 ID")
        seen.add(cid)
        expected = candidates[cid]
        for key in ("symbol", "evaluation_epoch", "route_origin"):
            if key in item and item[key] != expected[key]:
                raise ValueError(f"평가 입력 {key} 불일치")
        if item.get("decision_at") is not None:
            decision = _timestamp(item["decision_at"], "의사결정 시각")
            if not observed_at[cid] <= decision <= report_at:
                raise ValueError("후보 관측/의사결정/보고 시각 순서 위반")
        expected.update(deepcopy(item))
    complete = observations.get("complete") is True and observations.get("dropped_records") == 0
    payload["opportunities"] = list(candidates.values())
    if not complete:
        # 빠진 후보가 있는 수익 집계를 완전한 모집단 결과로 보이지 않게 한다.
        for row in payload["opportunities"]:
            row["baseline_eligible"] = None
    report = build_report(payload)
    return {"payload": payload, "capture_complete": complete,
            **population,
            "ready_opportunities": report["counts"]["allow"] + report["counts"]["cash"] + report["counts"]["baseline_cash"],
            "missing_evaluation_inputs": len(candidates) - len(seen),
            "report": report,
            "limitations": ["반환 후보 집합만 보존하며 전체 시장/스크리너 내부 제외 집합이 아니다.",
                            "REST 고가/WS HHMMSS로 판단 시점 고점이나 거래일을 추정하지 않는다.",
                            "신호/emit 성공은 현행 정책 승인·주문 접수·체결이 아니다.",
                            "evaluation_inputs는 별도 확인한 명시 자료이며 자동 정책 재생 결과가 아니다."]}
