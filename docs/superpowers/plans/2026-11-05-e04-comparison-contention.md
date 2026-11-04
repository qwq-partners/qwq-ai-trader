# E04-COMPARISON-CONTENTION — 배포 선행 실패 조사

기준 main4d0f2ad2, 운영a399c297. 사용자 다음 작업·문서/커밋/푸시·운영배포 승인 범위에서 새 main CI 실패를 먼저 해결한다. 완료 검증을 맹목 반복하거나 실호출·운영 제어를 바꾸지 않는다.

## Plan

main run41870477873은 정책 간 동시 일 상한 검사에서 기대한 comparison_daily_attempt_cap 대신 comparison_storage_unavailable을 받았다. 실제 하위 예외·트랜잭션 단계·임시 DB 상태를 확보해 원인을 입증한다. 단독 통과만으로 플레이크 해결을 주장하지 않는다. 신규 실패 사례와 결정적인 합성 회귀만 실행하며 과거 전체 검사 재실행은 하지 않는다.

## Do

별도 구현 worktree에서 execution/policy_registry와 관련 합성 검사만 작성한다. 원인 미확정이면 추정 수정하지 않는다. 확인되면 RED→최소 수정→GREEN. 글로벌/실험별 상한·원장 소유권/정산·unknown·기존 기록·완료 기준·공급자 시한/재시도·설정/KILL은 유지한다. 대기 시간 연장·일반 재시도·검사 완화로 통과시키지 않는다. 다른 파일의 변경 필요성은 coordinator가 먼저 범위를 검토한다.

## See

실제 예외/코드/경합 단계, 합성 재현·관련 검사, 독립 critical 리뷰를 기록한다. 새 소스의 최종 실제 WSL 직렬 전체 통과 후 푸시/PR/필수 CI·고정 HEAD 병합/main CI를 확인한다. AWS 전체는 WSL을 대신하지 않는다. 동일 소스의 과거 완료 검증과 새 수정 검증을 구분한다.

그 뒤 [WSL 정본 배포](../../operations/lightsail-deployment.md)의 check→고정SHA deploy→실제 재시작150초 후 postcheck를 수행한다. 운영 checkout을 미리 옮기지 않고, 새 PID/NRestarts0·broker연결·recoveryready·pending[]·보호파일/cron/KILL·worker/R1OFF를 대조한다. 원장에 실패/수정/리뷰/WSL/CI/운영 인수와 NEXT를 함께 남기고 결과 문서만으로 재배포하지 않는다.

## 조사 중 확인한 경계

통제 writer 보유와 동일 순서의 무주입 재현에서 `_admit` BEGIN IMMEDIATE의 SQLITE_BUSY/code5를 확인했다. 원 CI는 하위 예외가 없어 같은 원인이라고 단정하지 않는다. 정상적인 bounded lock 경합이면 운영 코드 대신 검사의 전제를 고친다: 첫 admission이 commit되고 의견은 미완료인 동안 다른 정책의 입장을 시작해 정확한 글로벌 cap을 검증하고, writer 경합의 storage 차단/전송0은 별도 검사로 고정한다. 모든 실패를 허용하는 단언 완화는 하지 않는다. 종료 뒤 잠금 잔존 결함이 발견되면 소스 수정 필요성을 먼저 검토한다.

## 구현 결과

테스트2파일만 수정했다. old/new 승자별 cap/실제writer 경합4건과 관련6파일206건이 통과했다(24.00초·격리0). 운영 소스·0.1초 잠금 대기·시한·정산·재시도 변경0. CI 당시 원인은 미확정이며 동일 순서 합성에서만 실제 SQLITE_BUSY를 확보했다. 독립 리뷰/실제 WSL/CI/배포 결과는 정본 원장에 이어 기록한다.
