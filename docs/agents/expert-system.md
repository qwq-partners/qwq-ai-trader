# 전문가 시스템 (Expert System)

> 도입일: 2026-05-29 (7명) → 2026-06-07 weekend_signal_expert → 2026-08-07 sector_council 추가 (총 9명)
> 위치: `src/experts/`
> 정의서: `.claude/agents/*-expert*.md`, `news-curator.md`, `macro-economist.md`

## 개요

8명의 도메인 전문가가 거시·뉴스·섹터·실적·갭risk를 분석하여 엔진 의사결정에 기여한다.
기존 6명 운영/분석 에이전트(trade-analyst 등)는 가격·수급·체결 위주였던 약점을 보완한다.

## 에이전트 명단

| 에이전트 | 도메인 | 핵심 출력 |
|----------|--------|----------|
| **news-curator** | 한·미·글로벌 뉴스 sentiment | 종목별 sentiment + 이벤트 태그 |
| **macro-economist** | 글로벌 거시 (Fed/금리/환율/원자재) + 반도체 바스켓 5일 (2026-06-07) | 거시 점수 + 영향 섹터 |
| **kr-market-expert** | KOSPI/KOSDAQ 수급·체결·옵션 + KOSPI200 야간선물 (2026-06-07) | 수급 점수 + 로테이션 섹터 |
| **us-market-expert** | SPY/QQQ/IWM/VIX/섹터 ETF + SOX 1일/5일 (2026-06-07) | 시장 점수 + 강세 섹터 |
| **kr-economy-expert** | 한국 거시 (한은/수출입/PF) | 한국 특화 거시 점수 |
| **global-micro-expert** | 반도체/2차전지/바이오/조선 공급망 | 산업 점수 + 수혜 종목 |
| **earnings-expert** | 어닝 캘린더·서프라이즈·드리프트 | 임박 어닝 + 평균 surprise |
| **weekend-signal-expert** (2026-06-07~) | ES=F/NQ=F/KIS 야간선물/NKD=F/KRW=X/VIX/BTC/ZB=F | 갭 risk 점수 |
| **sector-council** (2026-08-07~) | KR 13개 섹터 (반도체~방산·증권·보험) — ETF 모멘텀 60% + Perplexity 뉴스 40% | 섹터별 점수 — **체제 집계 제외**, 종목 판단 전용 (규칙#12 shadow·브리핑) |

## 슬롯 (2026-06-07 확장)

| 슬롯 | 시각 (KST) | 발화 요일 | 발송 채널 |
|------|-----------|-----------|-----------|
| morning | 07:30 | 평일(0~4) | report (LLM 모닝브리프 결합) |
| midday | 13:00 | 평일 | DM |
| after | 16:30 | 평일 | DM |
| sunday_evening | 22:00 | 일요일(6) | report |
| monday_premarket | 06:00 | 월요일(0) | report |

주말 슬롯(`sunday_evening`, `monday_premarket`)은 BEAR 합의 임계 완화 적용 — `confidence ≥0.6`, 1명 이상.
평일 슬롯은 기본값 (`confidence ≥0.7`, 2명).

## 공통 출력 (ExpertOpinion)

```python
@dataclass
class ExpertOpinion:
    expert: str              # 에이전트 이름
    score: int               # -100 ~ +100
    regime_bias: RegimeBias  # bull / neutral / bear
    confidence: float        # 0.0 ~ 1.0
    key_findings: List[str]  # 핵심 발견 3~5개
    affected_sectors: List[str]
    affected_symbols: List[str]
    issued_at: datetime
    valid_until: datetime    # 기본 cache_ttl_hours
    raw_evidence: Dict
    error: Optional[str]
```

## 호출 흐름

```
┌─────────────────────────────────────────────┐
│ scheduler 트리거 (07:30 / 13:00 / 16:30 등) │
└─────────────┬───────────────────────────────┘
              ▼
┌─────────────────────────────────────────────┐
│ ExpertOrchestrator.run_all(force=True)      │
│   - 활성 에이전트 병렬 실행 (asyncio.gather)│
│   - 각 에이전트: 캐시 체크 → _analyze       │
│   - 의견 → opinion_store + wiki ingest      │
└─────────────┬───────────────────────────────┘
              ▼
┌─────────────────────────────────────────────┐
│ 엔진 소비 포인트                              │
│ 1) market_regime.apply_expert_adjustment    │
│ 2) cross_validator 규칙 #11 (BEAR 게이트)    │
│ 3) engine.on_signal (news 종목 sentiment)   │
│ 4) daily_reviewer 프롬프트 (진화 컨텍스트)   │
└─────────────────────────────────────────────┘
```

## 엔진 통합 포인트

### 1) 시장 체제 보정
`MarketRegimeAdapter.apply_expert_adjustment(orchestrator)`
- `bear_consensus(confidence≥0.7, min_count=2)` → bull/sideways/neutral → bear
- `aggregate_regime_score ≥ +20` → sideways/neutral → bull
- `aggregate_regime_score ≤ -20` → bull/sideways → sideways

### 2) cross_validator 규칙 #11
- BUY 신호에 한해 작동
- `expert_orchestrator.bear_consensus(0.7, 2)` 참이면 **즉시 차단**
- 기존 10개 규칙과 독립

### 2-1) cross_validator 규칙 #12 (2026-08-07, shadow 전용)
- BUY 신호의 소속 섹터 카운슬 점수 ≤ -40이면 감지 기록 (차단 없음)
- hit log: `~/.cache/ai_trader/rule12_shadow_log.jsonl` — 적중률 관측 후 실차단 전환 판단
- 섹터 해석: 규칙#4의 metadata sector 재사용 → 없으면 `sector_of_cached()` (동기·메모이즈)
- sector_council은 `NON_REGIME_EXPERTS` — 체제 보정·다수결·BEAR 합의 3종 집계에서 제외

### 3) 진화 시스템 (daily_reviewer)
- LLM 프롬프트에 "## 오늘 전문가 의견" 섹션 추가
- 전문가가 경고했는데도 진입한 거래 분석 유도

### 4) quality_validator
- `_check_expert_output()` — 일일 발행 수, 평균 confidence, bull/bear 분포
- avg_confidence < 0.3 → warning
- 주간 발행 < 30건 → notice

## 안전장치

| 항목 | 동작 |
|------|------|
| 마스터 스위치 | `config experts.enabled: false` |
| fail_open | 전문가 오류 시 매매 차단 안 함 |
| 개별 on/off | `config experts.agents.{name}: false` |
| 호출 예산 | 에이전트당 일 50회 (orchestrator enforce) |
| graceful degradation | _analyze 예외 → NEUTRAL 의견 반환 |
| 캐시 fallback | LLM 실패 시 직전 캐시 의견 재사용 |

## 비용 통제

- 모델 라우팅:
  - 데이터 수집·요약: Gemini Flash Lite
  - 종합 판단: GPT-5.4
  - 검색: Perplexity sonar
- 일 예상 비용: $3~5 (현재 $1~2 → +$3)
- Perplexity Pro $20/월 한도 내

## 영속화

```
~/.cache/ai_trader/
├── experts/                   # opinion_store
│   └── {expert}_{YYYY-MM-DD}.jsonl
├── wiki/experts/              # 마크다운 누적
│   └── {expert}.md
└── manual_macro_overrides.json  # FOMC/CPI 발표일 수동 입력
```

## 운영

### 스케줄
- **KR scheduler** `run_expert_briefing()`:
  - 07:30 (장전), 13:00 (장중), 16:30 (장후) — 7명 전체 호출
- **US scheduler** `us_expert_loop()`:
  - KST 21:30 / 01:30 / 06:00 — us-market-expert 단독 호출

### 주간 토요일
- `wiki.lint_all()` — 500줄 초과 페이지 300줄 트림
- `daily_reviewer` 주간 회고에 전문가 의견 통합

### 로그 태그
- `[전문가]` — orchestrator 일반
- `[Orchestrator]` — 등록/병렬 분석
- `[macro]`, `[kr-market]`, `[us-market]`, `[news_curator]` 등 개별

## 트러블슈팅

### 전문가 전체 비활성
```bash
# 봇 재시작 없이 일시 정지: orchestrator.config.enabled = False
# 영구: config/default.yml에서 experts.enabled: false → 재시작
```

### 특정 전문가만 비활성
```yaml
experts:
  agents:
    global_micro_expert: false   # 이것만 끔
```

### KRX 인증 누락 (kr-market-expert)
- pykrx 일부 메서드가 KRX_ID/PW 필요
- graceful fallback: 수급 데이터 0개로 분석 진행, 신호 약화만 발생
- 해결: `~/.bashrc`에 `export KRX_ID=...` `export KRX_PW=...`

### Perplexity 한도 초과
- daily_call_budget 자동 enforce
- 캐시(6h TTL) 활용
- 임계 도달 시 캐시 의견으로 응답


---

## 팀 심의가 매수로 이어지지 않던 원인 (2026-08-03 진단)

심의 13건(8/2~8/3)이 전부 `stance=hold`, 신규 매수 0건이었다.
**예산·현금 문제가 아니다** — `TraderAgent.propose()`에는 현금이 인자로도 들어가지 않는다.
원인은 분석가 입력이 굶고 있었던 것이다.

### 인과 사슬
```
지표 미전달 + 펀더멘탈 no-op
  → 유효 근거가 news 하나뿐
  → Bull이 "근거 없음"을 이유로 REJECT (13/13)
  → 만장일치 반대 → debate_adj = -40
  → analyst_score(0~30) - 40 < BUY_THRESHOLD(20)
  → 전건 HOLD
```
분석가 점수만 보면 3/9건이 이미 매수 기준(≥20)을 넘겼는데 토론 -40에 전부 뒤집혔다.

### 버그 1 — 지표가 한 번도 전달되지 않았다
`kr_scheduler._run_team_deliberation_once()`
```python
"indicators": (getattr(s, "indicators", None)
               or getattr(s, "metadata", {}).get("indicators")
               if hasattr(s, "metadata") else None),
```
파이썬은 이를 `(A or B) if hasattr(...) else None`으로 묶는다.
`SwingCandidate`에는 `metadata`가 없으므로 **indicators를 갖고 있어도 항상 None**.
기술적 분석가가 "지표 없음"으로 전량 실패했다(8/3 9건 중 9건).
보유 종목은 아예 `"indicators": None` 하드코딩이었다.

→ 추출 순서를 `객체 → metadata → 스크리너 지표 캐시`로 명시. 보유 종목도 캐시에서 채운다.

### 버그 2 — 펀더멘탈 분석가가 필드명을 전부 잘못 읽었다
| 분석가가 읽던 이름 | `ValidationResult` 실제 필드 |
|---|---|
| `passed` | `approved` |
| `reason` | `block_reason` |
| `supply_demand` | `supply_demand_result` |
| `short_selling` | `short_selling_result` |
| `sd.foreign_net` (숫자 가정) | `foreign_net_buying` (**bool**) |
| `ss.short_ratio` (숫자 가정) | `in_top50` (**bool**) |

`getattr(obj, name, default)`가 조용히 삼켜서 **항상 score=0**을 내면서 `confidence=0.7`을
주장했다. 실패보다 나쁘다 — 가중평균에서 뉴스 점수를 절반으로 희석시키는 유령 근거였다.

→ 실제 스키마에 맞춰 재작성. 순매도 감점은 뺐다(bool은 "순매수 아님"까지만 말해준다).

### 실측 — 토론은 근거에 반응한다
동일 종목·동일 프롬프트로 근거만 바꿔 토론을 돌린 결과:

| 근거 | Bull | Bear | 판정 | 보정 |
|---|---|---|---|---|
| fund 0 + news만 (수정 전) | 반대 | 반대 | 만장일치 반대 | **-40** |
| fund 40 + tech 35 + news 61 | 지지 | 반대 | 의견 분열 | **-10** |

30점 스윙. 9건 재계산 시 1건이 HOLD→BUY로 바뀐다(삼성E&A, total 22).

### ⚠️ 남은 구조적 제약 — 이건 설계 판단이 필요하다
`debate_adj = -40`은 현실적 분석가 점수 범위(0~40)보다 크다. 즉 **만장일치 반대가
나오면 어떤 근거로도 매수가 불가능**하다. 의도된 fail-closed지만, 근거 품질이
낮은 상태에서 토론이 쉽게 만장일치 반대로 쏠리면 매수 경로가 사실상 닫힌다.
지금은 입력을 고쳤으니 며칠 관측 후 `BUY_THRESHOLD(20)` / `-40` 캘리브레이션을
재검토할 것. 표본 없이 임계값부터 낮추면 검증 계층을 무력화하는 것과 같다.


---

## 전체 사이클 정밀 지도 + Bear 캘리브레이션 (2026-08-03 심층 분석)

### 사이클 전모

```
스크리닝(5분 주기) → _last_screened 상위 5
  → 심의 슬롯: 10:30 / 11:30 / 13:00 / 14:00 (4회/일, 2026-08-03 확대, 휴장일 스킵)
  → ① 분석가 3인 병렬 (LLM 없음, 결정론적)
       fund ≤ +40 / tech ≤ +55 / news ≤ ±100 — 종합(가중평균) 현실 상단 ~52
  → ② Bull/Bear 토론 (2라운드, Bull=OpenAI·Bear=Gemini, seed 고정)
  → ③ Trader (결정론적): BUY iff total = 분석가종합 + 토론보정 ≥ 20
  → ④ gate_checker (cross_validator 규칙, score=70 고정 가정)
  → ⑤ PM: 승인/거부 + 오버라이드(SOFT_GATES만, conviction ≥ 0.75, 2회/일)
  → ⑥ Allocator: 섹터·현금·슬롯 배분
  → ⑦ shadow — 주문 미실행. 승격: 표본 200 + 동일입력 일치율 80%
```

### 매수 문턱의 산수 (BUY_THRESHOLD=20)

| 토론 결과 | 보정 | 필요한 분석가 종합 | 도달 가능성 |
|---|---|---|---|
| 만장일치 지지 | +20×conf | ≥ 0 | 쉬움 — **단, Bear가 ACCEPT해야 성립** |
| 분열/단독 | -10 | ≥ 30 | "좋은 날"(33)만 |
| 만장일치 반대 | -40×conf | ≥ 60 | **이론 상단(52)보다 높음 = 절대 거부권** |

-40의 절대 거부권 자체는 의도된 fail-closed로 수용 가능하다. 문제는 그 전제였다.

### 병목의 실체 — Bear는 ACCEPT를 낼 수 없었다

실측 **19/19 REJECT** (프로덕션 13 + 실험 6). 근거 최상 케이스(종합 52)에서도 REJECT.

원인은 프롬프트 비대칭:
- Bear: "'문제 없음'은 **허용되지 않는다**" (양보 금지) + ACCEPT의 판정 기준 부재
- Bull: "부실하면 **솔직히 인정하고 반대하라**" (양보 장려)
- R2 Bear: "리스크가 **해소**됐다면 ACCEPT" — 불가능한 문턱 (리스크는 해소되지 않는다)

모델은 지시대로 생생한 실패 서사를 쓴 뒤 자기 서사에 설득돼 REJECT한다.
연쇄 효과: 만장일치 지지 경로 폐쇄 → conviction ≥ 0.75 불가 → **PM 오버라이드도
죽은 조항** → 유일한 매수 경로가 "분열(-10) + 분석가 ≥ 30"으로 협착.
그리고 shadow 관측은 BUY 표본을 얻지 못해 "매수 판정 품질"을 측정할 수 없다 —
이 수리는 임계값 튜닝이 아니라 **측정 도구 수리**다.

### 캘리브레이션 내용 (`researchers.py`)

실패 시나리오 제시 의무는 유지. 판정 기준만 명시했다:
> "당신의 실패 시나리오가 구체적 촉매나 데이터 없이 일반론(차익 실현, 시장 변동성,
> 선반영 우려 등)에만 기대면 ACCEPT, 근거의 핵심을 직접 무너뜨리는 구체적 사실이
> 있으면 REJECT다. 모든 건에 REJECT를 내는 것은 리스크 평가가 아니라 평가 회피다."

R2의 "해소됐다면"도 R1과 같은 기준(치명적 vs 감수 가능)으로 정합화.

### 검증 — 근거 사다리 5단계 (실제 LLM, seed 고정)

| 케이스 | 분석가 | Bull | Bear | 합의 | total | 판정 |
|---|---|---|---|---|---|---|
| A 최상(52) | 52 | 지지 | **ACCEPT** | 지지 +20 | 72 | **BUY** |
| B 좋음(33) | 33 | 지지 | **ACCEPT** | 지지 +20 | 53 | **BUY** |
| C 보통(20) | 20 | 반대 | ACCEPT | 분열 -10 | 10 | HOLD |
| D 약함(10) | 10 | 반대 | ACCEPT | 분열 -10 | 0 | HOLD |
| E 악재(-20) | -20 | 반대 | **REJECT** | 반대 -40 | -60 | HOLD |

완전한 단조 사다리. 역할 분담도 올바르게 재배치됐다 —
**Bear는 '위험'을 감지하고(E), Bull은 '매력'을 요구한다(C·D에서 Bull이 반대)**.
약한 근거는 이제 Bear의 일반론이 아니라 Bull의 인정으로 걸러진다.

### 재현성 원장과의 정합
`prompt_hash = sha256(system + prompt)` — 시스템 프롬프트가 해시에 포함되므로
변경 전후 호출은 자동으로 다른 그룹이 된다. 일치율(승격 기준) 오염 없음.

### 남은 판단 사항 (관측 후)
- **-40 / BUY_THRESHOLD=20**: 유지. Bear가 판별력을 가진 지금, 만장일치 반대는
  "진짜 악재"에서만 나온다(E). 절대 거부권이어도 그게 의도다. 표본 쌓인 뒤 재검토.
- **표본 속도**: ~~2슬롯/일 × ~6종목 ≈ 12건/일 → ~17영업일~~ →
  **2026-08-03 슬롯 4개로 확대** (10:30/11:30/13:00/14:00): ~24건/일, 200표본까지 ~8영업일.
- **PM 오버라이드**: 만장일치 지지가 실제로 나오는 지금부터는 살아 있는 조항이다.
  오버라이드 발생 시 감사 원장 + 텔레그램으로 즉시 확인할 것.
