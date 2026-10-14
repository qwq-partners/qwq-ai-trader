# 기존 REST 응답의 장 상태 근거 보존 계획

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans 또는 superpowers:subagent-driven-development. 단계별 검증을 기록한다.

**Goal:** 추가 증권 API 요청 없이 기존 스크리닝 현재가 응답의 상태 원문과 논리 호출 구간을 보존하고, 판단 뒤 첫 호가에 대한 자료 가용성을 보고한다.
**Architecture:** `get_quote`의 제한된 부가 메타데이터 → 명시 opt-in한 기존 `rest_quote` 기록 → strict 원장 → 별도 REST 진단. WS 상태 분류·경제성 게이트와 분리한다.
**Tech Stack:** 기존 Python/asyncio/pytest, 새 의존성 없음.
**Spec:** [파일럿 이후 근거 수집 계약](../../research/market-state-pilot-readout-2026-10-14.md). 현재 요청은 이 후속 구현 진행이며 운영 활성화가 아니다.

## 범위와 계약

- Base main `597150da590d7d68c530800367d4f9d00061b72f`, feature 브랜치/별도 작업트리. 운영·설정·매수 중지·예약·월 $100 원장 변경 없음.
- REST 공식 공개 스키마 SHA256 `cb8da9262a405b865f2f3d61261f76bfbd16c053a4cf72e1abe47108940fbbe4`: 상태4필드·응답종목을 보존한다. N/00을 정상으로 판정하지 않는다.
- 시각은 `_api_get` 논리 호출 시작/반환(리미터·토큰갱신·재시도 포함), HTTP 전송/거래소 as-of가 아니다. 거래소 시각·사건 순번은 null.
- 스냅샷은 허용 필드만 담고 지문을 붙인다. 지문은 원천 인증이 아니다. 전체 응답·계좌·자격증명 저장 금지.
- `runner-llm-window-v1`의 `rest_market_state_evidence: true`를 명시한 연구에서만 영속 보존. 기존 rest_quote 행과 other 자원 몫을 사용하므로 새 구독·태스크·행은 만들지 않는다.
- 최신 결측/손상·종목 불일치·시각 역전/동률·오래됨·미검증/불완전 원장은 raw 가용으로 세지 않는다. WS 연결 세대를 REST에 복사하지 않는다.

## Task 1 — 순수 스냅샷·진단

담당 Astra/high 작업자, 파일 `src/analytics/kis_rest_market_state.py`, `tests/test_kis_rest_market_state.py`만 소유. `build_snapshot`, `validate_snapshot`, `associate_snapshot`, `SNAPSHOT_FIELDS` 제공. 상태는 항상 UNKNOWN, `usable_for_market_state=False`.
- [x] 실패 회귀 → 최소 구현 → 관련 테스트·문법·독립 검토.

## Task 2 — 기존 호출·원장·보고 연결

코디네이터 소유: `kis_kr.py`, `entry_observation.py`, `entry_observation_runtime.py`, `entry_observation_journal.py`, `decision_quote_join.py`, `report_decision_quotes.py`, 새 통합 시험.
- [x] 브로커 반환에 제한 스냅샷 추가(부가 기록 실패는 기존 가격 반환에 영향 없음), 추가 HTTP 요청 없음.
- [x] opt-in 계약을 설치·재생에서 대조하고 원장의 nested 필드 허용 목록에 연결. 기존 연구 결과 형태 보존.
- [x] 응답 뒤 첫 호가 표에 별도 REST 진단·가용/결측 집계 추가. 기존 evaluable·WS·순손익 판정 불변.
- [x] 실제 브로커 함수→명시 런타임→봉인/strict reader→보고 CLI 합성 통합 시험.

## Task 3 — SEE·문서·통합

- [x] UTC 전체 verify, 필요한 시간대 회귀, 비밀정보/diff 검사. 새 source/test 변경이 없으면 전체 반복 금지.
- [x] 독립 critical 최종 리뷰, 근거와 남은 실측/설치 작업을 연구 문서·CHANGELOG·CLAUDE·인덱스에 기록.
- [ ] 커밋·푸시·PR·해당 head CI 확인 후 통합 판단. 운영 배포/새 예약은 미수행.

## Review Focus

스냅샷 생성 오류가 가격 반환을 깨뜨리는지, opt-in 없는 과거 연구의 변경, 해시만 믿고 시각/종목을 승격하는지, 최신 손상 자료를 건너뛰는지, raw 가용을 정상시장/경제성으로 오해하는지를 시험·리뷰한다.

## 검증 확정

최종1538d29 전체6122/관련244/KST107검사(rc0), 실제 Claude Opus5/xhigh 종결 APPROVE·P0/P1 0. 세부 근거/한계/비차단 의견은 [PDS 정본](../../research/rest-market-state-evidence-2026-10-14.md)을 따른다. PR/CI와 병합은 푸시 후 정본에 연결한다.
