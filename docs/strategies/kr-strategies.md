# KR 전략 상세

> 최종 갱신: 2026-04-06

## 전략 배분 (evolved_overrides.yml 기준)

| 전략 | 배분 | 상태 | 포지션 크기 |
|------|------|------|-----------|
| SEPA Trend | 25% | 활성 | 25% equity |
| RSI2 Reversal | 25% | 활성 | 20% equity |
| Strategic Swing | 10% | 활성 | 25% equity (SEPA급) |
| Theme Chasing | 5% | 활성 | 15% equity |
| Gap & Go | 5% | 활성 | 15% equity |
| Core Holding | 30% | locked | 10% equity (30%/3종목) |
| Momentum Breakout | 0% | 비활성 | - |

## 1. SEPA Trend (`src/strategies/kr/sepa_trend.py`)

### 개요
미너비니 SEPA 추세 템플릿. MA 정렬 + 수급 + 재무 + 거래량 복합 스코어링.

### 스코어링 (100점 만점)
| 팩터 | 최대 점수 | 기준 |
|------|----------|------|
| 기술적 (SEPA pass + MA spread + 52주고점 + MRS + MA5>20) | 40 | sepa_pass=15, spread>10%=7, 고점-5%이내=7, MRS+slope=5 |
| 수급 LCI (z-score) | 20 | lci>1.5=20, 외국인/기관 순매수 |
| 재무 (PER/PBR/ROE) | 10 | ROE>10%=6, PER<20=2, PBR<3=2 |
| 거래량 모멘텀 | 10 | vol_ratio>2x=10, >1.5x=7, >1.2x=4 |
| 섹터 모멘텀 | 10 | sector_momentum_score 직접 반영 |

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
| 5일 하락 후 반등 | 10 | change_5d<-5%+반등=10 |
| 거래대금 증가 | 5 | vol_ratio>1.5=5 |

### 가드
- ATR = 0/None → 진입 차단
- ATR > 8.0% → **진입 차단** (극고변동성 역추세 진입 방지)
- VCP overlay >= 3.0 + MA200 상방 → position_multiplier 확대

---

## 3. Theme Chasing (`src/strategies/kr/theme_chasing.py`)

### 개요
핫 테마 종목 실시간 추종. 장중 급등 종목 포착.

### 필터
| 조건 | 값 |
|------|---|
| 최소 등락률 | min_change_pct (**2.5%**) |
| 최대 등락률 (09~10시) | 4% |
| 최대 등락률 (10시~) | 7% |
| ATR 상한 | **5.5%** (고변동 종목 차단) |
| 진입 시작 시간 | **09:30** (장초반 30분 변동성 회피) |
| 14:00 이후 | **진입 차단** |
| RSI > 75 | 차단 |
| MA20 대비 +25% | 차단 |
| 장중 고점 후퇴 > 3% | 차단 |
| **+5% 급등 시 눌림 < 1%** | **차단 (추격 방지)** |
| 대형주 20개 | 차단 |

### 스코어링
- 등락률 구간: 2~4%=20, 4~6%=14, 6~7%=8
- 테마 점수, 거래대금, 수급 가산
- 장중 고점 후퇴율 기반 점수 조정

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
