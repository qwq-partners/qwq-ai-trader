# 오프라인 시험 증거 도구 — 진행 원장

## 범위와 상태

2026-09-27 사용자 `rㄱ`으로 [실행계획](../superpowers/plans/2026-09-27-required-source-proof.md)의 구현을 승인받았다.
기준은 `3bdcfaa5d2bd17d8351b058f2ffe7709bcb73097`이다. 아래 상태는 구현 진행 중이며 완료 보고가 아니다.

- Plan: 실제 pytest 수집기, 순수 계약 검사기, 오프라인 CLI를 분리했다.
- Do: Task1(Terra/high)·Task2(Astra/high)를 같은 기준의 별도 worktree에서 병렬 작성하고 독립 리뷰 후 개발 브랜치에 통합했다. Task3 CLI(Terra/high)까지 포함한 시험 후보는 `353c1ce6a2642858bda9664dcabc364c20407d4d`다. 시험 workload는 직렬이다.
- See: Task1은 Sol/high 첫 리뷰의 Important2건을 RED로 재현·수정해 재승인, Task2는 별도 Astra/xhigh가 승인했다. 실제 생산자→검사기 인수·전체 UTC/KST는 완료했고, Task3와 전체 변경의 최종 독립 리뷰는 아직 남았다. 미실행 항목은 통과로 추정하지 않는다.

요청 모델·effort와 실제 실행 모델은 구분한다. 현재 도구가 실제 모델/effective effort를 노출하지 않아 미검증이다. 동일 공급자 독립 리뷰는 교차 공급자 리뷰가 아니다.

## 변경 경계

새 파일은 `scripts/dev/{verification_contract,pytest_evidence,check_verification_evidence}.py`와 대응하는 `tests/dev` 세 파일이다. 관련 문서만 추가 갱신한다.
기존 verify/workflow/conftest/제품 src/기존 테스트 단언·source/sequence 후보는 변경하지 않는다.
주문·전략·위험 설정, main, 배포·재시작·실 API 호출은 이번 범위 밖이다.

[성능 위험 수용 원장](recovery-performance-exceptions-2026-09-27.json)의 두 과거 관측은 개발 비차단이다.
원시 실패, 다른 assertion, 새 실패와 timeout을 성공으로 바꾸지 않는다. 기존 단회 성능 진단을 반복하지 않는다.

## 공통 계약의 구현 해석

- `nodes≤20,000`은 각 시험 inventory/results 목록의 건수다. JSON scalar 합계를 제한하는 의미가 아니다. 문서32MiB·깊이12 제한은 별도 유지한다.
- 격리 가드는 실제 pytest root의 `tests/conftest.py`다. 임시 저장소 시험에서도 진짜 guard bytes를 사용하며 제품 import 전에 격리를 설치한다.
- v1은 skip/xfail을 사실대로 기록하지만 승인하지 않는다. 기존 프로젝트 xfail2건은 그대로 유지한다. 따라서 이 도구만으로 실제 전체 suite의 최종 필수 검증을 승인할 수 없다.
- 수집기의 `finished`는 `pytest.main()` 반환을 뜻한다. 이후 OS 종료·atexit·외부 timeout은 미래 controller가 별도로 결속해야 한다.
- 네 슬롯의 오프라인 일치 결과에도 `production_eligible=false`를 고정한다. runtime 지문은 native 자격 인증이 아니며 run tuple은 실제 GitHub 이벤트의 인증이 아니다.

## 작업별 검증 근거

| 작업 | 실제 시험 | 독립 검토 |
| --- | --- | --- |
| 순수 검사기 | 최초 module 부재 RED(exit2), 추가 잘못된 type RED; 리뷰 반례3실패와 guard surrogate1실패 후 최종35 passed, coordinator 재검증35 passed/0.23s·격리0 | Sol/high, Important2건 모두 재리뷰 해소 |
| pytest 수집기 | 구현 전35 failed/11.98s; 최종49 passed/20.92s, coordinator 재검증49 passed/19.98s·격리0 | 구현자와 다른 Astra/xhigh, 지적0 |
| CLI·연결 | 최초 CLI 미구현 RED9실패(이후 제거한 source-text 단언1 포함); 최종 행동 시험9 passed, coordinator 재검증9 passed/16.22s·격리0 | 최종 Astra 리뷰 대기 |
| 관련 묶음 | 새 세 시험 + 기존 `test_verify.py`:98 passed/36.26s·격리0 | 최종 리뷰 대기 |
| 전체 UTC | 보정 실행6014 passed/16 skipped/2 xfailed/4 warnings,550.82s,exit0,격리0 | 최종 리뷰 대기 |
| 전체 KST | 6014 passed/16 skipped/2 xfailed/4 warnings,574.58s,exit0,격리0 | 최종 리뷰 대기 |

검사기 리뷰에서 발견한 것은 bounded JSON의 거대 정수·중첩·surrogate 예외 누출과 네 빈 inventory의 무의미한 통과였다. 정수128자리 상한·반복형 깊이 검사·UTF-8 검증·inventory 최소1건을 적용했다. 기존 시험 결과를 고치는 수정은 아니다. 수집기가 실제 수집 실패의 빈 결과를 원시 기록하더라도 검사기는 승인하지 않는다.

수집기 검증에는 정상/실패 원 rc, setup/teardown 실패, strict/nonstrict XPASS, xfail/skip, collection 오류·deselection·중단, guard 누락/별도 object 중복/alias, 늦은 종료 훅과 unconfigure 오류, 시간대·context 변경, 기존 파일·크기·출력 경로 거부가 포함된다. 실제 외부 접속이나 운영 파일로 부정 사례를 만들지 않았다.

## 사용 계약

개발용 수집기 예시(운영 명령이 아님):

```bash
TZ=UTC python -m scripts.dev.pytest_evidence \
  --verification-context context.json --verification-output receipt.json \
  -- tests/dev/test_verification_contract.py -q -p no:cacheprovider
```

`context.json`은 실행계획의 exact `run`·`slot`만 담는다. 실제 환경의 `TZ`가 context와 일치해야 하며 `PYTEST_ADDOPTS`/`PYTEST_PLUGINS`는 비어 있어야 한다. 출력은 실제 pytest root 내부의 새 파일이어야 한다. 수집기는 임의 시험을 실행하는 개발 도구이며 필수 lane의 선택·자격을 보증하지 않는다.

검사 CLI의 계약:

```bash
python -m scripts.dev.check_verification_evidence --expected expected.json \
  --receipt standard-utc.json --receipt standard-kst.json \
  --receipt proof-utc.json --receipt proof-kst.json
```

기대값은 승인된 시험 목록과 파일·환경 지문으로 독립 작성해야 한다. 실제 receipt를 복사해서 expected를 만드는 자기검증은 금지한다. 검사기는 파일만 읽고 stdout에 decision JSON 하나를 쓴다. exit0은 네 오프라인 증거의 일치, exit1은 거부, exit2는 인자/읽기 오류다. 네 실제 소형 pytest 실행으로 이 연결을 검증했지만 native proof 실행으로 보지 않는다.

검증 중 원래 pytest rc가 nonzero이면 수집기는 발행 오류가 추가돼도 그 rc를 유지한다. 원 rc0에서 발행이 실패하면 wrapper rc2다. receipt만으로 wrapper 프로세스 최종 성공이나 OS-level timeout 부재를 인증하지 않는다.

## 전체 시험의 환경 처분

초기 전체 UTC는 첫 `tests/deploy/test_deploy_scripts.py::test_local_script_defaults_to_read_only_check`에서 `HOME: unbound variable`로 실패했다(1 failed/20.74s,exit1,격리0). 성능 사례는 실행되기 전이었고 성능 예외를 적용하지 않았다.

원인은 env allowlist가 HOME을 제외한 반면, 기존 배포 스크립트가 키의 기본 경로를 HOME으로 확장한 것이다. 해당 시험은 SSH를 임시 가짜 실행기로 대체한다. 따라서 기존 입력 `QWQ_DEPLOY_SSH_KEY`에 새 임시 디렉터리의 존재하지 않는 키 경로를 넘겼다. 실제 HOME/키/SSH·운영 자격을 전달하지 않았다. 같은 단일 시험1 passed/0.13s를 확인한 뒤 별도 전체 UTC를 실행했다. 최초 실패는 삭제하거나 성공 결과로 대체하지 않는다.

전체 명령은 `tests -x`(첫 실패 종료), 시간대별900초, env allowlist, 기존 설치 plugin3개 명시, 직렬 workload다. 기존 `QWQ_RUN_RECOVERY_SCALE=1` opt-in16건은 활성화하지 않았고 기존 xfail2건도 보존했다. 기존4경고(pykrx deprecation1·계좌 lease fork 경고3)를 새 도구의 경고와 구분하며 숨기지 않았다.

## 후속 단계

현재 구현 검증 후에도 source proof 이식과 직접 child 격리, exact runtime qualification, 조건·사유·시험 소스 지문을 결속한 skip/xfail 정책, 실제 OS 결과 controller, 필수 CI·원격 보호 효력 검증은 별도 단계로 남는다.
이 도구 완료를 전체 엔진 설치 또는 운영 승격으로 보고하지 않는다.
