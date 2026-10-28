"""Pure, offline Decimal comparison for an already-planned order snapshot.

This module does not approve orders or recover a runtime gate decision.  It
only compares documented price/RR models from explicitly supplied inputs.
"""

from decimal import Decimal, DecimalException, InvalidOperation

from src.utils.fee_calculator import FeeCalculator, FeeConfig


def _decimal_text(value):
    """Make a finite Decimal JSON-safe without scientific notation."""
    return format(value.normalize(), "f")


def _price(value):
    if value is None:
        return None, "missing_price"
    if isinstance(value, bool) or isinstance(value, float):
        return None, "invalid_price"
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = Decimal(value)
        except (DecimalException, ValueError):
            return None, "invalid_price"
    else:
        return None, "invalid_price"
    if not parsed.is_finite() or parsed <= 0:
        return None, "invalid_price"
    return parsed, None


def _threshold(value):
    if isinstance(value, bool) or isinstance(value, float):
        return None
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = Decimal(value)
        except (DecimalException, ValueError):
            return None
    else:
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _blank(value):
    return not isinstance(value, str) or not value.strip()


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


def _gate(original_gate):
    if original_gate is None:
        return "threshold_unknown", {
            "basis": None, "threshold": None, "evidence_ref": None,
            "runtime_equivalence": "not_claimed",
        }
    if type(original_gate) is not dict:
        return "invalid_gate", {"runtime_equivalence": "not_claimed"}
    if original_gate.get("status") == "not_applicable":
        evidence_ref = original_gate.get("evidence_ref")
        if set(original_gate) != {"status", "evidence_ref"} or _blank(evidence_ref):
            return "invalid_gate", {"runtime_equivalence": "not_claimed"}
        return "not_applicable", {
            "basis": None, "threshold": None, "evidence_ref": evidence_ref,
            "runtime_equivalence": "not_claimed",
        }
    if set(original_gate) != {"basis", "positive_threshold", "evidence_ref"}:
        return "invalid_gate", {"runtime_equivalence": "not_claimed"}
    basis = original_gate["basis"]
    threshold = _threshold(original_gate["positive_threshold"])
    evidence_ref = original_gate["evidence_ref"]
    if basis not in {"gross", "entry_roundtrip_per_share"} or threshold is None or _blank(evidence_ref):
        return "invalid_gate", {"runtime_equivalence": "not_claimed"}
    return "decimal_reconstruction", {
        "basis": basis, "threshold": _decimal_text(threshold), "evidence_ref": evidence_ref,
        "runtime_equivalence": "not_claimed",
    }


def _valid_fee_config(fee_config):
    if not isinstance(fee_config, FeeConfig):
        return False
    for name in ("buy_commission_rate", "sell_commission_rate", "sell_tax_rate"):
        value = getattr(fee_config, name, None)
        if type(value) is not Decimal or not value.is_finite() or value < 0:
            return False
    return True


def _valid_fee(value):
    return type(value) is Decimal and value.is_finite() and value >= 0


def _comparison(value, threshold):
    return "meets_threshold" if value >= threshold else "below_threshold"


def compare_order_rr(*, entry_price, target_price, stop_price, quantity,
                     source_path, source_ref, price_basis, fee_config,
                     fee_config_ref, original_gate=None):
    """Compare gross, legacy one-share, and quantity-adjusted price RR.

    Prices accept finite positive ``Decimal`` values or Decimal strings.
    Quantity is an integer share count. Returned values are JSON-safe and
    every reported gate value is explicitly a Decimal reconstruction.
    """
    inputs = {
        "entry_price": None, "target_price": None, "stop_price": None,
        "quantity": quantity if type(quantity) is int else None,
    }
    provenance = {
        "source_path": source_path if isinstance(source_path, str) else None,
        "source_ref": source_ref if isinstance(source_ref, str) else None,
        "price_basis": price_basis if isinstance(price_basis, str) else None,
        "fee_config_ref": fee_config_ref if isinstance(fee_config_ref, str) else None,
    }
    gate_status, gate = _gate(original_gate)

    if any(_blank(value) for value in provenance.values()):
        result = _empty_result(inputs=inputs, provenance=provenance,
                               calculation_status="invalid", calculation_reason="invalid_provenance")
    else:
        prices = []
        errors = []
        for value in (entry_price, target_price, stop_price):
            parsed, error = _price(value)
            prices.append(parsed)
            errors.append(error)
        if any(error == "missing_price" for error in errors):
            result = _empty_result(inputs=inputs, provenance=provenance,
                                   calculation_status="unevaluable", calculation_reason="missing_price")
        elif any(errors):
            result = _empty_result(inputs=inputs, provenance=provenance,
                                   calculation_status="invalid", calculation_reason="invalid_price")
        else:
            entry, target, stop = prices
            inputs.update(entry_price=_decimal_text(entry), target_price=_decimal_text(target),
                          stop_price=_decimal_text(stop))
            if quantity is None:
                result = _empty_result(inputs=inputs, provenance=provenance,
                                       calculation_status="unevaluable", calculation_reason="missing_quantity")
            elif type(quantity) is not int or quantity < 0:
                result = _empty_result(inputs=inputs, provenance=provenance,
                                       calculation_status="invalid", calculation_reason="invalid_quantity")
            elif quantity == 0:
                result = _empty_result(inputs=inputs, provenance=provenance,
                                       calculation_status="not_planned", calculation_reason="zero_quantity")
            elif stop >= entry:
                result = _empty_result(inputs=inputs, provenance=provenance,
                                       calculation_status="invalid", calculation_reason="invalid_stop_price")
            elif not _valid_fee_config(fee_config):
                result = _empty_result(inputs=inputs, provenance=provenance,
                                       calculation_status="invalid", calculation_reason="invalid_fee_config")
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
