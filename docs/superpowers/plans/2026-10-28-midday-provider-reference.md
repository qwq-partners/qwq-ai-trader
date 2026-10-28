# R02-INDICATIVE 장중 참고자료 구현·통합 배포

> **For agentic workers:** superpowers:subagent-driven-development, 단계별 Plan→Do→See. 작업 상태 정본은 [실행 원장](../../operations/execution-roadmap.md)이다.

**Goal:** 최근 받은 KIS 지수 응답을 정직하게 활용해 장중 보고의 정보 공백을 줄인다.
**Architecture:** 기존 cache→MarketSnapshot→render→ReportStore/Telegram 슬롯을 재사용한다. 검증된 현재 지수가 없을 때만 별도의 provider-reference 본문을 만든다. 새로운 조회/수집/DB/schema/예약은 없다.
**Tech Stack:** 기존 Python renderer/scheduler, pytest, 기존 local_deploy.
**Spec:** [검토된 R02 후속 설계](../specs/2026-10-27-telegram-audience-design.md#intraday-source-followthrough-20261028). 10/28 사용자 묶음 실행·정상 운영배포·후속 작업 승인. 기준 main `138f41b53f5d31a5c56afe630dfa828216f626f3`.

## Global Constraints

- 거래 엔진/선정/사이징/주문/청산·킬스위치·월USD100·CLI/API 설정 불변. API/LLM 추가 호출0, 임의 텔레그램 시험 발송0.
- 기존 `current_indices`의 검증 계약을 유지한다. 수신시각을 source_observed_at으로 복사하거나 다른 응답의 가격/등락률/시각을 합치지 않는다.
- 참고란은 원천시각이 None인 기존 `source='kis'`, label KOSPI/KOSDAQ 행에만 허용한다. 원천시각이 손상/오래됨/미래인 행을 참고란으로 되살리지 않는다.
- 가격 유한 양수, 등락률 유한(정상0 허용), aware 수신시각 같은 KST일·현재이하·최대300초. weekday 정규시간09:00이상15:30미만만 참고 표시; 공식 휴장은 기존 scheduler trading_day가 차단한다. 렌더러에 새 calendar/API를 호출하지 않는다.
- 검증된 지수가 있으면 기존 본문 우선. 참고란만 있을 때 상승/하락의 확정 시장 방향, 확인된 전일 종가 대비, 확정 종가를 주장하지 않는다. KIS 제공 대비율·수신 HH:MM·원천시각 미제공·전일 기준 거래일 미확인 의미를 짧게 명시한다.
- 기존3시장슬롯/개인21시·중복방지·보호 알림 유지. 시장본문≤1,800자/35줄·계좌/엔진 자료 배제. 장전/장후 기존 출력 유지.
- 실제 RR 추출 절차와 첫 판독은 PR #292에서 DONE이다. 재구현/반복 조회하지 않고 이번 배포에 도구·안내를 포함한다. 새로운 유효 입력이 생겼을 때만 실제 비교를 재개한다.

## Review Focus

1. 원천시각 결측과 손상·stale를 구분하며 잘못된 자료를 reference로 승격하지 않는가.
2. 같은 label 중 충돌 값/중복 행을 임의 우선순위로 발송하지 않는가.
3. 정확히300초/300초 초과, UTC→KST 날짜 경계, 주말/휴장/장외·미래 수신을 구분하는가.
4. reference-only + sector 본문이 기존 '확인된 지수/시장 방향' 문구를 다시 넣지 않는가.
5. scheduler/실제 fake Telegram을 거친 중복·재시작·휴장 처리와 추가 fetch0을 보존하는가.

### Task 1: 참고란과 의미 정정 구현

**Owner:** Terra/high 구현자; 별도 worker checkout. **Files:** `src/analytics/audience_reports.py`, `src/schedulers/report_scheduler.py`(보류 사유 문구만), `tests/test_audience_reports.py`, 새 `tests/test_midday_provider_reference.py`.
**Interfaces:** 기존 `render_market_report(kind, snapshot, now)`와 MarketSnapshot 불변. 새 helper는 이 renderer 내부만 사용하고 원문 객체를 변형하지 않는다.

- [x] RED: 원천시각None·정상 최근수신 KIS2행이 reference-only 본문을 만들고 strict current_indices는 여전히0행인 검사부터 작성/실패 확인.
- [x] 최소 구현: 입력 검증·label별 중복/충돌 배제, 참고란 본문/출처·자료범위 문구. verified 지수 우선, morning/after 불변. old assertion은 바뀐 reference 계약에 한정해 갱신한다.
- [x] RED/GREEN 경계: 원천시각invalid/stale/future, received missing/naive/future/stale/previousday, 잘못된source/label·bool/NaN/Inf/price0, pct0, 300초경계, UTC/KST·휴일/장외. 정상 current_indices 동작 유지.
- [x] 통합: 실제 `ReportSchedule`+임시 `ReportStore`+mocked transport를 사용해 channel/midday1건·동시/재시작 중복0·휴장0·index_fetcher0을 확인. 캐시 peek→snapshot→보고 경로도 추가 fetch 없이 연결됨을 검증한다.
- [x] 관련 검사·문법·diff·독립 리뷰를 완료하고 코드 고정. coordinator가 전체 verify/비밀 검사·문서·CI를 맡는다.

### Task 2: 문서·자료 절차·고정 SHA 배포

**Owner:** coordinator. 작업 원장/CLAUDE/CHANGELOG/docs index·설계 상태 갱신. 실제 자료 준비는 PR #292의 read-only 절차·private0행 영수증을 재사용하고 미검증 수익을 구분한다.
- [x] 계획/실행/독립리뷰/전체 검증·운영 사전조건·NEXT를 기록한 뒤 commit/push/PR 검증/병합.
- [x] 독립 중요 배포 검토(Astra/high 또는 검증된 Claude Opus), 기존 local_deploy 사용. 장외·서비스 정상·미체결0·recovery ready·helper/보고 충돌 없음·실행전 HEAD/보호파일/cron/설정 지문 확인.
- [x] 고정 main SHA 배포→기존 verify/재기동/실패자동rollback→150초 이상 정상 인수. 환경/자격/override/캐시/원장·기존 예약 보존을 대조한다. 자연 토큰 갱신은 원본 지문과 별도 근거로 대사한다.
- [x] 정상 배포 뒤 다음 READY 항목을 재대조해 순차 진행한다. 자연 입력 대기는 정확한 조건/시각으로 남기고 재검사·수집 기능을 임의로 만들지 않는다.

## Pre-flight / 결정

| 연결 | 확인/결정 |
|---|---|
| KIS peek→renderer | report_values 같은 응답값/수신시간은 이미 전달됨. provider 수정 없음 |
| strict/reference→footer/sectors | reference-only에서 확정 시장 방향/원천시각 확보 문구를 출력하지 않도록 분기 |
| renderer→scheduler | 슬롯/멱등성 불변, 기존 '원천시각 미확보' 보류 설명은 reference까지 포함한 입력 부족 의미로 정정 |
| 자료 준비→배포 | PR #292 도구/절차 포함, 기존0행 결과를 새 실측으로 다시 세지 않음 |

참고 표시 자체는 원천시각/전일 기준 결측 해결이 아니다. 이 한계를 유지하는 표시 변경을 구현하며 새 지수 API를 선택하는 작업은 이번 범위가 아니다.

## Task 1 인수

Terra/high 구현·Sol/high 독립 코드 리뷰 및 Astra/high 배포 계약 리뷰를 완료했다(실제 model/effort metadata 미노출). coordinator 관련78검사·격리0, reviewer53검사/31독립단언, 장전/장후16조합 동일. 참고란/동일 중복 두 결함의 RED를 각각 확인했고 추가 경계 검사는 GREEN 회귀로 추가한 것이다. 전체 검증/CI·운영 인수와 NEXT의 정본은 실행 원장 및 최종 PR 영수증이다.

## 배포 시간 경계

기존 봇의00:05~00:10 로그/7일 초과 지정 캐시 정리와 충돌하지 않도록, 실제 배포 시작은00:15 이후 사전검사에서 고정한다. 준비 가능한 외곽 창은10/29 06:20까지지만07시 분석과 겹칠 정도로 지연되면 강행하지 않는다. 기존 정리/토큰 자연 갱신이 보호 지문을 바꾸면 원래 차이를 보존하고 별도 근거로 대사한다.

로컬 전체 verify:9,647passed/2skipped/2xfailed/8warnings·783.54초·rc0·격리0·문법/비밀PASS. 최종 문서 독립 리뷰도APPROVE/P0~P2 0이며11개 링크/앵커 정상. CI/병합·실제 배포는 최종 PR 완료 영수증을 따른다.

R02 전체 단계 완료: PR #293 고정9a193331·10/29 00:28:45 기동,00:31:35/169.6초/20항목PASS·보호48파일/cron 보존. [운영 영수증](https://github.com/qwq-partners/qwq-ai-trader/pull/293#issuecomment-6473256376). 다음 도래 기록 판독을 마쳤고 E04-NEWS-DEDUP의 재현된 결함으로 이어갔다. 새13:10 자연보고/모바일은 별도WAIT다.
