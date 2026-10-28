import json
from decimal import Decimal, Inexact, getcontext, localcontext

import pytest

from src.analytics.order_rr_comparison import compare_order_rr
from src.utils.fee_calculator import FeeConfig


D = Decimal
ZERO = FeeConfig(D("0"), D("0"), D("0"))
ROUNDED = FeeConfig(D("0.001"), D("0.002"), D("0.003"))


def compare(**overrides):
    values = dict(
        entry_price=D("1000"), target_price=D("1146"), stop_price=D("930"),
        quantity=10, source_path="unit/snapshot.json", source_ref="snapshot-1",
        price_basis="planned_limit", fee_config=ZERO, fee_config_ref="fees-v1",
    )
    values.update(overrides)
    return compare_order_rr(**values)


def test_computes_zero_fee_gross_legacy_and_quantity_model_from_explicit_snapshot():
    """Would fail if any RR model ignores the supplied P/T/S/q snapshot."""
    result = compare()

    assert result["analytical_only"] is True
    assert result["calculation_status"] == "ok"
    assert result["inputs"] == {
        "entry_price": "1000", "target_price": "1146", "stop_price": "930", "quantity": 10,
    }
    assert result["provenance"] == {
        "source_path": "unit/snapshot.json", "source_ref": "snapshot-1",
        "price_basis": "planned_limit", "fee_config_ref": "fees-v1",
        "fee_rates": {"buy_commission_rate": "0", "sell_commission_rate": "0", "sell_tax_rate": "0"},
    }
    assert result["gross_rr"] == str(D("146") / D("70"))
    assert result["legacy_one_share"]["rr"] == str(D("146") / D("70"))
    assert result["quantity_adjusted"] == {
        "buy_amount": "10000", "buy_fee": "0", "cost": "10000",
        "target_amount": "11460", "target_sell_fee": "0", "reward": "1460",
        "stop_amount": "9300", "stop_sell_fee": "0", "risk": "700", "rr": str(D("1460") / D("700")),
    }


def test_uses_combined_amount_fee_rounding_for_quantity_model_not_per_share_fills():
    """Would fail if combined order amounts are replaced with simulated partial fills."""
    result = compare(fee_config=ROUNDED, quantity=10)

    assert result["legacy_one_share"] == {
        "buy_fee": "1", "sell_fee_at_entry": "5", "reward": "140", "risk": "76",
        "rr": str(D("140") / D("76")),
    }
    assert result["quantity_adjusted"] == {
        "buy_amount": "10000", "buy_fee": "10", "cost": "10010",
        "target_amount": "11460", "target_sell_fee": "57", "reward": "1393",
        "stop_amount": "9300", "stop_sell_fee": "47", "risk": "757", "rr": str(D("1393") / D("757")),
    }
    assert result["provenance"]["fee_rates"] == {
        "buy_commission_rate": "0.001", "sell_commission_rate": "0.002", "sell_tax_rate": "0.003",
    }


def test_keeps_target_and_stop_price_snapshots_independent():
    """Would fail if target fees are reused for the stop or vice versa."""
    baseline = compare(fee_config=ROUNDED)
    target_changed = compare(target_price="1200", fee_config=ROUNDED)
    stop_changed = compare(stop_price="900", fee_config=ROUNDED)

    assert (baseline["quantity_adjusted"]["target_sell_fee"], baseline["quantity_adjusted"]["stop_sell_fee"]) == ("57", "47")
    assert (target_changed["quantity_adjusted"]["target_sell_fee"], target_changed["quantity_adjusted"]["stop_sell_fee"]) == ("60", "47")
    assert (stop_changed["quantity_adjusted"]["target_sell_fee"], stop_changed["quantity_adjusted"]["stop_sell_fee"]) == ("57", "45")


def test_gate_reconstruction_and_cross_model_comparisons_are_labeled_not_original_verdicts():
    """Would fail if a Decimal model result is presented as the runtime float gate."""
    result = compare(original_gate={
        "basis": "gross", "positive_threshold": "1.5", "evidence_ref": "scheduler:line-1",
    })

    assert result["gate_status"] == "decimal_reconstruction"
    assert result["gate"]["runtime_equivalence"] == "not_claimed"
    assert result["gate"]["decimal_comparison"] == "meets_threshold"
    assert result["hypothetical_policy_comparisons"] == [{
        "kind": "gross_threshold_vs_quantity_adjusted_net_rr", "status": "hypothetical_policy_comparison",
        "threshold": "1.5", "quantity_adjusted_net_rr": str(D("1460") / D("700")),
        "comparison": "meets_threshold",
    }]


def test_legacy_gate_to_quantity_comparison_is_labeled_as_a_model_change():
    """Would fail if legacy one-share and quantity RR were silently substituted."""
    result = compare(fee_config=ROUNDED, original_gate={
        "basis": "entry_roundtrip_per_share", "positive_threshold": D("2"), "evidence_ref": "base:rr",
    })

    assert result["gate"]["decimal_value"] == str(D("140") / D("76"))
    assert result["gate"]["decimal_comparison"] == "below_threshold"
    assert result["hypothetical_policy_comparisons"][0]["kind"] == "known_net_threshold_vs_quantity_adjusted_rr"
    assert result["hypothetical_policy_comparisons"][0]["status"] == "hypothetical_model_change"


def test_unknown_and_explicitly_not_applicable_gate_stay_distinct():
    """Would fail if an absent gate receives a guessed default threshold."""
    assert compare()["gate_status"] == "threshold_unknown"
    result = compare(original_gate={"status": "not_applicable", "evidence_ref": "vcp:no-rr-gate"})
    assert result["gate_status"] == "not_applicable"
    assert result["gate"]["threshold"] is None


@pytest.mark.parametrize("overrides, status, reason", [
    ({"quantity": None}, "unevaluable", "missing_quantity"),
    ({"entry_price": None}, "unevaluable", "missing_price"),
    ({"quantity": 0}, "not_planned", "zero_quantity"),
    ({"quantity": -1}, "invalid", "invalid_quantity"),
    ({"quantity": 1.0}, "invalid", "invalid_quantity"),
    ({"quantity": True}, "invalid", "invalid_quantity"),
    ({"stop_price": "1000"}, "invalid", "invalid_stop_price"),
    ({"entry_price": 1000.0}, "invalid", "invalid_price"),
])
def test_missing_and_invalid_inputs_have_distinct_safe_statuses(overrides, status, reason):
    """Would fail if invalid inputs become zero-valued RR calculations."""
    result = compare(**overrides)

    assert result["calculation_status"] == status
    assert result["calculation_reason"] == reason
    assert result["gross_rr"] is None
    assert result["quantity_adjusted"] is None


def test_rejects_each_invalid_fee_rate_and_reports_fee_failures_without_raw_values(monkeypatch):
    """Would fail if negative/NaN fees can cancel or exception data leaks into output."""
    for config in (
        FeeConfig(D("-0.1"), D("0"), D("0")), FeeConfig(D("0"), D("NaN"), D("0")),
        FeeConfig(D("0"), D("0"), D("-0.1")),
    ):
        result = compare(fee_config=config)
        assert result["calculation_status"] == "invalid"
        assert result["calculation_reason"] == "invalid_fee_config"

    class BrokenCalculator:
        def __init__(self, config):
            pass

        def calculate_buy_fee(self, amount):
            return D("NaN")

        def calculate_sell_fee(self, amount):
            raise RuntimeError("private amount=10000")

    monkeypatch.setattr("src.analytics.order_rr_comparison.FeeCalculator", BrokenCalculator)
    result = compare()
    assert result["calculation_status"] == "unavailable"
    assert result["calculation_reason"] == "fee_calculation_unavailable"
    assert "private" not in repr(result)


def test_invalid_source_ref_is_sanitized_to_a_json_serializable_invalid_result():
    """Would fail if rejected provenance objects leak into the analytical report."""
    result = compare(source_ref=object())

    assert result["calculation_status"] == "invalid"
    assert result["inputs"]["quantity"] == 10
    assert result["provenance"]["source_ref"] is None
    json.dumps(result)


def test_invalid_decimal_quantity_is_sanitized_separately_from_provenance():
    """Would fail if a rejected quantity leaks into a JSON report."""
    result = compare(quantity=D("1"))

    assert (result["calculation_status"], result["calculation_reason"]) == ("invalid", "invalid_quantity")
    assert result["inputs"]["quantity"] is None
    json.dumps(result)


@pytest.mark.parametrize("quantity, expected", [
    (1, {"buy_fee": "1", "target_sell_fee": "6", "stop_sell_fee": "5", "reward": "139", "risk": "76"}),
    (10, {"buy_fee": "10", "target_sell_fee": "57", "stop_sell_fee": "47", "reward": "1393", "risk": "757"}),
    (100, {"buy_fee": "100", "target_sell_fee": "573", "stop_sell_fee": "465", "reward": "13927", "risk": "7565"}),
])
def test_rounded_fee_snapshots_cover_q1_q10_and_q100(quantity, expected):
    """Would fail if quantity-specific combined price fees are not recalculated."""
    actual = compare(fee_config=ROUNDED, quantity=quantity)["quantity_adjusted"]

    assert {key: actual[key] for key in expected} == expected


@pytest.mark.parametrize("threshold", ["1.3", "1.5"])
def test_documented_gross_thresholds_remain_distinct_from_legacy_net(threshold):
    """Would fail if gross thresholds are rewritten as a net/legacy gate."""
    gross = compare(fee_config=ROUNDED, original_gate={
        "basis": "gross", "positive_threshold": threshold, "evidence_ref": "gross-source",
    })
    legacy = compare(fee_config=ROUNDED, original_gate={
        "basis": "entry_roundtrip_per_share", "positive_threshold": "2", "evidence_ref": "legacy-source",
    })

    assert gross["gate"]["decimal_comparison"] == "meets_threshold"
    assert gross["gate"]["decimal_value"] == str(D("146") / D("70"))
    assert legacy["gate"]["decimal_comparison"] == "below_threshold"
    assert legacy["hypothetical_policy_comparisons"][0]["status"] == "hypothetical_model_change"


@pytest.mark.parametrize("gate", [
    [], {"basis": [], "positive_threshold": "2", "evidence_ref": "x"},
    {"basis": {}, "positive_threshold": "2", "evidence_ref": "x"},
    {"basis": "other", "positive_threshold": "2", "evidence_ref": "x"},
    {"basis": "gross", "positive_threshold": "0", "evidence_ref": "x"},
    {"basis": "gross", "positive_threshold": "-1", "evidence_ref": "x"},
    {"basis": "gross", "positive_threshold": 2.0, "evidence_ref": "x"},
    {"basis": "gross", "positive_threshold": "NaN", "evidence_ref": "x"},
    {"basis": "gross", "positive_threshold": "2", "evidence_ref": " "},
    {"basis": "gross", "positive_threshold": "2"},
    {"basis": "gross", "positive_threshold": "2", "evidence_ref": "x", "extra": "forbidden"},
    {"status": "not_applicable", "evidence_ref": "x", "extra": "forbidden"},
])
def test_invalid_gate_forms_never_escape_or_change_calculation_status(gate):
    """Would fail if malformed provenance raises or is treated as a gate result."""
    result = compare(original_gate=gate)

    assert result["calculation_status"] == "ok"
    assert result["gate_status"] == "invalid_gate"
    assert set(result["gate"]) == {"basis", "threshold", "evidence_ref", "runtime_equivalence", "decimal_value", "decimal_comparison"}


@pytest.mark.parametrize("overrides, status, reason", [
    ({"target_price": None}, "unevaluable", "missing_price"), ({"stop_price": None}, "unevaluable", "missing_price"),
    ({"entry_price": "NaN"}, "invalid", "invalid_price"), ({"target_price": "Infinity"}, "invalid", "invalid_price"),
    ({"stop_price": D("NaN")}, "invalid", "invalid_price"), ({"entry_price": "0"}, "invalid", "invalid_price"),
    ({"target_price": "-1"}, "invalid", "invalid_price"), ({"stop_price": "not-a-number"}, "invalid", "invalid_price"),
    ({"entry_price": None, "target_price": "NaN"}, "invalid", "invalid_price"),
    ({"entry_price": None, "quantity": -1}, "invalid", "invalid_quantity"),
    ({"quantity": None, "stop_price": "1001"}, "invalid", "invalid_stop_price"),
    ({"quantity": 10 ** 12 + 1}, "invalid", "invalid_quantity"),
])
def test_invalid_inputs_beat_missing_inputs_and_stop_inversion(overrides, status, reason):
    """Would fail if malformed data is downgraded to an unevaluable snapshot."""
    result = compare(**overrides)

    assert result["calculation_status"] == status
    assert result["calculation_reason"] == reason


@pytest.mark.parametrize("target, expected_reward", [("1000", "0"), ("999", "-10")])
def test_target_at_or_below_entry_is_a_calculated_negative_or_zero_reward_case(target, expected_reward):
    """Would fail if target<=entry is rejected instead of analyzed."""
    result = compare(target_price=target, fee_config=ZERO)

    assert result["calculation_status"] == "ok"
    assert result["quantity_adjusted"]["reward"] == expected_reward


def test_combined_rounding_is_explicitly_distinct_from_split_arithmetic_without_partial_fills():
    """Would fail if combined order fees become a simulated sum of one-share fees."""
    rounded = compare(fee_config=ROUNDED, quantity=10)["quantity_adjusted"]
    buy_split = compare(fee_config=FeeConfig(D("0.0004"), D("0.0004"), D("0")), quantity=3)["quantity_adjusted"]

    assert (rounded["target_sell_fee"], rounded["stop_sell_fee"]) == ("57", "47")
    assert (rounded["target_sell_fee"], rounded["stop_sell_fee"]) != ("60", "50")
    assert buy_split["buy_fee"] == "1"
    assert buy_split["buy_fee"] != "0"


@pytest.mark.parametrize("side, returned", [
    ("buy", D("-1")), ("sell", D("-1")), ("buy", D("NaN")), ("sell", D("NaN")),
    ("buy", 0), ("sell", 0), ("buy", 1.0), ("sell", 1.0),
])
def test_each_fee_return_is_independently_validated(monkeypatch, side, returned):
    """Would fail if a bad individual calculator result can cancel a valid fee."""
    class BadReturnCalculator:
        def __init__(self, config):
            pass

        def calculate_buy_fee(self, amount):
            return returned if side == "buy" else D("0")

        def calculate_sell_fee(self, amount):
            return returned if side == "sell" else D("0")

    monkeypatch.setattr("src.analytics.order_rr_comparison.FeeCalculator", BadReturnCalculator)
    result = compare()
    assert (result["calculation_status"], result["calculation_reason"]) == ("unavailable", "fee_calculation_unavailable")


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_fee_calculator_exceptions_are_separate_from_bad_fee_values(monkeypatch, side):
    """Would fail if calculator exceptions expose messages or create partial values."""
    class RaisingCalculator:
        def __init__(self, config):
            pass

        def calculate_buy_fee(self, amount):
            if side == "buy":
                raise RuntimeError("private buy fee")
            return D("0")

        def calculate_sell_fee(self, amount):
            if side == "sell":
                raise RuntimeError("private sell fee")
            return D("0")

    monkeypatch.setattr("src.analytics.order_rr_comparison.FeeCalculator", RaisingCalculator)
    result = compare()
    assert result["calculation_status"] == "unavailable"
    assert result["gross_rr"] is None
    assert "private" not in repr(result)


def test_fixed_decimal_context_preserves_inputs_and_results_despite_caller_precision_and_traps():
    """Would fail if caller Decimal context changes the pure comparison result."""
    baseline = compare(entry_price="1000.123456", target_price="1146.123456", stop_price="930.123456")
    with localcontext() as caller_context:
        caller_context.prec = 6
        caller_context.traps[Inexact] = True
        constrained = compare(entry_price="1000.123456", target_price="1146.123456", stop_price="930.123456")

    assert constrained == baseline
    assert constrained["inputs"]["entry_price"] == "1000.123456"
    assert getcontext().prec != 6


def test_extreme_decimal_exponents_are_rejected_without_escaping():
    """Would fail if a huge Decimal exponent reaches formatting or arithmetic."""
    price = compare(entry_price="1E+1000000")
    gate = compare(original_gate={"basis": "gross", "positive_threshold": "1E+1000000", "evidence_ref": "x"})

    assert (price["calculation_status"], price["calculation_reason"]) == ("invalid", "invalid_price")
    assert (gate["calculation_status"], gate["gate_status"]) == ("ok", "invalid_gate")


def test_entry_price_snapshot_changes_buy_legacy_and_quantity_arithmetic_independently():
    """Would fail if an entry-only snapshot reuses the previous entry fees or cost."""
    result = compare(entry_price="1100", fee_config=ROUNDED)

    assert result["legacy_one_share"] == {
        "buy_fee": "1", "sell_fee_at_entry": "6", "reward": "39", "risk": "177",
        "rr": str(D("39") / D("177")),
    }
    assert {key: result["quantity_adjusted"][key] for key in ("buy_fee", "cost", "reward", "risk")} == {
        "buy_fee": "11", "cost": "11011", "reward": "392", "risk": "1758",
    }


def test_oversized_quantity_does_not_break_json_serialization():
    """Would fail if a rejected oversized integer is copied into the JSON result."""
    result = compare(quantity=10 ** 5000)

    assert (result["calculation_status"], result["calculation_reason"]) == ("invalid", "invalid_quantity")
    assert result["inputs"]["quantity"] is None
    json.dumps(result)
