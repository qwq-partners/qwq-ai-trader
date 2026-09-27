# Source Proof Boundaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax for tracking.

**Goal:** 일반 회귀를 보존하면서 source 증명의 수집·직접 child 격리를 검증하고 미자격 native 실행을 차단한다.

**Architecture:** helper를 byte-preserve하고 cases bootstrap만 제한적으로 바꾼다.
독립 inventory는 실제 collection과 별도로 작성한다. qualified runtime0이므로 실행 허용 기능은 없다.

**Tech Stack:** 기존 Python/pytest, stdlib importlib/hashlib/json, 작은 bounded subprocess.

**Spec:** `docs/superpowers/specs/2026-09-27-source-proof-boundaries-design.md`.

## Global Constraints

- base `711389227ce60e2d34740d4c20e45cf394fbda44`; docs-only 계획 commit 뒤 동일 SHA로 workers 분리.
- feature-only; src/config/guard/CI/기존 assertion·threshold 변경0, frozen 후보·원장 보존.
- qualified runtime0; native/SQL source 실행0, 외부 API/SSH/설치/권한/운영 변경0.
- root+최대3workers, fanout0. 각 writer 별도 worktree, 자기 critical 변경 승인 금지.
- 공유 `.superpowers/sdd/2026-09-27-source-proof-boundaries/test-workload.lock`으로 workload 직렬화.
- env allowlist PATH=/usr/bin:/bin, LANG=C.UTF-8, TZ=UTC 또는 Asia/Seoul,
  PYTHONDONTWRITEBYTECODE=1, PYTEST_DISABLE_PLUGIN_AUTOLOAD=1. HOME 전달/재정의0.
- 기본 Python은 `/home/ubuntu/projects/qwq-ai-trader/venv/bin/python`; source 자격을 뜻하지 않는다.
- full suite에 기존 가짜 SSH fixture용 `QWQ_DEPLOY_SSH_KEY=/tmp/qwq-verification-XQNQxF/nonexistent-key`만 추가.
- focused180초/full900초, UTC 성공 후 KST, raw 최초 실패 보존; 시간 초과를 성능 예외로 처리하지 않는다.

## Review Focus

1. 같은 이름의 다른 conftest/중복 module → 제품 import 이전 거부(Task1 변이 child).
2. explicit collection은 import를 실행함 → guard-before-product, call은 fixture 이전 거부(Task1 sentinel).
3. nested boolean/중복 JSON key → strict envelope rejection, inner oracle 불변(Task1 합성 시험).
4. parameter 문자열/stacked 순서 → count가 아닌 literal exact node set(Task2 독립 작성,Task3 수집).
5. default 발견에서 기존 node 손실 → baseline 집합 보존·추가 경로 whitelist(Task3 실제 collection).

## Task 1: helper 이식과 fail-closed cases bootstrap

**Owner:** Astra/high; concurrency/guard 경계 구현. Budget45분, 같은 요청 반복 최대1회.
**Files:** Create `tests/proofs/l3_source/{l3_source_lease_probe.py,source_lease_cases.py}`,
`tests/dev/test_source_proof_boundaries.py`만. 다른 파일·Git main/remote 변경 금지.
**Interfaces:** direct `--source-guard-child` / rejected `--source-fault-child`;
`_bind_exact_guard() -> dict`, `parse_fault_envelope(raw: str | bytes) -> dict`, `setup_module()`.
고정 path/hash/schema는 spec 그대로 사용한다.

- [ ] 새 시험부터 작성: helper hash equality, exact child evidence, altered hash/wrong path/
  duplicate object/non-list/nonzero violations, extra argv, product-import sentinel,
  unqualified direct/call의 원본 provenance/function fixture·seed·SQLite·native 호출0, helper path collision.
  session-scope pytest autouse fixture는 제외하며 sentinel은 기록 뒤 즉시 raise(원 함수 호출0)한다.
- [ ] 파일 부재/미구현 때문에 실패함을 env-isolated focused pytest로 기록한다.
- [ ] 원본594line helper를 apply_patch로 bytes 보존 이식하고 SHA256 확인한다.
- [ ] 원본 cases를 apply_patch로 이식한 뒤 spec의 bootstrap/envelope/path/차단만 추가한다.
  기존 test 함수/parameter/oracle assertion을 그대로 보존한다.
- [ ] envelope duplicate/extra/missing/type/guard mismatch를 작은 subprocess의 합성 입력으로
  시험한다. native 함수를 호출하는 방식으로 parser 시험하지 않는다.
- [ ] focused GREEN, diff/source 원본 대조·hash·isolation0·실행 명령/exit code를 report에 기록한다.
- [ ] 허용3파일만 commit, push하지 않는다. coordinator가 독립 리뷰 후 통합한다.

## Task 2: 독립 exact inventory

**Owner:** Terra/high; bounded pure implementation. Budget35분. Task1과 파일 겹침0.
**Files:** Create `scripts/dev/source_proof_inventory.py`, `tests/dev/test_source_proof_inventory.py`.
**Interfaces:** `SOURCE_NODES: tuple[str,...]`, `RELATED_NODES: tuple[str,...]`,
`inventory_digest(nodes: tuple[str,...]) -> str`,
`validate_inventory(actual: list[str], *, lane: str) -> None` (오류 ValueError 고정 code).
lane은 `source` 또는 `related`만. actual은 exact list[str], duplicate/unknown/missing reject.
canonical digest는 UTF8 JSON `ensure_ascii=False, separators=(',',':'), allow_nan=False`의 SHA256.

- [ ] 원본 decorator/literal을 읽고 exact expected tuple을 독립 작성한다. 실제 collection 금지.
- [ ] 정의 부재 RED를 기록한다; sorted108/source, sorted31/related, 고정 hash,
  missing/duplicate/unexpected/wrong-lane/type/bool/non-string을 시험한다.
- [ ] 순수 module 구현; actual 순서는 허용하되 identity multiset은 정확히 같아야 한다.
  bytes 최대 node2048, nodes20000; 실제 collector/CLI/native/runtime 자격 기능 추가 금지.
- [ ] focused GREEN, independent expected 작성 근거·명령/exit code 기록 후 두 파일 commit.

## Task 3: coordinator 수집·통합 검증

**Owner:** coordinator. worker는 실행하지 않는다. 분석3artifact와 검토 기록은 SDD 경로에 보존.

- [ ] 구현 전 base 기본 `pytest tests --collect-only -q`의 실제 node 집합·rc·guard를 저장한다.
  existing pytest_evidence wrapper의 collection receipt를 재사용할 수 있으나 finished/pass 판정 금지.
- [ ] Task1/2 독립 spec+quality 리뷰를 Sol/high 또는 critical Astra/xhigh로 수행한다.
  작성자 외 reviewer, 반려 시 원 writer가 수정·재리뷰한다.
- [ ] coordinator만 diff 확인 후 feature에 cherry-pick한다. expected tuple 수정 필요 시
  actual 복사 금지: 원본 decorator와 대조해 사유를 기록하고 reviewer 재확인한다.
- [ ] 통합 focused 시험을180초 cap으로 실행: 새 두 test파일 + 기존 evidence 관련3파일.
  정확한 exit0/guard0 없이는 완료로 기록하지 않는다.
- [ ] 기본 collection 기존 집합 포함/source0/새경계시험만 추가를 실제 집합으로 대조한다.
- [ ] source explicit collection exact108, related exact31을 따로 대조하고 raw receipt 보존한다.
  source의 실제 call-phase108은 실행하지 않는다.
- [ ] fresh whole-branch Astra/xhigh broad review와 발견사항 수정을 완료한다.
- [ ] coordinator 단독으로 전체 `python -m pytest tests -x -q -p no:cacheprovider
  -p pytest_asyncio.plugin -p pytest_cov.plugin -p anyio.pytest_plugin --tb=short` UTC→KST 실행.
  no HOME, shared lock, timeout900초, 원문 log·exit·guard·skip/xfail identity 보존.
- [ ] in-memory Python compile·secret pattern·git diff --check, source/guard/frozen hash 보존 확인.
- [ ] CHANGELOG/CLAUDE/docs README/report에 수집·native 미실행을 구분해 기록한다.
- [ ] feature commit/push+remoteSHA 확인. main/배포 변경 없이 다음 OS controller 설계 단계로 진행.

## Self-review

spec 각 절은 Task1(bootstrap/envelope), Task2(expected), Task3(집합/회귀/판정)에 대응한다.
환경 자격이나 OS 종료 증거를 이번 기능의 성공으로 주장하지 않는다. 문서 approval 대기는
사용자 자율진행 지시로 대체했지만 독립 설계/계획 리뷰는 구현 전에 수행한다.
