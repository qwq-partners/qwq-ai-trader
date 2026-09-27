# Runtime Admission Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax for tracking.

**Goal:** runtime 등록·subject·별도 관측의 일치만 검사하고 실제 실행 권한은 부여하지 않는 순수 부품을 만든다.

**Architecture:** 엄격한 세 문서 parser와 canonical digest, 외부 binding/상태별 대조를 한 module에 둔다.
등록부는 빈 상태로 고정하며 기존 source/OS/CI 경로에 연결하지 않는다.

**Tech Stack:** Python stdlib json/hashlib/re와 기존 pytest만. 새 dependency 없음.

**Spec:** `docs/superpowers/specs/2026-09-27-runtime-admission-contract-design.md`.

**진행:** 최종 부품60건·독립 Spec/Quality 승인, 통합 결합610건·격리0.
`18a2196` 문서 보완까지 fresh broad 승인(C0/I0/M0)을 받았다.
전체 UTC6329/641.35s·KST6329/643.67s, 각각 기존16skip/2xfail/4warnings·exit0/격리0,
compile545·비밀 패턴·diff·보존 지문 검사를 완료했다. 검증 코드 `18a2196`의 feature push와
원격 SHA 일치를 확인했으며 마감 문서도 같은 feature에 commit/push한다.
아래 원문 보존 공백은
현재 GREEN으로 소급 충족하지 않으며 [인수 원장](../../reviews/runtime-admission-2026-09-27.md)에 남긴다.

## Global Constraints

- 독립 계획 리뷰 APPROVE_PLAN_ONLY. 앞 OS 단계 검증 뒤 실제 base SHA를 확정하고 착수한다.
- feature-only. src/config/기존 guard/receipt/controller/source 실행 차단/CI 변경0.
- native/SQL/capsule/image/namespace 실행·설치·다운로드·API/SSH/운영 변경0.
- 각 입력 문서별 bytes≤1MiB/depth≤10; maps1..4096, registry0..256, path512bytes/component128bytes.
- 모든 성공에도 scope offline_runtime_contract_only, trust/native/execution/production=false.
- actual qualified capsule0; 실제 registry entries=[]이며 합성 fixture와 혼합하지 않는다.
- root+최대3workers/fanout0. writer 별도 같은 base worktree, coordinator만 통합한다.
- workload lock `.superpowers/sdd/2026-09-27-runtime-admission-contract/test-workload.lock`.
- focused180초/full900초, 기존 env allowlist·plugins, full은 root 단독 UTC→KST 직렬.
- 첫 실패/timeout 원문 보존. 기존 두 성능 예외를 새 실패/미완료에 적용하지 않는다.

## Review Focus

1. candidate가 registry/expected를 함께 제공 → 값 대조와 외부 신뢰를 구분, 항상 trustfalse(Task1).
2. source/role 외 파일 누락 → exact map 비교·필수 role, closure 완전성은 외부 검토라는 한계(Task1).
3. 서로 다른 state가 같은 subject로 중복 → 전체 registry 거부, 유리한 entry 선택 금지(Task1).
4. postapproval 보고서가 hash 자기참조 → subject에 registry/review/run을 허용하지 않음(Task1).
5. 한국어 path/깊은 JSON/서브클래스 → exact types·UTF8 bytes·bounded parse·고정 오류(Task1).

## Task 1: 순수 subject/registry/observation 계약과 빈 등록부

**Owner:** Terra/high, bounded pure implementation, budget40분. 독립 최종 reviewer Astra/xhigh.
**Files:** 새 `scripts/dev/source_runtime_contract.py`,
`tests/dev/test_source_runtime_contract.py`, `docs/verification/source-runtime-registry.json`만.
**Interfaces:** spec의 RuntimeContractError, parse_runtime_document,
subject_digest, evaluate_runtime_contract. IO/환경/Git/OS/native/다른 product import 금지.

- [x] 독립 literal fixture를 test 내부에 작성한다. runtime/source 역할 파일과 manifest·관측은
  합성 bytes/hash임을 표시하고 실제 runtime의 관측으로 보고하지 않는다.
- [x] `test_empty_registry_never_permits_execution`과
  `test_matching_declarations_are_offline_only`에서 각각 UNSUPPORTED/CONTRACT_MATCH 및
  trust/native/source_execution/production false를 단언한다.
- [ ] 최초 module 부재 RED의 완전 raw 보존: 당시 tool 기록만 남아 미충족이다.
  이미 구현된 module을 지워 과거 RED를 재창작하지 않는다. 개발 인수 한계로 별도 기록했다.
- [x] strict parser·subject_digest부터 구현하고 fixture 정상/subject byte변화와 후작성
  registry review변화의 독립성을 검사한다. sort_keys canonical JSON·정확한 path 계약을 따른다.
- [x] 등록/상태/외부 binding/관측 대조를 구현한다. mode exact two값, 상태 대체 없음,
  malformed REJECTED 우선, subject 미등록/UNKNOWN/RETIRED UNSUPPORTED를 고정한다.
  빈/UNKNOWN/RETIRED와 binding/관측 불일치를 결합해 REJECTED 우선순위를 검사하고,
  malformed 입력의 declared_state가 부분 추출되지 않고 null인지 확인한다.
- [x] `test_changed_file_identity_is_rejected`에서 interpreter/libpython/SQLite/loader/
  cases/guard/controller를 각각 변경한다. map 추가/누락, source/profile/provenance mismatch,
  registry revision/hash 불일치, state/mode 경계, 중복 entry를 별도 반례로 고정한다.
- [x] size/depth/map/entry/path UTF8 한도 직전/초과·duplicate JSON·NaN·surrogate·bool-as-int·
  subclass·unknown field/role·path traversal을 pure fixture로 검사한다. 실제 파일/프로세스0.
- [x] 빈 registry 파일의 exact semantic keys/entries=[]를 root-relative fixture read로 검증한다.
  module 자체는 파일을 읽지 않는다. qualified fixture를 실제 registry에 쓰지 않는다.
- [x] focused GREEN/exit0/격리0·명령/원문·제약을 report에 기록하고 허용3파일만 local commit.

## Task 2: coordinator See와 인계

**Owner:** coordinator; independent Astra/xhigh reviewer는 저자와 다르다.
**Files:** 관련 CHANGELOG/CLAUDE/docs README/report/계획 체크만.

- [x] whole3file diff+실제로 남은 RED/GREEN·spec을 독립 검토하고 findings를 수정·재리뷰한다.
  보완은 별도 Terra/high, 마지막 mode 시험은 coordinator가 맡았고 독립 reviewer는 유지했다.
  원 writer만 수정한다는 최초 역할 배치와 달라진 사실·최초 raw 공백을 인수 원장에 명시했다.
- [x] 승인 후보를 feature에 통합하고 집중시험+기존 계약/source boundary/OS 통합 시험을 실행한다.
  실제 registry 비어 있음/source 차단 무변경·외부 실행0을 확인한다.
- [x] root단독 전체 UTC→KST cap900, 기존16skip/2xfail 조건 보존·격리0과 actual rc를 확인한다.
  full suite 성공을 이 새 계약의 실행 허가로 바꾸지 않는다.
- [x] compile-only·secret scan·diff check·원본 guard/source/기존file 지문 보존을 확인한다.
- [x] 최종 보고서에 실제 완료/미확보 증거·qualified0·다음 별도 capsule 제작 조건을 기록한다.
- [x] 검증 코드의 feature commit/push+remote SHA 확인(`18a2196301fe6421192f409c2f87fd25481bc96d`).
  마감 문서는 같은 feature에 후속 commit/push한다. main/운영/주문/설정·CI 활성화는 진행하지 않는다.

## Self-review

세 문서와 상태 대조는 하나의 순수 task로 묶어 불필요한 병렬 인터페이스를 만들지 않았다.
모든 다섯 review input class는 Task1 반례로 대응한다. 파일/역할 최소 개수는 실제 closure
완전성이나 신뢰를 증명하지 않는다. 외부 native/격리/등록 근거가 필요한 부분은 다음 단계로 남긴다.
