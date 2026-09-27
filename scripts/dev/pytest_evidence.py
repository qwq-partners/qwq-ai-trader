"""실제 pytest 관측 receipt를 생성한다. runtime 자격이나 OS 종료 증거는 아니다.

완주 발행은 pytest.main 반환 뒤에만 한다. sessionfinish 뒤 unconfigure의
실패도 정상 receipt로 만들지 않으며 pytest의 phase/marker/exitstatus는 바꾸지 않는다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sys
import sysconfig
import time


MAX_BYTES = 32 * 1024 * 1024
MAX_NODES = 20_000
GUARD_PATH = "tests/conftest.py"


class _EvidenceError(ValueError):
    """원문 대신 고정 코드만 외부에 반환한다."""


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _EvidenceError("EVIDENCE_CONTEXT")
        result[key] = value
    return result


def _bad_constant(_value):
    raise _EvidenceError("EVIDENCE_CONTEXT")


def _read_context(path: Path) -> tuple[dict, bytes]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                         parse_constant=_bad_constant)
        if type(doc) is not dict or set(doc) != {"run", "slot"}:
            raise ValueError
        run, slot = doc["run"], doc["slot"]
        if type(run) is not dict or set(run) != {"event", "sha", "tree", "contract", "run_id", "attempt"}:
            raise ValueError
        if type(slot) is not dict or set(slot) != {"lane", "timezone"}:
            raise ValueError
        if run["event"] not in ("local", "pull_request", "push", "merge_group", "workflow_dispatch"):
            raise ValueError
        for field, length in (("sha", 40), ("tree", 40), ("contract", 64)):
            if type(run[field]) is not str or re.fullmatch(f"[0-9a-f]{{{length}}}", run[field]) is None:
                raise ValueError
        if type(run["run_id"]) is not str or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", run["run_id"]) is None:
            raise ValueError
        if type(run["attempt"]) is not int or run["attempt"] < 1:
            raise ValueError
        if slot["lane"] not in ("standard", "source-proof") or slot["timezone"] not in ("UTC", "Asia/Seoul"):
            raise ValueError
        return doc, raw
    except (OSError, ValueError, TypeError, RecursionError):
        raise _EvidenceError("EVIDENCE_CONTEXT") from None


def _check_timezone(expected: str) -> None:
    try:
        if os.environ.get("TZ") != expected or expected not in ("UTC", "Asia/Seoul"):
            raise ValueError
        time.tzset()
        # 2024-01-01T00:00:00Z: 역사적 서울 UTC offset과 DST의 영향을 배제한다.
        offset = time.localtime(1704067200).tm_gmtoff
        if offset != {"UTC": 0, "Asia/Seoul": 32400}[expected]:
            raise ValueError
    except (AttributeError, OSError, ValueError):
        raise _EvidenceError("EVIDENCE_TIMEZONE") from None


def _runtime_hash() -> str:
    return hashlib.sha256(_canonical({
        "implementation": sys.implementation.name,
        "version": sys.version,
        "soabi": sysconfig.get_config_var("SOABI"),
        "machine": platform.machine(),
        "executable_sha256": _file_hash(Path(sys.executable)),
    })).hexdigest()


class _Observer:
    """pytest hook는 관측만 하며 발행이나 종료 코드 수정은 하지 않는다."""

    def __init__(self):
        self.root: Path | None = None
        self.collected: list[str] = []
        self.results: dict[str, dict] = {}
        self.collection_errors = 0
        self.deselected = 0
        self.invalid = False

    def pytest_configure(self, config):
        self.root = Path(config.rootpath).resolve()

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]
        if len(self.collected) > MAX_NODES or len(set(self.collected)) != len(self.collected):
            self.invalid = True
        for node in self.collected:
            if len(node.encode("utf-8")) > 2048:
                self.invalid = True
            self.results[node] = {"nodeid": node, "setup": "not_run",
                                  "call": "not_run", "teardown": "not_run"}

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors += 1

    def pytest_deselected(self, items):
        self.deselected += len(items)

    def pytest_runtest_logreport(self, report):
        if report.nodeid not in self.results or report.when not in ("setup", "call", "teardown"):
            self.invalid = True
            return
        result = self.results[report.nodeid]
        if result[report.when] != "not_run":
            self.invalid = True
        outcome = report.outcome
        if hasattr(report, "wasxfail"):
            outcome = "xfailed" if report.skipped else "xpassed" if report.passed else outcome
        elif report.failed and isinstance(report.longrepr, str) and report.longrepr.startswith("[XPASS(strict)] "):
            outcome = "xpassed"
        if outcome not in ("passed", "failed", "skipped", "xfailed", "xpassed"):
            self.invalid = True
        result[report.when] = outcome

    def guard(self) -> dict:
        if self.root is None:
            raise _EvidenceError("EVIDENCE_SESSION")
        canonical = (self.root / GUARD_PATH).resolve()
        modules = {}
        for module in tuple(sys.modules.values()):
            # 모듈별 __getattr__ 부작용을 호출하지 않는다.
            try:
                attrs = vars(module)
            except TypeError:
                continue
            path = attrs.get("__file__")
            if isinstance(path, str) and Path(path).resolve() == canonical:
                modules[id(module)] = attrs
        violations = 0
        for attrs in modules.values():
            recorded = attrs.get("VIOLATIONS")
            if not isinstance(recorded, list):
                raise _EvidenceError("EVIDENCE_GUARD")
            violations += len(recorded)
        return {"path": GUARD_PATH, "sha256": _file_hash(canonical),
                "module_count": len(modules), "violations": violations}


def _publish(output: Path, context_path: Path, root: Path, receipt: dict) -> None:
    try:
        parent = output.parent.resolve(strict=True)
        if not parent.is_relative_to(root) or output.resolve() == context_path:
            raise ValueError
        raw = _canonical(receipt)
        if len(raw) > MAX_BYTES:
            raise ValueError
        # 부모 resolve 후 최종 파일은 exclusive create: 기존 파일/링크를 덮지 않는다.
        with (parent / output.name).open("xb") as stream:
            stream.write(raw)
    except (OSError, ValueError, TypeError):
        raise _EvidenceError("EVIDENCE_OUTPUT") from None


def _error(code: str, pytest_rc: int = 0) -> int:
    print(code, file=sys.stderr)
    return pytest_rc if pytest_rc != 0 else 2


def run_with_evidence(pytest_args: list[str], *, context_path: Path, output_path: Path) -> int:
    """실제 pytest rc를 보존한다. 사전/발행 실패는 원 rc가 0인 경우만 2다."""
    try:
        if (os.environ.get("PYTEST_ADDOPTS") or os.environ.get("PYTEST_PLUGINS")
                or any(arg.split("=", 1)[0] in ("--verification-context", "--verification-output") for arg in pytest_args)):
            raise _EvidenceError("EVIDENCE_ARGUMENTS")
        context_path = Path(context_path).resolve()
        output_path = Path(output_path).absolute()
        context, context_raw = _read_context(context_path)
        _check_timezone(context["slot"]["timezone"])
        runtime = _runtime_hash()
        producer = _file_hash(Path(__file__))
    except _EvidenceError as exc:
        return _error(str(exc))
    except (OSError, ValueError, TypeError):
        return _error("EVIDENCE_IDENTITY")

    import pytest

    observer = _Observer()
    try:
        rc = int(pytest.main(pytest_args, plugins=[observer]))
    except BaseException:
        # unconfigure와 late hook 예외도 완주 receipt를 남기지 않는다.
        return _error("EVIDENCE_PYTEST_EXCEPTION")
    try:
        _check_timezone(context["slot"]["timezone"])
        if _read_context(context_path)[1] != context_raw:
            raise _EvidenceError("EVIDENCE_CONTEXT")
        if observer.root is None or observer.invalid:
            raise _EvidenceError("EVIDENCE_SESSION")
        receipt = {
            "schema": "qwq.verification-receipt/v1", **context,
            "identity": {"runtime": runtime, "producer": producer,
                         "inventory": hashlib.sha256(_canonical(sorted(observer.collected))).hexdigest()},
            "collected": sorted(observer.collected),
            "results": [observer.results[node] for node in sorted(observer.collected)],
            "session": {"finished": True, "exit_code": rc,
                        "collection_errors": observer.collection_errors, "deselected": observer.deselected},
            "guard": observer.guard(),
        }
        _publish(output_path, context_path, observer.root, receipt)
    except _EvidenceError as exc:
        return _error(str(exc), rc)
    except (OSError, ValueError, TypeError):
        return _error("EVIDENCE_PUBLISH", rc)
    return rc


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse 기본 오류는 호출자가 제공한 경로/원문을 echo한다.
        raise _EvidenceError("EVIDENCE_ARGUMENTS")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(allow_abbrev=False)
    parser.add_argument("--verification-context", type=Path, required=True)
    parser.add_argument("--verification-output", type=Path, required=True)
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        separator = args.index("--")
        owned, pytest_args = args[:separator], args[separator + 1:]
        flags = [arg.split("=", 1)[0] for arg in owned]
        if flags.count("--verification-context") != 1 or flags.count("--verification-output") != 1:
            raise _EvidenceError("EVIDENCE_ARGUMENTS")
        options = parser.parse_args(owned)
    except (ValueError, _EvidenceError):
        return _error("EVIDENCE_ARGUMENTS")
    return run_with_evidence(pytest_args, context_path=options.verification_context,
                             output_path=options.verification_output)


if __name__ == "__main__":
    raise SystemExit(main())
