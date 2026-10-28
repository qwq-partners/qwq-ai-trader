"""판단 전 비교 입력 봉투 `comparison-input-envelope-v1` — 생산자(cross_validator)·소비자(작업자) 공용.

`llm_second_check` 가 공통 입력·역할 조건을 확정한 직후, 공급자 await 전에 한 번 만든다.
봉투는 같은 decision_id 의 판단 기록·신호 메타·비교 pair 와 조인된다.

출처 범위: `attestation.nonce_sha256` 은 이 프로세스 시작 때 만든 난수로 봉투 본문 해시를 HMAC-SHA256 한
값이다. 같은 프로세스의 작업자만 검증할 수 있다. 해시·HMAC 은 무결성과 "같은 프로세스가 만들었다" 까지만
보여 주며 생산자 코드의 정당성이나 원천 자료의 진위를 인증하지 않는다. 파일로 전달된 봉투는 다른 프로세스의
난수로 검증되지 않으므로 `caller_supplied_not_attested` 다.
"""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
from typing import Any

from .llm_decision_record import new_id

SCHEMA_VERSION = "comparison-input-envelope-v1"
RUN_ID = new_id("run")
_NONCE = secrets.token_bytes(32)          # 프로세스 밖으로 내보내지 않는다
_TRANSPORT_FIELDS = ("handed_off_at", "received_at", "attestation", "envelope_sha256")
_SOURCES = ("src/core/cross_validator.py", "src/core/entry_llm_context.py",
            "src/analytics/model_comparison_input.py")
_ROLE_KEYS = ("model", "max_output_tokens", "reasoning_effort", "temperature", "seed", "timeout_seconds")


def _canon(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _aware(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None and stamp.utcoffset() is not None else None


@lru_cache(maxsize=1)
def source_sha256() -> str | None:
    """생산 경로 소스 3개의 sha256. 첫 호출에서 한 번 읽어 캐시한다(런처가 배선 때 미리 데운다)."""
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    try:
        for name in _SOURCES:
            digest.update(name.encode() + b"\0" + (root / name).read_bytes() + b"\0")
    except OSError:
        return None
    return digest.hexdigest()


def producer_identity(*, experiment_id=None, config_sha256=None) -> dict:
    try:
        from ..utils.entry_risk import applied_sha
        code_version = applied_sha()
    except Exception:
        code_version = None
    return {"run_id": RUN_ID, "pid": os.getpid(), "code_version": code_version,
            "source_sha256": source_sha256(), "config_sha256": config_sha256, "experiment_id": experiment_id}


def operating_role_controls(llm_manager, *, fallback_prompt_suffix: str, fallback_timeout: float) -> dict:
    """운영 적대검증·단일 폴백이 실제로 쓰는 조건의 복사본. 원천에서 못 읽은 값은 None 이다.

    Bull/Bear 는 adversarial_validator 상수 + LLMManager 의 light 모델, 폴백은 STRATEGY_ANALYSIS 경로
    (primary→fallback)·max_tokens=100·system 없음. 온도를 넘기지 않는 호출은 원시 클라이언트 기본 0.3 이다
    (gpt-5 계열은 요청 본문에서 온도를 생략한다). 운영 Bull/Bear 시한은 패널 전체 12초, 폴백 시한은 체인 전체다
    (`fallback_chain_timeout_seconds`; 비교 러너가 같은 의미로 체인 전체에 적용한다).
    """
    from ..core import adversarial_validator as av
    from ..utils.llm import LLMManager, LLMProvider, LLMTask

    def model(provider, weight):
        getter = getattr(llm_manager, "_get_model", None)
        try:
            value = getter(provider, weight) if callable(getter) else None
        except Exception:
            return None
        return value if isinstance(value, str) and value else None

    def role(provider, weight, max_tokens, effort, temperature, seed, timeout):
        return dict(zip(_ROLE_KEYS, (model(provider, weight), max_tokens, effort, temperature, seed, float(timeout))))

    route = LLMManager.TASK_CONFIG[LLMTask.STRATEGY_ANALYSIS]
    return {
        "bull": role(LLMProvider.OPENAI, "light", av.MAX_TOKENS, "minimal", 0.3, av.VERDICT_SEED, av.LLM_TIMEOUT),
        "bear": role(LLMProvider.GEMINI, "light", av.MAX_TOKENS, None, 0.0, None, av.LLM_TIMEOUT),
        "fallback_chain": [role(p, w, 100, LLMManager._task_controls(model(p, w), w, {}).get("reasoning_effort"),
                                0.3, None, fallback_timeout)
                           for p, w in (route["primary"], route["fallback"])],
        # 운영 SINGLE_FALLBACK_TIMEOUT 은 primary→fallback 체인 전체 시한이다(시도별 timeout_seconds 와 별개).
        "fallback_chain_timeout_seconds": float(fallback_timeout),
        "fallback_prompt_suffix": fallback_prompt_suffix,
        "fallback_system": "",
        "empty_retries": 1,
        "strict_on_disagreement": bool(av.STRICT_ON_DISAGREEMENT),
    }


def body_sha256(envelope: dict) -> str:
    return _sha(_canon({k: v for k, v in envelope.items() if k not in _TRANSPORT_FIELDS}))


def _mac(nonce: bytes, message: str) -> str:
    return hmac.new(nonce, message.encode(), hashlib.sha256).hexdigest()


def build_envelope(*, decision_id, signal_id, symbol, strategy, score, market_regime, common_context,
                   role_controls, neutral_sizing, producer, input_available_at, handed_off_at,
                   nonce: bytes | None = None) -> dict:
    """JSON 왕복 복사로 호출자 객체와 분리한다(JSON 불가·NaN 은 ValueError)."""
    body = json.loads(_canon({
        "schema_version": SCHEMA_VERSION, "decision_id": decision_id, "signal_id": signal_id,
        "symbol": symbol, "strategy": strategy, "score": score, "market_regime": market_regime,
        "common_context": common_context,
        "common_context_sha256": _sha(common_context) if isinstance(common_context, str) else None,
        "role_controls": role_controls, "neutral_sizing": neutral_sizing, "producer": producer,
        "input_available_at": input_available_at,
    }))
    digest = body_sha256(body)
    if nonce is None:
        nonce = _NONCE                     # 생략만 기본값. 명시한 빈 바이트/다른 타입은 거부한다(4회차 P2)
    elif not isinstance(nonce, (bytes, bytearray)) or len(nonce) == 0:
        raise ValueError("nonce must be non-empty bytes or None")
    return {**body, "handed_off_at": handed_off_at, "received_at": None, "envelope_sha256": digest,
            "attestation": {"kind": "in_process_nonce", "nonce_sha256": _mac(bytes(nonce), digest)}}


def verify_envelope(envelope) -> str | None:
    """None 이면 통과. 실패 사유 코드: invalid_schema, invalid_field:<키>, context_hash_mismatch,
    envelope_hash_mismatch, unattested, clock_reversal, sizing_identity_mismatch."""
    if not isinstance(envelope, dict) or envelope.get("schema_version") != SCHEMA_VERSION:
        return "invalid_schema"
    for key in ("decision_id", "signal_id", "symbol", "common_context"):
        if not isinstance(envelope.get(key), str) or not envelope[key].strip():
            return f"invalid_field:{key}"
    for key in ("input_available_at", "handed_off_at"):
        if _aware(envelope.get(key)) is None:
            return f"invalid_field:{key}"
    if envelope.get("common_context_sha256") != _sha(envelope["common_context"]):
        return "context_hash_mismatch"
    try:
        digest = body_sha256(envelope)
    except (TypeError, ValueError):
        return "invalid_schema"
    if envelope.get("envelope_sha256") != digest:
        return "envelope_hash_mismatch"
    attestation = envelope.get("attestation")
    if (not isinstance(attestation, dict) or attestation.get("kind") != "in_process_nonce"
            or not isinstance(attestation.get("nonce_sha256"), str)
            or not hmac.compare_digest(attestation["nonce_sha256"], _mac(_NONCE, digest))):
        return "unattested"
    available, handed = _aware(envelope["input_available_at"]), _aware(envelope["handed_off_at"])
    received = _aware(envelope.get("received_at")) if envelope.get("received_at") is not None else None
    if handed < available or (received is not None and received < handed):
        return "clock_reversal"
    sizing = envelope.get("neutral_sizing")
    if sizing is not None:
        signal = sizing.get("signal") if isinstance(sizing, dict) else None
        captured = _aware(sizing.get("captured_at")) if isinstance(sizing, dict) else None
        if (not isinstance(signal, dict) or signal.get("signal_id") != envelope["signal_id"]
                or signal.get("symbol") != envelope["symbol"] or captured is None or captured > available):
            return "sizing_identity_mismatch"
    return None


def _pair_message(*, decision_id, signal_id, symbol, common_context, input_available_at, enqueued_at,
                  neutral_sizing) -> str:
    return _canon(["pair-attestation-v1", decision_id, signal_id, symbol,
                   _sha(common_context) if isinstance(common_context, str) else None,
                   input_available_at, enqueued_at, _sha(_canon(neutral_sizing))])


def attest_pair(**fields) -> str:
    """작업자가 검증한 봉투로 PairSpec 을 만들 때만 부른다. 같은 프로세스 난수 범위의 증명이다."""
    return _mac(_NONCE, _pair_message(**fields))


def verify_pair_attestation(spec) -> bool:
    token = getattr(spec, "attestation", None)
    try:
        expected = attest_pair(decision_id=spec.decision_id, signal_id=spec.signal_id, symbol=spec.symbol,
                               common_context=spec.common_context, input_available_at=spec.input_available_at,
                               enqueued_at=spec.enqueued_at, neutral_sizing=spec.neutral_sizing)
    except (TypeError, ValueError):
        return False
    return isinstance(token, str) and hmac.compare_digest(token, expected)
