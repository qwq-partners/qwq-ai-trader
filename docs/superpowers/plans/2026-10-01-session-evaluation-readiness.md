# 호가 근거 연결과 평가 기준 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. 코디네이터가 구현하고 별도 검토자가 결과를 확인한다.

**Goal:** 기존 원장을 원본 그대로 검증하고, 명시적으로 검토한 장 상태 근거를 호가에 연결해 비용별 가격 진단과 부족한 입력 목록을 만든다.

**Architecture:** 새 오프라인 조립기/CLI가 기존 원장 reader·prepare_input·markout 선택기를 재사용한다. 실제 수집/예약/기존 계산 규칙은 고정한다. 현재 저장 필드만으로 연속매매를 자동 증명하지 않는다.

**Tech Stack:** Python 표준 라이브러리, 기존 분석기, pytest, JSON.

**Spec:** 사용자 승인한 우선순위1(실제 호가 근거 연결)·2(평가 기준), `docs/research/current-engine-remaining-pds-2026-10-01.md`와 아래 입력 계약.

## Global Constraints

- 기준 `2734ef43f3df48cf6f5e8d90fcee1818bcf877ae`. 실제 활성화 대상5468208·설치 입력·매수 중지·설정·예약은 불변이다. 주문/SSH/서비스/API 계좌 접근을 하지 않는다.
- 현재 BOOK의 시간 구분0은 장중 표시일 뿐이다. 현재 보존 필드로 VI/단일가/정지 부재와 원천 거래일을 자동 확정할 공식 근거를 확보하지 못했다.
- 평가용 검토 입력은 작성자의 명시 확인이며, 해시/형식 검사는 원격 원본 인증이나 진술의 참 여부를 보증하지 않는다. 이 한계를 출력한다.
- 입력 원장은 수집 당시 study 원문 SHA로 검증한 뒤에만 파생0/10/30bp를 계산한다. 원본 변경·유리한 첫 호가 재선택·unknown 삭제 없음.
- 첫3개만 관측한 자료로 전체 후보/계좌 순수익을 통과시키지 않는다. 실제 source 근거 미확보는 구현 완료와 별개로 남는다.

## 입력/출력 계약

- `src/analytics/entry_evaluation_bundle.py`: `build_evaluation_bundle(study_bytes, observations, *, study_sha256, session_review=None)`.
- 실제 study 원문 bytes의 SHA와 journal 메타데이터의 SHA를 일치시킨 뒤 내부에서 study/epoch를 해석한다. 검토 입력 binding은 `study_sha256`, canonical `observation_sha256`, `capture_id`, `evaluation_epoch`를 정확히 일치시킨다.
- 검토 문서 `version=entry-session-review-v1`, `dataset_kind` 일치, `binding`, `quotes` 배열. quote별 필드는 `quote_id`, `symbol`, `record_sha256`, `session`, `basis`, `source_ref`, `source_sha256`, `reviewer_ref`, `reviewed_at`, `valid_from`, `valid_until`이다. 추가/중복/미등록 호가·잘못된 지문/시간은 거부한다.
- `basis`는 `external_event_history_review` 또는 `official_per_quote_status_review`만 지원한다. `session`은 `KRX_REGULAR_CONTINUOUS`, `AUCTION`, `VI`, `HALT`, `UNKNOWN` 중 하나다. 코드0/시계만으로 근거를 생성하지 않는다.
- 유효 구간은 진입/평가 호가의 수신~관측 구간을 포함해야 하고 동일 KST 날짜여야 한다. review 시각은 호가 관측 이후다. 근거의 수집 시점은 정책 사전 고정시각과 구분한다.
- 실제 원장 순서에서 기존 첫 진입 호가/900초 첫 bid 선택 규칙을 그대로 따른다. 별도 검토가 뒤의 좋은 호가를 지정해도 최초 호가를 대체하지 않는다. 누락 candidate/signal/order/quote는 검토 목록과 전체 분모에 남는다.
- 출력은 binding, 후보별 검토 필요 호가/지문/누락 이유, 기존 CLI에 넣을 `evaluation_inputs`, 원래 정책 결과,0/10/30bp 파생 결과와 지문, 자료 준비 상태다. 모든 결과 `production_eligible=false`, `account_return=null`이다.
- `scripts/prepare_entry_evaluation.py`: 명시 `--journal`, `--study`, `--max-journal-bytes`, 선택 `--session-review`만 읽고 stdout JSON을 출력한다. study/review는 각각4MiB 이하의 일반 파일이며 symlink/FIFO·중복 키·NaN을 거부한다. 네트워크·출력 파일 덮어쓰기 없음.

## Review Focus

- 정상 수집/CLI exit0와 실제 장 상태·수익성 확인을 혼동하지 않는가.
- 다른 원장/호가/기간/세대의 검토 근거가 섞이면 거부하는가.
- 최초 불량 호가·미관측 후보·비정규 세션·부족 근거를 그대로 보존하는가.
- 검토 시점이 늦어도 정책 고정시각을 소급하거나 원래 study를 바꾸지 않는가.
- 후속 연구의 날짜/분모/관측량 기준이 미래 수집 승인이나 계좌 손실 한도로 둔갑하지 않는가.

## Task 1 — 기존 분석 경로에 명시 검토 근거 연결

**Files:** 새 `src/analytics/entry_evaluation_bundle.py`, `scripts/prepare_entry_evaluation.py`, `tests/test_entry_evaluation_bundle.py`.

- [x] 합성 리허설 입력으로 근거 없음·적합한 근거·잘못된 binding/quote/hash/date·VI·첫 불량 호가·원본 불변을 테스트한다. 새 API 부재로 실패를 확인한다.
- [x] 순수 조립기와 명시 파일 CLI를 구현한다. 기존 분석기/운영 수집기는 수정하지 않는다.
- [x]0/10/30bp와 부분 모집단의 null 전체 손익을 확인한다. CLI study 불일치·symlink/FIFO·중복 키·파일 한도 거부를 확인한다.

## Task 2 — 결과를 보기 전 평가 기준 고정

**Files:** 새 `docs/research/current-engine-session-evaluation-2026-10-01.md`, 기존 최신 상태 문서.

- [x] 공식 소스/지문과 자동 근거 생성 불가의 한계를 기록한다. 실제 관측 시 사용할 명령과 검토 입력 예시를 제공한다.
- [x]10월2일 품질 pilot과 후속 가격 진단·미관측 확인 기간을 분리한다. 연구용 최소30쌍/10거래일은 검정력 보장이 아니며 현재 pilot은 충족할 수 없음을 밝힌다.
- [x] 후속 기간/독립 단위/비용 스트레스/누락/손실/기각·보류 조건을 고정하고, 후보 처분 근거·계좌 재생·계좌 절대 손실 한도 미확보 시 실제 투자 판정을 차단한다. 새 수집을 자동 예약하지 않는다.

## Task 3 — See와 통합

- [x] 독립 Sol/high 검토에서 입력 경계·원장 결합·계산/판정 과장을 확인한다. 실제 모델 메타데이터 미노출은 미검증으로 기록한다.
- [x] 관련/전체 테스트·비밀정보 검사·설치 제안 지문 보존을 검증하고 PR 통합 대상으로 준비한다. Git 통합 완료 여부는 해당 PR 상태를 따른다. 구현 완료와 실제 근거/수익 판정 미완료를 분리해 인계한다.

검증 결과: 신규35 passed, 전체3,082 passed / 2 xfailed(85.04초), 격리 위반0·비밀정보 검사 통과. 독립 코드/기준 재검토에서 지적 해소. 실제 장 상태 원천·통계 집계 코드·계좌 재생/승격 규약·절대 손실 한도는 이번 완료 범위가 아니며 연구 문서에 남겼다.
