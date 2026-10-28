"""Pure Decimal comparison for an already-planned order snapshot.

The calculation uses a fixed Decimal context (precision 28, half-even
rounding) and accepts finite Decimal values with at most 28 digits, an
adjusted exponent no larger than 28, and an absolute exponent no larger than
28.  Quantity is an ``int`` (not ``bool``) from zero through 10**12.
"""

from decimal import (Context, Decimal, DecimalException, DivisionByZero,
                     InvalidOperation, Overflow, ROUND_HALF_EVEN, localcontext)

from src.utils.fee_calculator import FeeCalculator, FeeConfig


DECIMAL_CONTEXT = Context(
    prec=28, rounding=ROUND_HALF_EVEN,
    traps=[InvalidOperation, DivisionByZero, Overflow],
)
MAX_QUANTITY = 10 ** 12
GATE_KEYS = ("basis", "threshold", "evidence_ref", "runtime_equivalence",
             "decimal_value", "decimal_comparison")


def _decimal_text(value):
    """Return a context-independent finite Decimal spelling without spare zeros."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _supported_decimal(value):
    if type(value) is not Decimal or not value.is_finite():
        return False
    tuple_value = value.as_tuple()
    return (len(tuple_value.digits) <= 28 and abs(value.adjusted()) <= 28
            and abs(tuple_value.exponent) <= 28)


def _parse_decimal(value, *, positive, reason):
    if value is None:
        return None, "missing"
    if isinstance(value, bool) or isinstance(value, float):
        return None, reason
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = Decimal(value)
        except (DecimalException, ValueError):
            return None, reason
    else:
        return None, reason
    if not _supported_decimal(parsed) or (positive and parsed <= 0):
        return None, reason
    return parsed, None


def _blank(value):
    return not isinstance(value, str) or not value.strip()


def _gate_record(**values):
    record = dict.fromkeys(GATE_KEYS)
    record["runtime_equivalence"] = "not_claimed"
    record.update(values)
    return record


def _gate(original_gate):
    if original_gate is None:
        return "threshold_unknown", _gate_record()
    if type(original_gate) is not dict:
        return "invalid_gate", _gate_record()
    if original_gate.get("status") == "not_applicable":
        evidence_ref = original_gate.get("evidence_ref")
        if set(original_gate) != {"status", "evidence_ref"} or _blank(evidence_ref):
            return "invalid_gate", _gate_record()
        return "not_applicable", _gate_record(evidence_ref=evidence_ref)
    if set(original_gate) != {"basis", "positive_threshold", "evidence_ref"}:
        return "invalid_gate", _gate_record()
    basis = original_gate["basis"]
    threshold, threshold_error = _parse_decimal(
        original_gate["positive_threshold"], positive=True, reason="invalid_gate",
    )
    evidence_ref = original_gate["evidence_ref"]
    if (not isinstance(basis, str) or basis not in ("gross", "entry_roundtrip_per_share")
            or threshold_error or _blank(evidence_ref)):
        return "invalid_gate", _gate_record()
    return "decimal_reconstruction", _gate_record(
        basis=basis, threshold=_decimal_text(threshold), evidence_ref=evidence_ref,
    )


def _fee_rates(fee_config):
    names = ("buy_commission_rate", "sell_commission_rate", "sell_tax_rate")
    if not isinstance(fee_config, FeeConfig):
        return None
    values = {name: getattr(fee_config, name, None) for name in names}
    if any(not _supported_decimal(value) or value < 0 for value in values.values()):
        return None
    return {name: _decimal_text(value) for name, value in values.items()}


def _valid_fee(value):
    return _supported_decimal(value) and value >= 0


def _comparison(value, threshold):
    return "meets_threshold" if value >= threshold else "below_threshold"


def _empty_result(*, inputs, provenance, calculation_status, calculation_reason):
    return {
        "analytical_only": True,
        "inputs": inputs,
        "provenance": provenance,
        "calculation_status": calculation_status,
        "calculation_reason": calculation_reason,
        "gross_rr": None,
        "legacy_one_share": None,
        "quantity_adjusted": None,
        "gate_status": None,
        "gate": None,
        "hypothetical_policy_comparisons": [],
    }


def compare_order_rr(*, entry_price, target_price, stop_price, quantity,
                     source_path, source_ref, price_basis, fee_config,
                     fee_config_ref, original_gate=None):
    """Compare gross, legacy one-share, and quantity-adjusted price RR.

    Invalid inputs take precedence over missing inputs, which take precedence
    over an otherwise-valid zero quantity.  Gate status is independent of the
    calculation status, and a gate result is only a Decimal reconstruction.
    """
    with localcontext(DECIMAL_CONTEXT):
        return _compare_order_rr(
            entry_price=entry_price, target_price=target_price, stop_price=stop_price,
            quantity=quantity, source_path=source_path, source_ref=source_ref,
            price_basis=price_basis, fee_config=fee_config, fee_config_ref=fee_config_ref,
            original_gate=original_gate,
        )


def _compare_order_rr(*, entry_price, target_price, stop_price, quantity,
                      source_path, source_ref, price_basis, fee_config,
                      fee_config_ref, original_gate):
    inputs = {
        "entry_price": None, "target_price": None, "stop_price": None,
        "quantity": quantity if type(quantity) is int else None,
    }
    provenance = {
        "source_path": source_path if isinstance(source_path, str) else None,
        "source_ref": source_ref if isinstance(source_ref, str) else None,
        "price_basis": price_basis if isinstance(price_basis, str) else None,
        "fee_config_ref": fee_config_ref if isinstance(fee_config_ref, str) else None,
        "fee_rates": _fee_rates(fee_config),
    }
    gate_status, gate = _gate(original_gate)
    prices = []
    errors = []
    for value in (entry_price, target_price, stop_price):
        parsed, error = _parse_decimal(value, positive=True, reason="invalid_price")
        prices.append(parsed)
        errors.append(error)
    entry, target, stop = prices
    for key, value in zip(("entry_price", "target_price", "stop_price"), prices):
        if value is not None:
            inputs[key] = _decimal_text(value)

    provenance_invalid = any(_blank(value) for key, value in provenance.items() if key != "fee_rates")
    price_invalid = any(error == "invalid_price" for error in errors)
    price_missing = any(error == "missing" for error in errors)
    quantity_invalid = quantity is not None and (type(quantity) is not int or not 0 <= quantity <= MAX_QUANTITY)
    quantity_missing = quantity is None
    stop_invalid = entry is not None and stop is not None and stop >= entry
    fee_invalid = provenance["fee_rates"] is None

    if provenance_invalid:
        result = _empty_result(inputs=inputs, provenance=provenance,
                               calculation_status="invalid", calculation_reason="invalid_provenance")
    elif price_invalid:
        result = _empty_result(inputs=inputs, provenance=provenance,
                               calculation_status="invalid", calculation_reason="invalid_price")
    elif quantity_invalid:
        result = _empty_result(inputs=inputs, provenance=provenance,
                               calculation_status="invalid", calculation_reason="invalid_quantity")
    elif stop_invalid:
        result = _empty_result(inputs=inputs, provenance=provenance,
                               calculation_status="invalid", calculation_reason="invalid_stop_price")
    elif fee_invalid:
        result = _empty_result(inputs=inputs, provenance=provenance,
                               calculation_status="invalid", calculation_reason="invalid_fee_config")
    elif price_missing:
        result = _empty_result(inputs=inputs, provenance=provenance,
                               calculation_status="unevaluable", calculation_reason="missing_price")
    elif quantity_missing:
        result = _empty_result(inputs=inputs, provenance=provenance,
                               calculation_status="unevaluable", calculation_reason="missing_quantity")
    elif quantity == 0:
        result = _empty_result(inputs=inputs, provenance=provenance,
                               calculation_status="not_planned", calculation_reason="zero_quantity")
    else:
        try:
            calculator = FeeCalculator(fee_config)
            legacy_buy_fee = calculator.calculate_buy_fee(entry)
            legacy_sell_fee = calculator.calculate_sell_fee(entry)
            buy_amount = entry * quantity
            target_amount = target * quantity
            stop_amount = stop * quantity
            buy_fee = calculator.calculate_buy_fee(buy_amount)
            target_sell_fee = calculator.calculate_sell_fee(target_amount)
            stop_sell_fee = calculator.calculate_sell_fee(stop_amount)
            if not all(_valid_fee(fee) for fee in (
                legacy_buy_fee, legacy_sell_fee, buy_fee, target_sell_fee, stop_sell_fee,
            )):
                raise InvalidOperation
            legacy_reward = (target - entry) - legacy_buy_fee - legacy_sell_fee
            legacy_risk = (entry - stop) + legacy_buy_fee + legacy_sell_fee
            cost = buy_amount + buy_fee
            reward = target_amount - target_sell_fee - cost
            risk = cost - (stop_amount - stop_sell_fee)
            if legacy_risk <= 0 or risk <= 0:
                raise InvalidOperation
            gross_rr = (target - entry) / (entry - stop)
            legacy_rr = legacy_reward / legacy_risk
            quantity_rr = reward / risk
        except Exception:
            result = _empty_result(inputs=inputs, provenance=provenance,
                                   calculation_status="unavailable",
                                   calculation_reason="fee_calculation_unavailable")
        else:
            result = {
                "analytical_only": True, "inputs": inputs, "provenance": provenance,
                "calculation_status": "ok", "calculation_reason": None,
                "gross_rr": _decimal_text(gross_rr),
                "legacy_one_share": {
                    "buy_fee": _decimal_text(legacy_buy_fee),
                    "sell_fee_at_entry": _decimal_text(legacy_sell_fee),
                    "reward": _decimal_text(legacy_reward), "risk": _decimal_text(legacy_risk),
                    "rr": _decimal_text(legacy_rr),
                },
                "quantity_adjusted": {
                    "buy_amount": _decimal_text(buy_amount), "buy_fee": _decimal_text(buy_fee),
                    "cost": _decimal_text(cost), "target_amount": _decimal_text(target_amount),
                    "target_sell_fee": _decimal_text(target_sell_fee), "reward": _decimal_text(reward),
                    "stop_amount": _decimal_text(stop_amount), "stop_sell_fee": _decimal_text(stop_sell_fee),
                    "risk": _decimal_text(risk), "rr": _decimal_text(quantity_rr),
                },
                "gate_status": None, "gate": None, "hypothetical_policy_comparisons": [],
            }

    result["gate_status"] = gate_status
    result["gate"] = gate
    if result["calculation_status"] == "ok" and gate_status == "decimal_reconstruction":
        threshold = Decimal(gate["threshold"])
        model_value = Decimal(result["gross_rr"] if gate["basis"] == "gross"
                              else result["legacy_one_share"]["rr"])
        gate.update(decimal_value=_decimal_text(model_value),
                    decimal_comparison=_comparison(model_value, threshold))
        quantity_rr = Decimal(result["quantity_adjusted"]["rr"])
        if gate["basis"] == "gross":
            kind, status = "gross_threshold_vs_quantity_adjusted_net_rr", "hypothetical_policy_comparison"
        else:
            kind, status = "known_net_threshold_vs_quantity_adjusted_rr", "hypothetical_model_change"
        result["hypothetical_policy_comparisons"] = [{
            "kind": kind, "status": status, "threshold": gate["threshold"],
            "quantity_adjusted_net_rr": _decimal_text(quantity_rr),
            "comparison": _comparison(quantity_rr, threshold),
        }]
    return result
