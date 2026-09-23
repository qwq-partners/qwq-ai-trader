# N3 final corrective review — call 2 of 2

Independent reviewer, requested claude-opus-5/xhigh. Tools-off only; use supplied
sanitized source. No tools/network/files/commands, no credentials/live data, no fan-out,
permissions/settings/operations changes. Policy ai-routing-v1-2026-09-20 applies.
Absolute600-second deadline, USD5 cap, last call within the N3 two-call budget.
Candidate `881bcec`, product `62410d5`; previous review candidate `93bd744`.
Return concise APPROVE or CHANGES_REQUIRED, only concrete remaining blockers and
bounded follow-ups. Do not re-expand the design or claim tests you did not run.

The first review ran successfully (actual model verified by streamed message and
terminal result, rc0,563.376s,USD1.43796), verdict CHANGES_REQUIRED. Its B1–B3 dispositions:

- B1: expose `mutation_in_flight` from captured lock states. Actual owner code calls
  `_block()` BEFORE awaited SQL commit; version itself is published synchronously
  afterward. Thus the normal in-flight symptom is a latch, not necessarily the presumed
  version gap. Preserve actual observed publication flag, but neutral finding text and
  explicit in-flight flag distinguish a transient barrier from a permanent failure.
- B2: additive `counts_complete` is true only for stable, not-in-flight captures without
  evidence_invalid. False means missing findings are not zero. True covers implemented
  owner diagnostics ONLY, not broker finality/install/A-C history/intraday-source proof.
- B3: unavailable/volatile mode is unknown, not asserted partial installation.

Additional independently reproduced corrections since first packet:

- int1→boolTrue, datetime→same ISO text, Task→codec-shaped tuple are detected by bounded
  private type/scalar evidence; this never enters the snapshot or report.
- Exact ZoneInfo.from_file permits arbitrary key objects. Exact str/None checks and
  scalar budgets now precede any comparison; custom __eq__ must not execute. Native
  reviewer had reproduced this regression and is independently rechecking the fix.

Bounded follow-ups addressed or explicitly disposed:

- F1: exact aware datetime/timezone/ZoneInfo scope retained; unsupported datetime
  subclasses/naive/custom tz fail insufficient by design. Known KST and rejected cases tested.
- F2: depth/node/text/total-text/integer limits tested; aggregate scalar budget includes
  Decimal digits, float hex, derived datetime and ZoneInfo keys.
- F3: real owner/queue full sale removes BOTH position and protection via `_closed`.
  No product change accepting orphan zero-quantity states; actual lifecycle cancel ACK
  stores child ref in command_ref while order_ref remains parent (source supplied).
  Actual normal pending protection quote has matching pending owner/audit/episode/intent.
  It may still report incomplete evidence: request-bound SELL stores planned_risk=None.
  That is intentionally not measured zero. It is pending, not a broken protection link;
  evidence_invalid/unsupported and counts_complete=False reflect missing risk measurement.
- F4: actual `_skip_reason` and `_apply_outcome` increment int counters; nonempty paths tested.
- F5: arbitrary2**32 cap rejected as covert-channel mitigation. Any allowed count/time
  field can encode data if a malicious caller fabricates it. This module guarantees fixed
  projection/no raw identifying fields for genuine capture, not authentication of invented
  snapshots. Keep finite256-bit bound and output all authority flagsFalse. Do not promote
  this limited threat model into a claim of covert-channel resistance.
- F6a: intraday_preemptive remains outside the GENERAL producer classifier; separate
  consumer/validation is explicitly deferred. F6b: absent MARKET_DATA key now supports
  partial capture rather than hiding otherwise-readable owner evidence.

Coordinator fresh diagnostic suite:120passed7.28s/rc0/isolation0. Full UTC/KST suite
has NOT yet run. The final native review is independent and in progress; don't assume
approval. Current report always readonlyTrue, trading_ready/automatic_action_allowed/
installation_verifiedFalse. No operational consumers exist.

Review current full product and supplied tests/excerpts, prioritizing corrected boundaries
and regressions. Avoid requiring live API proof for this offline module-only scope.
