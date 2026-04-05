# QWQ AI Trader - CLAUDE.md
> 최종 업데이트: 2026-04-06

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

## 에이전트 팀 (8명)
- 거래 분석: trade-analyst / 시장 분석: market-analyst
- 전략 조언: strategy-advisor / 엔진 점검: engine-monitor
- 리스크 감사: risk-auditor / 파라미터 최적화: param-optimizer
- 코드 리뷰: code-reviewer / 디버깅: debugger

## 프로젝트 개요
- KR+US 통합 트레이딩 엔진 (Full Rewrite)
- 단일 KIS appkey로 국내+해외 주식 동시 운영
- 비동기(asyncio) 이벤트 기반 아키텍처
- 단일 포트 8080에서 KR+US 대시보드 통합 서빙
- 크로스 전략 검증 게이트 + 시장 체제 사전 적응

## 프로젝트 경로
- 소스: `/home/user/projects/qwq-ai-trader`
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

## 핵심 아키텍처
```
UnifiedEngine (단일 엔진)
  ├── contexts: Dict[str, MarketContext]
  │   ├── "kr": MarketContext (KRBroker, KRSession, Portfolio(KRW), ...)
  │   └── "us": MarketContext (USBroker, USSession, Portfolio(USD), ...)
  ├── shared: KISTokenManager, TelegramNotifier, LLMManager
  └── dashboard: DashboardServer (포트 8080)
```

### 실행 흐름

**KR 스케줄러** (`kr_scheduler.py`):
| 태스크 | 간격 | 설명 |
|--------|------|------|
| `run_screening()` | 5분 | 종목 스크리닝 → 자동 시그널 |
| `run_fill_check()` | 10초 | 체결 확인 + WS 구독 갱신 |
| `run_portfolio_sync()` | 30초 | 포트폴리오 동기화 |
| `run_rest_price_feed()` | 20초 | REST 시세 피드 (WS 백업) |
| `run_theme_detection()` | 10분 | 테마 탐지 (뉴스 분석) |
| `run_pending_cleanup()` | 1분 | 교착 pending 정리 |
| `run_supply_demand_cache()` | 5분 | 수급 캐시 갱신 |

**KR 배치**:
- 08:20 `morning_scan` — 전략별 일일 스캔
- 09:01 `execute` — 전일 시그널 실행 (T+1)
- 19:30 `evening_scan` — 넥스트장 데이터로 스코어 보정
- 20:30 `evolve` — 자가 진화 (복기 → 파라미터 조정)

**US 스케줄러** (`us_scheduler.py`):
| 태스크 | 간격 | 설명 |
|--------|------|------|
| `screening_loop()` | 15분 | 유니버스 스캔 → 전략 신호 → 주문 |
| `exit_check_loop()` | 15초 | 보유 포지션 청산 체크 |
| `portfolio_sync_loop()` | 30초 | 잔고 동기화 |
| `order_check_loop()` | 10초 | 미체결 주문 상태 폴링 |
| `eod_close_loop()` | 30초 | 마감 15분 전 DAY 포지션 청산 |
| `screener_loop()` | 60분 | S&P500+400 점수 계산 |
| `watchlist_loop()` | 5분 | 상위 25 + 보유 종목 모니터링 |
| `theme_detection_loop()` | 30분 | US 테마 탐지 |
| `heartbeat_loop()` | 5분 | 상태 로깅 |

## 디렉토리 구조
```
src/
├── core/           # UnifiedEngine, MarketContext, types, event, evolution/
├── execution/      # broker/ (base, kis_kr, kis_us)
├── strategies/     # base, exit_manager, kr/ (5개), us/ (3개)
├── risk/           # manager.py (통합 RiskManager)
├── data/           # feeds/, providers/, storage/, universe.py
├── signals/        # screener/ (kr, us, swing), sentiment/, fundamentals/, strategic/
├── monitoring/     # health_monitor.py
├── dashboard/      # server, kr_api, us_api, sse, data_collector, static/
├── analytics/      # daily_report.py
├── indicators/     # atr.py, technical.py
├── schedulers/     # kr_scheduler, us_scheduler
└── utils/          # config, token_manager, session, logger, telegram, llm, fee_calculator

scripts/
├── run_trader.py       # 통합 트레이더 (--market kr|us|both --dry-run)
└── liquidate_all.py    # 긴급 전량 매도 (--market kr|us --force)
```

---

## 매매 전략

### 공통 사항
- 모든 전략은 `BaseStrategy` 상속, `generate_signal()` + `calculate_score()` 구현
- Decimal 정밀 계산, 최소 주가 KR 1,000원 / US $5

### KR 전략 (5개)
| 전략 | 파일 | 설명 |
|------|------|------|
| 모멘텀 | `kr/momentum.py` | 20일 고가 돌파 + 거래량 급증 |
| 테마추종 | `kr/theme_chasing.py` | 핫 테마 종목 추종 |
| 갭상승 | `kr/gap_and_go.py` | 갭상승 후 눌림목 매수 |
| SEPA | `kr/sepa_trend.py` | SEPA 추세 전략 (스윙) |
| RSI2 반전 | `kr/rsi2_reversal.py` | RSI(2) 과매도 반전 진입 |

### US 전략 (3개)
| 전략 | 파일 | 설명 |
|------|------|------|
| 모멘텀 | `us/momentum.py` | 20일 고가 돌파 브레이크아웃 |
| SEPA | `us/sepa_trend.py` | SEPA 추세 (RS 등급 기반) |
| 어닝스 드리프트 | `us/earnings_drift.py` | EPS 서프라이즈 후 모멘텀 |

### 청산 관리 (ExitManager)
- **1차 익절**: +5% → 30% 매도
- **2차 익절**: +10% → 50% 매도
- **3차 익절**: +12% → 50% 매도
- **트레일링**: 고점 대비 3% 하락, 수익 +5% 이상 시 활성화
- **ATR 동적 손절**: 기본 5%, ATR×2, 범위 4~7%
- **포지션 상태**: `PositionExitState` — NONE/FIRST/SECOND/THIRD/TRAILING 단계 추적

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
| 최대 포지션 수 | 4개 | |
| 기본 포지션 비율 | 25% | |
| 최대 포지션 비율 | 35% | |
| 최소 현금 보유 | 10% | |
| 최소 포지션 금액 | $50 | |
| 연속 손실 중단 | 3회 | 사이징 50% 축소 |

### 수수료
- **KR** (한투 BanKIS, 2026년~): 매수 0.014%, 매도 0.213% (수수료+거래세 0.20%), 왕복 약 0.227%
- **US** (KIS 해외주식): Zero-commission

---

## WebSocket 피드

### KR WebSocket (`kis_websocket.py`)
- **TR ID**: H0STCNT0 (실시간 체결가), H0STASP0 (호가)
- **서버**: ws://ops.koreainvestment.com:21000 (실전), :31000 (모의)
- **재연결**: 5초 지연 (exponential backoff, 최대 120초)

### US WebSocket (`kis_us_price_ws.py`)
- **TR ID**: HDFSCNT0 (해외주식 실시간체결)
- **tr_key**: {exchange}{symbol} (예: NASDAAPL, NYSEMSFT)
- **최대 구독**: 30종목
- **필드**: LAST(현재가), EVOL(체결량), VBID/VASK(호가잔량)

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
cd /home/user/projects/qwq-ai-trader
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

## 문서 업데이트
- 코드 변경 후 CHANGELOG.md 상단에 변경 이력 추가 (날짜, 커밋, 수정 파일, 상세 내용)
- MEMORY.md는 교훈/패턴/규칙만 기록 (변경 이력 금지, 150줄 이하 유지)
- CLAUDE.md는 현재 상태(current state)만 유지

---

## 대시보드 개발 패턴

새 기능 추가 시 아래 순서를 따름:
1. `data_collector.py` — 데이터 수집 메서드 추가
2. `kr_api.py` / `us_api.py` — REST 엔드포인트 추가
3. `sse.py` — 실시간 이벤트 추가 (필요 시)
4. HTML 템플릿 — 카드/페이지 추가
5. JS — 렌더링 함수 + SSE 핸들러

**주요 API 라우트**:
- `/api/portfolio`, `/api/positions`, `/api/orders`, `/api/risk` — KR
- `/api/us/portfolio` — US
- `/api/stream` — SSE 스트림

---

## 운영 모니터링 (HealthMonitor)

| 계층 | 주기 | 체크 항목 |
|------|------|----------|
| Critical | 15초 (장중) | 이벤트 루프 스톨, WS 피드 단절 |
| Important | 60초 | 포트폴리오 불일치, 브로커 연결, 리스크 제한 위반 |
| Periodic | 5분 | 로그 파일 크기, 캐시 정리, 리소스 사용량 |

**알림 쿨다운**: Critical 5분, Warning 15분, Info 1시간

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
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
INITIAL_CAPITAL (KR, 기본 500000)
```

## 의존성
- Python 3.11+
- 핵심: aiohttp, websockets, loguru, pyyaml, pydantic
- 데이터: pandas, numpy, scipy, pykrx, yfinance, finnhub-python
- LLM: openai, google-generativeai
- 모니터링: psutil
- 알림: python-telegram-bot
- US: exchange-calendars, asyncpg

## LLM 모델 선택
| 작업 | Primary | Fallback |
|------|---------|----------|
| 테마 탐지, 뉴스 요약 | Gemini 3.1 Flash Lite | OpenAI gpt-5-mini |
| 거래 복기, 전략 진화 | OpenAI gpt-5.4 | Gemini 3.1 Pro |

---

## 진화 시스템

- 매일 20:30 자동 실행 (KR)
- `TradeReviewer` → `DailyReviewer` → `StrategyEvolver`
- 최대 1개 파라미터만 변경 (race condition 방지)
- 평가 기간: 3영업일 + 5건 이상 거래
- 신뢰도 >= 0.6인 파라미터만 자동 적용
- 즉시 롤백: 손익비 < 1.0
- 내장 규칙: 승률 < 40% → 진입 기준 +5, 승률 > 65% → 진입 기준 -5
- 결과는 `evolved_overrides.yml`에 영속화

## Trade Wiki (Karpathy LLM Wiki 패턴)

- 거래 교훈을 전략/섹터/시장체제별 마크다운 위키로 축적
- 위치: `~/.cache/ai_trader/wiki/`
- 3가지 오퍼레이션:
  - **Ingest**: 매도 체결 → 관련 위키 3~5개 페이지 자동 업데이트 + LLM(Gemini Flash) 교훈 추출
  - **Query**: 크로스검증 시 전략/섹터/체제별 축적 교훈 컨텍스트 반환
  - **Lint**: 토요일 주간 헬스체크 (stale/저조 페이지 감지)
- 동시성: `asyncio.Lock`, fire-and-forget (매매 비차단)
- 크기 제한: 페이지 200줄, 로그 500줄, 전체 ~1MB

## US 엔진 고도화

- ATR 기반 포지션 사이징 (3개 전략 통일)
- SPY/QQQ 기반 시장 체제 판단 (`us_market_regime.py`)
- 크로스 검증 게이트 6규칙 (수급 제외, bear시 어닝스 허용)
- 체제별 파라미터: min_score_adj, max_daily_new_buys, position_mult_boost

---

## 트러블슈팅

### 봇이 응답 없거나 이상할 때
```bash
systemctl status qwq-ai-trader.service
journalctl -u qwq-ai-trader -n 50 --no-pager

# 재시작
echo 'user123!' | sudo -S -k systemctl restart qwq-ai-trader

# 싱글톤 락 파일 충돌 시
echo 'user123!' | sudo -S -k systemctl stop qwq-ai-trader
rm -f ~/.cache/ai_trader/*.lock ~/.cache/ai_trader/*.pid
echo 'user123!' | sudo -S -k systemctl start qwq-ai-trader
```

### 포트폴리오 동기화 이슈
- KIS API 응답 지연(수 분) → 유령 포지션 발생 가능
- 청산 실패 시 `broker.get_positions()`로 실제 보유 확인 후 정리
- 동기화 주기: KR 30초, US 30초

### WebSocket 중복 프로세스
- "ALREADY IN USE appkey" → `pkill -9 -f "run_trader.py"` 후 단일 재시작

### 매수가 실행되지 않을 때
1. 가용 현금 확인 (`get_available_cash()`)
2. 일일 손실 한도 도달 여부 (KR -5%, US -3%)
3. 스크리닝 쿨다운 확인
4. 일일 거래 횟수 한도 (KR 10회)
5. 로그에서 `[스크리닝]` 항목 확인

### 긴급 전량 매도
```bash
cd /home/user/projects/qwq-ai-trader
source venv/bin/activate
python scripts/liquidate_all.py --market kr    # KR 전량 매도
python scripts/liquidate_all.py --market us    # US 전량 매도
python scripts/liquidate_all.py --force        # 확인 없이 즉시 실행
```

## 실행 방법
```bash
source venv/bin/activate
python scripts/run_trader.py --market both                # KR+US 동시 실거래
python scripts/run_trader.py --market kr --dry-run        # KR 테스트
python scripts/run_trader.py --market us                  # US만 실거래
```
