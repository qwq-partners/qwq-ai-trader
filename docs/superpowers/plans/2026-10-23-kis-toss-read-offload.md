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

## T02

KIS·Toss 소비자별 역할 계약 — 2026-10-24

**결정: 현재 코드/자료 의미를 그대로 보존하는 추가 대체 소비자는 아직 없다.** 분석 일봉 캐시는 구조상 가장 가까운 후보지만, 시장·원주가/수정주가·당일 봉의 의미가 같다는 증거가 없어 적용 후보로 승인하지 않는다. 10월26일 실제 부하(T03)·보고 인수(T04) 후 확대 필요성을 판단하고, 증거가 없으면 T05를 DEFERRED로 기록하고 T06→E01 종목 선정/진입 평가로 돌아간다. 데이터를 더 연결하는 것 자체가 순수익 개선은 아니다.

### 확인한 범위와 공식 계약

- 코드 기준은 `603d2ee0`의 KIS/Toss 경로다. 운영 `44483017`에 관련 구현이 배포돼 있으나 현재 보고 연결은 pykrx/Naver 조회를 생략하는 경로이며 KIS 장중 선정/진입 요청을 줄였다는 뜻이 아니다.
- 10월24일 비인증 공개 명세를 다시 확인했다. [Toss OpenAPI](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json)는1.2.24, 원문 SHA256 `7e753c21dc4b938d8f9610ce3efd6542783018977cb030e39db5a006f5b59d40`다. [AsyncAPI](https://openapi.tossinvest.com/openapi-docs/latest/asyncapi.json)는1.2.2, SHA256 `130251057fd9535a3e276099f9166b445f8c51f505f30540758e4b209231282e`로 기존 WS 근거와 같다. 브라우저의 AsyncAPI 읽기 오류 후 직접 공개 JSON 조회로 확인했다. 시장자료·토큰·계좌 API 호출은0이다.
- 저장소 fixture의 OpenAPI1.2.17을 자동 갱신하지 않았다. 최신 명세에 수급·랭킹·섹터·시장지표 API가 있어도 현재 실행기의3개 GET 허용 목록/정규화/권한과는 별개다. “Toss가 제공하지 않는다”와 “우리 연결에는 없다”를 구분한다.
- [KIS 공식 기간별시세 샘플](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_itemchartprice/inquire_daily_itemchartprice.py)은 `J=KRX`, `fid_org_adj_prc=0` 수정주가, `1` 원주가로 명시한다. 현재 `src/execution/broker/kis_kr.py:get_daily_prices`는1을 보내므로 “수정주가 반영”이라는 주석을 계약 근거로 쓰면 안 된다. 이 불일치는 [9월30일 평가](../../research/current-engine-evaluation-2026-09-30.md)에서도 이미 확인했다. 여기서는 요청값/캐시/전략을 변경하지 않는다.

공개 원문과 읽기 감사는 `~/.local/state/qwq-task-audit/20261024/t02-public-spec/` 및 비공개 T02 검토 기록에 보존한다. 공식 명세 확인은 실제 응답의 정확성·시장 기준·허용 권한·효과를 인증하지 않는다.

### 현재 KIS 소비자와 대체 판단

아래 빈도는 소스/기본 설정 기준이며 실측 횟수가 아니다. 실제 HTTP 시도/거절·캐시 적중은 T03의 같은 실행 구간에서 확인한다.

| 요청/소비자 | 빈도·캐시 / 자료 의미 | Toss 적용 판단 |
|---|---|---|
| 계좌·보유·미체결·체결 | 주문 가능 시간 잔고30초/장외300초, 미체결 있으면 체결2초/없으면15초. 실제 KIS 계좌와 주문 귀속 | KIS 유지. 다른 증권사 계좌 응답으로 대체할 대상이 아님 |
| 현재가 `FHKST01010100` | 배치 갭/진입 재확인·관찰·보고 등 종목별 요청. 주문 가격 판단에 사용, 시장J | Toss `/prices`는 현재 정규화에서 시장 기준unknown이고 기존 필드 다수를 제공하지 않음. 진입·실행 대체 금지 |
| 호가 `FHKST01010200` / WS | 매도 best bid·실행용 깊이. KIS 주문 가능한 시장과 연결 | Toss 통합 호가를 KIS 체결 가능한 호가로 취급할 수 없음 |
| KIS 실시간 체결가/호가 피드 | `run_trader.py:964,2160`에서 시장자료를 engine.emit·보유종목 즉시청산 검사에 전달하고 호가는 별도 진입관측으로 전달. 보유 우선/나머지롤링. 미커버 보유종목은REST20초 루프 | 거래 판단과청산에직결되므로 Toss가격을동일이벤트로주입하지않음. 논리감시40종목 상수와채널등록cap≤41은다른단위이며 실제남은구독수로해석하지않음 |
| 분석 일봉 `FHKST03010100` | 스크리너 상위20의30봉·변동성/거래량·섹터ETF25봉. 공용 분석 캐시60초·64심볼·동일 깊이 동시 요청 합치기. 브로커 최대5페이지/500행 | 구조상 후보이나 **미적격**: KIS는KRX 원주가·오늘 행이 있으면 확정 표식 없이 보존하는 반면, Toss는 시장 기준unknown·adjusted기본true이며 로컬 파서가 오늘 행을미확정으로 분류한다. 날짜/값/확정 여부의 동등성 미입증. 전역 또는 캐시 통째 교체 금지 |
| 캐시 밖 직접 일봉 `FHKST03010100` | 코어MA200/거래량 상태·core screener250봉, 벤치마크/수확shadow/CF·스케줄러 상태검사 등25/45/60/250봉 요청. 각 배치·코어/종가 작업의 호출 시점에 실행하며 분석 캐시60초/동시합치기는 적용되지 않음 | 이번 캐시 후보의 범위 밖. 페이지별 실패 때 이미 확보한 짧은 시계열이 반환될 수 있어 소비자별 최소 길이/skip 검사를 보존해야 함 |
| 랭킹/수급 스크리닝 | 기본5분, 스크리너300초·외국인/기관600초 캐시. 다섯 원천을 병합 | 공개 Toss 유사 API는 있으나 현재 허용/정규화 경로에 없음. 모집단·단위·등록 외국인·잠정치 차이부터 확인 필요 |
| 종목 수급·가치 | 투자자 일별30분/추정10분/가치30분 캐시 | 현재 연결에 등가 계약 없음. 결측을0으로 채우거나 랭킹으로 대체하지 않음 |
| 지수·업종·야간선물·휴일 | 지수10초/업종10분/야간5분/휴일1일 캐시 등 | 시장 방어의 입력. Toss 캘린더는KRX+NXT 합집합이므로 현재 거래 세션 진실로 교체하지 않음 |
| 현재 장후 보고 가격 | 당일 아침 cohort·16시 slot | 이미 연결. 이 경로의 실제 사용은 T04에서 검증하며 장중 KIS 부하 절감으로 세지 않음 |

KIS GET은 대상 token/network/429/5xx 실패에 최대3회 시도한다. 일봉 첫 페이지 실패는빈배열, 뒤 페이지 실패는부분반환이 가능하다. 분석 캐시는요청 깊이보다 짧은 결과를 저장하지 않지만 원래 반환은 유지하며, 직접 소비자는각자 검사한다. T03에서 전체daily_prices 요청을 분석 캐시로 귀속하지 않는다. 직접 소비자 근거는`batch_analyzer.py:1982,2157`, `core_screener.py:283`, `shadow_lab.py:33`, `excess_return.py:533`, `counterfactual_tracker.py:361,371`, `kr_scheduler.py:7452`다.

WS 근거: `src/data/feeds/kis_websocket.py:115,550,718`, `src/data/feeds/quote_subscription.py:65`, `scripts/run_trader.py:964,2160`, `src/schedulers/kr_scheduler.py:5421,5447`. 이는현재소스계약이며 이번에 KIS 계정의실제구독여유나공급자현행한도를재조회한것은아니다.

코드 근거: `src/schedulers/kr_scheduler.py`(포트폴리오/체결 루프), `src/execution/broker/kis_kr.py`(각 TR), `src/data/providers/analysis_daily_cache.py`, `src/signals/screener/kr_screener.py`, `src/data/providers/kis_market_data.py`, `src/data/providers/sector_momentum.py`, `config/default.yml`. KIS 공용 리미터는10req/s이고 재시도/페이지는 논리 호출 수와 별도다.

### Toss의 시각·시장·자료 깊이·실패 계약

| 경로 | 필요한 보존 계약 | 현재 사용 범위 / 실패 동작 |
|---|---|---|
| REST prices | optional 공급자 timestamp와 로컬 수신시각 분리. timestamp 결측/미래/TTL 초과는 정상 시세로 승격하지 않음. KRW, market_basis unknown | 최대200심볼/요청. 보고 TTL≤60초 및16:05까지. 현재 설치된v3 목적은 `daily_report_current_price`,10/26~30일16:00–16:05·정확한 morning cohort. 실패/만료/늦은 결과는 기존 보고 조회로 폴백 |
| REST candles | KST 봉 날짜는 관측 신선도가 아님. 최신순 응답을 시간순 정렬, 최대200행/페이지·정확한 날짜/누락/중복·명시adjusted·시장 기준 확인. 오늘 봉은 현재 파서에서partial | 현재 helper는prices만 받음. candles는 코드 allowlist에 있지만 실행 경로·새 목적/기간·비교 계약이 필요. 뒤 페이지 실패를 완전한 시계열로 취급하지 않음 |
| REST KR calendar | KRX/NXT 합집합. 세션null과 결측 분리 | 현재 보고 helper에서 호출하지 않음. 시장 방어/휴장일 대체 대상에서 제외 |
| REST orderbook | 공개 `/api/v1/orderbook`의optional timestamp·KRW·정렬된bid/ask 가격/수량. 반환 깊이를 KIS10단계로 단정하지 않으며 KRX/KIS 실행시장 동등성은 미입증 | 공개 명세에는있지만 현재allowlist·정규화·TTL·소비자 폴백·목적/grant가없음. WS 명세는초기상태를 이REST로 조회하도록 안내하나 현observer는미구현. 현재 대체불가 |
| 공개 WS 체결·주문 이벤트 | AsyncAPI에는체결/호가/본인주문 채널이있음. 현재Toss관측구현은호가만지원 | 체결 이벤트의 엔진어댑터·TTL/장상태·폴백은미구현이며 본인Toss주문은KIS체결대체가아님. 공개채널존재를현재병행연결로표시하지않음 |
| WS orderbook:kr | KRX+NXT 통합, LOSSY·source sequence 없음, `kis_executable=false`. 공급자/수신시각 분리 | 기존 파일럿 코드는1~3종목·≤1시간·기본10k/최대50k프레임, 호가 상단 수량 저장. 초기 스냅샷/자동 재연결 없음. 종료/실패를 기록하고 완전 스트림을 주장하지 않음. v2 WS 승인과 현재v3 보고 승인은 별개 |

코드 근거: `src/data/providers/toss/{market_data,transport,client,report_protocol,report_quotes,approval,orderbook_stream}.py` 및 `src/observation/toss_report_helper.py`. 현재 설치 승인 문서는 미래 사용자 의도를 제한하는 규칙이 아니라, 현재 실행 가능한 목적·날짜의 증거다. 새 목적의 구현 여부는 구체적인 데이터 의미·부하·효과와 해당 실행 범위를 확인해 결정한다.

### T05 착수/보류 기준과 다음

1. **T03 먼저:** 같은 PID/InvocationID에서 `daily_prices` HTTP 시도/페이지와 분석 요청·적중·동시 합치기를 대조한다. 반복 쿼리가 실제 남아 있을 때만 해당 소비자를 검토한다.
2. **의미 보존:** 원주가/수정주가와 시장 기준을 일치시켜야 한다. Toss `adjusted=false`가 필요할 수 있지만 그것만으로 KRX/통합시장과 기업행사·거래량 기준의 동등성을 입증하지 못한다. KIS 반환에 오늘 행이 포함될 수 있고 momentum/ATR/섹터 계산은 마지막 행을 사용한다. 완료 표식 없이 보존된 이 행을 무조건 제외하면 전략 입력도 바뀐다. Toss 로컬 정규화의 오늘 행partial과 실제 시장 봉 완료는 구분한다. 전일 거래량만 쓰는 부분도 공유 캐시를 오염시키면 안 된다.
3. **실패 보존:** 명시 목적·기간·송신 예산 안에서만 조회하고, 불완전/잘못된 시각/권한 만료는 기존 KIS 경로로 돌아가야 한다. 취소를 숨기지 않는다. 공급자 전역 변경·진입/청산 가격 대체는 하지 않는다.
4. **결정 기한:** T03/T04 이후 후보가 없거나 효과가 작으면 T05 DEFERRED, T06에서 현재 보고 전용 유지 또는 중단을 결정하고 **E01 선정·진입 검증으로 복귀**한다. 늦어도10/30 승인 만료 때 미입증 상태를 기록하며 자동 연장/새20일 수집으로 대체하지 않는다.

이 단계의 완료는 역할 표와 대체 불가 이유의 검토 완료다. KIS 요청 절감·Toss 장중 병행·순수익 개선 완료를 뜻하지 않는다.
