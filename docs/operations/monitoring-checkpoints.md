# 모니터링 체크포인트

> 변경이 적용된 후 검증해야 할 항목을 시점·전략별로 정리합니다.
> 신규 모니터링 항목은 변경 커밋과 함께 이 문서에 추가합니다.
> 검증 완료 시 ✅ 표시 + 결과 요약 1줄 기록 후 다음 사이클로 이동.

## 활성 체크포인트

### 2026-05-12~ — 슬리피지 체제 분기 (bull 5% / neutral·bear 3%)

- **커밋**: 적용 예정 (2026-05-05)
- **변경**: `config/evolved_overrides.yml batch.max_entry_slippage_pct`
  - 단일 float 3.0 → dict `{bull: 5.0, neutral: 3.0, caution: 3.0, bear: 3.0}`
  - `batch_analyzer.py:166-185, 308-312` regime별 lookup
- **근거** (3-전문가 분석, 5/4):
  - trade-analyst: 갭업 +5~10% 구간 승률 53.8% (전체 평균 46.3% 초과)
  - 95-100점 구간 승률 37.9% (-0.58%) ← 추격매수 패턴 데이터
  - 09:00~09:29 진입 31.3% / -740k ← 장초반 차단 데이터 지지
  - market-analyst: 강세장 갭업 + 거래량 = 추세 시작 신호
  - strategy-advisor: bull 한정 완화가 daily_max -5% 영향 미미
- **5/4 케이스**:
  - 차단된 6종 평균 +9.8% 수익 — 강세장 갭업 미포착
  - 키움증권 +6.2% (갭 +3%로 차단) → 5%로 포착 가능
  - 삼성증권 +28.3% (갭 +25%) → cross_validator 추격매수 -15로 차단 (안전)
- **효과 가설**:
  - [ ] bull 레짐에서 신규 통과 종목 평균 PnL ≥ 0%
  - [ ] bear/neutral 레짐 거래는 변경 전 대비 ±2%p 이내
  - [ ] 일일 -5% 도달 0~1회
- **롤백 트리거**:
  - bull 갭업 통과 종목 3건 이상 -7%↓ 손절 → 24h 내 환원
  - 5영업일 누적 손익비 < 1.0 → bull 5→4%
- **검증 SQL**:
  ```sql
  SELECT
    market_regime,
    COUNT(*) AS n,
    ROUND(AVG(pnl_pct)::numeric, 2) AS avg_pnl,
    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins
  FROM trades
  WHERE market='KR'
    AND exit_time::date >= '2026-05-06'
    AND exit_type NOT IN ('kis_sync','sync_reconcile','sync_closed','sync_partial')
  GROUP BY market_regime;
  ```


### 2026-05-09~ — theme_chasing 전략 폐지 효과

- **커밋**: 적용 예정 (2026-05-04)
- **변경**: `config/evolved_overrides.yml`
  - `theme_chasing.enabled: true → false`
  - `strategy_allocation.theme_chasing: 5.0 → 0.0`
  - `strategy_allocation.sepa_trend: 44.2 → 49.2` (재배분)
- **근거** (param-optimizer DB 검증):
  - 누적 44건 -300k 손실
  - 점수 구간 75-85: 11~21% 승률 / -1.01%~-1.08% (최악)
  - 차단되는 70-75: 75% 승률(n=4) — 75 임계 역효과 판명
  - 보유 0일 78%, manual 청산 67%
  - 1일+ 잔류 4건만 75% 승률
- **확인 항목**:
  - [ ] 5/4부터 theme_chasing 신규 진입 0건 확인
  - [ ] sepa_trend allocation 49.2% 정상 작동 (한도 미초과)
  - [ ] 자본 활용률 향상 (theme 5% → sepa로 이동 후 진입 빈도)
- **재활성화 조건** (5/16 토 평가):
  - 보유 기간 필터(4일+ 우대) 추가
  - 또는 80+ 점수만 통과 (param-optimizer 데이터 기반 임계)
  - 단, 매크로 강세 테마장(예: 2차전지 폭등)에서만

### 2026-05-04 코드리뷰 P0/P1 즉시 수정

- **커밋**: 적용 예정 (2026-05-04)
- **변경**: `src/risk/manager.py` + `src/core/cross_validator.py`
  - **P0-A**: V자 재진입 1회 제한 (재손절 시 당일 영구 차단)
    - `_stop_loss_rebound_used` set 신규
    - daily_max worst case 6.25% → 5.0% 회귀
  - **P1-2**: 패널 보너스 side==BUY 분기 (매도 점수 부풀림 차단)
  - **P1-3**: 패널 21일 폐기 + freshness <0.5 보너스 0
  - **P1-1**: stale lock 6시간 → 실패/None은 30분 단축
  - **P2-5**: panel_risks 빈 시 LLM 가이드 미출력
- **확인 항목**:
  - [ ] V자 재진입 후 재손절 종목 → "당일 V자 반등 재진입 1회 제한" 로그
  - [ ] 패널 보너스가 sell 시그널에 적용되지 않는지
  - [ ] 21일 경과 패널 시 보너스 미적용 로그
- **롤백 트리거**: V자 재진입 차단으로 정상 진입 기회 누락 5건+ 시 검토

### 2026-05-09~ — 전문가 패널 통합 효과 (P0+P1+P2)

- **커밋**: 적용 예정 (2026-05-03)
- **변경**: `src/core/cross_validator.py`
  - P0: 모든 전략 진입 시 패널 추천 보너스 (`+max(2, conv × 10 × freshness)`)
  - P1: risk_factors → LLM 2차 검증 컨텍스트 주입
  - P2: LLM regime + 패널 regime 보수적 결합
- **확인 항목**:
  - [ ] 일요일 21:00 패널 갱신 후 6시간 내 cross_validator 자동 흡수 로그
  - [ ] LLM 2차 검증 프롬프트에 "주간 매크로 리스크" 섹션 출력
  - [ ] 패널 추천 종목 진입 시 점수 보너스 로그 (`전문가패널 추천(+X conv=...)`)
  - [ ] regime 결합 결과 (`LLM+패널 결합=trending_bull` 등)
- **효과 가설**:
  - [ ] 패널 추천 종목 진입 빈도 증가 (모든 전략에 보너스 확산)
  - [ ] 매크로 리스크 인식 시 LLM 거부율 증가 (의사결정 보수화)
  - [ ] 패널 미추천 + 약세 regime 종목 진입 감소
- **검증 SQL** (5/9 W19 후속복기 시점):
  ```sql
  WITH panel_picks AS (
    SELECT '005930' AS sym UNION SELECT '000660' UNION SELECT '064350'
    UNION SELECT '489790' UNION SELECT '009830'
  )
  SELECT
    CASE WHEN t.symbol IN (SELECT sym FROM panel_picks) THEN '추천' ELSE '비추천' END AS group_,
    COUNT(*) AS n,
    ROUND(AVG(pnl_pct)::numeric, 2) AS avg_pnl,
    ROUND(SUM(pnl)::numeric, 0) AS total_pnl
  FROM trades t
  WHERE market='KR' AND exit_time::date >= '2026-05-04'
    AND exit_type NOT IN ('kis_sync','sync_reconcile','sync_closed','sync_partial')
  GROUP BY group_;
  ```

### 2026-05-08~ (5영업일 후) — cross_validator 누적 감점 cap -15 효과

- **커밋**: 적용 예정 (2026-05-03)
- **변경**: `src/core/cross_validator.py` — 누적 감점이 `TOTAL_PENALTY_CAP=15`를 초과하면 capped. 추격매수/RSI과매수/적자+고PBR은 hard block 의도라 캡 예외.
- **효과 가설**:
  - [ ] 60-75점대 종목 차단율 30%↓
  - [ ] 통과 종목 5일 누적 승률 보존 (60+ 종목 82.4% 영역)
- **검증 SQL**:
  ```sql
  SELECT entry_strategy AS strat, COUNT(*) AS n,
         ROUND((SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END)::numeric / NULLIF(COUNT(*),0) * 100)::numeric, 1) AS win_rate,
         ROUND(AVG(pnl_pct)::numeric, 2) AS avg_pnl,
         ROUND(SUM(pnl)::numeric, 0) AS total_pnl
  FROM trades
  WHERE market='KR'
    AND exit_time::date >= '2026-05-04'
    AND exit_type NOT IN ('kis_sync','sync_reconcile','sync_closed','sync_partial')
    AND entry_signal_score BETWEEN 60 AND 75
  GROUP BY entry_strategy ORDER BY n DESC;
  ```
- **롤백 트리거**: 통과 종목 승률 5%p 이상 하락 시 즉시 롤백.

### 2026-05-08~ — rsi2_reversal/gap_and_go 1차 익절 4%×0.40 효과

- **커밋**: 적용 예정 (2026-05-03)
- **변경**: `scripts/run_trader.py:_strategy_exit_params`
  - rsi2_reversal: first_exit_pct 5.0→**4.0**, first_exit_ratio 0.20→**0.40**
  - gap_and_go: first_exit_pct ~2.4→**4.0**, first_exit_ratio 0.20→**0.40**
- **효과 가설**:
  - [ ] 거래당 평균 실현 PnL +0.3%p 개선 (단기 회전 1.5일 평균 보유 적합화)
  - [ ] 1차 익절 도달율 증가 (4% 임계 낮춤)
  - [ ] 잔여 포지션 손절률 감소 (40% 매도 후 보호)
- **검증 SQL**:
  ```sql
  SELECT entry_strategy AS strat, COUNT(*) AS n,
         ROUND(AVG(pnl_pct)::numeric, 2) AS avg_pnl,
         ROUND(SUM(pnl)::numeric, 0) AS total_pnl,
         SUM(CASE WHEN exit_type='first_take_profit' THEN 1 ELSE 0 END) AS first_tp_count,
         SUM(CASE WHEN exit_type='stop_loss' THEN 1 ELSE 0 END) AS stop_loss_count
  FROM trades
  WHERE market='KR'
    AND entry_strategy IN ('rsi2_reversal','gap_and_go')
    AND exit_time::date >= '2026-05-04'
  GROUP BY entry_strategy ORDER BY n DESC;
  ```
- **롤백 트리거**: 평균 PnL이 -0.5%p 이상 악화되면 5%/0.20으로 롤백.



### 2026-05-09 (토 00:00) — Weekly Rebalance 90일 시계열 + Wiki 컨텍스트 첫 반영

- **커밋**: afc09cb (90일 시계열) + Phase 1 (Wiki 컨텍스트)
- **변경**:
  - `strategy_evolver.rebalance_strategy_allocation` — 1주+30일+**90일** 시계열, 90일 우선 system_prompt
  - **Phase 1**: `_build_wiki_context()` — 전략별 wiki 교훈 + 직전 주 매도후 복기 LLM 분석 → user_prompt 주입
- **확인 항목**:
  - [ ] 5/9 00:00:10 KST 리밸런싱 실행 로그 (`journalctl -u qwq-ai-trader --since "2026-05-09 00:00"`)
  - [ ] LLM reasoning 출력에 "1주/30일/90일" 시계열 비교 명시 포함
  - [ ] **LLM reasoning에 Wiki 교훈 인용 포함 여부** (Phase 1 효과 측정)
  - [ ] `sync_from_db(days=90)` 정상 작동 (DB 동기화 보강 로그 확인)
  - [ ] rsi2_reversal allocation 변동 — 누적 60% 승률을 LLM이 인식했는가
  - [ ] strategic_swing 추가 상향(>20%) 발생 시 bull 편향 가드 검토
- **회귀 위험**:
  - review_period(90)이 빈 결과 시 system_prompt가 "90일 신뢰" 강조와 충돌 (P2-11 미반영)
  - wiki_ctx 5KB 추가로 LLM 토큰 비용 증가 (~$0.05/주 추정, 무시 가능)

### 2026-05-09~ (5영업일 후) — theme_chasing min_score 75 효과 검증

- **커밋**: afc09cb
- **변경**: `theme_chasing.min_score 65.0 → 75.0`
- **효과 가설** (5영업일 후 평가):
  - [ ] 거래 빈도: 주 5건 → **주 1.5~2건** (60~70% 감소)
  - [ ] 승률: 34% → **45~50%** (저질 진입 차단)
  - [ ] 평균 PnL%: -0.75% → **-0.2~+0.3%** (+0.5~1.0%p 개선)
- **검증 SQL**:
  ```sql
  SELECT COUNT(*) AS n,
         SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) AS wins,
         ROUND(AVG(pnl_pct)::numeric, 2) AS avg_pnl_pct,
         ROUND(SUM(pnl)::numeric, 0) AS total_pnl
  FROM trades
  WHERE entry_strategy='theme_chasing'
    AND market='KR'
    AND exit_time::date >= '2026-05-03'
    AND exit_type NOT IN ('kis_sync','sync_reconcile','sync_closed','sync_partial');
  ```
- **재조정 트리거**:
  - 거래 빈도 < 주 1건이면 70점 재검토
  - 승률 ≤ 35% 유지면 enabled=false 검토

### 2026-05-09~ — rsi2_reversal allocation 12.5% 작동 검증

- **커밋**: afc09cb
- **변경**: `strategy_allocation.rsi2_reversal 9.5 → 12.5`
- **확인 항목**:
  - [ ] rsi2 진입 빈도가 9.5% 시기 대비 회복했는가
  - [ ] 1포지션 진입 시 budget cap 12.5%로 축소된 사이즈 (3.18M @ 25.4M equity) — `min_position_value 200k` 초과 정상 작동
  - [ ] 누적(3/6~) 승률 60% 유지 또는 개선

### 2026-05-09 (토) — 매도 후속 복기 (W19) 트렌드 추적

- **커밋**: 4cbc7fd
- **확인 항목**:
  - [ ] 토 09:30~09:44 KST 자동 실행 (이전 09:00 → 변경)
  - [ ] stop_loss exit_type 매도후 평균 변화 (W18: +9.91%)
  - [ ] V자 반등 재진입(`_check_stop_loss_rebound`) 발생 사례 기록
  - [ ] strategic_swing 매도후 +12.24% 갭이 줄어드는가

### 즉시 (다음 매도 체결 시) — FILL 라벨링 + 재진입 V자 반등 라이브 검증

- **커밋**: afc09cb (FILL 라벨링), 973a07e+afc09cb (재진입 V자 반등)
- **확인 항목**:
  - [ ] 매도 체결 시 대시보드 주문 이벤트 로그에 "익절"/"손절"/"매도" 라벨 정확 표시
  - [ ] 마지막 분할청산 라벨 누락 회귀 없음 (trade_journal 폴백 작동 확인)
  - [ ] stop_loss 종목 재진입 발생 시 로그: `[재진입] {symbol} 손절 후 V자 반등 감지 — 재진입 허용 (V자 반등 +X.X% (>=+5%))`
  - [ ] 부분 청산 후 `_exited_today` 미등록 확인 (잔여분 손절 시 정상 등록)

## 완료된 체크포인트

(검증 완료 시 ✅ + 1줄 요약으로 여기에 이동)
