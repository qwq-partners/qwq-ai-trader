"""Independent synthetic CLI boundary probes, preserved as portable regressions.

Original temporary probes are retained unchanged; only checkout/interpreter
paths and module packaging differ here. No live Claude calls.
"""

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/dev/claude_review.py'
PYTHON = sys.executable
ENV = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent-synthetic-review-home', 'LANG': 'C.UTF-8', 'TZ': 'UTC', 'PYTHONDONTWRITEBYTECODE': '1'}
INIT = {'type': 'system', 'subtype': 'init', 'tools': []}
START = {'type': 'message_start', 'message': {'model': 'claude-opus-5'}}
RESULT = {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'Independent synthetic review'}

def encoded(events):
    return ''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in events).encode()

def fake(tmp_path, body):
    target = tmp_path / 'fake.py'
    pidfile = tmp_path / 'pid'
    target.write_text('#!' + PYTHON + '\nimport sys, os, json, time, signal, hashlib\n' +
                      f'open({str(pidfile)!r}, "w").write(str(os.getpid()))\n' + body)
    target.chmod(0o700)
    return target, pidfile

def args(target, extra=()):
    return [PYTHON, str(SCRIPT), '--claude-bin', str(target), '--startup-timeout', '1',
            '--idle-timeout', '0.4', '--total-timeout', '2', '--status-interval', '0.05', *extra]

def cleanup(pidfile):
    if pidfile.exists():
        try:
            os.killpg(int(pidfile.read_text()), signal.SIGKILL)
        except ProcessLookupError:
            pass

def run(tmp_path, body, payload=b'synthetic\r\ninput\x00\xed\x95\x9c', extra=()):
    target, pidfile = fake(tmp_path, body)
    try:
        return subprocess.run(args(target, extra), input=payload, capture_output=True, env=ENV, timeout=4)
    finally:
        cleanup(pidfile)

def emit(events):
    return f'sys.stdout.buffer.write({encoded(events)!r}); sys.stdout.buffer.flush()\n'

def test_concurrent_pipe_pressure_and_exact_bytes(tmp_path):
    payload = b'\r\n\x00\xed\x95\x9c' * 150000
    output = encoded([INIT, START] + [{'type': 'ping', 'noise': 'x' * 4000}] * 50)
    body = f'sys.stderr.buffer.write(b"synthetic-secret" * 40000); sys.stderr.flush()\nsys.stdout.buffer.write({output!r}); sys.stdout.flush()\npayload = sys.stdin.buffer.read()\n'
    body += f'assert hashlib.sha256(payload).hexdigest() == {hashlib.sha256(payload).hexdigest()!r}\n' + emit([RESULT])
    result = run(tmp_path, body, payload)
    assert result.returncode == 0, result.stdout + result.stderr
    assert b'synthetic-secret' not in result.stdout + result.stderr

@pytest.mark.parametrize('suffix', [b'', b'\xe3', b'\xff'])
def test_incremental_utf8_eof(tmp_path, suffix):
    data = encoded([INIT, START, dict(RESULT, result='한글')]).rstrip(b'\n') + suffix
    body = 'sys.stdin.buffer.read()\n' + f'data = {data!r}\nfor byte in data:\n sys.stdout.buffer.write(bytes([byte])); sys.stdout.flush(); time.sleep(.0001)\n'
    result = run(tmp_path, body)
    assert (result.returncode == 0) == (suffix == b''), result.stdout + result.stderr

@pytest.mark.parametrize('event', [
    {'type': 'error', 'error': {'type': 'api_error', 'message': 'synthetic-private'}},
    {'type': 'stream_event', 'event': {'type': 'content_block_start', 'content_block': {'type': 'tool_use'}}},
    {'type': 'assistant', 'message': {'model': 'claude-opus-5', 'content': [{'type': 'tool_use'}]}},
    {'type': 'assistant', 'message': {'model': 'claude-haiku-4'}},
])
def test_partial_then_error_model_or_tool_violation(tmp_path, event):
    result = run(tmp_path, 'sys.stdin.buffer.read()\n' + emit([INIT, START, {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'private-partial'}}, event, RESULT]))
    assert result.returncode != 0
    assert b'execution_status: complete' not in result.stdout
    assert b'private-partial' not in result.stdout + result.stderr
    assert b'synthetic-private' not in result.stdout + result.stderr

@pytest.mark.parametrize('events', [[INIT, START], [INIT, START, RESULT, RESULT], [INIT, START, dict(RESULT, is_error=0)], [INIT, START, dict(RESULT, result=[])], [INIT, START, dict(RESULT, result=' ')], [INIT, START, dict(RESULT, subtype='error')]])
def test_terminal_rejection(tmp_path, events):
    result = run(tmp_path, 'sys.stdin.buffer.read()\n' + emit(events))
    assert result.returncode != 0
    assert b'execution_status: complete' not in result.stdout

def test_large_output_bound(tmp_path):
    result = run(tmp_path, 'sys.stdin.buffer.read()\n' + emit([INIT, START]) + 'sys.stderr.buffer.write(b"x" * 100000); sys.stderr.flush(); time.sleep(2)\n', extra=['--max-output-bytes', '10000'])
    assert result.returncode != 0
    assert b'reason: output_limit' in result.stdout

def test_incomplete_input_is_not_success(tmp_path):
    result = run(tmp_path, emit([INIT, START, RESULT]), payload=b'x' * 1500000)
    assert result.returncode != 0
    assert b'reason: input_incomplete' in result.stdout

@pytest.mark.parametrize('event', [{'type': []}, {'type': 'system', 'subtype': []}, dict(RESULT, subtype=[])])
def test_malformed_schema_cannot_leave_running_child(tmp_path, event):
    marker = tmp_path / 'survived'
    target, pidfile = fake(tmp_path, 'sys.stdin.buffer.read()\n' + emit([INIT, START, event]) + f'time.sleep(.4); open({str(marker)!r}, "w").write("survived")\n')
    try:
        result = subprocess.run(args(target), input=b'x', capture_output=True, env=ENV, timeout=4)
        time.sleep(.5)
        assert not marker.exists(), (result.returncode, result.stdout.decode(), result.stderr.decode())
        assert b'execution_status: error' in result.stdout
        assert b'Traceback' not in result.stderr
    finally:
        cleanup(pidfile)

def test_final_failure_reaps_group_after_leader_and_pipes_exit(tmp_path):
    marker = tmp_path / 'orphan-survived'
    body = 'sys.stdin.buffer.read()\n' + emit([INIT, START])
    body += f'child = os.fork()\nif child == 0:\n os.close(0); os.close(1); os.close(2)\n time.sleep(.4)\n open({str(marker)!r}, "w").write("survived")\n os._exit(0)\nos._exit(0)\n'
    target, pidfile = fake(tmp_path, body)
    try:
        result = subprocess.run(args(target), input=b'x', capture_output=True, env=ENV, timeout=4)
        assert result.returncode != 0 and b'reason: missing_result' in result.stdout
        time.sleep(.5)
        assert not marker.exists(), result.stdout.decode()
    finally:
        cleanup(pidfile)

@pytest.mark.parametrize('sig', [signal.SIGINT, signal.SIGTERM])
def test_parent_interrupt_reaps_owned_process(tmp_path, sig):
    marker = tmp_path / 'signal-survived'
    target, pidfile = fake(tmp_path, 'sys.stdin.buffer.read()\n' + emit([INIT, START]) + f'time.sleep(.6); open({str(marker)!r}, "w").write("survived")\n')
    parent = subprocess.Popen(args(target), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV)
    try:
        parent.stdin.write(b'x'); parent.stdin.close(); parent.stdin = None
        deadline = time.monotonic() + 2
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert pidfile.exists()
        time.sleep(.04)
        parent.send_signal(sig)
        out, err = parent.communicate(timeout=3)
        assert parent.returncode != 0
        time.sleep(.7)
        assert not marker.exists(), (sig, parent.returncode, out.decode(), err.decode())
    finally:
        if parent.poll() is None:
            parent.kill(); parent.wait()
        cleanup(pidfile)

@pytest.mark.parametrize('outer', [
    {'type': 'error', 'event': {'type': 'ping'}},
    {'type': 'assistant', 'message': {'model': 'claude-haiku-4'}, 'event': {'type': 'ping'}},
    {'type': 'assistant', 'message': {'model': 'claude-opus-5', 'content': [{'type': 'tool_use'}]}, 'event': {'type': 'ping'}},
])
def test_nested_event_cannot_mask_outer_protocol_violation(tmp_path, outer):
    result = run(tmp_path, 'sys.stdin.buffer.read()\n' + emit([INIT, START, outer, RESULT]))
    assert result.returncode != 0, result.stdout.decode()

def test_result_then_hang_is_timeout(tmp_path):
    result = run(tmp_path, 'sys.stdin.buffer.read()\n' + emit([INIT, START, RESULT]) + 'time.sleep(3)\n', extra=['--total-timeout', '.15'])
    assert result.returncode == 124 and b'reason: total_timeout' in result.stdout

def test_idle_and_absolute_timeout_kill_surviving_child_with_open_pipes(tmp_path):
    marker = tmp_path / 'timeout-survived'
    body = 'sys.stdin.buffer.read()\n' + emit([INIT, START])
    body += f'child = os.fork()\nif child == 0:\n signal.signal(signal.SIGTERM, signal.SIG_IGN)\n time.sleep(.8)\n open({str(marker)!r}, "w").write("survived")\n os._exit(0)\nos._exit(0)\n'
    result = run(tmp_path, body, extra=['--idle-timeout', '.1', '--total-timeout', '.2'])
    time.sleep(.85)
    assert result.returncode == 124
    assert not marker.exists()

def test_new_thinking_block_resets_estimate_progress(tmp_path):
    events = [INIT, START,
              {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking', 'thinking': ''}},
              {'type': 'system', 'subtype': 'thinking_tokens', 'estimated_tokens': 100},
              {'type': 'content_block_stop', 'index': 0},
              {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'thinking', 'thinking': ''}},
              {'type': 'system', 'subtype': 'thinking_tokens', 'estimated_tokens': 1},
              {'type': 'system', 'subtype': 'thinking_tokens', 'estimated_tokens': 2},
              {'type': 'system', 'subtype': 'thinking_tokens', 'estimated_tokens': 3}, RESULT]
    body = 'sys.stdin.buffer.read()\n' + emit(events[:4])
    body += 'time.sleep(.07)\n' + emit(events[4:7])
    body += 'time.sleep(.07)\n' + emit(events[7:8])
    body += 'time.sleep(.07)\n' + emit(events[8:])
    result = run(tmp_path, body, extra=['--idle-timeout', '.12', '--total-timeout', '.7'])
    assert result.returncode == 0, result.stdout.decode()

@pytest.mark.parametrize('kind', ['delta_type_list', 'deep_json', 'oversized_cost_integer'])
def test_any_protocol_exception_must_cleanup_owned_child(tmp_path, kind):
    marker = tmp_path / 'survived-exception'
    if kind == 'delta_type_list':
        data = emit([INIT, START, {'type': 'content_block_delta', 'delta': {'type': [], 'thinking': 'synthetic-secret'}}])
    elif kind == 'deep_json':
        nested = '{"type":"stream_event","event":' * 1100 + '{"type":"ping"}' + '}' * 1100 + '\n'
        data = emit([INIT, START]) + f'sys.stdout.write({nested!r}); sys.stdout.flush()\n'
    else:
        data = emit([INIT, START, dict(RESULT, total_cost_usd=10 ** 1000)])
    target, pidfile = fake(tmp_path, 'sys.stdin.buffer.read()\n' + data + f'time.sleep(.45); open({str(marker)!r}, "w").write("survived")\n')
    try:
        result = subprocess.run(args(target), input=b'x', capture_output=True, env=ENV, timeout=4)
        time.sleep(.55)
        if kind == 'oversized_cost_integer' and result.returncode == 0:
            # Cost is optional metadata: safely reporting unknown is also valid.
            assert b'"cost_usd":"unknown"' in result.stdout
            assert b'Traceback' not in result.stderr
            return
        assert not marker.exists(), (kind, result.returncode, result.stdout.decode(), result.stderr.decode())
        assert result.returncode != 0
        assert b'Traceback' not in result.stderr
    finally:
        cleanup(pidfile)


def test_real_sigterm_during_postspawn_selector_setup_reaps_child(tmp_path):
    marker = tmp_path / 'setup-interrupt-survived'
    target, pidfile = fake(tmp_path, f'time.sleep(.4); open({str(marker)!r}, "w").write("survived")\n')
    # Deterministic fault injection at a real post-Popen setup boundary. The
    # signal, runner process, child process, session and cleanup are all real.
    wrapper = '''import os, runpy, selectors, signal, sys, time
original = selectors.DefaultSelector
def interrupt_selector_setup():
    deadline = time.monotonic() + 2
    while not os.path.exists(PIDFILE) and time.monotonic() < deadline:
        time.sleep(.005)
    os.kill(os.getpid(), signal.SIGTERM)
    return original()
selectors.DefaultSelector = interrupt_selector_setup
sys.argv = [SCRIPT, '--claude-bin', TARGET, '--total-timeout', '1']
runpy.run_path(SCRIPT, run_name='__main__')
'''
    wrapper = f'PIDFILE={str(pidfile)!r}\nSCRIPT={str(SCRIPT)!r}\nTARGET={str(target)!r}\n' + wrapper
    try:
        result = subprocess.run([PYTHON, '-c', wrapper], input=b'x', capture_output=True, env=ENV, timeout=4)
        time.sleep(.5)
        assert result.returncode != 0
        assert not marker.exists(), (result.returncode, result.stdout.decode(), result.stderr.decode())
        assert b'Traceback' not in result.stderr
    finally:
        cleanup(pidfile)
