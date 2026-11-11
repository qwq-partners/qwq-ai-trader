"""Pure validation of caller-supplied, offline entry-review evidence.

This is not an execution adapter, live-policy attestation, or economic analysis.
"""
import copy
from decimal import Decimal, localcontext
import math
import re

INPUT_VERSION = "model-comparison-offline-contract-v1"
REPORT_VERSION = "model-comparison-offline-report-v1"
INPUT_VERSION_V2 = "model-comparison-offline-contract-v2"
REPORT_VERSION_V2 = "model-comparison-offline-report-v2"
IDENTITY_POLICY_VERSION = "documented-model-identity-v1"
# Qualified 2026-11-01: https://developers.openai.com/api/docs/models/gpt-5-mini
# gpt-5.6-terra(2026-11-11 https://developers.openai.com/api/docs/models/gpt-5.6-terra): 문서화된 dated snapshot 없음
# (default snapshot = gpt-5.6-terra) → 신원은 requested==resolved 의 exact 매치로만 인정한다. 실호출이 dated id 를
# 돌려주면 공식 표에서 확인한 뒤 여기 추가한다(그 전까지 그 응답은 mismatch → hold).
DOCUMENTED_SNAPSHOTS = frozenset({("openai_chat", "gpt-5-mini", "gpt-5-mini-2025-08-07")})
PROFILE = "entry-independent-review-v1"
POLICY = {"requires_independent_review": True, "missing_review_policy": "hold",
          "single_model_fallback": False, "strict_on_disagreement": False,
          "panel_timeout_seconds": 12.0, "bear_internal_timeout_seconds": 11.5,
          "bear_transport_order": ["claude_cli", "anthropic_messages"]}
# 역사적(CLI 1차, ~2026-11-09)과 현행(L01 Task 2: API 1차) 두 순서의 증거를 모두 해석한다. 두 번째 시도의 인가 규칙은
# 첫 전송의 허용 실패 집합(FALLBACK / API_FALLBACK)으로 갈린다.
BEAR_TRANSPORT_ORDERS = (["claude_cli", "anthropic_messages"], ["anthropic_messages", "claude_cli"])
# Bull 기준도 두 시점을 해석한다: 역사적(gpt-5-mini/minimal, ~2026-11-11)과 현행(L02: gpt-5.6-terra/none —
# gpt-5-mini 는 2026-12-11 API 제거, terra 는 minimal 미지원). bull_current.requested_model 로 어느 프로필인지 고른다.
BULL_LEGACY = {"requested_model": "gpt-5-mini", "reasoning_effort": "minimal", "max_output_tokens": 400,
               "temperature": .3, "seed": 20260803, "empty_retries": 1}
BULL = {"requested_model": "gpt-5.6-terra", "reasoning_effort": "none", "max_output_tokens": 400,
        "temperature": .3, "seed": 20260803, "empty_retries": 1}
BULL_PROFILES = (BULL_LEGACY, BULL)
BEAR = {"requested_model": "claude-opus-5-5", "reasoning_effort": "low", "max_output_tokens": 400,
        "temperature": None, "seed": None, "empty_retries": 0}
ATTEMPT_FIELDS = {"attempt_id", "transport", "billing_channel", "requested_model", "resolved_model",
                  "requested_reasoning_effort", "effective_reasoning_effort", "status", "transport_error_kind",
                  "request_sent", "usage_known", "input_tokens", "output_tokens", "started_ms", "finished_ms",
                  "api_estimated_cost_usd", "subscription_estimated_cost_usd"}
STATUSES = {"success", "empty", "error", "timeout", "cancelled", "blocked", "refusal", "identity_mismatch", "policy_violation"}
FALLBACK = {"cli_unavailable", "cli_timeout", "cli_rate_limited", "cli_auth_unavailable", "cli_incomplete"}
API_FALLBACK = {"api_timeout", "api_unavailable", "api_auth_unavailable", "api_http_error", "transport_cooldown"}
# API 1차에서 구독 CLI 대체는 Bear 내부 시한(11.5초)까지 4.0초 이상 남아 있을 때만 보낸다(LLMManager._complete_claude 와 동일).
CLI_FALLBACK_FLOOR_MS = 4000


def _error(path, code="invalid"):
    raise ValueError(f"{path}: {code}")


def _fields(value, keys, path):
    if not isinstance(value, dict) or set(value) != set(keys):
        _error(path, "invalid_fields")


def _string(value, path, nullable=False):
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value.strip():
        _error(path)


def _identifier(value, path, nullable=False):
    if value is None and nullable:
        return
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
        _error(path)


def _safe_observation(value):
    if value is None or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
        return value
    return "other"


def _number(value, path, nullable=False):
    if value is None and nullable:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (isinstance(value, float) and not math.isfinite(value)) or value < 0:
        _error(path)


def _amount(value, path):
    if value is None:
        return
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value) or len(value) > 128:
        _error(path)


def _parse_verdict(text, positive_token, negative_token):
    upper = text.upper()
    pos, neg = upper.find(positive_token), upper.find(negative_token)
    if pos < 0 and neg < 0:
        return None
    return neg < 0 or (pos >= 0 and pos < neg)


def _same(actual, expected):
    # Python considers True == 1; the schema does not.
    if isinstance(expected, bool):
        return type(actual) is bool and actual == expected
    if isinstance(expected, int):
        return type(actual) is int and actual == expected
    if isinstance(expected, float):
        return type(actual) in (int, float) and (type(actual) is int or math.isfinite(actual)) and actual == expected
    return type(actual) is type(expected) and actual == expected


def _identity_match(attempt, v2):
    resolved = attempt["resolved_model"]
    if resolved is None:
        return "unknown"
    if resolved == attempt["requested_model"]:
        return "exact"
    if v2 and (attempt["transport"], attempt["requested_model"], resolved) in DOCUMENTED_SNAPSHOTS:
        return "documented_snapshot"
    return "mismatch"


def evaluate_contract(payload):
    """Validate offline evidence without I/O, mutation, or raw-content output."""
    _fields(payload, {"schema_version", "profile", "evidence_kind", "comparison_id", "common_input_sha256", "policy", "calls"}, "payload")
    if payload["schema_version"] not in (INPUT_VERSION, INPUT_VERSION_V2): _error("schema_version", "unsupported")
    v2 = payload["schema_version"] == INPUT_VERSION_V2
    if payload["profile"] != PROFILE: _error("profile", "unsupported")
    if payload["evidence_kind"] not in ("synthetic", "caller_supplied_recording"): _error("evidence_kind")
    _identifier(payload["comparison_id"], "comparison_id")
    if not isinstance(payload["common_input_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", payload["common_input_sha256"]): _error("common_input_sha256")
    _fields(payload["policy"], POLICY, "policy")
    for key, expected in POLICY.items():
        if key == "bear_transport_order":
            if payload["policy"][key] not in BEAR_TRANSPORT_ORDERS: _error("policy." + key, "unsupported")
        elif not _same(payload["policy"][key], expected): _error("policy." + key, "unsupported")
    order = payload["policy"]["bear_transport_order"]
    cli_primary = order[0] == "claude_cli"
    calls = payload["calls"]
    _fields(calls, {"bull_current", "bull_candidate", "bear"}, "calls")
    ids, violations, unknowns, observations = set(), [], [], {}
    costs = {"api_estimated_cost_usd_known_subtotal": Decimal(0), "subscription_estimated_cost_usd_known_subtotal": Decimal(0),
             "api_unknown_estimate_count": 0, "subscription_unknown_estimate_count": 0,
             "api_possibly_sent_unknown_estimate_count": 0, "missing_trace_count": 0,
             "api_unknown_usage_count": 0, "subscription_unknown_usage_count": 0, "unknown_transmission_count": 0}
    results = {}
    bull_ref = BULL
    for role in ("bull_current", "bull_candidate", "bear"):
        path = "calls." + role
        call = calls[role]
        _fields(call, {"controls", "whole_call_elapsed_ms", "outcome", "content", "attempts"}, path)
        controls = call["controls"]
        _fields(controls, BULL, path + ".controls")
        if role == "bull_current":
            bull_ref = next((p for p in BULL_PROFILES if p["requested_model"] == controls["requested_model"]), BULL)
        reference = BEAR if role == "bear" else bull_ref
        for key, expected in reference.items():
            if role == "bull_candidate" and key in {"requested_model", "reasoning_effort", "max_output_tokens"}:
                continue
            if not _same(controls[key], expected): _error(path + ".controls." + key, "unsupported")
        if role == "bull_candidate":
            _identifier(controls["requested_model"], path + ".controls.requested_model")
            if not controls["requested_model"].startswith("gpt-") or len(controls["requested_model"]) <= 4: _error(path + ".controls.requested_model")
            _identifier(controls["reasoning_effort"], path + ".controls.reasoning_effort")
            cap = controls["max_output_tokens"]
            if type(cap) is not int or not 1 <= cap <= 1000000: _error(path + ".controls.max_output_tokens")
            if (sum(controls[k] != bull_ref[k] for k in ("requested_model", "reasoning_effort", "max_output_tokens")) != 1
                    or (v2 and ("openai_chat", bull_ref["requested_model"], controls["requested_model"]) in DOCUMENTED_SNAPSHOTS)):
                _error(path + ".controls", "one_comparison_variable_required")
        elapsed = call["whole_call_elapsed_ms"]
        _number(elapsed, path + ".whole_call_elapsed_ms", True)
        if call["outcome"] not in ("success", "error", "timeout", "cancelled", "blocked", "missing"): _error(path + ".outcome")
        if not isinstance(call["content"], str): _error(path + ".content")
        attempts = call["attempts"]
        if not isinstance(attempts, list) or len(attempts) > 2: _error(path + ".attempts")
        role_issues = []
        def issue(code, *, unknown=False):
            entry = {"path": path, "code": code}
            (unknowns if unknown else violations).append(entry)
            role_issues.append(code)
        if elapsed is None: issue("whole_call_timing_unknown", unknown=True)
        elif call["outcome"] == "success" and elapsed >= (11500 if role == "bear" else 12000):
            issue("whole_call_deadline_exceeded")
        if not attempts and call["outcome"] in {"success", "error", "timeout", "cancelled"}:
            issue("terminal_attempt_unknown", unknown=True)
            costs["missing_trace_count"] += 1
        last_finish = None
        for i, a in enumerate(attempts):
            ap = f"{path}.attempts[{i}]"
            _fields(a, ATTEMPT_FIELDS, ap)
            _identifier(a["attempt_id"], ap + ".attempt_id")
            if a["attempt_id"] in ids: _error(ap + ".attempt_id", "duplicate")
            ids.add(a["attempt_id"])
            transports = ("claude_cli", "anthropic_messages") if role == "bear" else ("openai_chat",)
            if a["transport"] not in transports: _error(ap + ".transport", "unsupported")
            channel = "subscription" if a["transport"] == "claude_cli" else "api"
            if a["billing_channel"] != channel: _error(ap + ".billing_channel")
            if a["requested_model"] != controls["requested_model"]: _error(ap + ".requested_model")
            if a["requested_reasoning_effort"] != controls["reasoning_effort"]: _error(ap + ".requested_reasoning_effort")
            for key in ("resolved_model", "effective_reasoning_effort"):
                _string(a[key], ap + "." + key, True)
            _string(a["transport_error_kind"], ap + ".transport_error_kind", True)
            if _identity_match(a, v2) == "mismatch":
                issue("attempt_identity_mismatch")
            if not isinstance(a["status"], str) or a["status"] not in STATUSES: _error(ap + ".status")
            if a["request_sent"] is not None and type(a["request_sent"]) is not bool: _error(ap + ".request_sent")
            if type(a["usage_known"]) is not bool: _error(ap + ".usage_known")
            for key in ("input_tokens", "output_tokens"):
                value = a[key]
                if a["usage_known"]:
                    if type(value) is not int or value < 0: _error(ap + "." + key)
                elif value is not None: _error(ap + "." + key)
            for key in ("started_ms", "finished_ms"): _number(a[key], ap + "." + key, True)
            start, finish = a["started_ms"], a["finished_ms"]
            if (start is None) != (finish is None): _error(ap + ".timing")
            if start is not None:
                if finish < start or (last_finish is not None and start < last_finish): _error(ap + ".timing", "invalid_order")
                last_finish = finish
                if elapsed is not None and finish > elapsed: issue("attempt_after_whole_call")
                if role != "bear" and start >= 12000:
                    issue("bull_attempt_started_after_deadline")
                if a["status"] == "success" and finish >= (11500 if role == "bear" else 12000): issue("attempt_after_deadline")
                if role == "bear" and cli_primary and a["transport"] == "claude_cli" and a["status"] == "success" and finish >= 5750: issue("cli_success_after_deadline")
                if (role == "bear" and a["transport"] == "anthropic_messages"
                        and (start >= 11500 or (a["status"] == "success" and finish >= 11500))):
                    issue("api_after_deadline")
            elif role == "bear" and (a["status"] == "success" or a["transport"] == "anthropic_messages"):
                issue("transport_timing_unknown", unknown=True)
            elif role != "bear" and elapsed is not None and elapsed >= 12000:
                issue("transport_timing_unknown", unknown=True)
            for key in ("api_estimated_cost_usd", "subscription_estimated_cost_usd"): _amount(a[key], ap + "." + key)
            other = "subscription_estimated_cost_usd" if channel == "api" else "api_estimated_cost_usd"
            if a[other] is not None: _error(ap + "." + other, "wrong_channel")
            amount = a[channel + "_estimated_cost_usd"]
            if a["status"] in {"success", "empty"} and a["transport_error_kind"] is not None:
                issue("attempt_status_error_kind_inconsistent")
            if (a["transport"] == "claude_cli" and a["status"] == "timeout"
                    and a["transport_error_kind"] not in (None, "cli_timeout")):
                issue("attempt_status_error_kind_inconsistent")
            not_sent_contradiction = False
            if a["request_sent"] is False:
                response_status = a["status"] in {"success", "empty", "refusal", "identity_mismatch"}
                positive_tokens = any((a[key] or 0) > 0 for key in ("input_tokens", "output_tokens"))
                positive_cost = amount is not None and Decimal(amount) > 0
                not_sent_contradiction = (
                    response_status or positive_tokens or positive_cost or a["resolved_model"] is not None
                )
                if not_sent_contradiction:
                    issue("not_sent_attempt_contradiction")
            if amount is not None:
                with localcontext() as context:
                    context.prec = 400
                    costs[channel + "_estimated_cost_usd_known_subtotal"] += Decimal(amount)
            else:
                costs[channel + "_unknown_estimate_count"] += 1
                if channel == "api" and (a["request_sent"] is not False or not_sent_contradiction):
                    costs["api_possibly_sent_unknown_estimate_count"] += 1
            if not a["usage_known"]: costs[channel + "_unknown_usage_count"] += 1
            if a["request_sent"] is None: costs["unknown_transmission_count"] += 1
        if role == "bear":
            if attempts and attempts[0]["transport"] != order[0]: issue("cli_primary_missing" if cli_primary else "api_primary_missing")
            if len(attempts) == 2:
                first, second = attempts
                if second["transport"] != order[1]: issue("bear_transport_order")
                if cli_primary:
                    authorized = first["status"] in {"error", "timeout"} and first["transport_error_kind"] in FALLBACK
                else:   # API 1차: 가용성 실패 또는 전송 전 차단(예산·cooldown, request_sent False 증명 필수)만 구독 CLI 로 이어진다
                    authorized = ((first["status"] in {"error", "timeout"} and first["transport_error_kind"] in API_FALLBACK)
                                  or (first["status"] == "blocked" and first["request_sent"] is False
                                      and first["transport_error_kind"] in (None, "transport_cooldown")))
                    if second["started_ms"] is None:
                        if "transport_timing_unknown" not in role_issues: issue("transport_timing_unknown", unknown=True)
                    elif second["started_ms"] > 11500 - CLI_FALLBACK_FLOOR_MS:
                        issue("cli_fallback_below_time_floor")     # 운영은 잔여 4초 미만이면 CLI 를 보내지 않는다
                if not authorized: issue("bear_fallback_not_authorized")
        elif len(attempts) == 2 and attempts[0]["status"] != "empty": issue("bull_retry_not_authorized")
        verdict = _parse_verdict(call["content"], "ACCEPT" if role == "bear" else "APPROVE", "REJECT") if call["outcome"] == "success" else None
        if call["outcome"] != "success" and attempts and attempts[-1]["status"] == "success":
            issue("terminal_attempt_inconsistent")
        if call["outcome"] == "success":
            if not attempts: pass
            elif attempts[-1]["status"] == "empty" and not call["content"].strip(): pass
            elif attempts[-1]["status"] != "success": issue("terminal_attempt_inconsistent")
            else:
                final = attempts[-1]
                if _identity_match(final, v2) == "unknown": issue("resolved_identity_unknown", unknown=True)
                elif _identity_match(final, v2) == "mismatch": issue("resolved_identity_mismatch")
                if final["request_sent"] is False: issue("successful_attempt_not_sent")
                elif final["request_sent"] is None: issue("successful_transmission_unknown", unknown=True)
        attempt_observations = []
        for a in attempts:
            observation = {key: a[key] for key in (
                "attempt_id", "transport", "status", "request_sent", "requested_model",
                "requested_reasoning_effort", "started_ms", "finished_ms", "usage_known")}
            for key in ("resolved_model", "effective_reasoning_effort"):
                observation[key] = _safe_observation(a[key])
            kind = a["transport_error_kind"]
            observation["transport_error_kind"] = kind if kind in FALLBACK or kind in API_FALLBACK or kind is None else "other"
            if v2: observation["identity_match"] = _identity_match(a, v2)
            attempt_observations.append(observation)
        observations[role] = {"whole_call_elapsed_ms": elapsed, "outcome": call["outcome"],
                              "attempts": attempt_observations}
        results[role] = (verdict, role_issues)
    arms = {}
    for name, role in (("current", "bull_current"), ("candidate", "bull_candidate")):
        bull, bi = results[role]; bear, ri = results["bear"]
        both = bull is not None and bear is not None
        policy_verdict = ("approve" if bull or bear else "reject") if both else "hold"
        confidence = (1.0 if bull == bear else .5) if both else None
        reasons = list(bi) + list(ri)
        if not both: reasons.append("review_unavailable")
        certified = both and not reasons
        verdict = policy_verdict if certified else "hold"
        arms[name] = {"verdict": verdict, "approved": verdict == "approve", "confidence": confidence if certified else None,
                      "policy_verdict": policy_verdict, "bull_verdict": bull, "bear_verdict": bear,
                      "independent_review_status": "available" if certified else "unavailable", "failed": False,
                      "fallback_used": False, "reasons": reasons}
    for channel in ("api", "subscription"):
        key = channel + "_estimated_cost_usd_known_subtotal"
        costs[key] = format(costs[key], "f")
    costs["api_estimated_cost_usd_total"] = None if costs["api_possibly_sent_unknown_estimate_count"] or costs["missing_trace_count"] else costs["api_estimated_cost_usd_known_subtotal"]
    costs.update(subscription_incremental_billed_cost_usd=None, total_billed_cost_usd=None, scope="observed_supplied_input_only")
    report = {"schema_version": REPORT_VERSION_V2 if v2 else REPORT_VERSION, "profile": PROFILE, "evidence_kind": payload["evidence_kind"],
            "comparison_id": payload["comparison_id"], "common_input_sha256": payload["common_input_sha256"],
            "controls": {role: copy.deepcopy(calls[role]["controls"]) for role in calls}, "arms": arms,
            "contract_status": "nonconformant" if violations else "unverified" if unknowns else "conformant",
            "violations": violations, "unknowns": unknowns, "attempt_count": len(ids), "observations": observations,
            "costs": costs, "actual_execution": False, "economic_evaluable": False, "net_delta_krw": None,
            "live_activation_supported": False, "contract_scope": "supplied_controls_and_attempt_evidence_only", "limitations": ["admission_and_settlement_not_checked", "offline_contract_only", "caller_supplied_evidence_not_attested", "response_after_price_not_joined"] + (["synthetic_evidence"] if payload["evidence_kind"] == "synthetic" else [])}

    if v2:
        report["identity_policy_version"] = IDENTITY_POLICY_VERSION
        report["limitations"].append("documented_identity_not_current_attestation")
    return report
