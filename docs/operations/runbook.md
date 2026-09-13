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

> 서버 안에서의 배포·점검·PR은 프로젝트 스킬로 고정됨 (2026-09-10): `/deploy-local`
> (`scripts/deploy/local_deploy.sh <SHA>` — verify→재시작→헬스체크→실패 시 자동 롤백),
> `/ops-check` (`scripts/dev/ops_check.sh` — 오류·KIS 거절·아침 잡·포트폴리오 요약),
> `/pr-merge` (`gh` CLI, `~/.gh_token`). WSL에서의 정식 경로는 lightsail-deployment.md.

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

## 위험 사이징 canary 원장 추출 (오프라인, 2026-09-14~)

`risk.sizing_mode: risk` 의 첫 30건 판정용 원장·리포트를 만든다. **읽기 전용** — 주문·설정·상태파일을
바꾸지 않는다. 네트워크는 DB 조회만 쓰고 KIS/LLM 은 건드리지 않는다.

```bash
cd /home/ubuntu/projects/qwq-ai-trader
source venv/bin/activate

# 1) 원장 추출 — 판정용은 반드시 --source db (분할 매도 leg 이 trade_events 에만 있다)
venv/bin/python scripts/export_risk_ledger.py --source db --output /tmp/ledger.json --days 90

# 2) canary 리포트 (벤치마크는 선택 — 없으면 초과수익 null, 0 으로 대체하지 않는다)
venv/bin/python scripts/review_risk_canary.py --input /tmp/ledger.json \
    --cohort risk-sepa_trend-v1 --output /tmp/canary.json --min-sample 30
```

- `--source journal` 은 JSON 저널만 읽어 DB 없이도 돌지만, 분할 매도가
  `exits_aggregated`/`lots_ambiguous` 로 표본에서 빠진다 — **점검용으로만** 쓴다.
- 출력 `technical_status` 가 `failed` 면 성과 판정 이전에 계측을 먼저 고친다.
  주요 항목: `stop_pct_mismatch`(계획 SL ≠ 실제 등록 SL), `missing_field`,
  `initial_risk_mismatch`(체결 재계산 ≠ 원장 분모), `net_pnl_mismatch`.
- `excluded.legacy_unmeasured` 는 체결 경로 배선(2026-09-14) 이전 거래다. 정상이며,
  현재 설정으로 초기 위험을 추정해 표본에 넣지 않는다.
- `status` 는 `insufficient_sample` / `hold_expansion` / `further_review` 뿐이다 —
  **nominal 자동 복귀 권고는 출력되지 않는다**(모드 전환은 사람이 판단).
- 확정이 보류된 건: 로그에서 `[위험계측] ... 주문 완결 판정 불가` / `... 확정 생략` /
  `... 주문 종료 — 초기 위험 미확정` 을 확인한다. 이 경우 스냅샷의 `initial_risk_amount` 는 비고,
  원장 분모는 **exporter 가 저널 체결 × 계획 SL 로 재계산**한다(계획 위험만 남는 것이 아니다).

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
- 재시도 조건은 두 갈래로 분리 (2026-09-14 리뷰 F3) — 둘 다 잔고 `stock_value > 0`일 때만:
  - **전체 빈 응답**(봇 보유 ≥1건인데 KIS 포지션 0건): 매도 pending 여부와 **무관하게** 5초 후 1회 재조회.
    재조회도 0건이면 `[동기화] 재시도에도 KIS 포지션 0건 → API 오류로 간주` + `set_sync_status(False)`, 포지션·익절 단계·현금 전부 보존.
    pending 30분 초과·좀비 후보 마킹은 이 방어를 우회하지 못한다 (기존엔 보유 전부가 매도 pending이면 재조회 없이 유령 루프로 가서 31분 pending을 삭제했음).
  - **부분 누락**(봇 보유 중 일부만 응답에서 빠짐, 매도 pending 종목은 정상 누락이라 제외): 5초 후 1회 재시도 → 재시도에도 없으면 유령 정리 (2026-09-13)
  - `stock_value == 0`인 진짜 빈 계좌(수동 전량 매도 등)는 재시도 없이 유령 정리로 진행 (2026-09-03)
- **exit_exempt 종목**은 재시도 포함 **3주기(≈90초) 연속 누락**일 때만 제거 — 로그 `KIS 응답 누락 n/3회 — exit_exempt 종목이라 유령 제거 보류`가 3회 이어지면 실제 부재(수동 매도)로 본 것
- 잔고 조회 실패 시 포지션 조회 없이 종료(원장 TR 절약)
- **ExitManager 등록 재시도 대기열** `_pending_exit_registrations` (fill_check 주기 2/15초): sync 등록 예외·BUY 체결 등록 예외·체결 후 포지션 미생성(1초 대기 초과) 세 경로가 넣고,
  다음 주기부터 재시도한다(같은 주기엔 재시도하지 않음 — 방금 넣은 미생성 포지션을 '삭제됨'으로 즉시 버리던 결함 수정, 2026-09-14).
  성공한 등록만 대기열에서 빠지고, 실패 중에는 유지되며, 포지션이 사라진 종목만 정리. 등록 파라미터는 최초·재시도 모두
  `KRScheduler._resolve_registration_params(strategy)` 한 곳에서 조회(우선순위 `strategy → _sync → {}`, 리뷰 F6 — 재시도가 `_sync` 폴백을 잃고 빈 설정으로 등록하던 결함 수정).
  로그 `ExitManager 등록 실패 → 재시도 대기열` 뒤에 `재시도 등록 성공`이 안 따라오면 해당 종목은 손절 부재 — `exit_manager._states`를 확인한다.

### 매수 미실행 체크리스트
1. 가용 현금 확인 (`get_available_cash()` / `curl -s localhost:8080/api/portfolio` → `cash_ratio`)
   — exit_exempt 수동 풀매수 종목이 있으면 여기서 끝 (2026-07~ 펩트론 사례: 현금 0.4%)
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
- **루프 정체(살아 있지만 일을 못 하는 루프) 탐지 — 하트비트** (2026-09-13~, 2026-09-14 리뷰 F5 후속으로
  성공/실패/유휴 구분): `_supervised`는 죽은 루프만 재기동하므로 HTTP 500 재시도 폭풍(14일 방치)·수확 shadow
  2일 무동작·daily_bias 정체 같은 "조용한 열화"는 못 봤다. 09-13 최초 버전은 **조회 전에 beat 하거나
  예외를 삼킨 뒤 beat 하는 경로**가 있어(DART 공시조회 전 beat, REST 전량 실패에도 beat, 진화 스케줄러
  evolve() 예외 삼킴 후 beat) 40분간 5회 전부 실패해도 정체로 안 잡히는 결함이 있었다(F5) — 이번 버전은
  각 루프가 실제로 **완수(success)**했을 때만 정체 판정 기준을 갱신하고, 실패(failure)는 기준을 갱신하지
  않아 나이가 계속 쌓이며, 할 일이 없는 정상 상황은 유휴(idle, 사유 포함)로 별도 구분한다.
  - 장중 루프 6개(체결확인·동기화·스크리닝·REST시세·시장추세·공시경보)와 일 1회 잡 3개(수확
    shadow·변동성타게팅·진화)가 대상. 각 반복은 `src/utils/loop_heartbeat.record_attempt(name)`으로 시작해
    `record_success(name)`(완수)/`record_failure(name, reason)`(실패)/`record_idle(name, reason)`(할 일 없음,
    예: 장외 세션·보유 종목 0건·WS 전량 커버) 중 하나로 끝난다. `beat(name)`은 `record_success`의 별칭으로 호환 유지.
  - REST 분류: 조회 대상(보유 종목)이 있는데 성공 0건이면 실패, 대상 0건 또는 WS 전량 커버면 유휴.
    DART 분류: 보유 종목 전부 조회 실패면 실패, 일부만 실패면 성공이되 `note`에 degraded 표시(실패 종목 수),
    보유 0종목이면 유휴. 진화 스케줄러: `evolve()`가 waiting/keep/applied/rollback 등 정상 반환이면 성공,
    예외면 실패(예외를 삼킨 직후 무조건 beat 하던 경로 제거).
  - 설정으로 꺼진 기능(`VOL_TARGETING=0`, `DART_API_KEY` 미설정)은 `set_enabled(name, False, reason)`으로
    표시해 정체 경보에서 제외한다. **초기화 실패는 disabled로 위장하지 않는다** — 예를 들어 DART corp_code
    맵 로드 실패는 enabled=True인 채로 beat가 영영 안 찍혀 정체로 드러난다.
  - `kr_heartbeat_monitor`가 60초마다 `check()`로 판정한다: 장중 루프는 거래일 정규장(09:00~15:20)에만
    주기×3(최소 120초, 09:00 기산). 일 1회 잡은 **해당 거래일 예정시각(수확 08:40·변동성 08:30·진화
    20:30, `loop_heartbeat.DAILY_SCHEDULE`이 단일 출처 — 진화는 `config.kr.scheduler.evolution_time`으로
    기동 시 동기화) + 60분(`DAILY_GRACE_MINUTES`) grace** 이후에도 그날 예정시각 이후 성공/유휴가 없으면
    정체다(기존 "직전 거래일 자정 이후" 기준은 예정시각 전에도 오탐하고, 실패를 24시간 넘게 늦게 잡았다).
    즉 일일 잡의 정체 경보 창은 **당일 예정시각+60분 ~ 자정**이다 — 자정이 지나면 `sched`가 다음 거래일
    예정시각으로 다시 계산되어 grace가 재시작되므로, 자정 이후에는(다음 날 예정시각+60분 전까지) 전날
    미완료 건에 대한 정체 경보가 새로 뜨지 않는다(리뷰 advisory (g)).
    주말·공휴일은 점검 자체를 하지 않는다(휴장일 진입 시 즉시 반환). 재시작 시 각 스케줄러가 자신의
    상태 파일(harvest `last_run.json`, vol_targeting `vol_targeting.json`, 진화 `evolution_state.json`)에서
    "오늘 이미 완료"를 읽으면 `record_success(..., note="재시작 전 완료 복원")`으로 즉시 복원한다.
  - 정체 시 `[하트비트] <루프> N초 정체` WARNING + 루프별 시간당 1회 텔레그램, **자동 재기동은 없음**
    (원인 확인이 먼저: 해당 루프의 오류 로그·KIS 거절을 본다).
  - 확인: `curl -s localhost:8080/api/health | jq '.loops, .stale_loops, .loop_status'`
    (`loop_status`는 `enabled`/`idle_reason`/`last_attempt`/`last_success`/`consecutive_failures`/`next_due`
    + 실패 사유(`failure_reason`)·degraded 메모(`note`)) 또는 `ops_check.sh`의 하트비트 3줄(정체/실패 누적/
    degraded·비활성). 주기를 바꾸면 `loop_heartbeat.PERIODS`도 함께 갱신. 재시작 직후 기산점(beat 미기록 루프)은
    프로세스 시작 시각.
- **pykrx 간헐적 실패**: `Stock master: pykrx failed` → FDR → 72h 캐시 폴백 자동 전환
- **FDR `StockListing("KRX")` 404** (2026-09-09~, 업스트림 GitHub 캐시 소실, 0.9.202도 동일):
  수확 shadow 유니버스는 `harvest_shadow/universe.json` 캐시로 폴백(부트스트랩은 DB
  `kr_stock_master` 시총 1,000~50,000억). `[수확shadow] 유니버스 조회 실패 → 캐시 … 사용` 경고가
  정상 경로. data_collector/stock_master의 FDR 폴백도 같은 영향 — DB 경로가 살아 있어 무해
- **KIS HTTP 500 반복** (`[API] HTTP 500 <tr_id> EGW00201 …`): 원장 TR 초당 1건 초과.
  야간에도 30초 주기로 반복되면 같은 원장 TR 연속 호출 코드가 원인 — 경고의 `tr_id`로
  호출 주체를 추적한다 (2026-09-03 `_rate_limit` 원장 간격으로 해결)
- **원장 초과(EGW00215)는 장중 내내 발생** (2026-09-10 정정 — 9/8의 "개장 직후만" 결론은 저녁·주말만 확인한
  오판): 9/3~9/10 매일 09:00~15:30 시간당 70~140건, 장외 0건, **전부 잔고조회 `TTTC8434R`**(체결 8001R·
  매수가능 8908R은 거의 거절 없음). 동기화가 30초마다 8434R을 잔고→포지션 두 번 호출하고 두 번째가 ~50%
  거절되는 구조라 inquire-balance만 ~2초 간격을 요구한다는 가설 → `kis_rate_limit.LEDGER_TR_INTERVALS`
  로 8434R 2.1초(9/10 배포) → 9/11 09~12시 236→171건(−28%, 09시는 불변)으로 부분 확인. 이어서 잔고 응답
  `output1` 5초 스냅샷을 `get_positions`가 재사용해 동기화 사이클당 8434R **2→1회**(9/11). 영향은 재시도
  1~2초·간헐 동기화 스킵. `ops_check.sh`로 시간대 분포를 볼 때 **장중 구간**을 포함할 것.
- **EGW00133 연속 (토큰 재발급 1분 제한)**: 다른 컴포넌트가 토큰을 회전한 직후 브로커가
  EGW00123을 받고 무효화하던 락아웃 — 2026-09-03 회전 토큰 채택으로 해결. 재발 시
  `journalctl | grep -c EGW00133`로 빈도 확인, `[토큰] 매니저 토큰이 이미 회전됨` 로그가 정상 경로
- **주문 POST 실패 후 포지션 불일치**: 주문 접수/정정은 재전송하지 않으므로(중복 주문 방지)
  응답 유실 시 봇은 실패로 보고 KIS에는 체결이 있을 수 있다 → 30초 동기화의 `sync_detected`가
  정합하며, 그 전까지 대시보드 포지션이 KIS와 잠시 다를 수 있음 (정상)
- **MCP 모듈 없음**: `No module named 'mcp'` → 기능 영향 없음 (폴백 동작). `mcp`만 설치해도 소용없음 —
  `pykrx-mcp` 바이너리와 `npx naver-search-mcp`(부팅마다 npm 다운로드, 30초 타임아웃)가 필요하고 소비처
  (stock_validator 수급/버즈 보정)는 KIS 수급 데이터와 중복·미검증이라 **의도적으로 방치** (2026-09-10 평가)
- **운영 서버 RAM(3.8GB)은 Claude 플러그인 데몬과 공유됨**: 2026-09-10 `claude-mem` bun 워커가 RSS 1.3GB로
  스왑 2GB를 전부 채워 가용 118MB까지 떨어졌음(봇 460MB의 3배) → 플러그인 비활성화·데몬 종료로 가용 1.5GB 회복.
  상주 데몬을 두는 플러그인(메모리 DB, 브라우저 등)은 이 서버에 설치하지 말 것. 점검: `free -m`, `ps --sort=-rss`
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
