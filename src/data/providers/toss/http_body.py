"""압축을 받지 않는 유한 스트림과 엄격한 JSON 파싱 경계."""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass


class BodyError(ValueError):
    """원 응답/예외는 고정 사유로만 전달한다."""

    def __init__(self, code):
        self.code = code if code in {"body_bytes", "body_depth", "body_nodes",
            "body_string", "body_encoding", "invalid_json", "non_json", "timeout", "network_error"} else "invalid_json"
        super().__init__(self.code)


@dataclass(frozen=True)
class BodyLimits:
    max_bytes: int
    max_depth: int
    max_nodes: int
    max_string: int
    parse_timeout_seconds: float

    def __post_init__(self):
        for value, ceiling in ((self.max_bytes, 16 * 1024 * 1024), (self.max_depth, 64),
                               (self.max_nodes, 1_000_000), (self.max_string, 1024 * 1024)):
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError("invalid_body_limits")
        value = self.parse_timeout_seconds
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 30:
            raise ValueError("invalid_body_limits")


def _check(deadline, clock):
    remaining = deadline - clock()
    if not math.isfinite(remaining) or remaining <= 0:
        raise BodyError("timeout")
    return remaining


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BodyError("invalid_json")
        result[key] = value
    return result


def _constant(_):
    raise BodyError("invalid_json")


def _parse(raw, limits, deadline, clock):
    deadline = min(deadline, clock() + limits.parse_timeout_seconds)
    _check(deadline, clock)
    # JSON 디코더 진입 전에 문자열 밖 중첩을 세어 재귀/스택 폭증을 막는다.
    depth, quoted, escaped = 0, False, False
    for index, char in enumerate(raw):
        if index % 4096 == 0:
            _check(deadline, clock)
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
        elif char == 34:
            quoted = True
        elif char in (91, 123):
            depth += 1
            if depth > limits.max_depth:
                raise BodyError("body_depth")
        elif char in (93, 125):
            depth -= 1
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    _check(deadline, clock)
    stack, nodes = [value], 0
    while stack:
        _check(deadline, clock)
        item = stack.pop()
        nodes += 1
        if nodes > limits.max_nodes:
            raise BodyError("body_nodes")
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str):
            if len(item) > limits.max_string:
                raise BodyError("body_string")
            item.encode("utf-8")  # 고립 surrogate를 거부한다.
        elif isinstance(item, float) and not math.isfinite(item):
            raise BodyError("invalid_json")
    _check(deadline, clock)
    return value


async def read_json_bounded(response, *, limits, deadline, clock=time.monotonic):
    """identity만 허용하므로 wire와 decoded 바이트 한도가 같다.

    호출자는 반드시 auto_decompress=False로 응답을 연다. 동기 디코더는
    강제 선점할 수 없어 입력 크기/깊이를 먼저 제한하고 시간 초과 결과를 폐기한다.
    """
    try:
        remaining = _check(deadline, clock)
        headers = {str(key).lower(): value for key, value in response.headers.items()}
        if headers.get("content-encoding", "identity").strip().lower() != "identity":
            raise BodyError("body_encoding")
        length = headers.get("content-length")
        if length is not None:
            if not isinstance(length, str) or not length.isascii() or not length.isdigit():
                raise BodyError("invalid_json")
            if len(length) > 16 or int(length) > limits.max_bytes:
                raise BodyError("body_bytes")
        body = bytearray()
        async with asyncio.timeout(remaining):
            async for chunk in response.content.iter_chunked(min(65536, limits.max_bytes + 1)):
                _check(deadline, clock)
                if not isinstance(chunk, bytes):
                    raise BodyError("invalid_json")
                if len(body) + len(chunk) > limits.max_bytes:
                    raise BodyError("body_bytes")
                body.extend(chunk)
        _check(deadline, clock)
        return _parse(body, limits, deadline, clock)
    except asyncio.CancelledError:
        raise
    except BodyError as exc:
        raise BodyError(exc.code) from None
    except TimeoutError:
        raise BodyError("timeout") from None
    except json.JSONDecodeError:
        raise BodyError("non_json") from None
    except (ValueError, UnicodeError, RecursionError):
        raise BodyError("invalid_json") from None
    except Exception:
        raise BodyError("network_error") from None
