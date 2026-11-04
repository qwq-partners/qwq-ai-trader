# E04 Bear timeout evidence — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** 합성으로 취소 메타데이터 전달 공백을 재현하고 진단 영수증에 필요한 최소 보조 상태만 보존한다.

**Architecture:** 기존 진단의 예외 경계에서 인식 가능한 CancelledError 또는 TimeoutError의 직접 CancelledError 원인에 첨부된 LLMResponse만 검사한다. 기존 판정/과금 필드를 덮어쓰지 않고 선택적인 `timeout_observation`에 안전한 전송 상태만 투영한다. 전달 정보가 없거나 문맥/형식이 맞지 않으면 기존 unknown 영수증을 유지한다.

**Tech Stack:** Python asyncio, 기존 단일 시도 가드/Claude 어댑터/LLMResponse, pytest 합성 HTTP·임시 SQLite.

**Spec:** [직전 승인된 후속 범위](../../reviews/e04-bear-completion-review-2026-11-05.md#next-e04-bear-timeout-evidence). 사용자는 본 단위의 합성 확인·최소 수정·WSL 검증·커밋/푸시를 명시 승인했다. 반복 설계 승인이나 이전 검증 재실행 없이 이 범위를 진행한다.

## Global Constraints

- 모델 claude-opus-5-5·low·기존 입력/system·시한5.75초·400→800·시도2/재시도0·실험USD0.50·완료/중단 조건은 변경하지 않는다.
- 원장/가드가 정산 소유자다. 첨부 상태는 성공·verdict·settlement_known·비용 확정·unknown 예약 해제·800 진행·재시도를 바꾸지 않는다.
- 추가 실호출·원장 읽기/쓰기·운영 자격·배포/재시작·주문·설정/KILL·WSL 사용자 키/호스트키 변경 없음. 개발 검사는 합성 전송·임시 원장만 사용한다.
- 과거 영수증을 수정/보충하지 않는다. no attachment는 unknown이지 미전송 증거가 아니다. 클라이언트 request_sent 플래그는 공급자 수신 증거가 아니다.
- 원응답/content·prompt·예외 전문·숨은 추론·임의 속성·비용/토큰 추정값을 새 보조 객체에 넣지 않는다.
- source 범위는 src/analytics/bear_api_cap_diagnostic.py와 해당 테스트뿐이다. transport/guard/adapter/manager의 실행·정산 동작을 변경하지 않는다.
- 기존 완료 검증 반복 없음. 새 소스 관련 검사 후 최종 실제 WSL verify.sh 직렬 전체1회(리뷰 수정이 생기면 최종 SHA 기준), AWS 전체 대체 없음. WSL 통과 전 새 소스 push 금지.

## Review Focus

- 외부 TimeoutError의 직접 CancelledError 원인 vs 직접 취소 vs 내부 API timeout 반환: 기존 전파/중단 의미 유지.
- 정상 LLMResponse가 아닌 객체·잘못된 provider/요청 모델/전송/채널·bool 아닌 상태: 보조 정보 거절/미확인, 원문 유출 없음.
- 임의 예외의 첨부·깊은/cyclic exception chain: 일반 오류는 추출하지 않으며 직접 원인까지만 확인.
- 성공처럼 보이는 첨부·usage/cost 조작: receipt outcome=error/verdict=None/settlement_known=False·unknown 예약·호출1회 유지.
- 외부 timeout 뒤 정리: 소비표식·가드 슬롯·클라이언트 종료·실패 영수증 유지, 이후800 호출 없음.

## Task 1: 합성 재현과 최소 보조 상태

**Files:** Modify src/analytics/bear_api_cap_diagnostic.py; Create tests/test_bear_api_cap_timeout_evidence.py (기존 진단 합성 helper 재사용 가능).

**Interfaces:** private `_timeout_observation(error)` → allowlisted dict 또는 None. 기존 `_evidence` 예외 분기에서 선택적 `timeout_observation` 키만 추가하며 정상 결과 경로는 동일하다.

- [x] 먼저 실제 run_diagnostic→guard→adapter→가짜 전송에서 첨부 있음/없음의 직접 취소와 외부 timeout을 재현한다. 기존 코드에서 상태 소실로 실패하는 RED를 기록한다. API 내부 timeout이 정상 LLMResponse로 반환되는 경로는 기존 의미를 보존한다.
- [x] 최소 투영: 정확한 LLMResponse와 Anthropic/요청 claude-opus-5-5/anthropic_messages/api 문맥을 검증하고 request_sent(bool 또는 null), http_status(100..599 정수 또는 null), transport_error_kind(기존 허용 enum), refusal_detected(bool 또는 null)만 보조 객체에 허용한다. source 표시는 고정 문자열만 허용한다. 실제 모델 신원·usage/billing·토큰·비용은 보조 객체로 승격하지 않는다. request_sent=False와 HTTP 응답 같은 모순은 보조 정보를 거절한다. 인식 불가능/문맥 불일치/필드 비정상은 기존 unknown을 보존하며 원래 예외를 가리지 않는다. 깊은 chain 순회 없음.
- [x] 합성 수락: 직접/외부 취소 모두 상태 보존, no attachment/임의객체/문맥변조/임의예외·깊은원인·cycle은 정보미확인, status bool/비정상 문자열 차단, sentinel content/error/추론이 영수증에 없음. 결과와 ledger/가드·호출수/재시도/close·두 소비표식이 전후 동일함을 확인한다. 과거 원장/영수증은 실제 접근하지 않는다.
- [x] 새 파일+관련 tests/test_bear_api_cap_diagnostic.py tests/test_model_comparison_claude.py tests/test_claude_transport.py를 명시 실행한다. 새 코드 문법/diff 검사 뒤 커밋하고 report에 RED/GREEN 명령·rc·수치·잔여를 적는다. push는 coordinator가 WSL 뒤 수행한다.

## Task 2: 독립 리뷰·문서·WSL·통합

Coordinator가 diff/합성 증거를 확인하고 저자와 별도인 Astra/xhigh로 task+최종 범위 리뷰를 진행한다. 같은 단일 source task에 중복 리뷰는 만들지 않는다. 지적은 원인 재현·최소 보완·해당 부분 재리뷰로 처리한다. 실행 원장·기존 진단 문서·CHANGELOG·CLAUDE/README에 실제 범위와 NEXT를 반영한다.

기존 WSL 호스트 핀을 대조하고 임시 인증 등록 후 고정 SHA bundle을 별도 작업본에 전달한다. `QWQ_VERIFY_PYTHON=/home/user/projects/qwq-ai-trader/venv/bin/python bash scripts/dev/verify.sh` 직렬 전체·전후tree·로그hash·원본상태를 확인한다. 완료된 과거 결과를 재실행하지 않는다. 통과 뒤 새 feature commit/push/PR·필수CI, 이번 WSL 인증만 정리. 운영 배포·실호출은 없다. WSL 접근이 막히면 구현·리뷰·번들까지 준비하고 WSL 의존 단계만 기다린다.

## 검증 기록

소스2d6d7483(작성자60a8364c) 관련218검사(신규42)·격리0. 고정5729b489 독립 Astra/xhigh 리뷰 P0/P1/P2 0·Spec/Quality PASS(실제 모델/실효 effort 미노출). 같은 SHA 실제 WSL 직렬 전체10,526 passed/2 skipped/2 xfailed·rc0·394.61초·격리0·문법/비밀검사 PASS, 전후tree648ee21a·원본/작업본 clean·로그hash 확인. 이후 추가는 결과 문서뿐이며 PR 필수CI·작업용 인증 정리는 원장에 연결된 비공개 영수증으로 마감한다. 운영 배포·실호출 없음.
