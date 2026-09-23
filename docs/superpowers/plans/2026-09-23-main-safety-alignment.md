# Main Safety Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. 사용자가 지정한 병렬 구현·단일 coordinator 통합을 우선한다.

**Goal:** main 안전 동작과 미설치 owner 엔진의 경계를 함께 보존하고 수동 복구 진단 절차를 고정한다.

**Architecture:** main 계약을 먼저 시험으로 들여오고, KIS 응답/lease와 legacy/attached 경계를
순서대로 통합한다. 진단은 변경 권한 없는 증거 분류·운영 절차이며 설치 허가와 분리한다.

**Tech Stack:** Python3.12, pytest, asyncio, 기존 KIS adapter와 SQLite owner.

**Spec:** `docs/architecture/main-safety-alignment-2026-09-23.md`

## Global Constraints

- 기준 engine78a253b / mainafa6e1e / 관측후보3cfcdca. 변경된 후보에 역사 GREEN을 재사용하지 않는다.
- 운영 주문·설정·배포·재시작·실 API0. readiness=False·미지원 MODIFY·공식 증거 미충족 차단 유지.
- 계좌/자격/실원장 파일은 복사·커밋·리뷰 전송하지 않는다. 전체 suite는 coordinator만 직렬 실행.

## Review Focus

- attached heartbeat에 남은 legacy keep 장부가 있어도 legacy 송신/변경0 — Task2 실제 heartbeat 시험.
- 늦은 원장 응답이 새 lease를 해제하지 않음 — Task2 기존 lease 시험 유지.
- 빈 tr_cont가 legacy와 execution 수집기에서 다른 계약임 — Task2 양쪽 실제 가짜 HTTP 인수.
- 취소0/None/빈 목록을 같은 증거로 취급하지 않음 — Task2 main BUY/SELL 생존 시험 유지.
- 진단이 호출 중 증거를 고치거나 readiness를 열지 않음 — Task3 금지 함수/버전 절차를 명시.

## Task1: Main 계약 RED 고정

Files: main의 `tests/test_kis_tr_switch.py`, `test_kis_pagination_protocol.py`,
`test_engine_stale_pending_fixes.py`, `test_exit_exempt_sell_guard.py`,
`test_stale_sell_cancel_failure.py`; 결과 `docs/reviews/main-safety-contracts-red-2026-09-23.md`.

- [x] main afa6e1e의5파일을 같은 내용으로 격리 test-only worktree에 추가한다.
  `git show afa6e1e:tests/test_kis_tr_switch.py`로 읽고 apply_patch로 추가한다.
- [x] 기존 legacy stale26·session5를 실행해 기준을 확인한다.
- [x] 새5파일 실행. 수집 오류면 `--continue-on-collection-errors`로 가능한 행동도 측정한다.
  `pytest tests/test_kis_tr_switch.py tests/test_kis_pagination_protocol.py tests/test_engine_stale_pending_fixes.py tests/test_exit_exempt_sell_guard.py tests/test_stale_sell_cancel_failure.py -q -p no:cacheprovider --tb=short`
  Expected: feature의 부족한 main 계약이 RED. 수집 오류와 행동 실패를 별개로 기록한다.
- [x] assertion/xfail 무변경·main blob 동일성을 검증하고 RED임을 명시해 test-only commit한다.

## Task2: 수동 통합과 행동 보존

Files: `src/core/engine.py`, `src/execution/broker/kis_kr.py`, `src/utils/kis_rate_limit.py`,
자동 병합 대상 `src/core/batch_analyzer.py`, `src/schedulers/kr_scheduler.py`.

- [x] coordinator 전용 통합 worktree에서 검토된 관측 후보를 no-commit merge한다.
  문서 충돌은 양쪽 이력을 보존한다. `_attached_gateway`와 `_SellKeep`는 모두 필요하다.
- [x] KIS는 QueryResponse/result wrapper와 획득별 lease를 보존하면서 main TR/페이지 및
  관측 래퍼를 결합한다. 실제 첫 페이지/다음 페이지 헤더 시험을 먼저 읽는다.
- [x] attached heartbeat 회귀를 RED로 추가한다. 실제 engine/owner fixture에 legacy
  keep 장부를 심고 heartbeat를 호출해 broker·gateway·pending 변화가0인지 확인한다.
  최소 제품 경계는 `if getattr(self.engine, '_execution_runtime', None) is not None: return`.
- [x] closing auction 취소 금지/면제 재송신 금지2개 구 기대를 교체한다. cancel0 BUY는
  tracked-live/undecidable/vanished/구 호환 fake로 분리한다. 나머지 기대는 근거 없이 바꾸지 않는다.
- [x] Task1 main5파일 + KIS execution query/integration/evidence + owner/gateway/P1/lease/install
  시험을 실행한다. Expected: main 행동과 attached writer/sender 격리 모두 GREEN.
- [x] diff·시험·26개 처분표를 검토하고 통합 커밋한다. 미해결이면 merge 완료로 보고하지 않는다.

## Task3: 수동 복구 진단 절차

File: `docs/operations/protection-recovery-diagnostics.md`.

- [x] 모드(legacy/attached)·code SHA·owner/published/engine version·health·미적용 inbox를
  읽는 단계와 증거 없는 판단을 명확히 분리한다. legacy에 owner가 없으면 N/A로 표기한다.
- [x] 원장/감사/의도/예약/입력 source/RAM 연결의 분류표와 필요한 추가 증거를 작성한다.
- [x] `_audit_recovery` 등 변경 함수를 읽기 전용으로 부르지 않는지 코드와 대조한다.
- [x] 두 안정 스냅샷과 별도 승인된 브로커 증거 없이는 어떠한 수동 변경도 제시하지 않는다.
  도구가 아직 배선되지 않은 항목은 미지원으로 표시하고 운영 실행 명령을 발명하지 않는다.
- [x] 독립 리뷰에 절차의 파괴·권한 확대·증거 추정 가능성을 함께 검토시킨다.

## Task4: See

- [x] native critical + tools0 Opus 교차 리뷰. 실제 모델·비용·차단·범위를 기록한다.
- [x] 모든 worker 종료 후 env-i 격리에서 UTC→KST 전체 `pytest tests -q -p no:cacheprovider --tb=short`.
  PATH=/usr/bin:/bin, LANG=C.UTF-8, PYTHONDONTWRITEBYTECODE=1,
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key 사용.
- [x] 문법·비밀패턴·git diff --check. 전체 통과 후 feature 문서·커밋/푸시.
- [x] main/운영 전환과 C/F/G/R 미완을 별도로 보고한다. 진단 구현/배포를 과장하지 않는다.
