# Market report analyst prose implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development for the bounded content task. The coordinator integrates and owns operational actions; an independent reviewer approves the diff and examples.

**Goal:** Present a useful market argument and concrete follow-up conditions in readable Korean prose, with compact sources instead of internal evidence codes and repeated collection diagnostics.

**Architecture:** Keep the existing research JSON schema, evidence validation and frozen source/result storage. Improve the research prompt and pure reader-facing composition in `market_report_analysis.py`; no new provider, calls, scheduler/state/schema or transport changes. Existing user authorization covers follow-up development, validation, integration and normal deployment, so the established execution method continues.

**Tech Stack:** Existing Python pure formatter/validator, Telegram HTML text, isolated pytest fixtures.

**Spec:** [R02-INSIGHT in the execution roadmap](../../operations/execution-roadmap.md#r02-analyst-prose-20261031), following the user's explicit feedback after the 20:24 text trial.

## Design and constraints

- Lead with a ranked thesis: what changed, why it matters for earnings/demand/cost, and what would change the interpretation. Avoid generic “재점검이 필요한 구간” conclusions, repetitive headlines and unsupported price triggers.
- Keep `thesis` and `issues`/`event`/`impact`/`counterpoint`/`watch`/`evidence_ids` unchanged internally. The prompt asks for conditional mechanisms and grounded catalysts; declining prices are not causal proof and overlapping articles are not independent confirmation.
- Reader output: title/date → market thesis → up to two short issue sections with explanatory prose and `다음 확인` → market observations → compact source links and material basis notes. Do not print E/F IDs, `LLM 해석`, or four repeated field labels.
- Source links use original validated URLs and escaped, identifiable titles/providers. Preserve each source's actual publication/collection distinction, dated filings/observations/events, and title/index-only scope. Shared notes may be consolidated only with explicit scope. Do not fabricate publishers from portal URLs.
- Keep full coverage/status/limitations in the prompt and frozen audit. Do not dump collector status rows into reader prose. Retain material uncertainty as readable Korean; never turn collection time into publication time, index titles into filing contents, or timed prices into closing prices.
- Existing numeric market facts/dates and ETF sample scope remain. Rephrase only recognized boilerplate, never delete arbitrary source qualifications or model prose by keyword.
- When research cannot be used, clearly identify a factual market summary without inventing a failure cause. Historical price-only compatibility stays intact.
- Keep 3,000 characters/60 lines/4,096 UTF-16 units, source validation, one logical model call and one text send. No model/budget/configuration changes, additional Telegram test, paid model experiment, real order, buy resume, new schedule or observation extension.

## Review focus

- Missing publication/provider, mixed dates and title-only filings: compact labels must still tell the reader which source/date supports the assertion.
- Unsafe/quote-containing URLs, entity-encoded titles and duplicate articles: preserve escaping and avoid creating corroboration by presentation.
- A polished but unsupported interpretation: retain counterevidence, avoid invented technical triggers, and distinguish offline authored examples from actual model results.
- Long/wide research inputs and failed analysis: full text bounds, factual fallback and downstream delivery validation remain intact.
- Old research image tests: retain historical parser/receipt coverage with explicit legacy fixtures; new text output does not need to become an image.

## Task 1 — Pure research prompt and presentation

**Owner:** Terra6.1/high in a separate clean worktree at base `cadac660174074bb1687b3fb30e78a96abe69f20`. Root owns docs/integration. Independent Sol6.1/high reviews content and code; native actual/effective metadata is unexposed.

**Files:** `src/analytics/market_report_analysis.py`; relevant research content/E2E/runner/validation tests and a narrowly named legacy fixture if needed. No scheduler, transport, storage or model configuration edits.

**Interfaces:** Existing `build_analysis_prompt`, `compose_analysis`, `factual_fallback`; validated analysis object and raw frozen evidence remain unchanged. Bump the prompt version to distinguish the revised instruction/presentation contract.

- [ ] Write behavioral RED checks for reader-visible IDs/status noise absent, internal IDs/result unchanged, compact safe sources with mixed provenance, grounded insight instructions and readable factual fallback.
- [ ] Implement the bounded prompt/composition change and restore GREEN, including legacy image coverage without adapting the image renderer to new report bodies.
- [ ] Run related isolated tests and commit; hand off exact commands, RED/GREEN output and limitations. No full-suite duplication by worker.

## Task 2 — Independent review and integration

- [ ] Root regenerates an offline example from the already reviewed public 10/30 snapshot using the new composer; no live API/model calls. Review the actual text and distinguish it from natural LLM output.
- [ ] Independent review covers the full diff, internal evidence preservation, source/time truthfulness and the strength of the example's argument. Resolve findings before integration.
- [ ] Run `QWQ_VERIFY_PYTHON=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python bash scripts/dev/verify.sh` in the integration worktree, inspect secrets/diff, commit/push/PR and inspect CI.
- [ ] Recheck current operating SHA/PID/settings and 21:00/22:00 schedules before preparing a new fixed-SHA deployment; never reuse consumed wrappers. Independent critical gate review and normal verify/restart/rollback/150-second acceptance are required.
- [ ] Record final operation/review/CI/NEXT in the roadmap. Natural LLM insight and mobile acceptance remain WAIT; no automatic additional trial.
