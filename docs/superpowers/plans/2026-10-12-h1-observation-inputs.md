# H1 observation input preparation implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Follow the checkboxes in order.

**Goal:** Prepare a reviewable, deterministic twenty-date H1 input package without installing or scheduling it.

**Architecture:** An offline CLI accepts one explicit study template, calendar and destination roots. A library produces exact study/manifest/once bytes, their hashes, a proposed protocol and planned-day index. It always labels the output preparation only: neither source authenticity nor installation readiness is established by generating JSON.

**Tech Stack:** Existing Python standard library, CapturePlan/H1 validation and pytest; no additional dependencies.

**Spec:** [H1 contract](../../research/h1-entry-price-contract-2026-10-11.md) and [66차 evaluation contract](../../research/h1-multiday-capital-2026-10-12.md).

## Global constraints

- Base `99a9a307284d0034a5b8512cc16bd341e02f04e1`; ordinary offline implementation, no changes to live modules or root installers.
- Exact20 sorted unique weekdays, actual preparation time strictly before first-date midnight KST. Calendar authenticity remains a separately reviewed assertion.
- `runner-order-ready-window-v1`, first5,09:00/10:05/10:21 KST,14 scans; qualified buffer400000/queue8192/batch256/512MiB/64KiB; channels41/external0/headroom10/lease5100.
- Freshness1/5/3/30 seconds, horizon900/delay5, RR1.5 and0/10/30bps; all dates share one effective policy hash and epoch.
- Explicit absolute destination roots with no traversal; distinct study, manifest, once, journal and receipt paths per date. No production-state discovery, broker access, installation, scheduling or file overwrites.
- The coordinator owns documentation/integration. One Terra/high worker owns new implementation/tests in a separate worktree; Sol/high independently reviews. Actual model/effort is unverified unless exposed. No worker fanout; 25-minute implementation bound.

## Review focus

1. Backdated/future preparation timestamps or a date already begun must not become valid prospective registration.
2. A synthetic study, old signal-window profile or wrong cost/freshness policy must not silently become an observed H1 study.
3. Calendar formatting and SHA cannot prove market opening, fees, channels or session availability.
4. Generated planned-day paths are not captured days; quality evaluation initially uses `days:[]`, preserving all20 missing dates.
5. File bytes, hashes and normalized policy must agree across every date; no filled-in session review or reused receipt.

## Task1 — Offline package and CLI

**Files:** create `src/analytics/h1_observation_preparation.py`, `scripts/prepare_h1_observation.py`, `tests/test_h1_observation_preparation.py`.

**Interface:** `build_preparation(request, *, prepared_at: datetime) -> dict`. Strict request keys: `protocol_ref`, `study_template`, `calendar`, `input_root`, `state_root`. CLI `--input` reads a bounded explicit regular JSON file and uses the real aware current time; it exposes no timestamp override. Output goes to stdout only.

The template supplies its original per-capture schedule as first-date placeholders and all common policy refs. Require nonempty explicit regime policy: source `entry-gate-trace-v1/regime`, version `h1-gate-regime-policy-v1`, categories `downside_gate_passed`, `downside_gate_blocked`, `unknown`; missing/not-reached/unparseable is unknown. This is a recorded risk-gate grouping, not a bull/sideways/bear classifier. Preserve arbitrary common study metadata in policy hashing; never infer source facts.

Generate canonical newline-terminated JSON for all20 study/manifest/once triplets as `files:[{path,utf8,sha256}]`; target each root's `YYYY-MM-DD/`. Set the two policy fixed points and proposed protocol fixed point to preparation time. Per-day once interval08:55–08:57 KST; journal `engine.jsonl`, receipt `startup.receipt`. Do not emit systemd units or activation configs. Runtime preflight after materialization remains required.

Output includes `status:preparation_only`, `installation_ready:false`, `source_authenticity_verified:false`, `proposed_protocol`, `planned_days`, `evaluation_manifest` with the same protocol, empty days and preparation-time as_of, plus explicit unresolved external checks. Hash all20 policies using the existing normalization; validate the proposed protocol with `_protocol`. Inputs remain unmodified. Enforce the qualified capture profile, declared nonzero observed fees, fixed H1 price contract and explicit regime metadata before generating any success result.

- [x] Write failing tests: missing builder;20 exact byte/hash/CapturePlan round trips; equal-midnight/late,19/21/duplicate/weekend/unsorted dates; wrong version/profile/freshness/fees/regime; unsafe paths; deterministic output and input immutability; empty-day quality evaluation retains20 dates; CLI rejects malformed/duplicate-key/symlink/oversized JSON with no success output.
- [x] Run targeted tests and record expected failure.
- [x] Implement the bounded library/CLI without touching production modules.
- [x] Run targeted tests UTC/KST. Coordinator inspects diff, runs full verify and secrets checks after final source changes.
- [x] Independent review resolves findings; commit only after actual passing evidence.

## Task2 — Concrete preparation and evidence

- [x] Coordinator verifies public calendar/cost sources and identifies official session-history acquisition feasibility; inspect only explicitly needed existing private inputs without exposing them to workers.
- [x] Produce a locally staged package with actual source/config refs, public-fee assumptions clearly labeled, regime rule and candidate20 dates; do not call it a completed preregistration or installation while external checks remain.
- [x] Check all staged study bytes with current CapturePlan and H1 policy normalization, verify old inputs unchanged, retain output hashes and quality-only evidence outside Git.
- [x] Update CLAUDE.md, CHANGELOG.md, docs/README.md and monitoring checklist with PDS, current blockers and the next bounded install/evidence task. Review, commit, push and integrate via PR; no operating checkout change needed for offline preparation.
