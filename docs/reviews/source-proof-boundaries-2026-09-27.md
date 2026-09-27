# source 증명 수집·격리 경계 — 진행 원장

2026-09-27 KST. 기준 계획 commit `21b65e982a562ef627a1113378413e2eeb22c017`,
통합 후보 `cc18b9ccf8fa10f57f9034c62c4d1011bec85d0b`.
집중시험·실제 수집 대조·독립 broad 리뷰·전체 UTC/KST 검증을 완료했다.
판정은 `SOURCE_BOUNDARIES_VALIDATED`이며 수집·격리·미자격 실행 차단의 개발 한정 완료다.
source108 native 증명·전체 엔진·운영 전환 완료가 아니다.

## 목적과 한계

보존된 source 증명을 일반 pytest 발견과 분리하고, 별도 목록과 실제 수집 결과를 대조한다.
자격 없는 native 실행은 function fixture/seed/SQLite/source 호출 전에 거부한다.
수집은 import를 수행하지만 108개 증명의 call-phase 성공을 의미하지 않는다.
qualified runtime은 여전히0, native 실행0, 운영/제품/CI/main 변경0이다.

- Plan: [설계](../superpowers/specs/2026-09-27-source-proof-boundaries-design.md)·
  [계획](../superpowers/plans/2026-09-27-source-proof-boundaries.md)의 독립 계획 승인.
- Do: 같은 `21b65e9`에서 격리 worktree 두 개, Astra/high 경계 구현과 Terra/high 순수 목록 구현.
  집중시험은 공통 lock으로 직렬화하고 coordinator만 개발 브랜치에 통합했다.
- See: 작성자와 다른 Astra/xhigh(Task1), Sol/high(Task2) 검토를 거쳤다.
  실제 모델/effective effort metadata는 미노출·미검증이며 교차 공급자 검토라고 주장하지 않는다.

## 변경 내용

새 파일5개만 구현했다. 기존 `src`, guard, verify/CI, 시험 단언·성능 임계값은 변경하지 않았다.

1. `tests/proofs/l3_source/l3_source_lease_probe.py`: 원 helper594줄 bytes 보존.
2. `tests/proofs/l3_source/source_lease_cases.py`: 고정 guard 설치·모듈 실체 대조,
   직접 guard-only child·미자격 native 거부, strict outer fault envelope.
   원23개 test 함수/decorator·inner parser·mutation oracle은 보존했다.
3. `scripts/dev/source_proof_inventory.py`: 실제 collection과 독립 작성한 source108·관련31 목록.
4. 대응 `tests/dev` 두 파일: 경계53건과 목록20건.

원 helper와 guard SHA256은 각각
`32725179ac8695fdb8dcb87b969159a306899c238bac355e1050c9e607e1e55e`,
`7b7b26940309a2a165d9bdba7611b6730e83b90ae9ea5f9e725764ee4c0a23f7`다.
원본 별도 source/sequence/frozen 후보를 수정하지 않았다.

## 실제 검증

| 확인 | 실제 결과 |
| --- | --- |
| Task1 RED→GREEN | 최초52실패 → CFFI module 실체·SystemExit 처리 보완 →44실패/8통과 →52통과; 실제 ModuleType subclass 중복 회귀1실패 후 최종53 passed/36.84s |
| Task2 RED→GREEN | module 부재 exit2→12 passed; 리뷰의 lane 타입·상한 검증 반례3실패/17통과→20 passed/0.16s |
| 독립 task 리뷰 | Task1 명세/품질 승인; Task2 필수2·advisory2 보완 후 재승인 |
| 통합 broad 리뷰 | 작성자와 다른 Astra/xhigh 요청 검토 APPROVE_SCOPE, 신규 Critical/Important/Minor0; 전체 검증은 별도 조건 |
| 기존 기본 수집 | 6034개/21.22s |
| 통합 기본 수집 | 6107개/19.70s: 기존6034 모두 보존, 추가73개는 새 두 시험 파일만, source 발견0 |
| 명시 source 수집 | 독립 목록과 exact108개 일치/0.72s |
| 관련 수집 | 독립 목록과 exact31개 일치/1.09s |
| 통합 집중시험 | 새 두 파일+기존 evidence 세 파일168 passed/70.31s, exit0, 격리0 |
| 전체 UTC | 6089 passed/16 skipped/2 xfailed/기존4 warnings,587.25s,exit0,격리0 |
| 전체 KST | 6089 passed/16 skipped/2 xfailed/기존4 warnings,598.56s,exit0,격리0 |

수집 네 실행 모두 exit0·collection_errors0·deselected0·guard 실체1·위반0이다.
receipt의 `finished=true`는 pytest 반환이며 각 phase는 `not_run`이다. 이를 통과 증거로 쓰지 않는다.
집중168건은53+20+36+49+10이다. 후속 OS 계약의 결합 집중시험과 대조하면서
보존된 integrated collection receipt의 exact node를 파일별로 다시 세어 정정했다.
기존 표기의35/11은 이전 중간 결과를 잘못 옮긴 것이며, 원문168 passed와 총수는 변하지 않는다.
이전 단계의 관련100건에는 이번 집중 선택에 없는
기존 `test_verify.py`5건이 들어 있었으며, 전체 수집에서 누락된 시험은 없다.

목록의 canonical JSON SHA256:

- source: `d461b0d8f51ffc2b0c260f20480848aa872e08d94d27e83200406defd5131ef7`.
- related: `4ce0932d927d10b58c182a04fffa5386a41e35dff330efb21ce83df4dd52397e`.

로컬 원시 증거는 `.superpowers/sdd/2026-09-27-source-proof-boundaries/`에 보존하고
Git에 넣지 않는다. 집중 로그 SHA256
`19b3423e23b561f76d7e0fc062c526617744bb3be5fa34721d0cbf46fd7df89e`.
collection receipt SHA256은 source
`c331164fb4c9606028ed18c5f6fd65309e28e09e993469b8b31e0f17ddb9eaac`, related
`ad7a3d20c1e5cf3f47688823a93da0a79f4080804038fa3f254db83334773581`, 통합 기본
`c348116bf2f6b00bb5f10dca0100df50ae1957fbd81a99759a96af32393bf295`다.
독립 broad 보고서 SHA256은
`54f9daab8bb678d6f66211f253637e9ff2abd933e2369942ca894dfccfa07018`이다.

535개 tracked Python 파일을 메모리에서 compile했고 diff check가 통과했다.
tracked 전역 비밀 패턴 검사는 기존 08-18 개발계획의 가짜 private-key 시험 문자열1건을
탐지했다. 해당 문서는 이 후보에서 diff0이며 실제 키가 아니다. 이번 변경 파일 일치0을 별도 확인했다.
기존 `scripts/dev/verify.sh`와 동일한 plans 제외 패턴 검사도 일치0이다.

전체 시험은 coordinator 단독, UTC→KST 직렬, 각900초 cap, 첫 실패 종료 `-x`로 실행했다.
`env -i`에서 PATH/LANG/TZ/PYTHONDONTWRITEBYTECODE/PYTEST_DISABLE_PLUGIN_AUTOLOAD와
기존 fake-SSH fixture용 존재하지 않는 임시 키 경로만 전달했다. HOME/실제 키/운영 환경은
전달하지 않았다. 기존3 plugin을 명시했고 guard/pytest/설정은 변경하지 않았다.
기존16skip은 opt-in scale flag가 없는 고정 조건, strict xfail2는 기존 parity 두 시험이다.
전체 실제 결과의 건수와 해당 기존 파일·조건의 diff0을 대조했으며 개별 phase receipt를 새로
발행했다고 주장하지 않는다. pykrx1·계좌 lease fork3경고는 보존했다.

전체 raw 로그 SHA256은 UTC
`520b3efec732ca44aa7b765bb2f477d686c01f026aad32ed566cb8e6aff8504c`, KST
`046aa6c934a238f36fdab6b15c99828ba5dacd995b0418a69adc7b1f393c18de`다.
검증 뒤 문서 마감 `535e4941ffa25b899bde6f906c8eb3b29e853021`를 feature 브랜치에 푸시했다.
이는 코드 후보 `cc18b9c`와 비문서 파일이 동일하다. main 병합/배포/재시작은0이다.

## 다음 단계

전체 검증·독립 broad 리뷰를 통과해 `SOURCE_BOUNDARIES_VALIDATED`로 개발 한정 마감했다.
다음은 pytest 반환 이후 OS 종료·남은 자식 회수 증거의 한 슬롯 controller다.
exact runtime qualification·조건을 결속한 skip/xfail·필수 CI·엔진 설치는 별도 미완이다.
사용자의 18:00 KST 이후 복귀 전 자율 순차진행 요청은 개발 범위의 정례 확인 대기를 생략하되,
main/배포/재시작/주문/설정·권한 확대를 허용하는 것으로 해석하지 않는다.
