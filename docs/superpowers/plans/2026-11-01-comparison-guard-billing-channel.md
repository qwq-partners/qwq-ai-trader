# E04-GUARD-BILLING — 비교 호출 경계의 전송 채널 소유권

> 실행: `superpowers:subagent-driven-development`. 사용자 지시: PR #323 처리 뒤 지금 가능한 엔진 작업을 이어간다. 기존 설정·매수 중지는 유지하고 운영 배포·재시작·실주문·유료 비교 활성화는 하지 않는다.

**목적:** 앞으로 Claude CLI→API 전송을 비교 실행기에 연결할 때 구독 CLI 시도가 공유 월 원장을 예약·정산하거나 `unknown`으로 막는 일이 없도록, 시도 단위 입장·월 예약·정산의 소유권을 호출 경계(`ComparisonCallGuard`)에 명시한다. 실제 Claude 어댑터·worker 연결·R1 활성화는 이 작업에 포함하지 않는다.

**기준:** main `c022ef365b931afb76a1f3b9e3515052af52e499`(PR #323 병합, main CI 10,183 passed / 2 skipped / 2 xfailed). 11/1 17:03:16.662937 이상~17:42:10.256971 미만 KST의 `signal_events`는 0행·RR 문맥 0행(전체 최신 행 10/30 14:58:33)이라 E02-RR/E01 새 유효 입력은 WAIT다.

## 조사 결과 (정적 대조·합성 재현)

| 경계 | 현행 LLMManager(운영 entry Bear) | 비교 실행기(ComparisonCallGuard/SingleAttemptAdapter) |
|---|---|---|
| 구독 CLI 시도 | `_run_complete_attempt`가 `subscription = _client is self.claude_cli`이면 월 예약을 만들지 않는다(token None). 비용은 `subscription_estimated_cost_usd`·`billing_channel=subscription`으로만 집계 | 모든 입장 시도를 `ledger.reserve` 뒤 전송하고 `known/unknown/rejected`로 정산한다. 비과금 시도를 표현할 필드가 없다 |
| API 대체 시도 | CLI 정리 뒤 한 번만 별도 예약·정산 | 시도마다 예약·정산(동일) |
| LLMManager 감싸기 | — | `SingleAttemptAdapter`가 raw OpenAI/Gemini만 허용해 형식 검사로 차단. 감싸면 API 시도에 예약이 두 번 생긴다 |
| 비용 불명 | API 시도의 사용량 불명은 `unknown` 예약으로 유료 호출 차단(의도) | 같은 규칙. 구독 시도를 `actual_cost_usd=None`으로 넘기면 운영 호출까지 막는 `unknown`이 된다 |

따라서 재현 가능한 계약 차이는 **구독 전송을 표현할 수 없음**이다. Claude 어댑터가 없으므로 운영 결함이 아니라 연결 전 선행 조건이다.

## 설계 (최소 단위)

- `ComparisonRequest.billing_channel ∈ {api, subscription}` (기본 `api`). 요청 지문에 포함되므로 정책 `adapter_contract`를 `single-provider-attempt-v2`로 올려 구버전 정책이 저장된 원장은 `comparison_policy_mismatch`로 fail-closed 된다(운영 원장에는 비교 표가 없음: worker 미활성).
- `subscription` 요청: `_admit`으로 일 슬롯·지문을 고정하고 실험 유효기간·정책 단계(normal)만 읽기로 확인한다. 월 예약·80%·실험 상한 검사는 적용 대상이 아니다.
- 응답 정산 `subscription`은 `actual_cost_usd=None`만 허용한다. 구독 표시 추정비는 어댑터 value의 `subscription_estimated_cost_usd`로만 남기고 API 비용에 더하지 않는다.
- 요청 채널과 응답 정산이 다르면 `comparison_billing_channel_mismatch`로 거절한다. API 예약은 `unknown`으로 남고(과금 여부 불명), 구독 시도는 월 원장 없이 차단 ID만 남는다. 어느 쪽도 다른 채널로 바꿔 복구하지 않는다.
- `lookup`/`day_attempts`: 구독 결과는 예약 토큰 없이 `known`, `cost_usd=None`, `billing_channel`·`subscription_estimated_cost_usd` 노출. 토큰이 있는 구독 결과는 `comparison_settlement_mismatch`.
- 소유 범위: `src/analytics/model_comparison_execution.py`와 그 검사만. runner/adapter/worker/PairSpec·가격표·입장 정책·R1/Gemini 설정·주문·예약·배포는 변경하지 않는다.

## 순서와 수락 기준

- [x] RED/GREEN: 구독 시도의 월 예약 0·슬롯 소비·복구, 채널 불일치 3종, 구독 비용 보고 거절, API 요청의 구독 응답은 unknown 유지, 구독 전송 예외는 차단 ID, 유효기간/정책 단계 입장 검사, 요청 계약/직렬화 검사. 관련 9파일 398검사 PASS·격리 0(AWS 개발 worktree, 보조 검증).
- [x] 독립 리뷰: Opus5.5/high APPROVE(P2 4)·Codex Astra/high P1 1·P2 2 → 전부 반영(전송 전 채널 대조, 어댑터 선언/거절, 저장 채널 교차 검사, 복구 추정 범위, 일별 보고 구독 분리). 관련 462검사 PASS. 재리뷰는 원장 참조.
- [x] AWS 보조 전체 검증 1차(tree ce9b376c, 10,198 passed) · Codex 재리뷰 P1(미선언 콜백)·P2(차단 구독 집계) 반영 → 관련 463검사. 최종 tree의 재리뷰/AWS 전체/실제 WSL은 원장 참조.
- [ ] 실제 WSL 직렬 전체 검증. WSL 임시 접속은 사용자의 새 authorized_keys 등록이 필요하다.
- [ ] See: 원장·CHANGELOG·검토 문서 갱신, 커밋/푸시, PR. 운영 적용·실제 Claude 어댑터·유료 비교는 완료로 표시하지 않는다.

**보고 규칙(리뷰 반영):** 일별 API 지출 보고는 원장 `billing_channel=subscription`·`known` 시도를 `spend.subscription_attempts`에 표시 추정비와 함께 따로 적고 USD 합계·불명 집계에 넣지 않는다.
