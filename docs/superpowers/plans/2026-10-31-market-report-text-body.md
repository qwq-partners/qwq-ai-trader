# Market report text body implementation plan

> For agentic workers: use the existing isolated worktree and Plan → Do → See evidence in the execution roadmap. Coordinator integrates; implementation and final review are separate.

**Goal:** Deliver the complete market research report as selectable Telegram text. Images are for actual charts/diagrams, never a rasterized report body.

**Architecture:** Keep the public evidence capture, single LLM analysis, frozen body, slot claim and terminal delivery receipts. Remove routine body-to-image rendering from ReportSchedule and use its existing single sendMessage path. Suppress URL previews in this routine text transport so source links do not introduce unrelated images. Preserve stored historical image receipts and standalone chart/photo support.

**Tech Stack:** Existing Python scheduler, SQLite receipt store, aiohttp Telegram transport and isolated pytest fixtures. No dependencies or configuration changes.

**Spec:** User's 10/31 screenshot correction and [R02-TEXT](../../operations/execution-roadmap.md#r02-text-body-20261031).

## Global constraints

- Existing channel, 07:40/13:10/16:40 slots, individual 21:00 digest and important immediate alerts stay unchanged.
- Existing 3000 characters/60 lines and UTF-16 transport validation, news/disclosure provenance, model/budget and kill switch stay unchanged.
- No extra Telegram request, trial send, LLM/provider call, replay of consumed slots or deletion/rewrite of historical media.
- Development never reads production credentials, account data, logs or caches. Only the coordinator owns authorized operational checks/deployment.

## Review focus

- Research success and factual fallback both reach sendMessage with the complete validated body.
- Morning archive contains the actual full text, with no new image/caption substitution.
- Rejected/unknown/consumed slots do not create a second request or model call; prior image receipts stay consumed and readable.
- HTML formatting and all source URLs/limitations survive; URL previews are disabled only for routine reports.
- Other chart/photo transports and personal/immediate alerts preserve their contracts.

### Task 1: Text transport and regression evidence

**Files:** `src/schedulers/report_scheduler.py`, `src/utils/telegram.py`, relevant scheduler/research/delivery tests.

**Interfaces:** `prepare_market_report` returns the same final body; `publish_report` receives that body without photo/caption. No schema changes.

- [x] Update/add focused regressions for selectable full text, failed analysis fallback, archived morning text and no replay; observe failure against the current image route.
- [x] Remove routine body rasterization, preserve historical media support and disable routine link previews.
- [x] Run related tests with the project venv and tests/ explicitly; retain Red/Green output and actual commands.
- [x] Independent review, then run `QWQ_VERIFY_PYTHON=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python bash scripts/dev/verify.sh` in the integration worktree.
- [ ] Update reporting contract and entry docs, commit/push/PR with CI, then normal authorized fixed-SHA deployment only after current schedule/state checks. Never reuse consumed deployment attempts.
- [ ] Record source/operation/receipt evidence and NEXT in the single roadmap. Natural mobile acceptance remains separate.
