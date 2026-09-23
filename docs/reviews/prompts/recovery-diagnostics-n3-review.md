# N3 recovery diagnostics — independent critical review

You are the independent reviewer, not the author. Requested model claude-opus-5,
effort xhigh. Global policy ai-routing-v1-2026-09-20 applies: Plan→Do→See, no fan-out,
no tools/network/file/credential access, no commands executed, no permissions/settings
changes. Only caller-sanitized source supplied after this prompt may be consumed.
Do not request or reconstruct account/credential/live trading state. The runner is
tools-off, has a USD5 cap and an absolute600-second deadline. No deployment authority.

Scope: new `recovery_capture.py` + `recovery_diagnostics.py` and their tests. Existing
runtime/producer/owner/reservation excerpts are context, not newly approved behavior.
No existing runtime writers, senders, health endpoint or installer are to be changed.
Module-only exporter; installation/readiness/automatic recovery remain false.

Review the supplied design contract and actual source against it. Prioritize:

1. Read-only: no health/clock callback, store, audit, recovery, task scheduling, mutation,
   I/O. Two synchronous samples and exact-type private projection must not invoke
   arbitrary Python hooks through comparisons, dict keys, datetimes/tzinfo, deepcopy,
   error rendering or custom objects. Report limitations without claiming atomicity.
2. Stability: owner state/version and RAM/task identity/ingress must be compared, not
   counts alone. Snapshot stability is not publication consistency or install success.
3. Data minimization: no raw private IDs, symbols, account/order refs, prices, decisions,
   source strings, exception text or digest in DTO/report. Fixed whitelist only. Returned
   nested objects must not alias snapshot/live state; forged DTO fields must not leak.
4. Evidence: null/absent effect_source and delivered audit rows, UNKNOWN BUY, cash-only
   remaining reservations, None risk, invalid/bool quantities and malformed money.
   Cancel ACK is not finality, parent/intent/symbol/side/scope links matter. True partial
   SELL must not be falsely marked inconsistent because initial target differs from
   remaining position. Current portfolio/protection remaining must still agree.
5. Scope honesty: runtime absent is unknown not authenticated legacy; apparent complete
   wiring only attached_candidate. A/C dispositions are not durable per-symbol and
   empty RAM does not prove no previous failures. No repair or readiness authorization.

Return verdict APPROVE or CHANGES_REQUIRED, with only actionable findings. For each
finding cite file/function/line or concrete trigger, impact, and minimal regression.
Separate blocking correctness/security findings from bounded follow-ups. Do not invent
test results, actual model evidence, or infer live readiness from model agreement.
