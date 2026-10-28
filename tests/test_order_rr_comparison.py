import json
from decimal import Decimal

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


def test_keeps_target_and_stop_price_snapshots_independent():
    """Would fail if target fees are reused for the stop or vice versa."""
    result = compare(entry_price="1000", target_price="1001", stop_price="900", quantity=3,
                     fee_config=FeeConfig(D("0.0004"), D("0.0004"), D("0")))

    assert result["quantity_adjusted"]["buy_fee"] == "1"
    assert result["quantity_adjusted"]["target_sell_fee"] == "1"
    assert result["quantity_adjusted"]["stop_sell_fee"] == "1"
    assert result["quantity_adjusted"]["reward"] == "1"
    assert result["quantity_adjusted"]["risk"] == "302"


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


def test_invalid_inputs_are_sanitized_to_a_json_serializable_unavailable_result():
    """Would fail if rejected caller objects leak into the analytical report."""
    result = compare(quantity=D("1"), source_ref=object())

    assert result["calculation_status"] == "invalid"
    assert result["inputs"]["quantity"] is None
    assert result["provenance"]["source_ref"] is None
    json.dumps(result)
