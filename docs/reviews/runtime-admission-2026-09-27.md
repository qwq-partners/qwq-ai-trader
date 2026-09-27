# Runtime 등록 대조 — 개발 인수 원장

2026-09-27 KST. **부품 독립 승인·feature 통합·전체 UTC/KST 검증 완료.**
설계/계획은 [순수 계약](../superpowers/specs/2026-09-27-runtime-admission-contract-design.md)과
[실행계획](../superpowers/plans/2026-09-27-runtime-admission-contract.md)이다.
공통 시작점은 `af098769d7095c5630bbaa7c98301794ad213fd5`다.

## Plan → Do

Terra/high가 순수 parser/digest/대조 부품과 합성 시험, 빈 실제 registry 세 파일만 작성했다.
저자 후보 `c73de3d`의37건 통과 뒤 다른 Astra/xhigh가 세 중요 결함과 깊이 정의 불일치를
발견했다. 직접 dict의 canonical 크기 제한 누락, binding key subclass 허용, 구현 함수에
기댄 기대 hash/누락된 경계 시험이다. 실제 실행·신뢰 플래그는 처음부터 false였다.

별도 Terra/high 보완 `7606440`은 크기·키·깊이 검사와 독립 고정 fixture·7축 변경·각 문서
크기/오류 우선 행렬을 고정했다. 첫 RED는 fixture shape 오류가 섞였지만 수정된 유효 입력에서
R1의 초과 문서 수락과 R2의 잘못된 key 승인을 각각 실제 실패로 재현했다. 재리뷰가 발견한
invalid mode 시험 누락은 coordinator의 `697846f`에서7사례로 보완했다. 제품 추가 변경은 없다.
동일 리뷰어가 최종 R1~R4 CLOSED·새 지적0, Spec/Quality 승인했다.

통합 `7f16385`·`082756c`·`aa4ab4c`의 세 파일 bytes는 최종 승인 후보와 동일하다.
요청 모델/effort와 actual metadata 미노출을 구분한다. 같은 공급자 독립 리뷰이며
cross-provider 승인으로 표시하지 않는다. 구현자·보완자·승인자 역할을 구분한다.

## See — 현재 증거

| 범위 | 결과 |
| --- | --- |
| 최초 writer | 37 passed/0.46s·기록된 exit0/격리0; 전체 계약 승인 아님 |
| 보완의 유효 입력 RED | 의도 실패2/51 deselected·기록된 exit1/격리0 |
| 보완 writer | 53 passed/0.64s·기록된 exit0/격리0 |
| root `7606440` | 53 passed/1.12s·실제 도구 exit0/격리0 |
| root `697846f` | 60 passed/1.06s·실제 도구 exit0/격리0 |
| 통합 결합 `aa4ab4c` 코드 | 610 passed/164.68s·실제 도구 exit0/격리0 |
| 전체 UTC `18a2196` 코드 | 6329 passed/641.35s·16 skipped/2 xfailed/4 warnings·실제 도구 exit0/격리0 |
| 전체 KST 동일 코드 | 6329 passed/643.67s·16 skipped/2 xfailed/4 warnings·실제 도구 exit0/격리0 |

작성자·앞 한정 리뷰어와 다른 Astra/xhigh가 `af09876..a130330` 전체 delta를 검토했다.
필수 지적0, 문서 자문M1은 `18a2196`으로 닫고 새 C0/I0/M0·Spec/Quality 승인이다.
worker 종료 후 coordinator만 두 전체를 직렬·각900초 cap으로 실행했다. 검증 중 변경은
계획 진행 체크 문서뿐이며 src/scripts/tests/config/CI bytes는 `18a2196`과 같다.
기존 skip/xfail/경고를 숨기거나 새 예외를 추가하지 않았다. compile-only545와 비밀 패턴·
diff·원 source/guard/성능 예외/controller 지문도 확인했다. 이 기록은 개발 인수다.

첫 writer의 모듈 부재 RED는 당시 tool 기록만 있고 완전 raw 파일은 없다. 보완 중 첫 GREEN의
NameError 파일도 후속 실행에 덮어써졌다. root는 덮이기 전 읽었던 출력 excerpt만 사후 보존했으며
이를 완전한 최초 원문으로 주장하지 않는다. 보고서의 commit0/invalid mode 완료/원문 보존
문구는 사실과 맞게 정정했다. 새 GREEN으로 이 절차 공백을 숨기거나 과거 RED를 재창작하지 않는다.
최종 root 집중 실행은 공통 lock·180초 cap·env-i·pipefail+tee로 동시에 원문을 저장했다.
전체는 같은 환경/plugin/lock 아래900초 cap과 `tests -x -q`를 사용했다.
실제 원문은 `full-utc-18a2196.log`, `full-kst-18a2196.log`이며 pytest 출력과 도구 exit는
`coordinator-validation-ledger.md`의 후보/cwd/명령 기록과 함께 읽는다.

## 한계와 다음

실제 `docs/verification/source-runtime-registry.json`은 entries=[]다. 성공도 내용의
`CONTRACT_MATCH / offline_runtime_contract_only`이며 trust/native/source_execution/
production은 항상 false다. parser는 파일/환경/Git/OS/API/native를 읽거나 호출하지 않는다.
기존 source 차단·guard·OS controller·CI·제품 설정을 이 부품에 배선하지 않는다.

실제 capsule 원본/빌드·loader closure·격리 관측·독립 native 승인·신뢰된 registry revision·
원격 필수 gate가 필요하다. 현 host 버전 또는 합성 qualified fixture로 이를 대신하지 않는다.
source108 call-phase0·qualified runtime0, main/운영·주문·설정 변경0이다.

raw/리뷰/실행 원장은 coordinator `.superpowers/sdd/2026-09-27-runtime-admission-contract/`의
`task-1-*`, `coordinator-*`에 보존한다. 구조 실험의 후속 계획은 별도이며 이 계약 승인이
실제 allocator/consumer/cold 인수를 열지 않는다.
