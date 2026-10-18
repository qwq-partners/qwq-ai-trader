# First Exit Replay Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development with one coordinator and independent final review.

**Goal:** 같은 진입·장중 PRICE 경로에서 실제 청산 코드의 첫 판단 차이를 재현한다.

**Architecture:** 순수 입력 검증 → 독립 시계/임시 상태로 실제 ExitManager 실행 → 첫 신호 또는
경로 내 미관측 보고. CLI는 명시 입력만 읽고 새 보고 파일을 만든다. 운영 연결은 없다.

**Tech Stack:** Python, Decimal, stdlib types/tempfile, pytest, 기존 ExitManager.

**Spec:** [입력·격리·해석 계약](../specs/2026-10-18-first-exit-replay.md)

## Global Constraints

- 기준 main `4a24fbf123339c56ad27dd1331a9cfd03d449c45`, 별도 feature 작업 트리.
- 운영 ba29b903/PID2369151 및 v5 고정 입력/예약 변경 없음. 브로커·거래 엔진 유료 LLM 호출 없음.
  별도 개발 리뷰 호출과 확인된 비용/미확정 비용은 검토 문서에 구분한다.
- 실제 체결·순수익 추정 없음. 원천 불완전성을 전체 세션 무신호로 해석하지 않는다.
- 작업자는 분담 파일만 수정하고 최종 리뷰는 저자와 분리한다.

## Review Focus

- 임시 시계·경로가 원래 모듈 및 운영 캐시에 영향을 주는지: 병렬 격리/원본 보존 시험.
- 부분 매도 신호를 체결로 승격하는지: pending 상태·on_fill 미호출 시험.
- 시간대/시각 역전/NaN/중복 JSON 키: 입력 거부 시험.
- 경로 미관측과 정책 동등성을 혼동하는지: 양쪽 미관측 비교 null 시험.
- CLI가 입력/기존 보고를 덮어쓰거나 특수 파일에서 대기하는지: 새 파일 전용·정규 파일 시험.

## Task 1 — 실제 첫 판단 재생

Files: `src/analytics/first_exit_replay.py`, `tests/test_first_exit_replay.py`.
Interface: `replay_first_exit(payload: dict) -> dict`, `MAX_INPUT_BYTES`, `MAX_EVENTS`.
담당: Astra/high 요청, 실제 metadata 노출 여부 별도 기록. 상태 격리 중요 변경으로 분류.

- [x] 실패 시험: 손절, 본전 보호/ATR, 첫 부분매도, 무신호, 입력 검증, 병렬/전역 격리.
- [x] 실제 생성자/등록/가격 평가를 독립 globals·시계·임시 경로로 실행한다.
- [x] 직접 격리 실행과 동일 판단 확인, 정책/입력/소스 지문과 한계 출력.
- [x] 관련 pytest 통과와 diff 검토 후 작업자 커밋.

## Task 2 — CLI와 사용 계약

Files: `scripts/report_first_exit.py`, `tests/test_report_first_exit.py`, 관련 문서.
Interface: `--input <json> --output <new-json>`; 성공0, 잘못된 자료/파일2.
담당: 통합 조정자. Task1과 다른 파일에서 병렬 작업.

- [x] 먼저 크기/중복 키/덮어쓰기/비정규 파일/권한 시험을 작성하고 실패 확인.
- [x] 명시 파일만 제한 읽기, 기존 파일 덮어쓰기 없이 0600 결과 생성.
- [x] 모듈 통합 후 합성 장중 경로로 CLI 실행과 null 수익 결과 확인.
- [x] 한계·입력 예시·후속 실제 자료 요구를 문서화.

## Task 3 — SEE와 통합

- [x] 동작·입력·CLI 독립 리뷰와 전체 verify(문법·tests·비밀 검사) 확인.
- [x] 운영·v5 입력/예약 불변 확인. 배포 없음; 실제 경로/순수익 평가 미완료 명시.
- [x] 문서에 실제 검사·리뷰 범위와 결과 확인 위치를 기록.

최종 통합 조건: 정제 소스만 도구 없는 Claude Opus5/xhigh에 제공한 격리 리뷰의 실제 모델과
승인 판정 확인, 미해결 중요 지적 없음, PR 필수 CI 성공. 조정자만 commit/push/PR·병합을
진행한다. 이 조건들의 최종 실행 상태와 리뷰 영수증 요약은 PR 타임라인을 정본으로 남긴다.
