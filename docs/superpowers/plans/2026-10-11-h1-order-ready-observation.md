# H1 주문 준비 호가 배정 구현·배포 계획 (65차)

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans, task by task. 코디네이터만 통합·배포한다.

**Goal:** 조기 거절 신호가 H1 호가 관측 예산을 소진하지 않게 하고, 실제 주문 준비에 도달한 최초5개 신호를 구분해 수집할 수 있는 코드를 검증·배포한다.

**Architecture:** 기존 유한 관측 런타임에 `runner-order-ready-window-v1`을 추가한다. 기록된 `order_ready`만 비동기 호가 요청 큐로 전달하며, 기존 신호 생성 시 배정하는 v1은 유지한다. 관측은 주문 허용이나 체결 인증이 아니다.

**Tech Stack:** 기존 Python/asyncio/JSONL/pytest, systemd와 `local_deploy.sh`. 새 의존성 없음.

**Spec:** [64차 H1 계약](../../research/h1-entry-price-contract-2026-10-11.md). 사용자가 64차 완료 후 다음 구현과 운영 배포를 승인했다.

## Global Constraints

- `live_screening`/`gap_and_go` BUY, 기록된 양의 정수 수량, 최초5개 신호 identity. 주문/위험/사이징·매수 중지·자동매도 금지 정책 불변.
- 새 명시 버전에서만 `quote_admission=order_ready`, 등록 상한41·운영 여유10. 기존 study·원장·지문·once/영수증과 Toss 승인 자료 보존.
- 장중 실거래 없음. 개발 검증은 합성 입력만, 외부 API/운영 자격증명 접근0. 운영 조회·배포는 코디네이터만 수행.
- KST 09:00~10:05 접수·10:21 종료·최대14스캔의 용량 가정 검증. 합성 통과를 실제 틱 빈도/체결/수익 보장으로 해석하지 않음.
- 일반 모드 배포와 날짜별 새 수집 활성화를 구분. 새 study/실자료 manifest/일정은 이번 배포에서 만들지 않음.
- 여러 날짜/동시 자본 재생·장중 벤치마크는 후속 별도 계약이다. 본 변경이 완성하거나 실증한 것으로 쓰지 않음.

## Review Focus

1. 조기 신호 거절은 호가 lease를 소진하지 않아야 한다.
2. 같은 신호 중복과 같은 종목의 다른 신호를 혼동하지 않아야 한다.
3. scan은 창 안이지만 order_ready가 접수 마감 뒤인 경우 배정을 거절해야 한다.
4. 큐 가득 참/전송 실패/종료는 관측 실패로 남고 주문 처리나 보호 SELL을 기다리게 하지 않아야 한다.
5. 새 버전의 레코드·시간·신호 연결은 설치와 오프라인 strict reader에서 모두 검증되어야 한다.

## Task 1 — 명시 버전과 주문 준비 배정

**Files:** `src/analytics/entry_observation_runtime.py`, `entry_observation.py`, `entry_observation_journal.py`, `kis_frame_diagnostics.py`, `received_entry_input.py`; `src/data/feeds/quote_subscription.py`; gate 보고 CLI. engine 관측 접점은 유지한다. 시험은 `tests/test_signal_window_observation.py`, `tests/test_order_ready_window_observation.py`.

**Interfaces:** 새 버전 `runner-order-ready-window-v1`, `quote_admission='order_ready'`, `channels.max_candidates=5`. 기존 `capture_order_ready`의 저장 성공 뒤 buffer callback이 `submit_order_ready` 큐에 넣는다. 새 모드 lease identity는 signal_id이며 구독 기록에 signal_id/order_id를 보존한다.

독립 검토에서 대상 선정과 실제 구독 세대의 혼동을 확인해 별도 `order_ready_admission` kind로 구분한다. 저장→봉인→엄격 판독→가격 비교 입력까지 통합 회귀에 포함한다.

- [x] 새 계약/시간 경계/적격 조건/중복/예산5/실패 비전파 회귀를 작성하고 기존 코드에서 RED 확인.
- [x] 기존 API와 호가 소유권 구조를 유지해 최소 확장, 위험·주문 판정 변경 없음 확인.
- [x] 신규+구형 런타임/원장/구독 회귀 GREEN, UTC/KST 확인.

## Task 2 — 전체 창과 독립 검토

- [x] 기존 용량 도구를 새 버전 입력에 적용하거나 독립 합성 시험으로 전체81분·14스캔·종료/봉인을 검사.
- [x] 동시 운영 구독과 headroom, 큐/파일/메모리 한도와 실측되지 않은 가정을 기록.
- [x] 전체 verify·비밀정보·문서 링크 검사. 다른 공급자의 독립 소스 리뷰 후 지적 해결.

## Task 3 — 통합 및 운영 배포

- [x] 코드/현재 상태/체크리스트/검증 문서를 갱신하고 feature 브랜치 commit+push, PR+필수 CI 후 병합.
- [x] 배포 직전 시각·운영 HEAD·로컬 pending0·실행 복구 ready·활성 관측 없음, 보호 파일 지문 재확인. pending0은 증권사 전체 미체결 대사가 아님을 유지.
- [x] 고정 병합 SHA를 기존 `local_deploy.sh`로 배포(verify→재시작→health, 실패 자동 롤백).
- [x] 새 PID·기동 로그·HTTP/브로커/복구 상태·매수 중지/설정 지문·자동 재시작 여부 확인.
- [x] 배포 결과와 실제 남은 연구 조건을 문서에 기록한다. commit+push/PR 통합 결과는 해당 문서의 Git/PR 이력으로 확인한다.
