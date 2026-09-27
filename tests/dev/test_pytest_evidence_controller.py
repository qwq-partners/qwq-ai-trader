"""실제 프로세스 시험은 독립 회수자 아래에서만 실행한다."""

import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time

import pytest


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


def make_repo(tmp_path, body="def test_ok():\n    assert True\n", *, b1_isolated=False, b1_short_budget=False):
    """고정 guard/producer와 임시 안전 상태 모듈만 갖는 합성 저장소다."""
    if (type(b1_isolated) is not bool or type(b1_short_budget) is not bool
            or (b1_short_budget and not b1_isolated)):
        raise ValueError("B1_FIXTURE_MODE")
    repo = tmp_path / "repo"
    if b1_isolated and any(path.is_symlink() for path in (repo, *repo.parents)):
        raise ValueError("B1_FIXTURE_PATH")
    if b1_isolated:
        repo.mkdir(parents=True, mode=0o700)
    (repo / "scripts/dev").mkdir(parents=True)
    (repo / "tests").mkdir()
    for name in ("pytest_evidence.py", "pytest_evidence_bootstrap.py", "pytest_evidence_controller.py"):
        source = ROOT / "scripts/dev" / name
        if source.exists():
            (repo / "scripts/dev" / name).write_bytes(source.read_bytes())
    if b1_isolated:
        controller = repo / "scripts/dev/pytest_evidence_controller.py"
        raw = controller.read_bytes()
        old_lock = b'    _LOCK = "/home/ubuntu/projects/qwq-ai-trader/.claude/worktrees/owner-ticket-gate-20260926/.superpowers/sdd/2026-09-27-runtime-admission-contract/test-workload.lock"\n'
        old_total = b"        self.total_end = started + 900\n"
        short_total = b"        self.total_end = started + 6\n"
        fixed = (b"        self.run_end = self.total_end - 4\n",
                 b"            self.cleanup_end = min(now + 3, self.total_end - 1)\n",
                 b"            self.term_end = min(now + 1, self.cleanup_end)\n")
        lock = repo / ".b1-fixture/test-workload.lock"
        new_lock = ("    _LOCK = " + json.dumps(str(lock)) + "\n").encode()
        if (raw.count(old_lock) != 1 or raw.count(new_lock) != 0
                or raw.count(old_total) != 1 or raw.count(short_total) != 0
                or any(raw.count(line) != 1 for line in fixed)):
            raise ValueError("B1_FIXTURE_ANCHOR")
        lock.parent.mkdir(mode=0o700)
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise ValueError("B1_FIXTURE_LOCK")
        finally:
            os.close(fd)
        raw = raw.replace(old_lock, new_lock)
        if b1_short_budget:
            raw = raw.replace(old_total, short_total)
        if (raw.count(old_lock) != 0 or raw.count(new_lock) != 1
                or raw.count(old_total) != (0 if b1_short_budget else 1)
                or raw.count(short_total) != (1 if b1_short_budget else 0)
                or any(raw.count(line) != 1 for line in fixed)):
            raise ValueError("B1_FIXTURE_TRANSFORM")
        controller.write_bytes(raw)
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


def test_receipt_symlink_and_fifo_never_block_or_become_regular(tmp_path, monkeypatch):
    module = _controller()
    monkeypatch.setattr(module, "ROOT", tmp_path)
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
    import io
    import pytest
    module = _controller()
    observed = []
    pid = 4321
    def one_task(path):
        observed.append(("tasks", path))
        return [str(pid)]
    def empty_children(path, mode):
        observed.append(("children", path, mode))
        return io.BytesIO(b"")
    def existing_status(*args):
        observed.append(("waitid", args))
    monkeypatch.setattr(module.os, "listdir", one_task)
    monkeypatch.setattr(module.os, "getpid", lambda: pid)
    monkeypatch.setattr(module, "open", empty_children, raising=False)
    monkeypatch.setattr(module.os, "waitid", existing_status)
    def forbidden(*args):
        raise AssertionError("사전 소유하지 않은 자식 회수")
    monkeypatch.setattr(module.os, "waitpid", forbidden)
    monkeypatch.setattr(module.os, "fork", forbidden)
    monkeypatch.setattr(module.signal, "signal", forbidden)
    monkeypatch.setattr(module.ctypes, "CDLL", forbidden)
    monkeypatch.setattr(module.os, "pidfd_open", forbidden)
    monkeypatch.setattr(module.signal, "pidfd_send_signal", forbidden)
    with pytest.raises(ValueError):
        module._preflight()
    assert observed == [("tasks", "/proc/self/task"),
                        ("children", f"/proc/self/task/{pid}/children", "rb"),
                        ("waitid", (os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT | WALL))]


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
    import io
    import types
    import pytest
    module = _controller()
    pid = 4321
    for failure in ("set", "get", "readback", "pidfd"):
        with monkeypatch.context() as patch:
            calls = []
            def one_task(path):
                calls.append(("tasks", path))
                return [str(pid)]
            def empty_children(path, mode):
                calls.append(("children", path, mode))
                return io.BytesIO(b"")
            def prctl(command, value, *rest):
                calls.append(("prctl", command))
                if command == 36:
                    return -1 if failure == "set" else 0
                if failure == "get":
                    return -1
                ctypes.cast(value, ctypes.POINTER(ctypes.c_int))[0] = 0 if failure == "readback" else 1
                return 0
            def empty(*args):
                calls.append(("waitid", args))
                raise ChildProcessError
            def unavailable(*args):
                calls.append(("pidfd", args))
                raise PermissionError
            def reset(sig, handler):
                calls.append(("sigchld", sig, handler))
            def forbidden(*args):
                raise AssertionError("사전 검증 실패 뒤 fork")
            patch.setattr(module.os, "listdir", one_task)
            patch.setattr(module.os, "getpid", lambda: pid)
            patch.setattr(module, "open", empty_children, raising=False)
            patch.setattr(module.ctypes, "CDLL", lambda *a, **k: types.SimpleNamespace(prctl=prctl))
            patch.setattr(module.signal, "signal", reset)
            patch.setattr(module.os, "waitid", empty)
            patch.setattr(module.os, "pidfd_open", unavailable)
            patch.setattr(module.os, "fork", forbidden)
            patch.setattr(module.os, "waitpid", forbidden)
            patch.setattr(module.signal, "pidfd_send_signal", forbidden)
            with pytest.raises((ValueError, PermissionError)):
                module._preflight()
            expected = [("tasks", "/proc/self/task"),
                        ("children", f"/proc/self/task/{pid}/children", "rb"),
                        ("waitid", (os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT | WALL)),
                        ("sigchld", signal.SIGCHLD, signal.SIG_DFL),
                        ("prctl", 36)]
            if failure != "set":
                expected.append(("prctl", 37))
            if failure == "pidfd":
                expected.append(("pidfd", (pid, 0)))
            assert calls == expected


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


def _fake_main(tmp_path, monkeypatch):
    """실제 프로세스를 만들지 않는 main 경계 시험용 고정 의존성이다."""
    import types
    module = _controller()
    repo = make_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(module, "ROOT", repo)
    monkeypatch.setattr(module, "__file__", str(repo / "scripts/dev/pytest_evidence_controller.py"))
    guard = {"path": "tests/conftest.py", "sha256": GUARD_HASH, "module_count": 1, "violations": 0}
    producer = types.SimpleNamespace(_read_context=lambda p: (CONTEXT, b"{}"))
    bootstrap = types.SimpleNamespace(_guard=lambda *a, **k: guard, _load=lambda *a: producer)
    monkeypatch.setattr(module, "_load_bootstrap", lambda: bootstrap)
    monkeypatch.setattr(module, "_preflight", lambda: None)
    monkeypatch.setattr(module, "_probe", lambda *a: True)
    monkeypatch.setattr(module.signal, "signal", lambda *a: None)
    monkeypatch.setattr(module, "_RawPopen", lambda *a, **k: types.SimpleNamespace(pid=99, returncode=None))
    streams = {name: {"bytes": 0, "sha256": hashlib.sha256(b"").hexdigest(), "overflow": False}
               for name in ("stdout", "stderr")}
    args = ["--verification-context", "context.json", "--verification-output", "receipt.json",
            "--process-output", "process.json", "--timeout-seconds", "3", "--", "tests/test_tiny.py"]
    return module, repo, guard, streams, args


def test_r1_real_process_parent_replacement_cannot_publish_outside_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    repo = make_repo(tmp_path, f'''import atexit
from pathlib import Path
def change():
    Path('evidence').rename('evidence-old')
    Path('evidence').symlink_to({str(outside)!r}, target_is_directory=True)
def test_ok():
    atexit.register(change)
''')
    (repo / "evidence").mkdir()
    args = ["--verification-context", "context.json", "--verification-output", "receipt.json",
            "--process-output", "evidence/process.json", "--timeout-seconds", "3", "--", "tests/test_tiny.py"]
    result = run_case(repo, args=args)
    assert not (outside / "process.json").exists()
    assert result["rc"] == 125


def test_r1_replaced_receipt_parent_never_opens_external_sentinel(tmp_path, monkeypatch):
    module, repo, guard, streams, args = _fake_main(tmp_path, monkeypatch)
    (repo / "receipts").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "receipt.json"
    sentinel.write_bytes(b"external sentinel must not be read")
    sentinel_stat = sentinel.stat()
    args[3] = "receipts/receipt.json"
    opened = []
    real_open = os.open
    def tracked_open(*a, **kw):
        fd = real_open(*a, **kw)
        actual = os.fstat(fd)
        if (actual.st_dev, actual.st_ino) == (sentinel_stat.st_dev, sentinel_stat.st_ino):
            opened.append(fd)
        return fd
    def finished(owner, pipes, *rest):
        owner.record(99, 0)
        for fd in list(pipes.values()):
            os.close(fd)
        pipes.clear()
        (repo / "receipts").rename(repo / "receipts-old")
        (repo / "receipts").symlink_to(outside, target_is_directory=True)
        return True, guard, streams
    monkeypatch.setattr(module.os, "open", tracked_open)
    monkeypatch.setattr(module, "_observe", finished)
    assert module.main(args) == 125
    assert opened == []
    doc = json.loads((repo / "process.json").read_text())
    assert doc["receipt"] == {"state": "invalid", "bytes": 0, "sha256": None}
    assert doc["process"]["returncode"] == 0


def test_r2_unfinished_lifecycle_never_reads_receipt(tmp_path, monkeypatch):
    for mode in ("unstarted", "unreaped", "missing_eof", "observer_error"):
        with monkeypatch.context() as patch:
            module, repo, guard, streams, args = _fake_main(tmp_path / mode, patch)
            reads = []
            patch.setattr(module, "_receipt", lambda *a, **k: reads.append(True) or
                          {"state": "regular", "bytes": 0, "sha256": hashlib.sha256(b"").hexdigest()})
            def empty(*args):
                raise ChildProcessError
            def unfinished(owner, pipes, *rest):
                owner.reject("timeout")
                if mode != "unreaped" and not owner.reaped:
                    owner.record(99, 9)
                if mode == "observer_error":
                    raise RuntimeError("고정 관측 실패")
                for fd in list(pipes.values()):
                    os.close(fd)
                pipes.clear()
                return False, guard, streams
            patch.setattr(module, "_observe", unfinished)
            patch.setattr(module.os, "waitpid", empty)
            if mode == "unstarted":
                patch.setattr(module, "_probe", lambda *a: False)
            rc = module.main(args)
            assert reads == [], mode
            assert rc == (125 if mode == "unstarted" else 124)
            doc = json.loads((repo / "process.json").read_text())
            assert doc["receipt"] == {"state": "invalid", "bytes": 0, "sha256": None}
            assert doc["process"]["returncode"] == (None if mode in ("unstarted", "unreaped") else -9)
            assert doc["process"]["cleanup_complete"] is False


def test_main_failed_preflight_or_probe_never_launches_or_touches_children(tmp_path, monkeypatch):
    import io
    preflight_module = _controller()
    actual_preflight = preflight_module._preflight
    pid = 4321
    for mode in ("extra_task", "proc", "sigchld", "missing_probe_status", "probe_timeout", "probe_failed"):
        with monkeypatch.context() as patch:
            module, repo, guard, streams, args = _fake_main(tmp_path / mode, patch)
            actions = []
            target_calls = []
            def forbidden(*args, **kwargs):
                actions.append("forbidden child operation")
                raise AssertionError("미소유 자식 조작 또는 bootstrap 실행")
            def forbidden_preflight_boundary(*args, **kwargs):
                raise AssertionError("고정 preflight 거부 뒤 OS 경계")
            def startup_only_signal(sig, handler):
                if sig not in (signal.SIGTERM, signal.SIGINT):
                    raise AssertionError("고정 preflight 거부 뒤 신호 설정")
            if mode in ("extra_task", "proc", "sigchld"):
                patch.setattr(module, "_preflight", actual_preflight)
                patch.setattr(module, "_probe", forbidden)
                def tasks(path):
                    target_calls.append(("tasks", path))
                    return [str(pid), str(pid + 1)] if mode == "extra_task" else [str(pid)]
                patch.setattr(preflight_module.os, "listdir", tasks)
                patch.setattr(preflight_module.os, "getpid", lambda: pid)
                patch.setattr(preflight_module.ctypes, "CDLL", forbidden_preflight_boundary)
                patch.setattr(preflight_module.os, "pidfd_open", forbidden_preflight_boundary)
                if mode == "extra_task":
                    patch.setattr(preflight_module, "open", forbidden_preflight_boundary, raising=False)
                    patch.setattr(preflight_module.signal, "signal", startup_only_signal)
                elif mode == "proc":
                    def unavailable(path, *a, **k):
                        target_calls.append(("children", path, *a))
                        raise PermissionError("고정 proc 실패")
                    patch.setattr(preflight_module, "open", unavailable, raising=False)
                    patch.setattr(preflight_module.os, "waitid", forbidden_preflight_boundary)
                    patch.setattr(preflight_module.signal, "signal", startup_only_signal)
                else:
                    def empty_children(path, mode):
                        target_calls.append(("children", path, mode))
                        return io.BytesIO(b"")
                    def initial_empty(*a):
                        target_calls.append(("waitid", a))
                        raise ChildProcessError
                    def reset(sig, handler):
                        if sig == signal.SIGCHLD:
                            target_calls.append(("sigchld", sig, handler))
                            raise OSError("고정 SIGCHLD reset 실패")
                    patch.setattr(preflight_module, "open", empty_children, raising=False)
                    patch.setattr(preflight_module.os, "waitid", initial_empty)
                    patch.setattr(preflight_module.signal, "signal", reset)
            else:
                patch.setattr(module, "_probe", lambda *a: False)
                patch.setattr(module.signal, "signal", startup_only_signal)
            patch.setattr(module, "_RawPopen", forbidden)
            patch.setattr(module.os, "fork", forbidden)
            patch.setattr(module.os, "waitpid", forbidden)
            patch.setattr(module.signal, "pidfd_send_signal", forbidden)
            assert module.main(args) == 125
            assert actions == []
            doc = json.loads((repo / "process.json").read_text())
            assert doc["process"]["returncode"] is None
            assert doc["process"]["leader_reaped"] is False
            assert doc["process"]["ownership_probe_passed"] is False
            expected = [("tasks", "/proc/self/task")]
            if mode == "proc":
                expected.append(("children", f"/proc/self/task/{pid}/children", "rb"))
            elif mode == "sigchld":
                expected.extend([
                    ("children", f"/proc/self/task/{pid}/children", "rb"),
                    ("waitid", (os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT | WALL)),
                    ("sigchld", signal.SIGCHLD, signal.SIG_DFL),
                ])
            if mode in ("extra_task", "proc", "sigchld"):
                assert target_calls == expected
            else:
                assert target_calls == []


def test_main_repeated_observer_error_reaps_fake_alive_child_by_pidfd(tmp_path, monkeypatch):
    import io
    module, repo, guard, streams, args = _fake_main(tmp_path, monkeypatch)
    tick = [0.0]
    def clock():
        tick[0] += .03
        return tick[0]
    monkeypatch.setattr(module.time, "monotonic", clock)
    monkeypatch.setattr(module.time, "sleep", lambda *a: None)
    owners = []
    deadlines = []
    def broken(owner, *args):
        if owner.finish_end is None:
            owner.finish_end = clock() + 3
        owners.append(owner)
        deadlines.append(owner.finish_end)
        raise RuntimeError("고정 live observer 실패")
    monkeypatch.setattr(module, "_observe", broken)
    alive = [True]
    reaped = [False]
    events = []
    def wait(pid, flags):
        events.append(("wait", pid))
        if alive[0]:
            return 0, 0
        if not reaped[0]:
            reaped[0] = True
            return 99, 9
        raise ChildProcessError
    def send(fd, sig, *args):
        events.append(("signal", fd, sig))
        if sig == signal.SIGKILL:
            alive[0] = False
    real_close = os.close
    def close(fd):
        if fd == 710:
            events.append(("close_pidfd", fd))
        else:
            real_close(fd)
    monkeypatch.setattr(module.os, "waitpid", wait)
    monkeypatch.setattr(module.os, "pidfd_open", lambda *a: events.append(("open_pidfd", 99)) or 710)
    monkeypatch.setattr(module.signal, "pidfd_send_signal", send)
    monkeypatch.setattr(module.os, "close", close)
    monkeypatch.setattr(module, "open", lambda *a, **k: io.BytesIO(b"99"), raising=False)
    monkeypatch.setattr(module, "_receipt", lambda *a: (_ for _ in ()).throw(AssertionError("미완료 receipt 접근")))
    assert module.main(args) == 125
    doc = json.loads((repo / "process.json").read_text())
    assert doc["process"]["returncode"] == -9
    assert doc["process"]["term_sent"] is True
    assert doc["process"]["kill_sent"] is True
    assert doc["process"]["cleanup_complete"] is False
    assert deadlines[0] == deadlines[1] == owners[0].finish_end
    assert reaped == [True]
    signals = [item for item in events if item[0] == "signal"]
    assert signals and signals[-1] == ("signal", 710, signal.SIGKILL)
    assert sum(item[0] == "open_pidfd" for item in events) == sum(item[0] == "close_pidfd" for item in events)
    for index, item in enumerate(events):
        if item[0] == "signal":
            assert events[index - 2:index] == [("open_pidfd", 99), ("wait", 99)]


def test_partial_pipe_creation_failure_closes_owned_ends_once_without_launch(tmp_path, monkeypatch):
    import stat
    module, repo, guard, streams, args = _fake_main(tmp_path, monkeypatch)
    real_pipe, real_close = os.pipe, os.close
    owned = {}
    closed = []
    def partial_pipe():
        if owned:
            raise OSError("고정 두 번째 pipe 실패")
        pair = real_pipe()
        owned.update({fd: os.fstat(fd).st_ino for fd in pair})
        return pair
    def close(fd):
        actual = os.fstat(fd)
        if fd in owned and stat.S_ISFIFO(actual.st_mode) and actual.st_ino == owned[fd]:
            closed.append(fd)
        real_close(fd)
    def forbidden(*args, **kwargs):
        raise AssertionError("부분 pipe 실패 뒤 launch")
    monkeypatch.setattr(module.os, "pipe", partial_pipe)
    monkeypatch.setattr(module.os, "close", close)
    monkeypatch.setattr(module, "_RawPopen", forbidden)
    assert module.main(args) == 125
    assert sorted(closed) == sorted(owned)
    assert len(closed) == 2
    doc = json.loads((repo / "process.json").read_text())
    assert doc["process"]["returncode"] is None
    assert doc["receipt"]["state"] == "invalid"


def test_constructor_failure_reaps_fake_owned_child_without_fabricated_leader(tmp_path, monkeypatch):
    import io
    module, repo, guard, streams, args = _fake_main(tmp_path, monkeypatch)
    def failed_spawn(*args, **kwargs):
        raise RuntimeError("고정 생성자 실패")
    phase = ["alive"]
    def wait(pid, flags):
        if phase[0] == "alive":
            return 0, 0
        if phase[0] == "signaled":
            phase[0] = "reaped"
            return 99, 256
        raise ChildProcessError
    sent = []
    def send(fd, sig, *args):
        sent.append((fd, sig))
        phase[0] = "signaled"
    real_close = os.close
    monkeypatch.setattr(module, "_RawPopen", failed_spawn)
    monkeypatch.setattr(module.os, "waitpid", wait)
    monkeypatch.setattr(module.os, "pidfd_open", lambda *a: 710)
    monkeypatch.setattr(module.signal, "pidfd_send_signal", send)
    monkeypatch.setattr(module.os, "close", lambda fd: None if fd == 710 else real_close(fd))
    monkeypatch.setattr(module, "open", lambda *a, **k: io.BytesIO(b"99"), raising=False)
    assert module.main(args) == 125
    assert sent == [(710, signal.SIGTERM)]
    assert phase == ["reaped"]
    doc = json.loads((repo / "process.json").read_text())
    assert doc["process"]["returncode"] is None
    assert doc["process"]["leader_reaped"] is False
    assert doc["process"]["descendants_reaped"] == 1
    assert doc["process"]["cleanup_complete"] is False


def test_descriptor_close_failure_is_not_retried_or_reassigned(monkeypatch):
    module = _controller()
    owner = module._Owner()
    attempts = []
    def failed(fd):
        attempts.append(fd)
        raise OSError("고정 close 실패")
    monkeypatch.setattr(module.os, "close", failed)
    descriptors = [71, 72]
    module._close_descriptors(descriptors, owner)
    module._close_descriptors(descriptors, owner)
    assert attempts == [72, 71]
    assert descriptors == []
    assert owner.error == "io_error"


def test_empty_proc_does_not_hide_postleader_generic_zero(monkeypatch):
    import io
    module = _controller()
    owner = module._Owner()
    owner.leader = 99
    owner.record(99, 0)
    guard = {"path": "tests/conftest.py", "sha256": GUARD_HASH, "module_count": 1, "violations": 0}
    frame = json.dumps({"schema": "qwq.pytest-guard-ready/v1", "guard": guard}).encode() + b"\n"
    chunks = {31: [b""], 32: [b""], 33: [frame, b""]}
    waits = [0]
    def wait(pid, flags):
        waits[0] += 1
        if waits[0] == 1:
            return 0, 0
        raise ChildProcessError
    proc_reads = []
    def empty_proc(*args, **kwargs):
        proc_reads.append(True)
        return io.BytesIO(b"")
    monkeypatch.setattr(module.os, "waitpid", wait)
    monkeypatch.setattr(module.os, "read", lambda fd, n: chunks[fd].pop(0))
    monkeypatch.setattr(module.os, "close", lambda *a: None)
    monkeypatch.setattr(module, "open", empty_proc, raising=False)
    complete, _, _ = module._observe(owner, {"stdout": 31, "stderr": 32, "control": 33},
                                     {"stdout": io.BytesIO(), "stderr": io.BytesIO()}, guard,
                                     time.monotonic() + 2, [False])
    assert complete is True
    assert proc_reads == [True]
    assert owner.survived is True


def test_preleader_reaped_descendant_does_not_set_survivor():
    module = _controller()
    owner = module._Owner()
    owner.leader = 99
    owner.record(98, 0)
    owner.record(99, 0)
    assert owner.descendants == 1
    assert owner.survived is False
    assert owner.returncode == 0


def test_raw_popen_destructor_never_calls_implicit_poll(monkeypatch):
    module = _controller()
    child = module._RawPopen.__new__(module._RawPopen)
    def forbidden(*args, **kwargs):
        raise AssertionError("암묵 Popen 회수")
    for name in ("poll", "wait", "communicate", "send_signal", "terminate", "kill", "_internal_poll"):
        monkeypatch.setattr(child, name, forbidden)
    child._child_created = True
    child.returncode = None
    child.__del__()
    assert child.returncode is None


def test_source_loaders_execute_current_bytes_without_loader_exec(tmp_path, monkeypatch):
    module = _controller()
    repo = tmp_path / "repo"
    (repo / "scripts/dev").mkdir(parents=True)
    path = repo / "scripts/dev/pytest_evidence_bootstrap.py"
    path.write_bytes(b"ACTUAL_SOURCE = 'current source'\n")
    monkeypatch.setattr(module, "ROOT", repo)
    loader_type = type(importlib.util.spec_from_file_location("sample", path).loader)
    def forbidden(*args, **kwargs):
        raise AssertionError("pyc 선택 가능한 loader 실행")
    monkeypatch.setattr(loader_type, "exec_module", forbidden)
    assert module._load_bootstrap().ACTUAL_SOURCE == "current source"
    bootstrap_spec = importlib.util.spec_from_file_location("bootstrap_source_test", ROOT / "scripts/dev/pytest_evidence_bootstrap.py")
    bootstrap = importlib.util.module_from_spec(bootstrap_spec)
    exec(compile((ROOT / "scripts/dev/pytest_evidence_bootstrap.py").read_bytes(), bootstrap_spec.origin, "exec"), bootstrap.__dict__)
    monkeypatch.setitem(sys.modules, "_fixed_source_probe", None)
    assert bootstrap._load("_fixed_source_probe", path).ACTUAL_SOURCE == "current source"


def test_main_uses_no_popen_reaper_or_signal_after_successful_spawn(tmp_path, monkeypatch):
    module, repo, guard, streams, args = _fake_main(tmp_path, monkeypatch)
    original = _controller()._RawPopen
    objects = []
    class PoisonPopen(original):
        def __init__(self, *args, **kwargs):
            self.pid = 99
            self.returncode = None
            self._child_created = True
            objects.append(self)
            frame = json.dumps({"schema": "qwq.pytest-guard-ready/v1", "guard": guard}).encode() + b"\n"
            os.write(kwargs["pass_fds"][0], frame)
            (repo / "receipt.json").write_bytes(b"fixed adapter receipt")
        def forbidden(self, *args, **kwargs):
            raise AssertionError("성공 spawn 뒤 Popen 회수/신호 호출")
        poll = wait = communicate = send_signal = terminate = kill = _internal_poll = forbidden
    waited = []
    def wait(pid, flags):
        waited.append(pid)
        if len(waited) == 1:
            return 99, 0
        raise ChildProcessError
    monkeypatch.setattr(module, "_RawPopen", PoisonPopen)
    monkeypatch.setattr(module.os, "waitpid", wait)
    assert module.main(args) == 0
    assert objects[0].returncode == 0
    objects[0].__del__()
    assert waited[0] == 99
    assert all(pid == -1 for pid in waited[1:])


def test_real_known_zombie_is_adopted_and_reaped(tmp_path):
    repo = make_repo(tmp_path, '''import atexit, json, os, signal
from pathlib import Path
def spawn_zombie():
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(8)
    child = os.fork()
    if child == 0:
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.alarm(8)
        os._exit(0)
    status = os.waitid(os.P_PID, child, os.WEXITED | os.WNOWAIT)
    Path('zombie-observation.json').write_text(json.dumps({'code': status.si_code, 'status': status.si_status}))
def test_ok():
    atexit.register(spawn_zombie)
''')
    result = run_case(repo)
    assert json.loads((repo / "zombie-observation.json").read_text()) == {"code": os.CLD_EXITED, "status": 0}
    doc = json.loads((repo / "process.json").read_text())
    assert doc["process"]["descendants_reaped"] == 1
    assert doc["process"]["cleanup_complete"] is True
    assert doc["process"]["returncode"] == 0
    # 정책 경계는 실제 leader 회수 관측 시각이며 커널의 과거 생존 시각이 아니다.
    assert result["rc"] == (125 if doc["process"]["descendant_survived"] else 0)


def test_bound_paths_reject_changed_ancestor_and_replacement_directory(tmp_path, monkeypatch):
    import pytest
    module = _controller()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    for mode in ("ancestor_link", "replacement"):
        parent = tmp_path / mode / "inner"
        parent.mkdir(parents=True)
        path = parent / "process.json"
        bound = module._BoundPaths([path])
        try:
            if mode == "ancestor_link":
                (tmp_path / mode).rename(tmp_path / (mode + "-old"))
                (tmp_path / mode).symlink_to(tmp_path / (mode + "-old"), target_is_directory=True)
            else:
                parent.rename(parent.with_name("old"))
                parent.mkdir()
            with pytest.raises((OSError, ValueError)):
                bound.create(path)
            assert not path.exists()
        finally:
            bound.close()


def test_bound_paths_close_all_parent_fds_and_partial_walk_once(tmp_path, monkeypatch):
    import pytest
    module = _controller()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    (tmp_path / "parent").mkdir()
    bound = module._BoundPaths([tmp_path / "parent" / "result.json"])
    fds = [bound.root_fd, *bound.parents.values()]
    bound.close()
    bound.close()
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)
    real_close = os.close
    attempts = []
    def fails_once(fd):
        attempts.append(fd)
        real_close(fd)
        if len(attempts) == 1:
            raise OSError("고정 walk close 실패")
    monkeypatch.setattr(module.os, "close", fails_once)
    with pytest.raises(OSError):
        module._BoundPaths([tmp_path / "parent" / "another.json"])
    assert len(attempts) == len(set(attempts)) == 3


def test_postspawn_write_end_close_failure_keeps_owned_cleanup(tmp_path, monkeypatch):
    import io
    import types
    module, repo, guard, streams, args = _fake_main(tmp_path, monkeypatch)
    original_writes = []
    child_ends = []
    close_failed = [False]
    phase = ["alive"]
    real_close = os.close
    def spawn(*a, **kwargs):
        original_writes.extend([kwargs["stdout"], kwargs["stderr"], kwargs["pass_fds"][0]])
        child_ends.extend(os.dup(fd) for fd in original_writes)
        frame = json.dumps({"schema": "qwq.pytest-guard-ready/v1", "guard": guard}).encode() + b"\n"
        os.write(child_ends[2], frame)
        return types.SimpleNamespace(pid=99, returncode=None)
    def close(fd):
        if fd == 710:
            return
        real_close(fd)
        if original_writes and fd == original_writes[2] and not close_failed[0]:
            close_failed[0] = True
            raise OSError("고정 postspawn write-end close 실패")
    def send(fd, sig, *a):
        assert (fd, sig) == (710, signal.SIGTERM)
        while child_ends:
            real_close(child_ends.pop())
        phase[0] = "exited"
    def wait(pid, flags):
        if phase[0] == "alive":
            return 0, 0
        if phase[0] == "exited":
            phase[0] = "reaped"
            return 99, signal.SIGTERM
        raise ChildProcessError
    monkeypatch.setattr(module, "_RawPopen", spawn)
    monkeypatch.setattr(module.os, "close", close)
    monkeypatch.setattr(module.os, "waitpid", wait)
    monkeypatch.setattr(module.os, "pidfd_open", lambda *a: 710)
    monkeypatch.setattr(module.signal, "pidfd_send_signal", send)
    monkeypatch.setattr(module, "open", lambda *a, **k: io.BytesIO(b"99"), raising=False)
    try:
        assert module.main(args) == 125
    finally:
        while child_ends:
            real_close(child_ends.pop())
    doc = json.loads((repo / "process.json").read_text())
    assert close_failed == [True]
    assert phase == ["reaped"]
    assert doc["process"]["reason"] == "io_error"
    assert doc["process"]["returncode"] == -15
    assert doc["process"]["cleanup_complete"] is True


def test_receipt_io_failure_after_cleanup_retains_status_and_failure_artifact(tmp_path, monkeypatch):
    module, repo, guard, streams, args = _fake_main(tmp_path, monkeypatch)
    def finished(owner, pipes, *rest):
        owner.record(99, 0)
        for fd in pipes.values():
            os.close(fd)
        pipes.clear()
        return True, guard, streams
    def failed(*args):
        raise OSError("고정 receipt close 실패")
    monkeypatch.setattr(module, "_observe", finished)
    monkeypatch.setattr(module, "_receipt", failed)
    assert module.main(args) == 125
    doc = json.loads((repo / "process.json").read_text())
    assert doc["receipt"] == {"state": "invalid", "bytes": 0, "sha256": None}
    assert doc["process"]["reason"] == "io_error"
    assert doc["process"]["returncode"] == 0
    assert doc["process"]["cleanup_complete"] is True


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("phase", ["posthash", "receipt", "selection", "serialization", "create", "write"])
@pytest.mark.parametrize("first_error", [None, "timeout", "io_error"])
def test_finalization_signal_publication_cutoff_preserves_process_evidence(
        tmp_path, monkeypatch, sig, phase, first_error):
    """후행 중단 유실/경계 뒤 판정 변경/최초 오류 덮어쓰기를 검출한다."""
    from scripts.dev.verification_os_contract import evaluate_controlled_slot

    spec = importlib.util.spec_from_file_location(
        "_finalization_expected", ROOT / "tests/dev/test_controlled_verification_evidence.py")
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    module, repo, guard, streams, args = _fake_main(tmp_path, monkeypatch)
    expected = helpers._expected_before_run(repo, CONTEXT["run"])
    slot = expected["verification"]["slots"][0]
    receipt_raw = json.dumps({
        "schema": "qwq.verification-receipt/v1", **CONTEXT,
        "identity": slot["identity"], "collected": ["tests/test_tiny.py::test_ok"],
        "results": [{"nodeid": "tests/test_tiny.py::test_ok", "setup": "passed",
                     "call": "passed", "teardown": "passed"}],
        "session": {"finished": True, "exit_code": 0, "collection_errors": 0, "deselected": 0},
        "guard": guard,
    }).encode()
    callbacks = {}
    observed = [False]
    injected = []
    monkeypatch.setattr(module.signal, "signal", lambda number, callback: callbacks.__setitem__(number, callback))

    def interrupt():
        injected.append(phase)
        callbacks[sig](sig, None)

    def finished(owner, pipes, *rest):
        owner.record(99, 0)
        if first_error is not None:
            owner.reject(first_error)
        for fd in pipes.values():
            os.close(fd)
        pipes.clear()
        (repo / "receipt.json").write_bytes(receipt_raw)
        observed[0] = True
        return True, guard, streams

    monkeypatch.setattr(module, "_observe", finished)
    real_hash, real_receipt, real_canonical = module._hash, module._receipt, module._canonical
    real_create = module._BoundPaths.create

    def digest(path):
        value = real_hash(path)
        if phase == "posthash" and observed[0] and not injected:
            interrupt()
        return value

    def receipt(*args):
        value = real_receipt(*args)
        if phase == "receipt":
            interrupt()
        return value

    def canonical(value):
        if ((phase == "selection" and type(value) is list)
                or (phase == "serialization" and type(value) is dict)):
            interrupt()
        return real_canonical(value)

    def create(bounds, path, **kwargs):
        stream = real_create(bounds, path, **kwargs)
        if path == repo / "process.json":
            if phase == "create":
                interrupt()
            elif phase == "write":
                real_write = stream.write

                def write(raw):
                    interrupt()
                    return real_write(raw)

                stream.write = write
        return stream

    monkeypatch.setattr(module, "_hash", digest)
    monkeypatch.setattr(module, "_receipt", receipt)
    monkeypatch.setattr(module, "_canonical", canonical)
    monkeypatch.setattr(module._BoundPaths, "create", create)
    rc = module.main(args)
    doc = json.loads((repo / "process.json").read_bytes())
    reason = first_error or ("interrupted" if phase in ("posthash", "receipt") else "exited")
    assert injected == [phase]
    assert rc == {"timeout": 124, "io_error": 125, "interrupted": 125, "exited": 0}[reason]
    assert doc["process"]["reason"] == reason
    assert doc["process"]["returncode"] == 0
    assert doc["process"]["leader_reaped"] is True
    assert doc["process"]["cleanup_complete"] is True
    decision = evaluate_controlled_slot(receipt_raw, (repo / "process.json").read_bytes(), expected)
    assert decision["status"] == ("OS_RESULT_BOUND" if reason == "exited" else "REJECTED")
    assert decision["errors"] == ([] if reason == "exited" else ["PROCESS_EXIT_REJECTED"])


def test_b1_copied_fixture_isolation_is_exact_and_default_bytes_stay_unchanged(tmp_path):
    original = (ROOT / "scripts/dev/pytest_evidence_controller.py").read_bytes()
    old_lock = b'    _LOCK = "/home/ubuntu/projects/qwq-ai-trader/.claude/worktrees/owner-ticket-gate-20260926/.superpowers/sdd/2026-09-27-runtime-admission-contract/test-workload.lock"\n'
    old_total = b"        self.total_end = started + 900\n"
    short_total = b"        self.total_end = started + 6\n"
    assert original.count(old_lock) == original.count(old_total) == 1
    assert original.count(short_total) == 0
    default = make_repo(tmp_path / "default")
    assert (default / "scripts/dev/pytest_evidence_controller.py").read_bytes() == original
    assert not (default / ".b1-fixture").exists()
    for label, short in (("normal", False), ("duration", True)):
        repo = make_repo(tmp_path / label, b1_isolated=True, b1_short_budget=short)
        lock = repo / ".b1-fixture/test-workload.lock"
        info = lock.lstat()
        assert stat.S_ISREG(info.st_mode) and not lock.is_symlink()
        assert info.st_uid == os.getuid() and info.st_nlink == 1
        assert stat.S_IMODE(info.st_mode) == 0o600
        assert stat.S_IMODE(lock.parent.stat().st_mode) == 0o700
        new_lock = ("    _LOCK = " + json.dumps(str(lock)) + "\n").encode()
        expected = original.replace(old_lock, new_lock)
        if short:
            expected = expected.replace(old_total, short_total)
        copied = (repo / "scripts/dev/pytest_evidence_controller.py").read_bytes()
        assert copied == expected and copied.count(new_lock) == 1 and copied.count(old_lock) == 0
        assert copied.count(old_total) == (0 if short else 1)
        assert copied.count(short_total) == (1 if short else 0)
        for unchanged in (b"        self.run_end = self.total_end - 4\n",
                          b"            self.cleanup_end = min(now + 3, self.total_end - 1)\n",
                          b"            self.term_end = min(now + 1, self.cleanup_end)\n"):
            assert copied.count(unchanged) == 1


@pytest.mark.parametrize(("isolated", "short"), [(False, True), (0, False), (None, False), ("true", False), (True, 1)])
def test_b1_copied_fixture_invalid_mode_has_no_filesystem_effect(tmp_path, isolated, short):
    target = tmp_path / "untouched"
    with pytest.raises(ValueError):
        make_repo(target, b1_isolated=isolated, b1_short_budget=short)
    assert not target.exists()


@pytest.mark.parametrize("fault", ["missing_lock", "duplicate_lock", "missing_total", "changed_cleanup"])
def test_b1_copied_fixture_rejects_unreviewed_source_before_private_lock(tmp_path, monkeypatch, fault):
    raw = (ROOT / "scripts/dev/pytest_evidence_controller.py").read_bytes()
    old_lock = b'    _LOCK = "/home/ubuntu/projects/qwq-ai-trader/.claude/worktrees/owner-ticket-gate-20260926/.superpowers/sdd/2026-09-27-runtime-admission-contract/test-workload.lock"\n'
    if fault == "missing_lock":
        raw = raw.replace(old_lock, b'    _LOCK = "/not-the-reviewed-lock"\n')
    elif fault == "duplicate_lock":
        raw += old_lock
    elif fault == "missing_total":
        raw = raw.replace(b"        self.total_end = started + 900\n", b"        self.total_end = started + 6\n")
    else:
        raw = raw.replace(b"            self.cleanup_end = min(now + 3, self.total_end - 1)\n", b"            self.cleanup_end = now + 4\n")
    fake_root = tmp_path / "source"
    (fake_root / "scripts/dev").mkdir(parents=True)
    (fake_root / "scripts/dev/pytest_evidence_controller.py").write_bytes(raw)
    monkeypatch.setattr(sys.modules[__name__], "ROOT", fake_root)
    with pytest.raises(ValueError, match="B1_FIXTURE_ANCHOR"):
        make_repo(tmp_path / "copy", b1_isolated=True)
    assert not (tmp_path / "copy/repo/.b1-fixture").exists()


@pytest.mark.parametrize("symlink", [False, True])
def test_b1_copied_fixture_never_reuses_existing_lock(tmp_path, symlink):
    directory = tmp_path / "repo/.b1-fixture"
    directory.mkdir(parents=True, mode=0o700)
    victim = tmp_path / "preserve"
    victim.write_bytes(b"preserve")
    lock = directory / "test-workload.lock"
    if symlink:
        lock.symlink_to(victim)
    else:
        lock.write_bytes(b"existing-lock")
    with pytest.raises(FileExistsError):
        make_repo(tmp_path, b1_isolated=True)
    assert victim.read_bytes() == b"preserve"
    assert lock.is_symlink() is symlink
    assert lock.read_bytes() == (b"preserve" if symlink else b"existing-lock")


@pytest.mark.parametrize("component", ["parent", "repo", "scripts"])
def test_b1_copied_fixture_rejects_symlinked_parent_before_copy(tmp_path, component):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    if component == "parent":
        alias.symlink_to(real, target_is_directory=True)
    elif component == "repo":
        alias.mkdir()
        (alias / "repo").symlink_to(real, target_is_directory=True)
    else:
        (alias / "repo").mkdir(parents=True)
        (alias / "repo/scripts").symlink_to(real, target_is_directory=True)
    expected = FileExistsError if component == "scripts" else ValueError
    with pytest.raises(expected, match=None if component == "scripts" else "B1_FIXTURE_PATH"):
        make_repo(alias, b1_isolated=True)
    assert list(real.iterdir()) == []


def _b1_copied_args():
    return ["--profile", "b1-standard/v1", "--verification-context", "context.json",
            "--verification-output", "receipt.json", "--process-output", "process.json",
            "--timeout-seconds", "900", "--", "tests/test_tiny.py"]


def _b1_copied_identity(repo):
    paths = {"controller": repo / "scripts/dev/pytest_evidence_controller.py",
             "bootstrap": repo / "scripts/dev/pytest_evidence_bootstrap.py",
             "producer": repo / "scripts/dev/pytest_evidence.py", "guard": repo / "tests/conftest.py",
             "executable": Path(sys.executable)}
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}


def _assert_b1_copied_document(repo, result, identity):
    """독립 파일/리터럴만 대조한다. 변환된 복사본을 실프로파일 자격으로 쓰지 않는다."""
    import fcntl
    assert result["complete"] is True
    assert len(result["statuses"]) == 1  # 후보가 남긴 자손을 하네스가 대신 회수한 성공을 배제한다.
    raw = (repo / "process.json").read_bytes()
    assert 0 < len(raw) <= 65536
    doc = json.loads(raw)
    assert doc["schema"] == "qwq.verification-process-result/v2"
    assert doc["scope"] == "local_b1_process_only"
    assert doc["run"] == CONTEXT["run"] and doc["slot"] == CONTEXT["slot"]
    assert doc["identity"] == identity == _b1_copied_identity(repo)
    assert doc["launch"] == {
        "profile": "b1-standard/v1", "process_scope": "linux-subreaper/v1", "timeout_seconds": 900,
        "budget_profile": "inclusive-lock-cleanup-publication/v1", "retained_stream_bytes": 2097152,
        "overflow_observed_bytes": 2097153, "pytest_profile": "b1-x-fixed-plugins/v1",
        "environment_profile": "b1-env-i-fake-key/v1", "selection_profile": "tests-path-only/v1",
        "lock_profile": "owner-ticket-workload/v1",
        "selection_sha256": hashlib.sha256(b'["tests/test_tiny.py"]').hexdigest(),
    }
    assert doc["process"]["ownership_probe_passed"] is True
    assert doc["process"]["leader_reaped"] is True
    assert type(doc["process"]["returncode"]) is int
    assert doc["process"]["parent_guard"] == {
        "path": "tests/conftest.py", "sha256": GUARD_HASH, "module_count": 1, "violations": 0}
    facts = doc["coordination"]
    assert set(facts) == {"lock_acquired", "lock_identity_stable", "fake_key_absent_before",
                          "fake_key_absent_after", "fake_directory_removed", "fake_key_path_sha256"}
    for name in ("lock_acquired", "lock_identity_stable", "fake_key_absent_before",
                 "fake_key_absent_after", "fake_directory_removed"):
        assert facts[name] is True
    assert type(facts["fake_key_path_sha256"]) is str and len(facts["fake_key_path_sha256"]) == 64
    assert all(char in "0123456789abcdef" for char in facts["fake_key_path_sha256"])
    assert set(doc["streams"]) == {"stdout", "stderr"}
    for name, item in doc["streams"].items():
        saved = (repo / f"process.json.{name}.log").read_bytes()
        assert set(item) == {"bytes", "sha256", "overflow", "observed_bytes"}
        assert type(item["bytes"]) is int and len(saved) == item["bytes"] <= 2097152
        assert type(item["observed_bytes"]) is int and len(saved) <= item["observed_bytes"] <= 2097153
        assert type(item["overflow"]) is bool
        assert item["sha256"] == hashlib.sha256(saved).hexdigest()
        if item["overflow"]:
            assert item["observed_bytes"] == 2097153
    # JSON 후보의 lock 플래그만 믿지 않고, 반환 후 실제 독점 잠금 해제를 확인한다.
    with (repo / ".b1-fixture/test-workload.lock").open("rb") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return doc


def _assert_b1_copied_regular_receipt(repo, doc):
    raw = (repo / "receipt.json").read_bytes()
    assert doc["receipt"] == {"state": "regular", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    assert doc["process"]["guard"] == {
        "path": "tests/conftest.py", "sha256": GUARD_HASH, "module_count": 1, "violations": 0}
    assert doc["process"]["cleanup_complete"] is True


@pytest.mark.parametrize(("body", "cli", "raw_status", "reason"), [
    ("def test_ok():\n    assert True\n", 0, 0, "exited"),
    ("def test_failure():\n    assert False\n", 1, 1, "exited"),
    ("import atexit, os\ndef test_ok():\n    atexit.register(lambda: os._exit(9))\n", 9, 9, "exited"),
    ("import atexit, signal\ndef test_ok():\n    atexit.register(lambda: signal.raise_signal(signal.SIGTERM))\n", 143, -15, "signaled"),
], ids=["pass", "fail", "exit-nine", "sigterm"])
def test_b1_copied_real_status_and_receipt(tmp_path, body, cli, raw_status, reason):
    repo = make_repo(tmp_path, body, b1_isolated=True)
    identity = _b1_copied_identity(repo)
    result = run_case(repo, args=_b1_copied_args())
    assert result["rc"] == cli
    doc = _assert_b1_copied_document(repo, result, identity)
    _assert_b1_copied_regular_receipt(repo, doc)
    assert doc["process"]["returncode"] == raw_status and doc["process"]["reason"] == reason
    assert doc["process"]["descendant_survived"] is False
    assert doc["process"]["descendants_reaped"] == 0
    assert all(item["overflow"] is False for item in doc["streams"].values())


def test_b1_copied_real_receipt_then_short_budget_timeout(tmp_path):
    repo = make_repo(tmp_path, "import atexit, time\ndef test_ok():\n    atexit.register(lambda: time.sleep(7))\n",
                     b1_isolated=True, b1_short_budget=True)
    identity = _b1_copied_identity(repo)
    result = run_case(repo, args=_b1_copied_args())
    assert result["rc"] == 124
    doc = _assert_b1_copied_document(repo, result, identity)
    _assert_b1_copied_regular_receipt(repo, doc)  # 필수 체크포인트: 기동 전 시간초과는 대체 성공이 아니다.
    assert doc["process"]["returncode"] == -15 and doc["process"]["reason"] == "timeout"
    assert doc["process"]["term_sent"] is True and doc["process"]["descendant_survived"] is False


@pytest.mark.parametrize("late", [False, True], ids=["double-fork", "late-adoption"])
def test_b1_copied_real_detached_descendants_are_reaped(tmp_path, late):
    body = '''import atexit, os, signal, time
def backstop():
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(8)
def spawn():
    backstop()
    ready_read, ready_write = os.pipe()
    if os.fork() == 0:
        os.close(ready_read)
        backstop()
        os.setsid()
        if os.fork() == 0:
            backstop()
            if os.write(ready_write, b'R') != 1:
                os._exit(96)
            os.close(ready_write)
            time.sleep(7)
            os._exit(0)
        os.close(ready_write)
        os._exit(0)
    os.close(ready_write)
    ready = os.read(ready_read, 1)
    os.close(ready_read)
    if ready != b'R':
        os._exit(96)
    os.write(1, b'B1_COPIED_DESCENDANTS_READY\\n')
def test_ok():
    atexit.register(spawn)
'''
    if late:
        body = '''import atexit, os, signal, time
def backstop():
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(8)
def spawn():
    backstop()
    ready_read, ready_write = os.pipe()
    if os.fork() == 0:
        os.close(ready_read)
        backstop()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if os.fork() == 0:
            backstop()
            os.setsid()
            if os.write(ready_write, b'R') != 1:
                os._exit(96)
            os.close(ready_write)
            time.sleep(7)
            os._exit(0)
        os.close(ready_write)
        time.sleep(7)
        os._exit(0)
    os.close(ready_write)
    ready = os.read(ready_read, 1)
    os.close(ready_read)
    if ready != b'R':
        os._exit(96)
    os.write(1, b'B1_COPIED_DESCENDANTS_READY\\n')
def test_ok():
    atexit.register(spawn)
'''
    repo = make_repo(tmp_path, body, b1_isolated=True)
    identity = _b1_copied_identity(repo)
    result = run_case(repo, args=_b1_copied_args())
    assert result["rc"] == 125
    doc = _assert_b1_copied_document(repo, result, identity)
    _assert_b1_copied_regular_receipt(repo, doc)
    assert doc["process"]["returncode"] == 0 and doc["process"]["reason"] == "cleanup_error"
    assert doc["process"]["descendant_survived"] is True and doc["process"]["descendants_reaped"] == 2
    assert (repo / "process.json.stdout.log").read_bytes().count(b"B1_COPIED_DESCENDANTS_READY\n") == 1
    if late:
        assert doc["process"]["kill_sent"] is True


def test_b1_copied_real_both_streams_flood_are_bounded(tmp_path):
    repo = make_repo(tmp_path, '''import atexit, os, signal
def flood():
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(8)
    for _ in range(150):
        os.write(1, b'x' * 65536)
        os.write(2, b'y' * 65536)
def test_ok():
    atexit.register(flood)
''', b1_isolated=True)
    identity = _b1_copied_identity(repo)
    result = run_case(repo, args=_b1_copied_args())
    assert result["rc"] == 125
    doc = _assert_b1_copied_document(repo, result, identity)
    _assert_b1_copied_regular_receipt(repo, doc)
    assert doc["process"]["reason"] == "output_limit"
    assert any(item["overflow"] is True for item in doc["streams"].values())
    assert all(item["bytes"] > 0 for item in doc["streams"].values())


def test_b1_copied_real_bad_guard_frame_is_not_success(tmp_path):
    repo = make_repo(tmp_path, b1_isolated=True)
    path = repo / "scripts/dev/pytest_evidence_bootstrap.py"
    before = path.read_bytes()
    old = b"        if os.write(control, frame) != len(frame):\n            raise OSError\n"
    new = (b"        frame = frame.replace(b'" + GUARD_HASH.encode() + b"', b'0' * 64)\n" + old
           + b'        os.write(2, b"B1_COPIED_BAD_GUARD_SENT\\n")\n')
    assert before.count(old) == 1 and before.count(new) == 0
    transformed = before.replace(old, new)
    assert transformed.count(old) == 1 and transformed.count(new) == 1
    path.write_bytes(transformed)
    assert path.read_bytes() == transformed
    identity = _b1_copied_identity(repo)
    result = run_case(repo, args=_b1_copied_args())
    assert result["rc"] == 125
    doc = _assert_b1_copied_document(repo, result, identity)
    assert doc["process"]["guard"] is None and doc["process"]["reason"] == "startup_error"
    assert doc["process"]["cleanup_complete"] is True
    assert (repo / "process.json.stderr.log").read_bytes().count(b"B1_COPIED_BAD_GUARD_SENT\n") == 1


def test_b1_copied_real_contended_lock_has_no_published_workload(tmp_path):
    import fcntl
    repo = make_repo(tmp_path, b1_isolated=True, b1_short_budget=True)
    lock = repo / ".b1-fixture/test-workload.lock"
    before = lock.stat()
    identity = _b1_copied_identity(repo)
    with lock.open("rb") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = run_case(repo, args=_b1_copied_args())
            assert result["rc"] == 124 and result["complete"] is True
            assert len(result["statuses"]) == 1
            for name in ("receipt.json", "process.json", "process.json.stdout.log", "process.json.stderr.log"):
                assert not (repo / name).exists()
            after = lock.stat()
            assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
            assert lock.read_bytes() == b""
            assert _b1_copied_identity(repo) == identity
        finally:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)


def test_b1_copied_real_observer_escape_keeps_incomplete_evidence(tmp_path):
    assert hashlib.sha256((ROOT / "scripts/dev/pytest_evidence_controller.py").read_bytes()).hexdigest() == "bebf44daa73861efb52749a0f9dcbf047aa00665418ae09396512756f3ede1b0"
    repo = make_repo(tmp_path, b1_isolated=True)
    path = repo / "scripts/dev/pytest_evidence_controller.py"
    before = path.read_bytes()
    old = ('def _observe(owner, pipes, logs, expected_guard, deadline, stopped, *, profile=None):\n'
           '    """각 fd/회수 배치를 제한하여 기한과 다른 파이프를 굶기지 않는다."""\n').encode()
    new = old + b'    if profile == "b1-standard/v1":\n        os.write(2, b"B1_COPIED_OBSERVER_FAULT\\n")\n        raise RuntimeError("B1_COPIED_OBSERVER_FAULT")\n'
    assert before.count(old) == 1 and before.count(new) == 0
    transformed = before.replace(old, new)
    assert transformed.count(old) == 1 and transformed.count(new) == 1
    path.write_bytes(transformed)
    assert path.read_bytes() == transformed
    identity = _b1_copied_identity(repo)
    result = run_case(repo, args=_b1_copied_args())
    assert result["rc"] == 125
    assert (repo / "harness-child-2.log").read_bytes() == b"B1_COPIED_OBSERVER_FAULT\n" * 2
    doc = _assert_b1_copied_document(repo, result, identity)
    assert doc["process"]["cleanup_complete"] is False and doc["process"]["reason"] == "cleanup_error"
    assert doc["process"]["guard"] is None
    assert doc["receipt"] == {"state": "invalid", "bytes": 0, "sha256": None}
    assert doc["streams"] == {name: {"bytes": 0, "sha256": hashlib.sha256(b"").hexdigest(),
                                      "overflow": False, "observed_bytes": 0} for name in ("stdout", "stderr")}


def test_b1_copied_real_crash_after_candidate_has_nonzero_terminal(tmp_path):
    assert hashlib.sha256((ROOT / "scripts/dev/pytest_evidence_controller.py").read_bytes()).hexdigest() == "bebf44daa73861efb52749a0f9dcbf047aa00665418ae09396512756f3ede1b0"
    repo = make_repo(tmp_path, b1_isolated=True)
    path = repo / "scripts/dev/pytest_evidence_controller.py"
    before = path.read_bytes()
    old = b"        if finalization_failed:\n            return 125\n"
    new = b'        if publication_complete:\n            os.write(2, b"B1_COPIED_POST_CANDIDATE_CRASH\\n")\n            os._exit(97)\n' + old
    assert before.count(old) == 1 and before.count(new) == 0
    transformed = before.replace(old, new)
    assert transformed.count(old) == 1 and transformed.count(new) == 1
    path.write_bytes(transformed)
    assert path.read_bytes() == transformed
    identity = _b1_copied_identity(repo)
    result = run_case(repo, args=_b1_copied_args())
    assert result["rc"] == 97
    assert (repo / "harness-child-2.log").read_bytes() == b"B1_COPIED_POST_CANDIDATE_CRASH\n"
    doc = _assert_b1_copied_document(repo, result, identity)
    _assert_b1_copied_regular_receipt(repo, doc)
    assert doc["process"]["returncode"] == 0 and doc["process"]["reason"] == "exited"
    assert doc["process"]["descendant_survived"] is False and doc["process"]["descendants_reaped"] == 0
    assert all(item["overflow"] is False for item in doc["streams"].values())


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--harness":
        raise SystemExit(_harness(Path(sys.argv[2]), sys.argv[3] == "dummy", sys.argv[3] == "interrupt"))
    raise SystemExit(94)
