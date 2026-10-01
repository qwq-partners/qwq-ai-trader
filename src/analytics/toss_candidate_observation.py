"""엔진 후보 ID와 토스 통합 호가의 별도 품질 보고서. 손익/주문 판정 없음."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

from src.data.providers.toss.orderbook_stream import MARKET, SCHEMA, SOURCE, aware_time, decimal_string
from .entry_observation import observation_population


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("연결 식별자 필요")
    return value


def _anchors(engine, as_of):
    projection = engine.get('schema_version') in ('entry-anchor-projection-v1', 'entry-anchor-projection-v2')
    if projection:
        from src.observation.entry_anchor_input import validate_projection
        validate_projection(engine)
        if aware_time(engine['observed_at']) > as_of:
            raise ValueError('후보 투영이 보고 시각보다 늦음')
    elif engine.get("schema_version") != 1:
        raise ValueError("엔진 관측 schema 불일치")
    records = engine.get("records")
    if not isinstance(records, list) or len(records) > 50000:
        raise ValueError("유한 엔진 관측 배열 필요")
    if any(not isinstance(r, dict) or type(r.get("sequence")) is not int
           or r["sequence"] != i for i, r in enumerate(records, 1)):
        raise ValueError("엔진 관측 순서/결손")
    observation_population(records)
    scans = [r for r in records if r.get("kind") == "scan"]
    if len(scans) != 1 or scans[0].get("route_origin") != "live_screening":
        raise ValueError("정확히 한 스캔의 반환 후보 필요")
    scan = scans[0]
    scan_id = _text(scan.get("scan_id"))
    scan_at = aware_time(scan.get("observed_at"))
    maximum = 100 if engine.get('schema_version') == 'entry-anchor-projection-v2' else 3
    if scan_at > as_of or not isinstance(scan.get("candidates"), list) or not 1 <= len(scan["candidates"]) <= maximum:
        raise ValueError("후보 수/관측 시각 범위 위반")
    rows, symbols = {}, set()
    for candidate in scan["candidates"]:
        cid, symbol = _text(candidate.get("candidate_id")), _text(candidate.get("symbol"))
        if cid != f"{scan_id}:{symbol}" or cid in rows or symbol in symbols:
            raise ValueError("후보 ID 중복/불일치")
        symbols.add(symbol)
        rows[cid] = {"candidate_id": cid, "symbol": symbol, "scan_at": scan_at.isoformat(),
                     "signal_id": None, "signal_at": None, "decision_at": None, "order_id": None,
                     "requested_quantity": None, "engine_stage": "no_signal_observed"}
    signals, orders = {}, set()
    for record in records:
        kind = record.get("kind")
        if kind not in ("signal", "order_ready"):
            continue
        at = aware_time(record.get("observed_at"))
        if not scan_at <= at <= as_of or record["sequence"] <= scan["sequence"]:
            raise ValueError("후보/판단/보고 시각 순서 위반")
        sid = _text(record.get("signal_id"))
        if kind == "signal":
            cid = record.get("candidate_id")
            if cid not in rows or sid in signals or rows[cid]["signal_id"] is not None:
                raise ValueError("미연결/중복 신호")
            signals[sid] = cid
            rows[cid].update(signal_id=sid, signal_at=at.isoformat(), engine_stage="signal_observed")
        else:
            if sid not in signals:
                raise ValueError("미연결 주문 생성 기록")
            row = rows[signals[sid]]
            oid, qty = _text(record.get("order_id")), record.get("requested_quantity")
            if (row["decision_at"] is not None or oid in orders or record.get("symbol") != row["symbol"]
                    or type(qty) is not int or qty <= 0 or at < aware_time(row["signal_at"])):
                raise ValueError("주문 ID/종목/수량/시각 불일치")
            orders.add(oid)
            row.update(decision_at=at.isoformat(), order_id=oid, requested_quantity=qty,
                       engine_stage="order_ready_observed")
    return list(rows.values())


def _books(capture, as_of):
    if (capture.get("schema_version") != SCHEMA or capture.get("source") != SOURCE
            or capture.get("market_basis") != MARKET or capture.get("delivery") != "LOSSY"
            or capture.get("stream_complete") is not None):
        raise ValueError("토스 통합/LOSSY 관측 계약 불일치")
    start, end = aware_time(capture.get("start_at")), aware_time(capture.get("end_at"))
    if not start < end <= as_of or (end - start).total_seconds() > 3600:
        raise ValueError("종료된 관측 구간과 보고 시각 필요")
    symbols = capture.get("symbols")
    if (not isinstance(symbols, list) or not 1 <= len(symbols) <= 3
            or any(not isinstance(s, str) or len(s) != 6 or not s.isascii() or not s.isdigit() for s in symbols)
            or len(set(symbols)) != len(symbols)):
        raise ValueError("고정한 국내 관측 종목 필요")
    received_frames, max_frames = capture.get("received_frames"), capture.get("max_frames")
    if (type(max_frames) is not int or not 1 <= max_frames <= 50000
            or type(received_frames) is not int or not 0 <= received_frames <= max_frames):
        raise ValueError("수신 프레임 예산/개수 불일치")
    acknowledged, rejected = capture.get("acknowledged_symbols"), capture.get("rejected_symbols")
    if (not isinstance(acknowledged, list) or not isinstance(rejected, list)
            or any(not isinstance(x, str) for x in acknowledged + rejected)
            or len(set(acknowledged + rejected)) != len(acknowledged + rejected)
            or set(acknowledged + rejected) - set(symbols)):
        raise ValueError("구독 결과 불일치")
    ack_at = capture.get("acknowledged_at")
    if ack_at is not None:
        ack_at = aware_time(ack_at)
        if not start <= ack_at < end or set(acknowledged + rejected) != set(symbols):
            raise ValueError("구독 ACK 시각/모집단 불일치")
    elif acknowledged or rejected:
        raise ValueError("구독 ACK 근거 없음")
    records = capture.get("records")
    if not isinstance(records, list) or len(records) > received_frames:
        raise ValueError("호가 관측 배열 필요")
    age_limit = capture.get("max_source_age_seconds")
    if type(age_limit) is not int or not 1 <= age_limit <= 60:
        raise ValueError("호가 신선도 규약 필요")
    prior_at, prior_index, prior_source = start, 0, {}
    for quote in records:
        if not isinstance(quote, dict):
            raise ValueError("호가 관측 형식 위반")
        received = aware_time(quote.get("received_at"))
        index = quote.get("received_index")
        if (quote.get("source") != SOURCE or quote.get("market_basis") != MARKET
                or quote.get("delivery") != "LOSSY" or quote.get("source_sequence") is not None
                or quote.get("kis_executable") is not False or quote.get("symbol") not in acknowledged
                or ack_at is None or not ack_at <= received < end or received < prior_at
                or type(index) is not int or not prior_index < index <= received_frames
                or not isinstance(quote.get("quality_issues"), list)
                or any(not isinstance(x, str) for x in quote["quality_issues"])):
            raise ValueError("호가 출처/구독/순서 불일치")
        prior_at, prior_index = received, index
        source_at = quote.get("source_as_of")
        if source_at is not None:
            source_at = aware_time(source_at)
        if not quote["quality_issues"]:
            previous = prior_source.get(quote["symbol"])
            if (source_at is None or not 0 <= (received - source_at).total_seconds() <= age_limit
                    or (previous is not None and source_at <= previous)):
                raise ValueError("원천 시각 품질 표시 불일치")
        if source_at is not None and source_at <= received:
            prior_source[quote["symbol"]] = max(source_at, prior_source.get(quote["symbol"], source_at))
    return records


def _first_quote(quotes, symbol, anchor, common_reasons):
    reasons = list(common_reasons)
    if anchor is None:
        return {"status": "unknown", "reasons": reasons + ["decision_not_observed"], "quote": None}
    at = aware_time(anchor)
    first = next((q for q in quotes if q["symbol"] == symbol and aware_time(q["received_at"]) >= at), None)
    if first is None:
        return {"status": "unknown", "reasons": reasons + ["no_received_quote"], "quote": None}
    reasons.extend(first["quality_issues"])
    spread = None
    if not first["quality_issues"]:
        # 통합 호가 스프레드 진단이다. 이 가격으로 KIS 주문할 수 있다는 뜻이 아니다.
        try:
            ask, bid = decimal_string(first["ask"]), decimal_string(first["bid"])
            sizes = [decimal_string(first[k]) for k in ("ask_size", "bid_size")]
            if (not all(v.is_finite() and v > 0 for v in [ask, bid] + sizes) or bid > ask
                    or first.get("source_as_of") is None):
                raise ValueError("불완전 호가")
            spread = format(((ask - bid) / ask * Decimal("10000")).normalize(), "f")
        except (ValueError, TypeError, ArithmeticError):
            reasons.append("invalid_normalized_quote")
    delay = Decimal(str((aware_time(first["received_at"]) - at).total_seconds())) * 1000
    return {"status": "unknown" if reasons else "observed_snapshot", "reasons": sorted(set(reasons)),
            "quote": deepcopy(first), "spread_bps": spread, "received_delay_ms": str(delay.normalize())
            if delay != delay.to_integral_value() else str(int(delay))}


def build_toss_candidate_report(engine, capture, *, as_of):
    """전체 반환 후보와 정확한 ID 연결만 허용한다. 누락을 미매수/회피손실로 바꾸지 않는다."""
    as_of = aware_time(as_of)
    if not isinstance(engine, dict) or not isinstance(capture, dict):
        raise ValueError("명시한 엔진/토스 관측 필요")
    if _text(engine.get("evaluation_epoch")) != capture.get("evaluation_epoch"):
        raise ValueError("평가 세대 불일치")
    rows, quotes = _anchors(engine, as_of), _books(capture, as_of)
    start, end = aware_time(capture["start_at"]), aware_time(capture["end_at"])
    if any(not start <= aware_time(row["scan_at"]) < end
           or (row["decision_at"] is not None and not start <= aware_time(row["decision_at"]) < end)
           for row in rows):
        raise ValueError("엔진 후보/판단 시각이 관측 구간 밖")
    selection = engine.get('selection') if engine.get('schema_version') == 'entry-anchor-projection-v2' else None
    selected_ids = (set(selection['selected_candidate_ids']) if selection is not None
                    else {r['candidate_id'] for r in rows})
    if {r["symbol"] for r in rows if r['candidate_id'] in selected_ids} != set(capture["symbols"]):
        raise ValueError("엔진 반환 후보 전체와 구독 모집단 불일치")
    common = []
    if (engine.get("complete") is not True or type(engine.get("dropped_records")) is not int
            or engine["dropped_records"] != 0 or engine.get("incomplete_reasons") != []):
        common.append("engine_capture_incomplete")
    if capture.get("stop_reason") != "window_ended":
        common.append("toss_capture_incomplete")
    if capture.get("cleanup_failed") is not False:
        common.append("toss_cleanup_incomplete")
    for row in rows:
        row['selected_for_toss_capture'] = row['candidate_id'] in selected_ids
        if not row['selected_for_toss_capture']:
            row['engine_stage'] = 'outside_projected_subset'
            for key in ('first_after_scan', 'first_after_decision'):
                row[key] = dict(status='unknown', reasons=['outside_toss_capture_subset'], quote=None)
            continue
        reasons = list(common)
        if row["symbol"] in capture["rejected_symbols"]:
            reasons.append("subscription_rejected")
        row["first_after_scan"] = _first_quote(quotes, row["symbol"], row["scan_at"], reasons)
        row["first_after_decision"] = _first_quote(quotes, row["symbol"], row["decision_at"], reasons)
    return {"schema_version": "toss-candidate-quality-v1", "evaluation_epoch": engine["evaluation_epoch"],
            "as_of": as_of.isoformat(), "source": SOURCE, "market_basis": MARKET,
            "profit_comparison_available": False, "kis_execution_evidence": False,
            "stream_complete": None, "candidates": rows,
            "population": dict(returned_candidate_count=len(rows), observed_subset_count=len(selected_ids),
                               unobserved_candidate_count=len(rows)-len(selected_ids),
                               selection_rule=selection['rule'] if selection is not None else 'whole_returned_cohort'),
            "limitations": ["반환 후보 집합만 보존하며 스크리너 내부 탈락 종목 전체는 아니다.",
                            "처음 수신한 호가이며 최초 시장 호가/무손실 틱/체결 가능한 유동성이 아니다.",
                            "order_ready는 주문 생성 기록이며 매수 승인·접수·체결 증거가 아니다.",
                            "고정 구간 호가 품질 진단이며 진입 A/B·순수익 계산은 제공하지 않는다."]}
