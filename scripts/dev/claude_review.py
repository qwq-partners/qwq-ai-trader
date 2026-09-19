#!/usr/bin/env python3
"""Run one bounded, tools-off Claude review from explicitly supplied stdin.

This runner deliberately does not inspect a checkout, configuration, or the
parent environment beyond the small authentication-compatible allowlist.
"""

from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import math
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REQUESTED_MODEL = "claude-opus-5"
MAX_LINE_BYTES = 256 * 1024
MAX_INPUT_BYTES = 2 * 1024 * 1024


def positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a finite positive number") from exc
    if not number > 0 or number == float("inf"):
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bounded tools-off Claude review runner")
    parser.add_argument("--claude-bin", default="claude", help="Claude executable (test override)")
    parser.add_argument("--startup-timeout", type=positive_float, default=120.0)
    parser.add_argument("--idle-timeout", type=positive_float, default=180.0)
    parser.add_argument("--total-timeout", type=positive_float, default=900.0)
    parser.add_argument("--status-interval", type=positive_float, default=30.0)
    parser.add_argument("--max-output-bytes", type=int, default=2 * 1024 * 1024)
    parser.add_argument("--max-input-bytes", type=int, default=MAX_INPUT_BYTES)
    args = parser.parse_args()
    if args.max_output_bytes <= 0 or args.max_input_bytes <= 0:
        parser.error("byte limits must be positive")
    return args


def explicit_stdin(limit: int) -> bytes:
    """Accept only caller-sanitized, valid UTF-8 stdin without changing bytes."""
    payload = sys.stdin.buffer.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("input_limit")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("invalid_input") from exc
    return payload


def child_env() -> dict[str, str]:
    allowed = ("HOME", "PATH", "USER", "LOGNAME", "LANG", "TERM")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env["DISABLE_AUTOUPDATER"] = "1"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


@dataclass
class State:
    init_seen: bool = False
    actual_model_seen: bool = False
    actual_model: str | None = None
    result: dict[str, Any] | None = None
    violation: str | None = None
    last_progress: float | None = None
    thought_tokens: int = -1
    stderr_bytes: int = 0
    output_bytes: int = 0
    progress_events: int = 0
    error_categories: dict[str, int] | None = None

    def reject(self, reason: str) -> None:
        if self.violation is None:
            self.violation = reason
        if self.error_categories is None:
            self.error_categories = {}
        self.error_categories[reason] = self.error_categories.get(reason, 0) + 1


def event_model(event: dict[str, Any]) -> str | None:
    message = event.get("message")
    return message.get("model") if isinstance(message, dict) and isinstance(message.get("model"), str) else None


def nested_event(event: dict[str, Any]) -> dict[str, Any] | None:
    nested = event.get("event")
    return nested if isinstance(nested, dict) else None


def is_tool_event(event: dict[str, Any]) -> bool:
    if event.get("type") == "tool_use":
        return True
    block = event.get("content_block")
    if event.get("type") == "content_block_start" and isinstance(block, dict) and block.get("type") == "tool_use":
        return True
    return False


def accept_event(state: State, event: dict[str, Any], model: str, now: float, depth: int = 0) -> None:
    event_type = event.get("type")
    subtype = event.get("subtype")
    if not isinstance(event_type, str) or (subtype is not None and not isinstance(subtype, str)):
        state.reject("invalid_event")
        return
    if is_tool_event(event):
        state.reject("tool_violation")
    if event_type in {"error", "api_error", "apierror"} or subtype in {"error", "api_error", "apierror"}:
        state.reject("stream_error")
        return
    if event_type == "stream_event":
        if depth >= 1:
            state.reject("invalid_event")
            return
        nested = nested_event(event)
        if nested is None:
            state.reject("invalid_event")
            return
        accept_event(state, nested, model, now, depth + 1)
        return
    if "event" in event:
        state.reject("invalid_event")
        return
    if event_type == "system" and subtype == "init":
        state.init_seen = True
        if event.get("tools") != []:
            state.reject("tool_violation")
        return
    if event_type in {"assistant", "message_start"}:
        actual_model = event_model(event)
        message = event.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, list) and any(isinstance(item, dict) and item.get("type") == "tool_use" for item in content):
                state.reject("tool_violation")
            if message.get("error") is not None:
                state.reject("stream_error")
        if event.get("error") is not None:
            state.reject("stream_error")
        if actual_model != model:
            state.reject("model_mismatch")
        else:
            state.actual_model_seen = True
            state.actual_model = actual_model
            state.last_progress = now
        return
    if event_type == "system" and subtype == "thinking_tokens":
        tokens = event.get("estimated_tokens")
        if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > state.thought_tokens:
            state.thought_tokens = tokens
            state.last_progress = now
            state.progress_events += 1
        return
    if event_type == "content_block_delta":
        delta = event.get("delta")
        if not isinstance(delta, dict) or not isinstance(delta.get("type"), str):
            state.reject("invalid_event")
            return
        delta_type = delta["type"]
        content = delta.get("text") if delta_type == "text_delta" else delta.get("thinking")
        if delta_type in {"text_delta", "thinking_delta"} and isinstance(content, str) and content:
            state.last_progress = now
            state.progress_events += 1
        return
    if event_type == "content_block_start":
        block = event.get("content_block")
        if isinstance(block, dict) and block.get("type") == "thinking":
            state.thought_tokens = -1
        return
    if event_type == "result":
        if state.result is not None:
            state.reject("invalid_result")
        state.result = event


def terminate_owned(process: subprocess.Popen[bytes]) -> None:
    """Signal only this runner's process group, even if its leader already exited."""
    group_alive = False
    try:
        os.killpg(process.pid, signal.SIGTERM)
        group_alive = True
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 0.1
    while group_alive and time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            group_alive = False
            break
        time.sleep(0.02)
    if group_alive:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass


def failure(reason: str, timeout: bool = False) -> int:
    print("execution_status: error")
    print(f"reason: {reason}")
    return 124 if timeout else 1


def safe_usage(result: dict[str, Any] | None) -> dict[str, int] | str:
    if not isinstance(result, dict) or not isinstance(result.get("usage"), dict):
        return "unknown"
    return {
        key: value for key, value in result["usage"].items()
        if key in {"input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"}
        and isinstance(value, int) and not isinstance(value, bool) and value >= 0
    } or "unknown"


def safe_cost(result: dict[str, Any] | None) -> float | str:
    if not isinstance(result, dict):
        return "unknown"
    value = result.get("total_cost_usd")
    if isinstance(value, float) and math.isfinite(value) and value >= 0:
        return value
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 5:
        return value
    return "unknown"


def emit_status(
    state: State, payload_hash: str, payload_size: int, sent: int,
    started: float, now: float, args: argparse.Namespace, execution_status: str = "running",
    child_returncode: int | None = None,
) -> None:
    phase = "startup" if not state.actual_model_seen else "active"
    status: dict[str, Any] = {
        "execution_status": execution_status,
        "phase": phase,
        "timeout_scope": "startup_and_total" if phase == "startup" else "active_total_and_idle",
        "requested_model": REQUESTED_MODEL,
        "requested_effort": "xhigh",
        "input_sha256": payload_hash,
        "input_bytes": payload_size,
        "sent_bytes": sent,
        "progress_events": state.progress_events,
        "actual_model": state.actual_model or "unknown",
        "result_usage": safe_usage(state.result),
        "cost_usd": safe_cost(state.result),
        "stderr_bytes": state.stderr_bytes,
        "error_category_counts": state.error_categories or {},
        "total_remaining_s": round(max(0.0, started + args.total_timeout - now), 3),
        "elapsed_s": round(max(0.0, now - started), 3),
    }
    if execution_status != "running":
        result = state.result
        status["terminal_subtype"] = result.get("subtype") if isinstance(result, dict) and result.get("subtype") in {"success", "error"} else "unknown"
        status["child_returncode"] = child_returncode
    if state.actual_model_seen and state.result is None and state.last_progress is not None:
        status["idle_remaining_s"] = round(max(0.0, state.last_progress + args.idle_timeout - now), 3)
    print(json.dumps(status, sort_keys=True, separators=(",", ":")), flush=True)


def final_reason(state: State, returncode: int, input_complete: bool) -> str | None:
    if state.violation:
        return state.violation
    if not input_complete:
        return "input_incomplete"
    if returncode != 0:
        return "child_exit"
    if not state.init_seen or not state.actual_model_seen:
        return "missing_metadata"
    result = state.result
    if result is None:
        return "missing_result"
    if result.get("is_error") is not False:
        return "result_error"
    if result.get("subtype") != "success" or not isinstance(result.get("result"), str) or not result["result"].strip():
        return "invalid_result"
    return None


def run(args: argparse.Namespace, payload: bytes) -> int:
    state = State()
    payload_hash = hashlib.sha256(payload).hexdigest()
    with tempfile.TemporaryDirectory(prefix="claude-review-") as tempdir:
        mcp_config = Path(tempdir) / "empty-mcp.json"
        mcp_config.write_text('{"mcpServers": {}}', encoding="utf-8")
        command = [
            args.claude_bin, "--model", REQUESTED_MODEL, "--effort", "xhigh", "--safe-mode", "-p",
            "--tools", "", "--permission-mode", "dontAsk", "--disable-slash-commands", "--no-chrome",
            "--strict-mcp-config", "--mcp-config", str(mcp_config), "--no-session-persistence",
            "--output-format", "stream-json", "--verbose", "--include-partial-messages", "--max-budget-usd", "5",
        ]
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=tempdir, env=child_env(), start_new_session=True,
            )
        except OSError:
            return failure("startup_error")

        def interrupt_handler(_signum: int, _frame: Any) -> None:
            raise KeyboardInterrupt

        previous_sigint = signal.signal(signal.SIGINT, interrupt_handler)
        previous_sigterm = signal.signal(signal.SIGTERM, interrupt_handler)

        setup_started = time.monotonic()
        try:
            assert process.stdin and process.stdout and process.stderr
            selector = selectors.DefaultSelector()
            for fileobj, mode in ((process.stdin, "input"), (process.stdout, "stdout"), (process.stderr, "stderr")):
                os.set_blocking(fileobj.fileno(), False)
                selector.register(fileobj, selectors.EVENT_WRITE if mode == "input" else selectors.EVENT_READ, mode)
        except KeyboardInterrupt:
            state.reject("interrupted")
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            terminate_owned(process)
            signal.signal(signal.SIGINT, previous_sigint)
            signal.signal(signal.SIGTERM, previous_sigterm)
            emit_status(state, payload_hash, len(payload), 0, setup_started, time.monotonic(), args, "error", process.returncode)
            return failure("interrupted")
        except Exception:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            terminate_owned(process)
            signal.signal(signal.SIGINT, previous_sigint)
            signal.signal(signal.SIGTERM, previous_sigterm)
            return failure("runner_error")

        decoders = {"stdout": codecs.getincrementaldecoder("utf-8")("strict"), "stderr": codecs.getincrementaldecoder("utf-8")("strict")}
        text_buffer = ""
        sent = 0
        input_complete = False
        stdout_open = stderr_open = True
        started = last_status = time.monotonic()
        timeout_reason: str | None = None
        abort_reason: str | None = None

        def consume_line(line: str, now: float) -> None:
            if not line.strip() or state.violation == "bad_json":
                return
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                state.reject("bad_json")
                return
            if not isinstance(event, dict):
                state.reject("bad_json")
                return
            accept_event(state, event, REQUESTED_MODEL, now)

        try:
            while True:
                now = time.monotonic()
                if state.violation is not None:
                    abort_reason = state.violation
                    break
                if now - started >= args.total_timeout:
                    timeout_reason = "total_timeout"
                    break
                if not state.actual_model_seen and now - started >= args.startup_timeout:
                    timeout_reason = "startup_timeout"
                    break
                if state.result is None and state.actual_model_seen and state.last_progress is not None and now - state.last_progress >= args.idle_timeout:
                    timeout_reason = "idle_timeout"
                    break
                if now - last_status >= args.status_interval:
                    emit_status(state, payload_hash, len(payload), sent, started, now, args)
                    last_status = now

                if process.poll() is not None and not stdout_open and not stderr_open:
                    break
                deadlines = [started + args.total_timeout]
                if not state.actual_model_seen:
                    deadlines.append(started + args.startup_timeout)
                if state.result is None and state.actual_model_seen and state.last_progress is not None:
                    deadlines.append(state.last_progress + args.idle_timeout)
                timeout = max(0.0, min(0.1, min(deadlines) - now))
                for key, _ in selector.select(timeout):
                    mode = key.data
                    fileobj = key.fileobj
                    if mode == "input":
                        try:
                            written = os.write(fileobj.fileno(), payload[sent:])
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            selector.unregister(fileobj)
                            input_complete = False
                            continue
                        sent += written
                        if sent == len(payload):
                            selector.unregister(fileobj)
                            fileobj.close()
                            input_complete = True
                    else:
                        try:
                            chunk = os.read(fileobj.fileno(), 65536)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(fileobj)
                            if mode == "stdout":
                                stdout_open = False
                                try:
                                    tail = decoders[mode].decode(b"", final=True)
                                except UnicodeDecodeError:
                                    state.reject("invalid_output")
                                    tail = ""
                                text_buffer += tail
                                if text_buffer:
                                    consume_line(text_buffer, time.monotonic())
                                    text_buffer = ""
                            else:
                                stderr_open = False
                            continue
                        state.output_bytes += len(chunk)
                        if state.output_bytes > args.max_output_bytes:
                            state.reject("output_limit")
                        if mode == "stderr":
                            state.stderr_bytes += len(chunk)
                            continue
                        try:
                            decoded = decoders[mode].decode(chunk)
                        except UnicodeDecodeError:
                            state.reject("invalid_output")
                            decoded = ""
                        if len(text_buffer) + len(decoded) > MAX_LINE_BYTES:
                            state.reject("output_limit")
                            text_buffer = ""
                        else:
                            text_buffer += decoded
                        while "\n" in text_buffer:
                            line, text_buffer = text_buffer.split("\n", 1)
                            if len(line.encode("utf-8")) > MAX_LINE_BYTES:
                                state.reject("output_limit")
                            else:
                                consume_line(line, time.monotonic())
        except KeyboardInterrupt:
            state.reject("interrupted")
            emit_status(state, payload_hash, len(payload), sent, started, time.monotonic(), args, "error", process.returncode)
            return failure("interrupted")
        except Exception:
            state.reject("runner_error")
            emit_status(state, payload_hash, len(payload), sent, started, time.monotonic(), args, "error", process.returncode)
            return failure("runner_error")
        finally:
            selector.close()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
            # Ignore a repeated external termination while reaping the only
            # process group this runner created; restore handlers before exit.
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            terminate_owned(process)
            signal.signal(signal.SIGINT, previous_sigint)
            signal.signal(signal.SIGTERM, previous_sigterm)

        if timeout_reason is not None:
            emit_status(state, payload_hash, len(payload), sent, started, time.monotonic(), args, "error", process.returncode)
            return failure(timeout_reason, timeout=True)
        if abort_reason is not None:
            emit_status(state, payload_hash, len(payload), sent, started, time.monotonic(), args, "error", process.returncode)
            return failure(abort_reason)
        process.wait()
        reason = final_reason(state, process.returncode, input_complete)
        if reason:
            emit_status(state, payload_hash, len(payload), sent, started, time.monotonic(), args, "error", process.returncode)
            return failure(reason)
        assert state.result is not None
        emit_status(state, payload_hash, len(payload), sent, started, time.monotonic(), args, "complete", process.returncode)
        print("execution_status: complete")
        print("review_text:")
        print(state.result["result"])
        return 0


def main() -> int:
    args = parse_args()
    try:
        payload = explicit_stdin(args.max_input_bytes)
    except ValueError as exc:
        return failure(str(exc))
    return run(args, payload)


if __name__ == "__main__":
    raise SystemExit(main())
