# 외부 API 연동

> **23차 토스 WS(10-01, 로컬 미배포):** `orderbook_stream`은 인증된 전용 소켓의 소유권을 주입받는 한정 관측기다. 연결/토큰 발급 없이 국내 호가 full-replace1회·ACK 대조·최우선 호가·종료를 처리한다. 통합 KRX+NXT/LOSSY는 별도 후보 품질 보고에만 사용하고 KIS 호가/주문 경로에 투입하지 않는다. [선정·진입 규약23차](../research/current-engine-selection-entry-protocol-2026-09-30.md) 참조.

> **15차 관측 수명(10-01):** 명시 Runner 설치가 정지 feed의 기존 관측 조정기를 사용한다. 관측 종료는 후보 lease 수요만 닫고 운영 PRICE/BOOK·미확인 등록을 보존한다. 이미 전송 중인 요청은 해제 완료로 바꾸지 않으며 정확한 해제 ACK 허용 집합은 여전히 기본 빈 값이다. 새 세션/운영 API 호출을 이번 검증에서 실행하지 않았다.

> 최종 갱신: 2026-09-29 (접수 불명 분리·주문 CLI 거부·OrderRef·KOSPI 069500 대체 배포 반영). 토스 제한 관측은 09-22 18:00 KST grant 만료 뒤 서비스 disabled/inactive(재발급·퇴역은 사용자 결정 대기), 거래 소비자 미연결.


> **일별 체결 조회 완결 판정 (2026-09-29, 06:45 KST 배포 main `081ab6a`):** `KISBroker.get_fills_for_date_checked(d)` 는 `_query_daily_fills(…, status={})` 로
> 조회 완결 여부를 함께 돌려준다 — 완결 = 모든 페이지 `rt_cd=="0"` 이고 마지막 응답 헤더 `tr_cont` 가 D/E(10번째 페이지 포함).
> F/M 인데 ctx 가 비었거나(`contradictory_continuation`) 같은 ctx 반복(`repeated_ctx`), 헤더 없음(`missing_tr_cont`), 10페이지 상한(`page_cap`),
> 미연결·`rt_cd` 실패·예외·행 정규화 실패는 미완. 기본 호출(`status=None`)은 요청·반환이 이전과 같다(`check_fills` 무변경).
> 소비자는 초과수익 원장의 20:30 거래일 기록 대사다(설계 A §5-1).

## 브로커 — KIS (한국투자증권)

**2026-10-01 작업 브랜치 — WS 호가 관측 입력 정정(미배포):** `kis_websocket`의 H0STASP0/H0NXASP0 최우선 잔량을 0-based 23/33으로 수정했다. 기존 4/14는 2호가 가격이다. [KIS 공식 KRX 필드](https://raw.githubusercontent.com/koreainvestment/open-trading-api/main/examples_llm/domestic_stock/asking_price_krx/asking_price_krx.py), [공식 NXT 필드](https://raw.githubusercontent.com/koreainvestment/open-trading-api/main/examples_llm/domestic_stock/asking_price_nxt/asking_price_nxt.py)를 10-01 확인했다. TR·BSOP_HOUR 원문·HOUR_CLS_CODE·프레임 건수와 aware 수신시각을 metadata로 보존한다. HHMMSS의 거래일은 추정하지 않으며 source_as_of=null이다. 복수 레코드 분할·세션/VI 확정은 미구현, 기존 Event.timestamp/체결가/REST 매도호가 경로는 변경하지 않았다. run_trader에 기본 None 관측기용 콜백만 추가했으며 추가 구독·조회·운영 수집은 없다. 과거 손실과의 인과관계를 입증한 수정이 아니다.

### KR (src/execution/broker/kis_kr.py)
- 실시간 호가, 일봉/분봉 캔들
- 주문 실행 (매수/매도), 체결 확인
- 포지션/잔고 조회
- 넥스트장/프리장 시세 (FHPST02300000)
- **구/신 TR 전환 스위치** (`KIS_TR_SET`, 2026-09-21): 아래 5종만 구/신 두 벌을 갖는다.
  기본값 `legacy`(env 미설정)는 전환 전과 요청 본문·헤더·파싱이 완전히 같다.

  | 용도 | 구 TR (기본) | 신 TR (`KIS_TR_SET=new`) | 호출 지점 |
  |------|------|------|------|
  | 주식주문(현금) 매수 | `TTTC0802U` | `TTTC0012U` | `_get_tr_id_for_session` |
  | 주식주문(현금) 매도 | `TTTC0801U` | `TTTC0011U` | `_get_tr_id_for_session` |
  | 주식주문(정정취소) | `TTTC0803U` | `TTTC0013U` | `cancel_order` / `modify_order` |
  | 주식일별주문체결조회 | `TTTC8001R` | `TTTC0081R` | `_query_daily_fills` |
  | 주식정정취소가능주문조회 | `TTTC8036R` | `TTTC0084R` | `get_exchange_open_orders` |

  잔고 `TTTC8434R`·매수가능 `TTTC8908R`은 저장소에 구/신 구분이 없어 **변경 대상이 아니다**.
  `new` 모드에서만 order-cash 본문에 `EXCG_ID_DVSN_CD="KRX"`·`CNDT_PRIC=""`, 정정취소 본문에
  `EXCG_ID_DVSN_CD="KRX"`가 추가되고, 정정취소가능조회는 `rmn_qty`가 **없거나(None) 비어
  있으면**(공백 제거 후 빈 문자열) `psbl_qty`를 읽는다 (대체 뒤에도 비어 있으면 조용한 0 대신
  `None`=판단 불가). 구 TR 이 `EXCG_ID_DVSN_CD`를 받아들이는지는
  미확인이라 `legacy` 본문에는 싣지 않는다 (일별조회의 기존 `"ALL"` 은 두 모드 공통으로 유지).
  **주문(접수)의 신 TR·신 본문은 정규장(`regular`) 세션에만 적용한다** — 저장소에 NXT 주문
  예제가 없어 `pre_market`·`next_market` 의 올바른 `EXCG_ID_DVSN_CD` 값을 확정할 수 없어,
  그 두 세션 접수는 `new` 모드에서도 구 TR·구 본문 그대로다. 취소·정정·조회에는 세션 분기가
  없다(`new` 모드면 항상 `"KRX"`).
  출처: 공식 저장소 `koreainvestment/open-trading-api@b4e6249` 의 `examples_llm/`
  (`order_cash.py:103-130`, `order_rvsecncl.py:106-128`, `inquire_daily_ccld.py:141-174`,
  `inquire_psbl_rvsecncl.py:82-91`). 저장소는 **구 TR 의 지원 종료 일정도, 구/신 응답 필드가
  같은지도 말하지 않는다** — 전환·롤백 절차는 `docs/operations/runbook.md` 참조.
- **원장 TR 초당 1건 제한** (2026-09-03): 잔고 TTTC8434R·체결 TTTC8001R·미체결 TTTC8036R은
  KIS 원장 서버가 계좌당 초당 1건 초과 시 HTTP 500 `EGW00201`("원장에서 허용 가능한 초당
  거래건수를 초과", 코드 `EGW00215`)를 반환한다. 전역 리미터(18/s)와 별개라 원장 TR 간
  1.05초 간격을 강제한다 (주문 POST는 미적용). 포트폴리오 동기화가 잔고+포지션을 연속
  호출해 30초마다 HTTP 500이 나던 것이 원인이었음 (일 ~4,000건 재시도 경고).
- **연속조회 규약** (2026-09-21): 종료 판정은 **응답 헤더** `tr_cont`(`F`/`M`=다음 페이지 있음,
  `D`/`E`=마지막)로 한다 — 본문 `ctx_area_*100` 키는 마지막 페이지에도 채워져 와서 종료 근거가
  못 된다 (`_api_get`이 `data["_tr_cont"]`로 실어 준다). **요청 쪽**은 공식 저장소 예제와 같이
  첫 페이지에 `tr_cont`를 **싣지 않고**, 2페이지째부터 헤더 `tr_cont: "N"`을 보낸다
  (`_api_get(..., tr_cont="N")`, 같은 페이지의 재시도에도 같은 값 유지). 1페이지로 끝나는
  호출 — 운영 계좌 대부분 — 의 요청은 전환 전과 한 바이트도 같다. 적용 루프 3종:
  `get_positions`·`get_positions_for_account`(TTTC8434R)·`_query_daily_fills`. 세 루프는 **같은
  식으로 끝난다** — 행을 합친 직후 `_tr_cont`가 `D`/`E`면 즉시 종료하고, 그 뒤에 남은 빈 ctx
  키 검사는 헤더가 없는 응답을 위한 뒷받침이다 (`get_positions_for_account`에 2026-09-21 추가:
  마지막 페이지에도 채워져 오는 ctx 키 때문에 원장 호출이 1회 더 나가던 것이 없어진다).
  미체결 조회(`get_exchange_open_orders`)의 잘림 처리는 PR #81이 같은 함수에서 다룬다.
- **잔고 응답 스냅샷** (2026-09-11): `get_account_balance`의 inquire-balance 응답 `output1`을 5초 보관해
  바로 이어지는 `get_positions`가 재사용(1회용, 다음 페이지 있으면 미보관) — 동기화 30초 사이클의 8434R
  2회→1회. 장중 원장 초과(EGW00215)의 절반이 이 두 번째 호출이었다.
- **프로세스 공용 리미터** (`src/utils/kis_rate_limit.py`, 2026-09-03): 브로커·`kis_market_data`·
  `kr_screener`가 같은 appkey로 별도 세션을 쓰므로 게이트웨이 한도(20/s, `EGW00201`)는 합산으로
  걸린다 → 세 모듈이 하나의 슬라이딩 윈도우 + 호출 간 100ms 페이싱(10/s)을 공유 — 18/s·15/s는
  정속에서도 ~3% EGW00201 거절이 실측됐다(2026-09-03).
  KIS를 직접 호출하는 코드를 새로
  쓰면 `await kis_rate_limit.acquire(tr_id)`를 요청 직전에 넣을 것.
- 재시도 경고 형식: `[API] HTTP 500 TTTC8434R EGW00201 …, 1회 재시도` — `tr_id`로 호출 주체 식별
- **주문 POST 재전송 금지** (2026-09-03 P0): 접수(order-cash)·정정은 `_api_post(retry=False)` —
  타임아웃/연결 끊김/5xx 시 이미 접수됐을 수 있어 같은 본문을 다시 보내지 않는다 (hashkey는
  본문 무결성 검사이지 멱등키가 아님). 실패 반환 → 호출자 pending 해제 → 30초 동기화가
  실제 체결분을 sync_detected로 정합.
- **주문 POST 결과 분류 — 접수 불명(UNKNOWN) 분리** (2026-09-29 구현, 15:33 KST 배포 main `974a71f`): `submit_order` 가
  `_api_post` 진입 뒤 응답을 믿을 수 없으면 `(False, "[접수불명] …")` 를 돌려주고 감사 원장
  `unknown` 을 남긴다(반환 계약 `Tuple[bool, str]` 무변경). 불명의 효과(그날 BUY 보류·분할 SELL
  재발행 금지)는 `docs/risk/risk-and-exit.md` 맨 위 절.

  | 경로 | 분류 |
  |---|---|
  | POST 전 실패: 세션·토큰 발급 실패, 킬스위치, 세션/NXT/가격/hashkey 거절 | REJECT (현행) |
  | 응답 JSON 에 `rt_cd` 가 있고 `"0"` 이 아님 — HTTP 5xx JSON(예: EGW00201) 포함 | REJECT (현행) |
  | JSON 에 `rt_cd` 가 없거나 빈 값·null 이지만 `msg_cd` 가 있음 | REJECT (KIS 가 오류를 명시) |
  | 본문이 JSON 이 아님 (HTTP 상태 무관) | **UNKNOWN** (`_api_post` 반환에 `_unknown: True`) |
  | JSON 인데 `rt_cd` 무효이고 `msg_cd` 도 없음 (`{}`, `{"output":…}`) | **UNKNOWN** |
  | 401·본문 토큰 오류(EGW00121/123) | 현행: 토큰 갱신 후 같은 본문 재전송(최대 3회, 인증 거절은 접수 전이라는 전제). 마지막 시도의 비-JSON 401 은 UNKNOWN |
  | `retry=False` 의 `aiohttp.ClientError`·`asyncio.TimeoutError` (연결 수립 전 실패 포함) | **UNKNOWN** (`_unknown: True`) |
  | `submit_order` 에서 POST 진입 뒤의 예외(비-dict 본문 등) | **UNKNOWN** |
  | POST 진입 뒤 `asyncio.CancelledError`(종료 신호) | **UNKNOWN 기록 후 다시 raise**(알림 없음) |
  | rt_cd 0 인데 ODNO 없음(TEMP_) | 범위 밖 — 성공 경로 그대로 |

  매수 TR(구/신)은 `_api_post` 가 매 시도의 rate-limit 대기 직후·전송 전에 킬스위치(`kill_switch.check("buy")`, 2026-09-29 3단계)와
  접수 불명 보류를 다시 확인하고, 걸리면 보내지 않고 `_blocked: True` 로 돌려준다(`submit_order` → `blocked` 기록). SELL 은 재검사하지 않는다.
  취소(`retry=True`)에도 `_unknown` 이 실리지만 `cancel_order` 는 읽지 않는다 — 반환 불변.
- **감사 원장 `accept` 행의 주문 신원** (2026-09-29 구현, 20:47 KST 배포 main `785f1fe` — 설계
  `docs/superpowers/specs/2026-09-29-cli-refusal-orderref-design.md` D3): `submit_order` 성공 경로의
  EV_ACCEPT 한 곳에 필드를 더한다(기존 `order_id` 등·반환·추적 dict 무변경). 브로커 한 곳이라 봇·수동 매수·CLI 가 모두 덮인다.

  | 필드 | 값 |
  |---|---|
  | `odno`, `org_no`, `order_date`, `account_scope` | 항상. `odno`=ODNO(없으면 `TEMP_…` 그대로), `org_no`=`KRX_FWDG_ORD_ORGNO`(없거나 None 이면 `""`), `order_date`=로컬 `datetime.now().date()` ISO, `account_scope`=`"primary"`(단일 주문 계좌 — 원 계좌번호 미기록) |
  | `order_ref` | `[account_scope, "KR", 주문일, "KRX", ODNO, ORGNO, ""]` — W `OrderRef` 필드 순서. **세션이 regular/pre_close/closing 이고 ODNO·ORGNO 가 W 신원 규칙(비지 않은 `str`·앞뒤 공백 없음·ODNO 가 `TEMP_`/`local-` 아님)을 만족할 때만**. NXT 세션(pre_market/next_market)은 거래소 값 근거가 없어 생략 |
  | `session` | `submit_order` 가 이미 구한 `_get_current_market_session()` 값 |
  | `source` | `KISBroker.order_source`, 없으면 `Path(sys.argv[0]).name`(봇 `run_trader.py`, CLI 는 자기 스크립트명, 빈 argv 면 `"unknown"`). 대입으로 바꿀 수 있다 |

  신원 계산은 순수 함수 `kis_kr._accept_identity` 가 입력 검사만으로 예외 없이 한다 — 접수 성공 뒤의 예외는 posted 뒤 경로가 성공 주문을 접수 불명으로 바꾼다.
  이 필드로 재시작 때 브로커 `_pending_orders` 를 복원하지 않는다(누적 체결 이중 계상). 감사 원장은 fsync 가 없어
  응답 직후 크래시하면 행이 빠질 수 있다(그날 주문은 당일 소멸). 조회:
  `grep '"accept"' ~/.cache/ai_trader/audit/audit_$(date +%Y%m).jsonl`.
- **봇 실행 중 주문 CLI 거부** (2026-09-29 구현, 20:47 KST 배포 main `785f1fe`): `scripts/liquidate_all.py`(`--dry-run` 포함)·
  `sell_specific.py` 는 파싱 직후 `src/utils/trader_lock.hold_or_exit()` 로 봇 싱글톤 flock
  (`~/.cache/ai_trader/unified_trader.lock`)을 잡아 보고, 봇·다른 CLI 가 쥐고 있으면 KIS 호출·토큰 발급 전에 exit 2.
  별도 프로세스의 `KISBroker` 는 봇 장부·레이트 리미터(원장 TR 합산 EGW00215)를 공유하지 않기 때문이다.
  **새 주문 CLI 도 `hold_or_exit` 를 부른다.** 절차는 `docs/operations/runbook.md` '긴급 전량 매도'.
- **취소만 재시도 유지** (2026-09-21 재판단): 취소(order-rvsecncl `RVSE_CNCL_DVSN_CD="02"`)는
  `retry=True` 그대로다. 근거는 "멱등이라서"가 **아니라** 효과 한정이다 — ① `_api_post`가
  재전송하는 5xx 다수는 접수 전 거절이다(유량 `EGW00201`이 HTTP 500으로 온다), ② 전량 취소
  (`QTY_ALL_ORD_YN="Y"`)는 `ORGN_ODNO` 하나를 겨냥하므로 두 번 닿아도 **새 노출을 만들 수
  없다**(두 번째는 이미 취소된 주문에 대한 거절), ③ 응답이 유실되면 반환값은 `retry=False`
  에서도 똑같이 `False`다. 즉 재시도를 빼서 줄어드는 것은 중복 위험이 아니라 보호 취소
  (90초 SELL 폴백·10분 BUY 정리)의 성공률뿐이다. KIS가 취소 재전송을 어떻게 처리하는지에
  대한 공식 문서는 없다 — 위는 우리 쪽 노출 논증이지 브로커 멱등성 보장이 아니다.
  고정: `tests/test_kis_pagination_protocol.py::test_cancel_post_retries_after_http_500_and_succeeds`.
- **토큰 회전 채택** (2026-09-03 P1): 공유 `KISTokenManager`의 토큰이 다른 컴포넌트
  (kis_market_data/kr_screener)에 의해 회전되면 `_get_headers`가 즉시 채택하고, 토큰 오류
  응답 시 `_recover_token()`이 회전 토큰이 있으면 `invalidate()`를 생략한다 — 무조건 무효화가
  새 토큰까지 지워 재발급 1분 제한(EGW00133) 락아웃을 부르던 문제 (8월 6회).

### US (src/execution/broker/kis_us.py)
- 해외주식 주문/체결
- 미체결 조회 (TTTS3018R)
- 당일 체결 (TTTS3035R)
- 잔고 조회 (TTTS3012R)

### WebSocket
- KR: H0STCNT0(실시간 체결가), H0STASP0(호가)
- US: HDFSCNT0(해외 실시간 체결), H0GSCNI0(체결통보)

### KOSPI200 선물 시세 (kis_market_data.py, FHMIF10000000)
- 종목코드 신형식: `A01 + 연도끝1자리 + 월2자리` (예: 2026년 9월물 `A01609`) —
  구형 `101T9000` 체계 폐지됨 (2026-08-05 확인, 마스터 `fo_idx_code_mts.mst` 기준)
- 시장구분: `CM`=야간(18:00~05:00, 기준가=주간 종가 → prdy_ctrt=밤사이 변동률), `F`=주간
- 아침 스크리닝 선행지표로 사용 (US 지수보다 우선, kr_scheduler)

### 휴장일 조회 (kis_market_data.fetch_holidays, CTCA0903R)
- 한 응답이 달 전체를 덮지 않는다 — 운영 관측 `202609 → 7일`(09-01~09-24 범위), `202610 → 9일`(10-01~10-24)로 약 24일치에서 끝났다.
  연속조회(ctx/tr_cont) 의미는 공식 근거가 없어 쓰지 않고, 응답의 가장 늦은 날짜 다음 날을 BASS_DT 로 **새 첫 조회**를 보낸다
  (월말 도달·진전 없음(가장 늦은 날짜 < 커서)·빈 응답·4회 상한에서 정지, 매 호출 `kis_rate_limit.acquire()`).
  월말까지 확인한 결과만 캐시하고, 덜 덮은 결과·뒤 조회 실패는 수집분만 반환한다(warning). 2026-09-28~
- 기동 시(`run_trader.py`) 이번 달·다음 달, 매월 25일 이후(`kr_scheduler`) 다음 달을 받아 `engine.set_kr_market_holidays` 에 넣는다.
  같은 집합이 `utils.session` 에도 들어간다. 판정은 engine·session 모두 동적 ∪ fallback(`utils/session._KR_FALLBACK_HOLIDAYS` 한 곳). fallback 에 잘못 든 날은
  KIS 가 되돌릴 수 없으므로 확정된 날만 둔다. API 문서의 '1일 1회 호출 권장' 대비 월 2회 수준.

## 데이터 — 토스증권 Open API (별도 제한 관측 — 09-22 18:00 grant 만료, 서비스 inactive, **거래 소비자 미연결**)

> [설계서](../superpowers/plans/2026-09-15-toss-securities-fallback.md) · [관측 실행 경계](../operations/toss-shadow-runtime.md) · [실제 활성화 원장](../reviews/toss-observer-service-2026-09-17.md). 사용자 승인으로09/17 별도 서비스 ON·초기 발급 성공,09/18·21·22 관측/09/22 18시만료. 기존 거래 봇과 KIS 소비자는 그대로이며 장외 시점의 시세 표본은0이다.

- 구현 위치: `src/data/providers/toss/`의 보안 token store/manager, 조회 client/transport/limiter, 시장 자료 정규화, 합성 shadow 비교. `scripts/replay_toss_shadow.py`는 명시한 합성 JSON 파일만 읽어 stdout 보고서를 만든다.
- **관측 전용 후속**: lazy OAuth/GET·bounded body·별도 worker·지속 원장에 root 보호launcher/고정release·전용UID 서비스를 연결했다. release877768e, UTC/KST 각각1809 passed/기존xfail2. flag만으로 활성화하지 않으며 실제 registry/plan/grant 검증과 단일 시작 영수증을 사용한다. Toss 전용 자격만 별도 환경에 전달했고 기존 KIS 소비자·설정/venv는 무변경이다. 시세 GET/일반 갱신/3영업일 인수는 아직 별도 검증 대상이다.
- 공개 명세 `1.2.17`/2026-09-16 원본 SHA는 `tests/fixtures/toss/spec_contract.json`에 고정했다. 오프라인 fixture의 한도·시각·비교 임계값은 합성 예시이지 승인된 운영값이 아니다. `production_eligible=False`를 유지한다.

- 용도(예정): **읽기 전용 2차 시세·참조 데이터**. 청산·사이징·포트폴리오 평가/최고가·주문·체결·잔고·계좌·호가는 **전 세션 KIS 단독**. 브로커 전역 폴백 훅 금지; 표시용 wrapper opt-in과 후보/점수 변경 승격을 분리
- Base `https://openapi.tossinvest.com` · WS `wss://openapi-ws.tossinvest.com/ws/v1` · OpenAPI 3.1 스펙 `/openapi-docs/latest/openapi.json`
- 인증: OAuth2 client_credentials, 기존 관측 `expires_in=86399`(약24h, 상수 아님). **클라이언트당 유효 토큰 1개**. 단일 issuer/reader·별도 고정 락·최초 생성부터0600인 전용 보안 캐시 필요(일반 atomic writer 그대로 사용 금지)
- 허용 IP 사전 등록 필수(미등록 403). 시세·종목·수급·랭킹·지수·캘린더는 토큰만으로 조회(계좌 헤더 불필요)
- Rate limit: 그룹별 TPS(`MARKET_DATA` 15 / `MARKET_DATA_CHART` 20 / `RANKING` 5 / `STOCK` 5 / `STOCK_ALL` 1 / `STOCK_TRADING_TREND` 10 / `MARKET_INFO` 3), 응답 헤더 `X-RateLimit-*`·429 `Retry-After`. **KIS 리미터와 분리된 독립 게이트**를 쓸 것
- 주요 필드 제약: `/prices` 는 `lastPrice`·`timestamp` 만(등락률·거래량·전일종가 없음), `timestamp` 는 조건부 nullable, 종목 정보에 **업종 필드 없음**(WICS 대체 불가), `sharesOutstanding` 은 `/stocks` 에만(`/stocks/all` 에는 없음)
- **일봉 정렬이 KIS 와 반대다** — 토스 `/candles` 는 최신순 내림차순, KIS `get_daily_prices` 는 오래된 순. 소비자가 `[-1]`·`[-200:]` 로 "끝이 최신"을 가정하므로 어댑터에서 반드시 재정렬(같은 유형의 역전이 2026-08-07 실사고). 값·수량은 decimal **문자열**
- **`401 token-revoked`는 재발급 신호가 아니다** — 다른 유효 공유 캐시로만1회 재시도. 동일/없음/손상이면 mint0·auth unavailable 지속, reader는 항상 mint0. 발급 응답 유실/저장 실패도 자동 재발급 금지
- **공개 OAuth scopes가 비어 있음** — 시세 전용 권한을 주장하지 않는다. 메서드+조회 endpoint allowlist·고정 HTTPS origin·redirect 금지·계좌 호출 금지; POST는 issuer의 token 발급만 예외. 허용 IP만으로 서버 내부 오호출을 막을 수 없음
- **가격 기준**: 국내 시세는 KRX+NXT 통합이라 20:00 까지 갱신 — KIS 정규장 종가와 다르다(실측 0.9% 차이). 청산·사이징에 그대로 쓰지 말 것
- 환경변수: `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET` (`.env`, 커밋 금지)
- 코드 기본 및 기존 거래 봇은 `TOSS_API=0`(토큰/네트워크/캐시 소비/잡 등록0). 명시 승인된 별도 observer에만 `TOSS_API=1`; 약관·단일 발급·plan/grant를 확인하고 토스 상태는 운영 수급/섹터/포트폴리오와 분리했다.
- 일봉2개를 오늘/전일로 가정하지 않는다. 거래일·원 관측/수신 시각·확정/부분·시장/adjusted 기준을 검증하고 필수 결측은 legacy 숫자 dict를 만들지 않음(N/A 또는 스킵). 200은 페이지 크기이며 52주/252기간 요청은 전체 구간 확보 필요
- `/stocks/all`은 토스 거래 가능 목록이지 KRX 전체 정본이 아님. 종목 수급의 주/등록외국인/통합시장/잠정치와 시장 수급 금액 기준을 혼용하지 않음
- 합산 예산은 단일 송신자에서 공유(동시 reader 조회 금지). Reset은 1토큰 보충까지 초; 락·한도·HTTP·페이지·retry에 단일 deadline, 논리 조회 총 retry1·발급 retry0. 원문 인증/body/예외 로그 금지
- 위 TPS/IP/0.9%는 기존 조사 보고다. 이번 별도 서비스의 실제 검증은 초기 OAuth 발급까지이며 TPS/시세 정확도 재측정이 아니다. pinned 공개 스펙1.2.17 계약과 실제 관측 결과를 구분한다.

## 데이터 — pykrx

- KR 종목 마스터 (stock_list)
- 일봉 OHLCV
- `await asyncio.to_thread()` 필수 (동기 블로킹)
- **간헐적 실패** → DB 캐시 폴백
- 종목 마스터: pykrx 실패 시 FDR `StockListing("KRX")` 폴백
  (`storage/stock_master.py` 2026-04-21, `dashboard/data_collector.py` 2026-09-03)
- `get_market_sector_classifications`(WICS 업종): KRX 인증 없이는 항상 JSON 오류 →
  `sector_momentum`이 실패 시 **6시간 백오프** 후 키워드/파일 캐시 매핑 사용 (2026-09-03)

## 데이터 — FinanceDataReader 지수 일봉

- FDR 0.9.110 `DataReader("KS11"/"KQ11"/"KS200")` 는 KRX 를 부르지 않고 GitHub `FinanceData/fdr_krx_data_cache` 연도별 CSV 를 읽는다.
  읽기 실패를 삼키고 신선도 검사가 없어 **상류가 멈춰도 예외 없이 오래된 프레임**을 준다 — 2026-09-17 장중 부분봉(6724.34)에서 정지 확인(09-28).
- KOSPI 결정 소비처(스크리너 레짐·변동성 타게팅·수확 shadow)는 `utils/kospi_benchmark.load_kospi_daily`(1순위 `YAHOO:^KS11`, 신선도 검증)를 쓴다.
  세 소비처 모두 `FALLBACK_SOURCES`(`YAHOO:^KS11` → `KS11` → `069500` 최후 대체, 같은 검증)로 읽는다(스크리너·수확은 2026-09-29 15:33 KST 배포 main `974a71f`~).
  Yahoo 는 거래일 행을 빠뜨릴 수 있고(09-28 봉 결손 → FDR 이 NaN 종가로 채워 이력 전체 거부) 그 날은 069500 이 채택된다.
  069500 은 원 단위라 지수 pt 와 섞지 않는다 — 12:00 LLM 레짐 재분류는 대용 계열에 당일 KIS 지수 레벨을 잇지 않는다.
  FDR 069500 은 +24.2%/일 오염 이력이 있어, 스크리너·수확은 채택 전 소비 구간 |일수익률| > 12% 를 `proxy_outlier` 로 검사해 걸리면 채택하지 않는다(`proxy_return_outlier`).
  수확은 커서가 없으면 대용을 채택하지 않는다(`proxy_needs_cursor`). 한계: 검사 창이 소비 구간보다 약간 넓어 경계 부근 오염은 미채택 쪽으로 치우치고, 수확은 커서가 고정돼 그 오염이 창에서 빠지지 않아 Yahoo·KS11 회복까지 생략이 이어질 수 있다(의도 — 과잉 거부는 R1 이전 상태일 뿐).
  LLM 프롬프트(08:10/12:00 레짐·15:00 포지션 점검)는 대용일 때 `KODEX200 대용 일봉`·마지막 봉 날짜를 표기한다.
  FDR Yahoo 리더는 `end` 를 **로컬 자정** 기준으로 넘겨 KST 에선 end 가 빠지고 UTC 에선 다음 거래일이 섞인다 — `load_kospi_history` 는 하루 더 조회한 뒤 end 이후 행을 잘라 end 포함으로 맞춘다.
- `scripts/` 백테스트 3개(backtest_strategies·backtest_t1_gate·ab_exit_policy)는 `load_kospi_history`(과거 구간, Yahoo ^KS11 → KS11, end 포함·열린 구간 정지 경고)를 쓴다. quick_backtest 는 연구 venv(loguru 없음)라 FDR `YAHOO:^KS11` 을 직접 읽는다(폴백·정지 경고 없음) (2026-09-28~).
  `load_kospi_history` 는 end 절단 뒤 반환 구간 종가에 NaN/inf/0 이하가 하나라도 있으면 그 원천을 경고와 함께 건너뛴다(dropna·보간 없음, 모두 무효면 `(None, None)` → backtest_strategies 레짐은 삼성전자 대리(`samsung_proxy` 라벨) 또는 NEUTRAL, NaN 레짐 캐시는 만들지 않는다) (2026-09-29~, Yahoo 09-28 NaN 행).

## 데이터 — yfinance

- US 역사 데이터, 시가총액
- SPY/QQQ 벤치마크 (시장 체제 판단)
- S&P 500/400 유니버스
- `asyncio.to_thread()` 래핑

## 데이터 — Finnhub

- US 뉴스 피드
- 어닝 캘린더
- 재무 메트릭 (EPS, Revenue)

## 데이터 — Finviz

- US 종목 스크리닝
- Beta 리스크 보정
- 장중 모멘텀 확인
- Short Interest

## 데이터 — Yahoo Finance (v8 API)

- 시장 지수 (KOSPI, KOSDAQ, S&P500, NASDAQ, DOW)
- KOSPI 벤치마크 히스토리 (/api/benchmark)
- SPY/QQQ 등락률 (US 시장 체제)
- 환율 (USDKRW)
- **비공식 API** — 인증 불요, rate limit 주의

## 데이터 — DART

- 조회 상태 계약(2026-09-15 교차 리뷰 후속): HTTP 200 / status `000`의 **해석 가능한 공시 목록**은 위험·호재 키워드가 없는 중립 공시도 `DartCheckResult.fetched=True`. 목록·행 모양과 비어 있지 않은 문자열 `report_nm`을 검증한다. 제목 누락/공백/비문자·잘못된 목록은 `fetched=False`이며 캐시하지 않는다. 유효 목록의 0건·status `013`은 정상 조회다. 유효·손상 행이 섞이면 이미 확인된 위험 사실은 보존하되 획득 성공으로 포장하지 않는다. HTTP/API/파싱 실패와 정상 무위험 결과를 구분한다.
- 불완전 목록은 `positive_disclosures`를 비워 실제 StockValidator의 +0.10 및 스크리너의 +15 호재 가산을 만들지 않는다. 정상 완전 호재의 가산과 확인된 block/warning의 차단·감점은 유지한다. 이는 shadow 출력만의 변경이 아니라 **불완전 자료의 실제 긍정 가산을 억제하는 의미 변경**이며, 주문·점수 임계값·설정값을 바꾼 것은 아니다.
- 위험 공시 차단 (유상증자, 소송 등)
- 호재 공시 보너스 (자사주 매입 등)
- `_apply_dart_catalyst()` in kr_screener.py
- **보유 종목 공시 경보 (2026-08-20~)**: `kr_scheduler.run_dart_alert_scheduler` —
  장중~장후(08:00~16:00) 10분 주기로 보유 종목의 당일 신규 위험 공시
  (DartChecker BLOCK/WARNING 키워드)를 감지, 텔레그램 즉시 경보.
  경보 전용(자동 매도 없음), 일중 dedup, 캐시 우회(`use_cache=False`).
  비용 최대 48콜/시 (DART 한도 20,000/일). LLM 정성 해석·자동 대응은
  경보 정확도 관측 후 승격 (리서치 #3, docs/research/ai-trading-research-2026-08.md)

### MCP 런타임 제거와 종목 검증 범위 (2026-09-15)

엔진의 MCP 클라이언트·부팅 연결·수급/검색 버즈 조회는 제거했다. SDK나 `pykrx-mcp` 서버를 설치하지 않으며, 부팅 중 `npx`로 Naver MCP 서버를 내려받거나 실행하지 않는다. 이 변경은 일반 `pykrx` 라이브러리와 별개다.

- `StockValidator`는 네이버 뉴스·DART 직접 검증을 유지한다. 수급·공매도·검색 버즈의 공개 결과 필드는 호환성을 위해 남기되 미획득·무가산으로 취급한다. 전체 검증을 정상 완료했다고 승격하지 않으며, 기존 `approved` 기본값·확인된 DART 위험 차단·뉴스/공시 조정값은 유지한다. `approved=True`는 자료 충분성이나 위험 해소의 증명이 아니다.
- `SupplyTrendDetector`는 기존 KIS 투자자 일별 조회를 유지한다. KIS 공급자 미주입 시 기존 일일 수급 폴백을 사용한다. MCP 응답/거래대금 단위가 맞지 않던 폴백은 삭제하고, KIS를 검증기의 신규 가산 경로로 연결하지 않는다.
- 전략 수집기의 미소비 업종별 MCP 조회를 없앤다. 기존 결과 키의 호환성과 나머지 직접 공급자 경로를 보존한다.
- 당시 MCP 미연결 운영을 기준으로 한 정리다. 과거 MCP를 실제 연결한 별도 환경에서는 해당 보조 가산이 제거되므로 모든 환경의 신호가 동일하다고 주장하지 않는다. 후속 공급자 도입은 관측 시각·단위·응답 계약·점수 효과를 별도 검증해야 한다.

검증·배포 기록: [MCP 정리 보고서](../reviews/mcp-retirement-2026-09-15.md).

## 데이터 — AIK Stock Data (공시 요약, 2026-08-11~)

- `https://aikstockdata.com/data/public/disclosures.json` — DART 공공데이터
  재가공 공시 피드 (중요도 점수·유형 라벨·장구분 태깅), 무키·무인증
- 소비: `src/data/providers/disclosure_feed.py`
  ① 아침 브리핑(07:30 슬롯) — "최근 공시 중요도 상위 5건" 섹션
  ② 크로스검증 `llm_second_check` — 종목별 "최근 공시 (보조 참고)" 컨텍스트
  ③ 배치 LLM 랭킹(Gemini) — 후보 라인 공시 태그
  (②③은 배치 스캔 시 갱신되는 메모리 캐시(TTL 6h) 동기 조회 — 캐시 미적재 시 생략)
- ⚠️ **개인 운영 무료 서비스 — 지속성·정확성 무보증. fail-open 필수**
  (실패 시 빈 문자열, 브리핑에서 섹션 생략). 매매 판단 경로에 연결 금지
  (T+1 데이터). 출처 표기 조건부 라이선스 — 브리핑에 출처 명시함

## LLM — OpenAI (GPT-5.4)

### 용도 (heavy 작업)
| 태스크 | 용도 |
|--------|------|
| STRATEGY_ANALYSIS | 매수 전 LLM 이중검증 (크로스검증) |
| TRADE_REVIEW | 일일 거래 복기 (20:30) |
| MARKET_ANALYSIS | 장전 시장 진단 (08:50) |

### 한도
- 이중검증: 10회/일
- 일일 예산: $5

## LLM — Gemini Flash

### 용도 (light 작업)
| 태스크 | 용도 |
|--------|------|
| THEME_DETECTION | 테마 탐지, 뉴스 요약 |
| QUICK_CLASSIFY | 빠른 분류 |
| WIKI_INGEST | Wiki 교훈 추출 (~$0.0001/회) |
| QUICK_ANALYSIS | 빠른 실시간 분석 |

## LLM — Perplexity (Sonar)

- 장전 시장 진단 시 실시간 매크로 검색
- `_fetch_perplexity_context()` in market_regime.py
- 타임아웃 15초, API 키: PERPLEXITY_API_KEY

## LLM — Manus API (2026-08-05 도입 → 2026-08-19 비활성)

> ⚠️ **2026-08-19 구독 해지로 비활성** (`llm.manus.enabled: false`).
> 배치 작업(trade_review/strategy_analysis)은 기존 API 경로로 회귀 —
> OpenAI gpt-5.6-sol primary, Gemini 3.1 Pro 폴백. 클라이언트 코드는 보존,
> 재구독 시 enabled만 되돌리면 복원. 아래는 도입 당시 스펙.

- 배치성 작업(거래 복기·전략 진화·주간 분석)을 OpenAI API 대신 Manus 에이전트로 처리
- `src/utils/manus_client.py` — 태스크 기반 비동기 API:
  `POST /v2/task.create` → `GET /v2/task.listMessages` 5초 폴링 →
  `agent_status=stopped` 시 assistant_message / structured_output_result 추출
- 인증: `x-manus-api-key` 헤더, API 키: MANUS_API_KEY
- agent_profile: manus-1.6 (기본) | manus-1.6-lite | manus-1.6-max — `llm.manus.agent_profile`
- 라우팅: `llm.py`의 `MANUS_ALLOWED_TASKS` allowlist(trade_review/strategy_analysis/market_analysis)
  ∩ config `llm.manus.tasks`. 실패 시 OpenAI/Gemini API 자동 폴백
- **응답 수십 초~수 분 — 실시간 매매 경로 사용 금지** (배치 전용)
- waiting(추가 입력 요구)·타임아웃(기본 600s) 시 `task.stop` 호출로 크레딧 낭비 방지

## 알림 — Telegram Bot

- 체결 알림 (매수/매도)
- 일일 리포트 (16:00)
- LLM 장전 진단 (08:50)
- 주간 원칙 리포트 (토요일)
- 주간 리밸런싱 결과
- 환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
- **HTML 파싱 실패 폴백 (2026-08-05~)**: `parse_mode=HTML` 발송이 400
  `can't parse entities`로 거부되면 `parse_mode` 제거 후 plain text로 자동 재발송
  (`send_message`/`send_alert` 공통). 근본 대책은 발신부에서 동적 문자열
  `html.escape()` — 미이스케이프 `<` 포함 메시지(예: `0 < 200,000`)가 3회 재시도
  전량 실패하던 문제의 안전망.

## 수수료

| 시장 | 매수 | 매도 | 왕복 |
|------|------|------|------|
| KR (한투 BanKIS) | 0.014% | 0.213% (세금 포함) | ~0.227% |
| US (KIS 해외주식) | 0% | 0% | 0% |


### KIS WebSocket 관측 채널 계약 — 11차(2026-10-01)

[공식 유량 공지(2026-04-20 기준)](https://apiportal.koreainvestment.com/community/10000000-0000-0011-0000-000000000001/post/d0d1a83f-6f8d-4437-9700-6d26702fd989)와 [공개 JSON 원문](https://apiportal.koreainvestment.com/api/forums/10000000-0000-0011-0000-000000000001/posts/d0d1a83f-6f8d-4437-9700-6d26702fd989)을 10-01 확인했다. appkey당 한 WS 세션, 국내/해외/파생과 체결가/호가/예상체결/체결통보를 합쳐 41등록이다. `(tr_id, tr_key)`별로 등록을 세는 것은 공지 문구와 공식 요청 구조를 결합한 해석이며, 같은 종목 PRICE+BOOK은 2건으로 예산화한다. 기존 40종목 상수는 이 공식 한도 단위가 아니다. 요청 사이 기본 간격은 공지의 100~150ms 권고에 맞춰 150ms다.

[공식 ACK helper](https://github.com/koreainvestment/open-trading-api/blob/main/examples_user/kis_auth.py#L538-L559)는 header의 TR/key, body의 rt_cd/msg1을 읽는다. rt_cd 문자열 0과 UNSUB prefix를 해제로 분류하지만 안정적인 exact 성공 문자열/코드 목록은 찾지 못했다. 따라서 이번 조정기는 정확한 해제 성공 집합을 기본 비워 둔다. `unsubscribe_success_messages`를 쓸 때는 `unsubscribe_evidence_ref`가 필요하며 합성 응답은 실제 프로토콜 검증이 아니다. msg_cd 부재를 실패로 간주하지 않는다. 미확정/거절/timeout은 슬롯을 유지하고 이전 소켓이 닫힌 재연결에서만 상태를 리셋한다. ACK에 요청 ID가 없으므로 같은 연결에서 해제 완료한 key는 재사용하지 않는다.

새 관측은 별도 WS 세션을 만들지 않고 기존 피드에 선택 설치한다. 외부 등록 예약과 세션 독점 근거는 호출자가 명시하며 문자열 입력 자체가 운영 현황 검증을 대신하지 않는다. 기본 실행에는 이 설치 호출이 없고 브로커 호출/수집 활성화는 하지 않았다.


12차 근거 점검: 저장소의 source/docs/tests에는 실제 비식별 해제 ACK fixture가 없고 합성 응답만 있다. 공식 helper의 `rt_cd=0` 및 `UNSUB` 계열 판별 의미는 알려져 있으나, 로컬의 더 엄격한 exact 성공값 계약을 채울 자료는 없다. 이번에는 브로커/운영 로그를 호출하지 않았고 기존 허용 목록을 바꾸지 않았다. 후속 비식별 증거가 생기면 같은 연결·하나의 pending 해제 요청과 ACK의 key/시각/순서, 환경·TR·msg_cd 존재 여부와 정확한 값까지 범위를 한정해야 한다. 하나의 BOOK 응답을 PRICE/NXT/다른 환경에 일반화하거나 source 문자열만으로 진위를 입증했다고 주장하지 않는다. 현재 raw 문자열 tuple 계약의 범위 확장은 별도 구현/검토 대상이다.

### 토스 후보 WS 실행 연결 — 24차(2026-10-01, 미활성화)

[공식 AsyncAPI1.2.2](https://openapi.tossinvest.com/openapi-docs/latest/asyncapi.json)를 고정한 별도 v2 승인에서만 `wss://openapi-ws.tossinvest.com/ws/v1`의 `orderbook:kr`에 연결한다. 기존 REST 승인·퇴역 service 상태는 WS 재활성화 승인이 아니다. 국내 통합 KRX+NXT·LOSSY·초기 snapshot 없음·원천시각 null 가능성을 보존한다. 구현 한도1~3종목/한 구간은 공식 계정 한도와 별개의 첫 pilot 제한이다.

기존 token manager/issuer/sender lock을 재사용하며 발급·접속 대기는 중지/승인 만료에 연결된다. 고정 목적지·redirect/proxy/암묵 retry 금지·전용 소켓·자동 재접속 없음이다. 엔진 후보 ID는64KiB 이하 loopback 투영으로 받고 계좌/현금/원문 호가를 보내지 않는다. 종료 자료는 새 파일의 시작/최종 두 행이며 중간 디스크 보존은 없다. 실제 토큰·연결·휴장일·장중 품질은 시험하지 않았다. 실행 입력/원본 대조/보고 계약은 [규약24차](../research/current-engine-selection-entry-protocol-2026-09-30.md)를 따른다.

### 25차 토스 관측 후보 범위

공식 외부 API 계약은 변경하지 않았다. 내부 후보 투영은 첫 반환≤100개를 보존하고 원래 순위 앞3개만 구독한다. v2 승인 plan에 `candidate_selection_rule=first_three_in_returned_order`를 고정하며 기존 whole-cohort 승인으로 큰 목록의 일부를 임의 구독하지 않는다. 미관측 후보도 report 분모에 남긴다. 토스 WS는 REST status.json을 쓰지 않으므로 종료코드/최종 봉인 자료/엔진 원장으로 확인한다. [10월2일 설치안](../operations/entry-capture-installation-2026-10-02.md)은 아직 미실행이다.
