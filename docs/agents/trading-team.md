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

`kr_scheduler.run_team_deliberation` — 장중 **10:30 / 14:00** 2회.

| 대상 | 범위 |
|---|---|
| 매수 후보 | 최근 스크리닝 상위 5 (`bot._last_screened`) |
| 보유 종목 | 전체 재평가 (`portfolio.positions`) |

> ⚠️ **shadow 단계 — 주문을 내지 않는다.**
> 심의 결과를 저장·알림만 한다. 팀은 2026-08-02 신설이라 실전 데이터가 없고,
> 첫 통합 테스트에서 P0 결함이 3건 나왔다. 규칙 #11을 shadow_mode로 시작했던 것과
> 같은 방식으로, 며칠 관측해 판정 품질을 확인한 뒤 주문 경로 연결을 결정한다.

설정: `config/default.yml` → `kr.trading_team`
```yaml
kr:
  trading_team:
    enabled: true
    debate_rounds: 2
    allow_pm_override: false   # 2026-08-03 기본 비활성
    max_concurrent: 3
```

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
