# 실거래 KODEX200 초과수익 원장 — 구현 계획 (설계 A + 절충안 1단계)

> **상태 (2026-09-29):** 계획. 사용자 지시(원문): "계획서 나오면 리뷰까지 받은 후 설계 보완하고 이후 설계안대로 구현까지 가자".
> 범위는 **구현·시험·브랜치 커밋/푸시·PR 갱신**까지다. 배포·재시작·main 병합·설정/`.env`/킬스위치 변경은 하지 않는다(별도 지시).
> 기준: 브랜치 `claude/kodex200-excess-return-design-64d126`(PR #98) `0c8b32b`, 제품 코드 = main `e31c632`.
> 설계: `docs/superpowers/specs/2026-09-28-kodex200-excess-return-ledger-design.md`(비작성자 리뷰 APPROVE).
> 방향: 09-29 사용자 결정 절충안(설계 B §0) — 이 계획의 2단계가 절충안 순서 (1) 의 "sync_from_kis 주문번호 단위화·거래일 기록 미완 표시"다.
> 코드 사실은 읽기 전용 조사 3건(요청 Opus/high)이 뽑았고, 핵심 지점은 coordinator 가 다시 확인한다(구현 착수 전 각 줄 재확인 규칙).

## 0. 설계 보완 목록 (계획 리뷰 뒤 설계 문서에 반영)

조사로 드러난 사실 때문에 설계 A 의 문장을 고쳐야 하는 곳이다. 동작 원칙은 바꾸지 않는다.

| # | 설계 A 위치 | 보완 |
| --- | --- | --- |
| S1 | §5 `sync_entry` | KR 동기화 진입은 거래 id 접두 `KIS_SYNC_`(`trade_storage.py:995`, `entry_reason="KIS 동기화 복구"`)로만 식별된다. `entry_reason=="sync_detected"`·`SYNC_` 접두는 US 경로다(`us_scheduler.py:1921·2024`). 규칙은 셋 다 보되 KR 실효는 `KIS_SYNC_` 임을 적는다 |
| S2 | §5 동기화 청산 | `sync_detected` 는 KR 에서 **청산 유형**이다(`trade_journal.py:512`) — 설계대로 청산 쪽 목록에 둔다 |
| S3 | §5 `exits_missing` | exporter SELECT 가 `pnl`·`exit_price` NULL 을 0 으로 읽으므로(`export_risk_ledger.py:359·362`) DB 원천에서는 "net_pnl 없음"이 사실상 "SELL leg·fill 없음"과 같다 |
| S4 | §5 분류 입력 | exporter position dict 에는 `entry_reason`·`exit_type` 키가 없다 → build_ledger 가 `getattr` 로 두 키를 더한다(canary 는 필수 키만 검사해 영향 없음) |
| S5 | §2·§8 `is_sync` | 공용 함수로 바꾸는 호출처는 `data_collector.py:622-632` 하나다. `TradeRecord.is_sync`(`trade_journal.py:69-81`)는 05-27 에 일부러 청산 유형만 보게 했고 진화·복기 표본이 이것을 쓴다 → **바꾸지 않는다** |
| S6 | §4 DB 접근 | KR 봇의 DB 는 `bot.trade_journal`(TradeStorage) 의 `pool` 이다(`run_trader.py:713-715`). `pool` 이 있어도 `_db_available=False` 일 수 있다(`trade_storage.py:121-134`) → 세 조건 모두 확인 |
| S7 | §4 실행 창 | `last_review_date` 가 evolve **앞에서** 영속되므로(`kr_scheduler.py:5680-5694`) 20:30 블록 도중 재시작하면 그날 단계는 다시 돌지 않는다 → 다음 거래일 전체 재계산으로 복구된다고 적는다 |
| S8 | §4① 캐시 | `get_daily_prices` 는 페이지 실패 시 부분 행을 반환하고(`kis_kr.py:1888-1891`) 결과를 `unique[-days:]` 로 자른다(`:1938-1948`) — 이미 반영한 "첫 날짜 ≤ 가장 오래된 진입일일 때만 교체"가 이것을 막는다 |
| S9 | §6 보고 | 텔레그램 기본 `parse_mode="HTML"`(`telegram.py:47·164`) → 한 줄은 `html.escape` 한다 |
| S10 | §5·§7·§11 기록 미완 | 절충안 채택으로 `execution_day_status` 의 원천이 설계 B 가 아니라 **2단계의 `sync_from_kis`** 가 된다. 제외 사유 `record_incomplete` 를 §5 표에 넣고, 상태 행이 없는 날은 제외하지 않고 요약에 `day_status_missing` 로 센다(§7) |

## 1. 1단계 — 설계 A 본체

### T1. 공용 모듈 `src/analytics/excess_return.py` (신규)

- **import:** 표준 라이브러리 + loguru + `src.analytics.gate_performance` 의 `STOP_CLIP_PCT`·`BENCH_SYMBOL`(모듈 import 부작용 없음 — 조사 확인, `gate_performance.py:28-39·62-65`) + `src.utils.atomic_io.atomic_write_text`.
  **경로는 모두 인자로 받는다**(모듈 상수 금지 — conftest 격리가 import 시점 `~/.cache/ai_trader` 접근을 막고, 기존 시험 `test_loop_heartbeat_integration.py:273-331` 은 `Path.home` 만 패치한다).
- **함수(공개 이름):**
  - `to_decimal(v)`, `parse_date(ts)`, `buy_fills(pos)` — canary 의 `_dec`·`_date`·`_buys` 와 같은 동작(이동).
  - `load_benchmark(path)`, `position_benchmark(bench, pos)` — canary 에서 **문자 그대로 이동**(사유 문자열 `benchmark_missing`·`benchmark_date_missing: …`·`exits_missing` 유지 — `test_risk_canary_report.py:240` 이 검사). 행의 `bench_missing_reason` 에는 이 사유를, 제외 사유 `exclusion` 에는 §5 이름을 쓴다(둘을 섞지 않는다).
  - `kis_rows_to_bench(rows)` — `{"date":"YYYYMMDD","close":float}` → `[(YYYY-MM-DD, Decimal)]`, 8자리·종가>0 검사(`counterfactual_tracker.py:312-320` 선례).
  - `is_sync_entry(trade_id, entry_reason)` = `trade_id` 가 `KIS_SYNC_`/`SYNC_` 로 시작 또는 `entry_reason=="sync_detected"`;
    `is_sync_exit(exit_type)` = `exit_type in SYNC_EXIT_TYPES`(`kis_sync`·`sync_reconcile`·`sync_closed`·`sync_partial`·`sync_detected`).
  - `classify(pos, bench, day_status)` — §5 판정 순서(① `closed` 인데 exits 없음/`net_pnl` 없음 → `exits_missing` ② Σ매도 < Σ매수 → `awaiting_close` ③ Σ매도 > Σ매수 → `quantity_mismatch` ④ `lots_ambiguous`·`exits_aggregated`·`manual_entry`·`sync_entry`·`recovered_at_exit`·`bench_out_of_range`·`record_incomplete`).
  - `position_row(pos, bench, day_status, *, computed_at, code_sha)` — §5 schema 1 행(Decimal 문자열). `clip_pct` = `actual_stop_pct` → `stop_pct` → `-STOP_CLIP_PCT`, `stop_overshoot` 은 `entry_stop` 에서만.
  - `summarize(rows, previous_rows, *, today, awaiting_close, day_status_missing)` — §7 지표 × 창(전체·최근 90일) × 묶음(전체·전략·월·`exit_quality`·cohort). `status` 는 `insufficient_sample`/`measured` 둘뿐.
  - `format_weekly_line(summary)` — §7 한 줄(계산일 포함). 호출부가 `html.escape`.
  - `load_exporter(root)` — `backtest_gate.py:183-206` 과 같은 방식(`spec_from_file_location` → `sys.modules` 등록 → `exec_module`, 모듈 전역 캐시).
  - `async run_daily_update(*, broker, fetch, out_dir, root, now, code_sha)` — §4 ①~④. ① `oldest_entry` 를 알기 위해 ② 를 먼저 하고(순서만 바꾼다, 결과 동일), 거래일 수 N+5 로 `get_daily_prices("069500", days=min(N+5, 500))`, 첫 날짜 ≤ 가장 오래된 진입일일 때만 CSV 원자 교체 ③ 스냅샷 원자 교체 ④ `summary.json` 원자 교체 + `summary_history.jsonl` 같은 날짜 줄이 없을 때만 append(fsync, `position_ledger.py:203` 패턴). drift 는 **직전 계산일** 스냅샷(`positions_prev.jsonl` — 날짜가 다를 때만 교체 전 사본으로 보관)과 비교.
- **시험(먼저 RED):** `tests/test_excess_return_ledger.py` — 설계 §9 1~8 + S10 의 `record_incomplete`/`day_status_missing`. 가짜 브로커는 `SimpleNamespace(get_daily_prices=AsyncMock(...))`(`test_codex_cf_preservation.py:62`), 가짜 fetch 는 SQL 문자열로 분기하는 async 함수, 경로는 `tmp_path`. async 는 `asyncio.run()`(저장소 관례).

### T2. canary 가 공용 함수를 쓴다 — `scripts/review_risk_canary.py`

- import 앞에 exporter 와 같은 ROOT `sys.path` 삽입(`export_risk_ledger.py:49-51`) — 없으면 `python scripts/review_risk_canary.py` 단독 실행과 `test_risk_canary_report.py` 단독 실행이 `src` 를 못 찾는다.
- `load_benchmark`·`position_benchmark` 를 import, `_dec = to_decimal`, `_date = parse_date`, `_buys = buy_fills` 로 별칭(다른 함수가 계속 쓴다). `_strip` 은 남은 호출처가 없으면 지운다.
- **무변경 기준:** `tests/test_risk_canary_report.py`·`tests/test_entry_risk_lifecycle.py`·`tests/test_entry_risk_wiring.py` 가 수정 없이 통과.

### T3. exporter — `scripts/export_risk_ledger.py`

- `fetch_trade_records(fetch, days) -> List[TradeRecord]` 로 `:334-384`(SELECT 두 개·레코드 조립·`sell_legs` 부착)를 옮긴다. `_load_from_db` 는 `connect` → `pool is None` 폴백 → `return await fetch_trade_records(storage.pool.fetch, days)` → `finally disconnect` 만 남긴다. **새 함수는 connect/disconnect 를 하지 않는다**(봇 pool 보호).
- `_fills_and_exits`: `exit_quantity == 0` 조기 반환은 그대로 두고, `exit_price is None or <= 0` 검사를 leg 가 없을 때의 폴백 분기(`if len(exits) == 0:`) 맨 앞으로 옮긴다.
- `build_ledger` 가 position 에 `entry_reason = getattr(trade, "entry_reason", "")`, `exit_type = getattr(trade, "exit_type", "")` 를 더한다(시험의 `SimpleNamespace` 거래에는 두 속성이 없다 — `test_entry_risk_wiring.py:352-357`).
- **시험:** `exit_price=0`·SELL leg 있음 → leg 로 exits 생성(새 시험). 기존 세 시험 파일 무수정 통과. 알려진 운영 영향(시험 밖): leg 가 새로 생긴 risk cohort 포지션은 canary `net_pnl_mismatch`(평균단가 기반 DB pnl 과의 1원 초과 차이)가 새로 보일 수 있다 — 기술 검증 신호일 뿐 성과 판정은 바뀌지 않는다(CHANGELOG 에 적는다).

### T4. 대시보드 `is_sync` — `src/dashboard/data_collector.py:622-632`

- `ev["is_sync"] = is_sync_entry(trade_id, entry_reason) or is_sync_exit(_etype_ev)`. 새로 `sync_detected` 청산이 동기화로 표시된다(표시 전용 — `ev["is_sync"]` 를 읽는 프론트 코드는 없음, 조사). `TradeRecord.is_sync` 는 건드리지 않는다(S5).

### T5. 스케줄러 배선 — `src/schedulers/kr_scheduler.py`

- **20:30:** `run_evolution_scheduler` 의 `_hb.record_success("kr_evolution_scheduler", …)`(`:5728`) 뒤, `await asyncio.sleep(60)`(`:5730`) 앞, `if last_review_date != today:` 안쪽에 `await self._run_excess_return_step(bot, now)` 한 줄.
- `KRScheduler._run_excess_return_step(bot, now)`: `tj = getattr(bot, "trade_journal", None)`; `pool`·`_db_available`·`getattr(bot, "broker", None)` 셋 중 하나라도 없으면 **디스크를 건드리기 전에** return(기존 시험의 가짜 봇에는 broker·trade_journal 이 없다 — `test_loop_heartbeat_integration.py:302-309`).
  `out_dir = Path.home()/".cache"/"ai_trader"/"excess_return"` 는 호출 시점에 계산. `await asyncio.wait_for(run_daily_update(...), 60)` 를 `try/except Exception`(`TimeoutError` 포함)으로 감싸 `logger.warning("[초과수익] …")` 만. 바깥 블록의 `raise` 규칙(`:5738-5742`)에 닿지 않게 한다.
- **토요일:** `run_post_exit_review_scheduler` 의 게이트 성능 분석 `except`(`:6511-6512`) 뒤에 자체 try: `summary.json` 을 읽어 `format_weekly_line` → `send_alert(html.escape(line))`. 파일이 없으면 보내지 않는다(로그만). 같은 try 안의 `run_weekly` 가 실패하면 이 줄도 나가지 않는다 — 기존 구조의 한계로 기록.
- **시험:** 소스 구조 시험(주석 앵커 순서 — `record_success("kr_evolution_scheduler"` < `_run_excess_return_step` < 다음 `asyncio.sleep(60)`; `test_intraday_rs_kospi_change.py:25-47` 방식) + `_run_excess_return_step` 실행 시험 3개(① 속성 없는 봇 → 파일 0·예외 0 ② 가짜 pool·브로커·`Path.home=tmp_path` → 파일 4종 생성 ③ 브로커 예외·시간 초과 → 예외 전파 0). 기존 `test_evolution_scheduler_*` 무수정 통과·격리 위반 0.

### T6. 1단계 문서

`docs/risk/risk-and-exit.md`(판정 기준 절에 실거래 원장), `docs/operations/monitoring-checkpoints.md`(배포 후 첫 실행 확인 항목 — 설계 §9 마지막 문단), `docs/operations/runbook.md`(파일 위치 `~/.cache/ai_trader/excess_return/`), CHANGELOG, CLAUDE.md 캐시 목록, 설계 문서 §0 보완(S1~S10).

## 2. 2단계 — 거래일 기록 무결성 (절충안 1단계, DB 전용)

돈 경로 변경은 없다. 다만 `sync_from_kis` 는 기동 때도 돌고(`run_trader.py:716`) 그 뒤 `restore_daily_pnl_from_db`(`engine.py:930-999`)가 **당일 BUY 이벤트 수 → `daily_trades`, SELL pnl 합 → `daily_pnl`** 을 복원하므로, 행 **수**가 바뀌는 변경은 하지 않는다(아래 T8 은 BUY 행 수·SELL 행 수를 바꾸지 않는다).

### T7. 체결 조회 완결 여부 — `src/execution/broker/kis_kr.py`

- `_query_daily_fills(target_date=None, status=None)`: `status` 가 dict 이면 모든 페이지 `rt_cd=="0"` 이고 페이지 상한이 아닌 종료 조건(헤더 D/E·ctx 비움·빈 페이지)으로 끝났을 때만 `status["complete"]=True`, 아니면 `False` + `reason`. 기본값 `None` 이면 **동작 동일**(`check_fills` 무변경).
- `get_fills_for_date_checked(d) -> (fills, complete, reason)` 신규. 기존 `get_all_fills_for_date` 반환은 그대로.
- **시험:** `tests/test_kis_pagination_protocol.py`·`tests/test_kis_ledger_pagination_2026_09_15.py` 무수정 통과 + 새 시험(1페이지 실패 → `complete=False`, 2페이지 실패 → 부분 목록 + `False`, 페이지 상한 → `False`, 정상 종료 → `True`).

### T8. `TradeStorage.sync_from_kis` — `src/data/storage/trade_storage.py`

- 시그니처 `sync_from_kis(self, broker, engine=None, target_date=None) -> dict`(`{"complete": bool, "reasons": [...]}`; 기존 호출부는 반환값을 쓰지 않아 호환).
- 조회는 `get_fills_for_date_checked` 가 있으면 그것, 없으면 기존 함수 + `complete=False, reason="unchecked_api"`.
- **incomplete 사유**(조사 목록): 조회 미완결, 사전 조회 실패(`:983`), odno 조회 실패(`:1119`), 매도 대상 없음(`:1058`), 잔량 초과 클램프(`:1095`), 체결가 ≤ 0(`:1157`), 쓰기 최종 실패 카운터 증가, 큐 `join` 시한 초과, 바깥 예외(`:1221`). `_reconcile_pnl` 실패는 넣지 않는다(손익 보정이고 기록 누락이 아니다).
- **매수 복구:** 종목이 DB·캐시에 없을 때 `buy_fills[0]` 대신 그 종목 **모든 매수 odno 의 Σ수량·가중평균가**로 한 건을 기록한다(BUY 행 수는 종목당 1건 그대로 — 엔진이 첫 체결 때만 `record_entry` 를 쓰므로 odno 별 비교 복구는 가짜 거래를 만든다, 조사 함정 1).
- **쓰기 큐:** `_db_writer` 의 최종 실패 분기(`:234`)에 `self._write_failures += 1`. `sync_from_kis` 는 사전 조회 전과 끝에서 `await asyncio.wait_for(self._write_queue.join(), 30)`(시한 초과 = incomplete), 끝의 카운터 증가분을 사유로 본다. 기존 `asyncio.sleep(0.5)` 는 join 으로 대체.
- **상태 표:** `SCHEMA_SQL` 에 `execution_day_status(trade_date DATE PRIMARY KEY, status VARCHAR(12) NOT NULL, reasons TEXT, source VARCHAR(30), updated_at TIMESTAMP NOT NULL)`. **UNIQUE 추가·기존 표 변경 없음**(실패 시 봇 전체가 JSON 모드로 떨어진다 — 조사 함정 3). 쓰기는 **장 마감 뒤(15:40 KST 이후) 호출에서만** upsert 한다(장중 기동 호출은 하루가 끝나지 않았으므로 쓰지 않는다). 마지막 장 마감 뒤 결과가 이긴다.
- **시험(`sync_from_kis` 시험은 지금 0건 → 특성화부터):** `tests/test_sync_from_kis_integrity.py` — 가짜 브로커(`get_fills_for_date_checked`)·가짜 pool(`fetch`·`fetchval`·`execute`·`acquire`)·`_enqueue` 가로채기(`test_sell_journal_reason.py:69-80·170-185` 패턴)·`Path.home=tmp_path`. ① 현재 동작 특성화(매도 복구 수량·거래 id·exit_type) ② 조회 미완결 → `complete=False`·상태 행 `incomplete` ③ 매수 3 odno → Σ수량·가중평균 1건 ④ 쓰기 최종 실패 → incomplete ⑤ 15:40 전 호출 → 상태 행 없음 ⑥ BUY·SELL 행 수가 기존과 같다(위험 게이트 복원 입력 보존).

### T9. 설계 A 가 상태 표를 읽는다 — `src/analytics/excess_return.py`

- `run_daily_update` 가 `SELECT trade_date, status FROM execution_day_status WHERE trade_date >= $1` 로 읽는다. 표가 없거나 조회가 실패하면 상태 없음으로 진행(예외 삼킴·로그).
- 포지션의 [진입일, 마지막 청산일] 에 `incomplete` 날이 있으면 `record_incomplete` 로 제외. 상태 행이 없는 날만 걸친 포지션은 제외하지 않고 `day_status_missing` 로 센다(2단계 이전 이력 전부가 여기 해당 — 보이게만 한다).

### T10. 2단계 문서

`docs/integrations/external-apis.md`(체결 조회 완결 판정), `docs/operations/runbook.md`(상태 표 조회 SQL), CHANGELOG, 설계 A §5·§7·§11 보완(S10).

## 3. 검증

```bash
venv/bin/python -m py_compile src/analytics/excess_return.py scripts/export_risk_ledger.py scripts/review_risk_canary.py src/dashboard/data_collector.py src/schedulers/kr_scheduler.py src/execution/broker/kis_kr.py src/data/storage/trade_storage.py
```

- 단계마다: 새 시험 RED 확인 → 구현 → 관련 시험(위 파일들 + `tests/test_loop_heartbeat_integration.py`·`tests/test_codex_cf_preservation.py`·`tests/test_sell_journal_reason.py`) GREEN.
- 전체 회귀는 작업자가 모두 멈춘 뒤 조용한 시간대에 UTC·KST 각 1회(`QWQ_VERIFY_PYTHON` 지정 — worktree verify 함정, 메모리). **격리 위반 0** 을 출력 요약으로 직접 확인(conftest 는 위반을 실패로 만들지 않는다).
- 기준선: main `e31c632` 배포 verify 2113 passed / 2 xfailed / pykrx warning 1. 새 시험 수만큼 늘어야 하고 기존 결과는 같아야 한다.

## 4. 작업자·리뷰 배정

| 단계 | 작성(요청) | 리뷰(요청, 작성자 아님) | 근거 |
| --- | --- | --- | --- |
| 계획 리뷰 | — | Codex `gpt-6-astra` / high, read-only | 2단계가 기동 시 위험 게이트 입력(`restore_daily_pnl_from_db`)에 닿는 DB 쓰기 경로라 일반 리뷰(Sol)보다 한 단계 위 |
| 1단계 구현 | Claude Opus / high(단일 writer, 이 worktree) | Codex `gpt-6-astra` / high | 분석 경로 + 스케줄러 배선 |
| 2단계 구현 | Claude Opus / high | Codex `gpt-6-astra` / xhigh | DB 기록 경로·체결 조회 계약 |
| 통합·전체 회귀 | coordinator 만 | — | 정책: coordinator 단독 통합 |

동시 작업자는 합산 3명 이하, 작성자는 파일당 1명. 생성된 diff·명령은 제안으로 보고 coordinator 가 읽은 뒤 적용·실행한다.

## 5. 하지 않는 것

배포·재시작·main 병합, 설정·`.env`·킬스위치 변경, 주문·전략·위험 코드 변경, `TradeRecord.is_sync` 변경, `trades`/`trade_events` 에 열·UNIQUE 추가, `record_entry`/`record_exit` 시그니처 변경, 20:05 추가 `sync_from_kis` 호출(원장 TR 증가 — 절충안 후속으로 미룸), 과거 DB 행 수정.

## 6. 리뷰 기록

| 회차 | 리뷰어(요청 모델/effort, 실제 모델) | 결과 | 처리 |
| --- | --- | --- | --- |
| 1 | (리뷰 후 기록) | | |
