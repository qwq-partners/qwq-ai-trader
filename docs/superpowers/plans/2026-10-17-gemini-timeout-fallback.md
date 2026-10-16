# Gemini timeout fallback implementation plan

> **For agentic workers:** Use test-driven development and independent critical review; the coordinator integrates the two isolated worktrees.

**Goal:** Let a future, bounded Gemini timeout use the configured OpenAI fallback without inventing usage or discarding possible charges.

**Architecture:** Before a supported plain-text request, persist a published-rate ceiling. A transport timeout retains that ceiling as an uncertain budget hold with actual cost null. A fallback obtains its own atomic reservation against the remaining shared monthly budget.

**Tech stack:** Python asyncio/aiohttp, SQLite, pytest. Base `8d5e033b`.

**Spec:** The uncertainty-accounting proposal in [the October 19 operations record](../../operations/selection-capture-2026-10-19.md), narrowed to the exact stable `gemini-3.1-flash-lite` Standard single-candidate text request. Official references and verification go in the completion report.

## Constraints and review focus

- No live calls, operational ledger repair, deployment, restart, orders, or capture changes. The October 16 legacy unknown remains unbounded.
- Monthly USD100 and 80/95/100 policy still count every known charge, live reservation and retained uncertainty. Holds survive restart and month rollover.
- Only an explicitly classified transport timeout can acquire a hold; cancellation, orphans, malformed usage, storage errors, unsupported models and refusals keep their existing safety boundary.
- Complete-role and complete-with remain fixed-provider contracts. Task routing may use one existing fallback; no additional JSON repair or shadow after a bounded timeout chain.
- Historical preview prices remain labeled estimates for reporting, but new preview admission is rejected. Generic defaults migrate to the stable model.

## Task 1 — durable uncertain holds (isolated Astra/high worker)

Files: `src/utils/llm_budget.py`, new `src/utils/llm_budget_bounds.py`, new `tests/test_llm_timeout_holds.py`.

- [x] RED: real temporary SQLite tests for held null cost, fallback admission, insufficient/concurrent budget, restart/month carry, late settlement/breach, malformed certificate, unsupported/legacy/orphan cases.
- [x] DO: `reserve(..., timeout_hold_contract=None)` validates `GEMINI_TIMEOUT_CONTRACT` and records its ceiling transactionally; `settle(outcome='timeout')` returns a float only for a durably held certified timeout, otherwise None.
- [x] SEE: expose total/bounded/unbounded unresolved counts and `uncertain_cost_ceiling_usd`; verify all existing budget tests and provide commit/test evidence.

## Task 2 — manager and transport (coordinator)

Files: `src/utils/llm.py`, `config/default.yml`, transport/integration tests and current comparison fixtures.

- [x] RED: real transport with fake HTTP timeout and real temporary budget must complete via OpenAI while Gemini actual cost stays null; no extra JSON retry; fixed-provider behavior, unknown/cancel/failure boundaries and daily hold accounting remain honest.
- [x] DO: classify timeout explicitly, request a certificate only for the reviewed native Gemini envelope, distinguish bounded from unbounded daily uncertainty and add hold metadata to the attempt trail/audit. Block retired model before HTTP. Use stable defaults.
- [x] SEE: inspect combined diff, run related tests and full verify, secret/syntax checks, independent Astra/xhigh review. Record requested model/effort with actual metadata unverified.

## Integration and operational follow-up

- [x] Prepare current state, changelog, integration documentation and reviewed source for a committed, pushed PR; external CI status is recorded on the PR.
- [ ] Keep deployment separate: new code cannot infer a certificate for an old request. Existing uncertainty needs a separately evidenced conservative budget treatment; a fallback success is never retrospective Gemini settlement.
