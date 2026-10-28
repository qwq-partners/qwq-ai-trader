# GPT·Claude 엔진 전환 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. 아래 완료 표와 실행 원장을 함께 갱신한다.

**Goal:** 사용자 지시대로 신규 Gemini 호출을 중단하고 일반 분석은 GPT, 독립 위험 검토는 Claude로 배분한다.

**Architecture:** 공통 task/고정 역할과 별도 Bull/Bear·비교 도구의 호출 경로를 함께 관리한다. 과거 Gemini 기록·가격표는 대사용으로 보존하며 신규 호출 권한을 주지 않는다. Claude API와 구독 CLI는 다른 인증·과금 계약이므로 선택 전 전송부를 확정하지 않는다.

**Tech Stack:** Python asyncio, 기존 OpenAI client, SQLite 예산/판단 원장, pytest.

**Spec:** 2026-10-28 사용자 지시 “Gemni 사용하지 말고 gpt와 claude를 사용하자”; 정본 [execution-roadmap.md](../../operations/execution-roadmap.md), E04-D2.

## Global Constraints

- 일반 light는 `gpt-5-mini/minimal`, heavy는 기존 `gpt-5.6-sol`; 별도 복기 역할 `gpt-6.1-sol` 유지. 명시한 추론 제어는 보존하고 light 설정 변경 시 minimal 미지원·low 지원이면 기본 low를 사용한다.
- 월 USD100, `missing_cost_policy=log_and_continue`, 기존 비용·예약·불명 비용 이력 보존.
- Claude가 없거나 실패하면 독립 검토 미완료. GPT를 Claude로 표시하거나 Gemini로 복귀하지 않는다.
- 기존 매수 중지·실주문·운영 override·예약·비공개 자료를 변경하지 않는다. 코드 병합과 실행 중인 운영 전환을 구분한다.
- 로컬 검증은 외부 API·운영 자격증명 없이 수행한다. 코드 변경은 독립 리뷰·관련/전체 검사·비밀 검사 후 통합한다.
- Claude 연결 방식은 사용자 답변 대기(API 자격 없음, 구독 CLI 로그인만 확인된 상태). 답변 전에는 독립적인 GPT 역할 전환과 경로 차단 준비만 수행한다.

## Review Focus

- 과거 설정 또는 직접 비교 client가 Gemini 호출을 다시 활성화하지 않는가.
- 일반 GPT 실패 시 동일 모델 중복 재시도·유료 자동 승격이 생기지 않는가.
- Claude 부재/시간 초과/할당량 부족을 정상 독립 검토나 USD0으로 오인하지 않는가.
- provider별 출력·추론·종료·거절·취소·비용 메타데이터를 정확히 보존하는가.
- 고정 역할 판독기가 과거 Gemini 기록을 현재 GPT 전환의 성공 근거로 인정하지 않는가.

### Task 1: 고정 보고 역할과 판독 계약 전환

**Files:** `src/utils/llm_roles.py`, `scripts/ops/report_premarket_check.py`, 관련 역할/판독 테스트.

**Interfaces:** `ROLE_POLICY_VERSION='engine-roles-v2-openai'`; REPORT_ONE_LINE/MORNING_BRIEF는 `openai/gpt-5-mini`, `reasoning_effort='minimal'`, 기존 출력·입력·시한 유지. POST_EXIT_RESEARCH 유지. 현재 판독은 v2/OpenAI/stop를 검증하며 v1/Gemini 기록을 v2 성공으로 세지 않는다.

- [x] 기대 프로필·현행/과거 판독을 검사하는 실패 테스트를 추가/수정하고 RED 확인.
- [x] 프로필·판독 계약과 필요한 fixture만 변경. 반복 호출·유료 시험·운영 artifact 교체 없음.
- [x] 관련 검사를 통과시키고 diff·독립 리뷰 후 작업 브랜치에 통합. main/운영 반영은 Task4에서 별도 확인.

### Task 2: 공통 GPT 라우팅과 신규 Gemini 전송 차단

**Files:** `src/utils/llm.py`, `config/default.yml`, `.env.example`, `scripts/run_trader.py`, 비교 worker 경계와 관련 테스트.

**Interfaces:** 일반 `complete()` task primary는 OpenAI; light는 fallback 없음, heavy 실패 시 별도 모델인 OpenAI light 1회만. 기존 `complete_json()`의 형식 복구와 명시적 `complete_with(retry_on_empty=...)`의 빈 응답 복구는 유지하며 전체 API의 무조건1회 상한을 뜻하지 않는다. 고정 보고 역할은 한 번만 호출한다. 명시적 독립 provider 경로는 cross-provider fallback 없음. 역사 Gemini 가격/원장을 보존하고 신규 Gemini 전송은 client 경계에서도 미전송으로 차단한다. Claude 연결은 별도 의존 작업으로 둔다.

- [x] stale 설정·일반 task·직접 Gemini client·비교 activation의 신규 Gemini HTTP0 회귀 작성.
- [x] 공통 라우팅/명시적 provider lookup/차단 계약 구현. 새로운 유료 상향 폴백 없음.
- [x] 예산·attempt trail·refusal·deadline·historical ledger 관련 검사 통과. 전체에서 발견한 기존 Gemini 활성 전송 전제4개를 별도 테스트 수정으로 정비했고, 관련132검사·격리0을 확인했다. 최종 전체CI는 Task4의 별도 게이트다.
- [x] coordinator: `src/core/adversarial_validator.py`에서 `provider_error_code=provider_disabled`를 명시적 차단으로 처리. peer timeout 뒤에도 판정을 유지하고 단일 GPT fallback으로 우회하지 않는다. 독립 리뷰P1의 일일 한도 우회도 수정해 호출 전에 정책을 확인한다. 통합 관련131·독립 재검증12통과, 미해결P0/P1/P2 0.

### Task 3: Claude 독립 위험 검토 연결

**Files:** 선택한 transport client, `src/agents/researchers.py`, `src/core/adversarial_validator.py`, 예산/가격·메타데이터 및 테스트.

**Dependency:** API 또는 구독 CLI에 대한 사용자 선택. 인증 자격값은 문서/채팅/커밋에 넣지 않는다.

- [ ] 선택한 transport의 모델·인증·과금·시한/동시성·출력·취소 계약을 이 문서에 구체화한다.
- [ ] 정상/미인증/시간 초과/거절/잘린 출력/미확인 비용/취소/실제 모델 불일치 회귀를 먼저 작성한다.
- [ ] 두 Bear 역할을 Claude로 연결하고 미완료 상태를 보존한다. 신규 BUY의 독립 검토 요구와 단일 GPT fallback 표시를 함께 검증한다.

### Task 4: 통합 검증·운영 적용 기록

- [x] Phase A 독립 중요 소스 리뷰·일일 한도 우회 수정 재리뷰 승인. 마지막 테스트 전용4파일은 별도 한정 재검토한다.
- [ ] Phase A 최종 관련 검사·전체CI·비밀 검사 후 PR #283 통합. 정확한 최종 HEAD의 검증·병합 영수증은 해당 PR에서 확인한다.
- [x] 원장·CHANGELOG·관련 운영 문서에 Phase A와 미완료 Claude/운영 단계를 구분했다.
- [ ] Claude 연결 구현 후 전체 GPT·Claude 계약에 대한 독립 중요 리뷰·관련/전체 검증.
- [ ] 허용된 실행창에서 고정 SHA 적용, 신규 Gemini0·GPT/Claude 실제 identity·예산·기존 예약 대조. 적용 전까지 운영 미전환으로 명시한다.

## 근거

- 소스 경로 감사: main `cfdf4910`, 요청 Sol/high(실제 모델/effort metadata 미노출). 보고서 `/tmp/qwq-gpt-claude-route-inventory-20261028.md`.
- [GPT-5 mini 공식 모델](https://developers.openai.com/api/docs/models/gpt-5-mini): 현재 API 모델 ID와 Chat Completions 지원 확인. 기존 정상 모델을 유지하며 신규 모델 품질 향상을 주장하지 않는다.
- [Claude 요금](https://platform.claude.com/docs/en/about-claude/pricing), [구독 SDK/CLI 설명](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan): API 과금과 로그인된 구독 한도를 구분한다. API 단가를 CLI 실제 지출로 대입하지 않는다.

## 진행 기록

- 11:34 KST: 작업 branch `feature/openai-claude-routing-20261028`, base `cfdf4910`. 운영 `c4bc4f0e` 미변경. Claude 연결 질문 대기 중이며 Task1부터 수행.
- PR [#283](https://github.com/qwq-partners/qwq-ai-trader/pull/283)에 Gemini 차단/GPT 전환 단계만 제출한다. Claude 연결·운영 적용을 완료로 승격하지 않는다.
- 첫 완료된 로컬 전체 검증은4실패/9,288통과/2skip/2xfail·격리0이었다. 이4개를 새 계약으로 정비했다. 원래 age 검증·예산 단독 장애·전송별 원장·402 억제/미확인 비용 보존 검증은 유지하고, 사용 중지 정책이 월/일 한도에 우선한다는 통합 검사를 추가했다. 제품 코드 추가 수정 없이 관련132검사·격리0을 확인했으며 최종 PR 전체CI를 통합 게이트로 사용한다. 실패 실행이나 중간 중단 실행을 전체 성공으로 인용하지 않는다.

- 최종 통합 브랜치에서 위4개와 정책 차단·비교 worker·고정 역할·장전 판독을 함께 실행해 **201passed/23.47초·격리0**을 확인했다. 최종 전체 검증은 동일 코드의 PR CI로 수행한다.
