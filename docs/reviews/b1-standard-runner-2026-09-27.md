# B1 표준 실행기 — 별도 프로필 진행 원장

2026-09-27 KST. **계획 한정 승인, 구현·실제 프로필 검증 전이다.**
[설계](../superpowers/specs/2026-09-27-b1-standard-runner-design.md)와
[계획](../superpowers/plans/2026-09-27-b1-standard-runner.md)을 따른다.

## Plan

기존 실행기의 8MiB/v1 동작은 보존하고, 고정 B1 프로필에 스트림별 2MiB 보존·초과 감지,
공통 lock·격리 환경·종료/회수·900초 전체 상한을 결속한다. 기존 owner를 재사용하며
새 reaper·임의 상한/명령/환경 입력은 추가하지 않는다. `B1_PROCESS_BOUND`는 프로세스
증거 결속뿐이며 시험 결과/skip/xfail·native·CI·운영 승인으로 쓰지 않는다.

Sol/high 요청 조사와 Astra/high 요청 설계 뒤 별도 Astra/xhigh 요청 reviewer가
호출별 도구 대기시간은 전체 실행시간이 아니라는 I1을 지적했다. 같은 호스트의 독립
monotonic 전후 관측·연결된 원문을 이용한 보수적 상한으로 바꾸고 재리뷰에서
**APPROVE_RUNNER_PLAN_ONLY**, C0/I0/M0이다. 빈 시간 차감·더 좋은 수치 재실행은 없다.
실제 모델/effective effort metadata는 미노출·unverified이며 같은 공급자 독립 리뷰다.

## Do → See

독립 literal 계약 시험/RED → 순수 v2 검사 → 별도 controller/독립 시험 → critical 검토
→ worker 종료 후 실제 소규모 프로필 관측 → 기존 표준 전체 UTC/KST 순서다.
지금은 코드·시계 관측·새 프로필 시험 성공을 주장하지 않는다. 원문은
`.superpowers/sdd/2026-09-27-decoder-followup/`에, 후속 실행 원장은
`.superpowers/sdd/2026-09-27-b1-standard-runner/`에 보존한다.

N4097은 별도 의미 전이·전수 oracle·세 실행 예산의 승인과 결과 전까지 UNRUN이다.
현재 B1b 구성 시험과 파일 소유권을 분리한다. 기존 성능 예외·guard·제품·CI·main·운영·
주문·전략·위험/KIS/Toss 설정은 변경하지 않는다.
