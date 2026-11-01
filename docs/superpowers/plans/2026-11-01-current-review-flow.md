# E04-CURRENT-FLOW — 현행 독립 검토 판정·CLI→API 상위 비교 흐름 (구현·합성 검증, worker 비활성)

> 사용자 지시: E04-CLAUDE-ADAPTER(PR #325 병합, main `6e38a371`) 위에 "현행 GPT/Claude 독립 검토 정책에 맞는 상위 비교 흐름 구현, Claude CLI→API 대체를 명시적인 단일 시도들의 조합으로 처리, 필요한 worker 입력 검증·선택 주입 준비". 운영 배포/재시작·실제 모델 시험 호출·유료 비교 실행·worker/R1 활성화·실주문·관측 연장은 범위 밖. 설정·매수 중지·월 USD100·`log_and_continue`·Gemini 차단 유지.

**기준:** main `6e38a371`(PR #325, main CI run41310284898 10,250 passed / 2 skipped / 2 xfailed). 개발은 운영 checkout이 아닌 별도 worktree 브랜치 `feature/e04-current-flow-20261101`.

## 현행 계약 대조 (소스·오프라인 계약)

| 항목 | 운영 소스 | 오프라인 계약(`model_comparison_contract.POLICY/BEAR/FALLBACK`) | 이번 표현 |
|---|---|---|---|
| 판정 | `AdversarialValidator.validate`: `requires_independent_review`면 Bull/Bear 어느 한쪽 의견 없음 → `_review_unavailable`(hold, 단일 모델 fallback 요청 아님). 둘 다 있으면 `STRICT_ON_DISAGREEMENT=False` → 둘 중 하나라도 긍정이면 approve, 둘 다 부정이면 reject | `requires_independent_review True`·`missing_review_policy hold`·`single_model_fallback False`·`strict_on_disagreement False`; `policy_verdict`(의견만)와 `verdict`(근거까지, 미인증이면 hold) 분리 | `PairSpec.review_policy='entry-independent-review-v1'`: 양쪽 성공(신원 verified 포함) 의견이 있어야 approve/reject, 아니면 `hold`. `policy_verdict`와 `verdict`를 따로 적는다. 폴백 체인 금지·strict False 필수. 기본값 `legacy-single-opinion-v1`은 기존 의미 그대로 |
| 시한 | 패널 전체 `LLM_TIMEOUT=12.0`(역할 총 시한), Bear `call_timeout_seconds=11.5`(내부), CLI `min(8, 11.5/2)=5.75`, API는 11.5까지 남은 시간(`_deadline=deadline`) | `panel_timeout_seconds 12.0`, `bear_internal_timeout_seconds 11.5`, CLI 성공 5750ms 이전, API 시작·성공 11500ms 이전 | `bear.timeout_seconds`(역할 총), `bear_internal_timeout_seconds`(Bear 체인 예산, ≤역할 총), `bear_cli_timeout_seconds`(≤ min(8, 내부/2)), API는 `내부 − CLI 완료(정리 포함)까지 경과`만 전달하고 `bear_api_fallback.timeout_seconds`를 상한으로 더 줄인다. 체인 시작의 절대 시한을 어댑터 상한(`DEADLINE_AT`)으로 고정해 가드 입장 지연도 더해지지 않는다. 자동 연장 없음 |
| 대체 허용 | `complete_with`: CLI 결과가 `{cli_unavailable, cli_timeout, cli_rate_limited, cli_auth_unavailable, cli_incomplete}`이고 성공/거절이 아니며 시한이 남았을 때만 API 1회 | `FALLBACK` 동일 집합, `bear_transport_order [claude_cli, anthropic_messages]`, 두 번째 시도는 첫 시도가 error/timeout + 허용 종류일 때만 인가 | 러너 `_bear_role`: CLI attempt(`pair:bear:0`) 뒤 허용 전송 실패 종류이며 가드 예외/역할 시한/취소가 아니고 남은 시간이 있을 때만 API attempt(`pair:bear:1`). 거절·신원 불일치·정책 위반·가드 차단·취소·시한 소진은 API 0회 |
| 비용·소유권 | CLI 구독 시도 월 예약 0, API 대체만 예약·정산 | API 추정치·구독 표시 추정치·미상 과금 분리 | 각 시도는 기존 가드가 소유(어댑터 변경 없음). 두 시도 영수증 모두 보존, 최종 Bear 의견은 마지막 시도 1회만 계수 |

## 설계 (최소 단위)

- `PairSpec`: `review_policy`, `bear_internal_timeout_seconds`, `bear_cli_timeout_seconds`, `bear_api_fallback`(RoleControls, Claude·api 채널·low·temperature/seed None·같은 모델) 추가. 기본값은 스냅샷에서 생략해 역사적 manifest·행 바이트 동일. 프로필 규칙은 `PairSpec.validate_profile`로 분리해 worker init이 같은 검사를 쓴다.
- `PairedComparisonRunner(claude_adapter=…, claude_api_adapter=…)`: 채널별 Claude 어댑터. Bear 요청 채널에 맞는 어댑터가 없으면 claim 전 거절. `_bear_role`이 CLI→API 조합을 조정한다(어댑터 내부 retry/fallback 없음, 각 시도는 `guard.call` 1회).
- `_arm`: 현행 정책이면 `policy_verdict`/`verdict`/`approval_basis='entry_independent_review_v1'`/`review_policy`를 적고 hold는 sizing 재생을 하지 않는다(`unavailable_policy='hold_blocks_entry'`). 레거시 arm 형식은 바꾸지 않는다. API 대체가 시간 소진으로 생략되면 `bear_api_fallback_skipped_time_exhausted`, 인가되지 않은 실패면 `bear_api_fallback_not_authorized`.
- `report_pairs` 요약에 `hold` 계수 추가(역사적 행은 0).
- worker: manifest의 `review_policy`·`bear_empty_retries`·`bear_internal_timeout_seconds`·`bear_cli_timeout_seconds`·`bear_api_fallback`을 init에서 `validate_profile`로 검증하고 `_spec`에 전달한다. 현행 프로필이면 봉투 `role_controls`의 `bear_empty_retries`·`bear_internal_timeout_seconds`·`bear_transport_order`도 대조한다. `_raw_clients()`·`install_from_env` 차단은 그대로(실제 Claude 클라이언트 생성 경로 없음).

## 순서와 수락 기준

- [x] 합성 검사 먼저(`tests/test_model_comparison_current_flow.py`, 90검사 중 새 파일 41): 현행 정책 hold/합의/불일치, CLI 성공 시 API 0, 허용 실패에서만 API 1회, 금지 실패·가드 차단·취소·시한 소진 뒤 API 0, API 남은 시한 전달·시도별 시간 분리, 구독 월 원장 0·API 예약 1, 공유 Bear 의견 1회·시도별 비용 보존, 저장 실패→복구 재계산 일치, 역사적 스냅샷·행 보존, worker 프로필 검증·합성 주입·활성화 거부.
- [x] 관련 20파일 회귀 0(772 passed, AWS 보조) · 독립 Codex 교차 리뷰(요청 gpt-6-astra/high) 1~6회차 P1 6·P2 4 전부 반영 → 7회차(56459e82) **P0/P1/P2 0**.
- [x] AWS 보조 직렬 전체(HEAD 15b53483): 10,303 passed / 2 skipped / 2 xfailed·rc0·격리0 — 사용자 WSL을 대신하지 않는다.
- [x] 실제 WSL 직렬 전체(HEAD a83be570/tree 447be582, bundle 전달·미푸시): 10,303 passed / 2 skipped / 2 xfailed·475.6초·rc0·격리0 → 커밋/푸시·PR(필수 verify는 PR 영수증).
- [x] See: 원장·CHANGELOG·검토 문서 갱신. 운영 worker 연결·실제 호출·유료 비교·순수익은 완료로 표시하지 않는다.
