# Codex 독립 리뷰 후속 수정 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. 사용자 지정 병렬 구현은 격리 워크트리·서로 다른 파일로 수행한다.

**Goal:** 독립 리뷰에서 재현한 신규 P2 2건과 기존 시간 입력 계약 P2 2건을 수정하고, 검증·독립 리뷰·보호 PR 후 장외 배포한다.

**Architecture:** CF의 측정값과 평가 참여 상태를 분리한다. DART는 해석 가능한 목록만 정상 획득으로 처리한다. replay는 관측·만료 시각을 같은 순간 기준으로 비교한다.

**Tech Stack:** Python, pytest, 기존 JSON 원장, GitHub verify, systemd 배포 스크립트.

**Spec:** 사용자 승인된 Codex 독립 리뷰의 네 재현 사례(아래 작업별 계약). 기준 main `4b70a34`; 이미 머지된 PR #60은 보존한다.

## Global Constraints

- 주문·설정·킬스위치 변경 없음. 위험 사이징/팀 정책 승격·canary 시작 없음.
- 운영 원장·과거 연구 결과 재작성 없음. 외부 API·운영 자격증명 없는 오프라인 테스트만.
- 각 작업은 테스트 RED → 최소 수정 → GREEN → 커밋. 최종 리뷰어는 구현자와 분리한다.
- root 운영 checkout은 배포 트랜잭션 전 변경하지 않는다. main 직접 push·보호 우회 금지.
- 공용 문서 변경은 통합 담당만 수행한다. 작업 간 공유 소스 파일 없음.

### Task 1: CF 판정 불가 시 기존 측정 보존

**Files:** `src/analytics/counterfactual_tracker.py`, 직접 소비 경로가 있으면 최소 필터 변경, `tests/test_codex_evaluation_sources.py`, 신규 `tests/test_codex_cf_preservation.py`.

**Interfaces:** 기존 JSON 행/entry_px/r1/r5/r20/xN/deliberation_ids 유지. 체결 판정 `None`은 평가 제외 상태이고 `False`는 미체결 확인, `True`는 체결 확인이다. 다른 작업의 소스와 교차하지 않는다.

- [x] RED: 기존 BUY(entry_px=100,r5=-10,r20=-20,x5=-12)가 저널 읽기 실패 → 저장/재로드 → 미체결 확인을 거쳐도 값·ID가 그대로인지 테스트. 현 코드는 삭제 후 45봉 밖 날짜를 다른 가격(200)으로 재측정한다.
- [x] RED: 판정 불가 행은 모든 요약/성과 분모와 가격 updater에서 제외. unknown→false/true, 새 unknown, 오전 BUY→오후 HOLD를 포함한다.
- [x] 수정: 원본 측정값을 보존하며 제외 상태만 저장하고, false 복구 시 상태만 해제한다. 체결 확정 행은 미체결 표본에서 제거한다. 정상 레거시 행 호환. 정확한 표본일 봉이 없으면 다른 날로 가격을 대체하지 않는다.
- [x] GREEN: 관련 CF/평가 테스트 실행, 결과 및 소비 경로 점검을 report에 기록하고 소스·테스트만 커밋한다.

### Task 2: DART 목록 스키마 획득 검증

**Files:** `src/signals/fundamentals/dart_checker.py`, 신규 `tests/test_codex_dart_schema.py`.

**Interfaces:** `DartCheckResult` 기존 소비 계약 유지; 실제 중립/0건은 fetched=True, 파싱 불가능 목록은 fetched=False. 위험 사실은 부분 실패 때문에 지우지 않는다.

- [x] RED: HTTP200/status000에서 report_nm 누락·빈 문자열·공백·비문자/비dict행/잘못된 list를 정상 획득으로 보지 않는 테스트. 정상 중립/빈 목록/status013/위험·호재도 대조한다.
- [x] 수정: 해석 가능한 제목·목록 모양을 확인하고 malformed 입력을 예외 없이 실패 상태로 반환. 혼합 목록은 알려진 위험을 유지하되 획득 완전성을 과장하지 않는다. 실패 결과가 정상 캐시에 들어가지 않는지 확인한다.
- [x] GREEN: StockValidator→FundamentalAnalyst→판단 v2에서 malformed 응답이 risk_clear/매수 매력을 만드는 경로가 없는지 합성 테스트하고 관련 테스트 실행 후 커밋한다.

### Task 3: replay 만료·보고서 관측 시각 계약

**Files:** `scripts/team_policy_ab.py`, 필요하면 `src/execution/entry_plan.py`, 신규 `tests/test_codex_replay_time_contract.py`.

**Interfaces:** 명시 replay now의 naive=KST, aware는 실제 순간. now 생략 시 기존 host-local live 호환은 보존한다. top-level observed_at도 nested evidence와 동일한 미래 입력 배제 계약이다.

- [x] RED: 2026-08-04 15:30 naive KST / +09:00 / 06:30+00:00 만료가 같은 bar에서 동일 체결; 현재 aware는 CHECKER_ERROR가 no_fill_in_window로 숨겨진다.
- [x] RED: 결정10:00/data_as_of09:50/top observed_at10:01/nested None는 B/C 평가에서 제외. 정상 별도 보고서 보존, 정확한 경계·None·malformed 명시값·UTC/KST 혼합 포함.
- [x] 수정: 만료/시세 시각 비교 정규화, checker 오류와 일반 미체결 사유 구분. top-level 미래·무효 명시 관측 보고서 제외. 임계값·실주문 정책 무변경.
- [x] GREEN: UTC/KST 각각 관련 테스트, 기존 host-clock 호환 테스트 실행 후 소스·테스트 커밋.

## 통합·운영 인수

- [ ] 작업별 독립 Codex 리뷰 → 필요한 수정의 한정 재리뷰 → 전체 브랜치 최종 독립 리뷰.
- [x] 관련 기술 문서·CHANGELOG·CLAUDE 현재 상태·docs/README·MEMORY(교훈만) 갱신.
- [x] UTC/KST 전체 verify, 격리 위반 0, 문법·비밀정보 검사·git diff --check.
- [ ] feature push → PR verify → protected main merge.
- [ ] 직전 장외/pending0/청결/설정·킬스위치 지문 확인 후 local_deploy.sh 병합SHA. 재시작 후 초기 health 및 150초 이상·장외 동기화 300초 경과 점검.
- [ ] 실제 배포 SHA·시각·테스트·운영 상태·잔여 한계를 기록한다. 다음 장중/저녁 스케줄은 미관측으로 명시한다.
