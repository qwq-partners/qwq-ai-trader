# E04-CLAUDE-ADAPTER — Claude 단일 시도 어댑터 (구현·합성 검증, 운영 비활성)

> 사용자 지시: PR #324 처리 뒤 "새 과금 채널 소유권 위에 Claude 단일 시도 어댑터를 구현한다. 이번에는 구현과 합성 검증까지 하며 운영 활성화는 하지 않는다." 기존 설정·매수 중지·R1 OFF·Gemini 차단 유지. 운영 배포/재시작·실주문·실제 모델 시험 호출·유료 비교 활성화·관측 연장은 범위 밖.

**기준:** main `6261c323`(PR #324 병합, main CI run41299009048 10,200 passed / 2 skipped / 2 xfailed). 개발은 운영 checkout이 아닌 별도 worktree 브랜치에서 한다.

## 경계 확인 (정적 대조)

| 경계 | 현행 | 이번 결정 |
|---|---|---|
| `ComparisonRequest`/`ComparisonCallGuard` | `billing_channel` api/subscription, 어댑터 선언 채널을 전송 전 대조, 구독은 월 예약 0·일 슬롯/지문 소비 | 변경 최소: `temperature`에 None 허용(Claude 전송 미지원 필드 제외용). 정책 `adapter_contract` v2 유지 — 기존 요청 지문은 바뀌지 않는다 |
| `SingleAttemptAdapter`/`PairSpec` | raw OpenAI/Gemini만, Bear는 Gemini 고정, temperature 숫자 고정 | Bear만 Claude 허용(현행 entry Bear 표현), Bull 두 arm은 OpenAI, 명시 폴백 체인은 역사적 OpenAI/Gemini 그대로(Claude만 제외). `RoleControls.billing_channel`(기본 api)·`PairSpec.bear_empty_retries`(None=기존 의미, Claude Bear는 0 명시) 추가. API 어댑터는 Claude 모델·temperature None을 전송 전 거절 |
| `ClaudeCLIClient`/`AnthropicAPIClient` | 각각 단일 전송(CLI `CLAUDE_CODE_MAX_RETRIES=0`·`--max-turns 1`·자기 세션 그룹만 kill, API 단일 POST·aiohttp 재시도 없음), 신원/거절/불완전 출력 분류 내장, 지연 키 읽기 | 그대로 감싼다. 어댑터는 호출자 시한만 `deadline`으로 넘기고 늘리지 않는다 |
| `LLMManager` | CLI→API 대체·쿨다운·월 예약/정산 소유 | 감싸지 않는다(`isinstance` raw 전송 검사). CLI→API 대체 조정은 상위 흐름으로 분리 — 이번 범위 밖 |
| worker | `_raw_clients()`가 `provider_disabled`를 올려 활성화 거부 | 변경 없음. R1 OFF 유지 |

## 설계 (최소 단위)

- 새 `src/analytics/model_comparison_claude.py` `ClaudeSingleAttemptAdapter(client, clock)`: raw `ClaudeCLIClient`(subscription) 또는 `AnthropicAPIClient`(api)만 받고 `billing_channel`/`transport`를 전송 클래스에서 선언한다. 요청 채널 불일치·비Claude 모델·temperature/seed 지정·low 외 effort는 공급자 호출 전 `ValueError`. 한 번의 `__call__`은 `client.complete` 한 번이다.
- 정산: 구독은 `settlement=subscription`·`actual_cost_usd=None`·`cost_basis=subscription_not_api_billed`, CLI 표시 추정비는 value `subscription_estimated_cost_usd`로만 남긴다. API는 `SingleAttemptAdapter`와 같은 규칙(미전송 0·생성 전 거절 추론 0/released·신원 검증+사용량 확인 시 가격표·그 밖은 None→unknown 예약).
- 응답 상태: `success`는 전송 성공과 신원 `verified`가 함께일 때만. `transport_error_kind`·`refusal_detected`·`model_identity_status`·`request_sent`·`http_status`를 그대로 보존하고 `effective_reasoning_effort=None`으로 요청 effort와 구분한다. 취소는 원형 전파(가드가 `dispatch_entered` 상태로 남김).
- runner: `PairedComparisonRunner(claude_adapter=…)` 선택 주입, Claude 역할은 `_dispatch_for`로 그 어댑터에만 간다. 없으면 `claim`·슬롯 소비 전에 `claude_adapter_required`. 구독 시도는 `_sum_cost`에서 제외하고 arm 사유 `subscription_attempt_cost_not_api_billed`, 행 `subscription_attempts`(구독 시도가 있을 때만)로 구분한다(불명이 아니다).
- 리뷰 반영(복구 계약): 어댑터 value에 러너 예외 필드 `error`를 넣지 않는다(전송 실패는 `transport_error_kind`). 러너 `_failure`와 복구 `_verify_attempt`는 같은 규칙으로 미전송을 판정한다 — 슬롯 blocked·토큰 없음·가드 `dispatch_entered`가 True가 아닐 때(슬롯 blocked의 False, 슬롯 없는 일일 한도 차단의 None)만 0원, 구독 채널 실패는 `billing_channel`을 보존해 전송 불명/API 비용 해당 없음으로 남긴다. `PairSpec.snapshot()`은 Claude용 새 필드가 기본값이면 생략해 역사적 GPT/Gemini manifest·행을 바이트 동일하게 유지한다.

## 순서와 수락 기준

- [x] 합성 검사 먼저(`tests/test_model_comparison_claude.py`): 채널 불일치 전송 전 차단(어댑터·가드 양쪽), dispatch 1회·복구 시 재전송 0, CLI 경로 월 예약/정산 0, API 경로 가드 예약 1회·어댑터 단독 예약 0, 전달 제어(모델·effort·출력 상한·시한·temperature/seed 생략), 정상·신원 불일치·거절·불완전·timeout·미전송·취소·생성 전 거절, 실제 전송 클래스의 무자격 미전송, PairSpec Claude Bear 규칙·역사적 Gemini 쌍 보존, worker 활성화 거부.
- [x] 관련 기존 검사(runner/execution/admission/recovery/daily_report/worker/contract/input/e2e/claude_transport 등 19파일) 회귀 0 — 결과는 원장.
- [x] 독립 Codex 교차 리뷰 1회차(HEAD 6f75ac88, 요청 gpt-6-astra/high): P1 2·P2 2 → 전부 반영(아래). 재리뷰·최종은 원장.
- [ ] 실제 WSL 직렬 전체 `QWQ_VERIFY_PYTHON=/home/user/projects/qwq-ai-trader/venv/bin/python bash scripts/dev/verify.sh` PASS 뒤 커밋/푸시·PR. AWS 검사는 보조.
- [ ] See: 원장·CHANGELOG·검토 문서 갱신. 운영 적용·실제 호출·worker 연결·R1 활성화·순수익은 완료로 표시하지 않는다.
