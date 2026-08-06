# QWQ AI Trader - Changelog

## 2026-08-07 — fix(experts): 섹터 카운슬 점수 개편 — 뉴스 이중계산 제거 + 5/20일 블렌드

반도체 +49 이의 제기(실제 2주 -15~-22% 하락 중) 조사 후속. 원인: ① 테스트 시점 ETF
캐시 부재로 뉴스 단독 점수였고 ② Perplexity가 "주간 반등" 기사로 가격을 재채점(이중
계산). 사용자 승인 2건 + 코드리뷰(P0/P1 없음, P2 2건 반영):

- **뉴스 단독 표기** (1번): scores에 `qual_only` 플래그, finding "뉴스 단독" 표기,
  정량 전멸 시 신뢰도 상한 0.5.
- **정성 축 역할 분리** (2번): 프롬프트에서 주가 등락·수급 강도 채점 금지 —
  뉴스 재료(수주·실적·정책·사고)만 평가. 가격 추세는 정량 축 담당.
- **정량 축 5/20 블렌드** (2번): 단일 20일 → 5일 40% + 20일 60%.
  반도체 실측: 0.4×(+18.5%) + 0.6×(-14.8%) ≈ -1.5% → 약중립 (V자 구간의 정직한 표현).
  60일·이평선은 종목 레벨 MA200 필터와 역할 중복이라 배제.
- **provider 멀티 수익률**: `_calc_etf_returns`(1회 조회로 r5+r20),
  `get_all_sector_momentum_multi()` 신설, 신규 캐시 `etf_momentum_multi.json`.
  레거시 `etf_momentum.json`(20일 float)·`get_sepa_score` 등 기존 소비자 스키마 불변,
  구버전 캐시만 있으면 r20 단독 폴백. 이력 6봉 미만이면 r5 생략 (리뷰 P2 반영).
- 검증: 블렌드 산식 단위 테스트 + 라이브 스모크 (13개 섹터, 신뢰도 상한 동작 확인).

## 2026-08-07 — feat(experts): 섹터 카운슬 전문가 도입 (9번째, 종목 판단 전용) + 코드리뷰 반영

사용자 요청("섹터/업종별 전문가 — 조사 결과를 종목 매수/매도 판단에 반영"). 섹터별
전문가 다수 대신 **단일 전문가가 13개 섹터를 내부 분석**하는 카운슬 설계 — 체제 집계
희석 방지 + 비용 1명분 유지. shadow 관측 후 실반영 예정.

- **신규 `src/experts/sector_council.py`**: 반도체·IT·자동차·2차전지·바이오·건설·조선·
  철강·은행·증권·보험·화학·방산 13개. 섹터당 정량 60%(ETF 20일 모멘텀,
  SectorMomentumProvider 재사용) + 정성 40%(Perplexity 1회 호출 → 섹터별 정규식
  파싱 — 통짜 json.loads는 LLM 응답에서 자주 깨져 부분 복구 방식). valid_hours=3.
- **체제 집계 3종 제외**: `NON_REGIME_EXPERTS` — aggregate_bias·bear_consensus에서
  스킵 (regime_score는 허용목록이라 자동 제외). 브리핑 분포 카운트에서도 제외.
- **cross_validator 규칙#12 (shadow)**: BUY 종목 섹터 점수 ≤ -40이면 감지만 기록
  (`rule12_shadow_log.jsonl`, 일일 dedup). 규칙#11과 동일하게 적중률 관측 후 전환.
- **브리핑 "섹터 동향" 섹션**: 강세/약세 상위 3개 표시 (±15 이상만).
- **SECTOR_ETF_MAP 확장**: KODEX 증권(102970)·보험(140700) + 키워드 폴백 추가.
- **run_trader**: sector_council에 KIS 브로커 주입 (장전 ETF 캐시 만료 대비).
- **코드리뷰 (python-reviewer 에이전트, P0 0건·P1 2건·P2 4건 → P1 전량+P2 2건 수정)**:
  ① 섹터 동기 조회를 `SectorMomentumProvider.sector_of_cached()` 공식 메서드로 승격
  — 파일캐시 히트 시 인메모리 메모이즈 (validate() sync 핫패스 반복 디스크 I/O 제거),
  private 헬퍼(_keyword_sector 등) 크로스모듈 임포트 제거. ② 규칙#12가 규칙#4의
  해석된 sector 변수 재사용. ③ refresh_minutes 주석 정정 (실제 TTL은 valid_hours).
  잔여 P2 2건(provider 이중 인스턴스 ETF 캐시 경합·protected 접근)은 낮은 위험으로 보류.
- 검증: 라이브 스모크 13개 섹터 분석 성공 (qual 파싱 정상), 메모이즈 assert 통과,
  재시작 후 "[전문가] 9명 등록 완료" 확인. 문서: `docs/agents/expert-system.md` 갱신.

## 2026-08-06 — fix(experts): 갭risk 전문가 "데이터 부족" 오표기 + KIS 야간선물 3번째 적용

브리핑 제보("야간 신호 데이터 부족한데 신뢰도 78%?") 조사 — 실제로는 신호 6/7 수집
성공 상태였음. `weekend_signal_expert.py` 3건 수정 (사용자 승인):

- **"데이터 부족" 오표기**: findings는 임계치 초과 신호만 줄이 생기므로 전 신호가
  중립 구간이면 빈 리스트 → 기본 문구가 "데이터 부족"으로 잘못 표기됨.
  수집 ≥3개면 "야간 신호 중립 (수집 n/7)"로 구분, 미만일 때만 "데이터 부족".
- **무표기 감점 분기**: NQ -0.7~-2.0(-6점)·NQ 강세(+8)·KRW 원화강세(+5)·
  VIX 경계(-6)/안정(+5)·BTC risk-on(+5) 분기가 사유 없이 점수만 반영
  → 각각 finding 한 줄 추가 (브리핑의 "-6 · 데이터 부족" 조합 원인이 NQ 분기였음).
- **KR 야간선물 KIS 1순위** (kr_scheduler·kr_market_expert에 이은 3번째 적용):
  죽은 KS200=F를 gather에서 제거, KIS `get_night_futures_quote()` 우선 +
  NKD=F×0.7 프록시는 폴백으로 유지. 프록시 -0.65% vs 실제 -1.63%로
  희석되던 갭다운 신호가 임계(-1.0) 정상 발동.
- 실측: 동일 시장 상황에서 기존 -6·중립·"데이터 부족" → **-26·BEAR + 사유 3줄**
  (S&P -0.54%·NASDAQ -1.57%·KR 야간 -1.63% 갭다운 주의). 재시작 검증 완료.

## 2026-08-06 — fix(experts): 아침 전문가 브리핑 야간선물 누락 — KIS 직접 시세 경로 재사용

전일 KIS 야간선물 수집 복구(051b5e6) 후속. 07:30 전문가 브리핑에 야간선물이 계속 누락된다는
텔레그램 제보 — `kr_market_expert._fetch_kospi200_futures`가 KIS가 아닌 자체 yfinance
체인을 쓰고 있었고 사실상 전멸 상태였음:

- `KS200=F` 상장폐지(HTTP 404), `^KS200`은 현물 시간봉이 3일에 ~18개뿐이라
  24시간 비교(iloc[-24]) 불충족으로 **항상 None** → 니케이 NKD=F 프록시(×0.7)만 잔존.
  프록시 축소값이 ±1% 미만이면 findings 표기 기준에 미달해 리포트에서 누락.
- 수정: KIS `get_night_futures_quote()`(A01609/CM)를 1순위로 재사용 —
  주간 종가 대비 밤사이 변동률 직접 반영, 실패 시 기존 yfinance 체인 폴백 유지.
  반환 스키마(`overnight_chg_pct/source/last`) 불변, source="KIS:A01609"로 식별.
- 실측: 07:51 조회 1025.10 (-1.63%, 세션=night) — 야간장 마감(05:00) 후에도
  CM 구분이 지난밤 마감 데이터를 반환함을 확인 (아침 시간대 동작 검증).

## 2026-08-05 — fix(data): KOSPI200 야간선물 수집 복구 — 종목코드 신형식 + 야간 시장구분 CM

텔레그램 문의("야간선물 제대로 수집되고 있는지")로 조사 — **도입(2026-06-07) 이래 단 한 번도
성공한 적 없었음** (성공 로그 0건, 아침마다 HTTP 500/빈 응답 → US 지수로 무음 폴백).
`src/data/providers/kis_market_data.py` 3중 결함 수정:

- **종목코드 체계 개편 미반영**: 구형 `101{연도문자}{월코드}` 생성 → KRX 개편 후
  신형 `A01 + 연도끝1자리 + 월2자리` (2026년 9월물 = `A01609`).
  KIS 마스터 `fo_idx_code_mts.mst` 실측으로 확인. 구형 코드는 rt_cd=0에 output1 빈 값.
- **야간 세션 시장구분 누락**: `FID_COND_MRKT_DIV_CODE=F`는 주간 시세만 반환.
  야간(18:00~05:00)은 `CM` — CM 우선 조회 후 빈 응답이면 F 폴백.
  야간 응답의 `futs_sdpr`(기준가)=주간 종가라 `futs_prdy_ctrt`가 곧 밤사이 변동률.
- **응답 필드 키 불일치**: `prdy_vrss`/`prdy_ctrt`로 읽었으나 실제 키는
  `futs_prdy_vrss`/`futs_prdy_ctrt` (코드가 맞았어도 변동률 0으로 나왔을 결함).
  구키는 폴백으로 유지. 결과에 `session`(night/day) 필드 추가.
- 실측 검증: `A01609` 야간 1028.75 (-1.28%, 주간종가 1042.05 대비) — 외부 제보값
  (야간 시가 1023.75, 주간 대비 -1%대 조정)과 일치. 재시작 검증 완료.
- 소비처: `kr_scheduler.py` 아침 스크리닝(선행지표, US 지수보다 우선) + `scripts/futures_monitor.py`
  — 익일 아침 로그의 `[KIS] KOSPI200 야간선물` 라인으로 최종 확인 예정.

## 2026-08-05 — feat(llm): 배치 LLM 라우팅 Codex CLI → Manus API 전환

GPT를 API가 아닌 client(로컬 Codex CLI, ChatGPT 구독)로 쓰던 배치 경로를
Manus API v2로 교체. 실 API 스모크 테스트(11.9s, manus-1.6-lite) + 재시작 검증 완료.

- **신규 `src/utils/manus_client.py`**: task.create → task.listMessages 폴링(5s) →
  `agent_status=stopped` 시 assistant_message / structured_output_result 추출.
  기존 CodexResponse 호환 인터페이스(`complete(prompt, input_data, output_schema)`,
  `.json()`) 유지. waiting(추가 입력 요구)·타임아웃 시 task.stop으로 크레딧 낭비 방지,
  실패 시 기존 OpenAI/Gemini API 경로로 자동 폴백 (fail-open 라우팅은 기존과 동일).
- **`src/utils/llm.py`**: `_try_codex` → `_try_manus`, `LLMConfig` codex_* →
  manus_*(enabled/agent_profile/timeout_sec/tasks), `CODEX_ALLOWED_TASKS` →
  `MANUS_ALLOWED_TASKS` (allowlist 동일: trade_review/strategy_analysis/market_analysis).
  통계 키 `codex_count` → `manus_count`, 모델 라벨 `manus:<profile>`.
- **`config/default.yml`**: `llm.codex` → `llm.manus` (agent_profile manus-1.6,
  timeout 600s — 태스크 기반 비동기라 Codex 240s보다 여유). evolved_overrides에
  codex 키 없음 확인 (머지 충돌 없음).
- **삭제**: `src/utils/codex_client.py` (참조처 llm.py 1곳뿐이었음)
- **.env**: `MANUS_API_KEY` 추가 (인증 헤더 `x-manus-api-key`)
- 실시간 매매 경로 금지 원칙 유지 — 응답이 수십 초~수 분이라 배치 전용.
- 문서: `docs/integrations/external-apis.md` Manus 섹션 추가

## 2026-08-05 — fix(scheduler): 엔진 모니터링 발견 P2 3건 수정 (안전자산 영구 비활성·텔레그램 HTML·시장추세 AttributeError)

텔레그램 요청 엔진 모니터링에서 발견된 경고 3건 수정. 재시작 검증 완료.

- **안전자산 루프 일시 장애 → 영구 비활성 오판** (`kr_scheduler.py run_safe_asset_loop`):
  09:33 KIS HTTP 500 장애 시점에 후보 4종목 이름 조회가 전부 실패하자 키워드 미매칭과
  동일하게 취급해 루프를 영구 종료. `_names_fetched` 카운터 추가 — 이름 조회 0건이면
  일시 장애로 간주해 다음 5분 주기 재검증, 영구 비활성은 이름 조회 성공 후 키워드
  미매칭인 경우에만 발동. 후보 검증 예외 로그 debug→warning 승격.
- **텔레그램 HTML 파싱 실패로 알림 유실** (`utils/telegram.py`, `kr_scheduler.py` 팀심의):
  팀심의 요약 알림의 배분 거부 사유(`최소 포지션 금액 미달 (0 < 200,000)`)와 LLM 요약의
  원시 `<`가 Telegram HTML 파서를 깨뜨려 11:31/13:00/14:01 3건 전량 유실 (400
  can't parse entities는 재시도 3회도 동일 실패). ① 팀심의 알림 동적 필드
  `html.escape()` 적용, ② `send_message`/`send_alert` 공통: 해당 400 감지 시
  `parse_mode` 제거 후 plain text 폴백 재발송 (전 발신 경로 안전망).
- **시장추세 갱신 AttributeError** (`kr_scheduler.py run_market_trend_monitor`):
  `bot.engine.risk_manager`(engine.py 신호 관리자)에서 `update_market_trend`를 호출
  — 해당 메서드는 `risk/manager.py`의 RiskManager(`bot.risk_manager`) 소속 (동명
  클래스 혼동, MEMORY.md 기록 패턴). 2분 주기 KOSPI/KOSDAQ 장중 추세 갱신이 계속
  no-op이었음 → 참조 수정으로 recovering/하락세 판정 정상화.
- 문서: `docs/integrations/external-apis.md`(텔레그램 폴백),
  `docs/operations/monitoring-checkpoints.md`(안전자산 재검증 정책)

## 2026-08-05 — fix(engine): P2 일괄 처리 32건 + rsi2 폐지 완결 + trade_events market (c66e9da)

같은 날 P1 커밋(79ce79c)의 후속. 사용자 지시로 P2 전량 수정 → 최종 적대적 리뷰
(신규 P0/P1 없음, 지적 P2 1건 반영) → 재시작 검증 완료.

### 정책 조사 결과 2건 (사용자 요청)
- **rsi2 `strategy_allocation: 0.0`은 의도된 폐지가 맞음**: evolved `_meta` 2026-08-02
  기록 확인 (단독 -15.44%/손익비 0.98, 제거 시 +26.16%; core 30/sepa 20 재배분 동반).
  실거래도 폐지 후 0건 (마지막 6/23). 단 배치 T+1 라인이 `enabled`를 무시해 이론상
  시그널 생성 가능 + allocation 0.0=무제한이라 상한도 없던 이중 부실
  → **배치 라인에 enabled 게이트 추가** (키 부재 시 경고 후 비활성 취급, fail-closed).
  0.0=통과 시맨틱 자체는 strategic_swing 용례가 있어 전역 변경하지 않음.
  CLAUDE.md 전략 표에 폐지 표기 (17.5% 기재는 낡은 문서였음).
- **trade_events.market 컬럼 부재**: us_scheduler 직접 INSERT 2곳(부분 익절·전량 청산
  SELL)이 부재 컬럼 참조로 무음 실패 중이었음 → `_ensure_tables()`에 ALTER 마이그레이션
  추가 + 라이브 DB 즉시 적용 + 기존 US 심볼 행 399건 market='US' 백필.

### engine.py (11건)
교체 축출 entry_signal_score 부착(fill 시 시그널 캐시 score) / 매도 폴백 submit_order
반환 검사(실패 시 재시도, 2회 초과 CRITICAL) / 부분체결 예약현금 비례 차감 / 사이징
available에 코어 예약 차감(G3 정합 — "축소 대신 전면 거부" 제거) / 배율 축소의
min_position_value 바닥 클램프 / realized_pnl sell_qty 사용 / DB 백필 기준선 재설정
(_daily_stats_restored 플래그) / SELL 쿨다운 면제(BUY 한정) / daily_max_trades pending
BUY 합산(TOCTOU) / RiskManager 쪽 죽은 _pending_sector_map 제거 / _counted_buy_order_ids
JSON 영속화

### risk·유틸 (5건)
`_stop_loss_rebound_used` 파일 영속화(재시작 시 1회 제한 우회 방지) / daily_stats·
stop_loss_today·exited_today 원자적 쓰기 / atomic_io fsync+tmp 정리 / config_persistence
_save 예외 재전파(persist-first 롤백 데드코드 활성화) / `_LOSS_EXIT_TYPES`·trade_memory
손실 패턴에 emergency_stop 추가

### 청산·US (7건)
US ExitConfig 6필드 배선(min/max_stop·stale·ATR — 기존엔 YAML 무시하고 dataclass 기본값)
/ 재시작 등록 전략별 max_holding 배선 / 추가매수 시 initial_quantity 갱신(재시작 정합성
오탐→TP1 이중 발행 차단) / breakeven 분기 급락 TS 판정 시점 캡 / US sync 수량 보정 시
ExitManager remaining 동기화(**감소 방향만** — stale 스냅샷 상향 복원 race 차단, 최종
리뷰 반영) / earnings_drift 기본값 fail-closed / kis_kr.modify_order 최소 방어

### KR 스케줄러·검증 (7건)
코어 빈슬롯 pending 집계 실데이터화(_pending_orders set+시그널 캐시) / 코어 스케줄러
섹션 순서 교체(빈슬롯 continue가 금요 트림·초과비중 경고를 스킵하던 것) / 장중품질 RT
재확인에 ATR 동적 상한 적용 / cross_validator.validate(count_stats) — 팀 심의 shadow가
게이트 통계 오염하던 것 / 수동매수 15:30 이후 기동 시 다음 영업일 대기 /
_classify_exit_type에 emergency_stop 분류 복원(긴급 전량 매도가 manual로 오분류) /
asset_growth fs_div 이중 API 호출 제거(DART 호출량 절반)

### 보류 1건
- US 취소실패=체결 추정의 사유 구분: kis_us.cancel_order가 msg_cd를 반환하지 않아 구분
  불가. 현행(체결 추정 on_fill)이 이중 매도 방지 방향으로 보수적이므로 유지.
  kis_us.py에 msg_cd 반환 추가 시 재검토.

수정: `src/core/engine.py`, `src/risk/manager.py`, `src/utils/atomic_io.py`,
`src/core/evolution/config_persistence.py`, `src/core/evolution/trade_memory.py`,
`src/strategies/exit_manager.py`, `scripts/run_trader.py`, `src/schedulers/us_scheduler.py`,
`src/schedulers/kr_scheduler.py`, `src/core/cross_validator.py`, `src/core/batch_analyzer.py`,
`src/execution/broker/kis_kr.py`, `src/signals/fundamentals/asset_growth.py`,
`src/data/storage/trade_storage.py`, `config/default.yml`

## 2026-08-05 — fix(engine): 전체 엔진 재리뷰 — P1 14건 + 재리뷰 P1 2건·P2 2건 수정 (79ce79c)

5개 영역(엔진 시그널·주문 / 청산 ExitManager / KR 스케줄러 / US 스케줄러·run_trader /
리스크·검증·유틸) 병렬 심층 리뷰 → 전 P1을 본 세션에서 코드 재검증 후 수정 →
적대적 재리뷰로 수정분 검증(발견 P1 2건 즉시 반영) → 재시작·로그 확인 완료.

### A. 이중/과잉 매도 방지 (4건)
1. **batch monitor 분할 익절이 전량 매도로 변질** (`batch_analyzer.py`): position_monitor
   경로의 SELL Signal에 `metadata.quantity` 누락 → engine이 전량 폴백. WS 공백 구간에서
   30분 모니터가 먼저 +10% 감지 시 10% 대신 100% 매도되던 것 → metadata 추가
2. **US 체결 간주 경로의 on_fill 누락 → 이중 부분 매도** (`us_scheduler.py`): 포지션
   소멸(→`remove_position`)·수량 감소(→감소분 `on_fill`)·취소 실패=체결 추정(→주문수량
   `on_fill`) 3경로 모두 ExitManager 미반영 → pending 5분 만료 후 동일 익절 재발행이던 것.
   부수: 매도 취소 후 폴백 재제출 시 stage 롤백을 "재제출 포기 시에만"으로 이동
   (즉시 롤백 시 폴백 체결 후 stage 미승격 → 익절 중복 발동), 폴백 pending에
   `orig_qty`/`exit_type` 전파
3. **KR stale pending 취소 예외 시 무조건 해제 → 이중 매도 경로** (`kr_scheduler.py`):
   취소 API 예외(원 주문 생존 가능) 시 pending 유지-재시도로 전환 (0건 취소=해제 유지,
   15분 초과 시 강제 해제). 재리뷰 반영: WS 틱마다 재호출되는 함수라 **60초 스로틀**
   추가 (KIS rate limit 자가 소진 방지)
4. **US 재시작 정합성 리셋이 restore_stages에 의해 무효화** (`exit_manager.py`):
   register의 "익절 미실행 감지 → NONE 리셋"을 hp_cache 복원이 업그레이드로 되돌리던 것
   → `_integrity_reset_symbols` 마킹 후 restore_stages 스킵

### B. 스케줄러 슈퍼바이저 무효화 (kr_scheduler.py)
- 8/4 신설된 `_supervised()`가 실제로는 **10개 중 1개 루프만** 재기동 가능했다 —
  나머지 9개는 outer `except Exception`이 예외를 삼키고 정상 반환 → "종료 의도"로 간주.
  9곳 전부 `raise` 추가 (자정 리셋 루프 영구 사망 = 다음 날 일일손실 게이트 왜곡 방지)
- 미래핑 루프 6개 추가 래핑: fill_check / screening / rest_price_feed / market_trend /
  team_deliberation / health_monitor (+ health_monitor·team_deliberation outer except
  raise — 후자는 적대적 재리뷰 발견분)

### C. daily_trades 정합 (engine.py — 8/4 daily_max_trades 재도입의 후속 결함)
1. **부분체결 중복 카운트**: FillEvent 증분마다 +1이던 것 → `_counted_buy_order_ids`로
   주문 단위 카운트 (자정 리셋 clear)
2. **BUY 미영속 + 백필의 SELL 종속**: BUY 체결 시에도 `_save_daily_stats()`, DB 백필을
   SELL 존재·JSON 복원 여부와 독립 실행 (매수만 있던 날 재시작 시 브레이크 유실 방지)
3. **백필 쿼리 market 미구분**: trade_events에 market 컬럼이 없어 US 야간 거래
   건수/USD pnl이 KR에 혼입 가능 → KR 6자리 심볼 필터 (`symbol ~ '^[0-9]{6}$'`)

### D. 재진입 게이트 (risk/manager.py + kr_scheduler.py)
1. **V자 반등 1회권이 검증 시점에 소모**: can_open_position 통과 직후 마킹 → 후속
   게이트(현금/포지션 수/브로커 거부)에서 매수 무산 시에도 당일 영구 차단이던 것
   → `on_buy_filled()` 신설, 체결 확인 시점(fill_check BUY + KR sync 신규 포지션)에 소모
2. **`_stop_loss_today` 등록이 저널 성공에 종속**: 유일한 라이브 등록 경로가 저널
   record_exit 이후 같은 try 안 → 저널 예외(과거 TypeError 사고 전례) 시 손절 재진입
   차단 통째로 무음 실패 → 저널 앞 독립 블록으로 이동 + `is_full_exit` 전달
   (스냅샷 수량 비교 + on_fill 후 상태 소멸 보강, 기존 이중 호출 2곳 제거)

### E. US·레짐 (us_scheduler.py + exit_manager.py)
1. **KR 정규장 US WS 강제 종료가 no-op**: `_maybe_stop_us_price_ws()`가 포지션 보유 시
   즉시 반환 — 오버나이트 US 포지션 보유 시 매 KR 장마다 approval_key 충돌 방치
   → `force=True` 파라미터
2. **config_raw 경로 오류 2곳**: `config_raw`는 이미 us: 섹션인데 `.get("us")` 재호출
   → excluded_symbols(IXC/GUSH 등) 가드가 항상 빈 집합
3. **sync 선점 등록 파라미터 고착**: 매수 pending 중 KIS 이력 빈 응답 시 sync가 먼저
   포지션 생성 → "_sync" 타이트 파라미터(SL3/TS2/stale2일)로 등록되고 이후 복원 불가
   → 매수 pending 감지 시 전략 파라미터로 등록
4. **레짐 TS 적용이 ATR 연동 소실** (8/4 수정이 만든 역방향 버그): effective TS를
   레짐값으로 단순 치환 → `min(max(레짐TS, ATR×mult), cap)` 재계산으로 전환

### 기록만 (P2 잔여 — 후속 배치 처리 대상)
- **[우선] trade_events US INSERT가 부재 컬럼(market) 참조** — US SELL 이벤트 기록이
  전부 실패 중일 가능성 (적대적 재리뷰가 라이브 DB로 확인, 기존 버그)
- **[의도 확인 필요] evolved_overrides `rsi2_reversal: 0.0`** — 코드상 0.0은 "예산 무제한"
  (문서상 17.5%와 배치). 폐지 의도였다면 정반대 동작
- 엔진: entry_signal_score 항상 0(축출 점수 게이트 무력)·폴백 submit_order 반환 미확인·
  부분체결 예약금 미차감·코어 예약 사이징/게이트 불일치·LLM soft-reject 소액 전락·
  SELL 쿨다운 사이드 무관·daily_max TOCTOU·백필 기준선 0 저장·daily stats 복원의
  evolution 종속·`_counted_buy_order_ids` 미영속
- ExitManager: US ExitConfig 배선 누락(min/max_stop, stale)·추가매수 initial_qty 미갱신·
  breakeven 분기 crash TS 무력화·KR/US sync 수량 보정의 remaining 미반영·US 취소실패
  on_fill 오판 가능성(사유 미구분)
- 리스크/유틸: rebound_used 미영속·emergency_stop 데드 조건·atomic_io fsync 부재·
  daily_stats/stop_loss_today 비원자 쓰기·config_persistence 롤백 데드코드·
  asset_growth fs_div 중복 호출·modify_order 데드코드·밸류코어 스캔 시각 주석 불일치
- KR 스케줄러: 코어 빈슬롯 pending 오참조(set을 dict로)·섹션3 continue 스킵(금요 트림
  불능)·장중품질 RT cap 무효화·팀심의 cv 통계 오염·수동매수 장마감 후 즉시 제출
- US: run_trader 재시작 등록 max_holding 미배선·earnings_drift 기본값 True footgun

수정: `src/core/engine.py`, `src/core/batch_analyzer.py`, `src/schedulers/kr_scheduler.py`,
`src/schedulers/us_scheduler.py`, `src/strategies/exit_manager.py`, `src/risk/manager.py`

## 2026-08-05 — docs: /doctor 문서 정리 — CLAUDE.md 트림 + 지연 로딩 마이그레이션 (미커밋)

코드 변경 없음. 세션마다 로드되는 CLAUDE.md에서 코드로 파생 가능한 내용을 제거해
상주 컨텍스트 est. ~1.9k 토큰 절감.

- **CLAUDE.md**: 아키텍처 다이어그램·스케줄러 표·디렉토리 구조·WebSocket 피드·
  운영 모니터링 표·의존성 목록·에이전트 명단 상세 삭제 (docs/ 및 소스로 파생 가능,
  포인터로 대체). `/home/user/...` 낡은 경로 3곳 → `/home/ubuntu/...` 수정.
  트러블슈팅 → `docs/operations/runbook.md` 포인터로 교체.
  대시보드 개발 패턴 → `.claude/skills/dashboard-feature/SKILL.md` 신설 이전.
- **docs/operations/runbook.md**: CLAUDE.md에만 있던 트러블슈팅 2건 병합
  (WebSocket 중복 프로세스, 유령 포지션/포트폴리오 동기화).
- **.claude/skills/dashboard-feature/SKILL.md**: 신설 (대시보드 작업 시에만 로드).

## 2026-08-04 — fix(engine): 수정분 적대적 재리뷰 반영 — P1 4건 + P2 3건

같은 날 P0/P1·P2 수정 커밋(2988646, edc5b67)에 대한 재검증 리뷰 결과 반영.

### P1 (수정이 만든 문제 / 미달 수정)
1. **장중 리셋 가드의 기준선 공백**: "새 거래일 + 장중 첫 기동"(정상 시나리오)도 가드에
   걸려 `daily_start_unrealized_pnl=0` 잔류 → 누적 미실현 전체가 당일 손익으로 계산되어
   일일손실 한도 오발동 → 가드 스킵 시 기동 시점 미실현으로 기준선 재설정
2. **급락 dynamic SL 조임의 영구 잔류**: 해제 경로에 복원 로직이 없어 ATR 손절이
   조여진 채 보유 종료까지 잔류 → 상태 변형 제거
3. **급락 SL이 min_stop 클램프에 여전히 무효** (ATR 포지션): check_exit **판정 시점 캡**
   방식으로 전환 — 급락 활성 중 크래시 SL(2.0~3.0%)이 클램프 우회로 실효,
   해제 시 자동 원복 (2·3번은 동일 원인의 재설계)
4. **daily_trades DB 백필 의미 불일치**: 매도 건수로 백필돼 재도입된 daily_max_trades
   게이트가 부당 소진 → BUY 건수 별도 조회로 백필

### P2
- `atomic_io` encoding="utf-8" 명시 (locale 의존 제거)
- 팀 심의 allocator의 `bot.portfolio` 오참조 잔존 1건 → `bot.engine.portfolio`
- 슈퍼바이저 재기동 백오프 (60초→최대 30분 배증 — 즉시 재발 예외의 알림 스팸 방지)

### 재리뷰 통과 확인 (16개 수정 항목 중 나머지)
폴백 수량(_pending_quantities 부분체결 갱신과 정합)·exit_exempt 주입 순서·섹터맵
라이프사이클·pending 만료의 이중매도 안전성(엔진 pending 가드가 차단)·rollback no-op
전환(호출자 6곳 반환값 미사용)·batch_signal 이월 유지·KOFR 언패킹·철강 117680 등 통과.
잔존 기록: cancel_all 인메모리 의존(P2)·급락 중 레짐 재분류 시 effective TS 완화(P2)·
영문 reason "stop" 오분류 잠재 경로(현재 KR reason 전부 한국어라 무해)

수정: `src/core/engine.py`, `src/strategies/exit_manager.py`,
`src/schedulers/kr_scheduler.py`, `src/utils/atomic_io.py`

## 2026-08-04 — fix(engine): 리뷰 잔여분 일괄 처리 — 보류 정책 2건 + P2 12건

전체 엔진 리뷰(같은 날 P0/P1 커밋)의 후속. 사용자 지시로 잔여 건 전부 진행.

### 정책 결정 2건
- **daily_max_trades 재도입**: 과거 "가용 현금이 게이트"로 제거됐으나 문서·대시보드가
  계속 한도를 표기 → 문서화된 동작으로 복원. BUY만 차단, 청산 무제한 (`engine.py`)
- **배치 T+1 시그널의 09:30~10:30 -8 페널티 면제**: `metadata.batch_signal` 마커 기반
  (sepa/rsi2/vcp 09:30 집행은 설계상 정상 — core_holding 면제와 동일 근거).
  장중 자동진입 sepa는 마커가 없어 페널티 유지. neutral 장 sepa 하드 차단(규칙 3-3)은
  **변경 보류** — 매수 빈도에 직접 영향이라 별도 데이터 검토 필요

### P2 수정 12건
- 진화 스케줄러 실행일 영속화 (`evolution_state.json`) — 20:30 윈도우 재시작 중복 방지
- 안전자산(KOFR) 매수/매도 제출 결과 확인 — 실패 시 상태 저장/clear 금지
- `SECTOR_ETF_MAP` 철강 069500(KODEX 200!) → **117680**(KODEX 철강) — 시장 전체
  상승을 철강 급등으로 오인하던 매핑 오류
- 대시보드 정산 수수료율 하드코딩 제거 → `FeeConfig` 파생 (0.000141→0.000140527)
- `modify_order` 킬스위치 검사 추가 (매수 정정 추격 차단) + `or` falsy 패턴 제거
- `position_multiplier` 적용 후 max_value 재클램프 — 부스트가 G3 전체 거부로
  변질되던 것 (시즈널리티 경로와 동일 패턴)
- **원자적 쓰기 헬퍼** `src/utils/atomic_io.py` 신설 → `evolved_overrides.yml`·
  exit stage 파일·`pending_signals.json`·`core_holding_state.json` 적용
  (파손 시 진화 파라미터 default 회귀/stage 소실 방지)
- ExitManager: ① 당일 매수 후 당일 익절 포지션의 initial_qty 영속화 폴백
  ② rollback_stage 레거시 다운그레이드 제거 (sell_all 실패 시 stage 부당 하락 →
  1차 익절 중복 매도 경로 차단) ③ **pending_stage 5분 자동 만료** (`pending_since`
  신설 — rollback 미호출 경로에서 분할 익절 영구 불능이던 무음 실패 해소)

수정: `src/core/engine.py`, `src/core/cross_validator.py`, `src/core/batch_analyzer.py`,
`src/core/evolution/config_persistence.py`, `src/schedulers/kr_scheduler.py`,
`src/strategies/exit_manager.py`, `src/execution/broker/kis_kr.py`,
`src/dashboard/data_collector.py`, `src/data/providers/sector_momentum.py`,
`src/utils/atomic_io.py`(신규)

## 2026-08-04 — fix(engine): 전체 엔진 흐름 합동 리뷰 — P0 4건 + P1 8건 수정

4개 영역(시그널 경로·청산 경로·스케줄러·리스크) 병렬 심층 리뷰 → 전 P0/핵심 P1을
본 세션에서 직접 코드 재검증 후 수정. 공통 패턴: **"로그에는 작동하는 것처럼 보이지만
실제로는 꺼져 있는 무음 실패"**.

### P0 (4건 — 전건 검증 후 수정)
1. **매도 폴백 전량 청산** (`engine.py`): 부분 매도(분할익절 10%·코어 트림) 지정가가
   90초 미체결 시 시장가 폴백 수량이 `pos.quantity` 전량 하드코딩
   → `_pending_quantities` 원 주문 수량으로 클램프
2. **포지션 교체 축출의 exit_exempt 가드 부재** (`engine.py`): 7개 청산 가드 중
   유일하게 누락. 자동매도 금지 087010(펩트론)이 정렬상 1순위 축출 후보였다
   → `exit_manager._exit_exempt` live set을 엔진 RiskManager에 주입 + 후보 제외
3. **당일 손절 재진입 금지 전면 미작동** (`risk/manager.py`): `_stop_loss_today` 등록
   코드의 호출자가 0건 (영속 파일이 디스크에 존재한 적 없음이 물증). V자 재돌파 요구·
   당일 1회 제한·스크리닝 제외 3중 방어가 전부 데드 코드
   → `record_exit(exit_type=stop_loss)`에서 등록 + 스크리닝 제외 참조를
   `bot.risk_manager`(실데이터)로 교정 (기존엔 항상 빈 engine 쪽 set 참조)
4. **daily_stats 파손 시 장중 풀 리셋** (`engine.py` + `kr_scheduler.py`): 비원자적
   쓰기 파일이 파손되면 재시작 시 일일손실 기준선 0 리셋 + 미체결 전량 취소 강행
   → 원자적 쓰기(tmp+os.replace) + 장중(08:50~15:40) 복원 실패 시 리셋 생략 fail-safe

### P1 (8건 수정)
- **min_stop_pct 클램프 축소** (`exit_manager.py`): 4% 하한이 급락 오버라이드(2.0~3.0%)·
  전략별 타이트 손절까지 무력화 → ATR 산출값(dynamic)에만 적용
- **장중급락 파라미터의 None TypeError + effective 트레일링 미동기화**: None 가드 추가,
  dynamic SL·effective TS 동시 조임, 레짐 적용 시 effective TS 동기화,
  vcp_breakout `_strategy_exit_params` 등록(기존 None 등록이 TypeError 원인)
- **섹터 맵 누수** (`engine.py`): 등록을 전 체크 통과 후로 이동 + 거부/clear_pending
  경로에서 엔진 맵 pop + 자정 리셋 clear (누적 시 섹터 한도 오차단)
- **`_idx_change` 데드코드** (`kr_scheduler.py`): 가중 등락률 대입 — 약세 주의구간
  (-0.5~-1.0%) "85점 컷 강화"가 4개월+ 미작동이었다
- **stale 매도 pending 잠김**: 취소 0건(이미 소멸)을 실패로 취급해 예약현금 1.015배가
  익일까지 잠기던 것 → 0건은 해제, API 예외만 유지-재시도
- **휴장일 from-import 스테일**: 월간 갱신이 낡은 스냅샷과 병합해 직전 갱신분 유실
  (2개월+ 무중단 시 추석 등 소실) → 모듈 속성 참조로 교체
- **스케줄러 루프 슈퍼바이저 신설**: outer-except 패턴 10개 루프(자정 리셋·배치 등)가
  예외 1건에 조용히 영구 사망 → `_supervised()` 래퍼로 60초 후 재기동 + 텔레그램 경보
- **팀 심의 배선 2건**: `bot.portfolio`(부재 속성)→`bot.engine.portfolio`,
  `engine._cross_validator`(오참조)→`engine.risk_manager._cross_validator`
  — shadow 표본이 "전 후보 게이트 차단"으로 무효 수집되고 있었다
- **ExitManager stage 파일**: 파일명을 저장 시점 날짜로 재계산(7일+ 무중단 후 재시작 시
  전량 소실 방지) + 빈 파일을 유효 상태로 취급(전일 stale stage 오복원 방지)

### 기록만 (수정 보류)
- `daily_max_trades`(10회) 미강제 — engine.py:753에 의도적 제거 주석 존재, 실제 제동은
  현금 게이트+max_daily_new_buys(5). 정책 재확인 필요
- 배치 T+1 전략(sepa/rsi2/vcp)의 09:30 실행 -8 페널티, neutral/sideways sepa 하드 차단
  (레짐 체계 이원화) — 설계 의도 확인 필요한 정책 사안
- P2 약 30건 상세는 리뷰 보고 참조 (수수료 하드코딩, 캐시 축적, 비원자 쓰기 잔여 등)

수정: `src/core/engine.py`, `src/risk/manager.py`, `src/schedulers/kr_scheduler.py`,
`src/strategies/exit_manager.py`, `scripts/run_trader.py`

## 2026-08-04 — feat(kr): 밸류코어(가치·성장 장기보유) 신설 — shadow 관측 개시

사용자 요청("가치주·성장주 30~40% 투자, 장기 보유")로 신규 라인 구축.
설계 문서: `docs/strategies/value-growth-core-design.md` (LLM 정성 검증 단계 포함 승인).

### Phase 0 — 백테스트 검증 (`quick_backtest --idea kr_value / kr_growth` 신설)
KOSPI 시총 상위 300 × 5개년(2021~2025 매년 4/1 리밸런스, 12개월 보유), DART 실데이터
1,282 종목-연도 관측치 (⚠️ 현재 시점 유니버스 — 생존 편향, 상대 비교 한정):
- **성장 버킷 채택**: 매출≥15%+영익≥25%+PER<25 → **+33.7% vs 베이스라인 +26.0%
  (+7.7%p, t=3.04, 연도승 4/5)**
- **가치 버킷 조건부**: B/M 최저평가 승률 61.7% vs 고평가 46.3% (구조 확인)이나
  평균 초과 +0.9%p로 약함, 퀄리티 결합 연도승 2/5 → shadow 관측으로 최종 판단
- 발견: 성장 버킷에서 "2년 흑자" 필터가 +34.1%→+24.9%로 수익 절삭 (턴어라운드 배제)
  — min_profit_years 완화는 shadow 관측 후 재검토
- pykrx 히스토리 펀더는 KRX 인증 장애로 불가 → DART+FDR+yfinance 조합 (실운용과 동일 소스)

### 구현 (Phase 1~3)
- **`src/signals/fundamentals/financials.py`** — `FinancialsProvider`:
  fnlttSinglAcnt 1콜 = 3개년 매출/영익/순익/자산/부채/자본 + 파생지표(YoY/ROE/부채비율/
  흑자연수/마진개선). 30일 캐시(실패 1일), asset_growth 패턴 미러링
  - ⚠️ DART 실측 함정 2건: 순이익 계정명 **"당기순이익(손실)"**(접두 매칭 필수),
    **fs_div 파라미터 무시**(항상 CFS+OFS 동시 반환 → 파싱에서 행 필터).
    1차 백테스트에서 이 버그로 퀄리티 표본 0건 → 수정 후 재실행
- **`src/signals/screener/value_growth_screener.py`** — CoreScreener 상속.
  사전필터(유동성·MA200×0.9·60일≥-15%·금융/지주 제외) → DART 재무 fail-closed →
  자격필터(2년 흑자·부채<200%) → 가치/성장 2버킷 스코어링(각 100점, 컷 70) →
  섹터 캡(업종당 1, 저PBR 쏠림 방지). PBR/PER은 후보군 내 상대 분위 배점
- **`src/signals/fundamentals/value_qualitative.py`** — LLM 정성 검증 (gpt-5.4):
  사이클 피크/이익의 질/디스카운트 성격/재평가 촉매 4항목×25점, 컷 60, fail-closed.
  판정은 `~/.cache/ai_trader/value_qualitative/`에 영속 (다음 분기 컨텍스트 재사용)
- **스케줄러**: `run_value_growth_shadow_scheduler` (kr_scheduler) — 매주 월 10:40,
  스캔→LLM→`~/.cache/ai_trader/value_growth_shadow.json` 저장→텔레그램 보고. **주문 없음**
- **`kis_market_data.fetch_stock_valuation`**: 업종명(`bstp_kor_isnm`) 필드 추가 (섹터 캡용)
- **`config/default.yml`**: `kr.strategies.value_growth_core` 블록 (shadow_mode: true)

### 운영 계획 (설계 §2·§8)
- shadow 2~4주 → 실배분 15% → 1분기 후 30% (배분 재편은 실배분 전환 시:
  gap 35→20, core 30→25, sepa 20→15, swing 5→0 — 전환 시점에 최종 재확인)
- 실배분 전환 시 필수: 주문 배선 + ExitManager 레짐 제외 등록(REGIME_EXIT_PARAMS
  덮어쓰기 사고 방지) + cross_validator 수급 규칙 예외 + strategy_allocation 키 등록

### 코드리뷰 반영 (P0 0 · P1 3 · P2 8 — P1 전건 + P2 5건 수정)
P1은 모두 "shadow 관측 데이터의 대표성" 훼손 유형이라 관측 시작 전 수정:
- **P1-1 유니버스**: 상속받은 `get_top_stocks(150)`이 설계(시총 3000억+)보다 좁음
  → `universe_limit` 설정화, 밸류코어 300 (코어홀딩은 기존 150 유지)
- **P1-2 배당 10점 미배선**: DIV 소스 부재로 실효 만점 90점이던 것을 배점 재분배
  (PBR 분위 20→25, 이익 안정성 10→15). 소스 확보 시 재도입 주석 명기
- **P1-3 LLM 대상 버킷 쏠림**: 글로벌 top-8 → **버킷별 top-4** (상대분위 기반 가치
  버킷이 구조적으로 고득점해 성장 버킷이 관측에서 밀리는 문제)
- P2: `x and x/1e8` 금지 패턴 제거 · 캐시 스키마 변경 시 TypeError → 무효화 처리 ·
  reasons 버킷 혼재 분리 · 월요일 공휴일 시 ISO 주 기준 다음 영업일 폴백 ·
  스캔 윈도우 10:40(장중) → **16:10(장외)** 이동 (KIS rate limit 경합 회피) ·
  LLM max_tokens 300→1000 (reasoning 토큰 절단으로 인한 오탈락 방지)
- 미수정 P2 (기록): `_FIN_MAX_CONSEC_FAIL` 가드가 사실상 불활성 (프로바이더가 예외를
  삼키고 None 반환 — 시간 예산 가드가 실질 방어선, fail-closed 방향이라 안전) ·
  quick_backtest 연구 스크립트 내 0-falsy 패턴 잔존

수정: `scripts/quick_backtest.py`, `src/signals/fundamentals/financials.py`(신규),
`src/signals/fundamentals/value_qualitative.py`(신규),
`src/signals/screener/value_growth_screener.py`(신규), `src/signals/screener/core_screener.py`,
`src/data/providers/kis_market_data.py`, `src/core/batch_analyzer.py`,
`src/schedulers/kr_scheduler.py`, `config/default.yml`
문서: `docs/strategies/value-growth-core-design.md`(신규), `docs/strategies/kr-strategies.md`,
`CLAUDE.md`

## 2026-08-03 — chore(us): earnings_drift 재활성화 **보류 종결** (pending-decisions #6)

EPS 서프라이즈 가드 배선 + 통계 검증까지 마쳤으나 **활성화하지 않고 종결**한다.

### 검증은 통과했다 (`quick_backtest.py --idea earnings_drift` 신설)
24개월 S&P500 3,561 이벤트 / 반응일 종가 매수 후 10일 보유:
- 갭≥3%만(EPS 무관, 기존 프록시 방식) **+0.39% < 베이스라인 +0.76%** —
  2026-04-18 비활성 사유("갭만 보면 sell-the-news 무방비")를 데이터가 확인
- **EPS beat≥10% + 갭≥5% + 갭유지 +1.71% · 승률 61.0% (t=2.13, n=123)** — 조건부 통과
- 원본: `results/quickbt_earnings_drift_20260803.csv`

### 그럼에도 보류한 이유 2가지
1. **US 미운용** — 사용자가 미국 시장 투자 계획 없음을 확인. 실제로 systemd 서비스는
   `--market kr`이라 US 스케줄러가 기동조차 하지 않는다. 켜도 실행되지 않는 코드였다
2. **finnhub EPS 커버리지 부족** — 실측(2026-08-03) 어제~오늘 발표분 중
   actual+estimate를 모두 가진 종목이 **18개뿐**(전부 S&P500 밖), 원본 재조회 시 504.
   가드가 fail-closed라 켜도 거의 발화하지 못한다. 재활성화 전 선결 과제로 기록

### 남긴 것 / 되돌린 것
- **되돌림**: `config/default.yml` earnings_drift `enabled: true → false`
- **유지**: `EarningsProvider.get_recent_surprises()`(finnhub 어제~오늘 EPS 서프라이즈,
  1일 캐시, |estimate|<0.01 제외), 스케줄러 스캔 경로 2곳의 fail-closed 가드,
  `_USEngineBundle._earnings_surprise`, quick_backtest 검증 아이디어,
  튜닝값(min_gap_pct 5.0 / max_holding_days 10) — 재개 시 그대로 사용
- 봇 재시작 불필요: 현재 서비스는 KR 전용이라 US 전략 설정이 로드되지 않는다

수정: `config/default.yml`, `scripts/quick_backtest.py`, `scripts/run_trader.py`,
`src/data/providers/earnings.py`, `src/schedulers/us_scheduler.py`
문서: `docs/strategies/us-strategies.md`, `docs/strategies/pending-decisions.md`, `CLAUDE.md`

## 2026-08-03 — feat(agents): 심의 슬롯 2→4 확대 + 4개월 잠자던 NameError 발굴

### 슬롯 확대 (10:30 / 11:30 / 13:00 / 14:00)
승격 기준 200표본까지 ~17영업일이 걸려 관측 기간 단축 목적.
~12건/일 → ~24건/일, 200표본까지 **~8영업일**.
- 슬롯 창(분+10, 시 경계) 겹침 없음 자체 검증. `done_today` set이 슬롯별 중복 방지.
- 13:00은 13:50 자본활용률 체크·13:30 빈슬롯 윈도우와 무관 (심의는 shadow).

### 리뷰 중 발굴 — `run_market_trend_monitor` NameError (P1)
Pyright 지적을 추적하니 실제 버그였다. `now`가 정의된 적 없는 스코프에서
`now.strftime(...)` 호출 (`e92e829`, 2026-03-30 도입).
바깥 `except Exception → logger.debug`가 삼켜서 저널에 한 줄도 남지 않았고,
그 뒤에 있는 **08:50 LLM 장전 진단이 4개월간 한 번도 실행되지 않았다**
(로그 grep 실측 0건). 체제 갱신 자체는 NameError 이전에 완료돼 무사했다.
- `now = datetime.now()` 정의 추가.
- 삼킨 핸들러를 debug→warning + 예외 타입 포함으로 격상 —
  debug 레벨은 NameError를 4개월 숨겼다.

### 검증
- Codex 리뷰: P2 1건(주석 "장중 2회" 잔존)만 지적 → 반영. 그 외 이상 없음 확인.
- 슬롯 창 겹침·시 경계 자체 검증 통과, py_compile 통과, 재시작 에러 0건.


## 2026-08-03 — fix(us-exit): 전략별 max_holding_days 배선 갭 해소

P2 리뷰에서 발견한 기존 갭의 후속 정리. US 전략 config의 `max_holding_days`
(SEPA 20일 등)가 ExitManager에 전달되지 않아 **전 전략이 글로벌 기본 10영업일**로
강제 청산되고 있었다. earnings_reversal 한정 임시 배선을 일반화했다.

- `us_scheduler._strategy_max_holding()` 헬퍼 신설 — strategy 문자열로 전략
  인스턴스의 max_holding_days 조회 (미매칭/미설정 None → 글로벌, **0은 무제한
  의미라 falsy 판정 없이 그대로 전달**)
- 배선 2곳: 매수 체결 등록 + 재시작 복구 재등록(pos.strategy → _symbol_strategy
  폴백). 상태 파일이 max_holding을 영속화하므로 재시작에도 유지
- sync_detected(전략 불명 외부 진입)는 의도적으로 글로벌 유지 — 주석 명시
- **행동 변화**: SEPA 신규 매수 보유 한도 10→20영업일 (config 의도값 복원).
  momentum은 config 미설정이라 종전대로 10일. 기존 오픈 포지션은 저장된 상태
  유지(신규 매수부터 적용)
- 검증: 헬퍼 단위 7케이스(0=무제한 포함) + 재시작 후 에러 0건

수정: `src/schedulers/us_scheduler.py` / 문서: `docs/risk/risk-and-exit.md`

## 2026-08-03 — feat(quant): P2 3건 — 연구 백테스터·어닝 리버설(비활성)·자산증가율 감점

P0/P1(ae22214)에 이어 awesome-systematic-trading 검토의 P2 항목 구현.
핵심 설계: **"아이디어 → quick_backtest 1차 검증 → 정식 백테스터 → BacktestGate"
3단 깔때기**를 만들고, 신규 전략은 검증 통과 전 활성화하지 않는다.

### P2-3. 신규 전략 1차 스크리닝 도구 (`scripts/quick_backtest.py` 신설)
- **연구 전용 venv 분리** (`venv-research/`, gitignore): vectorbt+numba를 운영 venv에
  설치하면 numpy(2.4.4) 충돌로 실거래 엔진이 깨질 수 있어 완전 격리
- 내장 아이디어 3종 + 실행 결과:
  - `tom` (turn-of-month): **10년 유효** (SPY 윈도우 일평균 +0.092% vs +0.052%,
    KOSPI/QQQ 동일 방향) but **최근 3년 소멸** → P1-1 오버레이는 소폭(×1.10)이므로
    유지하되 분기 재검증 필요 항목으로 문서화
  - `lowvol` (KR 저변동성 quintile): **P1-2 강한 지지** — Q5 고변동군 20일 포워드
    +0.41%·승률 39.6% vs Q1 +5.63%·66.1%. 감점 임계 σ≥4% = Q5 경계와 일치
  - `earnings_reversal`: **기각** — 아래 P2-1 참조
- 판정 가이드가 출력에 포함됨 (채택/기각 기준 명시)

### P2-1. US 어닝 리버설 전략 — 구현 후 **검증 기각** (`us/earnings_reversal.py`, enabled: false 고정)
- 발표 전 5거래일 낙폭 ≥5% → 발표 전 매수 → 발표 후 3일 내 청산 (드리프트의 거울)
- `eng._earnings_upcoming` 신설 (finnhub 캘린더 오늘~+2일, 일 1회 갱신) —
  스케줄러 양쪽 스캔 경로에서 발표 예정 종목만 평가 허용, **캘린더 없으면 발화 금지
  (fail-closed)**. 발표 관통 갭 리스크로 사이징 ATR×0.5 고정 절반
- **검증 결과 (깔때기가 첫 실전에서 작동)**: finnhub 소표본(90건)은 방향성 지지처럼
  보였으나(낙폭군 +2.99%), yfinance 어닝 이력으로 표본을 3,711건으로 늘리자
  **낙폭과대군 -0.33%·승률 46.8% < 베이스라인 +0.36% — 리버설 엣지 없음, 기각.**
  오히려 급등군 +0.97%(t=2.01)로 드리프트 방향이 유효 → earnings_drift 재활성화
  (EPS surprise 가드 부착) 검토 근거로 기록
- 코드·캘린더 인프라는 유지 (레짐 변화 대비 + 어닝 데이터 파이프라인 재사용).
  전용 StrategyType.EARNINGS_REVERSAL 추가 (EARNINGS_DRIFT 재사용 시 재시작 복원과
  청산 설정이 드리프트로 오귀속되는 버그 — 리뷰에서 발견·수정)

### P2-2. 코어홀딩 자산 확장 감점 (`fundamentals/asset_growth.py` 신설)
- DART `fnlttSinglAcnt` 자산총계(당기/전기) → 전년 대비 증가율. CFS→OFS 폴백,
  30일 디스크 캐시, corp_code 맵은 기존 DartChecker 재사용
- 감점 전용: ≥50% → -5 / ≥30% → -3 (데이터 없으면 0, fail-open)
- `run_full_scan` 4.5단계 `_enrich_asset_growth` (수급 보강과 동일 패턴)
- 실조회 검증: 삼성전자 +10.2%(감점 없음), SK하이닉스 +46.9%(-3), 카카오 +7.8%

수정: `scripts/quick_backtest.py`(신설), `src/strategies/us/earnings_reversal.py`(신설),
`src/signals/fundamentals/asset_growth.py`(신설), `src/signals/screener/core_screener.py`,
`src/schedulers/us_scheduler.py`, `scripts/run_trader.py`, `config/default.yml`, `.gitignore`
문서: `docs/strategies/kr-strategies.md`, `docs/strategies/us-strategies.md`,
`docs/operations/runbook.md`, `CLAUDE.md`

## 2026-08-03 — fix(agents): Bear 판정 기준 캘리브레이션 — 매수 경로 병목의 실체

입력 기아 수정(fdba63c) 후 전체 사이클을 정밀 추적한 결과, 더 깊은 병목이 나왔다.

### 매수 문턱의 산수
BUY_THRESHOLD=20 기준, 토론 결과별 필요 분석가 종합: 만장일치 지지(+20)면 ≥0,
분열(-10)이면 ≥30, 만장일치 반대(-40)면 ≥60. 그런데 분석가 종합의 **이론 상단이
~52**(fund≤40/tech≤55/news 실측≤61의 가중평균)라서:
- 만장일치 반대 = 절대 거부권 (문턱 60 > 상단 52) — 의도된 fail-closed
- 실질 매수 경로 = 만장일치 지지 단 하나 (분열 경로는 "좋은 날"만 턱걸이)

### 병목의 실체 — Bear는 ACCEPT를 낼 수 없었다
실측 **19/19 REJECT** (프로덕션 13 + 실험 6). 근거 최상 케이스(어닝서프라이즈+
동반순매수+기술적 건전, 종합 52)에서도 REJECT. 원인은 프롬프트 비대칭:
- Bear: "문제 없음은 허용되지 않는다"(양보 금지) + ACCEPT 판정 기준 부재
- Bull: "부실하면 솔직히 인정하고 반대하라"(양보 장려)
- R2 Bear: "리스크가 **해소**됐다면 ACCEPT" — 불가능한 문턱
모델은 지시대로 실패 서사를 쓴 뒤 자기 서사에 설득돼 REJECT한다.

연쇄: 만장일치 지지 폐쇄 → conviction≥0.75 불가 → **PM 오버라이드 죽은 조항**
→ shadow 관측이 BUY 표본을 못 얻어 "매수 판정 품질" 측정 자체가 불가능.
따라서 이 수정은 임계값 튜닝이 아니라 **측정 도구 수리**다.

### 수정 (`src/agents/researchers.py`)
실패 시나리오 제시 의무는 유지하고 판정 기준만 명시:
일반론(차익실현·변동성·선반영)에만 기댄 시나리오 → ACCEPT,
근거의 핵심을 무너뜨리는 구체적 사실 → REJECT.
R2 "해소됐다면" → R1과 같은 기준(치명적 vs 감수 가능)으로 정합화.

### 검증 — 근거 사다리 5단계 (실제 LLM, seed 고정)
| | 분석가 | Bull | Bear | 합의 | total | 판정 |
|---|---|---|---|---|---|---|
| A 최상 | 52 | 지지 | ACCEPT | +20 | 72 | BUY |
| B 좋음 | 33 | 지지 | ACCEPT | +20 | 53 | BUY |
| C 보통 | 20 | 반대 | ACCEPT | -10 | 10 | HOLD |
| D 약함 | 10 | 반대 | ACCEPT | -10 | 0 | HOLD |
| E 악재 | -20 | 반대 | REJECT | -40 | -60 | HOLD |

완전한 단조 사다리. 역할도 올바르게 재배치 — **Bear는 위험을 감지하고(E),
Bull은 매력을 요구한다(C·D는 Bull이 거른다)**. 캘리브레이션 전에는 Bear가
모든 걸 걸렀고(구분 없이), Bull의 인정 조항은 작동할 기회가 없었다.

### 정합성
- 재현성 원장: prompt_hash가 system 포함 sha256이라 변경 전후가 자동 분리 — 일치율 오염 없음.
- -40/BUY_THRESHOLD: 유지. Bear가 판별력을 가진 지금 만장일치 반대는 진짜 악재에서만
  나온다. 표본 누적 후 재검토.
- 표본 속도: ~12건/일 → 승격 기준 200표본까지 약 17영업일.


## 2026-08-03 — feat(quant): awesome-systematic-trading 검토 반영 4건 (P0×2 + P1×2)

[paperswithbacktest/awesome-systematic-trading](https://github.com/paperswithbacktest/awesome-systematic-trading)
(논문 전략 40여 개 재현 백테스트 + 라이브러리 카탈로그)를 전체 분석한 뒤,
우리 제약(KR 왕복 수수료 0.227%, 공매도 불가, 소자본, T+1)에 맞는 것만 선별 도입.
Qlib/FinRL 등 RL 계열·pairs trading·프레임워크 교체는 명시적으로 기각
(LLM 진화 시스템과의 해석가능성 충돌, 수수료 구조 불일치).

### P0-1. quantstats 성과 tear sheet (`src/analytics/quantstats_report.py` 신설)
- equity 스냅샷 108일치의 `daily_pnl_pct` → 일별 수익률 시계열 → quantstats HTML 리포트
  (자산 곡선 차분이 아니므로 입출금/외부계좌 편입에 왜곡 없음)
- 벤치마크: FDR KOSPI 1차 → pykrx 폴백 (KRX 인증 이슈는 2026-04-21 stock_master 사고와 동일 패턴)
- API: `GET /api/performance/quantstats`(6h 캐시, `?refresh=1`), `/status` — `kr_api.py`
- `/performance` 페이지에 📊 퀀트 리포트 버튼. `requirements.txt`에 quantstats 추가
- 검증: 생성 4.4초/393KB, 엔드포인트 200 확인

### P0-2. BacktestGate walk-forward 확장 (`backtest_gate.py`)
- 기간 3→6개월, 판정에 **구간승 기준 추가**: 2개월×3구간 중 2구간 이상 baseline 초과
- 단일 구간 총수익률만 보면 특정 레짐에 우연히 맞은 파라미터가 통과하는
  과적합 취약점(López de Prado) 보완. 한 구간에 개선이 몰린 후보는 총수익률이 좋아도 기각
- `_run_once`가 `_equity_curve`를 회수(외부 직렬화에는 미노출), `_segment_returns` 정적 메서드
- 타임아웃 600→900s. 실측: 6개월×2회 49초(캐시 워밍). 합성 곡선 단위 검증 +
  실데이터 e2e 검증(sepa.min_score 60→65 후보를 -28.5%p로 정상 기각) 완료

### P1-1. 캘린더 시즈널리티 오버레이 (`src/utils/calendar_seasonality.py` 신설)
- 독립 전략이 아닌 **사이징 배율 오버레이**: turn-of-month(월말 2+월초 3거래일) KR/US ×1.10,
  US 옵션만기주(3째 금요일 주) ×1.05. 부스트만 있고 차단/축소 없음
- KR: `engine.py` 사이징에 적용 후 max_position_pct 상한 재적용.
  US: `us_scheduler.py` ATR×체제×캘린더 통합(최대 ×1.155로 35% 상한 이내), 미 동부 날짜 기준
- KR 옵션만기주는 미적용 (논문 근거가 미국 시장). `CALENDAR_SEASONALITY=0`으로 비활성화
- 검증: 2026-07/08 경계일 11케이스 판정 정확

### P1-2. 코어홀딩 저변동성 감점 (`core_screener.py`)
- 일별 수익률 60일 σ(`ret_vol_60d`) 신설 — 기존 volatility_20d는 가격 수준 분산이라
  추세 종목에서 과대평가됨
- 감점 전용(σ≥4% -10 / ≥3% -6 / ≥2.5% -3): min_score 보정을 흔들지 않고 급등락형만 배제.
  2026-06-04 코어홀딩 -263k 사고(급락형 종목 stale 사각지대)의 진입 단계 예방
- reasons에 `고변동성 σx.x%(-N)` 태그 → 대시보드에서 감점 사유 확인 가능

수정: `src/analytics/quantstats_report.py`(신설), `src/utils/calendar_seasonality.py`(신설),
`src/core/evolution/backtest_gate.py`, `src/core/engine.py`, `src/schedulers/us_scheduler.py`,
`src/signals/screener/core_screener.py`, `src/dashboard/kr_api.py`,
`src/dashboard/templates/performance.html`, `requirements.txt`
문서: `docs/evolution/evolution-system.md`, `docs/strategies/kr-strategies.md`,
`docs/strategies/us-strategies.md`, `docs/operations/runbook.md`

## 2026-08-03 — fix(agents): 팀 심의가 매수로 이어지지 않던 원인 2건 (입력 기아)

심의 13건(8/2~8/3)이 전부 `stance=hold`, 신규 매수 0건이었다.
**예산 문제가 아니다** — `TraderAgent.propose()`에는 현금이 인자로도 들어가지 않는다.
분석가 입력이 굶고 있었다.

### 인과 사슬
지표 미전달 + 펀더멘탈 no-op → 유효 근거가 news 하나뿐 → Bull이 "근거 없음"을
이유로 REJECT(13/13) → 만장일치 반대 → `debate_adj = -40` → 총점 < `BUY_THRESHOLD(20)`.
분석가 점수만 보면 **3/9건이 이미 매수 기준을 넘겼는데** 토론 -40에 전부 뒤집혔다.

### 버그 1 — 지표가 한 번도 전달되지 않았다 (`kr_scheduler.py`)
```python
"indicators": (getattr(s, "indicators", None)
               or getattr(s, "metadata", {}).get("indicators")
               if hasattr(s, "metadata") else None),
```
파이썬은 `(A or B) if hasattr(...) else None`으로 묶는다. `SwingCandidate`에는
`metadata`가 없어서 **indicators를 갖고 있어도 항상 None**이었다.
기술적 분석가가 "지표 없음"으로 전량 실패(8/3 9건 중 9건). 보유 종목은 아예
`"indicators": None` 하드코딩.
→ `객체 → metadata → 스크리너 지표 캐시` 순으로 추출. 보유 종목도 캐시에서 채운다.

### 버그 2 — 펀더멘탈 분석가가 필드명을 전부 잘못 읽었다 (`analysts.py`)
`passed`/`reason`/`supply_demand`/`short_selling` → 실제는
`approved`/`block_reason`/`supply_demand_result`/`short_selling_result`.
하위 필드도 숫자가 아니라 bool(`foreign_net_buying`, `in_top50`)이었다.
`getattr(obj, name, default)`가 조용히 삼켜서 **항상 score=0**을 내면서
`confidence=0.7`을 주장했다 — 실패보다 나쁘다. 가중평균에서 뉴스 점수를
절반으로 희석시키기만 하는 유령 근거였다(13건 전부 score 0).
→ 실제 스키마로 재작성. 순매도 감점은 제거(bool은 "순매수 아님"까지만 말해준다).

### 실측 — 토론은 근거에 반응한다
동일 종목·프롬프트로 근거만 바꿔 실제 LLM 토론을 돌렸다.

| 근거 | Bull | Bear | 판정 | 보정 |
|---|---|---|---|---|
| fund 0 + news만 (수정 전 재현) | 반대 | 반대 | 만장일치 반대 | **-40** |
| fund 40 + tech 35 + news 61 | 지지 | 반대 | 의견 분열 | **-10** |

30점 스윙. 8/3 9건 재계산 시 1건이 HOLD→BUY(삼성E&A, total -10 → 22).

### ⚠️ 남은 구조적 제약 (설계 판단 필요, 이번에 건드리지 않음)
`-40`은 현실적 분석가 점수 범위(0~40)보다 크다. **만장일치 반대가 나오면 어떤
근거로도 매수가 불가능**하다. 의도된 fail-closed지만 캘리브레이션 재검토가 필요하다.
입력을 고쳤으니 며칠 관측 후 판단할 것 — 표본 없이 임계값부터 낮추면
검증 계층을 무력화하는 것과 같다.

### 검증
- 삼항 우선순위 버그 실증: `SwingCandidate`에 지표가 있어도 구버전은 `None` 반환.
- 수정 후 기술적 분석가가 `score=35 conf=0.7` 정상 산출.
- 펀더멘탈 5개 시나리오(기본/동반순매수/한쪽/공매도상위/검증실패) 점수 정상 분기.
- 봇 재시작 에러 0건.


## 2026-08-03 — feat(office): 에이전트 활동 타임라인 + 캐릭터 상세 + SSE 실시간 브리지

"가상 오피스에 인터랙션이 없고 실제 에이전트가 뭘 하는지 안 보인다"는 지적에서 출발.
점검해 보니 **배관은 멀쩡하고 내용이 비어 있었다** — API·SSE·정적 자산 모두 200이고
폴링도 정상인데, 보여주는 게 엔진 컴포넌트 8개의 한 줄 상태뿐이었다.

정작 트레이딩 팀은 매일 심의를 돌리고 있었다. 2026-08-03 기준 9건, 파일 101KB.
분석가 3인 채점 → Bull·Bear 토론(서로 다른 모델) → 트레이더 제안 → PM 승인.
이게 오피스에 한 글자도 안 나왔다.

### (B) 에이전트 활동 타임라인 — `/office` 하단
- `GET /api/team/verdict/detail` 신설 — Bull/Bear 발언 원문, 분석가 리포트(점수·확신도·데이터 신선도).
  목록 API는 `detail_available: true`를 반환하면서 정작 상세 경로가 없었다.
- `GET /api/team/verdicts`에 `date` 파라미터 + `dates`(보관 일자) 추가.
  `TradingTeam.load_date()` / `available_dates()` 신설, `load_today`는 그 위에 얹었다.
- 카드 클릭 시 상세 조회(지연 로딩). 토론 라운드별 발언을 Bull(초록)/Bear(빨강)로 구분,
  사용 모델(`gpt-5-mini` / `gemini-3.1-flash-lite`)까지 표시.
- 분석가 리포트는 `age_minutes > 60`이면 노란색 — 오래된 데이터로 매긴 점수임을 드러낸다.

### (A) 캐릭터 상세 — 클릭해도 빈칸이던 문제
상위 앱은 `label`(번들 116회 참조)·`reasonCode`·`hint`·`activeFile`·`skill`을 상세 패널에 쓰는데
우리는 `hint`만 가끔 채우고 나머지는 항상 null이었다. `office.html`은 "캐릭터를 클릭하면
상세 상태가 열립니다"라고 안내하고 있었다.
- `_team_snapshot()` 신설 — 심의 파일을 **mtime 기준 캐시**(SSE가 2초마다 파생을 호출한다).
- `qa` 검증관 = 팀 심의 결과(종목·토론 요약·합의 여부), `res` 전문가팀 = 분석가 채점 내역.
- `arch`·`dev`·`designer`에 `skill` 부여. `workflow`에 심의 건수, `mood`에 "매수 0건" 반영.

### (SSE) 실시간 브리지
iframe 번들에 `protocol==='https:' && hostname!=='localhost' → EventSource 비활성` 가드가 있어
공개 도메인에서는 앱이 **SSE를 스스로 끄고** 폴링만 쓰고 있었다(유휴 시 백오프까지).
서버 `/api/office/status/stream`은 멀쩡히 동작 중이었다.
- 부모 창이 SSE를 받아 iframe으로 `postMessage`. 앱 리스너가 `source===window.parent`를
  허용하므로 번들 수정 없이 우회된다. 재연결은 지수 백오프(2초→최대 30초).
- 상단에 라이브 배지 추가. iframe 자체 폴링은 백업으로 유지된다.

### ⚠️ 표기 정정 — 승인 ≠ 매수
심의 9건이 전부 `approved=true`인데 `stance`는 전부 `hold`였다.
`approved`는 "PM이 제안을 승인했다"는 뜻이고, `hold` 제안을 승인해도 **신규 매수는 0건**이다.
초안은 이를 "승인 9 · 거부 0"으로 표시했는데 매수 9건으로 오독된다.
→ 목록·캐릭터 모두 **stance 기준**으로 바꿨다: `매수 0 · 보류 9 · 거부 0`.
`mood`의 "일은 했는데 성과 없음" 판정도 `approved==0`에서 `buys==0`으로 정정했다.

### 검증
- `/office`·`/api/office/status/stream`·`/api/team/verdicts`·`/api/team/verdict/detail`
  전부 공개 도메인에서 200, SSE 실수신 확인.
- 상세 API가 토론 4턴(라운드별 Bull/Bear + 모델명)과 분석가 리포트 3건을 정상 반환.
- HTML 구조 검증(미닫힌 태그 0), 봇 재시작 에러 0건.


## 2026-08-03 — fix(batch): 대기 시그널 이월을 스킵 사유별로 분리 (13:50 윈도우 복구)

직전 커밋(`b27790b`)에서 돌파 대기분만 이월했는데, 남은 문제를 사유별로 마저 정리했다.

### 문제
`execute_pending_signals()`가 끝에서 pending을 비우므로, 09:01 이후 윈도우
(12:30 낮스캔·13:50 자본활용률 체크)는 사실상 빈 파일을 읽었다.
특히 **13:50은 스캔 없이 execute만 호출**해서 "자본 미활용 시 추가 진입"이
SEPA/RSI2 등 모든 전략에서 한 번도 동작한 적이 없다.

전량 이월은 위험하다 — 갭다운으로 걸러진 신호를 오후에 재시도하면 게이트의
취지를 정면으로 뒤집는다. 그래서 사유별로 나눴다.

### 분류 기준 — "상태가 바뀌면 통과할 수 있는가"
`batch_analyzer.CARRY_REASONS`에 정의. 루프 내 스킵 지점 **12곳 전부** 분류했다.

| 이월 (7) | 폐기 (4 + 상한 초과) |
|---|---|
| `quote_fail` 조회 실패 — 일시적 API 장애 | 만료 — 시간이 지나면 더 확실히 만료 |
| `breakout_wait` 돌파 미달 — 장중 돌파 가능 | SEPA 14:30+ — 이후 시각은 더 늦어질 뿐 |
| `above_band` 밴드 상단 초과 — 눌리면 복귀 | 갭다운 — 악재 의심 종목은 당일 손대지 않는다 |
| `gap_up_score` 갭업 점수 미달 — 갭 축소 시 통과 | 이미 보유 중 — 재시도 대상 아님 |
| `intraday_gate` 급락 게이트 — 해제 시 통과 | |
| `strategy_limit` 전략 한도 — 청산 시 슬롯 | |
| `error` 실행 예외 — 일시적 | |

- **갭다운을 폐기로 둔 이유**: 오후에 반등해도 진입 근거(전일 종가 기준 스캔)는
  이미 무효다. 게이트가 막으려던 상황을 몇 시간 뒤에 통과시키는 셈이 된다.
- **SEPA 14:30+를 폐기로 둔 이유**: 오늘 안엔 절대 통과할 수 없고, 익일로 넘기면
  하루 지난 근거로 진입하게 된다.

### 안전장치
- `PendingSignal.retry_count` 신설 + `MAX_CARRY_RETRIES = 8`.
  재시작이 반복되면 윈도우 수와 무관하게 재시도가 누적될 수 있어 둔 폭주 방지선이다.
  만료(익영업일 15:30)가 1차 방어선, 이건 그 안에서의 2차선.
- 중복 매수는 기존 "이미 보유 중" 체크가 막는다 — 이월분도 매번 통과해야 한다.
- 이월 건수를 사유별로 로깅: `[배치분석] 다음 윈도우 이월 N개 (사유 n, ...)`.

### 부수 효과 (개선)
재시작 시 `pending_signals.json`이 비어 있으면 스케줄러가 "장 시작 후 스캔 미실행"으로
오판해 풀백 스캔 + 재실행을 돌렸다(`last_execute_date`가 메모리라 재시작 후 초기화됨).
이월로 파일이 유지되면서 이 중복 실행 경로가 줄어든다.

### 검증
- 스킵 지점 정적 감사: 12곳 전부 분류 확인 (이월 8 / 폐기 4).
- 정책 단위 테스트: 11개 사유 판정, 상한 8회 차단, 만료 건 제외,
  `retry_count` 비영값 JSON 왕복 보존.
- 구버전 JSON 하위호환 (`retry_count` setdefault).
- 봇 재시작 에러 0건, 종료 `Stopping`→`Stopped` 동일 초.

### 남은 제약 (코드 문제 아님)
13:50 추가 진입은 `현금 비중 > 25%` 조건이 붙어 있다. 현재 현금은 자본의 0.5%라
이월분이 있어도 이 경로는 실행되지 않는다 — 자금 확보 시점부터 실효가 생긴다.


## 2026-08-03 — fix(discovery): VCP 돌파 대기 시그널이 첫 윈도우에서 삭제되던 문제

`19c8e1a`(선행 발굴 라인) 리뷰 중 발견. 조건부 돌파 진입의 핵심 전제가 깨져 있었다.

### 문제
`execute_pending_signals()`는 끝에서 `self._pending = []` → `_save_json()`으로
대기 시그널 파일을 통째로 비운다. 돌파 트리거 미달로 스킵한 시그널도 여기 휩쓸려
삭제됐다. 코드 주석과 문서는 "pending을 유지한 채 스킵 → 09:01/12:30/13:50 재확인"이라고
적혀 있었지만, 실제로는 **첫 실행 윈도우에서 사라졌다**.

결과적으로 VCP 시그널은 "09:01 시점에 이미 트리거를 넘긴 경우"에만 진입할 수 있었다.
아직 안 움직인 종목을 돌파 순간에 잡겠다는 라인의 목적 자체가 무력화된 상태였다.
(12:30 낮 스캔이 `run_morning_scan()`으로 재탐지하므로 완전히 죽지는 않았으나,
같은 날 장중 돌파를 잡는 경로는 없었다)

### 수정 (`src/core/batch_analyzer.py`)
- `carry_over` 목록 추가 — 돌파 대기로 스킵한 시그널만 담는다.
- 종료 시 `self._pending = [s for s in carry_over if not s.is_expired()]`.
  전량 삭제 → 대기분만 이월. 만료(익영업일 15:30) 건은 제외.
- 재진입 중복은 기존 "이미 보유 중" 체크(`batch_analyzer.py:1086`)가 막으므로
  이월로 인한 중복 매수는 발생하지 않는다.

### 검증
- 하위호환: 구버전 JSON(entry_mode 키 없음) 로드 시 `close`/`0.0` 기본값 정상 주입.
- 이월 필터: 만료 1건 + 유효 1건 중 유효분만 유지. `to_dict()`↔`from_dict()` round-trip 일치.
- 봇 재시작 후 에러 0건. 종료도 `Stopping`→`Stopped` 동일 초 완료(SIGKILL 없음)로
  `19c8e1a`의 종료 경로 수정이 실측 확인됐다.

### 부수 정리
- `config/evolved_overrides.yml` `_meta`에 `strategy_allocation.vcp_breakout` 이력 추가.
  배분 합계가 90% → 100%가 됐는데(다른 전략 축소 없이 미배분분에 얹음) 근거가
  기록되지 않아 추적이 끊겼다. 이 저장소는 배분 변경 근거를 `_meta`에 남기는 규약이다.
- `docs/strategies/kr-strategies.md` — 진입 밴드 상단(`max_entry_price`)과 이월 규칙 명시.

### 함께 확인된 것 (이번 변경 아님, 별도 판단 필요)
13:50 자본활용률 체크는 스캔 없이 `execute_pending_signals()`만 호출하는데,
직전 윈도우가 pending을 비우므로 **모든 전략에서 사실상 빈 파일을 읽는다**.
이번 수정으로 돌파 대기분은 13:50에 재확인되지만, SEPA/RSI2의 "자본 미활용 시 추가 진입"은
여전히 동작하지 않는다. 스킵 사유(갭다운·점수미달 등)별로 재시도 가치가 달라
일괄 이월은 위험하므로 별도 결정이 필요하다.


## 2026-08-03 — feat(discovery): 선행 발굴 라인 신설 + 재시작 SIGKILL/코어 과다스캔 수정

### 배경 — 발굴 파이프라인 진단
"상승 가능성이 높은 종목을 미리 찾는다"는 관점에서 발굴 경로를 전수 점검한 결과,
**후보 유입 통로가 전부 후행적**이었다.

- 실시간 스크리닝(5분)의 1차 소스 8개가 전부 "이미 움직인 종목의 순위표"
  (거래량 급증 / 등락률 / 신고가 / 기관·외국인 순매수 / 네이버 순위 / 뉴스 호재)
- 배치 스캔 유니버스 218개 = 시총 상위 200 + 등락률 50 + 수급 100 → **시총 200위 밖
  종목은 이미 급등해야만 유니버스에 진입** (전체 2,800종목의 7.8%)
- VCP(변동성 수축 = 돌파 직전 패턴)는 이미 구현돼 잘 작동했으나 **오버레이 가점으로만**
  사용 — `candidate.score < 50`이면 스킵, RSI2/SEPA 필터 통과자에만 적용.
  VCP는 정의상 "아직 안 움직이고 거래량도 줄어드는" 패턴이라 그 두 필터를 통과하기 어렵다.
  실측 7/31: **VCP 12종목 탐지 → 오버레이 반영 2종목**

### P1. VCP 독립 발굴 라인 (`vcp_breakout`)
- `swing_screener._filter_vcp_breakout()` 신설 — RSI2/SEPA와 무관하게 독립 후보 생성
- VCP 탐지를 복합 점수 계산 **이전**으로 이동 (VCP 후보도 수급/재무 점수를 받도록)
- 진입은 종가가 아니라 **20일 고점 +0.5% 돌파 확인가**, 손절은 수축 저점(최대 -8%)
- `StrategyType.VCP_BREAKOUT` 신설, allocation 10% (default.yml + evolved_overrides.yml 양쪽)
- 하락장에서는 SEPA와 동일하게 차단, 주의장에서는 동일하게 기준 상향
- 오버레이 Layer 3(VCP 가점)은 `strategy == "vcp_breakout"`이면 스킵 (이중 가점 방지)
- **임계값을 실측으로 조정**: 초안 min_score=70은 실측 분포상 하루 1~5개뿐이라
  라인이 사실상 죽는다. MA정배열·거래량감소·수축2회를 별도로 강제하므로 이중 게이트 →
  **60으로 하향**. 우선주(종목코드 끝자리≠0)는 유동성 문제로 배제
- 탈락 사유 집계 로그 추가 — "왜 후보가 안 나오는가"를 추측이 아니라 수치로 확인

### P2. 유니버스 선행 통로
- 시총 상위 조회 **200 → 350** (설정 `kr.universe_leading.top_cap_limit`)
- 수급 누적(SupplyTrendDetector) / 직전 스캔 VCP 수축 종목을 유니버스에 편입
  (순위 API에 안 잡히는 수축 종목이 다음날 유니버스에서 탈락하는 것 방지)
- **실측: 유니버스 218 → 358종목 (+64%), SEPA 후보 9~11 → 18개**

### P3. 조건부 돌파 진입
- `PendingSignal.entry_mode / breakout_trigger` 추가 (JSON 하위호환 setdefault)
- `entry_mode="breakout"`이면 현재가가 트리거를 넘을 때만 발행, 아니면 pending 유지
- 프리장 검증(갭다운 취소 / 반등 취소 / R-R 재계산)은 entry=종가를 전제하므로
  돌파 모드에서는 건너뜀 — 안 그러면 트리거 대비 항상 음수라 전량 오취소된다
- ⚠️ 리뷰 정정: "08:20~19:30 11시간 공백"은 오독이었다. 낮 추가 스캔(12:30) +
  13:50 자본활용률 재실행이 **이미 존재**한다. 장중 스캔 신설은 하지 않았다.

### 운영 이슈 2건
1. **재시작마다 90초 SIGKILL** (최소 30시간 이상 모든 재시작이 이 경로)
   - 원인: `stop()`이 `running=False`만 세팅하고 태스크를 취소하지 않아
     `gather()`가 스케줄러 `sleep(5~10분)`과 대시보드 SSE 루프를 끝까지 기다림
   - 수정: `asyncio.Event` 기반 즉시 취소 경로 + 15초 타임아웃 후 강제 진행
   - **초기화 중 SIGTERM도 무시되던 구멍**을 별도로 발견해 수정
     (`_stop_event`를 `initialize()` 앞에서 생성 + 초기화 후 재확인)
   - 대시보드 `AppRunner(shutdown_timeout=5)` — 기본 60초라 마지막까지 남던 태스크
   - **실측: 90초 SIGKILL → 15초 정상 종료**
2. **CoreScreener 과다 실행** — 7/31 15:00~15:09에 유니버스 146종목 풀스캔 7회
   - 원인: fill_window가 4~10분 구간인데 루프는 1분 주기. 매수가 성사돼야만
     `last_fill_date`가 설정되므로 현금 부족 시 윈도우 내내 매분 재스캔
   - 수정: 윈도우 단위 시도 키(`last_attempt_key`)로 리밸런싱/빈슬롯 각 1회 제한

### 검증
- 실서비스 스캔 2회 실행: 유니버스 358종목, VCP 탐지 18종목,
  독립 라인 계측 `채택 0개 (중복 7, 점수미달 7, MA비정배열 2, 거래량미감소 1, 우선주 1)`
- 과거 5일 VCP 캐시로 라인 검증: **평균 4.0개/일 통과** (중복 제외 전),
  실측 중복률 39% 적용 시 실질 2~3개/일. 8/3 채택 0개는 하락장(KOSPI 20일 −13.8%)
  + 상위 종목이 SEPA와 겹친 결과이며 라인 자체는 정상 동작
- 재시작 3회 모두 SIGKILL 없이 15초 내 종료

### Codex 독립 리뷰 반영 (P1 3건 / P2 4건)
| 지적 | 판정 | 조치 |
|---|---|---|
| caution 레짐 VCP 기준이 SEPA config에 종속 (`sepa.min_score+10`) | **실제** | VCP 자체 `min_signal_score+10`로 분리. SEPA 튜닝이 VCP 문턱을 바꾸던 결합 제거 |
| `atr_14`로 통과시키고 metadata엔 `atr_pct=0` 저장 | **실제** | `calculate_all()`은 `atr_pct`를 만들지 않음을 확인 → **모든 VCP 시그널이 atr_pct=0**이었다. 확인한 값을 그대로 전달 (둘 다 % 단위라 환산 불필요 — Codex의 "가격으로 나누라"는 제안은 오히려 틀림) |
| VCP 점수 60~69 구간에서 복합점수 **음수 보정** | **실제** | 기준점 70→60으로 재정렬 + 하한 클램프 (편입 직후 감점되던 모순) |
| `detect_all()` 동기 호출로 이벤트 루프 블로킹 | **실제** | `asyncio.to_thread` 래핑 (유니버스 확대로 블로킹 시간도 함께 증가) |
| 리밸런싱/빈슬롯이 `last_attempt_key` 공유 | **실제** | 키 분리 — 뒤 경로가 앞 경로의 시도 상태를 지우던 문제 |
| 취소한 `gather_task`/`stop_waiter` 미회수 | **실제** | 자식 태스크 정리가 끝난 `finally` 시점에 회수. 초안대로 취소 직후 3초만 기다리면 그때는 아직 `done`이 아니라 회수가 안 된다(실측으로 확인) |
| default.yml ↔ evolved_overrides.yml **경로 불일치** | **오탐** | `config.py:240`이 `risk_config → kr.risk`로 매핑. 기존 키 전부 같은 규약 |

**검증 중 추가로 드러난 2건 (종료 경로가 정상화되며 노출)**
- `DashboardServer.stop()` 이중 호출 → `Site is not registered in runner` 오류.
  `run()`의 finally와 봇 `shutdown()`이 각각 호출하는데, 전에는 SIGKILL이 먼저 나서
  두 번째 호출이 도달하지 않아 가려져 있었다. → `_stopped` 멱등 가드
- `_GatheringFuture exception was never retrieved` → 위 회수 로직으로 해소
- **최종 실측: 종료 10초, SIGKILL·경고 0건**

**미반영 2건 (의도된 트레이드오프, 문서화로 대체)**
1. VCP 후보를 RSI2/SEPA와 **사전** 중복 제거 → 그 종목이 최종 시그널을 못 만들어도 VCP로 복구되지 않음.
   지적 자체는 정확하나, 제안된 "exclude 제거 후 최종 단계 dedupe"는 이 시점 후보 점수가 전부 0이라
   먼저 들어온 쪽이 이기게 되어 실효가 없다. 동일 종목 이중 노출 차단이라는 기존 설계를 유지한다.
   실측 중복률 39%(18개 중 7개)를 계측 로그로 상시 관측 가능.
2. 돌파 모드가 프리장 검증(갭다운 취소/반등 취소/R-R)을 우회 → 검증들이 전부 `entry=전일종가`를
   전제하므로 트리거 대비로는 항상 음수가 나와 전량 오취소된다. 트리거 통과 자체가 상승 확인이고
   `max_entry_price` 상한이 추격을 막으므로 현 단계에서는 수용.

> ⚠️ 현재 현금 78,238원(자본의 0.5%) — 발굴 개선분은 자금 확보 시점부터 실효가 발생한다.
> 대표 결정에 따라 펩트론 포지션은 그대로 유지.

## 2026-08-03 — refactor(principles): CORE PRINCIPLES 전면 개정(21→28) + 검증값 미적용 버그 3건

### 배경
전략이 단타 → 스윙/코어 중심으로 바뀌었는데 `CORE_PRINCIPLES`는 예전 문구 그대로였다.
Codex 교차 검증에 붙여 보니 **원칙이 코드보다 강하게 쓰여 있었다** —
"예외 없이", "전면 중단", "SEPA만", "필수"가 전부 실제 동작과 달랐다.
원칙은 LLM 크로스검증 컨텍스트로 주입되므로, 틀린 원칙은 그대로 잘못된 판단 근거가 된다.

### 원칙 개정 (`src/core/evolution/trading_principles.py`, 21 → 28개)
- 헤더에 **"원칙은 불변이 아니다"** 명시 — 전략이 바뀌면 원칙도 바뀐다. 다만 근거 없이는 못 바꾼다.
- 폐지 전략(`rsi2_reversal`·`theme_chasing`) 전제 원칙 삭제, 코어홀딩·팀 합의·진화 게이트 원칙 신설.
- 코드와 어긋난 서술 정정 (Codex 3라운드 지적 반영):
  | 원칙 | 개정 전 (사실과 다름) | 개정 후 |
  |------|------|------|
  | CORE-001 | "예외 없이 즉시 청산" | `exit_exempt`·`KILL_SWITCH_ALL` 2가지 예외 명시 |
  | CORE-002 | "-5% 초과 시 전면 중단" | **시장 회복세일 때만** 방어 전략 허용, -12.5% 이하 전면 중단 |
  | CORE-004 | universal | KR 전용 (ATR 1.2배 하드차단은 KR 자동진입 경로만, US엔 없음) |
  | CORE-029 | "동시 보유 8종목" | 비코어 8슬롯(잔여비율 가중) **+ 코어 3개 별도** — 실제 보유는 11개 초과 가능 |
- `scope="universal"` 8건을 `KR`로 분리 — US는 일일손실 -3%·10종목·35%·현금 10%로 값이 다르다.

### 실효 버그 3건 (원칙 검증 과정에서 발견)
1. **SEPA 1차 익절이 검증값을 한 번도 안 썼다** (`scripts/run_trader.py`)
   2026-08-02 백테스트로 `+10%/10%`를 검증하고 `default.yml`을 고쳤는데,
   `_strategy_exit_params["sepa_trend"]`에 `+5%/20%` 하드코딩이 남아 config를 덮어쓰고 있었다.
   → 해당 키 제거, `register_position(first_exit_pct=None)` → config 상속.
   (현재 보유는 `087010`(exit_exempt) 1건뿐이라 기존 포지션 영향 없음)
2. **SEPA 14:30 차단이 15:00~15:29를 통과시켰다** (`src/core/batch_analyzer.py`)
   `now.hour >= 14 and now.minute >= 30` → `minute=0 < 30`이라 15:00대가 거짓.
   → `(now.hour, now.minute) >= (14, 30)` 튜플 비교로 수정. 오버나이트 갭 리스크 노출 구멍이었다.
3. **폐지 전략이 손실한도 예외 목록에 잔류** (`src/risk/manager.py`)
   `defensive_strategies`에 `rsi2_reversal`이 남아 있었다. 지금은 `enabled=false`라 무해하지만,
   재활성화 시 -5% 초과 구간에서 조용히 허용된다. → 제거.

### 대시보드 — 원칙 하드코딩 제거 (`/principles`)
`principles.html`이 원칙 21개를 HTML에 직접 박아 두어 개정 전 상태로 굳어 있었다
(삭제된 CORE-005/009/017/019 표시, 신규 22~032 누락).
- `GET /api/principles` 신설 (`src/dashboard/kr_api.py`) — `CORE_PRINCIPLES` 원문 반환.
- 카드 목록을 JS 동적 렌더링으로 교체. 카테고리 미등록 값도 자동 그룹 생성, 모든 문자열 `esc()` 이스케이프.
- **코드가 단일 소스** — 앞으로 원칙을 고치면 대시보드가 자동으로 따라온다.
- 청산/게이트 설명 섹션의 낡은 수치 2건도 정정 (`-1.5%` → `-0.5%`(코어 -2.0%), "전면 중단" → "방어 전략만 허용").

### 문서
- `docs/risk/risk-and-exit.md`
  - 분할익절 변경 시 고칠 곳 **4 → 5곳** (`run_trader.py`의 `_strategy_exit_params` 추가).
    이번 버그가 정확히 이 5번째를 놓쳐서 생겼다.
  - 레짐별 파라미터 표를 실제 `REGIME_EXIT_PARAMS` 값으로 갱신 (표가 2026-08-02 이전 값이었다).

### 검증
- Codex 3라운드 교차 검증 (지적 → 수정 → 재검증). 마지막 라운드 지적 4건 전부 코드로 확인 후 반영.
- Codex 오판 1건 확인: `daily_exit_cooldown_threshold`가 "기본 0이라 비활성"이라는 지적은 사실과 다름
  (`RiskConfig` dataclass 기본값은 3, 분기 정상 동작).
- 봇 재시작 후 에러 0건, `/api/principles` 28개 정상 서빙.


## 2026-08-03 — feat(dashboard): 가상 오피스 — 엔진 상태 픽셀아트 시각화 (`/office`)

### 배경
숫자 카드로는 "지금 봇이 뭘 하고 있는지"가 한눈에 안 들어온다.
[KbWen/agent-virtual-office](https://github.com/KbWen/agent-virtual-office)(MIT)를 붙여
엔진 상태를 **8명 캐릭터가 있는 사무실 한 장면**으로 보여준다.

### 통합 방식 — 정적 번들 + Python 브릿지
원본은 React 19 + Vite + 자체 node 서버(5174)다. 그대로 띄우면 프로세스와 포트가 늘어나
**단일 포트 8080 원칙**이 깨진다. 그래서 한 번 빌드해 정적 파일로 넣고 상태 API만
aiohttp로 다시 구현했다 — **런타임에 node 프로세스 없음**.

| 파일 | 내용 |
|---|---|
| `src/dashboard/office_api.py` (신규) | 상태 브릿지. 엔진 파생 + POST + Claude Code 훅 파일 3소스 병합 |
| `src/dashboard/templates/office.html` (신규) | `/office` 페이지 (nav + 역할 범례 + iframe) |
| `src/dashboard/static/office/` (신규) | 빌드 산출물 (upstream `685e767`, v1.6.4) |
| `tools/office/` (신규) | `build.sh` + `ko.json` — 재현 가능한 재빌드 |
| `src/dashboard/server.py` | `/office` 라우트 + `/api/office/*` 등록, `/static/office/assets/` 캐시 예외 |
| 템플릿 8개 + `mobile-v2.js` | 네비게이션에 "오피스" 추가 (상단 pill + 모바일 하단 nav) |

### 역할 매핑 (운영 8명 + 전문가 7명 → 캐릭터 8명)
| role | 캐릭터 | 소스 |
|---|---|---|
| pm | 총괄 | `bot.running` / `engine.paused` / 세션 |
| arch | 체제분석 | `market_regime` (+LLM 코멘트) |
| dev | 스크리너 | `screener._last_screened`, `signals_generated` |
| qa | 검증관 | `cross_validator.get_stats()` |
| ops | 집행관 | `_pending_orders`, `orders_submitted/filled`, KILL_SWITCH_ALL |
| res | 전문가팀 | `expert_orchestrator.agents`, `theme_detector._themes` |
| gate | 리스크 | `can_trade`, 킬스위치, 일일손실 |
| designer | 진화 | `evolution/advice_*.json` → 없으면 `evolution_state.json` |

mood는 킬스위치/일일손실/체결대기 건수로 결정 (`frustrated`/`stuck`/`intense`/`smooth`/`idle`).

### upstream 패치 3가지
1. **API 경로** `/api/status` → `/api/office/status` — 대시보드가 이미 `/api/status`를
   KR 봇 상태로 쓰고 있어 **이름이 정면 충돌**했다.
2. **한국어 로케일** `ko.json` 추가 + 기본 언어 `ko`. 캐릭터 이름도 트레이딩 역할명으로 교체
   (개발자→스크리너, 문지기→리스크, 디자이너→진화 …). 말풍선도 트레이딩 맥락으로 다시 씀.
3. `index.html` 제목/설명 한국어화 + 외부 OG 이미지 메타 제거.

### 외부 신호 (Claude Code / CI)
- `POST /api/office/status` — shorthand `{"dev":"working"}` / full format 모두 허용.
  알 수 없는 role 폐기, 잘못된 status는 `idle`로 강등, 16KB 초과 413.
  `OFFICE_STATUS_TOKEN` 설정 시 Bearer 인증 요구.
- upstream 훅은 HTTP가 아니라 **파일**(`~/.claude/office-status*.json`)로 상태를 남긴다.
  브릿지가 이 디렉토리를 스캔해 자동 병합하므로 훅 등록만 하면 된다.
- 외부 신호는 **역할별 5분 TTL** — 만료되면 엔진 파생 상태로 자동 복귀.

### 성능
- GET은 ETag/304 지원 + 내용 무변경 시 `_seq` 유지 → 프론트 중복 렌더 없음
- 파생 계산 3초 캐시, 훅 스캔 2초 캐시, 진화 파일 확인 60초 캐시
- SSE는 변경 시에만 전송 (그 외 keepalive)
- `/static/office/assets/`는 파일명 해시가 있어 `immutable` 캐시 허용 (500KB 재다운로드 방지)

### 검증
- 단독 aiohttp 서버로 9개 시나리오 통과 (GET/304/shorthand/full/병합/잘못된 JSON/lang/SSE/훅 파일)
- 실서비스 재시작 후 `/office` 200, 8역할 실데이터 표시 (`장 마감 · 체제 중립`, 포지션 1/3, 전문가 8명, 테마 4건)
- headless Chromium 렌더 확인 — 콘솔 오류 0건

> 상세: `docs/operations/virtual-office.md`

## 2026-08-03 — fix: LLM 재현성 50% → 100% (seed 고정 + 빈 응답 제거)

원장을 만들자마자 드러난 재현성 미달을 해결했다. 원인이 **둘**이었고 각각 다르게 잡았다.

### 원인 ① 빈 응답 — `success=True`인데 `content=''`
- 직접 API 호출 20회에서는 재현되지 않았고, `LLMManager` 경유 6회 중 1회 발생.
  예산·rate limit 문제가 아니라 gpt-5 계열이 200에 빈 본문을 주는 경우였다.
- **해결**: `reasoning_effort` `"low"` → `"minimal"` + 빈 응답 시 1회 재시도.
  실측(각 5회): low는 reasoning 128~320토큰/총 236~437, minimal은 reasoning 0/총 112~156.
  품질 차이 없이 토큰만 줄었고 빈 응답 여지도 함께 줄었다.
- `LLMManager.complete_with(retry_on_empty=)` 신설 (기본 0 = 기존 동작 유지).

### 원인 ② 샘플링 비결정성 — 같은 입력에 판정이 뒤집힘
빈 응답을 없앤 뒤에도 6회 중 1회 판정이 반전됐다. 이게 본질적 원인이었다.

| 설정 | 동일 입력 6회 |
|---|---|
| OpenAI seed 없음 | REJECT×5, **APPROVE×1** → 불일치 |
| **OpenAI seed 고정** | **REJECT×6 → 일치** |
| Gemini temp 0.3 / 0.0 | 둘 다 일치 |

- **gpt-5 계열은 temperature 커스텀이 막혀 있어 seed 말고는 고정 수단이 없다.**
- **해결**: `OpenAIClient`에 `seed` 전달 추가, `DEBATE_SEED` 고정.
  Gemini는 `temperature=0.0`. `adversarial_validator`에도 동일 적용.

### 결과
**일치율 50% → 100%** (동일 입력 6회, 응답 문구까지 동일). 빈 응답 0/12건.
승격 기준(80%) 충족.

## 2026-08-03 — feat(agents): LLM 재현성 원장 — 판단 근거 append-only 기록

### 배경
토론 결과는 Trader 점수를 `+20/-40` 바꾸고 매수 여부를 가른다. 그런데 LLM은 같은 입력에도
다른 답을 낼 수 있다. 기록이 없으면 **"그날 왜 샀나"를 사후에 설명할 수 없고**,
모델 교체 전후를 같은 전략으로 비교할 수 없으며, shadow 성과가 실력인지 운인지 구분할 수 없다.
승격 기준의 "재현성 80%"는 측정 수단 없이는 확인 자체가 불가능했다.

### 신규 `src/agents/reproducibility.py`
`~/.cache/ai_trader/llm_ledger/llm_YYYYMMDD.jsonl` (append-only)

| 필드 | 용도 |
|---|---|
| `prompt` / `response` | **전문** — 요약본으로는 재실행 비교가 불가능하다 |
| `prompt_hash` | 재실행 시 입력 동일성 확인 (문자열 전체 비교 없이) |
| `model` / `provider` | **실제 응답 모델** — 폴백으로 요청과 달라질 수 있다 |
| `params` | max_tokens / reasoning_effort / weight |
| `input_snapshot_hash` | 분석가 보고서 스냅샷. **나이는 제외** — 매번 변해 비교 불가 |
| `verdict` / `latency_ms` | 판정·지연 |

- `LLMLedger.agreement_rate()` — `prompt_hash`로 묶어 동일 입력의 판정 일치율 계산.
  입력이 다르면 판정이 달라도 비재현이 아니므로 제외한다.
- `LLMLedger.model_usage()` — 모델 교체·폴백 발생 추적.

### 연결
- `researchers._ask`가 문자열 대신 메타(dict) 반환 — 기존엔 **실제 응답 모델 ID와 지연이 유실**됐다.
- `DebateTurn`에 `model`/`provider` 추가 → verdict 파일만 봐도 어느 모델이 판단했는지 안다.
- turns 직렬화 200자 → 500자 (전문은 원장에 있으므로 요약은 이 정도면 충분).
- `TradingTeam.get_stats()`에 `reproducibility` / `model_usage` 노출.

### ⚠️ 첫 실측에서 바로 문제가 드러났다
동일 입력 3회 실행 → **판정 일치율 50%** (승격 기준 80% 미달).
Bull(`gpt-5-mini`)이 3회 중 1회 무응답이었다 — 추론 모델의 빈 응답 문제가
재현성까지 갉아먹고 있다. **승격 전 해결 필요.**
(최종 consensus는 3회 모두 동일했으나, 개별 호출 수준에서는 재현되지 않았다.)

## 2026-08-03 — feat(agents): 포트폴리오 배분기 — 동시 승인분 섹터 집중 차단

### 배경
적대적 리뷰의 최우선 지적: "이름과 달리 PortfolioManager가 포트폴리오를 관리하지 않는다."
종목별 심의는 서로를 보지 못해 **후보 5개가 전부 같은 섹터여도 각각 승인**될 수 있었다.

`cross_validator`의 섹터 규칙(규칙4)은 **이미 보유 중인** 포지션만 센다.
같은 배치에서 동시에 승인된 후보들끼리는 서로를 볼 방법이 없었다.

### 신규 `src/agents/allocator.py`
심의 이후·주문 이전에 후보 **전체를 한 번에** 배분한다.
- 확신도 높은 순으로 배정하며 배정할 때마다 누적 상태(섹터·현금·슬롯)를 즉시 갱신 (원자적 적용)
- **한도는 새로 만들지 않는다** — `RiskConfig`(max_positions, max_position_pct,
  min_cash_reserve_pct, max_daily_new_buys, max_positions_per_sector, min_position_value)와
  `RiskManager._get_available_cash()`를 그대로 재사용. 숫자를 두 곳에 두면 언젠가 어긋난다.
- **allocator 거부는 오버라이드 불가** — 포트폴리오 제약은 계좌 생존에 직결된다.

### 연결
- `kr_scheduler._run_team_deliberation_once`: 심의 → **배분** → 알림 순서로 연결
- 후보에 `sector`를 실어 보내 `AnalystReport.metrics["sector"]`로 전달
- 텔레그램 요약에 배분 승인/거부 내역 추가

### 검증 (실측)
| 시나리오 | 결과 |
|---|---|
| 반도체 4 + 바이오 1 동시 승인 | 반도체 **2건만 승인**, 2건 "동시 승인분 포함" 차단 |
| 가용현금 300만 / 후보 4 | 예산 300만 초과 안 함, 나머지 최소금액 미달 거부 |
| 일일 매수 5/5 소진 | 전건 거부 |
| 포지션 7/8 보유 | 잔여 슬롯 1건만 승인 |

## 2026-08-03 — fix(P0): 적대적 리뷰 반영 — 신선도 fail-closed, PM 오버라이드 차단, 섹터 전달

Codex(gpt-5.6-sol) 적대적 **설계** 리뷰 결과를 검증해 타당한 6건을 반영했다.
판정은 "재설계 필요"였고, 지적 중 2건은 실측으로 사실 확인됐다.

### 🐞 P0-① 신선도 감쇠가 가중평균에서 상쇄됨 (수학적 결함)
- `aggregate_score`가 `Σ(score×w)/Σ(w)`라서 **모든 근거가 함께 낡으면 감쇠가 완전히 상쇄**된다.
  실측: 전부 신선 `+62` / 전부 4시간 전 `+62` — **동일**.
- ⚠️ 2026-08-02에 "+10 → +72로 개선"이라 보고한 검증은 *한쪽만* 낡은 경우였다.
  전부 낡은 경우를 테스트하지 않은 **검증 부실**이었다.
- **수정**: 소스별 hard TTL(`technical` 45분 / `fundamental`·`news` 180분) 초과분은 집계에서 제외.
  `evidence_quality()` 신설 — 유효 소스 ≥2개 + 감쇠 후 가중치 합 ≥0.5 미달이면 **신규 매수 금지**.
- 검증: 전부 4시간 전 → 종합 `+0`, 근거 `0.00`, `BUY→HOLD` 차단 확인.

### 🐞 P0-② 섹터 집중 규칙이 통째로 스킵됨
- `cross_validator` 규칙4는 `metadata.get("sector")`로만 동작하는데,
  팀의 `gate_checker`가 **`metadata={}`를 넘겨** 섹터 검사가 한 번도 실행되지 않았다.
  → 같은 섹터 5개가 전부 승인될 수 있는 상태였다.
- **수정**: 후보에 `sector`·`indicators`를 실어 `cv.validate(metadata=...)`로 전달.

### P0-③ PM 게이트 오버라이드 기본 비활성화
- 게이트 유효성은 실측됐지만(차단 신호 20영업일 -3.7%~-13.2%),
  **"LLM 만장일치가 그 성과를 역전한다"는 증거는 없다.**
- 게다가 만장일치면 `conviction 0.9`가 자동 부여돼 `MIN_CONVICTION=0.75`가 자동 충족 —
  조건이 걸림돌 역할을 못 했다.
- **수정**: `allow_pm_override: false`. shadow 표본으로 우위가 확인된 뒤 게이트별로 열 것.

### P0-④ 시장 컨텍스트 전부 만료 시 fail-open
- 만료 의견을 거르는 것까지는 맞았으나, **전부 만료면 빈 문자열을 반환**해
  "중립 시장"과 구분되지 않은 채 심의가 계속됐다.
- **수정**: "시장 컨텍스트 없음 — 보수적으로 판단하라"를 프롬프트에 명시 + WARNING 로그.

### P1-① 토론 실패가 매수에 fail-open
- 토론을 "안전 검증 계층"으로 도입했는데 장애 시 사이징만 줄여 통과시켰다 — 도입 목적과 모순.
- **수정**: 토론 실패 시 신규 BUY 차단(HOLD). 보유 종목 판단에는 계속 사용.

### P1-② Codex 라우팅 allowlist를 코드로 강제
- config 주석("실시간 태스크 금지")은 강제력이 없었다.
- **수정**: `CODEX_ALLOWED_TASKS` frozenset — config에 실수로 `quick_*`/`theme_*`를 넣어도 차단.

### 문서 — shadow → 실주문 승격 기준 명문화
"며칠 관측"을 표본 200건·레짐별 30건·비용 반영 P&L·재현성 80%·장애 시 주문 0건 등
7개 항목으로 대체 (`docs/agents/trading-team.md`). 자동 승격 금지.

### 반영하지 않은 지적 (판단)
- **포트폴리오 allocator 신설** — 타당하나 shadow 단계에 과도한 재설계.
  섹터 metadata 전달로 기존 게이트를 살리는 것을 우선하고, 실주문 연결 전 필수 과제로 문서화.
- **LLM을 주문 점수에서 완전 제거** — 설계 근간 변경이라 보류.
  대신 오버라이드 차단 + fail-closed 전환으로 위험을 축소했다.

## 2026-08-02 — feat(llm): 로컬 Codex CLI 라우팅 — 배치 작업 API 과금 → 구독 한도

### 배경
`codex exec`는 비대화형 실행을 지원하고 **최종 응답만 stdout으로** 내보낸다(진행 로그는 stderr).
배치성 LLM 작업을 여기로 넘기면 **API 과금이 ChatGPT 구독 한도로 대체**된다.

### 신규 — `src/utils/codex_client.py`
- `codex exec` async subprocess 래퍼. `--ephemeral`(세션 미저장) 사용.
- **`--output-schema` 지원** — JSON Schema로 응답 구조를 강제해 파싱 실패가 없다.
  기존 정규식 파싱(`_extract_json`)보다 훨씬 안정적.
- `input_data`를 stdin으로 넘기면 `<stdin>` 블록으로 첨부된다 (거래 내역 JSON 등 큰 입력용).

### 라우팅 — `LLMManager.complete()`
- config `llm.codex.tasks`에 있는 LLMTask만 Codex로 보낸다. **실패하면 기존 API로 자동 폴백.**
- 기본 대상: `trade_review`(거래 복기), `strategy_analysis`(전략 진화·주간 복기)
- Codex는 별도 프로세스라 API 토큰 통계·예산에 잡히지 않는다 (과금이 없으므로 daily_usage 미반영).

### ⚠️ 실시간 경로에는 쓰지 않는다
프로세스 기동 2~3초 + 응답 7~12초. 실측 비교:
| 경로 | 처리 | 지연 |
|---|---|---|
| `strategy_analysis` (배치) | **Codex** | 8.9초 |
| `quick_classify` (실시간) | API (Gemini) | 0.7초 |

Bull/Bear 토론은 종목당 2~4회 × 다수 종목이라 Codex로 돌리면 수 분이 걸린다.
`cross_validator` 2차 검증도 10초 타임아웃이라 프로세스 기동만으로 초과한다.
→ `codex.tasks`에 `quick_*` / `theme_*` 계열을 넣지 말 것.

### 실행 시 함정 (실측으로 확인, 코드 주석에도 기록)
1. **stdin을 안 써도 반드시 닫아야 한다.** 안 그러면 `Reading additional input from stdin...`
   상태로 무한 대기한다 (배경 실행에서 25분 소실).
2. stdout/stderr를 합치지 말 것 — 합치면 진행 로그가 응답에 섞인다.
3. 파일 접근이 필요하면 이 호스트에선 `danger-full-access` 필요(bubblewrap 차단).
   데이터를 stdin으로 넘기는 순수 분석이면 `read-only`로 충분.

### 검증
- 배치 → `model=codex:gpt-5.6-sol`, 실시간 → API 라우팅 분리 확인
- 폴백 2경로 실측: ① codex 실행파일 없음 ② 잘못된 모델 → 둘 다 API로 정상 폴백
- 구조화 출력: 거래 복기 JSON 스키마 준수 확인 (7~12초)

## 2026-08-02 — fix: Codex(gpt-5.6-sol) 2차 리뷰 P1 4건/P2 2건 + LLM heavy 모델 교체

### Codex gpt-5.6-sol 2차 리뷰 반영
1차 리뷰(gpt-5.4)가 놓친 것을 상위 모델이 추가로 잡았다. 기존 수정 6건은 "모두 올바름" 확인.
- **P1 `portfolio_manager.py`**: `gate_passed=True`인데 `blocked_gates`가 차 있으면
  하드게이트(킬스위치·손실한도)가 걸린 건도 "통과" 플래그 하나로 **fail-open**됐다.
  앞서 고친 P0(빈 목록 fail-closed)의 정반대 방향 구멍. → 모순은 차단으로 해석.
- **P1 `portfolio_manager.py`**: `confidence`/`conviction`이 `NaN`이면 `<` 비교가 전부 거짓이라
  **오버라이드 조건을 그냥 통과**하고 `size_multiplier`까지 NaN으로 번졌다.
  → `math.isfinite()` + `[0,1]` 범위 검증, 위반 시 거부. 사이징도 폴백값 적용.
- **P1 `team.py`**: 저장 락이 인스턴스별이라 `TradingTeam`이 둘이면 같은 파일·같은 `.tmp`를
  동시에 read-modify-write. → 모듈 전역 `_SAVE_LOCK` + 임시파일명에 pid/uuid 부여.
- **P1 `types.py`**: `data_as_of`가 tz-aware면 naive `datetime.now()`와 뺄셈에서 **TypeError**.
  `age_minutes`는 점수·프롬프트·저장 전 경로에서 쓰여 심의가 통째로 실패한다.
  → tzinfo 정규화, 계산 불가 시 `inf`(=가중치 0)로 처리.
- **P2 `analysts.py`**: `price_provider` 조회 시 `data_as_of`가 조회 시각이라 일봉이 실시간으로
  표시됐다. → DataFrame 마지막 관측 시각 사용.
- **P2 `team.py`**: `load_today(limit=0)`이 `rows[-0:]`라 전체 반환. → `limit<=0`이면 `[]`.

### LLM heavy 모델 교체 (gpt-5.4 → gpt-5.6-sol)
- `gpt-5.4`는 deprecated 예정(Codex `models_cache`의 `upgrade.model = gpt-5.6-terra`).
- `gpt-5.6-sol`(priority 1)이 **일반 OpenAI API에서도 동작**함을 실측 확인
  (sol / terra / 5.5 / 5.4 / 5-mini 전부 200 응답).
- **heavy만 교체**: `MARKET_ANALYSIS` / `TRADE_REVIEW` / `STRATEGY_ANALYSIS` —
  거래 복기·전략 진화 등 하루 수십 회 수준의 중요 판단.
- **light는 유지**(`gpt-5-mini`): Bull/Bear 토론이 종목당 2~4회씩 부르므로 호출량이 크다.
  frontier 모델로 올리면 비용이 급증한다. (2026-12-10 deprecation 예정이라 후속 검토 필요)
- ※ `.claude/agents/` 13개는 Claude Code 서브에이전트라 **Claude 모델만 지원**(haiku/sonnet/opus).
  gpt로 대체 불가하며, Codex가 필요하면 `codex exec` 위임 경로를 쓴다.

## 2026-08-02 — feat/fix: 에이전트 데이터 신선도 관리 + Codex 독립 리뷰 반영

### 데이터 신선도 (모든 에이전트가 최신 정보로 판단하도록)
- 🐞 **`orchestrator.snapshot()`이 만료 의견을 그대로 반환**했다.
  `ExpertAgent.cached()`는 주석부터 "만료 무관"이고 `ExpertOpinion.is_valid`는 아무도 쓰지 않았다.
  전문가 의견 TTL이 6~24시간이라, 팀이 토론 프롬프트에 **어제 만들어진 시장 진단**을
  "현재 상황"으로 주입할 수 있었다.
- **수정 4가지**:
  1. `team._market_context()` — `is_valid` 필터, 전부 만료면 컨텍스트 생략, 의견 나이 표기
  2. `AnalystReport.data_as_of` + `freshness_decayed_confidence()` — 반감기 60분 지수 감쇠
     (실측: 30분 0.57배 / 2시간 0.25배 / 6시간 0.016배)
  3. 토론 프롬프트에 근거별 나이 명시 + "오래된 근거는 할인해서 판단하라" 지시
  4. `bot._last_screened_at` → `indicators_as_of` 전달로 지표 나이 실측
- 효과 실측: 신선한 지표(+80)와 4시간 전 수급(-60) 조합에서 종합 점수 **+10 → +72**
  (과거가 현재를 상쇄하던 문제 해소)
- **축적형은 감쇠하지 않는다** — `trade_memory`(L1→L2→L3), `trade_wiki` 교훈은
  오래됐다고 가치가 떨어지지 않는다. 감쇠는 시황성 데이터(시세·수급·뉴스·레짐)에만 적용.

### Codex(gpt-5.4) 독립 리뷰 반영 — P0 1 / P1 1 / P2 2
1차 리뷰에서 놓친 결함을 외부 모델이 지적했다. 전부 실제 결함이라 수정했다.
- **P0 `portfolio_manager.py`**: `gate_passed=False`인데 `blocked_gates`가 비어 있으면
  하드게이트 가드와 화이트리스트 검사가 **빈 컬렉션이라 전부 통과**해 PM이 게이트를 그냥 뚫었다.
  → 근거 불명 차단은 fail-closed 거부. 정상 soft 게이트 오버라이드는 유지됨을 실측 확인.
- **P1 `team.py`**: `_deliberate()`가 `Exception`만 잡아 `asyncio.CancelledError`(타임아웃)에서
  verdict 저장이 통째로 건너뛰어졌다. 이미 증가한 공유 통계만 남아 오염.
  → `CancelledError` 별도 처리 + `asyncio.shield`로 저장 후 재전파.
- **P2 `researchers.py`**: 라운드 간 `bull_text`/`bear_text` 재사용으로, 응답이 빈 라운드에
  직전 텍스트가 그대로 turn에 기록돼 토론 이력이 오염됐다.
  → 라운드 원시 응답 분리, 무응답은 `(응답 없음)`/`stance=None`으로 명시.
- **P2 `team.py`**: `deliberate_many()` 예외 폴백만 `decision=None`이라 결과 shape이 깨졌다.
  → 기본 `PMDecision(approved=False, HOLD)` 채움.

### Codex CLI 연동 (참고)
실패 원인이 매번 달랐다 — 재현 시 참고:
1. bubblewrap 샌드박스 차단(`bwrap: loopback: Failed RTM_NEWADDR`) → `-s danger-full-access`
2. **stdin 대기로 25분 무한 정지** → `< /dev/null` 필수 (백그라운드 실행 시).
   출력도 `| tail`로 파이프하면 버퍼링돼 통째로 사라지므로 파일로 직접 리다이렉트할 것.
3. 모델 거부 `The 'gpt-5.6-sol' model is not supported when using Codex with a ChatGPT account`
   → **일시적 현상이었다.** 로그인 직후 계정 정보 전파 전에만 발생하며,
   이후 재확인 시 `gpt-5.6-sol / terra / luna / gpt-5.5` 모두 정상 동작.
   영구 제약으로 단정하지 말고 재시도할 것.
- ⚠️ 플러그인 companion(`codex-companion.mjs`)은 `sandbox: "read-only"`를 하드코딩하므로
  `~/.codex/config.toml`의 `sandbox_mode`가 무시된다. 이 환경에서는 `codex exec` 직접 호출이 필요.
- ⚠️ `gpt-5.4`는 deprecated 예정(`upgrade.model = gpt-5.6-terra`). 기본·권장은 **`gpt-5.6-sol`**(priority 1).

## 2026-08-02 — fix(P0): 전문가 8명 LLM 미연결 + Core Holding 비중 확대

### 🐞 P0 — 전문가 시스템의 LLM이 통째로 죽어 있었다
- `run_trader.py:601`이 `ExpertOrchestrator(llm_manager=getattr(self, "llm_manager", None))`인데
  **`self.llm_manager`는 어디에서도 설정되지 않는다** → 항상 `None` 주입.
- 영향 (전문가 4명의 LLM 기능 무력화):
  | 대상 | 증상 |
  |---|---|
  | `news_curator._classify_batch` | `llm_manager is None` → **즉시 return. 종목별 뉴스 sentiment 분류 자체가 미동작** |
  | `macro_economist._llm_synthesize` | 빈 문자열 반환 (거시 종합 없음) |
  | `global_micro_expert` / `kr_economy_expert` | 동일 패턴 |
- `get_symbol_sentiment()`가 항상 중립(score 0 / tags 없음)을 반환해 왔고,
  이를 소비하는 신규 `NewsAnalyst`까지 연쇄로 무력화될 상황이었다.
- **수정**: `get_llm_manager()` 싱글턴 직접 주입 + 초기화 로그에 `LLM=연결` 표기.
- 검증: 수정 전 `llm_manager=None`(분류 불가) → 수정 후 `LLMManager`(분류 가능) 실측 대조.

### Core Holding 비중 확대 (25% → 30%, sepa 25% → 20%)
- ⚠️ 직전 보고에서 "백테스트상 50%까지 적정, 현재 25%"라고 한 것은 **단위 혼동**이었다.
  백테스트의 50%는 3전략 내 상대비중, 25%는 전체 배분 중 비중이다.
  RSI2 폐지분을 양분하면서 상대비중은 이미 50:50에 도달해 있었다.
- RSI2 제거 상태에서 재측정 (6개월 × 60·120종목):
  | sepa : core | 6개월/120 | 6개월/60 | 손익비 |
  |---|---|---|---|
  | 60 : 40 | -3.74% | +1.82% | 2.51 / 2.39 |
  | 50 : 50 (기존) | +26.16% | +16.21% | 2.38 / 2.70 |
  | **40 : 60 (채택)** | **+26.84%** | **+32.40%** | 2.40 / **2.98** |
  | 30 : 70 | +31.96% | +31.85% | 2.46 / 2.98 |
- 30:70이 백테스트상 최고였으나 core가 전체 35%가 되어 단일 전략 집중도가
  gap_and_go(35%)와 동급이 된다 → **40:60(core 30%)으로 억제**. 두 시나리오 모두 개선.
- ※ `gap_and_go`(35%)와 `strategic_swing`(5%)은 백테스트가 지원하지 않아 이 비교에서 빠져 있다.
  전체 배분 최적화가 아니라 **3전략 내 상대비중 최적화**임에 유의.

## 2026-08-02 — feat: 종목 단위 에이전트 팀 (`src/agents/`) — TradingAgents 구조 도입

### 배경
- 참고: TauricResearch/TradingAgents, revfactory/harness.
- 기존 `src/experts/` 8명은 **시장·섹터 레벨**만 판단했다. 개별 종목에 대해
  "사도 되는가 / 계속 보유할 것인가"를 팀으로 논의하는 계층이 없었다.
- ⚠️ **층 구분**: harness는 `.claude/agents/`(개발 보조, 13개 기존)를 만드는 플러그인이고,
  이번 작업은 `src/`(런타임 매매)다. 서로 다른 층이다. harness의 6개 아키텍처 패턴은
  런타임 팀 설계 언어로 차용했다.

### 구조 (harness 패턴 매핑)
```
[전문가 풀]   도메인 전문가 8명 → 시장 컨텍스트     (기존 재사용)
[팬아웃/팬인] Analyst 3인 병렬                      LLM 미사용
[생성-검증]   Bull(OpenAI)/Bear(Gemini) 2R 토론     LLM 2~4회
[감독자]      Trader 종합 → 방향+사이징             결정론적
[생성-검증]   Risk게이트(11규칙) → PM 승인
```
- Analyst/Trader에 LLM을 쓰지 않은 이유: 지표 계산·점수 합산은 답이 정해진 일이라
  확률적 모델을 넣으면 백테스트·감사·회귀 테스트가 불가능해진다.

### 신규 파일
- `src/agents/{types,analysts,researchers,trader,portfolio_manager,team}.py`
- `docs/agents/trading-team.md`
- 대시보드: `/api/team/verdicts`, `/api/team/stats` + `/engine` 페이지 카드

### 코드리뷰 후 수정 (P0 3건 / P1 5건)
- **P0-① 단독 응답 편향** (`researchers.py`): Bull이 죽고 Bear만 남으면 그 판정이 합의가 됐다.
  Bear는 "실패 시나리오를 찾아라"는 역할이라 구조적 반대 편향이고, 반대로 Bear의 ACCEPT는
  "감수 가능한 리스크"지 매수 추천이 아니다. **실측에서 실제 오판 발생**.
  → 단독 반대는 존중(consensus=False), 단독 긍정은 합의 미승격(None, conf 0.3)으로 비대칭 처리.
- **P0-② exit_exempt SELL 누출** (`portfolio_manager.py`): stance!=BUY면 게이트를 아예 보지 않아
  자동매도 금지 종목(087010 펩트론)의 SELL 제안이 승인됐다. → PM이 무효화하도록 수정.
- **P0-③ 결과 저장 race** (`team.py`): 동시 심의 3건이 같은 파일을 read-modify-write.
  → asyncio.Lock + 임시파일 원자적 교체.
- **P1**: NewsAnalyst가 실제 스키마(`{score,tags,items}`)와 어긋나 headlines를 찾던 문제 +
  스케일 추정(`abs<=1이면 ×100`)이 0.5를 50으로 증폭시키는 위험 제거 /
  데이터 소스 없는 분석가가 confidence 0.3으로 집계에 참여하던 문제(→0.0) /
  토론 실패 시 사이징 무제한(→×0.7 상한) / 대시보드 CSS 클래스 오타(`badge-g`→`badge-green`)

### 검증
- 편측 응답 4케이스, exit_exempt SELL 차단, 토론실패 사이징 축소 각각 실측 확인.
- 통합 실행: 2라운드 토론 정상(입장 변경 감지), one_sided=0, 회귀 없음.

### 스케줄러 연결 (같은 날 추가)
- **`kr_scheduler.run_team_deliberation`** — 장중 10:30 / 14:00 2회.
  매수 후보 상위 5(`_last_screened`) + 보유 종목 전체 재평가.
- **`run_trader.py`**: `self.trading_team` 초기화 (stock_validator·dart_checker·
  expert_orchestrator·exit_manager 주입). 초기화 실패해도 매매 무영향
  (팀이 None이면 스케줄러 태스크를 띄우지 않음).
- **config `kr.trading_team`** 신설 (enabled / debate_rounds / allow_pm_override / max_concurrent).
- 🐞 연결 중 발견: `self.llm_manager`는 어디에서도 설정되지 않아
  `getattr(self, "llm_manager", None)`이 항상 None을 반환한다 →
  팀에는 `get_llm_manager()` 싱글턴을 직접 주입. (expert_orchestrator도 같은 패턴을
  쓰고 있어 별도 점검 필요.)

> ⚠️ **shadow 단계 — 주문을 내지 않는다.** 심의·기록·알림만 수행.
> 팀이 신설이라 실전 데이터가 없고 첫 통합 테스트에서 P0 3건이 나왔으므로,
> 며칠 관측 후 주문 경로 연결을 판단한다.

### 재시작 반영
이 재시작으로 앞선 커밋의 미적용분(RSI2 폐지, 배분 sepa 25/core 25, 1차 익절 +10%/10%)이
함께 적용됐다. 실측 확인: `rsi2_reversal alloc=0.0% enabled=False`,
`first_exit_pct=10.0 / ratio=0.1`, `exit_exempt 087010 복원`, 스케줄러 태스크 21개.

## 2026-08-02 — fix/tune: 백테스트-실제 엔진 동기화 + 1차 익절 재조정 (백테스트 검증 기반)

### 배경 — 백테스트가 실제 엔진과 달랐다
- 실제 `exit_manager.py`는 **ATR 연동 트레일링**을 쓴다:
  `effective_ts = min( max(config_ts, ATR% × 1.2), 6.0 )`, 코어홀딩 제외.
- 그런데 `scripts/backtest_strategies.py`는 **고정 `trailing_stop_pct=3.0`**만 썼고,
  기본값도 evolved 적용 전 값(익절비중 0.30, min/max stop 3.5/6.0)이었다.
- 결과적으로 백테스트가 실제보다 **비관적**으로 나왔다 (SEPA 3개월 -12.02% vs 실제 설정 -7.19%).
- ⚠️ 이 상태로 두면 신설 백테스트 게이트가 **실제와 다른 조건으로 A/B 판정**을 내렸을 것이다.

### 수정 1) 백테스트 엔진 동기화
- **scripts/backtest_strategies.py**:
  - `BacktestConfig`에 `enable_atr_linked_trailing`(True) / `atr_link_multiplier`(1.2) / `atr_link_cap_pct`(6.0) 추가
  - `BTExitManager.check_exit`에 실제 엔진과 동일한 ATR 연동 트레일링 공식 이식 (코어홀딩 제외)
  - `BTPosition.atr_pct` 필드 추가 — `atr_stop_pct`는 min/max로 clamp돼 원본 ATR 역산이 불가능하므로 별도 보관

### 수정 2) 1차 익절 재조정 (5.0%/20% → 10.0%/10%)
- **근거**: SEPA 81건 청산 사유 분해 결과
  | 청산 사유 | 비중 | 평균 손익 | 평균 보유 |
  |---|---|---|---|
  | 손절 | 39.5% | **-6.21%** | 2.2일 |
  | 1차 익절 (+5%) | 27.2% | +5.20% | **1.9일** |
  | 트레일링 | 25.9% | +6.26% | 2.6일 |
  | 2차 익절 (+15%) | 3.7% | **+23.21%** | 3.3일 |
  - 익절 +5.20% < 손절 -6.21% — "이익은 짧게, 손실은 길게"의 정반대 구조.
  - 2차 익절까지 살아남은 소수가 +23.21% → 추세를 태우면 크게 번다.
- **검증** (3·6개월 × 60·120종목 4개 시나리오): **손익비 전부 개선**
  1.53→1.85 / 1.67→2.01 / 1.98→2.21 / 2.03→2.70, 수익률 3/4 개선
  (6개월·60종목은 -3.87% → **+1.68%**로 전환). 3개월·120종목만 -19.67%→-21.41%로 악화.
- **트레일링 완화는 백테스트가 반증** — TS 5.5/6.0%로 넓히니 오히려 악화(-7.28%, -7.56%).
  현재 4.5% + cap 6.0%가 적정이므로 **변경하지 않음**.
- **적용 지점** (4곳 모두 동기화 필요):
  - `config/default.yml` `kr.exit_manager`: 5.0/0.30 → 10.0/0.10
  - `config/evolved_overrides.yml` `exit_manager`: 5.0/0.2 → 10.0/0.1 (+ `_meta` 근거 기록)
  - `src/strategies/exit_manager.py` `ExitConfig` 기본값: 10.0/0.10
  - `src/strategies/exit_manager.py` `REGIME_EXIT_PARAMS` (레짐별 오버라이드가 config를 덮으므로 필수):
    trending_bull 5→10, neutral 5→10, ranging 4→8, turning_point 4→8,
    trending_bear 3→**5** (약세장은 조기 실현이 합리적이라 보수적 상향)

### 검증
- `AppConfig.load()`로 최종 병합값 확인: `first_exit_pct=10.0`, `first_exit_ratio=0.1`,
  `trailing_stop_pct=4.5`, `max_stop_pct=8.0` (evolved_overrides 정상 오버라이드).
- 재시작 후 ExitManager 정상 초기화, `exit_exempt`(087010 펩트론) 보호 유지 확인.

### 한계 (정직한 기록)
- 검증 구간이 2026-05~08 하락장에 집중돼 있다. 상승장 표본이 없다.
- 3개월·120종목 시나리오에서는 악화했다 — 만능 개선이 아니다.
- 손익비가 4/4에서 개선된 점을 근거로 채택했으나, **상승장 도래 시 재검증 필요**.

## 2026-08-02 — feat: 킬스위치·감사원장·백테스트 게이트·게이트성능·적대적 검증 (외부 레포 벤치마킹)

### 배경
- 참고: HKUDS/Vibe-Trading, gameworkerkim/vibe-investing, maj34/financial-agent.
- 비교 결과 에이전트 구성·메모리(trade_memory 3-Layer, Trade Wiki)·게이트(11규칙)는 우리가 더 깊었고,
  실제 갭은 ①백테스트가 진화와 단절 ②킬스위치/감사원장 부재 ③확증 편향(단일 LLM `승인하시겠습니까?`)
  ④차단 신호의 사후 검증 부재 4가지였다.

### 1) 킬스위치 + 감사 원장 (Vibe-Trading의 mandate/kill switch 차용)
- **src/risk/kill_switch.py** (신규): 파일 존재만으로 주문 차단. 봇 재시작 불필요.
  - `~/.cache/ai_trader/KILL_SWITCH` → 신규 매수만 차단(청산 허용)
  - `~/.cache/ai_trader/KILL_SWITCH_ALL` → 전면 동결
  - 시장별 접미사(`_KR`/`_US`) 지원, 파일 내용은 차단 사유로 로그에 표시, TTL 2초 캐시
  - 파일시스템 오류 시 차단하지 않음(오탐으로 거래 정지 방지)
- **src/utils/audit_log.py** (신규): append-only JSONL 월별 감사 원장
  (`~/.cache/ai_trader/audit/audit_YYYYMM.jsonl`). submit/accept/reject/blocked 기록.
- **kis_kr.py `submit_order` / kis_us.py `_submit_order`**: 모든 주문이 통과하는 유일한 두 지점에 가드 삽입.
- ⚠️ `KILL_SWITCH_ALL`은 손절까지 막으므로 하락 노출이 무한정 열린다. 포지션 정리 후 사용할 것.

### 2) 백테스트 ↔ 진화 연결 (Research Autopilot 차용)
- 기존 문제: `scripts/backtest_strategies.py`(1,580줄)가 존재하지만 `strategy_evolver`가 호출하지 않아
  **실거래 5영업일/10건 표본만으로 파라미터를 변경** → 노이즈 학습 위험.
- **src/core/evolution/backtest_gate.py** (신규): 변경 적용 전 동일 기간 A/B 백테스트
  (baseline=현재값, candidate=제안값, 3개월/60종목 ≈ 42초).
  - 통과 조건: 총수익률 개선 + MDD 악화 ≤1%p + 후보 거래 ≥10건
  - 실패 시 **fail-closed**(변경 보류) — 검증 못 한 변경을 적용하느니 하루 미룬다
  - `PARAM_MAP`으로 진화 파라미터 → BacktestConfig 필드 매핑, 미지원 파라미터는 게이트 생략
  - `EVOLUTION_BACKTEST_GATE=0` 으로 비활성화 가능
- **scripts/backtest_strategies.py**: `ResultAnalyzer.metrics()` 분리, `run(save_results=)` 결과 dict 반환
  (기존 CLI 동작은 그대로).
- **strategy_evolver.py**: `evolve()`에 게이트 연결, `EvolutionState`에 `total_rejected_by_backtest`,
  `consecutive_gate_errors` 추가. 게이트 장애 3회 연속 시 텔레그램 알림(진화가 조용히 멈추는 것 방지).

### 3) 게이트 성능 분석 (Shadow Account 차용)
- **src/analytics/gate_performance.py** (신규): 차단된 신호의 20영업일 사후 수익률을 게이트별 집계.
  post-exit review가 "판 뒤 올랐나"를 본다면 이건 "막은 게 옳았나"를 본다.
- 토요일 09:30 `run_post_exit_review_scheduler`에 연결, 텔레그램 리포트.
- **초회 실측 (최근 90일, 5,259건)**:
  | 게이트 | 건수 | 차단 신호 20일 평균 | 회피성공 |
  |---|---|---|---|
  | G1_regime | 4,600 | **-9.28%** | 62% |
  | G3_risk | 209 | -11.24% | 65% |
  | G2_cross | 57 | **-13.21%** | 74% |
  | G5_cash | 64 | -10.95% | 70% |
  | G_intraday | 62 | -0.83% | 48% |
  | **PASSED(대조군)** | **120** | **-2.88%** | 57% |
  - 모든 게이트가 유효(차단 신호가 크게 하락). 특히 G2_cross가 가장 정확.
  - ⚠️ **다만 통과 신호조차 20영업일 평균 -2.88%** — 게이트가 아니라 진입 전략의 문제.
  - G_intraday만 효과 불명확(-0.83%, 기회손실 39%) → 재검토 대상.

### 4) 적대적 검증 + 멀티 LLM 합의 (vibe-investing 차용)
- 기존 `llm_second_check` 프롬프트는 "이 매수 시그널을 승인하시겠습니까?" — 승인을 기본값으로 깔아 확증 편향 유발.
- **src/core/adversarial_validator.py** (신규): 역할 분리 + 교차 검증
  - Bull(OpenAI): 지지 근거 평가 / Bear(Gemini): **실패 시나리오 제시 강제**("문제 없음" 답변 금지)
  - 만장일치 승인/거부 → confidence 1.0, 불일치 → 0.5(기본은 통과, `STRICT_ON_DISAGREEMENT`로 전환 가능)
  - fail-open: LLM 장애 시 기존 단일 LLM 경로로 폴백
- **src/utils/llm.py**: `complete_with(provider=...)` 추가(폴백 없이 provider 고정 — 모델 간 비교용),
  gpt-5 계열 `reasoning_effort` 파라미터 지원.
- ⚠️ **중요 발견**: gpt-5-mini는 추론 모델이라 `max_tokens`가 작으면 추론 토큰만 쓰고
  **본문이 빈 문자열로 온다(success=True, content='')**. 실측 120·400 모두 빈 응답 →
  `reasoning_effort="low"` + `MAX_TOKENS=400`으로 해결. 이 값을 줄이지 말 것.
- **cross_validator.py**: 적대검증 연결. LLM을 2회 호출하므로 일일 한도 카운터도 2회 증가시킴.

### 5) 리뷰에서 발견한 기존 버그 수정 (P0)
- **trade_memory.py:578 / trading_principles.py:329**: `from ..utils.llm import LLMTask` →
  `src/core/evolution/`에서 `..utils`는 `src.core.utils`(존재하지 않음) → **호출 시마다 ImportError**.
  `...utils`로 수정. 영향: 주간 L1→L2→L3 메모리 압축(`_llm_structured_review`)과
  주간 원칙 인사이트(`_generate_llm_weekly_insight`)가 그동안 동작하지 않았음.

### 검증
- 전체 py_compile 통과, 봇 재시작 후 `[진화] 백테스트 게이트 활성` 확인, DB/임포트 에러 없음.
- 킬스위치: 매수만 차단/전면 동결/해제 3케이스 실측 확인.
- 적대검증: 과열 신호(RSI 78.5, MA200 +32%, bear) → 만장일치 거부 / 건전 신호 → 만장일치 승인.

## 2026-08-02 — chore: 스토리지 정리 (DB 레거시 테이블 제거 + journald 상한 + retention 자동화)

### 배경
- 장기 미관리로 스토리지 누적: `ai_db` 1.34GB, journald 1.8GB (상한 미설정).
- DB에는 구 프로젝트 잔재 스키마(`ai`/`market`/`marts`/`ref`/`sim`)와 `public` 레거시 테이블이 남아 있었음.
  현 봇 코드가 실제 참조하는 테이블은 7개(`trades`, `trade_events`, `kr_stock_master`, `news_articles`,
  `theme_history`, `theme_stocks`, `signal_events`)뿐이며, 레거시 테이블은 마지막 데이터 2026-01 이전 + 인덱스 스캔 0회로 확인.

### 수정
- **DB (`ai_db`)** — 사용자 승인 후 백업 없이 DROP:
  - 스키마 전체 제거: `ai`, `market`, `marts`, `ref`, `sim` (CASCADE)
  - `public` 레거시 제거: `krx_minute`(539MB), `ats_trades`(102MB), `candles`, `market_context`,
    `cli_summaries`, `daily_factors`, `strategy_audit`, `strategy_config`, `tech_filter_scores`,
    `scouting_candidates`, `fundamental_reports`, `research_reports`, `kr_trading_calendar`,
    `account_snapshot`, `assets`, 뷰 `bars`/`fills`
  - 보존 기간 180일 적용: `news_articles` 20,466행 / `theme_history` 5,144행 삭제
    (조회 코드의 기본 범위는 7일이라 180일이면 충분)
  - 잔존 7개 테이블 `VACUUM FULL ANALYZE`
- **pg_cron** (`postgres` DB의 `cron.job`):
  - jobid 4·7 — 삭제된 테이블(`krx_minute`/`ats_trades`/`candles`) ANALYZE 참조 제거 → 잔존 테이블로 교체
  - jobid 8 `retention-180d` 신규 등록 (매일 02:30, `schedule_in_database(... ,'ai_db')`) — 180일 초과 뉴스/테마 자동 삭제
- **journald**: `/etc/systemd/journald.conf.d/99-qwq-limit.conf` 신규
  (`SystemMaxUse=500M`, `MaxRetentionSec=30day`, `SystemMaxFileSize=50M`) + `--vacuum-size=500M` 1회 수행

### 결과
- `ai_db` 1.34GB → **318MB**, journald 1.8GB → **410MB**, 루트 파티션 12GB → **9.0GB** (16% → 12%)
- 봇 무중단(재시작 없음), 정리 후 로그에 DB 관련 에러 없음
- ⚠️ 미해결: `logs/` 날짜 디렉토리가 봇 기동일 기준으로 고정되어 여러 날 로그가 한 폴더에 누적됨
  (예: `logs/20260729/`에 07-30~08-01 로그 존재). 용량은 14MB로 경미하나 로테이션 로직 점검 필요.

## 2026-06-23 — fix: 선제 stale 청산 SignalEvent import 버그 (폭락 방어 불능)

### 배경
- 6/23 KOSPI -9.99% 폭락 로그에서 발견: `batch_analyzer._preemptive_stale_exit_on_bear`가
  `from ..core.types import ... SignalEvent ...`로 잘못 import → `SignalEvent`는 `core.event`에 있어 ImportError.
- 결과: 약세장 진입 시 정체 손실 포지션 선제 청산 방어가 매번 예외로 실패(무해하나 방어 불능).

### 수정
- **src/core/batch_analyzer.py:1266**: 중복·오류 로컬 import 제거 (StrategyType/OrderSide/Signal/SignalStrength/SignalEvent는 이미 모듈 상단에서 정상 import됨).

## 2026-06-23 — 종목별 "절대 자동매도 금지(exit_exempt)" + 수동 풀매수 config화 (펩트론 087010)

### 배경
- 사용자 지시: 펩트론(087010)을 가용현금 전액 매수 + 손절 등 모든 자동매도 영구 면제(코어보다 강한 보호).
- 기존 `exit_exempt`(ExitManager)는 손절/트레일링/익절/stale만 차단하고, ExitManager를 거치지 않는 매도 경로(RSI2·보유기간·선제stale·LLM 종가점검·WS 실시간)는 막지 못함. 또 in-memory라 재시작 시 소실.

### 수정
- **config/default.yml (kr)**: `no_auto_exit_symbols: ['087010']` 추가 (재시작에도 유지되는 자동매도 금지 화이트리스트). `manual_buy_orders` 키 추가(로드용, 매수 후 비움).
- **scripts/run_trader.py**: 기동 시 `kr.no_auto_exit_symbols`를 읽어 `exit_manager.add_exit_exempt()` 복원 (재시작 보호 유지).
- **src/schedulers/kr_scheduler.py**:
  - `__init__`: `_manual_buy_orders`를 `config.kr.manual_buy_orders`에서 로드
  - `run_manual_buy_orders`: ①이미 보유 시 매수 스킵(재시작 안전) ②시장가→marketable 지정가(현재가+0.6%, KIS가 시장가 주문가능금액을 상한가 기준으로 계산해 전액 매수 불가했던 문제 해결) ③`send_alert(force=...)` 잘못된 kwarg 제거(기존 버그)
  - `_check_exit_signal`(WS 실시간): exit_exempt 종목 즉시 return
  - `_run_position_eod_llm_check`: exit_exempt 종목 LLM 청산 제외
- **src/core/batch_analyzer.py**: `monitor_positions` 루프 + `_preemptive_stale_exit_on_bear`에 exit_exempt 가드 (RSI2·보유기간초과·선제stale 차단)

### 결과
- 087010 85주 @ 194,982원 체결(2026-06-23 12:56), exit_exempt 등록. 7개 자동매도 경로 전부 차단 확인.
- ⚠️ 손절 부재 = 하락 100% 노출. 청산은 수동 판단 전용.

## 2026-06-23 — 일일 손익률 분모를 total_equity로 통일 (외부 계좌 합산 시 왜곡 수정)

### 배경
- 외부 계좌(KIS_EXT_ACCOUNTS) 합산 시 실제 운용 자본은 23.7M이지만, `.env` `INITIAL_CAPITAL=500,000` 이 분모로 사용되어 일일 손익률이 약 47배 부풀려 표시됨 (실제 -0.5% → 표시 -25%, 한도 5% 대비 사용률 500%).
- 동일 분모를 사용하는 리스크 매니저의 일일 손실 차단 로직도 함께 오작동 가능.

### 수정
- **src/dashboard/data_collector.py**: `get_portfolio()` `daily_pnl_pct`, `get_risk()` `daily_loss_pct` — 분모 `portfolio.initial_capital` → `portfolio.total_equity`
- **src/risk/manager.py:790-794**: `_is_daily_loss_limit_hit()` 분모 통일 (`initial_capital` → `total_equity`). 주석도 신정책으로 갱신.
- **src/dashboard/us_api.py:83-84**: US `handle_portfolio` `daily_pnl_pct` — `total_value` 기준
- **src/dashboard/sse.py:433-434**: US SSE `us_portfolio` 이벤트 — `total_value` 기준
- **src/analytics/equity_tracker.py:90,142**: 폴백 경로 분모를 `total_equity` 기준으로 통일 (주 경로는 이미 `prev_snapshot.total_equity` 사용)

### 영향
- 대시보드 "오늘 -25.00%" 표시가 실제 비율(-0.5%대)로 정상화
- 한도 -5% 차단 로직이 실제 자산 기준으로 동작 (이전엔 500k 기준으로 작은 손실에도 차단)
- 외부 계좌 미사용 환경에선 `initial_capital ≈ total_equity` 이므로 행동 변화 없음

## 2026-06-17 — LLM 마이그레이션 자동화 (Phase 3→4 자동 전환 모니터)

### 추가
- **scripts/llm_migration_monitor.py**: 일일 자동 실행 스크립트
  - 최근 7일 shadow 분석 → 텔레그램 일일 요약 발송
  - Shadow 시작 후 7일+ 경과 시 자동 전환 검토
    - 기준: both_success ≥95%, key_overlap ≥85%, shadow_failed ≤5%, n≥50
    - 충족 시: `evolved_overrides.yml`에 `llm.openai_model_light: gpt-5.4-mini` 추가 + 봇 자동 재시작
    - 미충족 시: 텔레그램 경고 + 사유 명시 + 수동 결정 요청
  - 전환 후 7일 안정성 모니터 → Phase 5 (shadow 비활성) 권장 알림
  - 상태 파일: `~/.cache/ai_trader/llm_migration_state.json`
- **cron 등록**: `0 22 * * *` (매일 22:00 KST)
  - 명령: `/home/ubuntu/projects/qwq-ai-trader/venv/bin/python /home/ubuntu/projects/qwq-ai-trader/scripts/llm_migration_monitor.py`
  - 로그: `~/.cache/ai_trader/llm_migration_monitor.log`

### 자동 진행 흐름
1. 6/17~6/23: 매일 22:00 일일 요약 (shadow 데이터 누적 보고)
2. 6/24~: 매일 22:00 전환 기준 평가 → 충족 시 즉시 자동 전환
3. 전환 후: 매일 새 모델 모니터, 7일 안정 시 Phase 5 알림
4. 모든 단계에서 텔레그램 통보 → 사용자 개입 없이 진행

### 안전장치
- 기준 미충족 시 자동 변경 안 함
- 봇 재시작 실패해도 evolved_overrides.yml 변경은 다음 정기 재시작 시 자동 적용
- 롤백: evolved_overrides.yml의 `llm.openai_model_light` 키 제거 → 기본값(gpt-5-mini) 복귀

## 2026-06-17 — Phase 3 Shadow A/B 활성화 (gpt-5-mini vs gpt-5.4-mini, 1주 비교)

### 배경
- Phase 2에서 `gpt-5.4-mini` 가용 확인
- 즉시 전환 전에 1주 Shadow 비교로 응답 품질·지연·토큰 사용 검증

### 변경
- **src/utils/llm.py**:
  - `LLMConfig.openai_model_light_shadow: str = ""` 신규 (빈 문자열=비활성)
  - `_maybe_fire_shadow()`: primary가 `openai_model_light`였을 때만 트리거
  - `_fire_shadow()`: 동일 프롬프트로 shadow_model 호출 (fire-and-forget), 비교 로그 기록
  - 로그: `~/.cache/ai_trader/llm_shadow/YYYYMMDD.jsonl`
  - 필드: `pair_id`, `task`, `primary.{model, success, content, in/out_tokens}`, `shadow.{..., error, latency_ms}`
- **config/default.yml**: `openai_model_light_shadow: "gpt-5.4-mini"` 활성
- **scripts/run_trader.py**: 시작 로그에 shadow 모델 표시
  - 신규 로그: `[LLM] 모델 설정: ..., openai_light=gpt-5-mini, shadow=gpt-5.4-mini, ...`
- **scripts/analyze_shadow.py**: shadow 로그 비교 분석 스크립트
  - 성공률, JSON 파싱율, key overlap, 응답 크기/토큰/지연 비교
  - `--days N`, `--json out.json` 옵션

### 영향
- light OpenAI 호출 시 추가 OpenAI 호출 1건 발생 (fire-and-forget, 본 응답 차단 없음)
- 베이스라인 286건/주 → 추가 비용 estimated $0.3~0.6/주 (gpt-5.4-mini ≈ gpt-5-mini 단가 가정)
- 비교 데이터 1주 누적 후 Phase 4 전환 결정

### 검증
- py_compile + restart 정상
- 시작 로그: `[LLM] 모델 설정: ..., shadow=gpt-5.4-mini, ...` 확인

## 2026-06-17 — ✅ Phase 2 후속 모델 가용성 프로브: gpt-5.4-mini 확인 (마이그레이션 경로 확정)

### 추가
- **scripts/probe_openai_models.py**: light/heavy 후속 모델 후보 14개를 OpenAI API에 실제 호출하여 가용성 + resolved snapshot 확인

### 프로브 결과 (2026-06-17)

**Light 가용** (gpt-5-mini 대체 경로):
| Alias | Resolved Snapshot | 비고 |
|-------|-------------------|------|
| ✅ gpt-5.4-mini | gpt-5.4-mini-2026-03-17 | **권장 — 같은 family 후속** |
| ✅ gpt-5.4-nano | gpt-5.4-nano-2026-03-17 | 저비용 대안 |

**Heavy 가용** (gpt-5.4 미래 대비):
| Alias | Resolved Snapshot |
|-------|-------------------|
| ✅ gpt-5.5 | gpt-5.5-2026-04-23 |

**기타 시도 → 모두 404 model_not_found**:
gpt-5.5-mini, gpt-5.6-mini, gpt-6-mini, gpt-6-nano, gpt-5.5-nano, gpt-5-mini-2026-03-05, gpt-5-mini-latest, gpt-5.6, gpt-6, gpt-6-pro, gpt-5.4-pro

### 마이그레이션 경로 확정
- `gpt-5-mini` (🔴 deprecated 2026-12-10) → **`gpt-5.4-mini`** (안전, 같은 family 후속)
- `gpt-5.4` (🟢 안전) → 유지, 필요 시 `gpt-5.5`로 업그레이드 가능

## 2026-06-17 — 🔴 GPT-5 alias→snapshot 캡처: gpt-5-mini가 Deprecation 대상으로 해석 (Phase 1 후속)

### 추가
- **scripts/capture_openai_snapshots.py**: config의 OpenAI alias 호출 → 응답 `model` 필드에서 실제 snapshot ID 추출

### 캡처 결과 (2026-06-17 첫 실행)
| Alias | Resolved Snapshot | Deprecation |
|-------|-------------------|-------------|
| gpt-5-mini | **gpt-5-mini-2025-08-07** | 🔴 **YES (2026-12-10 셧다운)** |
| gpt-5.4 | gpt-5.4-2026-03-05 | 🟢 NO |

### 영향 평가
- 사용 중인 `gpt-5-mini` alias가 deprecation 직접 대상 snapshot으로 해석됨
- OpenAI가 alias를 newer snapshot으로 auto-roll 하지 않으면 **12/10 셧다운 시 즉시 호출 실패**
- 베이스라인 286건/주 → 폴백(Gemini)으로 100% 전환 시 라우팅 변화 + 비용 증가 가능

### 긴급도 상승
- Phase 2 (후속 모델 조사) **즉시 진입 필요**
- 후보:
  - `gpt-5.4-mini` 또는 신규 `gpt-6-mini` 가용성 확인
  - 임시 폴백 강화 (Gemini로 사전 트래픽 이전)

## 2026-06-17 — quick_analysis 실패율 33% 원인 해결 (max_tokens 부족)

### 배경
- Phase 1 베이스라인 분석에서 quick_analysis task 실패율 33% (gpt-5-mini) / 31.6% (gemini) 발견
- 로그 분석:
  - **gemini 6건 실패**: 8개 포지션 응답이 ~860~900 bytes에서 JSON 닫히지 않고 잘림
  - **gpt-5-mini 2건 실패**: raw 빈 응답 — reasoning 모델이 reasoning에 토큰 소진
- 원인:
  1. `kr_scheduler.py:1210` 포지션 15:00 LLM `max_tokens=400` — 8개 포지션 응답에 부족
  2. `kr_scheduler.py:1565` 진입 검증 LLM `max_tokens=120` — GPT-5 reasoning 토큰 보장 안 됨

### 변경
- **`src/schedulers/kr_scheduler.py:1210`**: `max_tokens=400` → `max(600, len(pos_lines) * 150)` 동적 계산
  - 8개 포지션 시 1200 tokens 보장 (실제 응답 860~900 bytes 여유 확보)
- **`src/schedulers/kr_scheduler.py:1565`**: `max_tokens=120` → `600`
  - GPT-5 reasoning 모델은 reasoning에 토큰 소진 → 출력 짧아도 여유 필요

### 검증
- py_compile + restart 정상
- 후속 모니터링: 다음 7일 베이스라인 재실행으로 quick_analysis 성공률 ≥95% 회복 확인

## 2026-06-16 — LLM 베이스라인 수집 스크립트 (GPT-5 마이그레이션 Phase 1 후속)

### 추가
- **scripts/llm_baseline.py**: `~/.cache/ai_trader/llm_responses/*.jsonl`에서 task×model별 통계 집계
  - 호출수, 성공률, raw 응답 크기 (토큰 미기록 → bytes proxy)
  - Deprecation 대상 snapshot ID(`gpt-5-2025-08-07` 등) 직접 사용 감지
  - `--days N`, `--json out.json` 옵션
- 초기 베이스라인 (최근 7일):
  - 총 1,149건 호출
  - gemini-3.1-flash-lite-preview: 858건 (75%) — theme_detection, quick_analysis
  - gpt-5-mini: 286건 (25%) — theme_detection, quick_analysis
  - gpt-5.4: 5건 — strategy_analysis (heavy)
  - **Deprecation snapshot 직접 사용 0건** (alias만 사용)

### 마이그레이션 우선순위 (분석 결과)
1. **gpt-5-mini** (286건/주, alias) — 영향 최대, Shadow A/B 우선
2. **gpt-5.4** (5건/주, heavy) — 영향 작지만 품질 중요
3. Gemini 무관

## 2026-06-16 — LLM 모델명 config override (GPT-5 deprecation 마이그레이션 Phase 1)

### 배경
- OpenAI 발표: GPT-5/mini/nano/pro 2025-08-07 snapshot은 2026-12-10 셧다운
- 현재 사용: `gpt-5.4`(heavy), `gpt-5-mini`(light) — alias 형태 사용 중
- 코드 수정 없이 모델 교체 가능한 사전 작업 필요 (Phase 1)

### 변경
- **config/default.yml**: `llm:` 섹션에 모델명 키 4개 추가
  - `openai_model_heavy`, `openai_model_light`
  - `gemini_model_heavy`, `gemini_model_light`
- **src/utils/llm.py**:
  - `LLMConfig.from_config(cls, cfg)`: env(API키) + YAML(모델명) 결합 로드
  - `set_llm_config(cfg)`: 전역 LLM 설정 등록 함수 추가
  - `get_llm_manager()`: 등록된 설정 우선 사용 (없으면 from_env 폴백)
- **scripts/run_trader.py**: AppConfig 로드 직후 `set_llm_config()` 호출
  - 시작 로그: `[LLM] 모델 설정: openai_heavy=..., openai_light=..., gemini_heavy=..., gemini_light=...`

### 효과
- 모델 교체 시 코드 변경 불필요 — `config/default.yml` 또는 `evolved_overrides.yml`에서 키만 바꾸면 적용
- Shadow A/B 단계(Phase 3)에서 신구 모델 동시 사용 기반 마련
- 빈 문자열/키 없음 시 dataclass 기본값 폴백 (backward compat)

### 검증
- py_compile 통과
- restart 후 `[LLM] 모델 설정: openai_heavy=gpt-5.4, openai_light=gpt-5-mini, ...` 로그 확인

## 2026-06-16 (확장) — SECOND/THIRD 익절 단계도 0.7x 추가 디스카운트

### 배경
- 사용자 추가 지적: 2차/3차 익절 종목도 절반 이상 자금 회수된 상태 → 슬롯 양보 정당
- TRAILING만 디스카운트하면 분할익절 진행 중 종목은 잔여비율로만 가중 → 양보 부족

### 변경 (src/risk/manager.py:_get_position_weight)
- SECOND/THIRD 단계: 기존 weight에 추가 **× 0.7** 곱셈
- SECOND/THIRD 전용 floor: 0.2 → **0.15**
- NONE/FIRST는 변경 없음 (1차 익절은 잔여 80%로 아직 큰 포지션)
- TRAILING은 0.5x 유지 (직전 변경)

### 효과 (예시)
- 8개 모두 SECOND(잔여 48%) 시
  - 기존: max(0.2, 0.48) × 8 = 3.84 → 신규 4.16
  - 신규: max(0.15, 0.48×0.7) × 8 = 2.69 → **신규 5.31** (+1.15)
- 8개 모두 THIRD(잔여 31%) 시
  - 기존: max(0.2, 0.31) × 8 = 2.48 → 신규 5.52
  - 신규: max(0.15, 0.31×0.7) × 8 = 1.74 → **신규 6.26** (+0.74)

## 2026-06-16 — TRAILING 단계 슬롯 가중치 추가 디스카운트 (상승장 신규 매수 기회 확보)

### 배경
- 사용자 지적: 상승장에 1차/2차 익절 후 트레일링 진입 종목이 많아지면 max_positions 한계로 신규 매수 기회 상실 우려
- 기존 메커니즘(2026-05-06): 잔여 비율(`remain/orig`) 기반 가중 카운트, floor 0.2
- 라이브 검증 (현재 11개 포지션): raw=8(max 도달) → weighted=6.38 → 신규 1.62 슬롯 여유
- 기존 메커니즘이 50%는 커버하나, TRAILING 단계는 별도 디스카운트 없어 사용자 우려 일부 유효

### 변경 (src/risk/manager.py:_get_position_weight)
- TRAILING 단계 포지션: 기존 `weight = remain/orig`에 추가 **× 0.5** 곱셈
- TRAILING 전용 floor: 0.2 → **0.1** 완화 (완전 제외는 risk 노출 과다)
- 기존 NONE/FIRST/SECOND/THIRD 단계는 floor 0.2 유지 (변경 없음)
- 정당화: TRAILING은 +5% 이상 확정 이익 + 고점 추적 모드 → 신규 슬롯 우선 양보

### 효과 (예시)
- 8개 포지션 모두 TRAILING + 잔여 30% 가정:
  - 기존: max(0.2, 0.3) × 8 = 2.4 슬롯 → 5.6 여유
  - 신규: max(0.1, 0.3×0.5) × 8 = 1.2 슬롯 → **6.8 여유**
- 차이: 신규 슬롯 1.2개 추가 확보

### 검증
- py_compile 통과
- systemctl restart 후 active

## 2026-06-14 — 크로스검증 3건 패치 (rule11 dedup + SEPA 횡보 차단 + gap_and_go 장막판 캡)

### 배경
- 6/10~6/13 KR 거래 -507,756원, 승률 42%, 일별 악화 추세 (-58k → -175k → -273k)
- 단일 최대 손실 SK하이닉스 SEPA -272,868원 — sideways 레짐에 추세 전략 진입 부정합
- 6/12 14:09~14:57 gap_and_go 5건 집중 진입 → 다음날 오버나잇 손실 -175k 사고
- rule11 shadow log 5건이 전부 동일 종목·날짜(000810, 6/8) 중복 → 통계 의미 상실

### 변경 (src/core/cross_validator.py)
- **규칙 3-3 신규**: KR sideways/neutral 레짐 + sepa_trend → 매수 차단
  - 근거: 6/12 SK하이닉스 단일 -272k 사고, SEPA는 추세 추종 전략으로 횡보장 부정합
- **규칙 3-4 신규**: KR 14:30 이후 gap_and_go 신규 진입 일 2건 캡
  - `_gap_late_count` 일일 카운터, 통과 시점에 증가, 한도 도달 시 차단
  - 근거: 6/12 장막판 5건 집중 → 오버나잇 갭하락 노출 패턴
- **rule11 hit dedup**: `_log_rule11_hit`에 (date, symbol) 키셋 추가
  - 5분 주기 스크리닝이 동일 hit을 다회 기록하던 문제 해결
  - 진짜 unique 표본 카운트로 shadow → 활성화 결정 데이터 정합화
- `__init__`에 `_rule11_logged_today`, `_rule11_dedup_date`, `_gap_late_count`, `_gap_late_count_date` 신규
- `validate()` 입구에 일일 리셋 + `_late_gap_entry` 플래그 추가, 통과 종점에서 카운터 증가

### 보류 (W24 권고)
- gap_and_go.min_stop_pct 4.0% → 4.4% 완화 권고 — `evolution_state.active_change`(6/8 min_score 60→65) 평가 종료 후 적용 ("1건 변경 원칙" 준수)

### 검증
- py_compile 통과
- systemctl restart 후 active 확인, 에러 로그 없음

## 2026-06-09 — 좀비 포지션 자동 감지·정리 (KIS fill 수신 실패 대응)

### 배경
- 2026-06-08 사고: 032830 삼성생명이 KIS에서는 매도 체결됐는데 엔진 fill_check 미수신 → 엔진은 5주 보유로 인식, KIS는 0주
- 결과: 5분 주기로 매도 시도 19회 반복, KIS는 "주문 가능한 수량을 초과" 거부, _sync_portfolio는 "매도 pending 중" 보호 로직에 걸려 유령 정리 못함
- 사용자 수동 확인 후 봇 재시작으로만 정리 가능했던 사각지대

### 변경
- **`src/core/engine.py::RiskManager`**:
  - `_kis_qty_mismatch_count: Dict[str, int]` + `_zombie_candidate_symbols: Set[str]` 신규
  - `on_order` 실패 핸들러: 에러 메시지에 `APBK0400` 또는 `주문 가능한 수량` 포함 시 종목별 카운터 증가
  - 카운터 ≥2 + 매도 주문이면 `_zombie_candidate_symbols`에 추가 + 텔레그램 ⚠️ 알람 (비차단)
- **`src/schedulers/kr_scheduler.py::_sync_portfolio`**:
  - `_zombie_candidate_symbols`에 있는 종목은 pending 상태 무시하고 즉시 유령 정리
  - 추가 안전망: pending 시간이 1800초(30분) 이상 stale이면 강제 유령 처리
  - 정리 후 카운터·마킹 자동 클리어

### 효과
- KIS 잔고와 엔진 인식 불일치 자동 감지 (수동 재시작 불필요)
- 무한 매도 재시도 루프 차단
- 운영자에게 텔레그램 실시간 경고

## 2026-06-07 — 야간·주말 갭 risk 캡처 시스템 (전문가 시스템 확장)

### 배경
- 사용자 지적: "한국 선물·미장 반도체 약세인데 월요일 분위기 안 좋을 듯. 전문가가 catch up 해야 한다"
- 기존 전문가 시스템(07:30/13:00/16:30)은 야간/주말 미동작, KOSPI 야간선물·SOX 단기 변동 미반영

### A안 — 기존 전문가 보강
- **`src/experts/kr_market_expert.py`**:
  - `_fetch_kospi200_futures()` 추가 (KS200=F → ^KS200 → NKD=F 폴백)
  - score 룰: 야간선물 -2%+ → -18점 (월요일 갭다운 위험), -1%+ → -10점, +2%+ → +15점
- **`src/experts/us_market_expert.py`**:
  - `_fetch_semis_state()` 추가 (^SOX, SMH의 1일/5일 변동)
  - score 룰: SOX 5일 -3%+ → -18점 (KR 직격), SOX 당일 -2%+ → -10점
- **`src/experts/macro_economist.py`**:
  - `_fetch_semis_basket_5d()` 추가 (^SOX/SMH/NVDA/AMD/TSM 5일 평균)
  - 약한 가중치(-8/+5) — us_market_expert와 중복 회피

### B안 — 신규 슬롯 + 임계 완화
- **`src/schedulers/kr_scheduler.py::run_expert_briefing`**:
  - `sunday_evening` 22:00 (일요일만), `monday_premarket` 06:00 (월요일만) 슬롯 추가
  - 요일 가드(`slot_weekday_filter`)로 발화 제한
  - 주말 슬롯 한정 BEAR 합의 임계 완화 (`confidence ≥0.6`, 1명) — 주말 데이터 적어 신뢰도 자연 낮음
  - 새 슬롯은 morning과 동일하게 채널(report_chat_id) 발송

### C안 — 신규 전문가 (8번째)
- **`src/experts/weekend_signal_expert.py`** (신규):
  - 데이터: ES=F, NQ=F, KS200=F/NKD=F, KRW=X, ^VIX, BTC-USD, ZB=F
  - 룰: 야간 US 선물·KR 야간선물·원/달러·VIX·BTC 종합 점수 (-100~+100)
  - refresh 30분, valid 2시간 (시장 변동 빠름 반영)
  - `weights=1.2` (갭 risk 캐치용, 종합 score 우선)
- **`src/experts/orchestrator.py::register_all`**: 8번째로 등록, `market_experts` 튜플 포함
- **`src/experts/types.py`**: agents/weights dict에 `weekend_signal_expert` 추가
- **`scripts/run_trader.py`**: 등록 인원 하드코딩 "7명" → `len(orchestrator.agents)` 동적

### 효과
- 매주 일요일 22:00 KST + 월요일 06:00 KST에 갭 risk 자동 브리핑 (텔레그램 채널)
- 평일 슬롯에도 SOX/야간선물 정보가 us_market/kr_market 점수에 자동 반영
- 월요일 시가 갭다운 위험을 시스템이 사전 감지 → 사용자 매매 판단 우선순위 정보 제공

### 검증
- 봇 재시작 후 "전문가 8명 등록 완료" 확인
- 다음 sunday_evening 슬롯: 2026-06-07 22:00 KST (오늘 50분 후)

## 2026-06-05 — 모닝브리프 + 전문가 브리핑 통합 발송

### 변경
- `src/analytics/daily_report.py`:
  - 07:00 미국 마감 후 생성되는 LLM 모닝브리프를 별도 발송에서 **파일 캐시**로 전환
  - 캐시: `~/.cache/ai_trader/llm_morning_brief.json` (text + generated_at)
- `src/schedulers/kr_scheduler.py::_send_expert_briefing_telegram`:
  - morning 슬롯(07:30)에서 LLM 모닝브리프 캐시 prepend (cache age < 6h 가드)
  - 단일 통합 메시지로 채널 발송 (≥4096자 시 send_report 자동 분할)

### 효과
- 종전: 07:00 LLM 모닝브리프 1개, 07:30 전문가 브리핑 1개 — 두 메시지 분산
- 변경 후: 07:30에 모닝브리프 + 구분선 + 전문가 브리핑 = 단일 통합 메시지
- 첫 적용일: 2026-06-06 (06/05 캐시는 변경 전 코드로 미저장)

## 2026-06-05 — 전문가 브리핑 메시지 상세화

### 변경
- `src/schedulers/kr_scheduler.py::_send_expert_briefing_telegram`:
  - 종합 score 해석 라벨 추가 (강세 우위/약상승/중립/약하락/약세 우위)
  - bull/neutral/bear 분포 카운트 (총 N명 중 N/N/N)
  - BEAR 합의 기준 명시 (신뢰도 ≥70% + 2명 이상)
  - 신뢰도가 "데이터 충실도"임을 안내 (LLM 자기평가 아님)
  - 시장체제 전문가 5명만 종합 score에 반영됨을 표시 (뉴스/실적은 `(종합 미반영)` 태그)
  - 모든 슬롯에 전문가별 핵심 발견 1개씩 인라인 표시 (이전: morning만 별도 섹션)
  - morning 슬롯엔 "추가 인사이트" 섹션으로 두 번째 발견 첨부

### 의도
- 사용자 요청 (2026-06-04 23:39 KST): "레포트 더 상세하게 설명"
- 종합 score가 산출된 맥락(어떤 분포에서 나왔는지)을 한 메시지로 파악 가능
- 신뢰도 해석 오류 방지 (모델 확신이 아닌 데이터 양 기반임)

## 2026-06-04 — 코어홀딩 추세 진입 실패 손절 + stale 룰/예산 정상화

### 배경
- 사용자 질의: "수익률 별로인 코어를 계속 들고 가는 게 의미가 있냐"
- 진단: 오리온(-5.4%, 32영업일 보유)·SK(-6.9%, 6영업일 보유) 모두 진입 후 highest<avg
  → 추세 캐처 컨셉의 진입 실패형. stale 사각지대(±3% 밴드)에 갇혀 자동청산 불가

### 실행 (4단계 패키지)
1. **종목 매도** (`scripts/sell_specific.py` 신규):
   - 271560 오리온 16주 매도 → -118k (-5.4%)
   - 034730 SK 3주 매도 → -145k (-6.9%)
   - 합계 -263k 손절, 현금 4,036k 회수
2. **stale 룰 강화** (`config/evolved_overrides.yml`):
   - `core_stale_pnl_band_pct`: 3.0 → 7.0
   - `core_stale_strict_band_pct`: 2.0 → 5.0
   - 향후 -5~7% "느린 손실" 패턴 자동 컷
3. **코어 예산 정상화** (`config/evolved_overrides.yml`):
   - `risk_config.strategy_allocation.core_holding`: 10.0 → 20.0
   - `strategic_swing`: 38.4 → 28.4 (상쇄, 총합 98%)
   - 근거: KOSPI 5일 +9.4%/20일 +33.4% 강세장 + 6/4 매도 후 빈슬롯 즉시 활용
4. **봇 재시작 + 검증**: 09개 포지션 정상 로드, 코어홀딩스케줄러 정상 가동
5. **6/5 빈슬롯 매수 자동 가동 예정**: 09:10~10:00 첫 윈도우

### 영향
- 즉시: 사각지대 손실 누적 차단, 4M 현금 회수
- 1주: 강세장 코어 2~3종 신규 진입 가능
- 장기: 같은 패턴 시스템 자동 컷 → 매매 인지 비용 절감

## 2026-05-31 — 전문가 모닝 브리핑 → 미장 레포트 채널 라우팅

### 변경
- `src/schedulers/kr_scheduler.py`:
  - `_send_expert_briefing_telegram(use_report_channel=False)` 인자 추가
  - **morning 슬롯 (07:30)** → `report_chat_id` (-1003374679062, LLM 모닝브리프 채널)
  - **midday/after 슬롯** → 기본 DM (TELEGRAM_CHAT_ID 1754899925)
- 아침 슬롯 전용으로 **핵심 발견** 섹션 추가 (각 전문가 key_findings[0] 첨부)
- HTML parse mode 적용 (bold/italic)

### 의도
- 미장 마감 모닝브리프와 같은 채널에서 전문가 7명 의견 함께 받음
- 채널 구독자가 통합 시장 진단 (US 마감 + 전문가 진단) 한 곳에서 확인
- 장중/장후 브리핑은 개인 DM으로 분리 (운영자 전용)

### 검증
- py_compile OK, 봇 재시작 active
- 라이브 send_report 테스트 → True (채널 전송 성공)
- 다음 브리핑: 6/1 (월) 07:30 → 미장 채널 첫 전송

## 2026-05-30 — trailing_stop_pct 완화 (3.0 → 4.5, +50%)

### 배경
- 주간 매도 후속 복기 보고서 (n=66):
  - trailing 평균 매도후% **+22.54%** (가장 큰 놓침 패턴)
  - 놓친 상위 종목: 삼성전기, LG이노텍, SK하이닉스, LG전자 (반도체/IT 추세)
  - 추세 캐치 종목에서 trailing 조기컷이 반복

### 의사결정 분석
- 보고서 원안: 3.0 → 6.0 (+100%, "4% → 6%로 잘못 인용된 +50%")
- 보고서 daily_max 계산 부적절: trailing은 익절 단계라 손실 한도와 무관
- 결정: **보수적 단계 채택 (A안)** — 3.0 → 4.5 (+50%)
- 1주일 후 재평가 후 6.0 추가 확대 검토

### 변경
- `config/evolved_overrides.yml:43` — `trailing_stop_pct: 3.0 → 4.5`
- 인라인 주석: 변경 근거, 재평가 일자(2026-06-06) 명시

### 영향 범위
- 모든 전략의 trailing exit (단계적이므로 안전)
- trailing_activate_pct (5%)는 미변경 — CLAUDE.md "최대 1개 파라미터만 변경" 규칙 준수

### 검증
- yaml 파싱 OK
- 봇 재시작 active
- ExitManager stage 복원 8종목 정상
- trailing 관련 ERROR 0건

### 재평가 (2026-06-06 예정)
- trailing 평균 매도후% 다시 측정
- 개선(< +15%) 시 6.0% 추가 확대 검토
- 악화 시 즉시 5.0%로 후퇴

## 2026-05-29 — 전문가 시스템 운영 강화 (Shadow + 모니터링 + 매크로 캘린더)

### 1. 첫 거래일 모니터링
- `run_expert_briefing()` — 텔레그램 브리핑 전송 (07:30/13:00/16:30)
  - 종합 점수, bear_consensus, 7명 개별 score/confidence 이모지로 시각화
- `quality_validator` 결과 텔레그램 알림 (20:25)
  - 발행 수, 평균 신뢰도, bull/bear 분포, 주간 발행

### 2. Shadow Mode (규칙 #11)
- `ExpertConfig.shadow_mode` 신규 (기본 true)
- shadow_mode=true: BEAR 합의 감지 시 **차단하지 않고** hit_log에 기록만
- `~/.cache/ai_trader/rule11_shadow_log.jsonl` — 종목, 전문가 의견 시점 영속화
- `scripts/analyze_rule11_shadow.py` — 주간 hit rate 분석 (yfinance로 후속 가격 검증)
  - hit rate ≥60% → shadow_mode: false 권장
  - 50~60% → 1주일 추가 관찰
  - <50% → 규칙 비활성 유지

### 3. 수동 매크로 오버라이드 (FOMC/CPI/PCE/NFP 캘린더)
- `~/.cache/ai_trader/manual_macro_overrides.json` 작성
- 출처: Federal Reserve, BLS, BEA, Bank of Korea 공식 사이트
- 포함:
  - BOK 기준금리: 2.5% (2026-05-28 동결, 8회 연속)
  - Fed Funds Rate Target: 4.25~4.50%
  - **다음 발표 캘린더**:
    - 2026-06-05 NFP (5월분, 08:30 ET)
    - 2026-06-10 CPI (5월분, 08:30 ET)
    - 2026-06-17 FOMC (dot plot 갱신, 14:00 ET)
    - 2026-06-25 PCE (5월분, 08:30 ET)
    - 2026-07-10 BOK 금통위
- 발표 직후 사용자가 cpi_yoy 등 직접 갱신 권장

### 검증
- py_compile 전체 통과
- 봇 재시작 후 7명 등록 + 브리핑 스케줄러 시작 + ERROR 0건
- shadow_mode=True 확인 (config + 코드)

## 2026-05-29 — 전문가 시스템 P2 정리 (코드 품질)

리뷰 P2 항목 7건 정리. 운영 영향은 없으나 일관성·유지보수 향상.

- **P2-1** `__init__.py` docstring에서 존재하지 않는 perplexity.py 언급 제거 + wiki.py 추가
- **P2-2** `_build_opinion` issued_at/valid_until을 동일 now() 기준으로 통일 (시차 제거)
- **P2-3** `us2y` → `us_short_yield`로 이름 정정 (^IRX는 13주 T-bill, 2년물 아님)
- **P2-4** 거시 종합 LLM Task QUICK_ANALYSIS → MARKET_ANALYSIS (heavy 모델)
- **P2-5** `engine._expert_orchestrator` → public `engine.expert_orchestrator`
- **P2-6** `ExpertOpinion.from_dict` ValueError/TypeError 안전 파싱
- **P2-7** quality_validator 미설치 시 info → warning (집계 누락 방지)
- **Pyright 보강**: orchestrator.py `result` 타입 isinstance 분기 추가
- yfinance gold 정상범위 1000~4000 → 1000~4500 (2026 시점 시세 반영)

검증: py_compile 전체 통과, 봇 재시작 후 7명 등록 + ERROR 0건.

## 2026-05-29 — 전문가 시스템 P0/P1 후속 수정 (코드리뷰 28건 대응)

리뷰에서 발견된 P0 6건 + P1 7건 즉시 수정.

### P0 (치명적, 매매 동결 위험)
- **P0-1 (cross_validator.py:437)** BEAR 합의 게이트가 표본 부족 시 매매 영구 차단
  - 유효 의견 ≥4명일 때만 게이트 활성화
  - fail_open=true 존중 (평가 실패 시 통과)
- **P0-2 (quality_validator.py)** 동기 I/O가 메인 루프 stall 가능 → async + to_thread 전환
- **P0-3 (base.py)** _analyze가 dict 등 잘못된 타입 반환 시 AttributeError → isinstance 체크
- **P0-4 (orchestrator.py)** 음수 confidence/가중치로 score 부호 역전 → max(0, ...) 클램프
- **P0-6 (base.py)** yfinance 7명 병렬 호출 429 위험 → YFINANCE_SEMAPHORE(4) throttle
- **P0-7 (base.py)** 만료된 캐시 영구 재사용 → is_valid 체크 후에만 사용

### P1 (중요)
- **P1-1 (orchestrator.py)** aiohttp 세션 누수 → close_all() 메서드 추가
- **P1-3 (macro_economist.py)** yfinance 비정상값 → 지표별 정상 범위 (_VALID_RANGES) 검증
  - 검증 동작 확인: gold=4529 ($/oz) 정상범위[1000,4000] 이탈 자동 폐기
- **P1-4 (news_curator.py)** get_symbol_sentiment LLM 호출이 budget 우회 → _check_budget/inc_call 적용
- **P1-5 (news_curator.py)** LLM 응답 형식 변화 silent fail → warning 로그
- **P1-6 (market_regime.py)** apply_expert_adjustment이 pending_regime 메커니즘 무시
  → 10분 확인 시간 도입 (페이크 BEAR 변동 방지)
- **P1-7 (market_regime.py)** bear → bull 회복 경로 부재 → score≥10 + bear 해소 시 sideways 격상
- **P1-8 (kr_market_expert.py)** 공매도 휴일 미처리 → is_kr_market_holiday로 영업일 회피
- **P1-10 (earnings_expert.py)** DART API 미사용 (docstring 거짓) → 실제 호출 구현
  → 잠정실적/연결재무 키워드 필터, 10건 이상 시 score +5
- **P1-11** 영문 로그 prefix → 한국어 통일
  ([macro]→[거시], [kr-market]→[KR시장], [news_curator]→[뉴스큐레이터] 등)

### 검증
- P0-1: 표본 2명 + BEAR → 게이트 비활성 (정상)
- P0-4: 음수 confidence 무시 (정상)
- P1-1: close_all() 호출 성공
- P1-3: gold 4529 폐기 로그 확인
- 봇 재시작 후 7명 등록 OK + ERROR 0건

## 2026-05-29 — 전문가 시스템 도입 (7명 도메인 전문가)

### 배경
- 기존 6명 운영/분석 에이전트는 가격·수급·체결 중심
- 약점: 거시·뉴스·섹터·실적 등 **외부 정보 입력이 빈약**
- 글로벌 매크로(Fed/CPI/환율), 뉴스 sentiment, 산업 사이클을 자동 흡수하는 도메인 전문가 7명 추가

### 도입 전문가
| 전문가 | 역할 | 데이터 소스 | 주기 |
|--------|------|-------------|------|
| news-curator | 종목/섹터 sentiment + 이벤트 태그 | 네이버 + Finnhub + Perplexity | 30분 |
| macro-economist | Fed/금리/환율/원자재 거시 진단 | yfinance + Perplexity | 일 3회 |
| kr-market-expert | KOSPI 수급·섹터 로테이션 | pykrx + yfinance | 일 3회 |
| us-market-expert | SPY/QQQ/VIX/섹터 ETF/어닝 | yfinance + Finnhub | 일 3회 |
| kr-economy-expert | 한국 거시 (한은/수출입/PF) | Perplexity + yfinance | 일 2회 |
| global-micro-expert | 반도체·2차전지·바이오·조선 공급망 | Perplexity + yfinance | 일 2회 |
| earnings-expert | 어닝 캘린더·서프라이즈·드리프트 | Finnhub + DART | 일 2회 |

### 새 코드 (src/experts/, 약 2,500줄)
- `types.py` — ExpertOpinion, RegimeBias, ExpertConfig
- `base.py` — ExpertAgent (캐시·예산·에러 핸들링)
- `orchestrator.py` — 7명 조율, 병렬 실행, aggregate_regime_score, bear_consensus
- `opinion_store.py` — ~/.cache/ai_trader/experts/ 일별 영속화
- `wiki.py` — ~/.cache/ai_trader/wiki/experts/ 마크다운 누적 + 토요일 lint
- 7개 전문가 모듈

### 엔진 통합
1. `src/core/market_regime.py` — `apply_expert_adjustment()` 메서드
   - BEAR 합의 → bull/sideways/neutral → bear 강등
   - 강한 BULL(score≥+20) → sideways → bull 격상
2. `src/core/cross_validator.py` — **규칙 #11 (전문가 BEAR 합의 게이트)**
   - confidence≥0.7인 BEAR 의견 2명 이상 → BUY 신호 즉시 차단
3. `src/core/engine.py` — CrossValidator init에 expert_orchestrator 전달
4. `src/core/evolution/daily_reviewer.py` — LLM 프롬프트에 7명 의견 주입
5. `src/core/evolution/quality_validator.py` — `_check_expert_output()` 일일 신뢰도 체크
6. `src/schedulers/kr_scheduler.py` — `run_expert_briefing()` 07:30/13:00/16:30
7. `src/schedulers/us_scheduler.py` — `us_expert_loop()` ET 08:30/12:30/16:30
8. `scripts/run_trader.py` — bot.expert_orchestrator 초기화 + engine 주입

### 설정 (config/default.yml)
```yaml
experts:
  enabled: true
  fail_open: true
  daily_call_budget: 50
  cache_ttl_hours: 6
  agents: {...각 on/off}
  weights: {...품질 가중치}
```

### 신규 API 키 발급: **0개**
- NAVER/FINNHUB/PERPLEXITY/DART 모두 이미 보유
- FRED → yfinance ^TNX 등으로 우회
- ECOS(한은) → Perplexity 자연어 검색으로 우회
- 수동 입력 슬롯: `~/.cache/ai_trader/manual_macro_overrides.json`

### 안전장치
- **fail_open**: 전문가 오류 시 매매 차단 안 함 (기본값)
- **graceful degradation**: 모든 _analyze 예외 → NEUTRAL 의견 반환
- **마스터 스위치**: `experts.enabled: false`
- **개별 on/off**: agents.{name}: false
- **호출 예산**: 에이전트당 일 50회 상한, 초과 시 캐시 의존

### 검증
- 7명 병렬 분석 7/7 유효 (dry-run)
- aggregate_regime_score +3 neutral / bear_consensus False
- US bull score=+25 (SPY MA50+7.5%, 어닝 +10.7%) 정상 식별
- market_regime.apply_expert_adjustment dry-run OK
- cross_validator 규칙 #11 BUY 차단 동작 확인

### 알려진 제약
- pykrx 수급 데이터: KRX_ID/PW 미설정으로 일부 fail (graceful fallback)
- yfinance KRW=X 일부 stale 값 가능 → 수동 오버라이드 권장
- Perplexity 호출 빈도 증가 → Pro 한도(월 $20) 내

## 2026-05-11 — 코어 A안 재정의 + P0-1 계측 보강

### 1) 코어홀딩 "장기 추세 캐처" A안 (3~6개월 +30~50% 노림)

**배경**: 최근 6주 코어 12건 청산 분석 — 큰 수익(+14~30%)은 전부 "추세 캐치"(SK스퀘어/SKT/HD현대일렉). 점수 체계 "안정성 60% + 모멘텀 20%" 가중치가 박스권 대형주(KT/오리온/통신주) 선호 → 1차 익절 후 본전 회귀 사례 다수. 사용자 요청: "코어 종목 선택 로직 재점검, 보유 종목 너무 구림".

**변경 1: 진입 모멘텀 필터 (`core_screener._apply_base_filter`)**
- 60일 수익률 ≥ +5% (박스권 대형주 자동 배제)
- 신고가 80% 이내 (from_52w_high ≥ -20%)
- 기존: MA200 위, PER>0, 거래대금 유지

**변경 2: 점수 가중치 재배분 (`core_screener._score_*`)**
- 추세 안정성: 30 → **20점** (저변동성 가중치 제거, 박스권 신호 컷)
- 펀더멘탈: 30 → **20점** (코어 역할: 펀더보다 모멘텀 우선)
- 수급 추세: 20점 유지
- 모멘텀 품질: 20 → **30점** (60일 가중 단계별, 신고가 근접 추가, 모멘텀 가속 추가)
- RS 등급: **신규 +10점** (rs_rating 또는 mrs 백분위)

**변경 3: ExitManager 코어 분기 (`scripts/run_trader.py`)**
- stop_loss_pct: 15 → **10%** (조기 손절 강화)
- trailing_stop_pct: 8 → **12%** (느슨한 트레일링, 큰 추세 추종)
- 분할익절 ratio: 0/0/0 (이미 OFF, 유지)
- 효과: SK스퀘어 +38→+30 8%p 회귀 사례 방지, +30% 후 -12% trail까지 보호

**변경 4: stale 임계 단축 (`config/evolved_overrides.yml`)**
- Tier 1 알림: 30 → **20영업일** + ±3%
- Tier 2 자동매도(시간): 45 → **30영업일** + ±3%
- Tier 2 자동매도(조건): 30 → **20영업일** + ±2% + 거래량 50% 미만
- 쿨다운: 7일 → 5일

**변경 5: 격주 리밸런싱 (`kr_scheduler.run_core_holding_scheduler`)**
- rebalance_interval_weeks=2 (월 1회 → 격주 1회)
- last_rebalance_date 기반 트리거 (월별 트래킹과 병존)
- 토요일 갱신 → 박스권 종목 빠른 회수

**기대 효과**
- 오리온/KT급 박스권 종목 진입 자체 차단 (60일+5% 미달)
- 보유 중 박스권 → 5/19~5/30 자동 컷
- SK스퀘어급 추세 종목은 분할익절 OFF로 끝까지 가져감

### 2) P0-1 계측 보강 (수급 델타 영속화)

**배경**: 5/11 자동 cron 검증 → 수급 델타 진입 0건. `trades.entry_reason`에 'buy_signal'만 저장, supply_trend reasons는 메모리에서만 사용. 효과 측정 트레이스 zero. 5/18 재검증 예약(job c8ee3cd0).

**변경**
- `supply_trend.SupplyTrendStock`: `delta_ratio: float = 0.0` 필드 추가
- `_analyze_supply_trend`: 반환 객체에 `delta_ratio=round(delta_ratio, 2)` 주입
- `swing_screener._apply_strategic_overlays`: `candidate.indicators["supply_delta_ratio"]` 영속화 + reasons 표시
- `kr_scheduler._check_orders`: `_sig_metadata['indicators']['supply_delta_ratio']` → `_indicators["supply_delta_ratio"]` 추출 → `trade_journal.record_entry`로 `indicators_at_entry` 영속화

**검증 SQL (5/18 재시도)**
```sql
SELECT t.symbol, t.entry_strategy, t.entry_time::date,
       (t.indicators_at_entry->>'supply_delta_ratio')::float AS delta,
       t.pnl_pct
FROM trades t
WHERE t.market='KR' AND t.entry_time >= '2026-05-11'
  AND (t.indicators_at_entry->>'supply_delta_ratio')::float >= 3
ORDER BY t.entry_time DESC;
```

### 코드 리뷰 결과 (P0/P1/P2)
- **P0**: 0건
- **P1**: 0건 (ATR-linked trailing은 `not is_core` 가드 검증 완료 — 12% trailing 정상 적용)
- **P2 (모니터링)**:
  - 가중치 변경 시 점수 분포 변화로 min_score=70 통과 종목 변동 가능 → 첫 격주 리밸런싱 결과 점검
  - last_rebalance_date 파싱 시 짧은 "YYYY-MM" 형식 폴백 — 안전 try/except 처리됨
  - 새 점수 분포 모니터링: 30영업일 운영 후 통계 비교

## 2026-05-10 — 사전징후 P0-1: 수급 델타 보너스

### 배경
4-전문가 분석(strategy/market/trade/general-purpose) 결과, 급등 사전징후 포착 부족의 핵심 원인은 **수급 1차 미분(가속도)** 누락. 5/4 SK하이닉스 사례:
- 4/30~5/4 외국인 누적 412만주
- 5/4 단독 289만주 → 직전 5일 평균 대비 **약 3.5배 점프**
- 우리 봇은 이를 인식하지 못해 09:21 차단 + 진입가 한도 차단 → +31% 폭등 미포착

### 변경 (`src/signals/strategic/supply_trend.py`)
- `_analyze_supply_trend`에 `delta_ratio` 계산 추가
  - `today_net = foreign_daily[-1] + inst_daily[-1]`
  - `prev_5_avg = mean(직전 5일 외국인+기관)`
  - `delta_ratio = today_net / prev_5_avg`
- `_calculate_trend_score`에 `delta_ratio` 파라미터 추가
  - `≥5x` (폭증): **+30점**
  - `≥3x` (점프): **+20점**
- reasons에 "수급 델타 점프(N.Nx)" 표시

### 효과 가설 (5영업일 + 5건 평가)
- 5/4 SK하이닉스급 시그널 사전 포착 (delta_ratio ~3.5x → +20점)
- 진입 종목 D+1 평균 수익률 비적용 대비 **+0.5%p 개선**
- 외국인 5일 누적 + 델타 점프 조합 시 강한 시그널 (스코어 100 도달 가능)

### 안전 마진
- daily_max -5% 영향 0 (점수 보너스만, 진입 게이트 그대로)
- cross_validator 누적 감점 cap -15 그대로
- 추격매수 hard block(R6 -15) 그대로
- 점수 상승 → 진입 빈도 증가는 max_positions/daily_max_new_buys 한도로 통제

### 미반영 (보류 — 5영업일 평가 후)
- P0-2 장초반 -8 감점 bull 체제 분기
- P1 거래량/가격 다이버전스, BB Squeeze, 호가 체결강도, 섹터 동시 강세
- P2 텔레그램 채널 Phase 1 (수동 forward)

### 검증
- py_compile 통과
- 봇 재시작 정상
- monitoring-checkpoints.md에 5/16 평가 항목 등록

## 2026-05-09 — W19 monitoring 분석 + theme_chasing 5% 자동할당 P0 버그 수정

### 배경
5/9 00:00 weekly_rebalance 실행 후 W19 monitoring 자동 검증(SQL 9건) 분석 결과:
- **P0 버그**: `theme_chasing.enabled:false`인데 LLM rebalance가 **5% 자동 할당**
- 원인: `strategy_evolver._apply_allocation_guardrails`의 `self._config` 미주입 → enabled 체크 분기 무력화 → momentum_breakout만 0% 강제됨

### F-3 효과 검증 (5/4 이후 5영업일, 16건 거래)
| 전략 | n | 승률 | 평균 PnL | 총 손익 |
|------|---|------|---------|---------|
| core_holding | 1 | 100% | +30.22% | +741k (SK스퀘어) |
| sepa_trend | 4 | 75% | +5.05% | +221k |
| strategic_swing | 5 | 60% | +1.09% | +168k |
| gap_and_go | 4 | 75% | +1.63% | +107k |
| rsi2_reversal | 2 | 50% | -0.13% | -8k |
| **합계** | 16 | - | - | **+1,229k** |

→ F-3 자본 회전 + 패널 통합 + 슬리피지 분기 효과 양호.

### 변경

**P0: `_apply_allocation_guardrails` evolved_overrides 직접 읽기**
- `self._config` 미주입 회귀 방어
- `get_evolved_config_manager().get_overrides()` 호출
- enabled:false 전략을 _disabled set에 추가
- self._config 폴백 경로도 보존 (외부 주입 시)

**evolved_overrides.yml 즉시 환원**
- `strategy_allocation.theme_chasing: 5.0 → 0.0`
- `strategy_allocation.sepa_trend: 47.3 → 52.3` (재배분)
- `_meta`: source=manual_review_locked, 5/9 P0 버그 수정 사유 명기

### W19 monitoring 검증 결과 (9건 SQL)
✅ 누적 감점 cap -15: sepa 66.7%/+6.42%, gap/core 100% — 정상
✅ rsi2/gap 1차 익절 4%×0.40: gap 4건 first_tp 2 (50% 도달)
✅ core 트레일링 -8%: SK스퀘어 +30.22% / HD현대일렉 +14.6% 재확인
✅ theme_chasing 폐지 효과: 진입 0건 ✓
✅ SQL 화이트리스트: SELECT/WITH만 통과 (보안 정상)
⚠️ 패널 추천 효과: 추천 1건(+10.15%) vs 비추천 15건(+3.47%) — 표본 부족
⚠️ 슬리피지 SQL: market_regime 컬럼 없음 (DB 스키마 보강 필요, 별도 P1)

### 검증
- py_compile 통과
- 봇 재시작 정상
- 다음 weekly(5/16) theme_chasing 0% 유지 확인 예정

## 2026-05-07 — 코어 stale D안 하이브리드 자동매도 + P0/P1 코드리뷰

### 변경
- `_check_core_stale_alert` D안 하이브리드 자동매도 추가
  - **Tier 1 (알림)**: 30영업일+ ±3% (기존)
  - **Tier 2 (자동매도)**:
    - 시간 기반: 45영업일+ ±3%
    - 조건 기반: 30영업일+ ±2% + 거래량 50% 미만 5일+
- 자동매도 시 SignalEvent.SELL emit (조기경보와 동일 경로)
- 거래량: `broker.get_daily_prices(days=25)` → 당일 제외 후 5/20일 평균 비율
- `config/default.yml core_holding` 신규 6 파라미터

### 코드리뷰 P0/P1 즉시 수정 (4건)

**P0-1: `_pending_sells`/`exclude_symbols` 가드** (batch_analyzer.py)
- `_check_core_stale_alert`에 같은 사이클 중복 SELL 차단
- 호출자(_monitor_core_positions)가 exclude_symbols 전달

**P0-2: 정확한 KR 영업일 계산**
- `int(elapsed_days * 5/7)` 단순 환산 → `_kr_business_days(start, end)` 헬퍼
- `is_kr_market_holiday` 활용 (휴일 정확 반영)
- 자동매도 시점 1주 일찍 당겨지는 회귀 방지

**P0-3: daily_prices 정렬 + 당일 제외**
- 첫 인덱스 date 비교 → 당일이면 offset=1
- `len(volumes) == 20` 검증 강화
- 장 시작 직후 당일 거래량 0 왜곡 회피

**P1-4: rebalance_exclude 화이트리스트**
- 사용자가 `core_holding.rebalance_exclude` 설정한 종목은 자동매도 면제
- 운영자 보호 의지 일관성 (월 리밸런싱 + stale 자동매도 동일)

**P1-1: emit 실패 시 분기 분리**
- 발행 실패 시 텔레그램 재통지 + Tier 1 알림 경로 폴백
- 운영자가 자동매도 실패 인지 보장

### 안전 마진
- daily_max -5% 그대로 (자동매도는 SELL이라 신규 진입 한도 무관)
- ATR 트레일링 -8%, 조기경보 -12%, MA200 그대로
- 모든 안전 장치 4중 보존

### 검증
- py_compile 통과 (3 파일)
- 봇 재시작 정상

## 2026-05-07 — 코어홀딩 stale_alert (자동매도 X, 텔레그램만)

### 배경
strategy-advisor 분석 결과 **core_holding은 모든 stale 청산 로직에서 명시 제외** → ±5% 박스권으로 영원히 머물러도 자동 청산 0건. 자본 묶임 위험.

현 보유 코어:
- 271560 오리온: ±0.5% 박스권 5+영업일 (트리거 전부 휴면)
- 009540 HD한국조선해양: -3.4% (트레일링 미활성)

### 변경
- `src/core/batch_analyzer.py`:
  - 신규 `_check_core_stale_alert(core_positions)` 메서드
  - `_monitor_core_positions` 끝에서 호출
  - 조건: 30영업일+ 보유 + |PnL| ≤ 3% → 텔레그램 경보
  - 알림 후 7일 쿨다운 (재발송 방지)
  - 자동매도 X (사용자 결정 보존)
  - cache: `~/.cache/ai_trader/core_stale_alerts.json`
- `config/default.yml core_holding`:
  - `stale_alert_enabled: true`
  - `core_stale_days: 30`
  - `core_stale_pnl_band_pct: 3.0`
  - `core_stale_cooldown_days: 7`

### 안전 마진 (자동매도 안 함)
- daily_max -5% 영향 0
- ATR 트레일링 -8%, 조기경보 -12%, MA200 이탈 그대로
- 사용자가 알림 받고 다음 월초 리밸런싱에서 결정

### 효과 가설 (3개월 운용)
- H1: alert 발동 종목의 30일 후 PnL이 미발동 코어보다 저조 (Mann-Whitney U test)
- H2: 사용자 청산 결정 시 회수 자본의 다음 코어 후보 진입 IRR > 보유 지속 IRR

### 롤백 트리거
- 발동 종목이 30일 내 신고가 50%+ 돌파 → 임계 너무 빡빡 → 35영업일/±2% 완화
- 사용자 5회 모두 "보유 지속" 선택 → 임계 완화
- 첫 3건 알림 후 손익 악영향 → 비활성화

### 검증
- py_compile 통과
- 봇 재시작 정상

## 2026-05-06 — F-3 코드리뷰 P0/P1 즉시 수정

### 배경
F-3 변경(cc16461) 코드리뷰 결과 P0 2건 + P1 3건 발견. 즉시 수정.

### 변경

**P0-1: `max_daily_new_buys` 데드 키 강제** (`src/risk/manager.py:can_open_position`)
- F-3에 7건 한도 추가했으나 **코드 어디에도 강제 안 됨** (데드 키워드)
- can_open_position에 신규 카운트 로직 추가:
  ```python
  if side == BUY and strategy != core_holding:
      today_buy_count = sum(1 for p in positions if p.entry_time.date() == today)
      if today_buy_count >= config.max_daily_new_buys:
          return False
  ```
- 차단 메시지: "일일 신규 매수 한도 도달 (X/7, 코어 제외)"

**P0-2: capital_check 영속화** (`src/schedulers/kr_scheduler.py:4699-4740`)
- `last_capital_check_date` 인메모리만 → 봇 재시작 시 같은 날 중복 실행 가능
- `_capital_checked_<date>.flag` 파일 추가 (lunchtime과 동일 패턴)
- 실행 시각: 14:00 → **13:50** (sepa 14:30 차단 안전망 정렬)

**P1-3: execute_pending_signals 14:30 SEPA 차단** (`src/core/batch_analyzer.py:840`)
- `sepa_trend.generate_batch_signals`은 14:30 차단하나, pending에 저장된 시그널은 미적용
- 13:50 capital_check이 sepa 시그널을 14:30 윈도우에 강제 진입시킬 위험
- execute_pending_signals 루프 내 추가:
  ```python
  if sig.strategy == "sepa_trend" and now.hour >= 14 and now.minute >= 30:
      skip
  ```

**P1-4: capital_check None 방어**
- `getattr(portfolio, "total_equity", None)` 가드
- equity/cash None 시 warning 로그 + 다음 분에 재시도 가능
- `last_capital_check_date = today`를 성공 분기 안으로 이동 (예외 시 재시도 보장)

**P2-8: 로그 텍스트 잔존 정정**
- "12:30 윈도우" → 실제 시간 반영

### 검증
- py_compile 통과 (3 파일)
- 봇 재시작 정상

## 2026-05-06 — F-3 자본 회전 효율 개편 (한방 적용)

### 배경
4-전문가(engine-monitor + strategy-advisor + param-optimizer + market-analyst) 분석 결과:
- 사용자 우려 일부는 오해 (시그널 매일 가능, weekly = allocation 조정만)
- 일부는 사실: sepa 일평균 0.77건(이상치 1.0~1.5), 빈 평일 7%, 5/6 시점 현금 35.7%
- strategy-advisor 권고: **옵션 F (08:20 후보 큐 + 익절 트리거)** P0
- market-analyst 권고: **12:30 → 13:30 lunchtime 이동** (기관 오후 수급 반영)
- 사용자 결정: **"한방에 F-3"**

### 변경 (5건)

**1. `config/evolved_overrides.yml batch.strategy_limits 한도 확대**
- sepa_trend: 5 → **10** (이상치 일평균 1.0~1.5건 확보)
- rsi2_reversal: 3 → 5
- strategic_swing: 신규 명시 5
- gap_and_go: 2 → 3
- theme_chasing: 0 (폐지 5/4)

**2. `batch.min_score`: 60.0 → 55.0**
- engine-monitor 권고 (시장 수급 약세일 시그널 부족 해소)
- cross_validator 4중 안전망 그대로 → 추격매수 위험 미증가

**3. `batch.daily_max_new_buys`: 5 → 7**
- 자본 회전 가속 (기존 max_positions+flex 8+2=10 그대로)

**4. `batch.lunchtime_scan.time`: '12:30' → '13:30'**
- 기관 오후 수급 반영 시점 (market-analyst 권고)
- 14:00 자본활용률 체크와 30분 간격 두어 단계적 활용

**5. `src/schedulers/kr_scheduler.py` 14:00 자본활용률 체크 task 신규**
- 현금 비율 > 25% 시 `execute_pending_signals()` 자동 호출
- lunchtime_scan(13:30) 후 30분 후 재진입 트리거
- 추격매수 4중 안전망 그대로 (cross_validator -15, max_positions 가중, 14:30 sepa 차단, regime)

### 안전 마진 보존
- daily_max -5% 그대로 (4건 동시손절 4.48% < 5%)
- max_position_pct 28% 그대로
- max_positions 8 + flex 2 = 10 그대로
- cross_validator 누적 감점 cap -15 그대로
- 09:00~09:29 장초반 차단 그대로
- 14:30 이후 sepa 진입 차단 그대로

### 효과 가설 (5영업일 평가)
| 지표 | 현재 | 목표 |
|------|------|------|
| 현금 비중 | 35.7% | 18~20% |
| sepa 비중 | 19.7% | 35~40% |
| 일평균 sepa 진입 | 0.77건 | 1.5~2건 |
| 일평균 swing 진입 | 0.80건 | 1.2~1.5건 |
| 월 추가 수익 | - | +94~141k |

### 롤백 트리거
- 5영업일 daily_max -5% 도달 1회 이상 → 즉시 롤백
- 추격매수 손익비 -10% 악화 → min_score 55→58 단계 환원
- raw 보유 종목 12+ → 14:00 체크 임계 25% → 35% 상향

### 검증
- py_compile 통과
- 봇 재시작 정상 (active)
- monitoring-checkpoints.md에 5/13 평가 항목 등록 예정

## 2026-05-06 — max_positions 잔여 비율 가중 카운트

### 배경
사용자 지적: 1차/2차 익절로 잔여 작아진 포지션도 max_positions 카운트에서 1슬롯 차지 → 자본 충분한데 신규 진입 차단.

분할 익절 단계별 잔여:
- NONE 100% / FIRST 80% / SECOND 48% / THIRD 31% / TRAILING ~31%

### 변경
- `src/risk/manager.py`:
  - 신규 `set_exit_manager(em)` setter
  - 신규 `_get_position_weight(symbol)`: ExitManager의 `remaining_quantity / original_quantity` 비율 반환
    - 0.2 floor (트레일링 잔여라도 최소 1/5 슬롯 — 남용 방지)
    - ExitManager 미연결 시 1.0 폴백 (기존 동작)
  - `can_open_position` 비코어 카운트 변경:
    - 단순 `len(positions) - core_count` → `sum(weight)` 가중 합산
    - 차단 메시지에 raw count 함께 표시 (디버깅 가시성)
- `scripts/run_trader.py:610`:
  - ExitManager 초기화 직후 `risk_manager.set_exit_manager(exit_manager)` 호출
  - 로그: "RiskManager ↔ ExitManager 연결 (max_positions 가중 카운트)"

### 안전 장치
- max 슬롯 8 + flex 2 = **10 그대로 유지**
- 가중 합산이 10 초과 못 함
- 0.2 floor로 트레일링 잔여 무한 진입 방지
- 다른 게이트(현금, daily_max, 섹터, 전략 budget) 그대로

### 효과
- 자본 충분 + 1차 익절 진행된 포지션 다수 시 신규 슬롯 여유 확보
- 예: 5건 보유 중 3건이 FIRST(0.8) → 4.4 슬롯 사용 → 3.6 슬롯 여유 (이전 단순 카운트는 5)

### 검증
- py_compile 통과
- 봇 재시작 정상
- 로그 "RiskManager ↔ ExitManager 연결" 확인

## 2026-05-05 — 슬리피지 체제 분기 (bull 5% / neutral·bear 3%)

### 배경
2026-05-04 추천 10종 80% 적중 / 평균 +5.91% vs 우리 +2.39% → -3.5%p (~90만원) 기회비용. 차단 사유: `max_entry_slippage_pct = 3.0%` 갭업 컷이 강세장 폭등 종목 차단.

### 3-전문가 검증
- **trade-analyst**: "갭업 = 손해" 거짓 (DB 136건). +5~10% 구간 승률 53.8% (vs 전체 46.3%). 단, 점수 95-100점 37.9%/-0.58% + 09:00~09:29 진입 31.3%/-740k 패턴은 데이터 지지.
- **market-analyst**: 갭업 후속은 체제 의존. bull + 거래량 동반 = 추세 시작 신호.
- **strategy-advisor**: bull 한정 완화가 안전 (옵션 ②). 사용자 우려(추격매수 재발)는 bear/neutral에서 발생 → 그 영역은 보수 유지.

### 변경
- `config/evolved_overrides.yml batch.max_entry_slippage_pct`:
  - 단일 float `3.0` → dict `{bull: 5.0, neutral: 3.0, caution: 3.0, bear: 3.0}`
  - bull에서만 +2%p 완화, 나머지는 그대로
- `src/core/batch_analyzer.py:166-185, 308-312`:
  - dict 로드 + 하위 호환 (단일 float도 허용)
  - `_slippage_by_regime` 신규 attribute
  - PendingSignal 생성 시 `self._market_regime` 기반 lookup

### 효과 가설 (5영업일 + 5건 평가)
- bull 레짐 신규 통과 종목 평균 PnL ≥ 0%
- bear/neutral 거래는 변경 전 대비 ±2%p 이내
- 일일 -5% 도달 0~1회

### 안전 마진
- bear/sideways 3% 유지 (사용자 우려 보존)
- cross_validator 추격매수 -15 (등락률/ATR>1.5)는 그대로 — 5/4 삼성증권 +25% 갭은 어차피 차단
- 09:00~09:29 장초반 차단 그대로 (-740k 패턴 데이터)
- 누적 감점 cap -15 그대로

### 롤백 트리거
- bull 갭업 통과 종목 3건 이상 -7%↓ 손절 → 24h 내 환원
- 5영업일 누적 손익비 < 1.0 → bull 5→4%

### 검증
- py_compile 통과
- 봇 재시작 정상
- monitoring-checkpoints.md에 5/12 검증 항목 등록

## 2026-05-04 — theme_chasing 폐지 + 3-에이전트 검증 P0/P1 일괄

### 배경
2026-05-03 11건 변경 후 3-에이전트 통합 검증(risk-auditor + param-optimizer + 코드리뷰).

### theme_chasing 전략 폐지 (param-optimizer 검증 결과)
- **이전 변경(min_score 65→75)이 역효과 판명**:
  - DB 75-80점: 21.4% 승률 / -1.01% (최악 구간)
  - DB 80-85점: 11.1% 승률 / -1.08% (최악2)
  - DB 70-75점(차단되는 구간): 75% 승률(n=4) — 임계가 정반대 작용
- 누적 44건 -300k 손실, 보유 0일 78%, manual 청산 67% — 구조적 부적합
- 1일+ 잔류 4건만 75% 승률 → 점수 아닌 **보유 기간**이 진짜 구분자

**조치**:
- `theme_chasing.enabled: true → false`
- `strategy_allocation.theme_chasing: 5.0 → 0.0`, `sepa_trend: 44.2 → 49.2` (재배분)
- 재활성화 조건: 보유 기간 필터(4일+) 또는 80+ 임계 + 강세 테마장 한정

### 코드리뷰 P0/P1 즉시 수정 (5건)

**P0-A: V자 재진입 1회 제한** (`src/risk/manager.py`)
- 시나리오: 손절 → V자 +5% 재진입 → 재손절 → 또 V자 → 무한 재진입 가능
- daily_max worst case: 1종목 2회 손절 = 2.5%p, 4건 동반 시 6.25% (5% 한도 초과)
- 신규 `_stop_loss_rebound_used` set: V자 재진입 사용 마킹 → 재손절 시 당일 영구 차단

**P1-2: 패널 보너스 side==BUY 분기** (`src/core/cross_validator.py`)
- 매도 시그널에도 패널 보너스가 적용되어 청산 점수 부풀림 → 게이트 통과율 인위 증가
- `is_buy_signal = str(side).upper().endswith("BUY")` 가드 추가

**P1-3: 패널 21일 폐기 + freshness <0.5 보너스 0**
- 14일 후 신선도 0.3 floor에 의해 영구 +2 보너스 → stale 추천이 게이트 우회
- 21일 초과 시 보너스 미적용
- freshness < 0.5 시 보너스 미적용 (시장 상황 변화 반영)

**P1-1: stale lock 단축** (`_load_panel_outlook`)
- 일요 갱신 직후 첫 호출 실패 시 6시간 stale lock → 월요일 패널 미적용
- 실패/None lock 30분으로 단축, 성공 lock은 6시간 유지

**P2-5: panel_risks 빈 시 LLM 가이드 미출력**
- "위 매크로 리스크가..." 문구가 컨텍스트 없이 부유 → 헛된 NO 편향 가능
- `risk_guide` 변수로 조건부 출력

### 토큰 cap 완화 (사용자 요청 2~3배)
- `_build_wiki_context` 5KB → **12KB**
- 전략별 wiki 교훈 600자 → **1200자**
- 직전 주 매도후 복기 1500자 → **3000자**
- 직전 주 monitoring 1200자 → **2500자**
- panel_risks 3건/120자 → **5건/250자**

### 검증
- py_compile 통과 (3개 파일)
- 봇 재시작 정상
- monitoring-checkpoints.md에 검증 항목 등록

### param-optimizer 추가 발견 (모니터링)
- strategic_swing 18.8%: trending_bull에서 28.6% 승률(!) — bull 전환 시 위험. ranging 한정 우수
- 누적 cap -15: 60-75점 67.9% 승률 합산 — 합리적이나 영향 작음 (5건만 cap 적용)
- rsi2 4%×0.40: 단기 회전 적합 검증 (보유 28h vs SEPA 70h)
- rsi2 12.5%: 진입 빈도 변화 효과 제한적

## 2026-05-03 — 전문가 패널 통합 (P0+P1+P2 일괄)

### 배경
사용자 요청: "전문가 패널 분석을 단순 리포트 아닌 전략 활용까지 확대 + 리스크 팩터 반영".

기존 활용:
- swing_screener: 추천 종목 +25점 보너스 (SEPA만)
- kr_scheduler 장중 LLM 검증: 패널 regime으로 통과 기준 차등화

활용 한계:
- kr_screener(일반 스크리너) 미연동 — 모멘텀/테마/RSI2/갭 후보는 보너스 X
- risk_factors 미활용
- LLM regime + 패널 regime 이중 시스템, 우선순위 미정의

### 변경 (`src/core/cross_validator.py`)

**P0: 모든 전략 진입 시 패널 추천 보너스**
- 신규 메서드 `_load_panel_outlook()` (6시간 캐시)
- 규칙 10 추가: `symbol in self._panel_recommended` → `+max(2, conv × 10 × freshness)` 보너스
- swing_screener의 +25점과 별도로, 모든 전략에 일관 적용 (보수 보너스 +5~+10)
- 14일까지 신선도 0.3까지 감소

**P1: risk_factors → LLM 2차 검증 컨텍스트 주입**
- `llm_second_check`에 `📌 주간 매크로 리스크` 섹션 추가
- 상위 3건만 (토큰 절약), 각 120자 제한
- LLM에게 "리스크가 종목/전략에 직접 영향 가능 시 보수적 NO" 가이드

**P2: regime 이중 시스템 보수적 결합**
- 신규 메서드 `_combine_regime(llm_regime)`:
  - llm + panel 모두 bull → bull 유지
  - 둘 중 하나가 bear → bear 우선 (보수)
  - 그 외 neutral
- LLM 2차 검증 프롬프트에 `(LLM+패널 결합={panel_combined_regime})` 표시

### 검증
- py_compile 통과
- 봇 재시작 정상

### 모니터링
- 일요일 21:00 패널 갱신 후 cross_validator가 6시간 내 자동 흡수
- 다음 LLM 2차 검증부터 risk_factors 반영 시작
- 효과 측정: 패널 추천 종목 vs 미추천 종목 진입 후 실현 PnL 비교 (5/9 후속복기)

## 2026-05-03 — 엔진 P0 적용 (감점 cap + 단기 회전 1차 익절)

### 배경
strategy-advisor 엔진 흐름 진단에서 P0 2건 발견. 사용자 승인 후 일괄 적용.

### 변경

**P0-1: cross_validator 누적 감점 cap -15** (`src/core/cross_validator.py`)
- 시간대 -8 + 지표결손 -8 + MA200 -5 + 극단PER -5 = 최대 -26 누적 가능 → 60-70점대 종목 자동 차단(91.7% 승률 영역) 역설
- `TOTAL_PENALTY_CAP=15` 도입, 누적 감점 초과 시 capped
- Hard block 예외 화이트리스트: "추격매수", "RSI과매수", "적자+고PBR" — 단독 차단 의도 보존
- 가설: 60-75점대 차단율 30%↓, 통과 종목 승률 보존

**P0-2: rsi2_reversal/gap_and_go 1차 익절 분기** (`scripts/run_trader.py:_strategy_exit_params`)
- 단기 회전(평균 보유 1.5일) 전략은 SEPA(5-7일)와 같은 5%×0.20 비율 부적합
- rsi2_reversal: first_exit_pct 5.0→**4.0**, first_exit_ratio 0.20→**0.40**
- gap_and_go: first_exit_pct ~2.4→**4.0**, first_exit_ratio 0.20→**0.40**
- 가설: 거래당 평균 PnL +0.3%p 개선, 1차 도달율 증가, 잔여 손절률 감소

### 모니터링 체크포인트 등록
- `docs/operations/monitoring-checkpoints.md` 활성 섹션에 2건 추가 (검증 SQL + 롤백 트리거 포함)
- 5영업일(2026-05-08) 후 1차 평가

### 검증
- py_compile 통과
- 봇 재시작 정상

## 2026-05-03 — Phase 2+3 + 통합 코드리뷰 P0/P1 반영

### 배경
사용자 요청: Phase 2+3 진행 후 상세 코드리뷰. 두 에이전트 통합 리뷰 결과:
- Phase 1+2+3 코드리뷰: P0 3건(SQL 안전성, DB 누수, multi-statement) + P1 7건
- 엔진 전체 흐름 진단: P0 2건(누적 감점 cap, 분할익절 분기) + P1 5건

### Phase 2 신규 — 모니터링 자동 검증
- `src/analytics/monitoring_runner.py` 신규
  - `MonitoringRunner.run_weekly()`:
    - `docs/operations/monitoring-checkpoints.md` 활성 섹션 파싱
    - 활성 체크포인트의 SQL 코드블록 자동 추출 + 실행
    - 결과를 `~/.cache/ai_trader/wiki/monitoring/{iso_year}-W{iso_week:02d}.md`에 영속화
    - 텔레그램 요약 메시지
- `src/schedulers/kr_scheduler.py:4117-4135`
  - 토 09:30 post-exit 직후 monitoring_runner 자동 실행 hook

### Phase 3 — Wiki 컨텍스트에 monitoring 결과 통합
- `src/core/evolution/strategy_evolver.py:_build_wiki_context`
  - 직전 주 `wiki/monitoring/*.md` 추출 (~1200자) 추가
  - 다음 weekly_rebalance LLM 자동 흡수 → 의사결정 루프 닫힘

### 코드리뷰 P0/P1 반영
- **P0-3 SQL 화이트리스트** (`monitoring_runner.py:_execute_checkpoint`)
  - SELECT/WITH 시작만 허용 (DROP/DELETE/TRUNCATE/UPDATE 차단)
  - md 편집 실수로 인한 운영 DB 파괴 방지
- **P0-1 DB 연결 누수 보호**
  - `conn = None` 초기화 + finally close 패턴
  - `asyncio.CancelledError`는 명시적 raise (graceful shutdown 보장)
- **P1-4 monitoring 파일 정렬 mtime → 파일명**
  - `key=lambda p: p.name` (YYYY-WNN.md 정렬 가능)
  - 백업 복원 시 mtime 흐트러짐 방어
- **P1-6 monitoring 실패 텔레그램 알림 격상**
  - logger.warning → logger.error + `_send_error_alert`
  - 운영자가 stale 컨텍스트 인지 가능
- **P2 이모지 제거** (CLAUDE.md 규칙)
  - `❌` → `[ERROR]`

### 검증
- py_compile 3파일 통과
- 봇 재시작 정상

### 미반영 (사용자 결정 대기)
- 엔진 P0-1: cross_validator 누적 감점 cap -15 (60-70점대 종목 차단율 30%↓ 가설)
- 엔진 P0-2: rsi2_reversal/gap_and_go 1차 익절 5%×0.20 → 4%×0.40 (단기 회전 분기)

## 2026-05-03 — Phase 1: Wiki + Post-Exit → Rebalance LLM 컨텍스트 통합

### 배경
사용자 요청: "llm위키와 모니터링이 서로 연동되서 누적하며 리밸런싱 및 우리 전략판단에 도움이 되도록 하자".

기존 진단:
- Trade Wiki는 cross_validator(진입 게이트)에만 활용
- Strategy Evolver(rebalance)는 단순 통계만 보고 LLM 결정 — "왜 부진/양호한지" 인사이트 없음
- weekly_post_exit_*.md 페이지는 작성만 되고 흡수 약속만 있음

3단계 통합 설계 중 Phase 1 즉시 구현 (다음 5/9 weekly_rebalance에서 첫 반영).

### 변경
- `src/core/evolution/strategy_evolver.py`
  - **`_build_wiki_context(strategies)` 신규 메서드**:
    - `~/.cache/ai_trader/wiki/strategies/{strategy}.md`에서 "## 교훈" 섹션 추출 (전략당 ~600자)
    - 직전 주 `weekly_post_exit_*.md`의 LLM 분석 섹션 추출 (~1500자)
    - 마크다운 결합, 최대 5KB
  - **`rebalance_strategy_allocation` 통합**:
    - LLM 호출 직전 wiki_ctx 빌드
    - user_prompt에 "📚 누적 교훈 (Trade Wiki + 직전 주 매도후 복기)" 섹션 추가
  - **system_prompt 가이드 추가**:
    - "통계만 보지 말고 누적 교훈과 함께 판단"
    - "직전 주 매도후 복기 LLM 분석은 단기 신호 보조, 90일 누적 우선"
    - "Wiki 교훈에 명시된 패턴이 있으면 reasoning에 인용"
  - reasoning 필드 의무화: "1주/30일/90일 시계열 비교 + 누적 교훈 인용 명시"

### 검증
- 스모크 테스트: `_build_wiki_context` 2700자 정상 생성 (sepa/swing/rsi2/theme/core 5전략 교훈 + 직전 주 후속복기)
- py_compile 통과
- 봇 재시작 정상

### 미반영 (Phase 2, 3)
- Phase 2: Monitoring 체크포인트 자동 검증 (활성 SQL 자동 실행 → wiki/monitoring/*.md)
- Phase 3: 매일 evolve(20:30)와 cross_validator에도 통합

### 다음 검증
- 5/9 토 00:00 weekly_rebalance에서 LLM reasoning에 "Wiki 교훈 인용" 포함 여부 확인
- monitoring-checkpoints.md 갱신

## 2026-05-03 — 거래 원칙 리포트 분석 후속 + 코드리뷰 P0+P1 반영

### 배경
2026-05-03 주간 거래 원칙 리포트 분석 결과:
- L3 경험 원칙(67% 승률) vs 5/2 LLM rebalance(rsi2 9.5% 감액) 모순
- 단일 시점 표본(4월 7건) 과적합 의심
- theme_chasing 누적 -300k (44건, 승률 34.1%)

3건 권고 일괄 진행 후 통합 코드 리뷰에서 P0 2건 + P1 4건 발견.

### 변경

**A: rebalance LLM 시계열 90일 추가** (`src/core/evolution/strategy_evolver.py`)
- review_period(7) + (30) + **(90)** 3시계열
- system_prompt: 누적 90일 우선, 30일 vs 90일 부호 불일치 시 체제 전환 의심
- reasoning에 1주/30일/90일 비교 명시 의무화
- **P0-2 수정**: `sync_from_db(days=7)` → `sync_from_db(days=90)` (90일 표본 누락 방지)

**B: rsi2_reversal allocation 9.5 → 12.5** (`config/evolved_overrides.yml`)
- sepa_trend 47.2 → 44.2 (합계 100%)
- 근거: 누적 60% 승률 + L3 경험 67%/+1점 — 4월 7건 표본 과적합 보정
- `_meta`에 manual_review + 사유 명기

**C: theme_chasing.min_score 65 → 75** (`config/evolved_overrides.yml`)
- L1 18건 분석: 0일 보유 78%, manual 청산 67%, 1일+ 보유 4건만 승률 75%/+2.14%
- 65점은 테마 검출만 통과되는 낮은 게이트 — 75 상향으로 초기확산 구간(2-4%)+테마확산 5종목+ 강제
- 효과 가설: 거래 빈도 60-70% 감소, 승률 34→45-50%, 평균 PnL +0.5~1.0%p 개선

**P0-1 수정: FILL 라벨링 trade_journal 폴백** (`src/core/engine.py:386-440`)
- `tj.get_open_trades(symbol)` → `tj.get_open_trades()` + symbol 필터 + entry_time DESC 정렬
- 메서드 시그니처 인자 미지원 → TypeError로 폴백 무력화 → 정렬 명시로 최신 trade 보장
- `Fill.avg_price` 데드 폴백 제거 (Fill 데이터클래스에 해당 필드 없음)

**P1-3 수정: V자 반등 통과 시 단축 평가** (`src/risk/manager.py:251-269`)
- stop_loss V자 반등 통과 시 `stop_loss_rebound_passed = True` 플래그
- 다음 `_exited_today` 분기에서 단축 통과 (이미 +5% 재돌파 검증됨)
- 두 게이트 직렬 차단 회귀 방지

**P1-4 수정: 부분 청산 시 `_exited_today` 등록 금지** (`src/risk/manager.py:479-487`)
- `is_full_exit=False`면 등록 안 함
- 잔여분 손절 시 잘못된 기준선(첫 부분 청산가) 사용 방지

### 검증
- py_compile 통과 (3개 파일)
- 봇 재시작 정상 (active)
- 다음 weekly_rebalance(5/9 토 00:00)부터 90일 시계열 자동 반영

### 미반영 (의도적 보류)
- P2-11 (90일 review 빈 경우 system_prompt 동적 약화): 5/9 첫 실행 후 결과 보고 검토
- rsi2_reversal 12.5%: per-position 25%보다 낮아 진입 시 12.5%로 축소된 포지션 — 자본금 충분 (25.4M × 12.5% = 3.18M > 200k min_position_value) 시 정상 작동
- strategic_swing 18.8% bull 편향 가드: 5/9 결과 후 결정

## 2026-05-02 — 통합 코드 리뷰 P0+P1+P2 일괄 반영

### 배경
3-에이전트 통합 코드 리뷰(general-purpose + risk-auditor + engine-monitor)에서 P0 2건, P1 3건, P2 1건 발견. A안(stop_loss V자 반등 재진입 허용) 채택 후 일괄 수정.

### 변경

**P0-1: FILL 라벨링 강건성** (`src/core/engine.py:386-440`)
- 마지막 분할청산 시 portfolio.positions에서 종목이 제거된 후라면 라벨이 "매도"로 폴백되던 문제
- 폴백 체인: portfolio.positions → trade_journal 오픈 trade → fill 객체의 avg_price
- `except Exception: pass` → 좁은 예외(`InvalidOperation, ZeroDivisionError, TypeError, ValueError`) + 디버그 로그
- `from decimal import Decimal, InvalidOperation` 추가

**P0-2: 재진입 A안 — stop_loss 종목 V자 반등 재진입 허용** (`src/risk/manager.py:251-265, 478-518`)
- 신규 메서드 `_check_stop_loss_rebound(symbol, current_price)`:
  - 30분 쿨다운 충족
  - 청산가 대비 +5% 이상 재돌파
- 기존 `_stop_loss_today` 무조건 차단 → 위 조건 충족 시 차단 해제
- 근거: W18 후속복기 stop_loss 24건 중 17건(71%)이 매도 후 +3%↑ 상승

**P1-3: PostExitReviewer 09:00 → 09:30 KST** (`src/schedulers/kr_scheduler.py:4087-4092`)
- weekly_rebalance(00:00) + KIS API 폭주 회피 (70종목 14초+)
- 윈도우: 토 09:30~09:44

**P1-5: data_collector keywords type-only 매칭** (`src/dashboard/data_collector.py:1123-1136`)
- 기존: type 또는 message 부분 매치 → "신규 매수 cooldown" 같은 리스크 메시지 오염
- 변경: type 정확 매치만 (`set in` 검사)

**P2-6: CLAUDE.md 절대 금지 패턴 4건 수정**
- `src/core/evolution/trade_reviewer.py:222`, `:574`
  - `float(t.pnl_pct or 0)` → `float(pnl_pct_val) if pnl_pct_val is not None else 0`
  - `sum(float(t.pnl or 0) for ...)` → `sum(float(t.pnl) for ... if t.pnl is not None)`
- `src/analytics/post_exit_review.py:133-135, 155`
  - `float(r[...] or 0)` 4건 → `float(...) if ... is not None else 0.0` 명시 None 체크

### 검증
- py_compile 통과 (6개 파일)
- 봇 재시작 정상 (active)
- 기존 검증: W18 후속복기 71건 표본은 그대로 유지

### 미반영 (의도적 보류)
- **rsi2_reversal 9.5% 사실상 비활성**: per-position 20% > allocation 9.5%. 다음 weekly_rebalance에서 LLM 자동 조정 기대
- **strategic_swing 18.8% bull 편향 가드**: 5/9 LLM rebalance 결과 검토 후 결정

## 2026-05-02 — 재진입 가격 조건 완화 (V자 반등 포착)

### 배경
주간 매도 후속 복기 (W18) 분석 결과:
- stop_loss 24건 중 17건(71%)이 매도 후 +3%↑ 상승 (KEC +36%, 후성 +35.5%, 롯데쇼핑 +35.3%)
- exit_type별 매도후 평균: stop_loss +9.91%, trailing +9.66%
- 강세장에서 손절 후 V자 반등을 놓치는 패턴 다수

GPT-5.4가 제안한 `stop_reentry_cooldown 5→2영업일`은 실제 코드에 존재하지 않는 파라미터 (LLM 사실 오류). 실제 KR 재진입 로직은:
- 당일 손절 차단 (다음날 자동 해제)
- 당일 청산 후 30분 쿨다운 + 가격 조건 (-3%~+3% 또는 +3% 초과)

### 변경
- `src/risk/manager.py:506-513` — `check_reentry_condition` 가격 조건 완화
  - 눌림/횡보 허용 범위: `-3% ≤ from_exit ≤ 3%` → **`-5% ≤ from_exit ≤ 5%`**
  - 재돌파 임계: `from_exit > 3%` → `from_exit > 5%`
  - 급락 차단 임계: `< -3%` → `< -5%`
  - 영향: 당일 청산 종목의 30분 쿨다운 후 더 넓은 가격대에서 재진입 가능

### 안전 장치
- 당일 한정 변경 (다음날 자동 해제 메커니즘 그대로)
- 30분 쿨다운 유지 (즉시 추격 진입 방지)
- 급락 -5% 미만은 여전히 차단 (추격 손절 방지)

### 검증
- py_compile 통과
- 효과 측정: N≥5 누적 후 stop_loss 24건 → 다음 주 후속복기에서 매도후 추세 변화 확인

## 2026-05-02 — 주간 리밸런싱 Decimal+float TypeError 재발 방지

### 배경
2026-05-02 00:00:10 주간 리밸런싱 실행 중 `unsupported operand type(s) for +=: 'float' and 'decimal.Decimal'` 에러 발생. ISO week 18 상태 저장 후 실패 → 이번 주 리밸런싱 미실행.

### 근본 원인
`trade_reviewer.py:_calculate_max_drawdown`:
- `cumulative = 0` (int) 초기화 후 `cumulative += trade.pnl_pct`
- DB sync 경로에서 `pnl_pct`가 Decimal로 들어오면 첫 iter에서 cumulative가 Decimal로 변환되지만, 일부 trade가 float일 경우 두 번째 iter에서 Decimal+float 충돌.
- `_generate_summary_for_llm`의 `total_pnl = sum(t.pnl for t in trades)` 도 동일 패턴.

### 수정
- `src/core/evolution/trade_reviewer.py:210-230` — `_calculate_max_drawdown`:
  - 초기값 `0` → `0.0` (명시적 float)
  - `cumulative += trade.pnl_pct` → `cumulative += float(trade.pnl_pct or 0)`
- `src/core/evolution/trade_reviewer.py:573` — `_generate_summary_for_llm`:
  - `sum(t.pnl for t in trades)` → `sum(float(t.pnl or 0) for t in trades)`

### 검증
- 스모크 테스트: review_period(7) 8건/win 37.5%/pf 0.38/dd 10.58% / review_period(30) 58건/win 41.4%/pf 1.40/dd 26.56% 모두 정상 계산.
- 봇 재시작 정상.

### 미실행 리밸런싱
- 2026-05-02 ISO week 18 상태가 이미 저장되어 다음 토요일(2026-05-09)까지 자동 재시도 없음.
- 수동 트리거 원하면 `~/.cache/ai_trader/last_rebalance.json` 삭제 후 봇 재시작 권장 (장 마감 후 안전 시간).

## 2026-04-28 — 검증 차단 해소 (좀비 포지션 + allocation 재조정)

### 배경
09:30~09:32 매수 시그널 5건 모두 차단. 진단 결과:
- 010140 삼성중공업 (점수 98→87, LLM soft-reject 통과) → "전략 예산 소진: strategic_swing 한도=2,588,146 사용=2,731,000"
- 005930 삼성전자 (점수 93) → 동일 사유
- 175330 / 032640 / 012750 → 자동 -14점 감점(장초반 -8, 지표결손 -6)으로 50점 미만

근본 원인:
1. 034020 두산에너빌리티 strategic_swing 좀비 포지션 2건 (3/16 55주 + 3/26 28주, 합계 5,940,000+2,884,000=8.82M)이 DB OPEN으로 잔존. 실제 KIS 보유 0주 (3/27 sepa_trend stop_loss 통합 매도 시 strategic_swing이 정리 안 됨)
2. 봇이 일부 포지션의 `pos.strategy` 필드를 "strategic_swing"으로 복원 못 해 사용액을 한도 초과 상태로 인식
3. cross_validator 장초반 -8 페널티가 batch 전략(strategic_swing)에도 적용

### 변경
- **DB cleanup**:
  ```
  UPDATE trades SET exit_time='2026-03-27 15:30:00',
    exit_quantity=entry_quantity, exit_price=entry_price,
    pnl=0, pnl_pct=0, exit_type='sync_reconcile',
    exit_reason='좀비 정리 (KIS 실제 보유 0주, 다른 trade로 청산 통합 추정)'
  WHERE symbol='034020' AND exit_time IS NULL;
  ```
  - 2 rows updated. `exit_type='sync_reconcile'` 사용 (기존 enum 재활용 — `trade_journal._sync_exit_types`에 이미 등록되어 review/evolve 모집단에서 자동 제외)
- `config/evolved_overrides.yml`:
  - `risk_config.strategy_allocation.strategic_swing`: 10.0 → **15.0**
  - `risk_config.strategy_allocation.sepa_trend`: 50.0 → **45.0**
  - 합계 100% 유지 (gap_and_go 10 + theme_chasing 5 + rsi2_reversal 15 + core_holding 10)
  - `_meta` 갱신 (manual_review, timestamp 2026-04-28T14:40:00)
- `src/core/cross_validator.py:134-137`:
  - 09:30~10:30 -8점 페널티 예외에 `strategic_swing` 추가 (배치 T+1 전략은 09:30 진입이 정상)
  - 코드 주석에 "장중 진입 추가 시 재검토 필수" 가드 명기

### 코드 리뷰 P0 반영
- **P0**: `exit_type='cleanup'` 신규 enum 사용 시 `trade_journal._sync_exit_types` 미인식 → strategic_swing 승률 75%→64% 즉시 왜곡. → `sync_reconcile`(기존 enum)으로 재변경하여 회피.
- JSON 저널: 3/16, 3/26은 30일 retention 외 (4/13~ 만 잔존) → 메모리 부활 위험 없음 ✓.

### 데이터 검증
- 4월 KR 거래 통계 (전략별):
  - strategic_swing: 12건 75% 승률 +1.40M
  - core_holding: 6건 67% +0.37M
  - sepa_trend: 17건 65% +0.11M
  - rsi2_reversal: 7건 43% -0.002M
  - theme_chasing: 27건 30% **-0.12M** (5% 캡 유지 정당화)
- daily_max 안전 마진: `min_stop_pct 4.0% × max_position_pct 28% × 4건 동시손절 = 4.48% < 5%` 유지 ✓
- 표본 한계: strategic_swing 12건은 95% CI ±24%p (bull 편향 가능) — `_meta`에 명기

### 미해결 (의도된 동작)
- 장중 자동진입(theme_chasing 강제 매핑) + theme_chasing 5% 잔여 < min_position_value 200k → 0주 차단. 의도된 손실 통제(승률 30%) → 변경 불필요.

## 2026-04-28 — 대시보드 주문 이벤트 매수/매도/익절/손절 구분

### 배경
홈 화면 "주문 이벤트 로그"에서 매수/매도가 모두 "체결" 단일 라벨로만 표시되어, 익절/손절 시각 구분 불가. 사용자 요청으로 4단계 분류 추가.

### 변경
- `src/core/engine.py:386-417` — FILL 이벤트 핸들러 분류 로직
  - BUY 체결 → 라벨 "매수" (badge-blue)
  - SELL 체결 + gross PnL > +0.25% → "익절" (badge-green)
  - SELL 체결 + gross PnL < -0.25% → "손절" (badge-red)
  - SELL 체결 + ±0.25% 이내 → "매도" (badge-gray, 수수료 임계 동가)
  - 메시지에 PnL% 포함 (예: "삼성SDI 매도 5주 @ 614,000 (-4.95%)")
  - OrderSide enum + 문자열 직렬화 양쪽 안전 (`endswith("BUY")` 폴백)
  - avg_price=None/0 폴백 시 "매도" 라벨 + PnL 미표시
- `src/dashboard/data_collector.py:1123-1131` — `get_order_history` keywords에 매수/익절/손절/매도 추가
- `src/dashboard/static/js/dashboard.js:592-672` — 배지 매핑 4타입 추가 + 메시지 색상 분기 (익절 green / 손절 red / 매수 blue / 매도 gray) + "체결" fallback 유지 (재시작 전 잔존 이벤트 호환)
- `src/dashboard/templates/index.html:219-221` — `.badge-purple` `.badge-gray` CSS 정의 추가

### 코드 리뷰 반영 (P1+P2)
- P1-1: OrderSide enum 비교에 문자열 폴백 추가
- P1-2: 수수료 임계값 ±0.25% 도입 (KR 왕복 0.227% 근사)
- P1-3: docstring에 "가중평균 기준 라벨" 명시
- P2-1: `if avg_px is not None and avg_px > 0` (CLAUDE.md "절대 금지 패턴" 준수)
- P2-2: Decimal 일관 사용 (마지막 표시만 float)
- P2-4: typeColors 룩업 단순화 (`typeColors[evtType] || 'badge-blue'`)

### 검증
- py_compile 통과 (engine.py, data_collector.py)
- 봇 재시작 정상 (active)
- 백워드 컴팩트: 재시작 전 잔존 "체결" 이벤트는 dashboard.js fallback으로 처리

## 2026-04-28 — 주간 매도 후속 복기 시스템 (Post-Exit Review)

### 배경
4/27 사용자 분석: 최근 2주간 매도 16건 중 10건이 매도 후 평균 +10.5% 추가 상승, 5건만 -5.8% 회피 성공 (강세장 효과 ~60% 설명). 매도 후 추세를 매주 자동 추적해 전략 진화 사이클에 반영하는 시스템 필요.

### 추가
- `src/analytics/post_exit_review.py` — `PostExitReviewer` 클래스 신규
  - DB `trades`에서 최근 30일 KR 매도 거래 조회 (asyncpg pool 활용)
  - 종목별 KIS `get_quote()` 호출 (rate limit 0.2s sleep)
  - 매도 후 변동률 계산 + 분류 (놓침 ≥+3%, 회피 ≤-3%, 타당)
  - 전략 × exit_type 매트릭스 집계 (count, missed, avoided, avg_post_exit, avg_pnl_pct)
  - GPT-5.4 (LLMTask.STRATEGY_ANALYSIS, fallback Gemini Pro)로 인과 추론 + 검증 가능 가설 생성
  - JSON 리포트: `~/.cache/ai_trader/journal/post_exit_review_YYYYMMDD.json`
  - Wiki 페이지: `~/.cache/ai_trader/wiki/weekly_post_exit_YYYY-WNN.md` → 다음 weekly rebalance 시 LLM 컨텍스트로 자동 흡수
  - 텔레그램 리포트: Top 5 놓침/회피 + 전략별 평균 + LLM 인사이트
- `src/schedulers/kr_scheduler.py`
  - `run_post_exit_review_scheduler()` 메서드 추가 (토요일 09:00 KST 실행)
  - ISO week 기반 중복 실행 방지 (`last_post_exit_review.json` 영속화)
  - `create_tasks()`에 `kr_post_exit_review` 등록

### 안전 장치
- 표본 < 5건 시 LLM 호출 스킵 (결론 도출 불가)
- 거래정지/상폐 종목 시세 0 시 skip + 로그
- API 실패 시 종목별 retry 후 skip (전체 중단 방지)
- 진화 시스템 1건 변경 원칙 — LLM 출력 강제 가이드라인에 명시

### 검증
- 스모크 테스트: DB 77건 조회, 분류/집계/저장/위키/텔레그램 포맷 모두 정상
- py_compile 통과 (post_exit_review.py, kr_scheduler.py)
- 봇 재시작 후 `kr_post_exit_review` 태스크 정상 등록

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

## 2026-04-25 — 종목 발굴/진입 타이밍 P0+P1+P2 일괄 (3인 합동 분석 기반)

### 배경
3인 전문가(strategy-advisor/trade-analyst/market-analyst) 합동 분석 결과:
- 30일 거래에서 09시 진입 -440k (승률 26.7%, 76%가 theme_chasing)
- 12시 진입 +1.08M (승률 69.6%, strategic_swing 위주)
- 60-70점 sepa_trend 91.7% 승률 / 80-90점 31.8% 승률 역설
- 단기/중기 보유(<6h) -2.96M / 장기(>6h) +2.97M 단조성

### 수정 (5건)

**P0-1. 진입 시간대 가드** (`src/core/cross_validator.py`)
- 09:00~09:29: 모든 매수 신호 하드 차단 (core_holding 제외)
- 09:30~10:30: -8점 페널티 (변동성 방향 미확정)
- 12:30~13:00: +5 보너스 (점심 sweet spot)
- 기대: 월 +200~300k

**P0-2. 12시 sweet spot 보너스** (P0-1과 같은 변경에 포함)

**P1-1. SEPA 90+ 추격매수 페널티**
- sepa_trend 90점 이상 진입 시 -10점 페널티
- 근거: 80~90점 구간 -636k vs 60~70점 +937k 데이터 역설

**P1-2. 보유시간 정책 완화** (`config/evolved_overrides.yml`)
- `exit_manager.min_stop_pct` 3.5 → 4.5 상향
- 단기/중기 노이즈 손절 차단

**P2. 외국인 5일 누적 매수 상위 섹터 overlay**
- cross_validator 규칙 5-2 추가: `metadata["foreign_top_sectors"]`에 sector 포함 시 +5점
- (upstream 데이터 wiring은 후속 작업으로, 룰만 dormant 활성)

### 후속 (남은 P2 항목)
- 매크로 캘린더 (FOMC/한은/옵션만기) max_daily_new_buys=1 — 데이터 소스 결정 후
- DART 블록딜 공시 진입 차단 — RSS 파서 확장 필요
- 외국인 섹터 데이터 metadata 주입 (현재 룰은 활성화돼 있으나 입력 데이터 없음)

### 합산 기대 효과
월 +500k ~ +1M 개선 (3인 분석 기반 추정).

## 2026-04-24 — 당일 청산 쿨다운 오탐 제거 (손실청산 + Net Loss 게이트)

### 배경
기존 `daily_exit_cooldown_threshold=3` 규칙은 청산 종류 무관 카운트 → 수익 익절 3건으로도 신규 매수 전면 차단. 오늘(4/24) 실제 증상:
- 09:00/09:18/09:51 청산 3건 (PnL +1.58% 수익 중)
- 10:03~10:34 8건 유효 시그널 차단 (086790, 034020, 009540 등)
- 현금 19% 유휴 상태 기회 상실

4/14 -8.42% 사고 방지라는 원 의도(손실 연쇄 시 복구매수 차단)는 유지하되, 수익 상태 오탐 제거.

### 수정 (src/risk/manager.py + src/schedulers/kr_scheduler.py)
1. **`record_exit(exit_type=...)` 시그니처 확장**
   - `_LOSS_EXIT_TYPES = {"stop_loss", "breakeven"}` 클래스 상수
   - `is_new_exit AND is_loss_exit` 일 때만 `_daily_exit_count` 증가
   - 익절/트레일링/stale은 디버그 로그만, 카운트 제외
2. **`can_open_position` 쿨다운 체크 조건 확장**
   - 기존: `_daily_exit_count >= threshold` → 차단
   - 신규: `_daily_exit_count >= threshold AND daily_pnl_pct < -1%` → 차단
   - 수익 상태(≥-1%)면 쿨다운 도달해도 `매수 허용` 로그 후 통과
3. **호출자 업데이트** (`kr_scheduler.py` 2곳)
   - 정상 fill 경로 + DB 직접 기록 경로 모두 `exit_type=_etype` 전달

### 보존 로직
- `_exited_today` 동일 심볼 재진입 제한(30분 쿨다운)은 그대로
- 분할매도 중복 카운트 방지는 그대로 (심볼별 최초 1회)

## 2026-04-23 (후속 7) — 대시보드 Mobile-First v2 (70% 모바일 트래픽 최적화)

### 배경
모바일 사용 비중 70%, 데스크톱 20%. 기존 대시보드는 데스크톱 중심 설계 + 기본 반응형 땜빵만 적용돼 있어 모바일 UX가 저해됨. 페이지별 리디자인.

### 구현 (P1~P5 일괄)
- **신규 파일**:
  - `src/dashboard/static/css/mobile-v2.css` (300+줄) — 모바일 전용 오버라이드
  - `src/dashboard/static/js/mobile-v2.js` (330+줄) — 동적 UI 구성
- **서버 주입**: `server.py:_serve_page`에서 모든 HTML 응답에 `<link>` + `<script>` 자동 주입 + viewport meta `viewport-fit=cover`

### P1. 하단 Fixed Navigation + 스티키 요약 바
- 모바일에서만 상단 pill 네비 숨김, 하단 고정 5탭 nav (홈/거래/성과/테마/엔진) + safe-area-inset-bottom 대응
- 스티키 요약 바 (홈 전용): 총자산/오늘%/포지션 수 — 스크롤해도 항상 노출
- 44×44px 최소 터치 타겟 + 활성 탭 gradient 하이라이트

### P1-C. 홈 Quick KPI 그리드
- 2×2 카드: 총자산 / 오늘 손익 / 일일 리스크 / 포지션
- 10초마다 `/api/portfolio` + `/api/risk` 자동 갱신
- 기존 `.mc-equity-val`은 모바일 홈에서 숨김 (Quick KPI와 중복)

### P1-D. 포지션 스와이프 카드
- 가로 스크롤 스냅 (scroll-snap-type: x mandatory)
- 각 카드: 종목명 + 손익률 + 수량·전략
- 데스크톱 포지션 테이블은 모바일에선 숨김

### P2. Trades 카드 스택
- 기존 테이블 tbody를 MutationObserver로 감시, 카드 스택으로 재렌더
- 매수/매도 컬러 코딩 (cyan/red), PnL 오른쪽 배치
- 모바일에서 원본 테이블 `:has(#trades-body)` CSS로 숨김

### P3. Performance 탭 분할
- 섹션 헤더 텍스트 자동 감지 (요약/전략별/일별/차트) → 탭 버튼 생성
- 모바일 전용, `.perf-section.mv2-active`만 노출
- 라운드 탭 버튼 + 활성 하이라이트

### P4. 터치 타겟 / 폰트 강화
- 인터랙티브 요소 모두 `min-height: 44px`
- 핵심 라벨 최소 `0.7rem` 보장
- `.hide-scrollbar` 유틸 추가

### P5. Skeleton Loading
- `.mv2-skel` 클래스 — 로드 전 shimmer 애니메이션
- 데이터 바인딩 시 자동 제거

### 데스크톱 무영향
- 모든 규칙 `@media (max-width: 768px)` scoped → 20% 데스크톱 사용자는 기존 UI 그대로 유지

## 2026-04-23 (후속 6) — 시스템 리소스 모니터링 (인프라 다운사이징 검토용)

### 배경
AWS Lightsail $24 플랜 사용 중 — 오버스펙 여부 검토 위해 CPU/RAM/Disk 사용량 히스토리 수집.

### 구성
- `sysstat` 설치·활성화 (이미 설치돼 있었음, retention 7→31일로 확장)
- cron으로 10분 간격 자동 샘플링 (이미 활성)
- 새 API: `src/dashboard/system_api.py`
  - GET `/api/system/resources` — 현재 상태 + 최근 N일 p50/p95/peak + 다운사이징 힌트
  - GET `/api/system/history?metric=cpu|mem&days=N` — 시계열 (최대 500샘플)
- 대시보드 `엔진` 탭에 "시스템 리소스" 섹션 추가 (3카드: 현재/7일통계/다운사이징 힌트)
- 자동 힌트 룰 (보수적):
  - CPU: p95<30% AND peak<70% AND vCPU≥2 → 1 vCPU 감축 검토
  - Memory: p95<50% AND peak<60% → RAM 한 단계 감축 검토
  - Memory: peak>85% → 현 스펙 유지 권장 (OOM 리스크)
  - Disk: 사용률<40% → 더 작은 SSD 검토

### 초기 관측 결과 (지난 7일, 533 샘플)
- CPU: avg 4.8% / p95 11.8% / peak 28.4% — **과다 여유**
- Memory: avg 30.5% / p95 37.1% / peak 41.2% — 여유 충분
- Disk: 13.4% (10.2/76.4GB) — 극심 저사용

### 후속
- 1~2주 추가 관측 → RAM 감축 가능 여부 최종 판단 (현재 4GB → 2GB 시 peak 환산 ~82% 위험)
- Lightsail 플랜 비교: 현재 2vCPU/4GB/80GB vs $14 (1vCPU/2GB/60GB)

## 2026-04-23 (후속 5) — 포지션 교체 로직 + YAML 튜닝 이관

### B. 포지션 만석 자동 교체 (engine.py)
- G3_risk "최대 포지션 수 도달(8/8)" 차단 시 가장 약한 비코어 포지션 자동 매도 시그널 발행
- 선정 기준:
  1. core_holding 제외 (코어 보호)
  2. 수익 중인 포지션 제외 (승자 킬 금지)
  3. pending 제외 (이미 청산 진행 중)
  4. 10분 쿨다운 (같은 포지션 반복 축출 방지)
  5. 진입 점수 낮은 순(1순위) + 손실 큰 순(2순위)
  6. 신규 점수가 기존 점수 대비 최소 +5점 우위 필요
- 매도 시그널만 발행 → 다음 screening cycle에서 새 후보 자연 진입 (TOCTOU 안전)
- 근거: 거래분석 — 포지션 만석 차단 20건 재진입 시 avg +0.95% (기회비용 제한적이지만 자동 교체로 확장 가능)

### C. YAML 튜닝 이관 (config/default.yml + engine.py + cross_validator.py)
- `kr.validator` 섹션 신설 — 9개 튜닝 파라미터 YAML 토글 가능
  - `min_pass_score`, `missing_indicator_penalty_step/cap`, `llm_daily_max`
  - `llm_check_score_min`, `llm_bypass_score`, `llm_reject_size_mult`
  - `replacement_min_score`, `replacement_cooldown_sec`
- `CrossStrategyValidator.__init__` kwargs로 주입
- `RiskManager.__init__`에 `validator_config` kwarg 추가, run_trader.py가 YAML 읽어 전달
- 결과: 진화 시스템이 이 값들을 자가 학습 가능 (`evolved_overrides.yml`로 override 가능)

### 검증 (재시작 후 로그)
- `[KR] 엔진 리스크 매니저 등록 완료 (validator: LLM 범위=85~95, 교체 임계=85)` ← YAML 로드 확인

## 2026-04-23 (후속 4) — PER/PBR 파이프라인 연결 (R8 활성화)

### 수정
- `src/signals/screener/kr_screener.py` `ScreenedStock`에 `per`, `pbr` Optional[float] 필드 추가
- `fetch_batch_valuations()` 결과를 각 ScreenedStock에 보존
- `src/schedulers/kr_scheduler.py` live_screening signal metadata.indicators에 per/pbr 주입

### 결과 (기대)
- R8(적자+고PBR, PER>50 감점) 규칙 활성화
- 지표결손 감점 완전 제거 예상 (ATR + 수급 + PER/PBR 모두 공급)

## 2026-04-23 (후속 3) — 수급 데이터 파이프라인 연결 (R2 활성화)

### 배경
지난 리뷰에서 수급(기관/외국인) 지표 결손률 78.6%로 cross_validator R2(기관+외국인 동시 순매도 차단) 규칙이 사실상 비활성 상태. 이번 작업으로 파이프라인 연결.

### 수정
1. **theme_chasing**: 시그널 metadata.indicators에 `foreign_net_buy`/`inst_net_buy` 주입
   - `_foreign_cache`, `_institution_cache`에서 실시간 값 조회
   - theme_chasing은 원래 수급 데이터를 자체 봤지만 신호 external 전달이 누락됨 → cross_validator가 보지 못함
2. **kr_scheduler live_screening**: 시그널 metadata.indicators에 수급 주입
   - `~/.cache/ai_trader/supply_demand_YYYYMMDD.json` 로드 → per-symbol `foreign_net_buy`/`inst_net_buy` 전달
   - 수급 캐시는 `run_supply_demand_cache()`가 5분 주기로 저장 (78종목)

### 결과 (기대)
- R2 규칙 활성: 기관+외국인 동시 순매도 신호가 정확히 차단
- 지표결손 감점 빈도 추가 감소 (ATR에 이어 수급도 결손 리스트에서 제외)

### 남은 후속
- PER/PBR 파이프라인 (R8 활성화) — ScreenedStock에 필드 추가 필요 (edge-case 영향)

## 2026-04-23 (후속 2) — 지표 결손 근본 해결 + 매직넘버 상수화

### 수정
1. **BaseStrategy ATR 계산 추가** (`src/strategies/base.py`)
   - `_calculate_atr_pct()` 메서드 신설 (True Range 평균 / 현재가 × 100)
   - `_calculate_indicators()`에서 history ≥ 15 시 ATR_14 계산 → `indicators["atr_14"]` + `indicators["atr_pct"]`
   - 결과: 시그널 생성 시 ATR 자동 포함 → R6(추격매수 감지) 규칙 활성

2. **cross_validator 지표 결손 체크에 metadata fallback** (`src/core/cross_validator.py`)
   - 기존: `indicators.get("atr_pct")` 만 확인 → 스크리너가 top-level metadata에 세팅한 ATR 놓침
   - 수정: `indicators.get("atr_pct")` OR `metadata.get("atr_pct")` 양쪽 체크
   - PER/PBR, 수급도 동일 패턴 적용

3. **theme_chasing 매직넘버 config 화** (`src/strategies/kr/theme_chasing.py`)
   - `high_score_threshold: 90.0`, `high_score_size_mult: 0.5` 추가
   - 진화 시스템이 자가 학습으로 조정 가능

4. **cross_validator 매직넘버 상수화** (`src/core/cross_validator.py`)
   - `_MISSING_IND_PENALTY_STEP: 2`, `_MISSING_IND_PENALTY_CAP: 8` 상수

5. **engine.py G4_llm 매직넘버 상수화** (`src/core/engine.py`)
   - `self._LLM_CHECK_MIN = 85`, `self._LLM_BYPASS_AT = 95`, `self._LLM_REJECT_SIZE_MULT = 0.5`

### 남은 후속 과제
- PER/PBR: KIS market_data 프로바이더 pipeline 연결 필요
- 수급(기관/외국인): KR 전용, 별도 프로바이더 연결
- 클래스 상수들을 YAML 토글로 완전 이관 (진화 시스템과 연동 강화)

## 2026-04-23 (후속) — 지표 결손 근본 해결 (ATR 계산 BaseStrategy에 탑재)

### 배경
P0+P1 6건 수정 후 후속: cross_validator에 "지표 결손 시 -2점×개수, 최대 -8점" 감점 로직은 **임시 방편**이었음. 근본 문제는 BaseStrategy._calculate_indicators가 ATR을 계산하지 않아 78.6% 시그널이 R6(추격매수) 규칙을 우회하던 것.

### 수정
- `src/strategies/base.py`:
  - `_calculate_atr_pct()` 메서드 신설 (True Range 평균 / 현재가 × 100)
  - `_calculate_indicators()`에서 history ≥ 15 시 ATR_14 계산 → `indicators["atr_14"]` + `indicators["atr_pct"]`
  - cross_validator가 양쪽 키 모두 참조하므로 호환성 확보
- 결과: 시그널 생성 시 ATR 자동 포함 → R6(추격매수 감지) 규칙 활성 → 지표 결손 감점 빈도 감소 예상

### 관찰 지표
- 재시작 후 "지표결손(ATR,...)" 감점 빈도 감소 여부 (7일 모니터링)
- R6 "추격매수(등락/ATR=X.Xx) -15" 로그 발생률 증가 여부

### 남은 후속 과제
- PER/PBR: KIS market_data 프로바이더에서 제공 중이나 BaseStrategy에 pipeline 미연결
- 수급(기관/외국인): KR 전용, 별도 프로바이더 연결 필요
- 매직넘버 YAML 토글화 (0.5 / 85 / 90 / 95 / -2·-8 등)

## 2026-04-23 — 주문 파이프라인 전면 개선 (P0+P1 6건 일괄)

### 배경
3명 합동 리뷰(strategy-advisor/risk-auditor/trade-analyst) + general-purpose 코드 재리뷰 결과, 매수 시그널 게이트 파이프라인에서 수익성을 저해하는 6개 이슈 발견. 월간 +200k~350k원 수익 방어 목적으로 일괄 조치.

### P0 수정

#### 1. `src/strategies/kr/theme_chasing.py` — 시간대 필터 + 고점수 사이즈 축소
- 09:00~09:30 진입 차단 추가 (기존 14:00+ 차단은 유지)
- score≥90 시 `_pos_mult *= 0.5` 사이즈 50% 축소
- 근거: 거래분석 — 09시/14시 진입 각 2건 0승 전패, 고점수(≥85) avg -0.82% (고점수가 더 손실)

#### 2. `src/core/cross_validator.py` — 지표 결손 보수적 감점
- ATR/PER/PBR/수급(KR) 결손 시 -2점 × 결손 개수, 최대 -8점
- 근거: 지표 결손률 78~88% → R6/R7/R8/R2 사실상 비활성 상태 교정
- 장기 해결(screener 지표 필수 공급)은 후속 작업

#### 3. `src/core/engine.py:can_open_position` — 섹터 TOCTOU 수정
- `_pending_sector_map`의 pending 종목 섹터를 카운트에 포함
- 근거: 동일 섹터 2종목이 동시에 pending 상태일 때 `max_positions_per_sector=2` 우회 가능했음

### P1 수정

#### 4. `src/core/engine.py:_log_sig` — adjusted_score 로깅 버그 수정
- 기존엔 `event.score`를 mutation 후 `_log_sig` 호출 → DB의 `score`와 `adjusted_score` 둘 다 감점 후 값이 기록돼 원본 소실
- `original_score` 인자 추가 + `metadata["original_score"]` 폴백 로직
- G2_cross penalized/blocked 호출 시 원본+조정 모두 전달
- 검증: 재시작 후 DB에서 `score=83.65, adjusted_score=72.65` (-11점) 정확히 분리 기록 확인

#### 5. `src/core/engine.py` — G4_llm 재설계 (차단→축소)
- 변경 전: score≥85 & 비강세장 → LLM 호출, 거부 시 차단
- 변경 후:
  - score≥95: LLM 우회 (강한 모멘텀은 검증 건너뜀)
  - 85≤score<95: LLM 호출, 거부 시 position_multiplier × 0.5로 완화 (차단 X)
- 근거: trade-analyst — LLM 차단 40건이 재진입 시 avg +5.72% (차단이 기회 손실)
- 방어: SK하이닉스·하나금융 등 93~99점 강한 종목을 LLM이 잘못 차단하던 패턴 제거

#### 6. `src/core/cross_validator.py` — LLM 일일 한도 소진 로그 승격
- debug → warning 로그 승격 (기존엔 조용히 통과되어 보호 상실 인지 지연)

### 코드 리뷰 보강 (general-purpose agent)

- **metadata None 방어**: `event.signal.metadata is None` 시에도 dict 초기화 후 축소 적용 (치명 P1)
- 하드코딩 매직넘버(0.5/85/90/95/결손 2·8)는 후속 YAML 토글 대상으로 기록

### 관찰 지표 (1주일)
- G2 감점 통과율 변화 (결손 -8 전/후)
- G4 "soft-reject 축소" 트리거 후 승률 (기존 차단 후 재진입 +5.72% 대비)
- theme_chasing score≥90 진입의 승률/평균손익 (고점수 축소가 수익 개선 기여?)

### 수정 목록
- `src/strategies/kr/theme_chasing.py` — 시간대 필터 + 고점수 사이즈 축소
- `src/core/cross_validator.py` — 지표 결손 감점 + LLM 한도 로그 승격
- `src/core/engine.py` — _log_sig original_score, 섹터 TOCTOU, G4_llm 재설계

## 2026-04-22 (후속 2) — US 엔진 및 대시보드 UI 당분간 정지

### 배경
사용자 요청: "미국 엔진 당분간 기능 정지, 대시보드 US 관련 전부 숨김. 미국 투자 전략과 매수매도도 전부 정지."

### 수정
- systemd 유닛 `qwq-ai-trader.service`: `--market both` → `--market kr`
  - US 엔진/전략/브로커/WS/스크리너 전부 미초기화
  - US 주문/체결 경로 자동 비활성
- `src/dashboard/server.py`:
  - `self.us_engine is None` 시 모든 서빙 HTML 응답에 `</head>` 앞에 CSS/JS snippet 주입
  - US 관련 DOM 요소 전역 숨김 (IDs: `us-market-card`, `us-positions-full`, `us-trades-section`, `us-themes-section`, `us-performance-section`, `us-*-body`, `.mf-btn[data-val="us"/"all"]`, `.nav-pill[data-page="us"]`, `[data-market="us"]`, `.card:has(#cfg-us-market)` 등)
  - markets-grid를 1열로 전환하여 KR 카드가 전폭 확장
  - `window.US_ENABLED=false` 설정 + `loadUSData` noop 덮어쓰기 + `localStorage.market_filter=us|all` → `kr` 강제

### 되돌리기
- systemd 유닛 `--market kr` → `--market both`, `daemon-reload` + restart
- 서버 코드 변경 없음 (`us_engine`이 주입되면 자동 감지하여 snippet 미주입)

### 유의
- 기존 US 포지션 8건은 **수동 관리 필요** (봇이 자동 청산/트레일링 하지 않음)
- US 오버나이트 시세는 여전히 `kr_theme_detector`에서 KR 테마 점수 보정용으로 사용 (KR 측 분석 입력)

## 2026-04-22 (후속) — KIS WebSocket 즉시 끊김 서킷브레이커 수정

### 배경
오늘 09:00~14:10 (KR 정규장 거의 전부) 동안 WS가 5초 간격으로 연결→즉시 close_code=1006→재연결을 **3,685회 반복**. MEMORY에는 "approval_key 1개 제약 — KR↔US 시간 분리"로 mitigation 기록돼 있으나 당일 US WS는 꺼져 있었고 재접속 전에 서킷브레이커(3회 연속 즉시 끊김 → approval_key 강제 재발급)가 **한 번도 동작하지 않음**.

### 근본 원인
`_message_count == _msg_before`로 "즉시 끊김"을 판정하는데, KIS 서버가 연결을 버리기 전에 구독 ack/에러 응답(TEXT message) 1건이라도 보내면 `_message_count` 증가 → 카운터 reset → 서킷브레이커 영영 미동작. 실제 시세 데이터는 0건이지만 control 메시지 때문에 탐지 실패.

### 수정
- `src/data/feeds/kis_websocket.py` run 루프:
  - 즉시 끊김 판정을 `_price_data_count`(실제 시세 데이터)로 변경. 구독 ack는 반영 안 됨.
  - 연결 시작 시간 기록 → `duration < 10s AND 시세 0건`을 "즉시 끊김"으로 판정.
  - 서킷브레이커 발동 시 30초 sleep 추가 (KIS rate limit 회피).
  - 로그에 `duration=Xs, 시세수신=N건` 추가하여 진단 용이.

### 효과
오늘 같은 loop 발생 시 최대 15초 내 서킷브레이커 동작 → approval_key 강제 재발급 → 30초 대기 후 재연결. 추가 재발 방지.

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
