# QWQ AI Trader — 기술 문서

> 에이전트 참조용 구조화 문서. 개발/분석 시 카테고리별 참조.

## 문서 목록

### Architecture (아키텍처)
- [system-overview.md](architecture/system-overview.md) — 전체 시스템 구조, 신호 흐름(KR/US), 비동기 아키텍처, 핵심 파일

### Strategies (전략)
- [kr-strategies.md](strategies/kr-strategies.md) — KR 6개 전략: SEPA, RSI2, Theme, Gap, Strategic Swing, Core (스코어링, 가드, 사이징)
- [us-strategies.md](strategies/us-strategies.md) — US 3개 전략 + 시장체제 + 크로스검증 6규칙

### Risk (리스크)
- [risk-and-exit.md](risk/risk-and-exit.md) — 리스크 한도, 크로스검증 9규칙, 분할익절, ATR 동적손절, 포지션 사이징

### Evolution (진화)
- [evolution-system.md](evolution/evolution-system.md) — 3계층 메모리, Trade Wiki, 전략 진화, 일일 복기, 품질 검증, 거래 원칙

### Operations (운영)
- [runbook.md](operations/runbook.md) — 봇 관리, 코드 변경 프로토콜, 트러블슈팅, 캐시/로그 위치
- [monitoring-checkpoints.md](operations/monitoring-checkpoints.md) — 변경 적용 후 검증 체크포인트 (시점·전략별)

### Integrations (연동)
- [external-apis.md](integrations/external-apis.md) — KIS, pykrx, yfinance, Finnhub, Finviz, LLM(OpenAI/Gemini/Perplexity), Telegram, DART

### Legacy
- [ROADMAP_AGENT_TEAM.md](ROADMAP_AGENT_TEAM.md) — 에이전트 팀 6-Phase 로드맵 (초기 설계)

## 에이전트별 참조 가이드

| 에이전트 | 우선 참조 문서 |
|---------|-------------|
| **trade-analyst** | kr-strategies.md, risk-and-exit.md, evolution-system.md |
| **market-analyst** | us-strategies.md (시장체제), external-apis.md |
| **strategy-advisor** | kr-strategies.md, us-strategies.md, evolution-system.md |
| **engine-monitor** | runbook.md, system-overview.md |
| **risk-auditor** | risk-and-exit.md, runbook.md |
| **param-optimizer** | evolution-system.md, kr-strategies.md |
