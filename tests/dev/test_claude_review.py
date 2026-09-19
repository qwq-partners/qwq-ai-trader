"""Black-box tests for the bounded Claude review runner (no live Claude calls)."""

from __future__ import annotations

import json
import os
import hashlib
import signal
import stat
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev/claude_review.py"
PYTHON = Path(sys.executable)


def fake_claude(tmp_path: Path, body: str) -> tuple[Path, Path]:
    """Create a real executable which acts as the externally launched CLI."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    record = tmp_path / "child-record.json"
    program = tmp_path / "fake-claude.py"
    program.write_text(
        "#!" + sys.executable + "\n"
        + "import json, os, sys, time\n"
        + f"RECORD = {str(record)!r}\n"
        + "payload = sys.stdin.read()\n"
        + "mcp = sys.argv[sys.argv.index('--mcp-config') + 1]\n"
        + "json.dump({'argv': sys.argv[1:], 'cwd': os.getcwd(), 'env': dict(os.environ), 'payload': payload, 'mcp': open(mcp).read()}, open(RECORD, 'w'))\n"
        + "if '--chrome' in sys.argv or '--no-chrome' not in sys.argv: raise SystemExit(64)\n"
        + textwrap.dedent(body),
        encoding="utf-8",
    )
    program.chmod(program.stat().st_mode | stat.S_IXUSR)
    return program, record


def stream(events: list[dict], *, fragment: bool = False) -> str:
    lines = [json.dumps(event, ensure_ascii=False) for event in events]
    if fragment:
        return "\n".join(lines)  # exercise an EOF JSON fragment
    return "\n".join(lines) + "\n"


def good_events(*, model: str = "claude-opus-5", review: str = "검토 완료") -> list[dict]:
    return [
        {"type": "system", "subtype": "init", "tools": []},
        {"type": "message_start", "message": {"model": model}},
        {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 1},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "partial"}},
        {"type": "result", "subtype": "success", "is_error": False, "result": review},
    ]


def run_runner(
    fake: Path,
    prompt: str = "검토할 변경입니다.",
    *,
    extra: list[str] | None = None,
    timeout: float = 5,
    interpreter: Path | str = PYTHON,
) -> subprocess.CompletedProcess[str]:
    args = [str(interpreter), str(SCRIPT), "--claude-bin", str(fake)]
    if extra:
        args.extend(extra)
    return subprocess.run(
        args,
        cwd=ROOT,
        input=prompt,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=timeout,
        check=False,
        env={"HOME": "/safe/home", "PATH": os.environ["PATH"], "USER": "safe", "LOGNAME": "safe", "LANG": "C.UTF-8", "TERM": "xterm"},
    )


def child_record(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_completed_review_is_only_success_path(tmp_path):
    fake, record = fake_claude(tmp_path, f"sys.stdout.write({stream(good_events())!r}); sys.stdout.flush()\n")

    result = run_runner(fake)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "execution_status: complete" in result.stdout
    statuses = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    status = statuses[0]
    assert status["input_sha256"] == hashlib.sha256("검토할 변경입니다.".encode()).hexdigest()
    assert status["sent_bytes"] == len("검토할 변경입니다.".encode())
    assert status["actual_model"] in {"unknown", "claude-opus-5"}
    assert status["result_usage"] == "unknown"
    assert "estimated_tokens" not in result.stdout
    final = statuses[-1]
    assert final["execution_status"] == "complete"
    assert final["requested_effort"] == "xhigh"
    assert final["terminal_subtype"] == "success"
    assert final["child_returncode"] == 0
    assert final["cost_usd"] == "unknown"
    assert "review_text:\n검토 완료" in result.stdout
    assert "partial" not in result.stdout
    assert result.stderr == ""
    assert child_record(record)["payload"] == "검토할 변경입니다."


def test_runner_passes_locked_down_cli_environment_and_private_cwd(tmp_path):
    fake, record = fake_claude(tmp_path, f"sys.stdout.write({stream(good_events())!r})\n")

    result = run_runner(fake)

    assert result.returncode == 0, result.stdout + result.stderr
    observed = child_record(record)
    assert observed["argv"] == [
        "--model", "claude-opus-5", "--effort", "xhigh", "--safe-mode", "-p",
        "--tools", "", "--permission-mode", "dontAsk", "--disable-slash-commands",
        "--no-chrome", "--strict-mcp-config", "--mcp-config", observed["argv"][14],
        "--no-session-persistence", "--output-format", "stream-json", "--verbose",
        "--include-partial-messages", "--max-budget-usd", "5",
    ]
    assert Path(observed["argv"][14]).name == "empty-mcp.json"
    assert json.loads(observed["mcp"]) == {"mcpServers": {}}
    assert Path(observed["cwd"]).name.startswith("claude-review-")
    assert set(observed["env"]) == {"HOME", "PATH", "USER", "LOGNAME", "LANG", "TERM", "DISABLE_AUTOUPDATER", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"}
    assert observed["env"]["DISABLE_AUTOUPDATER"] == "1"
    assert observed["env"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"


def test_explicit_pre_sanitized_prompt_is_passed_byte_for_byte(tmp_path):
    fake, record = fake_claude(tmp_path, f"sys.stdout.write({stream(good_events())!r})\n")

    result = run_runner(fake, "ok\nsudo do-not-send\n비밀번호=never\nsk-secret\necho bad\nkeep")

    assert result.returncode == 0
    assert child_record(record)["payload"] == "ok\nsudo do-not-send\n비밀번호=never\nsk-secret\necho bad\nkeep"
    assert "do-not-send" not in result.stdout + result.stderr  # child input must not be echoed by the runner


def test_model_progress_can_outlive_old_short_timeout(tmp_path):
    events = good_events()
    body = "\n".join(
        f"sys.stdout.write({json.dumps(event)!r} + '\\n'); sys.stdout.flush(); time.sleep(0.07)"
        for event in events
    )
    fake, _ = fake_claude(tmp_path, body)

    result = run_runner(fake, extra=["--startup-timeout", "0.2", "--idle-timeout", "0.12", "--total-timeout", "0.8", "--status-interval", "0.03"])

    assert result.returncode == 0, result.stdout + result.stderr
    assert "execution_status: complete" in result.stdout


def test_status_spam_cannot_extend_model_progress_idle_deadline(tmp_path):
    init = json.dumps({"type": "system", "subtype": "init", "tools": []})
    start = json.dumps({"type": "message_start", "message": {"model": "claude-opus-5"}})
    body = f"""
sys.stdout.write({init!r} + '\\n' + {start!r} + '\\n'); sys.stdout.flush()
for _ in range(20):
    sys.stdout.write('{{"type":"system","subtype":"status","message":"still here"}}\\n'); sys.stdout.flush(); time.sleep(.03)
"""
    fake, _ = fake_claude(tmp_path, body)

    result = run_runner(fake, extra=["--startup-timeout", "0.2", "--idle-timeout", "0.12", "--total-timeout", "0.8", "--status-interval", "0.03"])

    assert result.returncode == 124
    assert "reason: idle_timeout" in result.stdout
    assert "still here" not in result.stdout + result.stderr


def test_absolute_deadline_wins_even_when_model_keeps_progressing(tmp_path):
    init = json.dumps({"type": "system", "subtype": "init", "tools": []})
    start = json.dumps({"type": "message_start", "message": {"model": "claude-opus-5"}})
    body = f"""
sys.stdout.write({init!r} + '\\n' + {start!r} + '\\n'); sys.stdout.flush()
for n in range(30):
    sys.stdout.write(json.dumps({{'type':'system','subtype':'thinking_tokens','estimated_tokens':n + 1}}) + '\\n'); sys.stdout.flush(); time.sleep(.025)
"""
    fake, _ = fake_claude(tmp_path, body)

    result = run_runner(fake, extra=["--startup-timeout", "0.2", "--idle-timeout", "0.12", "--total-timeout", "0.2", "--status-interval", "0.03"])

    assert result.returncode == 124
    assert "reason: total_timeout" in result.stdout


def test_invalid_or_incomplete_terminal_state_is_never_success(tmp_path):
    cases = [
        ([event for event in good_events() if event["type"] != "result"], "missing_result"),
        ([*good_events()[:-1], {"type": "result", "subtype": "success", "is_error": True, "result": "no"}], "result_error"),
        ([*good_events(), {"type": "result", "subtype": "success", "is_error": False, "result": "second"}], "invalid_result"),
        ([*good_events()[:-1], {"type": "result", "subtype": "success", "is_error": False, "result": ""}], "invalid_result"),
    ]
    for index, (events, reason) in enumerate(cases):
        fake, _ = fake_claude(tmp_path / str(index), f"sys.stdout.write({stream(events)!r})\n")
        result = run_runner(fake)
        assert result.returncode != 0
        assert f"reason: {reason}" in result.stdout
        assert "execution_status: complete" not in result.stdout


def test_bad_json_and_model_or_tool_violations_are_fail_closed(tmp_path):
    midstream_model_change = [
        *good_events()[:-2],
        {"type": "assistant", "message": {"model": "claude-haiku-4"}},
        *good_events()[-2:],
    ]
    cases = [
        ("sys.stdout.write('{not-json}\\n')", "bad_json"),
        ("sys.stdout.buffer.write(b'\\xff\\n'); sys.stdout.buffer.flush()", "invalid_output"),
        (f"sys.stdout.write({stream(good_events(model='claude-haiku-4'))!r})", "model_mismatch"),
        (f"sys.stdout.write({stream(midstream_model_change)!r})", "model_mismatch"),
        (f"sys.stdout.write({stream([*good_events()[:-1], {'type':'content_block_start','content_block':{'type':'tool_use'}}, good_events()[-1]])!r})", "tool_violation"),
        (f"sys.stdout.write({stream([{'type':'system','subtype':'init','tools':['bad']}, *good_events()[1:]])!r})", "tool_violation"),
    ]
    for index, (body, reason) in enumerate(cases):
        fake, _ = fake_claude(tmp_path / str(index), body)
        result = run_runner(fake)
        assert result.returncode != 0
        assert f"reason: {reason}" in result.stdout


def test_partial_text_is_not_a_terminal_review_and_eof_fragment_is_parsed(tmp_path):
    partial = good_events()[:-1]
    fake, _ = fake_claude(tmp_path, f"sys.stdout.write({stream(partial, fragment=True)!r})\n")
    failed = run_runner(fake)
    assert failed.returncode != 0 and "reason: missing_result" in failed.stdout

    fake, _ = fake_claude(tmp_path / "success", f"sys.stdout.write({stream(good_events(), fragment=True)!r})\n")
    completed = run_runner(fake)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_large_input_output_and_stderr_are_drained_without_secret_leak(tmp_path):
    noisy = [{"type": "system", "subtype": "status", "message": "x" * 2048} for _ in range(180)]
    events = [*good_events()[:-1], *noisy, good_events()[-1]]
    fake, record = fake_claude(
        tmp_path,
        f"sys.stderr.write('sk-hidden-error\\n' * 10000); sys.stderr.flush(); sys.stdout.write({stream(events)!r}); sys.stdout.flush()\n",
    )
    large_prompt = "safe\n" + ("z" * 700_000)

    result = run_runner(fake, large_prompt, timeout=10)

    assert result.returncode == 0, result.stdout + result.stderr
    assert len(child_record(record)["payload"]) == len(large_prompt)
    assert "sk-hidden-error" not in result.stdout + result.stderr
    assert "x" * 100 not in result.stdout + result.stderr


def test_result_before_child_exit_is_not_completed_and_timeout_reaps_child(tmp_path):
    marker = tmp_path / "survived"
    fake, _ = fake_claude(
        tmp_path,
        f"sys.stdout.write({stream(good_events())!r}); sys.stdout.flush(); time.sleep(.5); open({str(marker)!r}, 'w').write('bad')\n",
    )

    result = run_runner(fake, extra=["--startup-timeout", "0.2", "--idle-timeout", "0.2", "--total-timeout", "0.12", "--status-interval", "0.03"])
    time.sleep(0.55)

    assert result.returncode == 124
    assert "reason: total_timeout" in result.stdout
    assert not marker.exists(), "timeout must terminate the owned child before it continues"


def test_utf8_chunk_and_newline_boundaries_are_reassembled(tmp_path):
    raw = stream(good_events(review="한글 최종 검토")).encode("utf-8")
    cut = raw.index("한".encode("utf-8")) + 1
    fake, _ = fake_claude(
        tmp_path,
        f"data = {raw!r}; sys.stdout.buffer.write(data[:{cut}]); sys.stdout.buffer.flush(); sys.stdout.buffer.write(data[{cut}:]); sys.stdout.buffer.flush()\n",
    )

    result = run_runner(fake)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "한글 최종 검토" in result.stdout


def test_child_nonzero_exit_cannot_turn_a_valid_result_into_success(tmp_path):
    fake, _ = fake_claude(tmp_path, f"sys.stdout.write({stream(good_events())!r}); sys.stdout.flush(); raise SystemExit(7)\n")

    result = run_runner(fake)

    assert result.returncode != 0
    assert "reason: child_exit" in result.stdout
    assert "execution_status: complete" not in result.stdout


def test_complete_assistant_tool_content_and_error_events_fail_closed(tmp_path):
    """A forbidden event must make the wrapper exit while its child is still held alive."""
    def runner_wrapper(path, *, interpreter=PYTHON, startup_delay=0.0, disable_tool_hook=False):
        if not startup_delay and not disable_tool_hook:
            return interpreter
        wrapper = path / "runner-wrapper.py"
        body = (f"#!{sys.executable}\n"
                "import importlib.util, json, sys, time\n"
                f"time.sleep({startup_delay!r})\n"
                "script = sys.argv[1]\n"
                "spec = importlib.util.spec_from_file_location('review_runner_negative_control', script)\n"
                "module = importlib.util.module_from_spec(spec)\n"
                "sys.modules[spec.name] = module\n"
                "spec.loader.exec_module(module)\n")
        if disable_tool_hook:
            body += ("original_reject = module.State.reject\n"
                     "def reject_without_tool_violation(state, reason):\n"
                     "    if reason == 'tool_violation': return\n"
                     "    return original_reject(state, reason)\n"
                     "module.State.reject = reject_without_tool_violation\n")
        body += "sys.argv = [script, *sys.argv[2:]]\nraise SystemExit(module.main())\n"
        wrapper.write_text(body, encoding="utf-8")
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        return wrapper

    def wait_for(path, timeout=2.0):
        deadline = time.monotonic() + timeout
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(.005)
        return path.exists()

    def run_barrier(case_path, events, reason, *, startup_delay=0.0, disable_tool_hook=False):
        emit_gate, continue_gate = case_path / "emit", case_path / "continue"
        marker, emitted = case_path / "survived", case_path / "emitted"
        fake, ready = fake_claude(case_path / "fake", f"""
while not os.path.exists({str(emit_gate)!r}): time.sleep(.005)
open({str(emitted)!r}, 'w').write('emitted')
sys.stdout.write({stream(events)!r}); sys.stdout.flush()
while not os.path.exists({str(continue_gate)!r}): time.sleep(.005)
open({str(marker)!r}, 'w').write('survived')
""")
        outcome = {}
        interpreter = runner_wrapper(case_path, startup_delay=startup_delay,
                                     disable_tool_hook=disable_tool_hook)
        try:
            def invoke():
                try:
                    outcome['result'] = run_runner(fake, extra=["--total-timeout", "3"], timeout=10,
                                                    interpreter=interpreter)
                except BaseException as exc:
                    outcome['exception'] = exc

            worker = threading.Thread(
                target=invoke,
            )
            worker.start()
            assert wait_for(ready), "fake child did not reach startup readiness"
            emit_gate.write_text("release", encoding="utf-8")
            assert wait_for(emitted), "fake child did not emit the controlled event"
            worker.join(timeout=2)
            if disable_tool_hook:
                # Negative control: without this one hook, the wrapper remains
                # live behind the child continuation gate and the normal oracle
                # would fail.
                assert worker.is_alive()
                continue_gate.write_text("release", encoding="utf-8")
                worker.join(timeout=2)
                assert not worker.is_alive() and wait_for(marker)
                assert 'exception' not in outcome
                assert outcome['result'].returncode == 0
                assert "execution_status: complete" in outcome['result'].stdout
                return
            assert not worker.is_alive(), "forbidden event did not terminate wrapper before child continuation"
            assert 'exception' not in outcome
            result = outcome['result']
            assert result.returncode != 0
            assert f"reason: {reason}" in result.stdout
            continue_gate.write_text("release", encoding="utf-8")
            assert not wait_for(marker, timeout=.8), "terminated child wrote after wrapper exit"
        finally:
            if 'worker' in locals():
                continue_gate.write_text("release", encoding="utf-8")
                worker.join(timeout=10)
                assert not worker.is_alive(), "worker cleanup left a direct fake child path live"

    cases = [
        ([
            {"type": "system", "subtype": "init", "tools": []},
            {"type": "assistant", "message": {"model": "claude-opus-5", "content": [{"type": "tool_use", "name": "bad"}]}},
            good_events()[-1],
        ], "tool_violation"),
        ([*good_events()[:-1], {"type": "error", "error": {"type": "api_error"}}, good_events()[-1]], "stream_error"),
    ]
    for index, (events, reason) in enumerate(cases):
        run_barrier(tmp_path / str(index), events, reason)
    # Startup before the runner begins is deliberately outside the causal
    # oracle; the child is still held at the same post-event barrier.
    run_barrier(tmp_path / "startup-delay", cases[0][0], cases[0][1], startup_delay=.35)
    run_barrier(tmp_path / "negative-control", cases[0][0], cases[0][1], disable_tool_hook=True)


def test_invalid_utf8_input_is_rejected_before_any_child_execution(tmp_path):
    fake, record = fake_claude(tmp_path, f"sys.stdout.write({stream(good_events())!r})\n")
    result = subprocess.run(
        [str(PYTHON), str(SCRIPT), "--claude-bin", str(fake)],
        cwd=ROOT,
        input=b"pre-sanitized\xff",
        capture_output=True,
        check=False,
        env={"HOME": "/safe/home", "PATH": os.environ["PATH"]},
    )

    assert result.returncode != 0
    assert b"reason: invalid_input" in result.stdout
    assert not record.exists()


def test_malformed_and_outer_nested_events_fail_closed_without_orphan(tmp_path):
    cases = [
        {"type": []},
        {"type": "system", "subtype": []},
        {"type": "content_block_delta", "delta": {"type": []}},
        {"type": "error", "event": {"type": "ping"}},
        {"type": "assistant", "message": {"model": "claude-haiku-4"}, "event": {"type": "ping"}},
    ]
    for index, event in enumerate(cases):
        marker = tmp_path / f"orphan-{index}"
        fake, _ = fake_claude(
            tmp_path / str(index),
            f"sys.stdout.write({stream([*good_events()[:-1], event])!r}); sys.stdout.flush(); time.sleep(.35); open({str(marker)!r}, 'w').write('bad')\n",
        )
        result = run_runner(fake)
        time.sleep(.4)
        assert result.returncode != 0
        assert "Traceback" not in result.stderr
        assert not marker.exists()


def test_missing_result_after_leader_exit_reaps_stdio_closed_descendant(tmp_path):
    marker = tmp_path / "orphan"
    fake, _ = fake_claude(
        tmp_path,
        f"""sys.stdout.write({stream(good_events()[:-1])!r}); sys.stdout.flush()
child = os.fork()
if child == 0:
    os.close(0); os.close(1); os.close(2); time.sleep(.35); open({str(marker)!r}, 'w').write('bad'); os._exit(0)
os._exit(0)
""",
    )
    result = run_runner(fake)
    time.sleep(.4)
    assert result.returncode != 0 and "reason: missing_result" in result.stdout
    assert not marker.exists()


def test_sigterm_reaps_owned_child_process_group(tmp_path):
    marker = tmp_path / "signal-orphan"
    fake, record = fake_claude(tmp_path, f"sys.stdout.write({stream(good_events()[:-1])!r}); sys.stdout.flush(); time.sleep(.5); open({str(marker)!r}, 'w').write('bad')\n")
    process = subprocess.Popen(
        [str(PYTHON), str(SCRIPT), "--claude-bin", str(fake)],
        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={"HOME": "/safe/home", "PATH": os.environ["PATH"]},
    )
    assert process.stdin is not None
    process.stdin.write(b"x")
    process.stdin.close()
    process.stdin = None
    deadline = time.monotonic() + 1
    while not record.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=3)
    time.sleep(.55)
    assert process.returncode != 0
    assert not marker.exists()


def test_new_thinking_block_resets_estimate_progress(tmp_path):
    events = [
        *good_events()[:2],
        {"type": "content_block_start", "content_block": {"type": "thinking"}},
        {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 100},
        {"type": "content_block_start", "content_block": {"type": "thinking"}},
        {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 1},
        {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 2},
        good_events()[-1],
    ]
    body = "\n".join(
        f"sys.stdout.write({json.dumps(event)!r} + '\\n'); sys.stdout.flush(); time.sleep(.06)" for event in events
    )
    fake, _ = fake_claude(tmp_path, body)
    result = run_runner(fake, extra=["--idle-timeout", "0.15", "--total-timeout", "0.8"])
    assert result.returncode == 0, result.stdout + result.stderr


def test_deep_stream_envelope_and_huge_cost_fail_closed_without_traceback(tmp_path):
    nested: dict = {"type": "ping"}
    for _ in range(1100):
        nested = {"type": "stream_event", "event": nested}
    marker = tmp_path / "exception-orphan"
    fake, _ = fake_claude(
        tmp_path,
        f"sys.stdout.write({stream([*good_events()[:-1], nested])!r}); sys.stdout.flush(); time.sleep(.35); open({str(marker)!r}, 'w').write('bad')\n",
    )
    result = run_runner(fake)
    time.sleep(.4)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert not marker.exists()

    huge_cost = {**good_events()[-1], "total_cost_usd": 10 ** 1000}
    fake, _ = fake_claude(tmp_path / "cost", f"sys.stdout.write({stream([*good_events()[:-1], huge_cost])!r}); sys.stdout.flush()\n")
    result = run_runner(fake)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"cost_usd":"unknown"' in result.stdout
    assert "Traceback" not in result.stderr


def test_sigterm_during_selector_setup_reaps_postspawn_child(tmp_path):
    marker = tmp_path / "setup-orphan"
    fake, record = fake_claude(tmp_path, f"time.sleep(.35); open({str(marker)!r}, 'w').write('bad')\n")
    wrapper = f"""import os, runpy, selectors, signal, sys, time
record = {str(record)!r}
script = {str(SCRIPT)!r}
target = {str(fake)!r}
original = selectors.DefaultSelector
def interrupt_selector_setup():
    deadline = time.monotonic() + 1
    while not os.path.exists(record) and time.monotonic() < deadline:
        time.sleep(.005)
    os.kill(os.getpid(), signal.SIGTERM)
    return original()
selectors.DefaultSelector = interrupt_selector_setup
sys.argv = [script, '--claude-bin', target, '--total-timeout', '1']
runpy.run_path(script, run_name='__main__')
"""
    result = subprocess.run(
        [str(PYTHON), "-c", wrapper], input=b"x", capture_output=True,
        env={"HOME": "/safe/home", "PATH": os.environ["PATH"], "LANG": "C.UTF-8"}, timeout=4,
    )
    time.sleep(.45)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr.decode("utf-8", "replace")
    assert not marker.exists()
