# 로컬 pytest OS 종료·회수 증거 — 진행 원장

2026-09-27 KST. **두 부품 독립 승인·개발 통합 후 실제 결합 인수 진행 중. 전체 단계 승인은 아직 아니다.**

정본: [설계](../superpowers/specs/2026-09-27-os-process-evidence-design.md),
[실행계획](../superpowers/plans/2026-09-27-os-process-evidence.md).
공통 구현 base는 `59fa111c6ff56f09a7a69c91a6c9819d7ca02468`이다.

## Plan

기존 receipt의 `session.finished`는 pytest 반환을 뜻하고 OS 프로세스 종료를 증명하지 않는다.
결과를 쓴 뒤 실패하거나 자식이 남는 경우를 별도 관측하고, 원 receipt와 독립 기대값에 결속한다.
실행 범위는 고정된 local/standard 한 슬롯이며 범용 명령 실행기나 source proof launcher가 아니다.

- 순수 계약 작성: Terra/high, 별도 worktree의 새 모듈·시험 두 파일.
- 프로세스 controller/bootstrap 작성: Astra/high, 별도 worktree의 새 세 파일.
- 중요 계약·수명 검토: 작성자와 다른 Astra/xhigh. 같은 공급자 독립 리뷰다.
- coordinator가 diff·실제 명령 결과를 확인하고 통합한다. 전체 시험은 다른 작업자 없이 직렬이다.

요청 모델·effort는 위와 같지만 실제 모델/effective effort를 증명할 별도 metadata는 미노출이다.
단일 coordinator+최대3workers, 재위임0, focused workload는 공통 파일 lock으로 직렬화한다.
사용자의 자율 순차진행 지시는 반복 확인을 줄이며 미지원 native/운영 경계를 면제하지 않는다.

## Do — 현재 확인한 범위

### 순수 계약

첫 후보 `13d63d7`은 실제 문서 간 producer/guard 지문을 직접 결속하지 않아, 서로 모순된
두 기대 영역과 각각 맞는 증거를 승인할 수 있었다. 독립 리뷰가 현재 정상 fixture의 모순부터
확인했다. 깊은 직접 expectation의 `RecursionError`와 비문자열 launch equality 객체도 발견했다.

후속 `0bf37d7`은 동일 대상 지문을 직접 대조하고 깊은 입력 오류를 정규화하며 launch의 exact
문자열 타입을 검사한다. 원 작성자가 네 실패를 RED로 재현했다. 누락된 특성화 시험과 시험 입력
한 줄을 더 보완한 최종 Task1 `b2cca2f`는 독립 `APPROVE_SCOPE / R1~R4 CLOSED`를 받았다.
coordinator가 전체 diff·리뷰·원문을 읽고 최종 후보를 직접 재시험하고 `e08019b`까지 통합했다.
controller까지 포함한 단계 전체 승인은 아직 아니다.

| 실제 증거 | 결과 |
| --- | --- |
| 후속 결함 재현 | 4 failed / 30 passed, exit1, 격리0 |
| 작성자 Task1 최종 집중 | 73 passed / 0.38s, 보고된 exit0, 원문 격리0 |
| coordinator Task1 최종 새·기존 계약 결합 | 109 passed / 0.52s, 실제 도구 exit0, 격리0 |
| controller 독립 최종 리뷰 | `995d59e`에서 APPROVE_SCOPE, R1·R2 CLOSED |
| coordinator controller 최종 집중 | 64 passed / 24.90s, 실제 도구 exit0, 격리0 |
| 실제 producer/controller/consumer 결합 시험 | 작성 중 |
| 이번 후보 전체 UTC/KST·최종 broad | 미실행 |

coordinator 최종 명령은 Task1 worktree의 정확한 `b2cca2f`에서 다음 형태로 실행했다.
이전 `0bf37d7`의89 passed/0.46s도 원문으로 보존하되 최종 후보 결과로 재사용하지 않았다.

```text
flock -w 240 <coordinator-artifact>/test-workload.lock timeout --signal=TERM 300s
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 <existing-venv>/bin/python -m pytest
tests/dev/test_verification_os_contract.py tests/dev/test_verification_contract.py
-q -p no:cacheprovider -p pytest_asyncio.plugin --tb=short
```

### 실제 OS 관측 부품

별도 독립 하네스가 candidate controller의 직접 자식·입양 자식만 회수한다. fixture는 작고
종료 장치가 있는 고정 코드이며 기존 guard와 producer bytes를 보존한다. PID 번호만으로
신호를 보내지 않고 pidfd와 양성 child wait를 함께 사용한다. 불완전 회수는 통과가 아니다.
첫 후보 `96bccbd`의 집중46건은 통과했지만 독립 리뷰가 두 결함을 발견했다.

- R1: 최초 경로 검증 이후 부모 디렉터리가 교체되면 후행 receipt/결과 파일이 그 경로를
  따라갈 수 있었다. 최초 root/부모 FD를 보존하고 각 component의 nofollow·device/inode를
  대조한 뒤 그 최초 부모 FD 기준으로 파일을 여는 방식으로 수정했다.
- R2: leader 회수·전체 cleanup 완료가 확인되기 전에 receipt를 읽었다. 두 조건을 모두
  충족한 뒤에만 읽도록 제한하고 미완료·실패 사실은 그대로 남겼다.

작성자는 필수 경로 세 실패와 후행 receipt I/O 한 실패를 각각 RED로 보존했다. 선행 소유권
검사 실패의 launch0, 반복 observer 실패의 TERM/KILL·단일 FD 정리, 빈 proc와 wait0,
실제 zombie 입양/회수 및 금지된 Popen 회수 메서드 호출0도 추가 검증했다. 실제 zombie
시험은 post-leader 관측 순서를 강제하지 않으므로 그 정책의 결정적 recorder 시험과 구분한다.
최종 `995d59e`는 독립 한정 재리뷰 승인 후 root도 직접64건을 재실행했다. 승인 두 커밋은
`96c4610`/`b99d051`로 통합했으며 세 파일은 작성자 최종 후보와 byte 단위로 같다.

이것은 최초 검증된 부모와 파일 접근을 묶는 제한된 보강이다. 적대적 filesystem 전반의
불변성·sandbox나 모든 syscall의 하드 실시간 종료를 보장한다고 해석하지 않는다.

## See — 판정 한계와 남은 순서

성공 판정도 `OS_RESULT_BOUND / local_os_process_only`이며 native 자격, CI 출처, 운영 승격은
항상 false다. 기존16skip/2xfail을 이 소비 계약이 승인했다고 보고하지 않는다. source108 실제
call-phase와 qualified runtime은 여전히0이고 기존 source 실행 차단을 바꾸지 않는다.

남은 순서: 실제 producer/controller/consumer 결합 → 독립 broad 검토 →
root 단독 UTC/KST 전체 → 문법·비밀 패턴·불변 지문 대조 → 문서 마감·feature push다.
다음 runtime 등록 계약은 순수 문서 일치+빈 실제 등록부로 제한하며 이 단계 검증 뒤 착수한다.
cold source 후속 문서 준비는 별도이며 native/decoder/실제 소비자 증명을 대신하지 않는다.

이번 작업은 main·운영 서비스·주문·설정·외부 API·설치·권한·CI 활성화를 변경하지 않는다.
운영 상태를 새로 조회하지 않았으므로 과거 PID/배포 상태를 현재 사실로 재확인했다고 하지 않는다.

원문·리뷰·실제 exit 기록은 coordinator worktree의
`.superpowers/sdd/2026-09-27-os-process-evidence/`에 보존한다. 첫 Task1 원문만 해당 작성자
worktree의 같은 하위 경로에 있으며 후속과 coordinator 원문은 coordinator 경로에 있다.
이 문서는 진행 기록이며 완료 시 최신 정확한 후보·검증 결과로 갱신한다.
