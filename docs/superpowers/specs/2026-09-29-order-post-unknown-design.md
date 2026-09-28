# 주문 POST 접수 불명(UNKNOWN) 분리 — 설계 (절충안 2단계, 2026-09-29)

> **상태**: 설계 초안이며 아직 리뷰 전이다. 기준은 main `081ab6a`.
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
| HTTP 4xx, 본문이 JSON 이 아님 | REJECT. 클라이언트 오류라 처리 전이다 |
| HTTP 200·5xx, 본문이 JSON 이 아님 | **UNKNOWN** |
| retry=False 의 `aiohttp.ClientError`·`asyncio.TimeoutError` | **UNKNOWN**. 연결 수립 전 실패(ClientConnectorError)도 구분하지 않는다. 드물고, 한데 묶는 쪽이 안전하다 |
| `submit_order` 에서 POST 진입 뒤의 예외 | **UNKNOWN**. 비-dict 본문, 접수 후 파싱 예외를 포함한다 |
| rt_cd 0 인데 ODNO 가 없음(TEMP_) | 범위 밖. 성공 경로를 바꾸지 않는다. 관측 0건 (§5 한계) |

구현은 두 곳이다.

- `_api_post` 의 해당 두 갈래가 반환 dict 에 `"_unknown": True` 를 싣는다. 취소(`retry=True`)에도 이 키가 실리지만 `cancel_order` 는 읽지 않으므로 무해하다.
- `submit_order` 는 `_api_post` 호출 직전에 `posted = True` 를 세운다.

**반환 계약 `Tuple[bool, str]` 은 그대로 둔다.** UNKNOWN 은 `(False, "[접수불명] …")` 로 돌려준다.

- 접두어 상수는 새 모듈에 둔다.
- 엔진은 이미 실패 문자열(`APBK0400`)로 분기하므로 같은 관용을 따른다.
- 호출처 5곳은 바꾸지 않는다. 접두어를 모르는 호출처는 지금처럼 실패로 취급한다.

감사 원장에는 새 이벤트 `EV_UNKNOWN = "unknown"` 를 기록하고 `EV_REJECT` 는 남기지 않는다. KR 에는 원장을 읽는 제품 코드가 없다.

### D2. 상태는 "오늘의 접수 불명 장부" 하나 — 브로커 소유, 날짜 키, 파일 영속

새 모듈 `src/risk/order_unknown.py` 를 킬스위치 옆에 두고, 클래스 하나 `UnknownOrderBook(path)` 로 구현한다.

**메서드**

- `record(side, symbol, qty, reason, now)`: 오늘 항목을 추가하고 `atomic_write_json` 으로 저장한다. 쓰기가 실패해도 메모리 상태는 유지하고 ERROR 로그를 남긴다.
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

**다른 프로세스와 공유하지 않는다.** 공유가 필요한 경로가 없기 때문이다.

- CLI(`liquidate_all`, `sell_specific`)는 전량 SELL 만 하므로 I1 이 보호한다.
- 수동 풀매수는 봇 프로세스 안에서 돈다.
- 파일은 **같은 날 재시작**에만 쓰인다.

**해제는 날짜가 바뀔 때만 일어난다(I2).**

- 장중 자동 해제는 하지 않는다. 주문번호 없이는 사용자 HTS 주문과 확정적으로 구분할 수 없다(설계 B D17).
- 수동 해제는 파일 삭제 후 장 마감 뒤 재시작이다(runbook).

### D3. BUY UNKNOWN → 그날 신규 BUY 를 전부 보류

**차단 지점**

- **브로커 게이트**(권위): `submit_order` 머리, 킬스위치 바로 뒤다. BUY 이고 `unknown_buy_hold()` 가 사유를 주면 `record_blocked` 를 남기고 `(False, reason)` 을 반환한다. 엔진·폴백·수동 풀매수를 모두 덮는다.
- **엔진 조기 차단**(비용 절감): `on_signal` 의 "기존 포지션 보유 차단"(`engine.py:1847-1850`) 바로 뒤, 크로스 검증·LLM 호출 전이다. `getattr(broker, "unknown_buy_hold", None)` 가 호출 가능하고 **결과가 비어 있지 않은 str 일 때만** 차단한다. 가짜 브로커나 MagicMock 이 우연히 차단을 켜지 않게 하기 위해서다.

**엔진의 BUY 실패 처리는 현행 그대로 둔다**(`clear_pending` + `block_symbol`).

- 그날 BUY 가 전부 막히므로 예약 현금을 유지할 이유가 없다.
- pending 을 유지하면 그 종목의 손절 신호가 막힌다(CLAUDE.md "pending 은 종목 단위").

실제로 체결된 UNKNOWN BUY 는 현행 경로가 처리한다. 30초 동기화가 "포지션 추가"로 들여오고, ExitManager 등록으로 손절 보호를 받는다. 초과수익 원장에서는 sync_entry 로 제외된다.

### D4. SELL UNKNOWN → 그날 그 종목의 **분할** SELL 재발행만 금지, 전량 SELL 은 허용

사용자 문구는 "같은 종목 SELL 재발행 금지"다. 그러나 전량까지 막으면 그날 그 종목의 손절이 꺼진다. 전량 재발행은 I1 이 막으므로 어느 답이 참이어도 안전하다. 그래서 금지는 분할에만 건다.

**막는 곳 세 군데**

1. `on_signal`: `_sell_partial_intent` 를 정한 직후(`engine.py:2005-2018` 뒤)에 `if _sell_partial_intent and 불명SELL(sym): return None` 을 넣는다. pending 등록 전이라 정리할 장부가 없다.
2. `_fallback_stale_sell`: 이 경로는 on_signal 을 거치지 않는다. `_partial_intent` 이고 불명SELL 이면 시장가를 재제출하지 않고 `clear_pending` 후 반환한다.
3. `on_order` 좀비 카운터: 불명 SELL 종목의 `APBK0400` 은 세지 않는다. 살아 있는 불명 주문 때문에 전량 재발행이 수량 초과로 거절되는 것은 예상된 결과이지 좀비 신호가 아니다.

스케줄러의 `rollback_stage` 는 그대로 둔다. 롤백 뒤 재발행이 1번에서 막히므로 약 3분마다 로그 한 줄이 남을 뿐 무해하다.

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
  - HTTP 403 비-JSON → `_unknown` 없음
  - retry=False 에서 TimeoutError·ClientError → `_unknown`, POST 1회
  - 세션 연결 실패 → `_unknown` 없음
  - 5xx JSON → 본문 그대로
- **B2** `submit_order` UNKNOWN BUY
  - 반환이 `(False, "[접수불명]…")` 이다.
  - `EV_UNKNOWN` 1건, `EV_REJECT` 0건이 기록되고 추적 dict 는 비어 있다.
  - 다음 BUY 는 POST 0회로 차단되고 `record_blocked` 가 남는다. SELL 은 POST 된다.
- **B3** UNKNOWN SELL → `has_unknown_sell(sym)` 이 참이고, BUY 는 보류되지 않는다.
- **B4** POST 뒤 예외(본문 list) → UNKNOWN. POST 전 예외 → REJECT, 현행 문자열.
- **B5** 명시적 거절(rt_cd "1", msg_cd) → 현행 반환·원장, 보류 없음. 성공 경로의 반환·추적은 현행 그대로.
- **B6** 영속
  - 같은 날 새 장부 → 보류. 다음 날 → 해제.
  - 깨진 파일: mtime 이 오늘이면 보류, 과거면 무시.
  - 쓰기 실패 → 메모리 보류 유지.

**엔진**

- **E1** BUY 신호에 보류 사유가 있으면 `None` 이고 크로스 검증을 부르지 않는다. 사유가 None 이거나 str 이 아니면 통과한다.
- **E2** 분할 SELL 이 불명이면 `None`. 같은 종목의 전량 SELL(수량 미지정 또는 `exit_action=sell_all`)은 통과한다.
- **E3** 폴백: 분할이 불명이면 제출 0회이고 pending 이 해제된다. 폴백 전량은 제출된다.
- **E4** 불명 SELL 종목의 `APBK0400` 은 좀비 카운터에 들어가지 않는다. 불명이 아닌 종목은 현행대로 센다.

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

(리뷰 후 기입)
