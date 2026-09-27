# 로컬 pytest OS 종료·회수 증거 — 진행 원장

2026-09-27 KST. **개발 단계 검증 완료: 독립 리뷰·보완 재승인 후 통합 `9cf021f`에서 전체 UTC/KST 각각6265 passed·격리0을 확인했다. native 자격·CI·운영 승인은 아니다.**

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
| 실제 producer/controller/consumer 결합 시험 | 최종 `a77227f`, scoped APPROVE_SCOPE |
| coordinator 통합 관련9파일 | 313 passed / 115.36s, 실제 도구 exit0, 격리0 |
| fresh broad `535e494..da2bb0b` | CHANGES_REQUIRED: P1 1건, P2 1건 |
| broad 보완 `ffcc384` RED | 4 failed / 32 passed, exit1, 격리0 |
| 작성자 보완 집중 | 176 passed / 51.31s, exit0, 격리0 |
| coordinator 보완 후보 관련9파일 | 349 passed / 124.84s, 실제 도구 exit0, 격리0 |
| broad 한정 재리뷰 `ffcc384` | Spec/Quality APPROVE_SCOPE, R1/R2 ADDRESSED, 새 지적0 |
| 첫 전체 UTC `26dd5ae` | 1 failed / 163 passed / 123.49s, exit1, 격리0 |
| 전체 수집→실패 node 단독 진단 | OS tasks4/children 없음, 같은 실패1/6282 deselected/21.16s, exit1, 격리0 |
| 시험 전제 최종 보완 `329b1e3` | 독립 Spec/Quality APPROVE_SCOPE, containment R1 CLOSED |
| 작성자 최종 controller / root 전체수집 선택 | 100 passed/32.60s / 11 passed/20.11s, exit0·격리0 |
| 최종 전체 UTC `9cf021f` | 6265 passed / 641.79s, 16 skipped·2 xfailed·4 warnings, 실제 exit0·격리0 |
| 최종 전체 KST `9cf021f` | 6265 passed / 639.81s, 16 skipped·2 xfailed·4 warnings, 실제 exit0·격리0 |

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

### 실제 세 부품 결합

Sol/high 작성의 새 시험은 parent3개 안에서 고정 tiny repository11개를 실행한다. 정상 UTC/KST,
실제 assertion 실패, 유효 receipt 뒤 exit9/SIGTERM/hang/setsid 자손, skip/strict xfail,
두 실행의 receipt/process 교차 결속을 확인한다. 기대 node·run·slot·runtime/source/실행파일 지문은
실행 전에 독립 계산하며 실제 receipt에서 복사하지 않는다. 보존 stdout/stderr의 bytes/hash도 대조한다.

최초 실행은 기존 두 구현 위의 GREEN 특성화였다. 독립 scoped 리뷰의 실패 receipt 단언 누락을
보완해 literal node의 passed/failed/passed와 `CALL_FAILED`·`SESSION_EXIT_NONZERO` 보존까지
고정한 `a77227f`가 재승인됐다. 최종 두 커밋은 `ab636e4`/`da2bb0b`로 통합했고 root의
관련9파일313건(기존100+source 경계73+새 OS140)이 통과했다. 제품·기존 계약·guard 변경0이다.

첫 통합 집중 명령은 실제 파일이 아닌 `tests/dev/test_verify_script.py`를 목록에 넣어 exit4로
시험0건이었다. 실제 경로 `tests/dev/test_verify.py`로만 바로잡아 별도 로그에 실행했다.
처음 원문은 보존했고 이를 제품 회귀나 통과로 분류하지 않는다. 작성자 최종11개 실제 case의
임시 파일은 pytest 자동 보존기한 전에 별도 tar로 보존했다(SHA256
`af0798805a5a925145bf05c295a9304d820320f9e5bf921a1915d177d97dd27a`).

### fresh broad에서 확인한 후행 중단 경계

작성자·앞 한정 리뷰어와 다른 Astra/xhigh가 전체21파일 delta를 읽고 추가 P1 한 건을
발견했다. 자식 회수 완료 뒤 후행 코드 지문/receipt 읽기 중 받은 중단은 flag만 세우고
최종 판정에 반영하지 않아 성공으로 기록될 수 있다. 기존313건은 이 구간을 검사하지 않았다.
별도 Astra/high writer가 같은 후보에서 fake signal callback으로 RED를 고정하고 발행 판정의
명시적 cutoff 전 중단을 거부하도록 `ffcc384`로 보완했다. 실제 child rc·cleanup과 기존 최초 오류는
보존하며 새 회수/신호 권한이나 마지막 명령까지의 불가능한 원자성을 요구하지 않는다.

P2는 decoder 방향 문서의 헤더가 실제 방향 한정 승인 이후에도 검토 대기로 남은 기록 오류다.
같은 보완에서 해당 헤더만 정정하며 구현/native 승격은 하지 않는다. 한 차례 fix wave 뒤
원 전체 리뷰어가 R1/R2와 새 delta만 재검토해 두 지적 모두 해소·신규 지적0으로 승인했다.
수정은 `26dd5ae`로 통합했고 후보의 code/test/spec bytes는 같다. 새36사례는 두 신호·여섯 시점·선행
오류3종을 대조한다. cutoff는 후행 검사를 마친 뒤 flag를 읽는 단일 시점이며 뒤의 결과 문서
구성·serialization·create/write까지 신호 처리가 원자적이라는 뜻은 아니다.

coordinator도 `ffcc384`에서 관련9파일349건을 재실행했다. 전체541 Python파일의 메모리
compile·비밀 패턴·diff 검사는 통과했고 원 producer/guard/성능 예외 원장 지문은 같다.
이 결과를 전체 suite나 source/native 통과로 대체하지 않는다.

### 필수 전체 시험에서 새로 발견한 unit 전제 누락

전체 수집 후 pytest 프로세스에 OS task가4개 있었다. 기존 자식의 waitid non-consuming
검사를 의도한 unit은 task/proc 전제를 fake로 고정하지 않아 그 앞의 단일task 검사에서
ValueError로 종료됐다. `observed=[]`가 원문 실패이며 전체 수집→해당 node만 선택한
진단에서도 재현했다. 특정 dependency를 스레드의 원인으로 확정하지는 않았다.

실제 controller의 단일task 거부는 유지했다. Terra/high writer가 같은 preflight의
세 unit을 명시적 선행 입력과 표적 호출 단언으로 격리했다. 첫 `86e3752`의 독립 리뷰는
목표 거부가 사라졌을 때 뒤의 실제 proc 오류로 거짓 통과하거나 부모의 OS 설정에
도달할 수 있는 fixture 공백을 발견했다. `329b1e3`는 표적 뒤의 OS 경계를 삼켜지지
않는 AssertionError로 막고 실제 harness는 그대로 유지해 재승인됐다.

저자 최종 controller100건과 전체수집 선택11건이 통과했고, root도 최종 후보에서
전체수집 선택11건/20.11s/실제 exit0/격리0을 확인했다. 승인 두 커밋은
`ff9795b`/`9cf021f`로 통합했으며 code/test bytes는 같다. 기존 전체 실행 실패를
성능 예외로 면제하거나 scoped 승인만으로 전체 통과로 대체하지 않는다.

## See — 판정 한계와 남은 순서

성공 판정도 `OS_RESULT_BOUND / local_os_process_only`이며 native 자격, CI 출처, 운영 승격은
항상 false다. 기존16skip/2xfail을 이 소비 계약이 승인했다고 보고하지 않는다. source108 실제
call-phase와 qualified runtime은 여전히0이고 기존 source 실행 차단을 바꾸지 않는다.

root 단독 전체 UTC/KST를 각각900초 cap 안에 완료했다. 최신 후보541개 Python 파일의
메모리 compile, tracked 비밀 패턴 검사, diff check와 원 helper/guard/producer/예외 원장
지문 불변을 확인했다. 첫 compile 명령은 `python` 실행파일이 없어 실행0이었고,
기존 venv의 절대 Python 경로로 정정한 실제 검증이541 passed다. 신규 waiver는 없다.
문서 마감·feature push 후 다음 runtime 등록 계약을 순수 문서 일치+빈 실제 등록부로
제한해 착수한다. 별도 두 health 관측 일관성 수정은 실제 주문 판단과 분리한다.
cold source 후속 문서 준비는 별도이며 native/decoder/실제 소비자 증명을 대신하지 않는다.

이번 작업은 main·운영 서비스·주문·설정·외부 API·설치·권한·CI 활성화를 변경하지 않는다.
운영 상태를 새로 조회하지 않았으므로 과거 PID/배포 상태를 현재 사실로 재확인했다고 하지 않는다.

원문·리뷰·실제 exit 기록은 coordinator worktree의
`.superpowers/sdd/2026-09-27-os-process-evidence/`에 보존한다. 첫 Task1 원문만 해당 작성자
worktree의 같은 하위 경로에 있으며 후속과 coordinator 원문은 coordinator 경로에 있다.
전체 원문은 `full-utc-9cf021f.log`와 `full-kst-9cf021f.log`이며 이전 실패도 별도 보존한다.
