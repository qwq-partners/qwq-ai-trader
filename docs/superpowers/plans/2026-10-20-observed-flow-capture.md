# October 21 observed-flow capture implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Complete the tasks with independent review.

**Goal:** Prepare one prospective capture that preserves the original strict report and adds the predeclared trade-observed quote diagnostic, so candidate selection and entry timing can be compared.

**Architecture:** Extend the existing fixed-date installer/activation boundary with a distinct October 21 profile. Retain the October 20 files and results unchanged. Add a separate version to the pinned, offline readout runner; no trading rule or model changes.

**Tech Stack:** Python, pytest, existing systemd one-shot artifact contracts.

**Spec:** [existing installation contract](../../operations/pregate-capture-2026-10-20.md) and [observed-flow policy](../../research/pregate-readout-2026-10-20.md).

## Global constraints

- Base SHA: `a3d1f3eed3e1f1867a90a589b725b276d4aec0ff`; isolated feature branches, one writer per file.
- Target profile `20261021-pregate-v1`, epoch `2026-10-21-engine-pregate-v1`. Calendar confirmation remains a separate installation prerequisite.
- Install before 09:20 KST; activation [09:25,09:27); first returned scan [09:30,09:35); end 09:51. Empty first scan is not replaced.
- Same first-scan/top-5/100 metadata candidates, 41-channel ceiling, 10-channel headroom, record/memory/disk limits. Lease 1262 seconds covers 1260 seconds plus teardown margin.
- Explicit top-level `observed_regular_flow_policy`: version `pregate-observed-regular-flow-v1`, actual prospective `fixed_at`, preparation 5 seconds, state age 5 seconds, horizon 900 seconds, endpoint delay 5 seconds. No retrospective policy insertion.
- Only exactly consumed October 20 evidence may retire its drop-in; original journals and receipts remain.
- No orders, kill-switch removal, external APIs or credentials in tests, new paid LLM calls, deployment/restart/installation in the development tasks.
- Implementation, merging, deployment, installation, scheduling and actual collection are distinct states.

## Review focus

- Wrong old profile or changed file identity must prevent retirement; failed installation must disarm its own timer.
- October 20 readiness must not satisfy October 21 activation; source/config/input hashes remain pinned.
- Missing/malformed or future-to-activation observed policy must fail before timer arming.
- Tail/lease must cover the latest eligible candidate without extending the fixed preparation deadline.
- Readout manifests cannot silently change old profiles or label diagnostic pairs as fillable/profitable.

### Task 1: Fixed capture profile and transaction

**Files:** New `scripts/ops/entry_capture_prepare_20261021.py`; modify `scripts/ops/entry_capture_activate.py`; new `tests/test_entry_capture_prepare_20261021.py`, `tests/test_entry_capture_activate_20261021.py`.

**Interfaces:** Preserve public installer/activation functions and historical behavior. New activation profile returns exact paths/times; new installer requires the explicit observed-flow policy and exact capacity contract.

- [x] Add failing fixed-date, policy, prior-profile, deadline and transaction tests.
- [x] Implement the minimum isolated profile extension, retaining existing transaction protections.
- [x] Run new and historical installer/activation suites with synthetic host operations.
- [x] Commit the bounded change and provide actual test evidence.

### Task 2: Offline readout connection

**Files:** `scripts/ops/run_capture_readout.py`, `tests/test_pregate_readout_runner.py` or a new dedicated test file.

**Interfaces:** Add manifest version `deterministic-observed-flow-readout-v1` and alias `observed-flow`; invoke `report_pregate_observed_regular_flow.py` with the same pinned study, journal, as-of and bounded input handling. Legacy/pregate manifest versions retain their exact commands and meaning.

- [x] Add failing tests for source inventory, commands, binding/invariants and successful/invalid synthetic output.
- [x] Implement the distinct profile and conservative summary counters.
- [x] Run existing and new runner tests; no calendar or live data calls.
- [x] Verify the new fixed study loads in the existing runtime and reaches the diagnostic using synthetic data.

### Task 3: Integration, resource check and handoff

**Files:** New `docs/operations/pregate-capture-2026-10-21.md`; project status documents.

- [x] Inspect both diffs; run focused integration and capacity checks for the exact new window.
- [x] Obtain independent critical review (Astra/xhigh requested; actual metadata recorded only if exposed).
- [x] Run full verify and secret scan; resolve failures and run appropriate final checks.
- [x] Update documents with prepared/installed distinctions, concrete deployment prerequisites, readout commands and review/test evidence.
- [ ] Commit, push, attach PR, verify CI and integrate the reviewed tree.

Production application is a separate final step after concrete artifacts and prerequisites are reviewable; this plan itself does not claim it happened.
