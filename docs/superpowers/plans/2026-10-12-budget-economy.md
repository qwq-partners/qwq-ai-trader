# Monthly LLM economy policy — Plan → Do → See

Approved scope: preserve the existing $100 KST monthly ledger and activation; ignore no additional spending. At 80% committed estimate enter economy, at 95% reserve remaining capacity for explicitly essential judgments, at 100% stop paid calls. No live orders or BUY policy changes. Telegram delivery cannot authorize expenditure. Existing deployment authorization covers verified release.

## Plan

- Coordinator (Astra) owns integration, financial policy, model routing, provenance and runner. Base `52cd9a0`; feature worktree separate from production.
- Sol/high worker owns only deterministic notification module/tests in a separate worktree. Sol/high read-only auditor checks callsite classification and policy hazards. Actual native model metadata is unavailable. No fan-out or credentials/API/operations by workers.
- Final critical review: independent verified Claude Opus5/xhigh with sanitized source, tools disabled, bounded streamed invocation. Maximum coordinator plus three workers.

## Do

1. `llm_budget_policy.py` + `llm_budget.py`: optional additive policy tables, per-month sticky 80/95 high-water, persistent transition outbox with claim leases/backoff, admission priority and stale-profile check in the reservation transaction. Original settings, costs and activation survive upgrade. Unknown usage/bound breaches carry across months. Record task/profile/priority against reservation token.
2. `llm.py` + default config: opt-in policy, REPORT_SUMMARY task (normal heavy; economy same provider light), bounded profile reselection, economy output caps for summaries/classifiers only, stop optional shadow comparisons. Preserve important model/provider controls. Strip internal policy parameters before transport.
3. Callsite annotations: explicit essential live entry/holding/regime paths; three harmless heavy summaries use REPORT_SUMMARY. Research defaults nonessential. Tag budget-unavailable advisory validation separately from approval while preserving current bool/sizing semantics.
4. Monitor + runner: deterministic Korean Telegram notice, durable retry after failure/restart, month rollover observed even when idle, cancellation-safe runner ownership. Never invoke an LLM to report its own budget.
5. Update research/operations entry points with outcome and remaining profitability evaluation work. Commit/push PR, required CI, guarded fixed-SHA deploy, verify state/health and preserve existing kill/captures/evolved overrides.

## See

- Isolated tests: 80/95/100 boundaries and concurrent admission, activation upgrade, month rollover including in-flight/unknown use, profile selection and no transport leakage, notification claim/retry/restart, advisory provenance and runner shutdown.
- Full UTC verify, secret scan, independent review and fresh resolution tests before integration. No paid model probes or trading calls in local validation.
- Production check: fixed SHA, health/recovery/pending, active kill and protected state hashes, original ledger activation/spending preserved, notifier starts without first-normal spam, rollback on deploy failure.
- This change controls cost and records policy changes; it does not establish strategy profitability. Next remains same-time market evidence → small measured comparison → cost/latency-adjusted model and entry evaluation.
