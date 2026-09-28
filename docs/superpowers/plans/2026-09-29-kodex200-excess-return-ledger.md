# 실거래 KODEX200 초과수익 원장 — 구현 계획 (설계 A + 절충안 1단계)

> **상태 (2026-09-29, 3판):** 계획. 1판(`b94eba4`)·2판(`932ec5f`) 모두 교차 공급자 리뷰 REQUEST_CHANGES — 3판이 두 회차의 처분이다(§6).
> 3회차 계획 리뷰는 돌리지 않고 2단계 **구현 리뷰**에서 대사 규칙을 확인한다(반복 비용 대비 — 대사는 본질적으로 최선 노력 검출이다). 사용자 지시(원문): "계획서 나오면 리뷰까지 받은 후 설계 보완하고 이후 설계안대로 구현까지 가자".
> 범위는 **구현·시험·브랜치 커밋**까지다. 커밋은 프로젝트 규칙(CLAUDE.md "Always commit and push together")대로 feature 브랜치에 **비강제 푸시**하고
> 그 결과 PR #98 이 갱신된다 — main 병합·배포는 아니다(리뷰 P2-7 은 이 근거로 유지 처분). 배포·재시작·main 병합·설정/`.env`/킬스위치 변경은 하지 않는다(별도 지시).
> 기준: 브랜치 `claude/kodex200-excess-return-design-64d126`(PR #98) `0c8b32b`, 제품 코드 = main `e31c632`.
> 설계: `docs/superpowers/specs/2026-09-28-kodex200-excess-return-ledger-design.md`(비작성자 리뷰 APPROVE).
> 방향: 09-29 사용자 결정 절충안(설계 B §0) — 이 계획의 2단계가 절충안 순서 (1) 의 "거래일 기록 미완 표시"다. 절충안 문구의
> "sync_from_kis 주문번호 단위화"는 **복구 로직 변경 대신 대사 검증**으로 좁혔다: 복구 변경은 기동 시 일일 손익·거래 수 복원 입력을 바꾸기 때문이다(리뷰 P1-1·2).
> 코드 사실은 읽기 전용 조사 3건(요청 Opus/high)이 뽑았고, 핵심 지점은 coordinator 가 다시 확인한다(구현 착수 전 각 줄 재확인 규칙).

## 0. 설계 보완 목록 (계획 리뷰 뒤 설계 문서에 반영)

조사로 드러난 사실 때문에 설계 A 의 문장을 고쳐야 하는 곳이다. 동작 원칙은 바꾸지 않는다.

| # | 설계 A 위치 | 보완 |
| --- | --- | --- |
| S1 | §5 `sync_entry` | KR 동기화 진입은 거래 id 접두 `KIS_SYNC_`(`trade_storage.py:995`, `entry_reason="KIS 동기화 복구"`)로만 식별된다. `entry_reason=="sync_detected"`·`SYNC_` 접두는 US 경로다(`us_scheduler.py:1921·2024`). 규칙은 셋 다 보되 KR 실효는 `KIS_SYNC_` 임을 적는다 |
| S2 | §5 동기화 청산 | `sync_detected` 는 KR 에서 **청산 유형**이다(`trade_journal.py:512`) — 설계대로 청산 쪽 목록에 둔다 |
| S3 | §5 `exits_missing` | exporter SELECT 가 `pnl`·`exit_price` NULL 을 0 으로 읽는다(`export_risk_ledger.py:359·362`) → 원천 NULL 을 `pnl_missing` 으로 보존해 `exits_missing` 으로 뺀다(실제 0원과 구분, 설계 §6 계약) |
| S4 | §5 분류 입력 | exporter position dict 에는 `entry_reason`·`exit_type` 키가 없다 → build_ledger 가 `getattr` 로 두 키를 더한다(canary 는 필수 키만 검사해 영향 없음) |
| S5 | §2·§8 `is_sync` | 공용 함수로 바꾸는 호출처는 `data_collector.py:622-632` 하나다. `TradeRecord.is_sync`(`trade_journal.py:69-81`)는 05-27 에 일부러 청산 유형만 보게 했고 진화·복기 표본이 이것을 쓴다 → **바꾸지 않는다** |
| S6 | §4 DB 접근 | KR 봇의 DB 는 `bot.trade_journal`(TradeStorage) 의 `pool` 이다(`run_trader.py:713-715`). `pool` 이 있어도 `_db_available=False` 일 수 있다(`trade_storage.py:121-134`) → 세 조건 모두 확인 |
| S7 | §4 실행 창 | `last_review_date` 가 evolve **앞에서** 영속되므로(`kr_scheduler.py:5680-5694`) 20:30 블록 도중 재시작하면 그날 단계는 다시 돌지 않는다 → 다음 거래일 전체 재계산으로 복구된다고 적는다 |
| S8 | §4① 캐시 | `get_daily_prices` 는 페이지 실패 시 부분 행을 반환하고(`kis_kr.py:1888-1891`) 결과를 `unique[-days:]` 로 자른다(`:1938-1948`) — "첫 날짜 ≤ 가장 오래된 진입일일 때만 교체"는 과거 구간 확보 검사다(중간 결측은 행별 `bench_missing_reason` 이 드러낸다) |
| S9 | §6 보고 | 텔레그램 기본 `parse_mode="HTML"`(`telegram.py:47·164`) → 한 줄은 `html.escape` 한다 |
| S10 | §5·§7·§11 기록 미완 | 절충안 채택으로 `execution_day_status` 의 원천이 설계 B 가 아니라 **2단계의 20:30 기록 대사**(설계 §5-1)가 된다. 제외 사유 `record_incomplete`, 상태 행이 없는 날은 `day_status_missing` 으로 센다 |
| S11 | §5 판정 순서 | exporter 는 `exits_aggregated` 일 때 `lots_ambiguous` 도 켠다(`export_risk_ledger.py:281-282`) → `exits_aggregated` 를 먼저 판정한다 |

**상태: S1~S11 은 설계 문서에 반영했다(설계 §12 4회차).**

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
  - `classify(pos, bench, day_status)` — 설계 §5 판정 순서 ①~⑩(① `closed` 인데 exits 없음/`net_pnl` 없음/`pnl_missing` → `exits_missing` ② Σ매도 < Σ매수 → `awaiting_close` ③ Σ매도 > Σ매수 → `quantity_mismatch` ④ `exits_aggregated` ⑤ `lots_ambiguous` ⑥ `manual_entry` ⑦ `sync_entry` ⑧ `recovered_at_exit` ⑨ `bench_out_of_range` ⑩ `record_incomplete`). 제외 사유별 시험은 **exporter `build_ledger` 를 거친 입력**으로도 한 번씩 확인한다(인위적 dict 만으로는 ④⑤ 관계를 놓친다).
  - `position_row(pos, bench, day_status, *, computed_at, code_sha)` — §5 schema 1 행(Decimal 문자열). `clip_pct` = `actual_stop_pct` → `stop_pct` → `-STOP_CLIP_PCT`, `stop_overshoot` 은 `entry_stop` 에서만.
  - `summarize(rows, previous_rows, *, today, awaiting_close, day_status_missing)` — §7 지표 × 창(전체·최근 90일) × 묶음(전체·전략·월·`exit_quality`·cohort). `status` 는 `insufficient_sample`/`measured` 둘뿐.
  - `format_weekly_line(summary)` — §7 한 줄(계산일 포함). 호출부가 `html.escape`.
  - `load_exporter(root)` — `backtest_gate.py:183-206` 과 같은 방식(`spec_from_file_location` → `sys.modules` 등록 → `exec_module`, 모듈 전역 캐시).
  - `async run_daily_update(*, broker, fetch, out_dir, root, now, code_sha)` — §4 ①~④. ① `oldest_entry` 를 알기 위해 ② 를 먼저 하고(순서만 바꾼다, 결과 동일), 거래일 수 N+5 로 `get_daily_prices("069500", days=min(N+5, 500))`, 첫 날짜 ≤ 가장 오래된 진입일일 때만 CSV 원자 교체 ③ 스냅샷 원자 교체 ④ `summary.json` 원자 교체 + `summary_history.jsonl` 같은 날짜 줄이 없을 때만 append(fsync, `position_ledger.py:203` 패턴). drift 는 **직전 계산일** 스냅샷(`positions_prev.jsonl` — 날짜가 다를 때만 교체 전 사본으로 보관)과 비교.
- **시험(먼저 RED):** `tests/test_excess_return_ledger.py` — 설계 §9 1~9 + S10 의 `record_incomplete`/`day_status_missing` + 원천 NULL pnl ↔ 실제 0원 구분(DB 행 → `fetch_trade_records` → `build_ledger` → 행). 가짜 브로커는 `SimpleNamespace(get_daily_prices=AsyncMock(...))`(`test_codex_cf_preservation.py:62`), 가짜 fetch 는 SQL 문자열로 분기하는 async 함수, 경로는 `tmp_path`. async 는 `asyncio.run()`(저장소 관례).

### T2. canary 가 공용 함수를 쓴다 — `scripts/review_risk_canary.py`

- import 앞에 exporter 와 같은 ROOT `sys.path` 삽입(`export_risk_ledger.py:49-51`) — 없으면 `python scripts/review_risk_canary.py` 단독 실행과 `test_risk_canary_report.py` 단독 실행이 `src` 를 못 찾는다.
- `load_benchmark`·`position_benchmark` 를 import, `_dec = to_decimal`, `_date = parse_date`, `_buys = buy_fills` 로 별칭(다른 함수가 계속 쓴다). `_strip` 은 남은 호출처가 없으면 지운다.
- **무변경 기준:** canary **판정식**이 같고 `tests/test_risk_canary_report.py`·`tests/test_entry_risk_lifecycle.py`·`tests/test_entry_risk_wiring.py` 가 수정 없이 통과(T3 로 exits·벤치마크 범위가 바뀌면 운영 데이터의 계산 결과는 달라질 수 있다).

### T3. exporter — `scripts/export_risk_ledger.py`

- `fetch_trade_records(fetch, days) -> List[TradeRecord]` 로 `:334-384`(SELECT 두 개·레코드 조립·`sell_legs` 부착)를 옮긴다. `_load_from_db` 는 `connect` → `pool is None` 폴백 → `return await fetch_trade_records(storage.pool.fetch, days)` → `finally disconnect` 만 남긴다. **새 함수는 connect/disconnect 를 하지 않는다**(봇 pool 보호).
- `_fills_and_exits`: `exit_quantity == 0` 조기 반환은 그대로 두고, `exit_price is None or <= 0` 검사를 leg 가 없을 때의 폴백 분기(`if len(exits) == 0:`) 맨 앞으로 옮긴다.
- `fetch_trade_records` 가 각 레코드에 `pnl_missing = row["pnl"] is None` 을 붙이고(`sell_legs` 와 같은 동적 속성), `build_ledger` 가 position 에 `entry_reason = getattr(trade, "entry_reason", "")`, `exit_type = getattr(trade, "exit_type", "")`, `pnl_missing = getattr(trade, "pnl_missing", False)` 를 더한다(시험의 `SimpleNamespace` 거래에는 `entry_reason` 이 없다 — `test_entry_risk_wiring.py:352-357`, `exit_type` 은 있다).
- **시험:** `exit_price=0`·SELL leg 있음 → leg 로 exits 생성(새 시험). 기존 세 시험 파일 무수정 통과. 알려진 운영 영향(시험 밖): leg 가 새로 생긴 risk cohort 포지션은 canary `net_pnl_mismatch`(평균단가 기반 DB pnl 과의 1원 초과 차이)가 새로 보일 수 있다 — 기술 검증 신호일 뿐 성과 판정은 바뀌지 않는다(CHANGELOG 에 적는다).

### T4. 대시보드 `is_sync` — `src/dashboard/data_collector.py:622-632`

- `ev["is_sync"] = is_sync_entry(trade_id, entry_reason) or is_sync_exit(_etype_ev)`. 새로 `sync_detected` 청산이 동기화로 표시된다(표시 전용 — `ev["is_sync"]` 를 읽는 프론트 코드는 없음, 조사). `TradeRecord.is_sync` 는 건드리지 않는다(S5).

### T5. 스케줄러 배선 — `src/schedulers/kr_scheduler.py`

- **20:30:** `run_evolution_scheduler` 의 `if bot.strategy_evolver: … else: …` 블록 **전체가 끝난 뒤**(24칸 들여쓰기 — `:5728` 은 else 분기 안이다), `await asyncio.sleep(60)`(`:5730`) 앞, `if last_review_date != today:` 안쪽에 `await self._run_excess_return_step(bot, now)` 한 줄. evolve 성공·실패·부재 세 경우 모두에서 돈다.
- `KRScheduler._run_excess_return_step(bot, now)`: `tj = getattr(bot, "trade_journal", None)`; `pool`·`_db_available`·`getattr(bot, "broker", None)` 셋 중 하나라도 없으면 **디스크를 건드리기 전에** return(기존 시험의 가짜 봇에는 broker·trade_journal 이 없다 — `test_loop_heartbeat_integration.py:302-309`).
  `out_dir = Path.home()/".cache"/"ai_trader"/"excess_return"` 는 호출 시점에 계산. `await asyncio.wait_for(run_daily_update(...), 90)`(단계 90초 — 대사 45초, 큐 대기 20초) 를 `try/except Exception`(`TimeoutError` 포함)으로 감싸 `logger.warning("[초과수익] …")` 만. 바깥 블록의 `raise` 규칙(`:5734-5737`)에 닿지 않게 한다.
- **토요일:** `run_post_exit_review_scheduler` 의 게이트 성능 분석 `except`(`:6511-6512`) 뒤에 자체 try: `summary.json` 을 읽어 `format_weekly_line` → `send_alert(html.escape(line))`. 파일이 없으면 보내지 않는다(로그만). 같은 try 안의 `run_weekly` 가 실패하면 이 줄도 나가지 않는다 — 기존 구조의 한계로 기록.
- **시험:** 기존 스케줄러 하네스(`test_loop_heartbeat_integration.py:273-331` — `_FakeClockDatetime`·`object.__new__(KRScheduler)`·`asyncio.run(run_evolution_scheduler())`)로 evolve **성공·예외·부재** 세 경우 각각 `_run_excess_return_step` 이 한 번 불리는지 확인(메서드를 기록용 가짜로 교체) + 단계 실행 시험 3개(① 속성 없는 봇 → 파일 0·예외 0 ② 가짜 pool·브로커·`Path.home=tmp_path` → 파일 생성 ③ 브로커 예외·시간 초과 → 예외 전파 0) + 토요일 실행 시험(요약 있음 → `send_alert` 1회·HTML 이스케이프, 요약 없음 → 0회). 기존 `test_evolution_scheduler_*` 무수정 통과·격리 위반 0.

### T6. 1단계 문서

`docs/risk/risk-and-exit.md`(판정 기준 절에 실거래 원장), `docs/operations/monitoring-checkpoints.md`(배포 후 첫 실행 확인 항목 — 설계 §9 마지막 문단), `docs/operations/runbook.md`(파일 위치 `~/.cache/ai_trader/excess_return/`), CHANGELOG, CLAUDE.md 캐시 목록, 설계 문서 §0 보완(S1~S10).

## 2. 2단계 — 거래일 기록 대사 (절충안 1단계, 설계 §5-1)

**기존 복구 로직(`sync_from_kis`)과 쓰기 경로는 바꾸지 않는다.** 1판의 매수 Σ 복구·쓰기 실패 카운터·15:40 조건부 기록은 기동 시
`restore_daily_pnl_from_db`(`engine.py:930-999` — BUY 건수 → `daily_trades`, SELL `SUM(pnl)` → `daily_pnl`)의 입력을 바꾸고(리뷰 P1-1),
주문번호 단위 대사도 엔진 쓰기에 주문번호가 없어 정확히 할 수 없었다(P1-2). 2판은 **읽기 전용 대사**로 기록의 완결 여부만 표시한다.
그래서 돈 경로·위험 게이트 입력·대시보드·진화 입력은 바뀌지 않는다(새 표 하나와 20:30 조회 1회만 는다).

### T7. 체결 조회 완결 판정 — `src/execution/broker/kis_kr.py`

- `_query_daily_fills(target_date=None, status=None)`: `status` 가 dict 일 때만 판정을 채운다. **완결 = 모든 페이지 `rt_cd=="0"` 이고 마지막 페이지의
  응답 헤더 `tr_cont` 가 D/E**(연속조회 종료 규칙 — CLAUDE.md "KIS 연속조회 종료는 응답 헤더로 판정")일 때다. **10번째(마지막 허용) 페이지가 D/E 로 끝나도 완결**이고,
  10페이지 뒤에도 F/M 이면 상한 미완이다. 헤더가 F/M 인데
  ctx 가 비었거나 빈 페이지·같은 ctx 반복으로 루프가 끝나면 **미완**(`reason`=`contradictory_continuation`/`repeated_ctx`), 미연결은 `not_connected`.
  기본값 `None` 이면 **동작·호출 횟수 동일**(`check_fills` 무변경).
- `get_fills_for_date_checked(d) -> (fills, complete, reason)` 신규: 위 판정 + 행 정규화 실패(`tot_ccld_qty`·`avg_prvs` 변환 실패)도 미완으로 본다.
  기존 `get_all_fills_for_date` 는 그대로.
- **시험:** `tests/test_kis_pagination_protocol.py`·`tests/test_kis_ledger_pagination_2026_09_15.py` 무수정 통과 + 새 시험: 1페이지 실패 → 미완,
  2페이지 실패 → 부분 목록 + 미완, F/M 인데 ctx 빔 → 미완, **10번째 페이지가 D/E 로 끝남 → 완결 / 10페이지 뒤에도 F/M → 미완(상한)**, 정규화 실패 → 미완,
  기본 호출(`status=None`)의 요청 횟수·반환이 기존과 같음.

### T8. 기록 대사 — `src/analytics/excess_return.py` + 표 DDL

- `TradeStorage.SCHEMA_SQL` 에 `CREATE TABLE IF NOT EXISTS execution_day_status (trade_date DATE PRIMARY KEY, status VARCHAR(12) NOT NULL,
  reasons TEXT, source VARCHAR(30), checked_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL)`. 기존 표·UNIQUE·`_ensure_tables` 마이그레이션 무변경.
- **뜻(3판에서 낮춤):** `complete` = **"20:30 대사가 불일치를 찾지 못했다"** — 완전성의 증명이 아니다. 엔진 기록에 주문번호가 없어(`record_entry`/`record_exit`
  INSERT 에 `kis_order_no` 없음) 주문 단위 대응을 증명할 수 없기 때문이다. `incomplete` = 불일치를 찾았거나 대사를 끝내지 못했다.
- `async verify_day_records(*, broker, fetch, write_queue, day) -> dict` (`excess_return.py`, 저장은 하지 않고 결과만 돌려준다):
  ① `write_queue` 가 있으면 `await asyncio.wait_for(write_queue.join(), 20)`(큐 대기 20초) — 시한 초과면 `incomplete(write_queue_pending)` (join 은 처리 종료만 증명한다 — ⑤ 가 결과를 본다)
  ② `broker.get_fills_for_date_checked(day)` — 없거나 미완이면 `incomplete(fill_query_incomplete:<reason>)`
  ③ DB 일별 합: `SELECT symbol, event_type, SUM(quantity) FROM trade_events WHERE event_time::date = $1 GROUP BY symbol, event_type`
  ④ SELL: 종목별 Σ KIS(`sll_buy_dvsn_cd=="01"`, 주문번호별 누적 수량의 합) ≠ Σ DB SELL → `incomplete(sell_qty:<종목>)`(DB 에만 있는 종목 포함).
     BUY: KIS 매수 종목(`"02"`)에 DB BUY 가 없거나, **DB 에만 BUY 가 있으면** `incomplete(buy:<종목>)`. BUY 수량은 비교하지 않는다(엔진은 첫 체결에만 BUY 행을 쓴다 — 거짓 불일치 방지)
  ⑤ **거래 본체 교차 검증:** 오늘 SELL 이벤트가 있는 거래마다 `trades.exit_quantity` = 그 거래의 SELL 이벤트 수량 전체 합(모든 날짜)인지 확인 — 다르면
     `incomplete(trade_row:<trade_id>)`. 이벤트는 기록됐는데 `trades` UPDATE 만 실패한 경우(별도 큐 항목, `trade_storage.py:399-429`·직접 경로 `kr_scheduler.py:3033~`)를 잡는다.
     손익은 비교하지 않는다(`_reconcile_pnl` 이 `trades.pnl` 을 사후 보정해 거짓 불일치가 난다)
  ⑥ 체결 0건이고 DB 이벤트 0건이면 `complete`
  ⑦ ③·⑤ 조회가 예외면 `incomplete(db_query_failed)`.
- **저장과 적용:** 20:30 단계가 결과를 `execute` 로 upsert 하고(`source="kr_excess_20_30"`, `checked_at`), **같은 실행의 원장 계산에는 저장 성공 여부와 무관하게
  이번 결과를 직접 쓴다**(기존 행을 덮지 못한 저장 실패가 이전 판정을 되살리지 않게). 요약에 `day_status_saved: true/false` 를 남긴다.
- **시각 규약:** `event_time` 은 호스트 로컬 naive 시각이다(`trade_storage.py:67`, 직접 경로 `kr_scheduler.py:3031`). 대사 날짜 `day` 도 **같은 호스트 로컬 날짜**
  (20:30 블록의 `today`)를 쓴다 — 운영 호스트는 KST 다. 날짜만 KST 로 바꾸면 UTC 호스트에서 저장 시각과 어긋난다(리뷰 P2-4). 시험은 주입 시계로 UTC·KST 두 TZ 에서 같은 결과여야 한다.
- 알려진 검출 한계(적는다): 같은 종목에서 한 주문의 중복 기록과 다른 주문의 누락이 수량으로 상쇄되는 경우, 기존 보유 종목의 추가 매수 누락은 잡지 못한다 — legacy 에도 같은 한계이고(원칙: 새 방어를 만들지 않음) 설계 A 의 포지션 단위 규칙(Σ매도 ≥ Σ매수·`quantity_mismatch`)이 일부를 추가로 거른다.

### T9. 원장이 상태 표를 읽는다 — `src/analytics/excess_return.py`

- `run_daily_update` 가 `SELECT trade_date, status FROM execution_day_status WHERE trade_date >= $1` 로 읽고, 오늘 날짜는 T8 의 이번 결과로 덮어 쓴다.
- **표가 없는 것(도입 전)과 조회 실패를 구분한다:** 표 없음(`UndefinedTable`)만 "상태 없음"으로 진행하고, 그 밖의 조회 실패는 **이번 갱신을 중단**해 기존 스냅샷·요약을 보존한다
  (이미 제외했던 포지션이 일시 오류로 되살아나지 않게 — 리뷰 P1-3).
- 보유 구간에 `incomplete` 날이 있으면 `record_incomplete`, 상태 행이 없는 날만 걸친 포지션은 포함하고 `day_status_missing` 으로 센다.
- **실패 격리:** 대사(T8)부터 요약까지 전부 T5 의 한 `asyncio.wait_for(…, 90)`·`try/except` 안에서 돈다(리뷰 P2-6). 단계 90초 중 대사는 자체 시한 45초(큐 대기 20초 포함) — 초과는 `incomplete(verify_timeout)` 로 저장까지 한다.

### T10. 2단계 시험·문서

- `tests/test_excess_return_ledger.py` 에 대사 시험: SELL 수량 일치 → complete, 불일치(DB 5·KIS 10) → incomplete, DB 에만 SELL → incomplete, BUY 누락·DB 에만 BUY → incomplete,
  `trades.exit_quantity` ≠ SELL 이벤트 합 → incomplete, 0/0 → complete, 조회 미완 → incomplete, 큐 join 시한 초과 → incomplete, DB 조회 예외 → incomplete,
  저장 실패(기존 complete 행 위) → 이번 incomplete 가 원장에 적용·`day_status_saved=false`, 상태 표 조회 실패(표 있음) → 갱신 중단·기존 파일 보존, 표 없음 → 상태 없음으로 진행,
  UTC·KST 두 TZ 결과 동일, `record_incomplete`·`day_status_missing` 집계.
  쓰기 큐는 **실제 `asyncio.Queue`**(task_done 호출 여부로 join 동작 확인)를 쓴다.
- 문서: `docs/integrations/external-apis.md`(체결 조회 완결 판정), `docs/operations/runbook.md`(상태 표 조회 SQL), CHANGELOG.

## 3. 검증

```bash
venv/bin/python -m py_compile src/analytics/excess_return.py scripts/export_risk_ledger.py scripts/review_risk_canary.py src/dashboard/data_collector.py src/schedulers/kr_scheduler.py src/execution/broker/kis_kr.py src/data/storage/trade_storage.py
```

- 단계마다: 새 시험 RED 확인 → 구현 → 관련 시험(위 파일들 + `tests/test_loop_heartbeat_integration.py`·`tests/test_codex_cf_preservation.py`·`tests/test_sell_journal_reason.py`) GREEN.
- 전체 회귀는 작업자가 모두 멈춘 뒤 조용한 시간대에 UTC·KST 각 1회(`QWQ_VERIFY_PYTHON` 지정 — worktree verify 함정, 메모리). **격리 위반 0** 을 출력 요약으로 직접 확인(conftest 는 위반을 실패로 만들지 않는다).
- 기준선: main `e31c632` 배포 verify 2113 passed / 2 xfailed / pykrx warning 1. 새 시험 수만큼 늘어야 하고 기존 결과는 같아야 한다.
- 커밋 전 비밀정보 검사: `git diff --cached` 에 appkey·secret·token·계좌번호 패턴이 없는지 `grep -nEi 'appkey|appsecret|secret|token|CANO|password'` 로 확인(시험의 가짜 값 제외).

## 4. 작업자·리뷰 배정

| 단계 | 작성(요청) | 리뷰(요청, 작성자 아님) | 근거 |
| --- | --- | --- | --- |
| 계획 리뷰 | — | Codex `gpt-6-astra` / high, read-only | 2단계가 기동 시 위험 게이트 입력(`restore_daily_pnl_from_db`)에 닿는 DB 쓰기 경로라 일반 리뷰(Sol)보다 한 단계 위 |
| 1단계 구현 | Claude Opus / high(단일 writer, 이 worktree) | Codex `gpt-6-astra` / high | 분석 경로 + 스케줄러 배선 |
| 2단계 구현 | Claude Opus / high | Codex `gpt-6-astra` / high | 체결 조회 계약(돈 경로 `check_fills` 가 같은 함수를 쓴다 — 기본값 동작 동일 확인) + 읽기 전용 대사 |
| 통합·전체 회귀 | coordinator 만 | — | 정책: coordinator 단독 통합 |

동시 작업자는 합산 3명 이하, 작성자는 파일당 1명. 생성된 diff·명령은 제안으로 보고 coordinator 가 읽은 뒤 적용·실행한다.

## 5. 하지 않는 것

배포·재시작·main 병합, 설정·`.env`·킬스위치 변경, 주문·전략·위험 코드 변경, **`sync_from_kis`·`record_entry`/`record_exit`·쓰기 큐 변경**, `TradeRecord.is_sync` 변경, `trades`/`trade_events` 에 열·UNIQUE 추가, 추가 `sync_from_kis` 호출, 과거 DB 행 수정, 과거 날짜 대사.

## 6. 리뷰 기록

| 회차 | 리뷰어(요청 모델/effort, 실제 모델) | 결과 | 처리 |
| --- | --- | --- | --- |
| 1 | Codex 교차 공급자(요청 gpt-6-astra/high, read-only; rollout model `gpt-6-astra`·effort `high`) — `b94eba4` | REQUEST_CHANGES — P0 0, P1 4, P2 4 | 코드 근거 확인 후 2판: P1-1·2·3 → 2단계를 `sync_from_kis` 무변경 읽기 전용 대사로 축소(돈 경로·복원 입력 보존), P1-4 → 조회 완결 = 헤더 D/E 종료·모순/반복/상한/미연결/정규화 실패 미완, P2-5 → '조회 시점까지'·KST 고정·`target_date` 제거, P2-6 → 삽입 위치(if/else 밖)·하네스 3경우·토요일 실행 시험·실제 Queue, P2-7 → `pnl_missing` 보존, P2-8 → `exits_aggregated` 먼저. 인용 정정(`exit_type` 은 있음, raise `:5734-5737`), canary '판정식 유지', 비밀정보 검사 추가, 범위 문구 |
| 2 | 같은 조건 2회차(rollout model `gpt-6-astra`·effort `high`) — `932ec5f` | REQUEST_CHANGES — 1회차 해소 6·부분 3·미해소 1, 새 P1 3·P2 4 | 3판: P1-1 → `complete` 뜻을 '불일치 미발견'으로 낮추고 DB 에만 BUY·검출 한계 명시, P1-2 → `trades.exit_quantity` ↔ SELL 이벤트 합 교차 검증(손익 제외), P1-3 → 표 없음/조회 실패 구분·실패 시 갱신 중단, P2-4 → 호스트 로컬 시각 규약·두 TZ 시험, P2-5 → 이번 결과 직접 적용·`day_status_saved`, P2-6 → 대사~요약 한 시한·예외 안, P2-7 → 프로젝트 규칙(커밋=푸시) 근거로 유지. 3회차 계획 리뷰는 생략하고 구현 리뷰에서 확인 |

### 구현 기록

| 단계 | 작성(요청/실제) | 결과·리뷰 | 커밋 |
| --- | --- | --- | --- |
| 1단계 T1~T5 | Claude Opus/high(실제 Opus 5.5) + coordinator 보완 1건(벤치마크 상한) | 새 시험 49·변이 7 kill, 전체 UTC/KST 2163 passed. Codex 구현 리뷰(요청 gpt-6-astra/high, rollout 동일) REQUEST_CHANGES P1 1(coordinator 보완의 결함)·P2 3 → 처분 | `f913781`, 처분 `2be0049` |
| 2단계 T7·DDL | Claude Opus/high | 새 시험 43(기본 경로 13개 시나리오 기준선 대조) | `2be0049` |
| 2단계 T8·T9 | Claude Opus/high + coordinator 보완 2건(표 없음 판정 좁힘·거래 본체 SQL KR 필터) | 새 시험 23, 전체 UTC 2234 passed, KST 1차 Toss 간헐 1 failed → 2차 2234 passed | 이번 커밋 |

