# QWQ AI Trader - CLAUDE.md
> 최종 업데이트: 2026-03-03

## 세션 시작 시 필수 읽기
1. **`CHANGELOG.md`** — 최근 변경 이력 확인

## 언어 & 소통
- 모든 대화는 반드시 한국어(한글)로 진행할 것
- '커밋해줘' = commit AND push

## 프로젝트 개요
- KR+US 통합 트레이딩 엔진 (Full Rewrite)
- 단일 KIS appkey로 국내+해외 주식 동시 운영
- 비동기(asyncio) 이벤트 기반 아키텍처
- 단일 포트 8080에서 KR+US 대시보드 통합 서빙

## 프로젝트 경로
- 소스: `/home/user/projects/qwq-ai-trader`
- 가상환경: `venv/`
- 설정: `config/default.yml` (kr: + us: 섹션) + `config/evolved_overrides.yml`
- 환경변수: `.env`

## 핵심 아키텍처
```
UnifiedEngine (단일 엔진)
  ├── contexts: Dict[str, MarketContext]
  │   ├── "kr": MarketContext (KRBroker, KRSession, Portfolio(KRW), ...)
  │   └── "us": MarketContext (USBroker, USSession, Portfolio(USD), ...)
  ├── shared: KISTokenManager, TelegramNotifier, LLMManager
  └── dashboard: DashboardServer (포트 8080)
```

## 디렉토리 구조
```
src/
├── core/           # UnifiedEngine, MarketContext, types, event, evolution/
├── execution/      # broker/ (base, kis_kr, kis_us)
├── strategies/     # base, exit_manager, kr/ (4개), us/ (3개)
├── risk/           # manager.py (통합 RiskManager)
├── data/           # feeds/, providers/, storage/, universe.py
├── signals/        # screener/ (kr, us, swing), sentiment/, fundamentals/, strategic/
├── monitoring/     # health_monitor.py
├── dashboard/      # server, kr_api, us_api, sse, data_collector, static/
├── analytics/      # daily_report.py
├── indicators/     # atr.py, technical.py
├── schedulers/     # kr_scheduler, us_scheduler
└── utils/          # config, token_manager, session, logger, telegram, llm, fee_calculator
```

## 검증 프로토콜
```bash
# 문법 검증
cd /home/user/projects/qwq-ai-trader
source venv/bin/activate
find src/ scripts/ -name "*.py" -size +0c -exec python3 -m py_compile {} \;

# 서비스 관리 (Phase 7 이후)
echo 'user123!' | sudo -S -k systemctl restart qwq-ai-trader
systemctl is-active qwq-ai-trader
journalctl -u qwq-ai-trader -n 20 --no-pager
```

## 코딩 규칙
- 비동기: 모든 I/O는 `async/await`
- 정밀 계산: 금액/가격은 `Decimal(str(value))`
- 한국어: 주석, 로그 메시지 모두 한국어
- pykrx: 반드시 `await asyncio.to_thread(fn)` 래핑
- aiohttp: `timeout=aiohttp.ClientTimeout(total=30)` (숫자 리터럴 금지)
- falsy 패턴 금지: `if value is not None and value < 0` (not `if value and value < 0`)

## 환경변수 (.env)
```
KIS_APPKEY, KIS_APPSECRET, KIS_CANO, KIS_ENV (prod/dev)
KIS_EXT_ACCOUNTS (외부 계좌)
OPENAI_API_KEY, GEMINI_API_KEY
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
INITIAL_CAPITAL (KR, 기본 500000)
```
