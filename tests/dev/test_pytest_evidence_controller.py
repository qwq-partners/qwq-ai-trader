"""실제 프로세스 시험은 독립 회수자 아래에서만 실행한다."""

import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
WALL = 0x40000000
GUARD_HASH = "7b7b26940309a2a165d9bdba7611b6730e83b90ae9ea5f9e725764ee4c0a23f7"
CONTEXT = {"run": {"event": "local", "sha": "1" * 40, "tree": "2" * 40,
                    "contract": "3" * 64, "run_id": "os-fixture", "attempt": 1},
           "slot": {"lane": "standard", "timezone": "UTC"}}
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": "UTC",
       "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}


def _harness_child(repo, dummy):
    """fork 자식의 예외가 부모 finally/증거 발행으로 들어가지 못하게 한다."""
    try:
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.alarm(8)
        if dummy:
            child = os.fork()
            if child == 0:
                signal.alarm(8)
                os.setsid()
                time.sleep(7)
                os._exit(0)
            os._exit(7)
        os.chdir(repo)
        for target in (1, 2):
            out = os.open(str(repo / f"harness-child-{target}.log"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.dup2(out, target)
            os.close(out)
        args = json.loads((repo / "harness-args.json").read_text())
        os.execve(sys.executable, [sys.executable, "-I", "-B", str(repo / "scripts/dev/pytest_evidence_controller.py"), *args], ENV)
    finally:
        os._exit(95)


def _harness(repo, dummy=False, interrupt=False):
    """후보 함수에 의존하지 않는 전용 단일 회수자다."""
    stopped = [False]
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, lambda *_: stopped.__setitem__(0, True))
    signal.signal(signal.SIGINT, lambda *_: stopped.__setitem__(0, True))
    if len(os.listdir("/proc/self/task")) != 1:
        return 90
    try:
        os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT | WALL)
    except ChildProcessError:
        pass
    else:
        return 91
    libc = ctypes.CDLL(None, use_errno=True)
    value = ctypes.c_int()
    if libc.prctl(36, 1, 0, 0, 0) or libc.prctl(37, ctypes.byref(value), 0, 0, 0) or value.value != 1:
        return 92
    fd = os.pidfd_open(os.getpid())
    try:
        signal.pidfd_send_signal(fd, 0)
    finally:
        os.close(fd)
    started = time.monotonic()
    leader = None
    rc = None
    statuses = []
    complete = False
    metadata_ok = True

    def record(pid, status):
        nonlocal rc
        statuses.append([pid, os.waitstatus_to_exitcode(status)])
        if pid == leader:
            rc = os.waitstatus_to_exitcode(status)

    try:
        leader = os.fork()
        if leader == 0:
            _harness_child(repo, dummy)
        interrupted = False
        while rc is None and not stopped[0] and time.monotonic() - started < 12:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG | WALL)
                if pid:
                    record(pid, status)
            except ChildProcessError:
                break
            if interrupt and not interrupted and (repo / "receipt.json").exists():
                interrupt_fd = os.pidfd_open(leader)
                try:
                    pid, status = os.waitpid(leader, os.WNOHANG | WALL)
                    if pid:
                        record(pid, status)
                    else:
                        signal.pidfd_send_signal(interrupt_fd, signal.SIGTERM)
                        interrupted = True
                finally:
                    os.close(interrupt_fd)
            time.sleep(.01)
    finally:
        end = time.monotonic() + 3
        term_end = end - 2
        while time.monotonic() < end:
            try:
                for _ in range(64):
                    pid, status = os.waitpid(-1, os.WNOHANG | WALL)
                    if not pid:
                        break
                    record(pid, status)
            except ChildProcessError:
                complete = True
                break
            with open(f"/proc/self/task/{os.getpid()}/children", "rb") as stream:
                children_raw = stream.read(65537)
            if len(children_raw) > 65536:
                metadata_ok = False
            children = children_raw[:65536].split()
            for token in children[:64]:
                candidate = int(token)
                try:
                    owned_fd = os.pidfd_open(candidate)
                except ProcessLookupError:
                    continue
                try:
                    try:
                        pid, status = os.waitpid(candidate, os.WNOHANG | WALL)
                    except ChildProcessError:
                        continue
                    if pid:
                        record(pid, status)
                    else:
                        try:
                            signal.pidfd_send_signal(owned_fd, signal.SIGTERM if time.monotonic() < term_end else signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                finally:
                    os.close(owned_fd)
            time.sleep(.01)
        result = {"complete": complete and metadata_ok, "rc": rc, "statuses": statuses,
                  "elapsed": time.monotonic() - started}
        (repo / "harness-result.json").write_text(json.dumps(result))
    return 0 if complete and metadata_ok and rc is not None else 93


def run_case(repo, *, dummy=False, args=None, interrupt=False):
    """부모는 회수 하네스를 먼저 죽이지 않고 증거 누락이면 배치를 중단한다."""
    import pytest
    if args is None:
        args = ["--verification-context", "context.json", "--verification-output", "receipt.json",
                "--process-output", "process.json", "--timeout-seconds", "3", "--", "tests/test_tiny.py"]
    (repo / "harness-args.json").write_text(json.dumps(args))
    output = (repo / "harness.log").open("xb")
    child = subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).resolve()),
                              "--harness", str(repo), "dummy" if dummy else "interrupt" if interrupt else "controller"],
                             env=ENV, stdout=output, stderr=output)
    start = time.monotonic()
    sent = False
    while child.poll() is None and time.monotonic() - start < 20:
        if time.monotonic() - start >= 16 and not sent:
            fd = os.pidfd_open(child.pid)
            try:
                pid, status = os.waitpid(child.pid, os.WNOHANG | WALL)
                if pid:
                    child.returncode = os.waitstatus_to_exitcode(status)
                else:
                    signal.pidfd_send_signal(fd, signal.SIGTERM)
                    sent = True
            finally:
                os.close(fd)
        time.sleep(.01)
    output.close()
    path = repo / "harness-result.json"
    if child.returncode != 0 or not path.is_file():
        pytest.exit("독립 하네스 회수 증거 누락: 실제 프로세스 배치 중단", returncode=2)
    result = json.loads(path.read_text())
    if not result["complete"] or result["elapsed"] > 15.5:
        pytest.exit("독립 하네스 회수 기한/증거 실패", returncode=2)
    return result


def make_repo(tmp_path, body="def test_ok():\n    assert True\n"):
    """고정 guard/producer와 임시 안전 상태 모듈만 갖는 합성 저장소다."""
    repo = tmp_path / "repo"
    (repo / "scripts/dev").mkdir(parents=True)
    (repo / "tests").mkdir()
    for name in ("pytest_evidence.py", "pytest_evidence_bootstrap.py", "pytest_evidence_controller.py"):
        source = ROOT / "scripts/dev" / name
        if source.exists():
            (repo / "scripts/dev" / name).write_bytes(source.read_bytes())
    guard = (ROOT / "tests/conftest.py").read_bytes()
    assert hashlib.sha256(guard).hexdigest() == GUARD_HASH
    (repo / "tests/conftest.py").write_bytes(guard)
    for sub, name, content in (("risk", "kill_switch", "CACHE_DIR = None\ndef clear_cache():\n    pass\n"),
                               ("utils", "audit_log", "AUDIT_DIR = None\n")):
        directory = repo / "src" / sub
        directory.mkdir(parents=True)
        (directory / f"{name}.py").write_text(content)
    (repo / "tests/test_tiny.py").write_text(
        "import signal\nsignal.signal(signal.SIGALRM, signal.SIG_DFL)\nsignal.alarm(8)\n" + body)
    (repo / "context.json").write_text(json.dumps(CONTEXT))
    return repo


def test_00_independent_harness_reaps_failed_dummy(tmp_path):
    repo = make_repo(tmp_path)
    result = run_case(repo, dummy=True)
    assert result["rc"] == 7
    assert len(result["statuses"]) == 2
    assert result["complete"] is True


def test_controller_modules_exist():
    assert (ROOT / "scripts/dev/pytest_evidence_controller.py").is_file()
    assert (ROOT / "scripts/dev/pytest_evidence_bootstrap.py").is_file()


def test_real_pass_preserves_receipt_and_os_status(tmp_path):
    repo = make_repo(tmp_path)
    result = run_case(repo)
    assert result["rc"] == 0
    doc = json.loads((repo / "process.json").read_bytes())
    assert doc["process"]["returncode"] == 0
    assert doc["process"]["cleanup_complete"] is True
    assert doc["process"]["ownership_probe_passed"] is True
    assert doc["process"]["descendant_survived"] is False
    assert doc["process"]["reason"] == "exited"
    assert doc["receipt"]["sha256"] == hashlib.sha256((repo / "receipt.json").read_bytes()).hexdigest()
    assert doc["process"]["guard"] == doc["process"]["parent_guard"]


def _controller():
    spec = importlib.util.spec_from_file_location("_controller_test", ROOT / "scripts/dev/pytest_evidence_controller.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_failure_preserves_one(tmp_path):
    repo = make_repo(tmp_path, "def test_failure():\n    assert False\n")
    assert run_case(repo)["rc"] == 1
    assert json.loads((repo / "process.json").read_text())["process"]["returncode"] == 1


def test_real_receipt_then_exit_nine(tmp_path):
    repo = make_repo(tmp_path, "import atexit, os\ndef test_ok():\n    atexit.register(lambda: os._exit(9))\n")
    assert run_case(repo)["rc"] == 9
    doc = json.loads((repo / "process.json").read_text())
    assert doc["receipt"]["state"] == "regular"
    assert doc["process"]["returncode"] == 9
    assert doc["process"]["reason"] == "exited"


def test_real_receipt_then_signal(tmp_path):
    repo = make_repo(tmp_path, "import atexit, signal\ndef test_ok():\n    atexit.register(lambda: signal.raise_signal(signal.SIGTERM))\n")
    assert run_case(repo)["rc"] == 143
    doc = json.loads((repo / "process.json").read_text())
    assert doc["receipt"]["state"] == "regular"
    assert doc["process"]["returncode"] == -15
    assert doc["process"]["reason"] == "signaled"


def test_real_receipt_then_hang(tmp_path):
    repo = make_repo(tmp_path, "import atexit, time\ndef test_ok():\n    atexit.register(lambda: time.sleep(7))\n")
    assert run_case(repo)["rc"] == 124
    doc = json.loads((repo / "process.json").read_text())
    assert doc["receipt"]["state"] == "regular"
    assert doc["process"]["returncode"] == -15
    assert doc["process"]["reason"] == "timeout"
    assert doc["process"]["cleanup_complete"] is True


def test_real_detached_double_fork_is_rejected_and_reaped(tmp_path):
    repo = make_repo(tmp_path, '''import atexit, os, signal, time
def backstop():
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(8)
def spawn():
    backstop()
    if os.fork() == 0:
        backstop()
        os.setsid()
        if os.fork() == 0:
            backstop()
            time.sleep(7)
        os._exit(0)
def test_ok():
    atexit.register(spawn)
''')
    assert run_case(repo)["rc"] == 125
    doc = json.loads((repo / "process.json").read_text())
    assert doc["process"]["returncode"] == 0
    assert doc["process"]["descendant_survived"] is True
    assert doc["process"]["cleanup_complete"] is True
    assert doc["process"]["descendants_reaped"] == 2


def test_real_both_streams_flood_are_bounded(tmp_path):
    repo = make_repo(tmp_path, '''import atexit, os, signal
def flood():
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(8)
    for _ in range(150):
        os.write(1, b'x' * 65536)
        os.write(2, b'y' * 65536)
def test_ok():
    atexit.register(flood)
''')
    assert run_case(repo)["rc"] == 125
    doc = json.loads((repo / "process.json").read_text())
    assert doc["process"]["reason"] == "output_limit"
    assert doc["process"]["cleanup_complete"] is True
    assert any(item["overflow"] for item in doc["streams"].values())
    for name, item in doc["streams"].items():
        raw = (repo / f"process.json.{name}.log").read_bytes()
        assert len(raw) == item["bytes"] <= 8388609
        assert hashlib.sha256(raw).hexdigest() == item["sha256"]


def test_source_proof_is_rejected_before_launch(tmp_path):
    repo = make_repo(tmp_path)
    context = json.loads((repo / "context.json").read_text())
    context["slot"]["lane"] = "source-proof"
    (repo / "context.json").write_text(json.dumps(context))
    assert run_case(repo)["rc"] == 125
    assert not (repo / "receipt.json").exists()


def test_existing_output_is_not_overwritten(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "process.json").write_bytes(b"preserve")
    assert run_case(repo)["rc"] == 125
    assert (repo / "process.json").read_bytes() == b"preserve"
    assert not (repo / "receipt.json").exists()


def test_selection_escape_and_duplicate_are_rejected(tmp_path, monkeypatch):
    import pytest
    module = _controller()
    repo = make_repo(tmp_path)
    monkeypatch.setattr(module, "ROOT", repo)
    monkeypatch.chdir(repo)
    prefix = ["--verification-context", "context.json", "--verification-output", "receipt.json",
              "--process-output", "process.json", "--timeout-seconds", "3", "--"]
    for selection in (["../outside"], ["tests/test_tiny.py::test_ok"], ["tests/*"], ["-s"],
                      ["tests/test_tiny.py", "./tests/test_tiny.py"], ["tests/proofs"], []):
        with pytest.raises(ValueError):
            module._arguments(prefix + selection)


def test_pidfd_open_precedes_wait_and_no_signal_on_lost_authority(monkeypatch):
    module = _controller()
    owner = module._Owner()
    events = []
    monkeypatch.setattr(module.os, "pidfd_open", lambda *a: events.append("open") or 71)
    def lost(*args):
        events.append("wait")
        raise ChildProcessError
    monkeypatch.setattr(module.os, "waitpid", lost)
    monkeypatch.setattr(module.signal, "pidfd_send_signal", lambda *a: events.append("signal"))
    monkeypatch.setattr(module.os, "close", lambda fd: events.append("close"))
    owner.signal_candidate(123, signal.SIGTERM)
    assert events == ["open", "wait", "close"]
    assert owner.error == "cleanup_error"


def test_pidfd_zero_wait_grants_only_fd_signal_and_closes(monkeypatch):
    module = _controller()
    owner = module._Owner()
    events = []
    monkeypatch.setattr(module.os, "pidfd_open", lambda *a: events.append(("open", a)) or 71)
    monkeypatch.setattr(module.os, "waitpid", lambda *a: events.append(("wait", a)) or (0, 0))
    monkeypatch.setattr(module.signal, "pidfd_send_signal", lambda *a: events.append(("signal", a)))
    monkeypatch.setattr(module.os, "close", lambda *a: events.append(("close", a)))
    owner.signal_candidate(123, signal.SIGTERM)
    assert events == [("open", (123, 0)), ("wait", (123, os.WNOHANG | WALL)),
                      ("signal", (71, signal.SIGTERM, None, 0)), ("close", (71,))]
    assert owner.term is True


def test_raw_status_recorder_preserves_signal_and_postleader_zero(monkeypatch):
    module = _controller()
    owner = module._Owner()
    owner.leader = 12
    owner.record(12, 9)
    monkeypatch.setattr(module.os, "waitpid", lambda *a: (0, 0))
    assert owner.wait(-1) == "alive"
    assert owner.survived is True
    assert owner.returncode == -9
    owner.reject("timeout")
    owner.reject("cleanup_error")
    assert owner.error == "timeout"


def test_lost_leader_status_never_becomes_zero(monkeypatch):
    module = _controller()
    owner = module._Owner()
    owner.leader = 12
    def empty(*args):
        raise ChildProcessError
    monkeypatch.setattr(module.os, "waitpid", empty)
    assert owner.reap() is True
    assert owner.returncode is None
    assert owner.reaped is False
    assert owner.error == "cleanup_error"


def test_guard_frame_rejects_extra_bytes_duplicate_and_boolean():
    import pytest
    module = _controller()
    guard = {"path": "tests/conftest.py", "sha256": GUARD_HASH, "module_count": 1, "violations": 0}
    frame = json.dumps({"schema": "qwq.pytest-guard-ready/v1", "guard": guard}).encode() + b"\n"
    assert module._frame(frame, guard) == guard
    for raw in (b"", frame + b"x", frame + frame, frame.replace(b'"module_count": 1', b'"module_count": true'),
                frame.replace(GUARD_HASH.encode(), b"0" * 64), b"x" * 4097):
        with pytest.raises(ValueError):
            module._frame(raw, guard)


def test_receipt_symlink_and_fifo_never_block_or_become_regular(tmp_path):
    module = _controller()
    target = tmp_path / "target"
    target.write_bytes(b"receipt")
    link = tmp_path / "link"
    link.symlink_to(target)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    for path in (link, fifo):
        assert module._receipt(path) == {"state": "invalid", "bytes": 0, "sha256": None}


def test_caller_term_latches_interruption_and_reaps(tmp_path):
    repo = make_repo(tmp_path, "import atexit, time\ndef test_ok():\n    atexit.register(lambda: time.sleep(7))\n")
    assert run_case(repo, interrupt=True)["rc"] == 125
    doc = json.loads((repo / "process.json").read_text())
    assert doc["process"]["reason"] == "interrupted"
    assert doc["process"]["cleanup_complete"] is True
    assert doc["process"]["returncode"] == -15


def test_real_identity_change_after_receipt_is_rejected(tmp_path):
    repo = make_repo(tmp_path, '''import atexit
from pathlib import Path
def change():
    path = Path('scripts/dev/pytest_evidence_bootstrap.py')
    path.write_bytes(path.read_bytes() + b'\\n')
def test_ok():
    atexit.register(change)
''')
    assert run_case(repo)["rc"] == 125
    doc = json.loads((repo / "process.json").read_text())
    assert doc["process"]["reason"] == "identity_changed"
    assert doc["process"]["returncode"] == 0


def test_echild_does_not_discard_last_output_or_control(monkeypatch):
    import io
    module = _controller()
    owner = module._Owner()
    owner.leader = 12
    owner.record(12, 0)
    guard = {"path": "tests/conftest.py", "sha256": GUARD_HASH, "module_count": 1, "violations": 0}
    frame = json.dumps({"schema": "qwq.pytest-guard-ready/v1", "guard": guard}).encode() + b"\n"
    chunks = {31: [b"tail", b""], 32: [b"last", b""], 33: [frame, b"extra", b""]}
    def empty(*args):
        raise ChildProcessError
    monkeypatch.setattr(module.os, "waitpid", empty)
    monkeypatch.setattr(module.os, "read", lambda fd, n: chunks[fd].pop(0))
    monkeypatch.setattr(module.os, "set_blocking", lambda *a: None)
    monkeypatch.setattr(module.os, "close", lambda *a: None)
    logs = {"stdout": io.BytesIO(), "stderr": io.BytesIO()}
    complete, actual_guard, streams = module._observe(owner, {"stdout": 31, "stderr": 32, "control": 33},
                                                     logs, guard, time.monotonic() + 1, [False])
    assert complete is True
    assert actual_guard is None
    assert owner.error == "startup_error"
    assert logs["stdout"].getvalue() == b"tail"
    assert logs["stderr"].getvalue() == b"last"
    assert streams["stdout"]["bytes"] == 4


def test_echild_without_three_eof_remains_incomplete(monkeypatch):
    import io
    module = _controller()
    owner = module._Owner()
    owner.leader = 12
    owner.record(12, 0)
    tick = [0.0]
    def clock():
        tick[0] += .25
        return tick[0]
    def empty(*args):
        raise ChildProcessError
    def pending(*args):
        raise BlockingIOError
    monkeypatch.setattr(module.time, "monotonic", clock)
    monkeypatch.setattr(module.time, "sleep", lambda *a: None)
    monkeypatch.setattr(module.os, "waitpid", empty)
    monkeypatch.setattr(module.os, "read", pending)
    monkeypatch.setattr(module.os, "set_blocking", lambda *a: None)
    monkeypatch.setattr(module.os, "close", lambda *a: None)
    complete, _, _ = module._observe(owner, {"stdout": 31, "stderr": 32, "control": 33},
                                     {"stdout": io.BytesIO(), "stderr": io.BytesIO()}, {}, 100, [False])
    assert complete is False
    assert owner.error == "cleanup_error"


def test_pending_final_chunk_overflow_latches_even_with_echild(monkeypatch):
    import io
    module = _controller()
    owner = module._Owner()
    owner.leader = 12
    owner.record(12, 0)
    guard = {"path": "tests/conftest.py", "sha256": GUARD_HASH, "module_count": 1, "violations": 0}
    frame = json.dumps({"schema": "qwq.pytest-guard-ready/v1", "guard": guard}).encode() + b"\n"
    chunks = {31: [b"a" * 65536] * 128 + [b"x", b""], 32: [b""], 33: [frame, b""]}
    def empty(*args):
        raise ChildProcessError
    monkeypatch.setattr(module.os, "waitpid", empty)
    monkeypatch.setattr(module.os, "read", lambda fd, n: chunks[fd].pop(0))
    monkeypatch.setattr(module.os, "set_blocking", lambda *a: None)
    monkeypatch.setattr(module.os, "close", lambda *a: None)
    monkeypatch.setattr(module.time, "sleep", lambda *a: None)
    complete, _, streams = module._observe(owner, {"stdout": 31, "stderr": 32, "control": 33},
                                           {"stdout": io.BytesIO(), "stderr": io.BytesIO()}, guard,
                                           time.monotonic() + 2, [False])
    assert complete is True
    assert owner.error == "output_limit"
    assert streams["stdout"]["bytes"] == 8388609
    assert streams["stdout"]["overflow"] is True


def test_postleader_zombie_is_rejected_by_shared_recorder():
    module = _controller()
    owner = module._Owner()
    owner.leader = 12
    owner.record(12, 0)
    owner.record(13, 0)
    assert owner.survived is True
    assert owner.descendants == 1
    assert owner.returncode == 0


def test_pid_reuse_old_fd_esrch_never_signals_replacement(monkeypatch):
    module = _controller()
    owner = module._Owner()
    events = []
    monkeypatch.setattr(module.os, "pidfd_open", lambda *a: 71)
    monkeypatch.setattr(module.os, "waitpid", lambda *a: (0, 0))
    def vanished(fd, *args):
        events.append(fd)
        raise ProcessLookupError
    monkeypatch.setattr(module.signal, "pidfd_send_signal", vanished)
    monkeypatch.setattr(module.os, "close", lambda fd: events.append("closed"))
    owner.signal_candidate(123, signal.SIGKILL)
    assert events == [71, "closed"]
    assert owner.returncode is None
    assert owner.kill is False


def test_pidfd_permission_error_fails_closed(monkeypatch):
    module = _controller()
    owner = module._Owner()
    def denied(*args):
        raise PermissionError
    monkeypatch.setattr(module.os, "pidfd_open", denied)
    owner.signal_candidate(123, signal.SIGKILL)
    assert owner.error == "cleanup_error"
    assert owner.kill is False


def test_eintr_defers_work_without_reconstructing_status(monkeypatch):
    module = _controller()
    owner = module._Owner()
    def interrupted(*args):
        raise InterruptedError
    monkeypatch.setattr(module.os, "waitpid", interrupted)
    assert owner.wait(123) == "retry"
    assert owner.returncode is None


def test_module_import_does_not_change_ownership(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("import 중 프로세스 소유권 변경")
    monkeypatch.setattr(signal, "signal", forbidden)
    monkeypatch.setattr(ctypes, "CDLL", forbidden)
    monkeypatch.setattr(os, "fork", forbidden)
    monkeypatch.setattr(subprocess, "Popen", type("NoLaunch", (), {}))
    assert _controller().ROOT == ROOT


def test_reused_leader_number_alive_is_still_postleader_obligation(monkeypatch):
    module = _controller()
    owner = module._Owner()
    owner.leader = 12
    owner.record(12, 0)
    monkeypatch.setattr(module.os, "waitpid", lambda *a: (0, 0))
    assert owner.wait(12) == "alive"
    assert owner.survived is True


def test_probe_terminal_23_and_final_echild_are_both_required(monkeypatch):
    module = _controller()
    for actual_rc, wanted in ((23, True), (0, False)):
        with monkeypatch.context() as patch:
            calls = []
            def wait(pid, flags):
                calls.append((pid, flags))
                if len(calls) == 1:
                    return 99, actual_rc << 8
                raise ChildProcessError
            patch.setattr(module.os, "fork", lambda: 99)
            patch.setattr(module.os, "waitpid", wait)
            assert module._probe(time.monotonic() + 1, [False]) is wanted
            assert calls == [(99, os.WNOHANG | WALL), (-1, os.WNOHANG | WALL)]


def test_probe_missing_status_and_timeout_reject_without_bootstrap(monkeypatch):
    module = _controller()
    for kind in ("missing", "timeout"):
        with monkeypatch.context() as patch:
            tick = [0.0]
            def clock():
                tick[0] += .25
                return tick[0]
            def wait(*args):
                if kind == "missing":
                    raise ChildProcessError
                return 0, 0
            patch.setattr(module.os, "fork", lambda: 99)
            patch.setattr(module.os, "waitpid", wait)
            patch.setattr(module.time, "monotonic", clock)
            patch.setattr(module.time, "sleep", lambda *a: None)
            patch.setattr(module._Owner, "signal_children", lambda *a: None)
            assert module._probe(1, [False]) is False


def test_preflight_existing_child_never_consumes_status(monkeypatch):
    import pytest
    module = _controller()
    observed = []
    monkeypatch.setattr(module.os, "waitid", lambda *a: observed.append(a))
    def forbidden(*args):
        raise AssertionError("사전 소유하지 않은 자식 회수")
    monkeypatch.setattr(module.os, "waitpid", forbidden)
    with pytest.raises(ValueError):
        module._preflight()
    assert observed == [(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT | WALL)]


def test_real_late_adoption_after_term_budget_is_killed(tmp_path):
    repo = make_repo(tmp_path, '''import atexit, os, signal, time
def backstop():
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(8)
def spawn():
    backstop()
    if os.fork() == 0:
        backstop()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if os.fork() == 0:
            backstop()
            os.setsid()
            time.sleep(7)
            os._exit(0)
        time.sleep(7)
        os._exit(0)
def test_ok():
    atexit.register(spawn)
''')
    assert run_case(repo)["rc"] == 125
    doc = json.loads((repo / "process.json").read_text())
    assert doc["process"]["returncode"] == 0
    assert doc["process"]["kill_sent"] is True
    assert doc["process"]["descendant_survived"] is True
    assert doc["process"]["cleanup_complete"] is True
    assert doc["process"]["descendants_reaped"] == 2


def test_failed_log_write_hashes_only_preserved_prefix(monkeypatch):
    import io
    module = _controller()
    owner = module._Owner()
    owner.leader = 12
    owner.record(12, 0)
    guard = {"path": "tests/conftest.py", "sha256": GUARD_HASH, "module_count": 1, "violations": 0}
    frame = json.dumps({"schema": "qwq.pytest-guard-ready/v1", "guard": guard}).encode() + b"\n"
    chunks = {31: [b"lost", b""], 32: [b""], 33: [frame, b""]}
    class FailedLog:
        def write(self, raw):
            raise OSError("고정 쓰기 실패")
    def empty(*args):
        raise ChildProcessError
    monkeypatch.setattr(module.os, "waitpid", empty)
    monkeypatch.setattr(module.os, "read", lambda fd, n: chunks[fd].pop(0))
    monkeypatch.setattr(module.os, "set_blocking", lambda *a: None)
    monkeypatch.setattr(module.os, "close", lambda *a: None)
    complete, _, streams = module._observe(owner, {"stdout": 31, "stderr": 32, "control": 33},
                                           {"stdout": FailedLog(), "stderr": io.BytesIO()}, guard,
                                           time.monotonic() + 2, [False])
    assert complete is True
    assert owner.error == "io_error"
    assert streams["stdout"]["bytes"] == 0
    assert streams["stdout"]["sha256"] == hashlib.sha256(b"").hexdigest()


def _bootstrap():
    spec = importlib.util.spec_from_file_location("_bootstrap_test", ROOT / "scripts/dev/pytest_evidence_bootstrap.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_guard_rejects_nonmodule_spoof_but_accepts_real_subclass(tmp_path, monkeypatch):
    import types
    import pytest
    bootstrap = _bootstrap()
    repo = make_repo(tmp_path)
    class Pretender:
        __class__ = types.ModuleType
    class RealModule(types.ModuleType):
        pass
    for item, allowed in ((Pretender(), False), (RealModule("real"), True)):
        item.__file__ = str(repo / "tests/conftest.py")
        item.VIOLATIONS = []
        with monkeypatch.context() as patch:
            patch.setattr(sys, "modules", {"guard": item})
            if allowed:
                assert bootstrap._guard(repo)["module_count"] == 1
            else:
                with pytest.raises(ValueError):
                    bootstrap._guard(repo)


def test_bootstrap_rechecks_actual_guard_after_producer(monkeypatch):
    import types
    module = _bootstrap()
    guard = {"path": "tests/conftest.py", "sha256": GUARD_HASH, "module_count": 1, "violations": 0}
    observations = []
    def observe(*args, **kwargs):
        observations.append(kwargs)
        if len(observations) > 1:
            raise ValueError
        return guard
    monkeypatch.setattr(module, "_guard", observe)
    monkeypatch.setattr(module, "_load", lambda *a: types.SimpleNamespace(run_with_evidence=lambda *a, **k: 0))
    monkeypatch.setattr(module.os, "write", lambda fd, raw: len(raw))
    monkeypatch.setattr(module.os, "close", lambda fd: None)
    monkeypatch.setattr(sys, "path", list(sys.path))
    assert module.main(["71", "context.json", "receipt.json", "tests/test_tiny.py"]) == 125
    assert len(observations) == 2


def test_observer_retry_retains_fd_ownership_and_cleanup_deadline(monkeypatch):
    import io
    import pytest
    module = _controller()
    owner = module._Owner()
    owner.leader = 12
    owner.record(12, 0)
    pipes = {"stdout": 31, "stderr": 32, "control": 33}
    closed = []
    logs = {"stdout": io.BytesIO(), "stderr": io.BytesIO()}
    def empty(*args):
        raise ChildProcessError
    def first_read(fd, n):
        if fd == 31:
            return b""
        raise RuntimeError("고정 중간 관측 실패")
    monkeypatch.setattr(module.os, "waitpid", empty)
    monkeypatch.setattr(module.os, "read", first_read)
    monkeypatch.setattr(module.os, "close", lambda fd: closed.append(fd))
    with pytest.raises(RuntimeError):
        module._observe(owner, pipes, logs, {}, time.monotonic() + 2, [False])
    assert "stdout" not in pipes
    first_deadline = owner.finish_end
    owner.reject("io_error")
    monkeypatch.setattr(module.os, "read", lambda *a: b"")
    complete, _, _ = module._observe(owner, pipes, logs, {}, time.monotonic() + 2, [False])
    assert complete is True
    assert owner.finish_end == first_deadline
    assert closed == [31, 32, 33]


def test_real_bad_control_frames_are_rejected_even_with_zero_exit(tmp_path):
    guard = {"path": "tests/conftest.py", "sha256": GUARD_HASH, "module_count": 1, "violations": 0}
    frame = json.dumps({"schema": "qwq.pytest-guard-ready/v1", "guard": guard}).encode() + b"\n"
    for index, raw in enumerate((b"", frame + frame, frame.replace(GUARD_HASH.encode(), b"0" * 64), frame + b"x")):
        repo = make_repo(tmp_path / str(index))
        # 가드/producer는 원본 그대로이며 새 bootstrap의 실패만 고정 주입한다.
        (repo / "scripts/dev/pytest_evidence_bootstrap.py").write_text(
            (ROOT / "scripts/dev/pytest_evidence_bootstrap.py").read_text().replace(
                'if os.write(control, frame) != len(frame):',
                f'frame = {raw!r}\n        if os.write(control, frame) != len(frame):'))
        assert run_case(repo)["rc"] == 125
        doc = json.loads((repo / "process.json").read_text())
        assert doc["process"]["reason"] == "startup_error"
        assert doc["process"]["cleanup_complete"] is True
        assert doc["process"]["guard"] is None


def test_parent_symlink_and_absolute_escape_never_launch(tmp_path, monkeypatch):
    import pytest
    module = _controller()
    repo = make_repo(tmp_path)
    (repo / "linked").symlink_to(repo / "tests", target_is_directory=True)
    monkeypatch.setattr(module, "ROOT", repo)
    monkeypatch.chdir(repo)
    prefix = ["--verification-context", "context.json", "--verification-output", "receipt.json",
              "--process-output", "process.json", "--timeout-seconds", "3", "--"]
    for selector in (str(tmp_path), "linked/test_tiny.py"):
        with pytest.raises(ValueError):
            module._arguments(prefix + [selector])
    with pytest.raises(ValueError):
        module._path("linked/new.json", new=True)


def test_environment_pytest_options_are_rejected_even_empty(tmp_path, monkeypatch):
    import pytest
    module = _controller()
    repo = make_repo(tmp_path)
    monkeypatch.setattr(module, "ROOT", repo)
    monkeypatch.chdir(repo)
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        with monkeypatch.context() as patch:
            patch.setenv(name, "")
            with pytest.raises(ValueError):
                module._arguments([])


def test_completion_after_fixed_finish_deadline_is_not_approved(monkeypatch):
    import io
    module = _controller()
    owner = module._Owner()
    owner.leader = 12
    owner.record(12, 0)
    owner.finish_end = 0.0
    guard = {"path": "tests/conftest.py", "sha256": GUARD_HASH, "module_count": 1, "violations": 0}
    frame = json.dumps({"schema": "qwq.pytest-guard-ready/v1", "guard": guard}).encode() + b"\n"
    def empty(*args):
        raise ChildProcessError
    monkeypatch.setattr(module.os, "waitpid", empty)
    monkeypatch.setattr(module.os, "read", lambda *a: b"")
    monkeypatch.setattr(module.os, "close", lambda *a: None)
    complete, _, _ = module._observe(owner, {"stdout": 31, "stderr": 32, "control": 33},
                                     {"stdout": io.BytesIO(), "stderr": io.BytesIO()}, guard,
                                     time.monotonic() + 2, [False])
    assert complete is False


def test_real_nonmodule_guard_replacement_after_receipt_is_rejected(tmp_path):
    repo = make_repo(tmp_path, '''import sys, types
def test_ok():
    original = sys.modules['conftest']
    fake = types.SimpleNamespace(__file__=original.__file__, VIOLATIONS=[])
    sys.modules['conftest'] = fake
''')
    assert run_case(repo)["rc"] == 125
    doc = json.loads((repo / "process.json").read_text())
    assert doc["process"]["returncode"] == 125
    assert doc["receipt"]["state"] == "regular"
    assert doc["process"]["cleanup_complete"] is True


def test_repeated_postspawn_observer_exception_still_publishes_failure(tmp_path, monkeypatch):
    import types
    module = _controller()
    repo = make_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(module, "ROOT", repo)
    monkeypatch.setattr(module, "__file__", str(repo / "scripts/dev/pytest_evidence_controller.py"))
    guard = {"path": "tests/conftest.py", "sha256": GUARD_HASH, "module_count": 1, "violations": 0}
    producer = types.SimpleNamespace(_read_context=lambda p: (CONTEXT, b""))
    bootstrap = types.SimpleNamespace(_guard=lambda *a, **k: guard, _load=lambda *a: producer)
    monkeypatch.setattr(module, "_load_bootstrap", lambda: bootstrap)
    monkeypatch.setattr(module, "_preflight", lambda: None)
    monkeypatch.setattr(module, "_probe", lambda *a: True)
    monkeypatch.setattr(module.signal, "signal", lambda *a: None)
    monkeypatch.setattr(module, "_RawPopen", lambda *a, **k: types.SimpleNamespace(pid=99, returncode=None))
    def broken(owner, *args):
        if not owner.reaped:
            owner.record(99, 0)
        raise RuntimeError("고정 관측기 실패")
    def empty(*args):
        raise ChildProcessError
    monkeypatch.setattr(module, "_observe", broken)
    monkeypatch.setattr(module.os, "waitpid", empty)
    args = ["--verification-context", "context.json", "--verification-output", "receipt.json",
            "--process-output", "process.json", "--timeout-seconds", "3", "--", "tests/test_tiny.py"]
    assert module.main(args) == 125
    doc = json.loads((repo / "process.json").read_text())
    assert doc["process"]["cleanup_complete"] is False
    assert doc["process"]["returncode"] == 0
    assert doc["process"]["reason"] == "startup_error"


def test_preflight_rejects_unavailable_subreaper_or_pidfd_without_fork(monkeypatch):
    import types
    import pytest
    module = _controller()
    for failure in ("set", "get", "readback", "pidfd"):
        with monkeypatch.context() as patch:
            def prctl(command, value, *rest):
                if command == 36:
                    return -1 if failure == "set" else 0
                if failure == "get":
                    return -1
                ctypes.cast(value, ctypes.POINTER(ctypes.c_int))[0] = 0 if failure == "readback" else 1
                return 0
            def empty(*args):
                raise ChildProcessError
            def unavailable(*args):
                raise PermissionError
            def forbidden(*args):
                raise AssertionError("사전 검증 실패 뒤 fork")
            patch.setattr(module.ctypes, "CDLL", lambda *a, **k: types.SimpleNamespace(prctl=prctl))
            patch.setattr(module.signal, "signal", lambda *a: None)
            patch.setattr(module.os, "waitid", empty)
            patch.setattr(module.os, "pidfd_open", unavailable)
            patch.setattr(module.os, "fork", forbidden)
            with pytest.raises((ValueError, PermissionError)):
                module._preflight()


def test_startup_deadline_is_not_restarted_on_observer_entry(monkeypatch):
    import io
    module = _controller()
    owner = module._Owner()
    owner.leader = 12
    owner.record(12, 0)
    owner.startup_end = 0.0
    guard = {"path": "tests/conftest.py", "sha256": GUARD_HASH, "module_count": 1, "violations": 0}
    frame = json.dumps({"schema": "qwq.pytest-guard-ready/v1", "guard": guard}).encode() + b"\n"
    chunks = {31: [b""], 32: [b""], 33: [frame, b""]}
    def empty(*args):
        raise ChildProcessError
    monkeypatch.setattr(module.os, "waitpid", empty)
    monkeypatch.setattr(module.os, "read", lambda fd, n: chunks[fd].pop(0))
    monkeypatch.setattr(module.os, "close", lambda *a: None)
    complete, _, _ = module._observe(owner, {"stdout": 31, "stderr": 32, "control": 33},
                                     {"stdout": io.BytesIO(), "stderr": io.BytesIO()}, guard,
                                     time.monotonic() + 2, [False])
    assert complete is True
    assert owner.error == "startup_error"


def test_context_actual_raw_over_64k_is_rejected_before_preflight(tmp_path, monkeypatch):
    import types
    module = _controller()
    repo = make_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(module, "ROOT", repo)
    guard = {"path": "tests/conftest.py", "sha256": GUARD_HASH, "module_count": 1, "violations": 0}
    producer = types.SimpleNamespace(_read_context=lambda p: (CONTEXT, b"x" * 65537))
    bootstrap = types.SimpleNamespace(_guard=lambda *a, **k: guard, _load=lambda *a: producer)
    monkeypatch.setattr(module, "_load_bootstrap", lambda: bootstrap)
    monkeypatch.setattr(module.signal, "signal", lambda *a: None)
    def forbidden():
        raise AssertionError("과대 context가 사전 소유권 검사에 도달")
    monkeypatch.setattr(module, "_preflight", forbidden)
    args = ["--verification-context", "context.json", "--verification-output", "receipt.json",
            "--process-output", "process.json", "--timeout-seconds", "3", "--", "tests/test_tiny.py"]
    assert module.main(args) == 125
    assert not (repo / "process.json").exists()


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--harness":
        raise SystemExit(_harness(Path(sys.argv[2]), sys.argv[3] == "dummy", sys.argv[3] == "interrupt"))
    raise SystemExit(94)
