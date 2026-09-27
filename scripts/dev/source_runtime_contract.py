"""순수 runtime admission 선언 대조 계약.

이 모듈은 파일을 읽거나 실행 환경을 관측하지 않는다. 입력 bytes의 선언만
엄격히 검증하고 서로 대조하며, 어떤 결과에도 실행 권한을 부여하지 않는다.
"""

import hashlib
import json
import re


_MAX_DOCUMENT_BYTES = 1024 * 1024
_MAX_DEPTH = 10
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_OS_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SUBJECT_SCHEMA = "qwq.source-runtime-subject/v1"
_REGISTRY_SCHEMA = "qwq.source-runtime-registry/v1"
_OBSERVATION_SCHEMA = "qwq.source-runtime-observation/v1"
_DECISION_SCHEMA = "qwq.source-runtime-decision/v1"

_PROVENANCE_KEYS = frozenset((
    "cpython_source", "cpython_patches", "sqlite_source", "sqlite_patches",
    "build_recipe", "dependency_lock",
))
_RUNTIME_ROLES = frozenset((
    "interpreter", "libpython", "sqlite_extension", "sqlite_library", "loader",
))
_SOURCE_ROLES = frozenset((
    "helper", "cases", "guard", "controller", "bootstrap", "inventory", "oracle",
    "dependency_manifest",
))


class RuntimeContractError(ValueError):
    """입력 선언이 이 오프라인 계약의 정규 형식이 아닐 때 발생한다."""


def _invalid():
    raise RuntimeContractError("INVALID_RUNTIME_DOCUMENT")


def _no_duplicate_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _reject_constant(_value):
    _invalid()


def _validate_tree(value, depth=1, active=None):
    """직접 전달된 객체와 JSON 결과의 exact type/UTF-8/depth를 확인한다."""
    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            _invalid()
        return
    if type(value) in (bool, int) or value is None:
        return
    if type(value) not in (dict, list):
        _invalid()
    if depth > _MAX_DEPTH:
        _invalid()
    if active is None:
        active = set()
    identity = id(value)
    if identity in active:
        _invalid()
    active.add(identity)
    try:
        if type(value) is dict:
            for key, item in value.items():
                if type(key) is not str:
                    _invalid()
                _validate_tree(key, depth, active)
                _validate_tree(item, depth + 1 if type(item) in (dict, list) else depth, active)
        else:
            for item in value:
                _validate_tree(item, depth + 1 if type(item) in (dict, list) else depth, active)
    finally:
        active.remove(identity)


def _exact_dict(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        _invalid()
    return value


def _string(value, maximum=None):
    if type(value) is not str:
        _invalid()
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        _invalid()
    if maximum is not None and len(encoded) > maximum:
        _invalid()
    return value


def _hash(value):
    value = _string(value)
    if _HASH_RE.fullmatch(value) is None:
        _invalid()
    return value


def _nullable_hash(value):
    if value is None:
        return None
    return _hash(value)


def _path(value):
    value = _string(value, 512)
    if not value or value.startswith("/") or "\\" in value or "\x00" in value:
        _invalid()
    for component in value.split("/"):
        if component in ("", ".", ".."):
            _invalid()
        if len(component.encode("utf-8")) > 128:
            _invalid()
    return value


def _file_map(value):
    if type(value) is not dict or not 1 <= len(value) <= 4096:
        _invalid()
    for path, digest in value.items():
        _path(path)
        _hash(digest)
    return value


def _provenance(value):
    value = _exact_dict(value, _PROVENANCE_KEYS)
    for digest in value.values():
        _hash(digest)
    return value


def _profile(value):
    value = _exact_dict(value, {
        "id", "kernel_policy_sha256", "runtime_readonly", "source_readonly",
        "network_enabled", "home_mounted", "credentials_mounted", "private_tmp",
    })
    if value["id"] != "linux-ro-network-off/v1":
        _invalid()
    _hash(value["kernel_policy_sha256"])
    expected = {
        "runtime_readonly": True,
        "source_readonly": True,
        "network_enabled": False,
        "home_mounted": False,
        "credentials_mounted": False,
        "private_tmp": True,
    }
    for key, wanted in expected.items():
        if type(value[key]) is not bool or value[key] is not wanted:
            _invalid()
    return value


def _validate_subject(value):
    value = _exact_dict(value, {
        "schema", "platform", "runtime", "provenance", "profile", "runtime_files",
        "source_files", "roles",
    })
    if value["schema"] != _SUBJECT_SCHEMA:
        _invalid()
    platform = _exact_dict(value["platform"], {"image_sha256", "os_id", "architecture"})
    _hash(platform["image_sha256"])
    if type(platform["os_id"]) is not str or _OS_ID_RE.fullmatch(platform["os_id"]) is None:
        _invalid()
    if platform["architecture"] != "x86_64":
        _invalid()
    runtime = _exact_dict(value["runtime"], {"implementation", "version", "soabi", "build_sha256"})
    if (runtime["implementation"], runtime["version"], runtime["soabi"]) != (
        "cpython", "3.12.3", "cpython-312-x86_64-linux-gnu",
    ):
        _invalid()
    _hash(runtime["build_sha256"])
    _provenance(value["provenance"])
    _profile(value["profile"])
    runtime_files = _file_map(value["runtime_files"])
    source_files = _file_map(value["source_files"])
    roles = _exact_dict(value["roles"], _RUNTIME_ROLES | _SOURCE_ROLES)
    for role in _RUNTIME_ROLES:
        path = _path(roles[role])
        if path not in runtime_files:
            _invalid()
    for role in _SOURCE_ROLES:
        path = _path(roles[role])
        if path not in source_files:
            _invalid()
    return value


def _validate_registry(value):
    value = _exact_dict(value, {"schema", "entries"})
    if value["schema"] != _REGISTRY_SCHEMA or type(value["entries"]) is not list:
        _invalid()
    entries = value["entries"]
    if len(entries) > 256:
        _invalid()
    subjects = set()
    for entry in entries:
        entry = _exact_dict(entry, {
            "subject_sha256", "state", "profile_sha256", "bootstrap_plan_sha256",
            "evidence_sha256", "review_sha256",
        })
        subject = _hash(entry["subject_sha256"])
        if subject in subjects:
            _invalid()
        subjects.add(subject)
        _hash(entry["profile_sha256"])
        if entry["state"] not in {"UNKNOWN", "BOOTSTRAP_ALLOWED", "QUALIFIED", "RETIRED"}:
            _invalid()
        bootstrap = _nullable_hash(entry["bootstrap_plan_sha256"])
        evidence = _nullable_hash(entry["evidence_sha256"])
        review = _nullable_hash(entry["review_sha256"])
        if entry["state"] in {"BOOTSTRAP_ALLOWED", "QUALIFIED"} and bootstrap is None:
            _invalid()
        if entry["state"] == "QUALIFIED" and (evidence is None or review is None):
            _invalid()
    return value


def _validate_observation(value):
    value = _exact_dict(value, {
        "schema", "subject_sha256", "profile_sha256", "runtime_files", "source_files",
        "provenance",
    })
    if value["schema"] != _OBSERVATION_SCHEMA:
        _invalid()
    _hash(value["subject_sha256"])
    _hash(value["profile_sha256"])
    _file_map(value["runtime_files"])
    _file_map(value["source_files"])
    _provenance(value["provenance"])
    return value


def parse_runtime_document(raw: bytes, *, kind: str) -> dict:
    """bytes 문서를 strict JSON으로 파싱하고 kind별 v1 schema를 검증한다."""
    if type(raw) is not bytes or type(kind) is not str or kind not in {
        "subject", "registry", "observation",
    } or len(raw) > _MAX_DOCUMENT_BYTES:
        _invalid()
    try:
        decoded = raw.decode("utf-8")
        value = json.loads(
            decoded, object_pairs_hook=_no_duplicate_object, parse_constant=_reject_constant
        )
        _validate_tree(value)
        if kind == "subject":
            return _validate_subject(value)
        if kind == "registry":
            return _validate_registry(value)
        return _validate_observation(value)
    except RuntimeContractError:
        raise
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
        _invalid()


def _canonical(value):
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        _invalid()


def subject_digest(subject: dict) -> str:
    """검증된 subject 전체 canonical JSON bytes의 SHA-256을 돌려준다."""
    if type(subject) is not dict:
        _invalid()
    try:
        _validate_tree(subject)
        _validate_subject(subject)
        canonical = _canonical(subject)
        if len(canonical) > _MAX_DOCUMENT_BYTES:
            _invalid()
        return hashlib.sha256(canonical).hexdigest()
    except RuntimeContractError:
        raise
    except (TypeError, ValueError, RecursionError):
        _invalid()


def _profile_digest(subject):
    return hashlib.sha256(_canonical(subject["profile"])).hexdigest()


def _validate_binding(binding):
    if type(binding) is not dict:
        raise ValueError
    _validate_tree(binding)
    binding = _exact_dict(binding, {"expected_revision", "observed_revision", "expected_sha256"})
    for key in ("expected_revision", "observed_revision"):
        value = _string(binding[key])
        if _REVISION_RE.fullmatch(value) is None:
            raise ValueError
    _hash(binding["expected_sha256"])
    return binding


def _decision(status, errors, mode, declared_state):
    return {
        "schema": _DECISION_SCHEMA,
        "status": status,
        "errors": sorted(set(errors)),
        "mode": mode,
        "declared_state": declared_state,
        "scope": "offline_runtime_contract_only",
        "registry_trust_verified": False,
        "native_qualified": False,
        "source_execution_permitted": False,
        "production_eligible": False,
    }


def evaluate_runtime_contract(subject_raw: bytes, registry_raw: bytes, observation_raw: bytes, *,
                              mode: str, binding: dict) -> dict:
    """세 선언과 caller binding의 일치만 판정하며, 실행 권한은 항상 false다."""
    errors = []
    parsed = {}
    for name, raw in (
        ("subject", subject_raw), ("registry", registry_raw), ("observation", observation_raw),
    ):
        try:
            parsed[name] = parse_runtime_document(raw, kind=name)
        except RuntimeContractError:
            errors.append("INVALID_" + name.upper())
    output_mode = mode if type(mode) is str and mode in {"bootstrap", "required"} else None
    if output_mode is None:
        errors.append("INVALID_MODE")
    try:
        checked_binding = _validate_binding(binding)
    except (RuntimeContractError, ValueError, TypeError):
        checked_binding = None
        errors.append("INVALID_BINDING")
    if errors:
        return _decision("REJECTED", errors, output_mode, None)

    if (checked_binding["expected_revision"] != checked_binding["observed_revision"] or
            hashlib.sha256(registry_raw).hexdigest() != checked_binding["expected_sha256"]):
        return _decision("REJECTED", ["REGISTRY_BINDING_MISMATCH"], output_mode, None)

    subject = parsed["subject"]
    observation = parsed["observation"]
    digest = subject_digest(subject)
    profile_digest = _profile_digest(subject)
    mismatches = []
    if observation["subject_sha256"] != digest:
        mismatches.append("SUBJECT_MISMATCH")
    if observation["profile_sha256"] != profile_digest:
        mismatches.append("PROFILE_MISMATCH")
    if observation["runtime_files"] != subject["runtime_files"]:
        mismatches.append("RUNTIME_FILES_MISMATCH")
    if observation["source_files"] != subject["source_files"]:
        mismatches.append("SOURCE_FILES_MISMATCH")
    if observation["provenance"] != subject["provenance"]:
        mismatches.append("PROVENANCE_MISMATCH")
    if mismatches:
        return _decision("REJECTED", mismatches, output_mode, None)

    entry = next(
        (item for item in parsed["registry"]["entries"] if item["subject_sha256"] == digest), None
    )
    if entry is None:
        return _decision("UNSUPPORTED", ["SUBJECT_UNREGISTERED"], output_mode, None)
    declared_state = entry["state"]
    if entry["profile_sha256"] != profile_digest:
        return _decision("REJECTED", ["PROFILE_MISMATCH"], output_mode, declared_state)
    if declared_state in {"UNKNOWN", "RETIRED"}:
        return _decision("UNSUPPORTED", ["STATE_UNSUPPORTED"], output_mode, declared_state)
    if ((declared_state == "BOOTSTRAP_ALLOWED" and output_mode != "bootstrap") or
            (declared_state == "QUALIFIED" and output_mode != "required")):
        return _decision("REJECTED", ["MODE_STATE_MISMATCH"], output_mode, declared_state)
    return _decision("CONTRACT_MATCH", [], output_mode, declared_state)
