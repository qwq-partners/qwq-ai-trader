# P0-2 — attach 의 체결·종결 증거 생산자 (Plan, 2026-09-22)

> **상태: 완료(2026-09-22, engine `7500d3c`) — 부품·제품 호출자 0건·운영 미설치. 마감은 §7.** 사용자 결정 "엔진 전체를 옮기도록 하자"(2026-09-22)에 따른 attach 전환의 두 번째 전제 단계. 설치 차단 사유 **18**(attach 에 체결·종결 증거 생산자가 0건)과 **3**(fill projection)을 닫는다. 상위 결정은 `2026-09-22-kis-judgement-decisions.md` §5.

## 0. 과정과 판정

- Plan 워크플로(읽기 전용·pytest 0·제품 0줄): 조사 2(engine 부품의 실제 계약 / legacy 체결 경로와 attach 거부 지점, 요청 opus/high) → 설계(요청 opus/high) → 적대적 심사 2관점(요청 opus/xhigh).
- **심사 2건이 API 서버 오류(529 → 재시도 500)로 두 번 연속 실패**했고 대조 호출(opus, 한 줄 응답)도 500 이었다 — 공급자 장애. 재시도 한도(1회)를 썼으므로 정책대로 **교차 공급자로 대체: 심사 ① 을 Codex(요청 gpt-6-astra/xhigh)가 수행**했다. 심사 ②(코드 실현 가능성)는 초안이 BLOCK 이라 **개정본에 대해 실시**한다.
- **심사 ① 판정: BLOCK — must-fix 10 · 인용 오류 11 · legacy 보다 약한 지점 10.** 아래 §3.

## 1. 조사가 확정한 사실 (코드 기준, 줄은 HEAD `814a746`)

**engine 부품(전부 제품 호출자 0건)**
- 수집기 `queries.py::LegacyExecutionQueries.daily` — `TTTC0081R`, `PDNO=""`(계좌 전체), `CCLD_DVSN="00"`, `INQR_DVSN="01"`, `EXCG_ID_DVSN_CD=scope.exchange_scope`(기본 `"KRX"`). 출력은 `QueryCollection(pages: tuple[QueryPage])` 이고 파서가 받는 `EvidencePage` 와 **필드명·타입이 다르며 어댑터가 없다.** 종료는 응답 헤더 `tr_cont` 만. `finality_supported`/`trading_permission` 은 하드코딩 False. 리미터·환경 검사는 브로커 어댑터(`kis_kr.get_execution_daily`, `env != "prod"` 면 ValueError)에 있다.
- 파서 `evidence.py::parse_order_evidence` — 반환 상태는 `FINAL_FILLED` 아니면 `RECONCILING` 둘뿐(FINAL_CANCELLED 불가). supported 는 13 AND: `tr_id == "TTTC0081R"`·`session == "regular"`·`query_kind == "all"`·`ref.exchange == "KRX"`·`not chain`·수량 4조건 등. 행 매칭은 `(ord_dt, odno, ord_gno_brno, orgn_odno, excg_id_dvsn_cd) == ref` — **행의 거래소가 `ref.exchange` 와 문자 그대로 같아야** 하고 attach 의 `OrderRef` 는 `commands.py:506-508` 에서 `'KRX'` 를 **하드코딩**한다. **수집기와 파서의 어휘가 네 곳 어긋난다**(tr_id 는 같으나 `query_kind` "daily"↔"all", `query_scope` 키 이름, session 부재, 거래소 라벨).
- `lifecycle.reconcile(attempt_id, evidence, *, applied_quantity=None)` — `owner.mutate` 로 직렬화. 거부(조용히 False)·충돌(`evidence_conflict` 영구 래치)·정상 전이. `FillObservation` 을 만들지 않는다(참조 0건).
- 포트폴리오 반영 입구는 **하나**: `engine.apply_execution_observation(FillObservation)` → durable 접수(inbox) → `EXECUTION_FILL` 이벤트 → `runtime.apply_observation` → `owner.apply`(cursor 대비 `FillDelta`, 충돌이면 `NEEDS_RECONCILIATION`) → `_reduce`(economics/protection/…) → `_publish`(live `engine.portfolio`·ExitManager·RiskManager 를 **직접 대입**). `FillObservation` 생산자는 저장 행 재구성(replay) 뿐.
- `KRExecutionRuntime` 에는 **주기 task 가 하나도 없다** — 기존 task 는 전부 명령 1건당 단발(`_day_tasks` 예외 회수만 / `_protection_tasks` 는 실패 시 `_protection_failed` **영구 래치**). `shutdown()` 은 fixed-point drain.

**legacy 경로와 attach 의 단절**
- `run_fill_check`: 간격 `2 if open_orders else 15`(docstring 의 "2초/5초"는 코드와 다르다). `get_open_orders()` 는 **인메모리 캐시**(`_pending_orders`)라 attach(`submit_order` 미사용)에서는 항상 빈 리스트 → `check_fills()` 미호출. 두 번째 겹: 호출돼도 `_order_id_to_kis_no` 매칭이 전부 실패.
- 운영 조회 파라미터: `TTTC8001R`, **`CCLD_DVSN="01"`(체결만)**, `INQR_DVSN="00"`, **`EXCG_ID_DVSN_CD="ALL"`**, 읽는 키는 `odno`·`tot_ccld_qty`·`avg_prvs` 셋뿐. 원장 TR 예산: 8434R 이 하루 ≈1,100회, **체결 TR(8001R/0081R)은 현재 0회 — 예산이 통째로 비어 있다**(리미터 계좌당 1.05초).
- attach 의 거부 지점: `engine.py:541-547`(legacy FILL/ORDER/SIGNAL 거부) · `update_position`/`update_position_price`/`on_fill` 의 `ApplicationBlocked`.
- `KIS_TR_SET` 스위치는 **engine 브랜치에 없다**(main 의 `_TR_SETS` — 이식 전). 운영 기본 legacy 에서 수집기(신 TR 전용)와 제품의 TR 이 어긋난다.

## 2. 설계 초안 E1~E10 (요청 opus/high — 요약, BLOCK 됨)

E1 runtime 소유 단일 주기 task(`_day_tasks` 관례, `_protection_failed` 래치 불사용, shutdown 에서 cancel+drain) · E2 미해결 attempt 있을 때만 폴링, 3초 → 전이 없으면 2배 → 60초 상한 · E3 한 주기에 `reconcile` → `apply_execution_observation` 순서, 같은 수치 · E4 TR·범위는 부품 현행(`TTTC0081R`+KRX) 그대로, 파서 조건 불변 · E5 어댑터가 어휘 번역(`daily`→`all`, session 은 `request_binding['session']` 에서) · E6 `complete=False` 면 파서 미호출 · E7 `QueryPage`→`EvidencePage` 변환 · E8 `FillObservation` 은 evidence 에서 기계적으로(수수료 0·metadata 빈) · E9 판단 복제 0 · E10 factory 의 `install_gateway` 다음 줄에서 기동 + `execution_query_environment_required` 거부. 단계: 부품 3개 → runtime task → factory 배선.

## 3. 심사 ① (Codex gpt-6-astra/xhigh) — BLOCK, must-fix 10

| # | 결정 | 문제(요약) | 요구 변경 |
|---|---|---|---|
| 1 | E3 | **40/100 부분 체결 뒤 보호 SELL 이 막힌다** — `_evaluate` 가 같은 종목의 미종결 submit 이 있으면 방향과 무관하게 `unresolved_symbol_attempt` 로 거부(`commands.py:331`). 설계의 "부분 체결 뒤 보호 SELL 가능"은 코드와 반대 | 미체결 BUY 와 **검증된 보유분의 보호**를 분리 — 보유 40주 SELL 경로와 잔여 60주 추가 체결까지 보호 |
| 2 | E2·E3 | `reconcile` 성공 → apply 전 중단이면 재시작 뒤 포지션 0 이고 다음 조회가 실패하면 저장된 40주도 영영 안 적용. 더 나쁜 경로: 40주 inbox 접수 → 적용 FAILED → 다음 조회 100주 적용 성공 → 남은 40주 inbox·실패 ingress 가 **모든 명령을 막는다** | 조회와 독립된 **저장 관측 재처리** · 이전 누적 관측의 supersede·ingress 정리 · FAILED/NEEDS_RECONCILIATION/PARKED receipt 검사 |
| 3 | E3·E10 | P0-3 이전 활성화는 30초 sync 와 owner 게시가 보유·보호 상태를 번갈아 지운다 | P0-3 완료를 **실제 활성화 조건**으로 |
| 4 | E3·E4·E9 | chain(자식행)에서 "종결 포기"만 하고 **수량은 적용**한다 — "자식행이 보이면 수량 해석 포기" 전제 위반 | chain 관측에서는 새 수량을 적용하지 않는다(이미 적용분은 유지) |
| 5 | E4~E7 | 계좌가 TTTC0081R 을 거절하거나 필드 하나가 없으면 **조용히 무력** — 특히 파서가 계좌 전체 행을 먼저 파싱하므로 **무관한 주문 한 행의 결측이 우리 주문까지 무력화** | 활성화 전 계좌/TR/스키마/세션 지원 검증 · 마지막 유효 관측·미적용 시간·skip 원인을 health 에 노출 · 매칭 행만 엄격 파싱 |
| 6 | E4 | 주문이 NXT/SOR 로 체결되면 KRX 조회에 안 보이거나 `excg_id_dvsn_cd` 가 달라 매칭 실패 → 실제 노출 40주, owner 0주 | 지원 거래소 범위를 주문 허용 조건과 연결. `ALL` 을 `KRX` 로 라벨링하는 우회 금지 |
| 7 | E1 | 수집기 사망이 신규 노출을 막지 않는다 — 다른 종목 BUY 는 잔여 자금으로 계속 나간다 | 수집기 정상 동작을 **신규 BUY 의 조건**으로(보호 SELL 은 막지 않는다) |
| 8 | E2 | 60초는 손절 지연 상한이 아니다 — 미해결 0건 동안에도 delay 가 60초까지 커지고, 10페이지 × 15초 timeout 이 더해져 **≈210초**, 유한한 최악 상한 없음 | 새 attempt(ACK)에 **즉시 깨움** · 경제적 진전과 단순 accept 구분 · attempt 별 try/except |
| 9 | E8 | `exit_type` 누락 → 당일 손절 종목이 재매수 후보가 된다(legacy 는 `record_exit` + 스크리닝 제외). `entry_signal_score` 0 → 만석 교체의 `+5점` 검사가 뚫린다(**돈 경로다**) | 사유·점수를 브로커 응답이 아니라 **주문 intent/binding 에서** 잇는다 |
| 10 | E2·E5·E10 | 15:19 관측 저장 → 적용 전 중단 → 다음 날 재시작: "오늘"만 조회하면 전일 주문은 provenance 에서 거부, admission 도 닫힌다 | 미해결 주문일을 포함하는 조회와 전일 관측 적용 절차 |

**인용 오류(설계 초안이 틀린 것, 코드로 확인됨):** `unresolved_reason` 술어를 "그대로" 쓴다는 것(원 함수는 inbox·자식 명령·예약·cursor 까지 본다) · chain 이면 60초로 수렴한다는 예산(같은 수량·금액도 reconcile 은 True 를 돌려줘 3초로 복귀) · 페이지 중복만으로 영구 래치가 선다는 인과(실제 래치 반례는 같은 누적 수량의 금액이 변할 때) · `not_found` 가 상태 불변이라는 것(`owner.mutate` 는 version·live 게시를 전진) · 진입 점수가 "돈 경로 아님"이라는 것.

**legacy 보다 약한 지점:** 수집기 사망 중 신규 노출 허용(E1) · 유휴 backoff 의 첫 체결 지연(E2) · 관측/적용 분리 실패의 전 계좌 정지(E3) · ALL→KRX 축소와 미검증 TR·필드 계약(E4) · session 없는 attempt 영구 skip(E5) · 뒤 페이지 실패가 앞 페이지 체결까지 버림(E6) · 무관한 행 결측의 전면 무력화(E7) · 손절 재진입·교체 오판(E8) · 영구 충돌 제외의 광역 영향(E9) · sync 공존 미해결(E10).

## 4. coordinator 의 개정 방향 (개정 설계의 입력 — 확정이 아니다)

1. **must-fix 1 → P0-2 의 인수 조건으로 당긴다.** D4 의 "미해결 BUY 가 같은 종목의 보호 SELL 을 막지 않는다"는 원래 P2 였으나, 생산자가 생기는 P0-2 가 곧 그 전제다. 인수: 40/100 체결 뒤 **검증된 보유 40주의 SELL 이 owner 게이트를 통과**하고, 60주 예약은 그대로다.
2. **must-fix 2 → 생산자는 "이번 조회"가 아니라 "저장 상태"에서 적용을 유도한다.** 주기마다 (a) 조회가 성공했으면 reconcile, (b) 조회와 무관하게 `observed_quantity > applied_quantity` 인 attempt 전부에 대해 apply 를 시도한다. 누적 관측의 supersede(같은 attempt 의 이전 inbox 행)와 FAILED/PARKED receipt 처리는 `application.py` 의 실제 계약을 읽고 정한다 — 없으면 이 단계에서 만들지 않고 명명된 거부로 남긴다.
3. **must-fix 4 → 파서가 chain 을 evidence 에 노출**하고 생산자는 chain 이면 apply 를 건너뛴다.
4. **must-fix 5·7 → health 와 게이트.** `reconciler` 의 마지막 유효 관측 시각·미적용 attempt 수·skip 사유별 건수를 `health()` 에 노출. `_evaluate` 에 **BUY 한정** `reconciler_unavailable` 거부(task 죽음·N주기 연속 조회 실패). 파싱은 **매칭 후보 행(odno 일치)만 엄격**하게, chain 스캔은 `orgn_odno` 만 읽는다.
5. **must-fix 6 → 거래소를 하드코딩하지 않는다.** 조회는 운영과 같은 **`ALL`**, 매칭은 `(ord_dt, odno, ord_gno_brno)` 로 하고 `ref.exchange` 는 **행에서 읽어 채운다**(ACK 에는 거래소가 없다). supported 는 `excg_id_dvsn_cd` 가 허용 집합(`{"KRX"}` — Q27~29 확정 전) 안일 때만. chain 스캔도 ALL 범위.
6. **must-fix 8 → ACK 즉시 깨움.** `commands` 의 dispatch 가 ACK 를 받으면 runtime 의 `asyncio.Event` 를 set; 미해결 0건이면 sleep 없이 대기(조회 0회). backoff 는 "경제적 진전 없음"(observed 불변)에만. attempt 별 try/except.
7. **must-fix 9 → `request_binding` 에 `exit_type`·`entry_signal_score`·`strategy` 를 싣고** observation metadata 는 거기서만 만든다(브로커 응답에서 추측 0).
8. **must-fix 10 → 조회 구간 = [미해결 attempt 의 최소 `order_date`, 오늘].** 같은 프로세스 안에서 일자 전환 전이면 전일 체결도 적용된다. **재시작을 넘어가는 복구는 P3(차가운 시작)의 범위**로 명시하고, 설치기의 기존 거부(`startup_unresolved_prepared_attempt`·일자 전환)가 그 경계를 지킨다.
9. must-fix 3 → 이미 확정(P0-3 이 활성화 조건). 설치기에 `legacy_portfolio_writer_present` 류 거부를 P0-3 에서 넣는다.
10. **TR 결정(E4 재검토):** 운영 기본이 legacy 인 한 신 TR 전용 수집기는 어긋난다. main 의 `_TR_SETS` 를 engine 에 들이는 것이 선행이고, 수집기·파서는 **그 출처의 daily TR 을 읽되 종결 조건은 TR 값이 아니라 "응답 스키마 검증 통과"로** 건다. 이는 "tr_id 조건을 넓히는 것이 유일하게 설치 차단 사유를 코드에서 지우는 변경"이라는 초안의 우려를 **스키마 검증 + D10 기록기 확인**으로 대신한다. 이 항목은 개정 설계에서 다시 심사받는다.

## 5. 개정 설계 F1~F12 (요청 opus/high) 와 심사 2관점(요청 opus/xhigh, 둘 다 **REVISE**) → coordinator 처분 = **확정 설계**

**개정 설계의 골자(초안에서 뒤집은 것):** F1 backoff 를 **지운다**(정상 대기가 스스로 손절 지연을 키운다) — 대상 있으면 고정 3초, 없으면 `asyncio.Event` 에서 조회 0회로 잠들고 ACK 가 `runtime.notify_execution_change()` 로 깨운다 · F2 한 주기는 (A) 조회·reconcile·apply 와 (B) **조회와 무관한 저장 관측 재처리**를 따로 돈다 · F3 supersede 는 **새로 만들지 않는다** — `application.apply` 의 기존 계약(:511-522 stale 분기 → `_supersede` → `ALREADY_APPLIED` → engine 의 `resolve_previous_failures`)이 이미 한다(새 코드 0줄) · F4 파서가 `chain` 을 노출하고 chain 이면 reconcile 도 apply 도 하지 않는다(기록하면 `unresolved_execution_evidence` 가 전 종목 명령을 막는다) · F5 조회는 운영과 같은 **`"ALL"`**, 매칭 키에서 거래소를 빼고 **찾은 행의 거래소를 대조**(`exchange_mismatch`) — "행에서 읽어 ref 를 채운다"(§4-5)는 `record_result`·economics 가 KRX 를 강제해 **불가**하므로 기각 · F6 파싱 2단계(신원 4필드 방어적 스캔 → 후보 행만 15필드 엄격) · F7 health 7키 · F8 side 인지형 심볼 게이트(한 줄) + BUY 한정 `reconciler_unavailable` · F9 `FillObservation.metadata` 는 `request_binding['fill_metadata']` 에서만(identity 가 metadata 를 포함해 **강제**다 — 브로커 응답에서 만들면 두 번째 부분 체결에서 `ObservationError`) · F10 대상은 risk day 의 attempt·inbox 행만, 조회 구간도 그 하루(넓히면 economics 의 cross-day 거부 때문에 observed≠applied 로 전역 정지가 난다 — §4-8 기각) · **F11 TR 은 `TTTC0081R` 고정, `_TR_SETS` 이식 안 함, "스키마 검증으로 종결" 기각(§4-10 기각 — 스키마는 필드의 모양만 보고 의미가 다른 TR 에서도 통과하므로 그것으로 최종성을 인정하면 차단 사유 1·2 를 코드에서 지우는 것)** · F12 **factory 배선은 P0-2 에 없다** → P0-3.

**must-fix 대응(심사 재검증 반영):** 1·3·4 닫힘. 2·5·6·7·8·9·10 은 방향은 맞고 아래 처분으로 닫는다.

| 처분 | 내용 |
|---|---|
| P-1 | **주기 전체**(대상 선정~apply~health)를 `asyncio.wait_for(cycle_timeout)` 로 감싼다(초안은 조회만). `cycle_timeout ≥ request_timeout × 예상 페이지수` 를 명시하고 이 단계는 `max_pages` 를 낮춰 맞춘다. health 에 주기 시작 시각. |
| P-2 | `reconciler_blocked_reason()` 은 task.done/연속 미완결이 아니라 **"대상이 있는데 마지막 상태 전진 이후 경과 > k×interval"** 로 잰다(멈춘 주기·굶은 주기를 같은 문장으로). |
| P-3 | `unreadable_row > 0` 은 **chain 과 같게**(reconcile·apply 둘 다 skip, `chain_undecidable`). lenient 스캔도 `_parse_row` 와 같은 소문자 정규화·별칭 충돌 검사. |
| P-4 | A·B 두 집합 모두 `not day_admission_closed and state['risk']['day'] == now(KST).date()` 게이트 **뒤**(조회도 안 한다). health `day_admission_closed`/`prior_day`. 예산 절은 GET 수 + **주기당 checkpoint 커밋 수**(24시간)로 다시 쓴다. |
| P-5 | **reconcile 은 evidence 가 attempt 행을 바꿀 수 있을 때만** 부른다(`schema_valid` ∧ `order_quantity == attempt['quantity']` ∧ (누적 수량 증가 ∨ 금액 변화 ∨ 종결 전이 가능)). `not_found` 는 호출 자체를 건너뛴다 — 같은 응답 10주기에 `owner.version` 불변이 인수. |
| P-6 | B 를 두 갈래로: (B1) inbox 행 재접수 · (B2) **inbox 행이 없고 `observed > applied` 인 attempt** 는 저장된 `observed_quantity/observed_amount/order_ref/fill_metadata` 로 `FillObservation` 을 재구성(파서 경로와 **같은 함수** — `observation_id` 가 글자 단위로 같아야 supersede 가 맞물린다). |
| P-7 | 종료: 수집 task 는 cancel 이 아니라 `_closing` 으로 **자연 종료**하고 진행 중 apply 를 끝까지 기다린다. `runtime.apply_observation` 은 `_closing` 이면 거부. `shutdown` 의 drain 이 in-flight ingress task 를 본다. |
| P-8 | `prepare(request, context, *, sector=None, fill_metadata=None)` — 화이트리스트(`{name, sector, entry_signal_score, exit_type}`) 밖 키·JSON 비허용 타입은 **prepare 시점에** 거부, score 는 `None`/`float`. 자동 BUY 의 `entry_signal_score` 유한성은 **prepare 에서 `_require`** — 단, 독립 인수 37건 **또는 기존 시험**의 기대값이 바뀌면 P0-3 게이트로 미루고 그 사실을 보고한다. **Do 결과(coordinator 승인):** 37건은 GREEN 이었으나 `prepare` 를 직접 부르는 기존 시험 46건이 RED 가 되어 예외를 적용 — 유일한 제품 송신로인 `SignalGateway._fill_metadata` 가 BUY 의 점수 결측·비유한을 `entry_signal_score_required` 로 거부한다(돈 경로의 값은 실제 경로에서 보장). 게이트웨이를 우회하는 직접 호출자에 대한 prepare 시점 강제는 P0-3 게이트 범위. |
| P-9 | receipt 두 타입(`InboxReceipt`/`FillReceipt`)을 분기해 세고 PARKED 는 별도 카운터. |
| P-10 | **F8(게이트 변경)은 P0-3 으로 옮긴다**(심사 ① 지적: F12 가 생산자 배선을 P0-3 으로 옮겼으므로 "생산자 0건인 동안 게이트를 열지 않는다"는 D4 순서를 지킨다). P0-2 = 부품(1단계) + runtime 메서드(2단계)뿐. |
| P-11 | `valid_evidence_provenance` 의 superset 허용은 소비자 **세 곳**(evidence.py·lifecycle.py·**initial_r.py `_validate_finality`**) 모두. |
| P-12 | **새 설치 차단 사유 19 등록:** 정상 경로에서도 **체결마다 전 종목 명령 정지 창**이 생긴다(미적용 inbox 행 → `_owner_ready` 의 `unapplied_execution_observation`, 급락 때 체결과 보호가 겹치는 순간에 터진다 — legacy `check_fills` 는 매도를 막지 않는다). P0-2 는 창을 P-1 로 유한하게 만들고 health 에 노출한다; **`_owner_ready` 의 범위 축소는 P2(게이트 범위) 결정**. |
| P-13 | residual 추가: 부분 청산은 `exited_today` 조차 기록되지 않는다(economics 의 `if full:`) — P0-3/P1. F11 의 "PR #80 미병합·미배포" 인용은 낡았다(09-21 밤 병합·배포됨) — 논지("live 파일 0줄" 계약)는 유효. |

**단계(확정):** 1단계 부품(제품 호출자 0건·live 파일 0줄): `evidence.py`(2단계 파싱·`chain`·`exchange_mismatch`·`evidence_pages`·`parser_scope`) · `lifecycle.py`(`OrderEvidence.chain` 기본값 필드, provenance superset) · `initial_r.py`(provenance 소비자) · `application.py`(`observation_from_evidence` — 재구성 경로와 공용) · `queries.py`(`exchange_scope` 가법 kwarg) · `commands.py`(`_prepare` 의 binding `session`·`fill_metadata`, P-8) · `gateway.py`(BUY 의 score/name/sector 전달) → 2단계 runtime(`start_reconciler`/`reconcile_once`/`_reconcile_loop`/`notify_execution_change`/`reconciler_blocked_reason`/health, P-1·P-2·P-4~P-7·P-9) → **P0-3**: 게이트(F8)·factory 배선·`_sync_portfolio` attach 분기·`exit_type`(`_classify_exit_type` 재사용)·실계좌 스모크(D10).

**바뀌지 않는 것:** `trading_ready` 상수 False · 제품 호출자 0건 · MODIFY 미지원 · 운영(legacy) 무변경 · 독립 인수 37건 무수정.

## 6. Do·See (2026-09-22 오후, 기준 `169b130` → merge `038c3c3`)

- **Do 1단계(요청 opus/high, `daf8b3e`):** `evidence.py` +113/−16(2단계 파싱·`chain`·`exchange_mismatch`·`evidence_pages`·`parser_scope`) · `lifecycle.py`(`OrderEvidence.chain` 기본값 필드, provenance superset `in (ref.exchange, "ALL")`) · `application.py`(`observation_from_evidence`) · `queries.py`(`exchange_scope` kwarg, `{"KRX","ALL"}` 만 허용) · `commands.py`(`prepare(..., fill_metadata=None)`, `_fill_metadata` 화이트리스트·prepare 시점 거부·float 정규화, binding 에 `session`·`fill_metadata`) · `gateway.py`(BUY 에 `entry_signal_score`, 결측·비유한이면 `entry_signal_score_required`). 신규 `tests/test_execution_fill_producer_parts.py`. 설계와 다르게 한 것(coordinator 승인): unreadable 행이 있으면서 후보 행이 없으면 `not_found` 가 아니라 `chain_undecidable`(읽히지 않은 행이 우리 행일 수 있다 — fail-closed) · 별칭 충돌 검사는 신원 4필드에 한정(전 필드로 넓히면 기존 표본의 기대값이 바뀐다) · gateway 는 score 만 싣는다(Order/SignalEvent 에 name·sector 필드가 없고 sector 는 binding 이 이미 정본) · `initial_r.py` 는 이미 같은 provenance 함수를 타서 0줄. **P-8 예외 적용**(§5 표 참고).
- **See 1단계 — 독립 재현(요청 opus/xhigh): APPROVE(P0/P1 0, P2 5)** — 변이 6종 재실측 전부 kill, 추가 3종 생존(float 정규화 무하중·0.0 점수 폴백 무하중·supported 의 KRX 이중 가드 무하중) → 보강 `6905700`(시험만 +24) 으로 kill 확인.
- **Do 2단계(요청 opus/high, `1f94721`, 1단계 위):** `runtime.py` +345(`start_reconciler`·`reconcile_once`·`_reconcile_loop`·`notify_execution_change`·`reconciler_blocked_reason`·`health()['reconciler']`) · `commands.py` +2(`_record` 뒤 `notify_execution_change()` — ACK 만 고르지 않는다). 상수: `RECONCILER_MAX_PAGES=3`·`REQUEST_TIMEOUT=15.0`·`CYCLE_TIMEOUT=60.0`(15×3=45 + 적용 여유 15)·`INTERVAL=3.0`·`STALL_CYCLES=5`. apply 는 (A) 가 아니라 전부 (B) 에서 — 같은 주기의 A 가 observed 를 올리면 B2 가 곧바로 접수하므로 접수 경로가 하나다. health 는 `health()['reconciler']` 밑. 신규 `tests/test_execution_reconciler.py` 16건(가짜 QueryCollection 을 손으로 만들지 않고 실제 `LegacyExecutionQueries` 에 응답만 주입, 실제 prepare→ACK 로 binding, 실제 engine 큐를 pump).
- **See 2단계 — 독립 재현(요청 opus/xhigh): CHANGES_REQUIRED — P1 1·P2 5.** **P1:** B2 재구성이 `evidence_conflict`·적용 불가 상태의 attempt 를 제외하지 않아 그 관측이 inbox 에 RECEIVED 로 남고 `reduce_economics` 가 영구 ValueError → `unapplied_execution_observation` 으로 **전 종목 명령 정지**. P2: `blocked_reason` 이 A 대상만 봐 미적용 inbox 행만 쌓인 정지를 못 봄 · chain 이 B1 을 막지 않는데 시험 이름은 무조건 · drain 이 engine 사설 속성에 의존(단독 shutdown 교착 가능) · `last_reason` 이 적용 실패 주기에서 옛 사유를 물려받음 · not_found 죽은 이중 가드·PARKED 미고정.
- **처분(요청 opus/high, `636deb0`):** A·B 공통 `_appliable`(`APPLIABLE_ATTEMPT_STATES` = open/partial/final_filled/final_cancelled/final_expired — economics 의 리터럴과 대조하는 drift 가드 시험) · `_stalled_targets`(A 대상 + 미적용 inbox 행 + observed>applied) · **B1 은 chain 과 무관하게 진행(결정 — 그 행은 chain 이전에 검증된 durable 관측이고 막으면 차단 사유 19 가 영구화)** · drain 은 runtime 소유 task 만(수집 task 가 자연 종료 시 진행 중 apply 를 스스로 기다린다; 제품 순서 engine._shutdown → runtime.shutdown 은 P0-3) · 주기 끝 사유 확정(`progressed`/`idle`/`apply_failed`) · 죽은 가드 삭제 · PARKED 인수. 변이 4종 kill. 잔여 위험(docstring): `_appliable` 이 A 에서 `reconciling`·`blocked_unknown` 을 거르는데 현재 코드에서는 동작 보존(전자는 생성 경로 없음, 후자는 `order_ref` None) — 다른 reconcile 호출자가 생기면 재검토.
- **통합:** merge `--no-ff` ×2 → `038c3c3`. src +663/−29, 시험 +992. **제품 호출자 0건·live 파일 0줄**(`start_reconciler`·`KRExecutionRuntime(` 의 src/scripts 호출자 grep 0).
- **전체 suite 단독 직렬(`038c3c3`, `tests` 인자, 에이전트 0):** **UTC 5041 passed / 기존 xfail 2 / 385.09초**, **KST 5041 passed / 372.92초**, 격리 위반 0. load 0.29 → 1.08 → 1.23. (4993 + 48.)
- **Codex 교차 리뷰 11차(요청 gpt-6-astra/xhigh, 포그라운드, 정적 — 자기 must-fix 10 의 재검증 포함): CHANGES_REQUIRED — P0 0 · P1 3 · P2 1.** must-fix 4·5·6·9·10 은 닫힘 확인(6·9·10 은 P0-2 의 경계대로), 7 은 P0-3 이관이라 결함으로 세지 않음, **2·8 미완결**:
  - P1 ① **깨움 유실** — ACK 저장(`asyncio.shield(task)`) 뒤에만 notify 가 있어 호출자가 취소되면(저장은 성공) attempt 는 OPEN 인데 수집기는 무기한 잔다 → notify 를 저장 task 의 완료 콜백으로.
  - P1 ② **B 굶음·수집 예산** — A(조회) → B 순서라 조회가 60초를 소진하면 B 에 도달 못 함. `RECONCILER_MAX_PAGES=3` 은 파서에만 넘기고 실제 수집기는 `max_pages=10` 그대로 → 순서를 B1 → A → B2 로, 조회 별도 예산, `collect(max_pages=…)`.
  - P1 ③ **타임아웃이 진행 중 커밋을 끊는다** — `wait_for` 만료가 owner 변경(SQL commit·publish)을 취소해 `application._block()` → 전역 차단. 단순 시간 초과가 지속 차단으로 → 시작한 owner 변경은 runtime 소유 task 로 shield·drain, 타임아웃은 새 작업 시작만 멈춘다.
  - P2 퇴행 응답(20주 < 저장 40주)에 reconcile 이 호출돼 version 만 전진 → 호출 전 skip.
  - 확인받은 것: Event clear 순서의 깨움 유실 없음 · 파서 2단계(신원 정규화·별칭 충돌·4튜플·ALL chain·`chain_undecidable`) · metadata 타입 검사(bool 제외·NaN/inf 거부·0.0 보존·deepcopy) · 새 클래스/모듈·`x or default`·`Decimal(float)` 없음(일반 truthiness `if targets` 류는 존재 — 컬렉션 비어 있음 판정이라 규칙 위반 아님). `_appliable` docstring 의 "불가능"은 현재 호출 경로에 한정해야 한다.
  - **처분(요청 opus/high, `a602c3e`, merge `90737f1`):** notify 를 결과 저장 task 의 완료 콜백(성공 분기)으로 · 순서 B1 → A → B2 · 조회 별도 예산 `RECONCILER_QUERY_TIMEOUT=45`(=15×3) + `collect(max_pages=3)` 전달 · 주기가 시작한 owner 변경은 `_owner_task`(create_task + `_reconciler_tasks` 등록 + shield) · 퇴행 응답 `regressed_observation` skip · `_appliable` docstring 을 현재 경로 한정으로. 변이 5종 kill(m4 는 단언 완화 뒤 결과까지 확인 — shield 없이는 `shutdown()` 이 `command_state_drain_failed` 로 끝난다). 전체 suite **UTC 5046 / KST 5046 passed**·격리 0.
- **Codex 12차(요청 gpt-6-astra/xhigh, 11차 처분 delta): CHANGES_REQUIRED — P1 2 · P2 1.** 확인받은 것: notify 의 실패 분기와 상호배타 · shield 바깥 취소가 안쪽 task 를 끊지 않고 미완료 task 는 drain 대상 · B1/chain 정책 유지 · 퇴행 skip · 금지 패턴 없음. 남은 것: **P1 ① 첫 주기 B1 이 타임아웃되면 `_reconciler_target_count` 가 0 으로 남아 루프가 Event 에서 무기한 잔다**(owner task 완료도 깨우지 않음) → 잘지/돌지 판정은 호출 시점 `_stalled_targets`, owner task 완료 콜백이 notify · **P1 ② B2 의 15초가 보장되지 않는다**(조회 45초가 B1 소요를 차감하지 않고 A 의 reconcile 들도 남은 시간을 소진) → 마감시각 기반 예산(reserve 15초, 조회 timeout = min(45, 마감−reserve−now), A 는 reserve 앞에서 새 reconcile 시작 금지) · P2 취소 뒤 완료된 owner task 의 결과·예외가 progress/outcomes 에 안 남는다 → 기록을 task 본문으로.
  - **처분(요청 opus/high, `0844cca`, merge `3c7dcf3`):** 잘지/돌지를 호출 시점 `_stalled_targets` 로 · owner task 완료 콜백이 **주기 밖**일 때 notify(주기 안 완료는 깨우지 않는다 — 매번 깨우면 고정 interval 이 사라져 원장 TR 이 쉬지 않고 나간다) · `_steady()`(단조 시계 손잡이) · `reapply_deadline = 시작 + cycle_timeout − reserve`, reserve 는 기본 15초를 주기 예산에 **비례**(고정 15초면 5초짜리 시험 주기가 조회를 시작하지 못한다) · 조회 timeout `min(45, 마감−now)`·`query_budget_exhausted`·`reconcile_budget_exhausted` · 판정·progress·outcome·parked·apply_failed 기록을 owner task 본문으로. 변이 5종 kill(m1 은 1차 생존 → 시험 재고정). 전체 suite **UTC 5053 / KST 5053 passed**·격리 0.
- **Codex 13차(12차 처분 delta): CHANGES_REQUIRED — P1 1 · P2 1.** P1-a(깨움)는 유효 확인(콜백이 True 일 때 생략해도 finally → 판정 사이에 await 가 없어 놓치지 않는다). 남은 것: **P1 마감 검사가 "새 작업 시작"만 막고 이미 시작한 A·B1 의 대기는 마감에 걸리지 않는다** — A 가 44초에 시작한 reconcile 이 60초까지 늦으면 주기 전체가 취소돼 B2 에 못 간다 → 각 대기를 `wait_for(shield, 마감−now)` 로(대기자만 끊고 task 는 drain) · P2 대기자 취소 뒤 reconcile 예외가 `attempt_failed` 로 기록되지 않는다 → task 본문에서 기록·재전파, 대기자 중복 집계 금지.
  - **처분(요청 opus/high, `65d1cef`, merge `7500d3c`):** `_await_owner(pending, deadline, reason)` — `wait_for(shield, max(0, 마감−now))`, 만료는 대기자만 끊고 사유(`reconcile_wait_deadline`/`reapply_wait_deadline`)를 남긴 뒤 `None` → 호출자 break → B2. task 자신의 TimeoutError 는 `pending.cancelled()` 로 구분해 재전파 · `_reconcile_attempt` 를 **동기 함수**로(접수한 task 또는 건너뛰면 `None` — await 0 이라 대기자의 except 는 구조적으로 접수 전 실패만 본다) · B1 도 같은 방식, B2 는 마지막 단계라 마감 없음 · `store()` 본문이 `attempt_failed` 기록·재전파. 구현자가 변이 m4 로 잠복 결함(건너뛰기 5경로의 `False` 가 `wait_for` 에 들어가 TypeError → 대기자가 삼킴)을 찾아 고쳤다. 변이 4종 kill, reconciler 파일 3회 반복 플레이크 0. 전체 suite **UTC 5057 / KST 5057 passed**·격리 0.
- **Codex 14차(13차 처분 delta): APPROVE — 필수 수정 0.** 확인받은 것: shield future 는 `wait_for` 만료로 `cancelled()`=True, 안쪽 task 의 TimeoutError 는 False 로 전달돼 두 경로가 구분된다 · `CancelledError` 재전파 경로 · 느린 B2 는 `cycle_timeout` 으로 대기가 끝나고 task 는 결과·진전을 본문에서 기록하며 주기 밖 완료가 Event 를 세운다 · `_reconcile_attempt` 의 바깥 await 0(AST 확인) · 건너뛰기 5경로 전부 `None` · 금지 패턴·새 클래스/모듈 없음. 정밀화 1건(P2, 수정 불요): 대기자의 예외 분기가 `done()` 만 보고 예외 보유 여부를 직접 검사하지 않으나 확인한 경로에서는 마감 `None`·취소 재전파가 앞서 중복 집계가 없다.

## 7. P0-2 마감 (2026-09-22 14:4x KST, engine `7500d3c` + 문서)

**성과와 한계(보고 문장 — 이대로 인용한다):** attach 모드에 **체결·종결 증거의 생산자**가 생겼다 — runtime 이 소유하는 주기 task 가 미해결 attempt 가 있을 때만 일별체결조회(`TTTC0081R`·`ALL`)를 읽어 파서(2단계·chain 노출·거래소 대조) → `lifecycle.reconcile` → 저장 상태 기반 재처리 → `engine.apply_execution_observation` 로 넘긴다. 매수 체결이 포지션이 되고 예약이 풀리는 경로가 **코드로 존재**한다. **설치가 아니다** — 제품 호출자 0건(`start_reconciler` 배선은 P0-3), live 파일 0줄, `trading_ready` 상수 False. 설치 차단 사유 **18 은 "부품 완성"**(배선·활성화 조건은 P0-3), **3 은 그 부품이 닫을 수 있게 됐다**. 설계는 Codex BLOCK(must-fix 10) → 개정 → REVISE ×2 → 확정, 구현은 독립 재현 2회(APPROVE·CHANGES_REQUIRED→처분) + Codex 4라운드(11차 P1 3 → 12차 P1 2 → 13차 P1 1 → **14차 APPROVE**)를 거쳤다. 각 라운드가 실제 결함을 찾았다: 깨움 유실 · B 굶음 · 타임아웃이 진행 중 커밋을 끊음 · 퇴행 응답 churn · B1 타임아웃 뒤 무기한 잠듦 · B2 유보 미보장 · 대기 마감 부재 · 예외 미기록.

**남는 것(P0-3 입력):** ① `start_reconciler` 배선(`factory.install_attached_runtime`)·`execution_query_environment_required`·`reconciler_not_started` 거부·기동 순서(engine.run 이 돌아야 주기가 완주) ② 게이트 변경 F8(side 인지형 `unresolved_symbol_attempt`·BUY 한정 `reconciler_unavailable`) ③ `_sync_portfolio` 의 attach 분기(N2) ④ SELL 의 `exit_type` 생산(`_classify_exit_type` 재사용)·부분 청산의 `exited_today` ⑤ 종목명 생산자 ⑥ 실계좌 스모크(`TTTC0081R` 응답·15필드 철자·`ALL` 수용 — D10 기록기, 사용자 확인 대기) ⑦ 차단 사유 19(체결마다 전 종목 명령 정지 창 — P-1 로 유한, 범위 축소는 P2) ⑧ 비KRX 체결은 `exchange_mismatch` 로 영구 미해결(P1).
