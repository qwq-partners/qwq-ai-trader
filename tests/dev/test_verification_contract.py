import copy
import hashlib
import json

import pytest

from scripts.dev.verification_contract import (
    EvidenceError,
    evaluate_bundle,
    parse_document,
    validate_receipt,
)


SHA = "a" * 40
TREE = "b" * 40
CONTRACT = "c" * 64
PRODUCER = "d" * 64
GUARD = "e" * 64


def inventory(nodes):
    payload = json.dumps(
        sorted(nodes), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def run(attempt=1, sha=SHA):
    return {
        "event": "local",
        "sha": sha,
        "tree": TREE,
        "contract": CONTRACT,
        "run_id": "offline-contract-1",
        "attempt": attempt,
    }


def slot(lane, timezone):
    return {"lane": lane, "timezone": timezone}


def expected():
    slots = []
    for lane, timezone, runtime, nodes in (
        ("standard", "UTC", "1" * 64, ["tests/dev/test_standard.py::test_ok"]),
        ("standard", "Asia/Seoul", "2" * 64, ["tests/dev/test_standard.py::test_ok"]),
        ("source-proof", "UTC", "3" * 64, ["tests/proofs/test_source.py::test_ok"]),
        ("source-proof", "Asia/Seoul", "4" * 64, ["tests/proofs/test_source.py::test_ok"]),
    ):
        slots.append(
            {
                "slot": slot(lane, timezone),
                "identity": {
                    "runtime": runtime,
                    "producer": PRODUCER,
                    "inventory": inventory(nodes),
                },
                "nodes": nodes,
                "allowed_outcomes": {},
                "guard": {
                    "path": "tests/conftest.py",
                    "sha256": GUARD,
                    "module_count": 1,
                    "violations": 0,
                },
            }
        )
    return {
        "schema": "qwq.verification-expectation/v1",
        "run": run(),
        "slots": slots,
    }


def receipt_for(expectation, position, *, call="passed"):
    target = expectation["slots"][position]
    nodes = target["nodes"]
    return {
        "schema": "qwq.verification-receipt/v1",
        "run": copy.deepcopy(expectation["run"]),
        "slot": copy.deepcopy(target["slot"]),
        "identity": copy.deepcopy(target["identity"]),
        "collected": list(nodes),
        "results": [
            {"nodeid": node, "setup": "passed", "call": call, "teardown": "passed"}
            for node in nodes
        ],
        "session": {
            "finished": True,
            "exit_code": 0,
            "collection_errors": 0,
            "deselected": 0,
        },
        "guard": copy.deepcopy(target["guard"]),
    }


def four_valid_receipts():
    document = expected()
    return [receipt_for(document, position) for position in range(4)]


def test_bundle_requires_four_distinct_matching_slots():
    document = expected()
    assert evaluate_bundle(four_valid_receipts(), document)["status"] == "EVIDENCE_CONSISTENT"
    assert evaluate_bundle(four_valid_receipts()[:3], document)["status"] == "REJECTED"


def test_known_performance_failure_is_not_automatic_pass():
    document = expected()
    receipt = receipt_for(document, 0, call="failed")
    assert validate_receipt(receipt, document) == ("CALL_FAILED",)


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        (b'{"schema":"qwq.verification-receipt/v1","schema":"x"}', "receipt"),
        (b'{"schema":"qwq.verification-receipt/v1","value":NaN}', "receipt"),
        (b"\xff", "receipt"),
        (b'{"schema":"unknown/v1"}', "receipt"),
        (b'{"schema":"qwq.verification-receipt/v1","unknown":1}', "receipt"),
    ],
)
def test_parse_document_rejects_noncanonical_json(raw, kind):
    with pytest.raises(EvidenceError):
        parse_document(raw, kind=kind)


def test_parse_document_rejects_depth_thirteen_and_oversize_input():
    nested = "{}"
    for _ in range(13):
        nested = '{"x":' + nested + "}"
    with pytest.raises(EvidenceError):
        parse_document(nested.encode(), kind="receipt")
    with pytest.raises(EvidenceError):
        parse_document(b" " * (32 * 1024 * 1024 + 1), kind="receipt")


def test_parse_document_converts_bounded_adversarial_values_to_evidence_errors():
    huge_integer = b'{"value":' + b"1" * 5_000 + b"}"
    with pytest.raises(EvidenceError):
        parse_document(huge_integer, kind="receipt")
    nested = "0"
    for _ in range(500):
        nested = "[" + nested + "]"
    with pytest.raises(EvidenceError):
        parse_document(nested.encode(), kind="receipt")
    document = expected()
    raw = json.dumps(document).replace(
        "tests/dev/test_standard.py::test_ok", "\\ud800", 1
    ).encode()
    with pytest.raises(EvidenceError):
        parse_document(raw, kind="expectation")


def test_public_dictionary_validation_fails_closed_for_unpaired_surrogates():
    valid = expected()
    malformed = expected()
    malformed["slots"][0]["nodes"] = ["\ud800"]
    receipt = receipt_for(valid, 0)
    assert validate_receipt(receipt, malformed) == ("INVALID_EXPECTATION",)
    assert evaluate_bundle([receipt], malformed)["errors"] == ["INVALID_EXPECTATION"]
    malformed_guard = expected()
    malformed_guard["slots"][0]["guard"]["path"] = "\ud800"
    assert validate_receipt(receipt, malformed_guard) == ("INVALID_EXPECTATION",)


def test_parse_document_rejects_bool_attempt_and_unknown_expected_field():
    document = expected()
    document["run"]["attempt"] = True
    with pytest.raises(EvidenceError):
        parse_document(json.dumps(document).encode(), kind="expectation")
    document = expected()
    document["slots"][0]["allowed_outcomes"] = {"node": ["skipped"]}
    with pytest.raises(EvidenceError):
        parse_document(json.dumps(document).encode(), kind="expectation")


def test_finished_session_requires_integer_exit_code_but_unfinished_null_remains_unapproved():
    document = expected()
    finished = receipt_for(document, 0)
    finished["session"]["exit_code"] = None

    with pytest.raises(EvidenceError):
        parse_document(json.dumps(finished).encode(), kind="receipt")
    assert validate_receipt(finished, document) == ("INVALID_RECEIPT",)
    assert "INVALID_RECEIPT" in evaluate_bundle([finished], document)["errors"]

    unfinished = receipt_for(document, 0)
    unfinished["session"].update(finished=False, exit_code=None)

    assert parse_document(json.dumps(unfinished).encode(), kind="receipt") == unfinished
    assert validate_receipt(unfinished, document) == (
        "SESSION_EXIT_NONZERO",
        "SESSION_UNFINISHED",
    )
    assert evaluate_bundle([unfinished], document)["status"] == "REJECTED"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: document["slots"][0]["slot"].__setitem__("lane", []),
        lambda document: document["slots"][0].__setitem__("nodes", ["x" * 2049]),
        lambda document: document["slots"][0].__setitem__("nodes", ["node"] * 20_001),
    ],
)
def test_parse_document_turns_wrong_types_and_node_bounds_into_evidence_errors(mutate):
    document = expected()
    mutate(document)
    with pytest.raises(EvidenceError):
        parse_document(json.dumps(document).encode(), kind="expectation")


def test_bundle_accepts_distinct_runtime_per_lane():
    document = expected()
    assert evaluate_bundle(four_valid_receipts(), document)["errors"] == []


def test_bundle_rejects_four_empty_expected_and_actual_inventories():
    document = expected()
    for item in document["slots"]:
        item["nodes"] = []
        item["identity"]["inventory"] = inventory([])
    receipts = [receipt_for(document, position) for position in range(4)]
    decision = evaluate_bundle(receipts, document)
    assert decision["status"] == "REJECTED"
    assert "INVALID_EXPECTATION" in decision["errors"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda receipts: receipts.__setitem__(1, copy.deepcopy(receipts[0])),
        lambda receipts: receipts[0]["run"].__setitem__("attempt", 2),
        lambda receipts: receipts[0]["run"].__setitem__("sha", "f" * 40),
    ],
)
def test_bundle_rejects_reused_slot_previous_attempt_and_other_sha(mutate):
    document = expected()
    receipts = four_valid_receipts()
    mutate(receipts)
    assert evaluate_bundle(receipts, document)["status"] == "REJECTED"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda receipt: receipt.__setitem__("collected", []),
        lambda receipt: receipt.__setitem__("collected", receipt["collected"] * 2),
        lambda receipt: receipt.__setitem__("collected", receipt["collected"] + ["extra"]),
        lambda receipt: receipt["guard"].__setitem__("module_count", 0),
        lambda receipt: receipt["guard"].__setitem__("module_count", 2),
        lambda receipt: receipt["guard"].__setitem__("violations", 1),
        lambda receipt: receipt["session"].__setitem__("finished", False),
        lambda receipt: receipt["session"].__setitem__("exit_code", 124),
        lambda receipt: receipt["results"][0].__setitem__("setup", "failed"),
        lambda receipt: receipt["results"][0].__setitem__("teardown", "failed"),
        lambda receipt: receipt["results"][0].__setitem__("call", "skipped"),
        lambda receipt: receipt["results"][0].__setitem__("call", "xfailed"),
        lambda receipt: receipt["results"][0].__setitem__("call", "xpassed"),
        lambda receipt: receipt["identity"].__setitem__("inventory", "0" * 64),
    ],
)
def test_receipt_validation_fails_closed_for_contract_violations(mutate):
    document = expected()
    receipt = receipt_for(document, 0)
    mutate(receipt)
    errors = validate_receipt(receipt, document)
    assert errors
    assert all(isinstance(error, str) and error.isupper() for error in errors)


def test_direct_bad_dictionaries_return_fixed_errors_without_raw_echoes():
    errors = validate_receipt({"secret": "do-not-echo"}, {"bad": "expectation"})
    decision = evaluate_bundle([{"secret": "do-not-echo"}], {"bad": "expectation"})
    assert errors == ("INVALID_EXPECTATION", "INVALID_RECEIPT")
    assert decision["errors"] == ["INVALID_EXPECTATION", "INVALID_RECEIPT"]
    assert "secret" not in " ".join(decision["errors"])


def test_decision_has_exact_offline_nonproduction_shape():
    decision = evaluate_bundle(four_valid_receipts(), expected())
    assert decision == {
        "schema": "qwq.verification-decision/v1",
        "status": "EVIDENCE_CONSISTENT",
        "errors": [],
        "scope": "offline_evidence_only",
        "production_eligible": False,
    }
