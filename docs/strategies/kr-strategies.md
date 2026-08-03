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

## 3-B. VCP Breakout (선행 발굴 라인, 2026-08-03~)

### 개요
Minervini VCP(Volatility Contraction Pattern) = 상승 전 변동성이 단계적으로 수축하고
거래량이 마르는 구간. **아직 안 움직인 종목**을 잡는 유일한 라인이다.

코드는 전략 클래스가 아니라 스크리너 라인으로 구현된다:
- 탐지: `src/signals/strategic/vcp_detector.py` (기존)
- 후보 생성: `swing_screener._filter_vcp_breakout()`
- 시그널: `batch_analyzer._generate_vcp_signals()` → `StrategyType.VCP_BREAKOUT`

### 왜 독립 라인인가
도입 전 VCP는 **오버레이 가점**으로만 쓰였다. 그런데 오버레이는
`candidate.score < 50`이면 스킵되고, candidates는 RSI2/SEPA 필터 통과자만이다.
VCP는 정의상 "과매도도 아니고 추세 진행도 아닌" 구간이라 두 필터를 통과하기 어렵다.
실측 2026-07-31: VCP 12종목 탐지 → 오버레이 반영 2종목.

### 후보 게이트 (전부 AND)
| 조건 | 값 | 근거 |
|---|---|---|
| VCP 점수 | ≥ 60 | 실측 분포상 70이면 하루 1~5개뿐 → 라인이 죽는다 |
| MA 정배열 | 50 > 150 > 200 | 추세 기반 없는 수축은 하락 중 눌림과 구분 불가 |
| 거래량 감소 | True | 수축의 핵심 조건 |
| 수축 횟수 | ≥ 2 | 1회는 우연한 조정과 구분 불가 |
| 우선주 | 제외 | 코드 끝자리≠0. 유동성 부족으로 돌파 슬리피지 과다 |
| 중복 | RSI2/SEPA 후보면 제외 | 동일 종목 이중 노출 방지 |

### 진입 / 청산
- **진입**: 20일 고점 × (1 + 0.5%) 돌파 확인가 — `entry_mode="breakout"`
  현재가가 트리거 미만이면 발행하지 않고 pending 유지 (09:01 / 12:30 / 13:50 재확인)
  - 실제 진입 밴드는 `[트리거, 트리거 × (1+슬리피지)]` — 레짐별 3~5%.
    돌파 후 과열 갭업은 `max_entry_price` 상단에서 걸러진다.
  - ⚠️ `execute_pending_signals()`는 끝에서 pending을 **전부 비웠다**.
    2026-08-03부터 스킵 사유별로 재시도 가치를 판정해 이월한다 (아래 표).
    이 이월이 없으면 첫 윈도우에서 삭제돼 "재확인"이 성립하지 않는다.
  - 이월분의 재진입 중복은 상단 "이미 보유 중" 체크가 막는다. 만료(익영업일 15:30)
    시점을 넘긴 건은 이월에서 제외된다.
- **손절**: 20일 최저 (단, 진입가 대비 최대 -8%)
- **목표**: 진입가 +15% (실제 청산은 ExitManager 단계 익절이 담당)
- **배분**: `strategy_allocation.vcp_breakout = 10%`, per-position 15%

### 대기 시그널 이월 정책 (2026-08-03, 전 전략 공통)

`execute_pending_signals()`는 실행 후 pending을 비운다. 예전에는 **전량 삭제**여서
12:30 낮스캔·13:50 자본활용률 체크가 빈 파일을 읽었다. 이제 스킵 사유를
"상태가 바뀌면 통과할 수 있는가"로 나눠 그런 것만 다음 윈도우로 넘긴다.
정의는 `batch_analyzer.CARRY_REASONS`.

| 사유 | 처리 | 근거 |
|------|------|------|
| `quote_fail` 현재가 조회 실패·이상값 | 이월 | 일시적 API 장애 |
| `breakout_wait` 돌파 트리거 미달 | 이월 | 장중 돌파 가능 |
| `above_band` 현재가 > max_entry_price | 이월 | 눌리면 밴드 복귀 가능 |
| `gap_up_score` 갭업 요구점수 미달 | 이월 | 갭이 줄면 요구치도 낮아짐 |
| `intraday_gate` 장중 급락 게이트 | 이월 | 급락 상태 해제 시 통과 |
| `strategy_limit` 전략별 동시보유 한도 | 이월 | 청산되면 슬롯 발생 |
| `error` 실행 중 예외 | 이월 | 일시적 |
| 만료 | 폐기 | 시간이 지나면 더 확실히 만료 |
| SEPA 14:30+ 차단 | 폐기 | 이후 시각은 더 늦어질 뿐. 익일 이월은 하루 지난 근거로 진입 |
| 갭다운 | 폐기 | 게이트 취지가 "악재 의심 종목은 당일 손대지 않는다". 오후 반등해도 전일 종가 기준 근거는 무효 |
| 이미 보유 중 | 폐기 | 재시도 대상 아님 |

- **상한**: `MAX_CARRY_RETRIES = 8`. 재시작이 반복되면 윈도우 수와 무관하게 재시도가
  누적될 수 있어 둔 폭주 방지선이다. 만료(익영업일 15:30)가 1차 방어선.
- **중복 매수 없음**: 이월분도 "이미 보유 중" 체크를 매번 통과해야 한다.
- 이월 건수는 사유별로 로깅된다 — `[배치분석] 다음 윈도우 이월 N개 (사유 n, ...)`.

### 레짐 대응
- 하락장(bear): SEPA와 함께 **전면 차단** (돌파 실패율 급등)
- 주의장(caution): SEPA와 동일하게 min_score +10 상향

### 계측
스캔마다 탈락 사유가 집계된다:
```
[스윙스크리너] VCP 독립 라인: 18종목 중 채택 0개
  (탈락: 중복 7, 점수미달 7, MA비정배열 2, 거래량미감소 1, 우선주 1)
```
과거 5일 캐시 검증: 평균 4.0개/일 통과(중복 제외 전), 중복률 39% 적용 시 실질 2~3개/일.

### 설정 (`config/default.yml` → `kr.strategies.vcp_breakout`)
```yaml
vcp_breakout:
  enabled: true
  min_score: 60            # VCPDetector 점수 하한
  min_signal_score: 60     # 복합 점수 하한 (시그널 발행)
  breakout_buffer_pct: 0.5 # 돌파 확인 버퍼
```

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

### 저변동성 감점 (`core_screener.py`, 2026-08-03~)
Low Volatility Factor(재현 Sharpe 0.717) 응용 — 코어홀딩 후보 점수에 **감점 전용** 항목 추가.
- 지표: 일별 수익률 60일 표준편차 `ret_vol_60d` (가격 수준 분산인 volatility_20d와 다름 —
  추세 종목에서 과대평가되지 않는 일수익률 σ)
- 감점: σ≥4% → -10 / σ≥3% → -6 / σ≥2.5% → -3 / 그 외 0 (데이터 없으면 감점 없음)
- 가점 없이 감점만: 기존 min_score(70) 보정을 흔들지 않으면서 급등락형 종목만 배제
- 배경: 코어홀딩은 장기 보유라 급등락형이 들어오면 stale/손절 사고로 직결
  (2026-06-04 -263k 사례)
- **검증 (quick_backtest --idea lowvol, KR 60종목 12개월 워킹포워드)**:
  Q5 고변동군(σ4.3~9.1%) 20일 포워드 +0.41%·승률 39.6% vs
  Q1 저변동군 +5.63%·66.1% — 감점 임계 σ≥4%가 Q5 경계와 일치, 설계 타당 확인

### 자산 확장 감점 (`core_screener.py` + `fundamentals/asset_growth.py`, 2026-08-03~)
Asset Growth Effect(재현 Sharpe 0.835, 연간 리밸런싱) 응용 — 역시 **감점 전용**.
- 데이터: DART `fnlttSinglAcnt` 사업보고서 자산총계 (당기/전기) → 전년 대비 증가율
  - 연결(CFS) 우선 → 별도(OFS) 폴백, 30일 디스크 캐시 (연 단위 데이터)
  - corp_code 맵은 기존 DartChecker 인프라 재사용
- 감점: 증가율 ≥50% → -5 / ≥30% → -3 / 그 외 0 (데이터 없으면 감점 없음, fail-open)
- 근거: 증자·차입·인수로 총자산을 급격히 불린 기업의 후속 수익률 저하 (퀄리티 팩터)
- 스캔 흐름: `run_full_scan` 4.5단계 `_enrich_asset_growth` (수급 보강과 동일 패턴)

---

## 캘린더 시즈널리티 오버레이 (`src/utils/calendar_seasonality.py`, 2026-08-03~)

독립 전략이 아니라 **모든 전략의 매수 사이징 배율**로 동작하는 오버레이.
awesome-systematic-trading 재현 백테스트의 저변동 캘린더 이상현상을 응용했다.

| 윈도우 | 적용 시장 | 배율 | 근거 (재현 Sharpe/변동성) |
|--------|----------|------|--------------------------|
| 월말 마지막 2거래일 + 월초 첫 3거래일 (turn-of-month) | KR + US | ×1.10 | 0.305 / 7.2% |
| 옵션만기주 (3번째 금요일 낀 주 월~금) | US 전용 | ×1.05 | 0.452 / 5.0% |

- **부스트만 있고 차단/축소 없음** — 근거 없는 방어 규칙은 만들지 않는다
- KR 옵션만기주는 미적용 (논문 근거가 미국 시장, KR 만기주는 수급 변동성만 큼)
- 적용 지점: KR `engine.py` 사이징(배율 후 max_position_pct 상한 재적용),
  US `us_scheduler.py` (ATR×체제×캘린더 통합 배율, 날짜는 미 동부 기준)
- 비활성화: 환경변수 `CALENDAR_SEASONALITY=0`
- KR 거래일 판정은 `is_kr_market_holiday()`, US는 주말 제외 근사(공휴일 미스는 ±1일 오차)
- **검증 (quick_backtest --idea tom)**: 10년 기준 ToM 윈도우 일평균이 그 외 대비
  SPY +0.092% vs +0.052% / KOSPI +0.086% vs +0.048% / QQQ +0.115% vs +0.074%로
  유효. 단 **최근 3년은 효과 소멸** (윈도우가 오히려 낮음) — 장기 유효·최근 약화로
  판단해 ×1.10 소폭 부스트만 유지. 분기마다 재검증하고 계속 부진하면 제거할 것.

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
