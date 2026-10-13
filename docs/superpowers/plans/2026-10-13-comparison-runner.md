# Comparison runner: Plan → Do → See

Goal: preserve paid-call evidence and compare entry decisions using neutral pre-decision sizing and response-time evidence, without changing live execution.
Architecture: durable logical attempts in the existing budget database; immutable pre-LLM sizing evidence; explicit offline/shadow single-attempt provider and paired runner.
Tech stack: Python, SQLite, existing raw LLM clients, pytest.
Spec: docs/research/model-comparison-plan-2026-10-13.md plus the contracts below.
Base: ed624e21470c3d25ee2a75b8c7b1ce4faf14bc77. Coordinator: feature/comparison-runner-20261013 in isolated worktree.

## Global constraints
- No production checkout edits, deploy/restart/SSH/systemctl, timers, real orders, paid application LLM calls or broker calls. Preserve operating 161b394 and Oct14 pilot. No credentials/state in tests or commits.
- Same explicit comparison attempt ID cannot dispatch twice. Unknown or interrupted dispatch cannot be retried automatically. Persisted result must bind all request controls. Unknown cost stays unknown; existing monthly baseline/caps are never reset.
- Pre-decision capture must precede LLM mutation/await. Use actual sizing logic on isolated state, never infer approve quantity from a reduced result. Risk/admission gaps remain explicit; sizing output alone is not executable quantity.
- Paired comparison uses same role/input and pre-fixed policy. Distinguish historical offline replay from live shadow. Neither mode calls order code. No implied net-profit or model superiority claim.
- One coordinator, at most 3 workers; disjoint files/worktrees; critical authors cannot approve own integration. Requested and actual model evidence reported separately.

## Task 1: Durable comparison attempt identity
Owner: Astra/high worker; allowed files src/analytics/model_comparison_execution.py, src/utils/llm_budget.py only if essential, tests/test_model_comparison_execution.py and new focused recovery tests.
Interface: ComparisonRequest adds optional reasoning_effort and temperature (default 0.3); guard.call(..., attempt_id=stable_string) persists request fingerprint and JSON result. Existing no-ID calls may remain supported for compatibility but runner always supplies IDs. Return ComparisonAttemptResult remains value + cost; durable lookup method can expose state/token/result for reports.
Requirements: concurrent duplicate rejection, same-ID same-payload completed recovery without dispatch, changed-payload rejection, post-settlement record failure safety, cancellation/process loss fail-closed, no unknown-to-zero conversion. Preserve daily-slot accounting and existing policy compatibility. Avoid blind settlement retries; use durable evidence or explicit pending reconciliation if atomic recovery cannot be proven.
Tests: failing duplicate/result-loss tests first, then known/unknown/blocked/cancellation/restart/concurrency/changed-control cases and existing monthly tests. Commit own files; no push. Report /tmp/qwq-runner-task1-report.md.

## Task 2: Neutral sizing capture and replay
Owner: Astra/high worker; allowed files src/core/engine.py, src/core/cross_validator.py, src/analytics/llm_decision_record.py, new src/analytics/model_comparison_sizing.py and focused tests. Do not modify Task1/3 files.
Approved review follow-up: extend Task2 ownership to src/analytics/decision_quote_join.py and its focused consumer test, separating engine fail-open allowance from actual model approval counts. No concurrent writer used those files.
Interface: JSON-safe snapshot before llm_second_check, persisted with decision record; replay_policy_sizing(snapshot, verdict) returns quantity, multiplier, trace, stable snapshot/replay references and explicit admissibility scope. Contract sent to coordinator before final implementation.
Requirements: preserve existing runtime sizing/order behavior. Capture existing multiplier, current cash/reservations/strategy/core/risk inputs and overlays once; deterministic actual sizing path for approve/reject/unavailable. Use shared logic or isolated replay, no duplicate approximate policy. No production portfolio mutation/cache reads during later replay. Missing/invalid snapshot refuses economic evaluation. Existing downstream risk gates are not silently asserted passed.
Tests: failure first; pre-await isolation, absent/nested signal metadata, risk and nominal, min-position/min3/strategy-cash caps, pre-existing multiplier, capture failure leaves real decision unchanged. Commit own files; report /tmp/qwq-runner-task2-report.md.

## Task 3: Provider, paired runner, report integration
Owner: coordinator (critical integration); files new src/analytics/model_comparison_runner.py, provider adapter if separate, scripts/report_model_comparison.py, focused tests and docs.
Requirements: injected raw provider clients, one complete call per attempt (never nested LLMManager), allowlisted controls, timestamps/model/usage/cost proof, deterministic role-matched pair IDs and resume-safe results. Explicit offline versus live provenance, immutable role policy, failure/cost completeness in reports. Join validated quote evidence where supported; missing state, sizing admission, or pricing evidence means non-evaluable. CLI reporting reads explicit files only and cannot call providers or create monthly ledger. No autoactivation in engine.
Tests: fake-provider end-to-end with real temporary budget DB; resume no additional dispatch; request-control identity; cancellation and budget block; role mismatch; historical replay not live; delayed response price and unknown cost exclusions.

## See: failure modes and integration
1. Known response lost after settlement or unknown cost accidentally cleared.
2. Same logical pair dispatched again after restart/changed configuration.
3. LLM-mutated quantity reused as neutral evidence; future cash/overlay contaminates replay.
4. Role comparison presented as model comparison or historical timing presented as live opportunity.
5. Sizing-only or missing market state promoted to fill/profit evidence.
Run focused tests, then full UTC verify and time-sensitive KST checks, changed-file and repository secret checks. Independent critical source review, address findings and re-review changed scope. Update status/docs, commit/push/PR/CI/merge only with review evidence. No deployment in this task.

## Preflight and execution record

| Interfaces / scope | Check |
|---|---|
| Task1 → Task3 | Stable ID, full controls/JSON result/lookup agreed before integration; no overlapping writers |
| Task2 → Task3 | neutral-policy-sizing-v1 in inputs.neutral_sizing, replay sizing_only; no admission claim |
| Task1 internal | Persistence retry bounded to identical SQLITE_BUSY/LOCKED writes; settlement never blindly retried |
| Task2 internal | Actual sizing reused; capture before await; replay/cache/log state isolated |
| Task3 internal | Same-role change, common Bear, per-attempt budget, cached pair, failure evidence and source joins |

Coordinator integrated author commits 19f42bc/ddb0eda (Task1), 3197a35/75bf379 (Task2), both from ed624e2 in disjoint feature worktrees. Authors requested Astra/high; actual metadata unverified. Independent source reviews requested Claude Opus5/high with tools disabled, 480s/$4 each, actual model verified externally; final outcomes and full verification are recorded in docs/research/model-comparison-runner-2026-10-13.md.

Decisions: no automatic retry for blocked/interrupted IDs (prevents stale signal execution); no generic exception→zero-cost inference; retain existing fallback sizing policy while separately exposing missing model verdict; snapshots verify integrity, not provenance. These boundaries are explicit in the report and operating runbook.
