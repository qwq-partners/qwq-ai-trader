"""합성 CLI 자식/HTTP 응답만 사용하는 Claude 전송 경계 검사."""
import asyncio
import importlib
import json
import os
from pathlib import Path
import sys

import pytest

from src.utils.llm import LLMConfig

MODEL = "claude-opus-5-5"


@pytest.fixture
def transport():
    try:
        return importlib.import_module("src.utils.claude_transport")
    except ModuleNotFoundError:
        pytest.fail("Claude transport implementation is missing")


def events():
    return [
        {"type": "system", "subtype": "init", "model": MODEL, "tools": [],
         "mcp_servers": [], "apiKeySource": "none", "skills": [], "plugins": []},
        {"type": "assistant", "message": {"model": MODEL, "stop_reason": "end_turn",
         "content": [{"type": "text", "text": "bounded review"}]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "num_turns": 1, "result": "bounded review", "stop_reason": "end_turn",
         "usage": {"input_tokens": 2, "output_tokens": 8,
                   "cache_creation_input_tokens": 580, "cache_read_input_tokens": 20},
         "modelUsage": {MODEL: {"inputTokens": 2, "outputTokens": 8,
             "cacheCreationInputTokens": 580, "cacheReadInputTokens": 20,
             "canonicalModel": MODEL, "provider": "firstParty", "costBasis": "list"}},
         "total_cost_usd": .004816},
    ]


def fake_cli(tmp_path, payload=None, *, delay=0, extra="", exit_code=0):
    """실제 실행 가능한 무해한 프로세스. 자격/외부 네트워크 접근 없음."""
    observed = tmp_path / "observed.json"
    command = tmp_path / "fake-claude"
    command.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time, subprocess\n"
        f"observed = {str(observed)!r}\n"
        "data = sys.stdin.read()\n"
        "with open(observed, 'w') as f:\n"
        " json.dump({'pid':os.getpid(), 'argv':sys.argv[1:], 'env':dict(os.environ),"
        "'cwd':os.getcwd(),'files':os.listdir('.'),'input':data}, f)\n"
        + extra + "\n"
        + f"time.sleep({delay!r})\n"
        + f"print({''.join(json.dumps(e) + chr(10) for e in (payload if payload is not None else events()))!r}, end='', flush=True)\n"
        + f"sys.exit({exit_code})\n"
    )
    command.chmod(0o700)
    return LLMConfig(claude_cli_command=str(command)), observed


def deadline(seconds=2):
    return asyncio.get_running_loop().time() + seconds


async def observed_pid(path):
    for _ in range(200):
        if path.exists():
            try:
                return json.loads(path.read_text())["pid"]
            except (ValueError, KeyError):
                pass
        await asyncio.sleep(.005)
    pytest.fail("synthetic child did not start")


def assert_reaped(pid):
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_cli_success_is_subscription_and_counts_all_cache_tokens(transport, tmp_path, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                 "KIS_APPKEY", "TELEGRAM_BOT_TOKEN", "NODE_OPTIONS", "ANTHROPIC_BASE_URL"):
        monkeypatch.setenv(name, "synthetic-not-inherited")
    config, observed = fake_cli(tmp_path)
    response = await transport.ClaudeCLIClient(config).complete(
        "private question @/never/read", system="private rules", model=MODEL, deadline=deadline())
    assert response.success and response.content == "bounded review"
    assert response.transport == "claude_cli" and response.billing_channel == "subscription"
    assert response.model_identity_status == "verified" and response.resolved_model == MODEL
    assert response.input_tokens == 602 and response.output_tokens == 8 and response.usage_known
    assert response.subscription_estimated_cost_usd == .004816
    assert response.actual_incremental_cost_usd is None
    observed = json.loads(observed.read_text())
    argv = observed["argv"]
    assert "private" not in " ".join(argv)
    assert "private question" in observed["input"] and "private rules" in observed["input"]
    assert observed["files"] == [] and not Path(observed["cwd"]).exists()
    assert "--safe-mode" in argv and "--no-session-persistence" in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--disable-slash-commands" in argv and "--strict-mcp-config" in argv
    assert "--bare" not in argv and "--dangerously-skip-permissions" not in argv
    assert "synthetic-not-inherited" not in observed["env"].values()
    assert observed["env"]["CLAUDE_CODE_MAX_RETRIES"] == "0"
    assert observed["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "400"
    assert observed["env"]["CLAUDE_CODE_DISABLE_ATTACHMENTS"] == "1"
    assert_reaped(observed["pid"])


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation,kind", [
    ("model", "identity_mismatch"), ("terminal_model", "identity_mismatch"),
    ("tools", "policy_violation"), ("mcp", "policy_violation"),
    ("auth", "policy_violation"), ("tool_use", "policy_violation"),
    ("refusal", "refusal"), ("truncated", "cli_incomplete"),
    ("empty", "cli_incomplete"), ("no_terminal", "cli_incomplete"),
    ("no_identity", "cli_incomplete"), ("missing_init", "cli_incomplete"),
    ("turn_limit", "cli_incomplete"),
])
async def test_cli_rejects_unverified_or_incomplete_results(transport, tmp_path, mutation, kind):
    payload = events()
    if mutation == "model": payload[1]["message"]["model"] = "different-model"
    elif mutation == "terminal_model": payload[2]["modelUsage"]["different-model"] = {}
    elif mutation == "tools": payload[0]["tools"] = ["Read"]
    elif mutation == "mcp": payload[0]["mcp_servers"] = [{"name": "bad"}]
    elif mutation == "auth": payload[0]["apiKeySource"] = "env"
    elif mutation == "tool_use": payload[1]["message"]["content"] = [{"type": "tool_use"}]
    elif mutation == "refusal": payload[1]["message"]["stop_reason"] = "refusal"
    elif mutation == "truncated": payload[1]["message"]["stop_reason"] = "max_tokens"
    elif mutation == "empty": payload[2]["result"] = " "
    elif mutation == "no_terminal": payload.pop()
    elif mutation == "no_identity": payload[1]["message"].pop("model")
    elif mutation == "missing_init": payload.pop(0)
    elif mutation == "turn_limit": payload[2]["subtype"] = "error_max_turns"
    config, _ = fake_cli(tmp_path, payload)
    response = await transport.ClaudeCLIClient(config).complete("q", model=MODEL, deadline=deadline())
    assert not response.success and response.content == ""
    assert response.transport_error_kind == kind
    assert response.request_sent
    assert response.refusal_detected == (mutation == "refusal")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["expired", "oversize", "model", "effort", "tokens", "disabled"])
async def test_cli_invalid_or_expired_request_never_starts(transport, tmp_path, mode):
    config, observed = fake_cli(tmp_path)
    if mode == "disabled": config.claude_cli_enabled = False
    kwargs = {"model": MODEL, "deadline": deadline(-1 if mode == "expired" else 2)}
    if mode == "model": kwargs["model"] = "opus"
    if mode == "effort": kwargs["reasoning_effort"] = "high"
    if mode == "tokens": kwargs["max_tokens"] = 401
    response = await transport.ClaudeCLIClient(config).complete("x" * (65537 if mode == "oversize" else 1), **kwargs)
    assert not response.success and not response.request_sent and not observed.exists()


@pytest.mark.asyncio
async def test_cli_timeout_kills_and_reaps_real_child(transport, tmp_path):
    config, observed = fake_cli(tmp_path, delay=30)
    response = await transport.ClaudeCLIClient(config).complete("q", model=MODEL, deadline=deadline(.35))
    assert response.transport_error_kind == "cli_timeout" and response.request_sent
    assert_reaped(json.loads(observed.read_text())["pid"])


@pytest.mark.asyncio
async def test_cli_cancel_and_close_reap_child_and_attach_attempt(transport, tmp_path):
    config, observed = fake_cli(tmp_path, delay=30)
    client = transport.ClaudeCLIClient(config)
    task = asyncio.create_task(client.complete("q", model=MODEL, deadline=deadline()))
    pid = await observed_pid(observed)
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    response = caught.value._llm_response
    assert response.request_sent and response.transport == "claude_cli"
    assert response.transport_error_kind == "cancelled"
    assert_reaped(pid)
    observed.unlink()
    task = asyncio.create_task(client.complete("q", model=MODEL, deadline=deadline()))
    pid = await observed_pid(observed)
    await client.close()
    with pytest.raises(asyncio.CancelledError): await task
    assert_reaped(pid)
    response = await client.complete("q", model=MODEL)
    assert not response.request_sent and response.transport_error_kind == "client_closed"


@pytest.mark.asyncio
async def test_cli_lock_wait_uses_same_deadline_and_does_not_spawn(transport, tmp_path):
    config, observed = fake_cli(tmp_path, delay=30)
    client = transport.ClaudeCLIClient(config)
    first = asyncio.create_task(client.complete("q", model=MODEL, deadline=deadline()))
    pid = await observed_pid(observed)
    second = await client.complete("q", model=MODEL, deadline=deadline(.2))
    assert second.transport_error_kind == "cli_timeout" and not second.request_sent
    assert json.loads(observed.read_text())["pid"] == pid
    first.cancel()
    with pytest.raises(asyncio.CancelledError): await first
    assert_reaped(pid)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream,amount", [("stdout", 1048577), ("stderr", 65537)])
async def test_cli_output_caps_kill_real_child(transport, tmp_path, stream, amount):
    config, observed = fake_cli(tmp_path, delay=30,
        extra=f"sys.{stream}.write('x' * {amount}); sys.{stream}.flush()")
    response = await transport.ClaudeCLIClient(config).complete("q", model=MODEL, deadline=deadline())
    assert response.transport_error_kind == "cli_incomplete"
    assert_reaped(json.loads(observed.read_text())["pid"])


class Reply:
    def __init__(self, body, status=200, delay=0):
        self.body, self.status, self.delay = body, status, delay
    async def __aenter__(self):
        await asyncio.sleep(self.delay)
        return self
    async def __aexit__(self, *args): pass
    async def json(self): return self.body


class Session:
    closed = False
    def __init__(self, body=None, status=200, delay=0):
        self.reply = Reply(body if body is not None else api_body(), status, delay)
        self.calls = []
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.reply
    async def close(self): self.closed = True


def api_body():
    return {"id": "synthetic", "type": "message", "role": "assistant", "model": MODEL,
            "stop_reason": "end_turn", "content": [{"type": "text", "text": "review"}],
            "usage": {"input_tokens": 11, "output_tokens": 7,
                "cache_creation_input_tokens": 13, "cache_read_input_tokens": 17}}


@pytest.mark.asyncio
async def test_api_request_and_cache_usage(transport):
    client = transport.AnthropicAPIClient(LLMConfig(anthropic_api_key="synthetic-key"))
    client._session = Session()
    response = await client.complete("question", "rules", model=MODEL, deadline=deadline())
    assert response.success and response.resolved_model == MODEL
    assert response.input_tokens == 41 and response.output_tokens == 7
    assert response.transport == "anthropic_messages" and response.billing_channel == "api"
    url, request = client._session.calls[0]
    assert len(client._session.calls) == 1 and url == "https://api.anthropic.com/v1/messages"
    assert request["json"] == {"model": MODEL, "max_tokens": 400, "system": "rules",
        "messages": [{"role": "user", "content": "question"}],
        "output_config": {"effort": "low"}, "thinking": {"type": "adaptive"}}
    assert request["headers"]["x-api-key"] == "synthetic-key"
    assert request["allow_redirects"] is False
    await client.close()
    assert client._session.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation,kind", [
    ("model", "identity_mismatch"), ("missing_model", "api_incomplete"),
    ("stop", "api_incomplete"), ("refusal", "refusal"),
    ("tool", "policy_violation"), ("empty", "api_incomplete"),
])
async def test_api_requires_complete_identity_text(transport, mutation, kind):
    body = api_body()
    if mutation == "model": body["model"] = "different-model"
    elif mutation == "missing_model": body.pop("model")
    elif mutation == "stop": body["stop_reason"] = "max_tokens"
    elif mutation == "refusal": body["stop_reason"] = "refusal"
    elif mutation == "tool": body["content"] = [{"type": "tool_use"}]
    elif mutation == "empty": body["content"] = []
    client = transport.AnthropicAPIClient(LLMConfig(anthropic_api_key="synthetic-key"))
    client._session = Session(body)
    response = await client.complete("q", model=MODEL, deadline=deadline())
    assert not response.success and response.content == "" and response.transport_error_kind == kind


@pytest.mark.asyncio
@pytest.mark.parametrize("status,known,billing", [(429, False, "rejected_before_generation"),
    (500, False, "unknown"), (200, False, "unknown")])
async def test_api_missing_usage_and_errors_never_claim_free_usage(transport, status, known, billing):
    body = api_body() if status == 200 else {"error": {"type": "rate_limit_error", "message": "synthetic-key"}}
    body.pop("usage", None)
    client = transport.AnthropicAPIClient(LLMConfig(anthropic_api_key="synthetic-key"))
    client._session = Session(body, status)
    response = await client.complete("q", model=MODEL, deadline=deadline())
    assert response.usage_known is known and response.billing_status == billing
    assert "synthetic-key" not in (response.error or "")
    assert len(client._session.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["valid", "symlink", "mode", "oversize", "owner"])
async def test_key_file_is_lazy_and_rejects_unsafe_files(transport, tmp_path, monkeypatch, case):
    key = tmp_path / "key"
    key.write_text("synthetic-file-key\n")
    key.chmod(0o600)
    config = LLMConfig(anthropic_api_key_file=str(key))
    client = transport.AnthropicAPIClient(config)
    client._session = Session()
    if case == "symlink":
        link = tmp_path / "link"
        link.symlink_to(key)
        config.anthropic_api_key_file = str(link)
    elif case == "mode": key.chmod(0o644)
    elif case == "oversize": key.write_text("x" * 4097)
    elif case == "owner": monkeypatch.setattr(transport.os, "getuid", lambda: -1)
    response = await client.complete("q", model=MODEL, deadline=deadline())
    assert response.success is (case == "valid")
    assert len(client._session.calls) == (1 if case == "valid" else 0)
    assert response.request_sent is (case == "valid")


@pytest.mark.asyncio
async def test_api_cancel_preserves_sent_unknown_attempt(transport):
    client = transport.AnthropicAPIClient(LLMConfig(anthropic_api_key="synthetic-key"))
    client._session = Session(delay=30)
    task = asyncio.create_task(client.complete("q", model=MODEL, deadline=deadline()))
    await asyncio.sleep(.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught: await task
    response = caught.value._llm_response
    assert response.request_sent and not response.usage_known and response.billing_status == "unknown"
    assert response.transport == "anthropic_messages"


@pytest.mark.asyncio
async def test_cancel_during_spawn_waits_for_ownership_and_reaps(transport, tmp_path, monkeypatch):
    config, observed = fake_cli(tmp_path, delay=30)
    real_spawn = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    release = asyncio.Event()
    children = []
    async def delayed_spawn(*args, **kwargs):
        proc = await real_spawn(*args, **kwargs)
        children.append(proc)
        spawned.set()
        await release.wait()
        return proc
    monkeypatch.setattr(transport.asyncio, "create_subprocess_exec", delayed_spawn)
    client = transport.ClaudeCLIClient(config)
    task = asyncio.create_task(client.complete("q", model=MODEL, deadline=deadline()))
    await spawned.wait()
    task.cancel()
    await asyncio.sleep(.01)
    task.cancel()
    release.set()
    try:
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert not caught.value._llm_response.request_sent
        assert_reaped(children[0].pid)
    finally:
        for proc in children:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


@pytest.mark.asyncio
async def test_cancel_arriving_during_cleanup_is_not_swallowed(transport, tmp_path, monkeypatch):
    config, _ = fake_cli(tmp_path)
    client = transport.ClaudeCLIClient(config)
    cleanup_started = asyncio.Event()
    release = asyncio.Event()
    real_cleanup = client._cleanup
    async def delayed_cleanup(proc, tasks):
        cleanup_started.set()
        await release.wait()
        await real_cleanup(proc, tasks)
    monkeypatch.setattr(client, "_cleanup", delayed_cleanup)
    task = asyncio.create_task(client.complete("q", model=MODEL, deadline=deadline()))
    await cleanup_started.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught: await task
    assert caught.value._llm_response.transport_error_kind == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("violation,kind", [("refusal", "refusal"), ("model", "identity_mismatch"),
                                             ("tool", "policy_violation")])
async def test_malformed_tail_cannot_hide_forbidden_fallback(transport, tmp_path, violation, kind):
    payload = events()
    if violation == "refusal": payload[1]["message"]["stop_reason"] = "refusal"
    elif violation == "model": payload[1]["message"]["model"] = "other"
    elif violation == "tool": payload[1]["message"]["content"] = [{"type": "tool_use"}]
    config, _ = fake_cli(tmp_path, payload)
    script = Path(config.claude_cli_command)
    script.write_text(script.read_text().replace("sys.exit(0)", "print('{broken')\nsys.exit(0)"))
    response = await transport.ClaudeCLIClient(config).complete("q", model=MODEL, deadline=deadline())
    assert response.transport_error_kind == kind


@pytest.mark.asyncio
async def test_cli_timeout_kills_owned_descendant(transport, tmp_path):
    descendant = tmp_path / "descendant"
    config, observed = fake_cli(tmp_path, delay=30, extra=(
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"open({str(descendant)!r}, 'w').write(str(child.pid))"))
    response = await transport.ClaudeCLIClient(config).complete("q", model=MODEL, deadline=deadline(.4))
    assert response.transport_error_kind == "cli_timeout"
    assert_reaped(json.loads(observed.read_text())["pid"])
    pid = int(descendant.read_text())
    state = Path(f"/proc/{pid}/stat")
    # 손자는 init의 회수 대상이다. 실행/수면 상태로 생존해서는 안 된다.
    assert not state.exists() or state.read_text().split()[2] == "Z"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["expired", "missing_key", "invalid_model", "closed"])
async def test_api_local_failure_sends_nothing(transport, mode):
    client = transport.AnthropicAPIClient(LLMConfig(anthropic_api_key="" if mode == "missing_key" else "synthetic"))
    client._session = Session()
    if mode == "closed": await client.close()
    response = await client.complete("q", model="opus" if mode == "invalid_model" else MODEL,
        deadline=deadline(-1 if mode == "expired" else 1))
    assert not response.success and not response.request_sent and client._session.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("violation,kind", [("refusal", "refusal"), ("model", "identity_mismatch"),
                                             ("tool", "policy_violation"), ("auth", "policy_violation")])
async def test_streamed_forbidden_result_cannot_become_timeout(transport, tmp_path, violation, kind):
    payload = events()[:2]
    if violation == "refusal": payload[1]["message"]["stop_reason"] = "refusal"
    elif violation == "model": payload[1]["message"]["model"] = "other"
    elif violation == "tool": payload[1]["message"]["content"] = [{"type": "tool_use"}]
    elif violation == "auth": payload[0]["apiKeySource"] = "env"
    raw = ''.join(json.dumps(record) + '\n' for record in payload)
    config, observed = fake_cli(tmp_path, delay=30, extra=f"print({raw!r}, end='', flush=True)")
    response = await transport.ClaudeCLIClient(config).complete("q", model=MODEL, deadline=deadline(.4))
    assert response.transport_error_kind == kind
    assert_reaped(json.loads(observed.read_text())["pid"])


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["rate_limit_error", "authentication_error", "model_refusal_fallback"])
async def test_cli_structured_errors_are_classified_without_copying_text(transport, tmp_path, reason):
    payload = [{"type": "error", "error": {"type": reason, "message": "private synthetic detail"}}]
    config, _ = fake_cli(tmp_path, payload, exit_code=1)
    response = await transport.ClaudeCLIClient(config).complete("q", model=MODEL, deadline=deadline())
    assert response.transport_error_kind == {"rate_limit_error": "cli_rate_limited",
        "authentication_error": "cli_auth_unavailable", "model_refusal_fallback": "refusal"}[reason]
    assert "private" not in response.error


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("input_tokens", True), ("output_tokens", -1),
    ("cache_creation_input_tokens", None), ("cache_read_input_tokens", "12")])
async def test_api_malformed_usage_does_not_claim_known_cost(transport, field, value):
    body = api_body()
    body["usage"][field] = value
    client = transport.AnthropicAPIClient(LLMConfig(anthropic_api_key="synthetic"))
    client._session = Session(body)
    response = await client.complete("q", model=MODEL, deadline=deadline())
    assert response.success and not response.usage_known and response.billing_status == "unknown"


@pytest.mark.asyncio
async def test_api_timeout_and_close_preserve_attempt(transport):
    client = transport.AnthropicAPIClient(LLMConfig(anthropic_api_key="synthetic"))
    client._session = Session(delay=30)
    response = await client.complete("q", model=MODEL, deadline=deadline(.05))
    assert response.transport_error_kind == "api_timeout" and response.request_sent
    task = asyncio.create_task(client.complete("q", model=MODEL, deadline=deadline()))
    await asyncio.sleep(.01)
    await client.close()
    with pytest.raises(asyncio.CancelledError) as caught: await task
    assert caught.value._llm_response.request_sent
    assert client._session.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["block_type", "error_type", "event_type"])
async def test_cli_malformed_scalar_is_safe_failure(transport, tmp_path, path):
    payload = events()
    if path == "block_type": payload[1]["message"]["content"][0]["type"] = []
    elif path == "error_type": payload[1]["error"] = {"type": []}
    else: payload[1]["type"] = []
    config, _ = fake_cli(tmp_path, payload)
    response = await transport.ClaudeCLIClient(config).complete("q", model=MODEL, deadline=deadline())
    assert not response.success and response.transport_error_kind == "cli_incomplete"


@pytest.mark.asyncio
async def test_cli_terminal_failure_retains_reported_usage(transport, tmp_path):
    payload = events()
    payload[2]["modelUsage"]["different-model"] = {}
    config, _ = fake_cli(tmp_path, payload)
    response = await transport.ClaudeCLIClient(config).complete("q", model=MODEL, deadline=deadline())
    assert not response.success and response.transport_error_kind == "identity_mismatch"
    assert response.usage_known and response.input_tokens == 602
    assert response.subscription_estimated_cost_usd == .004816


@pytest.mark.asyncio
async def test_cli_qualified_stream_null_assistant_stop_and_terminal_end_turn(transport, tmp_path):
    # coordinator의2.1.310 합성 실측 형식; 숨은 추론/원문은 포함하지 않는다.
    payload = events()
    payload[1]["message"]["stop_reason"] = None
    payload.insert(2, {"type": "stream_event", "event": {
        "type": "message_delta", "delta": {"stop_reason": "end_turn"}}})
    payload[-1]["permission_denials"] = []
    config, observed = fake_cli(tmp_path, payload)
    response = await transport.ClaudeCLIClient(config).complete("q", model=MODEL, deadline=deadline())
    assert response.success and response.finish_reason == "end_turn"
    assert json.loads(observed.read_text())["env"]["CLAUDE_CODE_DISABLE_THINKING"] == "1"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["denied_tool", "missing_terminal_reason", "delta_truncated"])
async def test_cli_stop_and_permissions_must_be_consistent(transport, tmp_path, mode):
    payload = events()
    if mode == "denied_tool": payload[-1]["permission_denials"] = [{"tool_name": "Read"}]
    elif mode == "missing_terminal_reason": payload[-1].pop("stop_reason")
    else: payload.insert(2, {"type": "stream_event", "event": {
        "type": "message_delta", "delta": {"stop_reason": "max_tokens"}}})
    config, _ = fake_cli(tmp_path, payload)
    response = await transport.ClaudeCLIClient(config).complete("q", model=MODEL, deadline=deadline())
    assert not response.success
    assert response.transport_error_kind == ("policy_violation" if mode == "denied_tool" else "cli_incomplete")
    assert response.usage_known and response.input_tokens == 602


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("mode", ["cap", "timeout", "cancel"])
async def test_cli_flooded_pipe_cleanup_finishes_and_reaps(transport, tmp_path, monkeypatch, stream, mode):
    config, observed = fake_cli(tmp_path, delay=30, extra=(
        f"sys.{stream}.buffer.write(b'x' * (16 * 1024 * 1024)); sys.{stream}.flush()"))
    client = transport.ClaudeCLIClient(config)
    real_spawn = asyncio.create_subprocess_exec
    children = []

    async def track_spawn(*args, **kwargs):
        proc = await real_spawn(*args, **kwargs)
        children.append(proc)
        return proc

    monkeypatch.setattr(transport.asyncio, "create_subprocess_exec", track_spawn)
    if mode != "cap":
        # 파이프가 실제 pause 상태일 때 timeout/cancel 경로를 검사한다.
        async def stalled_reader(*args):
            await asyncio.Event().wait()
        monkeypatch.setattr(client, "_read", stalled_reader)
    task = asyncio.create_task(client.complete("q", model=MODEL, deadline=deadline(.4)))
    if mode == "cancel":
        for _ in range(100):
            if children and getattr(children[0], stream)._transport.is_reading() is False:
                break
            await asyncio.sleep(.002)
        else:
            pytest.fail("synthetic child pipe did not enter paused state")
        task.cancel()
    done, _ = await asyncio.wait({task}, timeout=1.0)
    completed = bool(done)
    if not completed:
        # RED 실행도 자식/파이프를 방치하지 않도록 테스트 자체가 구조한다.
        for proc in children:
            for pipe in (proc.stdout, proc.stderr):
                pipe._transport.close()
    result = (await asyncio.gather(task, return_exceptions=True))[0]
    assert completed, "flooded pipe prevented cleanup/reaping beyond the total deadline"
    if mode == "cancel":
        assert isinstance(result, asyncio.CancelledError)
    else:
        assert result.transport_error_kind == ("cli_incomplete" if mode == "cap" else "cli_timeout")
    assert_reaped(json.loads(observed.read_text())["pid"])
