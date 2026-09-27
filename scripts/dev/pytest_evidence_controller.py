"""Linux 전용 CLI: 한 pytest 자식의 실제 종료와 회수 증거를 남긴다.

모듈 import는 소유권이나 신호 상태를 바꾸지 않는다. 모든 wait는 단일
기록기로 모으며 신호 권한은 매번 pidfd를 연 뒤 child wait로 확인한다.
"""

from __future__ import annotations

import argparse
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


WALL = 0x40000000
LIMIT = 8 * 1024 * 1024
ROOT = Path(__file__).resolve().parents[2]


def _load_bootstrap():
    spec = importlib.util.spec_from_file_location("_qwq_bootstrap", ROOT / "scripts/dev/pytest_evidence_bootstrap.py")
    module = importlib.util.module_from_spec(spec)
    exec(compile((ROOT / "scripts/dev/pytest_evidence_bootstrap.py").read_bytes(),
                 str(ROOT / "scripts/dev/pytest_evidence_bootstrap.py"), "exec"), module.__dict__)
    return module


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path(value, *, new=False):
    path = Path(value)
    if ".." in path.parts:
        raise ValueError
    path = Path(os.path.abspath(path))
    if not path.is_relative_to(ROOT):
        raise ValueError
    for candidate in [path, *path.parents]:
        if candidate.is_symlink():
            raise ValueError
    if new:
        if path.exists() or not path.parent.is_dir():
            raise ValueError
    elif not path.exists() or not (path.is_file() or path.is_dir()):
        raise ValueError
    return path


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("PROCESS_ARGUMENTS")


def _arguments(argv):
    if Path.cwd() != ROOT or "PYTEST_ADDOPTS" in os.environ or "PYTEST_PLUGINS" in os.environ:
        raise ValueError
    parser = _Parser(allow_abbrev=False, add_help=False)
    names = ("--verification-context", "--verification-output", "--process-output", "--timeout-seconds")
    for name in names:
        parser.add_argument(name, required=True)
    split = argv.index("--")
    flags = [item.split("=", 1)[0] for item in argv[:split]]
    if any(flags.count(name) != 1 for name in names):
        raise ValueError
    options = parser.parse_args(argv[:split])
    if not options.timeout_seconds.isascii() or not options.timeout_seconds.isdecimal():
        raise ValueError
    options.timeout_seconds = int(options.timeout_seconds)
    if not 1 <= options.timeout_seconds <= 900:
        raise ValueError
    context = _path(options.verification_context)
    if not context.is_file() or context.stat().st_size > 65536:
        raise ValueError
    receipt = _path(options.verification_output, new=True)
    output = _path(options.process_output, new=True)
    logs = [_path(str(output) + suffix, new=True) for suffix in (".stdout.log", ".stderr.log")]
    paths = [receipt, output, *logs]
    if len(set(paths)) != 4 or context in paths:
        raise ValueError
    selected = []
    for value in argv[split + 1:]:
        if not value or value.startswith("-") or any(c in value for c in ("::", "*", "?", "[", "]")):
            raise ValueError
        path = _path(value)
        relative = path.relative_to(ROOT).as_posix()
        if not path.is_relative_to(ROOT / "tests") or path.is_relative_to(ROOT / "tests/proofs"):
            raise ValueError
        if path in paths or any(out.is_relative_to(path) for out in paths):
            raise ValueError
        if relative in selected:
            raise ValueError
        selected.append(relative)
    if not selected:
        raise ValueError
    return options, context, receipt, output, logs, selected


class _RawPopen(subprocess.Popen):
    def __del__(self):
        """미회수 상태도 Popen의 암묵적 poll/가짜 rc0으로 덮지 않는다."""


class _Owner:
    """관측 상태와 raw wait 기록의 유일한 소유자다."""

    def __init__(self):
        self.leader = None
        self.popen = None
        self.returncode = None
        self.reaped = False
        self.descendants = 0
        self.survived = False
        self.term = False
        self.kill = False
        self.error = None
        self.finish_end = None
        self.observation = None
        self.startup_end = None

    def reject(self, reason):
        if self.error is None:
            self.error = reason

    def record(self, pid, status):
        if not (os.WIFEXITED(status) or os.WIFSIGNALED(status)):
            self.reject("cleanup_error")
            return
        if pid == self.leader and not self.reaped:
            self.returncode = os.waitstatus_to_exitcode(status)
            self.reaped = True
            if self.popen is not None:
                self.popen.returncode = self.returncode
        else:
            if self.reaped:
                self.survived = True
            self.descendants += 1
            if self.descendants > 20000:
                self.reject("cleanup_error")

    def wait(self, pid):
        try:
            actual, status = os.waitpid(pid, os.WNOHANG | WALL)
        except ChildProcessError:
            return "empty"
        except InterruptedError:
            return "retry"
        except OSError:
            self.reject("cleanup_error")
            return "retry"
        if actual:
            self.record(actual, status)
            return "status"
        if self.reaped:
            self.survived = True
        return "alive"

    def reap(self):
        if self.leader is not None and not self.reaped:
            if self.wait(self.leader) == "empty":
                self.reject("cleanup_error")
        for _ in range(64):
            result = self.wait(-1)
            if result != "status":
                return result == "empty"
        return False

    def signal_candidate(self, pid, sig):
        try:
            fd = os.pidfd_open(pid, 0)
        except ProcessLookupError:
            return
        except OSError:
            self.reject("cleanup_error")
            return
        try:
            result = self.wait(pid)
            if result != "alive":
                if result == "empty":
                    self.reject("cleanup_error")
                return
            try:
                signal.pidfd_send_signal(fd, sig, None, 0)
                if sig == signal.SIGTERM:
                    self.term = True
                elif sig == signal.SIGKILL:
                    self.kill = True
            except ProcessLookupError:
                pass
            except OSError:
                self.reject("cleanup_error")
        finally:
            os.close(fd)

    def signal_children(self, sig):
        try:
            with open(f"/proc/self/task/{os.getpid()}/children", "rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                self.reject("cleanup_error")
            for token in raw[:65536].split()[:64]:
                if not token.isdigit() or int(token) <= 0:
                    self.reject("cleanup_error")
                    continue
                self.signal_candidate(int(token), sig)
        except (OSError, ValueError):
            self.reject("cleanup_error")


def _preflight():
    if sys.platform != "linux" or len(os.listdir("/proc/self/task")) != 1:
        raise ValueError
    with open(f"/proc/self/task/{os.getpid()}/children", "rb") as stream:
        if stream.read(65537).strip():
            raise ValueError
    try:
        os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT | WALL)
    except ChildProcessError:
        pass
    else:
        raise ValueError
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    libc = ctypes.CDLL(None, use_errno=True)
    actual = ctypes.c_int()
    if libc.prctl(36, 1, 0, 0, 0) or libc.prctl(37, ctypes.byref(actual), 0, 0, 0) or actual.value != 1:
        raise ValueError
    fd = os.pidfd_open(os.getpid(), 0)
    try:
        signal.pidfd_send_signal(fd, 0, None, 0)
    finally:
        os.close(fd)


def _probe(deadline, stopped):
    owner = _Owner()
    owner.leader = os.fork()
    if owner.leader == 0:
        os._exit(23)
    end = min(deadline, time.monotonic() + 1)
    while time.monotonic() < end and not stopped[0]:
        empty = owner.reap()
        if empty and owner.reaped:
            return owner.returncode == 23 and owner.error is None and time.monotonic() <= end
        time.sleep(.005)
    finish = time.monotonic() + 3
    while time.monotonic() < finish:
        if owner.reap():
            break
        owner.signal_children(signal.SIGTERM if time.monotonic() < finish - 2 else signal.SIGKILL)
        time.sleep(.005)
    return False


def _frame(raw, expected):
    def pairs(items):
        doc = {}
        for key, value in items:
            if key in doc:
                raise ValueError
            doc[key] = value
        return doc
    if len(raw) > 4096 or not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise ValueError
    doc = json.loads(raw.decode(), object_pairs_hook=pairs)
    if type(doc) is not dict or set(doc) != {"schema", "guard"} or doc["schema"] != "qwq.pytest-guard-ready/v1":
        raise ValueError
    guard = doc["guard"]
    if type(guard) is not dict or guard != expected or any(type(guard.get(k)) is not int for k in ("module_count", "violations")):
        raise ValueError
    return guard


def _receipt(path):
    invalid = {"state": "invalid", "bytes": 0, "sha256": None}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {"state": "missing", "bytes": 0, "sha256": None}
    except OSError:
        return invalid
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return invalid
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(32 * 1024 * 1024 + 1)
        if len(raw) > 32 * 1024 * 1024:
            return invalid
        return {"state": "regular", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    except OSError:
        return invalid
    finally:
        os.close(fd)


def _observe(owner, pipes, logs, expected_guard, deadline, stopped):
    """각 fd/회수 배치를 제한하여 기한과 다른 파이프를 굶기지 않는다."""
    if owner.observation is None:
        owner.observation = {
            "streams": {name: {"bytes": 0, "sha256": hashlib.sha256(), "overflow": False}
                        for name in ("stdout", "stderr")},
            "observed": {"stdout": 0, "stderr": 0}, "failed_logs": set(),
            "control": bytearray(), "guard": None,
            "startup_end": (min(deadline, time.monotonic() + 10) if owner.startup_end is None
                            else min(deadline, owner.startup_end)),
        }
    state = owner.observation
    streams, observed, failed_logs = state["streams"], state["observed"], state["failed_logs"]
    pending = pipes
    control = state["control"]
    guard = state["guard"]
    complete = False
    startup_end = state["startup_end"]

    def close_pipe(name):
        fd = pending.pop(name)
        try:
            os.close(fd)
        except OSError:
            owner.reject("io_error")

    while True:
        now = time.monotonic()
        if stopped[0]:
            owner.reject("interrupted")
        if now >= deadline and owner.finish_end is None:
            owner.reject("timeout")
        if guard is None and now >= startup_end:
            owner.reject("startup_error")
        empty = owner.reap()
        if owner.finish_end is None and (owner.reaped or owner.error):
            owner.finish_end = now + 3
        for name, fd in tuple(pending.items()):
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            except InterruptedError:
                continue
            except OSError:
                owner.reject("io_error")
                continue
            if not chunk:
                close_pipe(name)
                if name == "control":
                    try:
                        guard = _frame(bytes(control), expected_guard)
                        state["guard"] = guard
                    except (ValueError, UnicodeError, TypeError):
                        owner.reject("startup_error")
                continue
            if name == "control":
                control.extend(chunk[:max(0, 4097 - len(control))])
                if len(control) > 4096 or control.count(b"\n") > 1 or (b"\n" in control and not control.endswith(b"\n")):
                    owner.reject("startup_error")
            else:
                item = streams[name]
                observed[name] = min(LIMIT + 1, observed[name] + len(chunk))
                if name not in failed_logs:
                    prefix = chunk[:max(0, LIMIT + 1 - item["bytes"])]
                    try:
                        # 무버퍼 파일의 실제 write 결과만 보존 prefix로 집계한다.
                        written = logs[name].write(prefix)
                        if type(written) is not int or not 0 <= written <= len(prefix):
                            raise OSError
                        item["bytes"] += written
                        item["sha256"].update(prefix[:written])
                        if written != len(prefix):
                            raise OSError
                    except OSError:
                        failed_logs.add(name)
                        owner.reject("io_error")
                if observed[name] > LIMIT:
                    item["overflow"] = True
                    owner.reject("output_limit")
        if owner.finish_end is None and owner.error:
            owner.finish_end = time.monotonic() + 3
        if owner.reaped and not pending and owner.wait(-1) == "empty":
            complete = owner.finish_end is not None and time.monotonic() <= owner.finish_end
            if not complete:
                owner.reject("cleanup_error")
            break
        if owner.finish_end is not None:
            if time.monotonic() >= owner.finish_end:
                owner.reject("cleanup_error")
                break
            if not empty:
                owner.signal_children(signal.SIGTERM if time.monotonic() < owner.finish_end - 2 else signal.SIGKILL)
        time.sleep(.005)
    for name in tuple(pending):
        close_pipe(name)
    result = {name: {**item, "sha256": item["sha256"].hexdigest()} for name, item in streams.items()}
    return complete, guard, result


def _close_descriptors(descriptors, owner):
    """닫기 시도 전에 소유권 목록에서 제거하여 예외 뒤 중복 close를 막는다."""
    while descriptors:
        fd = descriptors.pop()
        try:
            os.close(fd)
        except OSError:
            owner.reject("io_error")


def _emergency_cleanup(owner, pipes):
    """관측기 자체 실패에도 원래 회수 기한 내 양성 자식만 정리한다.

    파이프 관측을 잃은 이 경로는 회수 성공을 인증하지 않는다.
    """
    owner.reject("cleanup_error")
    if owner.finish_end is None:
        owner.finish_end = time.monotonic() + 3
    while time.monotonic() < owner.finish_end:
        if owner.reap():
            break
        owner.signal_children(signal.SIGTERM if time.monotonic() < owner.finish_end - 2 else signal.SIGKILL)
        time.sleep(.005)
    descriptors = list(pipes.values())
    pipes.clear()
    _close_descriptors(descriptors, owner)


def main(argv: list[str] | None = None) -> int:
    started = time.monotonic()
    try:
        options, context_path, receipt_path, output, log_paths, selected = _arguments(list(sys.argv[1:] if argv is None else argv))
        bootstrap = _load_bootstrap()
        parent_guard = bootstrap._guard(ROOT, install=True)
        if parent_guard["violations"]:
            raise ValueError
        producer = bootstrap._load("_qwq_parent_producer", ROOT / "scripts/dev/pytest_evidence.py")
        context, context_raw = producer._read_context(context_path)
        if (len(context_raw) > 65536 or context["run"]["event"] != "local"
                or context["slot"]["lane"] != "standard"):
            raise ValueError
        code = {"controller": Path(__file__).resolve(), "bootstrap": ROOT / "scripts/dev/pytest_evidence_bootstrap.py",
                "producer": ROOT / "scripts/dev/pytest_evidence.py", "guard": ROOT / "tests/conftest.py",
                "executable": Path(sys.executable)}
        identity = {name: _hash(path) for name, path in code.items()}
    except (OSError, ValueError, TypeError, ImportError, AttributeError):
        return 125
    deadline = started + options.timeout_seconds
    stopped = [False]
    owner = _Owner()
    owner.startup_end = min(deadline, started + 10)
    probe = False
    guard = None
    complete = False
    streams = {name: {"bytes": 0, "sha256": hashlib.sha256(b"").hexdigest(), "overflow": False} for name in ("stdout", "stderr")}
    logs = {}
    pipes = {}
    writes = []
    spawn_attempted = False
    try:
        signal.signal(signal.SIGTERM, lambda *_: stopped.__setitem__(0, True))
        signal.signal(signal.SIGINT, lambda *_: stopped.__setitem__(0, True))
        for name, path in zip(("stdout", "stderr"), log_paths):
            logs[name] = path.open("xb", buffering=0)
        _preflight()
        probe = _probe(deadline, stopped)
        if not probe:
            raise ValueError
        if stopped[0] or time.monotonic() >= deadline:
            owner.reject("interrupted" if stopped[0] else "timeout")
            raise ValueError
        for name in ("stdout", "stderr", "control"):
            read_fd, write_fd = os.pipe()
            pipes[name] = read_fd
            writes.append(write_fd)
            os.set_blocking(read_fd, False)
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": context["slot"]["timezone"],
               "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        command = [str(Path(sys.executable).absolute()), "-I", "-B", str(code["bootstrap"]), str(writes[2]),
                   str(context_path), str(receipt_path), *selected]
        spawn_attempted = True
        owner.popen = _RawPopen(command, cwd=ROOT, env=env, close_fds=True,
                                start_new_session=True, pass_fds=(writes[2],),
                                stdin=subprocess.DEVNULL, stdout=writes[0], stderr=writes[1])
        owner.leader = owner.popen.pid
        _close_descriptors(writes, owner)
        complete, guard, streams = _observe(owner, pipes, logs, parent_guard, deadline, stopped)
        pipes.clear()
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
        owner.reject("interrupted" if stopped[0] else "startup_error")
        _close_descriptors(writes, owner)
        if owner.leader is not None:
            try:
                complete, guard, streams = _observe(owner, pipes, logs, parent_guard, min(deadline, time.monotonic()), stopped)
            except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
                _emergency_cleanup(owner, pipes)
                if owner.observation is not None:
                    guard = owner.observation["guard"]
                    streams = {name: {**item, "sha256": item["sha256"].hexdigest()}
                               for name, item in owner.observation["streams"].items()}
        elif spawn_attempted:
            _emergency_cleanup(owner, pipes)
    finally:
        descriptors = [*writes, *pipes.values()]
        writes.clear()
        pipes.clear()
        _close_descriptors(descriptors, owner)
        for stream in logs.values():
            try:
                stream.close()
            except OSError:
                owner.reject("io_error")
    try:
        for name, path in code.items():
            if name != "executable" and _hash(path) != identity[name]:
                owner.reject("identity_changed")
        parent_guard = bootstrap._guard(ROOT)
        if parent_guard["violations"] or (guard is not None and guard != parent_guard):
            owner.reject("startup_error")
    except (OSError, ValueError, TypeError):
        parent_guard = None
        owner.reject("identity_changed")
    if owner.survived:
        owner.reject("cleanup_error")
    receipt = _receipt(receipt_path)
    reason = owner.error or ("signaled" if owner.returncode is not None and owner.returncode < 0 else "exited")
    if not owner.reaped and owner.error is None:
        reason = "cleanup_error"
    doc = {"schema": "qwq.verification-process-result/v1", **context, "scope": "local_os_process_only",
           "identity": identity,
           "launch": {"profile": "pytest-evidence-bootstrap/v1", "process_scope": "linux-subreaper/v1",
                      "timeout_seconds": options.timeout_seconds, "selection_sha256": hashlib.sha256(_canonical(selected)).hexdigest()},
           "process": {"reason": reason, "returncode": owner.returncode, "term_sent": owner.term,
                       "kill_sent": owner.kill, "leader_reaped": owner.reaped, "descendant_survived": owner.survived,
                       "cleanup_complete": complete, "descendants_reaped": min(owner.descendants, 20000),
                       "ownership_probe_passed": probe, "guard": guard, "parent_guard": parent_guard},
           "streams": streams, "receipt": receipt}
    try:
        with output.open("xb") as stream:
            stream.write(_canonical(doc))
    except (OSError, ValueError, TypeError):
        return 125
    if reason == "timeout":
        return 124
    if owner.error or not complete or receipt["state"] != "regular":
        return 125
    return owner.returncode if owner.returncode >= 0 else 128 - owner.returncode


if __name__ == "__main__":
    raise SystemExit(main())
