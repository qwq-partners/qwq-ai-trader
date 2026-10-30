"""One durable logical report analysis, using only the existing public body.

The caller owns ReportStore.slot_guard from collection through delivery. An
interrupted claim is never reopened, including after unknown provider charges.
"""
import asyncio
import re
import uuid

from .report_delivery import durable_thread
from ..utils.llm_roles import LLMRole, get_role_profile


class InvalidMarketReport(ValueError):
    """Source/prepared content cannot safely enter the delivery pipeline."""


def _validate_body(body):
    if (not isinstance(body, str) or not body.strip() or len(body) > 1800
            or len(body.splitlines()) > 35):
        raise InvalidMarketReport("invalid report body")


def _prepare_fallback(source_body, factual_fallback):
    """Only an unusable factual report prevents delivery."""
    try:
        _validate_body(source_body)
        fallback = factual_fallback(source_body)
        _validate_body(fallback)
        return fallback
    except Exception:
        raise InvalidMarketReport("report input validation failed") from None


def _prepare_prompt(source_body, *, kind, day, build_analysis_prompt):
    """Optional analysis preparation must not suppress a safe factual report."""
    try:
        prompt, system = build_analysis_prompt(source_body, kind=kind, day=day)
        if not isinstance(prompt, str) or not prompt.strip() or not isinstance(system, str) or not system.strip():
            raise ValueError("invalid prompt")
        _, profile = get_role_profile(LLMRole.MARKET_BRIEF)
        if len(prompt.encode("utf-8")) + len(system.encode("utf-8")) > profile.max_input_bytes:
            raise ValueError("prompt too large")
        return prompt, system
    except Exception:
        return None


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


async def _complete_once(manager, prompt, system):
    """Propagate cancellation once, then drain provider cleanup and settlement."""
    task = asyncio.create_task(manager.complete_role(prompt, role=LLMRole.MARKET_BRIEF, system=system))
    try:
        return await asyncio.shield(task)
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


async def prepare_market_report(store, *, kind, day, source_snapshot_id=None,
                                source_body=None, manager=None, manager_factory=None):
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
        await durable_thread(store.complete_analysis, "market", kind, day,
                             owner=row["owner"], status="abandoned", final_body=fallback,
                             metadata={"reason": "interrupted"})
        return await durable_thread(store.analysis_receipt, "market", kind, day)

    fallback = _prepare_fallback(source_body, factual_fallback)
    prepared_prompt = _prepare_prompt(source_body, kind=kind, day=day,
                                       build_analysis_prompt=build_analysis_prompt)
    owner = uuid.uuid4().hex
    if not await durable_thread(store.claim_analysis, "market", kind, day,
                                source_snapshot_id, source_body, PROMPT_VERSION, owner=owner):
        # A delivery claim or unsupported competing caller consumed the slot.
        # Do not guess that an existing attempted analysis is abandoned here.
        return None
    if prepared_prompt is None:
        await durable_thread(store.complete_analysis, "market", kind, day, owner=owner,
                             status="failed", final_body=fallback,
                             metadata={"reason": "prompt_unavailable"})
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
                    final_body, result = compose_analysis(source_body, response.content, kind=kind, day=day)
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
