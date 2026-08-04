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

**정상 재시작 소요: 15초 내외** (2026-08-03 기준).
`journalctl`에 `State 'stop-sigterm' timed out. Killing.`이 보이면 종료 경로가 깨진 것이다.
2026-08-03 이전에는 모든 재시작이 90초 SIGKILL로 끝났다 — `stop()`이 태스크를 취소하지
않아 스케줄러 `sleep(5~10분)`과 대시보드 SSE 루프를 끝까지 기다렸기 때문.
종료 로그에서 `[종료] 신호 수신 → 실행 중 태스크 즉시 취소`가 찍히는지 확인할 것.

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
| `~/.cache/ai_trader/office_status.json` | 가상 오피스 외부 푸시 상태 (5분 TTL) |

## 신규 전략 1차 스크리닝 (quick_backtest, 2026-08-03~)

정식 백테스터에 올리기 전에 아이디어를 빠르게 기각/채택하는 연구 도구.
**운영 venv가 아니라 연구 venv로 실행** (vectorbt/numba의 numpy 충돌 방지 —
운영 venv에 vectorbt를 설치하지 말 것):

```bash
./venv-research/bin/python scripts/quick_backtest.py --idea tom --symbol SPY --months 120
./venv-research/bin/python scripts/quick_backtest.py --idea lowvol
./venv-research/bin/python scripts/quick_backtest.py --idea earnings_reversal --months 24
```

- 깔때기: 아이디어 → quick_backtest → 통과 시 backtest_strategies.py 정식 구현 → BacktestGate
- `venv-research/`는 gitignore 대상 (재생성: `python3 -m venv venv-research &&
  ./venv-research/bin/pip install vectorbt pykrx finance-datareader yfinance`)
- lowvol 아이디어는 정식 백테스터의 OHLCV 캐시를 재활용하므로 캐시가 없으면
  `backtest_strategies.py`를 먼저 1회 실행

## 퀀트 성과 리포트 (quantstats tear sheet, 2026-08-03~)

`/performance` 페이지의 **📊 퀀트 리포트** 버튼 → `/api/performance/quantstats`.
Sharpe/Sortino/Calmar, 월별 히트맵, KOSPI 대비 알파·베타를 담은 HTML tear sheet.

```bash
# 강제 재생성 (기본 6시간 캐시)
curl "http://localhost:8080/api/performance/quantstats?refresh=1" -o /dev/null

# 리포트 존재/신선도 확인
curl http://localhost:8080/api/performance/quantstats/status
```

- 원천: `~/.cache/ai_trader/journal/equity_*.json`의 `daily_pnl_pct`
  (자산 곡선 차분이 아니라서 입출금·외부계좌 편입에 왜곡되지 않음)
- 산출물: `~/.cache/ai_trader/reports/quantstats_kr.html` (원자적 교체)
- 벤치마크: FDR KOSPI(KS11) 1차 → pykrx 폴백 → 실패 시 벤치마크 없이 생성
- 표본 20거래일 미만이면 400 응답 (통계 무의미)
- 구현: `src/analytics/quantstats_report.py`

## 가상 오피스 (`/office`, 2026-08-03~)

엔진 상태를 8명 캐릭터로 시각화. 대시보드 `/office` 또는 모바일 하단 nav "오피스".

```bash
# 상태 확인 (엔진 파생 + 외부 푸시 병합 결과)
curl -s localhost:8080/api/office/status | python3 -m json.tool

# 외부 도구에서 상태 밀어넣기 (5분 TTL, 이후 엔진 상태로 자동 복귀)
curl -X POST localhost:8080/api/office/status \
  -H 'Content-Type: application/json' -d '{"dev":"working","workflow":"수동 점검"}'

# 화면이 안 뜰 때: 정적 번들 확인 → 없으면 재빌드
ls src/dashboard/static/office/assets/ || bash tools/office/build.sh
```

> 역할 매핑·API 계약·재빌드 절차: `docs/operations/virtual-office.md`

## 설정 파일

| 경로 | 역할 | 주의 |
|------|------|------|
| `config/default.yml` | 기본 설정 | evolved_overrides가 덮어쓸 수 있음 |
| `config/evolved_overrides.yml` | 진화 오버라이드 | **양쪽 모두 확인 필요** |
| `.env` | API 키 | 커밋 금지 |

## 킬스위치 (긴급 주문 차단, 2026-08-02~)

파일 하나로 주문을 즉시 막는다. **봇 재시작이 필요 없고**, 엔진이 오작동 중이어도 동작한다
(모든 주문이 통과하는 브로커 계층에서 검사).

```bash
# 신규 매수만 차단 (청산은 계속 허용) — 기본 대응
touch ~/.cache/ai_trader/KILL_SWITCH

# 사유를 적어두면 로그/감사원장에 함께 남는다
echo "급락장 수동 개입" > ~/.cache/ai_trader/KILL_SWITCH

# 매수·매도 전면 동결
touch ~/.cache/ai_trader/KILL_SWITCH_ALL

# 시장별 개별 차단
touch ~/.cache/ai_trader/KILL_SWITCH_KR
touch ~/.cache/ai_trader/KILL_SWITCH_US

# 해제
rm ~/.cache/ai_trader/KILL_SWITCH
```

> ⚠️ `KILL_SWITCH_ALL`은 **손절·트레일링까지 막는다.** 하락 노출이 무한정 열리므로,
> 포지션을 정리한 뒤 동결하거나 매수만 막는 `KILL_SWITCH`를 쓸 것.
> 반영까지 최대 2초(TTL 캐시).

### 감사 원장

`trade_journal`이 "체결된 거래"를 남긴다면, 감사 원장은 **시도된 모든 주문**을 남긴다
(제출·접수·거부·차단). append-only.

```bash
# 이번 달 기록
cat ~/.cache/ai_trader/audit/audit_$(date +%Y%m).jsonl

# 차단된 주문만
grep '"blocked"' ~/.cache/ai_trader/audit/audit_$(date +%Y%m).jsonl
```

## 스토리지 / DB 유지보수 (2026-08-02~)

### 봇이 실제 사용하는 DB 테이블 (`ai_db`, 전부 `public` 스키마)

`trades`, `trade_events`, `kr_stock_master`, `news_articles`, `theme_history`, `theme_stocks`, `signal_events`

> 이 7개 외 테이블이 보이면 레거시다. 2026-08-02에 구 프로젝트 스키마(`ai`/`market`/`marts`/`ref`/`sim`)와
> `public` 레거시 테이블(`krx_minute`, `ats_trades` 등)을 제거해 1.34GB → 318MB로 축소했다.

### 자동화된 유지보수 (pg_cron, `postgres` DB의 `cron.job`)

| jobid | 스케줄 | 내용 |
|-------|--------|------|
| 4 | 매일 02:00 | `news_articles`/`theme_history`/`kr_stock_master` ANALYZE |
| 8 | 매일 02:30 | **retention-180d** — 180일 초과 뉴스/테마 자동 삭제 |
| 7 | 일요일 03:00 | `trades`/`trade_events`/`signal_events`/`theme_stocks` ANALYZE |
| 6 | 일요일 04:00 | `pg_stat_statements_reset()` |

```bash
# 잡 확인
sudo -u postgres psql -d postgres -c "SELECT jobid,jobname,schedule,active,database FROM cron.job"

# 잡 등록은 반드시 schedule_in_database (cron 확장은 postgres DB에만 설치됨)
sudo -u postgres psql -d postgres -c "SELECT cron.schedule_in_database('name','0 2 * * *','SQL','ai_db')"
```

> ⚠️ 테이블을 DROP하면 pg_cron 잡의 ANALYZE 대상도 함께 정리할 것. 방치 시 매일 잡이 실패한다.

### 용량 점검 명령

```bash
# DB 전체/테이블별
sudo -u postgres psql -d ai_db -tc "SELECT pg_size_pretty(pg_database_size('ai_db'))"
sudo -u postgres psql -d ai_db -c "SELECT schemaname||'.'||relname, n_live_tup, \
  pg_size_pretty(pg_total_relation_size(relid)) FROM pg_stat_user_tables \
  ORDER BY pg_total_relation_size(relid) DESC"

# journald (상한 500M / 30일, drop-in: /etc/systemd/journald.conf.d/99-qwq-limit.conf)
journalctl --disk-usage
sudo journalctl --vacuum-size=500M
```

> 레거시 판별 기준: `pg_stat_user_tables`의 `idx_scan = 0` + 최신 데이터 시점이 수개월 전 →
> 코드에서 `grep -rn "<테이블명>" src/ scripts/`로 미참조 확인 후 DROP.

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

### WebSocket 중복 프로세스
- "ALREADY IN USE appkey" → `pkill -9 -f "run_trader.py"` 후 단일 재시작

### 포트폴리오 동기화 이슈 (유령 포지션)
- KIS API 응답 지연(수 분) → 유령 포지션 발생 가능
- 청산 실패 시 `broker.get_positions()`로 실제 보유 확인 후 정리
- 동기화 주기: KR 30초, US 30초

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


## 대기 시그널이 실행되지 않을 때 (배치)

`pending_signals.json`은 `execute_pending_signals()` 실행 후 **스킵 사유별로 선별 유지**된다
(2026-08-03~). 파일이 비어 있다고 곧바로 이상은 아니다.

```bash
# 남아 있는 대기 시그널과 이월 횟수 확인
python3 -c "import json,pathlib; \
  d=json.loads(pathlib.Path.home().joinpath('.cache/ai_trader/pending_signals.json').read_text()); \
  [print(s['symbol'], s['strategy'], 'retry=', s.get('retry_count',0), s.get('entry_mode')) for s in d]"

# 이월 사유 집계
journalctl -u qwq-ai-trader --since today | grep "다음 윈도우 이월"
```

- `retry_count`가 8(`MAX_CARRY_RETRIES`)에 닿으면 폐기되며 `이월 상한 도달` 경고가 남는다.
- 갭다운·이미 보유·만료·SEPA 14:30+ 로 스킵된 건은 **의도적으로 이월하지 않는다**.
  분류 근거는 `docs/strategies/kr-strategies.md`의 이월 정책 표 참조.
- 13:50 자본활용률 체크는 `현금 비중 > 25%`일 때만 추가 진입을 시도한다.
  현금이 적으면 이월분이 있어도 실행되지 않는다.
