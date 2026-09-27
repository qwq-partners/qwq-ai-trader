# pytest 실제 종료·회수 증거 — 로컬 한 슬롯

2026-09-27 KST. 독립 계획 재리뷰 `APPROVE_PLAN_ONLY` 후의 구현 명세다.
코드·실제 프로세스 회수·native 자격 승인은 별도이며 앞 단계 검증 후 착수한다.
사용자 자율 순차진행 범위의 개발 후속이다. 상위 계약은
[조건부 source 설계](2026-09-27-required-source-proof-design.md) §7이며,
현 v1 receipt의 `finished`가 pytest.main 반환이지 OS process 사망이 아니라는 간극을 닫는다.
운영 엔진/주문/설정·main·CI·외부 API·권한/설치는 변경하지 않는다.

## 선택과 성공 의미

채택: Linux 전용 CLI controller가 고정 pytest bootstrap 자식 하나와 그 자손만 소유하고,
실제 종료·회수 뒤 별도 process-result를 남긴다. 기존 producer/receipt/네 슬롯 판정은 그대로다.
제외: producer가 자신의 미래 exit를 자기신고하기, 호출자 프로세스에 subreaper를 설치하는
공개 library API, 단순 leader.wait를 자손 종료로 간주하기, 범용 shell/command 실행기.

새 순수 판정의 `OS_RESULT_BOUND`는 **local_os_process_only**이고
`native_qualified=false`, `ci_provenance_verified=false`, `production_eligible=false`다.
현재16skip/2xfail을 포함한 전체 suite를 이 판정으로 승인하지 않는다. v1의 미지원 outcome
거부를 보존한다. source-proof lane은 실행을 거부하고 standard/local 한 슬롯만 지원한다.

## 파일·표면

- `scripts/dev/pytest_evidence_bootstrap.py`: stdlib 기반 guard 선설치·단일 control frame 후 기존 producer.
- `scripts/dev/pytest_evidence_controller.py`: CLI만 지원하는 단일 용도 Linux 프로세스 소유자.
- `scripts/dev/verification_os_contract.py`: process-result strict parser와 한 슬롯 순수 validator.
- `tests/dev/test_pytest_evidence_controller.py`, `tests/dev/test_verification_os_contract.py`.

CLI는 다음 고정 option과 **시험 경로만** 받는다. 임의 executable/module/shell/pytest option은 없다.

```text
python scripts/dev/pytest_evidence_controller.py \
  --verification-context PATH --verification-output PATH \
  --process-output PATH --timeout-seconds N -- tests/PATH [tests/PATH ...]
```

root는 controller 파일의 `parents[2]`로 고정한다. cwd도 root여야 한다.
context는 root 내부 regular JSON(최대64KiB), 출력은 root 내부 서로 다른 새 파일이어야 한다.
출력 경로는 receipt, process-result와 process-result 뒤에 `.stdout.log`/`.stderr.log`를 붙인
원문 두 파일이다. 실제로는 총4출력이며 모두 exclusive-create, symlink/nonregular/기존 파일은 거부한다.
어느 출력도 context·실행 코드·guard·시험 입력을 덮지 않는다. 부모 path도 symlink traversal을 거부한다.
context·선택 경로는 resolved root 내부, 선택은 `tests` 또는 그 하위 regular file/directory만 허용한다.
`::`, wildcard, `..`, symlink, `-` option, 중복·빈 선택 및 명시 `tests/proofs` 하위 선택은
거부한다. `.env`/운영 state는 입력이 아니다.
child 선택 inventory는 별도 expected와 검증하며 경로 허용만으로 선택 완전성을 주장하지 않는다.

기존 producer의 context run/slot 형식은 보존하되 `event=local`, `lane=standard`만 실행한다.
고정 pytest argv는 `-q -p no:cacheprovider -p pytest_asyncio.plugin -p pytest_cov.plugin
-p anyio.pytest_plugin --tb=short`와 경로 목록이다. PYTEST_ADDOPTS/PYTEST_PLUGINS 입력은 거부한다.
child 환경은 PATH=/usr/bin:/bin, LANG=C.UTF-8, context와 같은 TZ,
PYTHONDONTWRITEBYTECODE=1, PYTEST_DISABLE_PLUGIN_AUTOLOAD=1만 전달한다. HOME을 전달/재지정하지 않는다.
SSH fixture 변수나 임의 env pass-through는 추가하지 않는다. 따라서 이 CLI로 기존 전체 suite를
곧바로 인증하는 것은 비목표다. 새로운 실제 표준 launcher profile은 후속 설계다.

## guard와 신원

parent와 child 모두 고정 `tests/conftest.py`를 제품 import 전에 설치·대조한다.
guard SHA256은 `7b7b26940309a2a165d9bdba7611b6730e83b90ae9ea5f9e725764ee4c0a23f7`이고
path/hash·실제 object1·VIOLATIONS exact list/0을 확인한다. 다른 conftest/duplicate는 거부한다.
parent에 기존 자식이나 다른 OS thread가 있으면 시작하지 않는다. 임의 caller에서 library로 쓰지 않는다.
parent guard 설치와 모든 dependency import를 완료한 **뒤** 최종 단일 OS task/자식0 검사를 한다.
그 이후 일반 callback/dependency import나 별도 reaper/thread를 도입하지 않는다. waitability
관측 뒤에도 SIGCHLD disposition과 단일 회수자 전제가 유지되어야 한다.
child executable은 sys.executable 절대경로, bootstrap은 고정 절대경로이며
`-I -B <bootstrap>`으로 실행한다. root 경로는 bootstrap이 명시 주입하고 PYTHONPATH는 받지 않는다.
이 옵션은 현 venv/site의 전체 보안 sandbox나 native qualification을 증명하지 않는다.

control FD는 parent가 새로 만들고 그 FD 하나만 pass_fds한다. child는 실제 guard 관측으로
`{"schema":"qwq.pytest-guard-ready/v1","guard":<receipt와 같은 guard dict>}` 한 줄을 보내고
FD를 닫은 뒤 producer를 호출한다. frame 최대4096bytes, startup10초 또는 전체 deadline 중
먼저 오는 기한을 적용한다. 누락/중복/위조 hash/모듈 수/위반/extra bytes는 startup_error다.
최종 receipt guard와 startup guard 모두 expected와 같아야 한다.

identity는 controller/bootstrap/producer/guard/executable의 시작 전 실제 bytes SHA256이다.
root 내부 실행 코드와 guard는 child 종료 뒤 다시 대조한다. 변경을 관측하면 identity_changed로
거부한다. 이것은 악의적 write-restore 공격에 대한 불변 filesystem 보장이나 CI attestation이 아니다.

## 실제 자식 소유권·기한

Linux `PR_SET_CHILD_SUBREAPER`/`PR_GET_CHILD_SUBREAPER`의 성공을 실제 확인한 전용 controller만
실행한다. 미지원은 startup_error이고 host fallback은 없다. child는 새 session과 close_fds로 시작한다.
권한 확대·cgroup/namespace 생성·운영 daemon 접근은 없다.

소유권 원칙: 단일 OS task·시작 시 자식0인 controller의 직접 자식과 입양된 자식만 signal/wait한다.
SIGCHLD를 SIG_DFL로 명시 reset하고 다른 handler는 중단 flag만 기록한다. preflight 자식0은
`waitid(P_ALL,0,WEXITED|WNOHANG|WNOWAIT|__WALL)`의 ECHILD로 확인하며 기존 자식을
reap하지 않는다. proc의 빈 목록은 증거가 아니다. 이 CLI만 launcher/reaper이며 `__WALL`은
Linux ABI `0x40000000`으로 모든 wait에 적용한다.
다른 task/서비스·프로세스 이름 검색·전역 PID sweep은 금지한다. leader를 회수한 뒤 stale PGID에
killpg를 보내지 않는다. 정리 순서는 direct owned children TERM,1초 후 잔존 owned children KILL,
추가2초 내 wait/reap이며 입양 자식도 같은 절대 cleanup 기한 안에서 반복 처리한다.
signal은 pidfd만 사용한다. preflight에서 wrapper 존재와 controller 자신의 pidfd에 대한
signal0 성공을 확인하고 닫는다. 실패면 bootstrap을 spawn하지 않는다. 각 양성 candidate는
pidfd를 **먼저** 열고 child-only `waitpid(pid,WNOHANG|__WALL)`로 대조한다. terminal이면
기록/reap만, ECHILD면 어떤 신호도 보내지 않으며, zero이면 그 fd에만 현재 TERM/KILL을 보낸다.
fd는 항상 닫는다.
PID 재사용 시 이전 fd는 새 프로세스에 신호를 보낼 수 없다. os.kill/killpg/Popen signal 폴백은 없다.
한 번에 fd하나, 반복 cleanup signal은 허용하되 PID-only once-sent cache는 쓰지 않는다.
앞서 관측했던 PID라는 이유만으로 signal하지 않으며 ESRCH도 성공 종료 증거로 바꾸지 않는다.
성공 spawn 뒤 Popen.poll/wait/communicate/context-manager/send_signal/terminate/kill을
호출하지 않는다. raw wait의 실제 leader status를 한 곳에서 기록하고 Popen.returncode에 반영한다.
leader 우선 wait 및 generic wait 모두 같은 recorder를 쓰며 ECHILD를 exit0으로 바꾸지 않는다.

SIGCHLD reset의 실제 host 효과를 upstream 버전 문자열만으로 인정하지 않는다. bootstrap 전에
추가 자식1개를 fork해 즉시 `_exit(23)`하게 하는 작은 **waitability probe**를 실행한다.
부모는 최대1초(전체 deadline보다 길 수 없음) 내 raw wait로 exit23 및 최종 ECHILD를 관측해야 한다.
성공한 probe만 회수한 뒤 bootstrap leader 추적을 시작한다. probe 실패/timeout도 위 pidfd 정리
경계를 적용하고 bootstrap은 시작하지 않는다. probe에 pytest/제품/native source 실행은 없다.
이 관측은 이후 parent의 SIGCHLD disposition/sole-reaper가 변하지 않는 조건에 한정된다.

실행 deadline은 시작부터 N초(정수1..900)이며 progress/output으로 늘리지 않는다.
stdout/stderr/control은 동시에 drain한다. 두 stream은 각각 최대8MiB+1byte까지 저장/지문화하고
초과 관측 즉시 output_limit로 정리한다. 원문은 위 전용 로그에만 보존하고 CLI에 echo하지 않는다.
leader를 reap한 뒤 아직 회수가 필요한 nonleader를 관측하면 zombie라도 승인하지 않는다.
`descendant_survived`는 이 보수적 후행 정리 의무 관측이며 정확한 과거 생존 순간의 증명이 아니다.
proc children은 양성 대상 발견에만 쓰고 각 read/batch도 유한하게 제한한다. pipe EOF만으로 자손0을
추론하지 않는다. final wait의 ECHILD와 종료·adoption 관측을 함께 요구한다.
spawn 직후 parent가 가진 모든 child-side pipe write end를 닫는다. `cleanup_complete=true`의
필수 조건은 실제 leader terminal status, stdout/stderr/control **세 pipe를 끝까지 drain한 EOF**,
고정 완료 예산 내 마지막 `waitpid(-1,WNOHANG|__WALL)`의 ECHILD다. ECHILD에서 남은
buffer를 버리거나 미관측 EOF를 추정하지 않는다. leader 회수 뒤 generic wait가 zero를 반환한
경우에도 proc 목록과 무관하게 `descendant_survived`를 latch한다. 후행 nonleader status도 같다.
이미 발생한 reject latch는 정리가 성공하거나 나중에 rc0을 관측해도 절대 지우지 않는다.
SIGINT/SIGTERM은 동일 정리를 거쳐 interrupted 기록; SIGKILL/host crash/발행 실패는 누락 증거다.
일반적인 fork/setsid/double-fork는 인수 대상이고 적대적 namespace/cgroup/ptrace escape는 지원 밖이다.
시간 상한은 응답 가능한 event loop의 예산이다. 동기 spawn/파일 I/O·커널 D-state/CPU deschedule을
강제로 선점하는 hard real-time 보증은 아니며 cleanup 실패는 그대로 실패로 남긴다.

## process-result v1

UTF8 최대64KiB, 깊이8, 중복 key/unknown field/schema/NaN/비정상 integer/bool-as-int 거부.
exact 최상위: `schema, run, slot, scope, identity, launch, process, streams, receipt`.

- schema=`qwq.verification-process-result/v1`, scope=`local_os_process_only`; run/slot은 기존 v1 구조.
- identity: `controller,bootstrap,producer,guard,executable`(각 SHA256).
- launch: `profile="pytest-evidence-bootstrap/v1"`, `process_scope="linux-subreaper/v1"`,
  `timeout_seconds`(1..900), `selection_sha256`(실제 경로 목록의 canonical JSON SHA256).
  선택은 입력 순서를 보존한 root-relative POSIX 경로이며 `.`/중복 slash/절대 경로 표기는
  정규화 뒤 중복 거부한다. digest는 이 list의 UTF8 JSON
  `ensure_ascii=False,separators=(',',':'),allow_nan=False`다. 경로 순서를 정렬하지 않는다.
- process: `reason`=exited/signaled/timeout/interrupted/startup_error/output_limit/cleanup_error/identity_changed/io_error;
  `returncode`(실제 leader wait 결과 -64..255 또는 미시작/미회수 null),
  `term_sent,kill_sent,leader_reaped,descendant_survived,cleanup_complete`(exact bool),
  `descendants_reaped`(0..20000 int), `ownership_probe_passed`(actual wait23+ECHILD exact bool),
  `guard`(실제 child frame guard 또는 null),
  `parent_guard`(실제 controller guard 또는 null; 최종 실제 위반 수도 검사).
  reason과 returncode는 별개다. timeout으로 kill된 rc=-9도 그대로 보존한다.
  미시작/미회수 leader는 returncode=null, leader_reaped=false다. probe의 exit23을
  leader rc로 사용하지 않는다. leader_reaped=true이면 반드시 실제 정수 rc가 있어야 한다.
- streams: stdout/stderr 각각 `{bytes,sha256,overflow}`. bytes0..8388609, hash는 실제 보존한
  prefix의 SHA256, overflow exact bool. overflow가 있으면 절대 승인하지 않는다.
- receipt: `{state,bytes,sha256}`. state=missing/regular/invalid; regular만 실제 bytes/hash를
  갖고 나머지는 bytes0/sha256null이다. 끝난 child와 회수 상태 확인 뒤에만 receipt를
  nofollow/nonblocking regular bounded32MiB로 읽는다. invalid도 정상 파일로 바꾸지 않는다.

process-result는 cleanup 뒤 마지막으로 exclusive-create한다. 정상 CLI exit는 child raw rc,
timeout124, child signal128+n, 그 밖의 controller 실패125. child rc0이어도 자손 잔존·격리·
발행 실패가 있으면125다. CLI0만으로 순수 판정의 승인을 대신하지 않는다.
ignored Python atexit 예외가 OS0인 경우까지 clean-atexit라고 인증하지 않는다.
atexit hang/signal/os._exit9는 실제 deadline/OS rc로 검출한다.

## 순수 결속 API

`ProcessEvidenceError(ValueError)`, `parse_process_result(raw: bytes) -> dict`,
`validate_controlled_receipt(receipt_raw: bytes, process_raw: bytes, expected: dict) -> tuple[str,...]`,
`evaluate_controlled_slot(receipt_raw: bytes, process_raw: bytes, expected: dict) -> dict`.
expected exact fields는 `verification`(기존 v1 expectation 전체), `process_identity`(위5hash),
`launch`(위 exact launch)다. 기존 `parse_document`/`validate_receipt`를 그대로 사용하고
run/slot/hash/identity/launch/guard/rc/완전 회수/stream 상한을 추가 대조한다.
expected 작성·신뢰는 호출자 책임이며 실제 collection에서 자동 생성하지 않는다.
네 슬롯을 실제 실행했거나 CI origin을 확인했다는 주장은 하지 않는다.
expected는 exact dict와 위 exact key/형식만 허용한다. identity는 lowercase hex64, launch는
process-result와 동일한 exact schema/type/range를 검증한다. verification은 canonical JSON으로
기존 `parse_document(kind="expectation")`에 전달해 네 슬롯 형식까지 검증하고 실제 한 슬롯만
`validate_receipt`로 대조한다. `evaluate_bundle`은 호출하지 않는다.

eligibility는 `reason=exited`, 실제 rc0, leader_reaped/ownership_probe_passed/cleanup_complete=true,
descendant_survived=false와 일치하는 양 guard/receipt/identity/launch, stream overflow=false를
모두 요구한다. term_sent/kill_sent=true도 거부한다. bytes>8MiB는 overflow=true여야 한다.
probe false·cleanup false·미관측 guard 등을 parser가 합법적 실패 증거로 읽더라도 승인은 금지다.
공개 parser는 고정된 세부 `ProcessEvidenceError`를 낼 수 있고, validator 경계에서 process
parser 예외를 `INVALID_PROCESS_RESULT` 한 code로 정규화한다. validator는 정렬·중복 제거한
다음 고정 code와 기존 `validate_receipt` code만 반환한다:
`INVALID_EXPECTATION`, `INVALID_RECEIPT`, `INVALID_PROCESS_RESULT`, `PROCESS_SCOPE_MISMATCH`,
`PROCESS_RUN_MISMATCH`, `PROCESS_SLOT_MISMATCH`, `PROCESS_IDENTITY_MISMATCH`,
`PROCESS_LAUNCH_MISMATCH`, `PROCESS_EXIT_REJECTED`, `PROCESS_CLEANUP_INCOMPLETE`,
`PROCESS_OWNERSHIP_UNPROVEN`, `PROCESS_DESCENDANT_SURVIVED`, `PROCESS_GUARD_MISMATCH`,
`PROCESS_STREAM_OVERFLOW`, `PROCESS_RECEIPT_MISMATCH`.

성공 exact decision은 schema=`qwq.verification-os-decision/v1`, status=`OS_RESULT_BOUND`,
errors=[], scope=`local_os_process_only`, native_qualified=false,
ci_provenance_verified=false, production_eligible=false. 거부는 status=`REJECTED`와 정렬된
고정 오류 code 목록이다. raw argv/path/env/exception/출력을 오류 code에 넣지 않는다.

## 검증과 후속

순수 strict schema/hash/rc/각 reason/guard/skip·xfail 거부 시험과 실제 작은 pytest child를 분리한다.
실제 exit0/1, receipt 이후 exit9/signal/hang, descendant survivor/setsid/double-fork,
양 pipe 동시 flood, guard frame 실패, output 기존파일/링크/경로탈출, caller SIGTERM을 RED부터 고정한다.
waitability probe의 정상23/유실 status/timeout은 각각 bootstrap 실행 여부를 검증한다.
실제 실패 시험의 고정 구조는 `pytest → 전용 harness → controller → 고정 fixture tree`다.
harness는 후보 controller의 helper를 재사용하지 않는 별도 구현이며 시작 전 단일 OS task/
자식0/subreaper와 pidfd 지원을 확인한다. pytest parent는 subreaper로 바꾸지 않는다.
harness는 case12초+TERM1초/KILL2초 cleanup, outer pytest 관측은20초다. outer는16초에
아직 살아 있는 직접 harness에 양성 child 확인+pidfd TERM으로 중단을 알릴 수 있지만
남은4초를 회수자에게 보장하며 harness를 먼저 KILL하거나 Popen timeout 자동 kill하지 않는다.
정상/실패/assertion/중단/timeout 모두 harness의 finally에서 직접 controller와 입양 자식을
pidfd-first→child wait→signal로 처리하고 최종 raw __WALL ECHILD를 요구한다. PID파일/PGID/
프로세스 검색/후보 controller의 cleanup 함수는 회수 권한이나 증거가 아니다.
각 fixture 자손은 작업·다음 fork 전에 자신의 독립 최대8초 종료 backstop을 설치한다.
unexpected harness 사망·20초 초과·최종 ECHILD 누락은 case/batch를 중단하는 실패다.
종료 타이머를 회수 증명으로 대체하거나 host-wide cleanup하지 않는다. 회수 불완전/응답불능
시나리오는 fake adapter에서만 실행한다. 고정 tiny dummy controller failure로 harness의
finally·실제 회수·기한을 먼저 검증한 뒤 의도적으로 실패하는 controller 시험을 시작한다.
마지막 ECHILD와 미소진 pipe, 마지막 chunk 초과/추가 control bytes, 빈 proc와 post-leader
generic zero는 adapter 인수로 추가한다. 이 harness는 test 한 파일 내부 전용이며 새 플랫폼이 아니다.
자식 PID를 인수 뒤 생존0으로 확인하되 unrelated process를 kill하는 cleanup helper는 금지한다.
기존 guard와 producer를 바꾸어 통과시키지 않는다. worker 집중시험은 직렬, root 전체 UTC→KST 후
독립 broad 리뷰·feature 통합만 허용한다. source108/native/runtime qualification 실행0이다.

설계 근거: [Linux subreaper](https://man7.org/linux/man-pages/man2/PR_SET_CHILD_SUBREAPER.2const.html),
[wait와 자식 회수](https://man7.org/linux/man-pages/man2/wait.2.html),
[Python subprocess](https://docs.python.org/3/library/subprocess.html).
이 문서의 세부 controller 정책은 해당 의미를 바탕으로 한 프로젝트 설계이며 공식 보증이 아니다.
