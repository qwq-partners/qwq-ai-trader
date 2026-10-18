# 주문 귀속 배포와 월요일 관측 교체 계획

> **For agentic workers:** Use superpowers:subagent-driven-development for the independently owned implementation tasks. One coordinator integrates and performs the authorized operational actions.

**Goal:** 새 주문 귀속 기능을 배포하고, 과거 동작의 호환 복구 버전과10월19일 관측을 함께 보존한다.

**Architecture:** 기존 `local_deploy.sh`를 두 단계로 사용한다. 먼저 과거 동작에 새 원장 읽기만 추가한 버전을 배포해 자동 롤백 기준을 확보하고, 이후 검증된 최종 기능을 배포한다. 기존 v4 예약의 파일은 보존하고 새 소스/설정/PID에 묶인 v5 입력을 같은 시간창으로 설치한다.

**Tech Stack:** Python, SQLite, pytest, GitHub PR/Verify, 기존 systemd 단발 관측 설치기.

**Spec:** 사용자 “ㄱㄱ 하고 완료되면 배포까지”; 선행 [주문 귀속 연결](https://github.com/qwq-partners/qwq-ai-trader/blob/ea5d54cd91a98322c13b781228b255da9cb5f7f8/docs/reviews/order-attribution-2026-10-18.md)의 복구 호환 조건.

## Global Constraints

- 작업 기준 `068f94bd`와 운영 `a0bc9c94`를 구분한다. 변경 없는 기능은 기존 검증 소스를 그대로 사용한다.
- 개발 작업자는 별도 worktree와 같은 기준 SHA를 사용하고 운영·자격증명·외부 API에 접근하지 않는다.
- 운영 실행은 coordinator만 한다. 매수 중지·월USD100·미확인 비용 정책·수동 설정·과거 원장을 보존한다.
- 주문을 실행하거나 신호/사이징 정책을 새로 완화하지 않는다. PR #218의 검증된 오류 수정과 #219의 귀속 기록만 적용한다.
- 기존 v1~v4 입력/백업/미소비 영수증을 보존한다. 과거 타이머를 실패 후 자동 재가동하지 않는다.
- 관측 창:10월19일09:25~09:27 활성화,09:30~09:40 입장,10:00 종료. 판독 시작10:05~10:20.
- 실수집·계좌 대사·순수익·새20일 관측·매수 재개는 이 배포 성공만으로 확정하지 않는다.

## Review Focus

- 새 원장을 쓴 뒤 과거 프로그램으로 자동 롤백해 읽기 실패하는 경우: 호환 reader를 실제 직전 배포본으로 둔다.
- reader-only 버전이 새 기록까지 쓰는 경우: 기존 ExecutionHistory 의도8필드의 불변을 시험한다.
- 운영 HEAD 변경 후 옛 예약이 단발 시도를 소비하는 경우: 배포 잠금 안에서 v4를 먼저 정지한다.
- 새 selection 스키마와 v5 입력이 다른 경우: 최종 통합 소스의 실제 CapturePlan과 원장 검증기로 확인한다.
- 설치 중 이전 예약 재활성화/영수증 생성: 기존 트랜잭션의 각 경계와 활성화 직전 가드를 유지한다.

## Task 1 — 호환 읽기만 추가한 복구 기준

Files: `src/core/order_attribution.py`, `src/execution/execution_ledger.py`, `tests/test_execution_attribution_reader_compat.py`.

- [x] PR #219의 검증된 validator와 선택적 facts 검증만 복사한다. Order/engine/ExecutionHistory writer는 변경하지 않는다.
- [x] 기존 행·혼합 행·호환 reader의 구형 추가 의도·최종 reader의 재개를 임시 SQLite로 검증한다.
- [x] 관련/전체 검사·독립 중요 리뷰·PR/main CI를 통과한다.

## Task 2 — v5 관측 설치 계약

Files: `scripts/ops/entry_capture_prepare_20261019_v5.py`, `scripts/ops/entry_capture_activate.py`, `tests/test_entry_capture_prepare_20261019_v5.py`.

- [x] v4 소스 해시를 고정한 별도 wrapper를 만들고 v1~v4의332개 원본/백업과 미소비 상태를 보호한다.
- [x] 명시적 v5 profile/epoch/경로와4개 이전 예약의 중지 가드를 연결한다. 과거 source pin은 수정하지 않는다.
- [x] 합성 경계 시험 후 PR #218/#219를 통합한 최종 소스로 selection-basis-v4/kr-selection-basis-v3 계약을 재검증한다.
- [x] 전체 검사·독립 중요 리뷰·PR/main CI를 통과한다.

## Task 3 — 승인된 운영 적용

- [x] 실제 서비스/미체결/복구/구독/예산·환경/설정/킬스위치/과거 관측 지문을 비공개 영수증에 고정한다.
- [x] 배포 잠금 안에서 v4 중지 → 호환 읽기 버전 고정 SHA 배포 → 기동150초 이후 확인한다.
- [x] 다시 가드 확인 → 최종 기능 고정 SHA 배포 → 기동150초 이후 원장/보호 파일/예산/로그 확인한다.
- [x] 새 v5 실제 입력을 검토·고정하고 staging/check/apply → 타이머/파일/미소비 상태를 대조한다.
- [x] 판독 소스와 manifest를 별도 v5 디렉터리에 준비해 --check를 수행한다. 미래 자료 내용은 미확정이다.
- [x] 실제 결과와 남은 작업을 문서에 갱신한다. 커밋·푸시 완료 근거는 후속 문서 PR/커밋을 따른다. 문서 후속은 재배포하지 않는다.

## 초기 SEE — 적용 전 이력

10월18일 장외 조회: 운영 `a0bc9c94`, PID2208722, NRestarts0, service active,
복구ready·미체결0·정체0. v4 타이머는10월19일09:25 enabled/active 상태다.
PR #218/#219는 검증 완료·미병합이다. 이 계획 작성 시점에 운영 변경은 아직 없다.

## 복구 형식과 진단 경계

이 복구 reader는 정확히 `order-attribution-v1` 한 필드 추가를 지원한다. 미래 형식이나
다른 facts 필드를 추가할 때는 복구 기준도 다시 검증해야 한다. 도우미 전체를 #219와
같은 바이트로 유지하므로 reader에서 쓰지 않는 생성기도 함께 있지만, 기존 writer는
계속 8필드만 쓴다. 첫 전체7302검사 통과 후 독립 리뷰의 기록기·고정 자료·대사 재개
시험 공백을 보완한다. 검증 완료와 배포 완료를 혼동하지 않는다.

복구가 `storage_fault/open_failed`면 자동 주문 재생이나 원장 수정을 하지 않는다.
운영 SHA와 이 계획의 호환 SHA, 고정 validator 지문, 원장 facts 버전을 먼저 대조한다.
읽기 전용 검증 실패 메시지만으로 형식 불일치와 무결성 오류를 구분할 수 없다.
원본을 변경하지 않는 조회로 orders/events의 facts·귀속 version과 고정 소스 SHA를
대조하고, 그 결과와 미체결을 검토한 뒤 기존 지원 대사 절차를 사용한다. 새 형식을 지우거나 버전을 바꾸어 통과시키지 않는다.

## 개발 SEE 갱신

[통합 검토·검증 근거](../../reviews/attribution-rollout-2026-10-18.md). reader 최종7314,
최종 기능 통합7956검사 통과. 독립 중요 재리뷰 승인 후 reader710af553→최종ba29b903
배포와 v5 설치·판독 입력 준비를 완료했다. [실행 영수증](../../operations/selection-capture-2026-10-19.md#10월18일-주문-귀속-배포와-v5-교체)을 따른다. 고정 fixture의 History 지문은 forward 생성기
출처이며 reader 기준의 현재 writer와 같다는 뜻이 아니다.
