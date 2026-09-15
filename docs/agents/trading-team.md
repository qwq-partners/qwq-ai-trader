# 종목 단위 에이전트 팀 (`src/agents/`)

> 2026-08-02 신설. TradingAgents(TauricResearch) 구조 참고 + harness 아키텍처 패턴으로 정리.

## 왜 만들었나

기존 `src/experts/`의 도메인 전문가 8명은 **시장·섹터 레벨**만 판단한다
(ExpertOpinion: regime_bias, affected_sectors). 개별 종목에 대해
"이걸 사도 되는가", "보유분을 계속 들고 갈 것인가"를 팀으로 논의하는 계층이 없었다.

## 두 층의 구분 (혼동 주의)

| 층 | 위치 | 실행 시점 | 역할 |
|---|---|---|---|
| 개발·운영 보조 | `.claude/agents/` (13개) | Claude Code 세션 | 코드리뷰·분석 지원 |
| **런타임 매매** | **`src/experts/` + `src/agents/`** | **장중 자동** | **실제 매매 판단** |

harness 플러그인은 전자를 생성하는 도구다. 이 문서는 **후자**를 다룬다.

## 파이프라인

```
[전문가 풀]   도메인 전문가 8명 → 시장·섹터 컨텍스트        (src/experts, 재사용)
      ↓
[팬아웃/팬인] Analyst 3인 병렬                              LLM 미사용
      ↓
[생성-검증]   Bull/Bear 2라운드 토론                        LLM 2~4회
      ↓
[감독자]      Trader 종합 → 제안(방향+사이징)               LLM 미사용, 결정론적
      ↓
[생성-검증]   Risk 게이트(cross_validator 11규칙) → PM 승인
      ↓
TeamVerdict → ~/.cache/ai_trader/team_verdicts/ → 대시보드
```

### 왜 Analyst와 Trader는 LLM을 안 쓰나

지표 계산과 점수 합산은 답이 정해진 일이다. 확률적 모델을 넣으면
같은 입력에 다른 출력이 나와 백테스트·감사·회귀 테스트가 불가능해진다.
창의적 판단이 필요한 지점(반대 논거 발굴)에만 LLM을 쓴다.

## 구성 요소

| 파일 | 역할 |
|---|---|
| `types.py` | AnalystReport / DebateResult / TradeProposal / PMDecision / TeamVerdict |
| `analysts.py` | Fundamental(dart+validator) / Technical(indicators) / News(news_curator) |
| `researchers.py` | Bull(OpenAI) vs Bear(Gemini) 2라운드 토론 |
| `trader.py` | 종합 점수 → BUY/HOLD/SELL + 사이징 배수 |
| `portfolio_manager.py` | 종목별 최종 승인/거부 |
| **`allocator.py`** | **후보 전체를 한 번에 배분 (포트폴리오 제약)** |
| `team.py` | 오케스트레이션, 동시 심의 제한, 결과 저장 |

## 포트폴리오 배분기 (`allocator.py`, 2026-08-03)

종목별 심의는 서로를 보지 못한다. 각 종목이 개별적으로 안전해도 전부 주문하면
포트폴리오는 안전하지 않다. 그래서 **심의 이후, 주문 이전**에 전체를 한 번에 보는 계층을 둔다.

> ⚠️ `cross_validator`의 섹터 규칙은 **이미 보유 중인** 포지션만 센다.
> 같은 배치에서 동시에 승인된 후보들끼리는 서로를 보지 못한다 — 그게 이 계층이 필요한 이유다.

**적용 제약** (전부 기존 `RiskConfig` 재사용 — 여기서 숫자를 새로 정의하지 않는다)

| 제약 | 출처 |
|---|---|
| 최대 포지션 수 | `max_positions` (8) |
| 섹터당 최대 | `max_positions_per_sector` (2) — **동시 승인분 포함** |
| 단일 종목 상한 | `max_position_pct` (28%) |
| 기본 배정 비율 | `base_position_pct` (25%) × 사이징 배수 |
| 가용 현금 | `RiskManager._get_available_cash()` (예비금 제외) |
| 일일 신규 매수 | `max_daily_new_buys` (5) |
| 최소 주문 금액 | `min_position_value` (20만원) |

**동작**: 확신도 높은 순으로 하나씩 배정하며, 배정할 때마다 누적 상태(섹터 카운트·현금·슬롯)를
즉시 갱신한다(원자적 적용). **allocator 거부는 오버라이드할 수 없다.**

실측 예 — 반도체 4 + 바이오 1 동시 승인 시:
```
✅ 삼성전자(반도체) 2,500,000원
✅ SK하이닉스(반도체) 2,500,000원
✅ 셀트리온(바이오) 2,500,000원
⛔ 한미반도체 — 섹터 집중 한도 (반도체 2/2) — 동시 승인분 포함
⛔ DB하이텍   — 섹터 집중 한도 (반도체 2/2) — 동시 승인분 포함
```

## 토론 판정 규칙 (중요)

| 상황 | consensus | confidence | 의미 |
|---|---|---|---|
| 양측 일치 | True/False | 1.0 | 만장일치 |
| 의견 분열 | None | 0.5 | 불확실 — Trader가 -10 감점 |
| **단독 반대** | **False** | 0.5 | 존중 (안전 쪽) |
| **단독 긍정** | **None** | 0.3 | **합의로 승격하지 않음** |
| 양측 무응답 | — | 0.0 | failed → fail-open |

> ⚠️ **단독 응답을 합의로 취급하면 안 된다.**
> Bear는 "실패 시나리오를 찾아라"는 역할이라, Bull이 죽고 Bear만 남으면
> 구조적으로 반대 편향이 된다. 반대로 Bear의 ACCEPT는 "감수할 만한 리스크"라는
> 뜻이지 매수 추천이 아니다. 초기 구현이 이 둘을 혼동해 실측에서 오판이 나왔다.

## PM 오버라이드 정책

> 🚫 **2026-08-03 기본 비활성화** (`allow_pm_override: false`).
> 게이트 유효성은 실측됐지만(차단 신호 20영업일 -3.7%~-13.2%),
> "LLM 만장일치가 그 성과를 역전한다"는 증거가 없다. 게다가 만장일치면
> `conviction 0.9`가 자동 부여돼 `MIN_CONVICTION(0.75)`이 자동 충족되므로
> 조건이 걸림돌 역할을 못 했다. shadow 표본으로 우위가 확인된 뒤 게이트별로 열 것.

아래는 활성화했을 때의 조건이다. 다음을 **모두** 만족해야 한다.

- 토론 만장일치 지지 (confidence 1.0)
- Trader 확신도 ≥ 0.75
- 차단 게이트가 `SOFT_GATES`에 속함
- 일일 한도(2회) 미소진

**절대 오버라이드 불가 (`HARD_GATES`)**
```
킬스위치 / 일일 손실 한도 / 현금·예산 부족 / 중복 보유 / exit_exempt
```
계좌 생존과 직결된다. "오늘은 확신이 있으니 손실 한도를 넘겨보자"가
계좌를 끝내는 전형적인 경로다.

오버라이드 시 사이징을 ×0.7로 낮추고, 감사 원장 기록 + 텔레그램 알림을 남긴다.

## LLM 재현성 원장 (`reproducibility.py`, 2026-08-03)

토론 결과는 Trader 점수를 `+20/-40` 바꾸고 매수 여부를 가른다. 그런데 LLM은 같은 입력에도
다른 답을 낼 수 있다. 기록이 없으면 **"그날 왜 샀나"를 사후에 설명할 수 없고**,
모델 교체 전후를 같은 전략으로 비교할 수도 없다.

**남기는 것** (`~/.cache/ai_trader/llm_ledger/llm_YYYYMMDD.jsonl`, append-only)

| 필드 | 용도 |
|---|---|
| `prompt` / `response` | 전문 (요약본으로는 재실행 비교 불가) |
| `prompt_hash` | 재실행 시 **입력이 동일한지** 문자열 비교 없이 확인 |
| `model` / `provider` | **실제 응답 모델** — 폴백으로 요청과 달라질 수 있다 |
| `params` | max_tokens, reasoning_effort, weight |
| `input_snapshot_hash` | 분석가 보고서 스냅샷 (나이는 제외 — 매번 변해 비교 불가) |
| `verdict` / `latency_ms` | 판정과 지연 |

`DebateTurn`에도 `model`/`provider`를 실어 verdict 파일만 봐도 어느 모델이 판단했는지 안다.

**재현성 측정** — `LLMLedger.agreement_rate()`가 `prompt_hash`로 묶어
동일 입력의 판정 일치율을 계산한다. 입력이 다르면 판정이 달라도 비재현이 아니므로 제외한다.

### 재현성 확보 (2026-08-03)

첫 실측은 **일치율 50%**였다. 원인이 둘이었고 각각 다르게 해결했다.

| 원인 | 증상 | 해결 |
|---|---|---|
| 빈 응답 | `success=True`인데 `content=''` (6회 중 1회) | `reasoning_effort="minimal"` + 빈 응답 1회 재시도 |
| **샘플링 비결정성** | 같은 입력에 판정이 뒤집힘 | **`seed` 고정** |

`gpt-5` 계열은 **temperature 커스텀이 막혀 있어** seed 말고는 판정을 고정할 방법이 없다.
실측: seed 없이 6회 → 1회 반전 / `seed=42` → 6회 전부 일치.
Gemini는 `temperature=0.0`으로 고정했다.

> ✅ **최종 실측: 일치율 100%** (동일 입력 6회, 응답 문구까지 동일). 승격 기준 충족.
> 상수: `DEBATE_SEED` / `REASONING_EFFORT` / `EMPTY_RETRY` (researchers.py)

## 데이터 신선도 (2026-08-02 추가)

에이전트가 **오래된 데이터를 현재 정보로 착각하는 것**이 가장 위험하다.
캐시는 성능상 필요하지만, 캐시된 값과 방금 계산한 값을 같은 무게로 합치면
종합 판단이 과거를 반영하게 된다.

### 소스별 캐시 수명

| 소스 | TTL | 팀이 가정하는 나이 |
|---|---|---|
| 전문가 의견 (`ExpertOpinion`) | 6~24시간 | `issued_at` 실측, **만료분은 제외** |
| 수급/공매도 (`stock_validator`) | 30분 | 보수적으로 15분 |
| 트렌드 버즈 | 2시간 | — |
| 종목 뉴스 sentiment | 1시간 | 보수적으로 30분 |
| 기술 지표 (스크리너) | 5분 주기 | `_last_screened_at` 실측 |

### 처리 방식

1. **만료 의견 제외** — `orchestrator.snapshot()`은 `cached()`를 그대로 주므로
   **만료 여부를 걸러주지 않는다**(`ExpertAgent.cached()` 주석에 "만료 무관"이라 명시).
   `_market_context()`가 `is_valid`로 필터링하고, 전부 만료면 컨텍스트를 아예 주지 않는다.
2. **hard TTL + 최소 근거량 (2026-08-03 추가)** — 감쇠만으로는 부족하다.
   `aggregate_score`가 가중평균이라 **모든 근거가 함께 낡으면 감쇠가 상쇄된다**
   (실측: 전부 신선 +62 / 전부 4시간 전 +62 — 동일했다).
   → TTL 초과분은 집계에서 제외하고, `evidence_quality()`로 최소 근거량을 검사해
   미달이면 신규 매수를 막는다.
3. **신선도 가중치 감쇠** — 반감기 60분 지수 감쇠 (30분 0.57배 / 2시간 0.25배).
   TTL 안쪽에서의 상대 비중 조정용이다.
4. **프롬프트에 나이 명시** — 토론 컨텍스트에 각 근거의 나이를 붙이고
   "오래된 근거는 할인해서 판단하라"고 지시한다. 나이를 감추면 모델은 전부 현재로 취급한다.
5. **스크리닝 시각 전달** — `bot._last_screened_at`을 `indicators_as_of`로 넘겨
   지표 나이를 실측한다.

> **축적이 자산인 것은 감쇠시키지 않는다.** `trade_memory`(L1→L2→L3)와
> `trade_wiki`의 거래 교훈은 오래됐다고 가치가 떨어지지 않는다 — 오히려 표본이 쌓일수록
> 신뢰도가 오른다. 감쇠는 **시황성 데이터**(시세·수급·뉴스·레짐)에만 적용한다.

## 안전장치

- **exit_exempt 종목의 SELL 제안은 PM이 무효화** — 자동매도 금지(예: 087010 펩트론)는
  팀 판단보다 우선한다. 수동 판단 전용.
- **토론 실패 시 신규 매수 차단(fail-closed)** — 반대 논거 검증이 이뤄지지 않은 상태이므로.
  보유 종목 판단에는 계속 사용한다.
- **근거 부족 시 신규 매수 차단** — 소스별 hard TTL(지표 45분 / 수급·뉴스 180분) 초과분은
  집계에서 제외하고, 유효 소스 <2개이거나 감쇠 후 가중치 합 <0.5면 매수 금지.
- **게이트 조회 실패 시** fail-closed (미분류 게이트는 보수적 거부).
- **동시 심의 3건 제한** — LLM rate limit 보호.
- **결과 저장은 Lock + 원자적 교체** — 동시 심의가 서로 덮어쓰지 않도록.

## 대시보드

`/engine` 페이지의 "에이전트 팀 심의" 카드.

| API | 내용 |
|---|---|
| `GET /api/team/verdicts?limit=&approved=` | 오늘 심의 목록 |
| `GET /api/team/stats` | 합의율·입장변경률·오버라이드 잔여 |

## 실행 (2026-08-02~)

`kr_scheduler.run_team_deliberation` — 장중 **10:30 / 11:30 / 13:00 / 14:00** 4슬롯(2026-08-03 2→4 확대, `SLOTS`).

| 대상 | 범위 |
|---|---|
| 매수 후보 | 최근 스크리닝 상위 5 (`bot._last_screened`) |
| 보유 종목 | 전체 재평가 (`portfolio.positions`) |

> ⚠️ **shadow 단계 — 주문을 내지 않는다.**
> 심의 결과를 저장·알림만 한다. 팀은 2026-08-02 신설이라 실전 데이터가 없고,
> 첫 통합 테스트에서 P0 결함이 3건 나왔다. 규칙 #11을 shadow_mode로 시작했던 것과
> 같은 방식으로, 며칠 관측해 판정 품질을 확인한 뒤 주문 경로 연결을 결정한다.
>
> **부분 승격 (2026-08-20)**: 팀은 여전히 주문을 생성·차단하지 못하지만,
> **BUY 승인 + conviction ≥0.75/0.90 verdicts는 엔진이 생성한 기존 매수 주문의
> 금액을 ×1.10/×1.20 부스트**한다 (`src/utils/team_conviction.py`,
> 개별 포지션 상한·전략 배분 잔여 한도는 재클램프). HOLD/REJECT는 사이징에
> 반영하지 않음 — CF 실측(47건)상 HOLD 차단 후보가 5일 평균 +6.31%로
> 차단 방향은 손해로 판명. `TEAM_CONVICTION=0`으로 비활성화.

설정: `config/default.yml` → `kr.trading_team`
```yaml
kr:
  trading_team:
    enabled: true
    debate_rounds: 2
    allow_pm_override: false   # 2026-08-03 기본 비활성
    max_concurrent: 3
```

## shadow 관측 리포트 (2026-08-03~)

```bash
python scripts/shadow_report.py              # 오늘
python scripts/shadow_report.py --days 7     # 최근 7일 누적
python scripts/shadow_report.py --telegram   # 텔레그램 전송
```

승격 기준 달성도를 숫자로 보여준다 — 심의 건수·판정 분포·보류 사유·토론 합의·
**재현성 일치율**·모델 사용 현황.

**자동 알림 (crontab)**

| 주기 | 내용 |
|---|---|
| 평일 16:00 | 일일 요약 (장 마감 15:30 + 14:00 심의 이후) |
| 일요일 09:00 | 주간 누적 (`--days 7`, 승격 기준 점검) |

로그: `~/.cache/ai_trader/shadow_report.log`

## shadow → 실주문 승격 기준 (2026-08-03 명문화)

"며칠 관측"은 기준이 아니다. 아래를 **모두** 충족하고 운영자가 명시 승인해야 승격한다.
자동 승격은 금지한다.

| 항목 | 기준 |
|---|---|
| 표본 수 | 독립 심의 **200건 이상**, 레짐별(bull/neutral/bear) 각 30건 이상 |
| shadow P&L | 체결 가능가 + 수수료·슬리피지 반영 후 **양(+)** |
| 증분 효과 | 기존 결정론적 경로 대비 수익·MDD·turnover 개선 |
| 게이트 대비 | 통과/차단/오버라이드별 20영업일 사후 성과 비교 |
| 재현성 | 동일 입력 재실행 시 **판정 일치율 80% 이상** |
| 장애 안전성 | stale·LLM 장애·공급자 폴백 상황에서 **주문 0건** 증명 |
| 포트폴리오 | 동일 섹터 동시 후보 포함 스트레스 테스트 통과 |

## T11 — 근거 계약·판단 v2(shadow)·조건부 진입계획·불변 원장 (2026-09-15)

> 계획서 `docs/superpowers/plans/2026-09-15-agent-team-evidence-entryplan.md`. 상태: **구현 완료(shadow)**, 투자 성능 검증 미완, 운영 승격 없음. 팀 BUY 는 여전히 실주문에 연결되지 않는다.

### 근거 계약 (`types.EvidenceItem`, `AnalystReport` 확장)
- 근거 1건마다 출처·원자료 식별자(`ref_id`)·관측 시각(`observed_at`, 모르면 None — 수집 시각·now 로 채우지 않는다)·수집 시각·회계기간·상태(full/partial/insufficient/error)·종류(fact/interpretation/assumption)·유효기간·`dedup_key`(같은 기사·공시 재인용은 1회만).
- 보고서는 `positive_basis`(긍정 근거 확인)와 `risk_clear`(검증 수행 + 위험 미발견)를 분리한다. 펀더멘털 "검증 통과" 는 risk_clear 일 뿐 긍정 근거가 아니다. 뉴스는 `limitations=["헤드라인 기반"]`.
- 2026-09-15 교차 리뷰 후속: `validation_pass_bonus`는 기존 점수의 검증 통과 가산분(0/10)을 `risk_clear`와 독립적으로 보존한다. DART 경고 뒤 `risk_clear=False`가 되어도 v2에서 +10은 취소한다. 구버전 `None`은 기존 risk_clear 기반 추정으로 호환하므로 과거의 불명확한 점수 성분까지 복원했다는 뜻은 아니다. 자료 시각 미상은 `data_as_of=null, age_minutes=null`로 표준 JSON에 보존한다.
- `stock_validator.ValidationResult.validated/data_status` — 하위 검증(수급·공매도·DART·뉴스)의 실제 획득 여부에서 유도. 기존 `approved` 의 의미·기본값은 그대로(다른 소비자 무영향).
- 기존 경로 버그 수정 2건(승인): `vol_ratio` 소비 키(거래량 +15 가 이전엔 절대 미발동), `evidence_quality` 의 유효 소스 수에서 confidence=0 보고서 제외. 기준선 특성화 테스트 `tests/test_t11_evidence_baseline.py`.

### 판단 v2 (`judgment.assess` → `TeamAssessment`, 플래그 `TEAM_ASSESSMENT_V2`, 기본 "1"=shadow)
| 필드 | 의미 |
|---|---|
| `merit_score/merit_status` | 매수 매력 — evidence 기반(usable fact, dedup 1회, confidence=0·만료·error 제외, risk_clear 단독 +10 미가산) |
| `risk_acceptable` | Bear 최종 ACCEPT=True / REJECT=False / None=기권 — **매수 매력에 가산하지 않는다** |
| `data_sufficiency` | full / partial / insufficient (유효 보고서 기준) |
| `entry_ready` + `entry_check` | EntryPlan shadow 검증 allow=True, wait/reject=False, 계획 없음=None |
| `consensus_level` | unanimous / split / one_sided / failed — 합의 수준일 뿐 확률이 아니다 |
| `success_probability` | 항상 None, `calibration_status="uncalibrated"` (예측 사건·기간·외부 검증 없음) |
| `independent_votes / final_votes / change_reasons` | R1 독립 표 보존, R2 변경 시 `변경사유: 새근거|이전해석오류 — …` 파싱(없으면 unrecorded) |
| `stance_v2` | buy_candidate = merit sufficient ∧ risk_acceptable ∧ data ≥ partial ∧ entry_ready; 아니면 hold/abstain |
- 채택 해석(통합 확정): 펀더멘털 '검증 통과' +10 은 positive_basis 유무와 무관하게 **항상 merit 에서 취소**한다(긍정 근거는 score 의 다른 항목으로 이미 반영).
- 기존 `TradeProposal/PMDecision/conviction`·`team_conviction_multiplier` 산식은 **기준선으로 불변**(`tests/test_t11_judgment_baseline.py`). 화면의 conviction 은 "합의 기반 지표·확률 미보정" 으로 표기.
- 재현성 원장 params 에 `reasoning_effort` 실제값·`seed`·`temperature`·`prompt_version="debate-v2-2026-09-15"`.

### 조건부 진입계획 (EntryPlan = `PendingSignal` 확장, `execution/entry_plan.check_entry_plan`, 플래그 `ENTRY_PLAN_SHADOW`, 기본 "1"=shadow)
- 정본은 PendingSignal 하나(`plan_id/setup/decided_at/inputs_ref/trigger/invalidation/required_inputs/assumptions/exit_policy_ref`). 배치 변환이 `Signal.metadata["entry_plan"]` 로 실어 주문 직전까지 조건이 유실되지 않는다.
- `check_entry_plan` → `PlanCheck(allow|wait|reject, 사유 코드)`: 만료·가격 상한(wait)·밴드 하한·트리거(VCP 돌파)·setup 별 필수 입력(gap_vwap 의 vwap 없으면 `INPUT_MISSING:vwap`)·급락(severe → reject, crash 는 기존 SEPA 차단 미러)·무효화·위험예산/슬롯/일일 한도(문맥 있을 때만)·비용 반영 손익비(`COST_RR_LOW` 는 기록만, 컷오프 미정).
- 엔진은 Order 생성 직전에 **기록만**(`signal_events` event_type=`shadow_plan_check`). 허용/차단하지 않는다. shadow 행은 `get_stats` 의 total_buy/block_rate 분모·`/api/signal-events` 기본 조회·SSE 실시간 피드에서 제외되며 `type=shadow_plan_check` 로만 조회한다(계측 오염 방지). 시장가 주문이 상한을 보장한다고 주장하지 않는다 — 지정가 도입은 모의 연구(E)까지.
- 팀 심의는 후보 dict 의 `entry_plan/current_price/quote_as_of/intraday_level` 로 같은 함수를 호출해 `entry_ready` 를 낸다. 후보 현재가는 스크리닝 시점 값이라 5분 경과 시 `QUOTE_STALE`(wait)이 잦다(별도 재조회는 미배선).
- 명시적으로 주입한 `now`와 만료·호가 시각 비교는 naive=KST, aware=실제 순간으로 정규화한다. `checked_at`/`quote_as_of` 감사 출력은 원래 표현을 유지한다. 만료 `now > expires_at`, 호가 5분 경계·shadow 전용 지위는 불변이다. 분석가/보고서의 now 생략 시 host-local 호환 경로와는 별도 계약이다.

### 불변 원장·실행 상태 (`team_ledger.py`)
- `~/.cache/ai_trader/team_ledger/deliberations_YYYYMMDD.jsonl` append-only, `deliberation_id = sha256(symbol|date|slot|input_snapshot_hash)[:16]` — 같은 입력 재시도는 같은 id(읽기 dedup). 같은 날 같은 종목의 여러 시점 판단·BUY/HOLD/REJECT/기권/실패 전부 보존. 기존 `team_verdicts/verdicts_*.json`(latest-per-symbol)은 대시보드·conviction 호환용으로 유지.
- 실행 상태: `candidate → waiting_trigger | plan_rejected → shadow_ready → order_submitted → filled`. 뒤 두 단계는 trade_journal 등 실제 증거가 있을 때만 표시(승인 BUY 를 체결로 가정하지 않는다).
- CF: 승인 BUY 중 체결 증거 없는 건은 `team_buy_unfilled` 로 추적(요약에서 '차단 적중' 프레이밍과 분리). 체결 증거는 콜백 우선, 콜백 미주입이면 실제 거래저널 `<TRADE_JOURNAL_DIR|~/.cache/ai_trader/journal>/trades_YYYYMMDD.json` 을 읽는다(날짜 파일 없음 = 그날 진입 없음 = 미체결). 저널 디렉터리 자체가 없으면 판정 불가로 **등록 보류** + 요약 경고(미체결로 오라벨하지 않음).
- CF 입력은 날짜별 **불변 심의 원장 우선**(2026-09-15 교차 리뷰 후속). 같은 날 오전 BUY→오후 HOLD도 BUY 집단으로 분류하며 모든 `deliberation_ids`를 보존한다. 종목·날짜당 종가 기준 1표본으로 기존 분모를 유지하고, 기존 HOLD의 BUY 재분류 시 가격·rN은 보존한다. 새 체결 증거가 확인되면 미체결 표본에서 제거하고 변경만 있어도 저장한다. **체결 판정 불가 때는 기존 행을 `fill_evidence_unknown=True`로 보존**하되 평가 분모·가격 갱신·보존기간 정리에서 제외한다. 새 판정 불가 BUY는 등록 보류한다. 미체결 확인으로 복구되면 제외 상태만 해제하고 원 측정값·ID를 재사용한다. 원 표본일 봉이 없으면 뒤 날짜를 진입일로 대체하지 않으며, 벤치마크도 정확한 기준일이 없으면 초과수익을 만들지 않는다. 원장 파일이 **없는 날짜만** latest 파일로 폴백하며, 빈/읽기 실패 원장은 최신 HOLD로 대체하지 않는다. 일중 시점별 수익률 연구는 아니며 원장이 없던 과거의 덮어쓴 판단은 복원할 수 없다. 기존 콜백 예외→False 계약은 이번 범위에서 변경하지 않았다.

- 가격 조회 예산 150건은 유지하되 행별 `last_price_attempted_at`을 저장해 미시도→가장 오래전 시도→기존 우선순위로 처리한다. 정확한 날짜가 없는 오래된 표본도 보존·재시도하며, 이들이 새 표본의 평가를 영구히 막지 않게 한다. 재시작 후에도 보조 시각이 복원된다.

### 추가 가치 검증 도구 (`scripts/team_policy_ab.py`)
- A 기존 규칙 / B +독립 근거 검토(R1) / C +토론(R2) 를 같은 후보군·시점에서 비교. 실험 1(선정: 진입·청산·비용 고정) / 실험 2(가격·시점: 기존 진입 vs EntryPlan 조건부, 일봉만으로 선후 불명확이면 미체결).
- 2026-09-15 교차 리뷰 후속: 고정 손절선 또는 이미 활성화된 트레일링선을 시가가 관통하면 시가로 잔여분을 청산한다(당일 고가·익절 판정보다 먼저). 비용·손절/익절 임계값은 불변이며 실엔진 청산을 수정한 것이 아니다.
- replay의 기준 시각은 `plan.decided_at`(naive=KST). 그 키 자체가 없는 구형 스냅샷만 후보일 KST 자정으로 고정 폴백하며 해상도 한계를 명시한다. 키가 있으나 무효/결측이거나 결정 KST 날짜가 후보일과 다르면 B/C는 기권한다. 원본 `data_as_of` 우선, 그 키가 없을 때만 판단 시각에서 `age_minutes`를 뺀다. 모든 TTL·감쇠·근거 만료를 이 시각으로 평가하며 UTC/KST 혼합도 같은 순간으로 비교한다. 미래 관측이 섞인 보고서는 항목별 점수 분해가 불가능하므로 **보고서 전체 제외**(다른 유효 보고서는 유지). 기존 no-age 합성 fixture는 명시 시각으로 보완했으며 결측을 fresh로 간주하던 계약은 폐기했다.
- 미래·명시 손상 `observed_at` 검사는 보고서 최상위와 nested 근거 모두에 적용한다. 키 없음/None은 미상으로 보존하고, 문자열 등 명시값이 파싱되지 않으면 관측 없음으로 정상화하지 않는다.
- timing의 `CHECKER_ERROR`는 일반 미체결·기회비용과 분리한다. 원 `candidates_selected`·상관 표본 제외 수 외에 `timing_candidates`(dedup 후), `checker_errors`/`checker_error_reasons`, `evaluated_candidates`(오류 제외 분모)를 함께 출력한다. 모두 검사 오류이면 `fill_rate=None`이며, 오류 제외로 분모가 줄어든 비율을 단독 성능 근거로 쓰지 않는다. 일봉 날짜 자정 판정·데이터 없음의 기존 처리 및 사전 등록 기준은 유지한다.
- 사전 등록(manifest): 주평가 = 포지션당 비용 차감 R 중앙값·평균, MDE +0.10R, 표본 ≥30/정책, 시간순 홀드아웃(마지막 1/3) 부호 유지, 종목-주 클러스터·겹치는 보유기간 dedup, 판단 시각 이전 필드만. 벤치마크 KODEX200(캐시 없으면 null).
- **실데이터 없음 → 합성 fixture 로 도구만 검증(`validation_status=synthetic_only`)**. 성능·확률 보정·승격 판정은 미검증/보류. 과거 LLM 재평가는 사후 지식 가능성을 한계로 명시.

## 남은 작업

- [x] 파이프라인 구현 + 단위·통합 검증
- [x] 대시보드 카드 + API
- [x] 스케줄러 연결 (shadow)
- [x] 적대적 리뷰 반영 (근거 fail-closed, PM 오버라이드 차단, 섹터 전달)
- [x] **포트폴리오 단위 allocator** — `allocator.py`. 섹터 집중(동시 승인분 포함)·
      슬롯·현금·일일한도를 원자적으로 적용
- [x] LLM 재현성 계약 — `reproducibility.py`. 프롬프트/응답 전문·모델 ID·입력 스냅샷 해시
      append-only 기록 + 동일 입력 판정 일치율 측정
- [ ] 심의 결과 → Trade Wiki 학습 루프

## 튜닝 포인트

```python
# team.py
MAX_CONCURRENT = 3          # 동시 심의 (LLM rate limit)
DELIBERATION_TIMEOUT = 90.0

# researchers.py
ROUND_TIMEOUT = 20.0
MAX_TOKENS = 400            # ⚠️ gpt-5 계열은 이 값이 작으면 본문이 빈 문자열로 온다

# trader.py
BUY_THRESHOLD = 20
SELL_THRESHOLD = -30

# portfolio_manager.py
MIN_CONVICTION = 0.75
DAILY_OVERRIDE_LIMIT = 2
```
