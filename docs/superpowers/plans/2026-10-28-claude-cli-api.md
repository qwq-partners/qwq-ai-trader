# Claude 구독 CLI 우선·API 대체 구현 계획

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans. E04-D2의 후속이며 우선순위 정본은 실행 원장이다.

**Goal:** Gemini 신규 호출 없이 GPT의 독립 위험 검토를 로그인된 Claude CLI로 수행하고, 가용성 실패 때만 Anthropic API를 한 번 사용한다.

**Architecture:** provider는 `anthropic`, transport는 `claude_cli`/`anthropic_messages`로 구분한다. manager가 각 시도와 정산을 소유하며 구독 사용량을 API 청구액으로 바꾸지 않는다. 두 Bear 소비자를 함께 연결하고 실제 검토 부재를 승인으로 처리하지 않는다.

**Tech Stack:** Python asyncio subprocess/aiohttp, 기존 LLMResponse·예산 SQLite·판단/재현성 원장, pytest.

**Spec:** 10/28 사용자 선택: 로그인 CLI 1안, 제공한 Claude API 2안. 기존 [전환 계획](2026-10-28-openai-claude-routing.md)의 Task3을 구체화한다. API 키는 저장소 밖0600 파일에만 저장했다. 자격값은 문서/명령 출력/커밋에 기록하지 않는다.

## Global Constraints

- 모델은 양쪽 모두 검증 대상 `claude-opus-5-5`, 요청 effort `low`. 진입/팀 출력 상한400, 기존 전체12초/20초를 늘리지 않는다. CLI에 전체시간의 절반(최대8초)을 주고, 종료/회수 후 실제 남은 시간에만 API를 시도한다. 소비자는 외부 timeout보다0.5초 짧은 내부 시한을 전달한다.
- 신규 Gemini0. 일반 분석/보고 GPT 정책과 기존 매수 중지·전략·실주문·예약·운영 프로세스는 변경하지 않는다. 이 구현/검증은 운영 배포가 아니다.
- CLI는 인스턴스당1개·별도 빈 임시cwd·stdin 입력·safe mode·도구0·MCP0·hook/skill/plugin 비활성·세션 미보존·명시 모델·새 세션이다. 기존 로그인만 사용하고 API키/브로커/Telegram 환경을 상속하지 않는다. `--bare`, 권한 우회, 다른 사용자 세션 resume, OAuth 추출은 사용하지 않는다.
- CLI 입력64KiB·stdout1MiB·stderr64KiB 상한, 총시한에는 lock 대기/기동/정리가 포함된다. 종료/취소 때 소유한 process group만 종료하고 회수한다. CLI 내부 재시도0·1turn·max output400(호출 인자)·표시 비용0.25 상한. 숨은 thinking/stdout 원문을 로그에 저장하지 않는다.
- CLI의 terminal/assistant 실제모델·종료사유·도구/MCP/API인증 출처를 확인한다. 모델 불일치·명시적 거절·도구/인증 정책 위반은 API로 우회하지 않는다. 실패/없는 terminal/잘린 응답/가용성 오류는 불완료이며, 허용된 가용성 오류에만 API1회. 호출자 취소·manager 종료는 재전송하지 않는다.
- API는 공식 `https://api.anthropic.com/v1/messages`로만 보낸다. API키파일은 사용 시에만 읽고 symlink/잘못된권한·소유자/과대파일을 거부한다. 키를 argv·로그·응답오류에 넣지 않는다. SDK 자동 재시도·캐시 생성·tools는 사용하지 않는다.
- CLI는 `billing_channel=subscription`, 추정 list cost는 `subscription_estimated_cost_usd`, 실제 추가 청구는null이다. API 원장 예약·월USD100·80/95/100%·`log_and_continue`는 API 시도에만 적용한다. 구독 실패를 API 비용 미확인으로 오인하거나 API 견적을 구독 실제 청구로 기록하지 않는다.
- 검증·리뷰는 기본 외부호출/운영자격0. 실제 자격/작은 연결 확인은 coordinator만 담당하고 별도 실측으로 기록한다. workers는 키/운영파일에 접근하지 않는다.

## Review Focus

- CLI가 늦거나 취소돼도 자식이 남거나 API가 동시에 실행되지 않는가.
- 상속된 API키·설정으로 구독 요청이 유료 API로 바뀌지 않는가.
- CLI 비용/캐시 토큰과 API 예산 예약이 혼합되거나 이중 집계되지 않는가.
- 모델 불일치/거절/빈 응답/한도 소진이 단독 GPT 승인으로 바뀌지 않는가.
- API fallback/외부 timeout에서도 이미 완료된 상대 모델과 실제 시도 원장이 보존되는가.

### Task 1: 제한된 Claude 전송부

**Files:** 새 `src/utils/claude_transport.py`, `tests/test_claude_transport.py`.
**Interfaces:** `ClaudeCLIClient(config)`와 `AnthropicAPIClient(config)`는 `provider=LLMProvider.ANTHROPIC`, `transport`와 `billing_channel`을 가진다. `async complete(prompt, system='', *, model, max_tokens=400, reasoning_effort='low', deadline=None, **kwargs) -> LLMResponse`, `async close()`를 제공한다. llm 모듈의 타입을 사용하되 manager는 client를 생성 시 지연 import해 순환을 피한다. `_session`은 API 테스트용 기존 HTTP 주입 패턴을 따른다. 각 client는 한 전송만 담당하며 fallback/예산을 소유하지 않는다. 취소 예외에는 해당 전송의 `_llm_response`를 붙인다.

- [ ] argv/env·terminal/identity·usage/cache·무송신/timeout/cancel·실제 자식 회수·HTTP 오류/미확인 사용량/키파일 보호 RED.
- [ ] 위 경계 구현; API `output_config.effort=low`, `thinking.type=disabled`, 명시 max_tokens. 성공은 정확한 모델·end_turn·비어 있지 않은 text뿐.
- [ ] 관련 격리 검사·보고·독립 검토.

### Task 2: manager 순차 라우팅·비용 분리

**Files:** `src/utils/llm.py`, `src/utils/llm_pricing.py`, `src/analytics/llm_decision_record.py`, 설정 예제, 관련 테스트.
**Interfaces:** ANTHROPIC complete_with에서만 CLI→API, `call_timeout_seconds` 기본12·상한20. CLI최대min(8,total/2), API실제남은시한, 각 `_complete_attempt` 독립 lifecycle·trail. API는 기존예산게이트를 통과해야 하며 CLI는 API예산 소진과 독립이다. CLI의 billing channel은 client identity에서 정하며 응답이 임의로 비용 면제를 지정할 수 없다. Claude config keyfile 기본은 환경변수 또는 `~/.config/qwq-ai-trader/anthropic-api-key`(지연 읽기), API 직접key는 repr 제외. 모델명/CLI enable 값은 엄격히 검증한다.

- [x] 공통 필드/정확한 provider/API 가격상한 RED4→GREEN4, commit765594bf. API가격은Opus 입력4/출력20달러/MTok; 예산 견적은1시간 캐시 쓰기 상한입력8을 보수적으로 사용한다.
- [ ] CLI 성공 API0·CLI가용성실패 API1·API예산부족0·거절/불일치0·취소/정산·구독비용null·기간합산 RED→GREEN.
- [ ] from_env/config와 두 공급자 가정 제거, 종료·감사·판단 기록 연결. 기존 OpenAI/Gemini 역사/한도 검사 유지.

### Task 3: 두 Bear·판단 보류·재현성 원장

**Files:** `src/agents/researchers.py`, `src/agents/reproducibility.py`, `src/core/adversarial_validator.py`, `src/core/cross_validator.py`, 관련 테스트.
**Interfaces:** 두 Bear는 ANTHROPIC/low·retry_on_empty0·call_timeout_seconds=외부시한-0.5를 사용한다. 실제 response의 transport/billing_channel/attempt_trail/resolved_model을 원장에 보존한다. timeout에서도 완료한 sibling/취소된 호출의 trail을 기록한다. 새로운 Claude 경로에서 독립 검토 실패를 `independent_review_status=unavailable`로 명시하고 approved=False/failed=False로 단독 GPT fallback을 막는다. 기존 manager없는/레짐·점수 부적격 경로는 유지한다. 실 manager의 Claude 요구는 한도 소진에도 보류하며 legacy fake의 예산 단독 계약과 구분한다.

- [ ] Bull/Bear실패·일일한도·구독/API두시도·timeout sibling 보존·불명/거절 회귀 RED→GREEN.
- [ ] 둘 다 유효할 때만 판정 사용, 과거 원장 하위호환 유지.

### Task 4: 통합·실측·기록

- [ ] 관련/전체 verify·비밀 검사·독립 중요 리뷰·최종 CI·PR 통합.
- [ ] 외부 실제 연결은 짧은 합성자료만 사용하고 시각/모델/시간/토큰/과금 의미를 비공개 영수증에 기록한다. 일반 테스트와 구분한다.
- [ ] 원장·CHANGELOG·CLAUDE/연동 문서에 선택·구현·검증·실측·운영을 분리. 고정 SHA 적용은 별도 운영 단계다.

## 초기 확인

- API 모델 목록GET200, 목록에정확한Opus5.5 확인. 생성/매매 요청이 아니다.
- 로그인CLI2.1.310의 합성1turn: 실제Opus5.5/도구0/MCP0/apiKeySource none,4.524초. 표시list cost0.004808달러는 실제 추가 청구가 아니다. 대표 거래지연/성능 근거로 일반화하지 않는다.
- [공식 API](https://platform.claude.com/docs/en/api/messages/create), [effort](https://platform.claude.com/docs/en/build-with-claude/effort), [가격](https://platform.claude.com/docs/en/about-claude/pricing), [CLI](https://code.claude.com/docs/en/headless), [출력/재시도 환경변수](https://code.claude.com/docs/ko/env-vars).
