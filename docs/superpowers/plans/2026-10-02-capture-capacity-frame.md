# Capture capacity and frame diagnostics implementation plan

> **For agentic workers:** Use superpowers:executing-plans or subagent-driven-development. Root alone integrates; no fan-out.

**Goal:** Complete capacity qualification, frame diagnostics, and available net-profit validation preparation in that order.
**Architecture:** Offline synthetic replay of existing collector/storage first; opt-in bounded frame diagnostics second; reuse existing economic tools third.
**Tech Stack:** Existing Python/asyncio/pytest, no new dependencies.
**Spec:** [설계](../specs/2026-10-02-capture-capacity-frame-design.md).

## T1 — capacity (root)

Root owns new src/analytics/toss_capture_capacity.py, scripts/qualify_toss_capture_capacity.py, tests/test_toss_capture_capacity.py. Terra/high dispatch and existing Sol/high followup could not start due to runtime thread limits, not model unavailability. No changes to existing collector or authority limits.

- [x] RED: synthetic real receiver→collector→artifact roundtrip, caps, time boundaries, finite inputs, hostile inputs, economic non-promotion.
- [x] Implement bounded generator/clock/socket and fixed qualification cases/CLI.
- [x] GREEN focused tests, run full synthetic suite and inspect results before T2 implementation.

## T2 — KIS frame diagnostics (separate Astra/high writer after T1)

- [x] Freeze explicit opt-in schema and tests; preserve existing collector/coordinator behavior.
- [x] RED then implement raw-free classifications, persistence and strict offline aggregate report.
- [x] GREEN original/new journal, frame handling and no-change control flow checks.

## T3 — net-profit preparation (root; Sol/high read-only pre-audit)

The audit found no existing actual account net-gain/TWR calculator. After T2 implement the bounded pure calculator and explicit-file CLI in [42차 계획](2026-10-02-account-net-return.md); do not duplicate the execution/entry/exit tools.

- [x] Inspect existing CLI/input contracts and actual available sanitized evidence.
- [x] Run feasible synthetic complete flows; record concrete actual-input blockers and one next testable improvement hypothesis.
- [x] Do not substitute synthetic returns for account performance or retroactively complete old pilot.

## See — independent integration

- [x] Inspect diffs, resolve independent Astra/xhigh findings, run combined/full tests and secrets.
- [x] Update CLAUDE/CHANGELOG/MEMORY/docs index/flow/checklist/PDS with results and remaining boundaries.
PR과 exact-head CI·merge 영수증은 해당 PR 본문에 기록한다(자기 참조 SHA 방지). 운영5468208/override를 보존한다.
