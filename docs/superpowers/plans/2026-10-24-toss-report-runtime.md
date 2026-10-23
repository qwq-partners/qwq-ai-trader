# Toss 보고 실제 실행 연결 Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking. 병렬 소유권·최종 통합은 아래 coordinator 계약을 따른다.

**Goal:** 검증된 실제 Toss 실행과 당일 보고 종목·가격 소비를 연결한다.
**Architecture:** root 보호 Toss helper가 authority/worker/원장을 소유하고 기존 봇은 bounded JSONL로 보고 입력과 결과만 교환한다.
**Tech Stack:** Python asyncio, 기존 Toss 승인/worker/parser/원장, root 보호 offline artifact.
**Spec:** `docs/superpowers/specs/2026-10-24-toss-report-runtime-design.md`.

## Global Constraints

기준 `b15fe957dffe39950a5199ac9d337e506700af7f`. production 소스/설정 직접 편집 없음.
주문·청산·매수 중지·월USD100·기존 observer namespace 불변. 외부 API/운영 자격 없이
검증한다. coordinator+최대3worker, fanout 금지, 파일 소유권 분리. 새 helper는
TOSS_REPORT_API=1에서만 생성·자동 재시작0, 실제 모델 metadata 미노출을 추정하지 않는다.

## Review Focus

- 검증 artifact와 실제 import가 다르거나 부모 자격이 자식에 새는 경우: 시작 전에 거부.
- 이전 날짜/구형 파일/장중 후보가 보고 cohort를 대신하는 경우: Toss unavailable·기존 보고 유지.
- 동일 슬롯 중복과 helper 재기동으로 발급 예산이 초기화되는 경우: 거부·새 grant 필요.
- timeout/취소/자식 사망 뒤 늦은 가격이 사용되는 경우: 메모리 삭제·기존 조회.
- request/cohort·가격/시각·TTL·grant/세션 경계 불일치: 응답 거부·순수익 미판정.

## Task 1 — 공유 계약과 부모 client (coordinator)

Files: 새 `src/data/providers/toss/report_protocol.py`, `report_helper.py`, 관련 tests.
Interfaces: frozen `ReportCohort(report_date, generated_at, members, cohort_id)`;
`ReportCohort.create(report_date, generated_at, members)`, `to_document()`, `from_document(document)`.
`ReportHelperClient(enabled=callable)`는 생성 I/O0, `async prepare_report(cohort, *, timeout=45.0)->bool`,
`get_report_quote(symbol, *, cohort=None)->Quote|None`, `async close()`를 제공한다.
공유 protocol은 request/result JSON 검증·해시·exact report snapshot을 제공하며 구현 전
signature를 worker에게 전달한다. JSONL request16KiB/response64KiB·원래45초 deadline 보존.

- [x] OFF·cohort/응답 위조·시각/TTL·pipe 상한·환경 유출·취소/종료·중복/재기동 실패 시험 작성/RED.
- [x] 최소 구현 후 GREEN, 실제 subprocess 대역으로 transport·정리 검증.

## Task 2 — trusted helper/조립/설치 준비 (Astra/high 별도 worktree)

Files: 새 `scripts/ops/toss_report/{launcher,package_release,install}.py`,
`src/observation/{toss_report_deployment,toss_report_helper}.py`, 전용 tests.
공유 protocol은 Task1 결과를 사용한다. 새 namespace만 사용하고 기존 observer 파일은 편집하지 않는다.
Root 검증 후 v3 전용 Deployment(report_display=True), claim_start, 실제 TossWorker/build_app,
ReportQuotes를 사용한다. 설치기는 dry-run 기본, useradd/systemctl/재시작0.

- [x] 승인/무결성 실패·단일 시작·실제 sealed child→완결 결과 시험 작성/RED.
- [x] 최소 구현/GREEN. 미설치와 실제 시작 증거를 분리한다.

## Task 3 — 아침 cohort 보존·실제 보고 순서 (Terra/high 별도 worktree)

Files: `src/analytics/daily_report.py`, `src/schedulers/kr_scheduler.py`, 전용 tests.
Shared interface: generator에 async `toss_report_prepare` callback을 두고, 아침 생성 시
cohort 저장, 복원 시 검증, evening `_update_results()` 직전에 await callback(cohort).
기존 `toss_quote_provider`는 helper의 getter와 연결한다. invalid/legacy cohort는 callback 없이
기존 가격 조회. 스케줄러 finally에서 helper.close(), 실제 보고시각/거래정책 불변.

- [x] 새 generator의 저장/복원 원시각·순위 보존, legacy fallback·wrong cohort·await barrier RED.
- [x] 구현/GREEN, 기존 보고 회귀 검증.

## Task 4 — 통합 See와 기록 (coordinator + 독립 Astra/xhigh)

- [x] 실제 startup→helper→원장→부모→보고 경로 통합 시험.
- [x] 로컬 전체 verify 실행·최종 관련 시험·문법/비밀 검사. 전체 실행의 실패/한계는 아래 영수증 참조.
- 최종 고정 소스 전체 검증은 이 브랜치 PR의 필수 `verify` CI가 정본이며 통과 후 병합한다.
- [x] 독립 중요 리뷰, 필요한 수정과 재검토(미해소 P0/P1/P2 0).
- [x] CLAUDE/CHANGELOG/문서 인덱스·모니터링 갱신.
- 커밋/푸시/CI/병합 결과는 `feature/toss-report-runtime-20261024` PR의 영수증을 따른다.
- [x] 실제 설치/배포 미실시 상태 기록; KIS 실스캔 감소와 Toss 실제보고 검증은 별도 상태.

## 진행 영수증

초기 source analysis: Astra/high startup 분석, Sol/high cohort 분석. 실제 모델/effort metadata
미노출. 기존 관련88시험 통과·격리0. 새로운 helper 설계 선택과 배포 범위는 spec을 따른다.


## 통합 진행 — Plan → Do → See

**Plan:** 기존 mutable 봇 내부에서 artifact를 import하는 대신, 별도 검증된 helper가
Toss 승인·토큰·원장을 소유한다. 부모는 아침 보고의 정확한 순위/종목·생성 시각·해시와
응답을 연결한다. 주문, KIS 구독, LLM 모델·예산은 이번 변경 대상이 아니다.

**Do:** helper는 봇 수명 중 한 번만 생성한다. scheduler 감독 루프가 재진입해도 닫힌
인스턴스를 새로 만들지 않는다. 구형/잘못된 보고 metadata는 기존 조회로 돌아가고,
새 목록은 기존 cohort의 가격을 읽지 못한다. 한 번 helper를 선택한 봇은 OFF 뒤
scheduler가 재진입해도 이전 supervisor의 주기 가격을 다시 연결하지 않는다. 응답 대기45초와 종료 정리 기한은 별도이며
종료 때 stdout을 작은 조각으로 비워 출력 상한 오류 이후의 교착을 피한다.
벽시계가 대기 도중 역행해도 최초 monotonic 기준의 TTL을 늘리지 않는다.

**See:** 실제 부모 client→격리된 launcher→v3 조립/시작 영수증→실제 worker·OAuth/
parser→원장 완료→JSONL→보고 생성기 저장/복원·가격 소비·본문 생성의 통합 시험이
통과했다(1passed/5.08초/rc0). HTTP만 합성 응답으로 대체하고 임시 경로의 root 소유권
metadata를 모사했으므로 실제 root 설치나 공급자 API 검증을 뜻하지 않는다. 두 종목의
순위/원시각·가격100·unknown 시장 기준·result_pct=None을 확인했다. 합성 토큰1회,
prices1회, 시작영수증1회, terminal2개, 거래 모듈 import0, 격리 위반0이다.

부모45시험 통과(9.64초), cohort 관련68시험 통과(8.80초), helper/기존 observer
관련177시험 통과(14.03초), 설치기10시험 통과. 각 실행은 중복을 포함하므로 합산하지
않는다. 전체 verify 및 최종 독립 리뷰는 아래 최종 영수증으로 확정한다.

초기 독립 Astra/xhigh 리뷰의 P1(stdout 포화 후 무기한 wait)·P2(요청 중 시계 역행으로
TTL 연장)를 재현 시험으로 확인하고 수정했다. 후속으로 scheduler 재진입과 잘못된
cohort의 가격 재사용을 막았다. worker 구현 중 최초 OAuth가 세션 종료를 넘길 수 있는
결함도 실제 sealed 시험으로 재현·수정했다. 요청 모델은 startup Astra/high,
cohort Terra/high, 독립 중요 리뷰 Astra/xhigh이며 실제 모델/유효 effort metadata는
미노출이다. 독립 Codex 대체는 기존 사용자 승인을 따랐으며 교차 공급자 리뷰는 아니다.

## 운영 상태와 다음 적용

이번 구현에는 root 설치·새 grant·플래그 변경·재시작·실제 API 호출이 없다. 마지막
확인한 운영 코드는 `a512268e`이며 기존 observer namespace와 원장을 변경하지 않았다.
신규 경로는 **기본 OFF**다. 코드 배포와 helper 설치·활성화는 별도 상태로 기록한다.

1. 검토·병합된 고정 소스로 `scripts/ops/toss_report/package_release.py`를 실행한다.
   기존 `scripts/ops/toss_observer/requirements.lock`과 해시 고정 offline wheelhouse만
   사용한다. 결과 manifest 지문·Python patch 버전·실제 import closure를 대조한다.
2. `/etc`, `/opt`, `/var/lib`, `/usr/libexec`의 `qwq-toss-report` 전용 namespace에
   맞춘 v3/report_display 문서를 준비한다. 실제 봇 UID/GID/보조 그룹·host·client,
   artifact/config/plan hash·명시 거래일(최대7일)·16:00–16:05·TTL60초·발급4회
   상한과 기존 승인/약관/저장/발급 소유권 근거를 결합한다. 승인 참조는 만들어 넣지 않는다.
3. 설치기를 먼저 기본 dry-run으로 검토한다. `--apply`는 새 경로만 생성하며 기존
   대상이 있으면 거부한다. dry-run은 전체 승인 유효성/실제 기동 성공의 증거가 아니다.
   부분 설치 실패 시 앞서 만든 경로는 자동 롤백하지 않으며 같은 `--apply` 재실행도
   기존 대상 거부로 끝난다. 네 namespace의 생성 결과·실패 영수증을 보존하고 별도
   검토로 복구한다. 자동 삭제·덮어쓰기·영수증 삭제로 재시도하지 않는다.
   root 설치 후 실제 봇 UID/GID/정확한 보조 그룹에서 아래 검사를 실행한다.
   launcher의 플래그는 `TOSS_API=1`이며 부모의 `TOSS_REPORT_API`와 다르다.
   OFF의 rc0은 검증 생략이므로 성공 근거로 세지 않는다.
   `env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TOSS_API=1 /usr/bin/python3 -I -S /usr/libexec/qwq-toss-report/launcher.py --check`
   는 manifest/설정/승인 조립을 검사하며 OAuth나 가격 API를 호출하지 않는다.
   기존 Toss 발급/관측 주체와 동시에 같은 client의 발급 권한을 소유하지 않게 확인한다.
4. 기존 승인된 secret 주입 경로로 부모 서비스에 `TOSS_CLIENT_ID`와
   `TOSS_CLIENT_SECRET`을 제공하고 값 자체를 출력하지 않는 존재 검사를 먼저 한다.
   설치기는 credential 파일이나 unit을 만들지 않으며 기존 observer의 비공개 자격을
   자동 복사하지 않는다. 저장소·artifact·설치 입력·로그에 값을 넣지 않는다.
   이후 `TOSS_REPORT_API=1`과 고정 봇 버전을 적용한다. 자식은 첫 유효16:00 보고에서
   시작한다. 소비한 시작 영수증을 지우거나 실패 시 같은 grant로 자동 재시작하지 않는다.
   봇 재시작/승인 만료 후에는 새 시작 권한 검토가 필요하다.
5. 다음 실제 보고에서 cohort·요청 시각·원장 terminal·가격 출처·폴백 건수를 대조한다.
   현재 보고 경로는 KIS 호출0이므로 이 변경의 직접 절감 대상은 pykrx/Naver다.
   KIS 분석 캐시의 절감은 동일 PID의 실제 스캔 전후 계측으로 별도로 확인한다.

### 오프라인 배포 후보 사전검사

기존 해시 고정 wheelhouse `/tmp/qwq-observer-wheels-20260917`에서 새 packager로
`/tmp/qwq-toss-report-prep-20261024/artifact`를 만들었다.264파일, manifest SHA256
`69d07c65fc559c69a500d7574a0ab05f69f568f180383281271b5563b43d514c`,
release ID `toss-report-preflight-20261024`. system Python `-I -S -B`에서 inventory
재계산 일치·실제 helper/deployment/aiohttp3.13.5 import·거래 모듈 import0을 확인했다.
이는 검토 중 소스의 임시 후보이며 root 설치/승인/운영 기동 증거가 아니다. 최종 고정
커밋으로 적용용 artifact를 다시 만들고 지문을 결합한다.

## 최종 검증·통합 영수증

독립 Astra/xhigh 중요 소스 리뷰는 초기 P1 1건/P2 2건을 재현·수정·재검증하고
미해소 P0/P1/P2 0으로 승인했다. 마지막 수정 후 실제 보고 통합을 포함한 독립13시험
통과(19.62초/rc0/격리0), root 관련12시험 통과(5.29초/rc0/격리0).
최종 staged Python diff SHA256은
`7731894e81ed7503747903ead1d79768eb6895f90935e34a1262e531d0d42967`이다.
독립 Sol/high 문서 검토의 `--check` 활성 플래그·부모 자격 공급·부분 설치 복구 설명을
반영하고 Sol/high 재검토로 미해소 P0/P1/P2 0을 확인했다.

로컬 전체 verify는 **rc1: 1 failed/9,025 passed/2 skipped/2 xfailed,963.20초**였다.
실행 중 마지막 P2의 scheduler8줄 수정이 반영되어 기존 함수의 `co_firstlineno`와
디스크 소스가 달라진 `test_wiring_positions_in_source`가 실패했다. 이를 전체 통과로
표시하지 않는다. 수정 종료 후 소스를 동결하고 해당 파일/새 기능6개 파일을 새 프로세스로
재실행해 **76passed/17.28초/rc0/격리0**을 확인했다. 제품 소스나 기존 검사는 이 실패를
없애려고 바꾸지 않았다. 최종 고정 커밋의 전체 결과는 PR 필수 verify CI에서 다시 확인한다.
스테이지된 전체29파일의 비밀 의심 패턴0·diff check rc0이며 독립 승인 소스 지문은 유지됐다.

worker의 중복 전체 실행은 root 검증과 자원을 나눠 쓰므로 coordinator가 중단했다.
중단 시6,060passed/2skipped/2xfailed,868.92초·격리0·rc2이며 전체 통과 근거가 아니다. 위 시험 성공만으로
Toss 운영 활성화, KIS WebSocket 용량 확장 또는 순수익 개선을 주장하지 않는다.


### 첫 CI의 UTC 테스트 시계 보완

PR #252 / run39983168339는1failed/9,026passed/2skipped/2xfailed,482.07초였다.
마지막 cohort 테스트의 추천일은2026-10-24로 고정했지만 `date.today()`는 실행 환경을
따라 UTC2026-10-23이 되어 파일 복원을 시도했다. 로컬 `TZ=UTC`에서 동일 실패와
운영 캐시 stat 차단1회를 재현했다(실제 파일 접근은 격리 가드가 막음).
제품 코드는 바꾸지 않고 테스트 날짜·보고 시계·임시 캐시 경로를 명시했다.
UTC/KST 양쪽에서 해당 cohort·scheduler·초과수익 scheduler 검사를 재실행해
각25passed(4.58초/4.69초)·rc0·격리0을 확인했다. 고정 커밋의 전체 결과는 후속 PR verify가 정본이다. 첫 CI 실패를 성공으로 덮어쓰지 않는다.


## 10월24일 설치·활성화 후속 — Plan → Do → See

**Plan:** 사용자의 “완료되면 다음 task 들도 이어서 진행” 요청에 따라 구현 다음의
전용 설치·고정 코드 배포·보고 플래그 활성화를 진행한다. 사용자 기존 조회/원장 저장/
단일 발급 승인 근거는 `docs/reviews/toss-pr-integration-2026-09-17.md`에 있으며,
현재 보고 표시 범위는 이번 후속 요청이다. 공급자 약관을 새로 검증했다는 뜻은 아니다.
허용 날짜는10월26~30일,16:00–16:05, grant 만료10월30일16:06 KST로 제한한다.
휴장에는 기존 거래일 검사가 실행을 생략한다. 날짜 자동 연장과 동일 grant 재시작은 없다.
자료 정리 기한은11월29일16:06 KST이며, 실행 승인 만료와 자료 삭제는 별개다.
새 namespace용 자동 삭제 예약은 아직 없으며 기한 전 별도 제한된 정리 절차가 필요하다.

**Do:** PR #252 병합 `4448301700730d7b86bdd74d9983ad551341bca8`는 최종
CI9,027passed/2skipped/2xfailed(475.88초), 격리0이다. 적용 artifact는 제품 소스가
동일한 `37a48ee0`에서 만든264파일이며 SHA256은
`dea4f86fe39452a84699fb795715e9120ae56b93157a1cd389613ff71d72c2c4`다.
실제 봇 UID/GID1000·보조그룹4/24/27/30/105/1000과 기존 client 일치를 확인했다.
기존 observer는disabled/inactive, 부모의 기존 자격 주입은존재하며 복사하지 않는다.
새 v3 문서의 plan SHA256은
`e3975c49e1ebc2b90257c173f951c81ce40717479a486575f9209a5ca53f6860`이다.
설치 dry-run ready=true/applied=false. 실제 설치·배포 결과는 아래 영수증으로 확정한다.

**See — 01:33 설치 시점 영수증(아래 최종 결과 이전):** root 설치 applied=true/rc0,01:33:14 KST 실제 봇
UID/GID/보조 그룹의 `TOSS_API=1 --check` rc0. 상태 디렉터리 소유1000:1000·0700·
비symlink, 검사 전후 변경0·token0·start receipt0을 확인했다. 부모 기존 환경 키 값은
모두 보존하고 `TOSS_REPORT_API=1`만 추가했다. 운영배포·사후 대조는 아직 미완료다.
이 시점에는 재시작 전 기존 PID이므로 새 코드·플래그는 실행 중 프로세스에 미적용이며
helper 시작·OAuth 발급·가격 API 호출0이다. 전체 verify→재시작→새 PID 복구/설정
대조→첫 거래일 아침 cohort→동일 새 PID 스캔 차분→16:00 보고 원장 순으로 확인한다.
독립 Astra/xhigh가 source/비식별 실제 입력의 조립과 시행 순서를 조건부 승인했고,
설치 후 조건은 위 무접속 실제 검사로 확인했다. 실제 모델/effort metadata는 미노출이다.
사전 상태01:31:28 KST: 운영a512268e/PID3076848·active·NRestarts0·복구ready·미체결0.
보호7파일·cron·비용 원장의 기존 terminal2,757행(정산2,756/unknown1)을 비교 기준으로 보존했다.
비공개 입력/검증/운영 영수증은 `~/.local/share/qwq-deploy/toss-report-20261024/`에 둔다.


### 운영 적용 완료

고정 `4448301700730d7b86bdd74d9983ad551341bca8`를 기존 `local_deploy.sh`로
배포했다. 운영 전체 **9,027passed/2skipped/2xfailed,722.65초**, 격리0·문법·
비밀정보 검사 통과. 배포rc0·자동 롤백 없음. 01:46:06 KST 새 PID3125313이 기동했다.
01:48:43 KST 사후 대조에서 기동156.8초·active·NRestarts0·복구ready·미체결0,
브로커 연결 정상·새 실행 오류0·보고 루프 종료 오류0을 확인했다.
보호6파일은 동일하며 `.env`는 기존 모든 키 값을 보존하고 `TOSS_REPORT_API=1`만 추가했다.
실제 새 PID의 플래그·기존 Toss 자격 존재를 확인했다. 기존 비용 terminal2,757행,
월USD100 설정·cron·사용자 진화 설정·매수 중지를 보존했다. 이전 PID3076848의
정상 종료 완료·미종료 HTTP 경고0·HTTP 정리 실패0을 확인했다.

설치·코드 배포·활성화는 **완료**, token/start receipt0으로 **실제 API 첫 실행은 대기**다.
첫 허용일은10월26일이며 아침08:25의 새 cohort가 있어야16:00 보고가 Toss를 요청한다.
후보0·휴장·잘못된/구형 cohort는 실수집 성공으로 세지 않는다. 새 경로 실패 시 기존
보고 조회를 사용하며 자동 helper 재시작·grant 연장·시작 영수증 삭제는 하지 않는다.

남은 순서는 [실측 체크리스트](../../operations/monitoring-checkpoints.md#다음-실제-실행의-판독-순서)의
아침 cohort → 같은 PID 실제 스캔 계측 →16:00 원장/출처 대조다. 설치만으로 KIS 제한
해결·조회 절감·순수익 개선을 주장하지 않는다. 11월29일16:06 보존 기한 정리는 별도
준비 대상이다. 이 후속은 문서만 갱신하며 추가 재시작/배포하지 않는다.


영수증 연결: 위 비공개 디렉터리의 `install-result.json`은 실제 설치,
`installed-check.json`은 실계정·상태 권한·시작 미소모,
`before.json`/`after.json`은 브로커 연결·보호 경로별 지문·기존 비용행 대조,
`env-change.json`은 기존 환경값 보존과 플래그 단독 변경,
`deploy.log`/`deploy-exit.json`은 전체검증·배포 종료,
`postcheck.json`은156.8초 후 상태/이전 정상 종료를 기록한다.
독립 Sol/high 문서 최종 대조는 차단 없음이며 앞선 시점 명료화와 영수증 추적성을 반영했다.
실제 모델/effort metadata는 미노출이고 교차 공급자 리뷰로 주장하지 않는다.
