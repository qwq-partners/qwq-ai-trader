# 리스크 관리 + 청산 전략

## 엔진 stale 미체결 정리 — `RiskManager.on_signal` 진입부 (2026-09-21 점검·수정)

두 루프는 `on_signal` 본문 안에만 있어 **SIGNAL 이 와야 돈다**(주기 태스크 아님). 예외 하나: 취소 실패로 유지 중인 분할 SELL 의 **재시도**는 엔진 하트비트(10초)가 구동한다(아래 '매도 쪽 취소 실패' 절).

| 대상 | 조건 | 동작 |
|------|------|------|
| 미체결 SELL | 90초 경과 + 09:00~15:20 | 거래소 취소 → 원 주문 수량으로 시장가 재주문(최대 2회). **분할 매도의 취소가 실패했을 수 있으면 재주문하지 않고** 아래 '매도 쪽 취소 실패' 단계(전량 청산은 종전 그대로) |
| 미체결 SELL | 90초 경과 + **동시호가 15:20~15:30** + 포지션 보유 | **아무것도 보내지 않는다** — 원 지정가·pending 유지 (2026-09-21 수정). 폴백 상한 도달·포지션이 사라진 pending 은 종전대로 해제 |
| 미체결 BUY | 10분 경과 | 거래소 취소 → pending·예약현금(×1.015) 해제. 취소 0건이면 아래 단계 |

**2026-09-21 수정 2건** (시험 `tests/test_engine_stale_pending_fixes.py`)
- **동시호가 분기 순서**: 종전엔 `cancel_all_for_symbol` 을 먼저 보낸 뒤 '지정가 유지'로 빠져, 청산 지정가가 재주문 없이 거래소에서 사라졌다(15:20 이후 낸 매도는 90초 뒤 반드시 취소됨). 취소보다 앞에서 분기한다(포지션이 남아 있는 경우만 — 15:30 이후엔 이 루프가 돌지 않으므로 폴백 상한·포지션 소멸 해제는 종전 순서 그대로 둔다).
- **BUY 취소 0건의 의미 분리**: `KISKRBroker.cancel_all_for_symbol`/`cancel_order` 는 **실패를 예외로 올리지 않고 0/False 를 돌려준다** — 코드·CHANGELOG(08-04/08-05)의 "API 예외만 유지-재시도"는 실제로는 도달하지 않았고, 0건에는 "추적 주문 없음(소멸)"과 "취소 실패(방금 체결됐거나 거래소에 생존)"가 섞여 있었다. 이제 0건이면:
  0. 부분체결로 그 종목 포지션이 이미 있으면 종전대로 해제 — pending 은 방향 구분 없이 그 종목의 손절·청산 신호까지 막기 때문에 청산을 우선한다(재매수는 '기존 포지션 보유 차단'이 막는다)
  1. 브로커가 그 종목 활성 주문을 추적하지 않음 → 소멸, 해제(2026-08-04 P1 그대로 — 예약현금 잠김 방지)
  2. 아직 추적 중(=취소 실패) 첫 회 → pending·예약 유지, 60초 뒤 재시도(방금 체결됐다면 5초 주기 체결 확인의 FillEvent 가 pending 을 지운다)
  3. 그 뒤에도 남아 있으면 거래소 실 미체결(TTTC8036R) 확인 — 생존·판단 불가(조회 실패)는 유지, 없으면 소멸(MTS 수동 취소 등)로 해제. 조회는 첫 페이지만 읽으므로 `get_exchange_open_orders(symbol=…)` 로 **종목을 지정한 호출에 한해** '첫 페이지에 그 종목이 없고 응답 헤더 `tr_cont` 가 F/M(다음 페이지 있음)'이면 `None`(판단 불가)을 돌려준다. 첫 페이지에서 찾았으면 생존 증거이므로 페이지가 남아도 버리지 않는다. 종목 미지정 호출(ExitManager pending 검증자)은 종전 동작 그대로다 — 찾은 생존 증거를 `None` 으로 낮추면 검증자가 pending 을 연장하지 못해 30분 하드 만료 뒤 같은 익절이 재발행되기 때문(교차 재리뷰 지적). 페이지 루프는 별도 PR.
  4. 유지 상한은 **연속 판단 불가 15회**(60초 간격 → 최소 15분)에만 적용한다 — 횟수는 생존 확인·첫 회 대기에서 1 로 되돌아가므로, 오래 생존이 확인되던 주문이 조회 한 번 실패로 풀리지 않는다: 조회 실패(EGW00215 등)로 예약현금이 자정 리셋까지 잠기지 않게 CRITICAL 로그와 함께 강제 해제(`KRScheduler._cleanup_stale_pending` 의 15분 강제 해제와 같은 정책, 수동 확인 대상). **거래소 생존이 확인된 주문은 상한과 무관하게 계속 유지**한다 — 살아 있는 주문의 예약·중복 차단을 풀지 않는다.
  5. 유지 중이던 BUY 가 **나중에 부분체결**되면 `on_fill` 이 그 자리에서 pending·예약을 푼다(유지 횟수 > 0 인 BUY 한정, 일반 부분체결의 잔량 추적은 종전 그대로). 다음 SIGNAL 의 stale 루프를 기다리면 그동안 `_check_exit_signal` 이 방향 구분 없는 엔진 pending 검사에서 반환해 보유분의 손절 신호가 만들어지지 않는다. 잔량은 브로커가 계속 추적해 체결되면 포지션에 반영된다. 체결 조회가 비어 FillEvent 없이 **잔고 동기화로만** 포지션이 반영되는 경우를 위해 `KRScheduler._check_exit_signal` 도 엔진 pending 검사 직전에 `RiskManager.release_kept_stale_buy(symbol)`(BUY pending · 유지 횟수 > 0 · 포지션 보유일 때만 해제)를 부른다 — 어떤 경로로 포지션이 생겼든 다른 SIGNAL 없이 가격 틱의 청산 검사가 열린다.
  6. 0건으로 해제할 때는 언제나 그 종목을 `block_symbol` 로 5분 차단한다(BUY 전용 쿨다운 — 청산은 막지 않는다). '미추적'은 방금 완전체결돼 FillEvent 가 큐에서 기다리는 중일 수 있어, 포지션이 반영되기 전의 같은 종목 재매수를 막는다. 취소 ACK 뒤의 재진입은 종전대로 허용.
  - 유지 횟수는 새 장부 없이 `_pending_fallback_count`(매도 폴백 횟수)를 같이 쓴다 — 한 종목의 pending 은 한 방향뿐이고 `clear_pending`/`on_fill`/자정 리셋이 지운다. 쓰기는 락 안에서 pending 존재를 확인한 뒤에만 하고(엔진 이벤트는 직렬이라 `on_fill` 은 끼어들지 못하지만, 별도 태스크인 스케줄러 정리의 `clear_pending` 은 await 사이에 끼어든다), 새 pending 등록 시 항상 0회로 되돌린다 — 잔존 값이 새면 그 SELL 은 첫 90초에 상한 분기로 빠져 시장가 폴백을 한 번도 못 쓴다.
  - 60초 스로틀은 pending 시각을 되감아 구현했다 — 유지 중에는 헬스 모니터(교착 판정은 그대로 발화)·대시보드의 경과 표시가 9~10분에 머문다. 원 발주 시각 보존이 필요해지면 스케줄러의 `_stale_cancel_last_try` 같은 별도 스로틀 장부로 바꾼다.
  - 한계: 취소·조회가 **예외를 올리는** 경로(바깥 `except`)는 종전대로 유지만 하고 횟수·스로틀을 적용받지 않는다 — `KISKRBroker` 의 취소·인메모리 조회는 예외를 올리지 않아 실사용 도달성은 낮다. 재시도는 SIGNAL 이 와야 돌므로 60초·15회는 하한이지 보장 시간이 아니다. BUY pending 은 종전과 같이 첫 10분 동안 부분체결 포지션의 청산 신호도 막는다(이번 변경은 그 뒤의 연장만 없앴다). 장부가 종목 단위라 주문 ID 별 종료·체결 반영을 가리지는 못한다 — 취소 ACK 직후의 재진입과 부분체결 미반영 창은 그대로이며 엔진 실행 안전성 이행의 설계 대상.

**점검했으나 두고 기록한 것** (엔진 실행 안전성 이행 브랜치의 설계 대상)
- **exit_exempt 가드 없음(폴백 루프)**: **현재 상태**(면제 = 6월부터 계속 보유 중인 `manual` 전략 087010)에서는 면제 종목이 엔진 SELL pending 에 들어오는 경로를 찾지 못했다 — 엔진 pending 쓰기는 `on_signal` 통과 주문뿐이고, SELL 발행처는 면제 가드 뒤이거나 `core_holding` 한정이거나 전략 내부 장부(`gap_and_go._gap_stocks`·`theme_chasing._position_themes`)에 오른 종목만 대상이며, 수동 매도(MTS·`scripts/sell_specific.py`·`liquidate_all.py`)는 별도 브로커 인스턴스라 엔진 장부와 무관하다. 또 이 루프에 가드를 둬도 그 시점엔 지정가 매도가 이미 나간 뒤라 방어 위치로 맞지 않는다(중앙 방어가 필요하면 `on_signal` 의 SELL 진입부). ⚠️ 구조적 불변식은 아니다(교차 리뷰 지적): ① 코어 경로(리밸런싱·stale 자동매도·조기경보·주간 트림)에는 면제 가드가 없어 `core_holding` 포지션을 면제로 지정하면 SELL 이 나간다. ② `StrategyManager.on_market_data` 는 보유 포지션을 전략 구분 없이 모든 활성 전략에 넘기고 `gap_and_go` 는 `position.strategy` 를 보지 않는다 — `_gap_stocks` 등록은 미보유일 때만 일어나고 자정에 비워지므로 계속 보유 중인 종목은 해당 없지만, 같은 날 갭 후보로 등록된 뒤 수동 매수된 면제 종목은 갭 시작점 이탈 시 SELL 이 나갈 수 있다. 둘 다 폴백 루프가 아니라 발행처의 공백이며 `on_signal` SELL 진입부의 중앙 면제 가드가 후속 과제다.
- **폴백 `submit_order` 예외 시 `clear_pending`**: `KISKRBroker.submit_order` 는 POST 이후의 모든 예외를 삼키고 `(False, msg)` 를 돌려주므로 이 `except` 는 POST 전(킬스위치·감사 원장·연결) 예외만 닿는다 — 주문이 나가지 않은 것이 확실하다. 접수 여부 불명(응답 유실)은 `(False, "네트워크 오류(재전송 금지)")` 로 일반 실패 분기에 들어온다. `on_order` 는 실패 즉시 pending 을 해제하지만(2026-09-03 P0 정책) 폴백의 첫 실패는 횟수만 올리고 다음 SIGNAL 에서 시장가를 다시 보낸다 — 접수 불명 뒤의 재제출이라 분할 매도에서는 중복 여지가 있고, 30초 포트폴리오 동기화는 아직 미체결인 접수 불명 주문의 종료를 증명하지 못한다. 3상태(접수/거절/불명) 구분은 브로커 계약 변경이라 이번 범위 밖(잔존 위험).
- **폴백 상한(2회) 뒤 `clear_pending` 만**: 횟수는 제출 **실패에도** 오른다 — 도달 경로는 '시장가 2건이 각각 90초 미체결' 또는 '첫 폴백 제출 실패 → 다음 SIGNAL 에서 두 번째 제출 성공 → 90초 미체결'이다(두 번 다 실패하면 그 자리에서 포기·해제). 살아 있는 시장가는 브로커가 계속 추적해 체결은 정상 반영된다. 전량 매도의 중복 발행은 KIS 가 주문가능수량 초과로 거절하지만 **분할 매도의 중복은 보유 수량 안이라 거절되지 않는다**. ExitManager 발 매도는 아래 스케줄러 정리가 3분에 먼저 개입한다.
- **매도 쪽 취소 실패 → 후속 PR 에서 수정(아래 '매도 쪽 취소 실패' 절)**. 당시 기록: 같은 원인(브로커가 실패를 0 으로 반환)으로 SELL 루프의 "취소 실패 → 시장가 재주문 건너뜀"과 스케줄러의 "예외 시 유지"도 도달하지 않는다. 취소 거절의 지배적 원인은 '이미 체결'이고 전량 중복은 KIS 가 거절하나, **분할 매도(예: 100주 보유·10주 익절)에서 취소가 실패하면 엔진이 시장가 10주를 더 내고 스케줄러도 pending·stage 를 풀어 재발행할 수 있다** — 교차 리뷰가 '이번 PR 의 회귀는 아니나 운영상 중요한 잔존 결함'으로 지적. 취소 완료·원 주문 종료를 확인한 뒤에만 재발행하도록 바꾸는 것은 매도 경로 전반(엔진 폴백 + 스케줄러 정리)의 재설계라 이번 범위 밖이며 최우선 후속 과제.
- **스케줄러 `_cleanup_stale_pending` 과의 중첩**: 별도 장부(`_exit_pending_timestamps`, 발행 시각 고정)로 WS 틱마다 3분(장전 5분) 초과분을 거래소 취소 → 양쪽 pending 해제 → stage 롤백한다. ExitManager 발 매도는 90초 엔진 폴백 1회 뒤 180초에 스케줄러가 전부 초기화하고 재발행하므로 엔진 폴백 2회차·상한 분기는 사실상 그 외 발행처(batch/core/교체 축출 — 스케줄러 장부 미등록)에서만 닿는다. 동시호가에도 스케줄러의 3분 취소·재발행은 그대로다(가격 갱신 효과, 미변경).

## 매도 쪽 취소 실패 — 살아 있는 분할 SELL 위 재발행 금지 (2026-09-21 후속, 시험 `tests/test_stale_sell_cancel_failure.py`)

**결함(재현):** 100주 보유, 1차 익절 10주 지정가 SELL 활성, 취소 API 실패(브로커가 0 반환). ① 엔진 90초 폴백은 반환값을 보지 않고 시장가 10주를 추가 제출했다 — 합계 20주는 보유 수량 안이라 KIS 가 거절하지 않는다(전량 매도만 APBK0400 으로 보호). ② 스케줄러 `_cleanup_stale_pending` 은 0건이면 양쪽 pending 을 풀고 `rollback_stage` 했다 → ExitManager 가 같은 10주를 재발행. 방금 체결됐는데 체결 확인이 아직 반영하지 않은 경우엔 롤백이 `ExitManager.on_fill` 의 stage 승격까지 지워(`pending_stage` 가 없으면 승격하지 않는다) 1차 익절이 다시 발동한다.

**적용 범위 = 분할 매도만, 판정은 등록 시점의 의도.** 과매도는 `원 주문 수량 < 보유 수량` 일 때만 가능하다. 전량 청산(손절·트레일링·EOD, 수량상 전량인 익절)은 원 주문이 살아 있으면 KIS 가 주문가능수량 0 으로 거절한다 → **두 경로 모두 종전 그대로 두어 손절 지연을 만들지 않는다.** 분할 여부는 나중에 추정하지 않고 **pending 을 등록할 때 남긴다**: 엔진은 `on_signal` 이 SELL 을 등록하며 `exit_action != "sell_all"` 이고 `주문 수량 < 그 시점 보유 수량` 이면 `_pending_signal_cache[종목] = {"sell_partial_intent": True}`(수명은 그 캐시와 같다 — `clear_pending`·`on_fill` 완결·새 등록에서 지움), 스케줄러는 `_check_exit_signal` 이 `sell_partial` 로 발행하며 `_partial_exit_pending[종목] = 등록 시각`(세대에 묶임)을 남긴다. 현재 수량 비교나 ExitManager `pending_stage` 로 추정하면 틀린다(교차 리뷰 2회차 P1): 늦게 도착한 잔고 스냅샷이 포트폴리오를 되돌리면 잔량 90주 손절이 `90 < 100` 으로 분할이 되고, 아래 '롤백 없는 해제' 뒤에는 옛 익절 stage 가 남은 채 새 손절 pending 이 등록된다.

**권한 분담:** 주문 생존의 근거는 **거래소 실 미체결**(브로커 인메모리 장부는 1차 힌트)이다. 엔진은 '취소 → 시장가 재발행'의 유일한 실행자, 스케줄러는 '청산 의도(stage) 롤백'의 유일한 실행자이고, **둘 다 같은 판정 함수 `RiskManager.stale_order_still_live(symbol, side, confirm)`(BUY 정리와 공용)로 생존이 부정되기 전에는 자기 행동을 하지 않는다.** 브로커 장부는 submit 성공에서 추가되고 완전 체결(`check_fills`)·취소 성공에서만 빠진다 — 거래소 측 취소·거부는 감지하지 못하므로 MTS 수동 취소 주문은 장부에 계속 남는다. 장부 확인은 **방향까지** 본다: 취소 건수는 종목 단위라, 같은 종목의 다른 주문(PR #81 이 해제한 BUY 잔량 등)이 취소된 1건을 SELL 취소로 읽으면 살아 있는 SELL 위에 시장가가 얹힌다 → "취소 뒤에도 그 종목의 활성 SELL 이 장부에 남았는가"로 판정한다.

| 분할 SELL 의 상태 | 엔진 90초 SELL 폴백 | 스케줄러 3분 정리(stale·고아 루프 공통) |
|------|--------------------|----------------------------------------|
| 취소 뒤 장부에 활성 SELL 없음(취소 ≥1건) | 종전대로 즉시 시장가 전환(지연 0) | 종전대로 해제 + stage 롤백 |
| 포지션 없음 | 종전대로 해제(재주문이 없어 위험 없음) | — |
| 취소 0건 · 브로커 미추적 | **첫 회는 대기**(장부에서 빠졌는데 pending 이 남았다 = FillEvent 가 오는 중), 다음에도 남으면 종전 시장가 폴백 | 종전대로 즉시 해제 — 엔진이 SELL 신호를 거부해 주문이 없던 고아가 흔한 경우라, 기다리면 그 종목의 청산 판정이 60초 더 막힌다 |
| 활성 SELL 이 장부에 남음(=취소 실패) 첫 회 | 조회 없이 대기, **20초** 뒤 취소 재시도 | 조회 없이 대기, **60초** 스로틀 뒤 재시도 |
| 그 뒤: 거래소에 SELL 미체결 있음 | 재주문 없이 유지, **60초**마다 취소 재시도(성공하면 그때 시장가 전환 — 생존 확정 뒤에는 복구가 사람 손이라 20초가 사는 게 없고, 상한 없는 유지에서 취소 POST·원장 조회가 하루 천 건 넘게 나가는 것을 줄인다). **상한 없음** — 풀면 발생원이 같은 분할 매도를 그 위에 재발행한다. ERROR 로그 1회, 5분을 넘기면 헬스 모니터 교착 경보(텔레그램) | 유지(해제·롤백 없음), 60초마다 재시도. **상한 없음**. 생존 첫 확인 시 텔레그램 CRITICAL("청산 주문 취소 불가") — pending 당 1회 |
| 그 뒤: 거래소에 없음 | 소멸(수동 취소 등) → 종전 시장가 폴백(원 주문 수량) | 소멸 → 종전대로 해제 + 롤백(재판단 허용) |
| 그 뒤: 판단 불가(조회 실패·다음 페이지 있음) | 유지. **연속 9회(90초 + 20초×9 = 270초 = 종전 최장 보유 90초×3)** 에서 CRITICAL 로그와 함께 **재주문 없이** pending 해제 | 유지. **연속 2회**(등록 후 약 3분 첫 회 대기 → 약 4분 판단 불가 1회 → **약 5분** 2회)에서 이 장부와 엔진 pending 을 풀되 **stage 는 롤백하지 않는다** |

**청산 지연 대 이중 매도의 균형**
- 전량 청산은 적용 범위 밖이라 추가 지연이 0 이다. 분할 매도도 취소가 성공하면 지연 0 이고, 지연이 생기는 것은 취소가 실패했을 때뿐이며 가장 흔한 원인인 '방금 체결'에서는 재주문이 애초에 필요 없다.
- 분할 매도를 유지하는 동안 pending 이 그 종목의 다른 신호(손절 포함)도 막는다. **생존이 확인된** 경우: 살아 있는 분할 매도가 있는 한 전량 손절은 KIS 가 수량 초과로 거절하므로(주문가능 = 보유 − 미체결 매도) pending 을 풀어도 손절은 나가지 못하고 같은 분할 익절만 재발행된다 → 상한 없이 유지 + 사람에게 경보. 그 미체결이 거래소에서 사라지면(체결·MTS 수동 취소) 다음 재시도에서 소멸로 판정돼 정상 흐름으로 돌아오고, 손절 조건은 가격에서 다시 도출된다(의도가 장부에 묶여 사라지지 않는다).
- **판단 불가**는 반대다: 원 주문이 이미 소멸했을 수도 있는데(그렇다면 종전 90초 시장가는 체결됐을 것이다) 풀릴 근거가 영영 오지 않을 수 있으므로(미체결이 많아 항상 다음 페이지가 있는 경우 포함) 짧은 시간 예산 안에서 푼다. 엔진 270초·스케줄러 약 5분으로 **두 장부의 예산을 맞췄다** — 스케줄러 장부가 더 오래 남으면 그동안 `_check_exit_signal` 이 반환해 손절 신호 자체가 생성되지 않는다(독립 리뷰 P1). 이때 엔진은 시장가를 얹지 않고, 스케줄러는 stage 를 롤백하지 않는다: `update_price` 에서 손절·보유기간·횡보 판정은 분할 익절 검사보다 앞이고, 트레일링은 뒤지만 `pending_stage` 가 걸려 있으면 그 검사가 아무것도 내지 않고 지나가 트레일링까지 진행한다(시험으로 고정) — 즉 손절·트레일링은 다시 열리고, 같은 분할 익절의 재발행은 ExitManager pending 검증자(거래소 확인 후에만 만료, 하드 30분)가 계속 막는다.
- 상한은 BUY 유지(PR #81)와 같이 **연속 '판단 불가'** 에만 적용한다 — 생존이 확인되면 횟수를 되돌린다. 나이·누적 횟수로 세면 오래 살아 있던 주문이 조회 실패 한 번으로 풀려 그 위에 재발행된다.
- **await 뒤 세대 재검증(스케줄러):** 정리 루프는 취소 전에 그 pending 의 등록 시각을 잡아 두고, 해제·롤백·표식 기록 직전과 생존 조회 직후에 같은 세대인지 확인한다. 그 사이 옛 익절 A 가 체결로 끝나고 같은 종목의 다음 익절 B 가 등록되면, A 에 대한 '소멸' 판정으로 B 의 양쪽 pending·stage 를 풀어 B 가 중복 발행된다(교차 리뷰 2회차 P1).
- **await 뒤 재검증(엔진):** 엔진은 취소·생존 조회 `await` 를 건넌 뒤 같은 SELL pending 인지(등록 시각·방향) 다시 확인하고 아니면 멈춘다. 그 사이 스케줄러가 취소에 성공해 pending 을 풀면 수량 장부가 비어, 재검증 없이는 폴백 수량이 보유 전량으로 대체된다(10주 익절 → 100주 시장가 — 교차 리뷰 P1). 엔진 2회차 폴백과 스케줄러 3분 정리는 둘 다 ~180초에 닿으므로 같은 가드가 기존 취소 `await` 의 경쟁도 닫는다.
- **재시도 구동:** stale 루프는 `on_signal` 안에만 있어 SIGNAL 이 끊기면 멈추고, 스케줄러 장부는 부분체결만 돼도 지워져 받쳐 주지 못한다. 유지는 '다음에 다시 본다'는 약속이라 **그 약속만은** 엔진 하트비트(10초, SIGNAL·FILL 과 같은 직렬 큐 — 루프와 겹치지 않는다)의 `on_heartbeat` 가 구동한다(교차 리뷰 P1) — 재시도 간격이 지난 유지분 중 가장 오래 기다린 **한 종목만**(브로커 I/O 를 한 핸들러에 몰면 뒤의 FILL 이 밀린다). 하트비트는 우선순위 10 이라 큐가 포화되면 밀린다: 10초는 발행 주기이지 처리 보장 시한이 아니다. 유지분이 없으면 아무것도 하지 않는다 — 90초 폴백·10분 BUY 정리의 SIGNAL 의존은 종전 그대로다.
- **스로틀:** 엔진은 pending 등록 시각을 되감지 않고 별도 장부 `_pending_cancel_keep = (횟수, 마지막 시도 시각, 생존 로그 여부)` 로 20초 간격을 만든다 — 되감으면 헬스 모니터의 5분 교착 경보가 '손절이 막힌 SELL 유지'를 영영 보지 못한다. 폴백 횟수(`_pending_fallback_count`, 상한 2)와 장부를 나눠 유지가 시장가 폴백 예산을 깎지 않으며, `clear_pending`·`on_fill`(완결)·새 pending 등록·폴백 제출 성공에서 지운다. 스케줄러는 60초 스로틀 표식을 **생존 판정이 끝날 때까지** 유지한다 — 판정 `await` 중에 WS 틱·REST 폴링·독립 정리 루프(`run_pending_cleanup`, 60초)가 같은 종목에 취소·조회를 겹쳐 보내지 않게. 표식 `_stale_cancel_miss = (pending 등록 시각, 연속 판단불가 횟수, 경보 여부)` 는 등록 시각에 묶어 체결 등 다른 경로로 끝난 이전 pending 의 표식이 새 pending 에 새지 않는다.
- **동시호가(15:20~15:30)**: 엔진은 PR #81 대로 취소보다 앞에서 분기하므로 이 구간에는 취소도 생존 조회도 보내지 않는다(유지 중이던 pending 도 그대로 지정가 유지, 하트비트도 같은 분기를 탄다). 스케줄러의 3분 취소·재발행은 종전 그대로이고 분할 매도의 취소 실패일 때만 위 단계를 탄다. 15:30 이후 엔진 루프·하트비트 재시도는 돌지 않으며 잔여는 장마감 리셋이 지운다.
- **KIS 부하:** 유지 1종목당 취소 POST 가 엔진 3회/분(생존 확정 뒤 1회/분) + 스케줄러 1회/분, 거래소 미체결 조회(원장 TR, 공용 리미터 경유 — 계좌당 초당 1건)가 같은 빈도로 더해진다. 체결 확인(미체결 있을 때 2초·유휴 15초)과 같은 레인이다. 1~2종목 고착은 수용 가능하나 다종목 동시 고착은 EGW00215 를 키울 수 있다(실측 없음 — 배포 후 체크포인트).

**브로커 계약을 바꾸는 안과의 비교(채택 안 함):** `cancel_all_for_symbol` 이 실패를 구분해 알리게(예외 또는 `(성공, 실패)` 반환) 바꾸는 안은 ① 호출처 6곳(엔진 2·스케줄러 2·장마감 리셋·운영 스크립트 3)이 전부 try/except 로 감싸고 있어 '예외 = 유지' 분기가 한꺼번에 살아나고 운영 스크립트(`liquidate_all.py` 등)의 동작까지 바뀐다. ② 실패를 정확히 알아도 **'취소 거절'은 원 주문이 체결됐는지 살아 있는지 말해 주지 않는다** — 거래소 조회는 어차피 필요하고, 계약 변경이 대체하는 것은 값싼 1차 힌트뿐이다. ③ 그 힌트는 현 계약에서도 정확히 유도된다: 취소 성공은 주문을 장부에서 빼므로, 취소 뒤 장부에 남은 그 방향의 활성 주문이 곧 취소에 실패한 주문이다(건수보다 정확하다). ④ 엔진 실행 안전성 이행 브랜치가 취소 결과·원 주문 최종상태를 분리하는 계약(`OrderEvidence`·lifecycle)을 새로 설계 중이라 legacy 계약을 지금 바꾸면 중복·충돌이다.

**한계(잔존 위험)**
- 하트비트가 구동하는 것은 **유지분의 재시도뿐**이다 — 첫 90초 폴백과 BUY 정리는 여전히 SIGNAL 이 와야 돈다(주기 태스크화는 엔진 이행 S4 범위). 20초·60초는 재시도 간격의 하한이고, 복구 시점은 취소·조회가 성공하는 때다.
- 죽은 주문(MTS 수동 취소)이 장부에 남은 종목은 그날 같은 방향의 취소가 늘 '장부에 남음'으로 읽혀, 분할 매도의 시장가 전환이 첫 회 대기(20초) + 거래소 확인만큼 늦는다(전량 청산은 무관).
- 판정은 종목·방향 단위다 — 같은 종목에 사람의 MTS 매도 미체결이 있으면 봇 주문이 죽었어도 생존으로 읽힌다. 거래소 조회는 첫 페이지만 읽고 다음 페이지 검사가 종목 단위라, 첫 페이지에 같은 종목의 BUY 만 있고 SELL 이 다음 페이지에 있으면 소멸로 읽는다(미체결 50건 초과가 전제 — 이 봇의 규모에서는 비현실적, 페이지 루프는 별도 PR).
- '거래소에 없음 = 소멸'은 체결 확인이 재시도 간격보다 오래 밀려 있으면 '체결됐으나 미반영'과 구분되지 않는다 — ExitManager pending 검증자(2026-08-08 P0)와 같은 신뢰 수준이다.
- 판단 불가 상한에서 pending 을 푼 뒤에는 batch·core 등 ExitManager 밖 발행처의 SELL 이 살아 있을 수 있는 분할 매도 위에 나갈 수 있다(`maybe_expire_pending` 은 ExitManager 발 재발행만 막는다). 반대로 **생존 확정 유지 중**에는 그 발행처들의 분할 매도(잔여 주문가능수량 안이라 KIS 가 거절하지 않을 주문)도 '어차피 못 나감'이 아니라 무기한 보류된다 — 사람 경보가 유일한 해소 수단이다.
- ExitManager 쪽 기존 한계(미변경): pending 검증자(`run_trader.py`)는 `get_exchange_open_orders()` 를 종목 없이 불러 다음 페이지 가드를 받지 못하고, `pending_stage` 하드 만료(검증자 배선 시 30분)는 판단 불가가 이어져도 발화해 같은 분할 익절을 다시 연다.
- `_pending_fallback_count ≥ 2` 해제는 새 관문보다 앞이다 — 두 번째 시장가가 살아 있어도 pending 이 풀리는 기존 동작은 그대로다. `on_fill` 의 pending 수량 차감이 방향·주문 ID 를 가리지 않는 것(늦은 BUY 체결이 SELL pending 수량을 깎는다)도 기존 결함이며, 그 경우 수량이 작아져 '분할'로 분류되므로 관문은 보수적으로 작동한다.
- 스케줄러의 '미추적 즉시 해제'에는 체결 확인이 장부에서 뺀 직후 ~ 스케줄러 장부를 지우기 전(수 ms~수백 ms)에 3분 넘은 pending 의 정리가 끼어드는 창이 남는다(종전과 같음).
- 스케줄러 고아 루프의 관문은 현재 도달하지 않는 심층 방어다 — 같은 호출에서 stale 루프가 같은 종목을 먼저 해제하거나 유지+스로틀하므로(변이 검증에서 유일하게 생존, 제거하지 않음).
- 장부가 종목 단위라 주문 ID 별 종료·부분체결 반영은 가리지 못한다 — 엔진 실행 안전성 이행의 설계 대상(그 브랜치의 `test_engine_legacy_stale_eviction_characterization.py` 는 '0건 → 시장가 재주문'을 현행으로 고정하고 있어 main 병합 시 이 절 기준으로 갱신해야 하고, `object.__new__(RiskManager)` 하네스는 `_pending_cancel_keep = {}` 한 줄이 필요하다).

## 2026-08-04 엔진 합동 리뷰 반영 (P0 4 + P1 8) — 동작 변경 요약

- **매도 폴백**: 미체결 90초 시장가 전환 시 수량 = 원 주문 수량(전량 아님).
  분할익절/코어 트림이 전량 청산으로 번지지 않는다
- **포지션 교체 축출**: `no_auto_exit_symbols`(exit_exempt) 종목은 축출 후보 제외
- **당일 손절 재진입 정책 활성화**: `record_exit(exit_type=stop_loss)` 시
  `_stop_loss_today` 등록 → V자 +5% 재돌파 + 당일 1회 제한 + 스크리닝 후보 제외가
  이날부터 실제 동작 (이전엔 전부 데드 코드)
- **min_stop_pct(4%) 클램프는 ATR 동적 손절에만** 적용 — 전략별/레짐별/장중급락
  오버라이드의 타이트 손절(2.0~3.5%)은 명시값 그대로 발동
- **장중급락 SL/TS 강화**: None 가드 + dynamic SL·effective TS 동시 조임으로 실효화.
  vcp_breakout도 전략별 청산 파라미터(SL 4.0/TS 2.5/stale 3d) 등록
- **daily_stats**: 원자적 쓰기 + 장중 복원 실패 시 리셋 생략 (일일손실 기준선 보호)
- **스케줄러 루프 슈퍼바이저**: 예외로 죽은 루프 60초 후 자동 재기동 + 텔레그램 경보
- **ExitManager stage 파일**: 저장 시점 날짜로 파일명 갱신, 빈 파일도 유효 상태로 복원

> 최종 갱신: 2026-06-16 (TRAILING 단계 슬롯 가중 추가 디스카운트)

## max_positions 잔여 비율 가중 카운트 (2026-05-06~)

분할익절 진행된 포지션이 신규 매수를 막지 않도록 슬롯 가중치 적용:
- 기본: `weight = remain / orig` (잔여 수량 / 원본 수량)
- **NONE/FIRST**: floor 0.2 (1차 익절은 잔여 80%로 아직 큰 포지션)
- **SECOND/THIRD** (2026-06-16 확장): 추가 × 0.7 곱셈, floor 0.15
- **TRAILING** (2026-06-16): 추가 × 0.5 곱셈, floor 0.1
- 정당화: 익절 진행 = 자금 회수 → 슬롯도 비례 양보, 상승장 신규 기회 확보
- 구현: `src/risk/manager.py:_get_position_weight`
- 비교 게이트: `non_core_weighted >= max_positions` (코어홀딩은 별도 슬롯)

## KR 리스크 (src/risk/manager.py + engine.py)

### 팩터 버킷 위험예산 (2026-08-08~, shadow 관측 중)

상관 전략 묶음의 총 노출을 팩터 단위로 캡 — 개별 전략 캡(G5_budget) 합만큼
동시 만석되는 것을 방지하는 상위 게이트 (`engine.RiskManager._check_factor_budget`,
게이트명 `G5_factor`).

| 버킷 | 캡 (% equity) | 전략 |
|------|------------|------|
| trend | 65% | sepa_trend, gap_and_go, momentum_breakout, vcp_breakout, strategic_swing (개별 합 75%) |
| quality | 20% | core_holding, value_growth |
| reversion | 10% | rsi2_reversal, theme_chasing (전부 폐지 — 예약) |

- 설정: `default.yml risk.kr.factor_budgets` (`RiskConfig.factor_budgets`)
- **`enforce: false`(현재)**: 초과 시 `(shadow) 팩터 예산 초과 관측` 로그만 남기고 통과
  — 캡 적정성 관측 후 true로 승격
- fail-open: 설정 부재/오류 시 통과 (개별 전략 캡이 1차 방어선)
- 전략은 첫 매칭 버킷에만 귀속
- **일일 노출 스냅샷 (2026-08-19~)**: 초과 이벤트는 매수 신호가 있어야만 평가되므로
  "초과 0건"과 "표본 부재"를 구분할 수 없음 (8/8~8/18 실측: 매수 0건 → 로그 0건).
  이를 보완해 저녁 품질검증 잡에서 `RiskManager.log_factor_exposure_snapshot()`이
  버킷별 노출(used_pct/cap_pct)을 `~/.cache/ai_trader/factor_exposure_log.jsonl`에
  매일 기록. **enforce 승격은 이 스냅샷 2주 축적 후 재판단** (2026-08-19 보류 결정)

### 전략 예산 캡 — 미체결 주문 포함 + 최소수량 보정 캡 (2026-09-03~)
- `strategy_allocation` 캡 계산(`engine` G5 조기차단·`calculate_position_size` 재클램프)에
  체결된 포지션뿐 아니라 **pending BUY 예약금**(`_pending_strategy_notional`)을 합산한다.
  장중 자동진입이 0.3초 간격으로 같은 전략 시그널을 연속 발행하면 체결 전이라 캡이
  N배 초과되던 누수 (gap_and_go 35% 캡에 ~50%).
- 분할익절용 **3주 최소수량 보정**은 `_strategy_remaining`도 만족할 때만 적용 —
  변동성 축소·일손실 반감·전략 캡으로 1~2주가 된 수량을 3주로 되돌리던 우회 경로 차단.

### 조건부 변동성 타게팅 (2026-08-19~)
모멘텀 계열 전략(sepa_trend/gap_and_go/momentum_breakout/vcp_breakout)의 신규 매수
사이징에 KOSPI 실현변동성 기반 축소 배율 적용 (`utils/volatility_targeting.py`,
`engine.calculate_position_size` 시즈널리티 직후).

- 근거: Bongaerts et al. (FAJ 2020) — 조건부 타게팅이 모멘텀 Sharpe ~2배 개선.
  단 로컬 검증(KODEX200 2015~26)에서 **고변동 국면의 평균 수익은 오히려 높고
  꼬리만 2배 나쁨**(fwd20 하위5% -11.2% vs -6.4%) → 임계를 25%로 올려 재앙 국면만
  개입 (18% 임계는 CAGR 희생 과다). 채택안: Sharpe 0.721→0.787, MDD -40.8→-34.8%,
  CAGR -2.1%p.
- 동작: vol ≤ 25% 무개입 / vol > 25% → ×max(0.4, 25/vol). 축소 전용, 청산 무관.
- 캐시 일 1회 갱신(08:30, `run_vol_targeting_scheduler`), 노후 3일+ 시 fail-open(×1.0).
- 일수익률 |12%| 초과 이상치 제외 (FDR 데이터 오염 실측 대응).
- 비활성화: `VOL_TARGETING=0`

### 손절선 초과 방치 워치독 (2026-08-19~)
- exit_exempt(자동매도 금지) 종목은 제외 (2026-09-03) — 침묵이 사용자 지시인 종목의 상시 오경보 방지
저녁 품질검증 잡에서 미실현손실 **-12% 미만**(최대 명목 손절 10% + 여유) 포지션을
감지하면 WARNING 로그 + 텔레그램 경보. 6월 sync_detected 청산 급증(5월 3건 →
6월 13건, 코어 2건은 5~13일 방치 후 -20% 수준 발견) 재발 감시용 최후 안전망 —
근본 원인 계열(재시작 등록 누락·코어 파라미터 소실)은 8월 초 exit_manager 보강으로
해소됐고, 이 워치독은 청산 경로가 다시 침묵할 경우를 잡는다.

### 일일 한도
| 항목 | 값 | 비고 |
|------|---|------|
| 일일 최대 손실 | -5.0% | effective_daily_pnl **÷ total_equity** 기준 (2026-06-23 변경 — 외부 계좌 합산 시 .env INITIAL_CAPITAL 왜곡 회피, 대시보드와 일치) |
| 일일 거래 횟수 | 10회 | daily_max_trades — BUY **주문 단위** 카운트 (8/5: 부분체결 증분 중복 제거, `_counted_buy_order_ids`), BUY 체결 시 즉시 영속화 + DB 백필은 KR 심볼 한정·SELL 존재와 독립 |
| 최대 포지션 수 | 8개 | max_positions (잔여 비율 가중 카운트) |
| 기본 포지션 비율 | 25% | equity 대비 |
| 최소 현금 보유 | 5% | total_equity 대비 |

### 스마트 사이드카 (일일 손실 구간별)
| 구간 | 동작 |
|------|------|
| -3.5% ~ -5% (경고) | 시장 회복세면 허용, 하락세면 차단 |
| -5% ~ -12.5% (한도) | 방어 전략(RSI2/core/SEPA)만 허용 |
| -12.5%+ (하드스탑) | 전면 매수 차단 |

### 포트폴리오 동기화 (trading_lock, KR 전용)
> **배경**: 대형 손실 10건 중 7건이 KIS API 일시 응답 지연 → 복구 과정 비정상 상태에서 신규 진입 (03-27 DB손해보험 -14%, SK하이닉스 -11.89% 등)

- 연속 3회 실패 → `_sync_healthy=False` → **매수 차단** (`can_open_position()` 1.5단계)
- 1회 성공 → 즉시 복구 + 타임스탬프 초기화
- **타임아웃 안전장치**: 차단 지속 10분 초과 시 CRITICAL 로그 + 강제 해제 (운영 연속성 보장)
- 차단 로그: `[리스크] 동기화 복구 중 신규 매수 차단 ({symbol})` — 심볼별 60초 쿨다운
- 구현: `src/risk/manager.py` (`_sync_healthy`, `_sync_unhealthy_since`, `_sync_timeout_minutes=10`)
- 호출 경로: `kr_scheduler._sync_portfolio()` 성공/실패마다 `set_sync_status()` 호출 → `engine.on_signal → _risk_validator.can_open_position` 게이트에서 차단

### 당일 손절 종목 V자 반등 재진입 (2026-05-02~05-04)
> **배경**: 주간 후속복기(W18) stop_loss 24건 중 17건(71%)이 매도 후 +3%↑ 상승. 강세장에서 V자 반등을 못 잡는 패턴.

- **차단 해제 조건** (`risk/manager.py:_check_stop_loss_rebound`):
  1. 청산 후 30분 이상 경과 (즉시 추격 방지)
  2. 청산가 대비 +5% 이상 재돌파 (명확한 반등 확인)
- **1회 제한 (5/4 P0-A)**: V자 재진입 사용 후 재손절 → 당일 영구 차단
  - `_stop_loss_rebound_used` set으로 마킹 (8/5: `stop_loss_rebound_used.json`
    파일 영속화 — 재시작 시 1회 제한 우회 방지)
  - daily_max worst case 6.25% (1종목 2회 손절) → 5.0% 회귀
  - **1회권 소모 시점 = 매수 체결 확인 (8/5 P1)**: `on_buy_filled()` —
    can_open_position 검증 통과 시점 마킹은 후속 게이트(현금/브로커 거부)에서
    매수 무산 시에도 당일 차단되는 오차단을 만들었음. fill_check BUY + sync
    신규 포지션 두 경로에서 호출
- **단축 평가**: V자 통과 시 다음 `_exited_today` 분기에서 재차단 안 됨 (`stop_loss_rebound_passed=True`)
- 로그: `[재진입] {symbol} 손절 후 V자 반등 감지 — 재진입 허용 (V자 반등 +X.X% (>=+5%))`

### 동일 종목 재진입 제한 (당일 청산 후, KR 전용)
- 30분 쿨다운 + 가격 조건 (`check_reentry_condition`):
  - **-5%~+5%**: 눌림/횡보 → 재진입 허용 (5/2 -3→-5% 완화)
  - **+5% 초과**: 재돌파 → 재진입 허용
  - **-5% 미만**: 급락 중 → 차단
- 부분 청산은 `_exited_today` 미등록 (5/3 P1-4) — 잔여분 손절 시 잘못된 기준선 방지
- **등록 경로 독립화 (8/5 P1)**: `risk_manager.record_exit()`는 trade journal 기록과
  독립된 선행 블록에서 실행 (저널 예외 시 재진입 차단이 무음 실패하던 것).
  `is_full_exit`는 매도 전 스냅샷 수량 비교 + ExitManager 상태 소멸로 판정

### 당일 청산 누적 쿨다운 (D+1 분리, KR/US 공통)
> **배경**: 4/14 -8.42% 사고 — 단일일에 다수 청산 + 다수 신규 매수 동시 발생, SK하이닉스 저점 청산 후 +16% 반등을 미스. "청산 당일은 현금 유지, 다음 거래일에 신규 진입" 규칙으로 교체.

- 카운터: `RiskManager._daily_exit_count` (+ `_daily_exit_count_date`) — `record_exit()` 호출 시 +1, 날짜 롤오버 자동 리셋
- 차단 로직: `can_open_position()` 마지막 단계(섹터 제한 뒤) — 다른 차단 사유(일일 손실/동기화/포지션 수)가 모두 우선
- 설정: `RiskConfig.daily_exit_cooldown_threshold: int = 3` (0이면 비활성 안전장치)
- 호출점: `src/schedulers/kr_scheduler.py` fill_check의 SELL 체결 기록 두 경로 (기존 `record_exit` 호출점 재사용, 신규 삽입 없음)
- 로그:
  - 카운터 증가: `[리스크] 당일 청산 누적: {n}/{threshold} ({symbol} @ {price})`
  - 차단: `[리스크] 당일 청산 {n}건 누적 — 신규 매수 차단 ({symbol}), 다음 거래일 재개 예정` (심볼별 60초 스팸 방지)
- 리셋: `reset_daily_stats()` (날짜 변경 감지 시) + `can_open_position()` 내부 방어적 날짜 체크
- 안전장치: threshold=0 이면 규칙 비활성 / 다른 차단이 우선이므로 기존 로직 회귀 없음 / 카운터는 KR/US 둘 다 증가하지만 US는 `max_daily_new_buys`가 이미 유사 기능 보완

## US 리스크

| 항목 | 값 |
|------|---|
| 일일 최대 손실 | -3.0% |
| 최대 포지션 수 | 10개 |
| 연속 손실 중단 | 3회 → 사이징 50% 축소 |
| 최소 현금 보유 | 10% |

## 크로스 전략 검증 (src/core/cross_validator.py)

### 10개 규칙 (KR) — 2026-05-03 패널 추천 추가

| 규칙 | 조건 | 효과 |
|------|------|------|
| 1 | RSI>70 + 추세 전략 (bull 제외) | **-5점** |
| 2 | 기관+외국인 동시 순매도 | theme/momentum/gap: **차단**, sepa_trend: **-10점** |
| 3 | 약세장 + theme_chasing / gap_and_go / rsi2_reversal / momentum_breakout | **차단** |
| 3-2 | caution + gap_and_go (KR, 2026-05-28) | **차단** (5/28 -357k 사고) |
| 3-3 | sideways/neutral + sepa_trend (KR, 2026-06-14) | **차단** (6/12 SK하이닉스 -272k 사고) |
| ~~3-4~~ | ~~14:30+ gap_and_go 일 2건 초과~~ | 🚫 **폐지 (2026-08-19)** — 6월 부검: 14:30+ 진입 5건은 +138k 흑자, 사고 진입은 이틀 전 오전. 갭EOD 가드로 대체 |
| 4 | 동일 섹터 N종목+ (KR=2, US=3) | **차단** |
| 5 | 당일 손절 동일 섹터 재진입 | -5점 |
| 6 | 등락률/ATR > 1.5 (추격매수) | -15점 (hard block, cap 예외) |
| 7 | MA200 하방 + 추세 추종 | **-5점** |
| 8 | 적자+고PBR (-10), 극단PER>50 (-5) | -5~10점 (적자+고PBR은 hard block, cap 예외) |
| 9 | 거래 메모리 L3 보정 | ±3점 |
| **10** | **전문가 패널 추천 (BUY only, 2026-05-03)** | **+max(2, conv×10×freshness)** |

### 누적 감점 cap (2026-05-03)
- **최대 누적 감점 -15점** 제한 (이전 최대 -26점 → 60-70점대 우수 종목 자동 차단 역설 방지)
- Hard block 예외 화이트리스트: `추격매수`, `RSI과매수`, `적자+고PBR` (단독 차단 의도 보존)
- 적용 위치: `cross_validator.py` 규칙 9 직후

### LLM 이중검증
- 조건: 점수 85+ AND 비강세장
- 한도: **10회/일** (비용 제어)
- 모델: GPT-5.4 (STRATEGY_ANALYSIS)
- 프롬프트 컨텍스트:
  - 지표 + 거래메모리 + Wiki 교훈
  - **regime 결합** (LLM regime + 패널 regime 보수적 결합 — 둘 중 bear → bear)
  - **주간 매크로 리스크** (전문가 패널 risk_factors 상위 5건/250자, 빈 시 가이드 미출력)
- fail-open: LLM 장애 시 매수 차단보다 기회 손실 방지 우선

### LLM 이중검증
- 조건: 점수 85+ AND 비강세장
- 한도: **10회/일** (비용 제어)
- 모델: GPT-5.4 (STRATEGY_ANALYSIS)
- 프롬프트 컨텍스트: 지표 + 거래메모리 + **Wiki 교훈**
- fail-open 의도: LLM 장애/한도 소진 시 매수 차단보다 기회 손실 방지를 우선. 규칙 1~9의 결정론적 게이트가 1차 안전장치.

## 청산 관리 (src/strategies/exit_manager.py)

### US 전략별 max_holding_days 배선 (2026-08-03~)

이전에는 US 전략 config의 `max_holding_days`(예: SEPA 20일)가 ExitManager에
전달되지 않아 **전 전략이 글로벌 기본 10영업일**로 강제 청산됐다 (배선 갭).

- `us_scheduler._strategy_max_holding(eng, strategy_value)` — 포지션의 strategy
  문자열로 전략 인스턴스의 `max_holding_days`를 조회. 미매칭/미설정이면 None → 글로벌
- **배선 지점 (2곳)**: ① 매수 체결 등록 ② 재시작 복구 재등록
  (`pos.strategy or _symbol_strategy` 폴백). ExitManager 상태 파일이
  `max_holding_days`를 영속화하므로 재시작에도 유지된다
- **제외**: sync_detected 포지션(전략 불명 외부 진입)은 의도적으로 글로벌 10일 유지
- `0`은 '무제한' 의미이므로 falsy 판정 없이 그대로 전달 (`is not None` 비교)
- 적용 결과: SEPA 신규 매수 10→**20영업일**, earnings_reversal(비활성) 3일,
  momentum은 config에 값이 없어 종전대로 글로벌 10일. **기존 오픈 포지션은
  상태가 이미 저장돼 있어 종전 10일 유지** — 신규 매수부터 적용

### 종목별 자동매도 절대 금지 (exit_exempt / no_auto_exit_symbols, 2026-06-23~)
- **용도**: 수동 풀매수·장기보유 종목을 모든 자동매도 로직에서 영구 제외 (코어보다 강한 보호 — 코어는 리밸런싱 교체 가능하나 이건 그것도 면제).
- **설정**: `config kr.no_auto_exit_symbols: ['087010', ...]` — 기동 시 `run_trader._initialize_kr`가 `exit_manager.add_exit_exempt()`로 복원(재시작에도 유지).
- **차단 경로 (7개, 누락 시 손절/청산 발생)**:
  1. ExitManager `update_price` 진입부(`_exit_exempt`) → 손절·트레일링·분할익절·stale·보유기간초과 일괄
  2. `kr_scheduler._check_exit_signal` (WS 실시간) → 즉시 return
  3. `kr_scheduler._run_position_eod_llm_check` → LLM 종가점검 청산 제외
  4. `batch_analyzer.monitor_positions` 루프 → RSI2 청산·보유기간초과·ExitManager 릴레이 스킵
  5. `batch_analyzer._preemptive_stale_exit_on_bear` → 약세장 선제 stale 청산 스킵
  - 코어 경로(rebalance/stale/early-warning)는 `strategy == "core_holding"` 한정이라 strategy="manual" 종목엔 미적용.
  6. `kr_scheduler._sync_portfolio` 유령 제거 — KIS 부분 응답 1회로는 제거하지 않고 3주기 연속 누락에서만 (2026-09-13, 이전엔 부분 응답 시 즉시 삭제 + ExitManager 상태 소실)
- **수동 매수**: `config kr.manual_buy_orders: [{symbol, name, exit_exempt}]` → 기동 시 1회 실행(보유 시 자동 스킵). KIS 시장가는 주문가능금액을 상한가 기준으로 계산하므로 marketable 지정가(현재가+0.6%)로 전액 체결.
- ⚠️ **손절 부재 = 하락 100% 노출.** 청산은 전적으로 수동 판단. (펩트론 087010: 2026-06-23 사용자 지시로 전액 매수 + 손절 면제)

### 분할 익절 단계
| 단계 | 조건 | 매도 비율 | 누적 |
|------|------|----------|------|
| 1차 (FIRST) | +10% | **10%** | 10% |
| 2차 (SECOND) | +15% | 잔여의 50% (=45%) | 55% |
| 3차 (THIRD) | +25% | 잔여의 50% (=22.5%) | 77.5% |
| 트레일링 | 3차 후 | 잔여 전량 | 100% |

(코드 `ExitConfig` 기본값 기준 — 2026-09-13 문서 정정. 1차 +5%/20%는 2026-08-02 백테스트로 +10%/10%로 바뀐 값이 문서에 남아 있었음)

### ATR 동적 손절
- 공식: `max(min_stop, min(max_stop, ATR × multiplier))`
- `min_stop_pct`: **4.0%** (`ExitConfig`·`default.yml`·`evolved_overrides.yml` 모두 4.0 — 2026-09-13 문서 정정, 3.5는 구값)
- `max_stop_pct`: **8.0%** (기존 6.0에서 확대)
- `atr_multiplier`: 2.0
- 예: ATR 6% → max(4.0, min(8.0, 12.0)) = **8.0%**, ATR 1% → **4.0%**

### 본전 보호
- FIRST 단계 이후: **-0.5%** 도달 시 본전 청산 (코드 `sell_fee_buffer` — 2026-09-13 문서 정정, -1.5는 구값. NONE 단계는 -2.0%)

### ATR 연동 트레일링 (ATR-linked trailing)
- **배경**: SK하이닉스 4/13 일시 저점에서 고정 3% 트레일링에 조기 청산 → 4/14~ +16% 반등 누락. 매크로 노이즈에 과민.
- **공식**: `effective_ts = min( max(config_ts, ATR_pct × atr_link_multiplier), atr_link_cap_pct )`
  - `atr_link_multiplier = 1.2` (기본)
  - `atr_link_cap_pct = 6.0%` (상한선, 손실 확대 방지)
  - 하한: REGIME/전략별 `trailing_stop_pct` 존중
- **예시**:
  - ATR 5%, config_ts 3% → effective = min(max(3.0, 6.0), 6.0) = **6.0%**
  - ATR 2%, config_ts 3% → effective = min(max(3.0, 2.4), 6.0) = **3.0%** (기존 방식 유지)
  - ATR 10%, config_ts 3% → effective = min(max(3.0, 12.0), 6.0) = **6.0%** (상한 clamp)
- **비활성 조건**: ATR 미전달(fallback) / 코어홀딩(is_core=True, 고정 트레일링 우선)
- **전달 경로**: 매수 체결 시 `_pending_signal_cache[symbol].metadata.atr_pct` → `register_position(atr_pct_hint=...)`
- **로그**: 트레일링 발동 시 `ATR-linked trailing: 고점 대비 X% (한도=-Y%)` 형태로 출력
- **상태 저장**: `PositionExitState.effective_trailing_stop_pct` 필드에 보관
- **백테스트 동기화 (2026-08-02)**: `scripts/backtest_strategies.py`도 동일 공식을 사용한다.
  이전에는 백테스트만 고정 3%를 써서 실제보다 비관적인 결과가 나왔다
  (SEPA 3개월 -12.02% vs 실제 설정 -7.19%). 진화 백테스트 게이트가 올바른 판정을 내리려면
  **두 구현이 항상 같아야 한다** — 한쪽을 고치면 반드시 다른 쪽도 고칠 것.

### 1차 익절 재조정 (2026-08-02, 백테스트 검증)
- **변경**: `+5% / 비중 20%` → **`+10% / 비중 10%`**
- **배경**: SEPA 81건 청산 분해 결과 1차 익절이 27.2%를 차지하는데 **평균 1.9일**에 발동했다.
  평균 익절 `+5.20%` < 평균 손절 `-6.21%` — 손익 비대칭이 뒤집혀 있었다.
  반면 2차 익절(+15%)까지 살아남은 건은 평균 **+23.21%**.
- **검증**: 3·6개월 × 60·120종목 4개 시나리오에서 손익비 전부 개선
  (1.53→1.85 / 1.67→2.01 / 1.98→2.21 / 2.03→2.70), 수익률 3/4 개선.
- **트레일링은 건드리지 않았다** — 5.5~6.0%로 완화하면 오히려 악화(-7.28%, -7.56%).
  현재 4.5% + cap 6.0%가 적정.
- ⚠️ **변경 시 5곳을 모두 고칠 것**: `default.yml` / `evolved_overrides.yml` /
  `ExitConfig` 기본값 / **`REGIME_EXIT_PARAMS`**(레짐별 값이 config를 덮어쓰므로 누락하면 무효화됨) /
  **`run_trader.py`의 `_strategy_exit_params`**(전략별 오버라이드가 config보다 우선한다).
- 🩹 **2026-08-03 후속**: 위 5번째를 놓쳐 `sepa_trend`에 `+5%/20%`가 남아 있었고,
  검증한 `+10%/10%`가 SEPA에는 **한 번도 적용되지 않았다**. 해당 키를 제거해
  `register_position(first_exit_pct=None)` → config 상속으로 되돌렸다.
  같은 사고 방지를 위해, 전략별 오버라이드는 **그 전략만의 고유값**(stale_high_days 등)만 두고
  공통 파라미터는 config에 맡긴다.
- **한계**: 검증 구간이 2026-05~08 하락장에 집중. 3개월·120종목에서는 악화.
  상승장 도래 시 재검증 필요.

### 레짐별 파라미터 (trending_bull 예시)
> 출처: `src/strategies/exit_manager.py` `REGIME_EXIT_PARAMS` (2026-08-02 반영 후 실제 값)

| 항목 | trending_bull | neutral | ranging | turning_point | trending_bear |
|------|------|------|------|------|------|
| SL | 5.0% | 4.0% | 4.0% | 4.0% | 3.5% |
| TS | 4.0% | 3.0% | 2.5% | 3.0% | 2.0% |
| TP1 | 10% | 10% | 8% | 8% | 5% |
| TP2 | 15% | 12% | 8% | 10% | 8% |
| TP3 | 25% | 20% | 14% | 18% | 14% |
| stale_high_days | 7 | 5 | 4 | 5 | 3 |

## 시장 체제별 동적 파라미터 (market_regime.py → engine.py)

| 파라미터 | bull | neutral | sideways | bear |
|----------|------|---------|---------|------|
| min_score_adj | -10 | 0 | +3 | +10 |
| max_daily_new_buys | 6 | 4 | 3 | 2 |
| position_mult_boost | 1.2x | 1.0x | 0.9x | 0.7x |
| max_positions_adj | +2 | 0 | 0 | -2 |
| base_position_pct | 30% | 25% | 25% | 20% |
| min_cash_reserve | 3% | 5% | 5% | 10% |

bull 시 효과: max_positions 8→10, 현금 5→3%, 비중 25→30% → **현금 적극 배치**

## 코어홀딩 초과 비중 관리

| 기준 | 동작 |
|------|------|
| 코어 비중 35%+ | 텔레그램 경고 (24시간 쿨다운) |
| 코어 비중 40%+ | 금요일 14:00 초과분 50% 트림 |
| 개별 종목 20%+ | 15%까지 축소 (max_position_pct) |
| 비코어 pool | 코어 실점유분 차감 (초과 시 보호) |

## 코어홀딩 A안 청산 파라미터 (2026-05-11~)

| 항목 | 값 | 비고 |
|------|---|------|
| stop_loss_pct | 10% | 이전 15 → 10 (조기 손절 강화) |
| trailing_stop_pct | 12% | 이전 8 → 12 (느슨한 추세 추종) |
| trailing_activate_pct | 10% | +10% 도달 후 트레일링 시작 |
| 분할익절 ratio | 0/0/0 | OFF (장기 추세 끝까지) |
| ATR-linked trailing | 비활성 | `not is_core` 가드, 고정 12% 우선 |
| max_holding_days | 무제한 | 0 |

**stale 자동 컷 (evolved_overrides, 2026-06-04 밴드 확대)**
- Tier 1 알림: 20영업일 + **±7%** (이전 ±3 → 느린 손실 -5~7% 패턴까지 포착)
- Tier 2 자동매도: 30영업일+±7% OR 20영업일+**±5%**+거래량 50% 미만 (이전 ±3/±2)
- 변경 배경: 2026-06-04 오리온(-5.4%/32일)·SK(-6.9%/6일) 추세 진입 실패 종목이
  ±3% 사각지대에 갇혀 자동청산 못한 사고 후속

**리밸런싱 주기**: 격주 (`rebalance_interval_weeks=2`, 이전 월 1회)

**예산 (2026-06-04 정상화)**: 20% (이전 10%, KOSPI 강세장 복귀로 30% 시절 수준 복원
검토 차 우선 20% 단계 적용. strategic_swing 38.4→28.4 상쇄)

## 위험 기반 사이징 (2026-09-13~, `risk.sizing_mode: risk`)

리뷰 권고 ③. 근거는 `docs/research/exit-policy-ab-2026-09.md` — 청산(ladder/channel) × 보유(current/extended) × 사이징(nominal/risk)
2×2×2 백테스트에서 **사이징 축만** sepa 6m·12m 두 윈도우 모두 게이트 통과 (MDD -21.6→-5.9 / -28.7→-11.2, 회전 78→34~43배, PF 1.06→1.29 / 1.17→1.39).

> **2026-09-14 정정 (리뷰 후속 F1/T2, 계획 `docs/superpowers/plans/2026-09-13-review-remediation.md`)**: 이전 구현은 분모를
> ATR×2(4~8 클램프)로 만들었지만 **신규 체결 등록(`kr_scheduler` register_position)은 price_history 없이 호출되어 ATR 동적 손절이
> 생기지 않고 전략 고정 SL 이 적용**된다 (SEPA ATR 1%에서 분모 4% vs 실제 5%). 사이징 분모를 실제 손절 해석으로 교체했다.
> 선택지 A — ATR hint 로 동적 손절을 새로 켜지 않는다 (청산 전략 불변). 기존 12개월 A/B 결과는 이 정합 후 재계산 전까지 확정 근거가 아니다.

```
분모(진입 손절)  = ExitManager.resolve_stop(dynamic=None, fixed=_strategy_exit_params[strategy].stop_loss_pct, is_core)
                  = src/utils/stop_policy.resolve_effective_stop:  dynamic(min_stop 하한) > strategy(미클램프) > ExitConfig.stop_loss_pct,
                    비코어는 INTRADAY_CRASH_PARAMS[현재 급락 레벨].stop_loss_pct 를 상한으로 cap  ← update_price 손절 판정과 같은 함수
                  실효값: sepa 5.0 (default.yml) / gap 3.5 (evolved) / vcp 4.0 (run_trader 하드코딩) / 전략 SL 없으면 글로벌 5.0
                  ⚠ risk_config.default_stop_loss_pct(evolved 2.8)는 ExitManager 손절이 아니므로 쓰지 않는다. ATR·atr_pct_hint 는 분모에 무관.
1차 금액        = total_equity × risk_per_trade_pct(0.7%) / 분모(%)      상한 = total_equity × risk_max_position_pct(18%)
최종 불변조건    = 모든 오버레이(강도·전략 배율·캘린더·변동성·팀 부스트)·전략 예산·최소금액·최소 3주 보정이 끝난 수량 q 에 대해
                  planned_risk(q) = (price×q + FeeCalculator.calculate_buy_fee(price×q)) × 분모/100 ≤ total_equity × 0.7%
                  초과 시 q 를 줄인다 (src/utils/sizing.risk_quantity_cap). 예) 1천만·1만원·SL 5% → 139주 (140주 = 70,009.85 > 70,000)
```
- 분모는 `engine.risk_manager._resolve_entry_stop(strategy)` 콜백 — `run_trader` 가 `stop_policy.make_entry_stop_resolver(exit_manager,
  _strategy_exit_params)` 로 배선. **콜백 미배선 또는 실제 SL 까지 무효(0·음수·NaN)면 신규 매수 거부(수량 0, pending 미생성)** —
  nominal 자동 복귀 없음 (계획 §2.2). 로그 `[리스크] ... 진입 손절 해석기 미배선 / 진입 손절 해석 실패`.
- 증액 오버레이(캘린더 1.10·팀 1.10/1.20·LLM 배율)는 상한을 넘기지 못하고, 축소 오버레이(변동성 타게팅·LLM 감액)는 되돌리지 않는다.
  상한 내 1~2주는 허용하되 **재클램프 결과가 `min_position_value` 미달이면 명시 거부**. 왕복 수수료를 SL% 에 다시 더하지 않는다
  (KR 손절 판정은 매수 비용 대비 net 손익률). 시장가 증거금 1.3배는 별도 현금 제약.
- 코어홀딩 제외(nominal 경로 그대로). 신호 강도 배율·전략별 기본 비율(sepa 25/vcp 15/gap 15)은 **쓰지 않는다**.
  메타 `position_multiplier`가 ATR 배율 그대로면 무시(백테스트 risk 공식에 없음), 다른 값(LLM 감액 등)이면 그대로 곱함.
- 급락 cap 은 주문 시점의 레벨로 해석한다. 체결 시점에 레벨·레짐이 바뀌면 등록 SL 이 달라질 수 있다 — 차이는 T3 원장 스냅샷에 기록.
- 계측: 신호 메타 `sizing_mode="risk"`, `risk_stop_pct`, `stop_source`(strategy/global/dynamic) — T3 가 `entry_risk` 스냅샷으로 확장.
  **canary**: 매수 재개 후 첫 30건을 포지션 원장 R·KODEX200 초과수익으로 판정(자동 nominal 복귀 없음). 파라미터 3종은 BacktestGate
  `PARAM_MAP`(`risk.*`)에 매핑돼 진화 제안도 게이트 경유.
- 실제 고정 SL 기준이라 risk 모드는 전략별 거의 고정 비중(sepa 14%·gap 18% 상한·vcp 17.5%)이 된다 — ATR 적응 효과로 해석하지 않는다.
- 백테스트 nominal(25% 고정)은 실엔진 nominal(전략 비율×강도×ATR 배율 ≈ 23~28%)과 완전 동일하진 않다 — 상대 비교로만 해석.

- **급락 cap 은 분모에 적용하지 않는다** (`resolve_stop(apply_crash_cap=False)`, 2026-09-14 리뷰 P2): cap 은 해제되면 SL 이 원래 값으로
  돌아가므로 임시 SL 2.5 로 나누면 급락 중 포지션이 커지고(180주) 해제 후 계획 위험 0.9% 가 된다. 신호 메타 `stop_crash_active` 로 주문 시점 cap 활성만 표시.
- **손절 설정 무효 시 청산 판정은 멈추지 않는다**: `resolve_effective_stop` 은 0·음수·NaN 을 ValueError 로 거부하지만 `update_price` 는 이를 잡아
  T2 이전 우선순위(무효 항목은 ExitConfig 기본값)로 폴백해 손절·익절·트레일링을 계속 판정한다(경고 1회). 사이징 경로는 그대로 거부(fail-closed).
- **한계(명시)**: 계획 위험 상한은 주문 시점 보장이다. 레짐 전환 시 `apply_regime_params` 가 비코어 포지션 SL 을 레짐값으로 덮어쓰는 기존 청산 정책
  (gap 3.5 → bull 5.0 이면 위험 0.9%)과 갭·슬리피지는 포함하지 않는다 — T3 원장 스냅샷(`planned_vs_filled_risk_delta`)과 canary 판정 기준에서 별도 분류.

## 진입 위험 스냅샷 원장 (`entry_risk`, 2026-09-14~ 계획서 T3/F4)

risk 모드 주문의 계획 위험을 **고정 스키마 JSON**으로 만들어 신호 → 주문 캐시 → `signal_events` →
체결 원장(`market_context.entry_risk`) → canary 원장까지 같은 값으로 흘린다. 코드: `src/utils/entry_risk.py`
(순수 함수, 네트워크·DB 없음) / 원장 생성 `scripts/export_risk_ledger.py` / 판정 `scripts/review_risk_canary.py`.

**기준 결함(F4)**: 09-13 risk 태그(`sizing_mode`/`risk_stop_pct`/`stop_source`/`stop_crash_active`)가 원본
`Signal.metadata` 에만 있고 **별개 dict 인 `event.metadata`**·주문 캐시·체결 원장에는 없어 원장에서
risk 모드 체결을 골라낼 수 없었다. 이제 스냅샷을 `event.metadata` 와 `event.signal.metadata`
**양쪽 별개 복사본**에 넣는다(이벤트를 공유 참조로 바꾸지 않는다).

```json
{"version": 1, "cohort_id": "risk-sepa_trend-v1", "sizing_mode": "risk", "strategy": "sepa_trend",
 "stop_basis": "net_pnl", "stop_pct": "5.0", "stop_source": "strategy", "stop_crash_active": false,
 "equity_at_decision": "10000000", "risk_budget_amount": "70000.0", "planned_price": "10000",
 "planned_quantity": 139, "planned_risk_amount": "69509.75",
 "signal_ts": "2026-09-14T10:30:00+09:00", "applied_sha": "<체크아웃 SHA>", "config_hash": "<sha256>"}
```
- `cohort_id` = `risk-<strategy>-v1`. 금액은 문자열 Decimal, 시각은 ISO8601, 수량은 int.
- `config_hash` 는 **자격증명 제외 allowlist**(`risk`/`kr`/`us`/`exit`/`strategies`/`factor_budgets` 등 섹션 +
  key/secret/token/cano/url 류 키 제외)만 sha256 한다. `applied_sha` 는 현재 체크아웃 SHA —
  **모듈 로드 시(프로세스 시작) 1회 선계산**하고 `applied_sha()` 는 캐시만 읽는다(조회 실패 시 `unknown`).
  사이징은 동기 경로라 이벤트 루프에서 `git` 프로세스를 띄우지 않는다.
- **집계 시 `signal_events` 는 `event_type=passed` 로 거른다.** G3 리스크 검증(`_risk_validator`/
  `can_open_position`)에서 거부된 이벤트에도 스냅샷이 남으므로(수량 0 만 제외 대상) blocked 행을 세면
  주문 없는 계획 위험이 섞인다 — 엔진 동작은 그대로 두고 집계 쪽에서 거른다.
- 생성 시점은 **모든 오버레이·최소금액·3주 보정·위험 상한이 끝난 최종 수량 확정 후**. 수량 0(주문·pending 미생성)에는
  스냅샷을 남기지 않는다 — 주문 없는 계측 태그 금지.

**R 정의**: 완결 포지션의 순손익(수수료 포함) ÷ **최초 진입 주문 완료 시 확정한 위험금액**.
- `planned_risk_amount` = (price×q + 매수수수료) × net SL% — T2 사이징 상한과 같은 식(주문 시점 계획값).
- `initial_risk_amount` = 체결 후 `confirm_initial_risk(fills, actual_stop_pct)` = Σ(체결가×수량) × 실제 초기 SL%.
  canary 의 `entry_cost`(수수료 제외 Σ price×quantity) 정의와 같은 분모라 기술 검증이 재계산으로 대조할 수 있고,
  매수수수료만큼의 차이는 `planned_vs_filled_risk_delta` 에 남는다(원장 예시 -9.75).
- 부분체결은 주문이 끝날 때까지 누적해 한 번에 확정한다. **확정된 분모는 부분매도·레짐 전환·재시작으로 바뀌지 않는다**:
  `ExitManager.set_initial_risk(symbol, amount, stop_pct)` 는 최초 1회만 기록하고(이미 있으면 False),
  stage 파일(`exit_stages_YYYY-MM-DD.json`)의 `initial_risk_amount`·`actual_stop_pct` 로 영속화·복원된다(구 파일에 키가
  없으면 None = 미계측, 기존 스키마 호환).
- 별도 추가 매수는 주문별 위험을 분리 기록한다. 원장에서 lot 구분이 불가능하면 `lots_ambiguous: true` 로 표본에서 제외.
- 확정값은 **체결 시 `merge_confirmed_risk(entry_risk, initial_risk_amount, actual_stop_pct, fills)`
  로 스냅샷에 병합해 `market_context.entry_risk` 에 저장한다**(`initial_risk_amount`/`actual_stop_pct`/
  `filled_quantity`/`entry_cost`/`planned_vs_filled_risk_delta` 추가). 순수 함수·원본 불변이고,
  이미 확정값이 있으면 그대로 돌려주므로 복수 부분체결에서 2회차 이후 호출이 분모를 바꾸거나 중복 기록하지 않는다.
  ExitManager 상태는 완전 청산 시 삭제되므로 **closed 거래의 분모는 원장 스냅샷이 유일한 출처**다.

**exporter 의 분모·순손익 규칙** (`scripts/export_risk_ledger.py`)
- `initial_risk_amount` 우선순위: ①스냅샷 확정값 → ②**open 포지션에 한해** ExitManager 영속 상태 →
  ③매수 체결 × `entry_risk.stop_pct` 재계산. closed 에 상태를 적용하면 같은 종목 재보유 시
  현재 포지션 값이 옛 거래에 오귀속된다.
- `net_pnl` 은 **원장의 누적 `trade.pnl`(수수료 포함)** 이 정본. 저널 `record_exit` 는 `exit_price` 를
  마지막 leg 로 덮어쓰고 `exit_quantity` 만 누적하므로, 체결 재구성값을 쓰면 분할 매도 건이 체계적으로 틀린다
  (재현: 139주 @10,000 → 13주 @11,000 + 126주 @10,700 에서 97,828 vs 93,936, R 1.408 vs 1.352).
- 매도 leg 은 `--source db` 에서 `trade_events` SELL 행으로 복원한다. journal 소스라 복원이 불가능한
  분할 매도는 `exits_aggregated: true` + `lots_ambiguous: true` 로 **표본에서 제외**한다
  (fills 는 누적 pnl 과 정합인 평균 체결가로 되돌려 기술 검증만 통과시킨다).
  → **canary 판정용 원장은 `--source db` 로 뽑을 것.**

**legacy/unmeasured**: 과거 거래·수동 포지션처럼 `market_context.entry_risk` 가 없는 건은 원장에 `entry_risk: null` /
`cohort_id: "legacy-unmeasured"` 로 내보낸다. **현재 설정으로 초기 위험을 추정해 canary 표본에 넣지 않는다.**

**원장 만들기(오프라인, 주문·설정 변경 없음)**
```bash
venv/bin/python scripts/export_risk_ledger.py --source db --output ledger.json --days 90
venv/bin/python scripts/review_risk_canary.py --input ledger.json --cohort risk-sepa_trend-v1 --output report.json
```
`--source db` 는 `TradeStorage` 연결을 재사용하되 `market_context` 를 포함해 직접 SELECT 한다
(`TradeJournal.sync_from_db` 는 그 컬럼을 조회하지 않아 스냅샷이 유실된다).

**배선 현황(2026-09-14)**: 엔진(스냅샷 생성·양쪽 메타·`_log_sig` allowlist)·저장(TradeStorage JSONB / TradeJournal JSON
그대로 직렬화)·ExitManager 영속·exporter·canary 왕복에 이어 **KR 체결 경로(`src/schedulers/kr_scheduler.py`)까지 배선 완료**.
배선 이전에 체결된 거래의 원장은 그대로 `legacy-unmeasured` 다(소급 추정하지 않는다).

### A 배선 (`run_fill_check` 의 BUY 체결 분기)

주문 단위 누적 `_entry_fill_lots[f"{order_id}|{symbol}"]` 하나가 전 과정을 운반한다.
전부 **계측 전용** — 어떤 실패도 매수·등록·청산에 영향을 주지 않는다(경고 로그 후 생략).

1. **체결 누적·스냅샷 확보** (`_capture_entry_fill`, FillEvent emit **전**):
   `{price, quantity, fee}` 를 주문별로 쌓고, 첫 체결에서 `_pending_signal_cache[symbol]` 을 복사해 둔다.
   엔진 `on_fill` 이 **주문 완결 시** 그 캐시를 비우므로(부분체결은 유지) emit 이후에 읽으면 이미 늦다.
   ATR hint·저널 메타·`entry_risk` 는 전부 이 복사본을 읽고, 엔진 캐시는 **pop 하지 않는다**
   (수명은 엔진 `on_fill` 완결·`clear_pending` 한 곳에 맡긴다).
2. **완결 판정**: `check_fills()` 직후의 `broker.get_open_orders()`(인메모리) 에 주문 id 가 없으면 완결.
   단일 체결로 끝나는 일반 경로는 첫 체결에서 바로 확정된다. 주문 id 를 읽을 수 없거나 조회가 실패하면
   **확정하지 않는다** — 과소 분모를 박느니 스냅샷의 `initial_risk_amount` 를 비워 두고 경고를 남긴다.
   그 경우 원장 분모는 **exporter 가 저널 체결 × 계획 SL 로 재계산**한다(`export_risk_ledger.py` 3순위).
3. **확정** (`_confirm_entry_risk`, ExitManager 등록 성공 **직후**, 주문당 1회):
   `resolve_stop(dynamic_stop_pct=None, fixed_stop_pct=exit_params["stop_loss_pct"],
   is_core=..., apply_crash_cap=False).stop_pct` — **등록에 쓴 것과 같은 설정·같은 창구**.
   → `confirm_initial_risk(fills, stop_pct)` → `ExitManager.set_initial_risk(...)`.
   `ValueError`(무효 손절·체결 0) 는 확정만 생략한다.
4. **원장 기록** (`_entry_risk_context`): `_sig_context_snapshot` 머지 **이후**,
   `record_entry` **이전**에 `market_context["entry_risk"] = merge_confirmed_risk(...)`.
   스냅샷이 없으면 키 자체를 만들지 않는다(exporter 가 `legacy-unmeasured` 로 분류).
   확정 전이면 계획값만 들어가고, 확정 시 `initial_risk_amount`/`actual_stop_pct`/`filled_quantity`/
   `entry_cost`/`planned_vs_filled_risk_delta` 가 붙는다.
5. **부분체결 뒤늦은 확정** (`_sync_journal_entry_risk`): 첫 체결에 이미 `record_entry` 된 레코드는
   `trade_journal.update_market_context(trade_id, {"entry_risk": ...})` 로 갱신한다
   (`TradeJournal` JSON 캐시 + `TradeStorage` 의 `UPDATE trades SET market_context=$1` 큐 — 기존 kwargs 시그니처 불변).
   멱등: `merge_confirmed_risk` 가 이미 확정값이 있으면 그대로 돌려주고, `journal_synced` 로 중복 갱신을 막는다.
   **완료한 진입의 분모 불변**: 같은 종목의 두 번째 매수 주문 lot 은 `set_initial_risk` 가 False 를 돌려주므로
   (`em_confirmed=False`) 저널을 건드리지 않고, 레코드에 이미 `initial_risk_amount` 가 있으면 갱신을 건너뛴다.
6. **등록 재시도**(`_pending_exit_registrations`) 성공 시 같은 확정을 재호출한다(멱등).
   그때까지 해당 종목의 미확정 누적은 정리하지 않는다.
7. **계획 SL ≠ 실제 SL**: 스냅샷의 `stop_pct`(계획)는 덮어쓰지 않고 `actual_stop_pct` 에 실제값을 둔다.
   → canary 기술 검증 `stop_pct_mismatch` 가 발화한다(무음 통과 금지).
8. **누수 방지**(`_prune_entry_lots`): 매 주기 끝에 미체결 목록에 없는 주문(완결·취소·만료)의 누적을 버린다.
   단, ExitManager 등록 재시도 대기 중인 종목의 미확정 누적은 남긴다. 완결 판정 불가 시에는 정리하지 않는다.
   버리기 전에 **주문 종료 = 주문 완료**로 보고 누적 체결로 확정한다(부분체결 뒤 잔여 취소·일자 전환).
   등록 설정이 없어 확정할 수 없으면 `[위험계측] ... 주문 종료 — 초기 위험 미확정` 경고를 남긴다.

> 다중 부분체결 진입은 저널 `entry_quantity`/`entry_price` 가 **첫 체결**로 고정된다(BUY 블록은
> `trade_id` 미설정 시에만 돌고 갱신 API 가 없다). exporter 는 스냅샷에 `filled_quantity`/`entry_cost` 가
> 있으면 매수 fill 을 그 값으로 만들어 `initial_risk_mismatch` 오탐을 막는다.

테스트: `tests/test_entry_risk_lifecycle.py`(from_signal → 사이징 → 주문 캐시 → signal_events → 저장/복원 → exporter → canary),
`tests/test_entry_risk_wiring.py`(단일 체결 확정 139주×10,000×5%=69,500원 · 부분체결 1회 확정·저널 갱신 ·
등록 재시도 후 확정 · 스냅샷 없음 · 손절 무효 · 완결 판정 불가 · 캐시 수명 · SL 불일치 · exporter→canary 왕복).

## ATR 포지션 사이징 (src/utils/sizing.py)

```
ATR ≤ 2%  → 1.0x (정상 비중)
ATR  5%   → 0.7x (30% 축소)
ATR  8%   → 0.4x (60% 축소)
ATR ≥ 10% → 0.3x (70% 축소)
구간 내: 선형 보간
```

- `sizing_mode: risk`(2026-09-13~)에서는 이 배율이 손절폭에 흡수되므로 사이징엔 적용되지 않는다 (위 절).

### ATR=0 가드 (전 전략 통일)
- SEPA, RSI2, Gap&Go, US Momentum, US SEPA → ATR 0/None 시 **진입 차단**
- US Earnings Drift → 0.8x 폴백 (lenient, 갭 자체가 고변동)

## RLAY 유형 매도 무한루프 방지

- sell_qty > 실제 보유 수량 → **자동 클램핑**
- 연속 3회 매도 실패 → 포트폴리오 동기화 강제 + 카운터 리셋
- 매도 성공 시 → 쿨다운 + 실패 카운터 `delattr` 정리
