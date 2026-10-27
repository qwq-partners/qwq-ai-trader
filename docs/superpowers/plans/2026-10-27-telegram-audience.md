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

**Interfaces:** 기존 quote에 `market_time`, `market_time_kind`를 부가한다. 기존 가격/점수 계산은 유지한다. `build_morning_brief` 각 입력의 `as_of`는 해당 그룹 관측일 또는 null, `valid`는 알려진 동일 날짜 근거만 인정한다.

- [ ] flat close와 timestamp 위치/길이 일치, nested regularMarketPrice/time 대응, 미래/비유한/불리언/누락 및 그룹 간 날짜 복사 결함을 먼저 실패 테스트로 재현한다.
- [ ] 가격과 맞는 명시 시각만 보존한다. nested의 정규시장 가격에 bar 시각을 임의로 붙이지 않는다. 날짜가 없거나 혼재하면 해당 그룹 근거가 유효하다고 하지 않는다.
- [ ] 관련 provider/brief 회귀를 오프라인 실행하고 diff·결과를 독립 검토한다.

### Task 2: 정기 결과 저장·수신 대상·전송 상태

**Files:** 새 `src/analytics/report_delivery.py`, `src/utils/telegram.py`, 새 `tests/test_report_delivery.py`.

**Interfaces:** `ReportStore(path=None).record(kind,text,now=None)`는 개인용 정기 자료 저장만 한다. `TelegramNotifier.record_report(kind,text)`도 같은 의미다. `publish_report(audience,kind,report_date,source_snapshot_id,text,store=None)`는 검증된 정기 대상에만 1회 전송한다. `ReportStore.claim/finish`는 SQLite 원자적 슬롯 소비와 상태를 보존한다.

- [ ] 두 저장소 연결의 같은 슬롯 경쟁, 재시작, 누락 설정, transport 예외, 서로 다른 대상 및 긴급 우회 테스트를 먼저 만든다.
- [ ] SQLite에 개인 결과와 정기 영수증을 분리 저장한다. `attempted` 뒤 crash는 자동 재시도하지 않는다. API 수락을 실제 수신으로 표현하지 않는다.
- [ ] 개인/채널 매체 설정 검사를 일관되게 하고 고정 수신자 기본값의 명시 설정 이전 절차를 기록한다. 기존 운영 기본값 제거는 실제 환경 전환과 함께 수행한다.
- [ ] 관련 검사와 독립 중요 리뷰를 수행한다.

### Task 3: 시장 본문·엔진 요약과 단일 발송 루프

**Files:** 새 `src/analytics/audience_reports.py`, 새 `src/schedulers/report_scheduler.py`, `src/schedulers/kr_scheduler.py`, `src/schedulers/us_scheduler.py`, `src/analytics/daily_report.py` 발송부, KIS 보고 메타데이터와 관련 테스트.

**Interfaces:** 시장 formatter는 허용된 시세/출처/관측시각 DTO만 받는다. `report_scheduler`는 기존 캐시와 분석 결과를 읽고 Task2 발송 계약을 사용한다. 계산 시각과 신규 발송 시각은 별개다.

- [ ] 거래일/휴장/주말, 슬롯 창, 유효 지수 1개/0개, 합성 엔진 비밀표식 비노출, 본문 길이/줄 수, 재시작/지연을 테스트한다.
- [ ] 원천 관측시각 없는 KIS 값을 현재 장중 근거로 승격하지 않는다. 숫자 결측과 진짜0을 분리한다. 공유 거래 소비자의 호환성을 확인한다.
- [ ] 기존 미국·전문가·후보 알림은 자료 생성으로 전환하고 새 루프에서 3회 시장/1회 엔진 요약을 구성한다. 휴장 시 시장은 조용히 소비하고 개인 요약은 유지한다.
- [ ] 장전 요약 평가 아카이브는 실제 발송한 본문과 연결한다. 미발송 생성본을 sent로 기록하지 않는다.
- [ ] 관련 검사와 독립 리뷰를 수행한다.

### Task 4: 보조 정기 발송 통합

**Files:** `src/core/batch_analyzer.py`, `src/core/evolution/daily_reviewer.py`, `src/schedulers/kr_scheduler.py` 보조 보고, `scripts/shadow_report.py`, `scripts/llm_migration_monitor.py`, 관련 테스트.

**Interfaces:** 정기 생산자는 Task2 `record_report`를 사용한다. 주문/체결/장애·위험/예산 전환은 기존 즉시 전송을 사용한다. 저장 성공은 발송 성공이 아니다.

- [ ] 후보0/무거래/shadow/변화 없는 hold/주간 자료는 단독 전송 없이 저장되는지, 중요 경보는 즉시 전달되는지 테스트한다.
- [ ] 정기 callsite를 명시적으로 전환하고 기존 연구 평가·저장 계약을 보존한다. cron CLI에는 기록 모드가 기본임을 표시한다.
- [ ] 외부 고정 설치본의 교체 목록·검증·롤백 순서를 문서화한다. 현재 cron 자체는 수정하지 않는다.

### Task 5: 통합 리뷰·문서·병합 인수

**Files:** 실행 원장, 보고 도메인 계약, CLAUDE.md, CHANGELOG.md, docs/README.md.

- [ ] 관련 검사, 전체 `QWQ_VERIFY_PYTHON=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python bash scripts/dev/verify.sh`, 비밀 검사.
- [ ] 독립 중요 리뷰에서 실제 차이·테스트 증거·채널 개인정보 경계·state 처리를 검토한다.
- [ ] 문서/커밋/push/PR/CI를 마친다. 구현·병합·운영 적용·자연 수신 확인을 별도로 기록한다.
- [ ] 운영 후속은 명시 설정 전환과 설치 cron 교체 후 고정 SHA 배포, 다음 자연 슬롯의 대상/횟수/API 결과 확인이다.

## Plan → Do → See 진행 기록

- Plan: 2026-10-27 사용자 진행 승인. base `00df8c4e`, 개발 worktree 사용, 운영 checkout 및 사용자 수정은 보존한다.
- Ruling: 수신자 기본값은 즉시 삭제하지 않는다. 현재 운영 환경이 의존하므로 명시 설정 전환을 실제 운영 인수 항목으로 둔다. 삭제만 하면 중요 알림을 끊는 위험이 있다.
- Ruling: 신규 보고에서 쓸 수 있는 관측일/시각은 입증된 필드만 사용한다. 현재성 부족은 표시/해석에서 제외하며 이번 변경으로 매매 freshness 정책까지 변경하지 않는다.
