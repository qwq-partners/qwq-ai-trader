# Independent critical final review — selection evidence and October 19 candidate

Verdict: APPROVE the bounded repository integration at c17a85fe7fcfb36be50f5c8f6ac0658b963a357c. P0=0, P1=0, P2=0. This is approval of the reviewed development changes and their stated limitations, not installation, scheduling, deployment, restart, paid calls, trading, production capacity or profitability approval.

## Identity and pin

- Role: independent reviewer, not implementation author; no delegation/fan-out.
- Requested model/effort: gpt-6-astra / xhigh under ai-routing-v1-2026-09-20. Actual model and effective effort are not exposed by the runtime, therefore unverified. No reviewer-initiated fallback occurred; hidden runtime routing is unverified. This is a native independent review, not a cross-provider review.
- Baseline: 023e26fd77d6ecc2a10350e75eec6906afae0a7a.
- Production-source pin inspected and tested: e25a72053f511a6c268b2adf2b0135fc5b33546c.
- Final integrated test pin reviewed: bf78518cab950f75d4382c8338cf7c3102bde3df.
- Final source/test/document pin: c17a85fe7fcfb36be50f5c8f6ac0658b963a357c. Git confirms no src/scripts change from e25a7205, and the final working tree is clean.
- Worktree: /home/ubuntu/projects/qwq-ai-trader/.claude/worktrees/codex-recent-work-d9b36c.
- Read-only repository review; only this report is written outside the repository. No credential/private evidence/network/API/LLM/service/order/SSH/deployment/install operations.

## Findings

No confirmed P0, P1, or P2 findings in the reviewed source, final test follow-up, or final documentation. Source, tests, evidence and the final commit were reconciled before issuing this verdict.

One explored counterexample was explicitly outside the coordinator-confirmed experiment: raw positive supply can coexist with false rank-qualified production flags. The shadow retains those flags and suppresses only the existing absence penalty when at least one numeric supply state is unobserved. A zero shadow delta is not a claim that flags agree with raw supply or that the current policy is correct. The coordinator added this clarification to docs/research/selection-evidence-followthrough-2026-10-16.md:27; this is a documented scope limitation, not a requested production change.

## Source review

- Raw numeric evidence is taken only from fresh FHPTJ04400000 payload rows, within the matching source ContextVar. Missing/empty/invalid values and provider/screener caches do not become zero or nonpositive facts. Market conflicts remain unobserved/conflict, and source_as_of remains null. The provider result/cache schema is unchanged.
- Ordered 24-stage observations follow real mutation boundaries, including separately accounted Naver/news merges. Delta/chain, merge sum, modifier sum, legacy post_merge_adjustment, occurrence, normalization and final score reconcile. Unchanged helper results remain not_evaluated; only the native SPDI completion marker permits completed status.
- SPDI is a local, same-stage observation. Actual score/floor and flag/change inputs are checked; final score, gate and economic effects remain null. Production scoring, decisions, provider calls and order paths are not changed.
- Context scope restores on exception/cancellation. Hooks catch evidence failures. Source objects and integrated fallback retain their own copied state; historical cached v3 evidence is not joined to current raw evidence, and v1/v2 requests receive their historical field contract.
- Schema handling is version-specific, bounded and strict. v3 opt-in and readiness expose the new stage bound, while historical readiness and settings remain unchanged. Invalid/unavailable new modifier evidence does not become an observed extended basis.
- New installer normalized against the October 16 installer changes only fixed identity/date/time, v3 settings, lease and explanatory text. October 16 installer is byte-preserved in Git. Pins, source/protected-file inventory, prior exact October 16 consumption paths, same-SHA requirement, kill/pending/process guards, trusted file handling, deployment lock, disk check and failure-disarm behavior remain present.
- October 19 contract is preparation/activation 09:25–09:27 KST, admission 09:30–09:40, capture end 10:00, lease 1800, selection v3/max_modifier_stages 24. This candidate source is not an actual installation or calendar/resource authorization.

## Independent verification on e25a7205

1. `PYTHONDONTWRITEBYTECODE=1 TZ=UTC /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -B -m pytest tests/test_selection_modifiers.py tests/test_selection_basis_observation.py tests/test_selection_source_basis.py tests/test_entry_capture_activate_20261019.py tests/test_entry_capture_prepare_20261019.py -q -p no:cacheprovider` — exit 0; 217 passed in 7.33s; isolation violations 0.
2. Separate local synthetic adversarial execution, with tests/conftest.py loaded before application imports — exit 0. Two overlapping scans of the same symbol retained independent foreign quantities -5 and +9, score 50 each, exactly one source invocation each, and restored outer ContextVars. Same-quantity cross-market duplicates retained conflict. Source-owned objects were unchanged with modifier capture off and on. Isolation violations 0.
3. Historical report golden comparison — loaded the baseline selection_basis.py from Git into a separate module and compared complete report dictionaries against the new implementation for selection-basis-v1 and selection-basis-v2, both fresh and integrated-cache fallback. Four exact-equality comparisons passed; exit 0; isolation violations 0.
4. Integrated October 19 loader/readiness checks under `TZ=Asia/Seoul` — 2 passed in 0.58s, exit 0, isolation violations 0.
5. Final real-screener v3 routing cases at bf78518c: `TZ=UTC .../venv/bin/python -B -m pytest tests/test_llm_capture_reachability.py -k v3_scheduler -q -p no:cacheprovider` — 2 passed, 11 deselected in 1.75s, exit 0, isolation violations 0. Reviewed the added test diff and worker mutation-test report; both cases retain the actual decorated screener and native scoring-to-broker-halt path while substituting external source/enrichment inputs.
6. Coordinator-owned full verification log `/tmp/qwq-oct16-selection-full-verify.log` independently read through completion: e25a7205, 6875 passed / 2 skipped / 2 xfailed / 4 warnings in 303.92s; isolation violations 0; Python syntax and secret scan completed. Coordinator reported exit 0. The added bf78518c change is test-only.
7. `git diff --check 023e26fd..e25a7205` — exit 0. `git diff --exit-code 023e26fd..e25a7205 -- scripts/ops/entry_capture_prepare_20261016.py` — exit 0.

## Resource evidence and final documentation

- Independently inspected the coordinator benchmark script and `/tmp/qwq_oct19_v3_resource_final.json`: 108,804 accepted and persisted, dropped 0, sealed true, error null, incomplete reasons empty, isolation violations 0. Publish 219.575s, close 0.196s, effective representative throughput 491.419 records/s. Baseline RSS 149,528,576 bytes, final RSS 300,052,480 bytes, charged object estimate 388,673,827 bytes. Address-space bound 805,306,368 bytes (768MiB); the production capture-retention cap remains 512MiB. Coordinator reported process exit 0.
- Independently inspected the separate 16-term script and `/tmp/qwq-oct19-v3-16term-evidence.json`: 100 candidates each with 16 terms and 24 stages, 101 persisted records, maximum serialized record 23,591 bytes, seal/fsync plus hash-checking reader/report assertions, isolation violations 0. Coordinator reported exit 0. This uses repeated synthetic sources and is a schema/size test, not sixteen independent market sources.
- Read the final c17a85fe documentation and verified its test counts, time window, unchanged production limits, narrow shadow semantics, initial benchmark failure and revised benchmark conditions against the available artifacts. The final documentation correctly separates representative synthetic qualification from live maximum throughput or overall production-process safety.
- Final `git diff --check 023e26fd..c17a85fe` passed, and `git diff --exit-code e25a7205..c17a85fe -- src scripts` returned 0.

## Limits

- Returned-candidate arithmetic is captured. Removed candidate details/counts remain explicitly uncollected (`removed_candidate_details_captured=false`, `destructive_stage_counts=null`), and helper per-candidate evaluation coverage is not established by unchanged snapshots. These are accepted, disclosed implementation limits.
- Numeric cache sidecars are not introduced; even cached positive production flags do not establish a fresh raw numeric quantity. No source timing is upgraded to an exchange as-of timestamp.
- Full suite/secret scan completion is recorded above. Resource qualification initially failed its 384MiB address-space benchmark when the native screener dependencies were imported after retaining records; coordinator controls separately reproduced address-space pressure at 512MiB. Those failed runs do not establish capacity. The successful revised representative benchmark preloads production dependencies and uses a disclosed 768MiB process address-space bound. It leaves the production 512MiB capture-retention cap unchanged and does not erase the failed lower-bound experiments. The existing connected-flow review is separately reported at /tmp/qwq-oct16-reachability-review.md; this reviewer is not relabeling it as independently rerun evidence.
- The real-screener-to-scheduler seam is pinned/reviewed at bf78518c, and final documentation/resource evidence are reconciled at c17a85fe. Subsequent report-link-only documentation may cite this verdict; changes to source, tests or material assertions require a new review delta.
- No live market throughput, API availability, current calendar status, account outcome, profitability or deployed process memory safety was proved by this review.
