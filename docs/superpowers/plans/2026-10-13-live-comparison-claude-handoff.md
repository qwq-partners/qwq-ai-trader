# Live comparison preparation — Claude implementation handoff

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement task by task. This handoff authorizes development, tests, documentation and a reviewable PR; the user will return to Codex for final independent review. Do not merge or deploy.

**Goal:** Prepare a bounded real-time shadow comparison that measures the effect of one LLM reasoning change on response-time prices, actual sizing policy and costs, without changing trading decisions.

**Architecture:** Reuse PR189's comparison guard, paired runner, sizing replay and quote reporter. Add trusted pre-call input delivery, a separately managed worker, evidence-bound recovery and conservative evaluation gates. Keep the existing shared monthly budget and immutable evidence; no second financial ledger or new market-data connection.

**Tech Stack:** Existing Python3.12, SQLite, asynchronous/threaded recording, raw provider clients, pytest; no additional infrastructure by default.

**Spec:** Read the current repository's AGENTS.md, CLAUDE.md, CHANGELOG.md, docs/README.md, docs/research/model-comparison-runner-2026-10-13.md, docs/research/model-comparison-plan-2026-10-13.md, docs/research/concurrent-observation-design-2026-10-13.md, and docs/operations/account-baseline-status-2026-10-13.md. Read /home/ubuntu/.config/ai-agents/model-routing.md before dispatch.

## Baseline and scope

- Client date: 2026-10-13 Asia/Seoul. PR189 merged at main d936518cbe6d431f343d719c84e8f12a84f5fa0a. Its tested head is 28d2e4b892a38cd7f964dba449bb89087fed2283; final local/CI5758 passed,2 skipped,2 xfailed; related KST147 passed. These are baseline results, not validation of your new changes.
- Verify current remote main. Start from a base containing PR189 in a new isolated feature/* worktree. Inspect newer commits and existing workers before assigning files; do not overwrite concurrent work.
- The operating checkout /home/ubuntu/projects/qwq-ai-trader remains161b394abcfa9f4b03435efc2ef9728385450edd, with a pre-existing dirty config/evolved_overrides.yml. Do not check out/pull/edit the operating tree.
- Preserve KILL_SWITCH_KR, existing monthly$100 activation/accounting, operating model/threshold/sizing/exit policy, and the October14 08:55 activation/09:00–09:05 pilot. Do not execute SSH, systemctl, restart, deployment, timer installation, real orders, real broker calls or application paid LLM calls today.
- Existing development-agent routing is separate from application model selection. Record requested model/effort and observed identity without claiming unexposed metadata. One coordinator plus at most3 workers; disjoint worktrees/files; no worker fan-out.
- All paid comparison wiring is default OFF. Tests use fake providers, synthetic records and temporary/copied test ledgers. Public official documentation research is permitted; credential-bearing API requests are not part of this development task.
- Deliver implementation and evidence, not just a plan. Complete each independent task with PLAN → DO → SEE. The final independent review belongs to Codex after the user's handoff; no self-approved merge or deploy.

## Review focus

1. Research must not bypass the operating economy policy, reset accounting, or exceed a cap through concurrent admission, retries, fallbacks or experiment-ID changes.
2. Decisions submitted after completion must not be presented as simultaneous comparisons; hashes prove integrity, not producer authenticity.
3. Worker failure/queue/storage pressure must not wait on the trading loop or silently relabel missing observations as success.
4. Recovery must preserve original responses and timestamps without resending uncertain paid calls or manufacturing a completed pair.
5. Sizing-only, raw market state and quote depth must not be promoted to actual executable quantity, fills, realized profit or account returns.

## Task1 — Shared-budget protection and comparison recovery (P0)

**Files to inspect/reuse:** src/analytics/model_comparison_execution.py, src/analytics/model_comparison_runner.py, src/utils/llm_budget.py, src/utils/llm_budget_policy.py, src/utils/llm_budget_reconciliation.py, scripts/reconcile_llm_budget.py, and their tests. Add a focused comparison recovery helper only where the existing implementation cannot express the contract.

**Known gaps:** The raw comparison guard reserves with research priority but does not mirror LLMManager's economy-stage shadow pause, and accepts a ledger with policy_enabled=False. The cost planner's reserve_share is not an enforced spending limit. The existing reconciliation CLI already supports evidence hash, ledger-state hash, preview and explicit apply; do not rebuild it. Reconciliation of an originally unknown comparison cost is not yet reflected cleanly in comparison lookup/reporting. Interrupted/final-write-failed pairs need evidence-preserving inspection/recovery.

**Interface contract:** Existing monthly reservations/settlements remain the financial source of truth. Comparison-specific inspection returns original response/usage, original estimated cost, reconciled ledger cost where proven, reservation token, attempt/pair identity and audit reference separately. It never overwrites original evidence or automatically changes an unknown cost to0.

- [ ] PLAN: Specify allowed budget modes, per-attempt reservation checks, experiment spending/reservation ceiling and operating reserve. For the live-ready path require an already enabled policy and normal mode. Recommend refusing reservations that would cross the80% economy threshold; document the exact boundary. Keep95% essential protection and100% stop. At monthly rollover do not rerun stale signals or clear unresolved costs.
- [ ] RED: Reproduce policy-disabled/economy bypass; unknown→existing audited reconciliation→lookup/report mismatch; final pair write failure; changed evidence/state; concurrent cap race.
- [ ] DO: Enforce the comparison admission condition inside the existing atomic reservation boundary using its actual units and committed amount. A status check followed by an unrelated reserve is insufficient. Reuse reservation/policy-attempt data; avoid a second monetary ledger or nested reservation. Never hold the budget transaction across provider calls, sizing replay or queue operations.
- [ ] DO: Connect existing reconciliation evidence to comparison inspection and cached-result reporting. A preview is read-only; tests may apply reconciliation to a temporary ledger only. Identical replay must not resend. If original pair timestamps/results are insufficient, report incomplete and stop; do not reconstruct them from the current clock.
- [ ] DO: For final pair writes, allow only bounded identical BUSY/LOCKED retries and exact commit-ack reconciliation. Explicit restoration requires the original result plus verified manifest/attempt linkage; without it export an incomplete diagnostic only. A successfully reconciled cost does not turn the original failed response into a successful decision. Correct the old runbook wording: a monthly cost reconciliation CLI exists; comparison-specific linkage/recovery is missing.
- [ ] SEE: Test80/95/100% boundaries, disabled policy, daily/monthly change, concurrent attempts, changing experiment ID, unknown cost, stale preview, tampered evidence, duplicate apply, short SQLite contention, process loss and partial pair recovery. Preserve the original monthly activation and existing settled amount in all cases.

## Task2 — Actual pre-call input, stable identity and provenance (P0)

**Files to inspect/reuse:** src/core/cross_validator.py, src/core/engine.py, src/core/entry_llm_context.py, src/analytics/llm_decision_record.py, src/analytics/model_comparison_sizing.py and the existing observation hook/journal. Prefer a small dedicated input module over spreading schema logic through the engine.

**Known gap:** build_record currently allocates decision_id when the decision finishes. A completed monthly decision JSONL is suitable for offline replay, not a same-time live shadow trigger. PairSpec currently accepts caller-supplied live provenance.

**Interface contract:** An immutable comparison input envelope carries decision_id, signal_id, symbol, producer/run identity, source/config/experiment fingerprints, common-context text and hash, exact role/fallback controls, neutral sizing snapshot, actual input-availability time and handoff/receive times. These IDs must match the later decision record and comparison attempts. Assign the ID once per actual invocation before asynchronous work, preserving old record compatibility.

- [ ] PLAN: Identify the precise point after common facts and role controls are fixed but before provider awaits. Freeze source/runtime/experiment identity and define how a worker verifies the producer; do not treat an arbitrary JSON flag or hash as attestation.
- [ ] RED: Show that a delayed completed record cannot become paired_live_shadow; cross-decision/context mix-up, duplicate delivery and concurrent invocations must be rejected or correctly isolated.
- [ ] DO: Add a default-OFF, bounded nonblocking handoff. Preserve the trading verdict and fail-open/soft-reject behavior if observation fails. Do not rebuild common facts later or use future cash/state.
- [ ] DO: Resolve the evaluation-anchor mismatch explicitly: existing code uses worker requested_at+900 while the research contract describes common input time+900. Define/version the live contract around the original common-input availability time; retain input, enqueue, dispatch and completion times separately. Update compatible reports/tests rather than silently reinterpreting historical rows.
- [ ] SEE: Cover frozen input, source/hash/identity mismatch, duplicate delivery, clock reversal, stale queue items, delayed provider completion, concurrency, recorder-disabled behavior and original decision-ID propagation. A decision that never reaches a real call remains distinguishable from a paid attempt.

## Task3 — Bounded comparison worker and lifecycle (P0)

**Files to inspect/reuse:** src/analytics/model_comparison_runner.py, existing raw provider clients and application shutdown hooks. Add a focused worker module and an explicit validation/dry-run entrypoint if needed; do not create a parallel trading engine.

**Interface contract:** Receive verified Task2 envelopes, submit PairSpec through the existing guard/runner, persist original pair results, expose health/counters and stop/drain with a fixed deadline. Paid execution is opt-in, disabled by default, and must pass Task1's policy/ledger checks.

- [ ] PLAN: Define queue/record/byte bounds, input-age deadline, one worker owner and single-pair concurrency for the initial experiment. Keep synchronous SQLite/replay/provider work outside the trading event loop. Define the producer/consumer transport and crash ownership clearly.
- [ ] RED: Exercise queue overflow, slow/failed writer, worker absence, duplicate processes, SIGTERM/cancellation, and timeout after a provider may have received the request.
- [ ] DO: Implement default-OFF startup wiring, bounded acceptance, durable result handling and bounded shutdown. Count offered/accepted/rejected/expired/started/finished/incomplete separately. Enqueue acknowledgement is not proof of durable storage or completed comparison.
- [ ] DO: Keep stale/partial/uncertain jobs out of automatic restart replay. Drain accepted work within a deadline or preserve explicit incomplete/unknown evidence. Add source/version/manifest checks and rollback/disable behavior. Do not install a service or timer.
- [ ] SEE: With fake providers and a temporary ledger, run input→worker→paired roles→result→shutdown/restart. The same ID must not cause another provider call or charge. Verify worker slowness/failure does not block the normal decision path or mutate pending orders/portfolio.

## Task4 — Market/admission evidence and conditional outcome gate (P1)

**Files to inspect/reuse:** src/analytics/kis_market_state.py, src/analytics/decision_quote_join.py, src/analytics/model_comparison_sizing.py, src/analytics/model_comparison_runner.py, src/analytics/model_comparison_outcome.py and the actual post-LLM risk path in src/core/engine.py. Reuse existing guard implementations where pure; avoid copying approximate trading rules.

**Interface contract:** Separate data completeness, documented market-state classification, replayable admission evidence and conditional economic eligibility. Every failure/unknown retains its reason and original source references. An evaluable conditional quote-price proxy is still actual_execution=false and never an actual fill/profit claim.

- [ ] PLAN: Inventory the real post-LLM gates (cash/strategy/factor limits, cooldown, quantities, risk/sector/holding limits, pending orders, trading-stop state and broker-dependent limits). State each input's required as-of time and whether it is safely replayable or externally unknowable.
- [ ] PLAN: Verify market-code meanings only with repository source artifacts or official primary documentation. Record version/hash/URL/date. Preserve raw fields. Missing events, empty values, different channel/connection/generation, stale state, unknown codes and conflicting state do not prove normal continuous trading.
- [ ] RED: Test good synthetic evidence against missing/stale/mismatched/reconnected/VI/halted evidence and each supported admission failure. Later portfolio snapshots must not leak into an earlier arm's hypothetical entry.
- [ ] DO: Implement a narrow pure evidence validator/replay for supported gates and documented market states. Do not invoke order-producing engine paths just to check admission. Unsupported broker/state gates remain unknown and cannot be marked passed.
- [ ] DO: Join each arm to its own first quote after availability and the same fixed evaluation horizon; expired/late arms are excluded. Bridge to the existing conditional outcome calculator only where all required evidence is actually supported. Missing cost, FX, admission or source proof keeps net_delta null; no blanket true override.
- [ ] SEE: Prove supported synthetic eligibility and rejection/unknown paths. Ask/bid size is insufficient to certify market-order VWAP, queue position, slippage or fills; keep explicit cost/slippage assumptions and conditional labels. Live readiness remains blocked by any required unresolved source meaning.

## Task5 — Small experiment manifest and daily report (P1)

**Files to inspect/reuse:** src/analytics/model_comparison_budget.py, model_comparison_execution.py, model_comparison_runner.py, scripts/report_model_comparison.py and existing monthly policy/notification/report facilities.

- [ ] PLAN: Draft one R experiment only: same Bull model/input/output cap, changing only the supported reasoning level (the current research draft is minimal400→low400). Confirm controls from the current source; do not silently substitute unsupported controls. Keep Bear/fallback/aggregation fixed. Separate the two shadow-policy results from the original live engine response.
- [ ] DO: Prepare an OFF manifest with at most1 pair/day and1 concurrent pair as the initial technical-sample proposal, explicit input freshness/output/attempt/USD ceilings, horizon, selection and exclusion rules, source fingerprints and stop conditions. Calculate the worst-case provider attempts including shared Bear, both Bull arms, empty retries and every fallback. Derive daily_attempt_cap and reservation dollars from that topology, not an old pairs/day table.
- [ ] DO: Use the same monthly$100 accounting and protect operating reserves. Report estimated vs measured/unknown costs. Count unique attempts once; arm scenario costs that share Bear must not be summed as actual spend. Do not use October13's historical available$99.70 as a current balance.
- [ ] DO: Keep the experiment's fixed USD ceiling and validity interval separate from the operating calendar-month reset. An experiment-ID change or month transition does not silently replenish that experimental ceiling; new policy activation/migration is a separate reviewed operation.
- [ ] DO: Extend the read-only daily report with offered/eligible/sampled/complete/excluded denominators, missing reasons, model identity/effort, input/queue/provider/result latency, per-arm post-response prices and quantities, original/reconciled costs and conditional economic fields. Preview notification text only; no real Telegram send today.
- [ ] SEE: Test zero samples, exclusions, partial results, unknown/reconciled costs, budget mode changes, day/month rollover and fixed selection independent of later outcomes. Do not tune thresholds daily on the same evaluation sample; technical bug fixes and hypothesis/version changes are distinct.

## Task6 — Integrated acceptance, documentation and Codex review package (P1)

- [ ] PLAN: Write the complete process flow and task/file ownership before parallel edits. Map every gate to evidence and a test. Keep implementation/status/deployment/collection/profitability separate.
- [ ] DO: Run an end-to-end fake-provider scenario from actual input-hook boundary through worker, shared budget, original decision record, paired result, strict synthetic observation journal and daily report. Also run abnormal shutdown/restart, full queue, storage failure, economy/essential/halt and unknown-cost cases.
- [ ] SEE: Run relevant tests, the full scripts/dev/verify.sh, UTC and KST time-sensitive tests, syntax checks, changed-file/repository secret scans and diff checks. Report exact commands, return codes and counts, including skipped/xfail and isolation violations. Do not reuse PR189's counts as your new validation.
- [ ] DO: Update CLAUDE.md, CHANGELOG.md, docs/README.md and the research/operations handoff. Keep account starting-NAV/external-flow/rights verification and benchmark work visible as unresolved independent tracks; no fabricated account TWR or semiconductor-excluded benchmark results.
- [ ] DO: Prepare tomorrow's decision checklist without installing anything: after October14 09:05 verify pilot completion/seal/drop counts, per-channel state receipt/connection generations and capacity; assess fixed-SHA deployment, recovery/rollback and a newly dated observation manifest from actual evidence. Technical pilot success is not simultaneous-model proof or economic validation.
- [ ] DO: Commit and push to the isolated feature branch, create a reviewable PR, wait for final-head CI, and report its URL/SHA. Do not merge, deploy or enable paid comparison. The user will return to Codex for independent review.

## Final report required from Claude

1. Task1–6 table: implemented / tested / unresolved, with exact blockers; no generic all-done claim.
2. Base SHA, final head SHA, PR URL, changed files and brief process diagram.
3. Reuse vs new behavior, user-visible trading behavior changes (expected none), compatibility/source-version decisions and remaining risks.
4. Exact fresh test/CI evidence, including operating-state/external-network access attempts.
5. Requested/observed development models and effort; application paid LLM/broker calls0, runtime changes0 (or explicitly disclose any deviation).
6. OFF experiment proposal with pairs/attempt/USD bounds and calculations; do not claim readiness if a required gate is unknown.
7. October14 actions versus work still possible offline, plus unresolved historical account/baseline matters.
8. Explicit statement that merge/deploy/activation await the user's subsequent Codex review; preserve original production/kill-switch/monthly baseline/pilot.

## Scope explicitly deferred

Real collection and pilot acceptance depend on October14 market data. Production deployment/activation, application paid calls and order-policy changes are outside today's development. Historical account income/starting NAV/external flows and comparable KODEX200/semiconductor-excluded benchmarks remain separate work; do not expand this PR into broker history collection or new strategy tuning. Policy migration between different experiment configurations is not a prerequisite for the first immutable experiment: preserve existing records and document unsupported migration rather than adding a reset escape hatch.
