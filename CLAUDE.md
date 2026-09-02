# QWQ AI Trader - CLAUDE.md
> 최종 업데이트: 2026-09-03 (운영 리뷰 — 현금 고갈 상태·섀도우 현황 실측 반영, 상세는 docs/ 참조)

## 세션 시작 시 필수 읽기

작업 시작 전 **반드시** 아래 파일을 읽을 것:

1. **`CHANGELOG.md`** — 최근 변경 이력 확인
   - 이미 구현된 기능 중복 작업 방지
   - 설계 결정 맥락 파악 (왜 이렇게 짜여 있는지)
   - 알려진 미해결 이슈 파악
2. **`docs/README.md`** — 기술 문서 인덱스
   - 아키텍처, 전략, 리스크, 진화 시스템, 운영, API 연동 상세 문서
   - 에이전트별 참조 가이드 포함

> 예: 유저가 "X 기능 추가해줘" 요청 시 → CHANGELOG에서 이미 구현됐는지 먼저 확인
> 예: 전략 수정 시 → `docs/strategies/kr-strategies.md` 참조

## 언어 & 소통
- 모든 대화는 반드시 한국어(한글)로 진행할 것
- '커밋해줘' = commit AND push. '푸시' = push. 애매하면 commit + push 기본.
- 'new' 또는 'fresh'로 요청하면 이전 실패한 패턴 참조 금지.

## Git & GitHub
- Use SSH for git push (not HTTPS). PAT-based auth if SSH unavailable.
- Always commit and push together unless explicitly told otherwise.
- `gh auth login` interactive mode does NOT work in this environment.

## 에이전트 팀 (15명 — 운영·분석 8명 + 도메인 전문가 7명)
- 명단·역할·주기: `.claude/agents/` 디렉토리 참조
- 전문가 시스템 상세: `docs/agents/expert-system.md` / 코드: `src/experts/`
- 출력: `ExpertOpinion` (score/bias/confidence/findings) → market_regime + cross_validator

## 프로젝트 개요
- KR+US 통합 트레이딩 엔진 (Full Rewrite)
- 단일 KIS appkey로 국내+해외 주식 동시 운영
- 비동기(asyncio) 이벤트 기반 아키텍처
- 단일 포트 8080에서 KR+US 대시보드 통합 서빙
- 크로스 전략 검증 게이트 + 시장 체제 사전 적응

## 프로젝트 경로
- 소스: `/home/ubuntu/projects/qwq-ai-trader`
- 가상환경: `venv/` (.venv 아님)
- 설정: `config/default.yml` (kr: + us: 섹션) + `config/evolved_overrides.yml`
- 환경변수: `.env`
- 로그: `logs/YYYYMMDD/`
- 캐시/상태: `~/.cache/ai_trader/`
  - `trade_journal[_kr|_us].json` — 거래 기록
  - `daily_stats[_kr|_us].json` — 일일 손익 영속화
  - `unified_trader.pid` — PID 파일

## 설정 주의사항
> **`evolved_overrides.yml`이 `default.yml` 위에 머지됨**
>
> 설정 변경 시 양쪽 모두 확인 필요. evolved_overrides가 default를 덮어쓰므로,
> default.yml만 바꿔도 evolved_overrides에 같은 키가 있으면 적용 안 됨.

## 아키텍처 & 실행 흐름
- UnifiedEngine 구조·스케줄러 태스크 주기·KR 배치 시각: `docs/architecture/system-overview.md` 및 `src/schedulers/` 소스 참조
- 디렉토리 구조는 `src/` 하위 `ls`로 확인 (모듈별 한 줄 설명은 위 아키텍처 문서)

---

## 매매 전략

### 공통 사항
- 모든 전략은 `BaseStrategy` 상속, `generate_signal()` + `calculate_score()` 구현
- Decimal 정밀 계산, 최소 주가 KR 1,000원 / US $5

### KR 전략 (6개)
| 전략 | 파일 | 설명 |
|------|------|------|
| 모멘텀 | `kr/momentum.py` | 20일 고가 돌파 + 거래량 급증 |
| 테마추종 | `kr/theme_chasing.py` | 🚫 **폐지** (2026-05-04, allocation 0%) |
| 갭상승 | `kr/gap_and_go.py` | 갭상승 후 눌림목 매수 |
| SEPA | `kr/sepa_trend.py` | SEPA 추세 전략 (스윙) |
| RSI2 반전 | `kr/rsi2_reversal.py` | 🚫 **폐지** (2026-08-02, enabled=false + allocation 0% — 백테스트 단독 -15.44%, 근거는 evolved_overrides `_meta`) |
| VCP 돌파 | (배치 스캔 라인) | 변동성 수축 후 20일 고점 돌파 — **선행 발굴** (2026-08-03~) |
| 밸류코어 | `value_growth_screener.py` | 가치·성장 2버킷 장기보유 — 🔍 **shadow 관측 중** (2026-08-04~, 주문 없음, 설계 `docs/strategies/value-growth-core-design.md`) |

### US 전략 (4개)
| 전략 | 파일 | 설명 |
|------|------|------|
| 모멘텀 | `us/momentum.py` | 20일 고가 돌파 브레이크아웃 |
| SEPA | `us/sepa_trend.py` | SEPA 추세 (RS 등급 기반) |
| 어닝스 드리프트 | `us/earnings_drift.py` | EPS 서프라이즈 후 모멘텀 (🚫 비활성 — 2026-08-03 검증 통과했으나 US 미운용·EPS 커버리지 부족으로 보류) |
| 어닝스 리버설 | `us/earnings_reversal.py` | 발표 전 낙폭과대 반등 (⛔ 검증 기각 2026-08-03 — 활성화 금지) |

### 청산 관리 (ExitManager)
- **1차 익절**: +10% → 10% 매도 (2026-08-02 백테스트 검증으로 +5%/20%에서 조정)
  - 기존값은 평균 1.9일에 발동해 추세 초입을 절단 (익절 +5.2% < 손절 -6.2%)
  - ⚠️ 변경 시 4곳 동시 수정: `default.yml` / `evolved_overrides.yml` / `ExitConfig` / **`REGIME_EXIT_PARAMS`**
- **2차 익절**: +15% → 잔여의 50% 매도
- **3차 익절**: +25% → 잔여의 50% 매도 (기본값, 레짐별 REGIME_EXIT_PARAMS로 조정)
- **트레일링**: 고점 대비 3% 하락, 수익 +5% 이상 시 활성화
- **ATR 동적 손절**: 기본 5%, ATR×2, 범위 3.5~8% (evolved_overrides)
- **포지션 상태**: `PositionExitState` — NONE/FIRST/SECOND/THIRD/TRAILING 단계 추적

### 코어홀딩 A안 (2026-05-11~ "장기 추세 캐처")
- **진입 필터**: MA200 위 + 60일 ≥+5% + 신고가 80% 이내 → 박스권 자동 배제
- **점수 (100점)**: 추세 20 + 펀더 20 + 수급 20 + 모멘텀 30 + RS등급 10
  + **저변동성 감점** 0~-10 (일수익률 60일 σ 기준, 2026-08-03~, 급등락형 배제)
  + **자산 확장 감점** 0~-5 (DART 총자산 증가율 ≥30/50%, 2026-08-03~, 퀄리티 팩터)
- **청산**: stop_loss 10%, trailing 12% (느슨), 분할익절 OFF, max_holding 무제한
- **stale**: Tier1 20영업일±3%, Tier2 30영업일±3% OR 20영업일±2%+거래량50%
- **리밸런싱**: 격주 (rebalance_interval_weeks=2)

### 캘린더 시즈널리티 오버레이 (2026-08-03~)
- `src/utils/calendar_seasonality.py` — 전 전략 매수 **사이징 배율** (독립 전략 아님)
- turn-of-month(월말 2+월초 3거래일) KR/US ×1.10, US 옵션만기주 ×1.05 (KR 만기주 미적용)
- 부스트 전용(차단·축소 없음), 상한(max_position_pct)은 재적용됨
- 비활성화: `CALENDAR_SEASONALITY=0`

### 조건부 변동성 타게팅 오버레이 (2026-08-19~)
- `src/utils/volatility_targeting.py` — 모멘텀 계열(sepa/gap/momentum/vcp) 매수 **사이징 축소 배율**
- KOSPI 20일 실현변동성 > **25%**(전체 거래일의 ~11% 극단 국면)일 때만 ×(25/vol), 하한 0.4
- 검증: KODEX200 2015~26 — Sharpe 0.721→0.787, MDD -40.8→-34.8% (CAGR -2.1%p 비용)
- 캐시: `~/.cache/ai_trader/vol_targeting.json` (매 거래일 08:30 갱신, 노후 3일+ 시 무개입)
- 축소 전용(레버리지 없음), 일수익률 |12%| 초과는 데이터 오류로 제외
- 비활성화: `VOL_TARGETING=0` / 상세: `docs/research/ai-trading-research-2026-08.md`

### 팀 심의 conviction 부스트 (2026-08-20~)
- `src/utils/team_conviction.py` — 팀 심의(BUY 승인 + conviction ≥0.75/0.90)
  종목의 신규 매수 사이징 ×1.10/×1.20 **부스트 전용**
- HOLD/REJECT는 사이징에 미반영 — CF 실측(47건): HOLD 차단 후보가 5일 +6.31%
  (차단·감액 용도는 손해로 판명)
- 비활성화: `TEAM_CONVICTION=0`

---

## 리스크 관리

### KR 리스크
| 항목 | 값 | 비고 |
|------|---|------|
| 일일 최대 손실 | -5.0% | effective_daily_pnl 기준 |
| 일일 거래 횟수 | 10회 | daily_max_trades |
| 일일 신규 매수 | 5개 | max_daily_new_buys |
| 최대 포지션 수 | 8개 | max_positions |
| 기본 포지션 비율 | 25% | equity 대비 |
| 최대 포지션 비율 | 28% | 개별 포지션 상한 |
| 최소 현금 보유 | 5% | total_equity 대비 |
| 최소 포지션 금액 | 20만원 | 미달 시 매수 거부 |

### US 리스크
| 항목 | 값 | 비고 |
|------|---|------|
| 일일 최대 손실 | -3.0% | |
| 최대 포지션 수 | 10개 | |
| 기본 포지션 비율 | 25% | |
| 최대 포지션 비율 | 35% | |
| 최소 현금 보유 | 10% | |
| 최소 포지션 금액 | $50 | |
| 연속 손실 중단 | 3회 | 사이징 50% 축소 |

### 팩터 버킷 위험예산 (2026-08-08~, shadow 관측 중)
- trend 65% / quality 20% / reversion 10% — 상관 전략 묶음 총 노출 캡 (`default.yml factor_budgets`)
- 현재 `enforce: false` (초과 시 로그만) — 상세 `docs/risk/risk-and-exit.md`
- **승격 보류 (2026-08-19)**: 8월 매수 0건으로 초과 이벤트 표본 부재 →
  일일 노출 스냅샷(`factor_exposure_log.jsonl`, 저녁 품질검증 잡) 2주 축적 후 재판단
  → **2026-09-03 재판단 불가**: 전략 포지션 0건이라 스냅샷 전부 0% — 매수 재개 후 2주로 이월

### 운영 상태 — 현금 고갈 (2026-07-01~, 2026-09-03 확인)
- 펩트론 087010 120주(`manual`, exit_exempt)가 자산의 **99.6%**, 현금 **0.4%(~7.8만원)**
  → 봇 신규 매수가 구조적으로 불가 (8·9월 주문 0건은 버그가 아니라 현금 부족).
  진화·CF·승격 표본 축적 전부 정지 상태. 해소는 사용자 판단(펩트론 일부 매도/입금).
- `/api/portfolio`의 `cash_ratio`로 즉시 확인. 코어홀딩 "빈슬롯 매수 시도(예산 잔여 2.9M)"
  로그는 equity 기준 예산이라 현금과 무관 — 0건 반복은 정상.

### 섀도우 관측 현황 (2026-09-03 운영 서버 점검 기준)
관측 전용(주문 무관) 항목 전체 목록 — 상세는 각 문서 참조:

| 항목 | 시작 | 상태 |
|------|------|------|
| 밸류코어 (`value_growth_core.shadow_mode`) | 08-04 | ✅ 주간 이력 3/8주 (W34~W36, 승격 평가는 8주+) |
| 비대칭 수확 G3 (`harvest_shadow.py`) | 08-13 | ✅ 매일 08:40 실행 중 — 2/30체결, 누적 -3.2R |
| 에이전트 팀 심의 (`trading_team`) | 08-02 | ✅ 장중 10:30/14:00 verdicts 축적 중 |
| 규칙 #11 전문가 BEAR (`experts.shadow_mode`) | 08-02 | ⏳ 14건 축적 / 승격 기준 CF 9/20건·r5 56% |
| 규칙 #12 섹터 카운슬 | 08-07 | ⏸ hit 0건 (BEAR 섹터 매수 후보 없음) |
| 팩터 버킷 (`factor_budgets.enforce: false`) | 08-08 | ⏸ 초과 표본 없음 → 노출 스냅샷으로 보완 (08-19~) |
| Counterfactual 추적 | 08-08 | ✅ 209건 추적 중 (신규 매수 0건이라 증가분은 규칙 게이트 발화분) |
| Shadow Lab (calibration/bandit) | 08-10 | ✅ 주기 리포트 발송 중 |
| LLM Shadow A/B (`openai_model_light_shadow`) | 06-17 | 🚫 **비활성화 (08-19)** — 발화 경로 소멸로 8/3 이후 표본 0 |

### 수수료
- **KR** (한투 BanKIS, 2026년~): 매수 0.014%, 매도 0.213% (수수료+거래세 0.20%), 왕복 약 0.227%
- **US** (KIS 해외주식): Zero-commission

---

## 검증 프로토콜 (절대 규칙)
코드 수정 후 반드시 아래 순서 수행:
1. `python3 -m py_compile <수정파일>` — 문법 검증
2. **봇 재시작**: `echo 'user123!' | sudo -S -k systemctl restart qwq-ai-trader`
   - ⚠️ `nohup python scripts/run_trader.py` 직접 실행 **절대 금지** (systemd와 충돌)
3. 상태 확인: `systemctl is-active qwq-ai-trader`
4. 로그 확인: `journalctl -u qwq-ai-trader -n 20 --no-pager`
5. 에러 없으면 완료 보고, 있으면 즉시 수정

```bash
# 문법 검증 (전체)
cd /home/ubuntu/projects/qwq-ai-trader
source venv/bin/activate
find src/ scripts/ -name "*.py" -size +0c -exec python3 -m py_compile {} \;

# 봇 관리 명령어
echo 'user123!' | sudo -S -k systemctl restart qwq-ai-trader  # 재시작
echo 'user123!' | sudo -S -k systemctl stop qwq-ai-trader     # 중지
systemctl is-active qwq-ai-trader                              # 상태
journalctl -u qwq-ai-trader -f                                 # 실시간 로그
```

## 코드 리뷰 프로토콜
사용자가 "리뷰해봐" 요청 시:
1. 변경된 모든 파일 재읽기 (캐시 의존 금지)
2. P0(치명적), P1(중요), P2(경미) 우선순위로 이슈 분류
3. 각 이슈: 파일명 + 라인번호 + 구체적 문제 + 수정방안
4. P0부터 수정 → py_compile → 재시작 → 로그 확인

## 문서 업데이트 (절대 규칙)

> **대전제: 모든 코드 변경은 관련 문서에도 반드시 반영해야 한다.**

코드 변경 시 **반드시** 아래 문서를 함께 업데이트:

1. **`CHANGELOG.md`** — 변경 이력 (날짜, 커밋, 수정 파일, 상세 내용)
2. **`docs/` 관련 문서** — 변경된 기능에 해당하는 기술 문서 업데이트
   - 전략 수정 → `docs/strategies/kr-strategies.md` 또는 `us-strategies.md`
   - 리스크/청산 변경 → `docs/risk/risk-and-exit.md`
   - 진화/위키 변경 → `docs/evolution/evolution-system.md`
   - 아키텍처/흐름 변경 → `docs/architecture/system-overview.md`
   - API 연동 변경 → `docs/integrations/external-apis.md`
   - 운영 절차 변경 → `docs/operations/runbook.md`
3. **`CLAUDE.md`** — 현재 상태(current state) 반영 (설정값, 전략 배분 등)
4. **`MEMORY.md`** — 교훈/패턴/규칙만 기록 (변경 이력 금지, 150줄 이하)

**문서 미업데이트 시 코드 변경 불완전으로 간주한다.**

---

## 대시보드 개발
- 새 기능 추가 절차·API 라우트·페이지 목록: `.claude/skills/dashboard-feature/SKILL.md` (대시보드 작업 시 로드)
- 가상 오피스 `/office`: 재빌드 `bash tools/office/build.sh` — **`static/office/`는 빌드 산출물이므로 직접 수정 금지** (상세: `docs/operations/virtual-office.md`)

---

## 코딩 규칙

### 패턴
- **비동기**: 모든 I/O는 `async/await` (aiohttp, asyncio)
- **데이터클래스**: 도메인 모델은 `@dataclass`
- **정밀 계산**: 금액/가격은 `Decimal` 사용 — `Decimal(str(value))` 로 변환 (float → Decimal 오차 방지)
- **한국어**: 주석, 로그 메시지 모두 한국어
- **로그 태그**: `[리스크]`, `[스크리닝]`, `[진화]` 등
- **pykrx**: 반드시 `await asyncio.to_thread(pykrx_func)` 래핑 — 동기 블로킹 금지
- **aiohttp timeout**: `timeout=aiohttp.ClientTimeout(total=30)` (숫자 리터럴 금지)

### 절대 금지 패턴

```python
# ❌ 잘못된 패턴 — 0, 0.0, "" 이 False로 처리됨
if value and value < 0:        # 0.0은 통과 안 됨
if atr and atr > 0:            # atr=0 조건 누락
result = value or default      # value=0 이면 default 반환

# ✅ 올바른 패턴
if value is not None and value < 0:
if atr is not None and atr > 0:
result = value if value is not None else default
```

### 주의사항
- `.env`에 API 키 저장 (커밋 금지)
- KIS API 토큰은 `~/.cache/ai_trader/`에 캐시
- **Position.current_price 반드시 체결가로 초기화** — 미초기화 시 unrealized_pnl -100% → 일일손실 즉시 트리거
- **pending 상태 관리**: 예외 핸들러에서 반드시 `clear_pending()` 호출 (누수 방지)
- **파일 수정 시 연관 체크**: types.py ↔ engine.py, exit_manager.py ↔ schedulers, config.py ↔ YAML
- **수수료 계산**: `FeeCalculator` 단일 사용 — data_collector/storage 내 하드코딩 금지
- **영업일 계산**: `is_kr_market_holiday()` 반드시 사용 (주말/공휴일 처리)

---

## 환경변수 (.env)
```
KIS_APPKEY, KIS_APPSECRET, KIS_CANO, KIS_ENV (prod/dev)
KIS_EXT_ACCOUNTS (외부 계좌, 형식: 이름:CANO:ACNT_PRDT_CD 쉼표 구분)
OPENAI_API_KEY, GEMINI_API_KEY
MANUS_API_KEY (미사용 — 2026-08-19 구독 해지, manus.enabled=false)
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
INITIAL_CAPITAL (KR, 기본 500000)
```

## LLM 모델 선택
| 작업 | Primary | Fallback |
|------|---------|----------|
| 테마 탐지, 뉴스 요약 | Gemini 3.1 Flash Lite | OpenAI gpt-5-mini |
| 거래 복기, 전략 진화 | OpenAI gpt-5.6-sol (2026-08-19~ Manus 해지로 회귀) | Gemini 3.1 Pro |

---

## 긴급 정지 (킬스위치, 2026-08-02~)

파일 하나로 주문을 즉시 차단. **봇 재시작 불필요**, 브로커 계층에서 검사하므로 엔진 오작동 시에도 동작.

```bash
touch ~/.cache/ai_trader/KILL_SWITCH       # 신규 매수만 차단 (청산 허용)
touch ~/.cache/ai_trader/KILL_SWITCH_ALL   # 전면 동결 (⚠️ 손절도 막힘)
rm ~/.cache/ai_trader/KILL_SWITCH          # 해제
```

- 시장별: `KILL_SWITCH_KR` / `KILL_SWITCH_US`, 파일 내용은 차단 사유로 기록됨 (반영 최대 2초)
- 감사 원장: `~/.cache/ai_trader/audit/audit_YYYYMM.jsonl` (시도된 모든 주문 append-only)

## 진화 시스템

- 매일 20:30 자동 실행 (KR)
- `TradeReviewer` → `DailyReviewer` → `StrategyEvolver` → **`BacktestGate`**
- 최대 1개 파라미터만 변경 (race condition 방지)
- 평가 기간: 5영업일 + 10건 이상 거래
- 신뢰도 >= 0.6인 파라미터만 자동 적용
- **백테스트 사전 검증 (2026-08-02~)**: 적용 전 A/B 백테스트(6개월/60종목)로 개선 확인.
  수익률 개선 + **walk-forward 구간승 2/3** (2개월×3구간, 2026-08-03~) + MDD 악화 ≤1%p
  + 거래 ≥10건이어야 통과. 실패 시 **보류(fail-closed)**.
  `EVOLUTION_BACKTEST_GATE=0`으로 비활성화 가능
- 즉시 롤백: 손익비 < 1.0
- 내장 규칙: 승률 < 40% → 진입 기준 +5, 승률 > 65% → 진입 기준 -5
- 결과는 `evolved_overrides.yml`에 영속화

## 주간 매도 후속 복기 (Post-Exit Review)

- 매주 토요일 09:00 KST 자동 실행 (`run_post_exit_review_scheduler`)
- 최근 30일 KR 매도 거래 → KIS 현재가 조회 → 매도 후 변동 추적
- 분류: +3% 이상=놓침, -3% 이하=회피, 그 사이=타당
- LLM: GPT-5.4 (STRATEGY_ANALYSIS, fallback Gemini Pro), 표본 ≥5건
- 출력: JSON + Wiki 페이지(`weekly_post_exit_YYYY-WNN.md`) + 텔레그램
- Wiki 페이지는 다음 weekly rebalance 시 LLM 컨텍스트로 자동 흡수

## Trade Wiki (Karpathy LLM Wiki 패턴)

- 거래 교훈을 전략/섹터/시장체제/**종목**별 마크다운 위키로 축적 (종목 차원 2026-08-07~)
- 위치: `~/.cache/ai_trader/wiki/`
- 3가지 오퍼레이션:
  - **Ingest**: 매도 체결 → 관련 위키 3~5개 페이지 자동 업데이트 + LLM(Gemini Flash) 교훈 추출
  - **Query**: 크로스검증 시 전략/섹터/체제별 교훈 + **종목 노트**(query_symbol) 컨텍스트 반환
  - **Lint**: 토요일 주간 헬스체크 (stale/저조 페이지 감지 + 180일 종목 페이지 아카이브)
- **종목 페이지** (`symbols/<코드>.md`): 거래 이력·교훈 + 전문가 리서치 노트
  (orchestrator가 9명 전문가 affected_symbols를 fire-and-forget 기록, 일일 출처 dedup,
  30개 롤링, 리서치 전용 신규 페이지 상한 200)
- 동시성: `asyncio.Lock`, fire-and-forget (매매 비차단)
- 크기 제한: 페이지 200줄, 로그 500줄, 전체 ~1MB

## US 엔진 고도화

- ATR 기반 포지션 사이징 (3개 전략 통일)
- SPY/QQQ 기반 시장 체제 판단 (`us_market_regime.py`)
- 크로스 검증 게이트 6규칙 (수급 제외, bear시 어닝스 허용)
- 체제별 파라미터: min_score_adj, max_daily_new_buys, position_mult_boost

---

## 트러블슈팅
- **전체 절차는 `docs/operations/runbook.md` 참조** — 봇 미응답, 싱글톤 락 충돌, 매수 미실행 체크리스트, 유령 포지션, WebSocket 중복 프로세스, DB 좀비 정리, 긴급 전량 매도

## 실행 방법
```bash
source venv/bin/activate
python scripts/run_trader.py --market both                # KR+US 동시 실거래
python scripts/run_trader.py --market kr --dry-run        # KR 테스트
python scripts/run_trader.py --market us                  # US만 실거래
```
