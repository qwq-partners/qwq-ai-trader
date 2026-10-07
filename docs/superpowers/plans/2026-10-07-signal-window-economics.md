# Signal Window Economics Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans for implementation; the coordinator owns integration and a separate reviewer checks the whole change.

**Goal:** Capture later real signals and their capital/quote evidence without changing trading decisions.

**Architecture:** Extend the opt-in observer with a bounded scan window and signal-triggered KIS quote demand. Reuse existing journal, capital, markout and cost evaluators; retain old first-scan contracts.

**Tech Stack:** Python/asyncio, pytest, existing Decimal/FeeCalculator.

**Spec:** `docs/research/current-engine-signal-window-2026-10-07.md`.

## Global Constraints

- `runner-signal-window-v1`, `max_scans` 1~100, `quote_admission=signal_created`.
- Full returned-candidate denominator, explicit incomplete/missing evidence, old v1~v4 compatibility.
- No operational access, installation, restart, orders, credentials, policy or sizing changes.
- Base `252df9cb11de4543c72667b0a2b2e12cbc11411f`; coordinator sole writer.

## Review Focus

- Second scan repeats a symbol: distinct identity, trace and signal linkage.
- Empty/failed scan and cap exhaustion: no silent replacement or complete result.
- Async subscription failure/end race: unchanged emit result and no renewed lease.
- Forged/mixed window population or source/policy references: rejected offline.
- Late order/first quote/missing markout: no invented price or zero profit.

### Task 1: Bound collection and signal quote admission

**Files:** `src/analytics/entry_observation.py`, `entry_observation_runtime.py`, `entry_gate_trace.py`, `src/data/feeds/quote_subscription.py`; new `tests/test_signal_window_observation.py`.

**Interfaces:** Buffer `scan_scope='window'`, `max_scans`; runtime new capture version with mandatory capital/markout; signal callback registered only by runtime, omitted in old modes. Each signal requests its original scan/symbol through existing coordinator.

- [x] Add tests for time/cap/empty scans, two linked signals, unrelated orders, end/failure, invalid plan.
- [x] Run tests and verify expected failures, then implement minimal observer changes.
- [x] Prove legacy first-scan and default-off behavior still pass.

### Task 2: Preserve downstream evidence and economics

**Files:** `src/analytics/entry_gate_report.py`, `kis_frame_diagnostics.py`, `received_entry_input.py`, `scripts/report_entry_gate_trace.py`; relevant tests.

**Interfaces:** Explicit `window_returned_scan_candidates` population; strict offline study/window binding; per-scan gate validation with one aggregated report. Existing evaluation bundle API unchanged.

- [x] Test repeated symbols across scans, reference mixing, source bounds and time violations.
- [x] Test later-scan economic input preserves the original denominator and missing evidence.
- [x] Implement and run focused tests, full verification and secret scan.
- [x] Independent Astra/xhigh source review; resolve findings; update CLAUDE/CHANGELOG/docs index and create PR #156.

Integration gate: required verify on the final PR head. [PR #156](https://github.com/qwq-partners/qwq-ai-trader/pull/156) is authoritative for CI/merge status.

## Execution record

- Plan: choose signal-triggered subscriptions because scan-triggered leases retain early candidates and exhaust later coverage. Existing evaluators already support multiple candidate IDs; gate reporting and runtime need explicit bounded support.
- Operational activation remains a separate concrete next step after capacity/profile verification.

- Do/See: initial six unsupported-mode failures, metadata-budget two failures and offline-binding two failures reproduced before fixes. Focused405 passed; full verify4479 passed/2knownxfail, syntax/secrets pass, isolation0.
- Reviewer dispatch: source audit Sol/high and independent critical review Astra/xhigh; requested identity only unless runtime exposes actual metadata. No fallback used.

- Final review: two P2 contract gaps reproduced by three failing tests; guards added in Toss consumers and gate CLI. Focused251 passed; independent reviewer ran the three reproducers (3 passed) and approved the fixes. No deferred findings. Final full verification4482 passed/2knownxfail, syntax/secrets pass and isolation0; CI gates integration.
