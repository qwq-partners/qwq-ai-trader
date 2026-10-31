# R02-VALIDATE Implementation Plan

> **For agentic workers:** Use the assigned isolated implementation worktree; coordinator owns integration, documentation, deployment and the authorized single send.

**Goal:** Fix the reproduced rejection of a legitimate source-backed market report, preserve unsupported-content checks, and deliver one validated analyst report after verification and deployment.

**Architecture:** Keep the existing frozen-input, one-call runner. Recognize only the generic term ETF when the renderer's ETF section contains actual observations. Add fixed validation codes/field paths without storing provider prose in routine failure metadata. Keep all model, budget, delivery and trading settings.

**Tech Stack:** Python, pytest, existing ReportStore/LLMManager and canonical deployment scripts.

**Spec / authorization:** [R02-VALIDATE in the execution ledger](../../operations/execution-roadmap.md#r02-validation-rejection-20261101); user explicitly requested diagnosis, fix, verification, commit/push/deployment and one normal Telegram report. No new product-format decision is required.

## Constraints and review focus

- No operating-checkout development, new orders, buy resume, model/profile changes, scheduled-slot replay or automatic model retry.
- Pre-push local verification means the user's Windows WSL checkout. AWS worktree checks are auxiliary evidence only. Retain GitHub's required verify check; do not request AWS-direct execution as the default substitute when WSL access is unavailable.
- Generic ETF vocabulary is conditional on a recognized populated ETF section, never arbitrary news/heading text. Other Latin tokens, dates and numbers remain citation-local; numeric spelling is exact.
- Validation diagnostics contain allowlisted codes and static paths only, never raw response, offending text or account/credential material.
- Existing cancellation, settlement, abandoned-attempt and fallback semantics remain intact.
- The lost 00:26 response cannot be assigned an exact cause retrospectively. The captured 01:57 response is the reproducible evidence.

## Task 1 — Reproduction and bounded implementation

Files: `src/analytics/market_report_analysis.py`, `src/analytics/market_report_runner.py`, focused tests.

- [x] Capture one authorized diagnostic response privately; replay it offline without rewriting it.
- [x] Identify the three rejected ETF fields and the prompt/validator mismatch.
- [x] Write failing synthetic regressions for populated/absent/spoofed ETF sections, unrelated Latin terms, exact numeric/date constraints and sanitized runner diagnostics.
- [x] Implement the narrow context exception, explicit prompt contract and safe failure diagnostics; advance the prompt version.
- [x] Pass focused tests and inspect unchanged captured-response composition without another paid call.

## Task 2 — Independent review and integration

- [x] Independent source review, address findings, then run serial full AWS auxiliary verification with `QWQ_VERIFY_PYTHON=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python bash scripts/dev/verify.sh`, with no external API/production credentials. This did not satisfy user WSL verification.
- [x] Inspect source diff, syntax/secrets/isolation evidence; update reporting contract and ledger; commit/push, PR CI, merge and main CI for PR #319.
- [x] Verify these WSL handoff documentation corrections in actual WSL; integrate them with the snapshot follow-up PR after the full pass.

## Task 2a — WSL-discovered snapshot integrity blocker

Actual WSL full verification of d896a37a failed one same-size mutation test (10,030 passed; isolation violations zero). An instrumented synthetic probe confirmed 18/20 overwrites retained identical size/mtime/ctime and bypassed metadata-only detection.

- [x] Add deterministic identical-metadata mutation regressions and observe RED (6 failed / 14 passed).
- [x] Compare a bounded second content pass on the same descriptor; preserve regular-file, size, EOF and metadata checks, with no retries/sleeps. Do not claim atomicity or complete ABA protection.
- [x] Independent actual Claude Opus 5.5/high review APPROVE and focused GREEN (20 passed, isolation zero).
- [x] Actual WSL serial full verification: 10,040 passed / 2 skipped / 2 xfailed, 366.27s test / 374.39s total, isolation zero, syntax/secrets PASS.
- [x] Source/doc commit/push from actual WSL; PR #320 and merged cfc9bc20 main CI each passed 10,040 tests with isolation zero.
- [x] Bind the resulting fixed SHA and reassess the real deployment window before collecting a fresh baseline; no early reservation or timeout of canonical rollback.

## Task 3 — Deployment and one report

- [x] Resume in the user's WSL checkout, run serial full verification and bind the reviewed main SHA with fresh operating/schedule/protected-state checks. The proposed AWS-direct exception was withdrawn after the user's clarification.
- [x] Use the canonical full-verification/rollback deploy path; accept only after at least 150 seconds and protected-state/broker/recovery checks.
- [x] Bind and offline-revalidate the already-generated frozen `ready/validated` response against the deployed code without another model call. The single sender preserves the actual output, labels the input date and refuses factual fallback.
- [x] Send once to the existing market channel, recording API acceptance separately from mobile reception/content acceptance. Update ledger/NEXT and deployment receipt; documentation-only follow-up does not trigger another deployment.

## Final operating receipt — 2026-11-01 KST

- Actual WSL full verification, PR #320 CI and merged-main CI: each 10,040 passed / 2 skipped / 2 xfailed, isolation zero. WSL initiated canonical fixed-SHA deployment of cfc9bc20; server full verification also passed 10,040 tests, rc0, no rollback.
- Restart04:18:41, PID4157369; canonical postcheck9/9. Initial acceptance30/31 retained unchanged: one protected versioned script had an approved comment-only change. Independent Astra/high reviewed exact source-hash/receipt-lineage reconciliation; synthetic gate22 and sender13 tests passed. Final04:35:11 acceptance32/32 at uptime988.61s:47files unchanged, one approved source artifact matches target. Settings, buy stop, cron, report store, evolution and expired T04 state preserved.
- One text report accepted by Telegram04:35:20; archived10/30 input label, no image, no scheduled-slot replay, no additional generation during sending. Actual mobile reception/content acceptance and natural output remain WAIT.
- Completion documentation only; no repeat deployment. Existing O02/NEXT and engine evidence constraints follow the execution ledger. The previous high-volume close timeout remains unexplained.
