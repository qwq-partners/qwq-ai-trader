"""제한된 Claude 단일 전송. 라우팅·재시도·예산 정산은 manager 소유다."""
from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import signal
import stat
import tempfile

import aiohttp

from .llm import LLMConfig, LLMProvider, LLMResponse, _billing_status

_MODEL = "claude-opus-5-5"
_INPUT_LIMIT = 64 * 1024
_STDOUT_LIMIT = 1024 * 1024
_STDERR_LIMIT = 64 * 1024
_CLEANUP_RESERVE = .1


class _TransportFailure(Exception):
    def __init__(self, kind):
        self.kind = kind


def _reject(response, kind):
    response.success = False
    response.content = ""
    response.error = kind
    response.transport_error_kind = kind
    if kind == "refusal":
        response.refusal_detected = True
    return response


def _usage(response, usage):
    """캐시 입력을 빠뜨리지 않으며 비정상 수치는 미확인으로 유지한다."""
    if not isinstance(usage, dict):
        return
    values = [usage.get("input_tokens"), usage.get("output_tokens"),
              usage.get("cache_creation_input_tokens", 0), usage.get("cache_read_input_tokens", 0)]
    if not all(type(n) is int and n >= 0 for n in values):
        return
    response.input_tokens = values[0] + values[2] + values[3]
    response.output_tokens = values[1]
    response.usage_known = True
    response.billing_status = "reported_usage"


def _controls(prompt, system, model, effort, max_tokens):
    return (isinstance(prompt, str) and isinstance(system, str)
            and bool(prompt.strip()) and model == _MODEL and effort == "low"
            and type(max_tokens) is int and 1 <= max_tokens <= 400
            and len(prompt.encode("utf-8")) + len(system.encode("utf-8")) <= _INPUT_LIMIT)


def _identity(response, actual):
    if not isinstance(actual, str) or not actual:
        response.model_identity_status = "unverified"
        return False
    response.resolved_model = actual
    response.model_identity_status = "verified" if actual == response.requested_model else "mismatch"
    if response.model_identity_status == "mismatch":
        raise _TransportFailure("identity_mismatch")
    return True


def _text_and_policy(message):
    blocks = message.get("content")
    if message.get("stop_reason") == "refusal":
        raise _TransportFailure("refusal")
    if not isinstance(blocks, list):
        return ""
    text = []
    for block in blocks:
        if not isinstance(block, dict):
            return ""
        kind = block.get("type")
        if kind in ("refusal", "redacted_refusal"):
            raise _TransportFailure("refusal")
        if kind in ("tool_use", "server_tool_use", "tool_result"):
            raise _TransportFailure("policy_violation")
        if kind == "text" and isinstance(block.get("text"), str):
            text.append(block["text"])
    return "".join(text).strip()


class _Client:
    provider = LLMProvider.ANTHROPIC

    def __init__(self, config):
        self.config = config
        self._closed = False
        self._calls = set()

    def _response(self, model, effort):
        return LLMResponse("", model, self.provider, success=False, requested_model=model,
                           requested_reasoning_effort=effort, usage_known=False,
                           request_sent=False, billing_status="not_sent", transport=self.transport,
                           billing_channel=self.billing_channel, model_identity_status="unverified")

    async def close(self):
        self._closed = True
        pending = [t for t in self._calls if t is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


class ClaudeCLIClient(_Client):
    transport = "claude_cli"
    billing_channel = "subscription"

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._lock = asyncio.Lock()

    def _command(self, model, effort):
        return [self.config.claude_cli_command, "--safe-mode", "-p", "--model", model,
                "--effort", effort, "--tools", "", "--strict-mcp-config", "--mcp-config",
                '{"mcpServers":{}}', "--no-session-persistence", "--disable-slash-commands",
                "--permission-mode", "dontAsk", "--setting-sources", "",
                # Safe mode leaves these built-ins active. Disable them only
                # for this invocation; preserve managed policy/security mods.
                "--settings", json.dumps({"enabledPlugins": {
                    "cc-plugin-agents-md@builtin": False,
                    "cc-plugin-telemetry@builtin": False,
                    "cc-plugin-plugin-authoring@builtin": False,
                }}),
                "--output-format", "stream-json", "--verbose", "--include-partial-messages", "--no-chrome",
                "--max-turns", "1", "--max-budget-usd", "0.25", "--system-prompt",
                "Provide a bounded independent review of the supplied input. Do not use tools."]

    @staticmethod
    def _environment(max_tokens):
        allowed = {"HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "TERM",
                   "SSL_CERT_FILE", "SSL_CERT_DIR"}
        env = {key: value for key, value in os.environ.items() if key in allowed}
        env.update(CLAUDE_CODE_MAX_RETRIES="0", CLAUDE_CODE_MAX_OUTPUT_TOKENS=str(max_tokens),
                   CLAUDE_CODE_DISABLE_ATTACHMENTS="1", CLAUDE_CODE_DISABLE_THINKING="1")
        return env

    @staticmethod
    def _inspect_record(record, response):
        """금지 사유를 수신 즉시 고정해 이후 timeout/잘림의 우회를 막는다."""
        if not isinstance(record, dict):
            return
        kind = record.get("type")
        if kind == "system" and record.get("subtype") == "init":
            if (record.get("tools") != [] or record.get("mcp_servers") != []
                    or record.get("apiKeySource") != "none"
                    or record.get("plugins", []) != [] or record.get("skills", []) != []):
                raise _TransportFailure("policy_violation")
        if kind == "assistant" and isinstance(record.get("message"), dict):
            message = record["message"]
            _text_and_policy(message)
            _identity(response, message.get("model"))
        if kind == "result":
            _usage(response, record.get("usage"))
            cost = record.get("total_cost_usd")
            if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
                response.subscription_estimated_cost_usd = float(cost)
            if record.get("permission_denials", []) != []:
                raise _TransportFailure("policy_violation")
            if record.get("stop_reason") == "refusal":
                raise _TransportFailure("refusal")
            models = record.get("modelUsage")
            if isinstance(models, dict) and models:
                if set(models) != {response.requested_model}:
                    response.model_identity_status = "mismatch"
                    raise _TransportFailure("identity_mismatch")
                details = models[response.requested_model]
                if isinstance(details, dict):
                    if details.get("canonicalModel", response.requested_model) != response.requested_model:
                        response.model_identity_status = "mismatch"
                        raise _TransportFailure("identity_mismatch")
                    if details.get("provider", "firstParty") != "firstParty":
                        raise _TransportFailure("policy_violation")
        if kind == "stream_event" and isinstance(record.get("event"), dict):
            event = record["event"]
            block = event.get("content_block")
            if isinstance(block, dict):
                _text_and_policy({"content": [block]})
            delta = event.get("delta")
            if isinstance(delta, dict) and delta.get("stop_reason") == "refusal":
                raise _TransportFailure("refusal")
            message = event.get("message")
            if isinstance(message, dict) and message.get("model"):
                _identity(response, message["model"])
        error = record.get("error")
        error_type = error.get("type") if isinstance(error, dict) else error
        if error_type in ("refusal", "model_refusal_fallback"):
            raise _TransportFailure("refusal")

    @classmethod
    async def _read(cls, stream, limit, response=None):
        output = bytearray()
        pending = bytearray()
        while chunk := await stream.read(min(65536, limit + 1 - len(output))):
            output.extend(chunk)
            if response is not None:
                pending.extend(chunk)
                while b"\n" in pending:
                    line, _, rest = pending.partition(b"\n")
                    pending = bytearray(rest)
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    try:
                        cls._inspect_record(record, response)
                    except _TransportFailure as exc:
                        _reject(response, exc.kind)
                        raise
            if len(output) > limit:
                raise _TransportFailure("cli_incomplete")
        return bytes(output)

    @staticmethod
    async def _cleanup(proc, io_tasks):
        # 고유 세션의 그룹만 종료한다. 정상 종료 뒤 남은 자손도 같은 그룹에 한정한다.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for task in io_tasks:
            if not task.done():
                task.cancel()
        if io_tasks:
            await asyncio.gather(*io_tasks, return_exceptions=True)
        if proc.stdin is not None:
            proc.stdin.close()

        async def discard(stream):
            # 상한 초과로 reader가 끝나면 asyncio 파이프는 pause 상태일 수 있다.
            # kill 이후 남은 바이트를 저장 없이 소비해 EOF/transport 종료를 진행한다.
            if stream is not None:
                while await stream.read(65536):
                    pass

        await asyncio.gather(discard(proc.stdout), discard(proc.stderr), proc.wait())

    async def _run(self, data, response, model, effort, max_tokens):
        proc = None
        io_tasks = []
        with tempfile.TemporaryDirectory(prefix="qwq-claude-") as cwd:
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                *self._command(model, effort), cwd=cwd, env=self._environment(max_tokens),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True))
            try:
                # 시작 중 취소되어도 생성 결과를 회수한 뒤 소유한 프로세스를 정리한다.
                proc = await asyncio.shield(spawn)
                io_tasks = [asyncio.create_task(self._read(proc.stdout, _STDOUT_LIMIT, response)),
                            asyncio.create_task(self._read(proc.stderr, _STDERR_LIMIT))]
                proc.stdin.write(data)
                response.request_sent = True
                response.billing_status = "unknown"
                await proc.stdin.drain()
                proc.stdin.close()
                stdout, _ = await asyncio.gather(*io_tasks)
                code = await proc.wait()
                self._parse(stdout, code, response)
            finally:
                cleanup_cancel = None
                if proc is None:
                    # 반복 취소도 생성 task에 전달하지 않는다. 반환된 소유권은 반드시 회수한다.
                    while not spawn.done():
                        try:
                            await asyncio.shield(spawn)
                        except asyncio.CancelledError as exc:
                            cleanup_cancel = exc
                        except OSError:
                            break
                    if not spawn.cancelled() and spawn.exception() is None:
                        proc = spawn.result()
                if proc is not None:
                    cleanup = asyncio.create_task(self._cleanup(proc, io_tasks))
                    # 반복 취소도 회수를 중단시키지 않는다.
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError as exc:
                            cleanup_cancel = exc
                    cleanup.result()
                if cleanup_cancel is not None:
                    raise cleanup_cancel

    @staticmethod
    def _parse(stdout, returncode, response):
        records = []
        malformed = False
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                malformed = True
                continue
            if not isinstance(record, dict):
                malformed = True
                continue
            records.append(record)
        terminals = [record for record in records if record.get("type") == "result"]
        inits = []
        assistants = []
        missing_identity = False
        for record in records:
            ClaudeCLIClient._inspect_record(record, response)
            kind = record.get("type")
            if kind == "system" and record.get("subtype") == "init":
                inits.append(record)
            elif kind == "assistant":
                message = record.get("message")
                if not isinstance(message, dict):
                    malformed = True
                    continue
                if not isinstance(message.get("model"), str) or not message["model"]:
                    missing_identity = True
                assistants.append(message)
            error = record.get("error")
            if error is not None:
                error_type = error.get("type") if isinstance(error, dict) else error
                if not isinstance(error_type, str):
                    malformed = True
        for record in records:
            error = record.get("error")
            error_type = error.get("type") if isinstance(error, dict) else error
            if error_type == "rate_limit_error":
                raise _TransportFailure("cli_rate_limited")
            if error_type in ("authentication_error", "not_logged_in"):
                raise _TransportFailure("cli_auth_unavailable")
        if malformed or missing_identity or len(terminals) != 1 or len(inits) != 1 or not assistants:
            raise _TransportFailure("cli_incomplete")
        terminal = terminals[0]
        model_usage = terminal.get("modelUsage")
        if not isinstance(model_usage, dict) or not model_usage:
            raise _TransportFailure("cli_incomplete")
        if set(model_usage) != {response.requested_model}:
            raise _TransportFailure("identity_mismatch")
        details = model_usage[response.requested_model]
        if not isinstance(details, dict):
            raise _TransportFailure("cli_incomplete")
        canonical = details.get("canonicalModel")
        if canonical is not None and canonical != response.requested_model:
            raise _TransportFailure("identity_mismatch")
        if details.get("provider", "firstParty") != "firstParty":
            raise _TransportFailure("policy_violation")
        # CLI2.1.310의 assistant stop_reason은 null, delta/terminal이 end_turn이다.
        reasons = [message.get("stop_reason") for message in assistants
                   if message.get("stop_reason") is not None]
        for record in records:
            event = record.get("event") if record.get("type") == "stream_event" else None
            if isinstance(event, dict) and event.get("type") == "message_delta":
                delta = event.get("delta")
                if isinstance(delta, dict) and delta.get("stop_reason") is not None:
                    reasons.append(delta["stop_reason"])
        response.finish_reason = terminal.get("stop_reason")
        result = terminal.get("result")
        if (returncode != 0 or terminal.get("subtype") != "success"
                or terminal.get("is_error") is not False or terminal.get("num_turns") != 1
                or response.finish_reason != "end_turn"
                or not reasons or any(reason != "end_turn" for reason in reasons)
                or not isinstance(result, str) or not result.strip()
                or not _text_and_policy(assistants[-1])):
            raise _TransportFailure("cli_incomplete")
        response.content = result.strip()
        response.success = True

    async def complete(self, prompt, system="", *, model, max_tokens=400,
                       reasoning_effort="low", deadline=None, **kwargs):
        response = self._response(model, reasoning_effort)
        loop = asyncio.get_running_loop()
        started = loop.time()
        if self._closed:
            return _reject(response, "client_closed")
        if not self.config.claude_cli_enabled:
            return _reject(response, "cli_unavailable")
        if not _controls(prompt, system, model, reasoning_effort, max_tokens):
            return _reject(response, "invalid_request")
        data = (system + "\n\n" + prompt if system else prompt).encode("utf-8")
        if len(data) > _INPUT_LIMIT:
            return _reject(response, "invalid_request")
        deadline = started + 8 if deadline is None else deadline
        if not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
            return _reject(response, "invalid_request")
        if deadline - _CLEANUP_RESERVE <= loop.time():
            return _reject(response, "cli_timeout")
        task = asyncio.current_task()
        self._calls.add(task)
        try:
            async with asyncio.timeout_at(deadline - _CLEANUP_RESERVE):
                async with self._lock:
                    if self._closed:
                        return _reject(response, "client_closed")
                    await self._run(data, response, model, reasoning_effort, max_tokens)
            return response
        except asyncio.CancelledError as exc:
            exc._llm_response = _reject(response, "cancelled")
            raise
        except TimeoutError:
            return _reject(response, response.transport_error_kind or "cli_timeout")
        except _TransportFailure as exc:
            return _reject(response, response.transport_error_kind or exc.kind)
        except OSError:
            return _reject(response, "cli_unavailable" if not response.request_sent else "cli_incomplete")
        finally:
            response.latency_ms = (loop.time() - started) * 1000
            self._calls.discard(task)


class AnthropicAPIClient(_Client):
    transport = "anthropic_messages"
    billing_channel = "api"

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._session = None

    def _key(self):
        key = self.config.anthropic_api_key
        if not key:
            path = self.config.anthropic_api_key_file
            if not path:
                raise _TransportFailure("api_auth_unavailable")
            # 최종 경로 symlink·TOCTOU를 fd 기반 검사로 방어한다. 값을 캐시하지 않는다.
            fd = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_uid != os.getuid() or info.st_size > 4096):
                    raise _TransportFailure("api_auth_unavailable")
                key = os.read(fd, 4097).decode("ascii").strip()
            finally:
                os.close(fd)
        if (not isinstance(key, str) or not key or len(key) > 4096
                or not key.isascii() or any(char.isspace() or ord(char) < 33 for char in key)):
            raise _TransportFailure("api_auth_unavailable")
        return key

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(trust_env=False)
        return self._session

    async def close(self):
        await super().close()
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def complete(self, prompt, system="", *, model, max_tokens=400,
                       reasoning_effort="low", deadline=None, **kwargs):
        response = self._response(model, reasoning_effort)
        loop = asyncio.get_running_loop()
        started = loop.time()
        if self._closed:
            return _reject(response, "client_closed")
        if not _controls(prompt, system, model, reasoning_effort, max_tokens):
            return _reject(response, "invalid_request")
        deadline = started + 12 if deadline is None else deadline
        if not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
            return _reject(response, "invalid_request")
        if deadline <= loop.time():
            return _reject(response, "api_timeout")
        task = asyncio.current_task()
        self._calls.add(task)
        try:
            async with asyncio.timeout_at(deadline):
                key = self._key()
                session = await self._get_session()
                body = {"model": model, "max_tokens": max_tokens,
                        "messages": [{"role": "user", "content": prompt}],
                        "output_config": {"effort": reasoning_effort}, "thinking": {"type": "adaptive"}}
                if system:
                    body["system"] = system
                response.request_sent = True
                response.billing_status = "unknown"
                async with session.post("https://api.anthropic.com/v1/messages", json=body,
                        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                 "content-type": "application/json"}, allow_redirects=False,
                        timeout=aiohttp.ClientTimeout(total=max(.001, deadline-loop.time()))) as reply:
                    response.http_status = reply.status
                    data = await reply.json()
                if not isinstance(data, dict):
                    return _reject(response, "api_incomplete")
                _usage(response, data.get("usage"))
                response.billing_status = _billing_status(data, response.http_status, response.usage_known)
                if response.http_status != 200:
                    error = data.get("error")
                    if isinstance(error, dict):
                        # 외부 오류 원문은 자격/프롬프트를 포함할 수 있어 복사하지 않는다.
                        error_type = error.get("type")
                        if error_type in ("authentication_error", "permission_error", "rate_limit_error",
                                          "invalid_request_error", "overloaded_error", "api_error"):
                            response.provider_error_type = error_type
                    return _reject(response, "api_http_error")
                text = _text_and_policy(data)
                if not _identity(response, data.get("model")):
                    return _reject(response, "api_incomplete")
                response.finish_reason = data.get("stop_reason")
                if response.finish_reason != "end_turn" or not text:
                    return _reject(response, "api_incomplete")
                response.content = text
                response.success = True
                return response
        except asyncio.CancelledError as exc:
            exc._llm_response = _reject(response, "cancelled")
            raise
        except TimeoutError:
            return _reject(response, "api_timeout")
        except _TransportFailure as exc:
            return _reject(response, exc.kind)
        except (OSError, UnicodeError):
            return _reject(response, "api_unavailable" if response.request_sent else "api_auth_unavailable")
        except (ValueError, aiohttp.ClientError):
            return _reject(response, "api_incomplete" if response.request_sent else "api_unavailable")
        finally:
            response.latency_ms = (loop.time() - started) * 1000
            self._calls.discard(task)
