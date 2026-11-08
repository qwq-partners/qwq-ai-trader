# L01-LLM-CREDIT-ROUTING — Ultra 월 API 크레딧 기준 엔진 LLM 라우팅 재설계 계획

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans. 우선순위 정본은 [실행 원장](../../operations/execution-roadmap.md#l01-llm-credit-routing-20261108)이다. 이 문서는 **계획서이며 승인 전이다.** 어느 Task도 아직 시작하지 않았다.

**Goal:** 매월 소멸하는 Ultra 플랜 Claude API 크레딧 USD200을 엔진의 LLM 호출에 우선 배정해 OpenAI 유료 청구(현재 월 약 USD30 추세)를 0에 가깝게 줄이고, 그 과정에서 비용 출처가 바뀌어도 판단 품질·fail-closed 동작·예산 원장의 정확성을 유지한다.

**Architecture:** 공급자 선택은 (1) 호출 빈도, (2) 비용 출처(크레딧 API / 구독 CLI / OpenAI 유료), (3) 실패 시 안전 동작의 세 축으로 결정한다. 고빈도 경량 역할은 가장 싼 **API** 모델, 저빈도 고가치 역할은 Claude API(크레딧), 구독 CLI는 크레딧이 덮지 않으므로 **대체 경로로 강등**한다. 월 예산 원장은 공급자를 구분하지 않으므로 상한을 크레딧 한도에 맞추되 소진 시 유료 호출 중단(기존 100% 정책)을 그대로 쓴다.

**Tech Stack:** 기존 `LLMManager`(`src/utils/llm.py`), `claude_transport.py`(CLI/API 전송), `llm_roles.py`(고정 역할 프로필), `llm_pricing.py`, 월 예산 SQLite(`llm_budget.py`), KR 스케줄러 테마 루프, pytest.

**Spec:** 11/8 사용자 지시 "1순위 Claude, 2순위 OpenAI, 깊은 추론은 Fable"에 대해 크레딧 조건을 공식 문서로 확인한 뒤 작성했다. 공식 조건([도움말](https://support.claude.com/en/articles/17154008-monthly-api-credits-for-max-ultra-and-team-plans)·[플랫폼 문서](https://platform.claude.com/docs/en/about-claude/api-credits-for-subscribers)): Ultra USD200/결제 주기, 이월 없음, 구매 크레딧보다 먼저 소진, Claude API·Batches·Playground·Managed Agents·Agent SDK에 적용, **대화형 Claude Code·앱 추가 사용량·Bedrock/Vertex/Foundry 미적용**, 소진 시 다른 크레딧·자동 충전이 없으면 다음 주기까지 API 요청 중단. 11/8 콘솔 잔액 USD197.52(만료 12/4) = 11월 Bear API 호출분 USD2.48 차감 후.

## 근거(11/8 읽기 전용 판독, 월 예산 원장 `~/.local/state/qwq-ai-trader/llm-budget.sqlite3`)

| 11/1~11/8 정산 | 모델 | 호출 | USD | 비고 |
|---|---|---|---|---|
| 테마 탐지·bull·quick_* | gpt-5-mini | 1,479 | 4.15 | 테마 탐지만 140회/일, **24시간·주말 포함**(30분 주기×주기당 약 3호출) |
| 웹 데일리 리포트 | gpt-6-astra | 15 | 3.06 | 거래일 3회, 회당 약 0.20 |
| 시장 조사 보고 | gpt-6.1-sol | 18 | 0.55 | 거래일 3회 |
| 주간 복기·진화 | gpt-5.6-sol | 17 | 0.49 | |
| **OpenAI 소계** | | | **8.24** | 거래일 약 1.25/일, 주말 약 0.50/일 → 월 약 30 |
| 독립 Bear(API 대체) | claude-opus-5-5 | 198 | 3.15 | 구독 CLI 성공 39건 vs API 대체 198건(84%) → 전부 크레딧 차감 |

- 10/29~11/8 OpenAI 원장 추정 합 약 USD10.0 ↔ 같은 기간 OpenAI 자동 충전 2회 USD11.18. 원장 추정은 추론 토큰을 일부 과소계상하나 청구를 설명한다.
- Codex 독립 리뷰는 ChatGPT 구독 토큰 인증(`~/.codex/auth.json`에 API 키 없음)이라 OpenAI API 청구와 무관하다.
- 현재 Bear 전송 순서 `["claude_cli", "anthropic_messages"]`(CLI 시한 min(8, 총/2), 허용 가용성 실패에만 API 1회)는 **크레딧 기준으로 거꾸로**다. CLI 성공분은 개발 세션과 같은 Claude Code 한도를 쓰고, API 대체분이 오히려 무료였다.
- 코드 제약: `LLMConfig.__post_init__`이 `claude_model != "claude-opus-5-5"`를 거부하고, `llm_pricing.py`에 Claude는 `claude-opus-5-5`(4/20)만 있다. `complete_role`은 `_get_client(provider)`·`_validate_controls`·종료 사유(`stop`/`STOP`)에 OpenAI/Gemini 가정이 남아 있고 Anthropic 전송은 `response_format=json_object`가 없다. 월 예산 `monthly_budget`를 바꾸면 `_settings`가 `budget_configuration_mismatch`로 **fail-closed** 한다(명시 이관 절차 필요).
- 보수 추정(Opus 5.5 4/20, 현재 호출량·토큰 가정, 실측 후 갱신): Bear 약 12/월, 웹 리포트 Opus 약 23/월(Fable 10/50이면 약 58), 시장 보고·복기 약 5/월, 경량 역할 Opus 약 45/월(Haiku 5.5 0.10/0.50이면 약 1/월, T1 시간대 제한 뒤 기준). 전부 Claude API로 옮겨도 월 약 85(경량 Haiku면 약 41, 웹 Fable이면 +35) ≈ **크레딧 200 이내**. 수치는 추정이며 정확도·순수익 개선의 증거가 아니다.

## Global Constraints

- 매수 중지(`KILL_SWITCH_KR`)·배분·전략·실주문·예약·운영 프로세스·`evolved_overrides` 미커밋 변경·R1 OFF·worker OFF·Gemini 차단은 변경하지 않는다. 모델 변경은 정확도나 순수익 개선의 증거가 아니다.
- 각 Task는 별도 PR·실제 WSL 직렬 전체 `verify.sh`·독립 리뷰(돈 경로/판단 경로는 `gpt-6-astra`/xhigh 또는 `claude-opus-5-5`/high, 작성자와 분리) 뒤 사용자 승인으로만 배포한다. 문서만의 변경은 재배포하지 않는다.
- 실호출은 **신원 검증 1회/모델**(T3)만 사용자 승인 아래 허용한다. 시험 발송·슬롯 재실행·재생성·관측 확장은 하지 않는다. 자격값(API 키 파일·CLI 로그인)은 문서·커밋·리뷰 입력에 넣지 않는다.
- 비용 출처가 크레딧이어도 월 예산 원장은 USD 추정으로 계속 기록한다. 크레딧 소진·만료는 외부 사실이므로 원장이 추정할 수 없고, API 4xx(크레딧 없음)는 기존 실패 경로(판단 hold·대체 본문·자동 유료 승격 없음)로 흡수돼야 한다. 구독 CLI 비용을 API 청구로, 크레딧 차감을 구독 사용으로 기록하지 않는다.
- 독립 Bear의 계약(양쪽 유효 의견 없으면 hold, 단일 모델 폴백 없음, `review_policy=entry-independent-review-v1`)은 전송 순서가 바뀌어도 유지한다. 비교 계약(`model_comparison_contract.py`)의 역사적 manifest(`bear_transport_order`)는 과거 영수증 해석용으로 보존하고 새 순서는 새 manifest 버전으로 추가한다.
- Fable 5.1 API는 thinking 상시 켜짐·`output_config.effort`로만 깊이 제어·입력 10/출력 50 USD/MTok·**조직 30일 데이터 보존 설정 요구**·응답 시간이 길다. 개발 에이전트 라우팅 정책의 Fable 제외(과거 refusal fallback 실측)는 이 계획과 별개로 유지한다.

## Review Focus

- Bear API 우선으로 바꾼 뒤 CLI 대체가 API 시한을 침범하거나, API 거절/불일치/빈 응답이 CLI로 "우회"되어 단독 GPT 승인으로 바뀌지 않는가.
- 크레딧 소진·만료(12/4)와 원장의 KST 월초 복귀가 어긋나는 구간(매월 1일~결제일)에서 API 4xx가 hold/대체 본문으로 끝나고 무한 재시도·유료 승격이 없는가.
- `monthly_budget` 변경이 기존 원장 상태(사용액·예약·unknown)를 지우지 않고 상한만 바꾸는가. `budget_configuration_mismatch` fail-closed가 우회되지 않는가.
- 모델별 신원 검증(`resolved_model`)·가격표·`_validate_controls`가 새 모델에도 적용되는가. JSON 역할(웹 리포트·시장 보고)이 Anthropic 경로에서 같은 검증기를 통과하는가.
- 테마 탐지 게이트가 거래일 판정(`is_kr_market_holiday` = 주말+KIS 동적∪fallback)을 쓰고, 장중 다른 소비자(스크리너 뉴스 심리·보고 입력)의 입력 결손을 '0 심리'로 바꾸지 않는가.

## 사전 결정(사용자 11/8 확정)

| ID | 결정 | 사용자 결정(11/8) | 비고 |
|---|---|---|---|
| D1 | Bear 전송 순서 | **API 우선, CLI 대체**(API 가용성 실패에만 1회) | 추천안 채택 |
| D2 | 월 예산 상한 | **Task 4 배포 뒤 100 → 200** 명시 이관 | 추천안 채택 |
| D3 | 웹 리포트 모델 | **처음부터 Fable 5.1** | 추천안(Opus 1단계) 대신 선택. 조직 30일 데이터 보존 설정·180초 시한 재검토 선행 |
| D4 | 경량 역할 | **Opus 5.5로 바로 이전**(Haiku 검증 생략) | 비용 약 5배 수용. 단 **Bull은 D8 결정 전까지 gpt-5-mini 유지**(아래) |
| D5 | heavy·시장 조사 보고 | **Fable 5.1**(리포트와 통일) | 90초 시한 재검토 선행 |
| D6 | OpenAI 자동 충전 | **계속 유지** | 잔여 OpenAI 호출(Bull 등) 안전망 |
| D7 | Anthropic 자동 충전 | **OFF 유지** | 크레딧 소진 시 API 중단 → 기존 실패 경로 |
| D8 | Bull 모델(신규 쟁점) | **미결** — 추천: gpt-5-mini 유지 | D4대로 Bull까지 Opus로 옮기면 Bull/Bear가 같은 모델이 되어 `entry-independent-review-v1`의 교차 공급자 독립성이 사라진다. 대안: Bull만 Haiku 5.5(검증 필요) |

**확정 결정 기준 보수 비용 추정(월, USD, 실측 전 가정):** 웹 리포트 Fable 약 58 · 시장 조사 보고 Fable 약 23 · 주간 복기/진화 Fable 약 5 · 경량 Opus(Task 1 게이트 뒤, Bull 제외) 약 40 · Bear Opus 약 12 → **합계 약 138**(Task 1 없이 경량 약 70이면 약 168). 크레딧 200 이내이나 80% 절약(160)·95% 필수 전용(190) 문턱에 근접할 수 있고, Fable 출력은 thinking 토큰을 포함해 과금되므로 실제는 추정의 1.5~2배까지 벌어질 수 있다. 초과 시 기존 100% 유료 중단 정책이 작동하고 D7 OFF라 과금은 0이다. 잔여 OpenAI는 Bull(gpt-5-mini) 월 약 1~2.

## Task 순서(각각 별도 PR, 앞 Task 배포 없이 다음 Task 착수 금지)

### Task 1: 테마 탐지 거래일·장 시간 게이트 (공급자 불변, 즉시 효과)

**Files:** `src/schedulers/kr_scheduler.py`(`run_theme_detection`), `src/signals/sentiment/kr_theme_detector.py`(필요 시 게이트 헬퍼), 테스트 `tests/test_kr_scheduler_theme_gate.py`(신규), `docs/architecture/system-overview.md`·CHANGELOG.
**Interfaces:** 거래일(`is_kr_market_holiday` False)의 07:00~16:00 KST에만 `detect_themes(force=True)` 호출. 창 밖에서는 다음 창 시작까지 대기(주입 시계·`asyncio.sleep` 상한 유지). 소비자(`get_all_stock_sentiments`·`get_recent_themes`·`/api/themes`)는 마지막 성공 결과와 `observed_at`을 그대로 제공하며 결손을 0 심리로 바꾸지 않는다(T03-THEME-SUPPLY-MISSING 원칙).

- [ ] RED: 토요일 03:00·거래일 02:00·거래일 10:00·휴장일 10:00 네 시점에서 호출 0/0/1/0, 창 밖 대기 시간이 다음 창 시작과 일치, 기존 소비자 결과 보존.
- [ ] GREEN·관련 검사·CHANGELOG·문서.
- [ ] 기대 효과: 140회/일(매일) → 거래일 약 55회/주말 0, OpenAI 월 약 USD10 절감. 배포 1주 뒤 원장으로 실측.

### Task 2: Bear 전송 순서 반전 (D1)

**Files:** `src/utils/llm.py`(ANTHROPIC `complete_with` 순서·시한 배분), `config/default.yml`(새 키 `claude_transport_order`, 기본 `[anthropic_messages, claude_cli]`, 엄격 검증), `src/analytics/model_comparison_contract.py`/`model_comparison_input.py`/`model_comparison_worker.py`(새 manifest 버전, 역사적 순서 보존), `src/analytics/llm_decision_record.py`(attempt_trail 순서 표기), 관련 테스트·`docs/research/engine-model-allocation-2026-10-15.md`·CHANGELOG.
**Interfaces:** API에 총 시한의 전부(기존 12초 기본·20초 상한 불변)를 주되 API가 허용 가용성 실패(`api_unavailable`/HTTP 5xx·429·크레딧 없음 4xx·시한)로 끝나고 남은 시한이 CLI 최소 기동 시간 이상일 때만 CLI 1회. API 거절·모델 불일치·빈 응답은 CLI로 우회하지 않는다(현 CLI→API 규칙의 거울). 월 원장 예약은 API 시도에만, CLI는 `billing_channel=subscription` 그대로.

- [ ] RED: API 성공 → CLI 0, API 5xx/크레딧 4xx → CLI 1, API 거절 → CLI 0·hold, 남은 시한 부족 → CLI 0, 비교 contract가 두 순서 manifest를 모두 해석.
- [ ] GREEN·독립 리뷰(판단 경로: xhigh)·실제 WSL 전체.
- [ ] 기대 효과: Bear 의견 확보율 상승(API 지연 중앙값 약 4.4초 실측), 개발 세션 Claude Code 한도 사용 0, 크레딧 차감 월 약 USD12.

### Task 3: Claude 모델 신원·가격 확장 (D3·D4·D5 전제)

**Files:** `src/utils/llm.py`(`claude_model` 허용 집합·역할별 모델), `src/utils/claude_transport.py`(모델별 `resolved_model` 검증·Fable `output_config.effort`·`stop_reason=refusal`/`stop_details`), `src/utils/llm_pricing.py`(Fable 5.1 입력 10/출력 50 USD/MTok·캐시 읽기, Opus 유지), `_validate_controls`, 테스트.
**Interfaces:** 허용 모델 `{claude-opus-5-5, claude-fable-5-1}`(D4로 Haiku 제외, D8이 Haiku를 택하면 추가); 요청 모델과 응답 `model`이 다르면 실패(기존 규칙). Fable은 `thinking` 미지정(상시)·`output_config.effort`만 전달, `stop_reason=refusal`은 실패로 기록하고 다른 모델로 우회하지 않는다. 역할별 시한: 웹 리포트 180초·시장 보고/복기 90초가 Fable 응답 시간에 충분한지 신원 검증 호출의 실측 지연으로 판단하고, 부족하면 시한 상향을 별도 결정으로 올린다(자동 상향 금지).

- [ ] RED/GREEN: 가격 계산·모델 검증·refusal 처리·Fable 파라미터 형태.
- [ ] **사용자 선행 확인:** Anthropic 콘솔의 조직 데이터 보존 설정이 30일 이상인지(Fable은 미만이면 400 `invalid_request_error`). 미충족이면 D3·D5는 Opus 5.5로 되돌리고 기록한다.
- [ ] **실호출 신원 검증 1회/모델**(사용자 승인, 크레딧 차감 수 센트): `resolved_model`·usage·지연·rate-limit 헤더 기록. 거절/불일치가 나면 그 모델은 라우팅에서 제외하고 기록한다(정책: 우회 금지).

### Task 4: 역할 재배정 (D3·D4·D5)

**Files:** `src/utils/llm_roles.py`(`ROLE_POLICY_VERSION` 승격·`WEB_REPORT`/`MARKET_BRIEF`/`POST_EXIT_RESEARCH` provider `anthropic`), `config/default.yml`(heavy/light 슬롯 provider·모델), `src/utils/llm.py`(`complete_role`의 Anthropic 분기·종료 사유·JSON 지시: Anthropic은 `response_format`이 없으므로 system에 JSON 전용 출력 지시 + 기존 파서/검증기 그대로), `src/analytics/web_report.py` 등 소비자는 불변, 테스트, `docs/agents/reporting.md`·`CLAUDE.md` LLM 표·CHANGELOG.
**Interfaces(11/8 확정):** `WEB_REPORT`·`MARKET_BRIEF`·`POST_EXIT_RESEARCH`·heavy 슬롯(`trade_review`/`strategy_analysis`/`market_analysis`) → `claude-fable-5-1`(effort medium, 출력 상한 유지, 시한은 Task 3 실측 뒤 결정). light 슬롯(테마 탐지·quick_*·wiki·news) → `claude-opus-5-5`(effort low). **Bull은 D8 확정 전까지 `gpt-5-mini` 유지**(Bear와 다른 공급자 보존). `REPORT_ONE_LINE`은 호출량이 크지 않아 Opus 5.5. 모닝 브리프는 결정론 조립이라 불변. 신원 검증(Task 3)에 실패한 모델은 그 역할을 이전하지 않고 현행 유지.

- [ ] RED/GREEN: 역할별 provider·모델·상한·JSON 검증 통과·실패 시 대체 본문/hold 경로 불변.
- [ ] 독립 리뷰·실제 WSL 전체·PR·사용자 승인 배포. 배포 뒤 첫 자연 슬롯(07:40 web morning 등)에서 `report_analyses.status`와 검증 거절 코드로 인수(수락 보장 아님).

### Task 5: 월 예산 상한 이관·소진 동작 (D2·D7)

**Files:** `src/utils/llm_budget.py`(상한 변경을 명시 이관 명령으로만 허용: `settings.limit_units` 갱신 + 변경 영수증, 사용액·예약·unknown 보존), `scripts/ops/llm_budget_migrate.py`(신규, 봇 정지 중만·`hold_or_exit`), `config/default.yml` `monthly_budget: 200.0`, `docs/operations/runbook.md`·CHANGELOG.
**Interfaces:** 80%=160·95%=190·100%=200 USD 추정. 크레딧 주기(결제일~)와 원장 주기(KST 월초)의 어긋남은 수용하되 문서화: 매월 1일~결제일 사이에 크레딧이 남아 있지 않으면 API 4xx → 기존 실패 경로. Anthropic 자동 충전 OFF 유지로 초과 과금 0.

- [ ] RED: 이관 없이 설정만 바꾸면 기존대로 `budget_configuration_mismatch`, 이관 후 사용액/예약 행 수 동일·상한만 변경.
- [ ] GREEN·독립 리뷰·운영 적용(배포 창에서 봇 정지→이관→기동, 영수증).

### Task 6: 사후 측정 (배포 7일 뒤, 읽기 전용)

- [ ] 원장 집계: 공급자·모델·역할별 일 비용, Bear API/CLI 성공률, 웹/시장 보고 `ready` 비율(수락과 구분), 콘솔 크레딧 잔액 ↔ 원장 추정 대조, OpenAI 청구 0 여부.
- [ ] 결과로 D8(Bull 모델)·Fable 유지 여부·시한 조정을 결정. 80% 절약 문턱 도달 여부와 Fable 실제 토큰(thinking 포함)을 추정과 대조한다. 모델 우위·순수익 주장은 하지 않는다.

## 진행 기록

- 2026-11-08: 사용자 "계획서 작성해". 11/8 읽기 전용 판독과 공식 크레딧 조건으로 본 계획 작성. 코드·설정·운영 변경 0. 다음 진입점은 사전 결정 D1~D7 확인 뒤 Task 1 착수.
- 2026-11-08: 사용자 D1~D7 확정(D1 API 우선·D2 이전 뒤 200·D3 웹 Fable·D4 경량 Opus·D5 heavy/시장 보고 Fable·D6 OpenAI 충전 유지·D7 Anthropic 충전 OFF). 새 쟁점 D8(Bull 모델) 미결, 추천 gpt-5-mini 유지. Task 1 착수는 D8 답변과 무관하므로 사용자 지시 시 바로 가능.
