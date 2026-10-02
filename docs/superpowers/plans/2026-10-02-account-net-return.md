# Account net return implementation plan

> **For agentic workers:** Use superpowers:executing-plans or subagent-driven-development. Root alone integrates; no fan-out.

**Goal:** Calculate provisional account KRW gain and exact cash-flow-boundary TWR from explicitly supplied complete evidence, without inventing missing account data.
**Architecture:** Pure Decimal report plus bounded explicit-file CLI. Optional matched benchmark portfolios use the same validation. No broker/provider dependencies.
**Tech Stack:** Python standard library and existing strict JSON decoder, pytest.
**Spec:** [42차 설계](../specs/2026-10-02-account-net-return-design.md).

Prerequisite: finish 40차 capacity and 41차 frame diagnostics before implementing this task. Root owns new src/analytics/account_net_return.py, scripts/report_account_net_return.py, tests/test_account_net_return.py. No existing trading/portfolio policy changes.

- [x] RED: independently calculated cash-flow/fee/dividend examples, incomplete/unknown boundary, causality/duplicate/contradiction, benchmark mismatch, bounded CLI read.
- [x] Implement strict schema, Decimal net gain/TWR and optional matched portfolio benchmark gaps.
- [x] GREEN: focused and existing reconciliation/entry/exit tests; synthetic CLI roundtrip. Actual account values remain uncomputed pending sources.
- [x] Independent final review, full tests/secrets, docs and local integration together with 40·41차. PR/CI receipt is recorded in the PR body.

Reporting explicitly distinguishes net KRW gain after outside operating costs, TWR after costs already embedded in the account, source authenticity and engine attribution. Missing benchmark or semiconductor-excluded evidence stays null/unavailable. Do not reuse internal daily_pnl_pct as verified account total return.
