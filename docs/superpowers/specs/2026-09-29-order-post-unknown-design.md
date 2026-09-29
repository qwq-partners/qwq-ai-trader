# 주문 POST 접수 불명(UNKNOWN) 분리 — 설계 (절충안 2단계, 2026-09-29)

> **상태**: 설계 v2 — 교차 공급자 리뷰 1회차(REQUEST_CHANGES P1 6·P2 2) 처분 반영(§7). 기준 main `081ab6a`. 구현(§8) 뒤 **2026-09-29 15:33 KST main `974a71f` 배포(PR #100)**.
> **위험 등급**: 돈 경로(critical)다. 결함 주입 시험과 다른 공급자 리뷰가 필수다.
> **절충안 순서**(설계 B §0, 09-29 사용자 결정): (1) 초과수익 원장 ✅ 배포 → **(2) 이 문서** → (3) 봇 실행 중 CLI 거부·OrderRef 영속.
> **사용자 원칙**
> - 과도한 방어 로직을 피한다.
> - 어느 답이 참이어도 안전한 설계로 한다.
> - 브로커 불변식을 뼈대로 삼는다. I1은 매도가능수량 초과 거부, I2는 봇의 KRX 당일 주문이 그날 소멸한다는 것이다.

## 1. 문제

KIS 주문 POST(`_api_post(retry=False)`)가 서버에 닿은 뒤 응답을 잃으면, 지금 코드는 이를 **확정 거절과 똑같이** 처리한다.

- `kis_kr.py:403-407`: 본문 JSON 파싱에 실패하면 `{"rt_cd":"-1"}` 를 반환한다.
- `kis_kr.py:413-416`: 네트워크 오류나 시한 초과(총 15초, 응답 읽기 포함)도 `{"rt_cd":"-1"}` 를 반환한다.
- `kis_kr.py:564-575`: `rt_cd != "0"` 이면 전부 `EV_REJECT` 로 기록하고 `(False, msg)` 를 반환한다.
- `kis_kr.py:609-618`: POST 뒤의 예외(본문이 dict 가 아님, `output` 형식 오류)도 `(False, str(e))` 로 돌려준다.
- `engine.py:2539-2545`: 엔진 `on_order` 는 실패하면 `clear_pending` 한다. 예약 현금·슬롯·전략예산이 즉시 풀리고, BUY 는 그 종목만 5분 쿨다운된다.

즉 KIS 가 접수했을 수도 있는 주문을 "없던 일"로 취급한다. 영향은 방향에 따라 다르다.

- **BUY**: 예약 없이 다른 종목 BUY 가 진행된다. 5분 뒤에는 같은 종목 BUY 도 다시 나가 중복 포지션이나 집중 한도 초과가 생길 수 있다. 브로커의 매수가능금액 검사가 총 현금은 지켜 주지만 종목 중복·한도는 지켜 주지 않는다.
- **분할 SELL**: 두 경로로 같은 주문이 다시 나간다.
  - 스케줄러 고아 정리(`kr_scheduler.py:963-1000`, 약 3분)가 `rollback_stage` 를 부르고, 다음 틱에 **같은 분할 익절을 재발행한다.**
  - 폴백 시장가 경로(`engine.py:2318`)도 다음 주기에 같은 수량을 다시 낸다.
  - 원 주문과 재발행 합계가 보유량 안이면 I1 이 막지 못해 두 번 팔린다.
- **전량 SELL**(손절·트레일링·EOD): I1 이 막으므로 이중 매도가 불가능하다. 그래서 09-21 원칙이 "분할 매도에만 관문"이다.

빈도는 낮다. 09-22 이후 로그 1주 동안 주문 제출 성공이 32건, 불명이 0건이었다. **드문 사건이라, 그날 BUY 를 전부 보류해도 비용이 작다.**

## 2. 결정

### D1. 분류는 브로커 한 곳에서 한다 — "POST 가 나간 뒤 응답을 믿을 수 없음"만 UNKNOWN

| 경로 | 분류 |
|---|---|
| POST 전 실패: 세션·토큰 발급 실패, 킬스위치, 세션/NXT/가격/hashkey 거절 | REJECT (현행) |
| 응답 JSON 에 `rt_cd` 가 있고 `"0"` 이 아님. HTTP 5xx JSON(예: EGW00201) 포함 | REJECT (현행). 서버가 거절을 명시했다 |
| 본문이 JSON 이 아님 (HTTP 상태 무관) | **UNKNOWN**. 응답 생성 주체를 확인할 수 없다(리뷰 P2-7) |
| JSON 인데 `rt_cd` 가 없거나 빈 값·null 이고 `msg_cd` 도 없음 (`{}`, `{"output":…}`) | **UNKNOWN** (리뷰 P1-3). `msg_cd` 가 있으면 KIS 가 오류를 명시한 것으로 REJECT |
| 401 · 본문 토큰 오류(EGW00121/123) | 현행: 토큰 갱신 후 같은 본문 재전송(최대 3회). 인증 거절은 접수 전이라는 전제다. 마지막 시도의 비-JSON 401 은 위 규칙으로 UNKNOWN |
| retry=False 의 `aiohttp.ClientError`·`asyncio.TimeoutError` | **UNKNOWN**. 연결 수립 전 실패(ClientConnectorError)도 구분하지 않는다. 드물고, 한데 묶는 쪽이 안전하다 |
| `submit_order` 에서 POST 진입 뒤의 예외 | **UNKNOWN**. 비-dict 본문, 접수 후 파싱 예외를 포함한다 |
| POST 진입 뒤의 `asyncio.CancelledError`(종료 신호) | **UNKNOWN 기록 후 다시 raise** (리뷰 P1-4). 알림은 보내지 않는다 |
| rt_cd 0 인데 ODNO 가 없음(TEMP_) | 범위 밖. 성공 경로를 바꾸지 않는다. 관측 0건 (§5 한계) |

구현은 두 곳이다.

- `_api_post` 의 해당 두 갈래가 반환 dict 에 `"_unknown": True` 를 싣는다. 취소(`retry=True`)에도 이 키가 실리지만 `cancel_order` 는 읽지 않으므로 무해하다.
- `submit_order` 는 `_api_post` 호출 직전에 `posted = True` 를 세운다. "POST 진입"은 `_api_post` 에 들어간 것까지를 뜻한다. 그 안의 rate-limit 대기 중 취소처럼 실제로는 전송 전인 경우도 UNKNOWN 으로 묶는다. 종료 순간에만 생기며, 보수적인 쪽(그날 BUY 보류)이다.

**반환 계약 `Tuple[bool, str]` 은 그대로 둔다.** UNKNOWN 은 `(False, "[접수불명] …")` 로 돌려준다.

- 접두어 상수는 새 모듈에 둔다.
- 엔진은 이미 실패 문자열(`APBK0400`)로 분기하므로 같은 관용을 따른다.
- 호출처 5곳은 바꾸지 않는다. 접두어를 모르는 호출처는 지금처럼 실패로 취급한다.

감사 원장에는 새 이벤트 `EV_UNKNOWN = "unknown"` 를 기록하고 `EV_REJECT` 는 남기지 않는다. KR 에는 원장을 읽는 제품 코드가 없다.

### D2. 상태는 "오늘의 접수 불명 장부" 하나 — 브로커 소유, 날짜 키, 파일 영속

새 모듈 `src/risk/order_unknown.py` 를 킬스위치 옆에 두고, 클래스 하나 `UnknownOrderBook(path)` 로 구현한다.

**메서드**

- `record(side, symbol, qty, reason, now)`: 오늘 항목을 추가한다. 저장은 **파일의 오늘 항목을 다시 읽어 합친 뒤** `atomic_write_json` 으로 한다. 봇과 CLI 가 같은 파일을 써도 서로의 기록을 덮지 않게 하기 위해서다(리뷰 P1-6). 쓰기가 실패해도 메모리 상태는 유지하고 ERROR 로그를 남기며, 알림에 "재시작 보호 없음"을 붙인다(리뷰 P2-8). **구현 리뷰 1회차 P1-1:** 재읽기→병합→쓰기 전체를 `<path>.lock` 의 `fcntl.flock(LOCK_EX)`(블로킹) 안에서 한다 — 한쪽이 읽은 뒤 다른 쪽이 쓴 기록을 덮지 않게. 잠금 실패(OSError)는 저장 실패와 같게 처리한다.
- `buy_hold_reason(today) -> Optional[str]`: 오늘 BUY UNKNOWN 이 하나라도 있으면 사유를 돌려준다.
- `has_unknown_sell(symbol, today) -> bool`

**로드** — 생성 시 1회만 한다.

| 파일 상태 | 처리 |
|---|---|
| 파일 없음 | 빈 장부 |
| 날짜가 오늘이 아님 | 빈 장부. 자정 리셋 코드가 필요 없다(`stop_loss_today` 선례) |
| JSON 깨짐, mtime 이 오늘 | BUY 보류와 전 종목 분할 SELL 금지를 켠다. 그날 한정 fail-closed |
| JSON 깨짐, mtime 이 과거 | 무시한다. 깨진 파일이 영구 보류를 만들지 않게 한다 |

**경로**는 `Path.home()/.cache/ai_trader/order_unknown.json` 이고 **생성 시점에 계산**한다. 그래서 시험의 `home` fixture 로 격리된다.

**브로커 연결**

- `KISBroker` 가 장부를 지연 생성한다.
- 공개 메서드는 `unknown_buy_hold()` 와 `has_unknown_sell(symbol)` 이다.
- "오늘"은 브로커 세션 판정과 같은 로컬 `datetime.now()` 를 쓴다(운영 서버 KST). 시험은 `now` 를 주입한다.

**실행 중인 프로세스끼리는 상태를 공유하지 않는다.** 파일은 **같은 날 재시작**(과 병합 저장)에만 쓰인다.

- 수동 풀매수는 봇 프로세스 안에서 돈다.
- CLI(`liquidate_all`, `sell_specific`)는 운영자가 직접 실행하는 도구이고, 이 단계의 D4 보장 범위 **밖**이다(리뷰 P1-6).
  - `sell_specific` 은 임의 수량(분할 포함)을 직접 제출한다. 봇도 CLI 의 불명 주문을 재시작 전까지 보지 못한다.
  - CLI 도 브로커를 거치므로 불명이면 파일에 기록되고 출력에 `[접수불명]` 이 찍힌다. 같은 날 봇을 재시작하면 반영된다.
  - 봇 실행 중 CLI 거부는 절충안 **(3)단계**의 범위이며, 거기서 닫는다.

**알려진 한계**: 저장이 실패한 채 같은 날 재시작하면 빈 장부로 열린다(이전 정상 파일이나 파일 부재가 남는다). 알림으로 운영자에게 알린다.

**해제는 날짜가 바뀔 때만 일어난다(I2).**

- 장중 자동 해제는 하지 않는다. 주문번호 없이는 사용자 HTS 주문과 확정적으로 구분할 수 없다(설계 B D17).
- 수동 해제는 파일 삭제 후 장 마감 뒤 재시작이다(runbook).

### D3. BUY UNKNOWN → 그날 신규 BUY 를 전부 보류

**차단 지점**

- **브로커 게이트**(권위): `submit_order` 머리, 킬스위치 바로 뒤다. BUY 이고 `unknown_buy_hold()` 가 사유를 주면 `record_blocked` 를 남기고 `(False, reason)` 을 반환한다. 엔진·폴백·수동 풀매수를 모두 덮는다.
- **전송 직전 재확인**(리뷰 P1-5): `_api_post(..., gate=...)` 를 추가한다. BUY 는 `gate=self.unknown_buy_hold` 를 넘긴다. `_api_post` 는 매 시도마다 `await self._rate_limit()` 직후, POST 전에 `gate()` 를 확인한다(401 재전송 포함). 사유가 있으면 전송하지 않고 `{"rt_cd":"-1","msg1":사유,"_blocked":True}` 를 반환하며, `submit_order` 는 이를 `record_blocked` + `(False, 사유)` 로 처리한다. 머리 게이트를 통과한 뒤 hashkey·rate-limit 을 기다리던 BUY 가, 그사이 생긴 UNKNOWN 을 넘어 전송되는 경쟁을 닫는다. 이미 전송된 주문은 되돌릴 수 없다(경계).
- **엔진 조기 차단**(비용 절감): `on_signal` 의 "기존 포지션 보유 차단"(`engine.py:1847-1850`) 바로 뒤, 크로스 검증·LLM 호출 전이다. `getattr(broker, "unknown_buy_hold", None)` 가 호출 가능하고 **결과가 비어 있지 않은 str 일 때만** 차단한다. 가짜 브로커나 MagicMock 이 우연히 차단을 켜지 않게 하기 위해서다.

**엔진의 BUY 실패 처리는 현행 그대로 둔다**(`clear_pending` + `block_symbol`).

- 그날 BUY 가 전부 막히므로 예약 현금을 유지할 이유가 없다.
- pending 을 유지하면 그 종목의 손절 신호가 막힌다(CLAUDE.md "pending 은 종목 단위").

실제로 체결된 UNKNOWN BUY 는 현행 경로가 처리한다. 30초 동기화가 "포지션 추가"로 들여오고, ExitManager 등록으로 손절 보호를 받는다. 초과수익 원장에서는 sync_entry 로 제외된다.

### D4. SELL UNKNOWN → 그날 그 종목의 **분할** SELL 재발행만 금지, 전량 SELL 은 허용

사용자 문구는 "같은 종목 SELL 재발행 금지"다. 그러나 전량까지 막으면 그날 그 종목의 손절이 꺼진다. 전량 재발행은 I1 이 막으므로 어느 답이 참이어도 안전하다. 그래서 금지는 분할에만 건다.

"분할"의 판정: 엔진의 기존 `_sell_partial_intent`(수량 < 보유) **또는** 신호 메타의 명시적 `exit_action == "sell_partial"`. 불명 분할이 실제 체결되면 동기화가 보유량만 줄이고 ExitManager 의 `remaining_quantity` 는 그대로다. 그 상태에서 같은 단계가 재발행되면 수량 == 보유량이 되어 기존 판정으로는 "전량"이 된다(리뷰 P1-1). 그래서 명시적 액션을 같이 본다. 기존 취소 실패용 표식(`sell_partial_intent`)의 의미는 바꾸지 않는다.

**막는 곳 네 군데**

1. **발생원 — ExitManager**(리뷰 P1-1·P1-2의 뿌리): `set_partial_exit_block(fn)` 훅을 추가한다(`set_pending_verifier` 와 같은 주입 방식, `run_trader.py:890` 옆에서 `broker.has_unknown_sell` 로 배선). ~~`update_price` 의 "2. 분할 익절"(`exit_manager.py:1051`)에서 훅이 참이고 `current_stage` 가 NONE·FIRST·SECOND 이면 `_check_partial_exit` 를 건너뛴다.~~ **구현 리뷰 1회차 P2-3:** `update_price` 는 항상 `_check_partial_exit(..., allow_partial=not 훅(symbol) is True)` 를 부르고, 1·2·3차 각 분기가 `exit_qty` 직후 pending 설정 전에 `action` 을 계산해 `sell_partial` 이고 허용되지 않으면 부작용 없이 None 을 돌려준다(pending·exit_history·영속 쓰기 없음). 잔량 전부인 단계 청산(`sell_all`)은 그대로 낸다(I1 이 이중 매도를 막는다).
   - 손절(`:1043`)은 그보다 **앞**에서, 트레일링·본전 이동은 뒤에서 그대로 판정된다.
   - 분할 신호 자체가 생기지 않으므로 스케줄러 `_exit_pending_symbols` 가 등록되지 않는다. 그래서 차단된 분할 pending 이 약 3분간 손절을 가리는 문제(P1-2)가 없다.
   - pending_stage 설정·롤백 반복, 틱마다의 영속 쓰기도 생기지 않는다. THIRD→TRAILING 전환은 막지 않는다.
2. **중앙 가드 — `on_signal`**: 수량을 정한 직후(`engine.py:2005-2018` 뒤)에 `if (분할) and 불명SELL(sym): return None` 을 넣는다. ExitManager 밖의 발행처를 위한 것이다. pending 등록 전이라 정리할 장부가 없다. pending 캐시에 `"sell_partial_action": True`(명시 액션)를 따로 남긴다.
3. **`_fallback_stale_sell`**: 이 경로는 on_signal 을 거치지 않는다. (`sell_partial_intent` 또는 `sell_partial_action`) 이고 불명SELL 이면 시장가를 재제출하지 않고 `clear_pending` 후 반환한다.
4. ~~**`on_order` 좀비 카운터**: 불명 SELL 종목의 `APBK0400` 은 세지 않는다.~~ **구현 리뷰 1회차 P1-2 로 삭제** — 전량 SELL 불명이 실제 체결되면 동기화가 유령 제거를 미루는데, 재발행의 APBK0400 을 세지 않으면 강제 정리 경로가 끊긴다. 불명 종목도 현행대로 센다(잘못된 좀비 알림 한 통보다 회복 경로가 중요하다. 강제 유령 제거는 KIS 잔고에 없는 종목에만 적용되므로 포지션 손실은 없다).

### D5. 알림

브로커의 UNKNOWN 처리 한 곳에서 텔레그램을 보낸다. fire-and-forget 이며 발송 예외는 관찰한다. 문구는 다음과 같다.

> ⚠️ 주문 접수 불명: {종목} {방향} {수량}주 — KIS 응답 유실(재전송 안 함). 오늘 신규 매수 보류 / 이 종목 분할 매도 재발행 금지. HTS 에서 주문 확인.

수동 풀매수 실패 경보는 현행대로 둔다.

## 3. 바뀌지 않는 것 (특성화로 고정)

- 성공 경로(rt_cd 0): 요청 본문, tr_id, `retry=False`, 추적 dict, `EV_ACCEPT`
- 명시적 거절(rt_cd≠0 JSON): 반환 문자열, `EV_REJECT`, 엔진 처리
- 취소 POST 재시도(`retry=True`)와 `cancel_order` 반환값
- `test_stale_sell_cancel_failure.py:522-537`: 접두어 없는 `"네트워크 오류(재전송 금지)"` 는 여전히 일반 실패로 처리된다.
- 주문·전략·위험 설정, 킬스위치, 사이징

## 4. 시험 (결함 주입, 시계 주입 — 벽시계 금지)

**브로커**

- **B1** `_api_post` 분류
  - HTTP 200 비-JSON → `_unknown`
  - retry=False 에서 TimeoutError·ClientError → `_unknown`, POST 1회
  - 세션 연결 실패 → `_unknown` 없음
  - 5xx JSON → 본문 그대로
  - HTTP 403 비-JSON → `_unknown` (v2: 비-JSON 은 상태 무관 UNKNOWN)
- **B2** `submit_order` UNKNOWN BUY
  - 반환이 `(False, "[접수불명]…")` 이다.
  - `EV_UNKNOWN` 1건, `EV_REJECT` 0건이 기록되고 추적 dict 는 비어 있다.
  - 다음 BUY 는 POST 0회로 차단되고 `record_blocked` 가 남는다. SELL 은 POST 된다.
- **B3** UNKNOWN SELL → `has_unknown_sell(sym)` 이 참이고, BUY 는 보류되지 않는다.
- **B4** POST 뒤 예외(본문 list) → UNKNOWN. POST 전 예외 → REJECT, 현행 문자열. 응답 `{}`·`{"output":{}}`·`{"rt_cd":null}`·`{"rt_cd":""}` → UNKNOWN, `{"msg_cd":"EGW00201"}`(rt_cd 없음) → REJECT.
- **B4b** POST 응답 대기 중 `CancelledError` → 다시 raise 되고, 같은 날 새 장부가 BUY 를 보류한다.
- **B4c** 전송 직전 재확인: 첫 BUY 가 hashkey 를 기다리는 사이 다른 BUY 가 UNKNOWN 이 되면, 첫 BUY 는 POST 0회 · `record_blocked`. 401 재전송 직전에도 gate 가 걸린다.
- **B5** 명시적 거절(rt_cd "1", msg_cd) → 현행 반환·원장, 보류 없음. 성공 경로의 반환·추적은 현행 그대로.
- **B6** 영속
  - 같은 날 새 장부 → 보류. 다음 날 → 해제.
  - 깨진 파일: mtime 이 오늘이면 보류, 과거면 무시.
  - 쓰기 실패 → 메모리 보류 유지, 알림에 재시작 보호 없음 표시. 실패 뒤 새 장부는 보류하지 않는다(문서화된 한계를 고정).
  - 같은 객체에서 날짜가 바뀌면 해제.
  - 병합 저장: 다른 장부가 먼저 쓴 오늘 BUY 항목이 내 SELL 기록 뒤에도 남는다.
  - mtime 은 `os.utime` 으로 고정하고, 시계는 `now` 로 주입한다. UTC/KST 실행이 시계 주입을 대체하지 않는다.

**엔진**

- **E1** BUY 신호에 보류 사유가 있으면 `None` 이고 크로스 검증을 부르지 않는다. 사유가 None 이거나 str 이 아니면 통과한다.
- **E0** ExitManager: 훅이 참이면 분할 익절 조건을 만족해도 `update_price` 가 None 이고 pending_stage 는 None 이다. 같은 가격 흐름에서 손절선 아래로 가면 `sell_all` 이 나온다. 훅이 거짓이면 현행 분할이 나온다. 스케줄러 통합: 차단 후 다음 틱의 손절이 `_exit_pending_symbols` 에 가려지지 않는다.
- **E2** 분할 SELL 이 불명이면 `None`. `exit_action="sell_partial"` 이고 수량 == 보유량이어도 `None` 이다(P1-1: 불명 체결 → 동기화로 보유만 줄어듦 → 같은 단계 재발행). 같은 종목의 전량 SELL(수량 미지정 또는 `exit_action=sell_all`)은 통과한다.
- **E3** 폴백: 분할이 불명이면 제출 0회이고 pending 이 해제된다. 폴백 전량은 제출된다.
- **E4** (구현 리뷰 1회차 P1-2 로 대조군 전환) 불명 SELL 종목에서도 `APBK0400` 을 현행대로 센다.
- **E0b** (P2-3) 차단 시 pending_stage None · exit_history 길이 불변 · 영속 쓰기 0. 잔량 1주처럼 단계 청산이 `sell_all` 이면 훅이 참이어도 낸다.

**회귀**: 전체 suite 를 UTC 와 KST 에서 각각 돌린다(US 제외 규칙 준수).

## 5. 알려진 한계 (의도적으로 둔다)

1. **살아 있는 불명 분할 SELL(지정가)이 그날 손절 전량 SELL 을 막는다.** 매도가능수량이 줄어 KIS 가 거절하는데, 주문번호가 없어 취소할 수 없다. 현행에도 있는 한계다. D5 알림을 받고 운영자가 HTS 에서 취소한다.
2. **불명 분할 SELL 이 실제로 체결됐다면, 다음 날 같은 단계가 한 번 더 나갈 수 있다.** FillEvent 가 없어 stage 가 승격되지 않기 때문이다. 최대 분할 1회분이고, 이익 구간 매도다. 오늘 두 주문이 동시에 살아 있는 이중 발행은 막는다.
3. TEMP_(접수는 확정, 추적은 불가)는 성공 경로로 둔다. 관측 0건이다.
4. 장중 해제는 없다. 드문 사건이라 그날 BUY 를 포기하는 쪽을 택한다.

## 6. 문서 반영 (구현 PR 에 포함)

- CHANGELOG
- `docs/risk/risk-and-exit.md`: 불명 규칙
- `docs/integrations/external-apis.md`: POST 분류표
- `docs/operations/runbook.md`: 알림 대응, 파일 위치, 수동 해제
- `docs/operations/monitoring-checkpoints.md`
- CLAUDE.md 주의사항 한 줄

## 7. 리뷰 기록

### 1회차 — 교차 공급자 설계 리뷰 (2026-09-29)

- 대상 `29f3763`(v1). Codex, 요청 gpt-6-astra/xhigh. **실제 모델 gpt-6-astra, effort xhigh** 는 rollout 세션 파일로 확인했다(thread `01a0ea24-05ee-73a3-a082-7fbf8153e95f`).
- 판정 **REQUEST_CHANGES** — P0 0 · P1 6 · P2 2. coordinator 가 전부 실코드와 대조해 확인했고, 모두 수용한다.

| # | 지적 | 처분 (v2) |
|---|---|---|
| P1-1 | 불명 분할이 체결된 뒤 동기화로 보유만 줄면, 같은 단계 재발행이 "전량"으로 재분류돼 차단을 우회한다 | D4: 분할 판정에 명시 `exit_action=sell_partial` 추가 + ExitManager 발생원 차단. 시험 E0·E2 |
| P1-2 | 엔진에서 버린 분할 신호의 스케줄러 pending 이 약 3분간 손절을 가린다 | D4-1: ExitManager 훅으로 분할 신호 자체를 만들지 않는다(스케줄러 pending 미등록). 시험 E0 통합 |
| P1-3 | `rt_cd` 누락·빈 값·null JSON 이 REJECT 로 샌다 | D1: `rt_cd` 무효 + `msg_cd` 없음 → UNKNOWN. 시험 B4 |
| P1-4 | 응답 대기 중 종료 취소(`CancelledError`)가 기록 없이 사라진다 | D1: POST 진입 뒤 취소는 UNKNOWN 기록 후 re-raise(전송 전 취소도 보수적으로 포함). 시험 B4b |
| P1-5 | 머리 게이트를 통과한 BUY 가 대기 중 생긴 UNKNOWN 을 넘어 전송된다 | D3: `_api_post` 의 gate — rate-limit 대기 직후·매 시도(401 재전송 포함). 시험 B4c |
| P1-6 | CLI 는 분할 SELL 도 하며, 두 프로세스가 같은 파일을 덮어쓸 수 있다 | D2: 병합 저장. CLI 는 이 단계 보장 범위 밖으로 명시((3)단계 선행조건). 시험 B6 병합 |
| P2-7 | 비-JSON 4xx 를 접수 전 거절로 볼 근거가 약하다 | D1: 비-JSON 은 상태 무관 UNKNOWN. 401·토큰 재전송 전제를 표에 명시 |
| P2-8 | 저장 실패 시 재시작 보호의 한계, mtime 시계 의존 | D2 한계 명시 + 알림 표시. 시험 B6 에 mtime 고정·날짜 전환·실패 뒤 재생성 추가 |

### 구현 리뷰 1회차 — 교차 공급자 (2026-09-29)

- 대상 `5e8d730`·`7029527`. Codex, rollout 기준 gpt-6-astra/xhigh. 판정 **REQUEST_CHANGES** — P0 0 · P1 2 · P2 2. coordinator 가 코드와 대조해 아래 처분을 확정했다.

| # | 지적 | 처분 |
|---|---|---|
| P1-1 | 병합 저장이 프로세스 간 직렬화되지 않아, 두 기록자가 같은 이전 상태를 읽으면 늦게 쓴 쪽이 먼저 쓴 기록을 지운다 | `record` 의 재읽기→병합→쓰기 전체를 `<path>.lock` 의 `fcntl.flock(LOCK_EX)` 로 감싼다. 잠금 실패는 저장 실패와 같다(False·메모리 유지·재시작 보호 없음 알림). 시험: 첫 장부 재읽기 직후 다른 기록자가 끼어들어도 두 항목 보존 + 재읽기 시점에 잠금이 잡혀 있음(비차단 잠금 시도가 막힘), 잠금 실패 |
| P1-2 | 좀비 카운터 제외가 전량 SELL 불명 체결 뒤 강제 유령 정리 경로를 끊는다 | 제외 분기 삭제(원래 코드). E4 는 "불명 종목도 센다" 대조군 |
| P2-3 | 단계 조건 훅이 잔량 전부인 단계 익절(`sell_all`)까지 막는다 | `_check_partial_exit(..., allow_partial)` — 분할만 부작용 없이 거른다. 단계 제한 조건 삭제 |
| P2-4 | 시험 공백: 실제 생성자의 장부 복원, run_trader 배선, E0 시계 | 실제 `KISBroker(config=KISConfig(…))` 로 임시 HOME 의 오늘 장부 복원 시험, run_trader AST 구조 시험, ExitManager `datetime`·`date` 고정 |

### 구현 리뷰 2회차(한정) — 교차 공급자 (2026-09-29)

- 대상 `7c57bb1`·`6542cb8`. Codex, rollout 기준 gpt-6-astra/xhigh. 판정 P0 0 · P1 0 · P2 1. 1회차 P1-1·P1-2·P2-4 는 닫혔다. 제품 코드 변경 없음(시험·문서만).

| # | 지적 | 처분 |
|---|---|---|
| P2 | `_check_partial_exit` 머리의 30분 하드 만료가 `allow_partial` 검사보다 먼저 돈다 | **변경 없음** — 기준 `296766a` 에서도 틱마다 도는 기존 동작이다(1회차 이전 훅이 그것까지 건너뛴 것이 기준 이탈이었다). 차단된 후보 자체는 pending·이력·영속을 만들지 않는다. 시험으로 고정: 훅이 참이어도 31분 된 pending_stage 는 만료(영속 1회)되고 새 분할 후보는 흔적이 없다 |
| 변이 둔감 | 2차·3차 가드를 하나만 지우는 변이를 시험이 잡지 못한다 | FIRST→2차, SECOND→3차 각각 "훅 참 → 분할 None·pending 네 필드·이력·영속 0" + "훅 없음 대조군 → 분할" + "잔량 전부 → sell_all" 시험 추가. 1·2·3차 가드를 각각 하나씩 지운 변이가 모두 실패 |

### 구현 리뷰 3회차(한정, 확인) — 교차 공급자 (2026-09-29)

- 대상 `ab6e164`(시험·문서만). Codex, rollout 기준 **gpt-6-astra/xhigh**(thread `01a0ea58-9291-7792-bef1-b769129c6c7b`).
- 판정 **APPROVE — P0 0 · P1 0 · P2 0.** 하드 만료 "변경 없음" 처분은 기준 `296766a` 와 대조해 타당하다고 보았고, 2회차 요구를 철회했다. 단계별 시험의 가드 민감도도 확인했다.
- 리뷰어는 시험·변이를 독립으로 다시 실행하지 않았다. coordinator 가 전체 suite 를 다시 돌린 결과는 UTC·KST 각각 2332 passed / 2 xfailed, 격리 위반 0 이다.

## 8. 구현 기록 (2026-09-29)

- 커밋: 구현 `5e8d730`(코드·시험), 문서 `7029527`, 구현 리뷰 1회차 반영 `7c57bb1`(코드·시험)·`6542cb8`(문서), 2회차(한정) 반영은 시험·문서 커밋 1개(제품 코드 무변경). 기준 HEAD `296766a`. 작성 Claude Opus 5.5(요청 opus/high). **구현 리뷰 2회차(한정) 처분 완료, 미배포.**
- 변경 파일: `src/risk/order_unknown.py`(신규), `src/execution/broker/kis_kr.py`, `src/utils/audit_log.py`(`EV_UNKNOWN`), `src/core/engine.py`, `src/strategies/exit_manager.py`, `scripts/run_trader.py`, 시험 `tests/test_order_post_unknown.py`(신규).
- 시험(2회차 반영 후): 신규 69건. 전체 UTC(`TZ=UTC`) **2332 passed / 2 xfailed / pykrx warning 1**, 전체 KST(`TZ=Asia/Seoul`) **2332 passed / 2 xfailed / pykrx warning 1**, 모두 "[테스트 격리] 운영 상태·외부 네트워크 접근 시도 0건". 기존 시험 무수정. (1회차 반영 후: 신규 64건, 대상 10개 파일 377 passed, 전체 UTC·KST 각 2327 passed / 2 xfailed.)
- 변이 확인(가드를 하나씩 끈 뒤 신규 시험 실행 → 되돌림, 해시로 복원 확인):
  - 1차: 브로커 UNKNOWN 판정 8건 실패, 전송 직전 재확인 2건(B4c), on_signal 분할 가드 3건(E2), 폴백 가드 2건(E3), 머리 게이트 1건(B2), 엔진 BUY 조기 차단 1건(E1). (좀비 카운터 제외는 P1-2 로 삭제, ExitManager 훅은 아래 재확인.)
  - 1회차 반영: 생성자 장부 생성 제거 1건(실제 생성자 복원), run_trader 배선 제거 1건(AST), `allow_partial` 검사 제거(3곳) 3건(E0), `update_price` 훅 무시 3건(E0), flock 제거 2건(재읽기 잠금·잠금 실패) — 모두 kill.
  - 2회차(한정): 단계 가드를 하나씩 제거 — 1차 4건, 2차 1건(FIRST→2차), 3차 1건(SECOND→3차) 실패 — 모두 kill.
- **설계 이탈** (기존 시험을 고치지 않기 위해 — 리뷰 대상):
  1. D3 전송 직전 재확인: `_api_post(..., gate=...)` 인자 대신 `_api_post` 가 tr_id 가 매수 TR(구/신, `_BUY_TR_IDS`)이면 `self.unknown_buy_hold()` 를 매 시도의 rate-limit 대기 직후 확인한다. `tests/test_kis_tr_switch.py` 의 가짜 `_api_post(url, tr_id, json_data, extra_headers=None, retry=True)` 가 `gate` 키워드를 받지 않아, 인자로 넘기면 BUY 제출이 TypeError → 실패로 바뀐다. 의미(매수만·매 시도·401 재전송 포함)는 같다. 정정 POST 는 매수 TR 이 아니라 대상 밖(설계와 같음).
  2. D4-2 명시 분할 액션: `_pending_signal_cache` 에 `"sell_partial_action"` 키를 넣지 않고 같은 수명(등록 시 설정/해제, `clear_pending`·`on_fill` 완결 시 삭제)의 별도 집합 `RiskManager._partial_action_marks()` 에 둔다. `tests/test_stale_sell_cancel_failure.py::test_engine_records_the_partial_intent_when_the_sell_is_registered` 가 캐시 값을 `{"sell_partial_intent": True}`/`None` 으로 정확히 비교한다.
  3. D2 장부 생성: `KISBroker.__init__` 에서 만든다(생성 시 1회 로드). `object.__new__` 시험 브로커는 장부가 없어 조회는 보류 없음이고, 첫 불명 기록 때 `default_path()` 로 지연 생성한다. 조회 시 지연 생성하면 기존 시험 브로커(`test_kis_tr_switch` 의 BUY 제출)가 운영 캐시 경로를 읽어 격리 위반이 된다.
- 구현 세부(설계 범위 안): 손상 파일(오늘 수정)은 장부에 전 방향·전 종목 표식 1건(`side="*"`, `symbol="*"`)으로 담아 병합 저장에도 보존한다. 보류 사유 문자열은 `[접수불명]` 접두어를 쓰지 않는다(불명 결과와 보류 차단을 구분). 알림 문구는 방향별 효과만 적는다(매수 → 오늘 신규 매수 보류, 매도 → 이 종목 분할 매도 재발행 금지). 잠금 파일 `order_unknown.json.lock` 은 남아도 무해하다(내용 없음, 닫히면 잠금 해제).
- 남은 한계: §5 그대로. 추가로 — 엔진 `on_signal` 의 분할 가드와 폴백 가드는 경고 로그를 스로틀하지 않는다(ExitManager 가 분할 신호를 만들지 않으므로 반복 발행처는 코어 트림 등 소수). `modify_order`(정정, `retry=False`)는 이번 분류 밖이다(설계 D1 은 `submit_order` 대상). 불명 종목의 좀비 알림은 오탐일 수 있다(P1-2 처분으로 감수). flock 은 같은 호스트의 프로세스 사이에서만 직렬화한다(운영은 단일 호스트).
