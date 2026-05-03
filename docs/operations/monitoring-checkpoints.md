# 모니터링 체크포인트

> 변경이 적용된 후 검증해야 할 항목을 시점·전략별로 정리합니다.
> 신규 모니터링 항목은 변경 커밋과 함께 이 문서에 추가합니다.
> 검증 완료 시 ✅ 표시 + 결과 요약 1줄 기록 후 다음 사이클로 이동.

## 활성 체크포인트

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
