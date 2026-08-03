# 보류 중 전략 결정 (재검토 대기)

> 데이터 부족(N<10/전략)으로 결정 보류 중인 항목 목록.
> StrategyEvolver 평가 임계값(5영업일 + 10건/전략) 누적 후 재검토.
> 최초 등록: 2026-04-21 (오늘 복기 N=2 + strategy-advisor 검증 결과)

## 재검토 트리거 조건

다음 조건 중 하나라도 충족 시 재검토:
- 특정 전략 누적 거래 ≥ 10건 AND 5영업일 이상 경과
- StrategyEvolver가 해당 전략 자동 평가 결과를 산출
- 일평균 손익 -2% 이상 또는 +2% 이상 이상 추세 연속 5일
- 사용자 명시 요청

---

## 항목 #1 — sepa_trend 1차 익절 조건부 강화 (P2)

**제안**: "강한 섹터(+3% 이상) AND RS≥0.95 AND ATR≤5%" 동시 충족 시 first_exit_pct를 5→7%로 상향

**근거**:
- 2026-04-21 현대모비스(012330) 거래에서 +4.02%에 1차익절 발동 (자동차 섹터 +3.4%, RS 0.96)
- 추세주는 1차익절을 늦추면 전체 R 확대 가능
- LLM 복기: "좋은 추세주는 분할 익절 후 추세 추종의 강점을 더 살려야"

**보류 사유**: N=1 단일 거래로는 결정 불가. 1차익절 늦추면 -2~-3% 되돌림 시 실현 수익 0 빈도 증가 위험.

**관련 파일**:
- `src/strategies/exit_manager.py` (REGIME_EXIT_PARAMS L68~100)
- `config/evolved_overrides.yml` (first_exit_ratio)

**재검토 시 확인 항목**:
- sepa_trend 누적 거래 ≥ 10건의 평균 보유 수익률 분포
- 1차익절 후 잔여 포지션의 추가 수익률 vs 손실률 분포
- 백테스트 (5→7% 변경 시 historical 영향)

---

## 항목 #2 — 자본 배분 가중치 추가 조정 (P2)

**제안**: 전략별 historical 성과 기반 strategy_allocation 미세 조정 (sepa_trend 30→40%? theme_chasing 5→3%?)

**근거**:
- 2026-04-21 결과: sepa_trend 1건 +34,474원 vs theme_chasing 1건 +6,649원 (자본 효율 차이)
- LLM 복기: "상위 기여 전략에 자본을 집중"

**보류 사유**:
- 이미 evolved_overrides.yml에 차등 적용 중 (sepa 30%, theme 5%)
- _meta 주석에 "표본 1건 근거 결정 무효화" 롤백 사례 기록됨 — 동일 함정 재현 위험
- N=2로 추가 가중치 조정은 통계적 정당성 없음

**관련 파일**:
- `config/evolved_overrides.yml` (strategy_allocation)
- `src/risk/manager.py`

**재검토 시 확인 항목**:
- 전략별 N≥30건 누적 시 평균 손익률, 승률, 손익비
- StrategyEvolver 자동 평가 결과
- 가중치 변경 시뮬레이션 (현재 자본 5백만원 기준)

---

## 항목 #3 — sepa_trend 우선순위 명시적 격상 (P2)

**제안**: 스크리너에서 sepa_trend 시그널을 다른 전략보다 우선 진입하도록 큐 우선순위 부여

**근거**:
- 2026-04-21 sepa_trend(현대모비스) +4.02%로 일일 수익 대부분 기여
- LLM 복기: "강한 업종 모멘텀과 RS가 동반되는 sepa_trend 셋업을 더 우선순위 높게 집행"

**보류 사유**:
- 현재 전략은 점수 기반 정렬로 처리 — 우선순위 따로 두지 않는 것이 통합성
- 명시적 우선순위 부여 시 시장 체제 변화에 둔감해질 위험
- N=1 단일 사례로 영구 우선순위 결정 부적절

**관련 파일**:
- `src/schedulers/kr_scheduler.py:run_screening` (자동진입 체크)
- `src/core/batch_analyzer.py`

**재검토 시 확인 항목**:
- 전략별 5영업일+ 평균 R/R 비교
- 시장 체제별(bull/bear/sideways) 전략 성과 차이
- 우선순위 부여 시 다양화(diversification) 손실 측정

---

## 항목 #4 — "큰 추세 살리기" 청산 룰 보강 (P2)

**제안**: 강한 추세 종목은 1차익절 스킵 또는 트레일링 임계값 +5%→+7%로 후행 활성화

**근거**:
- LLM 복기: "작은 수익의 수동 청산보다 큰 추세를 살리는 거래 비중 확대"
- 목표 일평균 +1%, 손익비 ≥1.5 달성 위해 큰 추세 보존 필요

**보류 사유**:
- 항목 #1과 중복/연계 (1차익절 조건부 강화의 연장선)
- 트레일링 임계값 변경은 false-trail 빈도 증가 가능성
- 보유 종목 범위와 청산 룰 변경은 백테스트 필수

**관련 파일**:
- `src/strategies/exit_manager.py` (트레일링 활성화 로직)
- `config/evolved_overrides.yml`

**재검토 시 확인 항목**:
- 트레일링 활성화 후 추가 수익 vs 손실 케이스 분포
- "큰 추세" 정의 (가격 +10% 이상? 보유 5일 이상? RS 등급?)
- 백테스트 (현재 룰 vs 제안 룰)

---

## 처리 이력

- 2026-04-21: 4건 등록 (사용자 승인). N≥10건/전략 누적 후 재검토 합의.
- 2026-08-03: #6 등록 → 같은 날 검증 수행 후 **보류 종결** (US 미운용 + EPS 커버리지 부족).

---

## 항목 #6 — earnings_drift 재활성화 검토 (2026-08-03 등록 → **2026-08-03 보류 종결**)

> **결론: 보류 (enabled: false 유지).** 통계 검증은 통과했으나 사용자의 **US 미운용 결정**으로
> 활성화하지 않는다. 검증·가드 코드는 남기되 설정은 비활성. 재개하려면 아래 "재개 조건" 참조.

**제안**: US earnings_drift(현재 enabled: false)를 EPS surprise 가드(finnhub `epsActual` vs
`epsEstimate` ≥10%) 부착 후 재활성화

**근거**:
- earnings_reversal 기각 검증(quick_backtest, 24개월 S&P500 3,711 이벤트)의 부산물:
  발표 전 급등군(pre5d≥+5%, n=449)의 발표 후 3일 수익률 **+0.97% (t=2.01, 승률 54.1%)**
  — 어닝 구간에서 리버설이 아니라 **드리프트(강세 지속)** 방향이 통계적으로 유의
- 기존 비활성 사유였던 "EPS surprise API 미연동"은 finnhub 캘린더의
  epsActual/epsEstimate 필드로 해소 가능 (`_earnings_upcoming` 인프라 재사용)

**검증 결과 (2026-08-03 수행, quick_backtest 24개월 S&P500 3,561 이벤트 / 10일 보유)**:
- 갭≥3%만(EPS 무관, 기존 프록시 방식): **+0.39% < 베이스라인 +0.76%** —
  "갭만 보고 진입은 sell-the-news 무방비"라던 2026-04-18 비활성 사유를 데이터가 확인
- **EPS beat≥10% + 갭≥5% + 갭유지: +1.71% · 승률 61.0% (t=2.13, n=123)** — 조건부 통과
  (갭≥7%는 +1.95%지만 n=84로 표본 약함). 다만 베이스라인 초과분 자체의 t는 ~1.2로 압도적이지 않음
- 원본: `results/quickbt_earnings_drift_20260803.csv`

**보류 사유 (2026-08-03 종결)**:
1. **US 미운용** — 사용자가 미국 시장 투자 계획 없음을 확인. systemd 서비스도
   `--market kr`로 US 스케줄러 자체가 기동하지 않는다 (활성화해도 실행되지 않는 코드)
2. **finnhub EPS 커버리지 부족** — 실측 결과 어제~오늘 발표분 중 actual+estimate가
   모두 있는 종목이 18개뿐(전부 S&P500 밖), 원본 재조회 시 504. 가드가 fail-closed라
   켜도 거의 발화하지 못한다

**남긴 것 / 되돌린 것**:
- 유지: `EarningsProvider.get_recent_surprises()`, 스케줄러 EPS 서프라이즈 가드 배선,
  `quick_backtest.py --idea earnings_drift` 검증 도구, 튜닝값(min_gap_pct 5.0 / max_holding_days 10)
- 되돌림: `config/default.yml` `enabled: true → false`

**재개 조건**: (1) US 시장 운용 재개 결정 **그리고** (2) EPS 데이터 소스 커버리지 확보
(finnhub 유료 티어 또는 대체 소스). 둘 다 충족 시 백테스트 재실행 없이 `enabled: true`만으로
재개 가능하나, 최소 1주 shadow 관측 권장.

**관련 파일**:
- `src/strategies/us/earnings_drift.py`, `src/data/providers/earnings.py`
- `src/schedulers/us_scheduler.py` (스캔 경로 2곳의 서프라이즈 가드)
- `results/quickbt_earnings_reversal_20260803.csv`, `results/quickbt_earnings_drift_20260803.csv`
