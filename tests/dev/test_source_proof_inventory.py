import pytest

from scripts.dev.source_proof_inventory import (
    RELATED_NODES,
    SOURCE_NODES,
    inventory_digest,
    validate_inventory,
)


SOURCE_DIGEST = "d461b0d8f51ffc2b0c260f20480848aa872e08d94d27e83200406defd5131ef7"
RELATED_DIGEST = "4ce0932d927d10b58c182a04fffa5386a41e35dff330efb21ce83df4dd52397e"


def test_source_inventory_is_sorted_exactly_108_nodes_with_fixed_digest():
    assert len(SOURCE_NODES) == 108
    assert SOURCE_NODES == tuple(sorted(SOURCE_NODES))
    assert inventory_digest(SOURCE_NODES) == SOURCE_DIGEST


def test_related_inventory_is_sorted_exactly_31_nodes_with_fixed_digest():
    assert len(RELATED_NODES) == 31
    assert RELATED_NODES == tuple(sorted(RELATED_NODES))
    assert inventory_digest(RELATED_NODES) == RELATED_DIGEST


@pytest.mark.parametrize(
    ("actual", "lane", "code"),
    [
        (list(SOURCE_NODES), "source", None),
        (list(reversed(RELATED_NODES)), "related", None),
        (list(SOURCE_NODES[:-1]), "source", "source_proof_inventory_missing"),
        (
            [*SOURCE_NODES, "tests/proofs/l3_source/source_lease_cases.py::unexpected"],
            "source",
            "source_proof_inventory_unexpected",
        ),
        ([SOURCE_NODES[0], SOURCE_NODES[0]], "source", "source_proof_inventory_duplicate"),
        (list(SOURCE_NODES), "wrong", "source_proof_inventory_invalid_lane"),
        (tuple(SOURCE_NODES), "source", "source_proof_inventory_invalid_actual"),
        ([True], "source", "source_proof_inventory_invalid_node"),
        ([SOURCE_NODES[0], 1], "source", "source_proof_inventory_invalid_node"),
        (["x" * 2049], "source", "source_proof_inventory_node_too_long"),
    ],
)
def test_validate_inventory_accepts_only_an_exact_lane_multiset(actual, lane, code):
    if code is None:
        validate_inventory(actual, lane=lane)
    else:
        with pytest.raises(ValueError, match=f"^{code}$"):
            validate_inventory(actual, lane=lane)
