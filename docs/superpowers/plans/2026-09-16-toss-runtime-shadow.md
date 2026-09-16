# Toss 승인 기반 관측 런타임 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** #67을 보존하며 승인 없는 송신이 불가능한 기본 OFF 현재가/캘린더 관측 런타임을 오프라인 검증한다.

**Architecture:** 승인 검증 → 전용 worker 소유 OAuth/GET/원장 → 불변 결과만 감독 루프에 반환한다. 기존 KIS 거래 경로에는 최소 후보 snapshot 게시 훅 외에 I/O나 판단 변경을 넣지 않는다. 실관측 승인과 소비자 승격은 별도다.

**Tech Stack:** Python 3.12, asyncio, threading, aiohttp, dataclasses, JSONL/SHA256, pytest; 신규 의존성 없음.

**Spec:** `docs/superpowers/specs/2026-09-16-toss-runtime-shadow-design.md` (2026-09-16 사용자 구현 승인).

## Global Constraints

- 작업 기준 `9624559` (#67 + #69 + #71 + 승인된 설계). feature 브랜치/격리 worktree, PR #68 보류 유지.
- 실제 자격·토큰/운영 경로·인증 API·추가 KIS HTTP·SSH·설정/주문·배포/재시작 0. 공개 OpenAPI 조회만 허용.
- OFF면 preflight/worker/task/file/client/session factory 모두 0. ON 승인 거부는 unavailable이며 OFF로 위장하지 않는다.
- 기존 token/store/client/rate/retry/allowlist/synthetic 계약 보존. GET transport의 bounded body 소비만 확장한다.
- 승인 registry는 서비스 UID 비쓰기·운영자 소유. 실제 배치 trust anchor/identity가 없으면 거부하며 임의 env 경로를 trust anchor로 쓰지 않는다.
- 실제 운영 수치·허가를 작성하지 않는다. 테스트 합성 값은 실행 기본값이 아니다.
- 모든 observation 결과 `production_eligible=False`; 돈 경로, candle live, 후보/점수 변경, 표시 fallback 제외.
- 구현자와 최종 리뷰어 분리. 사용자의 병렬 지시에 따라 disjoint 파일 + 별도 worktree로 Task 1/2/3만 병렬화한다.
- 에이전트는 자기 작업 파일만 수정한다. 부모가 공통 문서/CHANGELOG/통합을 담당한다. 모든 로컬 편집 apply_patch.
- 각 task는 RED 확인 → 최소 GREEN → 관련 회귀 → 명시 파일 commit/push. 최종 UTC/KST 전체 verify 및 비밀정보 검사.

## Plan → Do → See / 작업 소유권

| 단계 | 담당 모델 / effort | 산출물 |
|---|---|---|
| Plan | 부모 | 계약·파일 소유권·인수/리뷰 경계 |
| Do A | gpt-6-astra / high | 승인/권한/토큰 context |
| Do B | gpt-5.6-terra / high | 관측/통계/지속 원장/리포트 |
| Do C | gpt-6-astra / high | OAuth·bounded HTTP·공개 스펙 fixture |
| Do D | 부모 또는 후속 gpt-6-astra / high | worker·scheduler·heartbeat·통합 인수 |
| See | 구현자와 다른 gpt-6-astra / xhigh | task별 및 통합 독립 리뷰; 수정 후 재리뷰 |

## 교차 인터페이스 고정

Task 1의 `ObservationPlan.document`는 아래 최상위 스키마를 strict validation하고 recursively frozen Mapping/tuple로 제공한다. unknown key/NaN/bool-as-number/duplicate JSON key를 거부한다. 원본 bytes hash와 canonical hash를 모두 유지한다.

```
schema_version: 1
plan_id, dataset_kind: 'live', origin: 'https://openapi.tossinvest.com'
spec_version: '1.2.17', spec_sha256: '791082da4cb379117ed9fdc29a45bd42746f7a1aec368da1e9f4e1f3bfbff5b4'
dates: sorted unique YYYY-MM-DD strings
sessions: [{name: 'regular'|'pre'|'after', start: HH:MM, end: HH:MM}]
calendar_time: HH:MM
selection: {candidate_limit:int, max_snapshot_age_seconds:int, max_symbols:int,
            rule:'score_desc_symbol_asc_holdings_first'}
limits: {job_timeout_seconds:number, cleanup_timeout_seconds:number,
         preflight_timeout_seconds:number, max_pages:int, max_retries:1,
         circuit_failure_threshold:int, circuit_open_seconds:number,
         ledger_max_bytes:int, response_max_bytes:int, response_max_depth:int,
         response_max_nodes:int, response_max_string:int, parse_timeout_seconds:number,
         auth_max_issues:int,
         groups:{PRICES:int, CANDLES:int, MARKET_INFO:int}}
comparison: {max_age_seconds:int, max_skew_seconds:int, outlier_pct:number,
             min_valid_pairs:int, expected_market_basis:string}
acceptance: {min_coverage:number, max_provider_failure_rate:number,
             max_p95_pct:number, max_outlier_rate:number, max_missed_slots:int,
             max_latency_seconds:number, retention_days:int, min_business_days:int}
```

All values mandatory, finite bounded positive/nonnegative according to meaning; no live defaults. Existing rate limiter actual group names must be checked; adapter mapping lives in runtime if they differ.

발급 예산 해석: `auth_max_issues`는 한 worker/OAuthIssuer 수명 동안의 POST 시도 상한이다. 재시작을 합친 grant 전체의 누적 발급 한도를 보장하지 않는다. grant 기간·발급 권한은 송신마다 별도 검증하고 bootstrap 1회만 durable하게 소비한다. 운영자가 grant 전체 누적 발급 상한을 요구하면 별도 설계/검증 전에는 활성화하지 않는다.

Task 1 `LiveObservationGrant` is separate strict schema, owns approval evidence, not-before/expiry, client identity/host/UID/role, token directory/sender lock/ledger path, release/config hashes, exact plan hashes, query/renewal/bootstrap capabilities. Task 1 declares its exact schema in its tests/report before integration. No field may itself claim registry trust.

Runtime binding: `client_identity` is exactly the non-secret OAuth `client_id`, not an alias. The worker's lazy credential loader rejects a mismatch before POST; no guessed mapping or key logging is allowed.

Fixed API between workers:

```python
# approval.py
ObservationPlan.from_bytes(raw: bytes) -> ObservationPlan
# .document, .raw_hash, .canonical_hash
ApprovedAuthority.require(operation: str, *, deadline: float) -> float
# operation query/renewal/bootstrap; returns bounded monotonic deadline
# .plan, .grant, .authority_hash; .stop() blocks new authorization
load_authority(*, registry_path, plan_path, grant_id, trust, identity,
               clock=time.monotonic, now=utc_now) -> ApprovedAuthority
# trust/identity are explicit frozen dataclasses; no env-derived default anchors

# authorized_tokens.py
AuthorizedTokenProvider(manager, authority)
async .get_token(*, deadline)
async .recover(error_code, failed_token, *, deadline)
.observe_revocation(failed_token)  # sync safety write even after authority expiry
async .bootstrap(*, deadline)    # durable once, separate capability
make_authorized_issuer(authority, issue) -> zero_arg_async_callable
# issue keyword API matches OAuthIssuer.issue below; mandatory ContextVar

# oauth.py
Credentials(client_id: str, client_secret: str)  # redacted repr
OAuthIssuer(*, credential_loader, authorize, limits, max_issues,
            session_factory=None, clock=time.monotonic)
async .issue(*, operation: str, deadline: float) -> dict
async .close()
# authorize(operation, deadline=deadline)->bounded_deadline immediately before send

# http_body.py
BodyLimits(max_bytes, max_depth, max_nodes, max_string, parse_timeout_seconds)
async read_json_bounded(response, *, limits, deadline, clock=time.monotonic)
# identity Content-Encoding only; wire=decoded cap; strict JSON, no raw error
# AiohttpTransport gains optional body_limits and authorize(deadline=...)
# legacy fake sessions/tests become stream-capable; no unbounded json fallback

# observation_ledger.py
ObservationLedger(path, *, plan_hash, max_bytes)
.open()  # validate full chain; recover unfinished slots/attempts, no HTTP
.reserve_slot(slot_id, *, kind, snapshot: dict) -> tuple[str,...] | None
# snapshot contains symbols, snapshot_id and selection metadata; None duplicate
.begin(attempt_id)
.finish(attempt_id, *, reason, observation: dict | None = None)
.record_missed(slot_id, *, kind)
.summary() -> dict  # counts/incomplete/production_eligible False
.close()
# constructors no I/O; errors have fixed safe .code; schema allowlists

# observation.py
ObservationRunner(*, client, ledger, policy: Mapping, clock=time.monotonic, now=utc_now)
async .prices(*, slot_id: str, snapshot: dict) -> ObservationResult
async .calendar(*, slot_id: str, requested_date: str) -> ObservationResult
# policy = complete ObservationPlan.document, immutable; budget shared all chunks
# snapshot keys: snapshot_id, selected_at, source_success_at, selection_partial,
# symbols (list[str]), kis (dict[symbol, Quote]); serialization whitelist in runner
# result frozen: outcome ('success'|'failure'|'idle'|'duplicate'), degraded:bool,
# observation_count:int, valid_pairs:int, ledger_complete:bool, reason:str,
# requested_date:str|None; calendar success cannot count for wrong-date heartbeat
select_snapshot(*, candidates, holdings, source_success_at, now, policy, kis_quotes)
# candidates tuple[(symbol, score)], holdings tuple[str], timestamps aware or None
# returns above snapshot; stale/missing candidates excluded, holdings retained
```

Implementer must report interface issue before changing contract; parent records ruling and tells all consumers. Internal helpers/dataclasses can be added within file ownership.

### Task 1: 신뢰 승인 로더와 발급 권한 context

**Files:** Create `src/data/providers/toss/approval.py`, `authorized_tokens.py`; tests `tests/test_toss_live_authority.py`, `tests/test_toss_authorized_tokens.py`.

**Interfaces:** Produces approval/token APIs above. Consumes existing TokenManager/SecureTokenStore, not Task 3 implementation (issue fake has keyword signature above). Strict plan contract above is part of this task brief. Read approved spec §§4–5, complete project rules and existing token/store contracts.

- [x] RED: tests invalid/offline/unknown JSON, fake self-approved grant, writable registry ancestry, symlink/hardlinks, wrong host/release/path/hash; pure parsing creates no files.
- [x] GREEN: immutable plans/grants, bounded safe operator-registry read, binding comparison with injected host/release/UID identity; no actual config installed.
- [x] RED: expiry during queued GET/POST, monotonic bound despite wall-clock reversal; context absent/task-leak/cancel; reader mint0; revoked safety write even expired/stopped.
- [x] GREEN: `require` checks time/role/capability; token wrapper supplies/reset IssueContext to issuer closure. Pass renewal context to normal get/recover while allowing reader valid cached query. Revoked recovery itself never issues.
- [x] RED/GREEN: bootstrap once after intent even restart; unknown/revoked gate remains #67. Use durable private marker in validated token directory, not ephemeral bool; acquire existing issuer lock appropriately without recursive lock deadlock. Capability consumption before attempt, never auto-reset on errors.
- [x] Verify new tests + all existing token tests, report RED command/result and GREEN command/result; commit/push owned files only.

Representative required assertions:
```python
with pytest.raises(ApprovalError):
    authority.require('query', deadline=clock() + 30)
assert sent == []
wrapper.observe_revocation('synthetic-token')
assert store.load_revocations()  # safety persists despite expired authority
```

### Task 2: 지속 관측 원장·현재가/캘린더·오프라인 리포트

**Files:** Create `src/data/providers/toss/observation_ledger.py`, `observation.py`, `calendar_observation.py`; `scripts/review_toss_observation.py`; tests `tests/test_toss_observation_ledger.py`, `test_toss_observation.py`, `test_toss_calendar_observation.py`. Do not edit old shadow APIs/fixtures.

**Interfaces:** Produces ledger/runner/selection APIs above; consumes existing TossClient.get/RequestBudget/parse_prices/Quote with fake client in tests. Complete document is policy. No approval/OAuth dependency. Read approved spec §§6–8.

- [x] RED/GREEN: private append-only canonical JSONL sequence/hash chain, strict schema/size; constructors no I/O. Paths owner/mode/no-follow/nlink validated; fsync for slot, attempt and terminal. Preserve corrupt file and fail closed. Do not stringify raw exceptions/payloads.
- [x] RED/GREEN: same slot replay/new snapshot stays first snapshot; snapshot-only and incomplete attempts recover to interrupted with no new HTTP; valid terminal ACK-loss replay doesn't duplicate; missed slots no invented symbols. Ledger write failure stops sends and marks incomplete. Exactly one logical terminal per attempt.
- [x] RED/GREEN: selection deterministic held-first + candidate score-desc/code-asc, stale/missing candidates partial; keep quote original observed/fetched metadata, never fabricate KIS market time. Frozen input boundary.
- [x] RED/GREEN: prices chunks200 shared whole-job deadline/retry/page budget, persist all selected attempts before each request, preserve successful earlier chunks and explicit remaining failures/cancellation. Parse result via existing normalizer. Errors fixed reason enum.
- [x] RED/GREEN: exact Fraction comparison, currencies/status/freshness/skew/basis; unknown excluded; same source timestamps counted once across attempts, 0pairs None/insufficient; separate sessions/cohorts. Never relabel live as synthetic.
- [x] RED/GREEN: calendar `/api/v1/market-calendar/KR`, top-level result, today date exact, integrated explicitly null means holiday; missing key/empty object invalid; previous<today<next with valid KST session intervals and nonholiday neighbors. No market mutation, no CLOSED guard. Public 1.2.17 contract supplied below.
- [x] RED/GREEN: report CLI reads ledger only, no auth/import side-effect/network; reports denominator completeness, eligible alwaysFalse. No activation/gate flag.
- [x] Verify owned + old market/shadow tests; report tests and commit/push owned files only.

Public schema confirmed 2026-09-16: KR response.result fields today/previousBusinessDay/nextBusinessDay; each has date and explicit integrated. integrated is null or has preMarket/regularMarket/afterMarket, nullable individually. Sessions require startTime/endTime; pre/regular singlePriceAuctionStartTime and after singlePriceAuctionEndTime are optional/nullable (absence is not a malformed session). For present nonnull auction values: timezone+09:00, same day, start≤auction≤end. Integrated all-null object is unsupported (official representation is integrated:null). Source URL/hash in global contract.

```python
ledger.reserve_slot('2026-09-16T09:00:00+09:00', kind='prices', snapshot=snapshot)
reopened.open()
assert reopened.summary()['interrupted'] == len(snapshot['symbols'])
assert client.calls == []
```

### Task 3: 보안 OAuth 어댑터와 bounded HTTP

**Files:** Create `src/data/providers/toss/http_body.py`, `oauth.py`; Modify `src/data/providers/toss/transport.py` body-consumption only; tests `tests/test_toss_oauth.py`, `tests/test_toss_http_body.py`, minimal existing `tests/test_toss_client_boundary.py` fake response stream support if necessary; Create `tests/fixtures/toss/runtime_spec_contract.json`. Do not touch old spec_contract.json/hash.

**Interfaces:** Produces body/OAuth/transport APIs above. authorize injectable fake in tests, so no Task 1 dependency. Read existing transport/errors and approved spec §5.

- [x] Verify public unauthenticated OpenAPI hash/version (already parent-confirmed above); freeze used OAuth/calendar contract without credential samples. OAuth POST application/x-www-form-urlencoded grant_type=client_credentials/client_id/client_secret, success top-level access_token/token_type/expires_in, not result envelope, no Basic guessed auth.
- [x] RED/GREEN: stream cumulative bounded JSON bytes, identity encoding only, reject chunked overflow/compressed/duplicate keys/NaN/depth/node/string limits. Parse time budget before/after bounded parse, fixed sanitized errors, deadline applied read+parse. No unbounded response.json fallback for production or fakes.
- [x] RED/GREEN: OAuth fixed origin/exact path, SSLverify/redirectFalse/trust_envFalse/DummyCookieJar/no aiohttp retry, bounded body; fresh authority check before credentials and again immediately before send. auth issue cap separate from GET limit groups. POST retry0. Credential repr/load errors redacted; keys lazy only issuer.
- [x] RED/GREEN: cancellation/timeout/exceptions never echo request/response; close repeated cancellation safe, no orphan requests. TokenManager stores issuance_unknown, adapter never resets it or initiates recovery.
- [x] RED/GREEN: optional GET authorize check directly before send after async gates, approved deadline/limits, old3path allowlist/retry semantics unchanged. Existing fakes gain content.iter_chunked rather than bypass.
- [x] Verify owned + old client/rate/token tests; report tests and commit/push owned files only.

```python
await issuer.issue(operation='renewal', deadline=clock() + 2)
assert session.requests[0].url == 'https://openapi.tossinvest.com/oauth2/token'
assert len(session.requests) == 1
assert 'client_secret' not in repr(credentials)
```

### Task 4: runtime worker·배선·최종 See

**Files:** Create `src/data/providers/toss/runtime.py`, `src/schedulers/toss_shadow.py`; Modify `src/schedulers/kr_scheduler.py` create_tasks + successful screening copy hook only; Modify `src/utils/loop_heartbeat.py` minimal per-loop schedule registration/status fields; tests `tests/test_toss_runtime.py`, `tests/test_toss_shadow_scheduler.py`, `tests/test_toss_money_path_invariance.py`; parent docs/CHANGELOG/CLAUDE/README and review prompt/report.

실행 중 합의: worker 수명주기와 배치/조립 책임을 분리해 `runtime_factory.py`, `tests/test_toss_runtime_factory.py`를 추가한다. 신뢰된 launcher의 시작 시점 attestation 없이는 live 거부하며 현재 Git HEAD로 대체하지 않는다. 원장 보강은 최초 Terra/high 구현의 영속성/통계 리뷰 결함 때문에 Astra/high로 상향한다. `ObservationLedger.configure_plan(policy)`로 승인 일정 메타를 지속화하여 full-plan expected/미기록 슬롯을 구분한다; 메타 없는 기존 합성 원장의 coverage는 unavailable이다. 이는 운영 활성화 범위 확대가 아니다.

**Interfaces:** Consumes Task1–3 APIs above. No existing trade loop awaits Toss. Existing heartbeat public semantics stay unchanged. Trust anchor is explicit deployment object, not env-derived; absent refuses ON. Master flag exact opt-in only, defaultOFF.

- [x] RED/GREEN: OFF no preflight/file/factories/task/worker; ON missing anchor unavailable; separate bounded single preflight thread can hang without blocking trading loop. Post-timeout results discarded and no overlapping replacement. Grant accepted only after correct preflight.
- [x] RED/GREEN: worker single thread/own event loop constructs ledger/auth/HTTP after approval, sender lock acquired before token/key I/O. Boundedqueue1; immutable commands/results, no broker references; bootstrap tracked along with observations. Existing TokenManager issuance unknown contract remains.
- [x] RED/GREEN: stop gate→cancel/gather all→OAuthclose→clientclose→thread confirmation with bounded async waits. fsync hang gives stopping_unconfirmed and no replacementworker/sender. Multiple cancellation doesn't pretend success. Expired/stopped authority still permits revoked safety persistence.
- [x] RED/GREEN: KST fixed 5min slots within plan, daily calendar independent CLOSED; recover missed slots without imagined symbols; immutable selection copy from latest successful candidate hook+holdings/current-price cache only. Existing price metadata not invented. No additional KIS method call.
- [x] RED/GREEN: heartbeat register approved schedule; only durablecomplete+≥1validToss observation success, allinvalid/error failure, emptyoutside idle, busy budget skip not success; comparison/ledger/calendar separate diagnostics in health-compatible snapshot.
- [x] Test matrix R01–R14 including actual forbidden paths flags×KISfail invariance; old synthetic fixture/hash/APIunchanged.
  - 실행 범위: 실제 REST→ExitManager의 OFF/ON×KIS 성공/실패 지문 및 sync/fill/exit 정적 경계. 모든 broker/order 호출부의 동적 fault matrix까지 확장한 것으로 주장하지 않는다. 세부 근거/한계는 런타임 리뷰 보고서의 R01–R14 표에 보존한다.
- [x] See: task-scoped independent review, fix and re-review; integrated high-stakes reviewer separate from all implementers. Build acceptance table mapping each R to test or explicit gap; no unsupported completion claim.
- [ ] Run clean-env targeted tests and full verify UTC/KST, secret scan, gitdiffcheck. Update docs/schema/operator-only activation and stopping_unconfirmed recovery procedure, SHA/file-based external review prompt. Commit/push namedfiles, update DraftPR70 and inspectCI. Do not merge/deploy automatically.

## Verification commands

From each assigned worktree (Python absolute path shared, production environment stripped):

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=Asia/Seoul PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest -p no:cacheprovider -p pytest_asyncio.plugin tests/test_toss_live_authority.py --tb=short -q
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=Asia/Seoul PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTEST_ADDOPTS='-p no:cacheprovider --tb=short -rx' QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key QWQ_VERIFY_PYTHON=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python bash scripts/dev/verify.sh
```

Adjust first command to ownedtests + regression paths; repeat final verification with TZ=UTC. No production credentials inherited. New async tests preferably asyncio.run unless explicit plugin loaded by verify. Final report separates offline implementation from unstarted live acceptance.
