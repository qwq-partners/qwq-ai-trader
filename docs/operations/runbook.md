# 운영 매뉴얼 (Runbook)

> 최종 갱신: 2026-04-15

## 봇 관리

```bash
# 재시작
echo 'user123!' | sudo -S -k systemctl restart qwq-ai-trader

# 중지
echo 'user123!' | sudo -S -k systemctl stop qwq-ai-trader

# 상태
systemctl is-active qwq-ai-trader

# 실시간 로그
journalctl -u qwq-ai-trader -f

# 최근 로그
journalctl -u qwq-ai-trader -n 50 --no-pager
```

## 코드 변경 프로토콜

1. `python3 -m py_compile <수정파일>` — 문법 검증
2. 봇 재시작 (위 명령)
3. `systemctl is-active qwq-ai-trader` — 상태 확인
4. `journalctl -u qwq-ai-trader -n 20 --no-pager` — 에러 확인

**절대 금지**: `nohup python scripts/run_trader.py` 직접 실행 (systemd 충돌)

## 긴급 전량 매도

```bash
source venv/bin/activate
python scripts/liquidate_all.py --market kr    # KR
python scripts/liquidate_all.py --market us    # US
python scripts/liquidate_all.py --force        # 확인 없이
```

## 로그 파일 위치

| 경로 | 내용 |
|------|------|
| `logs/YYYYMMDD/trader_*.log` | 메인 트레이더 로그 |
| `logs/YYYYMMDD/error_*.log` | 에러 전용 |
| `logs/YYYYMMDD/screening_*.log` | 스크리닝 상세 |
| `logs/YYYYMMDD/trades_*.log` | 거래 이벤트 |

## 캐시 파일 위치

| 경로 | 내용 |
|------|------|
| `~/.cache/ai_trader/wiki/` | Trade Wiki (교훈 축적) |
| `~/.cache/ai_trader/trade_memory/` | L1/L2/L3 거래 메모리 |
| `~/.cache/ai_trader/evolution/` | 진화 상태 |
| `~/.cache/ai_trader/journal/` | 거래 저널 + LLM 리뷰 |
| `~/.cache/ai_trader/unified_trader.pid` | PID 파일 |
| `~/.cache/ai_trader/kis_token_prod.json` | KIS 토큰 캐시 |

## 설정 파일

| 경로 | 역할 | 주의 |
|------|------|------|
| `config/default.yml` | 기본 설정 | evolved_overrides가 덮어쓸 수 있음 |
| `config/evolved_overrides.yml` | 진화 오버라이드 | **양쪽 모두 확인 필요** |
| `.env` | API 키 | 커밋 금지 |

## 주간 자동화 (토요일)

| 시각 (KST) | 작업 | 위치 |
|-----------|------|------|
| Sat 00:00 | 전략 예산 리밸런싱 (StrategyEvolver) | `kr_weekly_rebalance` |
| Sat 00:00 | False Negative 분석 + Wiki Lint | (리밸런싱 후 연속 실행) |
| Sat 00:05 | 주간 거래 원칙 리포트 (TradingPrinciplesManager) | `kr_log_cleanup` 내 |
| **Sat 09:00** | **매도 후속 복기 (PostExitReviewer)** | `kr_post_exit_review` |

### 주간 매도 후속 복기 (2026-04-28~)

- **목적**: 최근 30일 매도 거래의 "매도 후 추세"를 추적해 전략 진화에 반영.
- **실행**: 매주 토요일 09:00 KST, ISO week 기반 중복 방지 (`~/.cache/ai_trader/last_post_exit_review.json`).
- **분류**: 매도 후 +3% 이상=놓침, -3% 이하=회피, 그 사이=타당.
- **LLM**: GPT-5.4 (STRATEGY_ANALYSIS, fallback Gemini Pro). 표본 < 5건이면 호출 스킵.
- **출력**:
  - JSON 리포트: `~/.cache/ai_trader/journal/post_exit_review_YYYYMMDD.json`
  - Wiki 페이지: `~/.cache/ai_trader/wiki/weekly_post_exit_YYYY-WNN.md` → 다음 weekly rebalance 시 LLM 컨텍스트로 자동 흡수
  - 텔레그램: Top 5 놓침/회피 + 전략별 평균 + LLM 인사이트
- **수동 실행**: `python -c "..."` 형태로는 broker 인스턴스 충돌 위험 있음 — 봇 외부에서는 mock broker 사용 권장.

## DB 좀비 포지션 진단/정리

### 증상
점수 90+ 매수 시그널이 "전략 예산 소진"으로 차단. 한도 산정에 의문.

### 진단 (2026-04-28 사고 기준)
```bash
# 1. 봇 인식 vs DB 보유 비교
PGPASSWORD=$DB_PW psql -U postgres -h localhost -d ai_db -c "
  SELECT symbol, name, entry_strategy,
         entry_quantity * entry_price AS cost
  FROM trades WHERE market='KR' AND exit_time IS NULL
  ORDER BY entry_strategy, cost DESC;"

# 2. 실제 KIS 보유 확인
curl -s http://localhost:8080/api/positions | python3 -m json.tool

# 3. 동일 종목이 DB OPEN인데 KIS에는 없으면 → 좀비
```

### 정리 SQL (반드시 `sync_reconcile` 사용)
```sql
UPDATE trades
SET exit_time='YYYY-MM-DD HH:MM:SS',  -- 실제 청산 추정 시각
    exit_quantity=entry_quantity,
    exit_price=entry_price,           -- pnl 0으로 강제 (회계 왜곡 인정)
    pnl=0, pnl_pct=0,
    exit_type='sync_reconcile',       -- ⚠️ 'cleanup' 금지! is_sync 필터 미인식
    exit_reason='좀비 정리 (사유 명기)'
WHERE symbol=? AND exit_time IS NULL;
```

**중요**: `exit_type='cleanup'`은 `trade_journal._sync_exit_types` 에 등록되지 않아 진화/리뷰 평가에서 패배로 잘못 집계됨. 반드시 `sync_reconcile` 사용.

### 사후 조치
1. DB 백업 확보: `pg_dump -t trades -t trade_events ai_db | gzip > ~/backups/...`
2. 봇 재시작 → 메타 복원 검증 (보유 종목 수 일치 확인)
3. `evolved_overrides.yml`의 strategy_allocation 한도 영향 재계산

## 트러블슈팅

### 봇 미응답
```bash
systemctl status qwq-ai-trader
journalctl -u qwq-ai-trader -n 50 --no-pager
```

### 싱글톤 락 충돌
```bash
echo 'user123!' | sudo -S -k systemctl stop qwq-ai-trader
rm -f ~/.cache/ai_trader/*.lock ~/.cache/ai_trader/*.pid
echo 'user123!' | sudo -S -k systemctl start qwq-ai-trader
```

### 매수 미실행 체크리스트
1. 가용 현금 확인 (`get_available_cash()`)
2. 일일 손실 한도 (-5% KR, -3% US)
3. 포지션 수 한도 (8 KR, 10 US)
4. 일일 거래 횟수 (10회 KR)
5. ATR=0 차단 여부 (로그에서 `ATR 누락/0 차단` 검색)
6. 크로스검증 차단 (`[크로스검증] 차단` 검색)
7. LLM 거부 (`LLM 이중검증 거부` 검색)

### RLAY 유형 매도 반복 실패
- `[US 매도 주문] {symbol} 수량 보정` 로그 확인
- 3회 연속 실패 시 자동 동기화
- 지속 시: 포트폴리오 수동 확인 → ExitManager stage 리셋

### 알려진 이슈
- **pykrx 간헐적 실패**: `Stock master: pykrx failed` → DB 폴백 자동 전환
- **MCP 모듈 없음**: `No module named 'mcp'` → 기능 영향 없음 (폴백 동작)
- **Yahoo Finance 지연**: KOSPI 데이터 2~3일 지연 → KIS 실시간 보충

### 거래 로그 누락 감지 (대시보드 vs KIS API 대조)

대시보드의 `/trades` 거래 이벤트 개수가 실제보다 적다면 다음 스크립트로 KIS API 체결내역과 대조:

```bash
source venv/bin/activate && python3 << 'EOF'
import asyncio
from datetime import date
from src.utils.config import load_dotenv
from src.utils.token_manager import get_token_manager
from src.execution.broker.kis_kr import KISBroker, KISConfig

async def main():
    load_dotenv()
    broker = KISBroker(KISConfig.from_env(), get_token_manager())
    await broker.connect()
    fills = await broker.get_all_fills_for_date(date.today())
    print(f"KIS API 오늘 KR 체결: {len(fills)}건")
    for f in fills:
        side = '매수' if f['sll_buy_dvsn_cd'] == '02' else '매도'
        print(f"  {f['ord_tmd']:<8} {side} {f['symbol']} {f['name']:<14} {f['tot_ccld_qty']}주 @ {f['avg_prvs']:,.0f}")
    await broker.disconnect()
asyncio.run(main())
EOF
```

DB 측 카운트:
```bash
PGPASSWORD=... psql -U postgres -d ai_db -c \
  "SELECT symbol, event_type, SUM(quantity) qty, COUNT(*) cnt FROM trade_events \
   WHERE event_time::date=CURRENT_DATE AND symbol ~ '^[0-9]{6}\$' \
   GROUP BY symbol, event_type ORDER BY symbol;"
```

**불일치 원인 체크리스트**:
1. `pos.trade_id` 복원 누락 (`_restore_position_metadata` 로그에서 `trade_id=N개` 확인)
2. `TradeStorage.record_entry()` TypeError (`BUY journal 기록 실패` 로그 grep)
3. `DB 직접 기록 실패: 오픈 포지션 없음` (부분매도 로직 문제)
4. `sync_from_kis`에서 `매도 복구 대상 trade 없음` (cross-day partial 쿼리 누락)
