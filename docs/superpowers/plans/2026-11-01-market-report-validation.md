# R02-VALIDATE Implementation Plan

> **For agentic workers:** Use the assigned isolated implementation worktree; coordinator owns integration, documentation, deployment and the authorized single send.

**Goal:** Fix the reproduced rejection of a legitimate source-backed market report, preserve unsupported-content checks, and deliver one validated analyst report after verification and deployment.

**Architecture:** Keep the existing frozen-input, one-call runner. Recognize only the generic term ETF when the renderer's ETF section contains actual observations. Add fixed validation codes/field paths without storing provider prose in routine failure metadata. Keep all model, budget, delivery and trading settings.

**Tech Stack:** Python, pytest, existing ReportStore/LLMManager and canonical deployment scripts.

**Spec / authorization:** [R02-VALIDATE in the execution ledger](../../operations/execution-roadmap.md#r02-validation-rejection-20261101); user explicitly requested diagnosis, fix, verification, commit/push/deployment and one normal Telegram report. No new product-format decision is required.

## Constraints and review focus

- No operating-checkout development, new orders, buy resume, model/profile changes, scheduled-slot replay or automatic model retry.
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

- [x] Independent source review, address findings, then run the serial full `QWQ_VERIFY_PYTHON=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python bash scripts/dev/verify.sh` with no external API/production credentials.
- [ ] Inspect diff, syntax/secrets/isolation evidence; update reporting contract and ledger; commit/push, PR CI, merge and main CI.

## Task 3 — Deployment and one report

- [ ] Resolve the current host's execution path against the updated WSL deployment rule; bind the reviewed main SHA and fresh operating/schedule/protected-state checks.
- [ ] Use the canonical full-verification/rollback deploy path; accept only after at least 150 seconds and protected-state/broker/recovery checks.
- [ ] Prepare one isolated report using the deployed code and frozen public input. The single sender requires `ready/validated`, preserves the actual model output, labels the input date and refuses factual fallback.
- [ ] Send once to the existing market channel, recording API acceptance separately from mobile reception/content acceptance. Update ledger/NEXT and deployment receipt; documentation-only follow-up does not trigger another deployment.
