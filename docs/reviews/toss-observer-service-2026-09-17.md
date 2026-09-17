# Toss 독립 관측 서비스 — Plan → Do → See

## 현재 상태

사용자가 상세 설계와 구현·독립 리뷰 후 별도 Toss 서비스 ON을 승인했다. 기존 거래 봇 무재시작·추가 KIS0·주문 무연결 범위다. **구현/리뷰 진행 중이며 실제 운영 설치·자격 복사·토큰 발급·활성화는 아직 하지 않았다.**

- 기준 main/root checkout: `8ff2f55f4515d049fbcd5fa1eea009b3ffc9f164`; 원격 main과 새 artifact만 후속 갱신하며 거래 checkout은 유지한다.
- 실행 계획: `docs/superpowers/plans/2026-09-17-toss-observer-service.md`, 상세 설계 승인 및 역할/인터페이스 고정 `2e772d6`.
- 09-17 20:30 KST 착수로 당일 첫 관측 창이 지났다. 09/18·21·22 및09/22 18:00만료 변경 여부를 사용자에게 확인 중이다. **답변 전 실제 날짜 자동 연장/과거 채움은 하지 않는다.**

## Plan / 역할

| 작업 | 구현 | 결과와 독립 리뷰 |
|---|---|---|
| 실행 신뢰·승인·시작 영수증 | Astra/high, launcher 격리 worktree | `0decfbf`, 집중48·전체1732 passed/2knownxfail, Astra/xhigh Approved·Minor1 보강 및 재리뷰 승인 |
| 캐시 입력·관측 감독·private 상태 | 부모, runtime 격리 worktree | `f5c60a7`, 집중39·전체1723 passed/2knownxfail, Important1 수정(db134ad)·재리뷰 승인 |
| 패키징·설치·원장 보존 | Astra/high, installer 격리 worktree | `696b91c`, 집중30·전체1714 passed/2knownxfail, 별도 Astra/xhigh Approved |
| 통합·운영 실행 | 부모; 최종 Astra/xhigh 별도 리뷰 | 미완료 |

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
- 후속 **UTC 순차 전체 verify1807 passed/기존xfail2/기존warning1,89.26초** 통과, 격리0·문법/비밀정보 검사 통과. KST와 추가 sealed-runtime 테스트가 들어간 최종 트리는 별도 확인한다. 기존 병렬 실행 실패 기록은 보존한다.

## See / 아직 남은 게이트

- 각 작업 독립 spec/quality 리뷰 및 수정, 통합 sealed artifact+실worker fakeHTTP 경계 인수.
- UTC/KST 전체 verify·비밀정보 검사·최종 독립 broad 리뷰·required CI exact head.
- 실제 root/전용 UID 설치·check-only(발급0)·새 unit 한 번 시작·기존 PID/설정 지문 대조.
- 장외 ON과 실제 GET/원장 관측을 구분하고3영업일/유효 가격 비교/소비자 승격은 별도 미완 인수로 유지한다.

## 운영 읽기 전용 확인

구현 중 재조회: `qwq-ai-trader.service` active/running, PID3274983, 시작09-17 00:47:47 KST; root checkout8ff2f55/clean. `qwq-toss-observer.service` not-found/inactive. 21:10:36 KST 로컬 health 메모리 읽기에서 broker_connected=true, broker/risk pending0, stale0. 추가 KIS 요청을 만들지 않는 경로만 조회했다. 기존 서비스 재시작/checkout 변경/주문/설정 변경0. 이 조회는 새 서비스 설치/인수 증거가 아니다.
