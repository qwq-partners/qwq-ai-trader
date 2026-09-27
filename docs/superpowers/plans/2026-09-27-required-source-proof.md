# Required Source Proof Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 필수 이중 검증의 첫 독립 부품으로 실제 pytest 증거 수집기와 오프라인 네 슬롯 검사기를 만들고, 사용자 승인 성능 예외를 실제 성공과 구분한다.

**Architecture:** 관측 hook은 실제 결과를 모으고 작은 소유 wrapper가 pytest 반환 후 최종 rc·guard를 기록한다. 순수 검사기는 독립 기대값과 네 lane×시간대 receipt를 대조한다. 검사 CLI는 파일만 읽으며 native capsule과 필수 CI 활성화는 후속 단계로 분리한다.

**Tech Stack:** Python 3.12, pytest, 표준 라이브러리, Bash, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-27-required-source-proof-design.md`와 2026-09-27 사용자의 기존 성능 실패 예외·개발 진행 지시.

**Status:** 독립 계획 한정 승인(`APPROVE_PLAN_ONLY`)·사용자 written-plan 검토 대기·미구현.
첫 리뷰 P1 세 건/P2 한 건을 반영했고 재리뷰에서 전부 closed·새 지적0이다.
검토한 계획 SHA256은 `6565eecf8e4d7bc5844df25490d847c897d3ac90f65ed9c97bb9f4edf7b1d651`,
재리뷰 artifact `plan-rereview.md` SHA256은 `16a20fa80204afc118357e41206f6efb8e1196e33b364900449b6a6307fe0645`다.
리뷰 뒤 변경은 이 상태/근거 기록뿐이다. 실제 모델/effective effort는 metadata 미노출로 미검증이다.

## Global Constraints

- 독립 검토된 실행계획이며 사용자 written-plan 검토 대기다. 구현·CI 활성화·runtime 자격 취득 완료가 아니다.
- 기준 `450a6ed593c0a0873e2bde0db8a1a8d956fe5d37`, coordinator는 기존 격리 feature worktree를 사용한다.
- 주문·전략·위험 설정·제품 소스·운영·main 변경 없음. KIS 거래/잔고·Toss 관측 역할 불변.
- 기존 실패 기록·원본 시험 단언을 보존한다. 성능 예외는 PASS, 임계 상향, blanket skip/xfail이 아니다.
- runtime 자격·guard 위반·정합성 실패·시험 누락·인프라 오류는 예외 대상이 아니다.
- `docs/reviews/recovery-performance-exceptions-2026-09-27.json`은 이미 발생한 두 관측의 수동 개발 처분 원장이다. 시험 실행기를 제어하는 설정이 아니며 receipt 검사기의 성공 판단 입력으로 사용하지 않는다.
- 기존 `verify.sh`·workflow·`tests/conftest.py`·제품 src·기존 시험 단언·source/sequence 후보는 이 1차 구현에서 변경하지 않는다.
- 실제 source proof용 qualified runtime은0개다. 오프라인 schema 합격을 runtime 인증 또는 최종 `verify` 성공이라고 쓰지 않는다.
- root+작업자 최대3명. 병렬 작성은 같은 base·별도 worktree·파일 소유권 분리. 시험 workload는 직렬, 전체시험은 읽기 전용 worker도 종료된 뒤 coordinator 단독 실행.
- 전체시험 예산900초/시간대, 집중 proof180초/시간대, 기존 source child10초를 늘리지 않는다. 이 단계에서 native proof workload는 실행하지 않는다.
- HOME·운영 자격증명·원장·캐시를 전달하지 않는다. 외부 API 호출0. `PYTEST_ADDOPTS`·`PYTEST_PLUGINS` 같은 외부 선택/주입 값은 시험 진입점에서 제거한다.

## Review Focus

- 성능과 정합성 단언이 같은 시험에 있는 경우 정합성 실패를 성능 예외로 삼키지 않는다. 같은 node의 skip/xfail도 조건 증거 없이 허용하지 않는다.
- timeout/부분 receipt를 기존 성능 실패로 오인하지 않는다.
- 다른 lane·시간대·run의 정상 결과를 재사용하지 않는다. context의 시간대가 실제 process와 다른 경우 거부한다.
- 후보 등록부·verifier의 자기승인으로 runtime 자격을 만들지 않는다.
- 중첩 proof 경로로 이동한 직접 child가 격리 guard를 잃지 않는다.

## 이번 범위와 다음 범위

이번에는 **작동하고 시험 가능한 오프라인 부품**을 완료한다. runtime image를 확보하지 못했다고
이 부품의 개발을 중단하지 않으며, 반대로 이 부품 완료를 원 설계 전체 완료로 보고하지 않는다.

| 원 설계 요구 | 이번 처분 |
| --- | --- |
| 같은 실행·정확한 시험 목록·실제 결과·격리 확인 | Task1~3에서 구현·합성 반례와 실제 소형 pytest로 검증 |
| 기존 두 성능 실패의 예외 | 이미 발생한 관측을 위 JSON 원장으로 개발 비차단 처리, 자동 PASS 변환0 |
| 기존 전체 회귀 유지 | 파일/단언/수집 규칙 불변 확인; 마지막 전체시험 raw 결과를 그대로 보고 |
| 기존 skip/xfail 조건 보존 | producer가 원시 outcome을 기록하되 v1 validator 허용은 보류; 기존 xfail2/marker 변경0, 실제 전체 receipt를 최종 승인하지 못하는 제한 명시 |
| source 파일 이식·직접 child·명시 inventory | 후속2단계. 이번에 파일을 이식하거나139건 통과를 재사용하지 않음 |
| capsule 제작/native qualification/신뢰 base 등록부 | 후속2단계. 후보 확보·loader/native 근거·실제 oracle 독립 인수 필요 |
| standard/proof launcher·원격 workflow·required 보호 | 후속3단계. 앞선 실제 producer 연결·runtime 자격·원격 보호 확인 후 활성화 |

Review Focus의 마지막 항목(직접 child)은 이번 변경으로 손대지 않는 경계다. Task3는 해당
기존 후보 지문 보존을 검증하고, 실제 child 이동 회귀는 후속2단계의 필수 RED로 넘긴다.

## 파일 소유권·실행 배정

| 작업 | 작성 파일 | 요청 모델/effort | 독립 리뷰 |
| --- | --- | --- | --- |
| Task1 순수 계약 검사 | `scripts/dev/verification_contract.py`, `tests/dev/test_verification_contract.py` | Terra/high — 고정 schema·순수 판정 | Sol/high |
| Task2 실제 pytest evidence | `scripts/dev/pytest_evidence.py`, `tests/dev/test_pytest_evidence.py` | Astra/high — 실제 lifecycle·guard 결속 | Astra/xhigh, 작성자 아님 |
| Task3 CLI·결합 인수·문서 | `scripts/dev/check_verification_evidence.py`, `tests/dev/test_check_verification_evidence.py`, 관련 문서 | Terra/high — 앞선 인터페이스 연결 | 최종 Astra/xhigh |

Task1·2만 공통 기준에서 병렬 작성 가능하다. Task3는 두 인터페이스 검토·통합 후 진행한다.
각 작성 task25분·각 리뷰8분을 최초 한도로 하고, 미완이면 산출물/진전을 보고하며 같은 실패를
무한 재시도하지 않는다. 실제 모델/effective effort는 metadata가 없으면 미검증으로 기록한다.
coordinator만 feature 통합·문서 commit/push를 맡는다. main·배포·재시작은 하지 않는다.

## 공통 인터페이스와 자료 상한

Python 표준 라이브러리만 사용한다. `verification_contract.py`는 다음 public API를 제공한다.

```python
class EvidenceError(ValueError): ...
def parse_document(raw: bytes, *, kind: str) -> dict: ...  # kind: receipt | expectation
def validate_receipt(receipt: dict, expected: dict) -> tuple[str, ...]: ...
def evaluate_bundle(receipts: list[dict], expected: dict) -> dict: ...
```

두 검증 함수의 `expected`는 아래의 완전한 expectation 문서이며 receipt의 slot과 일치하는 한 항목을 선택한다.

`parse_document`는 duplicate key·NaN/Infinity·UTF-8 오류·unknown schema/field·잘못된 타입을
`EvidenceError`로 거부한다. bool을 정수로 받지 않는다. document≤32MiB, depth≤12,
nodes≤20,000, nodeid≤2,048 UTF-8 bytes, code/hash는 고정된 길이를 검증한다. 기대값은 결과에서
자동 생성하지 않는다. 동등 비교용 목록은 정렬하되 중복은 제거하지 않고 오류로 처리한다.

Receipt schema `qwq.verification-receipt/v1`의 exact 최상위 필드:

- `schema`, `run`, `slot`, `identity`, `collected`, `results`, `session`, `guard`.
- `run`: `event`, `sha`(40 lowercase hex), `tree`(40 lowercase hex), `contract`(64 lowercase hex),
  `run_id`(1~128 ASCII 식별자), `attempt`(양의 정수). event는 `local`, `pull_request`, `push`, `merge_group`, `workflow_dispatch` 중 하나.
- `slot`: `lane`(`standard` 또는 `source-proof`), `timezone`(`UTC` 또는 `Asia/Seoul`).
- `identity`: `runtime`, `producer`, `inventory`(각64 lowercase hex). runtime은 관측 환경 지문이지 자격 승인 문자열이 아니다.
- `collected`: 중복 없는 실제 nodeid 목록. `results`: node마다 `nodeid`, `setup`, `call`, `teardown`.
  phase 값은 `passed`, `failed`, `skipped`, `xfailed`, `xpassed`, `not_run` 중 하나. 없는 phase는 `not_run`으로 남긴다.
- `session`: `finished`(bool), `exit_code`(정수 또는 미완료의 null), `collection_errors`(0 이상 정수), `deselected`(0 이상 정수).
- `guard`: `path`(repo-relative), `sha256`, `module_count`(0 이상 정수), `violations`(0 이상 정수). 운영 경로·예외 메시지·locals·환경변수 값은 기록하지 않는다.

Expectation schema `qwq.verification-expectation/v1`의 exact 필드: `schema`, `run`, `slots`.
`run`은 위 공유 tuple이며 `slots`는 정확히 네 항목이다. 각 항목은 `slot`, `identity`,
`nodes`, `allowed_outcomes`, `guard`로 구성한다. `allowed_outcomes`는 **v1에서 반드시 빈 object**다.
node/status만으로 기존 skip/xfail의 조건을 증명할 수 없으므로 해당 허용은 후속으로 미룬다.
wildcard·개수만의 허용·failed/xpassed 허용은 없다.
guard 기대값은 exact path/hash와 `module_count=1`, `violations=0`이다. source-proof 슬롯은
allowed_outcomes가 비어 있어야 한다. `identity.inventory`는 정렬된 nodes의 canonical JSON
(`ensure_ascii=False`, `separators=(',', ':')`, UTF-8)의 SHA256과 일치해야 한다.

Node 정상 lifecycle은 setup/call/teardown 모두 passed다. producer는 skip/xfail을 실제대로 기록하지만
v1 validator는 `UNSUPPORTED_OUTCOME`으로 거부한다. 기존 strict xfail2건이 있는 프로젝트 전체
receipt는 아직 이 부품만으로 승인할 수 없다. 원본 시험·marker를 삭제하거나 일반 pytest 성공을
실패로 바꾸는 것은 아니며, 후속에서는 condition/reason/test-source identity를 함께 고정해야 한다.
setup/teardown 실패와 XPASS도 항상 오류다.
`session.finished=True`, exit_code0, collection_errors0, deselected0와 실제 guard0이 모두 필요하다.
오류는 고정 reason code로 반환하고 민감한 원문을 echo하지 않는다.

Bundle 출력 exact 필드: `schema="qwq.verification-decision/v1"`,
`status`(`EVIDENCE_CONSISTENT` 또는 `REJECTED`), `errors`(reason code 목록),
`scope="offline_evidence_only"`, `production_eligible=false`.
이 부품은 native qualification·원격 보호·CI `needs`를 검증하지 않으므로 이 출력만으로 최종 gate를 열 수 없다.

### Task 1: 순수 receipt·네 슬롯 검사기

**Files:** 위 소유권 표의 Task1 두 파일.
**Interfaces:** 위 public API를 생산한다. pytest·제품·network·GitHub를 import/실행하지 않는다.

- [ ] **Step 1 — RED:** 다음 반례를 먼저 작성한다.

```python
def test_bundle_requires_four_distinct_matching_slots():
    assert evaluate_bundle(four_valid_receipts(), expected())['status'] == 'EVIDENCE_CONSISTENT'
    assert evaluate_bundle(four_valid_receipts()[:3], expected())['status'] == 'REJECTED'
def test_known_performance_failure_is_not_automatic_pass():
    receipt = receipt_with_call_failure()
    assert validate_receipt(receipt, expected())
```

  추가 parameter case: duplicate JSON key/depth13/32MiB+1/bool attempt/unknown field/NaN,
  lane 간 정상 runtime 차이는 허용, UTC→KST 복제·이전 attempt·다른 SHA는 거부,
  missing/duplicate/extra node·guard 미로딩/두 module/위반1·session 미완료/exit124,
  setup/teardown 실패·skip/xfail/XPASS·잘못된 inventory 지문 거부.
  같은 node를 allowed_outcomes에 추가해 skip을 허용하려는 입력도 거부한다.
- [ ] **Step 2 — RED 확인:** 아래 공통 집중 명령으로 이 파일을 실행해 누락 구현으로 실패함을 기록한다.
- [ ] **Step 3 — 최소 구현:** exact schema와 expected 대조만 구현한다. 원시 failed를 수동 예외 JSON으로 바꾸는 분기를 만들지 않는다.
- [ ] **Step 4 — GREEN/리뷰:** 같은 명령의 통과·격리0을 확인하고 Sol/high에게 diff·실제 증거를 넘긴다.
- [ ] **Step 5 — 통합:** 승인된 두 파일만 coordinator가 feature에 커밋한다. 다른 task 파일을 일괄 stage하지 않는다.

### Task 2: 실제 pytest evidence 생산자

**Files:** 위 소유권 표의 Task2 두 파일.
**Interfaces:** `pytest_evidence.py` 안의 관측 plugin과 public wrapper:

```python
def run_with_evidence(pytest_args: list[str], *, context_path: Path, output_path: Path) -> int: ...
```

CLI는 `python -m scripts.dev.pytest_evidence --verification-context PATH --verification-output PATH -- [pytest_args]`.
이것은 호출자가 선택한 소형 시험/개발 pytest를 실행하는 도구이지 필수 lane launcher가 아니다.
`PYTEST_ADDOPTS`/`PYTEST_PLUGINS`가 비어 있지 않으면 시작 전에 거부하고, context/output 옵션을
pytest_args에 다시 주입할 수 없다. 원시 pytest selection/deselection은 숨기지 않고 기록한다.
`--verification-context=PATH`는 `run`, `slot` 두 필드만 가진 JSON,
`--verification-output=PATH`는 새 artifact 파일이다. 이 context는 기대값이나 결과를 포함하지 않는다.
Task1과 동일 receipt schema를 생산하지만 Task1에 import 의존하지 않아 병렬 작성 가능하다.
plugin이 actual collected node로 inventory 지문을, 자신의 실제 파일 bytes로 producer 지문을 계산한다.
runtime은 `implementation`(sys.implementation.name), `version`(sys.version), `soabi`(sysconfig),
`machine`(platform.machine), `executable_sha256`의 canonical JSON 지문이다. 이는 v1 오프라인
환경 식별용이며 native closure qualification으로 사용할 수 없다. 공유 run tuple은 controller가
제공한 주장으로, 이번 부품은 그 값의 일관성만 검사한다. 미래 runner가 실제 git/event와 결속해야 한다.
wrapper는 시작 전 실제 `TZ`가 `UTC` 또는 `Asia/Seoul`이며 context와 같은지 확인한다.
누락·불일치는 pytest 시작 전에 거부한다. `TZ` 값을 바꾸지 않고 POSIX `time.tzset()`을 적용하며
고정 epoch의 UTC offset(0/32400초)을 확인한다. 반환 뒤에도 TZ/offset을 재검사한다.
시스템 시간대·HOME은 변경하지 않는다. 해당 API/zoneinfo가 없는 환경은 미지원이다.

- [ ] **Step 1 — RED:** 실제 tmp repo/소형 pytest subprocess로 다음을 고정한다.
  `test_real_pass_and_failure_keep_pytest_exit_codes`는 정상0·assertion 실패1을 그대로 요구한다.
  `test_swallowed_guard_violation_is_recorded`는 합성 guard의 VIOLATIONS1이 receipt에 남음을 요구한다.
  setup 실패/teardown 실패/strict xfail/XPASS/collection 오류/deselected/중도 종료,
  guard0개/동일 이름 다른 경로/동일 파일 두 module/기존 output 파일 거부를 추가한다.
  실제 `TZ=UTC/context=Asia/Seoul`과 역방향·누락·종료시 변조를 RED로 고정한다.
  늦은 sessionfinish exit 변경/guard 위반, unconfigure 예외의 실제 소형 hook도 추가한다.
- [ ] **Step 2 — RED 확인:** 소형 자식에 실제 guard를 설치한 격리 환경에서 실패를 기록한다.
  자식은 root/tests의 진짜 conftest를 제품 import 전에 로드한다. 실제 외부 접속/운영 경로 접근으로 negative를 만들지 않는다.
- [ ] **Step 3 — 최소 구현:** plugin은 collection/report 사실만 축적한다. wrapper가 `pytest.main(..., plugins=[observer])`
  반환 **후** 실제 반환 rc와 최종 guard identity/위반을 읽고 receipt를 발행한다. sessionfinish에서는
  완주 receipt를 발행하지 않는다. `finished`는 pytest.main이 정상 반환했음을 뜻하며 OS process 종료 증거가 아니다.
  반환 전 예외/unconfigure 오류는 정상 receipt를 남기지 않는다. 정상 반환 시 wrapper exit는 원 pytest rc,
  context/publisher 오류는2지만 원 pytest rc가 nonzero였으면 원 rc를 우선 보존한다. publisher 오류는 별도 stderr 코드다.
  테스트 phase·marker·pytest session.exitstatus는 변경하지 않는다. conftest exact 파일과 단일 module object를 관측한다.
  module_count는 sys.modules 이름 수가 아니라 canonical guard 파일에 대응하는 서로 다른 object 수다.
  완주 전 output을 성공 형태로 쓰지 않는다. output은 기존 파일을 덮어쓰지 않는 exclusive create로 쓴다.
  output의 resolve된 부모는 pytest root 내부여야 하고 context 자체도 덮어쓰지 않는다. 완성 JSON이
  32MiB를 넘으면 receipt를 쓰지 않고 별도 오류를 알린다. consumer는 누락/부분 파일을 거부한다.
  process가 강제 종료돼 파일이 없으면 consumer가 실패로 처리한다. 운영 원문·traceback locals는 저장하지 않는다.
  receipt 발행 뒤 강제 종료/atexit 실패까지 이 파일로 증명하지 않는다. 미래 controller가 실제 OS rc·timeout·reap를
  별도 결속하기 전에는 최종 CI 게이트로 사용할 수 없다. 합성 자식 시험은30초 상한·양 스트림 회수·timeout 후 kill/reap를 고정한다.
- [ ] **Step 4 — GREEN/리뷰:** 소형 pytest의 실제 raw rc·생산 JSON·격리0을 검토한다. Astra/xhigh가 작성자와 독립 리뷰한다.
- [ ] **Step 5 — 통합:** 승인된 두 파일만 feature에 통합한다. artifact나 tmp repo는 커밋하지 않는다.

### Task 3: 오프라인 CLI·결합 인수·문서

**Files:** 위 소유권 표의 Task3 두 파일, 이 계획·설계, CHANGELOG/CLAUDE/docs README/진행 원장.
**Interfaces:** `main(argv: list[str] | None = None) -> int`; `--expected PATH`와
반복 `--receipt PATH`(정확히4개). stdout은 위 decision JSON 하나, 정상0·검사 거부1·인자/읽기 오류2.
명령 실행·자식 프로세스·native SQL·자격 등록·네트워크·artifact 업로드 기능을 넣지 않는다.

- [ ] **Step 1 — RED:** 실제 CLI subprocess로 valid4/누락/중복/다른 run/잘린 JSON/oversize/nonregular 입력을 검증한다.
  Task2 소형 실제 pytest receipt를 모아 Task1에 넘기는 integration test를 만든다.
  실패를 수동 성능 예외 원장에 적어도 CLI가0으로 바뀌지 않는 반례를 고정한다.
- [ ] **Step 2 — RED 확인:** 두 선행 모듈은 승인된 실제 구현을 사용한다. 핵심 parser/producer를 mock하지 않는다.
- [ ] **Step 3 — 최소 구현:** 파일당 상한과 읽기 오류를 처리하고 pure API를 호출한다. file 읽기는 크기 사전 점검과 bounded read로 제한한다.
- [ ] **Step 4 — GREEN/전체 검증:** 집중 세 파일+기존 `tests/dev/test_verify.py`를 검증한 뒤 전체 UTC/KST를 직렬 실행한다.
  원시 실패/미완료는 모두 보고하고 알려진 이력의 개발 예외와 실제 이번 결과를 구분한다. 이력 면제로 pytest PASS를 주장하지 않는다.
  이번 UTC에 새 실패/미완료가 있으면 KST와 자동 완료·통합은 중단한다. 새 관측의 정확한 원인/범위를
  별도로 처분하기 전 기존 두 관측과 같은 예외로 간주하지 않는다. 이는 과거 두 관측만으로 개발 착수를 다시 막는 조건이 아니다.
- [ ] **Step 5 — 최종 See:** 아래 완료 조건을 확인하고 Astra/xhigh 전체 변경 리뷰, 문서·비밀정보 검사 후 feature commit/push.

## 실행 명령과 최종 See

집중 명령(작업 파일만 바꾸며 같은 invocation 형태로 RED/GREEN을 기록):

```bash
timeout --signal=TERM 180s env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest tests/dev/test_verification_contract.py -q -p no:cacheprovider --tb=short
```

전체시험은 위 명령의 timeout을900s, 파일 선택을`tests`로 바꾸고 UTC 후 `Asia/Seoul`을 사용한다.
실제 프로젝트에 필요한 plugin은 기존 로딩 계약을 조사해 명시하고 숨은 선택 옵션은 받지 않는다.
이번 실행의 첫 실패/미확정/인프라 오류에서 후속 workload와 자동 완료·통합을 중단하며 해당 task를 성공으로 보고하지 않는다.
성능 실패가 다시 나오면 원시 결과는 보존하고 이미 승인된 관측과 동일시하지 않는다.

- [ ] 네 expected 슬롯과 receipt schema가 일치하고 정상 lane 차이를 잘못 거부하지 않음.
- [ ] 원시 pytest 종료 코드 불변, 성능 예외 자동 적용0, guard·수집·phase 오류 거부.
- [ ] 기존 verify/workflow/conftest/src/원본 시험·source/sequence 후보 지문 불변.
- [ ] 새 파일 집중 RED→GREEN, 전체시험의 실제 결과·미완료·예외를 각각 보고.
- [ ] 최종 독립 리뷰와 모든 보류 사항 처분 기록. 모델 metadata 미노출은 미검증으로 표기.
- [ ] 관련 문서·secret scan·diff check·파일 allowlist 확인. 승인된 코드/문서만 feature commit/push.
- [ ] 전체 source proof·runtime qualification·원격 CI·main·운영은 미완료로 인계하고 다음 단계의 별도 계획으로 연결.
