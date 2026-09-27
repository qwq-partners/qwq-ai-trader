"""순수 runtime admission 계약은 어떤 실행 권한도 부여하지 않는다."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts.dev.source_runtime_contract import (
    RuntimeContractError,
    evaluate_runtime_contract,
    parse_runtime_document,
    subject_digest,
)


ROOT = Path(__file__).resolve().parents[2]
H = lambda character: character * 64


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def subject():
    runtime_files = {
        "bin/python": H("1"),
        "lib/libpython.so": H("2"),
        "lib/sqlite_extension.so": H("3"),
        "lib/libsqlite3.so": H("4"),
        "lib/ld-linux.so": H("5"),
    }
    source_files = {
        "source/helper.py": H("6"),
        "source/cases.py": H("7"),
        "source/guard.py": H("8"),
        "source/controller.py": H("9"),
        "source/bootstrap.py": H("a"),
        "source/inventory.py": H("b"),
        "source/oracle.py": H("c"),
        "source/dependency-manifest.txt": H("d"),
    }
    return {
        "schema": "qwq.source-runtime-subject/v1",
        "platform": {
            "image_sha256": H("e"),
            "os_id": "debian-12",
            "architecture": "x86_64",
        },
        "runtime": {
            "implementation": "cpython",
            "version": "3.12.3",
            "soabi": "cpython-312-x86_64-linux-gnu",
            "build_sha256": H("f"),
        },
        "provenance": {
            "cpython_source": H("0"),
            "cpython_patches": H("1"),
            "sqlite_source": H("2"),
            "sqlite_patches": H("3"),
            "build_recipe": H("4"),
            "dependency_lock": H("5"),
        },
        "profile": {
            "id": "linux-ro-network-off/v1",
            "kernel_policy_sha256": H("6"),
            "runtime_readonly": True,
            "source_readonly": True,
            "network_enabled": False,
            "home_mounted": False,
            "credentials_mounted": False,
            "private_tmp": True,
        },
        "runtime_files": runtime_files,
        "source_files": source_files,
        "roles": {
            "interpreter": "bin/python",
            "libpython": "lib/libpython.so",
            "sqlite_extension": "lib/sqlite_extension.so",
            "sqlite_library": "lib/libsqlite3.so",
            "loader": "lib/ld-linux.so",
            "helper": "source/helper.py",
            "cases": "source/cases.py",
            "guard": "source/guard.py",
            "controller": "source/controller.py",
            "bootstrap": "source/bootstrap.py",
            "inventory": "source/inventory.py",
            "oracle": "source/oracle.py",
            "dependency_manifest": "source/dependency-manifest.txt",
        },
    }


def observation(document):
    return {
        "schema": "qwq.source-runtime-observation/v1",
        "subject_sha256": subject_digest(document),
        "profile_sha256": hashlib.sha256(canonical(document["profile"])).hexdigest(),
        "runtime_files": copy.deepcopy(document["runtime_files"]),
        "source_files": copy.deepcopy(document["source_files"]),
        "provenance": copy.deepcopy(document["provenance"]),
    }


def registry(document, state="BOOTSTRAP_ALLOWED"):
    entry = {
        "subject_sha256": subject_digest(document),
        "state": state,
        "profile_sha256": hashlib.sha256(canonical(document["profile"])).hexdigest(),
        "bootstrap_plan_sha256": H("a") if state in {"BOOTSTRAP_ALLOWED", "QUALIFIED"} else None,
        "evidence_sha256": H("b") if state == "QUALIFIED" else None,
        "review_sha256": H("c") if state == "QUALIFIED" else None,
    }
    return {"schema": "qwq.source-runtime-registry/v1", "entries": [entry]}


def binding(raw, revision="a" * 40):
    return {
        "expected_revision": revision,
        "observed_revision": revision,
        "expected_sha256": hashlib.sha256(raw).hexdigest(),
    }


def evaluate(document=None, registered_state="BOOTSTRAP_ALLOWED", mode="bootstrap", *,
             registry_document=None, observation_document=None, binding_document=None):
    document = subject() if document is None else document
    registry_document = registry(document, registered_state) if registry_document is None else registry_document
    subject_raw = canonical(document)
    registry_raw = canonical(registry_document)
    observation_raw = canonical(observation(document) if observation_document is None else observation_document)
    return evaluate_runtime_contract(
        subject_raw,
        registry_raw,
        observation_raw,
        mode=mode,
        binding=binding(registry_raw) if binding_document is None else binding_document,
    )


def assert_offline_only(decision):
    assert decision["scope"] == "offline_runtime_contract_only"
    assert decision["registry_trust_verified"] is False
    assert decision["native_qualified"] is False
    assert decision["source_execution_permitted"] is False
    assert decision["production_eligible"] is False


def test_empty_registry_never_permits_execution():
    # 등록부가 비면 정상 관측이라도 실행을 허용해 버리는 회귀를 막는다.
    document = subject()
    decision = evaluate(
        document,
        registry_document={"schema": "qwq.source-runtime-registry/v1", "entries": []},
    )
    assert decision["status"] == "UNSUPPORTED"
    assert decision["errors"] == ["SUBJECT_UNREGISTERED"]
    assert decision["declared_state"] is None
    assert_offline_only(decision)


def test_matching_declarations_are_offline_only():
    # 일치 선언이 native qualification으로 승격되는 회귀를 막는다.
    decision = evaluate()
    assert decision["status"] == "CONTRACT_MATCH"
    assert decision["errors"] == []
    assert decision["declared_state"] == "BOOTSTRAP_ALLOWED"
    assert_offline_only(decision)


def test_subject_digest_is_canonical_and_excludes_registry_review_history():
    # 사후 registry review 변경이 subject 식별자를 바꾸는 자기참조 회귀를 막는다.
    document = subject()
    reordered = json.loads(canonical(document))
    assert subject_digest(document) == subject_digest(reordered)
    digest_before = subject_digest(document)
    changed = copy.deepcopy(document)
    changed["source_files"][changed["roles"]["helper"]] = H("e")
    assert subject_digest(changed) != digest_before
    first = evaluate(document, registered_state="QUALIFIED", mode="required")
    second_registry = registry(document, "QUALIFIED")
    second_registry["entries"][0]["review_sha256"] = H("d")
    second = evaluate(document, registered_state="QUALIFIED", mode="required", registry_document=second_registry)
    assert first["status"] == second["status"] == "CONTRACT_MATCH"
    assert subject_digest(document) == digest_before


@pytest.mark.parametrize(
    "role",
    ["interpreter", "libpython", "sqlite_extension", "sqlite_library", "loader", "cases", "guard", "controller"],
)
def test_changed_file_identity_is_rejected(role):
    # 역할이 가리키는 파일 hash가 달라도 통과하는 회귀를 막는다.
    document = subject()
    observed = observation(document)
    field = "runtime_files" if role in {"interpreter", "libpython", "sqlite_extension", "sqlite_library", "loader"} else "source_files"
    observed[field][document["roles"][role]] = H("e")
    decision = evaluate(document, observation_document=observed)
    assert decision["status"] == "REJECTED"
    assert decision["errors"] == ["RUNTIME_FILES_MISMATCH" if field == "runtime_files" else "SOURCE_FILES_MISMATCH"]


@pytest.mark.parametrize("mutation, expected", [
    (lambda observed: observed["runtime_files"].update({"extra/file.py": H("e")}), "RUNTIME_FILES_MISMATCH"),
    (lambda observed: observed["source_files"].pop("source/helper.py"), "SOURCE_FILES_MISMATCH"),
    (lambda observed: observed.update({"profile_sha256": H("e")}), "PROFILE_MISMATCH"),
    (lambda observed: observed["provenance"].update({"build_recipe": H("e")}), "PROVENANCE_MISMATCH"),
    (lambda observed: observed.update({"subject_sha256": H("e")}), "SUBJECT_MISMATCH"),
])
def test_observation_mismatches_are_rejected(mutation, expected):
    # 관측의 일부가 subject와 달라도 허용하는 회귀를 막는다.
    document = subject()
    observed = observation(document)
    mutation(observed)
    decision = evaluate(document, observation_document=observed)
    assert decision["status"] == "REJECTED"
    assert decision["errors"] == [expected]


@pytest.mark.parametrize("state, mode, status, error", [
    ("BOOTSTRAP_ALLOWED", "required", "REJECTED", "MODE_STATE_MISMATCH"),
    ("QUALIFIED", "bootstrap", "REJECTED", "MODE_STATE_MISMATCH"),
    ("UNKNOWN", "bootstrap", "UNSUPPORTED", "STATE_UNSUPPORTED"),
    ("RETIRED", "required", "UNSUPPORTED", "STATE_UNSUPPORTED"),
])
def test_registry_state_and_mode_boundaries(state, mode, status, error):
    # 유리한 mode/state 조합으로 대체 허용하는 회귀를 막는다.
    decision = evaluate(registered_state=state, mode=mode)
    assert decision["status"] == status
    assert decision["errors"] == [error]
    assert decision["declared_state"] == state
    assert_offline_only(decision)


def test_rejection_precedes_unregistered_or_state_unsupported_and_malformed_hides_state():
    # invalid 관측을 빈/UNKNOWN registry가 UNSUPPORTED로 가리는 회귀를 막는다.
    document = subject()
    invalid_observation = b'{"schema":"qwq.source-runtime-observation/v1","schema":"duplicate"}'
    empty = canonical({"schema": "qwq.source-runtime-registry/v1", "entries": []})
    result = evaluate_runtime_contract(
        canonical(document), empty, invalid_observation, mode="bootstrap", binding=binding(empty)
    )
    assert result["status"] == "REJECTED"
    assert result["errors"] == ["INVALID_OBSERVATION"]
    assert result["declared_state"] is None

    unknown = registry(document, "UNKNOWN")
    unknown_raw = canonical(unknown)
    broken_registry = unknown_raw.replace(b'"UNKNOWN"', b'"UNKNOWN","state":"RETIRED"')
    result = evaluate_runtime_contract(
        canonical(document), broken_registry, canonical(observation(document)),
        mode="bootstrap", binding=binding(broken_registry),
    )
    assert result["status"] == "REJECTED"
    assert result["errors"] == ["INVALID_REGISTRY"]
    assert result["declared_state"] is None


def test_binding_is_exact_and_binding_mismatch_is_rejected():
    # 서로 다른 revision 또는 registry bytes를 binding으로 수용하는 회귀를 막는다.
    document = subject()
    registry_raw = canonical(registry(document))
    mismatched_revision = binding(registry_raw)
    mismatched_revision["observed_revision"] = "b" * 40
    result = evaluate_runtime_contract(
        canonical(document), registry_raw, canonical(observation(document)),
        mode="bootstrap", binding=mismatched_revision,
    )
    assert result["status"] == "REJECTED"
    assert result["errors"] == ["REGISTRY_BINDING_MISMATCH"]

    wrong_hash = binding(registry_raw)
    wrong_hash["expected_sha256"] = H("b")
    result = evaluate_runtime_contract(
        canonical(document), registry_raw, canonical(observation(document)),
        mode="bootstrap", binding=wrong_hash,
    )
    assert result["status"] == "REJECTED"
    assert result["errors"] == ["REGISTRY_BINDING_MISMATCH"]


@pytest.mark.parametrize("raw", [
    b'{"schema":"qwq.source-runtime-subject/v1","schema":"duplicate"}',
    b'{"schema":"qwq.source-runtime-subject/v1","value":NaN}',
    b'{"schema":"qwq.source-runtime-subject/v1","value":"\\ud800"}',
    b"\xff",
    b"{}" + b" " * (1024 * 1024 + 1),
])
def test_parser_rejects_noncanonical_or_oversize_document(raw):
    # JSON 파서가 duplicate/NaN/surrogate/크기초과를 흘려보내는 회귀를 막는다.
    with pytest.raises(RuntimeContractError) as error:
        parse_runtime_document(raw, kind="subject")
    assert str(error.value) == "INVALID_RUNTIME_DOCUMENT"


def test_parser_enforces_path_utf8_boundaries_and_container_depth():
    # path와 JSON depth 경계가 byte 단위가 아닌 문자 수로 처리되는 회귀를 막는다.
    document = subject()
    component_128 = "가" * 42 + "aa"
    exact_path = "/".join([component_128, component_128, component_128, "가" * 41 + "aa"])
    assert len(exact_path.encode("utf-8")) == 512
    document["source_files"][exact_path] = document["source_files"].pop("source/helper.py")
    document["roles"]["helper"] = exact_path
    assert parse_runtime_document(canonical(document), kind="subject")["roles"]["helper"] == exact_path
    document["source_files"][exact_path + "x"] = document["source_files"].pop(exact_path)
    document["roles"]["helper"] = exact_path + "x"
    with pytest.raises(RuntimeContractError):
        parse_runtime_document(canonical(document), kind="subject")

    nested = "{}"
    for _ in range(10):
        nested = '{"x":' + nested + "}"
    with pytest.raises(RuntimeContractError):
        parse_runtime_document(nested.encode(), kind="subject")


def test_parser_enforces_file_map_and_registry_entry_boundaries():
    # file map/entry 상한을 한 칸 넘겨도 받아들이는 회귀를 막는다.
    document = subject()
    for index in range(4088):
        document["source_files"][f"generated/{index}"] = H("e")
    assert len(document["source_files"]) == 4096
    assert parse_runtime_document(canonical(document), kind="subject")["schema"] == (
        "qwq.source-runtime-subject/v1"
    )
    document["source_files"]["generated/overflow"] = H("e")
    with pytest.raises(RuntimeContractError):
        parse_runtime_document(canonical(document), kind="subject")

    document = subject()
    registry_document = registry(document)
    entry = registry_document["entries"][0]
    registry_document["entries"] = [
        {**entry, "subject_sha256": f"{index:064x}"} for index in range(256)
    ]
    assert parse_runtime_document(canonical(registry_document), kind="registry")["entries"][255][
        "subject_sha256"
    ] == f"{255:064x}"
    registry_document["entries"].append({**entry, "subject_sha256": H("e")})
    with pytest.raises(RuntimeContractError):
        parse_runtime_document(canonical(registry_document), kind="registry")


@pytest.mark.parametrize("mutator", [
    lambda value: value["profile"].update({"runtime_readonly": 1}),
    lambda value: value["roles"].update({"unknown": "source/helper.py"}),
    lambda value: value["source_files"].update({"../escape.py": H("e")}),
    lambda value: value["runtime_files"].update({"bin\\python": H("e")}),
    lambda value: value["runtime"].update({"version": "3.12.4"}),
])
def test_subject_schema_rejects_exact_type_unknown_role_and_noncanonical_path(mutator):
    # 느슨한 타입, 미등록 role, traversal/version fallback을 허용하는 회귀를 막는다.
    document = subject()
    mutator(document)
    with pytest.raises(RuntimeContractError) as error:
        parse_runtime_document(canonical(document), kind="subject")
    assert str(error.value) == "INVALID_RUNTIME_DOCUMENT"


def test_subject_digest_rejects_direct_cycles_and_subclassed_binding():
    # cycle/subclass가 일반 dict처럼 통과하는 회귀를 막는다.
    cyclic = subject()
    cyclic["platform"]["cycle"] = cyclic
    with pytest.raises(RuntimeContractError):
        subject_digest(cyclic)

    class Binding(dict):
        pass

    document = subject()
    raw = canonical(registry(document))
    result = evaluate_runtime_contract(
        canonical(document), raw, canonical(observation(document)), mode="bootstrap", binding=Binding(binding(raw))
    )
    assert result["status"] == "REJECTED"
    assert result["errors"] == ["INVALID_BINDING"]


def test_public_api_rejects_byte_and_dict_subclasses():
    # bytes/dict subclass를 exact input type으로 오인하는 회귀를 막는다.
    class Raw(bytes):
        pass

    class Document(dict):
        pass

    with pytest.raises(RuntimeContractError):
        parse_runtime_document(Raw(canonical(subject())), kind="subject")
    with pytest.raises(RuntimeContractError):
        subject_digest(Document(subject()))


def test_duplicate_registry_subject_and_actual_empty_registry_file_are_rejected_or_empty():
    # 중복 entry를 유리한 state로 선택하거나 실제 빈 registry를 바꾸는 회귀를 막는다.
    document = subject()
    duplicated = registry(document)
    duplicated["entries"].append(copy.deepcopy(duplicated["entries"][0]))
    decision = evaluate(document, registry_document=duplicated)
    assert decision["status"] == "REJECTED"
    assert decision["errors"] == ["INVALID_REGISTRY"]

    actual = json.loads((ROOT / "docs/verification/source-runtime-registry.json").read_text("utf-8"))
    assert actual == {"schema": "qwq.source-runtime-registry/v1", "entries": []}
