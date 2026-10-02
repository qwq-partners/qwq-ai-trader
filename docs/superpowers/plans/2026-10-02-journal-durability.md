# 체결 identity·거래 장부 commit — 37차 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. 단계별 RED→GREEN과 독립 최종 리뷰를 수행한다.

**Goal:** 미래 KR 체결의 장부 누락/중복을 막고 실제 DB commit 확인을 후처리 완료와 연결한다.
**Architecture:** 검증된 Fill identity → JSON 멱등 기록 → 실행별 PostgreSQL batch → 비차단 receipt → Scheduler 완료 확인. 원장 v1과 기존 비식별 호출 호환 유지.
**Tech Stack:** Python dataclass/Decimal/asyncio, asyncpg, 임시 PostgreSQL16, pytest.
**Spec:** [37차 설계](../specs/2026-10-02-journal-durability-design.md).

## Global Constraints

- 기준 SHA `0552f7648c64de63d91fe93e508ccdc3bd42ce71`, 별도 feature worktree, 파일 소유권 분리.
- 운영 API/계좌/설정/.env/log/cache/state/SSH/service/주문/배포/재시작 금지. bare pytest 금지, 명시 tests/만 실행.
- 로컬 임시 PostgreSQL은 독립 data/socket 경로와 TCP 비활성화, 설치/기존서비스 조작 없이 직접 소유한 프로세스만 종료.
- 스키마 이전은 코드/임시 DB에서만 검증. 운영 migration을 수행하지 않는다.

## Review Focus

1. 부분 BUY 두 번째 이후 행 누락과 같은 execution의 JSON/DB 중복 누계.
2. 이전 SELL 재시도로 최신 요약을 되돌리거나 실패한 앞 batch를 건너뛰는 경로.
3. COMMIT 응답 유실/DB 부재/예외를 성공으로 표시하는 경로.
4. DB 확인 대기로 청산 상태 적용이 지연되거나 재시도에서 후처리를 두 번 수행하는 경로.
5. 종목만의 SELL 귀속 추정, 오래된 schema/JSON 호환, 격리·종료 실패.

## Task 1 — 저장 계층

Owner: 별도 worker, Astra/high 요청(돈 경로의 상태/내구성),25분 제한·추가 fanout 금지. 소유 파일: `src/core/evolution/trade_journal.py`, `src/data/storage/trade_storage.py`, 새 `src/data/storage/execution_journal.py`, 전용 `tests/test_execution_journal.py`, `tests/test_trade_storage_execution.py`, `tests/test_trade_storage_execution_postgres.py`. 조정자가 동일 base worktree를 만든 뒤 배정한다.

- [x] RED: JSON 재호출/충돌·부분 BUY와 전체 batch/미확정 receipt 시험.
- [x] 설계의 타입/API·identity signature·transaction/receipt·migration/종료를 구현.
- [x] 실제 임시 PostgreSQL의 기존/new schema, rollback/중복/충돌/commit ambiguity 시험 및 기존 장부 회귀.

## Task 2 — Fill와 Scheduler

Owner: 조정자. `src/core/types.py`, `src/core/engine.py`(중복 내용 검사에 추가 identity 포함), `src/core/event.py`, `src/execution/execution_history.py`, `src/execution/broker/kis_kr.py`, `src/schedulers/kr_scheduler.py`, 새 `tests/test_journal_commit_handoff.py`와 필요한 기존 회귀만.

- [x] RED: 실제 합성 KIS 관측의 scope/date/ODNO 전달, 모든 BUY 증분, DB pending 이후 후처리 재실행0·다음 보호 체결 진행·commit 후 ack.
- [x] Fill metadata와 식별 KR journal 인자 연결. SELL trade ID 없으면 추정 direct SQL을 차단하고 근거 부족 유지.
- [x] `_drain_fill_handoffs`의 후처리 수행/저장 대기/완료 상태를 나누고 실패를 broker 실행 원장 fault로 연결. 보호 SELL 유지.
- [x] 관련 통합 시험 후 storage worker diff를 직접 검토/통합.

## Task 3 — See와 인계

- [x] 구현 비참여 Astra/xhigh 독립 리뷰, 실제 임시 DB 증거 포함 관련/전체 tests·문법·비밀정보·격리0.
- [x] CLAUDE/CHANGELOG/아키텍처/운영·모니터링·37차 통합 PDS에 코드와 프로세스/한계/다음 순서 갱신.
- [ ] 동일 제품 hash와 PR head CI 확인 후 통합. 실제 순수익·과거 복구·운영 적용 완료로 표현하지 않음.

이 체크리스트는 로컬 커밋 시점 기준이다. 최종 제품9파일 검토·검증 수치와 제한은 [37차 See](../../research/current-engine-integrated-pds-2026-10-02.md#37차-see--검증과-인계), 이후 CI/병합 결과는 해당 PR의 GitHub 기록을 정본으로 확인한다.
