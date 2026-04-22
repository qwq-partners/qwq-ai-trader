# QWQ AI Trader - Changelog

## 2026-04-22 — 거래 로그 누락 복구 + 재시작 메타 복원 (P0)

### 배경
대시보드에 당일 KR 매매 내역 7건 중 5건 누락 (67% 유실) — KIS API 체결내역 대조 결과:
- 누락된 매수 2건: S-Oil(010950) 09:30, LG디스플레이(034220) 11:50
- 누락된 매도 3건: HJ중공업(097230) 2차/3차 부분매도 (09:32, 11:20, 11:38, 총 28주)

### 근본 원인 분석
1. **재시작 후 `pos.trade_id` 미복원** — `_restore_position_metadata`가 strategy/entry_time만 복원하고 trade_id는 복원 안 함. 따라서 어제 진입 포지션은 모두 trade_id=None 상태로 등록 → SELL 체결 시 "DB 직접 기록" 폴백 경로로 빠짐.
2. **`TradeStorage.record_entry()` 시그니처 불일치** — kr_scheduler가 `entry_reasons`/`score_breakdown` kwarg를 넘겼지만 Wrapper 시그니처에 없어 TypeError → BUY 저널 기록 전량 실패.
3. **"DB 직접 기록" 부분매도 처리 오류** — `UPDATE trades SET exit_time=... WHERE exit_time IS NULL`이 1차 익절만으로 trade row를 완전 닫음. 이후 2차/3차 매도 시 `WHERE exit_time IS NULL`로 조회 실패 → "오픈 포지션 없음".
4. **`sync_from_kis` DB 쿼리 누락** — `WHERE entry_time::date = today OR (exit_time IS NULL AND entry_time < today)` 조건이 "어제 진입·오늘 부분청산" 케이스(exit_time NOT NULL, exit_qty<entry_qty)를 제외.
5. **`_find_recovery_target` 우선순위 누락** — 부분청산된 과거 trade를 recovery 대상에서 제외.
6. **`bot.engine.strategies` AttributeError** — `theme_chasing` 확산 체크 시 존재하지 않는 속성 접근 (correct: `bot.strategy_manager.strategies`). 청산 체크 루프마다 에러 스택 로깅.

### 수정
- `src/data/storage/trade_storage.py`
  - **`record_entry()` 시그니처 확장**: `entry_reasons`, `score_breakdown` kwarg 추가 + 기본 `market` kwarg도 journal로 forward.
  - **`_find_recovery_target()` 우선순위 5 추가**: 잔여 수량 있는 부분청산 trade도 매칭 (entry_date 제한 없음, 최근 순).
  - **`sync_from_kis()` DB trade_rows 쿼리 확장**: `COALESCE(exit_quantity,0) < entry_quantity AND entry_time >= today-30d` 조건 추가하여 어제 진입·오늘 부분청산 케이스 포함.
- `src/schedulers/kr_scheduler.py`
  - **DB 직접 기록 경로 부분매도 처리**: `remaining_after = _sell_pos_snap.quantity - fill.quantity` 판정으로 전량 청산 시에만 `exit_time` 세팅. 부분매도는 `exit_quantity`/`pnl`만 누적 UPDATE. SELECT 조건도 `OR exit_quantity < entry_quantity`로 완화.
  - **`theme_chasing` strategies 참조 수정**: `bot.engine.strategies` → `bot.strategy_manager.strategies`.
- `scripts/run_trader.py`
  - **KR `_restore_position_metadata()`**: `pos.trade_id` 복원 추가. 쿼리 조건도 `exit_time IS NULL OR exit_quantity < entry_quantity`로 완화하여 부분청산 trade도 매칭.
  - **US `_initialize_us()` 메타 복원**: 동일 패턴 적용 — `trade_id` 복원 + 부분청산 매칭.

### 재발 방지 체크리스트
- [ ] 신규 kwarg 추가 시 Wrapper(TradeStorage)도 반드시 동시 수정
- [ ] 부분매도 지원 필요한 경로는 모두 `WHERE exit_time IS NULL OR exit_qty < entry_qty` 패턴 사용
- [ ] 재시작 복원 로직은 strategy/entry_time/trade_id 3종 세트 복원 (KR/US 공통)
- [ ] 종료 후 KIS API 체결내역 vs trade_events DB 카운트 자동 대조 (TODO: 일일 리포트에 추가)

### 추가 수정 (2차 검증 후, user 피드백)
KIS 앱 체결내역 화면과 비교 결과 **개별 fill 가격이 DB와 불일치**하는 문제 발견.

**원인 7**: `sync_from_kis` 매도 복구가 `sell_fills[-1]`(사실상 가장 오래된 fill) 가격 + `missing_qty` 총합으로 단일 이벤트 기록. 예: HJ중공업 4개 fill(6@28400, 11@29550, 5@31150, 12@30650)에서 첫 6주는 정상 기록됐지만 나머지 28주는 "28주 @ 28,400" 단일 이벤트로 기록 → 가격/PnL 대폭 부정확.

**원인 8**: `_reconcile_pnl`이 `sell_count==1`일 때 trade_events의 price 컬럼까지 KIS 가중평균으로 UPDATE → 개별 매도 체결가가 소실. 예: 6주 @ 28,400 이벤트가 weighted avg 29,970.59로 덮어써짐.

**수정**
- `src/data/storage/trade_storage.py` `sync_from_kis` 매도 복구:
  - KIS 체결을 `ord_tmd` 기준 오름차순 정렬
  - `kis_order_no` 기준 idempotency — 이미 기록된 ODNO 스킵
  - `already_sold` 수량만큼 오래된 fill 순으로 선행 skip (부분 skip 지원)
  - 나머지를 fill별로 개별 `record_exit` 호출 (가격/시간/ODNO 각각 정확)
- `_reconcile_pnl`: trade_events 개별 price UPDATE 제거. trades 집계 row만 보정.

**결과 (HJ중공업 097230 backfill 후)**
- 09:02:41 6주 @ 28,400 pnl=+6,814 (4.17%)
- 09:32:29 11주 @ 29,550 pnl=+25,115 (8.39%)
- 11:20:47 5주 @ 31,150 pnl=+19,399 (14.26%)
- 11:38:53 12주 @ 30,650 pnl=+40,570 (12.43%)
- 합계 34주 net pnl=+91,898원 (기존 오기록 64,233원 대비 +27,665원 상향)

## 2026-04-21 — 진입 근거 표준화 + theme_chasing 확산 검증 (P0 #1+#2)

### 배경
오늘 거래 복기(N=2) + strategy-advisor 검증 결과:
- SK하이닉스 거래의 entry_reason이 "buy_signal" placeholder로만 기록됨 → 사후 복기/진화 학습 입력 데이터 무력화
- LLM 복기: "theme_chasing은 '올랐다'보다 '계속 확산될 근거가 있다'가 핵심" — 진입 후 확산 검증 부재
- 단일 일자 N=2는 통계적 결정 불가 → 데이터 인프라 보강을 모든 파라미터 튜닝의 전제로 결정

### 수정 #1 — Signal 진입 근거 표준화 (shadow 모드)
- `src/core/types.py` Signal 데이터클래스:
  - `reasons: List[str]` (구조화 진입 근거, 최소 2개 권장) 필드 추가
  - `score_breakdown: Dict[str, float]` (전략별 핵심 메트릭) 필드 추가
  - `context_snapshot: Dict[str, Any]` (시장 체제, 섹터 강도 등) 필드 추가
  - `effective_reasons()` 헬퍼: reasons → reason 폴백 로직
- `src/core/engine.py` RiskManager.on_signal:
  - BUY 시그널 placeholder 검증 (shadow 모드 — 경고만, 1주일 후 hard-reject 전환)
  - placeholder terms: `{"buy_signal", "auto_buy", "signal", ""}`
  - 미충족 시 `metadata.flags.append("weak_reason")` + WARNING 로깅
- `src/core/engine.py` `_pending_signal_cache` 확장: reasons/score_breakdown/context_snapshot 보존
- `src/core/evolution/trade_journal.py` TradeRecord:
  - `entry_reasons: List[str]`, `score_breakdown: Dict[str, float]` 필드 추가
  - `record_entry()` 신규 인자 + placeholder 검증 (shadow 모드)
  - reason 문자열 → reasons 리스트 자동 폴백 (쉼표/세미콜론 분리)
- `src/schedulers/kr_scheduler.py` run_fill_check:
  - 캐시 reasons/score_breakdown/context_snapshot 추출 → record_entry 전달
  - context_snapshot은 market_context에 병합

### 수정 #2 — theme_chasing 진입 후 확산 검증 (shadow 모드)
- `src/strategies/kr/theme_chasing.py`:
  - generate_signal에서 reasons/score_breakdown/context_snapshot 채우기 시범 적용
  - `check_post_entry_diffusion()` 신규 메서드:
    - 진입 +30~60분 윈도우만 검증 (1회성)
    - (a) 동테마 동반상승 종목 ≥ min_theme_breadth 유지
    - (b) 진입가 대비 -1.5% 미만 보유 중
    - 두 조건 모두 미충족 시 WARNING 로깅 (자동 청산 X)
- `src/schedulers/kr_scheduler.py` _check_exit_signal:
  - position.strategy == "theme_chasing" 일 때 check_post_entry_diffusion 호출
  - REST 피드 주기(20초)마다 평가 → 진입 +30~60분에만 1회 동작

### 적용 범위
- theme_chasing만 새 reasons 필드 활용 (예시)
- 나머지 4개 KR 전략(momentum, sepa_trend, rsi2_reversal, gap_and_go)은 기존 reason: str 사용
  → effective_reasons() 폴백으로 backward compat
- 점진 적용 권장 (strategy-advisor): 1주일 shadow 데이터 누적 후 다른 전략도 reasons 표준화

### 보류된 항목 (4건)
- `docs/strategies/pending-decisions.md` 등록
- 트리거 조건: 전략별 ≥10건 + 5영업일 / Evolver 자동 평가 / 일평균 ±2% 5일

### 검증
- `python3 -m py_compile src/core/types.py src/core/engine.py src/core/evolution/trade_journal.py src/schedulers/kr_scheduler.py src/strategies/kr/theme_chasing.py` → ALL OK
- 백워드 호환: 기존 Signal(reason="...") 패턴 그대로 작동
- placeholder 차단 비활성 (shadow 모드)이라 정상 매매 흐름 영향 없음

---

## 2026-04-21 — kr_stock_master FDR 폴백 추가 + US WS 라이프사이클 개선

### 사고 로그
- 04-21 09:30 KR 장 개시 후 매수 0건 (스크리닝 후보 0개)
- WebSocket close_code=1006 무한 재연결 루프 (KR ↔ US WS approval_key 충돌)
- DB 직접 조회: kospi500_yn='Y' 1개, KOSDAQ 시총 1,000억 이상 0개 — `kr_stock_master` 손상
- → 모든 우량주가 "비우량/소형주"로 필터링되어 매수 차단

### 근본 원인
1. **stock_master 손상**: 04-20 18:00 일일 갱신 시 pykrx가 KRX_ID/KRX_PW 환경변수 미설정으로 시가총액 빈값 반환 → DB가 0으로 덮어써짐
2. **WS approval_key 충돌**: US WS가 after_hours 세션에 계속 연결되어 KR 장 개시 시 단일 approval_key를 두고 충돌 → 매 5초 close_code=1006

### 수정
- `src/data/storage/stock_master.py` `_sync_load_index_members`:
  - pykrx 시총 100건 미만 시 FDR(`KRX-MARCAP`) 자동 폴백 추가
  - KOSPI500/KOSDAQ150도 FDR 시총 상위로 폴백
  - KRX 인증 환경변수 의존성 제거
- `src/schedulers/us_scheduler.py` `ws_market_loop`:
  - KR 정규장 시간(KST 08:50~15:30) 진입 시 US WS 강제 종료 (포지션 유무 무관)
  - approval_key 충돌 사전 차단

### 즉시 조치 (사고 대응)
- FDR로 `kr_stock_master` 임시 재구축: KOSPI500 501개, KOSDAQ150 150개, market_cap>0 2,782개 복원
- 봇 재시작으로 WS 데드락 해소
- 10:35 매수 흐름 정상 복원 (SK하이닉스 첫 체결)

### 검증
- `python3 -m py_compile src/data/storage/stock_master.py src/schedulers/us_scheduler.py` → OK
- DB 복구 후 다음 스크리닝 사이클: 통합 후보 38개 정상 통과
- 자동 매수 1건 체결 (000660 SK하이닉스)

---

## 2026-04-20 — RiskManager._pending_sector_map 초기화 누락 버그 수정

### 사고 로그
`08:00:16 | ERROR | src.core.engine:on_order:1583 | [리스크] 주문 제출 오류: 017670 — 'RiskManager' object has no attribute '_pending_sector_map'`

### 근본 원인
- `_pending_sector_map`은 UnifiedEngine.__init__ (line 181)에만 초기화되어 있음
- 하지만 RiskManager의 `clear_pending()` (line 1552), `_on_order_failure` (line 1475, 1492)에서 참조
- 주문 제출 예외 시 `clear_pending()` 호출 경로에서 AttributeError 발생 → 오전 8:00 장 개시 시 주문 2건 실패

### 수정
- `src/core/engine.py` RiskManager.__init__ (line ~1031 부근): `self._pending_sector_map: Dict[str, str] = {}` 추가

### 검증
- `python3 -m py_compile src/core/engine.py` → OK
- 재시작 후 로그에 AttributeError 재현 없음

---

## 2026-04-20 — KR 보유 포지션 UI 개선 (심볼코드 숨김)

### 변경
- `src/dashboard/static/js/dashboard.js` (line 185): KR 포지션 행에서 종목코드(예: 071050) 숨김 — 종목명 + 전략 뱃지만 표시
- 국내 종목은 한글 종목명이 직관적이므로 6자리 코드 생략이 가독성에 유리
- US 포지션은 ticker(AAPL 등)가 primary 식별자이므로 유지

---

## 2026-04-20 — 코어홀딩 is_core 플래그 정합성 버그 수정 (SK텔레콤/KT/삼성생명 사고)

### 사고 요약
재시작 후 코어 3종목(017670 SK텔레콤, 030200 KT, 032830 삼성생명)이 ExitManager에서 is_core=False로 잘못 등록 → WS 첫 체결가 수신 시 일반 분할 익절 규칙 적용 → 연쇄 매도 발생.

**실현 내역**:
- 017670: 29주 전량 → +553,424원 (1차+2차+3차 연속 트리거)
- 030200: 7주 (09:00 1차) + 30주 KIS 계좌 잔존 의심 (재시작 후 portfolio 누락)
- 032830: 2주 (09:34 1차) + 8주 (MA5 복합트레일링 연쇄 전량) → +266,322원

### 근본 원인
1. **run_trader.py 초기화 순서 버그** (주요): 
   - Step 10 `exit_manager.register_position` 호출 시점에 `pos.strategy=None`
   - Step 11에서야 DB 연결 + `_restore_position_metadata` 실행
   - 결과: `is_core = (pos.strategy == "core_holding")` 체크가 False로 평가되어 일반 포지션으로 등록
2. **trade_storage 설계 부작용**: 1차 익절만으로도 `trades.exit_time`이 기록되어 잔여 수량 있는데도 "closed"로 분류 → `WHERE exit_time IS NULL` 쿼리에서 누락 → `_restore_position_metadata` 도 strategy 복원 실패
3. **register_position 조기 리턴**: 동일 심볼 재등록 시 early return → 후속 portfolio sync에서 올바른 is_core=True가 와도 반영 못함

### 수정 파일
- `scripts/run_trader.py` (line 601~624): 
  - 신규 Step 10-0 추가: ExitManager register_position **이전**에 DB 연결 + `_restore_position_metadata` 실행
  - Step 11의 DB 재-연결 로직은 fallback으로 단순화
- `src/strategies/exit_manager.py` (line 399~): 
  - 재등록 시 `is_core=True`가 오면 기존 False 상태 승격 + 관련 파라미터(SL/TS/ratio) 동기 복사 + `_persist_states()` 즉시 저장
  - 대시보드(pos.strategy)와 ExitManager(is_core) 두 진실 불일치 방지

### 사용자 결정 사항
- 현재 보유 중 코어 분류 포지션 모두 일반 포지션으로 재분류 (원복 없이 초기화)
- 기존 `core_holding` 매수 로직이 빈 슬롯(3개)을 다시 채우도록 허용

### 검증
- `python3 -m py_compile` scripts/run_trader.py, src/strategies/exit_manager.py → OK
- 재시작 로그 확인:
  - `[KR] DB 선-연결 + 포지션 전략 복원 완료 (ExitManager 등록 전)` (Step 10-0 작동)
  - `[KR] 포지션 전략 복원 (DB): 3개` 먼저 출력
  - `[KR] 기존 포지션 8개 ExitManager 등록 완료 (코어: 0개)` — 사용자 의도대로 코어 0 인식

### 추가 조사 필요
- `030200 KT` 누락 30주 재동기화 (KIS 실계좌 확인 필요 — HTS 수동 체크)
- trade_storage의 partial-sell exit_time 덮어쓰기 로직 개선 검토 (별건)

---

## 2026-04-19 — 당일 청산 누적 D+1 쿨다운 규칙 추가

### 배경
4/14 -8.42% 사고 — 단일일에 다수 청산 + 다수 신규 매수가 동시 발생해 SK하이닉스 저점 청산 후 +16% 반등을 놓침.
"청산 당일은 현금 유지, 다음 거래일에 신규 진입" 규칙(D+1 분리)을 도입해 저점 청산 직후 같은 자금으로 급하게 새 종목에 들어가는 패턴을 차단.

### 변경 파일
- `src/risk/manager.py`
  - `_daily_exit_count: int = 0`, `_daily_exit_count_date: Optional[date]`, `_last_exit_cooldown_log: Dict[str, datetime]` 필드 추가
  - `record_exit()` — 카운터 +1 + 날짜 롤오버 자동 리셋 + `[리스크] 당일 청산 누적: n/threshold` 로그
  - `can_open_position()` — 섹터 제한 뒤(8단계)에 `_daily_exit_count >= threshold` 차단 로직 추가 (기존 차단이 우선)
  - `reset_daily_stats()` — 날짜 변경 시 카운터 리셋 경로 보강
- `src/core/types.py` — `RiskConfig.daily_exit_cooldown_threshold: int = 3` (0이면 비활성 안전장치)
- `docs/risk/risk-and-exit.md` — "당일 청산 누적 쿨다운 (D+1 분리)" 섹션 신규

### 구현 포인트
- **호출점 재사용**: `record_exit()`는 이미 `kr_scheduler.py` fill_check SELL 체결 두 경로(1420, 1542 라인)에서 호출 중이라 신규 삽입 없음 — 리스크 최소
- **시그니처 호환성 유지**: `can_open_position()` 파라미터/반환값 변경 없음
- **스팸 방지**: 심볼별 60초 로그 쿨다운 (동기화 차단과 동일 패턴)
- **evolved_overrides 튜닝 가능**: YAML에서 `risk.daily_exit_cooldown_threshold` 조정 가능

### 검증
- `python3 -m py_compile` src/risk/manager.py, src/core/types.py → OK
- 단위 시나리오:
  - 3건 청산 후 4번째 매수 → `(False, "당일 청산 3건 누적, 다음 거래일 재개")` 차단 확인
  - threshold=0 → 4건 청산해도 차단 없음 확인
  - 날짜 롤오버 → `_daily_exit_count` 0으로 리셋 + 매수 허용 확인
- 기존 차단 로직(동기화/일일손실/포지션수)이 모두 신규 규칙보다 앞에 위치 → 회귀 없음

### 봇 재시작
- **미반영** — 사용자 일괄 배포 예정

---

## 2026-04-19 — ExitManager ATR 연동 트레일링 스탑 (매크로 노이즈 방어)

### 배경
SK하이닉스 4/13 일시 저점에서 고정 3% 트레일링에 조기 청산 → 4/14~ +16% 반등 누락.
고정 트레일링이 고변동 종목의 매크로 노이즈에 과민하게 반응하는 문제를 해결.

### 변경 파일
- `src/strategies/exit_manager.py` — ATR-linked 트레일링 계산 + effective_trailing_stop_pct 상태 저장
- `src/schedulers/kr_scheduler.py` — 체결/리트라이/sync 3개 register_position 호출에 `atr_pct_hint` 전달
- `docs/risk/risk-and-exit.md` — "ATR 연동 트레일링 (ATR-linked trailing)" 섹션 신규

### 구현 상세
- **ExitConfig 신규 필드**:
  - `enable_atr_linked_trailing: bool = True`
  - `atr_link_multiplier: float = 1.2`
  - `atr_link_cap_pct: float = 6.0`
- **PositionExitState 신규 필드**: `effective_trailing_stop_pct: Optional[float]`
- **공식**: `effective_ts = min( max(config_ts, ATR_pct × 1.2), 6.0 )`
  - 하한: REGIME/전략별 `trailing_stop_pct` 존중
  - 상한: 6.0% (손실 확대 방지)
- **register_position 신규 파라미터**: `atr_pct_hint: Optional[float] = None`
  - price_history 계산 실패/코어홀딩 케이스 대비 외부 hint 수용
  - hint 없으면 기존 방식 fallback (effective_trailing_stop_pct=None)
- **update_price 트레일링 블록**: breakeven 활성/비활성 양쪽 모두 `effective_trailing_stop_pct` 우선 사용, 로그에 "ATR-linked trailing" 표시
- **호출 경로 3곳 (kr_scheduler)**:
  - fill 체결 (1569) — `_pending_signal_cache[symbol].metadata.atr_pct` pop 없이 조회
  - retry (1744) — 동일 캐시에서 hint 추출
  - portfolio sync (542) — `_ep.get("atr_pct")` 시 전달 (현재 dict에 키 없으면 None, 확장 여지)

### 검증
- `python3 -m py_compile` src/strategies/exit_manager.py, src/schedulers/kr_scheduler.py → OK
- 단위 시나리오 6개 모두 통과:
  - ATR=5% → 6.0% (상한 도달)
  - ATR=2% → 3.0% (config 하한 유지, 기존 방식)
  - ATR=None → None (fallback)
  - ATR=10% → 6.0% (상한 clamp)
  - strategy_ts=4.0, ATR=2% → 4.0% (전략 하한 존중)
  - is_core=True → None (코어홀딩 제외)
- 봇 재시작은 후속 작업(현재 세션 커밋 단계로 보고)

### 기대 효과
- 고변동 종목(ATR 4%+)은 트레일링 자동 확대 → 일시 저점 노이즈 흡수
- 저변동 종목(ATR 2% 이하)은 기존 3%/전략별 값 유지 → 보수적 수익 보호 유지
- 상한 6%로 비정상 고ATR 종목에서 손실 확대 방지

## 2026-04-19 — 시장 체제 VIX 보조지표 (경량)

### 배경
4/8 이란 휴전 랠리(KOSPI +6.87%)에서 MA20 후행 판단으로 bear 유지 → 4월 α -15.51%p 미스매치.
VIX 급변동 지표를 보조로 도입하여 체제 전환 타이밍을 개선.

### 변경 파일
- `src/core/market_regime.py` — VIX 조회/캐시/조정 로직 추가 (신규 파일 없음)
- `docs/strategies/kr-strategies.md` — "시장 체제 판단 보조지표 (VIX 경량 패널)" 섹션 추가

### 구현 상세
- **신규 상수**: `_VIX_CACHE_PATH`, `_VIX_CACHE_TTL_SEC=21600`, `_VIX_FEAR_THRESHOLD=30.0`, `_VIX_COMPLACENCY_THRESHOLD=15.0`
- **신규 메서드**:
  - `_load_vix_cache_or_refresh()` — JSON 캐시 읽기, 만료 시 백그라운드 fetch 예약
  - `_fetch_vix()` (async) — `asyncio.to_thread`로 yfinance 동기 호출 래핑 + 캐시 저장
  - `_fetch_vix_sync()` (static) — `yf.Ticker("^VIX").history(period="2d")`
  - `_classify_vix()` — fear/complacency/normal 라벨링
  - `_apply_vix_adjustment()` — bull → sideways 강등 (Fear 시)
- **`update_regime()` 동작 변경**:
  - 진입 직후 `_load_vix_cache_or_refresh()` 호출
  - `confirm_delay_sec` 변수로 bull 확인 지연 조건부 단축 (complacency 시 1800→600초)
  - bear 전환 지연은 안전 우선으로 기존 1800초 유지
  - `_regime_data`에 `vix`, `vix_state` 필드 추가
- **실패 안전성**: 네트워크/yfinance 예외는 `logger.debug`만 기록, 기존 MA20 로직 그대로 fallback

### 검증
- `python3 -m py_compile src/core/market_regime.py` → OK
- yfinance 실제 VIX 조회 성공 (17.48 @ 2026-04-17)
- Fear 시뮬(VIX=35, bull) → sideways 강등 확인
- Complacency 시뮬(VIX=12) → 10분 후 bull 전환 확인
- 캐시 파일 `~/.cache/ai_trader/vix_cache.json` 자동 생성 확인

### 제약
- `REGIME_PARAMS` 테이블 자체는 미변경 (별도 Phase)
- 첫 봇 기동 시 캐시 부재 → 첫 호출은 VIX 미반영, 두 번째 호출부터 적용

## 2026-04-19 — KIS 동기화 복구 중 신규 매수 차단 강화 (trading_lock)

### 배경
- 과거 대형 손실 10건 중 **7건**이 동일 패턴: KIS API 일시 응답 지연 → 포트폴리오 동기화 복구 과정에서 비정상 상태로 신규 진입
- 대표 사례: 03-27 DB손해보험 -14%, SK하이닉스 -11.89%

### 변경
- **`src/risk/manager.py`**
  - 기존 `_sync_healthy` 차단 로직 활용 + 타임아웃 안전장치 추가
  - 신규 필드: `_sync_unhealthy_since` (차단 시작 시각), `_sync_timeout_minutes=10`, `_last_sync_block_log` (심볼별 로그 쿨다운)
  - `can_open_position()` 1.5단계: 차단 시 `"[리스크] 동기화 복구 중 신규 매수 차단 ({symbol})"` 로그 + 심볼별 60초 쿨다운
  - 10분 초과 차단 유지 시 **CRITICAL 로그 + 강제 해제** (운영 영구 블로킹 방지)
  - `set_sync_status()`: 복구 시 타임스탬프 초기화 + 지속 분 로깅
- **`docs/risk/risk-and-exit.md`**: trading_lock 섹션 상세화

### 영향 범위
- KR 전용 (US 스케줄러는 `set_sync_status` 미호출 — 영향 없음)
- 호출 경로: `kr_scheduler._sync_portfolio()` → `risk_manager.set_sync_status()` → `engine.on_signal → _risk_validator.can_open_position` 게이트
- 기존 차단 규칙 변경 없음, 로그 명확화 + 타임아웃 안전장치만 추가
- 수정 파일: `src/risk/manager.py`, `docs/risk/risk-and-exit.md`, `CHANGELOG.md`

## 2026-04-18 — 대시보드 + 모바일앱 UI/UX 전면 개선 (Phase A~F)

### Phase A: 웹 P0 5건 (트레이더 의사결정 즉시 향상)
- **P0-1 + Impact1**: 포지션 테이블 "청산단계" 컬럼을 projection 위젯으로 확장
  - `index.html`: `.stage-proj` / `.stage-proj-bar` / `.stage-proj-lbl` 스타일 추가
  - `dashboard.js:renderStageProjection()` 신규 — SL~nextTP 구간 미니바 + "SL +X%p / TP1 -X%p" 라벨
  - KR/US 포지션 테이블 양쪽 적용
- **P0-2**: 일일 손실 한도 게이지바 — 숫자 표시에 색상 게이지 추가
  - `.risk-gauge-track/fill` + `.gauge-green/amber/red` 스타일
  - JS: >90% 빨강, >60% 주황, 그 외 초록. aria-valuenow 라이브 업데이트
- **P0-3**: WS 구독 라벨 "0" → "N종목" 명확화 + 장중 0일 때 경고색
- **P0-4**: pnl fallback NaN 가드 — `_rawPnl` isFinite 체크 + console.warn
- **P0-5**: 전략 특수 뱃지 확장 — 코어/테마/갭/RSI2 각각 `.sb-core/sb-theme/sb-gap/sb-rsi2` 추가

### Phase B: 웹 P1 (효율성)
- **P1-1**: 정렬 활성 컬럼 시각 피드백 — `.sortable-th.asc/desc` 배경색 + 하단 inset 박스 섀도
- **P1-3**: 성과 페이지 탭 전환 로딩 UI — `_showPerfLoading()` 상단 shimmer 오버레이

### Phase C: 웹 P2 디자인 시스템 & 접근성
- `dashboard.css` **31 → 120줄** 확장
  - 디자인 토큰: `--fs-xs~2xl`, `--sp-1~5`, `--t-fast/med/slow`
  - 1024/768/640/380 반응형 4단계
  - `:focus-visible` 키보드 네비 outline
  - `@media (prefers-reduced-motion)` 감속 모드
  - `.sr-only` 스크린리더 전용 클래스
- `index.html` 게이지에 `role="progressbar" aria-label` 적용

### Phase D: 보안 XSS 감사
- `common.js:esc()` 강화 — &/</>/"/'//  모두 escape (속성 폭주 방지)
- `common.js:safeUrl()` 신규 — javascript:/data:/vbscript: 스킴 차단
- `themes.js` 뉴스 URL에 `safeUrl()` + `rel="noopener noreferrer"` 강화
- `dashboard.js` 신호 이벤트 렌더링 — ev.name/symbol/strategy/block_reason 전건 esc() 적용

### Phase E: Impact 기능 2·3
- **LLM Signal Rationale**: 신호로그 차단 사유 → tooltip 확장 + 게이트 번호 title
- **Regime Exit Timeline**: KR 마켓 카드에 "체제별 청산 규칙" 토글 테이블
  - bull/neutral/sideways/bear 4개 체제 × SL/TS/TP1~3 한눈에
  - 현재 체제 행 자동 하이라이트 (regime 변경 시 동기화)
  - "bear 전환 시 RSI2/momentum 전면 차단" 명시
  - 키보드 접근성(Enter/Space 토글)

### Phase F: 모바일앱 동등 개선 (`/home/user/projects/ai-trader-mobile`)
- `app/(tabs)/index.tsx`:
  - `RiskGauge`: 임계값을 웹과 동일 60/90% 로 통일 + 손실 텍스트 색상 단계
  - `STRATEGY_LABELS`: rsi2_reversal/strategic_swing/core_holding 추가
  - `STRATEGY_BADGE`: 코어/테마/갭/RSI2 특수 배지 4종
  - `getStageProjection()`: 웹 `renderStageProjection`과 동일 로직
  - `PositionCard`: 전략 배지 + 청산 projection 미니바 + SL/TP 거리 표시
  - `accessibilityRole="progressbar"` 추가
- `EXIT_STATE_COLORS`에 `first/second/third` 상태 추가 (웹 동등)

### 검증
- `python3 -m py_compile` 전체 OK (엔진 변경 없음 — 대시보드 정적 자산만)
- `systemctl restart qwq-ai-trader` → active, 에러 없음
- 모바일 `tsc --noEmit` — 기존 demo-data.ts 타입 오류만, 금번 수정 관련 오류 0건

---

## 2026-04-18 — 3인 합동 전체 검증(코드/전략/데이터/종목선정) P0~P2 18건 일괄 수정

7개 전문 에이전트(engine-monitor, risk-auditor, trade-analyst, market-analyst, strategy-advisor, param-optimizer, Explore) 병렬 리뷰 + 교차 검증 후 일괄 수정.

### P0 — 치명적 (5건)
- **P0-1**: `src/core/cross_validator.py:125` — `_bear_block` 튜플에 `rsi2_reversal`, `momentum_breakout` 추가. Connors 원전 RSI(2) 규칙(지수 약세 시 역추세 진입 금지) 준수. 2020-03/2022-09 폭락 시 칼떨어지는 칼 진입 무방비였음.
- **P0-2**: `config/evolved_overrides.yml` — 3회 중복 리밸런싱(2026-04-18 02:00~02:06)으로 rsi2 15→40%, sepa 35→10%까지 편향된 것을 rsi2=25%, sepa=25%로 롤백. 근거가 표본 1건(4/17 +4.75%)뿐이었음.
- **P0-3**: `src/schedulers/kr_scheduler.py:3744` — `last_rebalance_week`를 `~/.cache/ai_trader/last_rebalance.json`에 영속화. 재시작 시 같은 주 중복 실행 방지 가드.
- **P0-4**: `config/default.yml:62` — `strategy_allocation`에 `strategic_swing: 0.0` 키 추가. `engine.G5_budget` 게이트 통과에 필요한 키가 default에 없어서 evolved 머지 실패 시 신호 전량 차단 위험.
- **P0-5**: `src/signals/screener/swing_screener.py:78` — RSI2/SEPA 후보 중복 제거(dedupe). 동일 종목이 양쪽 통과 시 score 높은 전략만 유지. 중복 시 단일 종목에 50%+ 집중 노출 위험.

### P1 — 중요 (8건)
- **P1-1**: `src/risk/manager.py:452` — 일일 손실 % 분모를 `total_equity`에서 `initial_capital`로 통일. 대시보드 표시값과 차단 기준 불일치로 "표시값 -4.9%인데 이미 차단" 혼선 제거.
- **P1-2**: `src/risk/manager.py:686` — `reset_daily_stats()`에 `self._consecutive_losses = 0` 추가. 전일 4연패 상태가 익일 포지션 사이징에 잔존하는 문제 해결.
- **P1-3**: `src/core/cross_validator.py:235-243` — docstring "5회" → "10회"로 실제 값 정정 + fail-open 정책 의도 주석 추가.
- **P1-4**: `src/signals/sentiment/kr_theme_detector.py:95-105` — 테마 매핑 오류 정리. 034730(SK 지주)이 건설테마 잘못 매핑 → GS건설(006360) 교체. 에스원(012750) 방산 오탐 → 한화시스템(272210). 게임주를 인터넷/플랫폼 테마에서 분리. "인터넷/플랫폼/건설/자동차" THEME_KEYWORDS 추가.
- **P1-5**: `src/strategies/base.py:363` — 52주 고가 계산을 명시적 250영업일 슬라이스로 변경. 기존 `history` 전체 max 방식은 history 길이가 200일이면 "200일 신고가"로 오작동, SEPA Stage 2 판정 오차.
- **P1-6**: `src/signals/screener/us_screener.py:253` — 섹터 ETF 모멘텀 계산만 하고 점수 미반영이던 것을 종목 score에 ±10 보너스 적용. bear 국면에서 XLP/XLV 방어 섹터 가산, XLY/XLK 약세 감점.
- **P1-7**: `config/default.yml:355` — `earnings_drift.enabled: false`. EPS surprise / 매출 성장률 API 미연동 상태에서 갭+거래량 프록시만으로 운용하면 sell-the-news 위험 무방비.
- **P1-8**: `src/data/storage/trade_storage.py:137` — writer 큐 shutdown timeout을 큐 크기 기반 동적 산정(10~60초)으로 변경. 대량 청산 시 데이터 손실 방지.

### P2 — 개선 (4건)
- **P2-1**: `config/evolved_overrides.yml` — `theme_chasing.min_score` 57→65 롤백. low_frequency 룰 자동 하향이 `before_win_rate=0.0` 기록 버그로 평가 불가 상태였음.
- **P2-2**: `src/data/providers/kis_market_data.py:30-58` — 캐시 maxsize=2000 + 타임스탬프 기반 간이 LRU 추가. 메모리 무한 증가 방지.
- **P2-3**: `src/utils/config.py:109-110` — fallback 수수료를 FeeCalculator(FeeConfig) 단일 소스와 일치(0.000140527 / 0.002130527). 설정 파싱 실패 시 수수료 이중 기준 방지.
- **P2-4**: `src/core/cross_validator.py:98-108,177-184` — 규칙1(RSI>70)과 규칙7(MA200 하방) 감점을 -10 → -5로 축소. 스크리너와 이중 감점 폭을 -20 → -15로 완화. 규칙1은 bull 체제 시 감점 생략.

### 검증
- `python3 -m py_compile` 전체 OK
- `systemctl restart qwq-ai-trader` → active, 31개 태스크 기동, 에러 없음
- US 시장 체제 neutral → bull 갱신, 테마 탐지 뉴스 76건 수집, 업종지수 조회 38개 정상

---

## 2026-04-15 — 리밸런싱 DB 기반 전환 + JSON flush 즉시 저장

### 수정 1: 리밸런싱 DB 동기화
- `src/core/evolution/trade_journal.py` — `sync_from_db()` 비동기 메서드 추가: DB `trades` 테이블에서 JSON에 누락된 거래 기록을 `_trades` dict에 보강
- `src/core/evolution/trade_journal.py` — `_row_to_trade_record()`, `_async_fetch_trade()`, `_recover_trade_from_db_sync()`, `_fetch_trade_from_db()` 헬퍼 추가
- `src/core/evolution/strategy_evolver.py` — `rebalance_strategy_allocation()` 시작 시 `await self.journal.sync_from_db(days=7)` 호출 추가

### 수정 2: record_exit 폴백 레코드 생성
- `src/core/evolution/trade_journal.py` — `record_exit()`에 optional 파라미터 `symbol`, `name`, `entry_price`, `entry_strategy` 추가
- 메모리에 trade_id가 없을 때: (1) DB에서 복원 시도 → (2) symbol 제공 시 최소 레코드 생성 → (3) 둘 다 실패 시 기존 None 반환
- `src/data/storage/trade_storage.py` — `record_exit()`에 동일 optional 파라미터 추가, `_journal.record_exit()`로 전달

---

## 2026-04-15 — P1/P2 엔진·스케줄러·브로커 계열 수정

### P1-1: `_pending_sector_map` clear_pending에서 정리 누락
- `src/core/engine.py` — `clear_pending()`에 `self._pending_sector_map.pop(symbol, None)` 추가

### P1-2: US `_sync_portfolio` Lock 미사용
- `src/schedulers/us_scheduler.py` — `_portfolio_lock` (asyncio.Lock) 추가, 포지션 제거 블록을 Lock으로 보호

### P1-3: US `_execute_exit` setattr 동적 속성 → Dict 전환
- `src/schedulers/us_scheduler.py` — `_sell_fail_counts: Dict[str, int]` 추가, `setattr/getattr/delattr/hasattr` → dict 접근으로 전환

### P1-4: `_USEngineBundle` running 플래그 이중관리 해소
- `scripts/run_trader.py` — `_running` 제거, `running` 단일 플래그만 사용

### P1-5: 이벤트 큐 포화 시 중요 이벤트 보존
- `src/core/engine.py` — `_purge_queue()` 메서드 추가, FILL/ORDER 이벤트는 큐 정리 시 절대 폐기하지 않음

### P2-1: 시그널 핸들러 중복 등록 제거
- `src/core/engine.py` — `_setup_signal_handlers()` 메서드 및 `import signal` 제거 (봇이 자체 핸들러로 덮어씀)

### P2-2: `_get_current_session()` 함수 내 import 의도 주석
- `src/core/engine.py` — 지연 임포트(순환 참조 방지) 의도 주석 추가

### P2-3: KIS US `available_cash=0` 처리 — P0-7에서 수정 완료 확인

### P2-4: `_log_sig` 연속 실패 경고 + or 패턴 수정
- `src/core/engine.py` — `event.score or 0` / `adjusted_score or event.score or 0` → None 체크 분리, 연속 10회 실패 시 WARNING 로그

### P2-5: `_screening_signal_cooldown` 크기 방어
- `src/schedulers/kr_scheduler.py` — 500 초과 시 최신 300건만 보존하는 방어 로직 추가

---

## 2026-04-15 — P1/P2 전략·청산·진화·설정 계열 수정

### P1-A: ExitManager FIRST stage 본전보호 버퍼 조정
- `src/strategies/exit_manager.py` — `sell_fee_buffer = -1.5` → `-0.5` (first_exit_ratio=0.2 기준 순손실 방지)

### P1-B: ExitManager first_exit_ratio 주석 보정
- `src/strategies/exit_manager.py:145` — 주석을 evolved_overrides에서 0.2로 오버라이드되는 것을 명시

### P1-C: gap_and_go ATR 가드 순서 수정
- `src/strategies/kr/gap_and_go.py` — ATR 가드를 "진입 신호" 로그 출력 전으로 이동 (모니터링 혼선 방지)

### P1-D: ExitManager eod_close 필드 주석 보강
- `src/strategies/exit_manager.py:192` — ExitManager 내부 미사용, us_scheduler가 직접 처리함을 주석에 명시

### P1-E: trade_memory Layer 2 태그 오류 수정
- `src/core/evolution/trade_memory.py:171` — `foreign_net_buy > 0`일 때 "기관매수" → "외국인매수" 정정

### P2-1: 어닝스 드리프트 target 하드코딩 제거
- `src/strategies/us/earnings_drift.py` — `close * 1.15` → `close * (1 + self.take_profit_pct / 100)` (config 참조)

### P2-2: US SEPA rs_val 중복 조회 제거
- `src/strategies/us/sepa_trend.py:139` — 2번째 `rs_val = indicators.get('rs_rating')` 제거, 첫 번째 결과 재사용

### P2-3: 테마 대형주 심볼 하드코딩 주석 보강
- `src/strategies/kr/theme_chasing.py:259` — 정적 목록 + 주기적 갱신 필요 + 종목명 주석 추가

### P2-4: 크로스검증 최소 점수 상수화
- `src/core/cross_validator.py` — 하드코딩 50 → `_MIN_PASS_SCORE = 50` 클래스 상수화

### P2-5: KR SEPA 스코어 문서 보정
- `docs/strategies/kr-strategies.md` — "100점 만점" → "100점 만점, overlay 포함 후 100점 클램핑" 명시

### P2-6: CLAUDE.md 청산 관리 설명 정확화
- `CLAUDE.md` — 2차 +15%, 3차 +25%, ATR 범위 3.5~8% 등 evolved_overrides 실제값 반영

---

## 2026-04-15 — P0 치명적 이슈 7건 수정

### P0-1: ExitManager restore_stages() NameError 수정
- `src/strategies/exit_manager.py` — `restore_stages()`에서 지역변수 `stage_order` 참조 → `self.STAGE_ORDER`로 수정

### P0-2: 수수료 하드코딩 제거 (kr_scheduler.py)
- `src/schedulers/kr_scheduler.py` — 하드코딩 수수료율(0.000131, 0.002, 0.000141) → `FeeCalculator.calculate_net_pnl()` 사용

### P0-3: `or` 패턴 위반 3곳 수정
- `src/strategies/kr/sepa_trend.py:68` — `supply_data_age or 0` → `None` 체크 분리
- `src/strategies/kr/rsi2_reversal.py:131` — `vcp_score or overlay_bonus or 0` → `None` 체크 체인
- `src/strategies/kr/theme_chasing.py:244` — `high or stck_hgpr or 0` → `None` 체크 체인

### P0-4: position_multiplier 이중 적용 확인
- 확인 완료: kr_scheduler에서 metadata에 설정만 하고, engine._calculate_position_size에서만 적용 → 이중 적용 없음

### P0-5: fill_check ExitManager 미등록 종목 재시도
- `src/schedulers/kr_scheduler.py` — `_pending_exit_registrations` set 추가, 포지션 대기 실패 시 다음 fill_check 주기에 재시도
- 경고 로그 레벨 WARNING → ERROR 상향

### P0-6: is_kr_market_holiday 동적+FALLBACK 이중 체크 제거
- `src/core/engine.py` — 동적 데이터 있으면 동적만 신뢰, 없을 때만 FALLBACK 사용

### P0-7: KIS US available_cash=0 시 None 반환 수정
- `src/execution/broker/kis_us.py` — `available_cash > 0` → `available_cash >= 0` (0원 정상 처리)

## 2026-04-17 — Phase 3: P1 하위 5건 + P2 주요 8건 수정

### P1-A: cooldown dict 무한 증가 방지
- `src/core/engine.py` — `_order_fail_cooldown`, `_last_signal_time` dict 크기 500 초과 시 일괄 정리 가드 추가

### P1-B: sector_map 고아 정리
- `src/core/engine.py` — 주문 거절(can_trade=False) 시 `_pending_sector_map.pop()` 추가 (2곳: 리스크 검증 + can_open_position)

### P1-C: run_trader.py finally에서 task cancel
- `scripts/run_trader.py` — finally 블록에서 모든 tasks cancel + await 처리 추가

### P1-D: strategic_swing ATR 가드 추가
- `src/core/batch_analyzer.py` — `_generate_strategic_signals()`에서 ATR=0/None 시 continue 가드 추가

### P1-E: 진화 param_bounds 범위 조정
- `src/core/evolution/strategy_evolver.py` — `min_score: (30,90)→(40,85)`, `max_atr_pct: (3.0,15.0)→(3.0,8.0)`

### P2-1: fire-and-forget task 예외 처리
- `src/core/engine.py` — `_log_sig()` create_task에 done_callback 추가 (unhandled exception 경고 방지)

### P2-2: 테마 max_change_pct 하드코딩 제거
- `src/strategies/kr/theme_chasing.py` — `min(..., 7.0)` → config 값 그대로 사용

### P2-3: CLAUDE.md 문서 오류 수정
- US 최대 포지션 수 4개→10개, 평가 기간 3영업일+5건→5영업일+10건, 1차 익절 30%→20%

### P2-4: config_persistence note 필드 저장
- `src/core/evolution/config_persistence.py` — `save_override()`에 `note` 파라미터 추가 (선택적, _meta에 저장)

### 문서 업데이트
- `CLAUDE.md` — 최종 업데이트 2026-04-17, US 리스크/진화/청산 실제값 반영
- `docs/evolution/evolution-system.md` — param_bounds min_score max 90→85 반영
- `docs/risk/risk-and-exit.md` — 갱신일 업데이트
- `docs/architecture/system-overview.md` — 갱신일 업데이트

---

## 2026-04-15 — P1 상위 중요 이슈 7건 수정

### P1-1: KR SEPA R/R 기준 통일
- `src/strategies/kr/sepa_trend.py` — `min_rr=1.5` → `min_rr=2.0` (US SEPA와 동일 기준)

### P1-2: RSI2 급락 감점 추가
- `src/strategies/kr/rsi2_reversal.py` — `change_5d < -15%` 시 -5점 감점 (추세 붕괴 위험)

### P1-3: CrossValidator 규칙2 — sepa_trend 수급 감점
- `src/core/cross_validator.py` — 기관+외국인 동시 순매도 시 sepa_trend는 차단 대신 -10점 감점 (배치 T+1 특성 반영)

### P1-4: US SEPA RS Rating 진입 차단
- `src/strategies/us/sepa_trend.py` — `rs_rating < min_rs_rating(70)` 시 return None (기존 감점 → 완전 차단)

### P1-5: 진화 잠금에 stop_loss_pct 추가
- `src/core/evolution/strategy_evolver.py` — `_locked_params`에 `stop_loss_pct` 추가

### P1-6: RSI2/Strategic Swing 3차 익절 상향
- `scripts/run_trader.py` — `rsi2_reversal` + `strategic_swing`의 `third_exit_pct: 12.0` → `20.0`

### P1-7: gap_and_go stop_loss 통일
- `config/evolved_overrides.yml` — `gap_and_go.stop_loss_pct: 2.5` → `3.5` (min_stop_pct와 일치)

### 문서 업데이트
- `docs/strategies/kr-strategies.md` — RSI2 5일 하락 점수 기준 변경 반영
- `docs/strategies/us-strategies.md` — US SEPA RS Rating 차단 기준 반영
- `docs/evolution/evolution-system.md` — 잠금 파라미터 목록 추가 (stop_loss_pct 포함)
- `docs/risk/risk-and-exit.md` — 크로스검증 규칙2 sepa_trend 감점 처리 반영

## 2026-04-15 — US 전략 3개 P0 진입 로직 강화

### P0-5: US SEPA MA200 데이터 부족 시 자동 통과 차단
- `src/strategies/us/sepa_trend.py` — MA200 상향 판정 시 데이터 220봉 미만이면 `sepa_pass += 1` (자동 통과) 제거
- 데이터 부족 시 기준 미통과로 처리 + debug 로그 추가

### P0-6: US 어닝스 드리프트 — 프록시 기반 명시 + 필터 강화
- `src/strategies/us/earnings_drift.py`
  - 클래스 docstring에 "현재 버전: 갭+거래량 프록시 기반, 실적 확인 API 미연동" 명시
  - `generate_signal()` 상단에 1회성 debug 경고 로그 추가
  - `min_gap_pct` 기본값 5.0% → 7.0% (일반 뉴스 갭 필터링 강화)
  - `min_volume_surge` 기본값 신설 3.5x (기존 하드코딩 2.5x → 설정 기반 3.5x)
- `config/default.yml` — US earnings_drift 섹션 `min_gap_pct: 7.0`, `min_volume_surge: 3.5` 반영

### P0-7: US 모멘텀 min_breakout_pct / volume_surge_ratio 상향
- `src/strategies/us/momentum.py` — 기본값 `min_breakout_pct` 0.8 → 2.0, `volume_surge_ratio` 2.0 → 2.5
- `config/default.yml` — US momentum 섹션 `min_breakout_pct: 2.0`, `volume_surge_ratio: 2.5` 반영

### 문서 업데이트
- `docs/strategies/us-strategies.md` — 3개 전략 변경사항 반영

## 2026-04-15 — P0 버그 수정: 섹터 하드코딩 + 진화 전략 미구분

### P0-11: cross_validator 섹터 집중도 하드코딩 → 설정 참조
- `src/core/cross_validator.py` — `same_sector_count >= 3` 하드코딩을 `self._max_sector_positions`로 교체
- `__init__`에 `max_sector_positions` 파라미터 추가 (기본값 2)
- `src/core/engine.py` — KR 엔진 호출 시 `config.max_positions_per_sector` 전달
- `scripts/run_trader.py` — US 엔진 호출 시 `trading_config.risk.max_positions_per_sector` 전달
- KR=2, US=3 설정값이 정상 적용됨

### P0-12: 진화 low_frequency 규칙 전략 미구분 수정
- `src/core/evolution/strategy_evolver.py` — `_find_triggered_rule()`에서 low_frequency 규칙 트리거 시 `review.strategy_performance` 활용
- `_narrow_targets_by_lowest_trades()` 메서드 추가: 와일드카드 `*.min_score` 타겟 중 거래가 가장 적은 전략만 선택
- 기존 문제: sepa 거래 부족인데 theme_chasing.min_score가 변경되는 현상 해결

## 2026-04-16 — 코어홀딩 초과 비중 관리 시스템

### P0: 초과 비중 감지 + 텔레그램 경고
- `src/schedulers/kr_scheduler.py` — 코어 비중 >= 35% 시 텔레그램 경고 (24시간 쿨다운)
- 종목별 평가금/비중/수익률 상세 포함

### P1: 비코어 pool_equity 보호
- `src/core/engine.py` — 코어 30% 초과 시 비코어 pool_equity에서 코어 실점유분 차감
- `_get_core_actual_value()` 메서드 추가
- 코어 39% 점유 → 비코어 pool = equity의 61% (기존: 100%)

### P1-2: 주간 트림 (부분 익절)
- `src/schedulers/kr_scheduler.py` — 매주 금요일 14:00 실행
- 코어 비중 >= 40% → 초과분의 50% 트림
- 개별 종목 >= 20% → max_position_pct(15%)까지 축소
- 가장 많이 오른 종목부터 부분 매도 (metadata.quantity로 수량 전달)
- 최소 트림 20만원 (수수료 대비), rebalance_exclude 종목 제외

### P2: config 파라미터 확장
- `config/default.yml` — overweight_alert_pct, trim_threshold_pct, trim_ratio, trim_min_value, individual_max_pct 추가

### 리뷰 수정 (P0×2, P2×3)
- P0-1: trim_qty → quantity 키 변경 (전량 매도 방지)
- P0-2: _send_telegram → send_alert 교체 (미존재 메서드)
- P2-1: 루프 내 불필요 import 제거
- P2-2: 하드코딩 비율(30%, 10%, 15%) → config 값 참조
- P2-4: _get_core_actual_value() 이중 호출 제거

### 수정 파일
- `src/core/engine.py` — pool_equity 보호 + _get_core_actual_value()
- `src/schedulers/kr_scheduler.py` — 경고 + 트림 로직
- `config/default.yml` — 코어 초과 비중 파라미터 5개

## 2026-04-15 — evolve() 호출 경로 복원 + 4/7~4/15 복기 기반 10대 개선

### evolve() 자동 호출 복원
- `src/schedulers/kr_scheduler.py` — 20:30 LLM 복기 직후 `strategy_evolver.evolve(days=7)` 호출 추가
- CLAUDE.md 설계("TradeReviewer → DailyReviewer → StrategyEvolver")대로 경로 복원
- 기존 가드레일 유지: 1개 파라미터/5영업일+10건 평가/악화 시 즉시 롤백
- 이전: LLM이 7일간 max_atr_pct를 반복 권고했으나 evolve() 미호출로 자동 반영 불가

## 2026-04-15 — 4/7~4/15 복기 기반 10대 개선

### P0: 즉시 조치 (3건)

#### SEPA max_atr_pct 가드 추가
- `src/strategies/kr/sepa_trend.py` — ATR 6% 초과 종목 진입 차단
- 기간 손실 Top 5 중 4건이 ATR 6%+ 종목 (LIG넥스원, 후성, KEC 등)
- `self.config.params.get("max_atr_pct", 6.0)` — 설정 파일로 조정 가능

#### Theme max_atr_pct 5.5 + min_change_pct 2.5%
- `src/strategies/kr/theme_chasing.py` — max_atr_pct 기본값 8.0→5.5
- `config/evolved_overrides.yml` — max_atr_pct: 5.5, min_change_pct: 2.5
- `config/default.yml` — 동기화

#### 장초반 진입 금지 30분 확대
- `config/default.yml` — batch execute_time: "09:01"→"09:30"
- `config/evolved_overrides.yml` — theme trading_start_time: "09:10"→"09:30"
- `src/strategies/kr/theme_chasing.py` — trading_start_time 기본값 "09:05"→"09:30"
- 4/7~4/8 장초반 15분 이내 4건 진입 모두 손절 (승률 0%)

### P1: 중요 개선 (4건)

#### 동일 섹터 추가 진입 경고 로그
- `src/core/engine.py` — 동일 섹터 2번째 진입 시 WARNING 로그 추가
- 기존 max_positions_per_sector=2 제한은 이미 구현됨

#### RSI2 max_atr_pct 8.0 가드 추가
- `src/strategies/kr/rsi2_reversal.py` — ATR 8% 초과 종목 역추세 진입 차단
- KEC(ATR 15.73%) 손절 -5.14% 사례 방지

#### 1차 익절 비율 (관찰)
- first_exit_ratio 이미 0.2(20%)로 축소됨
- PF 문제의 근본 원인은 고ATR 진입 → P0 ATR 가드로 해결 예상

### P2: 관찰/버그 수정 (3건)

#### equity 스냅샷 비정상 데이터 가드
- `src/analytics/equity_tracker.py` — 재시작 직후 동기화 전 상태 감지 시 저장 스킵
- 4/13 equity 파일 오류 원인: 봇 재시작 후 포트폴리오 미동기화 상태에서 backfill 실행

#### US sync 미청산 레코드
- 5건 존재하나 exit_time 기반 필터링으로 이미 무시됨 → 정리 불필요

#### 진화 시스템 max_atr_pct 자동 진화 지원
- `src/core/evolution/strategy_evolver.py` — _param_bounds에 max_atr_pct, min_change_pct, min_volume_ratio 등록
- 기존: _param_bounds 미등록 → LLM 권고 파라미터 자동 적용 불가

### 수정 파일
- `src/strategies/kr/sepa_trend.py` — max_atr_pct 가드
- `src/strategies/kr/theme_chasing.py` — max_atr_pct 5.5, trading_start_time 09:30
- `src/strategies/kr/rsi2_reversal.py` — max_atr_pct 가드
- `src/core/engine.py` — 섹터 중복 경고 로그
- `src/analytics/equity_tracker.py` — 비정상 데이터 가드
- `src/core/evolution/strategy_evolver.py` — _param_bounds 확장
- `config/default.yml` — execute_time 09:30, theme max_atr_pct 5.5
- `config/evolved_overrides.yml` — theme 파라미터 업데이트

## 2026-04-06 — Trade Wiki 시스템 (Karpathy LLM Wiki 패턴)

### Trade Wiki 구현 (22dad71, 6d0b301)
- **Karpathy LLM Wiki 패턴** 적용 — 거래 교훈을 전략/섹터/시장체제별 마크다운 위키로 축적
- `src/core/evolution/trade_wiki.py` 신규 (TradeWiki 클래스, ~350줄)
- 3가지 오퍼레이션:
  - **Ingest**: 매도 체결 → 전략/섹터/체제 위키 3~5개 페이지 자동 업데이트 + LLM 교훈 추출 (Gemini Flash)
  - **Query**: 크로스검증 시 관련 위키 교훈 컨텍스트 반환 (파일 읽기, <1ms)
  - **Lint**: 주간(토요일) 헬스체크 — stale 페이지, 저조 승률 감지
- 위키 구조: `~/.cache/ai_trader/wiki/{strategies,sectors,regimes}/*.md` + `index.md` + `log.md`
- 동시성: `asyncio.Lock` 보호, fire-and-forget (매매 비차단)
- 통합: KR/US 양쪽 SELL 체결 시 ingest, LLM 이중검증 프롬프트에 wiki 교훈 주입

### 리뷰 수정 (6d0b301)
- P0: LLM `generate()` → `complete()` + `resp.content` 접근
- P0: US 엔진에 TradeWiki 인스턴스 전달 (run_trader.py)
- P1: 비테이블(교훈) 섹션 max_rows 불릿 행 인식
- P1: `asyncio.Lock` 동시 ingest 방지
- P1: ingest 메서드 들여쓰기 정합성

### 최종 종합 리뷰 PASS
- P0: 0건 / P1: 0건 / P2: 3건 (경미, 기능 무해)
- 13개 검증 항목 전체 PASS (wiki 경로, import, ATR 가드, 배분 합계 등)

## 2026-04-04 — LLM 복기 반영 + 종합 리뷰 수정 + strategic_swing 승격

### LLM 복기 반영 (a073c03)
- ATR 동적 손절 범위 확대: `max_stop_pct` 6→8% (ATR 6%+ 종목 조기 손절 방지)
- 1차 익절 비율 축소: `first_exit_ratio` 30→20% (수익 거래 80% 추세 추종 잔류)
- theme_chasing 재설계: 급등률 상한 7%, 14:00+ 진입 차단, +5% 급등 시 눌림 1%+ 필수

### strategic_swing 정식 승격 (039ad2d)
- `_VALID_STRATEGIES`에 추가, 예산 10% 배분 (sepa 25%, rsi2 25%, core 30%)
- 복합 시그널(2계층+) 기반 고conviction 진입, 7건 57.1%/+2.50% 최고 성과

### RLAY 무한 루프 수정 (039ad2d)
- 매도 수량 > 실제 보유 → 자동 클램핑
- 연속 3회 실패 → 포트폴리오 동기화 강제 + 카운터 리셋

### 배치 indicators 누락 수정 (039ad2d, d4a5f6c)
- execute_pending_signals에서 스크리너 캐시 indicators 주입
- LLM 이중검증 "지표 비어있어 거부" → 정상 검증 가능
- position_multiplier 배치 경로 재계산 주입

### 종합 리뷰 수정 (ea56d5d, d4a5f6c, 0af09ec)
- P0: US sepa_trend logger 미임포트, 시장체제 항상 sideways 고정, theme_chasing high키 폴백
- P1: US 크로스검증 빈 indicators → indicator_cache 주입
- P1: US 시장체제 PARAMS 실제 적용 (min_score_adj, max_buys, position_mult_boost)
- P1: RSI2 데드코드, or 0 패턴, 매도실패 카운터 정리

## 2026-04-02 — US 엔진 고도화: KR 엔진 3대 기능 이식

### Phase 1: ATR 기반 포지션 사이징
- US 3개 전략(모멘텀, SEPA, 어닝스드리프트)에 `atr_position_multiplier` 적용
- ATR=0/None 시 모멘텀/SEPA 진입 차단 (데이터 품질 가드)
- `_process_signal()`에서 `position_multiplier` 메타데이터 읽어 수량 조정
- 고점수(85+) 배율 완화: min 0.75x 보장

### Phase 2: 시장 체제 인식 (SPY/QQQ 기반)
- `src/core/us_market_regime.py` 신규 생성
- SPY 60% + QQQ 40% 가중 평균 등락률 기반 bull/bear/sideways/neutral 판단
- 임계값: US 시장 특성 반영 (bull > +0.7%, bear < -0.7%)
- 체제별 파라미터: min_score_adj, max_daily_new_buys, position_mult_boost
- heartbeat_loop(5분)에서 Yahoo Finance로 SPY/QQQ 데이터 갱신

### Phase 3: 크로스 검증 게이트
- `CrossStrategyValidator`에 `market="US"` 파라미터 추가
- US 적용 규칙 6개: RSI과매수, 약세장차단(모멘텀만), 섹터과집중, 추격매수, MA200하방, 밸류에이션
- US 제외 규칙 3개: 수급(데이터없음), 동일섹터손절(전면차단으로 불필요), 거래메모리(미구현)
- US bear 시 earnings_drift는 허용 (어닝 서프라이즈 특성)
- `_process_signal()`에 검증 게이트 삽입 (포지션 사이징 전)

### 수정 파일
- `src/core/us_market_regime.py` — 신규: US 시장 체제 판단
- `src/core/cross_validator.py` — market 파라미터 + US 규칙 분기
- `src/strategies/us/momentum.py` — ATR 사이징 + ATR=0 가드
- `src/strategies/us/sepa_trend.py` — ATR 사이징 + ATR=0 가드
- `src/strategies/us/earnings_drift.py` — ATR 사이징
- `src/schedulers/us_scheduler.py` — 크로스검증 게이트 + ATR 사이징 적용 + 시장체제 업데이트
- `scripts/run_trader.py` — _USEngineBundle에 market_regime, cross_validator 추가

## 2026-04-01 — SEPA 복기 기반 5대 회피 패턴 차단

### ATR 데이터 품질 가드
- `atr_14=0.0` 또는 `None` 시 SEPA 진입 차단 (기존: 기본값 5% 적용 → 위험)
- 변동성 지표 누락 상태에서 정상 검증 없이 진입하는 패턴 원천 방지

### 종목 리더십(MRS) 검증 강화
- MRS < 0 (종목 RS 음수) → -5점 감점 (기존: 감점 없음)
- 섹터 강세만 보고 RS 낮은 종목 편입 방지

### MA50 최소 거리 요구
- 가격이 MA50 대비 +2% 미만 → -5점 감점
- 애매한 추세(MA50 겨우 상회) 진입 패턴 차단

### 거래량 최소 게이트
- vol_ratio 1.0~1.2 구간 보너스 제거 (기존 +4점 → 0점)
- vol_ratio < 0.8 → -5점 감점 (거래량 부족 시 돌파 확인 불가)

### 장중 후반(14:30+) 진입 차단
- 14:30 이후 SEPA 신규 시그널 생성 차단
- 익일 장초반 갭 손절 노출 방지 (오버나이트 갭 리스크)

### LLM 복기 반영 (추가 4건)
- **LLM 이중검증 한도 확대**: 5회 → 10회/일 (`cross_validator.py`)
- **RSI2/Gap&Go ATR=0 가드**: 전 전략 통일 (기존 SEPA만 적용)
- **RSI2 비중 확대**: 10% → 15% (SEPA 45% → 40%) — 과매도 반전 기회 포착 강화
- **고점수 포지션 사이징 완화**: 80+ 최소 0.65배, 85+ 0.75배, 90+ 0.85배 보장

### 수정 파일
- `src/strategies/kr/sepa_trend.py` — 5대 회피 패턴 + 포지션 사이징 완화
- `src/strategies/kr/rsi2_reversal.py` — ATR=0 진입 차단
- `src/strategies/kr/gap_and_go.py` — ATR=0 진입 차단
- `src/core/cross_validator.py` — LLM 한도 5→10회
- `config/evolved_overrides.yml` — RSI2 15%, SEPA 40%

## 2026-03-31 — 대시보드 개선 5~6: 벤치마크 비교 + 전략 카드

### 성과 차트: 포트폴리오 vs KOSPI 벤치마크
- **벤치마크 비교 차트** 추가 (`performance.html`, `performance.js`)
  - 포트폴리오 누적 수익률 vs KOSPI 누적 수익률 시계열 오버레이
  - Alpha(초과수익) 자동 계산 + 헤더에 색상 표시
  - 기간 선택 연동 (1주/1개월/3개월/전체)
- **벤치마크 API** 추가 (`kr_api.py`)
  - `/api/benchmark?days=N` — KOSPI 일별 종가 (Yahoo Finance, 10분 캐시)

### 전략별 성과 카드
- **전략 카드 그리드** 추가 (`performance.html`, `performance.js`)
  - 전략별 승률 프로그레스 바 + 평균 수익률 + 총 손익 + 승/패
  - 전략 컬러 코딩 (SEPA=green, 테마=amber, RSI2=red 등)
  - 자동 레이아웃 (auto-fill, 최소 220px)

### 모바일 반응형 개선 (개선 7)
- **responsive.css v5**: 3단계 브레이크포인트 (768px/480px/360px)
- **차트 높이 축소**: 태블릿 220px, 폰 180px (기존 320px)
- **카드 패딩 축소**: 태블릿 16px, 폰 12px (기존 24px)
- **테이블 컬럼 자동 숨기기**: 성과 일별 테이블(현금/포지션), KR/US 비교(변동액)
  - `:has()` 셀렉터 활용 (Chrome 105+)
- **전략 카드 반응형**: 태블릿 170px, 폰 2열, 초소형 1열
- **티커 스트립 축소**: 폰에서 .5rem 글씨, 패딩 축소
- **입력/버튼 축소**: date input, filter-tab, btn-primary 폰 사이즈 최적화
- **CSS 버전 v=5**: 전체 HTML 템플릿 캐시 갱신

### 수정 파일
- `src/dashboard/kr_api.py` — `/api/benchmark` 엔드포인트 + 10분 캐시
- `src/dashboard/templates/performance.html` — 벤치마크 차트 카드 + 전략 카드 컨테이너
- `src/dashboard/static/js/performance.js` — `fetchBenchmark`, `renderBenchmarkChart`, `renderStrategyCards`
- `src/dashboard/static/css/responsive.css` — Mobile Enhancement v5 (3단계 브레이크포인트)
- `src/dashboard/templates/*.html` — CSS 버전 v=5 갱신 (7개 파일)

### AI 판단 로그 (개선 8)
- **엔진 페이지에 AI 판단 섹션** 추가 (`engine.html`, `engine.js`)
  - 크로스 검증 현황 (통과/차단/감점 + 통과율 게이지)
  - 시장 체제 + LLM 장전 진단 (bull/bear/sideways + 진단 텍스트)
  - 활성 거래 원칙 목록 (L1/L2/L3 건수 + delta/confidence)
  - 60초 자동 갱신

### 거래 일지 (개선 9)
- **거래 페이지에 Daily Review 카드** 추가 (`trades.html`, `trades.js`)
  - 날짜 선택 연동 — 해당 날짜의 AI 복기 자동 로드
  - 성공 패턴, 실패 패턴, 교훈 구조화 표시
  - `/api/daily-review` 엔드포인트 활용

### 전략 구성 히트맵 (개선 10)
- **성과 페이지에 Plotly Treemap** 추가 (`performance.html`, `performance.js`)
  - 전략별 거래 수 기반 면적 + 평균 수익률 기반 색상 (적/녹)
  - 전략명, 거래수, 승률, 평균 수익률 호버 표시
  - 기간 선택 탭 연동

### 알림 설정 (개선 11)
- **설정 페이지에 알림 설정 카드** 추가 (`settings.html`)
  - 텔레그램 연결 상태, 일일 손실 알림 한도, 최대 거래 알림
  - 매수/매도 체결 알림, 장전 LLM 진단, 주간 원칙 리포트 스케줄 표시

### 코드 리뷰 수정
- `common.js` 캐시 버전 v=5 전체 통일 (evolution, themes, settlement, settings, index)
- `engine.js` 중복 `esc()` 함수 제거 (common.js 전역 함수 사용)

### 전체 수정 파일
- `src/dashboard/kr_api.py` — `/api/benchmark` 벤치마크 API
- `src/dashboard/templates/performance.html` — 벤치마크 + 전략카드 + 트리맵
- `src/dashboard/templates/engine.html` — AI 판단 로그 섹션
- `src/dashboard/templates/trades.html` — 거래 일지 카드
- `src/dashboard/templates/settings.html` — 알림 설정 카드
- `src/dashboard/static/js/performance.js` — 벤치마크/카드/트리맵 렌더링
- `src/dashboard/static/js/engine.js` — fetchAILog + 중복 esc 제거
- `src/dashboard/static/js/trades.js` — loadDailyJournal
- `src/dashboard/static/css/responsive.css` — Mobile Enhancement v5
- `src/dashboard/templates/*.html` — CSS/JS 캐시 버전 통일

## 2026-03-30 — Phase 1~6: 에이전트 팀 아키텍처 + PRISM 채용

### 거래 원칙 시스템 + 대시보드 개선 (428f063~606cecf)
- **거래 원칙 21개**: 리스크(4), 진입(8), 청산(4), 포트폴리오(5) — 모든 원칙에 source(구현 코드) 참조
- **주간 리포트**: 매주 토요일 00:00 텔레그램 전송 (메모리 현황 + LLM 인사이트 + 원칙 리마인더)
- **대시보드 AI 엔진 카드**: 시장 체제 배지 + 크로스 검증 통과/차단 + 거래 원칙 수 + LLM 진단

### 17라운드 전체 리뷰 (ff44ad5)
- neutral 고착 방지 (혼조→sideways), market_level Layer 2 전달, 진화 프롬프트 가드레일 일치
- 장전 진단 텔레그램 전송 (AI판단+체제+넥스트장+테마+뉴스)

### 3곳 LLM 통합 + 데이터 소스 확장 (e92e829~b290ce7)
- **매수 전 LLM 2차 검증**: GPT-5.4, 하루 5회, 고점수(85+) 비강세장만
- **저녁 LLM 구조화 복기**: AVOID/FOCUS 원칙 자동 생성 → Layer 3 환류
- **장전 LLM 시장 진단**: 08:50 [공격/중립/방어] 판단 + 체제 미세 조정
- **Perplexity 실시간 매크로 검색**: Sonar 모델, $0.005/회
- **넥스트장 시세 연동**: 보유 종목 5개 get_overtime_price() graceful
- **뉴스 헤드라인 5건**: theme_detector 최근 뉴스 주입
- **리뷰 수정**: task=STRATEGY_ANALYSIS 명시, LLM 상태 lazy 초기화

### 최종 리뷰 수정 (77270d7)
- 크로스검증 `or` 패턴 → `is None` 체크 (CLAUDE.md 규칙 준수)
- `_kospi_level` 5구간 레벨 계산 + `record_outcome` 전달 (시장 변곡점 학습 활성화)

### Phase 6: PRISM 우위 영역 채용 (87ed14d)
1. **펀더멘탈 밸류에이션 필터**: 적자+고PBR -10점, 극단PER(>50) -5점
2. **시장 지수 레벨 학습**: TradeMemory에 KOSPI 레벨 구간별 승률 → 원칙 추출
3. **LLM 종합 판단**: 고점수(85+) 비강세장에서 선택적 2차 검증 (fail-open)
4. **LLM 보조 회고**: 주간 압축 시 손실 패턴 LLM 분석 (선택적)

### Phase 1~5 통합 리뷰 수정 (988f096)
- P0: 크로스검증 규칙5 섹터 정확 비교 (record_exit에 sector 추가)
- P1: score 원본 보존, RSI 안전 변환, 설정 절대경로, entry_indicators 복원

### Phase 1~5 완료: 에이전트 팀 아키텍처

### Phase 1: 크로스 전략 검증 게이트 (`cross_validator.py`)
- 8개 교차 검증 규칙 (RSI과매수, 수급불일치, 체제부적합, 섹터과집중, 추격매수, 거래메모리 등)
- engine.py `on_signal()`에 게이트 삽입 — 감점 후 50점 미만 차단

### Phase 2: 시장 체제 사전 적응 (`market_regime.py`)
- bull/bear/sideways/neutral 4단계 체제 판단
- KOSPI+KOSDAQ 기반 2분마다 갱신 → engine._market_regime으로 크로스 검증 연동

### Phase 3: 거래 메모리 시스템 (`trade_memory.py`)
- Layer 1: 원시 기록 (진입/청산 지표, 시장 체제, 전략, 섹터)
- Layer 2: 요약 압축 (7일 이후, 패턴 → 결과)
- Layer 3: 원칙 추출 (승률/PnL 기반 score ±3 보정, 90일 미검증 비활성)
- kr_scheduler 매도 체결 시 자동 기록 + 크로스 검증에서 점수 보정 활용

### Phase 4: 품질 검증 파이프라인 (`quality_validator.py`)
- 매일 20:30 evolve 직전 자동 실행
- 거래 성과 + 설정 일관성 + 크로스 통계 + 포지션 집중도 검증
- 금요일 거래 메모리 주간 압축 자동 트리거

### Phase 5: 에이전트 팀 8명 구성
- trade-analyst, market-analyst, strategy-advisor, engine-monitor
- risk-auditor, param-optimizer + code-reviewer, debugger
- `.claude/agents/*.md` 6개 신규 + CLAUDE.md 위임 규칙 갱신

### 로드맵 (`docs/ROADMAP_AGENT_TEAM.md`)
- PRISM-INSIGHT 분석 기반 6-Phase 로드맵 수립
- Phase 6(LLM 종합 판단) 후속 예정

---

## 2026-03-28 — 16라운드: 진화 시스템 가드레일 강화 (d03dc26)

- **P0-1**: 비활성 전략(momentum_breakout) 예산 0% 강제 — 진화가 12.5% 배정한 것 차단
- **P0-2**: 합계 상한 105%→100%, 단일 전략 70%→60%, 주당 변동 15→10%p
- **P1-2**: daily_max_trades 30→10 복원
- 가드레일에 합계 재검증 루프 + 비활성 전략 _disabled 세트 추가
- evolved_overrides 복원: sepa 45%, rsi2 10%, theme 10%, gap 5%, momentum 0%

---

## 2026-03-27 — 15라운드 전체 리뷰 + 회피 패턴 + 집중 기회

### 15라운드 전체 리뷰 수정 (1bbe7dd)
- **P0-1**: very_strong 신호 배율 2.0→1.3 (단일 종목 28% 과잉 집중 방지)
- **P0-2**: 전략 배분 합계 105%→100% (비활성 momentum 5%→0%)
- **P0-3**: stop_loss_pct=3.0→3.5 (min_stop_pct=3.5 정합성)
- **P1-1**: daily_pnl_pct 기준 initial_capital→total_equity 통일
- **P1-5**: Gap&Go ATR position_multiplier 적용 (고변동 갭 종목 사이징 누락)
- **P1-8**: KR 섹터 집중도 제한 작동 — can_open_position에 sector 전달
- **P1-9**: 본전보호 FIRST -2.5%→-1.5% (1차 익절 후 순손실 방지)
- **P2-2**: check_rr_ratio risk≤0 → False (잘못된 손절가 차단)
- **P2-8**: ATR sizing 계단함수→선형 보간 (불연속 점프 제거)

### 회피 패턴 5건 + 집중 기회 3건 (d6f1ba4)

### 회피 패턴
1. **장초반 추격 방지**: theme_chasing 시간대별 max_change 차등 (09~10시: 4%, 이후: 8%)
2. **대형주 테마 차단**: 시총 상위 20종목 theme_chasing 제외
3. **기대수익 미검증 차단**: 장중 자동진입 R/R≥1.5 체크 추가
4. **theme EOD 갭리스크**: 15:10 이후 수익률 +1% 미만 theme 포지션 강제 청산
5. **고점 추격 차단**: 등락률/ATR >1.2x 시 장중 자동진입 거부 + 시간대별 등락률 상한

### 집중 기회
- **theme 등락률 세분화**: 2~4%(초기확산) 20점 / 4~6% 14점 / 6~8% 8점

### 기타
- US 매도 trade_events DB 기록 누락 수정 (c1cd60b)
- US 마켓 필터 시 코어홀딩 섹션 숨김 (979848f)

---

## 2026-03-25 — 14라운드 리뷰 + 집중 기회 5건 + US 대시보드 개선

### 14라운드 리뷰 수정 (7e524ad)
- **P0-1**: 재진입 +1%~+3% 데드존 해소 → -3%~+3% 통합 허용, -3% 미만만 급락차단
- **P1-1**: SEPA 고점수+고ATR 시 최소 비중 보장 (`min→max`, score≥90: 0.8배 최소)
- **P1-2**: `_exited_today` JSON 영속화 + 재시작 복원 + 분할매도 최초가격 보존
- **P1-3**: RSI2 VCP — `vcp_score` 우선 사용 + `overlay_bonus` 폴백, None 안전 체크
- **P1-4**: theme_chasing MA20 15% → 25% 완화 (테마 단기급등 특성 반영)

### 집중 기회 구현 (74e5fe0)
1. **SEPA 우선 배분**: score 90+ → 1.4배, 85+ & MRS>0 → 1.2배 position_multiplier
2. **RSI2 + VCP 결합**: MA200 상방 + overlay_bonus≥3 → 1.3배 배율, ATR 사이징 추가
3. **트레일링 연장**: FIRST 본전보호 -1.5% → -2.5% (눌림목 조기 청산 방지)
4. **theme_chasing 과열 차단**: RSI>75 차단, MA20 대비 +15% 초과 차단
5. **재진입 제한**: 당일 청산 종목 30분 쿨다운 + 눌림(-3%~+1%)/재돌파(+3%) 확인형

### US 대시보드 개선 (782545f)

- 통계 카드 5개 추가 (실현손익, 미실현손익, 매수건수, 매도건수, 승/패)
- 보유 현황 테이블 추가 (종목명, 수량, 평균/현재가, 평가손익, 전략, 단계)
- 전체/매수/매도 필터 탭 + 건수 카운트
- 종목명 표시 (코드 + 한글명), 전략명 한글화
- 상태 배지 세분화 (손절/익절/분할익절/트레일링/EOD)
- KR 동기화 set_sync_status 접근 경로 수정 (6a3197a)

---

## 2026-03-24 — 복기 기반 트레이딩 개선 7건 + 리뷰 P1 수정

### 리뷰 수정 (c697353)
- **P1-1**: `sepa_trend.py` — score 음수 방지 `max(0, min(score, 100))`
- **P1-2**: ATR→position_multiplier 매핑 3곳 중복 → `utils/sizing.py` 공통 헬퍼 추출
- **P1-4**: `kr_scheduler.py` — ATR 파싱을 `stock.atr_pct` 직접 접근으로 변경 (reason 파싱 폴백 유지)
- **P2-5**: `sepa_trend.py` — close 변수 재선언 제거

### 개선 7건 (d524679)

### 1. theme_chasing max_holding_days 3일 제한 (`run_trader.py`)
- theme_chasing exit_params에 `max_holding_days: 3` 추가 — 단기 테마 전략 보유기간 제한

### 2. 지표 추가: ma200_distance_pct + high_20d/low_20d (`technical.py`)
- MA200 대비 거리(%) — 과확장 필터용
- 20일 고저 — 눌림 보너스/추격 감점용

### 3. 60일 급등 과확장 필터 (`sepa_trend.py`, `swing_screener.py`)
- SEPA generate_batch_signals: MA200 대비 +80% 이상 → 후보 차단
- _calculate_sepa_score: MA200 +50% → -10점, +30% → -5점
- swing_screener _base_technical_score(sepa_trend): 동일 감점 적용

### 4. SEPA 눌림 보너스 / 추격 감점 (`sepa_trend.py`)
- 20일 고점 대비 -3%~-7% 눌림 → +5점 보너스
- 20일 고가 돌파 직후 → -5점 추격 감점

### 5. ATR 진입 필터 (`theme_chasing.py`, `kr_scheduler.py`, `default.yml`)
- ThemeChasingConfig에 `max_atr_pct: 8.0` 추가
- theme_chasing _check_entry_signal: ATR > max_atr_pct → 진입 차단
- kr_scheduler 장중 스크리닝: ATR > 10% → 종목 제외
- config/default.yml에 기본값 추가

### 6. ATR 기반 포지션 사이징 (`sepa_trend.py`, `theme_chasing.py`, `kr_scheduler.py`)
- ATR ≤3%: 1.0배 / 3~5%: 0.8배 / 5~8%: 0.6배 / >8%: 0.4배
- sepa_trend: signal.metadata에 position_multiplier 추가
- theme_chasing: Signal 직접 생성으로 변경 + atr_pct, position_multiplier, theme_name metadata
- kr_scheduler 장중 시그널: ATR 배율과 오버나이트 배율 중 min() 적용, 최소 0.4배 클램핑

### 7. 동기화 장애 시 매수 차단 프로토콜 (`risk/manager.py`, `kr_scheduler.py`)
- RiskManager에 `_sync_healthy`, `_sync_fail_count` 추가
- `set_sync_status(healthy)`: 연속 3회 실패 → 매수 차단, 성공 1회 → 즉시 복구
- `can_open_position()`: sync 장애 시 매수 거부
- kr_scheduler `_sync_portfolio`: 성공/실패/재시도실패 시 상태 갱신
- `run_portfolio_sync` 루프 예외에서도 갱신

---

## 2026-03-23 — 13라운드 코드 리뷰: P0 1건 + P1 2건 수정

### P0: engine.py vs RiskManager 일일 손실 기준 불일치 → 스마트 사이드카 무력화 (`engine.py`)
- **문제**: engine.py는 `daily_pnl`(실현만) -5%에서 무조건 차단, RiskManager는 `effective_daily_pnl`(미실현 포함)으로 스마트 사이드카 적용 → RiskManager가 "허용"해도 engine이 막거나, 미실현 -4.7%를 engine이 감지 못해 통과시키는 이중 불일치
- **수정**: engine.py의 소프트 체크(실현 -5%) 제거 → 하드캡만 유지, `effective_daily_pnl` 기준 + 하드캡을 RiskManager와 동일(2.5×=12.5%)로 통일. 세밀한 판단은 RiskManager 스마트 사이드카에 위임.

### P1-1: `run_market_trend_monitor` 장외시간 60초 sleep 루프 (`kr_scheduler.py`)
- **문제**: NEXT/CLOSED 세션에서 `continue` 후 60초 sleep → 120초에 도달 못 함
- **수정**: 장외 시간에도 120초 sleep으로 통일

### P1-2: `update_market_trend` 빈 dict 시 추세 왜곡 (`risk/manager.py`)
- **문제**: kospi={}, kosdaq={} 입력 시 모두 0 → avg_change=0 → 회복세 오판
- **수정**: 양쪽 price 모두 없으면 early return

### 수정 파일
| 파일 | 수정 내용 |
|------|-----------|
| `src/core/engine.py` | 소프트 체크 제거 → 하드캡만 유지 (effective_daily_pnl 기준, 12.5%) |
| `src/risk/manager.py` | update_market_trend 빈 dict 가드 |
| `src/schedulers/kr_scheduler.py` | 장외 시간 sleep 120초 통일 |

## 2026-03-23 — 리뷰: 사이드카 경고 구간 분리 + 지수 OHLC 추세 판단

### P1 수정: 경고 구간 조기 진입 + 2단계 분리 (`risk/manager.py`)
- **문제**: 기존 경고 구간이 -5%~-12.5%로, 미실현 -4.7% 상황에서 진입 못함 → 매수 차단 안 됨
- **수정**: 2단계로 분리
  - 경고 구간(-3.5%~-5%): 시장 회복세 → 전면 허용 / 하락세 → 사이드카 차단
  - 한도 초과(-5%~-12.5%): 시장 회복세 → 방어적 전략만 / 하락세 → 전면 차단
  - 하드 스탑(-12.5%+): 무조건 전면 차단

### 개선: 지수 OHLC 기반 추세 판단 (`kis_market_data.py`, `risk/manager.py`)
- KIS API(FHPUP02100000)에서 시가/고가/저가 필드 추가 파싱
- 추세 판단 3지표: 전일대비 등락률 + 시가대비 방향 + 장중 위치(고저 내 현재가 비율)
- 혼조세 시 이전 상태 유지 (잦은 ON/OFF 전환 방지)

### 수정 파일
| 파일 | 수정 내용 |
|------|-----------|
| `src/risk/manager.py` | 경고 구간 -3.5% 조기 진입 + 2단계 분리 + OHLC 추세 판단 |
| `src/data/providers/kis_market_data.py` | fetch_index_price에 open/high/low 필드 추가 |
| `src/schedulers/kr_scheduler.py` | update_market_trend에 dict 전체 전달 |

## 2026-03-23 — 로그 분석 기반 개선 2건: 스마트 사이드카 + 유령 포지션 레이스 컨디션

### Feature: 시장 추세 연동 스마트 사이드카 (`risk/manager.py`, `kr_scheduler.py`)
- **문제**: 일일 손실 -4.7% 상태에서 개별 종목 손실인데도 전체 매수가 차단되지 않거나, 반대로 시장 반등 시에도 일괄 차단되는 비효율
- **설계**: 일일 손실 경고 구간(-5%~-12.5%)에서 KOSPI/KOSDAQ 장중 등락률 기반 판단
  - 시장 하락세(평균 < -0.3%) → 사이드카 ON (전면 차단)
  - 시장 회복세(평균 >= -0.3%) → 사이드카 OFF (SEPA/RSI2/코어홀딩 허용)
  - 추세 정보 없음 → 기존 차등 리스크 유지 (방어적 전략만)
- `run_market_trend_monitor()` 2분 주기로 KOSPI/KOSDAQ 지수 조회 → `RiskManager.update_market_trend()` 갱신
- 하드 스탑(-12.5%) 초과 시 시장 추세 무관 전면 차단

### Fix: 유령 포지션 레이스 컨디션 (`kr_scheduler.py`)
- **문제**: 매도 주문 제출(12:59:31) → KIS 체결 반영 → 동기화(12:59:37)에서 유령 제거 → fill 수신(12:59:38) 시 포지션 없음 → daily_pnl 미반영
- **수정**: `_sync_portfolio()`에서 `_exit_pending_symbols`에 포함된 종목은 유령 판정 보류

### 수정 파일
| 파일 | 수정 내용 |
|------|-----------|
| `src/risk/manager.py` | `update_market_trend()` + `_is_daily_loss_limit_hit` 시장 추세 연동 |
| `src/schedulers/kr_scheduler.py` | `run_market_trend_monitor()` 2분 주기 + 유령 포지션 pending 보호 |

## 2026-03-23 — 신규 TR 커밋 리뷰: P1 1건 수정

### P1: `fetch_investor_trend_estimate` or-chain에서 0값 무시 (`kis_market_data.py`)
- **문제**: `output.get("frgn_ntby_qty") or output.get(...)` — 순매수 0주일 때 falsy → 다음 키(잔고수량 등)로 폴백 → 수급 데이터 왜곡
- **수정**: `is not None` 체크로 교체 — 0 값 정상 보존
- CLAUDE.md 절대 금지 패턴 (`value or default` — value=0이면 default 반환) 해당

### 수정 파일
| 파일 | 수정 내용 |
|------|-----------|
| `src/data/providers/kis_market_data.py` | or-chain → is not None 순차 체크 |

## 2026-03-22 — 12라운드 코드 리뷰: P1 1건 수정

### P1: `_fill_composite_single` 실패 시 무한 재시도 (`kr_scheduler.py`, `batch_analyzer.py`)
- **문제**: pykrx 빈 응답(장외시간) 또는 예외 시 캐시에 미추가 → 다음 20초 틱에 재호출 → 장외시간 동안 수백 회 불필요한 pykrx 호출 (KRX rate limit 위험)
- **수정**: 실패/빈 응답 시에도 `self._ma5_cache[symbol] = None` sentinel 등록 → 재시도 방지
- ExitManager의 `_check_composite_trailing`은 `ma5 is not None` 체크로 sentinel 안전 처리

### 수정 파일
| 파일 | 수정 내용 |
|------|-----------|
| `src/schedulers/kr_scheduler.py` | `_fill_composite_single` 실패 시 sentinel 캐시 등록 |
| `src/core/batch_analyzer.py` | 동일 수정 |

## 2026-03-20 — 11라운드 코드 리뷰: P0 1건 + P1 4건 수정

### P0: 복합 트레일링 breakeven 미활성 시 미작동 (`exit_manager.py`)
- **문제**: `_check_composite_trailing()`이 `breakeven_activated=True` 블록 내부에서만 호출 → 1차 익절 직후 가격 하락으로 breakeven 미활성 시 MA5/전일저가 청산 불가
- **수정**: 복합 트레일링 호출을 breakeven 블록 밖으로 이동, stage >= min_stage이면 독립 실행

### P1-1: 테마 확산도 장 초반 전면 차단 (`theme_chasing.py`)
- **문제**: `get_indicators(ts)` 캐시 미스(장 초반) → 모든 종목 None → breadth_count=0 → min_theme_breadth 미충족
- **수정**: 캐시된 종목 2개 미만이면 확산도 체크 스킵 (다른 필터로 품질 보장)

### P1-2: 장중 신규 매수 종목 복합캐시 미포함 (`kr_scheduler.py`, `batch_analyzer.py`)
- **문제**: `_refresh_composite_cache()` 일 1회 실행 → 장중 진입 종목 캐시 없음 → 복합 트레일링 무효
- **수정**: `_fill_composite_single()` 추가 — REST 피드에서 캐시 미스 발견 시 해당 종목만 즉시 갱신

### P1-3: 복합캐시 메모리 누수 (`kr_scheduler.py`, `batch_analyzer.py`)
- **문제**: `_ma5_cache`/`_prev_low_cache`에 추가만 하고 삭제 없음 → 장기 운영 시 점진적 증가
- **수정**: 날짜 변경 시 `clear()` 후 재구축

### P1-4: STAGE_ORDER 중복 정의 통일 (`exit_manager.py`)
- **문제**: 동일 stage 리스트가 클래스 속성 + 메서드 내 로컬 변수로 4곳 중복 → 불일치 위험
- **수정**: `ExitManager.STAGE_ORDER` 클래스 상수로 통일, 메서드 내 로컬 변수 참조로 교체

### 수정 파일
| 파일 | 수정 내용 |
|------|-----------|
| `src/strategies/exit_manager.py` | 복합 트레일링 위치 이동 + STAGE_ORDER 통일 |
| `src/strategies/kr/theme_chasing.py` | 테마 확산도 캐시 미스 보정 |
| `src/schedulers/kr_scheduler.py` | `_fill_composite_single()` + 캐시 clear() |
| `src/core/batch_analyzer.py` | `_fill_composite_single()` + 캐시 clear() |

## 2026-03-19 — 성과 개선 후속 3건: 본전보호 완화 + 저효율 청산 + 거래 기록 품질

### Fix 1: 본전 보호 Stage별 차등 버퍼 (`exit_manager.py`)
- **문제**: 1차 익절(+5%) 후 정상 눌림목에서 +0.25%까지 하락 시 잔여 80% 전량 청산 → 추세 조기 포기
- **수정**: Stage별 버퍼 차등 적용
  - FIRST: -1.5% (20% 이미 수익 확보, 추세 추종 여유)
  - SECOND: -0.5% (추가 수익 확보, 버퍼 축소)
  - THIRD/TRAILING: +0.25% (기존 유지, 수수료 보호)
  - 코어홀딩: -2.0% (기존 유지)

### Fix 2: 익절 후 저효율 포지션 청산 (`exit_manager.py`)
- **문제**: 기존 횡보 청산은 `stage=NONE`에서만 작동 → 1차 익절 후 +3%에서 5일 이상 체류하는 저효율 포지션 방치
- **수정**: `post_exit_stale_days=5`, `post_exit_stale_pnl_pct=3.0%` 추가
  - stage >= FIRST & 5영업일+ 보유 & 수익률 < 3% & 신고가 3일 이상 미갱신 → 전량 청산
  - 신고가 갱신 중이면 추세 진행으로 판단 → 스킵

### Fix 3: KR 거래 기록 품질 강화 (`kr_scheduler.py`)
- **문제**: `record_entry()` 호출 시 `indicators`, `market_context`, `theme_info` 미전달 → 복기 데이터 부실
- **수정**: 매수 체결 시 자동 수집하여 전달
  - `indicators`: ATR, RSI, volume_ratio, change_pct
  - `market_context`: 시장 레짐(llm_regime_today), 세션, 시그널 소스
  - `theme_info`: 테마명, 테마 점수

### 수정 파일
| 파일 | 수정 내용 |
|------|-----------|
| `src/strategies/exit_manager.py` | ExitConfig 필드 + 본전보호 차등 버퍼 + 익절후 저효율 청산 |
| `src/schedulers/kr_scheduler.py` | record_entry에 indicators/market_context/theme_info 전달 |
| `config/default.yml` | post_exit_stale_days/pnl_pct 기본값 |

## 2026-03-18 — 성과 개선 2건: 복합 트레일링 스탑 + 테마 추종 진입 품질 강화

### Feature 1: 복합 트레일링 스탑 (MA5 + 전일저가)
- **ExitManager.update_price()** 시그니처 확장: `market_data` 파라미터 추가 (하위 호환)
- **ExitConfig** 4개 필드 추가: `enable_composite_trailing`, `composite_trail_min_stage`, `composite_ma5_buffer_pct`, `composite_prev_low_enabled`
- **복합 트레일링 로직**: 1차 익절 이후 MA5 - 0.5% 이탈 또는 전일저가 이탈 시 전량 청산 (코어홀딩 제외)
- **KR 스케줄러**: `_refresh_composite_cache()` — pykrx 기반 MA5/전일저가 일 1회 캐시
- **BatchAnalyzer**: `monitor_positions()`에서도 동일 복합 트레일링 데이터 전달
- 기존 ATR 트레일링과 OR 관계 — 어느 하나라도 발동 시 청산

### Feature 2: 테마 추종 진입 품질 강화
- **ThemeChasingConfig** 4개 필드 추가: `min_trading_value`(5억), `min_theme_breadth`(3종목), `theme_breadth_change_pct`(1%), `max_high_retreat_pct`(3%)
- **거래대금 필터**: 당일 누적 거래대금 < 5억원 종목 차단
- **테마 확산도**: 동일 테마 내 동반 상승 종목 3개 미만 시 차단 (고립 상승 배제)
- **장중 고점 후퇴**: 고점 대비 3% 초과 후퇴 시 차단 (이미 꺾인 종목 배제)
- **스코어링 재분배**: 테마 40 + 등락률 20 + 거래량 15 + 확산도 15 + 고점유지 10 = 100점

### 수정 파일
| 파일 | 수정 내용 |
|------|-----------|
| `src/strategies/exit_manager.py` | ExitConfig 필드 + update_price 시그니처 + _check_composite_trailing() |
| `src/strategies/kr/theme_chasing.py` | Config 필드 + 필터 3종 + 스코어링 확장 |
| `src/schedulers/kr_scheduler.py` | MA5/전일저가 캐시 + _check_exit_signal market_data 전달 |
| `src/core/batch_analyzer.py` | monitor_positions 복합캐시 + market_data 전달 |
| `config/default.yml` | 복합 트레일링 + 테마 품질 파라미터 기본값 |

## 2026-03-18 — 커밋 리뷰 P1/P2 수정 5건

### P1: 부분 매도 체결 오탐 (`us_scheduler.py:2044`)
- **문제**: `orig_qty` 없는 구버전 pending에서 fallback `pos.quantity + expected_qty` → 항상 True → 30초 후 오탐
- **수정**: `"orig_qty" in pending` 존재 시에만 부분 매도 감지

### P2: 코드 품질 4건
- `_retry_key` 미사용 변수 제거 (`us_scheduler.py:2109`)
- `inspect.signature` → 직접 kwarg 전달로 단순화 (`trade_storage.py:259`)
- `if True:` 불필요한 감싸기 제거 + 들여쓰기 정리 (`us_scheduler.py:1469`)
- `signals.index(sig)` O(n) → `enumerate` O(1) (`us_scheduler.py:867`)

## 2026-03-18 — 일일 리뷰 개선 2건

### P1: KR entry_signal_score 전량 0 기록 버그 (`kr_scheduler.py:1364`)
- **문제**: `getattr(bot.engine, '_pending_signal_cache', {})` — `_pending_signal_cache`는 `engine.risk_manager`에 위치하나 `engine` 자체에서 조회 → 항상 `{}` 반환 → 모든 KR 거래의 signal_score=0
- **수정**: `getattr(bot.engine.risk_manager, '_pending_signal_cache', {})`로 올바른 경로 참조

### P2: US 스크리닝 자금 부족 연속 실패 시 조기 종료 (`us_scheduler.py`)
- **문제**: 자금 부족(25건/일) 시에도 나머지 시그널 전부 순회 → 불필요한 API 호출 낭비
- **수정**: `_consecutive_fund_fail` 카운터 추가, 연속 3건 자금 부족 시 스크리닝 루프 break
- **범위**: `_process_signal` 내 qty≤0 + submit_buy_order 실패("주문가능금액") 양쪽 모두 사유 기록

### 수정 파일
| 파일 | 수정 내용 |
|------|-----------|
| `src/schedulers/kr_scheduler.py` | signal_cache 경로 수정 (engine → risk_manager) |
| `src/schedulers/us_scheduler.py` | 자금 부족 연속 실패 조기 종료 + reject_reason 기록 |

## 2026-03-18 — 10라운드 코드 리뷰 P0 수정 7건

### P0-1: Cash=0 sync skip (`us_scheduler.py:1446`)
- **문제**: `cash_val > 0` 조건으로 cash=0 상태(전액 투자)를 skip → portfolio.cash 미갱신
- **수정**: `cash_val >= 0`으로 변경, 0도 유효한 값으로 동기화

### P0-2: 전략 exit 실패 시 ExitManager 손절 차단 (`us_scheduler.py:1269-1289`)
- **문제**: `strategy_exit_attempted=True`가 전략 exit 시도만으로 설정 → 실패해도 ExitManager 완전 skip → 손절 미발동
- **수정**: `strategy_exit_submitted=bool(exit_ok)`로 변경, 매도 주문 성공 시에만 ExitManager skip

### P0-3: WS+REST exit 체크 레이스 컨디션 (`us_scheduler.py`)
- **문제**: `_on_us_ws_price`와 `_check_exits`가 동시에 같은 포지션에서 exit 시그널 발생 → 이중 매도
- **수정**: per-symbol `asyncio.Lock` 추가, 한쪽이 처리 중이면 다른 쪽 skip

### P0-4: RSI2 ATR=None 시 stop/target 미설정 (`rsi2_reversal.py:86-93`)
- **문제**: ATR 미제공 시 스크리너 기본값(-5%/+5%, R:R 1:1) 유지 → `check_rr_ratio(min_rr=2.0)` 실패 → 시그널 전부 탈락
- **수정**: ATR=None일 때 기본 stop=5%, target=10% (R:R 2:1) 폴백 추가

### P0-5: EOD close price=0 시장가 실패 (`us_scheduler.py:_eod_close`)
- **문제**: DAY 포지션 마감 청산에 `price=0` (시장가) 사용 → KIS US API 거부
- **수정**: 현재가 × 0.98 aggressive limit으로 변경

### P0-6: 매도 폴백 무한 재시도 루프 (`us_scheduler.py:_check_orders`)
- **문제**: 매도 취소 → 폴백 → 재취소 → 무한 반복 가능
- **수정**: `_sell_retry_count[symbol]` per-symbol 최대 3회 제한, 초과 시 수동 확인 알림

### P0-7: equity≤0 시 일일 손실 한도 bypass (`risk/manager.py:265`)
- **문제**: `equity <= 0`일 때 `return False` → 손실 한도 미도달 판정 → 추가 매수 가능
- **수정**: `return True`로 변경 (equity 0 이하 → 거래 차단)

### 수정 파일
| 파일 | 수정 내용 |
|------|-----------|
| `src/schedulers/us_scheduler.py` | P0-1,2,3,5,6 |
| `src/strategies/kr/rsi2_reversal.py` | P0-4 |
| `src/risk/manager.py` | P0-7 |

## 2026-03-18 — US WS 통합 + 매도 폴백 수정 (`us_scheduler.py`)

### WS approval_key 충돌 해소
- **문제**: `kis_us_ws`(체결통보) + `kis_us_price_ws`(가격) 두 개가 approval_key 경쟁 → "ALREADY IN USE appkey" → `price_ws=off`
- **수정**: `us_price_ws`에 체결통보(H0GSCNI0) 통합 구독, 별도 `kis_ws`는 `us_price_ws` 없을 때만 폴백
- **결과**: 단일 WS에서 가격+체결통보 동시 처리, 충돌 해소

## 2026-03-18 — US 매도 폴백: 시장가→적극지정가 (`us_scheduler.py`)

### 문제
- IMMX 1차 익절 지정가 미체결 → 2분 타임아웃 → 시장가(`price=0`) 폴백 → KIS US API "주문단가를 입력 하십시오" 에러
- KIS 해외주식 API는 시장가 주문을 지원하지 않음 (ORD_DVSN="00"에서 price=0 불가)
- 2번 연속 같은 실패 패턴 반복

### 수정
- 2곳의 시장가 폴백 → **적극지정가 폴백** (현재가 -2% 지정가)으로 변경
  1. `_check_orders` inquire-ccnl 미확인 타임아웃 후 폴백 (line ~1970)
  2. `_check_orders` pending status 타임아웃 후 폴백 (line ~2070)
- 현재가 조회 실패 시 원래 pending price를 기반으로 -2% 설정

## 2026-03-17 — US 엔진 P0/P1/P2 3건 수정 (WS 연결, 거래소 매핑, 매도 감지)

### P0: US WebSocket 전혀 연결 안됨 (치명적)
- **원인**: `minutes_to_open()` → 장중에 `None` 반환 → `None <= 10` → TypeError → 코루틴 사망
- **영향**: 실시간 가격 피드 없음, EXIT 체크가 REST 폴링에만 의존 (15초 지연)
- **수정**: `us_scheduler.py` 3곳에서 `_mto is not None and _mto <= 10` 패턴 적용
- **추가**: `ws_market_loop` 초기화 섹션 try/except 추가 (silent crash 방지)
- **결과**: `price_ws=ok(8)` — WS 정상 연결, 보유 종목 실시간 구독

### P1: 22개 종목 현재가 조회 실패 (매 스크리닝)
- **원인**: `yfinance.get_info()`가 `exchange` 필드 미반환 → 모든 종목이 `NASD` 기본값 → NYSE/AMEX 종목 KIS API 실패
- **수정**: `src/data/providers/yfinance.py` `get_info()`에 `'exchange': info.get('exchange', '')` 추가
- **결과**: SEI(NYSE), EC(NYSE), BP(NYSE) 등 정상 조회 (`NYSESEI` 정확히 매핑)

### P2: 매도 pending 2분 지연 감지
- **원인**: `inquire-ccnl` 빈 결과 반복 → 2분 타임아웃 후에야 취소 시도로 감지
- **수정**: 매수뿐 아니라 매도도 포트폴리오 기반 체결 감지 추가
  - 전량 매도: 포지션 소멸 → 즉시 pending 정리
  - 부분 매도: `orig_qty` 대비 수량 감소 → 체결 간주
- **pending에 `orig_qty` 필드 추가** (매도 주문 시 원래 보유 수량 기록)

## 2026-03-16 — US 당일 재매수 차단 강화 (`us_scheduler.py`)

### 문제
- ORKA: 익절 매도 후 같은 날 동일 종목 재진입 → 하락으로 손실
- `_stopped_today`가 `stop_loss`/`trailing` 매도만 차단, 익절은 미차단
- 봇 재시작 시 `_stopped_today` 메모리 초기화 → 파일은 저장하지만 로드하지 않음

### 수정
1. **모든 매도 유형 재매수 차단**: `if exit_type in ("stop_loss", "trailing"):` → `if True:` (익절 포함)
2. **재시작 시 파일 복원**: 일일 리셋에서 `stopped_today_{YYYYMMDD}.json` 파일 로드 추가
   - 파일 위치: `~/.cache/ai_trader_us/stopped_today_{YYYYMMDD}.json`
   - 새 거래일이면 파일 없음 → 빈 set (정상)
   - 장중 재시작이면 파일 존재 → 이전 청산 종목 복원

### 효과
- 동일 종목 당일 재진입 완전 차단 (매도 사유 무관)
- 봇 재시작해도 차단 목록 유지

## 2026-03-16 — US 프리마켓 가격 괴리 방지 2중 게이트 (`us_scheduler.py`)

### 문제
- AXTI 매수 직후 1분만에 -5.99% 손절: 스크리닝이 yfinance 전일종가 기반 → 프리마켓 가격 괴리 무시
- `_run_screening()` 시그널 생성 시점에 당일 가격 변동 체크 없음
- `_process_signal()` 주문 직전에도 시그널가 vs 현재가 갭 체크 없음

### 수정 1: Finviz 실시간 가격 사전 필터 (`_run_screening()` 내)
- 시그널 생성 후, 주문 전에 **Finviz `get_intraday_scan()` 배치 조회** (1회 API 호출로 전체 시그널 종목)
- 당일 변동률 ≤ -3% → 시그널 제거 (하락 종목 매수 차단)
- Finviz 실시간가 vs 시그널 평가가 괴리 ≥ 5% → 시그널 제거

### 수정 2: KIS 현재가 갭 체크 (`_process_signal()` 내)
- 주문 직전 `get_quote()` 현재가 vs `signal.price` 비교
- 현재가 < 시그널가 -3% → "가격 괴리 차단"
- 현재가 > 시그널가 +5% → "추격매수 차단"

### 효과
- 2중 게이트: ① Finviz 배치(효율적) → ② KIS 개별(정확) → 프리마켓 함정 매수 차단

## 2026-03-16 — 코어홀딩 예산 예약 + 빈슬롯 즉시 매수 (2개 파일)

### 문제
- `strategy_allocation.core_holding: 30%`가 **상한(cap)**으로만 작동, **예약(reservation)**이 아님
- SEPA 등 비코어 전략이 전체 자산에서 포지션 계산 → 코어 30% 예산까지 소진
- 코어 매수는 월초 첫 영업일에만 가능 → 빈 슬롯이 한 달간 방치

### 수정 1: 코어 예산 예약 (`src/core/engine.py`)
- `_get_core_reserve()` 메서드 추가: `equity × 30% - 현재코어포지션가치 = 예약금`
- `on_signal()`: 비코어 매수 시 가용현금에서 코어 예약금 차감
- `_calculate_position_size()`: 비코어 전략의 `pool_equity = equity - core_reserve`
- 코어 전략은 전체 equity 기준 유지

### 수정 2: 빈슬롯 즉시 매수 (`src/schedulers/kr_scheduler.py`)
- 기존: 월초 첫 영업일 09:05~13:04 윈도우에서만 리밸런싱
- 변경: 매일 장중 코어 포지션 < max(3) && 예산 잔여 시 즉시 스캔+매수
- 빈슬롯 매수 윈도우: 09:10~09:14, 10:00~10:04, 13:30~13:34
- 일일 1회 시도 제한 (last_fill_date 추적)
- 월초 풀 리밸런싱(교체 판단)은 기존대로 유지

### 리뷰 후 추가 수정 (P1 3건, P2 1건)
- **P1-1**: `_calculate_position_size()` available에서 코어 예약 이중 차감 제거 (pool_equity에서 이미 반영)
- **P1-2**: 하이브리드 모드에서도 비코어 전략에 코어 예약 적용 (현재 비활성이나 방어적 추가)
- **P1-3**: `can_open_position()` 호출 시 `reserved_cash`에 코어 예약금 포함하여 2차 검증 강화
- **P2-3**: 빈슬롯 매수 실패 시 `last_fill_date` 미설정 → 다음 윈도우에서 재시도 허용

### 동작 예시 (자본 50만원)
| 시점 | 기존 | 변경 후 |
|------|------|---------|
| SEPA 매수 시 | pool=50만 → 25% = 12.5만 | pool=35만(코어15만 예약) → 25% = 8.75만 |
| 코어 빈 슬롯 | 다음달 초까지 대기 | 당일 09:10 스캔 → 즉시 매수 |

## 2026-03-15 — US/KR 뉴스 중복제거 개선 (2개 파일)

### 문제
- `us_theme_detector.py`: `seen_hashes`가 `Set[str]`로 영구 누적 → 봇 기동 후 수 시간 내 RSS 70건 중 68건 차단 → 뉴스 2건만 통과 → LLM 분석 스킵 (min_news_count=3 미달)
- `kr_theme_detector.py`: scikit-learn 미설치로 TF-IDF 유사도 중복제거 비활성화 → 유사 기사 53건이 LLM 프롬프트에 중복 유입

### 수정: US 뉴스 중복제거 2단계 구조 (`us_theme_detector.py`)
- **1차 SHA1**: `Set[str]` → `Dict[str, datetime]` TTL 2시간 기반 — 2시간 지난 기사 해시 만료, 재수집 허용
- **2차 TF-IDF 유사도**: `_is_similar_to_existing()` 추가 — 영문 기사 코사인 유사도 ≥ 0.85 중복 판정
  - `sklearn.feature_extraction.text.TfidfVectorizer` (max_features=200, ngram_range=(1,2))
  - 인메모리 슬라이딩 캐시 최대 500건, TTL 4시간 자동 만료
- 로그 포맷 KR과 통일: `전체=N, SHA1제거=N, 유사도제거=N, 최종=N`

### 수정: KR 뉴스 유사도 중복제거 활성화
- `requirements.txt`: `scikit-learn>=1.4.0` 추가 (venv 설치 완료)
- 효과: `유사도제거=0 → 53건` 추가 제거, 최종 98건 → 46건으로 품질 향상

### 추가 수정 (2차)
- `us_theme_detector.py`: 뉴스 부족(0건) 시 `_cleanup_stale()` 스킵 → 기존 테마 보존
  - 원인: SHA1 TTL 2h 내 재수집 시 0건 → 즉시 cleanup → 테마 삭제 → "뉴스 부족" 루프
- `us_theme_detector.py`: `_cleanup_stale()` stale 타임아웃 1h → 4h (SHA1 TTL 2h + 버퍼 커버)
- `kr_theme_detector.py`: `_cache_days` 7일 → 1일
  - 원인: 7일치 500건 DB 로드 → 신규 기사 97% 유사도 차단 → 최종 5건만 통과

### 수치 비교

| | 수정 전 | 수정 후 |
|---|---|---|
| US 뉴스 최종 통과 | 2~3건 (기동 후 수시간) | 60+건 |
| US 활성 테마 | 1개 (stale, 반복 삭제) | 2개 (4시간 유지) |
| KR 유사도 제거 후 최종 | 5~15건 (과필터) | 61건 |

## 2026-03-14 — KR 전략 백테스트 엔진 구현

### 신규 파일
- `scripts/backtest_strategies.py` (~870줄): SEPA, RSI-2, Core Holding 전략 백테스트 엔진

### 주요 기능
- **3전략 미러링**: 실제 전략의 100점 스코어링 로직 (SEPA/RSI-2/Core) 충실 재현
- **청산 시뮬레이션**: 3단계 분할 익절 + ATR 동적 손절 + 트레일링 + 횡보/추세 무효화 청산
- **리스크 관리**: 전략별 배분 (SEPA 60%/RSI-2 10%/Core 30%), 포지션 수 제한, 일일 손실 제한
- **T+1 실행**: 시그널 당일 생성 → 익일 시가 체결 (look-ahead bias 방지)
- **레짐 필터**: KOSPI/삼성전자 MA 기반 BULLISH/NEUTRAL/BEARISH 판단, BEARISH 시 SEPA 차단
- **설정 연동**: `default.yml` + `evolved_overrides.yml` 자동 머지
- **pykrx OHLCV**: pickle 캐싱, 2차 실행 시 데이터 로드 5초 이내
- **결과 출력**: 콘솔 요약 + CSV (거래 내역, 자산 추이, 요약)

### 6개월 백테스트 결과 (2025-09 ~ 2026-03, 150종목)
- 총 수익률: -8.81%, MDD: -17.49%, 승률: 58.8%, 손익비: 1.57
- SEPA -9.5% (약세장 손실 주도), RSI-2 +1.4% (유일 수익), Core -0.7% (1건 발동)

### CLI
```bash
python scripts/backtest_strategies.py --months 6 --strategies sepa,rsi2,core
python scripts/backtest_strategies.py --months 1 --universe-size 30  # 스모크 테스트
```

## 2026-03-14 (9차) — 전수 코드 리뷰 P0 7건 + P1 3건 수정 (6개 파일)

### P0 수정 (7건)
- `batch_analyzer.py`: `sig.metadata` None 접근 방어 (`(sig.metadata or {}).get(...)`) — 스캔 크래시 방지
- `batch_analyzer.py`: 프리장 R/R 재검증 `downside` 계산에 `abs()` 추가 — 프리장 급락 시 R/R 부호 오류 수정
- `batch_analyzer.py`: 코어 조기경보 중복 신호 방지 — `_exited_symbols` 세트로 이미 청산 발행된 종목 제외
- `batch_analyzer.py`: 텔레그램 알림 실패 silent swallow → 경고 로그 추가
- `risk/manager.py`: `get_risk_metrics()`에서 `effective_daily_pnl` AttributeError 방어 (`getattr` 패턴)
- `risk/manager.py`: `calculate_position_size()` 가용현금 음수 방어 (`max(0, cash - reserve)`)
- `exit_manager.py`: 재시작 정합성 검증에 부분체결 허용 버퍼 5% 추가 — 중복 매도 방지

### P1 수정 (3건)
- `strategy_evolver.py`: 진화 평가 거래 필터에 `entry_time >= applied` 조건 추가 — 변경 이전 진입 거래 제외
- `strategy_evolver.py`: `_clamp_value()` float 파라미터 타입 보존 — int 캐스팅 소수점 손실 방지
- `strategy_evolver.py`: 데이터 부족 시 자동 "keep" → "rollback" (보수적) — 미검증 파라미터 영구 정착 방지

### 기타 개선
- `types.py`: `RiskConfig.max_core_positions` 필드 추가 (기본 3) — max_core 하드코딩 제거
- `risk/manager.py`: 코어홀딩 상한 검증에서 config 참조 (`getattr(self.config, 'max_core_positions', 3)`)
- `exit_manager.py`: `highest_price` 영속화 시 `float()` → `str()` — Decimal 정밀도 보존

## 2026-03-14 — LLM 모델 업그레이드 + 프리장 시그널 재검증 (3개 파일)

### LLM 모델 업그레이드
- `src/utils/llm.py`: GPT 5.2→5.4, Gemini 3.0→3.1 (flash-lite-preview, pro-preview) 업그레이드

### 프리장 가격 기반 시그널 재검증 (NXT 대상 종목)
- `src/core/batch_analyzer.py`: `_premarket_revalidate()` 메서드 추가 — 08:20 스캔 후 09:01 실행 전 프리장 가격 변동 반영
  - 공통: 프리장 급락 ≤-5% → 시그널 취소 (악재 의심)
  - RSI-2: 프리장 반등 ≥+3% → 시그널 취소 (평균회귀 소멸)
  - SEPA/코어홀딩: 프리장 가격 기준 R/R < 1.3 → 시그널 취소
- `config/default.yml`: `premarket_revalidation` 설정 섹션 추가 (rsi2_bounce_cancel_pct, gap_down_cancel_pct, min_rr_ratio)

## 2026-03-14 — P0/P1/P2 개선 4개 항목 구현 (5개 파일)

### P0: 레짐 충돌 가드 (Regime Conflict Guard)
- `kr_scheduler.py`: `_resolve_regime_conflict()` 메서드 추가 — KOSPI 기술 레짐(bear/caution)이 LLM 레짐(trending_bull 등)과 충돌 시 안전한 쪽으로 조정
- `kr_scheduler.py`: `_apply_regime_to_exit_manager()`에서 충돌 해소 로직 적용
- `default.yml`: `regime_conflict_guard_enabled` 설정 추가 (기본 true)

### P1: 09:01 슬라이딩 윈도우 (시그널 분산 실행)
- `batch_analyzer.py`: `execute_pending_signals()`에 `signal_interval_sec` 간격 분산 실행 적용 — 장 초반 슬리피지 위험 분산
- `default.yml`: `kr.batch.signal_interval_sec: 30` 설정 추가

### P2: 코어홀딩 이벤트 기반 조기 경보
- `batch_analyzer.py`: `_monitor_core_positions()` 메서드 추가 — 수익률 -12% 조기경보, MA200 이탈 연속 3일 시 즉시 매도 시그널
- `batch_analyzer.py`: `monitor_positions()` 끝에 코어 조기경보 호출 추가
- `default.yml`: `early_ma200_alert_days`, `early_loss_alert_pct`, `early_rescore_alert` 설정 추가

### P1: 파라미터 민감도 분석 스크립트
- `scripts/sensitivity_analysis.py`: 신규 — 주요 전략 파라미터(점수, 익절, 트레일링) ±변동 시 과거 시그널/거래 영향 분석 도구

## 2026-03-14 (8차) — 코드+전략 심층 리뷰 P0 1건 + P1 3건 수정 (3개 파일)

### P0 수정 (1건)
- `strategy_evolver.py`: 진화 평가에서 `t.exit_time.date() > applied` → `>=` (적용 당일 거래 누락 → wait 상태 장기화 방지)

### P1 수정 (3건)
- `risk/manager.py`: core_holding `can_open_position`에서 3개 상한 가드 추가 — 리밸런싱 외 경로 코어 초과 진입 차단
- `strategy_evolver.py`: 진화 평가 PF 계산에서 `total_loss or 1` → `min(..., 99.9)` 상한 적용 (loss=0 시 PF 왜곡 방지)
- `us_scheduler.py`: `_check_exits` 전략 매도 실패 시 `strategy_exit_attempted=True` 고정 — ExitManager 폴백 중복 주문 차단

## 2026-03-14 — 동기화 포지션 분리 (정합성 이벤트 vs 전략 거래)

### 배경
동기화/복구로 생성된 포지션(entry_reason="sync_detected", SYNC_* ID)이 전략 통계(승률, 손익비, 진화)를 왜곡하는 문제. 의사결정 없는 포지션을 '정합성 이벤트'로 분류하고 리포트에서 분리.

### 변경 (5개 파일)
- `trade_journal.py`: `TradeRecord.is_sync` 프로퍼티 추가 — `entry_reason=="sync_detected"` 또는 `id.startswith("SYNC_")` 판별
- `trade_journal.py`: `get_statistics(exclude_sync=True)` — 통계에서 동기화 거래 기본 제외
- `trade_reviewer.py`: `review_period()` 시작 시 sync 거래 필터링 + 제외 건수 로깅
- `daily_reviewer.py`: `generate_trade_report()` — sync 거래 분리, `sync_events` 섹션으로 이력 보존 (통계 미포함)
- `strategy_evolver.py`: `_evaluate_active_change()` — 진화 평가 거래에서 sync 제외
- `data_collector.py`: `get_trade_events()` — 각 이벤트에 `is_sync` 플래그 추가 (대시보드 UI 분리용)

## 2026-03-14 (7차) — 코어홀딩 심층 코드+전략 리뷰 P0 1건 + P1 5건 수정 (4개 파일)
> commit: f568cce

### P0 수정 (1건)
- `strategy_evolver.py`: 주간 리밸런싱 비례 축소 시 core_holding도 함께 축소되던 버그 — 비대상 전략(core_holding)을 total에서 제외하고 valid 전략만 축소

### P1 수정 (5건)
- `exit_manager.py`: 코어 breakeven 활성화 시 `highest_price`를 현재가로 리셋 — 활성화 직후 고점 괴리로 즉시 트레일링(-8%) 발동 방지
- `exit_manager.py`: 코어 본전보호 버퍼 `0.25%`→`-2.0%` — +10% 도달 후 조정 시 장기 보유 허용
- `batch_analyzer.py`: 리밸런싱 손절 판단 3곳 `unrealized_pnl_pct`→`unrealized_pnl_net_pct` (수수료 포함, 대시보드와 일치)
- `batch_analyzer.py`: `buy_candidates` 전체 `portfolio.positions` 체크 — 스윙+코어 이중 보유 방지 (ExitManager state 충돌)
- `kr_scheduler.py`: 리밸런싱 오후 윈도우 `13:00-13:04` 추가 — 오전 3회 전부 실패 시 fallback

## 2026-03-14 (6차) — 코어홀딩 심층 코드+전략 리뷰 P0 4건 + P1 6건 수정 (6개 파일)
> commit: 018f390

### P0 수정 (4건)
- `batch_analyzer.py`: `monitor_positions()` 보유기간 10일 강제청산에서 코어홀딩 제외 — 11일째부터 매 30분 청산 시그널 발행 방지
- `batch_analyzer.py`: pending_buys 재시도 시 매도 미체결 확인 + 2일 초과 pending 자동 폐기 (가격 괴리 위험)
- `strategy_evolver.py`: `_apply_allocation_guardrails`에서 진화 비대상 전략(core_holding) 보존 — 주간 리밸런싱이 코어 30% 삭제하던 버그
- `evolved_overrides.yml`: `core_holding: 30.0` 명시 + `_meta`에 `manual_review_locked` 잠금

### P1 수정 (6건)
- `core_screener.py`: 배당 무조건 5점→0점 (데이터 미조회 시 변별력 없는 중립 방지)
- `core_screener.py`: PER 범위 확대 (5-20 5점, ≤35 3점, ≤60 1점) — 한국 대형 성장주 반영
- `data_collector.py`: `rebalance_day > 28` 가드 (2월 등 짧은 달 `ValueError` 방지)
- `data_collector.py`: 대시보드 수익률 `unrealized_pnl_pct`→`unrealized_pnl_net_pct` (수수료 포함)
- `run_trader.py`: `is_core` fallback — `position.strategy == "core_holding"` 직접 판별 (stage 파일 만료 시 안전)
- `batch_analyzer.py`: 스캔 후보 0건 시 기존 포지션 -10% 손실 체크만 별도 수행 (하락장 리밸런싱 불가 방지)

## 2026-03-14 (5차) — 코어홀딩 심층 코드+전략 리뷰 P0 5건 + P1 5건 수정 (5개 파일)
> commit: 43d7aa8

### P0 수정 (5건)
- `exit_manager.py`: 코어 트레일링스탑 미발동 — stage=NONE 고착(ratio=0 → 분할익절 없음 → stage 영구 NONE) → `or state.is_core` 조건 추가로 전량 매도 경로 확보
- `exit_manager.py`: 코어 본전보호 미작동 — `stage != NONE` 조건에 걸려 코어 본전보호 불가 → `or state.is_core` 추가
- `core_screener.py`: 시총 필터 dead code — `_min_market_cap_b` 설정만 있고 실제 필터링 없음 → StockMaster DB에서 시총 직접 조회 + 필터 적용 + 시총 순위 정렬
- `core_screener.py`: 수급 바이너리 스코어링 — 순매수면 10점/순매도면 1점 → 금액 기반 구간별 배점 (500억+/100억+/30억+ 각각 10/8/6점)
- `batch_analyzer.py`: 매도+매수 동시 발행 충돌 — 매도 미체결 상태에서 매수 발행 시 현금 부족 → 2단계 리밸런싱 (매도 선행 → pending_core_buys 저장 → 다음 윈도우에서 매수)

### P1 수정 (5건)
- `exit_manager.py`: 코어 exit ratio 미영속화/복원 — 재시작 시 글로벌 기본값(0.3) 적용 → `_persist_states`/`register_position`에 ratio 저장/복원 추가
- `exit_manager.py`: `_check_partial_exit`에 `is_core` 가드 추가 — ratio 복원 실패 시에도 분할 익절 안전 차단
- `core_screener.py`: PER=0 통과 + API 실패 점수 역설 — `per != 0 and per < 0` → `per <= 0`; 데이터 미조회 8점 > 소규모 매도 2점 역설 → 동일 2점
- `config.py`: evolved_overrides 전략파라미터 → `kr.strategies.{component}`에도 동시 머지 (theme_chasing enabled 등 미적용 해결)
- `config.py`: fallback strategy_allocation에 `core_holding: 30.0`, `strategic_swing: 0.0` 추가
- `risk/manager.py`: `defensive_strategies` 실제 전략명으로 수정 (`mean_reversion` 등 미사용 → `rsi2_reversal`, `core_holding`)

## 2026-03-13 (4차) — 코어홀딩 심층 코드+전략 리뷰 P0 2건 + P1 8건 수정 (8개 파일)

### P0 수정 (2건)
- `core_screener.py`: MA200 rolling 계산 수정 — 고정 MA200 대신 각 날짜별 rolling MA200으로 비교 (ma200_below_days 정확도)
- `kis_kr.py`: `fid_org_adj_prc: "0"→"1"` 수정주가 반영 (액면분할/무상증자 종목 MA200·수익률 왜곡 해결)

### P1 수정 (8건)
- `batch_analyzer.py`: 코어 매수 시그널 strength STRONG→NORMAL (1.5x 곱연산으로 2종목만 도달하는 문제 해결)
- `exit_manager.py`: 코어홀딩 본전보호 활성화 경로 추가 (is_core=True → trailing_activate_pct 도달 시 직접 활성화)
- `config.py`: evolved_overrides `exit_manager` → `kr.exit_manager` 동시 머지 추가
- `kr_scheduler.py`: 코어 리밸런싱 첫 윈도우 09:01→09:05 (기존 배치 실행과 시간 충돌 방지)
- `risk/manager.py`: max_positions에서 코어 포지션 제외 — 코어/단기 슬롯 경쟁 해소
- `core_screener.py`: 펀더멘탈 배당 중립 3→5점 (만점 30 달성 가능), StockMaster 장애 로그 ERROR 격상
- `core_screener.py`: `_score_trend`/`_score_momentum` 서브스코어 클램프 추가 (30/20점 상한)
- `types.py`: strategy_allocation에 strategic_swing 키 추가 (US SEPA cap 적용 가능)

## 2026-03-13 (3차) — 코어홀딩 최종 리뷰 P0/P1 6건 수정 (4개 파일)

- `batch_analyzer.py`: MA200 이탈 1일→연속 N일 체크, remaining_slots 음수 방어
- `batch_analyzer.py`: replace_threshold 1:1 매칭(과다 매도 방지), bool 반환
- `core_screener.py`: ma200_below_days 지표 추가, 수급 실패 시 중립 4점
- `kr_scheduler.py`: 리밸런싱 반환값 기반 재시도
- `types.py`: strategy_allocation default_factory에 core_holding 추가

## 2026-03-13 (2차) — 코어홀딩 P0/P1 버그 20건 일괄 수정 (9개 파일)

### P0 수정 (7건)
- `batch_analyzer.py`: remaining_slots 교체 매도 반영 (매도 후 빈 슬롯에 매수 가능)
- `batch_analyzer.py`: replace_threshold(+15점 교체)/ma200_break_days(MA200 이탈) 구현
- `core_screener.py`: 펀더멘탈 스코어 8→30점 확장 (ROE추정, EPS>0, 시총순위, PBR구간)
- `core_screener.py`: fetch_batch_valuations 30건→배치루프, 수급 순차→병렬 처리
- `config.py`: evolved_overrides risk_config→kr.risk 동시 머지 (전략배분 미적용 해결)
- `data_collector.py`: AppConfig 객체 접근 수정 (isinstance dict→hasattr trading)

### P1 수정 (13건)
- `core_holding.py`: stop_price 15% 하드코딩→config.stop_loss_pct, exc_info 추가
- `exit_manager.py`: stale_high is_core 가드, 코어 파라미터 영속화+복원
- `core_screener.py`: truthy패턴(or 0), 수급점수역전, 미사용코드, PER필터 수정
- `dashboard.js`: 예산 30% 하드코딩→서버 alloc_pct
- `kr_scheduler.py`: 리밸런싱 재시도 윈도우(09:01/09:30/10:00), 독스트링 수정
- `sse.py`: core_holdings 주기 10→30초

## 2026-03-13 — 코어홀딩 전체 흐름 검증 + P0/P1 수정 (7개 파일)

### P0 수정 (3건)
- **`src/strategies/exit_manager.py`**
  - `trailing_activate_pct` 포지션별 오버라이드 추가 (PositionExitState 필드 + register_position 파라미터 + update_price에서 사용). 코어 10%로 설정되나 글로벌 5%가 적용되던 문제 해결
  - 횡보 조기 청산(`stale_exit_days`)에 `not state.is_core` 가드 추가. 코어 포지션 5영업일 후 전량 청산 방지
  - 코어 포지션 ATR 동적 손절 비활성화 (`is_core`일 때 dynamic_stop 계산 건너뛰기). 15% 고정 손절이 6~7% ATR로 덮어씌워지던 문제 해결
- **`src/core/engine.py`** — `strategy_position_pct`에 `CORE_HOLDING: 10.0` 추가 (30%예산÷3종목). 25% 폴백으로 과대 사이징 방지

### P1 수정 (5건)
- **`src/core/batch_analyzer.py`** — `execute_core_rebalance()` 빈 슬롯만 매수 (교체 매도 미체결 상태에서 매수 시도 방지)
- **`src/schedulers/kr_scheduler.py`** — 08:20 불필요 스캔 제거 (09:01이 독립적으로 스캔하므로 API 2회 호출 낭비 해소)
- **`src/dashboard/static/js/dashboard.js`** — `applyMarketFilter()`에 코어홀딩 섹션 추가 (US 필터 시 숨김)
- **`src/dashboard/data_collector.py`** — budget 30%/max_positions 3 하드코딩 → 설정에서 읽도록 변경, batch_analyzer 중복 선언 정리
- **3개 register_position call site** — `trailing_activate_pct` 파라미터 전달 추가 (kr_scheduler.py ×2, run_trader.py ×1)

## 2026-03-13 — 코어홀딩 P0/P1 버그 수정 (4개 파일)

### P0 수정 (5건)
- **`src/signals/screener/core_screener.py`** — 전면 재작성
  - `StockMaster.get_all_stocks()` → `get_top_stocks(limit=150)` 사용 (기존 메서드 존재하지 않음)
  - `KISMarketData.get_market_cap_top()` 제거 → `fetch_batch_valuations()` 사용
  - `broker.get_daily_candles()` → `broker.get_daily_prices(symbol, days=250)` 사용
  - `KISMarketData.get_daily_prices()` 폴백 제거 (해당 메서드 없음)
  - dead ternary (`_get_daily_candles_sync` 분기) 제거 (P1 #7)
  - 수급 데이터 별도 `_enrich_supply_demand()` 메서드로 분리 (`fetch_stock_investor_daily()` 사용)
- **`src/schedulers/kr_scheduler.py`** — 2개 `register_position()` call site에 `is_core`/`max_holding_days` 전달 추가
- **`scripts/run_trader.py`** — 1개 `register_position()` call site에 `is_core`/`max_holding_days` 전달 추가

### P1 수정 (2건)
- **`src/signals/screener/core_screener.py`** — PBR 스코어링 순서 수정 (pbr<3 → 3점, pbr<5 → 2점, 좁은 범위 먼저)
- **`src/core/batch_analyzer.py`** — `execute_core_rebalance()`: 스캔에 포함되지 않은 종목은 유지 (스캔 실패 ≠ 기본 필터 미달)

## 2026-03-13 — KR 코어홀딩(Core Holding) 중장기 전략 구현 (17개 파일)

### 신규 파일 (2개)
- **`src/strategies/kr/core_holding.py`** — CoreHoldingStrategy (배치 시그널 생성, 100점 스코어링)
- **`src/signals/screener/core_screener.py`** — CoreScreener (대형주 유니버스→지표→스코어링)

### 핵심 변경
- **`src/core/types.py`** — `StrategyType.CORE_HOLDING`, `TimeHorizon.MEDIUM_TERM` 추가
- **`src/strategies/exit_manager.py`** — `PositionExitState`에 `is_core`/`max_holding_days` 추가, ratio=0 분할익절 비활성화 가드, `apply_regime_params()` 코어 포지션 스킵, 포지션별 max_holding_days 우선 적용
- **`config/default.yml`** — `kr.strategies.core_holding` 섹션 추가, `max_positions` 5→8, `strategy_allocation`에 `core_holding: 30.0`
- **`config/evolved_overrides.yml`** — `strategy_allocation` 재조정 (core_holding 30%, sepa 42%, rsi2 17.5%, theme 7%, gap 3.5%), `max_positions` 8
- **`scripts/run_trader.py`** — `_strategy_exit_params`에 core_holding 엔트리 추가 (SL 15%, TS 8%, 분할익절 비활성화), BatchAnalyzer에 core_holding config 전달
- **`src/core/batch_analyzer.py`** — 코어홀딩 전략/스크리너 초기화, `run_core_scan()`, `execute_core_rebalance()` 메서드 추가
- **`src/schedulers/kr_scheduler.py`** — `run_core_rebalance_scheduler()` 월초 리밸런싱 태스크 추가

### 대시보드
- **`src/dashboard/data_collector.py`** — `get_core_holdings()` 메서드 (코어 포지션, 요약, 리밸런싱 일정)
- **`src/dashboard/kr_api.py`** — `/api/core-holdings` GET 라우트
- **`src/dashboard/sse.py`** — `core_holdings` 이벤트 (10초 주기)
- **`src/dashboard/static/js/common.js`** — SSE eventTypes에 `core_holdings` 추가
- **`src/dashboard/templates/index.html`** — 코어홀딩 카드형 섹션 (KR 포지션 위)
- **`src/dashboard/static/js/dashboard.js`** — `renderCoreHoldings()` 함수 (카드형 레이아웃, 빈 슬롯, 비중 바)

### 설계 요약
- 전체 자본의 30%(~690만), 최대 3종목, 월 1회 리밸런싱
- 분할 익절 비활성화, 손절 -15%, 트레일링 고점 -8% (활성화: +10%)
- 시총 5000억+, 주가 5000원+, MA200 위, PER>0 필터
- ExitManager 코어 포지션: 레짐 오버라이드 제외, 보유기간 무제한
- 교체: 재스코어 < 55 또는 수익률 -10% 시 리밸런싱 매도

---

## 2026-03-13 — 2차 전체 코드 리뷰 + 전략 리뷰 일괄 수정 (19개 파일)

### P0 코드 수정 (5건)

- **`execution/broker/base.py:138`** — `from src.utils...` 절대 import → `from ...utils...` 상대 import (ModuleNotFoundError 방지)
- **`core/engine.py:1221`** — BUY 주문 `event.strategy.value` None 방어 누락 → `if event.strategy else "unknown"` 추가
- **`data/providers/kis_market_data.py:427`** — 캐시 타임스탬프 `time.time()`→`datetime.now()` (타입 불일치 TypeError 방지)
- **`core/engine.py:1134`** — `now` 변수 섀도잉 → `cash_warn_now` 분리 (stale 쿨다운 방지)
- **`scripts/futures_monitor.py:285`** — deprecated `asyncio.get_event_loop()` → `get_running_loop()`

### P1 코드 수정 (12건)

- **`core/evolution/daily_reviewer.py:186-187`** — `round(float(pnl))` 제거 (US $0.50 소수점 손익 보존)
- **`core/engine.py:1348`** — `event.quantity=None` 시 경고 로그 + pending 전체 해제 (영구 잠금 방지)
- **`core/engine.py:1337`** — Fill 폴백 생성 시 `strategy` 메타데이터 전달 추가
- **`core/engine.py:1036`** — `time_val` 중복 계산 삭제 (1001행과 중복)
- **`core/evolution/strategy_evolver.py:286`** — `_save_state()` try/except 래핑 (디스크 풀 시 크래시 방지)
- **`signals/screener/us_screener.py:344`** — RSI 계산 SMA→Wilder's Smoothing 교체 (전략 모듈과 일관성)
- **`strategies/kr/theme_chasing.py:187,195`** — ThemeInfo 객체 `.get()` 호출 전 `isinstance(dict)` 타입 체크
- **`data/feeds/kis_websocket.py:258`** — `create_task` fire-and-forget → `_rebuild_task` 인스턴스 변수 + done_callback
- **`strategies/us/sepa_trend.py:68`** — `sepa_pass += 0.5` → `+= 1` (int/float 혼합 방지)
- **`dashboard/kr_api.py`** — Yahoo Finance `ClientSession` 매 호출 생성 → 함수 레벨 1회 생성/재사용
- **`dashboard/sse.py`** — SSE `_http_session` lazy 생성/재사용 + `stop()` async 전환
- **`dashboard/server.py:148`** — `sse_manager.stop()` → `await sse_manager.stop()` (async 호환)

### P0 전략 수정 (3건)

| 항목 | 변경 전 | 변경 후 | 파일 |
|------|--------|--------|------|
| KR exit second/third_exit_pct | 10%/12% | 15%/25% | `default.yml` (코드 기본값 동기화) |
| KR min_stop_pct | 2.5% | 3.5% | `evolved_overrides.yml` (whipsaw 방지) |
| KR max_positions | 7(evolved)/10(default) | 5/5 | 양쪽 동기화 (자본 대비 현실적) |

### P1 전략 수정 (8건)

| 항목 | 변경 전 | 변경 후 | 파일 |
|------|--------|--------|------|
| KR 모멘텀 stop/tp/trailing | 2%/5%/1.5% | 5%/15%/3% | `kr/momentum.py` (ExitManager 정렬) |
| 테마추종 stop_loss | 2.5% | 3.5% | `evolved_overrides.yml` |
| US 모멘텀 min_score | 50 | 65 | `default.yml` |
| US 어닝스 stop_loss | 8.0% | 5.5% | `default.yml` |
| 진화 stop 하한 | 1.5% | 3.0% | `strategy_evolver.py` |
| 갭앤고 entry_end_time | 11:30 | 10:30 | `gap_and_go.py` |
| SEPA T-2 min_score 하한 | 45 | 50 | `sepa_trend.py` |
| trending_bear stop | 2.5% | 3.5% | `exit_manager.py` |

---

## 2026-03-11 — 전체 코드 리뷰 + 전략 리뷰 일괄 수정 (22개 파일)

### P0 코드 수정 (치명적)

**`src/strategies/exit_manager.py`** — `or` 금지 패턴 전면 교체 (16곳)
- `first/second/third_exit_pct or config` → `is not None` 패턴 (분할 익절 0.0 무시 방지)
- `dynamic_stop_pct or stop_loss_pct or config` → 3단 `is not None` 체인 (손절률)
- `trailing_stop_pct or config` → `is not None` (트레일링)
- `current_price or avg_price` → `is not None and > 0` (고점 추적 오동작 방지)
- `atr_pct or 2.0` → `is not None` (ATR 기본값)
- `initial_quantity` 0 falsy → `is not None` (재시작 정합성)
- ATR 승수 `* 1.5` 하드코딩 → `ExitConfig.atr_trailing_multiplier` 필드

**`src/risk/manager.py`** — 손절/익절 0.0 falsy 방지
- `if position.stop_loss and ...` → `is not None and ...` (3곳)
- `can_open_position()` 일일 손실: `daily_pnl` → `effective_daily_pnl` (미실현 포함)

**`src/utils/telegram.py`** — 이벤트 루프 내 `asyncio.run()` 충돌 수정
- `send_sync/send_alert_sync`: 실행 중 루프 감지 → `create_task()` / `asyncio.run()` 분기

**deprecated `asyncio.get_event_loop()`** → `get_running_loop()` 교체
- `stock_master.py`, `kr_scheduler.py`, `batch_analyzer.py`

**`scripts/run_trader.py`** — fire-and-forget Task 예외 소실 방지
- `create_task()` 반환값 저장 + `add_done_callback()`

**`src/schedulers/kr_scheduler.py`** — Decimal×float 혼합 방지
- `pnl_pct` 계산에 `float()` 명시 변환

### P1 코드 수정 (중요)

- **`engine.py`**: `on_market_data/on_theme` 반환값 `None` → `[]`, `or 0` 패턴 5곳 수정
- **`us_scheduler.py`**: bare `except Exception: pass` → 최소 로깅 (10곳), `or` 패턴 6곳
- **`kr_scheduler.py`**: `or` 금지 패턴 4곳 수정
- **`llm.py`**: `model or config` → `is not None` (빈 문자열 보호)
- **`data_collector.py`**: pykrx 최상단 import → lazy import
- **`us_screener.py`**: `scan_date: date = None` → `Optional[date]`
- **`swing_screener.py`**: `if ma200 and close` 금지 패턴 4곳 수정

### 전략 파라미터 조정

| 항목 | 변경 전 | 변경 후 | 파일 |
|------|--------|--------|------|
| KR 테마 max_change_pct | 12% | 8% | `theme_chasing.py` |
| US 모멘텀 min_breakout_pct | 0.3% | 1.0% | `default.yml` |
| US base_position_pct | 40% | 25% | `default.yml` |
| US max_position_pct | 50% | 35% | `default.yml` |
| KR max_positions_per_sector | 3 | 2 | `default.yml` |
| ranging 레짐 stop_loss | 3.0% | 4.0% | `exit_manager.py` |
| ranging 레짐 trailing_stop | 2.0% | 2.5% | `exit_manager.py` |
| 진화 최소 거래 수 | 5건 | 10건 | `strategy_evolver.py` |
| 진화 평가 기간 | 3일 | 5일 | `strategy_evolver.py` |

### 전략 코드 수정

- **`us/momentum.py`**: RS Ranking 감점을 min_score 체크 이전으로 이동
- **`kr/sepa_trend.py`**: 적자(PER<0) -5점, 고PBR(>10) -3점 감점 추가
- **`kr/gap_and_go.py`**: Decimal vs int 비교 → `Decimal(str(...))` 명시
- **`kr/momentum.py`**: float vs int 비교 → `float(...)` 명시
- **`kr/sepa_trend.py`, `kr/rsi2_reversal.py`**: `or 0` 금지 패턴 8곳 수정

---

## 2026-03-11 — 대시보드 전광판 US 지수 표시 수정

**`src/dashboard/sse.py`**
- `import aiohttp` 누락 수정 — `from aiohttp import web`만 있어 `aiohttp.ClientSession`/`ClientTimeout` NameError 발생
- Yahoo Finance API 호출이 silent fail → US 지수(S&P500, NASDAQ, DOW) 전광판 미표시 원인
- 수정 후 KOSPI, KOSDAQ, S&P500, NASDAQ, DOW, 개별주 모두 정상 표시

---

## 2026-03-11 — fetch_index_price TR 수정 (commit `edd809b`)

`fetch_index_price()`에서 잘못된 TR 사용 수정:
- `FHKUP03500100` → **`FHPUP02100000`** (업종지수 현재가 API)
- `FID_COND_MRKT_DIV_CODE="U"` = 업종(業種) 코드, US시장 코드가 아님
- KOSPI `0001` / KOSDAQ `1001` 모두 실시간 정상 반환 확인

---

## 2026-03-11 — 재시작 익절 미실행 버그 수정 + 대시보드 지수 실시간화 (commit `2b1b36a`)

### 문제
봇 재시작 시 분할 익절 stage가 파일에 먼저 기록된 뒤 주문/체결 이전에 종료되면,
다음 기동 시 stage=FIRST(혹은 그 이상)지만 실제 매도는 없는 불일치 상태 발생.
→ 1차 익절 등 이전 단계가 영구 스킵됨.

### 핵심 수정 — ExitManager pending_stage 패턴

**`src/strategies/exit_manager.py`**

- **`pending_stage` 필드 추가** (`PositionExitState`): fill 확인 전 임시 목표 stage 보관.
  파일에 저장 안 함 → 재시작 시 None → current_stage=NONE → 1차 익절 자동 재발행.
- **`update_price()`**: `state.current_stage = ExitStage.FIRST` 대신 `state.pending_stage = ExitStage.FIRST`.
  stage가 파일에 저장되는 시점을 fill 이후로 이연.
- **`on_fill()`**: fill 확인 후 `pending_stage → current_stage` 승격. stage advance의 유일한 지점.
- **`rollback_stage()`**: pending_stage 먼저 클리어 (fill 미수신). 없으면 current_stage 한 단계 롤백 (레거시).

### 재시작 정합성 검증 (initial_qty)

- **`_persist_states()`**: `initial_qty` 추가 저장.
  - stage=NONE: 현재 수량 기록 (최초 진입 수량).
  - stage>NONE: 기존 파일 값 보존 (부분 매도 후 재시작 시 post-sell qty 덮어쓰기 방지).
- **`register_position()`**: 파일의 `initial_qty` 로드 후 정합성 검증.
  `stage≠NONE AND KIS_qty > expected_after_1st` → stage NONE 리셋 → 자동 재발행.

### 대시보드 지수 실시간화

**`src/data/providers/kis_market_data.py`**
- `fetch_index_price(index_code)` 추가: KIS `FHKUP03500100` KOSPI(0001)/KOSDAQ(1001) 실시간 조회.
  10초 캐시, 실패 시 Yahoo Finance 폴백.

**`src/dashboard/sse.py`**
- `_fetch_market_indices()` 추가: KIS 실시간 → Yahoo Finance 폴백 (5종목 통합).
  결과를 `/api/market/indices` HTTP 캐시와 동기화.
- 브로드캐스트 루프에 `market_indices` 이벤트 추가 (10초 주기 push).

**`src/dashboard/static/js/common.js`**
- `SSEClient` 이벤트 타입에 `market_indices` 추가.
- `_applyTickerData()` 공통 함수 분리 (SSE/HTTP 폴링 공유).
- `fetchNavIndices()` 폴링 주기: 30s → 60s (SSE가 주채널).

**`src/dashboard/kr_api.py`**
- `/api/market/indices` HTTP 캐시 TTL: 30s → 10s.

---

## 2026-03-11 — SEPA 코어+트레이더 청산 구조 + 추세 무효화 시간 스탑

### 수정 파일
| 파일 | 변경 내용 |
|------|----------|
| `src/strategies/exit_manager.py` | 전략별 분할 비율(ratio) 지원 + 신고가 실패 시간 스탑(stale_high_days) 추가 |
| `scripts/run_trader.py` | SEPA 코어+트레이더 프로필 + sync_detected 보수적 리스크 프로필 |
| `src/schedulers/kr_scheduler.py` | 체결/동기화 시 전략별 ratio/stale_high_days 파라미터 전달 |
| `src/schedulers/us_scheduler.py` | sync 포지션 등록 시 보수적 리스크 파라미터 적용 |

### 상세

**1. SEPA 코어+트레이더 구조 (큰 추세 수익 극대화)**
- 기존: 1차(30%) → 2차(50%) → 3차(50%) = 원래 수량의 ~82%가 12% 이전 청산
- 변경: 1차(20%) → 2차(25%) → 3차(25%) = ~42%만 고정 TP로 청산, 나머지 코어는 트레일링
- 3차 익절 목표: 12% → 15%로 상향
- PositionExitState에 전략별 `first/second/third_exit_ratio` 필드 추가

**2. 신고가 실패 시간 스탑 (추세 무효화 감지)**
- ExitConfig에 `stale_high_days`, `stale_high_min_pnl_pct` 추가
- PositionExitState에 `last_new_high_date`, `stale_high_days` 추가
- SEPA: 3영업일 신고가 갱신 실패 + PnL < 3% → 전량 청산
- 기회비용 절감: 장기 방치 손실 방지

**3. sync_detected 보수적 리스크 (회피 패턴 방지)**
- `_sync` 전략 프로필 신설: SL=3%, TS=2%, TP1=3%/TP2=5%/TP3=8%
- sync 포지션은 2영업일 신고가 실패 시 즉시 청산
- KR/US 동기화 경로 모두 적용

---

## 2026-03-10 — RS Ranking pykrx → yfinance 전환

### 수정 파일
| 파일 | 변경 내용 |
|------|----------|
| `src/signals/screener/kr_screener.py` | `_apply_rs_ranking_bonus`: pykrx `get_index_ohlcv` → yfinance `^KS11` |

### 상세

**pykrx `get_index_ohlcv` KeyError: '지수명'**
- 원인: KRX 웹사이트 데이터 형식 변경 → pykrx 1.2.4 내부 `IndexTicker.get_name()` 실패
- 영향: 5분마다 `[Screener] RS Ranking 보너스 오류 (무시): '지수명'` 반복 (하루 200회+)
- 수정: KOSPI 벤치마크 조회를 yfinance `^KS11`로 전환, MultiIndex 컬럼 처리 추가
- pykrx는 다른 용도(종목 마스터 등)에서 여전히 사용 중이나 index OHLCV는 yfinance로 대체

---

## 2026-03-10 — US entry_time DB 복원 + 횡보 종목 조기 청산

### 수정 파일
| 파일 | 변경 내용 |
|------|----------|
| `scripts/run_trader.py` | US 포지션 entry_time을 trades 테이블에서 복원 (datetime.now() → 실제 매수 시점) |

### 상세

**US entry_time 재시작 시 리셋 문제**
- 기존: `_initialize_us`에서 `entry_time=datetime.now()` → 매번 보유기간 0일로 초기화
- 수정: TradeStorage 초기화 후 DB(trades 테이블)에서 실제 매수 시점 복원
- KR과 동일 패턴 (`_restore_position_metadata` 방식)
- 효과: 보유기간 초과 청산 + 횡보 청산이 재시작 후에도 정상 동작

---

## 2026-03-10 — 횡보 종목 조기 청산 로직 추가

### 수정 파일
| 파일 | 변경 내용 |
|------|----------|
| `src/strategies/exit_manager.py` | `stale_exit_days` / `stale_exit_pnl_pct` 설정 + 횡보 청산 로직 |
| `config/default.yml` | KR: 5영업일/±2%, US: 7영업일/±3% 기본값 |

### 상세

**횡보 조기 청산**
- 조건: N영업일 이상 보유 & |수익률| < X% & 1차 익절 전(stage=NONE)
- KR: 5영업일 보유 & ±2% 이내 → 전량 매도
- US: 7영업일 보유 & ±3% 이내 → 전량 매도
- 1차 익절 완료 후에는 적용 안 됨 (수익 중인 포지션 보호)
- 기존 보유기간 초과(KR 10일/US 20일)와 손절은 별도로 동작
- `evolved_overrides.yml`에서 오버라이드 가능

---

## 2026-03-10 — US 미체결 주문 타임아웃 누수 수정

### 수정 파일
| 파일 | 변경 내용 |
|------|----------|
| `src/schedulers/us_scheduler.py` | `_check_orders` history 비어있을 때 즉시 return 제거 → 타임아웃 항상 체크 |
| `src/schedulers/us_scheduler.py` | pending 타임아웃 시 매도 stage 롤백 + 시장가 재시도 로직 추가 |
| `src/schedulers/us_scheduler.py` | `_recover_pending_orders` 고아 주문 감지/취소 + nccs 폴백 추가 |
| `src/execution/broker/kis_us.py` | `get_outstanding_orders()` 미체결 전용 API (inquire-nccs) 추가 |
| `src/execution/broker/kis_us.py` | `get_order_history` output1 비어있을 때 응답 키 디버그 로깅 |

### 상세

**P0: _check_orders history 빈 결과 시 pending 영구 잔류**
- `get_order_history()`가 빈 결과 반환 시 `if not history: return`으로 즉시 종료
- 이후 모든 타임아웃 로직(매도 2분, 매수 10분, 부분체결, 시장가 폴백)에 도달 불가
- 수정: `history = history or []`로 처리, 빈 history에서도 pending 타임아웃 체크 진행

**P1: 매수 주문 포트폴리오 기반 체결 감지**
- 매수 pending인데 포지션에 이미 존재 → 체결로 간주하여 pending 즉시 정리
- 30초 유예 후 감지 (포트폴리오 동기화 시차 고려)

**P1: 매도 타임아웃 시 stage 롤백 누락**
- 매도 pending이 타임아웃/취소로 정리될 때 ExitManager stage 롤백 미호출
- 수정: `rollback_stage()` 호출 + 정규장에서 시장가 재주문

**P1: 고아 주문 감지 부재 (재시작 시)**
- `_recover_pending_orders`에서 고아 매도 주문 발견 시 취소 + stage 롤백
- `inquire-ccnl` 빈 결과 시 `inquire-nccs` (TTTS3018R) 미체결 전용 API 폴백

---

## 2026-03-09 — 전체 코드 리뷰 P1 잔여 이슈 8건 수정

### 수정 파일
| 파일 | 변경 내용 |
|------|----------|
| `src/strategies/exit_manager.py` | 보유기간 달력일→영업일 변환 + `_count_business_days()` 메서드 추가 |
| `src/schedulers/kr_scheduler.py` | 매도 체결 시 `exit_manager.on_fill()` 호출 추가 (remaining_quantity 즉시 갱신) |
| `src/schedulers/us_scheduler.py` | 매도 체결 시 `on_fill()` 호출 + exit_check_loop stage 복원 전 대기 |
| `src/data/feeds/kis_us_price_ws.py` | approval_key 무효화 감지 (즉시 끊김 3회) + backoff 리셋 조건 수정 |
| `scripts/run_trader.py` | `_USEngineBundle._running = True`로 통일 |
| `src/risk/manager.py` | `_consecutive_losses` 재시작 시 daily_stats에서 복원 |
| `src/dashboard/static/js/common.js` | SSE eventTypes에서 미전송 `health_checks` 제거 |

### 상세

**P1-1: on_fill 미호출 → remaining_quantity 30초 지연 문제**
- KR: 매도 체결(fill_check) 시 `exit_manager.on_fill()` 즉시 호출
- US: 매도 체결(_check_orders) 시 `on_fill()` 호출 (on_position_closed 전)
- 효과: 분할 매도 후 다음 update_price까지 중복 시그널 방지

**P1-3: 보유기간 달력일→영업일**
- KR: `is_kr_market_holiday()` 사용 (주말+공휴일 제외)
- US: 주말 제외 (exchange_calendars 의존성 회피)

**P1-6: US exit_check_loop stage 복원 전 실행 방지**
- `_exit_stages_restored` 플래그 확인, 미복원 시 5초 대기 후 continue

**P1-7+8: US WS approval_key 무효화 감지 + backoff 수정**
- KR WS와 동일 패턴: 메시지 0개 수신 후 3회 연속 즉시 끊김 감지 → 키 초기화
- backoff 리셋: 메시지 수신 성공 시에만 BASE로 리셋 (비정상 종료 시 지수 백오프 유지)

**P1-9: _USEngineBundle running 불일치**
- `_running = False` → `_running = True`로 수정 (running과 동일)

**P1-11: _consecutive_losses 재시작 미복원**
- `_load_daily_stats()`에서 `daily_stats.consecutive_losses` → `_consecutive_losses` 동기화

**P1-12: SSE health_checks 미전송 이벤트 정리**
- common.js eventTypes에서 제거 (REST 폴링으로 정상 동작)

## 2026-03-09 — 전체 코드 리뷰 P0/P1 이슈 수정

### 수정 파일
| 파일 | 변경 내용 |
|------|----------|
| `src/schedulers/us_scheduler.py` | P0: `_execute_exit` 반환값(bool) 추가 — 매도 실패 시 stage 롤백 + ExitManager 폴백 허용 |
| `src/strategies/exit_manager.py` | P1: `rollback_stage()`에 `_persist_states()` 추가, `remove_position()`에 영속화+_persisted 정리 추가 |
| `src/schedulers/kr_scheduler.py` | P1: 유령 포지션 제거 시 `_states.pop()` → `remove_position()` 호출로 변경 (영속화 포함) |
| `src/analytics/daily_report.py` | P0: 야간선물 dict 키 안전 접근 (`nf["key"]` → `nf.get("key")`) |

### 상세

**P0-1: US 매도 주문 실패 시 ExitManager stage 롤백 누락**
- 문제: `_execute_exit` 실패 시 stage만 올라가고 실제 매도 안 됨 → 해당 익절 단계 영구 건너뜀
- 수정: 실패 시 `rollback_stage()` 호출 + `return False`

**P0-2: US 전략 exit 실패 시 ExitManager 폴백 누락**
- 문제: `strategy_exit_attempted=True`인데 주문 실패 → 손절/ExitManager 체크 모두 스킵
- 수정: `_execute_exit` 반환값으로 실제 성공 여부 판단, 실패 시 ExitManager 폴백

**P0-3: 야간선물 dict KeyError**
- 문제: `nf["price"]`, `nf["change_pct"]` 직접 접근 → 키 누락 시 레포트 전체 실패
- 수정: `nf.get()` 패턴으로 안전 접근, None 시 조기 반환

**P1-2: rollback_stage 영속화 누락**
- 문제: 롤백 후 재시작 시 롤백 전 stage가 복원됨
- 수정: `_persist_states()` 호출 추가

**P1-5: 유령 포지션 정리 불완전**
- 문제: KR `_states.pop()` 직접 사용 → `_entry_times`, `_persisted`, stage 파일 미정리
- 수정: `remove_position()` 호출로 통일 (영속화 포함)

## 2026-03-09 — 대시보드 성과+자산 탭 통합

### 수정 파일
| 파일 | 변경 내용 |
|------|----------|
| `src/dashboard/templates/performance.html` | 성과+자산 탭 통합 레이아웃 (6 요약카드 + 총자산 차트 + 전략/청산 분석 + 일별 히스토리 + KR/US 비교) |
| `src/dashboard/static/js/performance.js` | 통합 JS (equity.js 기능 흡수: 일별 포지션 확장, KR/US 비교 차트/테이블) |
| `src/dashboard/server.py` | `/equity` → `/performance` 리다이렉트 |
| `src/dashboard/templates/*.html` (7개) | 네비게이션에서 "자산" 링크 제거 (8탭→7탭) |
| `src/dashboard/templates/equity.html` | **삭제** |
| `src/dashboard/static/js/equity.js` | **삭제** |

### 상세

**성과 탭 통합 레이아웃**
- ① 6개 요약 카드: 총자산, 기간수익률, MDD, 거래수, 승률, PF
- ② 총자산 추이 차트 (Plotly, 일별 손익 바 포함)
- ③ 전략별 승률/거래수 차트 + 청산유형별 평균수익률 차트
- ④ 전략별 성과 테이블
- ⑤ 일별 히스토리 테이블 (expandable 포지션 상세)
- ⑥ KR/US 비교 (수익률 차트 + 일별 대조 테이블)
- 기간 탭: 1주/1개월/3개월/전체
- 마켓 필터: 통합/국내/미국

**네비 정리**
- "자산" 탭 제거, `/equity` 접속 시 `/performance`로 자동 리다이렉트
- 7개 탭: 실시간 → 거래 → 성과 → 테마 → 복기 → 엔진 → 설정

## 2026-03-09 — 텔레그램 아침 레포트에 KOSPI200 야간선물 등락률 추가

### 수정 파일
| 파일 | 변경 내용 |
|------|----------|
| `src/analytics/daily_report.py` | 07:00 US 마감 레포트에 야간선물 섹션 추가 + 08:00 아침 레포트 US 요약에 1줄 추가 |

### 상세

**07:00 미국증시 마감 레포트 (`generate_us_market_report`)**
- 주요 지수 섹션 바로 뒤에 "■ KOSPI200 야간선물" 섹션 추가
- `get_night_futures_quote()` 호출 → `🔼 +1.23% (345.67pt) 강세` 형태 표시
- 조회 실패 시 해당 섹션 skip (나머지 레포트 정상 발송)

**08:00 아침 레포트 (`_fetch_us_market_summary`)**
- US 시장 요약 끝에 `KOSPI200 야간선물 ▲1.23%` 1줄 추가
- 조회 실패 시 skip

**헬퍼 메서드 추가**
- `_get_night_futures_quote()`: KISMarketData 인스턴스 획득 + 야간선물 시세 조회
- `_fetch_night_futures_section()`: 07:00 레포트용 HTML 포맷 섹션 생성

## 2026-03-09 — ExitManager 분할 익절 로직 개선 (3건)

### 수정 파일
| 파일 | 변경 내용 |
|------|----------|
| `src/strategies/exit_manager.py` | ATR트레일링 조기 전량 청산 방지 + 재시작 시 고점 보정 + max_holding_days config 주입 |

### 상세

**P1: ATR트레일링 분할 익절 전 조기 전량 청산 방지**
- 문제: breakeven 활성화 후 ATR트레일링이 stage에 관계없이 전량 매도 → 1차 익절 직후 2차/3차 기회 소멸
- 사례: 삼성중공업 09:06 1차 익절(60주) → 09:15 ATR트레일링 전량 청산(142주), 9분 만에 분할 종료
- 수정: THIRD/TRAILING stage에서만 ATR트레일링 전량 매도, FIRST/SECOND에서는 고점을 현재가로 리셋하여 분할 익절 우선

**P1: 재시작 시 highest_price 과도 괴리 보정**
- 문제: 저장된 고점이 현재가보다 5% 초과 높으면 첫 가격 업데이트에서 즉시 트레일링 발동
- 수정: register_position() 복원 시 괴리 5% 초과면 현재가로 리셋 + WARNING 로그

**P2: max_holding_days config 주입**
- 문제: ExitManager._max_holding_days가 10일 하드코딩, 외부 설정 불가
- 수정: ExitConfig.max_holding_days 필드 추가, config에서 주입 가능

## 2026-03-08 — 엔진 탭 대시보드 구현

### 개요
- 자가수정 에이전트 상태 + 엔진 로그 + LLM 운영 루프를 통합 표시하는 "엔진" 탭 신규 추가
- 기존 7개 탭 → 8개 탭 (실시간/거래/성과/자산/테마/복기/**엔진**/설정)

### 신규 파일
| 파일 | 설명 |
|------|------|
| `src/dashboard/engine_api.py` | `/api/engine/*` REST API 6개 엔드포인트 |
| `src/dashboard/templates/engine.html` | 엔진 탭 HTML (5섹션 레이아웃) |
| `src/dashboard/static/js/engine.js` | API 호출 + 렌더링 + 자동 폴링 |

### 수정 파일
| 파일 | 변경 내용 |
|------|----------|
| `server.py` | engine_api import + `/engine` 라우트 + API 등록 |
| `index.html` | nav에 "엔진" 탭 추가 |
| `trades.html` | nav에 "엔진" 탭 추가 |
| `performance.html` | nav에 "엔진" 탭 추가 |
| `equity.html` | nav에 "엔진" 탭 추가 |
| `themes.html` | nav에 "엔진" 탭 추가 |
| `evolution.html` | nav에 "엔진" 탭 추가 |
| `settings.html` | nav에 "엔진" 탭 추가 |

### API 엔드포인트
| 엔드포인트 | 설명 |
|-----------|------|
| `GET /api/engine/healer/status` | self-healer 서비스 상태 (5초 캐시) |
| `GET /api/engine/healer/history` | 수정 이력 (최근 50건) |
| `GET /api/engine/logs` | 엔진 로그 (NOISE 필터, 레벨 화이트리스트) |
| `GET /api/engine/llm-regime` | LLM 레짐 분류 현황 |
| `GET /api/engine/daily-bias` | Daily Bias 보정값 |
| `GET /api/engine/false-negatives` | False Negative 분석 |

### 설계 문서
- `docs/engine-tab-design.md` 기반 구현
- P0 리뷰 반영: 비동기 subprocess, 입력 화이트리스트, 메모리 캐시

---

## 2026-03-08 — P0/P1 보안·안전성 패치 (코드 리뷰 후속)

### Batch 1: 보안 긴급 수정 (self-healer)
| 파일 | 이슈 | 수정 내용 |
|------|------|----------|
| `rollback.py` | P0-1: sudo 비밀번호 하드코딩 | `sudo -n` (NOPASSWD sudoers) 전환, 비밀번호 제거 |
| `rollback.py` | P0-3: proc.kill() 후 wait() 미호출 | 모든 kill() 후 wait() 추가, 좀비 프로세스 방지 |
| `rollback.py` | P0-4: re.compile(user_input) ReDoS | 정규식 → 단순 `in` 문자열 매칭 전환 |
| `healer_agent.py` | P0-2: --dangerously-skip-permissions | `--allowedTools` 화이트리스트 전환 (Read,Edit,Write,Glob,Grep + git/py_compile) |
| `healer_agent.py` | P0-2: git add -A 무차별 스테이징 | 프롬프트에서 수정 파일만 add 지시 (.env 등 방지) |
| `error_watcher.py` | P1-5: 동기 tail_journal 이벤트루프 블로킹 | `asyncio.create_subprocess_exec` + `async for` 비동기 전환 |
| `error_watcher.py` | P1-6: T3 무제한 LLM 호출 | `can_fix()` 일일 한도 체크 추가 |

### Batch 2: 거래 안전성 (kr_scheduler.py)
| 이슈 | 수정 내용 |
|------|----------|
| P0-5: LLM exit_today 무검증 SELL | 가격 데이터 검증 + 수익 3%+ 포지션은 ExitManager 위임 |
| P1-2: trailing_stop_pct None TypeError | None 시 기본값 3.0% 적용 |
| P1-3: Decimal/float 혼합 | `pos.entry_price` 등 None 체크를 `is not None`으로 통일 |

### Batch 3: 코드 품질
| 파일 | 이슈 | 수정 내용 |
|------|------|----------|
| `daily_reviewer.py` | P1-7: `current and suggested` 금지 패턴 | `is not None` 패턴으로 수정 |

### 인프라
- `/etc/sudoers.d/qwq-self-healer` NOPASSWD 규칙 설정 완료

---

## 2026-03-08 — 자가수정 에이전트 (Self-Healer) 구현
> `scripts/self_healer/` 전체 신규

### 개요
- journalctl 실시간 감시 → 오류 발생 시 Claude Code 자동 호출 → 코드 분석·수정·재배포
- 3티어 분류: T1(자동수정), T2(승인 후 배포), T3(분석만 보고)

### 신규 파일
| 파일 | 설명 |
|------|------|
| `error_watcher.py` | 메인 데몬 — journalctl tail + 패턴 매칭 + 디바운싱(30초) |
| `error_classifier.py` | 오류 분류 + 스택트레이스에서 파일/라인 추출 |
| `healer_agent.py` | Claude Code `--dangerously-skip-permissions -p` 호출 + 결과 파싱 |
| `rollback.py` | pre-fix 해시 저장 + 60초 검증 + git revert 자동 롤백 |
| `notifier.py` | 텔레그램 알림 (T1 완료/T2 승인/T3 보고) + 승인 폴링 |
| `patterns.yaml` | 오류 패턴 라이브러리 (NOISE 15개 + T1 10개 + T2 10개 + T3 10개) |
| `state.json` | 일일 수정 카운터 + 쿨다운 상태 |
| `qwq-self-healer.service` | systemd 서비스 파일 |

### 안전장치
- 하루 최대 3회 자동 수정, 수정 간 5분 쿨다운
- 수정 후 60초 모니터링 → 동일 오류 재발 시 자동 롤백
- T1 반복 3회 → T2 승격 (텔레그램 승인 필요)
- 프로세스 락 파일로 동시 실행 방지

---

## 2026-03-08 — LLM 운영 루프 고도화 (6개 기능 추가)
> `kr_scheduler.py`, `batch_analyzer.py`, `daily_reviewer.py`, `strategy_evolver.py`, `default.yml`

### 1. daily_bias.json 피드백 루프
- `DailyReviewer._save_daily_bias()`: 매일 20:30 LLM 리뷰 후 운영 바이어스 추출
- assessment, sepa/rsi2 score boost, avoid_entry_before, top_lesson 저장
- 익일 배치 스캔에서 자동 반영 (피드백 루프 단절 해소)

### 2. 08:10 LLM 시장 레짐 분류기
- `_run_llm_regime_classifier()`: US 마감 + KOSPI 5일/20일 + 전날 bias 기반
- Gemini Flash로 trending_bull/ranging/trending_bear/turning_point 분류
- `llm_regime_today.json` 저장 → 배치 스캔에서 전략 우선순위 반영

### 3. 배치 스캔 후 LLM 컨텍스트 필터
- `BatchAnalyzer._llm_rank_candidates()`: 배치 후보에 LLM 필터 적용
- regime → lead_strategy 기반 전략별 score 조정
- daily_bias → sepa/rsi2 score boost 적용
- 5개+ 후보 시 Gemini Flash 우선순위 재조정 (priority +3, exclude -8)

### 4. 15:00 포지션 LLM 종가 점검
- `_run_position_eod_llm_check()`: 장 마감 전 보유 포지션 LLM 판단
- action: exit_today → 즉시 SELL 시그널, tighten → 트레일링 -0.5%, hold → 유지
- 텔레그램 간단 보고

### 5. LLM Verify 재설계 (score 구간별 차등)
- 90점+ → 항상 LLM 검증
- 75~89점 → 거래대금 급증(vol_ratio≥2.0) OR 외인 순매수 시에만 검증
- 75점 미만 → LLM 검증 없음 (기존: 95점 이상만)

### 6. False Negative 분석 (주간)
- `_analyze_false_negatives()`: 주간 리밸런싱 시 놓친 폭등(+8%↑) 종목 분석
- pykrx 상승 종목 vs 배치 스캔 결과 비교 → LLM 패턴 분석
- `false_negative_patterns.json` 누적 저장

### 진화 로직 충돌 방지
- `StrategyEvolver` docstring에 daily_bias/regime/진화 우선순위 명시
- daily_bias는 일시적 score 보정(당일 한정), 진화는 영속적 변경 → 충돌 없음

### config 추가
- `kr.llm_ops`: 6개 기능 모두 on/off 가능 (기본: true)

---

## 2026-03-07 — KR 손익비 최적화 + 전광판 전탭 통합 + 진화 잠금
> `bda3986` | `evolved_overrides.yml`, `strategy_evolver.py`, `common.js`, `dashboard.js`

### 손익비(R:R) 파라미터 조정
- `base_position_pct`: 10% → **25%** (포지션 크기 복원, 수익 레버리지 확보)
- `trailing_activate_pct`: 2.5% → **5.0%** (1차 익절과 동일, 분할 익절 우선 보장)
- `trailing_stop_pct`: 2.5% → **3.0%** (noise 탈출 방지)
- `max_positions`: 10 → **7** (집중도 향상)

### 진화 잠금 시스템
- `strategy_evolver._locked_params`: 4개 파라미터 진화 대상 영구 제외
  - `base_position_pct`, `trailing_stop_pct`, `trailing_activate_pct`, `first_exit_pct`
- 규칙 기반 + LLM 기반 양쪽 모두에서 잠금 체크 적용
- 수동 분석 후에만 조정 가능 (실거래 데이터 축적 후 재평가)

### 전광판 전탭 통합
- `_tickerColor`, `_buildTickerHTML`, `fetchNavIndices` → `common.js`로 이관
- 모든 탭(거래내역, 성과분석, 테마 등)에서 실시간 전광판 30초 갱신
- 데이터 로드 전 숨김 → 로드 후 fade-in 효과

---

## 2026-03-07 — 구조적 한계 3종 극복 (시장 레짐 감지 + 신선도 할인)
> `fe35f32` | `swing_screener.py`, `sepa_trend.py`, `batch_analyzer.py`

### 1. KOSPI 기반 시장 레짐 감지 + 하락장 보호
- **`SwingScreener.get_market_regime()`**: KOSPI 5일/20일 변화율 기반 레짐 판단
  - `bear`: 5일≤-3% OR 20일≤-5% / `caution`: 5일≤-1.5% OR 20일≤-2.5%
  - `bull`: 5일≥+1% AND 20일≥0% / `neutral`: 그 외
- **`batch_analyzer._scan_and_build()`**: 레짐별 시그널 필터링
  - `bear`: SEPA/STRATEGIC_SWING 전면 차단, RSI2(score≥70)만 허용
  - `caution`: SEPA 기준 +10pt 상향
- **`execute_pending_signals()`**: 레짐별 강도/손절 조정
  - `bear`: SignalStrength.NORMAL (포지션 축소), 손절 -3.5% 타이트
  - `caution`: 손절 -2.5% 소폭 타이트
- **`monitor_positions()`**: 레짐별 트레일링 스탑 자동 조정
  - `bear`: 3%→2%, 활성화 5%→3% / `caution`: 2.5%/4% / 회복 시 자동 복구
- **아침 스캔 알림**: 레짐 이모지 (🔴BEAR/🟡CAUTION/🟢BULL) + 경고 메시지 포함

### 2. 전문가 패널 신선도 할인
- `created_at` 기반 days_old 계산
- `freshness = max(0.3, 1.0 - days_old / 14)` → Day0=100%, Day7=50%, Day14=30%
- Layer1 보너스에 freshness 곱셈 (최소 3pt 보장), reasons에 신선도 % 표시

### 3. 수급 데이터 신선도 추적 + LCI 할인
- `supply_data_age`: 0=당일KIS, 1=T-1pykrx, 2=캐시파일 → `candidate.indicators`에 저장
- `lci_discount = max(0.7, 1.0 - age * 0.15)` → T-1: 15% 할인, T-2: 30% 할인
- SEPA 점수 계산 시 LCI/수급 점수에 discount 적용

---

## 2026-03-07 — 오버레이 점수 SEPA/RSI2 최종 점수 반영
> `a682150` | `sepa_trend.py`, `rsi2_reversal.py`, `swing_screener.py`

### 구조 갭 수정
- **문제**: `swing_screener._apply_strategic_overlay`가 `candidate.score`에 overlay 가산하지만,
  `generate_batch_signals`에서 `_calculate_sepa_score()`로 **완전 재계산** → overlay 무시
  - 예: base=58 + VCP(+15) = 73 → 재계산 시 58 → min_score=60 탈락
- **수정**: overlay 합산값을 `candidate.indicators["overlay_bonus"]`에 저장
- **`sepa_trend._calculate_sepa_score`**: 마지막에 `overlay_bonus` 가산
- **`rsi2_reversal._calculate_rsi2_score`**: 동일 처리
- **효과**: 경계선 종목(score 55~65)에서 VCP/패널/수급 있으면 SEPA/RSI2 정상 포착

---

## 2026-03-07 — 전략 흐름 분석 후 버그 3종 수정 + 갭다운 필터 완화
> `d55c72f` | `kr_scheduler.py`, `batch_analyzer.py`, `engine.py`, `evolved_overrides.yml`

### Bug 1: RSI2 장중 탐지 무동작 (제거)
- `ScreenedStock`에 `indicators` 속성 없음 → 모든 종목에서 AttributeError → 완전 무동작
- RSI(2)는 일봉 전용 → 진입은 08:20 + 12:30 배치 스캔(SwingScreener)으로만 처리

### Bug 2: RSI2 exit `_check_exit_signal` 무동작 (이전)
- `ScreenedStock.indicators.rsi_2` 없음 → None 반환 → 청산 로직 미작동
- `batch_analyzer.monitor_positions`에 `_calc_rsi2_from_fdr()` 추가
  - FDR 30일 일봉 다운로드 후 Wilder's RSI(2) 계산 (동기 함수, `run_in_executor`)
  - RSI(2) > 70이면 청산 시그널 (30분마다 체크)

### Bug 3: STRATEGIC_SWING 포지션 크기 10% 폴백
- `engine.py strategy_position_pct` dict에 `STRATEGIC_SWING` 누락 → `base_position_pct=10%` 폴백
- 25% 추가 (복합 3계층 시그널이므로 SEPA와 동일 배분)

### 갭다운 필터 완화
- `evolved_overrides.yml gap_down_skip_pct: -2.0 → -3.5`
- 장 시작 -2~3% 오실레이션 후 반등하는 SEPA 강세 종목 포착

---

## 2026-03-07 — 주간 5% 달성 2단계 개선 + 스캔 확장
> `f5da22c`, `aba0626` | `kr_scheduler.py`, `batch_analyzer.py`, `config.py`, `strategy_evolver.py`

### RSI2 개선 (aba0626)
- 12:30 낮 추가 스캔: `run_morning_scan()` + `execute_pending_signals()` (장중 2번째 기회)
- RSI2 배치 스캔만 사용 (장중 탐지는 ScreenedStock 구조 제약으로 불가 — 이후 Bug1로 제거)

### 진화 시스템 보호 (f5da22c)
- `strategy_evolver`: `base_position_pct` 하한 5%→20% (진화 알고리즘 과보수화 방지)
- `strategy_evolver`: `daily_max_loss_pct` 상한 5%→8%
- `config.py`: `section_map`에 `batch` 추가 (evolved_overrides.batch 정상 적용)
- `strategy_limits`: sepa 3→5개, rsi2 3개 (config 이관)

---

## 2026-03-07 — A-3 revert + 2차 코드리뷰 버그 수정
> `5faf787`, `14c370c` | `engine.py`, `batch_analyzer.py`

### A-3 revert (5faf787)
- 진화 알고리즘이 `base_position_pct`를 10%로 보수화 → 하드코딩 dict 유지
- `CLAUDE.md` 기준값: SEPA 25%, RSI2 20%, STRATEGIC_SWING 25%
- `MOMENTUM_BREAKOUT: 0.0` 완전 차단 추가 (`return 0` 분기)

### 2차 코드리뷰 수정 (14c370c)
- KR fill signal_score 수정 (strategy 타입 기반 fallback)
- batch fallback: `MOMENTUM_BREAKOUT` → `SEPA_TREND` (비활성 전략 폴백)
- `strategy_limits` config 이관 완료

---

## 2026-03-07 — 코드레벨 리뷰 A-1~A-4 수정 + 거래·엔진 종합 개선
> `a943c23`, `28957c3` | `exit_manager.py`, `engine.py`, `batch_analyzer.py`, `evolved_overrides.yml`

### A-1: 1차 익절 수량 수정 (a943c23)
- `_check_partial_exit`: `original_quantity * first_exit_ratio` → `remaining_quantity` 기준으로 통일
- sync 복원 시 over-sell 위험 해소

### A-2: trailing/breakeven 순서 충돌 수정 (a943c23)
- `breakeven_activated` 후 `ExitStage.FIRST` 완료 전에는 본전 보호 미적용
- TNGX 조기청산(1차 익절 전 breakeven 조건 도달 즉시 청산) 원인 수정

### A-4: stage 리셋 조건 강화 (a943c23)
- qty 10% 이상 증가만 NONE 리셋 (소량 sync 오차 무시, US sync 이중 익절 방지)

### 거래·엔진 종합 개선 (28957c3)
- `evolved_overrides`: `momentum_breakout.enabled: false`, strategy_allocation 재배분
  (sepa_trend 60%, rsi2_reversal 25%, momentum_breakout 0%)
- 유령 포지션 6건 DB 정리 (018250, 034020×3, 004020, 024110 — 15일 경과)
- `_reconcile_ghost_us_trades`: exit_type 추론 로직 추가
- `_on_order_filled`: SYNC_ 중복 방지 (기존 entry UPDATE)
- LLM `complete_json`: Invalid JSON 1회 retry
- `Trade.holding_time`: max(0, delta) 음수 방지
- `max_positions`: 7→10

---

## 2026-03-07 — 야간 로그 분석 기반 6가지 안정성 개선
> `5dd61f1` | `run_trader.py`, `us_scheduler.py`, `kis_us.py`, `dart_checker.py`
- systemd MemoryMax 1G→3G, TimeoutStopSec 30→60 (OOM 연쇄 재시작 방지)
- `_stopped_today` 파일 영속화 (`~/.cache/ai_trader_us/stopped_today_YYYYMMDD.json`)
  → 재시작 후 손절/트레일링 청산 종목 즉시 재매수 방지 (TNGX 3사이클 반복 원인)
- `_order_fail_blacklist`: ETP미신청/매수불가 종목 당일 재시도 차단 (FTGC, PDBC, PAA)
- `get_volume_surge` MINX 오류: MINX 없이 retry + WARNING→DEBUG 다운그레이드
- `_quote_fail_count`: 현재가 3회 실패 종목 세션 내 블랙리스트 (CVE, BNO, GUSH 등)
- DART corpCode BadZipFile 오류 → 만료 캐시 폴백 강화

## 2026-03-07 — US 거래량급증 API 오타 + 보유종목 중복매수 방지 + 3차익절 표기
> `30c8fa2` | `kis_us.py`, `us_scheduler.py`, `dashboard.js`

### 수정 내용
1. **kis_us.py**: volume-surge API 파라미터 `MIXN` → `MINX` 수정 (철자 뒤바뀜으로 NAS/NYS/AMS 전체 오류)
2. **us_scheduler.py**: 스크리닝 루프에서 기보유 종목 스킵 추가 (KR과 동일하게 추가 매수 방지)
3. **dashboard.js**: `exitStageLabel`에 `'third'` → `'3차익절'` 매핑 누락 수정

---

## 2026-03-06 — US 거래내역 대시보드 매수+매도 통합 표시
> `bc564a6` | `us_api.py`

### 문제
- US 거래내역이 매수 중심으로만 표시 (매도 누락)

### 원인 및 수정
1. **`created_at::date` → `event_time::date`**: DB 삽입 시각이 아닌 실제 거래 시각 기준으로 필터
2. **`trades JOIN market='US'`**: symbol 패턴 필터 제거 → 정확한 마켓 분리
3. **`trades` 테이블 SELL 보완**: `trade_events`에 SELL 레코드 없을 때 `trades.exit_time/exit_price` 로 SELL 행 합성
4. **미청산 BUY 현재가 보강**: 오픈 포지션 `current_price/pnl/pnl_pct` 실시간 주입
5. KR `get_trade_events()` 와 동일한 구조로 통일 (2단계 조회 패턴)

---

## 2026-03-06 — 프리장/넥스트장 시세수신 버그 수정
> `ecb34af` | `kis_websocket.py`, `kr_scheduler.py`, `run_trader.py`

### 문제
- 프리장(08:00–08:50)에서 KIS WS close_code=1006 5초 루프 반복
- 넥스트장(15:30–18:00)에서 정규장 종가(정적) 를 시세로 사용

### 원인
- `_subscribe_symbol()`이 모든 보유종목에 `H0NXCNT0` 전송
  → TIGER 레버리지 ETF 등 NXT 비대상 종목 구독 시 KIS 서버 즉시 1006 차단
- NXT 종목 목록을 WS에 전달하는 코드 없음 (`_nxt_symbols` 항상 공집합)

### 수정
1. **`kis_websocket._subscribe_symbol()`**: 프리/넥스트장 + NXT 비대상 종목 → 구독 건너뜀 (REST 폴링 커버)
2. **`kis_websocket._apply_subscriptions()`**: 보유종목도 NXT 필터 적용
3. **`run_trader.py`**: 시작 시 `broker.get_nxt_symbols()` → `ws_feed.set_nxt_symbols()` (650개 로드)
4. **`kr_scheduler.run_rest_price_feed()`**: 넥스트장 세션 감지 시 `ovtm_untp_prpr`(시간외단일가) 사용

---

## 2026-03-06 — US 해외주식 KIS 체결 동기화 (sync_from_kis_us)
> `b35ec2a` | `kis_us.py`, `trade_storage.py`, `us_scheduler.py`

### 신규 기능
KR의 `sync_from_kis` 와 동일하게, 장 마감 후 KIS TTTS3035R 체결 내역을 DB와 대조해 누락 거래 복구

### 구현
1. **`kis_us.get_all_fills_for_date()`**: `get_order_history()` 래퍼 — KR broker와 동일 포맷 반환
2. **`trade_storage.calc_pnl_us()`**: zero-commission PnL 계산 (USD float 반환)
3. **`trade_storage.sync_from_kis_us()`**: 누락 매수/매도 DB 복구 (`market='US'` 필터)
4. **`trade_storage._reconcile_pnl_us()`**: KIS 실체결가 기준 PnL 보정 ($0.01 이하 무시)
5. **`us_scheduler.eod_close_loop`**: 매 거래일 16:20 ET 이후 1회 자동 실행

### KR sync_from_kis 와의 차이
- `market='US'` 조건 DB 조회 (KR 거래와 완전 분리)
- zero-commission (수수료·세금 0)
- PnL 단위: USD float (KR은 KRW int)

---

## 2026-03-06 — US 대시보드 거래내역 표시 수정 + KIS API 날짜 기준 수정
> `1157f63`, `982d5a7` | `us_api.py`, `us_scheduler.py`, `trades.html`, `trades.js`

### 수정 내용
1. **`us_api.py`**: trades 쿼리를 `trades` 테이블(exit_time IS NOT NULL 필터로 미청산 누락) → `trade_events` 테이블로 변경
   - `metadata` 컬럼 참조 제거 (존재하지 않음) → 개별 컬럼(strategy, pnl 등) 직접 조회
   - asyncpg `$1::date` 바인딩에 `datetime.date` 객체 전달 (str은 toordinal 에러)
   - 날짜별 조회 지원 (`?date=YYYY-MM-DD`)
2. **`us_scheduler.py`**: KIS API 주문 조회 날짜 기준 ET→KST 수정 (KIS는 KST 기준)
3. **`trades.html`/`trades.js`**: US 거래 섹션에 날짜 선택 UI 추가

---

## 2026-03-06 — US 거래 기록 누락 + 재시작 시 미체결 주문 복원
> `0e858cc` | `us_scheduler.py`, `trade_storage.py`, `us_api.py`, `exit_manager.py`, `run_trader.py`

### 핵심 수정
1. **`us_scheduler.py`**: `_sync_portfolio`에서 수량 변화 감지 → 거래 기록 보완
   - 재시작 후 `_pending_orders` 비어있으면 `_check_orders` 스킵 → 체결 기록 누락
   - `_prev_qty_snapshot` 비교로 수량 감소 시 exit 기록, 신규 감지 시 entry 기록
2. **`us_scheduler.py`**: `_recover_pending_orders` 추가 — 재시작 시 KIS 미체결 주문 복원
   - `order_check_loop` 시작 시 1회 실행 (전일+당일 조회)
3. **`us_scheduler.py`**: 매도 실패 5분 쿨다운 (`_sell_fail_cooldown`) 추가
   - "가능수량 부족" 반복 매도 시도 방지
4. **`trade_storage.py`**: `market` 컬럼 추가 (`KR`/`US` 분리), 마이그레이션 포함
5. **`us_api.py`**: trades 엔드포인트 → `market='US'` 직접 SQL 필터 (심볼 기반 필터 제거)
6. **`exit_manager.py`**: stage 변경 시 `_persist_states()` 즉시 호출 (재시작 시 복원 보장)
7. **`us_scheduler.py`**: hp_cache에 `entry_times`, `strategies` 추가 (재시작 시 메타데이터 복원)
8. **`run_trader.py`**: Position `entry_time=datetime.now()` 초기화 누락 수정

---

## 2026-03-06 — US ExitManager 분할 익절 완전 수정 (P0 4건)

### 근본 원인
US 포지션의 분할 익절(1차/2차/3차)이 전혀 동작하지 않았음. 복합 버그 4건이 동시에 작용.

### P0 수정 4건

1. **`scripts/run_trader.py`**: `get_positions()` 반환 키 불일치 — `"qty"` vs `"quantity"`
   - `pos.get("quantity", 0)` → `pos.get("qty") or pos.get("quantity") or 0`
   - 포지션 quantity=0으로 등록 → `remaining_quantity=0` → `update_price` 항상 skip

2. **`scripts/run_trader.py`**: `restore_stages()` 순서 버그 — `_states` 비어있는 상태에서 복원 시도
   - `register_position` → `restore_stages` 순서로 변경 (이전: 역순)

3. **`scripts/run_trader.py`**: `ExitManager(config=..., market="US")` — `market` 파라미터 누락
   - 기본값 `"KR"`로 동작 → stage 파일명/수수료 계산 오류

4. **`src/schedulers/us_scheduler.py`**: 재시작 후 기존 포지션 ExitManager 미등록
   - `_sync_portfolio` 기존 포지션 업데이트 시 `register_position` 누락 → `_states` 비어있음
   - `if symbol not in eng.exit_manager._states:` 조건 추가하여 자동 재등록

### 기타
- `us_scheduler.py`: `restore_stages`를 포지션 루프 뒤로 이동 (동일 순서 버그)
- `exit_stages_us_*.json` 파일명 정상화 (market suffix 적용)
- 과도한 진단 로그 정리 (INFO → DEBUG)

---

## 2026-03-05 — 전체 코드 리뷰 + US coroutine 버그 수정

### P1 수정 3건
- `kr_scheduler.py`: `_overnight_sentiment` 변수를 try 블록 전에 초기화 (스코프 안전성)
- `kr_scheduler.py`: f-string 삼항 연산자 → if/else 분리 (가독성)
- `kis_market_data.py`: 야간선물 장외시간 네거티브 캐시 60초 (불필요 API 호출 방지)

### US coroutine never awaited 수정
- `us_screener.py`: `scan_premarket_gap` → `async def`로 변경
- `us_screener.py:483`: `get_intraday_scan` 호출에 `await` + `[symbol]` 리스트 전달
- `us_scheduler.py:407`: `await` 추가

---

## 2026-03-05 — US 오버나이트 + KOSPI200 야간선물 레짐 연동

### 1. screen_all에 오버나이트 레짐 직접 연동
- **`src/signals/screener/kr_screener.py`**: `screen_all()`에 `overnight_sentiment`, `overnight_volatility` 파라미터 추가
  - 7-7 단계: bearish → 수급 없는 종목 -20pt, 수급 있는 종목 -5pt
  - bullish → 기관/외국인 수급 종목 +10pt
- **`src/schedulers/kr_scheduler.py`**: 스크리닝 루프에서 `get_overnight_signal()` 호출 → screen_all에 전달

### 2. 변동성 기반 동적 포지션 사이징
- **`src/schedulers/kr_scheduler.py`**: 자동 진입 시 오버나이트 변동성에 따른 조정
  - bearish → min_score=85, 일일진입=1회
  - 변동성 2~3% → min_score +3, position_multiplier=0.7
  - 변동성 3%+ → min_score +5, position_multiplier=0.5
  - `metadata.position_multiplier`로 엔진 포지션 사이징에 반영 (기존 메커니즘 활용)

### 3. KOSPI200 야간선물(KRX) 현재가 조회
- **`src/data/providers/kis_market_data.py`**: `get_night_futures_quote()` 신규 메서드
  - KIS API TR ID: `FHMIF10000000`, 종목코드: `101W09` (KOSPI200 근월물)
  - 등락률 ±1% 기준 bullish/bearish/neutral 판정
  - 5분 캐시, price/change_pct/volume/sentiment 반환
- **`src/schedulers/kr_scheduler.py`**: US 지수보다 야간선물 sentiment 우선 적용
  - 야간선물 데이터가 있고 neutral이 아니면 US 지수 sentiment를 덮어씀

### 수정 파일
- `src/signals/screener/kr_screener.py`
- `src/schedulers/kr_scheduler.py`
- `src/data/providers/kis_market_data.py`

---

## 2026-03-05 — KR 종목 선별 고도화 3종 (대장주/재료소멸/수급)

### 1. 테마 대장주 독식 필터 (Winner Takes All)
- **`src/signals/screener/kr_screener.py`**: `screen_all()` 7-5 단계 추가
  - 같은 테마 내 여러 종목이 올라왔을 때 점수 기준 1등(대장주)에 +10pt 보너스
  - 2등 이하 종목에 -25pt 감점 + "테마[X] 2등주 감점 (대장: Y)" 사유 태깅
  - theme_detector의 stock_sentiments에서 테마 그룹핑

### 2. 재료 생애주기 필터 (Buy the rumor, Sell the news)
- **`src/signals/sentiment/kr_theme_detector.py`**: LLM 프롬프트에 `catalyst_phase` 필드 추가
  - `rumor`: 기대감/루머/검토 단계 → 스크리너에서 +8pt 보너스
  - `confirmed`: 확정/완료 단계 → 급등(+5%) 시 -30pt, 상승(+2%) 시 -15pt 감점
  - `_stock_sentiments` 저장 구조에 `catalyst_phase` 필드 추가
- **`src/signals/screener/kr_screener.py`**: 7-5b 재료 생애주기 필터 단계 추가

### 3. 개인 단독 매수 감점 필터
- **`src/signals/screener/kr_screener.py`**: 7-6 단계 추가
  - 상승(+3%) 중인데 기관/외국인 수급이 없는 종목에 -15pt 감점
  - "개인단독매수 의심" 사유 태깅

### 수정 파일
- `src/signals/screener/kr_screener.py`
- `src/signals/sentiment/kr_theme_detector.py`

---

## 2026-03-05 — 종목 필터링 재검증 (P0 1건 + P1 3건 수정)

### P0 수정 (1건)
- **`src/strategies/us/momentum.py:34`**: 최소 주가 $5 필터 누락 → `close < 5.0` 체크 추가
- **`src/strategies/us/sepa_trend.py:35`**: 동일 — $5 필터 추가
- **`src/strategies/us/earnings_drift.py:42`**: 동일 — $5 필터 추가
  - 스크리너 우회 경로(거래량급증, 동적유니버스)로 penny stock 진입 가능했음

### P1 수정 (3건)
- **`src/signals/screener/kr_screener.py:1748`**: `screen_all()` min_price 기본값 `0` → `1000` (호출처 미지정 시 1,000원 미만 종목 우회 방지)
- **`src/strategies/base.py:301`, `src/indicators/technical.py:361`**: vol_ratio 기본값 `0` → `1.0` (중립값) — 거래량 데이터 없을 때 0이면 의미없는 차단/통과 발생
- **`src/strategies/us/sepa_trend.py:47,83`**: `if not all([ma50, ...])` → `any(v is None or v <= 0 ...)` + `if ma5 > 0` → `if ma5 is not None` (0값 False 버그 수정)

### 수정 파일
- `src/strategies/us/momentum.py`, `src/strategies/us/sepa_trend.py`, `src/strategies/us/earnings_drift.py`
- `src/signals/screener/kr_screener.py`, `src/strategies/base.py`, `src/indicators/technical.py`

---

## 2026-03-05 — US KIS WS 체결통보 콜백 구현

### 수정: `scripts/run_trader.py`
- **`_on_kis_fill()`**: placeholder → 실제 구현
  - 체결 즉시 상세 로그 출력 (종목, 수량, 가격, 전략, 주문번호)
  - 텔레그램 즉시 알림 (REST 폴링 10초 대기 없이 WS Push 시점에 발송)
  - pending 주문 매칭하여 전략명 포함
  - 실제 포지션 처리는 기존 `order_check_loop`이 담당 (중복 처리 방지)

---

## 2026-03-05 — 전체 코드 복기 P0+P1 수정 (16건)

### P0 수정 (9건)
- **`src/core/engine.py:1296`**: 시장가 주문 `order.price=None` 포맷 크래시 → price_str 분기 처리
- **`src/core/engine.py:1084`**: `RiskConfig`에 없는 `pre_market_slippage_buffer_pct` → `engine.config`에서 getattr로 접근
- **`src/risk/manager.py:218-227`**: KR에서 `max_positions`/`min_cash_reserve` 체크 누락 → KR+US 공통 적용
- **`src/schedulers/kr_scheduler.py:345`**: 청산 pending 예외 시 `discard()` 미호출 → 교착 방지 추가
- **`src/schedulers/kr_scheduler.py:834`**: 포트폴리오 동기화 120초 → 30초 (설계 일치)
- **`src/schedulers/kr_scheduler.py:55`**: 수동매수 하드코딩 `_manual_buy_orders` 비우기 (1회 실행 완료)
- **`src/schedulers/us_scheduler.py:1014`**: 일일 통계 리셋 레이스 컨디션 → `portfolio_sync_loop` 중복 제거
- **`src/strategies/kr/momentum.py:293-309`**: `if ma5 and ma20` → `if ma5 is not None and ma20 is not None` (0값 False 방지)
- **`src/dashboard/kr_api.py:370`**: `os._exit(0)` → `sys.exit(0)` (graceful shutdown)

### P1 수정 (7건)
- **`src/strategies/kr/rsi2_reversal.py`**: `check_rr_ratio()` R/R 필터 추가 + `if close and` 패턴 수정(2건)
- **`src/strategies/kr/gap_and_go.py:98`**: `min_price` 필터 추가 (동전주 진입 차단)
- **`src/core/engine.py:522`**: `if not pos.strategy` → `if pos.strategy is None` 패턴 수정
- **`src/core/engine.py:562`**: 음수 수량 포지션 경고 + 0 보정 후 제거
- **`src/data/providers/supply_score.py:32`**: 영업일 계산에 `is_kr_market_holiday()` 적용
- **`src/strategies/exit_manager.py:401`**: 본전 이탈 판정에 매도 수수료 버퍼 0.25% 추가
- **`src/schedulers/kr_scheduler.py:1498`**: 수급캐시 루프에 공휴일 체크 추가

### 수정 파일
- `src/core/engine.py`, `src/risk/manager.py`
- `src/schedulers/kr_scheduler.py`, `src/schedulers/us_scheduler.py`
- `src/strategies/kr/momentum.py`, `src/strategies/kr/rsi2_reversal.py`, `src/strategies/kr/gap_and_go.py`
- `src/strategies/exit_manager.py`
- `src/dashboard/kr_api.py`, `src/data/providers/supply_score.py`

---

## 2026-03-05 — US 테마/섹터 탐지기 구현 + 수동매수/청산예외 기능

### 신규: `src/signals/sentiment/us_theme_detector.py`
- **RSS 뉴스 수집**: MarketWatch, CNBC, Yahoo Finance RSS (무료, API 키 불필요)
- **Finnhub 뉴스**: API 키 있으면 보너스 소스로 활용
- **LLM 테마 추출**: Gemini Flash로 영문 뉴스 → 테마/종목 임팩트 JSON 추출
- **섹터 ETF 모멘텀**: SPDR 11개 섹터 ETF (XLK~XLC) 1일 수익률로 테마 점수 보정 (±15점)
- **12개 테마**: AI/Semiconductors, Cloud/SaaS, EV/Clean Energy, Biotech/Pharma, Fintech/Payments, Cybersecurity, Space/Defense, Nuclear Energy, Quantum Computing, Robotics/Automation, Streaming/Media, Cannabis
- **종목 센티멘트**: impact(-10~+10), direction, theme, reason (1시간 유효)
- **대시보드 연동**: `/api/us/themes` 엔드포인트 정상 동작

### 수정: `scripts/run_trader.py`
- finnhub_key 조건 제거 → RSS+LLM 기반이므로 항상 USThemeDetector 초기화

### 신규: 수동 매수 예약 + 청산 예외 기능
- **`src/strategies/exit_manager.py`**: `_exit_exempt` 셋 추가 — `add_exit_exempt()`, `remove_exit_exempt()`, `is_exit_exempt()` 메서드
- **`src/schedulers/kr_scheduler.py`**: `run_manual_buy_orders()` — 09:00 장 시작 시 수동 시장가 매수 + 청산 예외 등록
- **적용**: 123320 TIGER 레버리지 ETF 가용예산 풀매수, 익절/손절 비활성화

---

## 2026-03-04 — 스크리닝 시스템 8가지 개선 (KR+US 공통)

### 1. 인트라데이 전략 재활성화
- **`config/evolved_overrides.yml`**: gap_and_go, momentum_breakout, theme_chasing → `enabled: true`
- **배분 조정**: SEPA 30%, Momentum 25%, RSI2 20%, Gap&Go 15%, Theme 10%

### 2. RS Ranking 통합
- **`src/signals/screener/us_screener.py`**: SPY 벤치마크 기반 RS 보너스 (RS≥80: +15pt, RS≥70: +10pt, RS<30: -10pt)
- **`src/signals/screener/kr_screener.py`**: KOSPI 지수 대비 상대강도 `_apply_rs_ranking_bonus()` 필터 추가
- **`src/strategies/us/sepa_trend.py`, `us/momentum.py`**: RS rating 점수 반영 (최대 +10pt)
- **`src/strategies/base.py`**: USBaseStrategy에 `set_benchmark()` + `_get_indicators()`에 RS 자동 계산
- **`src/schedulers/us_scheduler.py`**: SPY 벤치마크 전략 자동 주입

### 3. R/R 비율 필터
- **`src/strategies/base.py`**: `check_rr_ratio()` 헬퍼 (KR BaseStrategy + US USBaseStrategy)
- **적용**: SEPA(KR+US), Momentum(US), EarningsDrift(US), Gap&Go(KR) — min R/R 2.0
- **`config/default.yml`**: `min_rr_ratio: 2.0` 설정 추가

### 4. 프리마켓 갭 스캔
- **`src/signals/screener/kr_screener.py`**: `screen_premarket_gap()` — 08:30~09:00 갭상승 종목 탐지
- **`src/signals/screener/us_screener.py`**: `scan_premarket_gap()` — Finviz 프리마켓 데이터 활용
- **`screen_all()`**: 08~09시 자동 프리마켓 갭 스캔 통합
- **`us_scheduler.py`**: 프리마켓 갭 종목 스크리닝 최우선 삽입

### 5. 촉매 스캔 (DART + Earnings)
- **`src/signals/screener/kr_screener.py`**: `_apply_dart_catalyst()` — DART 공시 긍정/위험/차단 자동 처리
- **`src/signals/screener/us_screener.py`**: 어닝스 촉매 보너스 (갭상승+3%: +15pt, +1%: +8pt)
- **`us_scheduler.py`**: earnings_today → screener 자동 주입

### 6. ORB (Opening Range Breakout) 확인 매수
- **`src/strategies/kr/gap_and_go.py`**: ORB 범위(고/저) 추적, 상단 돌파 시 +10pt 보너스
- **`src/strategies/us/momentum.py`**: 전일 고가 돌파 + 갭업 ORB 보너스 +5pt

### 7. 섹터 로테이션 시그널
- **`src/signals/screener/kr_screener.py`**: `_apply_sector_rotation_bonus()` — SectorMomentumProvider 활용, 강세섹터 +10pt, 약세섹터 -10pt
- **`src/signals/screener/us_screener.py`**: SPDR 섹터 ETF (XLK, XLF, XLV 등) 20일 모멘텀 계산

### 8. 동적 유니버스 확장
- **`src/schedulers/us_scheduler.py`**: screener 상위 50종목(score≥60) 자동 유니버스 편입 (최대 30개/사이클)

### 수정 파일
- `config/evolved_overrides.yml`, `config/default.yml`
- `src/strategies/base.py`, `src/strategies/kr/sepa_trend.py`, `src/strategies/kr/gap_and_go.py`
- `src/strategies/us/sepa_trend.py`, `src/strategies/us/momentum.py`, `src/strategies/us/earnings_drift.py`
- `src/signals/screener/kr_screener.py`, `src/signals/screener/us_screener.py`
- `src/schedulers/us_scheduler.py`
- `src/indicators/technical.py` (기존 `rs_rating()` 활용)

---

## 2026-03-04 — US 엔진 7가지 버그 수정 (장 오픈 대비)

### P0: initial_capital 매 동기화 덮어쓰기 → 최초 1회만 설정
- **`src/schedulers/us_scheduler.py`**: `_sync_portfolio()`에서 `initial_capital`을 `total_equity`로 30초마다 덮어쓰던 문제 수정
- **영향**: `total_pnl`(총 손익)이 항상 0에 수렴하여 수익 추적 불가 + 리스크 판단 왜곡

### P0: exit_stages 반복 복원 → 초기화 시 1회만
- **`src/schedulers/us_scheduler.py`**: `_sync_portfolio()`에서 `exit_stages` 캐시를 매 동기화마다 복원하던 문제 → `_exit_stages_restored` 플래그로 1회만 실행
- **영향**: 런타임 중 진행된 익절 단계(FIRST→SECOND)가 캐시의 이전 값으로 롤백되어 중복 분할매도 발생 가능

### P0: 전략 exit 실패 시 ExitManager 손절 누락 방지
- **`src/schedulers/us_scheduler.py`**: `_check_exits()`에서 전략별 `check_exit()` 호출 후 `_execute_exit` 실패 시에도 `break`로 ExitManager 체크를 건너뛰던 문제 → `strategy_exit_attempted` 플래그로 전략 exit 미발동 시 ExitManager 정상 실행

### P1: 부분체결(partial) 교착 상태 해소
- **`src/schedulers/us_scheduler.py`**: `_check_orders()`에서 `partial` 상태에 로그만 남기고 교착되던 문제 → 부분체결 타임아웃 추가 (매도 3분, 매수 15분), 타임아웃 시 잔여 취소 + 체결분 반영

### P1: EOD 청산 중복 실행 방지
- **`src/schedulers/us_scheduler.py`**: `eod_close_loop()`에서 마감 15분간 30초마다 `_eod_close()` 반복 호출되던 문제 → `_eod_close_done` 날짜 플래그로 당일 1회만 실행

### P1: 매수 체결 시 기존 포지션 수량/평균가 갱신
- **`src/schedulers/us_scheduler.py`**: `_on_order_filled()` 매수 체결 시 sync에서 이미 생성된 포지션의 수량/평균가를 체결 정보로 갱신하지 않던 문제 수정

### P1: API 빈 응답 방어 강화
- **`src/schedulers/us_scheduler.py`**: `_sync_portfolio()`에서 account_info는 있지만 positions만 빈 배열로 반환된 경우 로컬 포지션 급감 방어 로직 추가

### P1: 스크리너 캐시 주말 무효화 방지
- **`src/signals/screener/us_screener.py`**: 캐시 유효기간 1일→3일 (금요일 스캔 → 월요일 사용 가능)

## 2026-03-04 — stock_master 안정화 + WS 장시간 제어

### P0: pykrx stock_master 로딩 실패 해결
- **`src/dashboard/data_collector.py`**: pykrx 실패 시 StockMaster DB(`kr_stock_master` 테이블)에서 종목명 폴백 로드 (3708개 종목)
- **`src/dashboard/data_collector.py`**: pykrx 재시도 횟수 제한 (최대 3회) — 무한 반복 WARNING 방지
- **`src/dashboard/data_collector.py`**: 캐시 파일 단일화 (`stock_master.json`), TTL 72시간으로 확장
- **원인**: pykrx `get_market_ticker_list()`가 장 마감 후 KRX 서버에서 빈 응답 반환 → `index -1` 에러

### P1: KR WebSocket 장 마감 후 불필요한 연결 방지
- **`src/data/feeds/kis_websocket.py`**: `_is_market_active()` 메서드 추가 — `KRSession`으로 장외 시간 판별
- **`src/data/feeds/kis_websocket.py`**: `run()` 루프에서 장 마감(CLOSED) 시 WS 연결 해제 + 대기, 장 시작 시 자동 재연결
- **효과**: 장 마감 후 2분마다 끊기던 WS 재연결 사이클 완전 제거

## 2026-03-04 — CLAUDE.md 대폭 업데이트

### 문서: CLAUDE.md 상세화 (ai-trader-v2 참고)
- **`CLAUDE.md`**: 79줄 → 300줄+ 대폭 확장
  - 세션 시작 필수 읽기 설명 보강 (중복 작업 방지, 맥락 파악)
  - Git & GitHub 섹션 신규 추가
  - 코드 리뷰 프로토콜 추가 (P0/P1/P2 분류)
  - 매매 전략 상세 (KR 5개 + US 3개 나열, ExitManager 파라미터)
  - 리스크 관리 테이블 (KR/US 분리, 상세 파라미터)
  - 수수료 정보 (KR 왕복 0.227%, US Zero-commission)
  - 실행 흐름 상세 (KR 스케줄러 7태스크 + US 스케줄러 9태스크)
  - WebSocket 피드 정보 (KR H0STCNT0, US HDFSCNT0)
  - 대시보드 개발 패턴, 운영 모니터링 계층
  - 코딩 규칙 금지 패턴 코드 예시
  - 설정 주의사항 (evolved_overrides 머지)
  - 의존성, LLM 모델 선택, 진화 시스템 상세
  - 트러블슈팅 가이드 (5개 시나리오)
  - 실행 방법 (--market kr|us|both)

## 2026-03-04 — WS 실시간 포지션 모니터링 + 스캔 품질 개선 + 전략 다변화

### P0: WS 실시간 보유 포지션 모니터링 구현
- **`scripts/run_trader.py`**: `_load_existing_positions()` 후 `ws_feed.set_priority_symbols()` + `subscribe()` 호출하여 보유 종목 WS 자동 구독
- **`scripts/run_trader.py`**: `_on_market_data()` 콜백에서 보유 종목 수신 시 `kr_scheduler._check_exit_signal()` 즉시 호출 (WS 실시간 청산 체크)
- **`scripts/run_trader.py`**: `kr_scheduler` 인스턴스를 봇 속성으로 저장 (WS 콜백 접근용)
- **`src/schedulers/kr_scheduler.py`**: `run_fill_check()` BUY 체결 시 WS priority symbols 갱신 + 신규 심볼 구독
- **`src/schedulers/kr_scheduler.py`**: `run_rest_price_feed()` 폴링 간격 45초 → 20초 (WS 백업 역할 강화)

### P1: FDR 조회 타임아웃 개선
- **`src/signals/screener/swing_screener.py`**: `_calculate_all_indicators()` 타임아웃 10초 → 15초, 실패 시 1회 재시도
- **`src/signals/screener/swing_screener.py`**: `_load_benchmark_index()` FDR 실패 시 KIS API (`broker.get_daily_prices("0001")`) 폴백

### P2: SEPA 전략 점수 완화 + 전략 다변화
- **`src/core/batch_analyzer.py`**: `execute_pending_signals()` 전략별 최대 포지션 수 제한 추가 (`rsi2_reversal` 최대 3개, `sepa_trend` 최대 3개, 기타 2개)
- `config/default.yml` sepa_trend min_score는 이미 55 (변경 불필요)

### P3: 포지션 모니터링 간격 단축
- **`config/default.yml`**: `position_update_interval` 30 → 10분
- **`src/schedulers/kr_scheduler.py`**: 기본값 30 → 10분

### P4: US 포트폴리오 초기화 None 비교 버그
- **`scripts/run_trader.py`**: `_initialize_us()` 잔고 조회 시 `balance.get('total_equity') or 0` + `is not None` 가드 추가

### P5: stock_master 로컬 캐시 폴백
- **`src/dashboard/data_collector.py`**: `_load_stock_master_sync()` pykrx 성공 시 `~/.cache/ai_trader/stock_master_kospi.json` 캐시 저장, 실패 시 캐시 로드 (TTL 48시간)

## 2026-03-04 — 버그 상세 리뷰 + 수정 (2차)

### 수정 완료
- **재시작 중복 매수 방지** (`kr_scheduler.py`): `last_execute_date` 플래그 파일 영속화 (`~/.cache/ai_trader/executed_YYYY-MM-DD.flag`). 풀백/catch-up/정규 실행 3곳 모두 적용. 오래된 플래그 자동 정리
- **config 경로 전수 수정** (`kr_scheduler.py`): `bot.config.get("scheduler")` → `bot.config.get("kr", "scheduler")` 등 6곳. `intraday_buy`, `momentum_breakout` 포함. 기존엔 항상 기본값 폴백되던 문제 해결
- **portfolio guard 추가** (`batch_analyzer.py`): `execute_pending_signals()` 시작 시 포지션 비어있으면 `broker.get_positions()` 호출하여 복구
- **VCP timedelta import** (`vcp_detector.py`): `_cleanup_old_cache`에서 `timedelta` 미정의 → import 추가

### 확인 완료 (문제없음)
- **get_positions 타입**: KR scheduler, data_collector 모두 KR broker(`Dict[str, Position]`) 사용. US broker와 혼용 없음
- **pykrx 동기 호출**: `supply_score.py`, `sector_momentum.py`, `swing_screener.py` 모두 `asyncio.to_thread()` 정상 래핑

## 2026-03-04 — 코드 리뷰 + 전략 흐름 검증

### P0 수정 (Critical)
- **KR ORDER 핸들러 누락**: `EventType.ORDER` 핸들러가 미등록 → 매수/매도 주문이 이벤트 큐에서 드롭됨. `RiskManager.on_order()` 추가하여 `broker.submit_order()` 호출
- **ExitManager 메서드명 불일치**: `check_exit()` → `update_price()` 변경. KR 손절/익절 불가 해결
- **대시보드 SSE 미실행**: `dashboard.start()` → `dashboard.run()` (브로드캐스트 루프 포함)
- **RiskManager daily loss**: `daily_pnl` → `effective_daily_pnl` (미실현 손익 반영)
- **KR MarketContext session 누락**: `KRSession()` 인스턴스 추가

### US 거래소 코드 수정
- 현재가 조회: `NASD` → `NAS`, `NYSE` → `NYS`, `AMEX` → `AMS` 변환 (`_EXCD_QUOTE_MAP`)
- FRMI 시세 조회 실패 → 해결, 60주 매도 체결 완료
- 매도 수량: `float` → `int` 변환 누락 수정
- US 잔고: `output2.frcr_dncl_amt`(예수금) + `frcr_evlu_amt`(주식평가금) 사용

### 프론트엔드 수정
- JS: `/api/us-proxy/api/us/` → `/api/us/` (프록시 제거)
- SSE: `us_status`, `us_portfolio`, `us_positions`, `us_risk` 이벤트 구독 추가
- HTML 템플릿(8개) 누락 복사
- `rm._config` → `rm.config` (AttributeError 수정, 3곳)

### 2차 심층 리뷰 (P0×3 + P1×3 추가 수정)
- **P0**: on_fill에서 `update_position(fill)` 호출 추가 (체결 즉시 포트폴리오 갱신)
- **P0**: ExitManager에 float 대신 Decimal 전달 (TypeError 해결)
- **P0**: on_order에서 `event.order` 직접 사용 (order_type/strategy 보존)
- **P1**: 매수 체결 시 ExitManager 즉시 등록 (2분 지연 → 즉시)
- **P1**: 매도 체결 시 `_exit_pending_symbols` 즉시 해제 (3분 지연 → 즉시)

### 최종 검증 결과
| 흐름 | 상태 |
|------|------|
| KR 장중 스크리닝 → 매수 | PASS (ORDER 핸들러 + event.order) |
| KR 체결 확인 → 포지션 등록 | PASS (on_fill에서 update_position + ExitManager 등록) |
| KR 분할 익절/손절 → 매도 | PASS (update_price + Decimal) |
| US 스크리닝 → 매수 | PASS |
| US 청산 → 매도 | PASS |
| KR 배치 스캔 → T+1 실행 | PASS (ORDER 핸들러) |

---

## 2026-03-03 — 초기 구조 (Phase 0-6)
**커밋**: `4790280` feat: KR+US 통합 트레이딩 엔진 초기 구조

### 프로젝트 생성
- GitHub 리포: `qwq-partners/qwq-ai-trader` (private)
- 기존 `ai-trader-v2` (KR)와 `ai-trader-us` (US)를 하나로 통합
- 근본 원인: 같은 KIS appkey로 두 프로세스 → HTTP 500 토큰 충돌

### 생성된 파일 (103개, 38,538줄)
**핵심 아키텍처**:
- `src/core/engine.py` — UnifiedEngine (KR+US 단일 이벤트 루프)
- `src/core/market_context.py` — MarketContext (시장별 컴포넌트 번들)
- `src/core/types.py` — 통합 도메인 타입 (Market, Position, Portfolio, Signal + market 필드)
- `src/core/event.py` — 통합 이벤트 시스템 (15개 EventType)

**유틸리티**:
- `src/utils/token_manager.py` — KISTokenManager (단일 인스턴스, 핵심!)
- `src/utils/config.py` — 통합 YAML 로더 (kr: + us: 섹션)
- `src/utils/session.py` — KRSession + USSession + USMarketCalendar
- `src/utils/logger.py`, `telegram.py`, `llm.py`, `fee_calculator.py`

**브로커**:
- `src/execution/broker/kis_kr.py` — KR 국내주식 (1,731줄)
- `src/execution/broker/kis_us.py` — US 해외주식 (1,018줄)
- 공유 토큰 매니저 주입 패턴

**전략 (7개)**:
- KR: Momentum, Theme, Gap&Go, SEPA (src/strategies/kr/)
- US: Momentum, SEPA, EarningsDrift (src/strategies/us/)
- 통합 ExitManager + RiskManager (시장별 설정 분기)

**데이터 레이어**:
- feeds: KIS WS, Finnhub WS, KIS US WS
- providers: yfinance, finviz, earnings, sector_momentum, supply_score, kis_market_data
- storage: stock_master, trade_storage
- screeners: kr_screener, us_screener, swing_screener

**스케줄러**:
- `src/schedulers/kr_scheduler.py` — KR 18개 백그라운드 작업
- `src/schedulers/us_scheduler.py` — US 10개 백그라운드 태스크

**대시보드 (포트 8080 통합)**:
- `src/dashboard/server.py` — 통합 aiohttp 서버
- `src/dashboard/kr_api.py` — KR REST API (/api/*)
- `src/dashboard/us_api.py` — US REST API (/api/us/*)
- `src/dashboard/sse.py` — 통합 SSE (KR+US 이벤트)
- `src/dashboard/data_collector.py` — KR 데이터 수집기
- static/ — HTML/JS/CSS

**진화 시스템**:
- trade_journal, trade_reviewer, llm_strategist, strategy_evolver, config_persistence

**설정**:
- `config/default.yml` — 통합 설정 (kr: + us: 섹션)
- `config/evolved_overrides.yml` — KR 진화 오버라이드

### 남은 작업 (Phase 7)
- [ ] systemd 유닛 작성 (qwq-ai-trader.service)
- [ ] import 경로 불일치 수정 (런타임 테스트)
- [ ] --dry-run 실행 테스트
- [ ] 기존 ai-trader + ai-trader-us 서비스 교체
- [ ] 모바일 앱 API 엔드포인트 업데이트
