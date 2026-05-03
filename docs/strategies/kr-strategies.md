# KR 전략 상세

> 최종 갱신: 2026-05-04 (theme_chasing 폐지, rsi2/gap 단기 회전 분기, 전문가 패널 통합)

## 전략 배분 (evolved_overrides.yml 기준)

| 전략 | 배분 | 상태 | 포지션 크기 |
|------|------|------|-----------|
| SEPA Trend | **49.2%** | 활성 | 25% equity |
| RSI2 Reversal | **12.5%** | 활성 | 20% equity |
| Strategic Swing | **18.8%** | 활성 ⚠️ | 25% equity (SEPA급) |
| Gap & Go | 9.5% | 활성 | 15% equity |
| Core Holding | 10% | locked | 10% equity |
| **Theme Chasing** | **0%** | 🚫 폐지 | - |
| Momentum Breakout | 0% | 비활성 | - |

⚠️ Strategic Swing: trending_bull에서 28.6% 승률 (param-optimizer DB) — bull 전환 시 18.8%
노출이 역풍 가능성. ranging 레짐에서 85.7% 우수.

## 1차 익절 분기 (단기/중기 회전 차등) — 2026-05-03 추가

| 전략 | 1차 익절 % | 매도 비율 | 비고 |
|------|----------|---------|------|
| SEPA Trend / Strategic Swing | 5% | 0.20 | 추세 추종 (보유 5-7일) |
| **RSI2 Reversal** | **4%** | **0.40** | 단기 반전 (보유 1.5일) |
| **Gap & Go** | **4%** | **0.40** | 단기 모멘텀 |
| Core Holding | 5% | 0.0 (분할 비활성) | 트레일링만 |

`scripts/run_trader.py:_strategy_exit_params`에 정의.

## 전문가 패널 통합 (2026-05-03~)

`signals/strategic/expert_panel.py` — 일요일 21:00 갱신, GPT-5.4 4명 병렬 호출.

**활용 경로 3건:**
1. **swing_screener** (sepa_trend, strategic_swing): 추천 종목 +25점 부스트
2. **cross_validator 규칙 10**: 모든 전략에 +max(2, conv×10×freshness) 보너스
   - side==BUY 한정, 21일 폐기, freshness<0.5 보너스 0
3. **LLM 2차 검증**: regime 결합 + risk_factors 컨텍스트 주입 (상위 5건)

## 1. SEPA Trend (`src/strategies/kr/sepa_trend.py`)

### 개요
미너비니 SEPA 추세 템플릿. MA 정렬 + 수급 + 재무 + 거래량 복합 스코어링.

### 스코어링 (100점 만점, overlay 포함 후 100점 클램핑)
| 팩터 | 최대 점수 | 기준 |
|------|----------|------|
| 기술적 (SEPA pass + MA spread + 52주고점 + MRS + MA5>20) | 40 | sepa_pass=15, spread>10%=7, 고점-5%이내=7, MRS+slope=5 |
| 수급 LCI (z-score) | 20 | lci>1.5=20, 외국인/기관 순매수 |
| 재무 (PER/PBR/ROE) | 10 | ROE>10%=6, PER<20=2, PBR<3=2 |
| 거래량 모멘텀 | 10 | vol_ratio>2x=10, >1.5x=7, >1.2x=4 |
| 섹터 모멘텀 | 10 | sector_momentum_score 직접 반영 |
| overlay_bonus (VCP/전문가/수급) | 가산 | `min(score + overlay, 100)` 클램핑 |

### 감점 규칙
| 조건 | 감점 |
|------|------|
| MA200 과확장 >50% | -10 |
| MA200 과확장 >30% | -5 |
| 20일 고점 돌파 직후 (추격) | -5 |
| MA50 대비 +2% 미만 (애매한 추세) | -5 |
| MRS < 0 (종목 RS 음수) | -5 |
| 거래량 < 0.8x | -5 |
| 적자 기업 (PER < 0) | -5 |

### 가드
- ATR = 0/None → **진입 차단**
- ATR > 6.0% → **진입 차단** (고변동성 노이즈 손절 방지)
- 14:30 이후 → **신규 진입 차단** (오버나이트 갭 리스크)
- MA200 대비 +80% → 과확장 차단

### 포지션 사이징 (position_multiplier)
- ATR 기반: `atr_position_multiplier(atr_pct)` (2%→1.0, 10%→0.3)
- 고점수 확대: 90+ → min 0.85x, 85+ (MRS>0) → min 0.75x, 80+ → min 0.65x

---

## 2. RSI2 Reversal (`src/strategies/kr/rsi2_reversal.py`)

### 개요
RSI(2) 과매도 반전 진입. 상위 추세(MA200) 필터 결합.

### 스코어링 (100점 만점)
| 팩터 | 최대 점수 | 기준 |
|------|----------|------|
| RSI(2) 과매도 | 30 | RSI<5=30, <10=22, <15=11 |
| MA200 상방 | 15 | +20%=15, +10%=11, 양수=7 |
| BB 하단 이탈 | 15 | -2%이하=15, -2~0%=10 |
| 수급 (외국인/기관) | 20 | 한쪽 순매수=12~20 |
| MRS(상대강도) | 5 | MRS>0+slope>0=5 |
| 5일 하락 후 반등 | 10 | change_5d<-15%=-5(급락감점), <-5%=+10, <-3%=+5 |
| 거래대금 증가 | 5 | vol_ratio>1.5=5 |

### 가드
- ATR = 0/None → 진입 차단
- ATR > 8.0% → **진입 차단** (극고변동성 역추세 진입 방지)
- VCP overlay >= 3.0 + MA200 상방 → position_multiplier 확대
- **약세장(market_regime=bear) 전면 차단** — 2026-04-18 추가. Connors 원전 RSI(2) 규칙
  (지수가 MA200 하방 또는 약세장에서는 역추세 진입 금지) 준수. 크로스검증 규칙 3에서
  `_bear_block`에 `rsi2_reversal`, `momentum_breakout` 추가.

---

## 3. Theme Chasing (`src/strategies/kr/theme_chasing.py`) — 🚫 폐지 (2026-05-04)

### ❌ 비활성 상태
`evolved_overrides.yml`: `theme_chasing.enabled: false`, `allocation: 0.0%`.

### 폐지 근거 (param-optimizer DB 검증, 2026-05-04)
- 누적 44건 -300k 손실 (3월~4월)
- 점수 구간별 실제 승률 (역설):
  | 구간 | n | 승률 | 평균 PnL |
  |------|---|------|---------|
  | 70-75 | 4 | **75.0%** | +0.97% |
  | 75-80 | 14 | 21.4% | -1.01% |
  | 80-85 | 9 | 11.1% | -1.08% |
  | 85+ | 16 | 43.8% | -0.82% |
  → min_score 75 상향(이전 변경)이 차단하는 70-75는 75% 승률 우수, 통과되는 75-85는 최악 → 임계 정반대 작용
- 보유 기간이 진짜 구분자: 0일 22.2% / 4일+ 66.7% — 점수 무관

### 재활성화 조건 (5/16 토 평가)
- 보유 기간 필터(4일+ 잔류 우대) 도입
- 또는 80+ 임계 + 강세 테마장(예: 2차전지 폭등) 한정
- `evolved_overrides.yml _meta.theme_chasing.enabled` 사유 참조

### 기존 설정 (참고용 — 비활성 중)
| 조건 | 값 |
|------|---|
| 최소 등락률 | 2.5% |
| ATR 상한 | 5.5% |
| 진입 시작 시간 | 09:30 |
| 14:00 이후 | 진입 차단 |
| min_score | 75 (5/3 65→75, 5/4 폐지) |

---

## 4. Gap & Go (`src/strategies/kr/gap_and_go.py`)

### 개요
갭상승 후 눌림목 매수. 장초반 모멘텀 포착.

### 가드
- ATR = 0/None → 진입 차단 (return None)
- 시간 윈도우 제한 (entry_start_time ~ entry_end_time)

---

## 5. Strategic Swing (`src/core/batch_analyzer.py`)

### 개요
별도 전략 파일 없음. BatchAnalyzer의 `_generate_strategic_signals()`에서 생성.
SEPA/RSI2 후보 중 **2계층 이상 복합 시그널** (전문가패널+수급추세+VCP) 교차 확인 종목.

### 조건
- `strategic_layers >= 2`
- `score >= _strategic_min_score` (기본 70)
- 포지션 크기: SEPA급 25%

---

## 6. Core Holding (`src/core/batch_analyzer.py`)

### 개요
장기 보유 전략. 별도 예산 풀(30%). 월초 리밸런싱.

### 특징
- max_positions = 3
- 리밸런싱 제외 종목 설정 가능 (evolved_overrides.yml)
- ATR 동적 손절 비활성 (고정 SL)

---

## 시장 체제 판단 보조지표 (VIX 경량 패널) — 2026-04-19 추가

### 배경
`MarketRegimeAdapter`의 MA20/시가대비 기반 판단은 후행적이다. 4/8 이란 휴전 랠리
(KOSPI +6.87%)에서 시스템이 4/14까지 bear 체제를 유지해 월간 알파 -15.51%p 손실.
이를 보완하기 위해 CBOE VIX(^VIX)를 보조지표로 도입.

### 구현 (`src/core/market_regime.py`)
- **조회**: `yfinance.Ticker("^VIX").history(period="2d")` → 최근 종가
  - 동기 호출은 `asyncio.to_thread`로 래핑 (이벤트 루프 블로킹 방지)
- **캐시**: `~/.cache/ai_trader/vix_cache.json` (JSON `{timestamp, value}`)
  - TTL 6시간 — 1일 1회 이상만 네트워크 조회 (yfinance rate limit 보호)
  - `update_regime()` 호출 시 캐시 읽기 + 만료 시 백그라운드 task로 refresh
- **실패 처리**: 네트워크/라이브러리 예외는 조용히 `logger.debug`만 남기고 기존 로직 fallback.
  VIX 조회 실패가 전체 엔진 차단을 유발하지 않는다.

### 판단 규칙

| VIX 상태 | 값 | 동작 |
|---------|----|------|
| Fear | VIX >= 30 | 기준 체제가 `bull`이면 `sideways`로 강등 (급변동 예고) |
| Normal | 15 < VIX < 30 | 기존 로직 그대로 |
| Complacency | VIX <= 15 | bull 전환 확인 지연 **1800초 → 600초** 단축 (랠리 포착) |

주의: bear 전환은 안전 우선 — complacency에도 기존 1800초 유지.

### 로그 형식
```
[체제] VIX=35.0 (fear), 기준 체제 bull → 조정 sideways
[체제] VIX=17.5 (normal) 갱신 완료
```

### 제약
- `REGIME_PARAMS` 테이블 자체는 변경하지 않음 (파라미터 조정은 별도 단계)
- VIX 조회 주기 1일 1회 (캐시 TTL 6시간으로 자연 제한)
- 첫 봇 기동 시 캐시가 없으면 백그라운드 fetch 예약 — 첫 호출은 VIX 미반영,
  두 번째 호출부터 반영 (감수 범위 내)

### 회귀 테스트 아이디어
1. **VIX=None (캐시 부재 + 네트워크 실패)** → 기존 MA20 기반 판정과 동일한 결과
2. **VIX=12 (complacency)** + bull 조건 → 첫 호출 pending, 10분 후 두 번째 호출에서 bull
3. **VIX=35 (fear)** + bull 조건 → 즉시 sideways
4. **VIX=20 (normal)** → 기존 로직과 완전 동일 (회귀 없음)
5. 장초 09:00~10:00 neutral 고정 시간에 VIX fear가 오면 → neutral 유지 (VIX 적용 전)
