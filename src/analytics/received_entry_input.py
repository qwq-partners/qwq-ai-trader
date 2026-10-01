"""수신 시각 기준 후보 연결. 원래 기록 순서를 사용하고 결측은 추정하지 않는다."""
from __future__ import annotations

from copy import deepcopy
from typing import Mapping

from .entry_price_shadow import build_report, _mapping, _text, _timestamp


def _subscription_stream_complete(records, report_at):
    """구독 기록이 있는 수집본만 연결 계약 검사. 기존 오프라인 가격 대리치는 유지.

    후보의 배정 여부는 승인 근거가 아니다. 실제 공유 운영 BOOK도 현재 채널 ACK와
    같은 세대의 Quote가 있으면 사용할 수 있다. 단절/불명은 보수적으로 세대 전체 차단.
    """
    if not any(isinstance(r, Mapping) and (r.get("kind") == "quote_subscription" or
            (r.get("kind") == "ws_quote" and isinstance(r.get("provenance"), Mapping)
             and any(r["provenance"].get(k) is not None for k in ("generation", "connection_id")))) for r in records):
        return True
    statuses = {"connected", "desired", "allocated", "shared_operational", "unallocated", "expired",
                "requested", "acknowledged", "observed", "unsubscribe_requested", "released",
                "rejected", "unknown", "connection_gap"}
    candidates, channels = {}, {}
    current = None
    last_time = None
    try:
        for record in records:
            kind = record.get("kind")
            if kind == "scan":
                candidates.update({c["candidate_id"]: c["symbol"] for c in record["candidates"]})
                continue
            if kind not in ("quote_subscription", "ws_quote"):
                continue
            at = _timestamp(record.get("observed_at"), "구독 관측 시각")
            if at > report_at or (last_time is not None and at < last_time):
                return False
            last_time = at
            if kind == "ws_quote":
                provenance = _mapping(record.get("provenance"), "호가 연결 증거")
                generation = provenance.get("generation")
                if type(generation) is not int or (generation, provenance.get("connection_id")) != current:
                    return False
                if channels.get((provenance.get("tr_id"), record.get("symbol"))) != "acknowledged":
                    return False
                continue
            status = record.get("status")
            if status not in statuses or status in ("unknown", "rejected", "connection_gap"):
                return False
            cid = record.get("candidate_id")
            if cid is not None and (cid not in candidates or record.get("symbol") != candidates[cid]):
                return False
            generation, connection = record.get("generation"), record.get("connection_id")
            if type(generation) is int and generation == 0 and connection is None and status in ("desired", "unallocated", "allocated", "expired"):
                continue  # 연결 전 대기 기록은 호가/채널 증거로 쓰지 않는다.
            if type(generation) is not int or generation <= 0 or not isinstance(connection, str) or not connection:
                return False
            if status == "connected":
                if current is not None:
                    return False  # 같은 버퍼의 재연결은 완전 수집이 아니다.
                current = generation, connection
                continue
            if (generation, connection) != current:
                return False
            key = record.get("tr_id"), record.get("symbol")
            if status in ("requested", "acknowledged", "observed", "unsubscribe_requested", "released"):
                if key[0] not in ("H0STCNT0", "H0STASP0", "H0NXCNT0", "H0NXASP0") or not isinstance(key[1], str):
                    return False
                if status == "requested":
                    if key in channels:
                        return False
                    channels[key] = "requested"
                elif status == "acknowledged":
                    if channels.get(key) != "requested":
                        return False
                    channels[key] = status
                elif status == "unsubscribe_requested":
                    if channels.get(key) != "acknowledged":
                        return False
                    channels[key] = status
                elif status == "released":
                    if channels.get(key) != "unsubscribe_requested":
                        return False
                    channels[key] = status
                elif channels.get(key) not in ("requested", "acknowledged"):
                    return False
        return True
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def prepare_received_input(context: Mapping, observations: Mapping, evaluation_inputs: list) -> dict:
    """관측 수량/손절과 명시 규약 연결. 자본은 명시 예산 또는 opt-in 관측 한도."""
    payload = deepcopy(dict(context))
    payload["opportunities"] = []
    if payload.get("data_basis") != "received_snapshot":
        raise ValueError("수신 기준 전용 연결기")
    build_report(payload)
    capital_mode = "capital_policy" in payload
    markout_policy = None
    if payload.get("outcome_basis") == "fixed_horizon_bid_markout":
        from .entry_markout import MarkoutPolicy
        markout_policy = MarkoutPolicy.from_dict(payload["markout_policy"])
    if (type(observations.get("schema_version")) is not int or observations["schema_version"] != 1
            or observations.get("evaluation_epoch") != payload["evaluation_epoch"]):
        raise ValueError("관측 schema/평가 세대 불일치")
    records = observations.get("records")
    if not isinstance(records, list) or not isinstance(evaluation_inputs, list):
        raise ValueError("records/evaluation_inputs 배열 필요")
    from .entry_observation import observation_population
    population = observation_population(records)
    if any(isinstance(r, dict) and (r.get('kind') == 'selection_basis' or r.get('selection_basis_expected'))
           for r in records):
        from .selection_basis import build_selection_report
        build_selection_report(observations)
    candidates, signals, orders, quotes, rest = {}, {}, {}, [], {}
    candidate_signals = {}
    quote_ids = set()
    report_at = _timestamp(payload["as_of"], "보고 시각")
    subscription_gap = not _subscription_stream_complete(records, report_at)
    # 정렬해서 자료를 복구하지 않는다. 원래 버퍼의 연속 순서가 없으면 연결 불가다.
    for seq, record in enumerate(records, 1):
        _mapping(record, "record")
        if type(record.get("sequence")) is not int or record["sequence"] != seq:
            raise ValueError("관측 순서 결손/변경")
        kind = record.get("kind")
        if kind == "scan":
            scan_at = _timestamp(record.get("observed_at"), "후보 관측 시각")
            if scan_at > report_at:
                raise ValueError("후보 관측이 보고 시각보다 늦음")
            for item in record["candidates"]:
                cid = _text(item.get("candidate_id"), "candidate_id")
                if cid in candidates or cid != f"{record['scan_id']}:{item['symbol']}":
                    raise ValueError("후보 연결 ID 중복/불일치")
                candidates[cid] = (item, record)
        elif kind in ("signal", "rest_quote", "emit_result"):
            cid = record.get("candidate_id")
            if cid not in candidates:
                raise ValueError("관측의 미연결 후보 ID")
            if kind == "signal":
                sid = _text(record.get("signal_id"), "signal_id")
                if sid in signals:
                    raise ValueError("중복 signal_id")
                signals[sid] = record
                candidate_signals.setdefault(cid, []).append(record)
            elif kind == "rest_quote":
                rest.setdefault(cid, []).append(record)
        elif kind == "order_ready":
            sid = record.get("signal_id")
            if sid not in signals or sid in orders:
                raise ValueError("주문 생성 관측의 미연결/중복 signal_id")
            orders[sid] = record
        elif kind == "ws_quote":
            qid = _text(record.get("quote_id"), "quote_id")
            if qid in quote_ids:
                raise ValueError("중복 quote_id")
            quote_ids.add(qid)
            quotes.append(record)
        elif kind == "quote_subscription":
            # 상태 기록은 후보 승인이나 호가의 대체 자료가 아니다.
            subscription_gap |= record.get("status") == "connection_gap"
        elif kind == 'selection_basis':
            if _timestamp(record.get('observed_at'), '선정 근거 관측 시각') > report_at:
                raise ValueError('선정 근거 관측이 보고 시각보다 늦음')
            # 위에서 후보 연결을 검증했다. 선정 점수를 가격/진입 승인으로 사용하지 않는다.
        else:
            raise ValueError("지원하지 않는 관측 kind")
    supplements = {}
    allowed = {"opportunity_id", "capital_budget", "capital_budget_ref", "exit_policy_ref",
               "policy_inputs_at", "quote_session", "outcome"}
    if capital_mode:
        allowed -= {"capital_budget", "capital_budget_ref"}
    if markout_policy:
        allowed = (allowed - {"outcome", "exit_policy_ref"}) | {"markout_session"}
    for value in evaluation_inputs:
        item = _mapping(value, "evaluation_input")
        cid = item.get("opportunity_id")
        if cid not in candidates or cid in supplements or set(item) - allowed:
            raise ValueError("평가 입력의 미연결/중복 ID 또는 관측값 덮어쓰기")
        supplements[cid] = item
    complete = (observations.get("complete") is True
                and not subscription_gap
                and type(observations.get("dropped_records")) is int
                and observations["dropped_records"] == 0)
    for cid, (stock, scan) in candidates.items():
        row = {"opportunity_id": cid, "symbol": stock["symbol"], "route_origin": "live_screening",
               "strategy": None, "evaluation_epoch": payload["evaluation_epoch"],
               "decision_at": None, "baseline_eligible": None}
        try:
            linked = candidate_signals.get(cid, [])
            if len(linked) != 1:
                raise ValueError("MISSING_OR_AMBIGUOUS_SIGNAL")
            signal = linked[0]
            row["strategy"] = signal.get("strategy")
            order = orders.get(signal["signal_id"])
            if order is None:
                raise ValueError("MISSING_ORDER_READY")
            if order.get("symbol") != stock["symbol"] or order.get("strategy") != row["strategy"]:
                raise ValueError("ORDER_IDENTITY_MISMATCH")
            if (order.get("stop_basis") != "net_pnl" or order.get("stop_resolved_at_stage") != "order_ready"
                    or order.get("fill_applied") is not False):
                raise ValueError("ORDER_STOP_BASIS_MISMATCH")
            _text(order.get("stop_snapshot_ref"), "stop_snapshot_ref")
            decision = _timestamp(order.get("observed_at"), "decision_at")
            row["decision_at"] = order["observed_at"]
            scan_at = _timestamp(scan["observed_at"], "scan_at")
            signal_at = _timestamp(signal.get("observed_at"), "signal_at")
            if not scan_at <= signal_at <= decision <= report_at:
                raise ValueError("CANDIDATE_SIGNAL_DECISION_TIME_ORDER")
            responses = [r for r in rest.get(cid, []) if r["sequence"] < signal["sequence"]]
            if not responses:
                raise ValueError("MISSING_PRE_SIGNAL_REST")
            response = responses[-1]  # 신호 이전 마지막 실제 응답. 무효이면 이전 값으로 후퇴하지 않는다.
            requested = _timestamp(response.get("requested_at"), "target.requested_at")
            received = _timestamp(response.get("observed_at"), "target.received_at")
            if not scan_at <= requested <= received <= signal_at:
                raise ValueError("TARGET_NOT_KNOWN_BEFORE_SIGNAL")
            target = {"price": _mapping(response.get("quote"), "rest.quote").get("high"),
                      "basis": "received_intraday_high", "source": response.get("source"),
                      "source_as_of": response.get("source_as_of"), "requested_at": response["requested_at"],
                      "received_at": response["observed_at"]}
            first = next((q for q in quotes if q.get("symbol") == stock["symbol"]
                          and q["sequence"] > order["sequence"]
                          and _mapping(q.get("provenance"), "quote.provenance").get("tr_id") != "H0NXASP0"), None)
            if first is None:
                raise ValueError("MISSING_FIRST_RECORDED_KRX_QUOTE")
            supplement = supplements.get(cid, {})
            if _timestamp(supplement.get("policy_inputs_at"), "policy_inputs_at") > decision:
                raise ValueError("POLICY_INPUTS_AFTER_DECISION")
            if capital_mode:
                capital_fields = {"capital_evidence": {
                    "snapshot": deepcopy(order.get("capital_snapshot")),
                    "snapshot_ref": order.get("capital_snapshot_ref"),
                    "snapshot_status": order.get("capital_snapshot_status"),
                    "signal_id": signal["signal_id"], "order_id": order.get("order_id"),
                    "signal_observed_at": signal.get("observed_at"),
                    "order_reference_price": order.get("order_reference_price"),
                    "order_type": order.get("order_type")}}
            else:
                capital_fields = {"capital_budget": supplement.get("capital_budget"),
                                  "capital_budget_ref": _text(supplement.get("capital_budget_ref"), "capital_budget_ref")}
            exit_ref = (order["stop_snapshot_ref"] if markout_policy
                        else _text(supplement.get("exit_policy_ref"), "exit_policy_ref"))
            session = _mapping(supplement.get("quote_session"), "quote_session")
            if session.get("quote_id") != first["quote_id"]:
                raise ValueError("FIRST_QUOTE_SESSION_EVIDENCE_MISMATCH")
            quote = {**deepcopy(first["provenance"]), "quote_id": first["quote_id"],
                     "observed_at": first.get("observed_at"), "ask": first.get("ask"),
                     "bid": first.get("bid"), "ask_size": first.get("ask_size"),
                     "session": session.get("session"), "session_evidence_ref": session.get("evidence_ref"),
                     "selection_rule": "first_recorded_krx_after_decision"}
            row.update(baseline_eligible=True if complete else None, baseline_basis="engine_order_ready",
                       baseline_scope="pre_transport_order_creation", quantity=order.get("requested_quantity"),
                       **capital_fields, target=target, quote=quote,
                       stop={"pct": order.get("effective_stop_pct"), "basis": "net_pnl", "policy_ref": exit_ref,
                             "as_of": row["decision_at"], "available_at": row["decision_at"]},
                       risk_stop_pct=order.get("risk_stop_pct"), stop_snapshot_ref=order["stop_snapshot_ref"],
                       outcome=deepcopy(supplement.get("outcome")))
            if markout_policy:
                from .entry_markout import select_markout_quote
                try:
                    selected = select_markout_quote(first, quotes, markout_policy)
                    if selected is not None:
                        evidence = _mapping(supplement.get("markout_session"), "markout_session")
                        if evidence.get("quote_id") != selected["quote_id"]:
                            raise ValueError("MARKOUT_FIRST_QUOTE_SESSION_EVIDENCE_MISMATCH")
                        row["markout_quote"] = {
                            **deepcopy(selected["provenance"]), "quote_id": selected["quote_id"],
                            "observed_at": selected.get("observed_at"), "ask": selected.get("ask"),
                            "bid": selected.get("bid"), "bid_size": selected.get("bid_size"),
                            "session": evidence.get("session"), "session_evidence_ref": evidence.get("evidence_ref"),
                            "selection_rule": "first_recorded_krx_at_or_after_horizon"}
                except ValueError as exc:
                    row["markout_assembly_reason"] = str(exc)
        except ValueError as exc:
            row["assembly_reason"] = str(exc)
        payload["opportunities"].append(row)
    report = build_report(payload)
    return {"payload": payload, "capture_complete": complete,
            **population,
            "ready_opportunities": report["counts"]["allow"] + report["counts"]["cash"],
            "missing_evaluation_inputs": len(candidates) - len(supplements), "report": report,
            "limitations": [
                "수신 정보의 가격 조건 진단이며 시장시각 검증·현행 ATR 목표 재현·실제 체결 결과가 아니다.",
                "반환 후보 전체를 남긴다. 신호/주문 생성/신규 후보 호가 누락은 현금이 아니라 unknown이다.",
                "완전성은 이 버퍼의 명시 유실 여부만 뜻하며 미구독 호가·네트워크 유실을 보장하지 않는다.",
                "주문 생성은 전송 전 정책 통과이며 브로커 승인·킬스위치 통과·체결을 의미하지 않는다.",
                "초기 손절은 판단 시점 예상값이다. 자본 정책과 전체 청산 정책은 사전 고정하며 관측 한도 사용은 명시적으로 선택한다.",
            ]}
