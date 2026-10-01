# 과거 주문·체결·장부·baseline 증거 대사 — 36차 Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. 조정자가 통합하며 작성자와 최종 중요 변경 검토자를 분리한다.

**Goal:** 실제 원장을 변경하지 않고, 제공된 증권사 원주문·거래 이벤트·잔고 자료에서 복구에 필요한 누락/중복/차이와 구체적인 후속 조치를 찾는다.

**Architecture:** SQLite는 mode=ro 읽기 transaction에서 원래 이벤트와 현재 상태를 재검증해 export한다. 별도 순수 비교기는 명시 JSON 자료만 읽고 원주문 identity·증분 multiset·수량·금액을 비교한다. CLI는 원장과 세 자료의 지문/개수/선언된 기간·출처를 결과에 고정한다. 이 도구는 계좌 상태를 변경하거나 runtime hold를 해제하지 않는다.

**Tech Stack:** sqlite3, Decimal, 표준 JSON/argparse, pytest 임시 파일.

**Spec:** 본 문서 아래 입력/판정 계약. 기준 main `3be93a95a1b76dbd4f8de521c45df8bc0315e54b`.

## Global Constraints / 입력 계약

- 운영 API·상태·계좌 자료·자격증명·SSH·배포·재시작·주문 접근은 이번 개발/검증 범위 밖. 합성 임시 자료로 검증하며 bare pytest 금지; 명시 tests/만 실행한다.
- `read_execution_ledger(path, expected_scope)`는 부재/손상/scope·schema 불일치를 빈 원장으로 바꾸지 않는다. SQLite source bytes/events/sessions 불변. WAL/rollback sidecar가 있는 자료는 snapshot 복사본 확보가 필요한 미지원 입력으로 거부하며 immutable=1로 WAL을 무시하지 않는다.
- 반환 export: `format='execution-ledger-export-v1'`, `account_scope`, `schema_version=1`, `event_count`, `state_digest`, `source_sha256`, `sessions`, `orders`. session/이벤트의 원래 시각은 없으므로 생성하지 않는다.
- 외부 공통 envelope: `schema_version=1`, `account_scope`, `market='KR'`, `source_kind`, `captured_at`(timezone 포함 ISO), `window_start/window_end`(YYYY-MM-DD, KST 거래일), `declared_complete`(bool), `source_ref`(비어 있지 않은 설명), `rows`(list). 출처/완결성은 선언이며 내용의 진실 인증이 아니다. root 비교 입력은 broker/journal/baseline별 envelope 세 개다.
- broker `source_kind='broker_all_orders'`: 원시 문자열 필드 `ord_dt, odno, orgn_odno, pdno, sll_buy_dvsn_cd, ord_qty, tot_ccld_qty, avg_prvs, cncl_yn, cnc_cfrm_qty, rmn_qty, rjct_qty`. 0체결도 보존하며 정확히 동일한 중복 행도 보고한다. 누적 평균가×수량은 계산금액이며 독립 결제금액이 아니다.
- journal `source_kind='journal_events'`: `event_id`, `trade_id`, `market='KR'`, `event_date`(YYYY-MM-DD), `date_basis='Asia/Seoul'`, `symbol`, `side='buy'|'sell'`, `kis_order_no`(NULL 허용하되 근거 부족으로 보고), `quantity`(정수), `price`(Decimal 문자열). 현재 일반 DB 기록의 NULL ODNO를 종목명으로 보충하지 않는다.
- baseline `source_kind='baseline_snapshots'`: rows에 `owner='broker'|'internal'`, `captured_at`(aware ISO), `positions`([{symbol,quantity}]), `cash`({amount:Decimal문자열|null,basis:'orderable'|'deposit'|'settled'|'portfolio'|'unknown'})를 각각 최대 한 개 제공한다. 양쪽 값의 비교만 가능하며 현금 종류가 다르면 비교하지 않는다. 단일 snapshot으로 과거 체결 포함/계좌 현금 이동을 역산하지 않는다.
- 모든 알려진 주문을 `(order_date, ODNO)`로 연결하고 symbol/side/original quantity를 검증한다. 동일 ODNO 다른 날짜는 별개다. 정정 계보/범위 밖 미완료/unclean session은 명시 gap이다.
- ledger↔journal은 `(증분 수량, round(Decimal(price),2))`의 multiset과 합계 수량·금액을 따로 비교한다. 합계 일치만으로 중복/누락 상쇄를 통과시키지 않는다. `handoff_returned`는 장부 내구성 증거가 아니다.
- 오류 행을 조용히 생략하지 않고 입력 개수·유효/무효/중복 개수와 이슈를 보존한다. JSON의 중복 key/비표준 NaN도 CLI에서 거부한다.
- 최종 상태는 제공 자료의 `consistent|incomplete|mismatch`이며 계좌 복구 완료와 구분한다. 항상 `runtime_release_allowed=false`, `replay_allowed=false`, `state_mutated=false`, `baseline_inclusion='unverified'`다. 해제 API/복구 주문/기록 덮어쓰기 기능은 제공하지 않는다.

## Review Focus

1. 없는/손상/WAL SQLite가 read-only export 과정에서 새 session 또는 파일을 만들지 않는지 검사한다.
2. 원장 주문0인 unclean session, terminal/receipt 모두 맞는 unclean session도 위험기간/누락0을 추정하지 않는다.
3. 계좌범위·기간·시간대·선언완결성 혼입은 전체 결과에 남기며 일부 정상 행만으로 성공하지 않는다.
4. 같은 합계/다른 증분행, 같은 ODNO/다른 날짜, NULL identity, raw duplicate와 Decimal 반올림을 구분한다.
5. baseline의 현금0/결측/현금종류 차이 및 자료 시각 차이를 체결 누락이나 수익으로 채우지 않는다.

## Task 1 — 읽기 전용 원장 export

**Owner:** 별도 worktree Astra/high 요청(원장 무결성). 파일 `src/execution/execution_ledger.py`, 새 `tests/test_execution_ledger_readonly.py`만 작성.
- [x] 위 반환 계약의 `read_execution_ledger(path:Path, expected_scope:str)->dict`와 bytes/rows/파일부재/sidecar/scope/schema/corruption 검증을 RED→GREEN으로 구현했다.
- [x] 기존42개 store 회귀와 새 임시SQLite read-only25개를 통과했다. 실제 DB/운영 상태를 읽지 않았다.

## Task 2 — 대사 및 CLI

**Owner:** 조정자. 새 `src/execution/recovery_evidence.py`, `scripts/reconcile_execution_evidence.py`, `tests/test_recovery_evidence.py`, `tests/test_recovery_evidence_cli.py`.
- [x] `reconcile_execution_evidence(ledger, broker, journal, baseline)->dict`를 위 schema와 판정 기준으로 구현했다. 구조적으로 해석 불가한 envelope는 ValueError; 행 오류/불완전성은 보고서에 보존한다.
- [x] 실제 임시 원장과 JSON 파일을 함께 입력하는 CLI를 만들었다. 계좌 scope와 네 입력 경로를 명시하며 기본 운영 경로/환경/네트워크 사용은 없다. 보고서는 stdout JSON; 오류는 민감 입력을 출력하지 않고 exit2.
- [x] 실제 임시 원장→CLI 보고서와 입력 불변, 추가주문/행상쇄/반올림/원천누락/현금비교 회귀40개를 검증했다. 관련 전체107개 통과.

## Task 3 — See 및 인계

- [x] 별도 구현 비참여 Astra/xhigh 요청 리뷰 APPROVE. 관련107개·전체3,596 passed/2xfail·문법·비밀정보·격리0 확인. 실제 모델/실효 effort 메타데이터는 미노출로 미검증.
- [x] CLAUDE/CHANGELOG/인계 문서에 사용법·입력 예시·보고서 의미·남은 실제 근거를 기록했다. 운영 해제 완료로 표현하지 않는다.
- [ ] 검토된 diff를 PR/동일head CI로 통합한다. 기존 예약5468208·운영 override·매수 중지 보존.

## 실제 복구와 수익 검증으로 이어가는 순서

원본 자료를 확보한 뒤 같은 도구의 주문별 지적을 해결한다. 과거 이벤트의 DB 기록 identity 보강/잔고 포함 근거와 명시 계좌 범위 대사가 끝나야 보류 해제와 baseline 채택을 별도 결정할 수 있다. 예정 관측 자료의 품질 확인과 비용 후 진입·청산 비교는 이 운영 보류를 자동 해제하지 않으며 병행 가능한 다음 수익 연구다.
