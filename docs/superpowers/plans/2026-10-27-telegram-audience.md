# Telegram 수신 대상·발송 통합 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. 승인된 설계를 같은 세션에서 순서대로 구현한다.

**Goal:** 채널은 한국시장 3회, 개인은 엔진 요약 1회와 중요 즉시 알림으로 나누고 원천 날짜를 보존한다.

**Architecture:** 기존 수집·분석은 유지한다. 정기 생산자는 비공개 보고 저장소에 결과를 기록하고 전용 스케줄러가 대상별 본문을 발송한다. 시장 DTO에는 엔진 객체를 전달하지 않는다. 날짜와 전송 상태는 추정하지 않는다.

**Tech Stack:** Python, asyncio, SQLite, 기존 Telegram/KIS/Yahoo 어댑터, pytest.

**Spec:** [승인 설계](../specs/2026-10-27-telegram-audience-design.md). 상태의 정본은 [R02](../../operations/execution-roadmap.md#r02-telegram).

## Global Constraints

- 채널 07:40/13:10/16:40 KST, 개인 21:00. 토요일 주간 요약이 일일 요약을 대체한다.
- 시장 정기 보고는 한국 거래일만. 슬롯 창은 시작 후 10분, 날짜/대상/종류마다 1회 시도. 주문·체결·중요 장애는 이 제한을 적용하지 않는다.
- 장중은 당일 원천시각과 수신시각 모두 0~5분인 KOSPI 또는 KOSDAQ이 필수다. 결측은 슬롯 소비와 개인 품질 기록으로 남긴다.
- 기본 텍스트 1개, 1,800자/35줄 이하. 기준일 경고는 자료 범위로 모으며 결측을 0/오늘로 만들지 않는다.
- 후보 수집 08:25/16:00, Toss exact cohort·기간·권한과 기존 분석/저장은 유지한다. 채널에 후보·계좌·현금·킬스위치를 넣지 않는다.
- 거래 정책, 월 USD100, 운영 환경·cron·고정 설치본은 이 저장소 작업에서 바꾸지 않는다. 실제 운영 인수는 별도 상태다.
- 테스트는 외부 API/자격증명/운영 상태에 접근하지 않는다. 병렬 작성은 같은 base의 별도 worktree·파일 소유권을 사용한다.

## Review Focus

1. API 응답이 끊기거나 프로세스가 죽어도 재시작 시 정기 메시지를 반복하지 않는가.
2. 채널 설정 누락 시 개인 대상으로, 개인 설정 누락 시 채널로 폴백하지 않는가.
3. 장 마감 자료와 intraday 자료, 수신시각과 원천시각을 혼동하지 않는가.
4. 정기 보고 억제 때문에 분석·위험 감시·후보 저장이나 긴급 알림까지 중단하지 않는가.
5. 기존 설치 cron은 미갱신 상태임을 표시하며 저장소 변경만으로 실제 발송 감소를 주장하지 않는가.

### Task 1: 원천 날짜 보존과 그룹 계보

**Files:** `src/data/providers/us_market_data.py`, `src/analytics/us_report_data.py`, `src/analytics/daily_report.py`의 `build_morning_brief`만, 관련 날짜 테스트.

**Interfaces:** 보고 전용 `report_market_time`, `report_market_time_kind`를 부가하고 기존 거래 소비자의 `market_time` 계약은 유지한다. 기존 가격/점수 계산은 유지한다. `build_morning_brief` 각 입력의 `as_of`는 해당 그룹 관측일 또는 null, `valid`는 알려진 동일 날짜 근거만 인정한다.

- [x] flat close와 timestamp 위치/길이 일치, nested regularMarketPrice/time 대응, 미래/비유한/불리언/누락 및 그룹 간 날짜 복사 결함을 먼저 실패 테스트로 재현한다.
- [x] 가격과 맞는 명시 시각만 보존한다. nested의 정규시장 가격에 bar 시각을 임의로 붙이지 않는다. 날짜가 없거나 혼재하면 해당 그룹 근거가 유효하다고 하지 않는다.
- [x] 관련 provider/brief 회귀를 오프라인 실행하고 diff·결과를 독립 검토한다.

### Task 2: 정기 결과 저장·수신 대상·전송 상태

**Files:** 새 `src/analytics/report_delivery.py`, `src/utils/telegram.py`, 새 `tests/test_report_delivery.py`.

**Interfaces:** `ReportStore(path=None).record(kind,text,now=None)`는 개인용 정기 자료 저장만 한다. `TelegramNotifier.record_report(kind,text)`도 같은 의미다. `publish_report(audience,kind,report_date,source_snapshot_id,text,store=None)`는 검증된 정기 대상에만 1회 전송한다. `ReportStore.claim/finish`는 SQLite 원자적 슬롯 소비와 상태를 보존한다.

- [x] 두 저장소 연결의 같은 슬롯 경쟁, 재시작, 누락 설정, transport 예외, 서로 다른 대상 및 긴급 우회 테스트를 먼저 만든다.
- [x] SQLite에 개인 결과와 정기 영수증을 분리 저장한다. `attempted` 뒤 crash는 자동 재시도하지 않는다. API 수락을 실제 수신으로 표현하지 않는다.
- [x] 개인/채널 매체 설정 검사를 일관되게 하고 고정 수신자 기본값의 명시 설정 이전 절차를 기록한다. 기존 운영 기본값 제거는 실제 환경 전환과 함께 수행한다.
- [x] 관련 검사와 독립 중요 리뷰를 수행한다.

### Task 3: 시장 본문·엔진 요약과 단일 발송 루프

**Files:** 새 `src/analytics/audience_reports.py`, 새 `src/schedulers/report_scheduler.py`, `src/schedulers/kr_scheduler.py`, `src/schedulers/us_scheduler.py`, `src/analytics/daily_report.py` 발송부, KIS 보고 메타데이터와 관련 테스트.

**Interfaces:** 시장 formatter는 허용된 시세/출처/관측시각 DTO만 받는다. `report_scheduler`는 기존 캐시와 분석 결과를 읽고 Task2 발송 계약을 사용한다. 계산 시각과 신규 발송 시각은 별개다.

- [x] 거래일/휴장/주말, 슬롯 창, 유효 지수 1개/0개, 합성 엔진 비밀표식 비노출, 본문 길이/줄 수, 재시작/지연을 테스트한다.
- [x] 원천 관측시각 없는 KIS 값을 현재 장중 근거로 승격하지 않는다. 숫자 결측과 진짜0을 분리한다. 공유 거래 소비자의 호환성을 확인한다.
- [x] 기존 미국·전문가·후보 알림은 자료 생성으로 전환하고 새 루프에서 3회 시장/1회 엔진 요약을 구성한다. 휴장 시 시장은 조용히 소비하고 개인 요약은 유지한다.
- [x] 장전 요약 평가 아카이브는 실제 발송한 본문과 연결한다. 미발송 생성본을 sent로 기록하지 않는다.
- [x] 관련 검사와 독립 리뷰를 수행한다.

### Task 4: 보조 정기 발송 통합

**Files:** `src/core/batch_analyzer.py`, `src/core/evolution/daily_reviewer.py`, `src/schedulers/kr_scheduler.py` 보조 보고, `scripts/shadow_report.py`, `scripts/llm_migration_monitor.py`, 관련 테스트.

**Interfaces:** 정기 생산자는 Task2 `record_report`를 사용한다. 주문/체결/장애·위험/예산 전환은 기존 즉시 전송을 사용한다. 저장 성공은 발송 성공이 아니다.

- [x] 후보0/무거래/shadow/변화 없는 hold/주간 자료는 단독 전송 없이 저장되는지, 중요 경보는 즉시 전달되는지 테스트한다.
- [x] 정기 callsite를 명시적으로 전환하고 기존 연구 평가·저장 계약을 보존한다. shadow cron은 명시 `--record`, monitor는 기본 기록 모드임을 표시한다.
- [x] 외부 고정 설치본의 교체 목록·검증·롤백 순서를 문서화한다. 현재 cron 자체는 수정하지 않는다.

### Task 5: 통합 리뷰·문서·병합 인수

**Files:** 실행 원장, 보고 도메인 계약, CLAUDE.md, CHANGELOG.md, docs/README.md.

- 병합 필수 게이트: 관련 검사, 전체 `QWQ_VERIFY_PYTHON=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python bash scripts/dev/verify.sh`, 비밀 검사.
- [x] 독립 중요 리뷰에서 실제 차이·테스트 증거·채널 개인정보 경계·state 처리를 검토한다.
- 인수 절차: 문서/커밋/push/PR/CI를 마친다. 최종 CI/병합 상태는 [PR #275](https://github.com/qwq-partners/qwq-ai-trader/pull/275)의 최신 head 검사·merge 기록으로 확인한다. 구현·병합·운영 적용·자연 수신 확인을 별도로 기록한다.
- [ ] 운영 후속은 명시 설정 전환과 설치 cron 교체 후 고정 SHA 배포, 다음 자연 슬롯의 대상/횟수/API 결과 확인이다.

## Plan → Do → See 진행 기록

- Plan: 2026-10-27 사용자 진행 승인. base `00df8c4e`, 개발 worktree 사용, 운영 checkout 및 사용자 수정은 보존한다.
- Ruling: 수신자 기본값은 즉시 삭제하지 않는다. 현재 운영 환경이 의존하므로 명시 설정 전환을 실제 운영 인수 항목으로 둔다. 삭제만 하면 중요 알림을 끊는 위험이 있다.
- Ruling: 신규 보고에서 쓸 수 있는 관측일/시각은 입증된 필드만 사용한다. 현재성 부족은 표시/해석에서 제외하며 이번 변경으로 매매 freshness 정책까지 변경하지 않는다.

## 운영 인수 체크리스트 (10/27 승인, 16:10 단발 예약 설치)

1. 보호 파일/예약/운영 SHA와 미체결·복구 상태를 기존 배포 절차로 확인한다. 현재 요청에서 운영 변경을 별도로 허용하기 전에는 실행하지 않는다.
2. 현재 의미상 개인/채널 ID를 **비공개 환경**의 `TELEGRAM_CHAT_ID_ALERT`/`TELEGRAM_CHAT_ID_REPORT`에 명시한다. 서로 다른 기존 대상을 그대로 옮기며 ID·토큰을 문서/로그에 출력하지 않는다. 코드의 역사적 기본값 제거는 명시 설정 확인 뒤 별도 작은 변경으로 한다.
3. shadow 일일/주간 cron의 `--telegram`을 새 `--record`로 바꾸고, 22시 모델 비교의 저장소 밖 고정본을 검증한 SHA로 다시 설치한다. [runbook의 R02 7파일 manifest](../../operations/runbook.md#r02-fixed-report-artifact)를 따르고 실제 기록 모드의 격리 smoke를 실행한다. monitor의 새 기본 동작은 엔진 요약 입력 저장이다. shadow는 기본 출력만 하므로 두 cron 모두 명시 `--record`가 필요하다. `--telegram`은 명시 수동 요청용으로만 남긴다. 실행 시각과 분석 범위는 유지한다.
4. 이 고정 SHA를 기존 배포 절차로 적용한다. 후보/Toss08:25/16:00 생성/준비·기존 승인10/30 만료·거래/LLM 설정을 그대로 대조한다.
5. 시험 메시지 없이 다음 자연 슬롯에서 비공개 `report_delivery/reports.sqlite3`의 대상·종류·source snapshot·시도/수락/불명/생략·실제 원문을 대조한다. API 수락과 사용자의 모바일 수신은 별개다.
6. 국내 지수 원천시각 확보 전 장중 생략은 예상된 자료 결측이다. 날짜를 붙이거나 매매 조건을 바꿔 이를 통과시키지 않는다. 이미 수집하는 소스에 입증 가능한 관측시각이 있는지 별도 연결 검토가 다음 자료 과제다.
7. 롤백 시 코드·고정 cron·명시 설정 전환의 각각을 복구한다. 발송 영수증 DB는 삭제하지 않아 같은 슬롯이 다시 발송되지 않게 한다. 구버전 스케줄러는 이 DB를 이해하지 못하므로 롤백 직후 구버전 슬롯 중복 가능성을 따로 점검한다.

## 실행 중 결정·검토 범위

- Task1의 실제 모델 metadata는 미노출이며 요청 Terra/high, 독립 Sol/high다. Task2는 coordinator 구현·독립 Astra/high다. 둘 다 effective effort를 확인한 것으로 쓰지 않는다.
- KIS 숫자는 보고 전용 `report_values`로 분리했다. 기존 공유 거래 필드의0채움/원천 신선도 정책을 바꾸면 위험 판단에도 영향을 주므로 이번 보고 변경에서 조용히 확대하지 않는다.
- 기존 국내 지수 캐시는 수신 시각만 있고 원천시각이 없다. 새 장중 보고는 구현돼도 현재 입력으로는 자동 생략될 수 있다. 채널3회는 조건부 발송 슬롯이며 실제3회 도착을 보장하지 않는다.
- 보고 날짜와 집계 구간을 구분한다.21시 뒤 기록은 다음날 요약에 들어가22시 cron 자료가 사라지지 않는다.
- 신규 시장 보고는 기존 미국 요약 전문과 다르므로 장전 평가 아카이브에서 옛 생성본을 `exact_text`로 연결하지 않는다. 실제 발송문만 저장하고 기존 예측 평가 대상은 만들지 않는다.

- 통합 리뷰 반영: 코어 횡보 경보는 자동 청산 전 사용자 대응이 필요하므로 즉시 개인 알림을 유지한다. 같은 종류의 결과도 append 기록으로 전부 보존하고 요약은 건수와 최신 결과임을 명시한다.
- 발송 영수증은 접수/거절/결과 불명/설정 오류/본문 검증 실패를 구분한다. 미완료 attempted도 다음 개인 요약의 최근2일 보고 상태에 포함하며 자동 재시도하지 않는다.

- 최종 리뷰 후속: 실패/불명 영수증은 예상된 자료 보류보다 먼저 표시한다. 구형 평가행은 과거 표본수를 재작성하지 않고 `구형 N건 포함`을 명시한다. 실제 source의 heartbeat API는 메모리 전용이고 기존 supervisor가 오류를 재시작하므로 영구 종료 지적은 재현되지 않았다. 창 안/밖 실제 heartbeat 1반복 검사를 추가했다.

## 10/27 운영 인수 후속

사용자 배포 승인을 받았다. [실행 원장](../../operations/execution-roadmap.md#r02-deploy-20261027)의16:10 단발 실행·실제 결과를 따른다. 장중 재시작 제한 때문에 장외로 배정하며, 오늘 Toss 시작영수증이 소비됐다면 남은 슬롯을 훼손하지 않고 중단한다. 코드 배포가 끝나기 전에 `--record` cron을 활성화하지 않는다.

14:05 KST 단발 예약 설치 완료. 독립 중요 소스 검토READY·격리 모의 실행8건OK·실제 기록 모드1행rc0. 운영 적용/자연 수신 항목은 아직 체크하지 않는다. 실행 결과와 cron 전환의 attempted/verified 상태를 먼저 확인한다.
