# 주문·체결 영구 기록 및 재시작 경계 — 35차 Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. 조정자가 통합하며 작성자와 최종 중요 변경 검토자를 분리한다.

**Goal:** 프로세스가 종료되어도 접수·관측 체결·메모리 적용·후처리 반환을 구분해 남기고, 불명확한 과거 체결을 새 잔고에 재적용하지 않는다.

**Architecture:** 계좌/환경 범위의 SQLite 원장에 주문 의도를 POST 전에, 증분 체결을 큐 인계 전에 commit한다. 각 실행에 session 시작/정상 종료 표식을 남긴다. 재시작은 미완결 주문 또는 비정상 종료/기록 손상을 별도 recovery hold로 취급한다. broker에서 불러온 잔고는 과거 execution replay 허가가 아니다.

**Tech Stack:** Python asyncio.to_thread, sqlite3 트랜잭션/FULL synchronous, Decimal, 기존 KIS/Engine/Scheduler.

**Spec:** 본 문서의 아래 설계 계약. 기준 main `6126c9160b67fd9987aee307c89fbb671d5594f7`.

## 설계 계약 / Global Constraints

- 실거래/운영 API·자격증명·운영 상태 읽기·SSH·배포·재시작·설정 변경은 범위 밖이다. 합성 입력과 임시 SQLite만 검증한다. bare pytest 금지, 명시 tests/만 실행한다.
- 원장에는 account scope 해시만 사용하며 원문 계좌번호·키·토큰·원본 API 응답은 저장하지 않는다. 허용된 주문 사실과 Decimal 문자열만 저장한다.
- local order id는 세션 UUID와 결합한다. execution id는 주문 key와 누적 체결 경계로 결정하며 동일 id의 다른 내용은 오류다.
- receipt는 `portfolio_applied`, `handoff_returned`로 구분한다. 후자는 기존 저널 DB 큐/JSON 쓰기 성공의 내구성 증명이 아니다.
- SQLite schema/scope/레코드 형식/수량·금액 회귀/중복 충돌/쓰기 실패는 성공으로 변환하지 않는다. I/O fault는 현재 실행에서 자동 해제하지 않는다.
- 이전 미완결 주문·비정상 종료 session·손상 원장은 BUY 전체와 관련 종목 partial SELL을 보류한다. full protective SELL 및 취소/조회는 허용한다. 기록 불가 때 full SELL은 현재 session 시작이 이미 저장된 경우에만 허용하며 비정상 session으로 남는다. 시작 표식 자체가 없거나 종료 중이면 새 주문 전부 보류한다.
- 비정상 종료 session에는 미기록 보호 SELL 가능성이 있으므로 빈 주문 목록이나 날짜 변경으로 해제하지 않는다. 정상 종료는 미완결 주문/미적용·후처리 체결/IO fault가 없어야 가능하다. 과거 Fill 자동 재생과 수동 해제 도구는 이번에 제공하지 않는다.
- 운영에 첫 설치하기 전의 주문은 이 새 원장으로 소급 증명할 수 없다. 완전한 계좌 대사·정정 계보·broker 잔고 포함 시각·과거 장부 자동 복구는 별도 미완료다.

## Review Focus

1. POST 직전 crash, accept 뒤 저장 실패, 체결 적용 뒤 receipt 전 crash: 새 프로세스는 미확정 상태를 정상/거절로 바꾸지 않는다.
2. 같은 execution 중복 receipt는 수량을 두 번 증가시키지 않고, 잘못된 누적량/가격/세션 소유권은 거부한다.
3. hashkey/rate-limit/token await 중 hold 발생: 마지막 POST에서도 BUY/partial SELL 차단, full SELL 유지.
4. 정상 처리 뒤 graceful disconnect만 clean session이 되며, 실패/진행 중 종료는 다음 시작에 보존한다.
5. 새 잔고 초기화 뒤 과거 체결은 큐로 내보내지 않는다. 파일 부재(최초 설치)와 손상/다른 scope는 구분한다.

## Task 1 — durable store (독립 writer, Astra/high 요청)

**Files:** `src/execution/execution_ledger.py`, `tests/test_execution_ledger.py`.
**Interfaces:** `ExecutionLedger(path, scope)`; async `open(session_id)->snapshot`, `intent(key, facts)`, `accepted(key, odno, orgno)`, `outcome(key,status)`, `observe(key,cumulative_quantity,cumulative_average,terminal=False)->execution_id|None`, `receipt(execution_id,stage)`, `close_session()`, `snapshot()`. 상세 JSON 계약은 구현 인계와 최종 코드의 docstring으로 고정한다.
- [x] 임시 파일 재개/중복/수량·가격 충돌/receipt 순서/강제 종료/rollback/계좌 범위·스키마·손상 테스트를 먼저 실패시킨다.
- [x] 모든 mutation을 직렬화된 thread 내 트랜잭션으로 commit하고 독립 임시 DB roundtrip 검증을 통과한다.

## Task 2 — broker와 체결 인계 (조정자)

**Files:** `src/execution/broker/kis_kr.py`, `src/core/types.py`, `src/schedulers/kr_scheduler.py`, 관련 새 통합 tests.
- [x] 합성 broker로 intent→접수→증분→적용→후처리→정상 종료→재개 및 각 실패 경계를 먼저 재현한다.
- [x] 생성 시 원장 객체를 만들고 첫 connect/submit/fill 전에 open한다. scope 분리 경로는 기존 주문 불명 장부와 같은 부모 아래이며 시험은 임시 경로를 주입한다.
- [x] BUY/partial SELL 별도 gate와 최종 POST 재검사를 연결한다. Fill에 execution_id를 추가하고 applied/returned 순서로 기록한다. 이전 session 주문은 메모리 pending이나 Fill로 복원하지 않는다.
- [x] graceful disconnect의 clean 가능 여부를 검사하고 recovery 상태는 명시 보고 메서드로 노출한다.

## Task 3 — See 및 통합

- [x] 독립 Astra/xhigh 요청 최종 리뷰; 미해결 지적 수정 후 관련 tests 재실행.
- [x] 전체 `scripts/dev/verify.sh`(명시 tests/)·문법·비밀정보·격리0 확인, 문서/흐름/한계 갱신.
- [ ] 검토된 소스 diff만 PR 생성·동일 head CI 확인·통합. 이 마지막 게이트의 실제 완료는 GitHub PR 기록을 정본으로 확인한다. 운영/예약5468208/매수 중지는 변경하지 않는다.

## 다음 수익 작업과의 관계

이 단계는 재시작에서 손익/잔고가 왜곡되는 경로를 막는 기반이다. 순수익 향상을 증명하지 않는다. 예정 후보 관측의 품질 점검 → 같은 후보 비용 후 진입 비교 → 같은 진입 청산 대조 → 독립 체결/현금흐름 계좌 대사 및 전체 KODEX200/반도체 제외 병행 비교 순서를 유지한다.

## See 기록

신규42+27개·전체3531 passed/2xfailed/기존경고1·격리0·문법/비밀정보 통과. 독립 검토의 P1 두 건/P2 한 건은 회귀로 수정했고 최종 승인 소스 지문은 통합 PDS35차에 기록했다. 과거 자동 복구/수동 해제/DB 내구성/운영 적용은 완료 항목에 포함하지 않는다.
