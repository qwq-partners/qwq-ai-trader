# 모니터링 체크포인트

> 변경이 적용된 후 검증해야 할 항목을 시점·전략별로 정리합니다.
> 신규 모니터링 항목은 변경 커밋과 함께 이 문서에 추가합니다.
> 검증 완료 시 ✅ 표시 + 결과 요약 1줄 기록 후 다음 사이클로 이동.

## 활성 체크포인트

### 2026-08-10~ (2주 관측) — 팩터 버킷 위험예산 shadow 검증

- **적용 일자**: 2026-08-08 (engine `_check_factor_budget`, enforce=false)
- 검증 1: `journalctl | grep "팩터 예산"` — shadow 초과 관측 로그 빈도 확인
  (trend 캡 65% 대비 실제 동시 노출이 얼마나 근접하는지)
- 검증 2: 2주간 초과 관측이 0이면 캡이 무의미하게 높은 것 → 하향 검토,
  빈번하면 개별 전략 캡과의 균형 재설계
- 승격 조건: 관측 데이터로 캡 확정 후 `enforce: true` (G5_factor 차단 활성화)

### 2026-08-10~ (첫 거래일 + 토요일) — TCA 체결 슬리피지 계측 가동 검증

- **적용 일자**: 2026-08-08 (tca.py 신규 + kis_kr check_fills 훅)
- 검증 1 (월 8/10 장중): 매수 체결 발생 시 `~/.cache/ai_trader/tca.jsonl`에
  레코드 축적 확인 (`wc -l`, cost_bps 부호 상식 점검 — BUY 시장가는 양수 우세 예상)
- 검증 2 (토 8/15 09:30): "🧾 주간 TCA 체결 비용" 텔레그램 수신 확인
- 검증 3: 50bps 초과 WARNING 로그 발생 빈도 — 과도하면 시장가→marketable 지정가
  전환 검토의 근거 데이터가 됨

### 2026-05-25~ (1주일 후) — 5/18 5종 변경 통합 검증

- **적용 일자**: 2026-05-18 (커밋 05bcdd5 + 75ccec4)
- **변경**: P0-1 데이터 파이프라인 수리 / P1 KOFR 자동운용 / P2-a 트레일링 차등 / P2-b 요일 클러스터 학습 / P3 갭다운 추적

#### A. P0-1 수급 델타 영속화 (자동 cron b10426bf, 5/25 10:37)
- 검증: `indicators_at_entry ? 'supply_delta_ratio'` 진입 N건
- 목표: 5건+ 영속화 (3차 마지막, 미달 시 강제 롤백)
- SQL: `SELECT COUNT(*) FROM trades WHERE market='KR' AND entry_time >= '2026-05-18' AND indicators_at_entry ? 'supply_delta_ratio';`
- 캐시 확인: `ls ~/.cache/ai_trader/strategic/supply_trend_*.json`
- 롤백 트리거: 5건 미달 OR delta_ratio≥3 D+1 < 0%

#### B. P1 KOFR 자동 운용
- 첫 매수 발동 시점 추적 (다음 폭락 KOSPI -2%↓ + 현금 30%↑)
- 4-OR 청산 트리거 정확도:
  - [ ] (a) 시장 정상화 트리거 — caution→normal 시점
  - [ ] (b) 신규 시그널 트리거 — pending_signals 발견 시점
  - [ ] (c) 5영업일 한도 트리거
  - [ ] (d) 수동 청산 (텔레그램 통합 후)
- 상한 25% 준수 — equity × 0.25 초과 보유 사례 0건 확인
- 최소 현금 5% 유지 — 다른 시그널 진입 차단 사례 0건
- 로그 위치: `[안전자산] KOFR 매수/매도`
- 상태 파일: `~/.cache/ai_trader/safe_asset_state.json`
- 후보 검증 (2026-08-05 수정): 이름 조회가 전부 실패(KIS HTTP 500 등 일시 장애)하면
  영구 비활성 대신 다음 5분 주기에 재검증. 영구 비활성은 **이름이 조회됐는데
  키워드 미매칭**인 경우에만 발동 (`[안전자산] 모든 후보 검증 실패` 로그 확인)
- 롤백 트리거:
  - KOFR 1주 보유 후 SEPA 시그널 발생했는데 (b) 트리거 미발화
  - 5영업일 초과해도 (c) 자동 청산 안 됨
  - 매수 시도 후 broker submit_order 실패율 ≥30%

#### C. P2-a 복합 트레일링 전략별 차등
- 검증 SQL:
```sql
SELECT entry_strategy, exit_type, COUNT(*),
       AVG(pnl_pct) FILTER (WHERE pnl_pct IS NOT NULL) AS avg
FROM trades
WHERE market='KR' AND exit_time >= '2026-05-18'
  AND exit_reason LIKE '%복합트레일링%'
GROUP BY 1, 2 ORDER BY 3 DESC;
```
- 모니터링: RSI2 0.3% 버퍼로 조기 청산 빈도 증가 여부
- 조정 트리거: RSI2 복합 트레일링 청산 후 D+1 +3%+ 회복 비율 ≥40% → 0.4%로 완화

#### D. P2-b 금요일 청산 클러스터 L3 학습
- 첫 학습 발화 추적: 다음 금요일 청산 3건+ 발생 시 L3 원칙 자동 등록
- 학습 후 적용 검증: 다음 월요일 SEPA 시그널 점수에 "메모리보정(-2)" 표시
- 신뢰도 추이: 사례 누적 시 0.5 → 0.85 점진 증가
- 검증 위치: `[거래메모리] 신규 원칙: 금요일 청산 클러스터` 로그
- 롤백 트리거: 5건 사례 누적 후 월요일 KOSPI +1%↑ 사례가 -1%↓ 사례보다 많으면 패턴 무효 (active=False 수동 처리)

#### E. P3 갭다운 차단 정확도
- 누적 파일: `~/.cache/ai_trader/gap_down_blocked_YYYY-MM-DD.json`
- 4주 후 (2026-06-15~) 정확도 분석:
  - 차단 종목의 D+5 종가 < 시그널 진입가 → 회피 성공
  - 회피율 ≥75% → 임계 -5% 유지
  - 회피율 50~75% → 임계 -6%로 완화 검토
  - 회피율 < 50% → 임계 폐지 검토

### 2026-05-19~ (1주일 후) — PostgreSQL 튜닝 효과 검증

- **적용 일자**: 2026-05-12 08:33 KST
- **변경 내용**:
  - shared_buffers 128MB → 512MB
  - effective_cache_size 기본 → 2GB
  - work_mem 4MB → 16MB
  - maintenance_work_mem → 128MB
  - pg_stat_statements 활성화
  - pg_cron 활성화 (매일 02:00 ANALYZE, 일요일 03:00 VACUUM ANALYZE, 일요일 04:00 stat_statements reset)
- **검증 항목**:
  - [x] 테이블 캐시 히트율 82.9% → **84.5%** (미미한 개선, 추가 튜닝 필요 — 아래 추가 조치)
  - [x] pg_stat_statements top10 쿼리 분석 — 최대 평균 **42.4ms** (1초+ 쿼리 없음 ✅)
  - [x] 봇 메모리 사용량 — **540MB RSS** (정상, 1GB 미달)
  - [x] pg_cron 잡 3개 — **실패율 100%** ⚠️ "connection failed" — 5/19 use_background_workers=on 추가 적용
  - [ ] dead tuple 누적 추세 (다음 주 재확인)
- **1차 검증 결과 (2026-05-19 cron 05b945cd 발화)**:
  - 캐시 히트율 84.5% (목표 95% 미달) → shared_buffers 512MB 추가 부족 가능성
  - pg_stat_statements: 평균 1초+ 쿼리 0건 → **인덱스 보강 불요** ✅
  - 봇 메모리 정상
  - pg_cron 100% 실패 → **추가 조치 적용**: cron.use_background_workers=on, max_worker_processes=12, cron.max_running_jobs=8 + PG 재시작 (5/19 10:30 KST)
- **추가 조치 후 재검증 필요** (다음 주):
  - 매일 02:00 ANALYZE 잡 실행 성공 확인
  - shared_buffers 512→1GB 증가 검토 (RAM 27% — 다소 공격적, OOM 모니터링 후 결정)
- **2차 검증 (2026-05-26, 자동 cron 650004db)**:
  - pg_cron 7일 결과:
    - jobid 4 (analyze-large-tables): **7회 succeeded ✅** (매일 02:00 정상)
    - jobid 5 (weekly-vacuum): **1회 failed** — "VACUUM cannot be executed from a function"
    - jobid 6 (reset-stat-statements): 1회 succeeded ✅
  - 캐시 히트율 **77.7%** ⚠️ (5/19 84.5% → -6.8%p 악화) — 데이터 누적 + shared_buffers 부족
  - 인덱스 캐시 99.4% (정상)
  - 봇 메모리 RSS 611MB (1GB 미달, 정상)
- **2차 조치 (2026-05-26)**:
  - weekly-vacuum 잡 삭제 → **weekly-analyze-all** 신규 등록 (VACUUM 제외, ANALYZE만)
    - 이유: pg_cron background worker가 VACUUM을 함수 내 실행 못 함
    - 대안: autovacuum이 VACUUM 자동 처리 + 일요일 ANALYZE만 명시
  - **shared_buffers 512MB → 1GB 증가** (RAM 3.7GB의 27%)
  - PG 재시작, 봇 active 유지 ✅
- **3차 검증 필요** (1주 후, 2026-06-02):
  - 캐시 히트율 1GB로 90%+ 회복 여부
  - OS 메모리 압박 여부 (free 명령 확인)
  - pg_cron 잡 4개 모두 succeeded 확인 (analyze-large + weekly-analyze + reset-stat-statements)
- **인덱스 보강 검토** (top10 쿼리 분석 후):
  - 자주 쓰이는 WHERE 컬럼 인덱스 누락 여부
  - 미사용 인덱스 정리 (단 pkey/unique 제외)
- **검증 SQL**:
  ```sql
  -- 캐시 히트율
  SELECT sum(heap_blks_hit) * 100.0 / NULLIF(sum(heap_blks_hit) + sum(heap_blks_read), 0) AS hit_pct
  FROM pg_statio_user_tables;
  -- top10 느린 쿼리
  SELECT query, calls, mean_exec_time, total_exec_time
  FROM pg_stat_statements
  ORDER BY mean_exec_time DESC LIMIT 10;
  -- pg_cron 실행 이력
  SELECT jobid, status, start_time, end_time FROM cron.job_run_details
  ORDER BY start_time DESC LIMIT 20;
  ```
- **롤백 트리거**:
  - OS 메모리 압박으로 봇 OOM 발생 → shared_buffers 256MB로 축소
  - pg_cron 잡 실패율 > 20% → 잡 비활성화 또는 일정 조정

### 2026-06-12~ (1개월 후) — DB 중장기 최적화 검토

- **선결 조건**: 5/19 1차 검증 완료
- **검토 항목**:
  - [ ] krx_minute (607만행 539MB) 파티셔닝 도입 — 월별 RANGE 파티셔닝
  - [ ] ats_trades (156만행) 파티셔닝 검토
  - [ ] candles (94만행) 파티셔닝 필요성 (소형이라 우선순위 낮음)
- **트리거**: krx_minute 쿼리가 top10에 들고 평균 1초+ 걸리면 우선 진행
- **작업 난이도**: 중-상 (파티셔닝은 다운타임 또는 점진 마이그레이션 필요)

### 2026-05-16~ (5영업일 후) — 수급 델타 보너스 P0-1 효과

- **커밋**: 7200ebe (2026-05-10 적용)
- **변경**: `supply_trend._calculate_trend_score`
  - `delta_ratio = today_net / mean(5d_avg_net)`
  - `≥5x`: +30점 / `≥3x`: +20점
- **근거**: 5/4 SK하이닉스 외국인 4/30~5/4 누적 412만주, 5/4 단독 289만주 = 약 3.5배 점프 (사전징후)
- **1차 평가 (2026-05-11, 자동 cron 04d1d954)**: ⚠️ **계측 부재 — 평가 불가, 7일 연장**
  - 수급 델타 진입 건수: 0건 (`entry_reason LIKE '%수급 델타%'` 매칭 0)
  - 동기간 SEPA 7건 (avg D+1 +2.58%), Swing 7건 (avg +0.64%) — 비교 불가
  - 원인: `trades.entry_reason`에 'buy_signal' 같은 간단 텍스트만 저장됨
  - supply_trend reasons는 메모리에서만 사용되고 영속화 안 됨 → 효과 측정 트레이스 없음
  - 보강 PR: a39ac20 — swing_screener+kr_scheduler에 supply_delta_ratio 영속화 추가
  - 재평가 예정: 2026-05-18 (계측 보강 + 7일 데이터 축적 후)
- **3차 평가 (2026-05-25, 자동 cron b10426bf)**: ❌ **21일 한계 도달 — 영속화 0건, 롤백 필요**
  - 캐시 파일 4개 생성 (5/18, 5/19, 5/20, 5/22) — 모두 `[]` 빈 배열
  - SupplyTrendDetector 실행됐으나 **점수 50+ 통과 종목 0건**
  - 영속화 진입 0건 → swing_screener trending dict 빈 상태
  - 동기간 비교군: SEPA 9건 -1.25%, Swing 5건 -0.26% (폭락장 영향)
  - **결정**: 효과 측정 데이터 부재 → P0-1 강제 롤백 또는 _calculate_trend_score 임계 완화 (50→30) 검토
  - ✅ **롤백 적용 (2026-05-25)**: 사용자 위임 후 A안 채택
    - supply_trend._calculate_trend_score: delta_ratio +30/+20 점수 부여 제거
    - delta_ratio 계산 자체는 유지 (indicators_at_entry 영속화로 향후 데이터 누적)
    - 향후 점수 체계 재설계 시 별도 PR (5/4 SK하이닉스급 사례 데이터 더 모은 후)
- **2차 평가 (2026-05-18, 자동 cron c8ee3cd0)**: ⚠️ **데이터 파이프라인 실패 — 영속화 0건, 마지막 연장**
  - `indicators_at_entry ? 'supply_delta_ratio'` 진입: 0건
  - 동기간 SEPA 2건 (avg -4.60%), Swing 4건 (avg -0.33%) — 폭락장 영향
  - 근본 원인: SupplyTrendDetector 자체가 거의 동작 안 함
    - 08:15/08:20 실행 시 유니버스 0종목 (장 시작 전 KIS 당일 데이터 없음)
    - pykrx MCP 의존 — "pykrx MCP 미사용 → KIS 당일 데이터로 대체" 후 0종목
    - 캐시 파일 supply_trend_*.json 존재하지 않음
  - 손익비/D+1 < 0% 트리거 조건 발동했으나 **폭락장 영향**이 더 커서 P0-1 자체 효과로 단정 불가
  - **결정**: 5/25까지 3차 마지막 연장 (총 14영업일, 한계 21영업일)
  - **선결**: 데이터 파이프라인 수리 (SupplyTrendDetector 실행 시각 또는 데이터 소스 변경)
- **효과 가설**:
  - [ ] delta_ratio ≥3 종목 발견 빈도 (주간 N건)
  - [ ] 해당 종목 진입 시 D+1 평균 PnL 비적용 대비 +0.5%p 개선
  - [ ] 5/4 SK하이닉스급 사전징후 종목 사전 포착
- **검증 SQL** (5/18 재시도):
  ```sql
  -- supply_trend metadata에 delta_ratio 영속화 후
  SELECT t.symbol, t.name, t.entry_strategy, t.entry_time::date,
         t.entry_signal_score AS score, t.pnl_pct,
         (t.indicators_at_entry->>'supply_delta_ratio')::float AS delta
  FROM trades t
  WHERE t.market='KR'
    AND t.entry_time::date >= '2026-05-11'
    AND (t.indicators_at_entry->>'supply_delta_ratio')::float >= 3
  ORDER BY t.entry_time DESC;
  ```
- **롤백 트리거**:
  - 5건 평가 시 손익비 < 1.0 → 즉시 보너스 삭제
  - delta_ratio ≥3 종목의 D+1 평균 < 0% → 임계 5x로 상향
  - 진입 빈도 폭증 (일평균 3건 이상 추가) → 보너스 +20→+10 축소

### 2026-06-07~ (3개월 운용 후 평가) — 코어 stale D안 자동매도 효과

- **커밋**: 적용 예정 (2026-05-07, 6번째 코어 stale 변경)
- **D안 하이브리드**:
  - Tier 1 알림: 30영업일+ ±3%
  - Tier 2 자동매도: 45영업일+ ±3% OR 30영업일+ ±2%+거래량 50% 미만
- **확인 항목**:
  - [ ] 자동매도 발생 사례 (시간 기반 vs 조건 기반)
  - [ ] 자동매도 후 30일 추세 (회피 vs 놓침)
  - [ ] rebalance_exclude 화이트리스트 정상 작동
  - [ ] 봇 재시작 후 영업일 계산 정확성 (entry_time 기반)
- **롤백 트리거**:
  - 자동매도 발동 종목 30일 내 +20%↑ 추가 상승 → 임계 60영업일 완화
  - 사용자 명시 보호 종목 자동매도 사례 발생 → rebalance_exclude 사용 권장 + 알림
  - 첫 3건 자동매도 후 회수 자본의 후속 IRR < 매도 종목 30일 후 PnL → 비활성화

### 2026-06-07~ (3개월 운용 후 평가) — 코어홀딩 stale_alert 효과 (대체)

- **커밋**: 적용 예정 (2026-05-07)
- **변경**: `_check_core_stale_alert` (자동매도 X, 텔레그램만)
- **조건**: 30영업일+ 보유 + |PnL| ≤ 3% → 알림 1회 (7일 쿨다운)
- **확인 항목**:
  - [ ] 알림 발송 사례 누적 추적
  - [ ] 알림 후 사용자 결정 (보유 지속 vs 청산)
  - [ ] 청산 결정 시 회수 자본의 후속 사용 효과
- **효과 가설**:
  - H1: alert 발동 종목의 30일 후 PnL이 미발동 코어보다 저조
  - H2: 사용자 청산 결정 시 회수 자본 → 다음 코어 IRR 우월
- **검증 SQL** (3개월 후):
  ```sql
  SELECT symbol, name, entry_time::date, exit_time::date, pnl_pct, exit_reason
  FROM trades
  WHERE entry_strategy='core_holding'
    AND exit_time::date >= '2026-05-07'
  ORDER BY exit_time DESC;
  ```
- **롤백 트리거**:
  - 발동 종목 30일 내 신고가 50%+ 돌파 → 35영업일/±2% 완화
  - 사용자 5회 모두 "보유 지속" → 임계 완화
  - 첫 3건 손익 악영향 → 비활성화

### 2026-05-13~ — F-3 자본 회전 효율 개편 효과

- **커밋**: 적용 예정 (2026-05-06)
- **변경 5건**: strategy_limits 한도 확대 + min_score 60→55 + daily_max_new_buys 5→7 + lunchtime 13:30 + 14:00 자본활용률 체크 task
- **확인 항목**:
  - [ ] 일평균 sepa 진입 1.5+ (이전 0.77)
  - [ ] 일평균 swing 진입 1.2+ (이전 0.80)
  - [ ] 현금 비중 18~20% (이전 35.7% 5/6 단일, 90일 평균 13%)
  - [ ] 14:00 자본활용률 체크 발동 로그 + 추가 진입 건수
  - [ ] daily_max -5% 도달 0회
- **롤백 트리거**:
  - daily_max 도달 1회+ → 즉시 롤백 (5단계 모두 환원)
  - 손익비 -10% 악화 → min_score 55→58 단계 환원
  - raw 보유 12+ → 14:00 체크 임계 25%→35% 상향
- **검증 SQL**:
  ```sql
  SELECT entry_strategy, COUNT(*) AS n,
         ROUND(AVG(pnl_pct)::numeric, 2) AS avg_pnl,
         ROUND(SUM(pnl)::numeric, 0) AS total_pnl
  FROM trades
  WHERE market='KR' AND entry_time::date >= '2026-05-07'
    AND exit_time IS NOT NULL
    AND exit_type NOT IN ('kis_sync','sync_reconcile','sync_closed','sync_partial')
  GROUP BY entry_strategy ORDER BY n DESC;
  ```

### 2026-05-13~ — max_positions 잔여 비율 가중 카운트 효과

- **커밋**: 적용 예정 (2026-05-06)
- **변경**: `risk/manager.py:can_open_position` 비코어 카운트
  - `len(positions) - core` → `sum(remaining/original weight)`
  - 0.2 floor + ExitManager 미연결 1.0 폴백
- **확인 항목**:
  - [ ] 차단 메시지에 "가중 X.X / 8" 형식 표시
  - [ ] 1차 익절 진행된 포지션의 weight 0.8 적용
  - [ ] 트레일링 단계 0.2 floor 적용 (남용 방지)
- **효과 가설**:
  - 진입 빈도 증가 (가중 슬롯 여유)
  - 일일 -5% 도달 횟수 변화 ≤ ±1회
  - 보유 종목 수 raw count 증가는 max+flex(=10) 한도 내
- **롤백 트리거**:
  - daily_max -5% 도달 5영업일 2회+ 시 0.2 floor → 0.4 상향
  - raw count 12+ 도달 시 즉시 환원 (안전 장치 무력화 의심)

### 2026-05-16~ — core_holding 트레일링 -8% 한도 사례 추적

- **상태**: A안 유지 (현 상태) — 2026-04-28 + 2026-05-06 동일 결정
- **누적 사례**:
  - 2026-04-28: 267260 HD현대일렉트릭 +14.6% (고점 +24.8% → -8.02%)
  - 2026-05-06: 402340 SK스퀘어 +30.2% (고점 +40.1% → -8.12%)
- **패턴**: core_holding ATR 트레일링 -8% 한도가 강세 종목 +30~40% 도달 시 발동
- **고점 → 실현 갭**: 평균 약 10%p (정상 ATR 노이즈 범위)
- **재검토 트리거**:
  - N≥5 누적 시 통계적 유의성 평가
  - 고점→실현 갭 평균 15%p 이상 또는 매도 후 +20%↑ 추가 상승 패턴 시 옵션 B 검토
- **검토 옵션** (재검토 시):
  - A: 현 상태 (트레일링 -8% 유지) — 수익 보호 우선
  - B: core_holding 트레일링 -8% → -12% 완화 — 강세 추적 우선
  - C: core_holding 트레일링 면제 — 장기 보유 본연 목적
- **검증 SQL** (5/16):
  ```sql
  SELECT symbol, name, ROUND(pnl_pct::numeric,2) AS realized_pnl,
         exit_reason, exit_time::date AS out_dt
  FROM trades
  WHERE entry_strategy='core_holding'
    AND exit_type='trailing'
    AND exit_time::date >= '2026-04-01'
  ORDER BY exit_time DESC;
  ```

### 2026-05-18~ (2주 후) — 09:00~09:29 장초반 차단 재검토

- **상태**: A안 채택 (현 상태 유지) — 2026-05-04 사용자 결정
- **재검토 사유**:
  - 도입 근거 (2026-04-25): 30일 09시 진입 -440k 중 **76%가 theme_chasing**
  - **2026-05-04 theme_chasing 폐지**로 주요 손실 원인 제거됨
  - 5/4 SK하이닉스 09:21 시그널 차단 → +12.5% 놓침 (실제 기회비용 발생)
  - 현재 차단 규칙은 theme 포함 데이터 기반
- **재검토 시점**: 2026-05-18 (theme 폐지 후 2주 누적)
- **재검토 SQL** (theme 폐지 후 09시 진입 데이터):
  ```sql
  -- theme_chasing 폐지(5/4) 이후 09:00~09:29 진입 종목 통계
  SELECT
    EXTRACT(HOUR FROM entry_time) || ':' ||
      CASE WHEN EXTRACT(MINUTE FROM entry_time) < 30 THEN '00-29' ELSE '30-59' END AS time_slot,
    entry_strategy,
    COUNT(*) AS n,
    ROUND(AVG(pnl_pct)::numeric, 2) AS avg_pnl,
    ROUND((SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)::numeric / NULLIF(COUNT(*),0) * 100)::numeric, 1) AS win_rate
  FROM trades
  WHERE market='KR'
    AND entry_time::date >= '2026-05-05'
    AND exit_time IS NOT NULL
    AND exit_type NOT IN ('kis_sync','sync_reconcile','sync_closed','sync_partial')
  GROUP BY time_slot, entry_strategy
  ORDER BY time_slot, n DESC;
  ```
- **검토 옵션** (재검토 시):
  - 옵션 1: 현 상태 유지 (theme 없이도 -440k 패턴 재현 시)
  - 옵션 2: strategic_swing/sepa_trend만 09:00~09:29 허용
  - 옵션 3: bull 레짐에서만 차단 해제
  - 옵션 4: 차단 시간 09:00~**09:15** 단축
- **재검토 트리거**:
  - theme 폐지 후 09:00~09:29 진입 N≥10건 누적
  - 평균 PnL이 변경 전(-0.20%) 대비 +0.5%p 이상 개선되면 옵션 2~4 검토

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

### 2026-09-14~ — 위험 사이징 canary 리포트 (`scripts/review_risk_canary.py`, 오프라인)
- **도구**: `venv/bin/python scripts/review_risk_canary.py --input <검증용 포지션 원장.json> --benchmark <kodex200.csv> --cohort risk-sepa-v1 --output <report.json> [--sha <적용SHA>] [--min-sample 30] [--as-of YYYY-MM-DD]` — 주문·설정 변경 없음, 네트워크·`~/.cache` 무접촉. 원장 스키마는 모듈 docstring(T3 exporter 계약, `entry_risk` 12키).
- **종료 코드**: 2 = 파일 누락·JSON 파싱 실패·필수 필드 오류(부족 항목 stderr) / 0 = 데이터 정상. **종료 코드로 성과를 판단하지 않는다.**
- **status**: `insufficient_sample`(완결 < 30, 판정 보류 — 표본을 만들려고 거래를 늘리거나 사이징 모드를 자동 전환하지 않음) / `hold_expansion`(완결 ≥30 이고 평균 R≤0 또는 PF≤1 또는 평균 동일기간 초과수익≤0 → 확대 보류) / `further_review`(그 외 — 자동 승격 아님). 통계적 유의성 판정이 아닌 보수적 확대 검토 규칙. **어떤 상태에서도 nominal 자동 복귀를 권고하지 않는다.**
- **technical_status**(성과와 별개): `entry_risk` 필수키·`actual_stop_pct`·`exits` 누락, `position_id` 중복, 계획 위험 > 예산, 계획 SL ≠ 실제 SL, `initial_risk_amount` ≠ 진입비용×실제 SL(1원 허용), `net_pnl` ≠ Σ매도−Σ매수−Σ수수료(1원 허용) → 한 건이라도 `failed`.
- **체크포인트**: 첫 5건 `technical_status=passed` / 10건 재계산 불일치 0건 / 30건 status 보고. 벤치마크는 일 종가(진입일·청산일) 기준이며 장중 체결가와의 시점 차이는 가정으로 기록. 벤치마크 결손 포지션의 초과수익은 null(0 대체 없음).
- 제외 집계: `cohort_mismatch`(cohort/SHA 불일치) · `legacy_unmeasured`(`entry_risk` null 또는 sizing_mode≠risk) · `open` · `lots_ambiguous` · `missing_initial_risk`.

### 2026-09-14~ — 장전 전망 사후 평가 원장 (모닝브리프 scope + 레짐 시간 범위)

- **대상**: `src/analytics/morning_brief_eval.py`, `DailyReportGenerator.evaluate_morning_brief()`, `MarketRegimeAdapter` 시간 범위 분리
- **원장**: `~/.cache/ai_trader/morning_brief_eval.jsonl` (1일 1줄, 저녁 리포트 시각에 기록)
- **확인 항목**:
  - [ ] 브리프 캐시에 `scope`·`inputs`·`claims`·`model`이 저장되는가 (07:00 생성 직후 파일 확인)
  - [ ] `scope=us_close_only`인 날 본문에 "갭 출발/상승 출발/시가 매수" 단정이 없는가 (`removed_claims` 건수 로그)
  - [ ] 07:30 통합 메시지가 기존대로 브리프 본문을 결합하는가 (`text` 키 호환)
  - [ ] 저녁에 원장 1줄이 추가되고 `evaluated=true`인가 (지수 조회 실패일은 `evaluated=false` + 사유)
  - [ ] 원장 줄의 `brief_date`가 그날 `date`와 같은가 — 다르면 `evaluated=false`(전날 브리프 재사용)이며 07:00 브리프 생성 실패를 함께 확인한다
  - [ ] `claims.open_direction`/`close_direction`이 미국 마감 서술에서 잡히지 않았는가 (KR 주어 가드 — `scope=us_close_only`인 날은 항상 `null`)
  - [ ] 급락일(`intraday_risk=crash/severe`)에 유효 레짐이 `bull`/`trending_bull`로 남지 않는가
  - [ ] 09:30 이후 `open_expectation`이 만료로 표시되는가 (`get_summary()["horizons"]`)
  - [ ] `horizons.mid_trend`가 실시간 `update_regime()` 결과와 일치하는가 (`missing=true`면 지수 미수집 — 오래된 값이 남지 않는다)
- **판정 규칙**: 20건 이상 누적 전에는 적중률로 프롬프트·임계값을 바꾸지 않는다. `summarize()`의 표본 수를 먼저 본다. 하루 결과(예: 09-14 개장·종가 동시 miss)로 규칙을 바꾸지 않는다.

### 2026-09-14~ — 장중 레짐 입력 신선도·급락 캡 (T9 A)
- `~/.cache/ai_trader/llm_regime_today.json` 의 `input_meta`: 08:10 실행은 `kr_as_of` 가 스크리너 벤치마크 **로드 시각**(고정 문자열 아님), 12:00 실행은 `kospi_today_pct`·`intraday_crash_level`·`intraday_crash_as_of`(감지기 갱신 시각)가 채워져야 한다. `missing_fields` 에 `KOSPI_c5/KOSPI_c20` 이면 스크리너 캐시 없음, `급락감지기(당일 갱신 없음)` 이면 5분 루프 전 또는 전일 상태(08:10 실행에서는 정상).
- `regime_capped: true`·`confidence_raw` 가 있으면 LLM 원본이 급락 캡으로 neutral 이 된 것 — 대시보드 confidence 는 원본 확신도가 아님.
- 5분 급락 루프 후 `adapter.regime`(`/api/engine/regime` 의 `horizons.effective_regime`)이 crash/severe 에서 bull→sideways 로 강등되는지, 다음 날 장전에는 전일 crash 가 남지 않는지(`intraday_risk` as_of 당일 게이트).
- 20:30 하트비트 `kr_evolution_scheduler` note 에 `모닝브리프 평가:` 가 붙는지(없으면 report_generator 미초기화·타임아웃 120초·브리프 파일 없음 중 하나 — 로그로 구분).
- 07:30 전문가 브리핑에 `자료 부족 N명`·`커버리지 부족` 표시와 브리프 상충 문구가 조건대로 나오는지.

### 2026-09-15~ — T10 연결 경로 일관성 (배포 후 확인)

T10(F13~F22, 통합 SHA `a6d81d0`)은 단계별 수정이 아니라 자료 수집→검증→레짐→소비자→발송→평가 **연결 경로**를 다룬다 — 각 단계가 옳아도 다음 단계가 옛 값을 읽으면 원점이므로, 배포 후에는 아래 항목을 개별이 아니라 하루치 흐름으로 함께 확인한다.

- **12:00 레짐 재분류**: `~/.cache/ai_trader/llm_regime_today.json` 의 `input_meta` 에 `kospi_bars_as_of`(봉 기반 지표 as_of, `kr_as_of` 와 분리) · `intraday_cap_level`(급락 캡이 실제로 어느 레벨로 제한했는지) · `missing_fields` 가 채워지는지.
- **30분 sync 로그**: `journalctl -u qwq-ai-trader | grep "레짐동기화"` 에 `충돌 해소 ... 어댑터=` 형태로 어댑터 값이 병합 근거로 찍히는지 — 감지기 상태만으로 결정되면 F14 회귀.
- **`monitor_positions` 무음 확인**: 30분 포지션 모니터 루프가 더 이상 레짐 재적용 로그를 내지 않는지(레짐 적용 로그는 `_apply_regime_to_exit_manager` 한 곳에서만 나와야 함, F15 회귀 시 stale_high_days 가 아침 값으로 되돌아옴).
- **07:30 아카이브**: `~/.cache/ai_trader/morning_brief/<kr_date>.json` 이 생성되고 `dispatch[]` 에 07:30 morning 슬롯 항목(status=sent/failed)이 붙는지 — 다른 슬롯(midday/after 등)은 기록하지 않는 것이 정상.
- **20:30 원장**: `morning_brief_eval.jsonl` 최신 줄의 `dispatched: true`·`brief_ref` 가 위 아카이브 경로를 가리키는지.
- **전문가 브리핑 결측 표시**: "자료 부족 N명" 또는 "unknown" 표시가 실제 결측 상황(수급·공매도 등 원자료 부재)에서만 나오는지, 정상 자료 있는 날은 안 나오는지.
- **야간선물 결측 빈도**: `missing_inputs` 에 `KR/JP 야간선물(as_of 미상 또는 만료)` 이 낮 시간 호출(세션 외)에서는 정상 발생 — 야간 세션 중(18:00~05:00) 반복되면 F17 회귀 의심.
- **Yahoo VIX 결측 표시**: US 지수 조회 실패 시 `indices_normalized.VIX` 가 `0` 이 아니라 `missing: true` 로 표시되는지.

20건 누적 전에는 위 관측을 근거로 임계값·판정 규칙을 바꾸지 않는다(`morning_brief_eval.summarize()` 표본 수 우선 확인).

### 2026-09-15~ — T11 팀 근거 계약·판단 v2·EntryPlan shadow (배포 후 관측, 승격 판정 아님)

- [ ] `~/.cache/ai_trader/team_ledger/deliberations_YYYYMMDD.jsonl` 이 슬롯마다 append 되고 같은 종목의 복수 시점 판단이 보존되는가(`team_ledger.count_samples`: buy_approved/buy_rejected/hold/abstained/failed 분포).
- [ ] `TeamAssessment`: `abstained` 비율과 사유(근거 부족/토론 실패), `data_sufficiency` 분포, `success_probability` 가 항상 None 인가.
- [ ] `signal_events` event_type=`shadow_plan_check` 행(총계·block_rate 분모·기본 조회·SSE 에서 제외, `type=shadow_plan_check` 로 조회)의 status/reasons 분포 — `QUOTE_STALE` 비율(호가 as_of 는 09:01 변환 시각), `COST_RR_LOW` 부착 비율(컷오프 결정은 분포를 본 뒤 별도 승인).
- [ ] 팀 심의 `entry_ready` 가 대부분 wait(QUOTE_STALE) 이면 후보 현재가 재조회 배선을 검토(KIS 호출 증가 → 별도 승인).
- [ ] CF `team_buy_unfilled` 등록 건수 — 실제 체결분(거래저널 `trades_YYYYMMDD.json` 에 있는 종목)이 섞이지 않는지, '체결 대조 판정 불가 … 보류 중' 경고가 뜨면 저널 디렉터리 부재를 먼저 확인.
- [ ] 돈 경로 불변 확인: 09:01 주문 수량·가격·유형이 배포 전과 같은 규칙으로 산출되는가(`tests/test_t11_money_path_baseline.py` 와 같은 지문).
- 승격·엣지 판정에 이 관측을 쓰지 않는다. A/B/C 러너는 연구용 스냅샷이 확보될 때까지 `synthetic_only`.

## 완료된 체크포인트

(검증 완료 시 ✅ + 1줄 요약으로 여기에 이동)
