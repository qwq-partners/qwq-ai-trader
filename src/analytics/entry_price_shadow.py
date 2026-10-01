"""장중 갭 후보의 가격 조건 비교 — 명시 입력만 사용하는 주문 없는 계산기.

실시간 엔진에 연결하지 않는다. 당일 고점/손절 스냅샷은 판단 당시 고정된 입력이며,
청산 결과는 같은 진입에 대한 공통 정책의 가격 대리치만 받는다. 계좌 재생·성과 승격
기능은 없다. 알려진 미진입(현금)과 자료 부족(unknown)을 반드시 구분한다.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import Decimal, DecimalException, localcontext
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from ..utils.fee_calculator import FeeCalculator, FeeConfig
from ..utils.sizing import planned_risk

KST = ZoneInfo("Asia/Seoul")
MIN_NET_RR = Decimal("1.5")  # 기존 장중 경로의 기준 후보값. 최적화/승격 값이 아니다.


def _number(value: Any, name: str, *, zero: bool = False) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"INVALID:{name}")
    try:
        number = Decimal(str(value))
    except (DecimalException, ValueError):
        raise ValueError(f"INVALID:{name}") from None
    if not number.is_finite() or number < 0 or (number == 0 and not zero):
        raise ValueError(f"INVALID:{name}")
    return number


def _integer(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"INVALID:{name}")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"INVALID:{name}")
    return value.strip()


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"MISSING:{name}")
    return value


def _timestamp(value: Any, name: str) -> datetime:
    try:
        result = datetime.fromisoformat(_text(value, name).replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ValueError(f"INVALID_TIME:{name}") from None


def _known_at(snapshot: Mapping[str, Any], decision: datetime, name: str) -> datetime:
    as_of = _timestamp(snapshot.get("as_of"), f"{name}.as_of")
    available = _timestamp(snapshot.get("available_at"), f"{name}.available_at")
    if not as_of <= available <= decision:
        raise ValueError(f"NOT_KNOWN_AT_DECISION:{name}")
    return as_of


@dataclass(frozen=True)
class PriceGatePolicy:
    """자료 신선도와 슬리피지는 입력에서 고정한다. 임계값 탐색은 제공하지 않는다."""

    max_quote_age_seconds: Decimal
    max_decision_delay_seconds: Decimal
    entry_slippage_bps: Decimal
    exit_slippage_bps: Decimal

    @classmethod
    def from_dict(cls, value: Any) -> PriceGatePolicy:
        source = _mapping(value, "policy")
        fields = {name: _number(source.get(name), f"policy.{name}", zero=True)
                  for name in cls.__dataclass_fields__}
        if any(fields[name] >= 10000 for name in ("entry_slippage_bps", "exit_slippage_bps")):
            raise ValueError("INVALID:policy.slippage_bps")
        return cls(**fields)


def _fees(value: Any) -> FeeCalculator:
    source = _mapping(value, "fees")
    rates = {name: _number(source.get(name), f"fees.{name}", zero=True)
             for name in FeeConfig.__dataclass_fields__}
    if rates["buy_commission_rate"] >= 1 or rates["sell_commission_rate"] + rates["sell_tax_rate"] >= 1:
        raise ValueError("INVALID:fees.rates")
    return FeeCalculator(FeeConfig(**rates))


def _amounts(ask: Decimal, target: Decimal, qty: int, stop_pct: Decimal,
             fees: FeeCalculator, policy: PriceGatePolicy) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    entry = ask * (1 + policy.entry_slippage_bps / 10000)
    exit_target = target * (1 - policy.exit_slippage_bps / 10000)
    gain, _ = fees.calculate_net_pnl(entry, exit_target, qty)
    loss = planned_risk(entry, qty, stop_pct, fees)  # net SL에 왕복 비용을 다시 더하지 않는다.
    capital = entry * qty + fees.calculate_buy_fee(entry * qty)
    return entry, gain, loss, capital


def _price_cap(target: Decimal, qty: int, stop_pct: Decimal, budget: Decimal,
               fees: FeeCalculator, policy: PriceGatePolicy, capital_limits=None) -> int:
    """정수 원 단위 가격 대리치 상한. 거래소 호가단위/주문 가능 가격을 보장하지 않는다."""
    low, high = 0, int(min(target, budget / qty))
    while low < high:
        mid = (low + high + 1) // 2
        entry, gain, loss, capital = _amounts(Decimal(mid), target, qty, stop_pct, fees, policy)
        if (gain >= MIN_NET_RR * loss and capital <= budget
                and (capital_limits is None or capital_limits.failure(entry * qty, capital) is None)):
            low = mid
        else:
            high = mid - 1
    return low


def _outcome_pnl(outcome: Any, entry: Decimal, qty: int, entered_at: datetime,
                 as_of: datetime, exit_policy_ref: str, fees: FeeCalculator,
                 policy: PriceGatePolicy) -> Decimal:
    value = _mapping(outcome, "outcome")
    if value.get("basis") != "common_policy_exit_proxy" or value.get("exit_policy_ref") != exit_policy_ref:
        raise ValueError("OUTCOME_POLICY_OR_BASIS_MISMATCH")
    legs = value.get("legs")
    if not isinstance(legs, list) or not legs:
        raise ValueError("MISSING:outcome.legs")
    proceeds, sold, previous = Decimal(0), 0, entered_at
    for item in legs:
        leg = _mapping(item, "outcome.leg")
        when = _timestamp(leg.get("at"), "outcome.at")
        if not previous <= when <= as_of:
            raise ValueError("INVALID_TIME:outcome")
        previous = when
        count = _integer(leg.get("quantity"), "outcome.quantity")
        price = _number(leg.get("price"), "outcome.price") * (1 - policy.exit_slippage_bps / 10000)
        amount = price * count
        proceeds += amount - fees.calculate_sell_fee(amount)
        sold += count
    if sold != qty:
        raise ValueError("OUTCOME_QUANTITY_MISMATCH")
    cost = entry * qty
    return proceeds - cost - fees.calculate_buy_fee(cost)


def _evaluate(record: Mapping[str, Any], *, epoch: str, as_of: datetime,
              fees: FeeCalculator, policy: PriceGatePolicy,
              received_policy: Mapping | None = None, markout_policy=None, capital_policy=None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "opportunity_id": record["opportunity_id"], "symbol": record["symbol"],
        "gate_status": "unknown", "reason_codes": [], "outcome_status": "unknown",
        "entry_price_proxy": None, "target_net_gain": None, "planned_net_loss": None,
        "net_rr": None, "max_ask_whole_krw": None,
        "a_quantity": None, "b_quantity": None,
        "a_net_pnl": None, "b_net_pnl": None, "delta_net_pnl": None,
    }
    try:
        route = _text(record.get("route_origin"), "route_origin")
        strategy = _text(record.get("strategy"), "strategy")
    except ValueError as exc:
        result["reason_codes"] = [str(exc)]
        return result
    if route != "live_screening" or strategy != "gap_and_go":
        result.update(gate_status="out_of_scope", reason_codes=["ROUTE_OUT_OF_SCOPE"])
        return result
    try:
        if record.get("evaluation_epoch") != epoch:
            raise ValueError("EVALUATION_EPOCH_MISMATCH")
        decision = _timestamp(record.get("decision_at"), "decision_at")
        if decision > as_of:
            raise ValueError("DECISION_AFTER_REPORT")
        baseline = record.get("baseline_eligible")
        if capital_policy is not None and record.get("baseline_basis") != "engine_order_ready":
            raise ValueError("CAPITAL_REQUIRES_ORDER_READY")
        supported_basis = ("order_free_policy", "engine_order_ready") if received_policy else ("order_free_policy",)
        if (type(baseline) is not bool or record.get("baseline_basis") not in supported_basis
                or (record.get("baseline_basis") == "engine_order_ready" and not baseline)):
            raise ValueError("BASELINE_POLICY_UNKNOWN")
        if not baseline:
            result.update(gate_status="baseline_cash", reason_codes=["BASELINE_INELIGIBLE"],
                          a_quantity=0, b_quantity=0, outcome_status="known_cash",
                          a_net_pnl="0", b_net_pnl="0", delta_net_pnl="0")
            return result
        qty = _integer(record.get("quantity"), "quantity")
        capital_limits = None
        if capital_policy is not None:
            from .entry_capital_limits import limits_from_record
            capital_limits = limits_from_record(record, capital_policy)
            budget = capital_limits.cash
            result.update(capital_basis="pre_pending_internal_guard_proxy",
                          capital_constraints=capital_limits.export())
        else:
            budget = _number(record.get("capital_budget"), "capital_budget")
        target_data = _mapping(record.get("target"), "target")
        if received_policy is None:
            target_time = _known_at(target_data, decision, "target")
            if (target_data.get("basis") != "intraday_high_at_decision"
                    or target_time.astimezone(KST).date() != decision.astimezone(KST).date()):
                raise ValueError("TARGET_BASIS_OR_SESSION_MISMATCH")
            if target_time != decision:
                raise ValueError("NOT_KNOWN_THROUGH_DECISION:target")
        else:
            requested = _timestamp(target_data.get("requested_at"), "target.requested_at")
            target_time = _timestamp(target_data.get("received_at"), "target.received_at")
            if (target_data.get("basis") != "received_intraday_high"
                    or target_data.get("source") != "KIS_FHKST01010100"
                    or target_data.get("source_as_of") is not None
                    or not requested <= target_time <= decision
                    or requested.astimezone(KST).date() != decision.astimezone(KST).date()):
                raise ValueError("RECEIVED_TARGET_PROVENANCE_OR_TIME")
            if (Decimal(str((target_time - requested).total_seconds())) > received_policy["max_rest_rtt_seconds"]
                    or Decimal(str((decision - target_time).total_seconds())) > received_policy["max_target_age_seconds"]):
                raise ValueError("RECEIVED_TARGET_STALE_OR_DELAYED")
        target = _number(target_data.get("price"), "target.price")
        if target > Decimal("1e12"):
            raise ValueError("INVALID:target.price_range")
        stop = _mapping(record.get("stop"), "stop")
        _known_at(stop, decision, "stop")
        if stop.get("basis") != "net_pnl":
            raise ValueError("STOP_BASIS_NOT_NET")
        stop_pct = _number(stop.get("pct"), "stop.pct")
        if stop_pct >= 100:
            raise ValueError("INVALID:stop.pct")
        exit_policy_ref = _text(stop.get("policy_ref"), "stop.policy_ref")
        quote = _mapping(record.get("quote"), "quote")
        received = _timestamp(quote.get("received_at"), "quote.received_at")
        if received_policy is None:
            quote_at = _timestamp(quote.get("as_of"), "quote.as_of")
            entered_at = received
            if not decision <= quote_at <= received <= as_of:
                raise ValueError("QUOTE_TIME_ORDER")
            age = received - quote_at
        else:
            _text(quote.get("quote_id"), "quote.quote_id")
            _text(quote.get("session_evidence_ref"), "quote.session_evidence_ref")
            quote_at = received
            entered_at = _timestamp(quote.get("observed_at"), "quote.observed_at")
            if (quote.get("tr_id") != "H0STASP0" or type(quote.get("message_count")) is not int
                    or quote["message_count"] != 1 or quote.get("source_as_of") is not None
                    or quote.get("selection_rule") != "first_recorded_krx_after_decision"
                    or not decision <= received <= entered_at <= as_of
                    or received.astimezone(KST).date() != decision.astimezone(KST).date()):
                raise ValueError("RECEIVED_QUOTE_PROVENANCE_OR_TIME")
            age = entered_at - received  # 로컬 전달 지연이며 시장 원천 신선도가 아니다.
        if (Decimal(str(age.total_seconds())) > policy.max_quote_age_seconds
                or Decimal(str((entered_at - decision).total_seconds())) > policy.max_decision_delay_seconds):
            raise ValueError("QUOTE_STALE_OR_DELAYED")
        local = quote_at.astimezone(KST)
        if (quote.get("session") != "KRX_REGULAR_CONTINUOUS" or local.weekday() >= 5
                or not time(9) <= local.time() < time(15, 20)):
            raise ValueError("UNSUPPORTED_QUOTE_SESSION")
        ask = _number(quote.get("ask"), "quote.ask")
        bid = _number(quote.get("bid"), "quote.bid")
        if bid > ask or _integer(quote.get("ask_size"), "quote.ask_size") < qty:
            raise ValueError("QUOTE_CROSSED_OR_INSUFFICIENT_SIZE")
        entry, gain, loss, capital = _amounts(ask, target, qty, stop_pct, fees, policy)
        if capital_limits is not None:
            failed = capital_limits.failure(entry * qty, capital)
            if failed is not None:
                raise ValueError("CAPITAL_CONSTRAINT_FAILED:" + failed)
        elif capital > budget:
            raise ValueError("COMMON_QUANTITY_EXCEEDS_BUDGET")
        status = "allow" if gain >= MIN_NET_RR * loss else "cash"
        result.update(gate_status=status, reason_codes=[] if status == "allow" else ["NET_RR_BELOW_1_5"],
                      entry_price_proxy=str(entry), target_net_gain=str(gain), planned_net_loss=str(loss),
                      net_rr=str(gain / loss), max_ask_whole_krw=str(_price_cap(target, qty, stop_pct, budget, fees, policy, capital_limits)),
                      a_quantity=qty, b_quantity=qty if status == "allow" else 0)
        if status == "cash":
            result["b_net_pnl"] = "0"  # 확정된 미진입. A의 미래 손익은 아직 불명일 수 있다.
        try:
            if markout_policy is None:
                pnl = _outcome_pnl(record.get("outcome"), entry, qty, entered_at, as_of, exit_policy_ref, fees, policy)
                outcome_status = "complete_proxy"
            else:
                from .entry_markout import evaluate_markout
                if record.get("markout_assembly_reason"):
                    raise ValueError(record["markout_assembly_reason"])
                evaluated = evaluate_markout(record, entry=entry, qty=qty, decision=decision,
                                             entered_at=entered_at, as_of=as_of, fees=fees,
                                             price_policy=policy, policy=markout_policy)
                pnl = evaluated.pop("pnl")
                result.update(evaluated)
                outcome_status = "complete_markout_proxy"
            b_pnl = pnl if status == "allow" else Decimal(0)
            result.update(outcome_status=outcome_status, a_net_pnl=str(pnl), b_net_pnl=str(b_pnl),
                          delta_net_pnl=str(b_pnl - pnl))
        except (ValueError, DecimalException) as exc:
            result["outcome_reason"] = str(exc)
    except (ValueError, DecimalException) as exc:
        result["reason_codes"] = [str(exc)]
    return result


def build_report(payload: Mapping[str, Any]) -> dict[str, Any]:
    """오류 입력은 ValueError, 개별 불완전 기회는 unknown으로 분모에 보존한다."""
    source = _mapping(payload, "payload")
    if type(source.get("schema_version")) is not int or source["schema_version"] != 1:
        raise ValueError("지원하지 않는 schema_version")
    if source.get("dataset_kind") not in ("synthetic", "observed"):
        raise ValueError("dataset_kind는 synthetic 또는 observed여야 한다")
    epoch = _text(source.get("evaluation_epoch"), "evaluation_epoch")
    as_of = _timestamp(source.get("as_of"), "as_of")
    fee_ref = _text(source.get("fee_model_ref"), "fee_model_ref")
    policy = PriceGatePolicy.from_dict(source.get("policy"))
    fees = _fees(source.get("fees"))
    data_basis = source.get("data_basis", "market_timestamp")
    if data_basis not in ("market_timestamp", "received_snapshot"):
        raise ValueError("지원하지 않는 data_basis")
    received_policy = None
    if data_basis == "received_snapshot":
        raw = _mapping(source.get("received_policy"), "received_policy")
        version = _text(raw.get("version"), "received_policy.version")
        if version != "received-v1":
            raise ValueError("지원하지 않는 received_policy.version")
        received_policy = {"version": version, **{
            name: _number(raw.get(name), f"received_policy.{name}", zero=True)
            for name in ("max_rest_rtt_seconds", "max_target_age_seconds")}}
    outcome_basis = source.get("outcome_basis", "common_policy_exit_proxy")
    markout_policy = None
    if outcome_basis == "fixed_horizon_bid_markout":
        if data_basis != "received_snapshot":
            raise ValueError("고정 시점 호가 평가는 수신 기준 모드 전용")
        from .entry_markout import MarkoutPolicy
        markout_policy = MarkoutPolicy.from_dict(source.get("markout_policy"))
    elif outcome_basis != "common_policy_exit_proxy" or source.get("markout_policy") is not None:
        raise ValueError("지원하지 않거나 혼합된 outcome_basis")
    capital_policy = None
    if "capital_policy" in source:
        if data_basis != "received_snapshot":
            raise ValueError("자본 관측 한도는 수신 기준 모드 전용")
        from .entry_capital_limits import CapitalPolicy
        capital_policy = CapitalPolicy.from_dict(source["capital_policy"])
        if "capture" in source:
            capture = _mapping(source["capture"], "capture")
            if any(capture.get(key) != getattr(capital_policy, key)
                   for key in ("source_version_ref", "configuration_ref")):
                raise ValueError("자본 정책의 소스/설정 참조 불일치")
    records = source.get("opportunities")
    if not isinstance(records, list):
        raise ValueError("opportunities는 배열이어야 한다")
    ids, economic_keys = set(), set()
    for record in records:
        _mapping(record, "opportunity")
        if ((capital_policy is not None and any(k in record for k in ("capital_budget", "capital_budget_ref")))
                or (capital_policy is None and "capital_evidence" in record)):
            raise ValueError("명시 예산과 자본 관측 모드 혼합 금지")
        if record.get("data_basis", data_basis) != data_basis:
            raise ValueError("자료 시각 기준 혼합 금지")
        if (record.get("outcome_basis", outcome_basis) != outcome_basis
                or (markout_policy is not None and record.get("outcome") is not None)
                or (markout_policy is None and record.get("markout_quote") is not None)):
            raise ValueError("청산 경로와 고정 시점 평가 혼합 금지")
        oid = _text(record.get("opportunity_id"), "opportunity_id")
        symbol = _text(record.get("symbol"), "symbol")
        if oid in ids:
            raise ValueError("중복 opportunity_id")
        ids.add(oid)
        try:
            instant = _timestamp(record.get("decision_at"), "decision_at")
        except ValueError:
            continue  # 시각 불명은 개별 unknown이며 원래 행을 버리지 않는다.
        # 평가 시 문자열 정규화와 중복 검사 기준을 일치시킨다.
        key = (symbol, instant, str(record.get("route_origin")).strip(), str(record.get("strategy")).strip())
        if key in economic_keys:
            raise ValueError("중복 종목/의사결정 시각/경로")
        economic_keys.add(key)
    with localcontext() as context:
        context.prec = 34
        rows = [_evaluate(r, epoch=epoch, as_of=as_of, fees=fees, policy=policy,
                          received_policy=received_policy, markout_policy=markout_policy,
                          capital_policy=capital_policy) for r in records]
        counts = Counter(r["gate_status"] for r in rows)
        in_scope = len(rows) - counts["out_of_scope"]
        pairs = [r for r in rows if r["delta_net_pnl"] is not None]
        known_delta = sum((Decimal(r["delta_net_pnl"]) for r in pairs), Decimal(0)) if pairs else None
        avoided = sum((max(Decimal(r["delta_net_pnl"]), Decimal(0)) for r in pairs), Decimal(0)) if pairs else None
        missed = sum((max(-Decimal(r["delta_net_pnl"]), Decimal(0)) for r in pairs), Decimal(0)) if pairs else None
        complete = bool(in_scope) and len(pairs) == in_scope
    return {
        "schema_version": 1, "dataset_kind": source["dataset_kind"], "evaluation_epoch": epoch,
        "as_of": as_of.isoformat(), "fee_model_ref": fee_ref,
        "data_basis": data_basis,
        "data_quality": "received_snapshot_price_proxy" if received_policy else "market_timestamp_price_proxy",
        "received_policy": {k: str(v) for k, v in received_policy.items()} if received_policy else None,
        "outcome_basis": outcome_basis,
        "markout_policy": markout_policy.export() if markout_policy else None,
        "capital_policy": capital_policy.export() if capital_policy else None,
        "fees": {name: str(getattr(fees.config, name)) for name in FeeConfig.__dataclass_fields__},
        "policy": {**{name: str(getattr(policy, name)) for name in policy.__dataclass_fields__}, "min_net_rr": str(MIN_NET_RR)},
        "production_eligible": False, "account_return": None,
        "performance_status": "opportunity_diagnostic_only" if complete else "incomplete",
        "counts": {"total": len(rows), "in_scope": in_scope, "paired_outcomes": len(pairs),
                   **{name: counts[name] for name in ("allow", "cash", "baseline_cash", "unknown", "out_of_scope")}},
        "summary": {"known_pair_delta_net_pnl": str(known_delta) if known_delta is not None else None,
                    "known_avoided_loss": str(avoided) if avoided is not None else None,
                    "known_missed_gain": str(missed) if missed is not None else None,
                    "complete_delta_net_pnl": str(known_delta) if complete else None},
        "limitations": [
            "선택적 자본 관측 모드는 내부 예약 전 금액과 사전 선언 위험/비중 한도를 적용한다. 실효 설정 검증·실제 사이징 재현·브로커 주문 가능액 보장이 아니다.",
            "고정 시점 bid 평가는 중간 손절/익절/부분체결을 재생하지 않는다. 현행 청산·실제 체결·계좌 수익이 아니다.",
            "수신 기준 모드는 판단 전에 받은 고가를 고정하고 이후 첫 기록 KRX 호가 도착 시 비교한다. 시장 원천 시각·현재 고점·실제 체결을 입증하지 않는다.",
            "기회별 가격 대리치 비교이며 실제 체결·계좌 재생·수익성 승격 결과가 아니다.",
            "입력의 observed 표시는 원천 검증이나 전체 후보 분모의 완전성을 보장하지 않는다.",
            "정수 원 단위 상한은 거래소 호가단위와 다르며 시장가 체결 상한을 보장하지 않는다.",
            "최우선 잔량이 충분해도 체결 순서·지연·호가 취소로 전량 체결이 보장되지 않는다.",
            "손절은 계획 위험이다. 갭 관통·동적 손절 변경·체결 비용은 이를 초과할 수 있다.",
            "기회 손익 합계는 겹치는 자본·현금·슬롯·재스캔·운영비를 재생한 계좌 손익이 아니다.",
            "동일 정책 청산 가격 대리치는 입력 제공자가 재생해야 하며 계산기가 OHLC로 추정하지 않는다.",
        ],
        "opportunities": rows,
    }
