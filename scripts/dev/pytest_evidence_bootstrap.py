"""고정 격리 가드를 설치한 뒤 기존 pytest receipt 생산자를 호출한다."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import ModuleType


GUARD_HASH = "7b7b26940309a2a165d9bdba7611b6730e83b90ae9ea5f9e725764ee4c0a23f7"
PYTEST_ARGS = ["-q", "-p", "no:cacheprovider", "-p", "pytest_asyncio.plugin",
               "-p", "pytest_cov.plugin", "-p", "anyio.pytest_plugin", "--tb=short"]


def _load(name, path, source=None):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    exec(compile(path.read_bytes() if source is None else source, str(path), "exec"), module.__dict__)
    return module


def _guard(root: Path, *, install=False) -> dict:
    """양쪽 소유자가 같은 실제 모듈 객체와 파일 지문을 관측한다."""
    path = root / "tests/conftest.py"
    source = path.read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    if digest != GUARD_HASH:
        raise ValueError("PROCESS_GUARD")
    modules = {}
    for name, module in tuple(sys.modules.items()):
        named = name.rsplit(".", 1)[-1] == "conftest"
        if ModuleType not in type(module).__mro__:
            if named:
                raise ValueError("PROCESS_GUARD")
            continue
        attrs = ModuleType.__getattribute__(module, "__dict__")
        filename = attrs.get("__file__")
        if named and (type(filename) is not str or Path(filename).resolve() != path):
            raise ValueError("PROCESS_GUARD")
        if isinstance(filename, str) and Path(filename).resolve() == path:
            modules[id(module)] = attrs
        elif isinstance(filename, str) and Path(filename).name == "conftest.py":
            raise ValueError("PROCESS_GUARD")
    if install and not modules:
        if "conftest" in sys.modules:
            raise ValueError("PROCESS_GUARD")
        _load("conftest", path, source)
        return _guard(root)
    if len(modules) != 1:
        raise ValueError("PROCESS_GUARD")
    violations = next(iter(modules.values())).get("VIOLATIONS")
    if type(violations) is not list:
        raise ValueError("PROCESS_GUARD")
    return {"path": "tests/conftest.py", "sha256": digest,
            "module_count": 1, "violations": len(violations)}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    control = None
    try:
        b1 = bool(args and args[0] == "--b1-standard-v1")
        if b1:
            args = args[1:]
        if any(value.startswith("--b1-") or (b1 and value.startswith("-")) for value in args):
            raise ValueError
        if len(args) < 4:
            raise ValueError
        control = int(args[0])
        root = Path(__file__).resolve().parents[2]
        if Path.cwd() != root:
            raise ValueError
        guard = _guard(root, install=True)
        if guard["violations"]:
            raise ValueError
        frame = json.dumps({"schema": "qwq.pytest-guard-ready/v1", "guard": guard},
                           separators=(",", ":")).encode() + b"\n"
        if os.write(control, frame) != len(frame):
            raise OSError
        os.close(control)
        control = None
        sys.path.insert(0, str(root))
        producer = _load("_qwq_evidence_producer", root / "scripts/dev/pytest_evidence.py")
        pytest_args = (["-x"] if b1 else []) + PYTEST_ARGS
        rc = producer.run_with_evidence(pytest_args + args[3:],
                                       context_path=Path(args[1]), output_path=Path(args[2]))
        if _guard(root) != guard:
            return rc if rc != 0 else 125
        return rc
    except (OSError, ValueError, TypeError, ImportError):
        return 125
    finally:
        if control is not None:
            os.close(control)


if __name__ == "__main__":
    raise SystemExit(main())
