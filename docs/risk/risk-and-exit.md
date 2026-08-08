# 리스크 관리 + 청산 전략

## 2026-08-04 엔진 합동 리뷰 반영 (P0 4 + P1 8) — 동작 변경 요약

- **매도 폴백**: 미체결 90초 시장가 전환 시 수량 = 원 주문 수량(전량 아님).
  분할익절/코어 트림이 전량 청산으로 번지지 않는다
- **포지션 교체 축출**: `no_auto_exit_symbols`(exit_exempt) 종목은 축출 후보 제외
- **당일 손절 재진입 정책 활성화**: `record_exit(exit_type=stop_loss)` 시
  `_stop_loss_today` 등록 → V자 +5% 재돌파 + 당일 1회 제한 + 스크리닝 후보 제외가
  이날부터 실제 동작 (이전엔 전부 데드 코드)
- **min_stop_pct(4%) 클램프는 ATR 동적 손절에만** 적용 — 전략별/레짐별/장중급락
  오버라이드의 타이트 손절(2.0~3.5%)은 명시값 그대로 발동
- **장중급락 SL/TS 강화**: None 가드 + dynamic SL·effective TS 동시 조임으로 실효화.
  vcp_breakout도 전략별 청산 파라미터(SL 4.0/TS 2.5/stale 3d) 등록
- **daily_stats**: 원자적 쓰기 + 장중 복원 실패 시 리셋 생략 (일일손실 기준선 보호)
- **스케줄러 루프 슈퍼바이저**: 예외로 죽은 루프 60초 후 자동 재기동 + 텔레그램 경보
- **ExitManager stage 파일**: 저장 시점 날짜로 파일명 갱신, 빈 파일도 유효 상태로 복원

> 최종 갱신: 2026-06-16 (TRAILING 단계 슬롯 가중 추가 디스카운트)

## max_positions 잔여 비율 가중 카운트 (2026-05-06~)

분할익절 진행된 포지션이 신규 매수를 막지 않도록 슬롯 가중치 적용:
- 기본: `weight = remain / orig` (잔여 수량 / 원본 수량)
- **NONE/FIRST**: floor 0.2 (1차 익절은 잔여 80%로 아직 큰 포지션)
- **SECOND/THIRD** (2026-06-16 확장): 추가 × 0.7 곱셈, floor 0.15
- **TRAILING** (2026-06-16): 추가 × 0.5 곱셈, floor 0.1
- 정당화: 익절 진행 = 자금 회수 → 슬롯도 비례 양보, 상승장 신규 기회 확보
- 구현: `src/risk/manager.py:_get_position_weight`
- 비교 게이트: `non_core_weighted >= max_positions` (코어홀딩은 별도 슬롯)

## KR 리스크 (src/risk/manager.py + engine.py)

### 팩터 버킷 위험예산 (2026-08-08~, shadow 관측 중)

상관 전략 묶음의 총 노출을 팩터 단위로 캡 — 개별 전략 캡(G5_budget) 합만큼
동시 만석되는 것을 방지하는 상위 게이트 (`engine.RiskManager._check_factor_budget`,
게이트명 `G5_factor`).

| 버킷 | 캡 (% equity) | 전략 |
|------|------------|------|
| trend | 65% | sepa_trend, gap_and_go, momentum_breakout, vcp_breakout, strategic_swing (개별 합 75%) |
| quality | 20% | core_holding, value_growth |
| reversion | 10% | rsi2_reversal, theme_chasing (전부 폐지 — 예약) |

- 설정: `default.yml risk.kr.factor_budgets` (`RiskConfig.factor_budgets`)
- **`enforce: false`(현재)**: 초과 시 `(shadow) 팩터 예산 초과 관측` 로그만 남기고 통과
  — 캡 적정성 관측 후 true로 승격
- fail-open: 설정 부재/오류 시 통과 (개별 전략 캡이 1차 방어선)
- 전략은 첫 매칭 버킷에만 귀속

### 일일 한도
| 항목 | 값 | 비고 |
|------|---|------|
| 일일 최대 손실 | -5.0% | effective_daily_pnl **÷ total_equity** 기준 (2026-06-23 변경 — 외부 계좌 합산 시 .env INITIAL_CAPITAL 왜곡 회피, 대시보드와 일치) |
| 일일 거래 횟수 | 10회 | daily_max_trades — BUY **주문 단위** 카운트 (8/5: 부분체결 증분 중복 제거, `_counted_buy_order_ids`), BUY 체결 시 즉시 영속화 + DB 백필은 KR 심볼 한정·SELL 존재와 독립 |
| 최대 포지션 수 | 8개 | max_positions (잔여 비율 가중 카운트) |
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
  - `_stop_loss_rebound_used` set으로 마킹 (8/5: `stop_loss_rebound_used.json`
    파일 영속화 — 재시작 시 1회 제한 우회 방지)
  - daily_max worst case 6.25% (1종목 2회 손절) → 5.0% 회귀
  - **1회권 소모 시점 = 매수 체결 확인 (8/5 P1)**: `on_buy_filled()` —
    can_open_position 검증 통과 시점 마킹은 후속 게이트(현금/브로커 거부)에서
    매수 무산 시에도 당일 차단되는 오차단을 만들었음. fill_check BUY + sync
    신규 포지션 두 경로에서 호출
- **단축 평가**: V자 통과 시 다음 `_exited_today` 분기에서 재차단 안 됨 (`stop_loss_rebound_passed=True`)
- 로그: `[재진입] {symbol} 손절 후 V자 반등 감지 — 재진입 허용 (V자 반등 +X.X% (>=+5%))`

### 동일 종목 재진입 제한 (당일 청산 후, KR 전용)
- 30분 쿨다운 + 가격 조건 (`check_reentry_condition`):
  - **-5%~+5%**: 눌림/횡보 → 재진입 허용 (5/2 -3→-5% 완화)
  - **+5% 초과**: 재돌파 → 재진입 허용
  - **-5% 미만**: 급락 중 → 차단
- 부분 청산은 `_exited_today` 미등록 (5/3 P1-4) — 잔여분 손절 시 잘못된 기준선 방지
- **등록 경로 독립화 (8/5 P1)**: `risk_manager.record_exit()`는 trade journal 기록과
  독립된 선행 블록에서 실행 (저널 예외 시 재진입 차단이 무음 실패하던 것).
  `is_full_exit`는 매도 전 스냅샷 수량 비교 + ExitManager 상태 소멸로 판정

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
| 3-2 | caution + gap_and_go (KR, 2026-05-28) | **차단** (5/28 -357k 사고) |
| 3-3 | sideways/neutral + sepa_trend (KR, 2026-06-14) | **차단** (6/12 SK하이닉스 -272k 사고) |
| 3-4 | 14:30+ gap_and_go 일 2건 초과 (KR, 2026-06-14) | **차단** (6/12 5건 집중 → 오버나잇 -175k 사고) |
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

### US 전략별 max_holding_days 배선 (2026-08-03~)

이전에는 US 전략 config의 `max_holding_days`(예: SEPA 20일)가 ExitManager에
전달되지 않아 **전 전략이 글로벌 기본 10영업일**로 강제 청산됐다 (배선 갭).

- `us_scheduler._strategy_max_holding(eng, strategy_value)` — 포지션의 strategy
  문자열로 전략 인스턴스의 `max_holding_days`를 조회. 미매칭/미설정이면 None → 글로벌
- **배선 지점 (2곳)**: ① 매수 체결 등록 ② 재시작 복구 재등록
  (`pos.strategy or _symbol_strategy` 폴백). ExitManager 상태 파일이
  `max_holding_days`를 영속화하므로 재시작에도 유지된다
- **제외**: sync_detected 포지션(전략 불명 외부 진입)은 의도적으로 글로벌 10일 유지
- `0`은 '무제한' 의미이므로 falsy 판정 없이 그대로 전달 (`is not None` 비교)
- 적용 결과: SEPA 신규 매수 10→**20영업일**, earnings_reversal(비활성) 3일,
  momentum은 config에 값이 없어 종전대로 글로벌 10일. **기존 오픈 포지션은
  상태가 이미 저장돼 있어 종전 10일 유지** — 신규 매수부터 적용

### 종목별 자동매도 절대 금지 (exit_exempt / no_auto_exit_symbols, 2026-06-23~)
- **용도**: 수동 풀매수·장기보유 종목을 모든 자동매도 로직에서 영구 제외 (코어보다 강한 보호 — 코어는 리밸런싱 교체 가능하나 이건 그것도 면제).
- **설정**: `config kr.no_auto_exit_symbols: ['087010', ...]` — 기동 시 `run_trader._initialize_kr`가 `exit_manager.add_exit_exempt()`로 복원(재시작에도 유지).
- **차단 경로 (7개, 누락 시 손절/청산 발생)**:
  1. ExitManager `update_price` 진입부(`_exit_exempt`) → 손절·트레일링·분할익절·stale·보유기간초과 일괄
  2. `kr_scheduler._check_exit_signal` (WS 실시간) → 즉시 return
  3. `kr_scheduler._run_position_eod_llm_check` → LLM 종가점검 청산 제외
  4. `batch_analyzer.monitor_positions` 루프 → RSI2 청산·보유기간초과·ExitManager 릴레이 스킵
  5. `batch_analyzer._preemptive_stale_exit_on_bear` → 약세장 선제 stale 청산 스킵
  - 코어 경로(rebalance/stale/early-warning)는 `strategy == "core_holding"` 한정이라 strategy="manual" 종목엔 미적용.
- **수동 매수**: `config kr.manual_buy_orders: [{symbol, name, exit_exempt}]` → 기동 시 1회 실행(보유 시 자동 스킵). KIS 시장가는 주문가능금액을 상한가 기준으로 계산하므로 marketable 지정가(현재가+0.6%)로 전액 체결.
- ⚠️ **손절 부재 = 하락 100% 노출.** 청산은 전적으로 수동 판단. (펩트론 087010: 2026-06-23 사용자 지시로 전액 매수 + 손절 면제)

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
- **백테스트 동기화 (2026-08-02)**: `scripts/backtest_strategies.py`도 동일 공식을 사용한다.
  이전에는 백테스트만 고정 3%를 써서 실제보다 비관적인 결과가 나왔다
  (SEPA 3개월 -12.02% vs 실제 설정 -7.19%). 진화 백테스트 게이트가 올바른 판정을 내리려면
  **두 구현이 항상 같아야 한다** — 한쪽을 고치면 반드시 다른 쪽도 고칠 것.

### 1차 익절 재조정 (2026-08-02, 백테스트 검증)
- **변경**: `+5% / 비중 20%` → **`+10% / 비중 10%`**
- **배경**: SEPA 81건 청산 분해 결과 1차 익절이 27.2%를 차지하는데 **평균 1.9일**에 발동했다.
  평균 익절 `+5.20%` < 평균 손절 `-6.21%` — 손익 비대칭이 뒤집혀 있었다.
  반면 2차 익절(+15%)까지 살아남은 건은 평균 **+23.21%**.
- **검증**: 3·6개월 × 60·120종목 4개 시나리오에서 손익비 전부 개선
  (1.53→1.85 / 1.67→2.01 / 1.98→2.21 / 2.03→2.70), 수익률 3/4 개선.
- **트레일링은 건드리지 않았다** — 5.5~6.0%로 완화하면 오히려 악화(-7.28%, -7.56%).
  현재 4.5% + cap 6.0%가 적정.
- ⚠️ **변경 시 5곳을 모두 고칠 것**: `default.yml` / `evolved_overrides.yml` /
  `ExitConfig` 기본값 / **`REGIME_EXIT_PARAMS`**(레짐별 값이 config를 덮어쓰므로 누락하면 무효화됨) /
  **`run_trader.py`의 `_strategy_exit_params`**(전략별 오버라이드가 config보다 우선한다).
- 🩹 **2026-08-03 후속**: 위 5번째를 놓쳐 `sepa_trend`에 `+5%/20%`가 남아 있었고,
  검증한 `+10%/10%`가 SEPA에는 **한 번도 적용되지 않았다**. 해당 키를 제거해
  `register_position(first_exit_pct=None)` → config 상속으로 되돌렸다.
  같은 사고 방지를 위해, 전략별 오버라이드는 **그 전략만의 고유값**(stale_high_days 등)만 두고
  공통 파라미터는 config에 맡긴다.
- **한계**: 검증 구간이 2026-05~08 하락장에 집중. 3개월·120종목에서는 악화.
  상승장 도래 시 재검증 필요.

### 레짐별 파라미터 (trending_bull 예시)
> 출처: `src/strategies/exit_manager.py` `REGIME_EXIT_PARAMS` (2026-08-02 반영 후 실제 값)

| 항목 | trending_bull | neutral | ranging | turning_point | trending_bear |
|------|------|------|------|------|------|
| SL | 5.0% | 4.0% | 4.0% | 4.0% | 3.5% |
| TS | 4.0% | 3.0% | 2.5% | 3.0% | 2.0% |
| TP1 | 10% | 10% | 8% | 8% | 5% |
| TP2 | 15% | 12% | 8% | 10% | 8% |
| TP3 | 25% | 20% | 14% | 18% | 14% |
| stale_high_days | 7 | 5 | 4 | 5 | 3 |

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

## 코어홀딩 A안 청산 파라미터 (2026-05-11~)

| 항목 | 값 | 비고 |
|------|---|------|
| stop_loss_pct | 10% | 이전 15 → 10 (조기 손절 강화) |
| trailing_stop_pct | 12% | 이전 8 → 12 (느슨한 추세 추종) |
| trailing_activate_pct | 10% | +10% 도달 후 트레일링 시작 |
| 분할익절 ratio | 0/0/0 | OFF (장기 추세 끝까지) |
| ATR-linked trailing | 비활성 | `not is_core` 가드, 고정 12% 우선 |
| max_holding_days | 무제한 | 0 |

**stale 자동 컷 (evolved_overrides, 2026-06-04 밴드 확대)**
- Tier 1 알림: 20영업일 + **±7%** (이전 ±3 → 느린 손실 -5~7% 패턴까지 포착)
- Tier 2 자동매도: 30영업일+±7% OR 20영업일+**±5%**+거래량 50% 미만 (이전 ±3/±2)
- 변경 배경: 2026-06-04 오리온(-5.4%/32일)·SK(-6.9%/6일) 추세 진입 실패 종목이
  ±3% 사각지대에 갇혀 자동청산 못한 사고 후속

**리밸런싱 주기**: 격주 (`rebalance_interval_weeks=2`, 이전 월 1회)

**예산 (2026-06-04 정상화)**: 20% (이전 10%, KOSPI 강세장 복귀로 30% 시절 수준 복원
검토 차 우선 20% 단계 적용. strategic_swing 38.4→28.4 상쇄)

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
