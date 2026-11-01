"""Offline contract examples; all evidence in these tests is synthetic."""
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.analytics.model_comparison_contract import evaluate_contract, _parse_verdict

ROOT = Path(__file__).resolve().parents[1]


def attempt(role, model, effort, transport, channel):
    return dict(attempt_id=role, transport=transport, billing_channel=channel,
                requested_model=model, resolved_model=model, requested_reasoning_effort=effort,
                effective_reasoning_effort=None, status="success", transport_error_kind=None,
                request_sent=True, usage_known=True, input_tokens=10, output_tokens=2,
                started_ms=0, finished_ms=1000, api_estimated_cost_usd="0.01" if channel == "api" else None,
                subscription_estimated_cost_usd="0" if channel == "subscription" else None)


def payload():
    bull = dict(requested_model="gpt-5-mini", reasoning_effort="minimal", max_output_tokens=400,
                temperature=0.3, seed=20260803, empty_retries=1)
    bear = dict(requested_model="claude-opus-5-5", reasoning_effort="low", max_output_tokens=400,
                temperature=None, seed=None, empty_retries=0)
    candidate = dict(bull, reasoning_effort="low")
    def call(role, controls, transport, channel, text):
        return dict(controls=controls, whole_call_elapsed_ms=1000, outcome="success", content=text,
                    attempts=[attempt(role, controls["requested_model"], controls["reasoning_effort"], transport, channel)])
    return dict(schema_version="model-comparison-offline-contract-v1", profile="entry-independent-review-v1",
                evidence_kind="synthetic", comparison_id="synthetic-1", common_input_sha256="a" * 64,
                policy=dict(requires_independent_review=True, missing_review_policy="hold", single_model_fallback=False,
                            strict_on_disagreement=False, panel_timeout_seconds=12.0,
                            bear_internal_timeout_seconds=11.5, bear_transport_order=["claude_cli", "anthropic_messages"]),
                calls=dict(bull_current=call("current", bull, "openai_chat", "api", "APPROVE"),
                           bull_candidate=call("candidate", candidate, "openai_chat", "api", "APPROVE"),
                           bear=call("bear", bear, "claude_cli", "subscription", "ACCEPT")))


def test_success_is_offline_and_shared_bear_cost_is_counted_once():
    p = payload(); before = copy.deepcopy(p)
    result = evaluate_contract(p)
    assert p == before
    assert result["contract_status"] == "conformant"
    assert result["arms"]["current"]["verdict"] == "approve"
    assert result["attempt_count"] == 3
    assert result["costs"]["api_estimated_cost_usd_total"] == "0.02"
    assert result["actual_execution"] is False and result["economic_evaluable"] is False
    assert "APPROVE" not in json.dumps(result)


@pytest.mark.parametrize("bull,bear,verdict,confidence", [("APPROVE", "ACCEPT", "approve", 1), ("REJECT", "REJECT", "reject", 1), ("REJECT", "ACCEPT", "approve", .5), ("APPROVE", "REJECT", "approve", .5)])
def test_policy_votes(bull, bear, verdict, confidence):
    p = payload(); p["calls"]["bull_current"]["content"] = bull; p["calls"]["bear"]["content"] = bear
    arm = evaluate_contract(p)["arms"]["current"]
    assert (arm["verdict"], arm["confidence"]) == (verdict, confidence)


@pytest.mark.parametrize("role", ["bull_current", "bear"])
@pytest.mark.parametrize("outcome", ["error", "timeout", "cancelled", "blocked", "missing"])
def test_unavailable_review_holds_and_preserves_sibling(role, outcome):
    p = payload(); p["calls"][role].update(outcome=outcome, content="", attempts=[])
    arm = evaluate_contract(p)["arms"]["current"]
    assert (arm["verdict"], arm["failed"], arm["fallback_used"]) == ("hold", False, False)
    assert (arm["bear_verdict"] if role == "bull_current" else arm["bull_verdict"]) is True


def test_missing_timing_or_identity_is_unverified_and_holds():
    p = payload(); p["calls"]["bear"]["whole_call_elapsed_ms"] = None
    p["calls"]["bull_current"]["attempts"][0]["resolved_model"] = None
    r = evaluate_contract(p)
    assert r["contract_status"] == "unverified"
    assert r["arms"]["current"]["verdict"] == "hold"


def test_fallback_retains_separate_costs_and_obeys_whole_deadline():
    p = payload(); b = p["calls"]["bear"]; cli = b["attempts"][0]
    cli.update(status="timeout", transport_error_kind="cli_timeout", usage_known=False, input_tokens=None, output_tokens=None, finished_ms=6000, subscription_estimated_cost_usd=None)
    api = attempt("bear-api", "claude-opus-5-5", "low", "anthropic_messages", "api")
    api.update(started_ms=6000, finished_ms=9000); b["attempts"].append(api); b["whole_call_elapsed_ms"] = 10000
    r = evaluate_contract(p)
    assert r["arms"]["current"]["verdict"] == "approve"
    assert r["attempt_count"] == 4 and r["costs"]["api_estimated_cost_usd_total"] == "0.03"
    assert r["costs"]["subscription_unknown_usage_count"] == 1
    b["whole_call_elapsed_ms"] = 13000
    assert evaluate_contract(p)["arms"]["current"]["verdict"] == "hold"


@pytest.mark.parametrize("status,kind", [("success", None), ("empty", None), ("refusal", "cli_timeout"), ("identity_mismatch", "cli_timeout"), ("policy_violation", "cli_timeout")])
def test_unsafe_fallback_is_nonconformant(status, kind):
    p = payload(); b = p["calls"]["bear"]; b["attempts"][0].update(status=status, transport_error_kind=kind)
    a = attempt("bear-api", "claude-opus-5-5", "low", "anthropic_messages", "api"); a.update(started_ms=1000, finished_ms=2000)
    b["attempts"].append(a); b["whole_call_elapsed_ms"] = 2000
    r = evaluate_contract(p)
    assert r["contract_status"] == "nonconformant" and r["arms"]["current"]["verdict"] == "hold"


def test_unknown_cost_and_identity_mismatch_never_approve():
    p = payload(); a = p["calls"]["bull_current"]["attempts"][0]
    a.update(resolved_model="gpt-other", api_estimated_cost_usd=None)
    r = evaluate_contract(p)
    assert r["arms"]["current"]["verdict"] == "hold"
    assert r["costs"]["api_estimated_cost_usd_total"] is None
    assert r["costs"]["api_estimated_cost_usd_known_subtotal"] == "0.01"


@pytest.mark.parametrize("change", [lambda p: p["policy"].update(strict_on_disagreement=True), lambda p: p["calls"]["bull_candidate"].update(controls=copy.deepcopy(p["calls"]["bull_current"]["controls"])), lambda p: p["calls"]["bear"]["attempts"][0].update(attempt_id="current"), lambda p: p["calls"]["bear"]["attempts"][0].update(input_tokens=True), lambda p: p["calls"]["bear"]["attempts"][0].update(api_estimated_cost_usd="1"), lambda p: p.update(private="secret")])
def test_malformed_input_is_rejected_without_content(change):
    p = payload(); change(p)
    with pytest.raises(ValueError) as exc:
        evaluate_contract(p)
    assert "secret" not in str(exc.value)


def test_parser_matches_existing_parser():
    from src.core.adversarial_validator import _parse_verdict as existing
    for text in ("", "approve then reject", "REJECT but APPROVE", "unrelated", "ACCEPT and REJECT"):
        for positive in ("APPROVE", "ACCEPT"):
            assert _parse_verdict(text, positive, "REJECT") == existing(text, positive, "REJECT")


def test_cli_explicit_path_and_safe_invalid_json(tmp_path):
    f = tmp_path / "in.json"; f.write_text(json.dumps(payload()))
    command = [sys.executable, str(ROOT / "scripts/check_model_comparison_contract.py"), "--input", str(f)]
    ok = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert ok.returncode == 0 and json.loads(ok.stdout)["contract_status"] == "conformant"
    f.write_text('{"secret":"private", "secret":1}')
    bad = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert bad.returncode == 2 and "private" not in bad.stderr


@pytest.mark.parametrize("role,text", [("bull_current", ""), ("bear", "unparseable")])
def test_unparseable_reviews_hold(role, text):
    p = payload(); p["calls"][role]["content"] = text
    if not text: p["calls"][role]["attempts"][0]["status"] = "empty"
    r = evaluate_contract(p)
    assert r["arms"]["current"]["verdict"] == "hold"
    assert r["contract_status"] == "conformant"


@pytest.mark.parametrize("field,value", [("outcome", []), ("outcome", {}), ("whole_call_elapsed_ms", True), ("whole_call_elapsed_ms", float('nan'))])
def test_bad_call_types_are_safe_errors(field, value):
    p = payload(); p["calls"]["bear"][field] = value
    with pytest.raises(ValueError): evaluate_contract(p)


def test_api_missing_attempt_timing_does_not_certify_fallback():
    p = payload(); b = p["calls"]["bear"]; b["attempts"][0].update(status="timeout", transport_error_kind="cli_timeout")
    a = attempt("api", "claude-opus-5-5", "low", "anthropic_messages", "api")
    a.update(started_ms=None, finished_ms=None); b["attempts"].append(a); b["whole_call_elapsed_ms"] = 2000
    r = evaluate_contract(p)
    assert r["contract_status"] == "unverified" and r["arms"]["current"]["verdict"] == "hold"


def test_bull_retry_only_after_empty_and_uses_original_deadline():
    p = payload(); call = p["calls"]["bull_current"]; first = call["attempts"][0]
    first.update(status="empty", finished_ms=8000)
    second = attempt("retry", "gpt-5-mini", "minimal", "openai_chat", "api")
    second.update(started_ms=8000, finished_ms=11000)
    call["attempts"].append(second); call["whole_call_elapsed_ms"] = 11000
    assert evaluate_contract(p)["arms"]["current"]["verdict"] == "approve"
    call["whole_call_elapsed_ms"] = 12000
    assert evaluate_contract(p)["arms"]["current"]["verdict"] == "hold"
    call["whole_call_elapsed_ms"] = 11000; first["status"] = "error"
    assert evaluate_contract(p)["contract_status"] == "nonconformant"


def test_cli_safely_rejects_missing_oversized_and_nonregular_input(tmp_path):
    script = str(ROOT / "scripts/check_model_comparison_contract.py")
    for path in (tmp_path / "missing-private", tmp_path, tmp_path / "large"):
        if path.name == "large": path.write_bytes(b"x" * (1024 * 1024 + 1))
        result = subprocess.run([sys.executable, script, "--input", str(path)], cwd=tmp_path, capture_output=True, text=True)
        assert result.returncode == 2 and "private" not in result.stderr


def test_cli_held_report_is_successful_execution(tmp_path):
    p = payload(); p["calls"]["bear"].update(outcome="missing", attempts=[], content="")
    f = tmp_path / "held.json"; f.write_text(json.dumps(p))
    result = subprocess.run([sys.executable, str(ROOT / "scripts/check_model_comparison_contract.py"), "--input", str(f)], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0 and json.loads(result.stdout)["arms"]["current"]["verdict"] == "hold"


def test_contract_imports_only_standard_library():
    import ast
    module = ast.parse((ROOT / "src/analytics/model_comparison_contract.py").read_text())
    imports = {alias.name.split(".")[0] for node in ast.walk(module) if isinstance(node, ast.Import) for alias in node.names}
    imports |= {node.module.split(".")[0] for node in ast.walk(module) if isinstance(node, ast.ImportFrom)}
    assert imports <= sys.stdlib_module_names


def test_not_sent_unknown_estimate_is_counted_without_invalidating_api_total():
    p = payload(); b = p["calls"]["bear"]
    b["attempts"][0].update(status="error", transport_error_kind="cli_unavailable", request_sent=False, resolved_model=None,
                             usage_known=False, input_tokens=None, output_tokens=None,
                             subscription_estimated_cost_usd=None)
    a = attempt("bear-api", "claude-opus-5-5", "low", "anthropic_messages", "api")
    a.update(started_ms=1000, finished_ms=2000); b["attempts"].append(a); b["whole_call_elapsed_ms"] = 2000
    r = evaluate_contract(p)
    assert r["arms"]["current"]["verdict"] == "approve"
    assert r["costs"]["subscription_unknown_estimate_count"] == 1
    assert r["costs"]["api_estimated_cost_usd_total"] == "0.03"


@pytest.mark.parametrize("role", ["bear", "bull_current"])
def test_prior_attempt_identity_mismatch_blocks_continuation(role):
    p = payload(); c = p["calls"][role]; first = c["attempts"][0]
    if role == "bear":
        first.update(status="timeout", transport_error_kind="cli_timeout", resolved_model="claude-other")
        retry = attempt("retry", "claude-opus-5-5", "low", "anthropic_messages", "api")
    else:
        first.update(status="empty", resolved_model="gpt-other")
        retry = attempt("retry", "gpt-5-mini", "minimal", "openai_chat", "api")
    retry.update(started_ms=1000, finished_ms=2000)
    c["attempts"].append(retry); c["whole_call_elapsed_ms"] = 2000
    r = evaluate_contract(p)
    assert r["contract_status"] == "nonconformant"
    assert r["arms"]["current"]["verdict"] == "hold"


@pytest.mark.parametrize("change", [
    {"status": "empty"}, {"status": "success"}, {"status": "refusal"},
    {"status": "identity_mismatch"}, {"input_tokens": 1},
    {"api_estimated_cost_usd": "0.01"}, {"resolved_model": "gpt-5-mini"},
])
def test_not_sent_attempt_contradictions_hold_but_preserve_cost(change):
    p = payload(); c = p["calls"]["bull_current"]; first = c["attempts"][0]
    first.update(status="error", request_sent=False, input_tokens=0, output_tokens=0,
                 api_estimated_cost_usd="0", resolved_model=None)
    first.update(change)
    second = attempt("retry", "gpt-5-mini", "minimal", "openai_chat", "api")
    second.update(started_ms=1000, finished_ms=2000)
    c["attempts"].append(second); c["whole_call_elapsed_ms"] = 2000
    r = evaluate_contract(p)
    assert r["contract_status"] == "nonconformant"
    assert r["arms"]["current"]["verdict"] == "hold"
    assert any(item["code"] == "not_sent_attempt_contradiction" for item in r["violations"])
    assert r["costs"]["api_estimated_cost_usd_known_subtotal"] == ("0.03" if change.get("api_estimated_cost_usd") else "0.02")


def test_unsuccessful_terminal_outcome_with_successful_attempt_is_nonconformant():
    p = payload(); p["calls"]["bear"]["outcome"] = "timeout"
    r = evaluate_contract(p)
    assert r["contract_status"] == "nonconformant" and r["arms"]["current"]["verdict"] == "hold"


@pytest.mark.parametrize("status,kind", [("success", "cli_incomplete"), ("empty", "cli_incomplete"), ("timeout", "cli_rate_limited")])
def test_inconsistent_status_and_error_kind_is_nonconformant(status, kind):
    p = payload(); c = p["calls"]["bear"]; c["attempts"][0].update(status=status, transport_error_kind=kind)
    if status == "empty": c["content"] = ""
    if status == "timeout":
        second = attempt("retry", "claude-opus-5-5", "low", "anthropic_messages", "api")
        second.update(started_ms=1000, finished_ms=2000); c["attempts"].append(second); c["whole_call_elapsed_ms"] = 2000
    r = evaluate_contract(p)
    assert r["contract_status"] == "nonconformant" and r["arms"]["current"]["verdict"] == "hold"


def test_cost_output_uses_fixed_point_decimal():
    p = payload(); p["calls"]["bull_current"]["attempts"][0]["api_estimated_cost_usd"] = "0.0000001"
    p["calls"]["bull_candidate"]["attempts"][0]["api_estimated_cost_usd"] = "0"
    assert evaluate_contract(p)["costs"]["api_estimated_cost_usd_total"] == "0.0000001"


@pytest.mark.parametrize("path", ["comparison_id", "attempt_id", "candidate_model", "candidate_effort"])
@pytest.mark.parametrize("bad", ["private prompt text", "x" * 129])
def test_reported_identifiers_are_bounded_and_safe(path, bad):
    p = payload(); a = p["calls"]["bull_current"]["attempts"][0]
    if path == "comparison_id": p[path] = bad
    elif path == "candidate_model": p["calls"]["bull_candidate"]["controls"]["requested_model"] = bad
    elif path == "candidate_effort": p["calls"]["bull_candidate"]["controls"]["reasoning_effort"] = bad
    else: a[path] = bad
    with pytest.raises(ValueError) as exc: evaluate_contract(p)
    assert bad not in str(exc.value)


@pytest.mark.parametrize("sent", [False, None])
def test_final_success_without_proven_transmission_holds(sent):
    p = payload(); p["calls"]["bear"]["attempts"][0]["request_sent"] = sent
    r = evaluate_contract(p)
    assert r["arms"]["current"]["verdict"] == "hold"
    assert r["contract_status"] == ("nonconformant" if sent is False else "unverified")


def test_success_without_terminal_attempt_is_unverified():
    p = payload(); p["calls"]["bear"]["attempts"] = []
    r = evaluate_contract(p)
    assert r["contract_status"] == "unverified" and r["arms"]["current"]["verdict"] == "hold"


@pytest.mark.parametrize("role", ["bull_current", "bear"])
@pytest.mark.parametrize("outcome", ["success", "error", "timeout", "cancelled"])
def test_missing_attempt_trace_leaves_cost_total_unknown(role, outcome):
    p = payload(); p["calls"][role].update(outcome=outcome, attempts=[])
    r = evaluate_contract(p)
    assert r["contract_status"] == "unverified"
    assert r["costs"]["missing_trace_count"] == 1
    assert r["costs"]["api_estimated_cost_usd_total"] is None
    assert r["attempt_count"] == 2
    assert r["arms"]["current"]["verdict"] == "hold"


@pytest.mark.parametrize("role", ["bull_current", "bear"])
@pytest.mark.parametrize("outcome", ["missing", "blocked"])
def test_missing_and_blocked_empty_trace_are_allowed(role, outcome):
    p = payload(); p["calls"][role].update(outcome=outcome, content="", attempts=[])
    r = evaluate_contract(p)
    assert r["contract_status"] == "conformant" and r["costs"]["missing_trace_count"] == 0


def test_unusual_observed_identity_and_effort_are_redacted_but_costs_preserved():
    p = payload(); a = p["calls"]["bear"]["attempts"][0]
    a.update(resolved_model="claude-opus-5-5@20261101", effective_reasoning_effort="private prose")
    r = evaluate_contract(p)
    assert r["contract_status"] == "nonconformant" and r["arms"]["current"]["verdict"] == "hold"
    assert r["costs"]["api_estimated_cost_usd_total"] == "0.02"
    assert r["observations"]["bear"]["attempts"][0]["resolved_model"] == "other"
    assert r["observations"]["bear"]["attempts"][0]["effective_reasoning_effort"] == "other"
    assert "private prose" not in json.dumps(r)


@pytest.mark.parametrize("role,outcome,elapsed,kind", [("bull_current", "timeout", 12000, None), ("bear", "timeout", 11500, None), ("bear", "cancelled", 11500, None)])
def test_timeout_or_cancellation_at_deadline_is_valid_held_evidence(role, outcome, elapsed, kind):
    p = payload(); c = p["calls"][role]
    c["attempts"][0].update(status=outcome, finished_ms=elapsed, transport_error_kind=kind)
    c.update(outcome=outcome, content="", whole_call_elapsed_ms=elapsed)
    r = evaluate_contract(p)
    assert r["contract_status"] == "conformant" and r["arms"]["current"]["verdict"] == "hold"


@pytest.mark.parametrize("elapsed,expected", [(11499, "approve"), (11500, "hold")])
def test_bear_whole_call_boundary(elapsed, expected):
    p = payload(); p["calls"]["bear"]["whole_call_elapsed_ms"] = elapsed
    assert evaluate_contract(p)["arms"]["current"]["verdict"] == expected


@pytest.mark.parametrize("finish,expected", [(5749, "approve"), (5750, "hold")])
def test_cli_success_boundary(finish, expected):
    p = payload(); p["calls"]["bear"]["attempts"][0]["finished_ms"] = finish
    p["calls"]["bear"]["whole_call_elapsed_ms"] = finish
    assert evaluate_contract(p)["arms"]["current"]["verdict"] == expected


def test_attempt_finish_after_whole_call_is_nonconformant():
    p = payload(); p["calls"]["bear"]["whole_call_elapsed_ms"] = 999
    assert evaluate_contract(p)["contract_status"] == "nonconformant"


@pytest.mark.parametrize("finish,expected", [(11499, "approve"), (11500, "hold")])
def test_api_success_deadline_boundary(finish, expected):
    p = payload(); c = p["calls"]["bear"]
    c["attempts"][0].update(status="timeout", transport_error_kind="cli_timeout")
    a = attempt("api", "claude-opus-5-5", "low", "anthropic_messages", "api")
    a.update(started_ms=1000, finished_ms=finish); c["attempts"].append(a); c["whole_call_elapsed_ms"] = finish
    assert evaluate_contract(p)["arms"]["current"]["verdict"] == expected


def test_api_timeout_finishing_after_deadline_is_held_without_violation():
    p = payload(); c = p["calls"]["bear"]
    c["attempts"][0].update(status="timeout", transport_error_kind="cli_timeout")
    a = attempt("api", "claude-opus-5-5", "low", "anthropic_messages", "api")
    a.update(status="timeout", started_ms=1000, finished_ms=12000)
    c["attempts"].append(a); c.update(outcome="timeout", content="", whole_call_elapsed_ms=12000)
    r = evaluate_contract(p)
    assert r["contract_status"] == "conformant" and r["arms"]["current"]["verdict"] == "hold"


def test_api_only_and_second_cli_are_nonconformant():
    p = payload(); c = p["calls"]["bear"]
    c["attempts"][0].update(transport="anthropic_messages", billing_channel="api", api_estimated_cost_usd="0", subscription_estimated_cost_usd=None)
    assert evaluate_contract(p)["contract_status"] == "nonconformant"
    p = payload(); c = p["calls"]["bear"]; c["attempts"][0].update(status="timeout", transport_error_kind="cli_timeout")
    a = attempt("second-cli", "claude-opus-5-5", "low", "claude_cli", "subscription")
    a.update(started_ms=1000, finished_ms=2000); c["attempts"].append(a); c["whole_call_elapsed_ms"] = 2000
    assert evaluate_contract(p)["contract_status"] == "nonconformant"


@pytest.mark.parametrize("change", [
    lambda p: p["calls"]["bear"]["attempts"].extend([{}, {}]),
    lambda p: p["calls"]["bear"]["attempts"][0].update(transport="gemini"),
    lambda p: p["policy"].update(bear_internal_timeout_seconds=19.5),
    lambda p: p["calls"]["bear"]["controls"].update(max_output_tokens=800),
])
def test_unsupported_profiles_and_attempt_shapes_rejected(change):
    p = payload(); change(p)
    with pytest.raises(ValueError): evaluate_contract(p)


def test_unknown_cli_timeout_kind_does_not_authorize_api_fallback():
    p = payload(); c = p["calls"]["bear"]
    c["attempts"][0].update(status="timeout", transport_error_kind=None)
    a = attempt("api", "claude-opus-5-5", "low", "anthropic_messages", "api")
    a.update(started_ms=1000, finished_ms=2000); c["attempts"].append(a); c["whole_call_elapsed_ms"] = 2000
    r = evaluate_contract(p)
    assert r["contract_status"] == "nonconformant" and r["arms"]["current"]["verdict"] == "hold"


def test_api_start_at_deadline_is_nonconformant_even_if_timed_out():
    p = payload(); c = p["calls"]["bear"]
    c["attempts"][0].update(status="timeout", transport_error_kind="cli_timeout")
    a = attempt("api", "claude-opus-5-5", "low", "anthropic_messages", "api")
    a.update(status="timeout", started_ms=11500, finished_ms=12000)
    c["attempts"].append(a); c.update(outcome="timeout", content="", whole_call_elapsed_ms=12000)
    assert evaluate_contract(p)["contract_status"] == "nonconformant"


def test_contradicted_not_sent_unknown_cost_cannot_complete_api_total():
    p = payload(); a = p["calls"]["bull_current"]["attempts"][0]
    a.update(request_sent=False, api_estimated_cost_usd=None)
    r = evaluate_contract(p)
    assert r["contract_status"] == "nonconformant"
    assert r["costs"]["api_possibly_sent_unknown_estimate_count"] == 1
    assert r["costs"]["api_estimated_cost_usd_total"] is None


def test_genuine_not_sent_unknown_cost_does_not_prevent_api_total():
    p = payload(); c = p["calls"]["bull_current"]; a = c["attempts"][0]
    a.update(status="error", request_sent=False, resolved_model=None, usage_known=False,
             input_tokens=None, output_tokens=None, api_estimated_cost_usd=None)
    c.update(outcome="error", content="")
    r = evaluate_contract(p)
    assert r["contract_status"] == "conformant"
    assert r["costs"]["api_possibly_sent_unknown_estimate_count"] == 0
    assert r["costs"]["api_estimated_cost_usd_total"] == "0.01"


@pytest.mark.parametrize("retry", [False, True])
def test_bull_attempt_cannot_start_at_deadline_even_if_timeout(retry):
    p = payload(); c = p["calls"]["bull_current"]
    a = c["attempts"][0]
    if retry:
        a["status"] = "empty"
        a = attempt("retry", "gpt-5-mini", "minimal", "openai_chat", "api")
        c["attempts"].append(a)
    a.update(status="timeout", started_ms=12000, finished_ms=12500)
    c.update(outcome="timeout", content="", whole_call_elapsed_ms=12500)
    r = evaluate_contract(p)
    assert r["contract_status"] == "nonconformant"
    assert r["arms"]["current"]["verdict"] == "hold"


@pytest.mark.parametrize("role", ["bull_current", "bull_candidate"])
@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("elapsed", [12000, 12500])
def test_bull_unknown_attempt_start_at_or_after_deadline_is_unverified(role, retry, elapsed):
    p = payload(); c = p["calls"][role]; a = c["attempts"][0]
    if retry:
        a["status"] = "empty"
        controls = c["controls"]
        a = attempt("retry", controls["requested_model"], controls["reasoning_effort"], "openai_chat", "api")
        c["attempts"].append(a)
    a.update(status="timeout", started_ms=None, finished_ms=None)
    c.update(outcome="timeout", content="", whole_call_elapsed_ms=elapsed)
    r = evaluate_contract(p)
    assert r["contract_status"] == "unverified"
    assert any(x["code"] == "transport_timing_unknown" for x in r["unknowns"])
    arm = "current" if role == "bull_current" else "candidate"
    assert r["arms"][arm]["verdict"] == "hold"


@pytest.mark.parametrize("role", ["bull_current", "bull_candidate"])
def test_bull_success_unknown_attempt_timing_under_deadline_remains_conformant(role):
    p = payload(); p["calls"][role]["attempts"][0].update(started_ms=None, finished_ms=None)
    r = evaluate_contract(p)
    assert r["contract_status"] == "conformant"
    arm = "current" if role == "bull_current" else "candidate"
    assert r["arms"][arm]["verdict"] == "approve"
