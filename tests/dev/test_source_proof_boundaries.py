"""오프라인 경계 시험. native source 증명 대상의 원 함수는 실행하지 않는다."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "tests/proofs/l3_source/source_lease_cases.py"
HELPER = CASES.with_name("l3_source_lease_probe.py")
GUARD_SHA = "7b7b26940309a2a165d9bdba7611b6730e83b90ae9ea5f9e725764ee4c0a23f7"
GUARD = {"path": "tests/conftest.py", "sha256": GUARD_SHA,
         "module_count": 1, "violations": 0}
RECORD = {"case": "source_fault", "fault": True, "worker_settled": True,
          "follower_queued": True, "exit_drain_observed": True,
          "post_fault_turn": True, "same_slot": True, "lock_held": True,
          "cleanup_completed": False, "follower_entered": False}


def child(code, *arguments):
    assert CASES.is_file(), "source cases boundary is missing"
    return subprocess.run(
        [sys.executable, "-c", code, str(CASES), *arguments],
        cwd=ROOT, text=True, capture_output=True, timeout=20,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": "UTC",
             "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
             "PYTHONPATH": os.pathsep.join((str(ROOT / "tests"), str(ROOT)))},
    )


LOAD = '''
import importlib.util, json, sys, types
from pathlib import Path
cases_path = Path(sys.argv[1])
root = cases_path.parents[3]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
guard = load("conftest", root / "tests/conftest.py")
'''


def test_helper_bytes_preserve_the_frozen_native_subject():
    # helper 변경으로 기존 조건부 증명의 대상이 바뀌는 결함을 잡는다.
    assert HELPER.is_file(), "frozen source helper is missing"
    assert hashlib.sha256(HELPER.read_bytes()).hexdigest() == (
        "32725179ac8695fdb8dcb87b969159a306899c238bac355e1050c9e607e1e55e")


@pytest.mark.parametrize("arguments, expected", [
    (["--source-guard-child"], "guard"),
    (["--source-fault-child", "must-not-open.sqlite3", "foreign", "none"],
     "source_proof_native_subject_unqualified"),
    ([], "source_proof_invalid_arguments"),
    (["--source-guard-child", "extra"], "source_proof_invalid_arguments"),
    (["--source-fault-child", "secret-path", "foreign"], "source_proof_invalid_arguments"),
    (["--source-fault-child", "secret-path", "foreign", "none", "extra"],
     "source_proof_invalid_arguments"),
    (["--unknown"], "source_proof_invalid_arguments"),
])
def test_direct_modes_refuse_before_product_or_helper_import(arguments, expected):
    result = child('''
import importlib.abc, json, runpy, sys
calls = []
class Sentinel(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "src" or fullname.startswith("src.") or "l3_source_lease_probe" in fullname:
            calls.append(fullname)
            raise AssertionError("product_or_helper_imported")
sys.meta_path.insert(0, Sentinel())
sys.argv = sys.argv[1:]
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
except SystemExit as error:
    if type(error.code) is str:
        print(error.code, file=sys.stderr)
        raise SystemExit(1)
    raise
finally:
    print("IMPORT_CALLS=" + json.dumps(calls), file=sys.stderr)
''', *arguments)
    assert result.stderr.endswith("IMPORT_CALLS=[]\n"), result.stderr
    if expected == "guard":
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"schema": "qwq.source-guard/v1", "guard": GUARD}
        assert len(result.stdout.splitlines()) == 1
    else:
        assert result.returncode != 0
        assert result.stdout == ""
        assert result.stderr == expected + "\nIMPORT_CALLS=[]\n"


@pytest.mark.parametrize("corruption", [
    "hash", "wrong_path", "duplicate", "non_list", "nonempty", "missing",
    "getattr", "helper_wrong_path", "helper_duplicate", "subclass_duplicate",
])
def test_import_rejects_untrusted_guard_or_helper_identity(corruption):
    result = child(LOAD + '''
mode = sys.argv[2]
getattr_calls = []
if mode == "hash":
    original = Path.read_bytes
    def read_bytes(path):
        if path == root / "tests/conftest.py":
            return b"altered guard bytes"
        return original(path)
    Path.read_bytes = read_bytes
elif mode == "wrong_path":
    guard.__file__ = str(root / "wrong/conftest.py")
elif mode in ("duplicate", "subclass_duplicate"):
    class DerivedModule(types.ModuleType):
        pass
    module_type = DerivedModule if mode == "subclass_duplicate" else types.ModuleType
    other = module_type("duplicate")
    other.__dict__.update(guard.__dict__)
    sys.modules["duplicate_guard"] = other
elif mode == "non_list":
    guard.VIOLATIONS = ()
elif mode == "nonempty":
    guard.VIOLATIONS = ["synthetic"]
elif mode == "missing":
    del sys.modules["conftest"]
elif mode == "getattr":
    del guard.VIOLATIONS
    def forbidden(name):
        getattr_calls.append(name)
        raise AssertionError("module_getattr_called")
    guard.__getattr__ = forbidden
elif mode == "helper_wrong_path":
    other = types.ModuleType("l3_source_lease_probe")
    other.__file__ = str(root / "wrong/l3_source_lease_probe.py")
    sys.modules["l3_source_lease_probe"] = other
elif mode == "helper_duplicate":
    helper = load("l3_source_lease_probe", cases_path.with_name("l3_source_lease_probe.py"))
    other = types.ModuleType("duplicate_helper")
    other.__dict__.update(helper.__dict__)
    sys.modules["duplicate_helper"] = other
try:
    load("boundary_cases", cases_path)
except RuntimeError as exc:
    print(str(exc))
else:
    raise AssertionError("untrusted_module_accepted")
assert not getattr_calls
''', corruption)
    assert result.returncode == 0, result.stderr
    expected = "source_proof_helper_invalid" if corruption.startswith("helper") else "source_proof_guard_invalid"
    assert result.stdout == expected + "\n"


def test_existing_exact_guard_is_reused_and_reobserved():
    result = child(LOAD + '''
module = load("boundary_cases", cases_path)
before = module._bind_exact_guard()
sys.modules["same_guard_alias"] = guard
assert module._bind_exact_guard() == before
guard.VIOLATIONS.append("synthetic")
try:
    module._bind_exact_guard()
except RuntimeError as exc:
    assert str(exc) == "source_proof_guard_invalid"
else:
    raise AssertionError("stale_guard_evidence")
print(json.dumps(before))
''')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == GUARD


def envelope():
    return {"schema": "qwq.source-fault/v1", "guard": dict(GUARD), "record": dict(RECORD)}


@pytest.mark.parametrize("kind", ["text", "bytes", "exact_limit"])
def test_fault_envelope_returns_preserved_inner_record(kind):
    raw = json.dumps(envelope(), separators=(",", ":"))
    if kind == "exact_limit":
        raw += " " * (4096 - len(raw.encode("utf-8")))
    result = child(LOAD + '''
module = load("boundary_cases", cases_path)
raw = sys.argv[2]
if sys.argv[3] == "bytes":
    raw = raw.encode("utf-8")
print(json.dumps(module.parse_fault_envelope(raw)))
''', raw, kind)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == RECORD


@pytest.mark.parametrize("corruption", [
    "duplicate_outer", "duplicate_guard", "duplicate_record", "extra_outer", "missing_outer",
    "extra_guard", "missing_guard", "extra_record", "missing_record", "outer_list",
    "guard_list", "record_list", "schema", "path", "sha256", "count_zero", "count_two",
    "count_bool", "count_float", "violations_nonzero", "violations_bool", "violations_float",
    "inner_bool", "inner_case", "oversize", "utf8_size", "invalid_utf8", "invalid_type",
    "trailing_record", "empty",
])
def test_fault_envelope_rejects_corrupted_synthetic_evidence(corruption):
    data = envelope()
    if corruption.startswith("extra_") or corruption.startswith("missing_"):
        operation, target = corruption.split("_")
        target = data if target == "outer" else data[target]
        if operation == "extra":
            target["extra"] = False
        else:
            del target[next(iter(target))]
    updates = {
        "schema": (data, "schema", "qwq.source-guard/v1"),
        "path": (data["guard"], "path", "wrong/conftest.py"),
        "sha256": (data["guard"], "sha256", "0" * 64),
        "count_zero": (data["guard"], "module_count", 0),
        "count_two": (data["guard"], "module_count", 2),
        "count_bool": (data["guard"], "module_count", True),
        "count_float": (data["guard"], "module_count", 1.0),
        "violations_nonzero": (data["guard"], "violations", 1),
        "violations_bool": (data["guard"], "violations", False),
        "violations_float": (data["guard"], "violations", 0.0),
        "inner_bool": (data["record"], "fault", 1),
        "inner_case": (data["record"], "case", "wrong"),
        "guard_list": (data, "guard", []), "record_list": (data, "record", []),
    }
    if corruption in updates:
        target, key, value = updates[corruption]
        target[key] = value
    raw = json.dumps(data, separators=(",", ":"))
    if corruption.startswith("duplicate_"):
        key = {"duplicate_outer": "schema", "duplicate_guard": "path", "duplicate_record": "case"}[corruption]
        raw = raw.replace('"' + key + '":', '"' + key + '":null,"' + key + '":', 1)
    if corruption == "outer_list":
        raw = "[]"
    elif corruption == "oversize":
        raw += " " * 4096
    elif corruption == "utf8_size":
        raw += "한" * 1400
    elif corruption == "trailing_record":
        raw += "\n{}"
    elif corruption == "empty":
        raw = ""
    result = child(LOAD + '''
module = load("boundary_cases", cases_path)
raw = sys.argv[2]
if sys.argv[3] == "invalid_utf8":
    raw = b"\\xff"
elif sys.argv[3] == "invalid_type":
    raw = {}
try:
    module.parse_fault_envelope(raw)
except (AssertionError, ValueError):
    print("rejected")
else:
    raise AssertionError("invalid_envelope_accepted")
''', raw, corruption)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "rejected\n"


def test_unqualified_pytest_setup_precedes_every_function_fixture_and_native_call():
    # setup_module 제거 시 sentinel이 기록 후 즉시 raise하고 원 함수는 실행하지 않는다.
    result = child('''
import contextlib, io, json, sqlite3, sys
import pytest
calls, reports = [], []
def stop(name):
    calls.append(name)
    raise AssertionError("forbidden_subject_entry")
def connect(*args, **kwargs):
    stop("sqlite_connect")
sqlite3.connect = connect
class Sentinel:
    def pytest_collection_modifyitems(self, items):
        assert len(items) == 1
        module = items[0].module
        functions = [module.seed, module.verified_connections.__wrapped__,
                     module.probe._read_source, module.probe._native_execute,
                     module.probe._native_snapshot, module.probe._retire_native,
                     module.probe.start_source_probe]
        fixture = module.native_connection_provenance
        original = getattr(fixture, "_fixture_function", None)
        if original is None:
            original = fixture.__wrapped__
        functions.append(original)
        codes = {function.__code__: function.__name__ for function in functions}
        def profile(frame, event, arg):
            if event == "call" and frame.f_code in codes:
                stop(codes[frame.f_code])
        sys.setprofile(profile)
    def pytest_fixture_setup(self, fixturedef, request):
        if fixturedef.scope == "function":
            stop("function_fixture:" + fixturedef.argname)
    def pytest_runtest_logreport(self, report):
        reports.append([report.when, report.outcome, str(report.longrepr)])
capture = io.StringIO()
try:
    with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
        rc = pytest.main([sys.argv[1] + "::test_page_head_move_counts_live_edges_separately_from_removed_work[0]",
                          "-q", "-p", "no:cacheprovider", "--tb=short"], plugins=[Sentinel()])
finally:
    sys.setprofile(None)
guards = {id(value): value for value in tuple(sys.modules.values())
          if type(value) is type(sys) and value.__dict__.get("__file__")
          and str(value.__dict__["__file__"]).endswith("/tests/conftest.py")}
assert len(guards) == 1
assert next(iter(guards.values())).__dict__["VIOLATIONS"] == []
print(json.dumps({"rc": int(rc), "calls": calls, "reports": reports,
                  "output": capture.getvalue()}))
''')
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["rc"] == 1, evidence["output"]
    assert evidence["calls"] == []
    setup = [report for report in evidence["reports"] if report[0] == "setup"]
    assert len(setup) == 1 and setup[0][1] == "failed"
    assert "source_proof_native_subject_unqualified" in setup[0][2]
    assert not any(report[0] == "call" for report in evidence["reports"])
