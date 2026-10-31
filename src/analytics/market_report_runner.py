"""One durable logical report analysis from frozen public facts and research.

The caller owns ReportStore.slot_guard from collection through delivery. An
interrupted claim is never reopened, including after unknown provider charges.
"""
import asyncio
from datetime import datetime
import hashlib
import json
import re
import uuid
from loguru import logger

from .report_delivery import durable_thread, encode_public_evidence, validate_report_body
from ..utils.llm_roles import LLMRole, get_role_profile

CAPTURE_TIMEOUT_SECONDS = 30.0


class InvalidMarketReport(ValueError):
    """Source/prepared content cannot safely enter the delivery pipeline."""


def _validate_body(body, *, source=False):
    try:
        validate_report_body(body, source=source)
    except ValueError:
        raise InvalidMarketReport("invalid report body") from None


def _prepare_fallback(source_body, factual_fallback):
    """Only an unusable factual report prevents delivery."""
    try:
        _validate_body(source_body, source=True)
        fallback = factual_fallback(source_body)
        _validate_body(fallback)
        return fallback
    except Exception:
        raise InvalidMarketReport("report input validation failed") from None


def _research_fallback(source_body, fallback, public_evidence, factual_fallback):
    """Optional research formatting cannot invalidate the verified base facts."""
    if public_evidence is not None:
        try:
            prepared = factual_fallback(source_body, public_evidence=public_evidence)
            _validate_body(prepared)
            return prepared
        except Exception:
            pass
    return fallback


def _prepare_prompt(source_body, *, kind, day, build_analysis_prompt, public_evidence=None):
    """Optional analysis preparation must not suppress a safe factual report."""
    try:
        evidence = {"public_evidence": public_evidence} if public_evidence is not None else {}
        prompt, system = build_analysis_prompt(source_body, kind=kind, day=day, **evidence)
        if not isinstance(prompt, str) or not prompt.strip() or not isinstance(system, str) or not system.strip():
            raise ValueError("invalid prompt")
        _, profile = get_role_profile(LLMRole.MARKET_BRIEF)
        if len(prompt.encode("utf-8")) + len(system.encode("utf-8")) > profile.max_input_bytes:
            raise ValueError("prompt too large")
        return prompt, system
    except Exception:
        return None


async def _drain_capture(task):
    """Observe cleanup without sending a second cancellation to the reader."""
    interrupted = False
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            interrupted = True
    if not task.cancelled():
        task.exception()
    return interrupted


async def _capture_once(factory, now):
    """One bounded capture; timeout cleanup still owns the enclosing slot."""
    task = asyncio.create_task(factory(now=now))
    try:
        # wait does not turn cancellation of the child into caller cancellation.
        await asyncio.wait({task}, timeout=CAPTURE_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        task.cancel()
        await _drain_capture(task)
        raise
    if not task.done():
        task.cancel()
        if await _drain_capture(task):
            raise asyncio.CancelledError
        raise TimeoutError("public evidence capture deadline")
    if task.cancelled():
        raise RuntimeError("public evidence capture cancelled internally")
    return task.result()


async def _prepare_research(bundle, factory, now):
    """Optional imports, capture and validation all stay off the factual path."""
    try:
        from .market_research_sources import normalize_public_research, empty_public_research
    except Exception as error:
        logger.warning("공개 조사 준비 실패 import: {}", type(error).__name__)
        return None, "research_capture_failed"
    stage = "capture" if factory is not None else "normalize"
    try:
        if factory is not None:
            bundle = await _capture_once(factory, now)
        stage = "normalize"
        prepared = normalize_public_research(bundle)
        captured = datetime.fromisoformat(prepared["captured_at"])
        if now is not None and (captured.tzinfo is None or captured > now):
            raise ValueError("future research cutoff")
        encoded, _ = encode_public_evidence(prepared)
        return json.loads(encoded), None
    except Exception as error:
        logger.warning("공개 조사 준비 실패 {}: {}", stage, type(error).__name__)
        try:
            prepared = normalize_public_research(empty_public_research(now, "unavailable"))
            encoded, _ = encode_public_evidence(prepared)
            return json.loads(encoded), "research_capture_failed"
        except Exception as fallback_error:
            logger.warning("공개 조사 준비 실패 fallback: {}", type(fallback_error).__name__)
            return None, "research_capture_failed"


def _failure_status(response, *, provider_exception=False, invocation_started=False):
    sent = getattr(response, "request_sent", None)
    uncertain = sent is True and getattr(response, "usage_known", False) is not True
    if provider_exception and sent is None and invocation_started:
        uncertain = True
    return "unknown" if uncertain else "failed"


def _safe_metadata(response, *, reason=None):
    """Allowlisted metadata only: no raw provider response, prompt or error text."""
    def value(row, key):
        return row.get(key) if isinstance(row, dict) else getattr(row, key, None)

    def extract(row):
        clean = {}
        for key in ("requested_model", "resolved_model", "budget_reservation_token",
                    "finish_reason", "budget_block_reason", "billing_status",
                    "task", "role", "provider", "transport", "billing_channel"):
            item = value(row, key)
            if isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", item):
                clean[key] = item
        for key in ("usage_known", "request_sent", "success", "refusal_detected"):
            item = value(row, key)
            if type(item) is bool:
                clean[key] = item
        if clean.get("usage_known") is True:
            for key in ("input_tokens", "output_tokens"):
                item = value(row, key)
                if type(item) is int and 0 <= item <= 1_000_000_000:
                    clean[key] = item
        return clean

    metadata = extract(response)
    trail = value(response, "attempt_trail")
    if isinstance(trail, list):
        metadata["attempt_trail"] = [extract(row) for row in trail[:8] if isinstance(row, dict)]
    if reason is not None:
        metadata["reason"] = reason
    return metadata


async def _complete_once(manager, prompt, system, role=LLMRole.MARKET_BRIEF):
    """Propagate cancellation once, then drain provider cleanup and settlement."""
    task = asyncio.create_task(manager.complete_role(prompt, role=role, system=system))
    try:
        await asyncio.wait({task})
    except asyncio.CancelledError as cancelled:
        task.cancel()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        try:
            response = task.result()
        except BaseException as error:
            response = getattr(error, "_llm_response", None)
        cancelled._llm_response = response
        raise
    try:
        return task.result()
    except asyncio.CancelledError as cancelled:
        # The caller was not cancelled. Preserve any completed settlement while
        # allowing the ordinary provider-failure path to send the factual report.
        failure = RuntimeError("market brief cancelled internally")
        failure._llm_response = getattr(cancelled, "_llm_response", None)
        raise failure from None


async def prepare_market_report(store, *, kind, day, source_snapshot_id=None,
                                source_body=None, manager=None, manager_factory=None,
                                public_evidence=None, public_evidence_factory=None, now=None):
    """Resolve under the exclusive slot guard; reuse an exact frozen result."""
    from .market_report_analysis import (
        PROMPT_VERSION, build_analysis_prompt, compose_analysis, factual_fallback,
    )

    row = await durable_thread(store.analysis_receipt, "market", kind, day)
    if row is not None and row["status"] != "attempted":
        if row["metadata"].get("reason") == "invalid_source":
            raise InvalidMarketReport("stored report input was invalid")
        _validate_body(row["final_body"])
        return row
    if row is not None:
        # Holding the slot lock proves the previous writer no longer owns it.
        # Never turn an orphaned logical attempt into a new paid request.
        try:
            fallback = _prepare_fallback(row["source_body"], factual_fallback)
        except InvalidMarketReport:
            # Diagnostic body is never sent: this flag makes every subsequent
            # caller take the explicit failed-validation delivery path too.
            await durable_thread(store.complete_analysis, "market", kind, day,
                                 owner=row["owner"], status="abandoned",
                                 final_body="분석 미확보 — 저장된 보고 입력 검증 실패.",
                                 metadata={"reason": "invalid_source"})
            raise
        fallback = _research_fallback(row["source_body"], fallback, row.get("public_evidence"), factual_fallback)
        await durable_thread(store.complete_analysis, "market", kind, day,
                             owner=row["owner"], status="abandoned", final_body=fallback,
                             metadata={"reason": "interrupted"})
        return await durable_thread(store.analysis_receipt, "market", kind, day)

    fallback = _prepare_fallback(source_body, factual_fallback)
    if not isinstance(source_snapshot_id, str) or not source_snapshot_id or len(source_snapshot_id) > 128:
        raise InvalidMarketReport("invalid market snapshot identity")
    research_requested = public_evidence is not None or public_evidence_factory is not None
    research_failure = None
    if research_requested:
        public_evidence, research_failure = await _prepare_research(public_evidence, public_evidence_factory, now)
        fallback = _research_fallback(source_body, fallback, public_evidence, factual_fallback)
    if public_evidence is not None:
        _, evidence_hash = encode_public_evidence(public_evidence)
        source_snapshot_id = hashlib.sha256(json.dumps([
            source_snapshot_id, hashlib.sha256(source_body.encode()).hexdigest(), evidence_hash,
        ], separators=(",", ":")).encode()).hexdigest()
    prepared_prompt = _prepare_prompt(source_body, kind=kind, day=day,
                                       build_analysis_prompt=build_analysis_prompt, public_evidence=public_evidence)
    owner = uuid.uuid4().hex
    if not await durable_thread(store.claim_analysis, "market", kind, day,
                                source_snapshot_id, source_body, PROMPT_VERSION, owner=owner,
                                public_evidence=public_evidence):
        # A delivery claim or unsupported competing caller consumed the slot.
        # Do not guess that an existing attempted analysis is abandoned here.
        return None
    has_events = public_evidence is not None and any(
        row.get("kind") in {"news", "disclosure", "calendar"} for row in public_evidence.get("records", []))
    if research_failure or prepared_prompt is None or (research_requested and not has_events):
        reason = research_failure or ("research_evidence_missing" if research_requested and not has_events else "prompt_unavailable")
        await durable_thread(store.complete_analysis, "market", kind, day, owner=owner,
                             status="failed", final_body=fallback,
                             metadata={"reason": reason})
        return await durable_thread(store.analysis_receipt, "market", kind, day)
    prompt, system = prepared_prompt
    response, result, status, reason = None, None, "failed", "manager_unavailable"
    final_body = fallback
    invocation_started = False
    try:
        if manager is None and manager_factory is not None:
            manager = manager_factory()
        if manager is not None:
            invocation_started = True
            response = await _complete_once(manager, prompt, system)
            status = _failure_status(response)
            if getattr(response, "refusal_detected", False):
                reason = "refused"
            elif getattr(response, "success", False) is not True:
                reason = "provider_failed"
            elif getattr(response, "finish_reason", None) != "stop":
                reason = "incomplete"
            else:
                try:
                    evidence = {"public_evidence": public_evidence} if public_evidence is not None else {}
                    final_body, result = compose_analysis(source_body, response.content, kind=kind, day=day, **evidence)
                    _validate_body(final_body)
                except Exception:
                    final_body, result, reason = fallback, None, "invalid_output"
                else:
                    status, reason = "ready", "validated"
    except asyncio.CancelledError as cancelled:
        # "unknown" here means analysis completion was interrupted, not that
        # billing is necessarily unknown. Preserve recovered usage in metadata.
        await durable_thread(store.complete_analysis, "market", kind, day,
                             owner=owner, status="unknown", final_body=fallback,
                             metadata=_safe_metadata(getattr(cancelled, "_llm_response", None), reason="cancelled"))
        raise
    except Exception as error:
        response = getattr(error, "_llm_response", response)
        status = _failure_status(response, provider_exception=True, invocation_started=invocation_started)
        final_body, result, reason = fallback, None, "provider_exception" if invocation_started else "manager_unavailable"
    await durable_thread(store.complete_analysis, "market", kind, day, owner=owner,
                         status=status, final_body=final_body, result=result,
                         metadata=_safe_metadata(response, reason=reason))
    return await durable_thread(store.analysis_receipt, "market", kind, day)
