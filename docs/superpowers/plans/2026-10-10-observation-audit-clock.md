# 관측 종료와 분석 시각 분리 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. 코디네이터만 통합하며 운영 변경은 포함하지 않는다.

**Goal:** 10월8일 실제 관측을 원본·경제성 기준 시각을 보존한 채 평가하고 주문 준비 이전 병목을 판독한다.

**Architecture:** 불변 study.as_of는 가격·horizon의 경제성 기준이다. 선택 인자 analysis_as_of는 이후 실행되는 봉인·보고·검토 시각 검증에만 사용한다. 종료 이후 레코드를 허용하지 않는다.

**Tech Stack:** Python, pytest, 기존 strict journal reader/entry evaluation bundle.

**Spec:** [신호 시간대 설계](../../research/current-engine-signal-window-2026-10-07.md), [10월8일 고정 입력](../../operations/entry-capture-installation-2026-10-08.md).

## Global Constraints

- 기준 SHA 4e27139a5c23cf7ea7be610babef5ea01a4db2eb. 구현은 별도 worktree, 문서는 코디네이터 소유.
- 주문·리스크·선정 설정·운영 배포·재시작·새 수집은 변경하지 않는다.
- 실제 자료는 코디네이터만 읽으며 Git에는 익명 집계·지문만 기록한다.
- 구현 Astra/high, 소스 원인 분석 Sol/high, 최종 독립 리뷰 Astra/xhigh 요청. 실제 모델 metadata 미노출 시 확인된 것으로 쓰지 않는다. worker 재위임 금지.

## Review Focus

- 종료 후 정상 봉인: 종료+0.017878초 seal은 더 늦은 명시 분석 시각에서만 허용한다.
- 종료 후 자료: 분석 시각을 늦춰도 window.end_at 이후 observed_at은 거부한다.
- 미래·미지정 시간대: 분석 시각 뒤 봉인, naive/잘못된 시각, 경제성 기준보다 이른 분석은 거부한다.
- 호환성: 인자 미지정은 기존 검증을 유지하고 window 평가기의 여러 스캔 미지원 범위를 확대하지 않는다.
- 수익 오해: order_ready가 없으면 unknown/null을 유지하며 호가가 많아도 수량·진입을 보간하지 않는다.

## Task 1 — 오프라인 시각 계약 수정

파일: src/analytics/entry_observation.py, received_entry_input.py, entry_evaluation_bundle.py, entry_exit_comparison.py, entry_window_evaluation.py 및 관련 CLI·테스트. 별도 분석 시각을 keyword로 전달하고 출력 provenance를 경제성 payload 밖에 둔다.

- [x] 합성 원장으로 정상 지연 봉인의 실패를 재현한다.
- [x] analysis_as_of 전달·검증과 window 레코드 상한을 구현한다.
- [x] 위 다섯 실패/호환 사례 및 관련 기존 테스트를 실행한다.
- [x] 코디네이터가 diff를 검토하고 독립 리뷰·전체 verify 후 통합한다.

## Task 2 — 실제 관측과 개선 우선순위

- [x] 원본 지문·봉인·누락·선정·gate·호가 범위를 기존 도구로 대조한다.
- [x] 신호→수량→리스크→order_ready 경계를 소스와 비공개 로그로 추적한다. 로그의 손실 비율을 계좌 일수익률로 단정하지 않는다.
- [x] 원본 study 수정 없이 bundle을 생성하고 unknown 이유·비교쌍·null 손익을 확인한다.
- [x] PDS 결과, 모니터링과 인계 문서를 갱신하고 구현과 운영 적용 상태를 구분한다.

통합 검증4683개·독립 검증322개·비밀정보/문법/격리 검사 통과. 공개 단계는 이 계획과 함께 커밋·푸시하고 PR의 필수 CI 후 main에 반영한다. 실제 CI/머지 상태는 PR checks/Git 이력이 정본이며 운영1d45704는 유지한다.
