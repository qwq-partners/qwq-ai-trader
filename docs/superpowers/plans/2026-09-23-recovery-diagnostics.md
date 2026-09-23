# N3 Recovery Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 기존 runtime의 부족한 복구 증거를 무부작용·비식별 보고서로 반환한다.

**Architecture:** private memory adapter가 두 표본을 함수 내부에서만 비교한다. frozen
안전 DTO를 순수 builder가 JSON-safe 고정 schema로 변환한다. 운영 소비자는 연결하지 않는다.

**Tech Stack:** Python, dataclasses, Decimal, asyncio, pytest; 기존 의존성만 사용.

**Spec:** `docs/architecture/recovery-diagnostics-2026-09-23.md`

## Global Constraints

- 항상 `read_only=True`, `automatic_action_allowed=False`, `trading_ready=False`, `installation_verified=False`.
- health/clock/store/load/restore/audit/sweep/recovery/network 호출 금지.
- main 병합·운영 변경·실 API·설정·Toss grant 변경 금지.
- 동일 spec commit의 격리 worktree, 한 파일 한 작성자, worker fan-out 금지.
- 전체 pytest는 coordinator가 모든 worker 종료 후 UTC→KST 직렬 실행.
- 외부 Opus5/xhigh 검토는 선별 소스 tools-off, 최대2회×USD5/600초.

## Review Focus

1. 같은 owner version/같은 task 수에서 RAM 내용·객체가 바뀌면 volatile (Task1/2).
2. null effect_source/delivered 감사행도 미연결 보호 증거일 수 있음 (Task1/2).
3. cash-only 예약·None 위험·bool 수량·NaN은 0/정상으로 포장 금지 (Task1/2).
4. 부분체결 후 초기 target≠현재 수량은 정상일 수 있음 (Task1/2).
5. 반환된 객체 변이·custom hook·순환·부족한 배선은 비밀 유출/정상 판정 금지 (Task1/2).

## Task 1: Safe capture and classifier

**Owner:** Astra/high, 별도 worktree, 35분 상한. 초과 시 현재 diff/검증·미완을 인계.
**Files:** create `src/execution/safety/recovery_capture.py`,
`src/execution/safety/recovery_diagnostics.py`, `tests/test_recovery_diagnostics.py`.
**Interfaces:** spec의 두 함수, `RecoverySnapshot` frozen DTO. 보고서 findings code는 spec 고정.

- [x] RED: 아래 API 부재와 핵심 계약을 실제 실패로 확인하고 명령/exit 기록.

```python
def test_missing_runtime_does_not_authenticate_legacy():
    snapshot = capture_recovery_snapshot(None, captured_at=NOW)
    report = build_recovery_diagnostic(snapshot)
    assert report['mode'] == 'unknown'
    assert report['snapshot_stable'] is None
    assert report['trading_ready'] is False
    assert 'snapshot_unavailable' in {r['code'] for r in report['findings']}
```

- [x] spec의 코드별 synthetic case와 Review Focus 5개를 parametrized 시험으로 작성.
  원문 sentinel은 모든 문자열/식별자/가격 자리에 넣고 JSON 출력에 없어야 한다.
  bool count/version, 음수, NaN/Infinity, 누락/None은 evidence_invalid/unknown이다.
- [x] exact type·bounded clone·두 표본 비교·private adapter를 구현. raw는 반환하지 않는다.
- [x] pure classifier/DTO/builder 구현. 고정 enum/건수만 반환하고 결과 변이를 격리한다.
- [x] focused pytest GREEN, diff 확인, 지정 파일만 커밋. 전체 suite는 실행하지 않는다.

Run (작업 worktree):

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest tests/test_recovery_diagnostics.py -q -p no:cacheprovider --tb=short --show-capture=no
```

## Task 2: Independent real-runtime acceptance and integration

**Owner:** coordinator, Task1과 병렬; 다른 파일만 작성.
**Files:** create `tests/test_recovery_diagnostics_acceptance.py`.
**Consumes:** 동일 함수 signatures, 고정 findings 코드.

- [x] 기존 `tests/test_execution_runtime.py::setup`의 실제 owner/engine 및 실제 producer로
  캡처 전후 state/version/RAM 예약 동등성을 검사하는 RED를 고정한다.

```python
def forbidden(*args, **kwargs):
    raise AssertionError('diagnostic crossed a read-only boundary')
# monkeypatch runtime.health, clock, owner.mutate, store.load/commit,
# producer.health/_audit_recovery/sweep, broker HTTP/recovery writers.
# report = build_recovery_diagnostic(capture_recovery_snapshot(runtime, captured_at=NOW))
# assert original owner state, versions, producer RAM remain equal
```

- [x] missing/partial binding, 실제 episode/restart retry 중첩 반환 격리, 부분 SELL 체결,
  null/delivered 감사, 동수 task 교체를 synthetic 하네스에서 인수한다.
- [x] Task1 diff/검증 확인 후 coordinator만 cherry-pick하고 focused 두 파일+기존146건 실행.
- [x] 작성자가 아닌 Astra/xhigh 독립 검토와 tools-off Opus5/xhigh 교차 검토를 완료.
  findings는 재현→수정→회귀→재검토, 비공개 원문은 리뷰 패키지에 넣지 않는다.

## Task 3: Verify and handoff

**Owner:** coordinator, 작업자 모두 종료한 뒤 실행.
**Files:** CHANGELOG.md, docs/README.md, 기존 N2 architecture/runbook/p1-next-steps,
`docs/reviews/recovery-diagnostics-2026-09-23.md`, 이 plan.

- [x] 모든 worker 종료 확인, 아래 전체 pytest를 UTC와 KST 순서로 실행.

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest tests -q -p no:cacheprovider --tb=short --show-capture=no
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=Asia/Seoul PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest tests -q -p no:cacheprovider --tb=short --show-capture=no
```

- [x] `git diff --check`, 제품 문법·비밀정보 검사, 실제 테스트 수/모델/리뷰/비용/잔여 기록.
- [x] 현 제품과 절차 문구를 맞추되 N2 과거 검증 결과는 재작성하지 않는다.
- [x] 검증 제품/시험을 정본 개발선에 FF 통합·feature 푸시. clean 상태/원격 head 확인 후만 수행.
  작업 브랜치의 별도 원격 복사본은 만들지 않는다. 후속 문서도 같은 개발선으로 통합한다.
- [x] 운영 미설치·readiness False·후속 관측 전달 및 기존 차단을 최종 인계 문서에 기록한다.
