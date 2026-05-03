# 리스크 관리 + 청산 전략

> 최종 갱신: 2026-05-04 (V자 재진입 1회 제한, 누적 감점 cap, 패널 통합 P0/P1)

## KR 리스크 (src/risk/manager.py + engine.py)

### 일일 한도
| 항목 | 값 | 비고 |
|------|---|------|
| 일일 최대 손실 | -5.0% | effective_daily_pnl **÷ initial_capital** 기준 (2026-04-18 분모 통일 — 대시보드 표시와 일치) |
| 일일 거래 횟수 | 10회 | daily_max_trades |
| 최대 포지션 수 | 8개 | max_positions |
| 기본 포지션 비율 | 25% | equity 대비 |
| 최소 현금 보유 | 5% | total_equity 대비 |

### 스마트 사이드카 (일일 손실 구간별)
| 구간 | 동작 |
|------|------|
| -3.5% ~ -5% (경고) | 시장 회복세면 허용, 하락세면 차단 |
| -5% ~ -12.5% (한도) | 방어 전략(RSI2/core/SEPA)만 허용 |
| -12.5%+ (하드스탑) | 전면 매수 차단 |

### 포트폴리오 동기화 (trading_lock, KR 전용)
> **배경**: 대형 손실 10건 중 7건이 KIS API 일시 응답 지연 → 복구 과정 비정상 상태에서 신규 진입 (03-27 DB손해보험 -14%, SK하이닉스 -11.89% 등)

- 연속 3회 실패 → `_sync_healthy=False` → **매수 차단** (`can_open_position()` 1.5단계)
- 1회 성공 → 즉시 복구 + 타임스탬프 초기화
- **타임아웃 안전장치**: 차단 지속 10분 초과 시 CRITICAL 로그 + 강제 해제 (운영 연속성 보장)
- 차단 로그: `[리스크] 동기화 복구 중 신규 매수 차단 ({symbol})` — 심볼별 60초 쿨다운
- 구현: `src/risk/manager.py` (`_sync_healthy`, `_sync_unhealthy_since`, `_sync_timeout_minutes=10`)
- 호출 경로: `kr_scheduler._sync_portfolio()` 성공/실패마다 `set_sync_status()` 호출 → `engine.on_signal → _risk_validator.can_open_position` 게이트에서 차단

### 당일 손절 종목 V자 반등 재진입 (2026-05-02~05-04)
> **배경**: 주간 후속복기(W18) stop_loss 24건 중 17건(71%)이 매도 후 +3%↑ 상승. 강세장에서 V자 반등을 못 잡는 패턴.

- **차단 해제 조건** (`risk/manager.py:_check_stop_loss_rebound`):
  1. 청산 후 30분 이상 경과 (즉시 추격 방지)
  2. 청산가 대비 +5% 이상 재돌파 (명확한 반등 확인)
- **1회 제한 (5/4 P0-A)**: V자 재진입 사용 후 재손절 → 당일 영구 차단
  - `_stop_loss_rebound_used` set으로 마킹
  - daily_max worst case 6.25% (1종목 2회 손절) → 5.0% 회귀
- **단축 평가**: V자 통과 시 다음 `_exited_today` 분기에서 재차단 안 됨 (`stop_loss_rebound_passed=True`)
- 로그: `[재진입] {symbol} 손절 후 V자 반등 감지 — 재진입 허용 (V자 반등 +X.X% (>=+5%))`

### 동일 종목 재진입 제한 (당일 청산 후, KR 전용)
- 30분 쿨다운 + 가격 조건 (`check_reentry_condition`):
  - **-5%~+5%**: 눌림/횡보 → 재진입 허용 (5/2 -3→-5% 완화)
  - **+5% 초과**: 재돌파 → 재진입 허용
  - **-5% 미만**: 급락 중 → 차단
- 부분 청산은 `_exited_today` 미등록 (5/3 P1-4) — 잔여분 손절 시 잘못된 기준선 방지

### 당일 청산 누적 쿨다운 (D+1 분리, KR/US 공통)
> **배경**: 4/14 -8.42% 사고 — 단일일에 다수 청산 + 다수 신규 매수 동시 발생, SK하이닉스 저점 청산 후 +16% 반등을 미스. "청산 당일은 현금 유지, 다음 거래일에 신규 진입" 규칙으로 교체.

- 카운터: `RiskManager._daily_exit_count` (+ `_daily_exit_count_date`) — `record_exit()` 호출 시 +1, 날짜 롤오버 자동 리셋
- 차단 로직: `can_open_position()` 마지막 단계(섹터 제한 뒤) — 다른 차단 사유(일일 손실/동기화/포지션 수)가 모두 우선
- 설정: `RiskConfig.daily_exit_cooldown_threshold: int = 3` (0이면 비활성 안전장치)
- 호출점: `src/schedulers/kr_scheduler.py` fill_check의 SELL 체결 기록 두 경로 (기존 `record_exit` 호출점 재사용, 신규 삽입 없음)
- 로그:
  - 카운터 증가: `[리스크] 당일 청산 누적: {n}/{threshold} ({symbol} @ {price})`
  - 차단: `[리스크] 당일 청산 {n}건 누적 — 신규 매수 차단 ({symbol}), 다음 거래일 재개 예정` (심볼별 60초 스팸 방지)
- 리셋: `reset_daily_stats()` (날짜 변경 감지 시) + `can_open_position()` 내부 방어적 날짜 체크
- 안전장치: threshold=0 이면 규칙 비활성 / 다른 차단이 우선이므로 기존 로직 회귀 없음 / 카운터는 KR/US 둘 다 증가하지만 US는 `max_daily_new_buys`가 이미 유사 기능 보완

## US 리스크

| 항목 | 값 |
|------|---|
| 일일 최대 손실 | -3.0% |
| 최대 포지션 수 | 10개 |
| 연속 손실 중단 | 3회 → 사이징 50% 축소 |
| 최소 현금 보유 | 10% |

## 크로스 전략 검증 (src/core/cross_validator.py)

### 10개 규칙 (KR) — 2026-05-03 패널 추천 추가

| 규칙 | 조건 | 효과 |
|------|------|------|
| 1 | RSI>70 + 추세 전략 (bull 제외) | **-5점** |
| 2 | 기관+외국인 동시 순매도 | theme/momentum/gap: **차단**, sepa_trend: **-10점** |
| 3 | 약세장 + theme_chasing / gap_and_go / rsi2_reversal / momentum_breakout | **차단** |
| 4 | 동일 섹터 N종목+ (KR=2, US=3) | **차단** |
| 5 | 당일 손절 동일 섹터 재진입 | -5점 |
| 6 | 등락률/ATR > 1.5 (추격매수) | -15점 (hard block, cap 예외) |
| 7 | MA200 하방 + 추세 추종 | **-5점** |
| 8 | 적자+고PBR (-10), 극단PER>50 (-5) | -5~10점 (적자+고PBR은 hard block, cap 예외) |
| 9 | 거래 메모리 L3 보정 | ±3점 |
| **10** | **전문가 패널 추천 (BUY only, 2026-05-03)** | **+max(2, conv×10×freshness)** |

### 누적 감점 cap (2026-05-03)
- **최대 누적 감점 -15점** 제한 (이전 최대 -26점 → 60-70점대 우수 종목 자동 차단 역설 방지)
- Hard block 예외 화이트리스트: `추격매수`, `RSI과매수`, `적자+고PBR` (단독 차단 의도 보존)
- 적용 위치: `cross_validator.py` 규칙 9 직후

### LLM 이중검증
- 조건: 점수 85+ AND 비강세장
- 한도: **10회/일** (비용 제어)
- 모델: GPT-5.4 (STRATEGY_ANALYSIS)
- 프롬프트 컨텍스트:
  - 지표 + 거래메모리 + Wiki 교훈
  - **regime 결합** (LLM regime + 패널 regime 보수적 결합 — 둘 중 bear → bear)
  - **주간 매크로 리스크** (전문가 패널 risk_factors 상위 5건/250자, 빈 시 가이드 미출력)
- fail-open: LLM 장애 시 매수 차단보다 기회 손실 방지 우선

### LLM 이중검증
- 조건: 점수 85+ AND 비강세장
- 한도: **10회/일** (비용 제어)
- 모델: GPT-5.4 (STRATEGY_ANALYSIS)
- 프롬프트 컨텍스트: 지표 + 거래메모리 + **Wiki 교훈**
- fail-open 의도: LLM 장애/한도 소진 시 매수 차단보다 기회 손실 방지를 우선. 규칙 1~9의 결정론적 게이트가 1차 안전장치.

## 청산 관리 (src/strategies/exit_manager.py)

### 분할 익절 단계
| 단계 | 조건 | 매도 비율 | 누적 |
|------|------|----------|------|
| 1차 (FIRST) | +5% | **20%** | 20% |
| 2차 (SECOND) | +15% | 25% | 45% |
| 3차 (THIRD) | +25% | 50% | 72.5% |
| 트레일링 | 3차 후 | 잔여 전량 | 100% |

### ATR 동적 손절
- 공식: `max(min_stop, min(max_stop, ATR × multiplier))`
- `min_stop_pct`: 3.5%
- `max_stop_pct`: **8.0%** (기존 6.0에서 확대)
- `atr_multiplier`: 2.0
- 예: ATR 6% → max(3.5, min(8.0, 12.0)) = **8.0%**

### 본전 보호
- FIRST 단계 이후: -1.5% 도달 시 본전 청산

### ATR 연동 트레일링 (ATR-linked trailing)
- **배경**: SK하이닉스 4/13 일시 저점에서 고정 3% 트레일링에 조기 청산 → 4/14~ +16% 반등 누락. 매크로 노이즈에 과민.
- **공식**: `effective_ts = min( max(config_ts, ATR_pct × atr_link_multiplier), atr_link_cap_pct )`
  - `atr_link_multiplier = 1.2` (기본)
  - `atr_link_cap_pct = 6.0%` (상한선, 손실 확대 방지)
  - 하한: REGIME/전략별 `trailing_stop_pct` 존중
- **예시**:
  - ATR 5%, config_ts 3% → effective = min(max(3.0, 6.0), 6.0) = **6.0%**
  - ATR 2%, config_ts 3% → effective = min(max(3.0, 2.4), 6.0) = **3.0%** (기존 방식 유지)
  - ATR 10%, config_ts 3% → effective = min(max(3.0, 12.0), 6.0) = **6.0%** (상한 clamp)
- **비활성 조건**: ATR 미전달(fallback) / 코어홀딩(is_core=True, 고정 트레일링 우선)
- **전달 경로**: 매수 체결 시 `_pending_signal_cache[symbol].metadata.atr_pct` → `register_position(atr_pct_hint=...)`
- **로그**: 트레일링 발동 시 `ATR-linked trailing: 고점 대비 X% (한도=-Y%)` 형태로 출력
- **상태 저장**: `PositionExitState.effective_trailing_stop_pct` 필드에 보관

### 레짐별 파라미터 (trending_bull 예시)
| 항목 | bull | bear | sideways |
|------|------|------|---------|
| SL | 5.0% | 3.0% | 4.0% |
| TS | 4.0% | 2.5% | 3.0% |
| TP1 | 5% | 3% | 4% |
| TP2 | 15% | 8% | 10% |
| TP3 | 25% | 15% | 20% |

## 시장 체제별 동적 파라미터 (market_regime.py → engine.py)

| 파라미터 | bull | neutral | sideways | bear |
|----------|------|---------|---------|------|
| min_score_adj | -10 | 0 | +3 | +10 |
| max_daily_new_buys | 6 | 4 | 3 | 2 |
| position_mult_boost | 1.2x | 1.0x | 0.9x | 0.7x |
| max_positions_adj | +2 | 0 | 0 | -2 |
| base_position_pct | 30% | 25% | 25% | 20% |
| min_cash_reserve | 3% | 5% | 5% | 10% |

bull 시 효과: max_positions 8→10, 현금 5→3%, 비중 25→30% → **현금 적극 배치**

## 코어홀딩 초과 비중 관리

| 기준 | 동작 |
|------|------|
| 코어 비중 35%+ | 텔레그램 경고 (24시간 쿨다운) |
| 코어 비중 40%+ | 금요일 14:00 초과분 50% 트림 |
| 개별 종목 20%+ | 15%까지 축소 (max_position_pct) |
| 비코어 pool | 코어 실점유분 차감 (초과 시 보호) |

## ATR 포지션 사이징 (src/utils/sizing.py)

```
ATR ≤ 2%  → 1.0x (정상 비중)
ATR  5%   → 0.7x (30% 축소)
ATR  8%   → 0.4x (60% 축소)
ATR ≥ 10% → 0.3x (70% 축소)
구간 내: 선형 보간
```

### ATR=0 가드 (전 전략 통일)
- SEPA, RSI2, Gap&Go, US Momentum, US SEPA → ATR 0/None 시 **진입 차단**
- US Earnings Drift → 0.8x 폴백 (lenient, 갭 자체가 고변동)

## RLAY 유형 매도 무한루프 방지

- sell_qty > 실제 보유 수량 → **자동 클램핑**
- 연속 3회 매도 실패 → 포트폴리오 동기화 강제 + 카운터 리셋
- 매도 성공 시 → 쿨다운 + 실패 카운터 `delattr` 정리
