# Independent critical review — 2026-10-16 premarket package

## Verdict

**Installation gate: APPROVE** for the three standalone, dated system-managed timers/services running as User=ubuntu and Group=ubuntu and reviewed code/manifests described below. This is approval of the proposed installation package, not a claim that installation, timer execution, actual role calls, capture completion, price evaluability, or profit has occurred.

- `run_capture_readout.py`: **spec APPROVE; quality APPROVE**.
- `report_premarket_check.py`: **spec APPROVE; quality APPROVE**, after the corrections identified during this review.
- Selection/exit research contract: **APPROVE** after the exact-schema correction verified below.
- Final open findings: **P0 0 / P1 0 / P2 0**. No trading-engine changes, restart, existing capture-timer changes, credentials, or paid calls are authorized by this review.

## Scope and identity

- Requested reviewer: `gpt-6-astra`, xhigh, independent of both authors. Actual model and effective effort metadata were not exposed: **unverified**. No cross-provider assurance is claimed.
- Canonical model-routing policy was read fully; no fan-out.
- Both source worktrees were verified at base `377f0f52fcb9d6fd63af17d7d10ebf3b8556ef77`.
- Final integrated source reviewed at `/home/ubuntu/projects/qwq-ai-trader/.claude/worktrees/codex-recent-work-d9b36c`.
- Read original two review diffs, current scripts/tests, readout operations document, final selection/exit research MD/JSON, relevant producer and health-handler code, implementer handoff, and `/tmp/qwq-premarket-unit-review` with three manifests/six unit files.
- No production/private/env/cache/log/account files, actual audit/capture files, external API/network, systemctl/cron, paid calls, orders, SSH, deployment, or restart were used by this reviewer. The only reviewer-written file is this report. No full test suite was rerun.

## Reviewed source identity

- Readout script SHA-256: `4bc25ea17eb4807d856828d43aed7ad21721dc40327f4f45fd4f76035d6650eb`.
- Final monitor script SHA-256: `0ed198cd58d3de5865348316c971ee6f7cb7ae5a6bb579f42f30017fbf17b144`.
- Readout tests SHA-256: `e3a8b89766d2228a9149f62c72d6cb46193323735c594a6f48fb1ff78b4426f1`.
- Final monitor tests SHA-256: `81d7a602cda2c776b6fe97391f28c68806ccb742c5fe95cabdf07fbd86b66189`.
- Readout operations doc SHA-256: `2dbc57025c7883d6760632c655e3ff3ff33d4c2039b7afe588c6840843af1c05`.
- Full readout source fingerprint independently executed from the integrated source and matched the manifest: `6a23c2e8d17a94e9571a719dac48e2cb931e5ca1037d00f788b7c53e3251837c` (rc0).
- Bound study SHA: `39957565351aaab0ef4c4ce7f8461206118cb0af09719536a0bb7f0686d22024`.

## Readout assessment

The closed manifest admits only fixed inputs/output, study/source hashes, and an aware same-date window. Actual clock is checked before output creation and again after the nonblocking lock. Exclusive, fsynced attempt creation precedes copying and subprocess execution; existing incomplete attempts also prevent rerun. Input and output path components are opened without following symlinks. Existing files are not overwritten. File size/type checks, private ownership/modes, fixed-prefix decision copying, repeated prefix hash, and retained input identities support a reviewable snapshot while keeping decision-recording finality explicitly unverified.

Only the four established offline CLIs execute, sequentially, with fixed arguments, the same timestamp and snapshots, no session review fabrication, no shell, a cleared environment, isolated HOME/TMP, and Python isolation/bytecode controls. Per-child wall/CPU/address-space/output/FD/core limits and process-group timeout termination are present. The enclosing proposed system-managed service running as ubuntu adds whole-cgroup memory/CPU/wall limits and control-group termination.

At `scripts/ops/run_capture_readout.py:368-378`, command rc0/valid JSON is insufficient: strict journal verified/complete/integer drop0 and technical_ready must also pass. The current malformed nested readiness shape fails closed and retains a blocked summary. The unconditional null/false/unverified economic and finality fields are appropriate. Missing decisions, zero checks, or no comparable pairs cannot be read as profit or a complete economic sample.

## Monitor assessment and resolved findings

1. **Resolved P1 — future archive evidence**, `scripts/ops/report_premarket_check.py:103-124` (original diff:96-114). Initially a 07:35 review accepted a generated 08:01 / sent 08:30 archive record. The fixed code receives the observation cutoff and requires generated <= sent <= cutoff on the requested day, with matching generated version/hash. Independent pure-function reproduction now rejects it. Delivery remains explicitly application-record evidence, not recipient receipt.
2. **Resolved P1 — call provenance omission**, `scripts/ops/report_premarket_check.py:80-97` (original diff:78-90). Initially a row with provider=openai, request_sent=false, refusal_detected=true and an unknown role policy still became call_metadata_verified. The corrected verifier requires gemini, request_sent=true, refusal=false, exact current role policy, expected requested/actual model metadata, minimal thinking, successful STOP and finite usage/cost/latency. Real producer field names and constants were checked in `src/utils/llm.py` and `src/utils/llm_roles.py`.
3. **Resolved P2 — duplicate attempt aggregation**, same verifier. Exact duplicated audit rows initially doubled attempts/tokens/cost while remaining verified. The fixed result marks review_required, reports duplicate_rows, and makes estimated cost null. Raw row/token totals are not an approval when duplicates exist.
4. **Resolved P2 — durable attempt and failed health receipt**, `scripts/ops/report_premarket_check.py:232-263`. Directory fsync now occurs before collect, and malformed health structures/HTTP failures yield a retained read_failed result after the attempt is consumed.
5. Additional final changes were read: aware manifest windows are required; preopen now compares expected MainPID, HEAD, all provided file pins, timer state/time, kill-switch presence, unused capture receipt, memory-only health, and disk free capacity. The added preopen and failed-collection regressions cover changed pins/PID and consumed failure.

The monitor copies only whitelisted health/model metadata to its output. Source inspection followed `/api/health` through `get_system_health`, execution-recovery `report`/`pending_handoffs`, and metrics snapshots: these inspect current memory and do not call the exchange. Results explicitly distinguish this from exchange order reconciliation.

## Research contract correction

**Resolved P2 — frozen exit policy incompatible with the existing evaluator.** The original `exit_comparison_policy` nested object included four explanatory extra fields, while `src/analytics/entry_exit_comparison.py:14-20` requires exactly five keys. Passing the original object to the real `_policy` reproduced `ValueError: comparison policy schema/version mismatch`. Explanatory fields were moved to sibling `exit_comparison_notes` (current JSON:107-111), leaving the five executable policy keys at :76-82. The exact final nested object now succeeds against real `_policy`, retaining fixed_at `2026-10-15T14:18:28+00:00` and gap Decimal('5').

The document keeps full-source ablation, limited occurrence-bonus shadow, and same-entry initial-stop comparison separate. It does not invent missing full-candidate economics, session-review evidence, fills, account return, or source independence. `de6c5bf7..377f0f52` was independently confirmed to touch only six documentation files, supporting the stated relevant-code equivalence. The full detailed economic thesis was not re-derived beyond the bounded source/contract checks above.

## Verification evidence

- Reviewer: 9 focused in-memory assertions, rc0, on valid call metadata, four inconsistent provenance patches, duplicate ambiguity/cost, future archive cutoff, and two malformed health shapes. No files/network were used by these assertions.
- Reviewer: malformed readiness pure-function reproduction returned blocked and retained the summary through an in-memory writer.
- Reviewer: real `_policy` rejected the original research policy and accepted the corrected final object, rc0 with the project venv. An initial system-Python import attempt failed because loguru was not installed; rerunning with the documented project runtime resolved that environment-only issue.
- Reviewer: integrated source fingerprint command rc0 with exact expected digest.
- Author handoff inspected: final related readout/analytics tests **186 passed in 20.61s, rc0**, including real four-CLI sealed/unsealed synthetic E2E, crash/once, limits, input immutability and secret isolation. This is author test evidence, not a reviewer full-suite execution.
- Coordinator reported final integrated scripts **57 passed in 18.45s** (45 readout +12 monitor), no production-state/network attempts; final test code was inspected. Full-suite/integration evidence remains coordinator-owned.
- Coordinator reported initial user-template validation rc0 and final `systemd-analyze verify` of all six system-manager templates rc0, with an unrelated existing claude-session.service KillMode warning unchanged. Reviewer inspected their final content without querying or changing either service manager.

## Concrete installation package and boundaries

Reviewed role/preopen/readout calendars are exactly 2026-10-16 07:35 /08:50 /09:45 Asia/Seoul, each with an aligned strict runtime window. They are standalone system-manager units with explicit User=ubuntu and Group=ubuntu, `Restart=no`, `Persistent=false`, `AccuracySec=1s`, no random delay. `env -i`, Python `-I -B`, UMask0077, NoNewPrivileges, Nice10, CPUQuota50%, MemoryHigh512M/MemoryMax768M, TimeoutStartSec480, KillMode=control-group and no core dumps are present. Output stays under a separate private state directory. No dependency starts/restarts the trading engine or changes its capture timer.

`ExecStartPre` checks the reviewed fixed release+three manifests using bundle.sha256. Before installation is reported complete, coordinator must generate and verify that final checksum from the reviewed clean commit. The final preopen manifest was independently counted and validated: **334 pins = 265 prior pins +69 scripts**, all absolute paths with well-formed SHA-256 values. One pin is the production .env path. The coordinator confirmed existing authorization for hash-only configuration preservation: the checker reads bounded bytes solely for SHA-256 and does not parse, load into the environment, output the contents, or use credentials. The reviewer inspected only the manifest path/hash and did not open the production .env. This hash-only behavior is to be explicit in the operations document.

The initial user-manager longevity prerequisite is **removed**. The coordinator confirmed Linger=no and left it unchanged; the revised package uses the system manager, with every service explicitly running as ubuntu:ubuntu. No privileged ExecStart/ExecStartPre prefixes or dependency that starts/restarts the trading engine are present. The six new dated units are separate from all existing bot/capture units. With Persistent=false, a host that is off at the scheduled instant intentionally skips the missed execution; reboot before the instant does not depend on a user login. This approves the narrow scheduling change and does not authorize changes to account linger, existing trading settings, or bot/capture services.

The interpreter/installed libraries and fixed source copy are trusted-runtime assumptions, not a security sandbox. A hard kill can leave only the consumed attempt and partial evidence. The 768MiB cgroup cap can safely fail a large analysis; a successful smaller synthetic run does not guarantee every maximum-size capture fits. Missing or failed evidence requires review, never economic promotion.


## Final narrow-delta review — system manager

**Final source gate: APPROVE. Final installation gate: APPROVE. Open P0/P1/P2: 0.** These verdicts supersede the earlier user-manager installation assumption. The approved target is the final six system units in `/tmp/qwq-premarket-unit-review`, the three pinned manifests including 334 preopen pins, and the previously reviewed fixed source. Actual installation and scheduled execution remain separate evidence.

Fresh static assertions, rc0, confirmed explicit ubuntu User/Group in all three services; absence of privilege-override command prefixes; Restart=no, TimeoutStartSec480, KillMode=control-group, NoNewPrivileges, CPUQuota50%, MemoryMax768M; and exact dated/timezone timer calendars with Persistent=false. No production paths or service-manager calls were used. The revised `docs/operations/premarket-followthrough-2026-10-16.md` was read and consistently describes the system manager, unchanged Linger=no, ubuntu service identity, separate existing units, resource limits, skipped missed jobs and incomplete installation/execution status.

Final reviewed unit SHA-256:

- preopen.service: `f667acd0190cf105c5902d01c75bd88b2dc547225b2ac3a66945bfa3c43b3b9c`
- preopen.timer: `3fd917076e1b5fe450d711016dfeb19de0e499f1ec9754b57ab5ab9166baef96`
- readout.service: `c0f05de613d8c4d7be908ed579232ff88ad6da140a034e5bba618ffd9b9fbe58`
- readout.timer: `79ba92f892219c451a53b9f2572d0d77c99cb6baec5bd329f43e7ad812dddeb8`
- roles.service: `dde51334cf58e167e742b4508fe37962617b1b12ef89b2f618b132477b293721`
- roles.timer: `d9f93a5eda9ac70c4db7edb6b0979dab3685f071f4f4af7358522e8cd93166ea`

No broad tests were run for this manager-only delta. Full verify is coordinator-owned and was still running at handoff; this report does not claim its result. Actual model/effective effort metadata remains unverified.
