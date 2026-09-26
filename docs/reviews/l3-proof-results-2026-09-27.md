# L3-P0 source·sequence 증명 실행 결과

2026-09-27 KST. **시험용 부품의 한정 검토 완료·전체 인수 차단. 단회 진단은 미재현·미확정으로 종료.**

정본: [실행계획](../superpowers/plans/2026-09-27-recovery-lifecycle-proof.md),
[설계 제안](../superpowers/specs/2026-09-27-recovery-build-lifecycle-design.md),
[N5 진행 원장](recovery-projection-progress-2026-09-27.md).

## Plan

사용자가 실행계획을 확인하고 진행을 지시했다. 두 시험용 부품은 공통 base
`7208acc8358e1c08bd7725d5d64d5059c24cd65b`에서 시작한다. 계획 SHA256은
`1c494e9c5beb9eded2e9addee404a3c8ff8d3ec846f10625f4a593fc69728418`이다.

- source: `feature/l3-source-proof-20260927`, 새 source helper·시험 두 파일만.
- sequence: `feature/l3-sequence-proof-20260927`, 새 sequence helper·시험 두 파일만.
- 구현 요청 Astra/high 두 명, 독립 critical 리뷰 요청 Astra/xhigh(작성자와 분리).
  검증 환경 정적 대조 Sol/high 요청. 실제 모델/effective effort metadata는 미노출·미검증이며
  같은 공급자의 독립 검토를 cross-provider 검증이라고 하지 않는다.
- 구현은 독립 worktree에서 병렬, workload는 coordinator 단일 슬롯으로 직렬 실행한다.
  제품 src·기존 시험·store schema·GC/보존 정책·설정은 수정하지 않는다.

## Do

두 작성자가 초기 import 부재 RED 뒤 시험용 구현을 작성했다. sequence의 저장된 알려진 예외가
노드를 붙잡는 회귀를 4 failed / 1 passed로 재현하고 고정 오류 envelope 수정 뒤 5 passed로
확인했다. source의 페이지 이동에서 남은 참조 수 오산을 1 failed / 1 passed로 재현해 수정했다.
source 전체 집중 시험은 두 번 모두 180초 timeout(exit124)으로 끝났으며 통과로 세지 않는다.
두 번째 실행은 관측기의 중간 root edge 누락과 Python3.12 opcode 관측 활성화 문제였다.
유한 RED 뒤 이를 보완한 집중3건은 통과했다. SQLite cursor 내부 참조도 독립 검토에서
계수 누락으로 확인했다. 닫힌 cursor metadata만 최소6 edge를 해제하는데1로 세므로
budget4를 만족하지 못한다. 해당 후보는 **PROOF_FAIL**이며 기존 SQL 작업자 내부에서
native 수명을 완결하는 보정안을 독립 설계 검토 후 구현했다. 모든 SQL cursor의 실제 사망,
metadata alias 부재, 최초 foreign 오류 보존과 cleanup proof 보류를 요구한다. 검증된 fixture의
기본 `detect_types=0` 생성 이력에 조건부인 시험이며 임의 warm connection 인증은 미지원이다.
기존 핸들 시험은 취소 waiter가 남긴 참조와 실제 마지막 핸들을 섞어 전제를 위반했다.
유한 RED로 분리한 뒤 shield 취소 시험과 마지막 핸들 해제 시험을 각각 확인했다.
helper는 변경하지 않았고 정확한 기존 참조 보유자는 미확정이다. 수정 후보의 source79건과
기존 관련3파일은 **110 passed / 89.06s / exit0 / 격리0**였다. 전체 독립 리뷰에서
child 결과의 중복 key·잘못된 case와 변이 결과의 단순 불일치 판정을 지적받았다.
실제 잘못된 입력20개 수용을 RED로 고정하고 정확한 schema/변이별 기대 사실/두 task의
실제 성공 종료를 검증하도록 고쳤다. 집중38건 및 새 후보 관련 회귀
**139 passed / 91.17s / exit0 / 격리0**다. 독립 재리뷰는
`APPROVE_TEST_ONLY_CONDITIONAL_SLICE`로 보완을 승인했다. 이는 시험 증거 수용 결함이며
고정 child가 실제 malformed 출력을 생성하거나 제품 gate가 열린 것을 재현한 것은 아니다.
native 증명은 exact CPython3.12.3 한정이다. CI의 Python3.12 micro가 고정돼 있지 않아
다른 patch에서의 지원·통과를 주장할 수 없으며 이 작업에서 CI/버전 guard를 바꾸지 않는다.

sequence도 최초 독립 리뷰에서 **CHANGES_REQUIRED**였다. 커서 등록 실패 후 미등록 참조가
남는데도 정상 정리로 표시되는 경계, 보관된 IndexError의 빈 노드 수명, fault 변이 시험이
실험 timeout·종료 실패까지 검출 성공으로 세는 경계를 수정했다. 두 코드 경계는 독립
합성 probe로 재현했고, 후속 회귀 RED는 11 failed / 1 passed다. 빈 노드 수명 문제를 큰
payload 누수나 연쇄 해제 증거로 과장하지 않는다. 수정 뒤 해당 집중15건은 통과했고,
독립 재리뷰는 `APPROVE_FIX1_FUNCTIONAL_SLICE_ONLY`로 원 지적을 해소했다고 판정했다.
그러나 최종 관련 회귀는 **1 failed / 175 passed / 179.46s, 외부 exit124**다. 신규59건은
통과했으나 기존 controlled100k에서 index 단계33.173999749ms가5ms를 넘었다. 원인 인과는
미확정이며 같은 후보를 재실행하지 않는다. full UTC/KST·코드 commit/push·통합은 보류한다.

전체 suite와 코드 commit/push는 coordinator가 검증·리뷰 증거를 확인한 뒤만 수행한다.
실패·미완료 상태를 남기는 이 문서들은 별도 독립 검토 후 docs-only 커밋 대상이다.
cold-open/JSON decode/full builder/실제 owner 배선은 이 작업 밖이다.

## See — 실제 결과와 미실행 구분

| 검사 | 현재 결과 |
| --- | --- |
| 공통 base의 store/gate/policy registration/projection 관련 기준선 | 148 passed,69.64s,exit0,격리0 |
| Task1 RED/GREEN/독립 리뷰 | 초기 native FAIL; 보완 후 계획 회귀139 passed/91.17s/exit0·조건부 구조 승인, CI 지원 충돌 별도 |
| Task2 RED/GREEN/독립 리뷰 | 기능 수정 한정 재리뷰 승인; 최종1 failed/175 passed·exit124로 전체 차단 |
| 별도 진단 도구 시험 | 지적 보완 후27 passed·16 subtests passed/0.28s/exit0·격리0, 독립 구현 검토 승인 |
| 별도 단회 index CPU 진단 | INCONCLUSIVE_NOT_REPRODUCED; 원본1 passed/12.78s/exit0·격리0, 기존 실패 면제 아님 |
| 결합 후보 전체 UTC/KST | 미실행 |
| 최종 broad 리뷰·코드 commit/push | 미실행 |

현재 미커밋 후보 지문(모두 base `7208acc…`, 새 파일만):

| 파일 | SHA256 |
| --- | --- |
| source helper | `32725179ac8695fdb8dcb87b969159a306899c238bac355e1050c9e607e1e55e` |
| source tests | `35906c3c7b59c8addb852f97bf26c43621756ddb7f18f54ca98412bcfe8771a8` |
| sequence helper | `ea8b961f67228e17b2108b87c256f50417c662495867cda62d3d67067f59b60e` |
| sequence tests | `fdcf8ed26482009ace8b7eabf063826accbfb18d05e3f864729cb84635f56e6a` |

기준선 명령(새 source worktree, 새 파일 작성 전):

```bash
timeout --signal=TERM 180s env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC \
  PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_execution_state_store.py tests/test_execution_owner_ticket_gate.py \
  tests/test_execution_policy_registration_boundaries.py tests/test_recovery_projection.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

이는 clean Task1+Task3 base의 회귀 결과이며 frozen Task2의 성능 재시험이 아니다.
**기존 controlled5k13.023871ms FAIL/6UNRUN, 이전 raw/policy/100k 실패와 전체 엔진 전환 차단은
그대로다.** 시제품 구조 검증으로 latency/GC/allocator·운영 readiness를 승인하지 않는다.

이번7208acc 기반 새 후보의 controlled100k 실패와 frozen721fe1c7 후보의 controlled5k 실패는
서로 다른 증거다. 새 실패의 baseline은1.229285099ms, overrun13건, 해당 측정 GC0이었다.
정상 baseline만으로 실행 도중 scheduling/allocator 영향이나 새 시험의 인과를 확정하지 않는다.
새로 발생하는 첫 index 초과 구간에서 thread CPU와 경과 시간을 분리하는 한정 진단을
별도로 설계·구현했다. 독립 검토에서 invalid 결과의 exit0 수용과 관측기 비용 대조의
wrapper 누락을 지적받아 RED로 고정·수정했다. 실제 제품 호출과 동일한 wrapper를
512회씩 전후 대조하고, 불완전한 결과는 성공 종료하지 못하도록 했다. 최종 도구 시험은
27 passed·16 subtests passed/0.28s/exit0이며 독립 재리뷰는 `APPROVE_IMPLEMENTATION`이다.

### 단회 진단의 최종 결과

clean base `7208acc…`의 기존 controlled100k 한 셀을 오프라인에서 **정확히 한 번** 실행했다.
제품·기존 시험 파일은 수정하지 않았고, 외부 관측기가 호출 경계만 임시 감싼 뒤 복원했다.
Python3.12.3·pytest9.0.3, 고정 환경·입력·5ms 기준·기존 GC 조건을 유지했다.
30초 child 상한과 TERM/KILL 각1초, 출력2MiB 상한 아래서 정상 종료·회수됐다.

| 단회 관측 | 실제 값 |
| --- | --- |
| 원본 pytest / driver / supervisor | 1 passed/12.78s; 종료 코드 모두0; 격리 위반0 |
| baseline / 원본 바깥 측정 최대 advance | 1.649404ms /4.204637ms |
| real advance 호출 수 / 선택된 첫 초과 사건 | 19,213 /없음(`first=null`) |
| 전후 관측기 비용 최대 | 33.768µs /25.826µs, 각50µs 이하 |
| 전후512회 비용 합계 | 4.940281ms /3.673761ms, 각5ms 이하 |
| 보고·복원·완료 / 출력·프로세스 회수 | 모두 유효; overflow 없음 |
| 최종 판정 | **INCONCLUSIVE_NOT_REPRODUCED — 미재현·원인 미확정** |

이 실행에서는 판정할 첫 초과 사건이 없으므로 CPU 지배·스케줄링·호스트 원인을 주장하지
않는다. `1 passed`는 이 계측 실행의 결과일 뿐, 이전33.174ms·13.023871ms 실패를 해소하거나
전체 성능 인수를 통과한 것이 아니다. 프로토콜을 종료했으며 추가 측정·재시도는 하지 않았다.
다음 성능 작업은 원인을 구분할 **새로운 관측 질문과 독립 검토** 또는 명시적인 인수 계약
결정이 필요하다. 같은 후보를 다시 실행해 통과값을 고르는 방식은 사용하지 않는다.
별도의 Astra/xhigh 독립 결과 대조도 `VALID_PROTOCOL_COMPLETION /
INCONCLUSIVE_NOT_REPRODUCED`로 이 해석을 확인했다. 추가 workload는 실행하지 않았다.

원문은 artifact의 `baseline-perf-diagnostic-once.log`이며 SHA256은
`c8ea8f5211afca77cd73dd433620b24cfa43dc2d5f760e04c25070318b79f49c`다.
진단 driver SHA256 `e3caa81ecd74233b3128f0b29c4cf4334b090c15157e0c87cf049576109f8e20`,
supervisor SHA256 `8f2257374c3c06063384d054f15a0a8548024f851877ccceefac902b4b5baae9`를
실행 전후 대조했다. validation checkout의 HEAD·세 고정 blob·clean 상태도 변하지 않았다.

## 증거·안전 경계

각 brief/report/검증 로그/리뷰/진행 ledger는 문서 worktree의
`.superpowers/sdd/2026-09-27-recovery-lifecycle-proof/`에 보존한다. 이 경로는 Git 제외이며
정리 시 근거를 먼저 보존해야 한다. 과거 다른 계획의 artifact를 수정하거나 삭제하지 않는다.

main·운영 서비스·주문·설정 변경0이며 이번 작업에서 운영 상태를 조회하지 않았다.
source·sequence의 관측 증명이 실패하면 PROOF_FAIL/미확정으로 기록하고 제품 통합하지 않는다.

## 남은 순서와 자동 진행 경계

1. source 조건부 구조 검토는 끝났지만 지원 Python 범위와 기존100k 성능 실패는 먼저 닫아야 한다.
   139 passed나 sequence 기능 한정 승인을 전체 인수로 바꾸지 않는다. 단회 진단 종료 후에도
   두 차단 조건은 그대로이며, 다음 단계 구현은 미시작이다.
2. 두 증명 및 선행 검증이 통과하면 cold `_open/_load`·factory 선행 load의 소유권 지도와
   RED 계획을 작성해 독립 검토한다. decoder·실제 소비자 구현을 같이 시작하지 않는다.
3. 이후 allocation-time decoder → 실제 consumer 반납/tuple·JSON 호환 → full managed
   lifecycle 순서로 계약·시험을 닫는다. 새 제품 API/허용 파일 확대는 별도 명세·승인 대상이다.
4. 원 계획 Task4–15는 owner token → commit → restore → writer inventory → 송신 권한 →
   경제/체결 → 일일/quote/recovery → 정책/위험/레짐 → runtime → indexed producer →
   A-B-A-B →63셀 인수 순서다. Task2 성능·L3 수명이 먼저 해결돼야 한다.

사용자의 순차 자동 진행 지시는 반복 확인을 줄이는 지시다. 인수 임계 변경, 실패 면제,
제품 전환·main·운영 권한 확대는 뜻하지 않는다. 같은 실패 후보의 재측정으로 통과값을
고르지 않는다. 문법4파일·제품 import0·해당 신규 파일 비밀 패턴 검사는 통과했지만
전체 UTC/KST·broad review·코드 커밋/푸시는 아직 미실행이다.

Python 지원 충돌도 읽기 전용으로 검토했다. 현재 두 파일 계약 안에서는 자동 해소되지 않는다.
runner별 native capability를 새로 증명하거나, 별도의 **필수** 조건부 proof job으로 나누는
명시적 지원/실행 계약이 필요하다. 두 번째 방식도 standard full suite와 proof job이 모두
필수여야 하며 skip/xfail/선택 실행으로 대체하지 않는다. 정확한 micro로 CI를 고정하는 선택은
build·의존성·보안 지원 근거가 없어 권고하지 않았다. 어떤 선택도 채택·설정 변경하지 않았다.

## 적용한 진행 판단과 틀렸을 때의 비용

- 독립 구현 둘은 격리 worktree에서 병렬: 승인된 계획과 사용자 모델 배분 규칙을 따른다.
  독립성 판단이 틀리면 중복/충돌 변경을 재작업하며 자동 병합하지 않는다.
- 무시되는 artifact도 보존: 미완료 작업 인계와 실패 원문 보존을 우선한다.
  불필요했더라도 디스크 사용만 늘며 증거를 삭제하지 않는다.
- 리뷰 전 커밋 대신 정확한 미커밋 diff/hash 제공: 검증 뒤 커밋하는 계획을 따른다.
  후보가 움직였으면 해시 대조 후 재리뷰가 필요하다.
- 범위 안의 작은 후속 판단은 추가 확인 없이 진행: 사용자의 명시 지시를 따른다.
  판단이 틀리면 미통합 시험 후보를 재계획하며 운영으로 넘기지 않는다.
