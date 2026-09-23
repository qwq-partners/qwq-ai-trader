# N4 Recovery Scale Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 합성 누적 상태에서 실제 N3 캡처와 owner/sweep 비용을 측정해 health 연결 가능 여부를 판정한다.

**Architecture:** 제품 무변경, 실제 runtime fixture와 test-owned 상태를 사용하는 opt-in 벤치다. 기본 suite는 측정 하네스의 결정적 계약만 검증하고, 긴 측정은 규모별 timeout 프로세스로 격리한다.

**Tech Stack:** Python 3.12, pytest, asyncio, perf_counter, tracemalloc, 합성 SQLite tmp 저장소.

**Spec:** `docs/architecture/recovery-observability-scale-2026-09-23.md`.

## Global Constraints

- 제품·운영·주문·설정·보존/상한 변경0; 합성 자료만 사용, 실 API/자격/실 상태 접근0.
- 사전 규모0/100/1000/5000, 비 tracing 동기 지연 연결 게이트50ms, 각 측정 프로세스30초.
- 타임아웃·미측정·상한 거부를 성공/0으로 쓰지 않음. 실제 전체 C/F/G/R·설치 차단 유지.
- full suite는 모든 작업자 종료 후 coordinator가 UTC→KST 직렬 실행한다.

## Review Focus

1. 하네스가 public state 복사본만 증식해 실제 측정 상태를 바꾸지 않는 오류 — 실제 private test-owned RAM의 각 행 수 단언.
2. 같은 mutable row를 반복 alias하거나 intent/attempt 링크가 충돌하는 오류 — 서로 독립 row/고유 링크 단언.
3. 상한 거부가 빠르다는 이유로 성능 통과 — captured/counts/status를 latency와 별도 기록.
4. tracing overhead/fixture setup 시간을 일반 latency에 합산 — 별도 타이밍/측정 함수와 반환 키 인수.
5. sweep이 복구/주문 writer로 들어가거나 관측을 상태 진실성으로 오인 — 실패 spy·전후 owner 불변, 합성/미승격 표식.

## Task 1: 테스트 전용 규모 하네스

Files: 신규 `tests/test_recovery_diagnostics_performance.py`만. 공통 helper가 반드시 필요하면
신규 `tests/recovery_scale_harness.py` 하나까지 허용한다. 기존 제품·시험 파일 변경 금지.
Consumes: `test_execution_runtime.setup/opened`, N3 capture/builder, 실제 ProtectionProducer.
Produces: `measure_case(size, kind)`의 비식별 dict; kind는 capture/owner/sweep 중 하나이며
크기·wall_max_ms·loop_stall_ms·peak_bytes·report_bytes·지원 상태·finding codes를 기록한다.
가령 미측정 값은 `None`이며 `status='not_measured'`, 완료는 `status='measured'`다.
`classify_measurement(*, snapshot_stable, wall_max_ms)`는 exact bool 안정 표본 여부와
50ms 한도를 별도 `capture_supported`/`connection_eligible` bool로 판정하는 test-only helper다.

- [x] 승인 env-i로 기존 diagnostics3파일 기준선을 확인했다: **122 passed/7.79초/rc0/격리0**.
- [ ] 결정적 하네스 계약 시험을 먼저 쓰고 부재/오류로 실패하는 RED를 확인한다.
  ```python
  def test_report_distinguishes_unavailable_from_fast_success():
      row = classify_measurement(snapshot_stable=None, wall_max_ms=1.0)
      assert row['capture_supported'] is False
      assert row['connection_eligible'] is False
  ```
- [ ] test-only helper를 구현하고 위5개 Review Focus를 실제 runtime 시험으로 인수한다.
  ```python
  assert len(runtime.owner._state['intents']) == size
  assert len(runtime.owner._state['attempts']) == size
  before = deepcopy(runtime.owner._state)
  report = build_recovery_diagnostic(capture_recovery_snapshot(runtime, captured_at=NOW))
  assert runtime.owner._state == before
  assert report['automatic_action_allowed'] is False
  ```
- [ ] opt-in `QWQ_RUN_RECOVERY_SCALE=1` 없으면 장기 벤치만 skip한다. 일반 계약 시험은 skip하지 않는다.
- [ ] 원 상태와 spy0·finite schema를 검증하고 신규 파일만 커밋한다. coordinator에 실제 명령/rc·측정치·한계를 인계한다.

## Task 2: 독립 검토·측정·개발 통합

Files: coordinator의 spec/plan, 신규 `docs/reviews/recovery-scale-2026-09-23.md`, 개발 인계 문서의 좁은 최신 상태.

- [ ] Sol/high가 Task1의 자료 링크·진짜 코드 호출·시간 경계·과대 주장 여부를 독립 검토한다.
- [ ] 각 size/kind를 외부 timeout30초의 opt-in pytest로 실행한다. 원문 식별자/실 상태 출력0.
- [ ] 50ms/지원성 게이트의 원 결과를 표로 저장한다. 실패 시 HTTP/경보 배선을 구현하지 않는다.
- [ ] 작업자 종료 후 UTC/KST 전체, 문법/비밀 패턴/diff 검사를 실행한다.
- [ ] 승인된 tests/docs만 정본 개발선으로 통합/푸시하고 실제 한계와 후속 재설계를 인계한다.
  main과 runtime 설치는 그대로 유지한다. 전체 엔진 승격이나 장기 실운영 성능 완료라고 보고하지 않는다.
