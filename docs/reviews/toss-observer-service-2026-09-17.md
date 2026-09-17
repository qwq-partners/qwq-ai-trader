# Toss 독립 관측 서비스 — Plan → Do → See

## 현재 상태

사용자가 상세 설계와 새 관측 일정을 명시 승인했다. **09-17 21:47:37 KST 별도 Toss 서비스 ON(PID3335469), 초기 토큰 발급 성공·장외 idle을 확인했다.** 기존 거래 봇은 PID3274983·checkout8ff2f55 그대로이며 재시작·주문·설정 변경 없이 추가 KIS0·주문 무연결 구성을 적용했다. 아직 가격/캘린더 실관측과3영업일 인수는 미완료다. 소스 구현·CI/main 이력은 [PR #74](https://github.com/qwq-partners/qwq-ai-trader/pull/74), 병합 인계는 [PR #75](https://github.com/qwq-partners/qwq-ai-trader/pull/75)로 구분한다.

- 기준 main/root checkout: `8ff2f55f4515d049fbcd5fa1eea009b3ffc9f164`; 원격 main과 새 artifact만 후속 갱신하며 거래 checkout은 유지한다.
- 실행 계획: `docs/superpowers/plans/2026-09-17-toss-observer-service.md`, 상세 설계 승인 및 역할/인터페이스 고정 `2e772d6`.
- 당초 첫 관측일 경과 후 사용자가 **09/18·21·22 관측,09/22 18:00 KST 만료**를 확정했다. 실제 plan/grant에 이 날짜를 고정했으며 이후 자동 연장·과거 채움은 금지한다.
- **09-17 21:34:47 KST PR #74 병합 완료**: 원격 main `c2fe787b59a25b2a6ca0931c82622f47a6771ca5`. CI run35221651554는 정확한 head `877768eef234f94df10a63e7bac452865dcc9f18`에서1809 passed/2 known xfailed, 격리0·문법/비밀정보 검사 통과(48.90초, 환경별 기존 경고33). 병합 main과 해당 head의 tree diff0을 확인했다. 거래 봇 로컬 main은 pull하지 않아 여전히8ff2f55이다.

## Plan / 역할

| 작업 | 구현 | 결과와 독립 리뷰 |
|---|---|---|
| 실행 신뢰·승인·시작 영수증 | Astra/high, launcher 격리 worktree | `0decfbf`, 집중48·전체1732 passed/2knownxfail, Astra/xhigh Approved·Minor1 보강 및 재리뷰 승인 |
| 캐시 입력·관측 감독·private 상태 | 부모, runtime 격리 worktree | `f5c60a7`, 집중39·전체1723 passed/2knownxfail, Important1 수정(db134ad)·재리뷰 승인 |
| 패키징·설치·원장 보존 | Astra/high, installer 격리 worktree | `696b91c`, 집중30·전체1714 passed/2knownxfail, 별도 Astra/xhigh Approved |
| 통합·운영 실행 | 부모; 인수 테스트 Terra/high; 최종 Astra/xhigh, 운영 절차 Astra/high 별도 리뷰 | 소스 통합·검증·리뷰 및 실제 설치·ON 완료, 장외 대기·초기 발급까지 확인 |

계획한 Terra/high 구현 슬롯이 pending_init 상태의 기존 agent 때문에 확보되지 않아 부모가 관측 루프를 구현했다. 병렬 구현은 나머지 두 작업에서 유지하며 최종 리뷰어는 구현자와 분리한다. 이 배치의 비용은 통합자의 구현 부담이며 안전 요구나 리뷰 게이트를 완화하지 않는다.

## Do / 구현 증거

- 코드와 의존성 파일별 manifest, plan raw/canonical hash, config hash, 실제UID/GID/groups/host 검증. `-I -S`로 site 초기화를 막고 검증 후에만 승인 app/deps 경로를 추가한다.
- 첫 grant worker 수명만 허용하는 O_EXCL·file/dirfsync 시작 영수증. fsync 실패 후에도 지우거나 같은 grant를 재사용하지 않는다. OFF 및 check-only에서 accessor·worker·발급0은 합성 인수하며 systemd 환경 전달과 혼동하지 않는다.
- 입력 strict loopback GET, fail≠empty, code-only 선정. 원장 ACK 후 read-only 계수 대조, 성공 시각 확정, 캘린더/가격 별도 coverage, comparison=insufficient/valid_pairs0/production_eligibleFalse.
- TDD에서 원장 대조 실패 후 성공 시각 잔류와 startup 도중 stop 미처리 두 경로를 추가 재현하고 수정했다. 이들은 신규 구현 중 발견된 결함이며 기존 거래 봇의 운영 사고로 기록하지 않는다.
- 의존성은 기존 설치 버전의 공개 wheel만 별도 임시 wheelhouse로 확보했다. 기존 봇 venv/requirements는 수정하지 않았다.
- 독립 리뷰의 Important1(입력 close1초+worker10초 별도 예산)을 공통 monotonic deadline으로 수정했다. 종료 불확실 시 인수도None을 유지한다. Minor1(승인 파일 없는 identity 거부 fixture)은 정상 성공 대조군→변조로 보강했다. 각 RED2/RED8→서비스18/launcher48 GREEN, scoped 재리뷰 신규C/I/M0.
- 통합 집중117건 통과 후 수정·추가된 실제 worker/지속 원장 성공·입력 실패·공급자 실패 및 패키지 closure4건 통과. 별도 wheel 기반 package 빌드 및 `/usr/bin/python3 -I -S -B` import 스모크에서 aiohttp3.13.5/관측 모듈 로드·거래 모듈0 확인. 최종운영artifact는 리뷰 후 소스로 다시 만든다.
- installer dry-run은 proposed 새 일정으로 `ready=true, applied=false`를 반환했다. 자격 읽기·계정/설치 파일/실제grant 생성·systemctl0이며 일정 승인이나 실제 배포로 계산하지 않는다.
- 최종 리뷰 소스 `877768e`로 hash-locked wheel 오프라인 빌드를 다시 실행했다. artifact SHA256 `1f046e498a2258028cfafb6850b3fa37685a2bda38a3e410225b4f3607c9ffba`; system Python `-I -S -B`에서 aiohttp3.13.5·관측 모듈 import 및 거래 모듈0, installer dry-run `ready=true/applied=false`를 확인했다. 이는 `/tmp` 준비물이며 root 보호 운영 릴리스 설치가 아니다. 실제 설치 전 동일 digest와 플랫폼을 다시 검증한다.

## 통합 검증 중 발견한 실행 부하 영향

- UTC/KST 전체 verify를 동시에 실행한 첫 런: UTC1805pass/2fail, KST1806pass/1fail(각기존xfail2/pykrxwarning1, 격리0). 실패는 기존 `test_two_processes_share_one_renewal`, `test_two_process_observations_are_idempotent_and_never_clobber`의 `process.join(timeout=5)` 뒤 자식 exitcode=None이었다.
- 정확한 관련3개를 cold 임시pycache·동일env의 단독프로세스로 실행하니3pass/7.84초. 두전체동시 실행에 따른 시작 지연 가능성은 있으나 이 결과만으로 회귀가 없다고 단정하지 않는다. 전체 UTC/KST 순차 재검증이 남아 있으며 실패를 통과로 덮거나 토큰/운영 임계값을 바꾸지 않았다.
- 후속 **UTC 순차 전체 verify1807 passed/기존xfail2/기존warning1,89.26초** 통과, 격리0·문법/비밀정보 검사 통과. 이후 KST는 기존1807건 통과·작성 중 sealed-runtime 신규1건 실패였다. 신규 fixture의 bound token 메서드와 검증 후 test hook 배치를 보완하며 최종 트리는 별도 확인한다. 기존 병렬 실행 실패 기록은 보존한다.
- PR #74 초기 head `f8e51f3` CI(run35220241366)는1806 passed/1 failed/기존xfail2였다. 합성 manifest는 setup-python3.12.14로 만들지만 테스트 subprocess는 `/usr/bin/python3`를 사용해 exact-patch 검증에서 거부됐다. subprocess를 manifest와 같은 `sys.executable -I -S`로 맞추고 patch version 불일치 거부 회귀를 추가했다(launcher33 passed). **운영 Python 검증을 완화하지 않는다.** CI의 기존 NumPy timedelta 경고33건과 로컬 pykrx 경고1건은 환경별로 구분한다.
- 독립 Astra/xhigh 최종 broad 리뷰는 `8ff2f55..f8e51f3` 31파일 전체에서 Approved(Critical/Important/Minor0). 이후 추가된 테스트는 한정 재리뷰와 최종 UTC/KST·CI 성공이 필요하다.
- 위 테스트 변경과 검증 문서 3파일 한정 재리뷰도 **Approved(C/I/M0)**. 최종 동일 소스/테스트 트리 순차 verify는 **UTC1809 passed/2 known xfailed/1 기존 warning(85.37초), KST1809 passed/2 known xfailed/1 기존 warning(85.67초)**. 격리 위반0·문법/비밀정보 검사 통과. 새 시험 의존성 closure를 포함한 sealed-main 흐름은 실제 worker/원장/receipt1/정상 종료를 검증하지만 실 OAuth·root/systemd 인수는 아니다.

## S01~S12 증거와 한계

| 계약 | 오프라인 검증 위치 / 운영 한계 |
|---|---|
| S01·S02 | launcher/deployment 합성 테스트 + 실제 전용 UID `--check` 성공·시작 전 state 파일0·root 파일 권한 및 systemd 자격 범위 검증 |
| S03·S04 | 단일 시작·발급/승인/unknown 경계는 합성 테스트. 운영 receipt1·sender 소유자1·초기 토큰 generation1/ready 확인. 실환경 재시작·revoked/unknown 유발 테스트는 하지 않음 |
| S05·S06 | `test_toss_observer_positions.py`, `test_toss_observer_service.py`: bounded cache 입력·결측/빈 보유·선정/overflow·슬롯/비교 제외 |
| S07·S08 | service/status/integration 테스트: 실패 시 성공 시각 불변·원장 대조·공통 종료 예산. sealed-runtime은 정상 worker·지속 원장·receipt1·SIGTERM 종료. 실제 systemd 강제 종료 미실행 |
| S09·S10 | integration/sealed-runtime/package/install 테스트 및 UTC/KST 전체 verify: 거래 모듈 분리·실행 연결·폐쇄 패키지·설치 dry-run. sealed-runtime은 설치된 시험 의존성 복사이며 lock 검증은 별도 wheel 빌드로 확인 |
| S11 | retention 합성 안전 경계 테스트·실제 timer enabled/active 확인. 만료 원장 실제 삭제는 아직 미실행 |
| S12 | 운영 전후 기존 봇 PID3274983/checkout8ff2f55 유지·설정/킬스위치7경로 지문 동일·broker connected/pending0/stale0 확인 |

## See / 아직 남은 게이트

- 완료: 각 작업 독립 spec/quality 리뷰·수정·재리뷰, sealed artifact+실worker fakeHTTP 통합 인수, UTC/KST 전체 verify·비밀정보 검사·최종 broad 및 한정 리뷰.
- 완료: PR #74 required CI exact head 성공·보호 규칙 경유 main 병합. 기존 거래 봇 checkout은 유지했다.
- 완료: 실제 root/전용 UID 설치·자격 없는 check-only(발급0)·새 unit 한 번 시작·기존 PID/설정 지문 대조.
- 장외 ON과 실제 GET/원장 관측을 구분하고3영업일/유효 가격 비교/소비자 승격은 별도 미완 인수로 유지한다.

## 운영 읽기 전용 확인

구현 중 재조회: `qwq-ai-trader.service` active/running, PID3274983, 시작09-17 00:47:47 KST; root checkout8ff2f55/clean. `qwq-toss-observer.service` not-found/inactive. 21:10:36 KST 로컬 health 메모리 읽기에서 broker_connected=true, broker/risk pending0, stale0. 추가 KIS 요청을 만들지 않는 경로만 조회했다. 기존 서비스 재시작/checkout 변경/주문/설정 변경0. 이 조회는 새 서비스 설치/인수 증거가 아니다.

21:34:34 KST 최종 health도 broker_connected=true, broker/risk pending0, stale0. 동일 PID3274983·observer not-found를 재확인했다. systemd `ExecMainStartTimestamp`는00:47:46이며 이전00:47:47은 시작 로그 기준이다. PR 병합 후에도 로컬 checkout8ff2f55/clean을 확인했다. 운영 gate는 새 관측 일정 답변 → 실제 root/UID/자격 설치·check-only → 새 unit 시작 순이며, 기존 거래 봇의 재시작은 포함하지 않는다.

## 실제 설치·ON (사용자 일정 확정 후)

- 사전 확인: main7fe6e28의 실행 소스/설치 도구/retention/unit가 승인 소스877768e와 동일. Python3.12.3, systemd255, NTP 동기화,57GiB 여유,8080 listener가 기존 PID3274983임을 확인했다. 설정3파일·킬스위치4경로 지문은 비공개 메모리에서 전후 대조했다.
- 배포 직전 새 KST verify **1809 passed/2 known xfailed/1 기존 warning,86.00초**, 격리0·문법/비밀정보 검사 통과. Astra/high 운영 절차 검토 GO 이후 부모만 실제 설치를 수행했다.
- `install.py --apply`만 먼저 실행, 서비스 UID997/GID987(추가 그룹 없음, nologin), root 보호 릴리스/승인 파일·Toss 전용0600 자격 파일을 만들었다. 기존 `.env`는 변경하지 않았다. release877768e, artifact `1f046e498a2258028cfafb6850b3fa37685a2bda38a3e410225b4f3607c9ffba`.
- grant `toss-observer-20260918-v1`: not_before09-17 21:45:59 KST, expires09-22 18:00 KST. plan `toss-observer-holdings-20260918-v1`:09/18·21·22, 캘린더08:55, 가격09:00≤t<15:20의5분 격자. 원장 삭제 기한10/22 18:00 이후, timer는 매일19:00 KST다.
- 실제 전용 사용자에서 **Toss 자격 없는 env-i + TOSS_API=1 + `/usr/bin/python3 -I -S ... --check`** 성공. 시작 전 토큰/영수증/원장 등 state 파일0을 확인했다. root 자격 파일은 기존 값에서 정확히 Toss2필드만 복사됐고 grant/client binding도 값 출력 없이 대조했다.
- `daemon-reload` 뒤 effective unit에 drop-in/추가 실행 훅 없음·지정 EnvironmentFile1개·CPU25%/Memory192MiB/Tasks16·NoNewPrivileges/ProtectSystem=strict/ProtectHome·Restart=no 확인. `systemd-analyze verify` exit0 중 기존 **claude-session.service의 KillMode=none 경고1건**은 범위 밖으로 보존하고 변경하지 않았다.
- retention timer만 enable/start(다음09/18 19:00), observer는 **start1회**. systemd 시작21:47:37·PID3335469, application 시작21:47:40. 초기 토큰 generation1·auth_state=ready, receipt1개/PID일치, sender/원장 flock 소유자 모두3335469, 재시작0을 확인했다. 토큰/자격값과 auth 지문은 문서에 남기지 않는다.
- 21:48 관측: idle, price/calendar 관측0, 예정 슬롯228/3, 모든 성공 시각None, 비교insufficient·유효쌍0·인수None·production_eligible=False. 원장572bytes는 계획 기록이며 시세 표본이 아니다. 메모리 약31MiB·태스크3, 현재 환경에 Toss2자격만 있고 KIS/OpenAI/Gemini/Telegram 자격은 없음. 새 서비스 오류/Traceback/unclosed 로그0.
- 21:49 기존 봇 재확인: 동일 PID/시작 시각·checkout8ff2f55/clean·보호7경로 동일, broker connected=true·broker/risk pending0·stale0. 주문/전략/킬스위치/거래 봇 venv 변경 및 거래 봇 재시작0.
- 21:53 이후 재확인: 동일 observer PID·idle·분 단위 상태 갱신, 원장의 read-only summary incomplete=false/중복0·관측/미완attempt0. 기존 거래 프로세스 환경의 Toss OFF도 값 노출 없이 확인했다. ON 이후 기존 봇 KIS 오류 패턴/Traceback0. 원장 회계 정상과 미래 관측 인수 완료를 구분한다.
- 미완: 실제 가격·캘린더 GET/정규화, 일반 토큰 갱신,3영업일 관측 인수, 만료 시 종료/락 반환, 보존 기한 삭제. 확인 절차는 [모니터링 체크포인트](../operations/monitoring-checkpoints.md)의 독립 Toss 절에 기록한다. 관측 실행만 자동이며 이번 작업으로 별도 외부 알림/예약 점검을 만들지는 않았다.

## 활성화 후 인계 검증

- 문서 독립 Terra/medium 리뷰는 기존 PR #75를 새 문서 PR로 혼동한 지적1건을 실제 병합 이력(7fe6e28) 대조 후 철회, 발견사항0으로 승인했다.
- 설치 후 재실행한 전체 verify는 **1808 passed/1 failed/2 known xfailed/1 기존 warning,87.08초**, 격리0이었다. `test_dry_run_does_not_read_credentials_or_mutate`가 호스트의 실제 계정 조회를 사용해 이미 설치된 전용 계정을 발견하고 `service_user_exists`로 거부됐다. 운영 설치 가드는 정상이며, 테스트의 계정 부재 가정이 잘못 격리된 것이다. 실제 계정을 삭제하거나 운영 가드를 완화하지 않고 OS 조회 경계만 합성 입력으로 고정해 재검증한다.
- Terra/high가 설치 테스트1파일만 수정: 계정/그룹 조회를 합성 부재로 격리하고 기존 그룹 충돌 거부 대조군을 추가했다. 기존 사용자 충돌 거부와 실제 `check_account_available`는 유지한다. 집중 RED1 failed/14 passed → GREEN16 passed(0.25초), 관련 통합4 passed(0.50초). 실행 소스/설치 코드/의존성은 변경하지 않았고 서비스도 다시 시작하지 않았다.
- 한정 Terra/medium 재리뷰 발견사항0 승인 후 전체 KST verify **1810 passed/2 known xfailed/1 기존 warning(84.86초)**, 운영 상태/외부 네트워크 격리 위반0·문법/비밀정보 검사 통과. 위 실패 기록을 보존하며 최종 통과와 구분한다. 후속 문서·테스트 PR의 exact-head CI와 병합 상태는 해당 PR을 정본으로 확인한다.
