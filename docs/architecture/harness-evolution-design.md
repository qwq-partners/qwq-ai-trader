# 하네스 엔지니어링 적용 설계 — 자기개선 루프 고도화

> 작성: 2026-08-10 (Claude 독립 분석 + Codex 협업 설계 병합)
> 참조: Lilian Weng, "Harness Engineering for Self-Improvement" (2026-07-04)
> 상태: **4페이즈 1차 구현 완료** (2026-08-10, 커밋 2ef5f7a→961b7bf→2ca9c75→d820b23
> + 통합 리뷰 반영. 페이즈별 상세는 CHANGELOG). 남은 장기 과제는 하단 §7 참조.

## 0. 한 줄 결론

우리에게 필요한 Self-Improving Harness는 코드를 스스로 고치는 시스템이 아니라,
**"원장이 사실을 확정하고 → weakness miner가 검증 가능한 반복 실패를 만들며 →
StrategyEvolver가 좁은 수치 변경 하나를 제안하고 → 독립 게이트가 held-in/held-out을
검사하며 → 승인·기각·반사실 결과가 전부 ID로 연결되는 보수적 폐루프"**다.
진화 범위 확장보다 측정 신뢰성과 실패 분류가 먼저다.

## 1. 현황 대조 (포스트 패턴 ↔ 우리 시스템)

| 포스트 개념 | 우리 구현 | 상태 |
|---|---|---|
| Pattern 1 목표 루프 | 진화 시스템 (TradeReviewer→DailyReviewer→StrategyEvolver→BacktestGate) + 주간 post-exit review | ✅ 구조 완비 |
| Pattern 2 파일시스템 = 영속 메모리 | Trade Wiki, position_ledger/tca/counterfactual jsonl, evolved_overrides.yml | ✅ 강함 |
| Pattern 3 서브에이전트/백그라운드 잡 | 전문가 15명 + orchestrator + 배치 LLM 라우팅 | ✅ |
| Self-Improving ① 약점 마이닝 | LLM 자유서술 복기 + trade_memory | ❌ **최대 갭** — 검증자 기반 군집화 없음 |
| Self-Improving ② bounded 제안 | 최대 1파라미터 + 신뢰도≥0.6 + min/max/change/total 가드레일 (`strategy_evolver.py:1250`) | ✅ 모범적 |
| Self-Improving ③ 검증·병합 | BacktestGate: A/B + walk-forward 2/3 + MDD 가드, fail-closed | 🟡 기각 후보 원장·held-in/out 계약·실현 대조 부재 |
| ACE (Generator/Reflector/Curator) | Wiki Ingest / LLM 교훈 추출 / Lint | 🟡 Reflector 계층 사실상 부재, 교훈 비구조·dedup 없음 |
| Verifier 구축 | 원장·TCA·counterfactual (2026-08-08 신설) | ✅ 정확한 선행 투자였음 |

**기존 인식 정정** (Codex 검증): `evolve()` 호출 경로는 존재한다 (kr_scheduler 20:30 진화 루프).
실제 단절은 **TradeReviewer의 자유서술 복기가 진화 규칙 트리거의 구조화 입력으로
연결되지 않는 것**이다. (메모리 `project_evolution_gap.md` 현행화 필요)

## 2. 핵심 갭 3가지

### 갭 A. 약점 마이닝 — 실패의 검증자 기반 군집화 부재

`review_period()`와 `_find_triggered_rule()` 사이에 "관측된 손실 → 검증 가능한 실패
패턴 → 원인 → 수정 가능한 컴포넌트" 변환 계층이 없다.

**설계: `weakness_miner`** (신규 모듈, 주간 배치)
- 입력: position_ledger + TCA + counterfactual + 체제/진입 스냅샷 + 청산 사유
- 2층 구조:
  - 1층 **결정론적 cohort**: 전략×체제×청산사유×보유일 버킷 (재실행 시 동일 결과)
  - 2층 **verifier 규칙 판정**: 예) "동일 전략·체제에서 MFE 양수였으나 최종 PnL 음수
    N건 이상" → `exit_profit_giveback` 패턴 확정
- LLM 역할 제한: 패턴 설명·원인 후보 작성만. `causal_status=confirmed` 부여 금지,
  진화 직접 발동 금지
- failure record 계약 (필수 필드):
  `failure_id / verifier_outcome / terminal_cause / causal_status(confirmed·supported·speculative)
  / mechanism(entry·sizing·exit·execution·data·gate) / cohort_key / evidence_refs(원장 ID)
  / sample_size / effect_size / confidence / first_seen / last_seen`
- unreliable 포지션: 표본에서 제외하되 `data_quality` 패턴으로 별도 집계 (숨기지 않음)
- ⚠️ 빈도 기반 마이닝은 다수 패턴만 강화함 — **severity(손실 크기)와 recency를
  별도 축**으로 가져 희귀·고손실 패턴 소실 방지 (Codex 보완)

### 갭 B. 기각 후보 원장 + 게이트 예측력 검증

기각은 `total_rejected_by_backtest` 카운터만 남고 상세가 소실된다.

**설계: 진화 후보 불변 원장** (`evolution_candidates.jsonl`, append-only)
- 필드: `candidate_id / parent_version / proposed_change / trigger_failure_ids /
  predicted_impact / gate_metrics / gate_decision / decision_type(performance_reject·
  infra_hold·insufficient_data) / future_eval_due_at / counterfactual_results`
- **predicted-vs-counterfactual 대조**: 기각 후보는 적용되지 않았으므로 "실현"이 없음 —
  이후 시세로 동결 후보를 재생한 **사후 반사실 성과**로 게이트 판단의 calibration을
  평가한다 (명칭 혼동 금지, Codex 정정)
- 효과: 같은 아이디어의 표현만 바꾼 재시도 차단, 게이트 자체의 예측력 검증

### 갭 C. Trade Wiki의 ACE 격상

**설계: 정규화 lesson store + Markdown projection**
- 진실 원천: YAML/JSONL lesson store (Markdown은 사람이 읽는 렌더링 결과물)
- lesson 레코드: `identifier(lesson_type×mechanism×전략×체제×schema_version 정규화 키)
  / description / scope / evidence(지지·반례 포지션 ID) / verifier(패턴 ID·표본·효과크기)
  / confidence / status(candidate→active→deprecated→archived)`
- **결정론적 dedup**: 정규화→canonical key→동일 키 병합(근거·통계만)→중복 evidence
  차단. LLM은 `related_to` 후보 표시만 — 의미 병합의 진실 원천으로 쓰지 않음
- Reflector 신설 (주간 배치): 동일 cohort 성공·실패 대조, 단일 사례 교훈에 낮은
  신뢰도, 충돌 교훈은 삭제 대신 `conflicts_with` 연결
- 200줄 제한은 **렌더링 페이지에만** 적용 — 정규화 저장소는 무제한+아카이브
  (현행은 진실 원천이 잘려나가는 brevity bias 구조)

## 3. 편집 가능 표면 정책 (진화 대상 확장 기준)

| 표면 | 판정 | 조건 |
|---|---|---|
| 전략 파라미터 | ✅ 유지 (현행) | 트리거 failure_id·기대 지표·이전 기각 사유 메타데이터 추가 |
| REGIME_EXIT_PARAMS | 🟡 조건부 확장 (1순위 후보) | 체제별 최소 표본 + 변화율 제한 + stop/TP 불변식 검사 + point-in-time 백테스트 |
| cross_validator 감점 폭 | 🟡 외부화 먼저, 진화는 장기 shadow만 | 비안전성 규칙 한정, 목적함수는 통과율이 아닌 이후 r1/r5/r20 위험조정성과 |
| 전문가 가중치 | 🟡 중장기 | 발언→주문 attribution + calibration(Brier) 확보 전에는 주간 리포트만 |
| 프롬프트 | ❌ 자동 진화 금지 | 사람 작성 + 버전 관리 + 고정 회귀 세트 + shadow A/B + 사람 승인까지만 |
| 게이트·verifier·kill switch·권한·감사 | ❌ 영구 읽기 전용 | 진화 워크스페이스 밖 |

## 4. 실패 모드 방어 체크리스트

- **과신** (분할익절 사고 재발 방지): 모든 평가자의 표본 단위를 position_id로 통일,
  승률 단독 성공 선언 금지 (신뢰구간·expectancy·MDD·표본수 동반), n<10 소표본 배지,
  **과거 오염 지표 감사** — 부풀려진 이벤트 승률로 통과했던 과거 진화 결정·Wiki 교훈에
  `metric_version` 부여 후 재검토 (Codex 지적 — 초안 누락 항목)
- **보상 해킹**: held-out 결과를 proposer에 미반환, 후보 family별 탐색 예산 기록,
  반복 후보에 다중검정 보정, gate 통과≠실계좌 승격 (shadow/canary 단계 분리)
- **다양성 붕괴**: 회차당 서로 다른 mechanism 후보 2~3개 생성하되 승격은 1개,
  최근 선택 역비례 탐색 우선순위, 희귀 체제 교훈 보존, 전문가 가중 entropy floor

## 5. 로드맵 (기존 분기 계획과 병합, 4페이즈)

**Phase 0 — 측정 신뢰성** (진행 중 + 즉시, S~M)
1. 원장·TCA 안정화 (가동 중 — 8월 관측)
2. **과거 오염 지표 감사** (분할익절 부풀림 기간의 진화 결정·Wiki 교훈 재계산·표식)
3. 진화 후보·기각 불변 원장 (S)

**Phase 1 — 실패 분류** (9월, M)
4. predicted-vs-counterfactual 대조 리포트
5. held-in/held-out 평가 계약 고정 (인프라 보류 vs 성능 기각 분리 유지)
6. weakness_miner 1단계 (결정론 cohort + verifier 스키마) → 2단계 (원인·중증도)
7. StrategyEvolver 구조화 연결: failure_id → 단일 bounded 제안

**Phase 2 — 기억 구조화** (10월, M)
8. Wiki 안정 ID + 정규화 lesson store + 결정론 dedup
9. Reflector/Curator 주간 흐름 (반례·충돌 관리)
10. 팩터 위험예산 shadow 성적표 → enforce 승격 판단

**Phase 3 — bounded 확장** (11월~, L)
11. point-in-time 유니버스 (생존편향·시점 누수 차단 — **표면 확장의 전제조건**)
12. REGIME_EXIT_PARAMS bounded shadow 진화
13. validator 감점 폭 외부화 → 장기 shadow 최적화
14. 전문가 calibration 리포트 → (조건 충족 시) 가중치 shadow 조정
15. contextual bandit shadow → 제한적 canary

## 6. 하지 말 것 (합의)

1. **Meta-Harness / 하네스 코드 자기수정** — 1인 운영: 리뷰 독립성 없음, 실계좌 즉시 손실
2. **프롬프트 자동 진화** — 약한 평가자를 가장 빨리 공략하는 표면
3. **대규모 evolutionary search** — held-out 표본 고갈, 데이터 마이닝 편향
4. **LLM 의미 dedup을 진실 원천으로** — 반례·적용 범위 소실 (context collapse)
5. **전문가 증원** — 필요한 건 수가 아니라 attribution·calibration
6. **게이트 통과율을 목적함수로** — 조작 가능 proxy. 목적함수는 비용 차감 손익·MDD·calibration
7. **위험예산·bandit 동시 활성화** — 효과 식별 불가. 하나씩 shadow→canary→적용
8. **분산 서브에이전트 인프라** — 현 규모엔 주간 배치 + 파일 원장으로 충분

## 6.5 최종 판정 (2026-08-11 Codex): **조건부 승인**

> 조건: **LessonStore는 단일 프로세스·단일 인스턴스 writer여야 한다.**
> 현행 보장: ① 봇은 systemd 싱글톤 + PID 락(unified_trader.pid)으로 단일 프로세스
> ② 모든 소비자(position_ledger·kr_scheduler·trade_wiki)가 모듈 싱글톤
> `get_lesson_store()` 경유. **다중 프로세스에서 lessons.json을 쓰는 코드를
> 추가하지 말 것** (필요해지면 파일 lock 또는 이벤트 스풀로 전환 — §7 참조).

## 7. 통합 리뷰 후 장기 이관 과제 (2026-08-10 Codex 지적 — 타당하나 현 단계 초과)

- 게이트 반환을 PASS/PERFORMANCE_REJECT/INFRA_HOLD/UNSUPPORTED/INSUFFICIENT
  enum 계약으로 전면 개편 (현재: 기계 생성 제안만 skip 시 fail-closed로 부분 대응)
- evolvable 파라미터 단일 registry (proposer·bounds·gate 매핑·applier 공유)
- lesson 생성의 이벤트 스풀 분리 (현재: fill 경로 동기 upsert — 예외 격리됨)
- PIT 스냅샷 manifest (code SHA·config hash) + gate/replay의 PIT 소비
- candidate_id를 제안 시 1회 생성한 UUID로 (현재: 내용 해시 — 동일일 재시도 충돌 가능)
- 전문가 calibration의 Brier score·자산별 벤치마크 (현재: 방향 적중률 휴리스틱)

## 부록 — 협업 이력

- Claude: 포스트 분석, 코드 실측 grounding (evolve 흐름·wiki 구조·게이트), 초안 P1~P4
- Codex: 초안 교차 검토 — 오염 지표 감사 누락 지적, weakness_miner 스키마 강화
  (verifier_outcome/causal_status/mechanism), "실현" 명칭 정정(사후 반사실),
  point-in-time 우선순위 상향, 편집 표면 3단 분리, 21항목 로드맵 원안
- 이견 없이 수렴한 지점: 프롬프트 자동 진화 금지, 보수적 폐루프가 목표 형태
