# Toss 독립 관측 서비스 — Plan → Do → See

## 현재 상태

사용자가 상세 설계와 구현·독립 리뷰 후 별도 Toss 서비스 ON을 승인했다. 기존 거래 봇 무재시작·추가 KIS0·주문 무연결 범위다. **소스 구현·독립 리뷰·UTC/KST 전체 로컬 검증은 완료했으며 실제 운영 설치·자격 복사·토큰 발급·활성화는 아직 하지 않았다.** CI exact head와 원격 main 병합 이력은 [PR #74](https://github.com/qwq-partners/qwq-ai-trader/pull/74)가 정본이다. 원격 병합 여부와 거래 봇의 로컬 checkout 갱신을 혼동하지 않는다.

- 기준 main/root checkout: `8ff2f55f4515d049fbcd5fa1eea009b3ffc9f164`; 원격 main과 새 artifact만 후속 갱신하며 거래 checkout은 유지한다.
- 실행 계획: `docs/superpowers/plans/2026-09-17-toss-observer-service.md`, 상세 설계 승인 및 역할/인터페이스 고정 `2e772d6`.
- 09-17 20:30 KST 착수로 당일 첫 관측 창이 지났다. 09/18·21·22 및09/22 18:00만료 변경 여부를 사용자에게 확인 중이다. **답변 전 실제 날짜 자동 연장/과거 채움은 하지 않는다.**

## Plan / 역할

| 작업 | 구현 | 결과와 독립 리뷰 |
|---|---|---|
| 실행 신뢰·승인·시작 영수증 | Astra/high, launcher 격리 worktree | `0decfbf`, 집중48·전체1732 passed/2knownxfail, Astra/xhigh Approved·Minor1 보강 및 재리뷰 승인 |
| 캐시 입력·관측 감독·private 상태 | 부모, runtime 격리 worktree | `f5c60a7`, 집중39·전체1723 passed/2knownxfail, Important1 수정(db134ad)·재리뷰 승인 |
| 패키징·설치·원장 보존 | Astra/high, installer 격리 worktree | `696b91c`, 집중30·전체1714 passed/2knownxfail, 별도 Astra/xhigh Approved |
| 통합·운영 실행 | 부모; 인수 테스트 Terra/high; 최종 Astra/xhigh 별도 리뷰 | 소스 통합·로컬 검증·독립 리뷰 완료, 실제 운영 미실행 |

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
| S01·S02 | `test_toss_observer_launcher.py`, `test_toss_observer_deployment.py`: OFF·검증 전 import0·파일/identity/plan 변조·시작 권한. 실제 systemd 자격 전달 미실행 |
| S03·S04 | deployment 및 기존 `test_toss_live_authority.py`, `test_toss_token_contract.py`, `test_toss_runtime_factory.py`: 단일 시작·발급/승인/unknown 경계. 실토큰 인수 아님 |
| S05·S06 | `test_toss_observer_positions.py`, `test_toss_observer_service.py`: bounded cache 입력·결측/빈 보유·선정/overflow·슬롯/비교 제외 |
| S07·S08 | service/status/integration 테스트: 실패 시 성공 시각 불변·원장 대조·공통 종료 예산. sealed-runtime은 정상 worker·지속 원장·receipt1·SIGTERM 종료. 실제 systemd 강제 종료 미실행 |
| S09·S10 | integration/sealed-runtime/package/install 테스트 및 UTC/KST 전체 verify: 거래 모듈 분리·실행 연결·폐쇄 패키지·설치 dry-run. sealed-runtime은 설치된 시험 의존성 복사이며 lock 검증은 별도 wheel 빌드로 확인 |
| S11 | `test_toss_observer_retention.py`: 합성 cohort 기한/권한/경로/실행 중 삭제 방어. 실제 보존 timer 인수 미실행 |
| S12 | 기존 봇 읽기 전용 기준선만 확인. 새 서비스 설치 전후 PID/설정 지문 대조는 운영 게이트로 남음 |

## See / 아직 남은 게이트

- 완료: 각 작업 독립 spec/quality 리뷰·수정·재리뷰, sealed artifact+실worker fakeHTTP 통합 인수, UTC/KST 전체 verify·비밀정보 검사·최종 broad 및 한정 리뷰.
- PR #74의 required CI exact head 성공을 확인한 후에만 main 병합한다. 기존 거래 봇 checkout은 병합 후에도 유지한다.
- 실제 root/전용 UID 설치·check-only(발급0)·새 unit 한 번 시작·기존 PID/설정 지문 대조.
- 장외 ON과 실제 GET/원장 관측을 구분하고3영업일/유효 가격 비교/소비자 승격은 별도 미완 인수로 유지한다.

## 운영 읽기 전용 확인

구현 중 재조회: `qwq-ai-trader.service` active/running, PID3274983, 시작09-17 00:47:47 KST; root checkout8ff2f55/clean. `qwq-toss-observer.service` not-found/inactive. 21:10:36 KST 로컬 health 메모리 읽기에서 broker_connected=true, broker/risk pending0, stale0. 추가 KIS 요청을 만들지 않는 경로만 조회했다. 기존 서비스 재시작/checkout 변경/주문/설정 변경0. 이 조회는 새 서비스 설치/인수 증거가 아니다.
