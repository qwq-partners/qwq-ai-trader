# 진화 시스템 + Trade Wiki

> 최종 갱신: 2026-04-06

## 전체 구조

```
src/core/evolution/
├── trade_memory.py      — 3계층 거래 메모리 (L1→L2→L3)
├── trade_wiki.py        — Karpathy LLM Wiki 패턴 (전략/섹터/체제별 축적)
├── trading_principles.py — 핵심 불변 원칙 21개 + 학습 원칙
├── daily_reviewer.py    — 일일 거래 복기 (LLM 종합 평가)
├── quality_validator.py — 20:30 evolve 직전 5개 품질 검증
├── strategy_evolver.py  — 파라미터 자동 튜닝 + 주간 리밸런싱
├── trade_reviewer.py    — 개별 거래 복기
├── trade_journal.py     — 거래 기록 (JSON + DB)
└── config_persistence.py — evolved_overrides.yml 영속화
```

## 1. 거래 메모리 (trade_memory.py)

### 3계층 구조
| 계층 | 보존 기간 | 내용 | 최대 |
|------|----------|------|------|
| L1 (원시) | 0~7일 | TradeOutcome: 종목/전략/섹터/지표/체제 | 200건 |
| L2 (요약) | 8~30일 | TradeSummary: 패턴+결과+승률 | 500건 |
| L3 (원칙) | 31일+ | TradePrinciple: 규칙+신뢰도+delta | 무제한 |

### 주요 메서드
- `record_outcome()`: 매도 체결 시 L1 기록
- `compress_layers()`: 금요일 20:30 — L1→L2→L3 + LLM 구조화 복기 (AVOID/FOCUS)
- `get_score_adjustment()`: 매수 시 L3 원칙 매칭 → ±3점 보정
- `get_context_for_signal()`: LLM 이중검증용 최근 유사 거래 컨텍스트

### 원칙 추출 규칙
- 긍정: 승률 ≥ 60% AND avg > 1.0% → +1~+3
- 경고: 승률 ≤ 35% AND avg < -1.0% → -1~-3
- 시장 레벨별 원칙도 추출 (KOSPI 구간별 승률)

## 2. Trade Wiki (trade_wiki.py) — Karpathy LLM Wiki 패턴

### 위치
`~/.cache/ai_trader/wiki/`

### 구조
```
wiki/
├── index.md              — 카테고리별 카탈로그 (매 ingest 자동 재생성)
├── log.md                — 추가전용 시계열 기록 (500줄 상한)
├── strategies/
│   ├── sepa_trend.md     — 승률/건수 + 최근 거래 테이블 + LLM 교훈
│   ├── rsi2_reversal.md
│   └── ...
├── sectors/
│   └── {섹터명}.md       — 섹터별 거래 패턴
└── regimes/
    ├── bull.md           — 강세장 학습
    ├── bear.md
    └── sideways.md
```

### 3가지 오퍼레이션

#### Ingest (매도 체결 시)
1. 전략 페이지: 프론트매터(trade_count, wins, win_rate) + 거래 테이블 행 추가
2. 섹터 페이지: 동일 구조
3. 체제 페이지: 동일 구조
4. LLM 교훈 추출: Gemini Flash → 1~2줄 → 전략 페이지 "## 교훈" 섹션
5. log.md 추가
6. index.md 재생성

#### Query (크로스검증 시)
- 전략/섹터/체제 페이지에서 프론트매터 통계 + 최근 교훈 3건 반환
- 순수 파일 읽기 (<1ms, LLM 불필요)
- 크로스검증 LLM 프롬프트에 "위키 교훈:" 컨텍스트 주입

#### Lint (토요일 주간)
- stale 페이지 감지 (30일+ 미업데이트)
- 저조 승률 경고 (5건+ 거래, 30% 미만)

### 안전장치
- `asyncio.Lock`: 동시 ingest 방지
- fire-and-forget: `asyncio.create_task()`, 매매 비차단
- 크기 제한: 페이지 200줄, 로그 500줄, 거래 테이블 30행 FIFO

### LLM 교훈 추출
- 태스크: `LLMTask.WIKI_INGEST` (Gemini Flash, ~$0.0001/회)
- 메서드: `self._llm.complete(prompt, task=..., max_tokens=150)`
- 응답: `resp.content.strip()` (LLMResponse 객체)

## 3. 전략 진화 (strategy_evolver.py) — 매일 20:30 자동 실행

### 유효 전략
```python
_VALID_STRATEGIES = {
    "momentum_breakout", "sepa_trend", "rsi2_reversal",
    "theme_chasing", "gap_and_go", "strategic_swing",
}
```

### 가드레일
- 최소 5%, 최대 60% (개별 전략)
- 주당 변동 ±10%p 이내
- 합계 100% 강제 (재검증 루프 3회)
- 비활성 전략 0% 고정 (momentum_breakout)
- core_holding은 별도 관리 (locked)

### 주간 리밸런싱 (토요일 00:00)
- LLM(Gemini) 기반 성과 분석 → 배분 제안
- 가드레일 적용 후 `evolved_overrides.yml` 저장
- 텔레그램 리포트

## 4. 일일 복기 (daily_reviewer.py)

### 매일 16:00 — 거래 리포트
- 당일 거래 전체 집계 (매수/매도/청산)
- 오후 결과 레포트 텔레그램 발송

### 매일 20:30 — LLM 종합 평가
- GPT-5.4 (TRADE_REVIEW) 기반 거래별 복기
- 평가: good / fair / poor
- 회피 패턴 + 집중 기회 추출
- `llm_review_{date}.json` 저장

## 5. 품질 검증 (quality_validator.py)

매일 20:30 evolve 직전 실행. 5개 검증:
1. 거래 성과 (승률, 손익비)
2. 설정 일관성 (default.yml ↔ evolved_overrides.yml)
3. 크로스 검증 통계 (차단율)
4. 포지션 집중도 (섹터)
5. 금요일 메모리 압축 트리거

## 6. 거래 원칙 (trading_principles.py)

### 핵심 불변 원칙 21개
- 리스크(4), 진입(8), 청산(4), 포트폴리오(5)
- 모든 원칙에 `source` 필드 (구현 코드 참조)

### 주간 리포트 (토요일 00:00)
- 메모리 현황 (L1/L2/L3) + LLM 인사이트 + 원칙 리마인더
- 텔레그램 전송
