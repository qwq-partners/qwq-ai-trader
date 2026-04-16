# US 전략 상세

> 최종 갱신: 2026-04-15

## US 엔진 고도화 (2026-04-02~)

KR 엔진의 3대 기능을 이식:
1. **ATR 포지션 사이징** — 전 전략 통일
2. **시장 체제** — SPY/QQQ 기반 bull/bear/sideways
3. **크로스 검증 게이트** — 6규칙 (수급 제외, bear시 어닝스 허용)

## 1. Momentum Breakout (`src/strategies/us/momentum.py`)

- 20일 고가 돌파 + 거래량 2.5x 확인 (기존 2.0x → 2.5x 상향)
- **min_breakout_pct: 2.0%** (기존 0.8% → 2.0% 상향, 노이즈 필터링 강화)
- RSI > 80 차단
- RS Ranking >= 80 시 +10점 보너스
- ATR = 0/None → **진입 차단**
- `position_multiplier` metadata 전달
- 고점수(85+) → min 0.75x 보장

## 2. SEPA Trend (`src/strategies/us/sepa_trend.py`)

- 미너비니 5/6 기준 통과
- **MA200 상향 판정: 데이터 220봉 미만 시 기준 미통과** (기존 자동 통과 → 차단)
- **RS Rating < min_rs_rating(70) → 진입 차단** (기존 감점(-5) → 완전 차단)
- RS Ranking 보너스 (80+ → +10, 70+ → +5)
- ATR = 0/None → **진입 차단**
- `position_multiplier` metadata 전달
- `from loguru import logger` 필수 (P0 수정 완료)

## 3. Earnings Drift (`src/strategies/us/earnings_drift.py`)

- **현재 버전: 갭+거래량 프록시 기반 (실적 확인 API 미연동)**
- 갭 **7.0%+** (기존 5.0% → 상향, 일반 뉴스 갭 필터링 강화)
- 거래량 **3.5x+** (기존 2.5x → 상향, 진정한 어닝 반응 폭증 필터)
- 갭 유지(close > open)
- ATR 가드: **lenient** (0/None → 0.8x 폴백, 진입 허용)
  - 어닝 서프라이즈는 갭 자체가 고변동 → 데이터 없어도 기회 포착
- bear 장에서도 허용 (크로스검증 예외)

## US 시장 체제 (`src/core/us_market_regime.py`)

### 판단 기준
- SPY 60% + QQQ 40% 가중 평균 등락률
- bull: avg > +0.7%, bear: avg < -0.7%, sideways: 나머지
- vs_open 역방향 시 sideways로 완화

### 체제별 파라미터
| 체제 | min_score_adj | max_buys | position_boost |
|------|-------------|----------|---------------|
| bull | -5 | 3 | 1.1x |
| sideways | +3 | 2 | 0.9x |
| bear | +10 | 1 | 0.7x |
| neutral | 0 | 3 | 1.0x |

### 적용 위치
- `_run_screening()`: 점수 보정 + max_buys 제한
- `_process_signal()`: position_mult_boost 적용

## US 크로스 검증 (6규칙)

| 규칙 | 내용 | US 특이사항 |
|------|------|-----------|
| 1 | RSI 과매수 (>70) + 추세 전략 → -10 | 동일 |
| 3 | 약세장 공격 전략 차단 | **momentum만 차단** (SEPA/어닝스 허용) |
| 4 | 동일 섹터 N종목 과집중 (`max_positions_per_sector`) | US=3 (설정 참조) |
| 6 | ATR 대비 등락률 과다 → -15 | 동일 |
| 7 | MA200 하방 추세 추종 → -10 | 동일 |
| 8 | 펀더멘탈 밸류에이션 | 동일 |

**US 제외 규칙**: 규칙2(수급 데이터 없음), 규칙5(전면 차단으로 불필요), 규칙9(거래메모리 미구현)

### indicators 주입
`_process_signal()`에서 `eng._indicator_cache.get(symbol, {})`를 metadata에 주입 → 크로스검증 규칙 1,6,7,8 활성화
