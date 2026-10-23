# KIS 조회 절약과 Toss 분석 연결 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. 작업자는 담당 파일만 수정하고 coordinator가 통합한다.

**Goal:** KIS의 반복 조회와 누락된 거절 피드백을 줄이고 Toss의 실제 분석 연결 조건을 정비한다.
**Architecture:** 직접 GET의 공용 오류 처리, 분석 전용 일봉 재사용, 개별 Toss 읽기 소비자 순서다.
**Tech Stack:** Python asyncio, aiohttp, pytest.
**Spec:** [설계](../specs/2026-10-23-kis-toss-read-offload-design.md).

## Global Constraints

- 기준 main `1c418bb8`; 운영 `4f39c90c`와 dirty 설정은 개발 중 보존한다.
- 주문/계좌/청산 메서드, 조회 한도, 자동 재시도는 바꾸지 않는다.
- 운영 API 없이 시험한다. 이전 독립 Codex 리뷰 대체 승인을 유지한다.

## Review Focus

- 오류 본문 유실/과대/지연/취소가 새 무제한 대기를 만들지 않아야 한다.
- 정확한 EGW00201만 공용 대기로 연결되고 한 응답은 한 번 집계된다.
- 요청 깊이·브로커·날짜·세션을 넘는 캐시 재사용을 막는다.
- 취소된 대기자와 남은 대기자의 수명을 구분하고 미회수 태스크를 남기지 않는다.
- 관측 승인과 실제 분석 소비자 연결·설치 상태를 구분한다.

## Task 1: 직접 KIS GET 거절 피드백

담당 Astra/high, 별도 `qwq-kis-feedback-20261023` worktree.
파일: `kis_direct_metrics.py`, `kis_market_data.py`, `kr_screener.py`의 직접 GET,
관련 tests. 인터페이스: 모든 직접 GET에서 bounded 오류 검사 후 기존 반환 유지.

- [x] HTTP500 EGW00201 미분류 재현: 9 failed/13 passed, rc1.
- [x] confirmed code를 공용 note_rejection으로 전달: 관련54시험 통과, rc0.

## Task 2: 분석 일봉의 공유 요청·짧은 캐시

담당 coordinator. 파일: `src/data/providers/analysis_daily_cache.py`,
`tests/test_analysis_daily_cache.py`; Task1 통합 뒤 스크리너와 섹터 분석 호출 연결.
인터페이스: `get_analysis_daily_prices(broker, symbol, days=...)`는 기존 list 계약을
유지하며 브로커별 캐시와 진행 요청을 공유한다.

- [x] 동일 요청·깊이·반환 변이·실패 재조회·만료/세션·취소 시험: 기존 단순 전달에서2실패 재현.
- [x] 60초/64종목/500봉 캐시와 요청 합치기 구현, 관련 시험 통과.
- [x] 실제 브로커와 스크리너/섹터를 통과하는 반복 HTTP 2→1 재현·통과. 합성 자료에서 점수 동일.
- [x] health의 `analysis_daily_requests`에 요청/캐시적중/동시합치기/공급자호출을 별도 노출.
- [x] 독립 리뷰 P2 수정: 새 25봉 캐시가 있는 동안 이전 30봉 요청에 세 번째 대기자가
  합류해도 최신 자료를 삭제하지 않는다. 수정 전105/기대106 실패, 수정 후 캐시18시험·
  KIS/캐시 관련81시험 통과, rc0·격리0. 리뷰어가 수정 재검토 후 한정 승인했다.

## Task 3: Toss 실제 소비자 연결

Sol/high가 읽기 전용 분석으로 운영 factory/승인/현재 호출 위치를 확인한다.
- [x] 실제 소비자·출처/시각 계약·승인 수명주기·검증 경계를 확정한다.
- [x] 가능한 연결을 구현·시험하고 미확정 공급자 기준/설치 조건은 구분해 기록한다.

첫 연결은 새 v3의 명시 `daily_report_current_price` 소비만 허용하는 in-process supervisor다.
별도 서비스의 비공개 파일 공유·권한 확장은 없고 v1/v2는 관측 전용으로 남는다.
보고 스케줄러는 동적으로 현재 provider를 찾는다. 구현 파일: Toss approval/runtime/
observation, supervisor, 보고 생성기, 작은 스케줄러 주입 및 관련 시험. root는 스케줄러
주입, Astra/high 별도 worktree는 나머지를 담당한다. `Deployment.report_display=True`와
grant의 `report_display`도 필요하며 기본값은 False다. 첫 연결에서 KIS 감소를 과장하지
않는다(기존 보고 KIS 분기는 운영 `_bot` 미주입). 새로운 운영 deployment는 별도다.

Do/See: 승인 loader→실제 worker→합성 OAuth/GET→가격 parser→완결 원장→supervisor→
일일 보고를 실제 호출하는 시험을 포함해 관련200개가 통과했다(rc0, 격리0).
supervisor가 나중에 만들어지는 순서는 실제 보고 scheduler 시험으로 별도 확인했다.
TTL은 원 관측/수신시각과 monotonic 경과를 모두 검사한다. 슬롯 종료 직전 제출 뒤
벽시계가 멈춰도 최초 deadline을 넘은 결과는 게시하지 않는다. 새로운 목적이 없는
v1/v2와 미설치 deployment는 보고 소비를 활성화하지 않는다.

## SEE와 통합

- [x] 독립 Astra/xhigh 중요 리뷰, 관련 시험, 전체 verify, 비밀 검사.
- [x] 문서·CHANGELOG·CLAUDE·인덱스 갱신, 구현 `a75f0529` commit/push 및
  [PR #250](https://github.com/qwq-partners/qwq-ai-trader/pull/250) 생성.
- [x] 운영 미적용·새 Toss 설정 미설치·요청 감소 실측 미실행을 구분한다.

## 운영 적용 전 확인 및 남은 순서

1. KIS/cache 코드 적용 후 같은 길이·동일 세션에서 TR/source별 HTTP 시도·EGW00201,
   분석 요청/적중/합쳐진 요청/공급자 호출, 자료 나이와 후보 결측을 함께 비교한다.
   공급자 호출 수는 HTTP 페이지 수와 다르다. 60초 캐시는 5분 스캔 자체를 생략하지 않는다.
2. Toss v3의 날짜·session·선정 종목·TTL·고정 소스/설정·새 grant를 보고 목적에 맞춰
   준비한다. 현재 후보 보고 시각은16:00이며 기존 장중 관측09:00–15:20은 맞지 않는다.
   5분 수집과 보고의 실행 순서도 확인해야 한다. 같은 시각 예약만으로 가격 준비를
   보장하지 않는다. 신선한 같은 종목 가격이 없으면 기존 소스로 조회한다.
3. 새 설정 설치 후 보고 시각에 실제 Toss 공급 여부·출처/시각 보존·기존 조회 생략을
   확인한다. 이 작업은 기존 별도 관측 서비스의 파일/권한이나 예약을 변경하지 않는다.
4. 후보 WS/대시보드 및 일봉 확대는 별도 후속이다. 가격시장·기준일·수정주가·깊이가
   맞는지 먼저 검증하고, KIS 주문/계좌/청산 공급자를 자동 교체하지 않는다.

구현·오프라인 검증은 완료했다. 운영 활성화·배포·실제 요청 감소·수익 개선을
완료로 표시하지 않는다. 작업자/리뷰어의 요청 모델은 위 역할대로이며 실제 모델과
실효 effort metadata는 미노출이다. 독립 Codex 리뷰이며 교차 제공자 리뷰로 부르지 않는다.

## 독립 리뷰 영수증

기준 `1c418bb8469ad910d9504b5a7a0589194ca8a33d` 대비, 구현하지 않은 독립
Astra/xhigh 요청 리뷰어가 단계별 검토했다. 작업 파일은 리뷰어가 수정하지 않았다.

- KIS/cache: 최신 짧은 자료를 삭제하는 P2 한 건 수정·재현·재리뷰 뒤 승인.
  관련7파일81 passed/4.10초/rc0·격리0. 세션 변경 후 늦은 완료·여러 깊이의 완료
  순서·모든 대기자 취소와 재시도·event loop 분리의 추가 합성 검증도 통과했다.
- Toss 보고: 관련7파일198 passed/19.37초/rc0·격리0, 미해결 P0/P1/P2 없음.
  별도16:00 허용 세션의 실제 worker/parser/원장→supervisor→보고 전달 및 종료 후
  무효화가 통과했다. 실제 loader로 만든 v2 WS 승인은 새 보고 소비를 거부했다.
  최초 작업 기한·TTL·grant 만료·중단·원장 실패·기존 조회 폴백을 확인했다.
- 위 승인은 코드와 합성 자료에 한정한다. 전체 verify/비밀검사 결과, CI,
  운영 설치와 조회 감소 실측은 각 영수증으로 별도 기록한다.

## 전체 검증 영수증

통합 작업본에서 다음 명령을 실행했다.

```bash
QWQ_VERIFY_PYTHON=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python bash scripts/dev/verify.sh
```

**8,910 passed / 2 skipped / 2 xfailed**,
711.95초, 전체 스크립트rc0이다. Python 문법·비밀정보 검사 통과, 운영 상태·외부
네트워크 접근0을 확인했다. 경고8개(pykrx 등의 기존 의존성과 multiprocessing
fork 경고)는 실패와 구분한다. 작업본/staged diff 검사와 신규 문서 링크 확인도 통과했다.

현재 운영 체크아웃의 HEAD는`4f39c90c5fed132ac067d84eaf4106ed24c78046`,
사용자 dirty `config/evolved_overrides.yml`가 그대로다. 이번 단계에서 운영 서비스,
API, 자격증명, 재시작, 예약, Toss grant/deployment 설치를 수행하지 않았다.
운영 상태/PID/복구 지표는 이번에 재조회하지 않았으며 이전 배포 영수증과 구분한다.

구현은 `a75f05291c1fe62bc5713eae5a649319bdabb1a2`로 푸시했다. 이후 변경은
이 검증 영수증과 PR 연결만 갱신하며 제품 코드/시험은 동일하다. 원격 CI와 최종
병합 SHA의 정본은 [PR #250](https://github.com/qwq-partners/qwq-ai-trader/pull/250)이다.
PR 생성과 운영 배포를 혼동하지 않는다. 앱의 PR 첨부 도구는 응답 지연으로 확인
불가였지만 GitHub PR 생성은 성공했다.

## 10월23일 후속 — 운영 배포와 Toss 실제 기동 경로 점검

사용자가 위 다음 순서에 대해 `ㄱㄱ`로 진행을 승인했다. 고정 대상은 PR #250 병합
`a512268e64717d6db84b67734b3d3360a455594a`다. 검토한 `4416fa5b`와 tree가 같고,
PR CI 8,910 passed/2 skipped/2 xfailed·467.31초·격리0·문법/비밀 검사 통과를 확인했다.

배포 전22:37:17 KST 확인: 운영`4f39c90c`/PID3053423·NRestarts0·복구ready·미체결0,
실행 중 관측 서비스 없음. 보호7파일·월USD100·기존 비용 원장2,740행(정산2,739/
미확인1)과 cron을 비공개 영수증에 보관했다. 기존 `local_deploy.sh`의 전체 검증·
재시작·헬스체크·초기 실패 자동 롤백을 사용하며 최종 결과는 아래에 기록한다.

### Toss: 설정만으로 활성화 가능하다는 설명 정정

독립 Sol/high 읽기 전용 점검에서 실제 운영 진입점의 추가 공백을 확인했다.
실제 모델/실효 effort metadata는 미노출이다. 관련54시험 통과·외부/운영 접근0은
아래 운영 기동 연결을 증명하지 않는다. 앞선 통합 시험은 실제 worker/parser/원장을
통과하지만 합성 Deployment와 supervisor 할당을 사용했다.

1. `scripts/run_trader.py`는 봇 생성 후 `bot.run()`을 부르며,
   `_toss_observation_deployment`를 주입하는 운영 경로가 없다. `attach_shadow`는
   해당 객체가 없으면 unavailable로 끝난다.
2. 실제 `src/observation/toss_deployment.py` 조립기는 v3 capability를 거부하며
   `report_display=True`도 넘기지 않는다. 기존 trusted launcher는 별도 observer를
   시작하는 경로이므로 거래 봇 내부 supervisor로 객체를 전달하지 않는다.
3. 보고 대상은 당일 아침 `RecommendedStock` 목록이나, 현재 supervisor는 장중
   `ScreenedStock` 목록을 받는다. 장 마감 뒤 오래된 후보는 제외되고 보유 종목만
   남을 수 있다. 보고 종목과 수집 종목의 일치를 보장하지 않는다.
4. 같은16:00에 수집과 발송을 예약해도 결과 도착 순서는 보장되지 않는다.
   승인 세션과 TTL에 더해 수집 완료 대기와 실패 시 기존 조회가 필요하다.

따라서 **Toss는 운영 미활성화**이며 플래그·날짜·grant만 바꿔 해결하지 않는다.
현재 Git HEAD를 StartupAttestation으로 꾸미거나 기존 별도 서비스의 private state/
발급 기록·원장을 재사용하지 않는다. 이 추가 구현은 KIS 배포와 분리한다.

### 다음 구현의 완료 조건

- 기존 in-process 설계를 유지한다면, 실제 시작 시 고정 artifact를 검증하는 trusted
  launcher와 명시 deployment 주입 경로부터 연결한다. mutable checkout의 임의
  런타임 계산을 신뢰된 시작 증명이라고 하지 않는다. 별도 sidecar를 유지하는 대안은
  추천 종목 입력/가격 출력용 IPC까지 포함한 다른 설계이며 설정 변경으로 분류하지 않는다.
- v3 조립·검증에서 새 보고 목적과 capability, opt-in, 날짜/세션을 실제로 통과시킨다.
- 당일 보고 목록(재시작 시 복원 포함)과 순위를 별도 snapshot으로 전달하고,
  장중 periodic screen이나 보유 목록으로 대체하지 않는다.
- 수집 완료를 제한 시간 안에서 기다린 뒤 발송한다. 실패·만료·기한 초과는 기존
  조회로 돌아가며, 실제 진입점부터 실제 보고 cohort까지의 통합 시험을 통과해야 한다.

Toss 보고 연결은 현재 KIS 호출 절감 경로가 아니다. 운영 보고에 `_bot` 주입이
없어 기존 KIS 분기가 호출되지 않으며, 첫 Toss hit는 pykrx/Naver 조회를 생략한다.
신규 운영 구조의 비용 대비 효과를 판단할 때 이 사실을 유지한다. KIS의 직접적인
절감은 분석 캐시 계수·TR별 요청/거절과 이후 후보 WS/일봉 분담에서 확인한다.

### 실측 해석 기준

배포 전22:27:17–22:37:17의10분 저널은12건, HTTP500/EGW00201/EGW00215/
ERROR가 모두0이었다. 이미 장외에 호출 제한이 재현되지 않은 구간이므로 배포 후
0건도 개선률 증거가 아니다. 프로세스 재시작 전후 누적 계수를 직접 빼지 않는다.
새 프로세스의 두 시점 사이에 요청/적중/동시 합치기/공급자 호출과 TR별 시도/거절을
읽고, 다음 실제 스캔 구간에서 자료 나이·후보 결측을 함께 확인해야 한다.

추가로 기존PID3053423의22:38:19경(최초 health 파일 기록시각)–22:49:04를 비교했다.
브로커HTTP4회·직접HTTP1회가 모두 성공, 거절/HTTP오류0이다. 분석 스캔이 활발한
시간의 표본이 아니며 새 프로세스의 기동 조회와 직접 비교해 개선률을 계산하지 않는다.

비공개 실행 근거: `~/.local/state/qwq-deploy/kis-toss-20261023/`.
계좌 금액·토큰·원문 로그·비용 원장은 저장소에 복사하지 않는다.

### 운영 배포 완료 — 22:54 사후 확인

기존 `local_deploy.sh`가 고정 `a512268e64717d6db84b67734b3d3360a455594a`를
검증·배포했다. 전체 **8,910 passed / 2 skipped / 2 xfailed**,707.44초·경고8개,
운영 상태/외부 네트워크 시험 접근0·문법/비밀 검사 통과, 스크립트rc0·롤백 없음.

22:49:57 KST 새 프로세스 시작,22:54:03.993915 KST 별도 사후 확인:

- 운영 HEAD가 고정 SHA와 일치하며 PID3076848·active/running·기동246.2초·
  NRestarts0. 실제 작업 디렉터리와8080 포트 소유자가 같은 프로세스다.
- 복구ready·미체결0, 전문가 스케줄러5개 슬롯 시작. 새 실행 저널147건에서
  ERROR/CRITICAL/Traceback0·보고 루프 예외 사망0.
- 보호7파일·사용자 dirty override·매수 중지·월USD100·cron 보존.
  배포 전 비용 원장2,740행은 내용까지 동일하며, 사후 정산2,741/미확인1이다.
  자연 실행의 신규 정산2행과 기존 행 변경을 구분한다.
- 이전PID3053423 정상 종료 완료, 해당 실행의 미종료 HTTP 세션 경고0·HTTP 정리 실패0.
- 기존 관측 서비스는 비활성 그대로이며 새 Toss 설치·예약·grant 발급은 수행하지 않았다.

22:54:42 새 health에서 `analysis_daily_requests`의 요청/캐시적중/동시 합치기/
공급자 호출이 모두0인 것을 확인했다. 새 계측 노출과 실제 절감 입증은 별개다.
원자료는 비공개 `before.json`, `after.json`, `deploy.log`, `new-process-start.json`에
보관한다. 이번 문서 후속은 운영 재배포 대상이 아니다.
