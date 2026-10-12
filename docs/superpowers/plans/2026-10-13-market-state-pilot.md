# 한 종목 장 상태 파일럿 실행 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development. 조정자가 통합과 운영을 담당한다.

**Goal:** 단일 연결의 유한 기술 파일럿을 구현하고 실제 구독 여유와 남은 순수익 검증 작업을 확인한다.
**Architecture:** 별도 기술 계약/버퍼 → 기존 CapturePlan/런타임 → 정적 lease/실효 준비 상태 → 기존 단발 설치 경로. 경제성 모집단과 분리한다.
**Tech Stack:** Python, asyncio, pytest, 기존 JSONL 원장/systemd 설치기.
**Spec:** ../specs/2026-10-13-market-state-pilot-design.md

## Global Constraints

- 운영/관측 단일 KIS 연결, 기존 운영 시세 수요 보존, 주문/매수 중지/모델/예산 정책 불변.
- 한 종목·미래 같은 평일·09:00 이상~15:20 미만·최대300초. 관측 자료는 정상 연속매매/수익성 자동 증명이 아니다.
- 운영 자격증명/상태를 로컬 시험에서 사용하지 않는다. 병렬 작성자는 동일 base의 별도 worktree/별도 파일만 수정한다.
- 기본 OFF 통합과 실제 파일럿 활성화를 구분한다. 실패/불명 점유를 슬롯으로 재활용하지 않는다.

## Review Focus

- 연결 시작 전에 파일만 존재하는 가짜 준비 완료.
- 시작 준비 중 TTL 소진, 시각 역행/지연으로 창 밖 자료 포함.
- 운영40종목/80채널 수요를 보유 수만 보고 축소 평가.
- 파일럿에 scanner/order_ready가 섞이거나 운영 PRICE가 끊김.
- MKO 침묵/로컬 수신 순서를 거래소 정상 상태로 해석.

## Task 1: 기술 계약과 버퍼

Files: `src/analytics/market_state_pilot.py`, `tests/test_market_state_pilot_contract.py`.
Interface: `PILOT_VERSION`, `validate_pilot_context(context, *, study_path, now)`, `PilotBuffer(plan, now)`.
- [x] 순수 계약/버퍼 실패 검사 작성 및 실패 확인.
- [x] 엄격 기술 전용 필드/미래 구간/자원·채널/lease 검증, 기술 자료만 복사.
- [x] UTC/KST 관련 검사. 조정자 통합 전 독립 diff 확인.

## Task 2: 런타임·채널·준비 상태

Files: `entry_observation_runtime.py`, `quote_subscription.py`, `kis_websocket.py`, `kr_api.py`, 새 통합 시험.
- [x] 정적 lease 설치 전용/중복/만료/운영 우선/실제 준비/종료 반례 작성.
- [x] CapturePlan 기술 분기, 시작 전 정적 lease, 준비 조회를 연결.
- [x] 기존 fake feed/단일 socket 시험으로 운영 시세와 상태의 격리 및 완전성 검증.

## Task 3: 설치 가능성 및 남은 작업

Files: 실제 설치기 확장 여부는 실효 수요와 준비 경로 확인 후 결정, `docs/research/market-state-pilot-2026-10-13.md`.
- [x] 실효 수요를 계산/노출하고 고정 구간·단발 설치 가드를 검증한다. 수요 검증이 불가능하면 활성화 미예약으로 명시한다.
- [x] 기존 계좌/벤치마크/청산/모델 비교 기록에서 완료·미완료를 대조한다.
- [ ] 코드/프로세스 통합 리뷰·전체 시험·문서/메모리·커밋/푸시/PR/CI/병합.
- [ ] 장외 배포 가능 시간/건강/보호 지문을 재확인하고 승인 범위의 배포, 실제 수집은 별도 결과로 기록.
