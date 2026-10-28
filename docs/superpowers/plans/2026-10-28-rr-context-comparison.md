# E02-RR-COMPARE 구현·실자료 준비 계획

> **For agentic workers:** Use superpowers:subagent-driven-development. 작업 순서와 완료 상태의 정본은 [실행 원장](../../operations/execution-roadmap.md#e02-rr-compare-next)이다.

**Goal:** 선정 당시 근거와 실행 신호의 가격·손절·계획 수량을 비용 후 기준으로 나란히 비교하여 다음 선정/진입 개선 한 가지를 찾는다.
**Architecture:** 기존 `compare_order_rr`를 순수 adapter와 명시 파일만 받는 CLI에서 호출한다. 실자료 추출은 운영 저장소의 필요한 필드만 읽는 별도 절차이며 분석 도구에는 DB·환경·네트워크 연결을 넣지 않는다.
**Tech Stack:** Python 표준 라이브러리, 기존 Decimal 비교기/FeeConfig, pytest.
**Spec:** [승인된 NEXT 계약](../../operations/execution-roadmap.md#e02-rr-compare-next). 사용자 10/28 후속 실행 승인. 기준 main `fcb3a71e876f0db37522e0e849fe81e29450fce4`.

## Global Constraints

- live engine/전략/주문/비용률/매수 중지/예산/예약/DB schema 변경0. 새 API·LLM 호출·가상자금·체결 생성0.
- 원래 float 진단과 Decimal 재구성을 별도 표시한다. 선정 당시 비용률로 고정한 조건부 분석이며 실제 실행 RR·순수익·주문 승인으로 표시하지 않는다.
- requested_quantity / entry_risk.planned_quantity / size_trace_quantity를 분리한다. 서로 다르면 충돌을 표시하고 선택하지 않는다. 결측·실제0·양수를 구분한다. 원래 선정 수량은 없으므로 현재 계획 수량을 양쪽 가격에 적용한 비교라고 명시한다.
- 최초 SEPA gate만 지원한다. unknown gate·비용·가격·수량을 기본값으로 복원하지 않는다. 실제 intent 조인은 최초 범위에서 제외한다.
- 저장 row id는 신호 id가 아니다. row symbol/strategy/side, metadata.signal_id와 rr_context.signal.signal_id의 일치 여부를 검사한다. 다른 종목·전략·SELL·ID 충돌은 비교하지 않는다.
- private env/계좌/원문 로그는 worker에 제공하지 않는다. 개발·시험은 합성 자료만 사용한다. 실제 판독의 자료 종류와 입력 지문을 보존한다.

## Review Focus

- 같은 signal의 여러 기록행을 서로 다른 투자 기회로 세는 오류: 행 수·고유 신호 수를 분리한다.
- captured snapshot이라도 비교기의 숫자 범위 밖이거나 손상된 필드면 지원 불가로 남긴다.
- selected fee rates와 당시 float gate를 현재 비용/실제 주문 결정으로 바꿔 읽지 않는다.
- 파일 중복 JSON 키·NaN/Inf·비정상/과대·빈 입력·실자료/합성 구분·에러 메시지의 원문 노출을 확인한다.
- 데이터가 없으면 분모와 결측 사유를 내며 수익0/정상/효과 없음으로 판정하지 않는다.

### Task 1: 오프라인 adapter/CLI와 검사

**Files:** 새 `src/analytics/rr_context_comparison.py`, `scripts/compare_rr_context.py`, `tests/test_rr_context_comparison.py`만 구현자가 수정한다.
**Interfaces:** `compare_rr_rows(rows: list, *, source_ref: str, data_kind: str) -> dict`; `data_kind`는 actual/synthetic, CLI `--input PATH --data-kind actual|synthetic --format json|markdown`(기본 json). 입력은 기존 signal_events 행의 JSON 배열이며 파일 최대32MiB/행 최대10,000. stdout 결과만 내고 파일/DB/설정 쓰기0.

- [x] RED: 실제 producer schema를 합성한 행에서 기존 비교기와 동일한 선정/현재 계산, 가격 이동·손절 변경, 3개 수량 출처별 결과를 단언한다. 제품 모듈 부재를 명시적으로 실패시키고 기록한다.
- [x] 구현: selection_observed 원형 필드, current_reference, 각 quantity lane의 selected_prices/current_prices 계산과 사유를 반환한다. 원래 gate 진단·가격 변화율·입력/기록 출처·범위 제한·분모를 포함한다. 원문 metadata 전체를 출력하지 않는다. 기존 `copy_rr_selection`과 `compare_order_rr`를 재사용하되 실제 필드의 제약을 소스와 대조한다.
- [x] 경계: missing/0/conflicting quantities, identity/source/version conflict, unsupported/legacy/SELL, fee0/unknown, 불완전 가격, Decimal28 대 snapshot128 범위, 반복 신호 행, empty list를 검증한다.
- [x] CLI: 명시 파일만 읽고 중복 키/비유한수/과대 입력/읽는 중 변경을 거부한다. 입력 SHA256와 actual/synthetic 선언을 결과에 붙인다. 실패 rc2·짧은 분류 메시지이며 raw payload/비밀값을 포함하지 않는다. JSON/Markdown을 실제 subprocess로 검증한다.
- [x] 관련 검사/문법/diff → coordinator 전체 verify·비밀검사 → 독립 코드/계약 리뷰 → 통합.

### Task 2: 실제 자료 추출·첫 판독 준비

**Owner:** coordinator only. **Files:** 기존 실행 원장/관련 사용 문서, 실제 출력은 비공개 작업 디렉터리.
- [x] signal_events 실제 schema/기존 연결 경로를 소스로 확인한다. 최소 필드만 포함하는 시간 범위·행 상한·읽기 전용 transaction/statement timeout의 추출 계약을 준비한다. 분석 CLI와 DB 추출을 분리한다.
- [x] 운영 연결이 기존 승인·접근 범위 안에서 가능하면 배포 시점 이후 자료를 1회만 읽고 필요한 필드만 private0600로 저장한다. 연결/권한 확대 또는 새 API 호출이 필요하면 추출 절차와 제한만 기록한다.
- [x] 실제 입력은 actual로 판독한다. 없으면0행/coverage만 보고하고 합성 성과로 대신하지 않는다. 새로운 장기 수집을 시작하지 않는다.

### Task 3: 장중 보고 기준 설계와 통합 문서

**Owner:** coordinator; 독립 source-only 분석 병행 가능. **Files:** 실행 원장의 R02 절과 관련 보고 설계 문서.
- [x] 기존 지수 자료의 원천 날짜/시각·전일 비교·호출 부담을 검토하고 최소 입력 연결안을 설계한다. 이번 단계는 설계이며 매매 정책 변경이 아니다.
- [x] 독립 리뷰·전체 검사 근거, 실제 판독 한계, 운영fcb3a71e 유지·예약 보존, NEXT를 원장/CLAUDE/CHANGELOG/docs 인덱스에 반영한다.
- [ ] commit/push/PR CI·병합을 완료한다. 오프라인 도구/문서만이면 운영 재시작·배포는 하지 않는다.

## Pre-flight / rulings

| 소비 관계 | 충돌 확인·결정 |
|---|---|
| rr_context → Task1 | snapshot128자리와 계산28자리의 범위 차이는 unavailable로 보존. 현재 수량을 선정 당시 실제 수량으로 표현하지 않음 |
| Task1 → Task2 | DB 연결 없는 CLI와 실제 추출이 충돌하지 않도록 추출은 coordinator의 별도 읽기 절차로 제한 |
| Task1 ↔ Task3 | 공유 제품 파일 없음. 원장/문서는 coordinator 단독 작성 |

기존 상세 설계/사용자 실행 승인을 재사용한다. 새 매매정책이나 유료 비교 승인을 요청하는 작업이 아니다. 최종 리뷰는 구현자와 분리하고 완료 근거를 실행 원장에 남긴다.

## 실행 결과

Task1 관련147검사·전체9,618검사·격리0·독립 최종 APPROVE, P2 1건은 재현 후 수정·재리뷰로 닫았다. Task2 실자료0행/무비교 판독, Task3 R02설계/통합 문서 검토는 완료했다. 마지막 commit/push/CI/병합 항목은 [PR #292 최종 영수증](https://github.com/qwq-partners/qwq-ai-trader/pull/292)으로 닫으며 이 체크박스를 위한 별도 배포/상태 PR은 만들지 않는다. 운영fcb3a71e 유지, 새 유료 호출·재시작·예약·매매 변경0.
