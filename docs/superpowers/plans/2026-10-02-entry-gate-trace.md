# Entry gate trace implementation plan

> **For agentic workers:** Use superpowers:executing-plans or subagent-driven-development for assigned tasks. Root alone integrates; no worker fan-out.

**Goal:** Record the actually evaluated live-screening gates without changing trading behavior.
**Architecture:** Explicit v3 capture enables bounded per-candidate traces; existing journal carries scalar-only rows; offline report preserves all candidates and missing evidence.
**Tech Stack:** Existing Python/asyncio/pytest; no new dependencies.
**Spec:** [39차 설계](../specs/2026-10-02-entry-gate-trace-design.md).

## Global constraints

- Base dafd4766a55868b6456c0ea9a9796791a95985fc; distinct worktrees/file ownership.
- No API/credentials/runtime files/services/orders/deploy/restart. Synthetic tests only, explicit tests/ paths.
- v1/v2 unchanged; v3 explicit and disabled by default; no new schedule or activation.
- Max100 candidates,31 fixed ordered stages; no outcome/PnL/approval inference.

## Review focus

- Observer failure or serialization must not suppress/duplicate a signal.
- Short-circuit not-reached conditions must not be reported as evaluated failures.
- Batch/emit/cancellation/observation-window termination must retain the original denominator.
- Future timestamps, duplicate steps and contradictory terminal states must not survive strict reporting.
- Old studies and separate trading routes must not inherit new instrumentation or eligibility.

## Task 1 — contract, bounded trace and capture wiring (root)

Files: new `src/analytics/entry_gate_trace.py`; modify `entry_observation.py`, `entry_observation_runtime.py`, `entry_observation_journal.py`; new `tests/test_entry_gate_trace.py`.

Interfaces: exactly the trace API/settings in spec; journal `steps` allowlist; v3 binds source/config refs and reserves scan+selection+trace burst.

- [x] RED: optional disabled trace, actual ordered fail/unknown, invalid/duplicate/oversized input, publish failure, first-cohort membership and late close.
- [x] Implement bounded trace and v3 manifest/buffer/journal connection with scalar-only recording.
- [x] GREEN: focused trace/runtime/journal/selection tests and synthetic persistence roundtrip.

## Task 2 — actual scheduler connection (Astra/high writer)

Files: `src/schedulers/kr_scheduler.py`; new `tests/test_entry_gate_scheduler.py`; adjust existing AST fixture globals only if necessary in `tests/test_entry_observation.py`.

Consumes `begin_gate_trace`, `check/note/stop/signal/finish` from spec. No threshold/selection/REST/engine behavior changes.

- [x] RED: real scheduler synthetic cycle for first-cause evidence and observer enabled/disabled/failing equivalence.
- [x] Instrument existing actual branches, preserve short-circuit order and route scope, close on all exits including cancellation.
- [x] GREEN: global/candidate/strategy/error/count cap paths, emitted event and call/counter equivalence.

## Task 3 — offline explanation (root)

Terra/high dispatch and a subsequent Sol/high handoff failed at the runtime thread limit before implementation. Root completed this task; no Terra/Sol implementation result or model-unavailability claim is recorded.

Files: new `src/analytics/entry_gate_report.py`, `scripts/report_entry_gate_trace.py`, `tests/test_entry_gate_report.py`.

Produces `build_gate_report(observations, *, as_of)` with candidate/stage counts, incomplete reasons, first observed fail, no economic values. CLI takes explicit journal/study/as-of and max byte bound; uses existing strict journal reader and original study hash.

- [x] RED: legacy unknown, causal trace report, contradictory/future/duplicate/orphan rejection.
- [x] Implement pure report and offline CLI; preserve original population even with missing traces/overflow.
- [x] GREEN: strict synthetic journal→CLI and hostile/partial records.

## Task 4 — See and integration (root + independent reviewer)

- [x] Inspect each patch before integration; run combined relevant tests.
- [x] Independent Astra/xhigh final review, resolve concrete findings.
- [x] Full `scripts/dev/verify.sh` with project venv, syntax/secrets/isolation and doc links.
- [x] Update CLAUDE/CHANGELOG/MEMORY/docs index/architecture/checklist/PDS with evidence and remaining capture/account work.
Integration follows these local checks: PR + exact-head CI + merge; the GitHub PR records head/CI/merge receipts. Preserve operating checkout/config. Profitability and deployment remain unverified.
