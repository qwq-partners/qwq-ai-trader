# QWQ AI Trader — 기술 문서

> 에이전트 참조용 구조화 문서. 개발/분석 시 카테고리별 참조.

## 문서 목록

### Architecture (아키텍처)
- [system-overview.md](architecture/system-overview.md) — 전체 시스템 구조, 신호 흐름(KR/US), 비동기 아키텍처, 핵심 파일
- [harness-evolution-design.md](architecture/harness-evolution-design.md) — 하네스 엔지니어링 적용 설계 (2026-08-10, Claude+Codex 협업): weakness_miner·기각 후보 원장·Wiki ACE 격상·편집 표면 정책·4페이즈 로드맵

### Strategies (전략)
- [kr-strategies.md](strategies/kr-strategies.md) — KR 6개 전략: SEPA, RSI2, Theme, Gap, Strategic Swing, Core (스코어링, 가드, 사이징)
- [us-strategies.md](strategies/us-strategies.md) — US 3개 전략 + 시장체제 + 크로스검증 6규칙

### Risk (리스크)
- [risk-and-exit.md](risk/risk-and-exit.md) — 리스크 한도, 크로스검증 9규칙, 분할익절, ATR 동적손절, 포지션 사이징

### Evolution (진화)
- [evolution-system.md](evolution/evolution-system.md) — 3계층 메모리, Trade Wiki, 전략 진화, 일일 복기, 품질 검증, 거래 원칙

### Operations (운영)
- [local-development.md](operations/local-development.md) — WSL2 기반 Claude Code·Codex 로컬 개발환경 구성과 안전한 PR 흐름
- [github-quality-gate.md](operations/github-quality-gate.md) — PR 자동 검증과 `main` 브랜치 보호 운영 절차
- [runbook.md](operations/runbook.md) — 봇 관리, 코드 변경 프로토콜, 트러블슈팅, 캐시/로그 위치
- [monitoring-checkpoints.md](operations/monitoring-checkpoints.md) — 변경 적용 후 검증 체크포인트 (시점·전략별)
- [virtual-office.md](operations/virtual-office.md) — 가상 오피스(`/office`) 픽셀아트 시각화: 역할 매핑, 상태 API, 재빌드

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
| **engine-monitor** | runbook.md, system-overview.md, virtual-office.md |
| **risk-auditor** | risk-and-exit.md, runbook.md |
| **param-optimizer** | evolution-system.md, kr-strategies.md |
