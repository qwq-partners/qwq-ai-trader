# QWQ AI Trader — 에이전트 팀 기반 진화 로드맵

> 작성일: 2026-03-30
> 영감: [PRISM-INSIGHT](https://github.com/dragon1086/prism-insight) 에이전트 팀 아키텍처

---

## 현재 상태 (As-Is)

```
사용자 ←→ Claude Code
              │
              ├── code-reviewer (코드 리뷰 위임)
              ├── debugger (디버깅 위임)
              └── quantum-growth-engine (트레이딩 전반 위임)

엔진 내부:
  UnifiedEngine
    ├── 전략들 (독립 score 계산 → 독립 시그널 발행)
    │   ├── SEPATrend → score=78 → Signal → 엔진 실행
    │   ├── RSI2Reversal → score=70 → Signal → 엔진 실행
    │   ├── ThemeChasing → score=65 → Signal → 엔진 실행
    │   └── GapAndGo → score=80 → Signal → 엔진 실행
    ├── RiskManager (규칙 기반 차단)
    ├── ExitManager (분할 익절 + 트레일링)
    └── StrategyEvolver (20:30 파라미터 자동 조정)
```

**핵심 약점**:
1. 각 전략이 **독립적으로 판단** — 교차 검증 없음
2. 시장 체제가 **사후 방어**에만 사용 (사이드카) — 사전 조정 없음
3. 진화 시스템이 **파라미터만 조정** — 판단 기준은 고정
4. 거래 경험이 **축적되지 않음** — 같은 실수 반복 가능
5. 에이전트가 **대화 레벨**에서만 동작 — 엔진 내부에 통합 안 됨

---

## 목표 상태 (To-Be)

```
┌─────────────────────────────────────────────────────────────┐
│                    에이전트 팀 시스템                          │
│                                                             │
│  [분석팀]              [전략팀]            [실행팀]           │
│  ┌──────────────┐     ┌────────────┐     ┌──────────────┐   │
│  │ 기술분석 Agent│     │ 크로스검증  │     │ 포지션관리   │   │
│  │ 수급분석 Agent│ ──→ │ Gate Agent │ ──→ │ Agent        │   │
│  │ 시장체제 Agent│     │ (종합 판단) │     │ (사이징+실행) │   │
│  │ 테마분석 Agent│     └────────────┘     └──────────────┘   │
│  └──────────────┘           │                    │           │
│                             │                    │           │
│  [검증팀]              [진화팀]                              │
│  ┌──────────────┐     ┌────────────┐                        │
│  │ 품질평가 Agent│     │ 거래메모리  │                        │
│  │ 리스크감사   │ ←── │ Agent      │                        │
│  │ Agent        │     │ 파라미터   │                        │
│  └──────────────┘     │ 최적화    │                        │
│                       │ Agent      │                        │
│                       └────────────┘                        │
└─────────────────────────────────────────────────────────────┘
```

---

## Phase 1: 크로스 전략 검증 게이트 (1주)

> **목표**: 독립 시그널의 맹점을 보완하는 교차 검증 레이어

### 구현 내용

```python
# src/core/cross_validator.py (신규)

class CrossStrategyValidator:
    """다중 전략 신호를 교차 검증하는 게이트"""

    def validate(self, signal: Signal, market_context: dict) -> Optional[Signal]:
        """
        규칙 기반 교차 검증 (LLM 없이, 실시간 성능 유지)

        검증 항목:
        1. 전략 간 모순 감지 (SEPA 매수 vs RSI2 과매수)
        2. 수급-기술 불일치 (기술적 좋지만 기관 순매도)
        3. 시장 체제 부합성 (약세장에서 공격적 테마 차단)
        4. 섹터 과열 감지 (동일 섹터 시그널 3개 이상)
        """
```

### 검증 규칙 (초기 5개)

| # | 규칙 | 조건 | 조치 |
|---|------|------|------|
| 1 | SEPA 매수 + RSI(14) > 70 | 기술적 과매수 | score -10 |
| 2 | 테마 매수 + 기관+외국인 동시 순매도 | 수급 불일치 | 차단 |
| 3 | 약세 체제 + 공격적 전략(테마/갭) | 체제 부적합 | 차단 |
| 4 | 동일 섹터 신규 매수 3건+ | 섹터 집중 | 차단 |
| 5 | 전일 손절 종목과 동일 섹터 | 연쇄 손절 위험 | score -15 |

### 적용 위치

```
기존: 전략 → Signal → engine.on_signal() → 주문
변경: 전략 → Signal → CrossValidator.validate() → engine.on_signal() → 주문
```

engine.py의 `on_signal()` 최상단에 검증 게이트 삽입. 기존 코드 변경 최소화.

---

## Phase 2: 시장 체제 사전 적응 (1주)

> **목표**: 시장 상황에 따라 전략 파라미터를 사전에 조정

### 구현 내용

```python
# src/core/market_regime.py (신규)

class MarketRegimeAdapter:
    """시장 체제별 전략 파라미터 동적 조정"""

    # 체제 판단 기준 (KOSPI 기반)
    # - bull:     KOSPI > MA20 & 5일 변화 > +2%
    # - bear:     KOSPI < MA20 & 5일 변화 < -2%
    # - sideways: 그 외

    REGIME_PARAMS = {
        "bull": {
            "sepa_min_score": 65,
            "theme_max_change": 10.0,
            "max_daily_new_buys": 5,
            "position_mult_boost": 1.1,
            "rsi2_weight": 0.8,       # 추세 추종 강화, 역추세 약화
        },
        "bear": {
            "sepa_min_score": 80,
            "theme_max_change": 4.0,
            "max_daily_new_buys": 2,
            "position_mult_boost": 0.6,
            "rsi2_weight": 1.3,       # 역추세(눌림목) 강화
        },
        "sideways": {
            "sepa_min_score": 72,
            "theme_max_change": 6.0,
            "max_daily_new_buys": 3,
            "position_mult_boost": 0.9,
            "rsi2_weight": 1.1,
        },
    }
```

### 적용 위치

- `kr_scheduler.py` 장 시작 시 (08:50) 체제 판단 → 당일 파라미터 세팅
- `run_screening()` 내 min_score, max_change 등에 체제 파라미터 반영
- 기존 스마트 사이드카는 "사후 방어"로 유지 (이중 안전망)

---

## Phase 3: 거래 메모리 시스템 (2주)

> **목표**: 거래 경험이 축적되어 같은 실수를 반복하지 않는 시스템

### PRISM의 3-Layer 메모리 차용 + QWQ 맞춤화

```python
# src/core/evolution/trade_memory.py (신규)

class TradeMemory:
    """거래 경험 축적 + 원칙 추출 + 매수 점수 보정"""

    # Layer 1: 원시 기록 (0~7일)
    # - 진입/청산 시점의 전체 지표 스냅샷
    # - 시장 체제, 섹터 상태, 뉴스 센티멘트

    # Layer 2: 요약 기록 (8~30일)
    # - "반도체 + 기관매수 + SEPA 85점 → +8% 익절 (5일 보유)"
    # - "바이오 테마 + RSI 72 → -4% 손절 (추격 진입)"

    # Layer 3: 원칙 (31일+)
    # - "반도체 섹터 기관 순매수 시 SEPA 적극 진입" (confidence: 0.8)
    # - "RSI 70+ 테마주 진입은 손절 확률 높음" (confidence: 0.7)
    # - confidence < 0.3 → 비활성화
    # - 90일 미검증 → 삭제
```

### 데이터 흐름

```
매도 체결
  → record_outcome(진입 지표, 청산 사유, 수익률)
  → Layer 1에 저장

매주 금요일 20:30 (evolve 후)
  → compress_layers()
  → Layer 1 → Layer 2 요약
  → Layer 2 → Layer 3 원칙 추출 (LLM 사용)

매수 시그널 생성 시
  → get_score_adjustment(symbol, indicators)
  → Layer 3 원칙 매칭 → score ±3 보정
```

### 저장소

- `~/.cache/ai_trader/trade_memory/`
  - `layer1_YYYY-MM.json` — 원시 기록
  - `layer2.json` — 요약 기록
  - `layer3_principles.json` — 추출된 원칙

---

## Phase 4: 품질 검증 파이프라인 (1주)

> **목표**: 엔진 출력의 정확성을 자동 검증

### 구현 내용

```python
# src/core/evolution/quality_validator.py (신규)

class QualityValidator:
    """매일 저녁 자동 품질 검증"""

    async def run_daily_validation(self):
        """20:25 (evolve 직전) 실행"""

        results = {
            # 1. 스크리닝 적중률: 오늘 스크리닝한 종목 vs 실제 종가
            "screening_accuracy": await self._check_screening_vs_actual(),

            # 2. 진화 파라미터 합리성: 최근 변경이 성과에 미친 영향
            "evolution_sanity": await self._check_param_changes(),

            # 3. 테마 탐지 교차 검증: LLM 테마 vs 실제 섹터 지수 움직임
            "theme_accuracy": await self._cross_check_themes(),

            # 4. 리스크 한도 준수: 일일 손실, 포지션 집중도, 섹터 분산
            "risk_compliance": await self._check_risk_limits(),

            # 5. 시그널 품질: 발행 시그널 중 실제 체결 비율, 수익 비율
            "signal_quality": await self._check_signal_outcomes(),
        }

        # 경고 임계값 초과 시 텔레그램 알림
        # 결과를 daily_report에 포함
```

### 적용 위치

- 기존 `run_batch_scheduler`의 20:30 `evolve` 직전에 `quality_validator.run_daily_validation()` 호출
- 검증 결과를 evolve에 컨텍스트로 전달 → 파라미터 조정의 근거로 사용

---

## Phase 5: Claude Code 에이전트 팀 구성 (1주)

> **목표**: CLAUDE.md의 에이전트 위임 체계를 트레이딩 전문 팀으로 재구성

### 현재 → 변경

```
[현재 3명]
code-reviewer, debugger, quantum-growth-engine

[변경 8명 — 5개 팀]

분석팀:
  trade-analyst        거래 내역 분석, 승패율, 패턴 추출
  market-analyst       시장 체제 판단, 섹터 로테이션 분석

전략팀:
  strategy-advisor     전략 조언, 파라미터 제안, 교차 검증 설계

실행팀:
  engine-monitor       엔진 상태 점검, 로그 분석, 이상 탐지

검증팀:
  code-reviewer        코드 리뷰 (기존)
  risk-auditor         리스크 설정 검증, 포지션 집중도 감사

진화팀:
  param-optimizer      진화 파라미터 검증, A/B 비교
  debugger             디버깅 (기존)
```

### 에이전트 정의 파일

```
.claude/agents/
  ├── trade-analyst.md      # 거래 분석 전문가
  ├── market-analyst.md     # 시장 분석 전문가
  ├── strategy-advisor.md   # 전략 조언가
  ├── engine-monitor.md     # 엔진 모니터
  ├── risk-auditor.md       # 리스크 감사
  └── param-optimizer.md    # 파라미터 최적화
```

### ~/CLAUDE.md 에이전트 위임 규칙 변경

```markdown
## 에이전트 위임
- 코드 리뷰: code-reviewer
- 디버깅: debugger
- 거래 분석 (거래내역, 승패율, 패턴): trade-analyst
- 시장 분석 (체제 판단, 섹터): market-analyst
- 전략 조언 (파라미터, 전략 설계): strategy-advisor
- 엔진 점검 (로그, 상태, 이상): engine-monitor
- 리스크 감사 (설정 검증, 집중도): risk-auditor
- 파라미터 최적화 (진화 검증, A/B): param-optimizer
```

---

## Phase 6: LLM 종합 판단 (장기)

> **목표**: PRISM의 "투자전략가"처럼 LLM이 최종 매수 판단에 참여

### 설계 방향

Phase 1의 규칙 기반 CrossValidator가 안정화된 후,
LLM 기반 종합 판단을 **선택적으로** 추가:

```python
# 고점수(score >= 85) 시그널만 LLM 2차 검증
if signal.score >= 85 and market_regime != "bull":
    llm_verdict = await llm_strategy_check(
        signal=signal,
        technical=indicators,
        supply_demand=supply_data,
        market_regime=regime,
        trade_memory=memory.get_principles(),
    )
    if not llm_verdict.approved:
        signal.score -= 20
        signal.metadata["llm_veto"] = llm_verdict.reason
```

**주의**: LLM 호출은 비용+지연이 있으므로 **모든 시그널이 아닌 고점수 시그널만** 대상.
강세장에서는 LLM 검증 생략 (속도 우선).

---

## 구현 일정

| Phase | 기간 | 핵심 산출물 | 의존성 |
|-------|------|-----------|--------|
| **1** | 1주 | `cross_validator.py` + engine 통합 | 없음 |
| **2** | 1주 | `market_regime.py` + 스케줄러 연동 | Phase 1 |
| **3** | 2주 | `trade_memory.py` + 3-Layer 압축 | Phase 1, 2 |
| **4** | 1주 | `quality_validator.py` + evolve 연동 | Phase 3 |
| **5** | 1주 | `.claude/agents/*.md` + CLAUDE.md 갱신 | 없음 (병렬 가능) |
| **6** | 장기 | LLM 종합 판단 (선택적) | Phase 1~4 안정화 후 |

**총 예상: 6~7주** (Phase 5는 병렬 진행 가능)

---

## 성과 측정 기준

| 지표 | 현재 | 목표 | 측정 방법 |
|------|------|------|----------|
| 일일 승률 | ~50% | 55%+ | trade_journal 집계 |
| 평균 손절 크기 | -4~5% | -3.5% 이내 | 크로스 검증으로 추격 진입 감소 |
| 섹터 집중도 | 무제한 | 동일 섹터 3개 이내 | risk_auditor 감사 |
| 스크리닝 적중률 | 미측정 | 60%+ | quality_validator |
| 진화 파라미터 안정성 | 매주 변동 | 2주 안정 유지 | param_optimizer |
| 같은 패턴 반복 손실 | 빈번 | 2회 이내 | trade_memory Layer 3 |

---

## PRISM vs QWQ 최종 비교

| 차원 | PRISM | QWQ (현재) | QWQ (To-Be) |
|------|-------|-----------|-------------|
| 분석 | 6개 LLM 에이전트 | 규칙 기반 스크리너 | 규칙 + 크로스 검증 |
| 판단 | LLM 투자전략가 | 각 전략 독립 | 크로스 게이트 + (LLM 선택적) |
| 실행 | 배치 (아침/오후) | **실시간 이벤트 루프** | 실시간 유지 (강점) |
| 적응 | 시장 체제별 기준 | 사후 사이드카 | **사전 체제 적응** |
| 학습 | 3-Layer 메모리 압축 | 파라미터만 조정 | **3-Layer 메모리 + 원칙** |
| 검증 | 품질평가사 에이전트 | 수동 리뷰 | **자동 품질 검증** |
| 분업 | 13개 전문 에이전트 | 3개 범용 에이전트 | **8개 전문 에이전트** |

**QWQ의 고유 강점** (PRISM에 없는 것):
- 실시간 WebSocket 기반 체결/시세 — PRISM은 배치 기반
- 분할 익절 3단계 + 복합 트레일링 — PRISM은 올인/올아웃
- ATR 기반 동적 포지션 사이징 — PRISM은 고정 10%
- 스마트 사이드카 (시장 추세 연동) — PRISM은 단순 손절
- KIS API 직접 통합 (실시간 주문) — PRISM은 시뮬레이션 위주
