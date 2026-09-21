# Claude 인계 — 브랜치 정리와 엔진 마이그레이션 (2026-09-20)

> **인계 이후 진행 위치(2026-09-21 갱신): B2/B3 는 S1~S5 로 완료됐다(한정 승인·운영 미설치, engine 전체 UTC/KST 각 4872 passed).** 다음 세션은 바로 아래 "B2/B3 이후" 절의 **차단 사유 15항**과 문서 끝의 **"다음 세션에게 전달할 시작 문장(2026-09-21)"** 에서 시작한다. 아래 줄은 착수 당시 기록이다.
> **인계 이후 진행 위치(2026-09-20 갱신):** 아래 §1 "B2/B3" 는 착수됐다. 단계별 Plan/Do/See·재현 명령·리뷰 판정·이어받는 체크리스트·잔여는 **`docs/reviews/b2b3-stage-ledger-2026-09-20.md`(단계 원장)** 가 정본이고, 계약·단계 정의는 `docs/superpowers/plans/2026-09-20-b2b3-request-bound-qualification.md` 다. 이 문서의 경계(main 병합·배포·재시작·주문·설정·Toss grant 미실행, `trading_ready=False`, MODIFY 미지원)는 그대로 유효하다. 이어받는 세션은 원장의 "상태 요약"과 "공통 작업 방법"부터 읽는다.

## 먼저 읽을 결론

운영 기준은 main, 계속 개발할 정본은 **`feature/engine-safety-design-20260917`** 한 개다. C4 제품 기준은 **`ab044c4edeb702911fee998973cb00a263a7b085`**다. 추가 확인 없이 정리하라는 후속 지시에 따라 다른 로컬54/원격7 branch heads와53개 worktree는 **복구 가능한 archive로 전환**했다. 최종 상시 branch/worktree는 main·engine 두 개이며 임시 문서 PR 작업공간은 병합 후 제거한다. 다른 worker를 다시 활성화하거나 중복 구현하지 않는다.

**전체 엔진의 main 병합·운영 전환은 아직 승인 가능한 상태가 아니다.** C4 한정 통과를 전체 마이그레이션 완료로 확대하지 않는다. 이번 정리는 개발선을 폐기하거나 거래 안전 장벽을 해제하지 않았다. 사용자가 요청한 다음 개발·설계는 이 문서를 읽은 새 Claude 세션에서 이어간다.

## B2/B3 이후 — attach 설치 차단 사유와 이월 목록 (2026-09-21 갱신)

아래 §1(B2/B3)은 engine 브랜치에서 S1~S5 로 진행됐다. 단계별 Plan/Do/See·SHA·전체 suite 숫자·리뷰 판정은 [단계 원장](../reviews/b2b3-stage-ledger-2026-09-20.md)의 "상태 요약"이 정본이다. **만들어진 것은 "runtime 이 붙은 엔진에서 SIGNAL 이 기존 후보 판단을 거친 뒤 owner 의 prepare → final 재검사 → dispatch 한 길로만 나가는 경로"이고 설치가 아니다.** 제품에 `KRExecutionRuntime(`·`.attach(`·`install_gateway(`·`recover_unsent(` 호출자는 0건(단계마다 grep 으로 재확인), `trading_ready` 는 항상 False, 모든 MODIFY 는 미지원이다. 모든 GREEN 은 fake HTTP·주입 시계·시험용 합성 startup 허가 위의 결과이고, 현금 고갈로 정상 BUY 의 실경로 표본은 0건이다.

### attach 설치 전에 닫아야 할 것 (하나라도 열려 있으면 설치하지 않는다)

| # | 차단 사유 | 왜 막는가 | 여는 작업 |
|---|---|---|---|
| 1 | **취소·체결 최종성의 증거 계약이 제품에 없다** | 증거 파서가 취소 체인을 `supported_finality=False` 로 두고, `lifecycle.reconcile` 의 제품 호출자가 0건이고, `EXECUTION_FILL` 을 만드는 코드는 engine 에 있으나 **그것을 구동하는 실제 관측 공급 경로가 0건**이다(Codex 5차-B 의 표현 한정). 그래서 attach 모드에는 **미체결 SELL 의 시장가 에스컬레이션과 미체결 BUY 의 타임아웃 취소가 없다** — 보호 SELL 에 관해 attach 는 legacy 보다 덜 안전하다 | 아래 §3 공식 증거 + 10A2/10C(체인 최종성, reconcile·fill 생산자). 그 뒤에야 "취소 확정 뒤 재주문"을 설계한다 |
| 2 | `trading_ready` 를 여는 실제 startup 대사가 없다 | 합성 허가는 시험 안에만 있다. 강제 True 금지 | §3 최초 잔고/체결 cutoff 증거 + 10A3 |
| 3 | fill projection 이 없다 | attach 에서는 ORDER 이벤트를 큐에 싣지 않고 legacy 미체결 장부를 쓰지 않는다(S3 결정 ③④). 체결 메타(entry_tags·전략) 인계와 pending 교착 감시가 비어 있다. 제품의 ORDER 구독자는 `RiskManager.on_order` 한 곳뿐이라 끊기는 별도 소비자는 없으나, **attach 의 조기 반환이 함께 건너뛰는 관측이 있다**: SIGNAL 의 대시보드 표시(`engine.py` `_process_event` 의 SIGNAL 추적 블록)·`on_order` 안의 SELL 수량 불일치 누적과 텔레그램 경보(좀비 포지션 감지)·`health_monitor` 의 pending 교착 검사(빈 legacy 장부를 읽고 "정상"을 돌려준다)(Codex 5차-A P2) | 10A3/10C — owner 의 결과·상태에서 관측을 공급한다. ORDER 를 다시 발행해 legacy 송신자를 부르지 않는다 |
| 4 | factory·설치 순서 — **부품은 있고 호출자·앞 단계가 없다** | `config_version` 5축 조립·제품 PolicyContext publisher·regime owner → command owner 순서·`runtime.restore()` 뒤 엔진 루프 시작 전 `await runtime.gateway.recover_unsent()`(안 부르면 prepare~claim 사이 잔류가 예약과 일자 전환을 막는다). **10A3a·10A3b-1 이 부품을 만들었다(`src/execution/safety/factory.py`):** 로드된 설정에서 정책을 만들어 게시하는 세 함수와 **거부형 설치기 `install_attached_runtime`** — 설치 순서와 명명된 거부 16종을 고정하고(거부는 전부 live 를 건드리기 전, attach 는 마지막 인접 두 줄), `recover_unsent()` 와 kind 무관 잔존 검사까지 한다. **제품 호출자는 0건이고, 최초 checkpoint·레짐 baseline·일자 전환을 만드는 앞 단계가 없어 운영에서는 항상 거부로 끝난다** — 이 항목은 열려 있다. 설치기가 남긴 계약: 구간 1 의 거부면 legacy 로 계속 가도 되지만 `execution_runtime_already_restored` 와 구간 2 의 실패 뒤에는 **legacy 로 돌아가면 안 된다**(live 가 저장본 값이다) · store 는 호출자가 모든 경로에서 닫는다 · 10C 의 레짐 배선은 "VIX 갱신 뒤 추세를 다시 평가한다"를 계약으로 넣어야 한다(성공한 추세 갱신이 뒤에 거는 VIX 갱신이 그 추세 판단을 stale 로 만들어 다음 갱신까지 모든 prepare 가 막힌다) | 10A3b 의 나머지(호출자·시계 주입·관측·store 읽기 전용 개방) + 공식 증거 뒤의 "앞 단계" |
| 5 | `_exit_exempt_ref` 주입 재현 + **면제 추가 경로를 owner 로** | 제품 주입은 `scripts/run_trader.py:854` 한 곳뿐이다. 빠지면 eviction 의 exit_exempt 보호(펩트론 087010)가 사라진다. 또 attach 에서 면제의 정본은 owner state 다 — `publish_protection` 이 live set 의 **내용을 owner DTO 로 교체**하므로 런타임 `exit_manager.add_exit_exempt(...)`(`kr_scheduler.py:7537·7614`)는 다음 게시에서 지워진다(S5 Plan 확인). **이중 실패다(10A3a 가 실큐로 고정 — `tests/test_execution_owner_gate_authority.py`):** ① 런타임 추가 직후의 다음 명령은 `_owner_ready` 의 보호 등식이 깨져 `legacy_protection_writer_conflict` 로 통째로 끝나고(POST 0) ② 그 뒤 **실제로 성공한 owner 게시**가 한 번 일어나면 면제가 조용히 사라진다 — 조작자는 "면제를 걸었다"고 믿는다. (거부된 명령이 면제를 지우는 것은 아니고, 정책 재게시도 같은 등식에 막히므로 충돌을 스스로 풀지 못한다 — 충돌 상태는 그 등식을 거치지 않는 owner commit 이 올 때까지 이어진다. 시험은 직접 `owner.mutate` 로 소실을 보였고, 제품의 어느 게시 경로가 등식을 거치지 않는지는 **미확인**이다 — Codex 6차의 한정.) 조기 반환 한 줄로는 고칠 수 없다(면제 미등록의 조용한 실패가 된다) | 10A3b(주입) + 10C(owner command 로 추가 경로 이행 — 한 덩어리로) |
| 6 | 진입 가격의 provenance·freshness 가 증명되지 않는다 | gateway 가 게시하는 entry quote 의 출처가 SIGNAL 자체(`source='signal'`)이고, 게시 시각은 원 시세 시각이 아니라 **처리 시각 `now` 로 다시 찍힌다**(`gateway.py:130`) — 오래된 가격도 새 시각으로 게시된다. owner 의 `current_entry_quote_required` 가 보장하는 것은 게시 행 존재·요청 평가 가격과의 일치·KST 당일성·미래 시각 아님뿐이고, 원 시세의 신뢰성·최대 경과시간·현재 호가·시장가 체결가 상한은 보장하지 않는다. 같은 종목에 `market_sources` 행이 이미 있으면 gateway 는 덮어쓰지 않는다(Codex 5차-A P1). **소스 주석이 이 일을 A3 factory 에 배정해 뒀다** — `market_source.py` 의 `source_is_current` 는 행이 없으면 True 를 돌려주며 "A3 factory must require actual source for every BUY" 라고 적혀 있다(10A3 Plan 이 발견 — 닫지 않고 넘기면 그 주석도 고친다) | 10A2/10A3b — 독립 원관측·원 시각·유효기간의 결합, factory 의 "실제 source 요구" |
| 7 | owner 밖의 직접 writer/sender 가 남아 있다 | KOFR(`kr_scheduler.py:6754·6818`)·수동 매수(7597)·CLI 2개·`KRScheduler._cleanup_stale_pending`(별도 장부, 취소 0건을 최종성으로 읽는 예약 해제)·`run_trader.py` 의 sector lookup 예외 뭉갬 | 10C |
| 8 | attach 에서 예외로 끝나는 소비자 | `src/dashboard/data_collector.py:1572` 가 `_reserved_cash` property 를 직접 읽는다 | 10A3(attach 인지형으로) |
| 9 | core_reserve 설정 출처가 둘이다 | engine `_get_core_reserve()` 와 owner `core_reserve(snapshot)` | 10A3 |
| 10 | **attach 에서 가격 없는 SELL SIGNAL 은 거부된다** | 호가도 `event.price` 도 없으면 legacy 는 MARKET SELL 을 보내지만 attach 는 gateway 의 평가 가격 부재(`invalid_request_price`)로 끝난다 — POST 0. fail-closed 이나 보호 SELL 의 한 갈래가 닫혀 있다(S5 Plan 확인). **market source 를 붙이는 것만으로는 닫히지 않는다** — SELL 의 `_bind` 는 owner 에 저장된 시세를 조회하지 않는다. 반대 방향의 위험도 있다: 호가 조회가 실패하면 **오래된 양의 신호 가격**이 LIMIT SELL 의 지정가가 되고(`engine.py` `_get_sell_price`), SELL 은 BUY 전용 quote 검사를 받지 않는다(미체결 위험 — 정적 추론, Codex 5차-A P1). **호가 조회가 정상 성공한 경우에도** 판단→final 사이에 내려간 호가는 다시 보지 않으며(S4 결정 ③ — 거부형 재검사는 손절 방향을 막으므로 도입하지 않은 절충), 미체결이어도 에스컬레이션이 없다(#1) (Codex 5차-B P2) | 10A2/10A3 — SELL 평가 가격의 조회 경로와 오래된 fallback 가격 정책. 평가 가격과 실제 주문 지정가를 구분해 설계한다 |
| 11 | sidecar 와 owner 의 정책 출처가 다르다 | 같은 축(최대 포지션·최소 현금·포지션 크기·섹터)을 `risk/manager.py` 의 게이트와 owner 의 `evaluate_entry_policy` 가 각각 평가하는데, owner 의 `EffectiveRiskPolicy` 를 RiskConfig 에서 만드는 제품 코드가 없다(시험은 리터럴) — 두 결론의 정합은 출처를 묶은 뒤에야 인수할 수 있다. S5 wave 1 이 확인한 구체 사례: **최소 현금 축의 게시값은 죽은 값이다**(engine `get_available_cash()` 와 owner `build_owned_snapshot` 이 둘 다 레짐 표 `REGIME_PARAMS` 를 읽어 게시값을 덮어쓴다 — 현재는 같은 표라 일치). 또 `RiskConfig` 의 dataclass 기본값(최소 현금 15%·최소 포지션 금액 50만)은 운영 YAML(5%·20만)과 다르다 — factory 는 기본값이 아니라 **로드된 설정**에서 정책을 만들어야 한다. **10A3a 가 그 부품을 만들었다**(`src/execution/safety/factory.py` — 인스턴스에서 정책을 유도하는 builder·`config_version` 5축 어댑터·`PolicyContext` publisher, **제품 호출자 0건이라 이 항목은 아직 열려 있다**). 함께 확인된 것: 로더 `_build_risk_config` 는 YAML 의 `kr.risk.hybrid`(실제로 키가 있다)·`max_core_positions`·`daily_exit_cooldown_threshold` 를 **읽지 않는다**(dataclass 기본값으로 돈다 — 켜도 조용히 무시된다) · owner 의 최소 현금 축은 독립 게이트가 아니다(같은 게시값이 최소 현금 검사와 가용 현금 계산 두 곳에 들어가 한쪽을 꺼도 다른 쪽이 다른 사유로 막는다) | 10A3b factory 의 인수 조건 |
| 12 | **주문번호 없는 ACK(UNKNOWN) 한 건이 attach 의 모든 새 명령을 멈춘다 — 보호 SELL·CANCEL 포함** | owner 는 `blocked_unknown` 행이 하나라도 있으면 종목과 무관하게 새 후보를 `unresolved_execution_evidence` 로 거부한다(S5 wave 1 의 B1 이 다른 종목 BUY 로 실큐 확인). **그 검사는 새 요청의 side·kind 를 가리지 않는다**(`commands.py` `_evaluate` — 10A3 Plan 이 코드로 확인, 10A3a 의 `test_c1_…` 이 실큐로 고정): 손절 SELL 도 나가지 못한다. 범위 한정(Codex 6차): 전역 정지를 일으키는 기존 행은 **SUBMIT 의 UNKNOWN** 이다 — UNKNOWN 인 자식 명령(cancel)은 전역 정지가 아니라 같은 종목의 새 SUBMIT 만 막는다(attach 에는 취소 배선이 없어 지금은 발생하지 않는다). fail-closed 로는 옳지만 **해제 경로 자체가 설계돼 있지 않다**: 해제 수단은 `lifecycle.reconcile` 뿐이고 그 제품 호출자가 0건인 데다, UNKNOWN 이 된 attempt 는 `order_ref` 가 None 이라 reconcile 의 첫 검사(`attempt["order_ref"] != evidence.ref`)에서 **항상 탈락한다** — 증거가 와도 지금 코드로는 풀 수 없다(2026-09-21 증거 조사에서 코드로 확인) — 설치하면 네트워크 이상 한 번이 수동 state 수술 전까지의 전면 정지가 된다 | #1 의 증거 계약 + reconcile 의 제품 배선(10A2/10C), 운영자 절차(runbook) |
| 13 | CV 의 판단 시각이 주입 시계가 아니다 | `cross_validator.py` 가 함수 안에서 `datetime` 을 다시 import 해 프로세스 벽시계로 `now_hm` 을 읽고, `qualification._check_clock` 이 그것을 owner 의 주입 시계와 대조한다. 운영에서는 둘 다 실제 시각(KST 호스트)이라 일치하므로 **운영 영향은 낮다** — 재생·시험·시계 주입 설계의 전제다(S5 wave 1 — 어긋나게 두면 BUY 표본 12/18 실패). 벽시계는 `now_hm` 하나가 아니다: `_panel_loaded_at` 도 결정으로 반출돼 owner 의 만료 계산에 들어가고 naive 를 KST 로 해석한다(UTC 호스트면 9시간 어긋난다 — 10A3 Plan) | 10A3b — CV 에 clock 주입(`now_hm` 과 패널 시각을 **같이**) |
| 14 | **만석 교체(eviction)의 "쿨다운 안 전역 1건" 은 프로세스 재시작을 넘지 못한다** | 그 기록(`_REPLACEMENT_LAST_EVICT_TS`)은 engine 내부 RiskManager 의 in-memory dict 뿐이고 owner state 에 대응 필드가 없다. attach 에는 fill projection 이 없어(#3) 축출 SELL 뒤에도 만석 상태가 유지되므로, 재기동 직후 같은 쿨다운 창 안에서 **두 번째 축출 SELL** 이 나갈 수 있다(미해결로 남은 첫 희생자는 owner 가 후보에서 빼지만 다음 약한 후보가 나간다). legacy 의 종목별 쿨다운도 in-memory 지만 legacy 는 체결이 포지션을 실제로 비운다 — S5 wave 2 재현자의 정적 분석, 시험 미구성 | 10A3 — 교체 기록을 owner state 로(축출 사유가 붙은 attempt/효과 원장) |
| 15 | 실효 있는 stale 축은 regime 1개다 | `panel_outlook:*`·`trade_memory:*` 는 결정 시점에만 게시돼 판단→final 대조가 replay 구속 전용이다. config 축은 제품 publisher 가 생겨야 실효가 생긴다(#4) | 10A3 + 지속 publisher 설계 |

**차단 사유 1·2·12 를 여는 길 — 사용자가 할 수 있는 일(2026-09-21):** [KIS 증거 좁히기](../integrations/kis-execution-evidence-narrowing-2026-09-21.md)에 **KIS 에 그대로 보낼 질문지 31문항**과 비식별 응답 수집 체크리스트가 있다. 공개 공식 자료를 다시 훑은 결과 닫힌 계약 행은 0개였고(KIS 자신이 "최종 여부를 직접 알려 주는 정보는 없다"·"체결통보 수신 ≠ 당사 원장 반영 완료"라고 명시한다), 남은 것은 KIS 의 답변이나 승인된 비식별 실응답뿐이다. 같은 조사가 코드에서 찾은 것: 증거 파서(`TTTC0081R`)와 수집기(`TTTC8001R`)의 TR 불일치 · 파서의 필수 키 `cnc_cfrm_qty` 대 포털 표기 `cncl_cfrm_qty` · 빈 `cncl_yn` 거부 — 셋 다 fail-closed 이고 **답이 오면 가장 먼저 정할 코드 지점**이다. KR 체결통보(`H0STCNI0`)의 소비자는 제품에 없다(상수 한 줄뿐).

**2026-09-21 저녁 갱신 — 기준 자료를 공식 저장소로 고정(사용자 지시):** `koreainvestment/open-trading-api@b4e6249` 를 전수 조사해 31문항 각각의 답변 상태를 [저장소 기준 상태표](../integrations/kis-repo-grounding-2026-09-21.md)로 만들었다. **저장소가 고정하는 것은 요청·응답의 모양뿐이고 취소 최종성·누적 범위·cutoff 는 저장소도 말하지 않는다 — 차단 사유 1·2·12 는 그대로다.** 위 세 코드 지점 중 둘은 저장소 모양에 맞췄다: TR 불일치는 수집기를 `TTTC0081R`/`TTTC0084R` 로 옮겨 닫았고, 철자는 **둘 다 받되 값이 다르면 malformed** 로 했다(철자 확정이 아니다). 빈 `cncl_yn` 거부는 보수적 선택으로 **유지**한다(저장소가 값 집합을 말하지 않는다). 새로 남긴 경고: **chain 판정은 조회한 거래소 범위에 의존한다** — KRX 만 조회하면 NXT/SOR 의 자식행이 보이지 않아 최종성 인정이 더 쉬워지는 fail-open 방향이고, engine 주문이 구 TR 이라 KRX 로만 나간다는 추론이 현재의 방어다(Q27·Q29). 상세는 [이행 명세](../integrations/kis-tr-migration-spec-2026-09-21.md) §8-2.

설치 차단과 별개로 **제품이 쓰는 KIS 주문·취소·조회 TR 5종이 전부 "구TR"이다**(가이드: "사전고지 없이 막힐 수 있다" / 2025-02 공지: "자동 매핑되어 즉각적인 영향은 없으나 장기적으로 삭제 가능성") — main 기준의 별도 과제다. **작업 칩 `task_90d688f8` 은 닫았고 이 세션이 직접 브랜치 `fix/kis-tr-id-migration-switch`(base `origin/main`)로 진행했다: `KIS_TR_SET` 기본 `legacy` 에서 요청이 바이트 단위로 같은 무동작 PR.** 신 TR 은 정규장 주문 접수에만 적용하고, 전환(`KIS_TR_SET=new`) 전에 닫아야 할 미해결(취소·정정이 주문의 접수 세션을 모른 채 `"KRX"` 를 붙인다 · 실계좌로만 확인되는 11항)은 [이행 명세](../integrations/kis-tr-migration-spec-2026-09-21.md) §8-1 과 main 쪽 runbook 에 있다. **PR 병합·배포·재시작·전환은 사용자 확인 뒤, 장 마감 후에만.** 같은 호출 지점의 동작 변경 결함(취소 POST 의 `retry=False` 누락 · 미체결 조회 50건 잘림 · 요청 `tr_cont` 미송신)은 무동작 PR 에 섞지 않고 다음 PR 로 남겼다. 또 **운영 경로(legacy)의 현행 결함 5건**이 S4 특성화로 드러나 있다(동시호가에 취소만 보내고 재주문 없음·90초 폴백 루프에 exit_exempt 확인 없음·`submit_order` 예외 시 접수 여부를 모른 채 `clear_pending`·폴백 상한 뒤 원 지정가 방치·취소 0건 예약 해제). engine 브랜치는 이것을 고치지 않고 시험으로 고정만 했다 — main 기준의 별도 운영 수정 과제다.

### 기타 이월 (차단은 아니나 잊지 말 것)

- 3건 이상 동시 미해결 BUY 의 누적 정합 · dispatch 의 network await 동안 엔진 루프가 멈추는 지연 상한 · UNKNOWN 자식만 남은 종목이 `unresolved_symbols()` 에서 빠지는 것(취소를 켜는 작업의 인수 조건).
- **S5 인수가 고정한 범위의 한계(10A3 factory 의 인수 조건으로 넘긴다):** 독립 인수 파일에서 결정 게이트로 하중된 것은 **sidecar(`risk/manager.py`) 층과 owner 의 `daily_trade_limit` 뿐**이다. owner 쪽 최소 현금·최대 포지션 수·당일 손절 재진입·섹터 한도 경계는 각각 무력화해도 인수 파일이 GREEN 이다(항상 sidecar 나 짝이 되는 owner 지점이 먼저 막는 이중 방어 — 단위 시험은 따로 있다). factory 가 정책 출처를 묶을 때 "각 owner 게이트가 단독으로 결정 게이트가 되는 표본"을 인수 조건에 넣는다. owner 의 섹터 한도는 두 판정 지점이 같은 사유 문자열(`sector_limit`)을 써서 어느 쪽이 막았는지 사유만으로는 구분할 수 없다.
- attach 에서 eviction 의 **종목별** 쿨다운은 전역 쿨다운에 포섭돼 사실상 죽은 검사다(legacy 와의 차이 목록). eviction 경로에 금지 falsy 판정 3곳(`… or 0`·`current_price or avg_price`, `engine.py` `_try_evict_weakest_position`)이 남아 있다 — 현재 행동 차이는 없다.
- trend 팩터 버킷(65%)은 `RiskConfig` **기본값**(코어 배분 30%)에서는 현금 게이트가 항상 먼저 막아 도달할 수 없다. 운영 설정(코어 0)에서는 도달 가능하고 현재 `enforce=false` 라 주문 영향은 없다 — 버킷 승격을 판단할 때의 전제다.
- 성능: `ExecutionStateStore.commit` 이 매 commit 마다 state 전체를 직렬화한다. 판단 사실의 분석 원장 projection 은 10C.
- flaky: Toss `RequestBudget(1)` 실시간 1초 예산 시험·개발용 리뷰 실행기의 0.12~0.8초 시한 시험은 호스트 부하에 민감하다(전체 suite 는 단독 직렬·`nice` 금지).

## 작업 장소와 기준

- 운영 checkout: `/home/ubuntu/projects/qwq-ai-trader` (main).
- 엔진 작업공간: `/home/ubuntu/projects/qwq-ai-trader/.claude/worktrees/engine-safety-design-20260917`.
- 1차 감사 시 main `465a029`, engine `ab044c4`(+18/-0). PR #77 이후 문서-only 동기화 기준은 main `4222f7442a63ded50dc2d96a3840a0f8acb041e6`, engine `792a4988c87049615bdda3689852f7b3acc0ac04`다. 이번 archive 인계도 문서만 추가한다. 현재 SHA는 Git으로 재확인하며 뒤처진 main 코드를 재구현하지 않는다.
- 이 인계 문서의 main 병합 이후에는 **문서만 바뀐 main을 engine에 merge**해 인계 문서도 가져온다. source/test 트리와 `ab044c4`의 차이를 대조한다. 강제 push/rebase로 기존 검토 이력을 바꾸지 않는다.
- 복구 정본: [추가 archive 보고서](../reviews/retired-workspace-archive-2026-09-20.md). dirty28·staged12개를 포함한53개 원본과 전체 Git 객체/index는 `/home/ubuntu/projects/qwq-retired-workspaces-20260920.c2dhUU`에 있다. **원래 worker 경로는 더 이상 존재하지 않으며 archive의 `.git` 포인터도 직접 사용하지 않는다.** 보고서의 복구 절차를 따른다. [1차 정리 보고서](../reviews/branch-consolidation-2026-09-20.md)의 보존 목록은 역사 기록이다.
- 이 문서보다 실제 Git 상태를 우선한다. 시작 직전 `git status --short`, `git log -5 --oneline`, `gh pr list`로 다른 세션 변경을 확인한다.

## 운영에 반영한 범위와 유지한 것

2026-09-20 10:36 KST, 기존 checkout `8ff2f55`를 이미 병합·별도 설치된 토스 서비스 기준 main `465a029`로 fast-forward했다. 변화는 별도 observer 모듈/설치 도구/테스트/문서이며, 기존 거래 엔진·스케줄러·브로커·위험·Toss provider·run_trader·설정·의존성 경로의 diff는 0이었다. **거래 프로세스의 새 코드 배포나 새 엔진 활성화를 했다는 뜻이 아니다.** 인계 문서 PR 이후 main이 더 전진하더라도 문서만 추가된다.

- 거래 봇 PID **3534327**, 시작 **09-19 06:14:40 KST**. 이는 이번 작업 이전의 시작이며 원인/당시 실행 SHA를 재구성했다고 주장하지 않는다. checkout SHA와 프로세스 시작 SHA를 혼동하지 않는다.
- Toss 별도 서비스 PID **3335469**, 시작 **09-17 21:47:37 KST**, active 유지. 봉인 릴리스는 기존 `877768e` 기록을 따르며 이번에 재설치/재시작하지 않았다.
- 10:36 재확인: broker connected, broker/risk pending 0, stale loops 0. 설정3파일·킬스위치4경로 지문 불변. 주문·계좌·잔고는 계속 **KIS 단독**이고 Toss는 관측 전용이다.
- 기존 거래 소스에 적용할 변경이 없고 Toss는 같은 grant로 재시작하면 안 되므로 **서비스 재시작0**. 신규 발급·실 API 검증·설정 변경도 하지 않았다.
- 읽기 전용 최근2시간 집계: ERROR/Traceback0, EGW00215/토큰 오류0, EGW00201 12건. 게이트웨이 경고 원인은 미확정이며 이번 정리에서 고쳤다고 보고하지 않는다.
- 승인된 Toss 관측은 **09/18·09/21·09/22**, **09/22 18:00 KST 만료** 그대로다. 미래 날짜를 완료 처리하거나 기간을 자동 연장하지 않는다. 이후 표본/만료/락 반환 인수는 [관측 보고서](../reviews/toss-observer-service-2026-09-17.md)와 [점검표](monitoring-checkpoints.md)를 따른다.

## 완료된 개발과 아직 미완인 개발

아래 engine 문서는 **engine 브랜치에만 존재**한다. main에서 상대 링크가 깨지지 않도록 C4 고정 커밋 링크를 사용했다.

| 영역 | 현재 판정 | 정본 |
| --- | --- | --- |
| legacy 조회·실제 limiter/응답 어댑터 | 오프라인 실제 broker 연결 시험·한정 리뷰 완료 | [후속 진행 원장](https://github.com/qwq-partners/qwq-ai-trader/blob/ab044c4edeb702911fee998973cb00a263a7b085/docs/reviews/engine-execution-followup-2026-09-18.md) |
| 요청·예약·경제·보호·core receipt·복구·outbox ACK·계좌 lease/drain | 구현 조각별 인수 완료, 전체 운영 설치 아님 | 같은 진행 원장 |
| 정책 generation·입력 변경 전파·지속 source 권한 | 실제 결함 RED→수정·재리뷰 완료 | [source 권한](https://github.com/qwq-partners/qwq-ai-trader/blob/ab044c4edeb702911fee998973cb00a263a7b085/docs/reviews/source-authority-followup-2026-09-20.md) |
| 실제 2분 레짐 루프 | owner 분기·버전/결측/저장 경계 한정 완료 | [2분 루프](https://github.com/qwq-partners/qwq-ai-trader/blob/ab044c4edeb702911fee998973cb00a263a7b085/docs/reviews/two-minute-regime-owner-2026-09-20.md) |
| C3 정오·JSON LLM·보호 replay | 실제 스케줄러 인수·한정 승인 완료 | [C3](https://github.com/qwq-partners/qwq-ai-trader/blob/ab044c4edeb702911fee998973cb00a263a7b085/docs/reviews/noon-regime-protection-replay-2026-09-20.md) |
| C4 장전 text diagnosis·소비자 정합성 | actual caller·성공일 dedupe·정책/표시·게시 실패 장벽 완료 | [C4](https://github.com/qwq-partners/qwq-ai-trader/blob/ab044c4edeb702911fee998973cb00a263a7b085/docs/reviews/morning-regime-owner-2026-09-20.md) |
| qualification·최종 sizing·실제 SIGNAL → owner 송신 | **S1~S5 한정 승인·운영 미설치(2026-09-21)** — 독립 인수 37건·Codex 최종 broad 리뷰 포함, engine 전체 UTC/KST 각 4872 passed. 설치가 아니며 위 "차단 사유" 15항이 열려 있다 | engine 브랜치 `docs/reviews/b2b3-stage-ledger-2026-09-20.md` |
| factory·나머지 writer/sender·공식 증거·전체 인수 | 미완, 운영 전환 차단 — **다음 시작점** | 같은 계획 10A3/10C, 위 "차단 사유"와 아래 순서 |

브랜치 전체는 166파일 +44,697/-389줄이며 **전부 비활성 코드가 아니다**. WS 46필드 parser, 공용 limiter, 기존 sizing/regime wrapper와 DB DDL도 바뀐다. 따라서 owner가 아직 자동 설치되지 않는다는 이유로 통째 merge/restart하지 않는다. 실제 `KRExecutionRuntime.trading_ready`는 False, factory 자동 생성/attach는 미배선이며 임의 attach는 legacy SIGNAL/ORDER/FILL을 차단한다.

## 다음 세션의 Plan → Do → See

### 1. B2/B3 — 실제 요청에 qualification·최종 sizing 연결 (진행됨 — 원장이 정본, 아래는 인계 당시의 원 지시)

> 2026-09-21: 이 절의 Plan/Do/See 는 단계 원장의 S1~S5 로 수행됐다. 아래 원문은 무엇을 요구했는지의 기록으로 남긴다. 미충족으로 남은 것은 위 "차단 사유"·"기타 이월"에 있다(예: UNKNOWN 의 실큐 접합 대조 범위, 부분체결·늦은 응답의 fill projection).

**Plan**

1. engine 작업공간의 `CLAUDE.md`, 최근 `CHANGELOG.md`, `docs/README.md`, 위 writer 이행 계획 10B와 C4 보고서를 읽는다.
2. `src/core/engine.py`의 실제 `on_signal/on_order`와 같은 파일 `RiskManager._calculate_position_size`, `src/execution/safety/`의 command/runtime/정책 소비 경로를 다시 찾아 파일별 소유권을 고정한다. 별도 `src/risk/manager.py` 클래스와 혼동하지 않는다. 경로/함수는 수정 전에 `rg`로 확인한다.
3. CV·LLM·시간 규칙·시장 자료·sector·손절·score·정책 version을 immutable decision facts로 묶는 계약과 갱신 주체를 정한다. 현재 allow bool이나 broker 체결 metadata를 원 판단의 증거로 대신 쓰지 않는다.
4. 구현, 독립 실제 큐 인수, 최종 리뷰를 분리한다. 같은 파일을 동시에 수정하지 않는다.

**Do**

1. 실제 EventBus/큐/on_signal/on_order/SQLite/Portfolio/RiskManager를 사용하고 외부 HTTP·시계만 합성한 실패 시험부터 추가한다.
2. CV·LLM·현금·다른 예약·정책이 network await 중 바뀌면 stale/reject 및 **POST0**이 되는지 고정한다. 무관 fill/ACK가 무조건 전역 stale을 만들지 않는 대조도 둔다.
3. 정상 MARKET BUY·LIMIT/MARKET SELL을 보존한다. 실제 request의 수량·가격·계좌·origin·부모와 prepare/claim/final을 결합하고, 동일 pure sizing kernel로 최종 경제/예약을 재검사한다.
4. UNKNOWN 예약 유지, 취소 ACK≠최종성, 중복 dispatch0, 부분체결·늦은 응답·caller cancel·SQL/게시 실패·새 runtime restore를 확인한다. 강제 `trading_ready=True`나 합성 startup 허가로 운영 장벽을 우회하지 않는다.

**See**

- 실제 정상/거부 대조·RED/GREEN·독립 spec/quality 리뷰와 exact SHA를 기록한다.
- 전체 UTC/KST는 **직렬** 실행하고 격리 위반0·문법·비밀정보 검사를 통과한다.
- “순수 helper 통과”, “raw writer 차단”, “모델 합의”를 정상 거래 파이프라인 이행 완료로 세지 않는다.

### 2. 10A3/10C — 단일 owner와 남은 writer/sender

B2/B3의 정상 거래 연결 후에 명시 factory와 프로세스 종료 순서를 묶는다. scheduler SAFE/USER, KOFR, 별도 수동 CLI/IPC, broker/effect 송신, WS quote, fill 수집→지원 증거→core receipt, sync·면제·초기 인계·일일 reset을 **한 경로씩** 이행한다.

각 경로의 현재 writer 검색 목록, 성공/실패 실제 caller 시험, 제거된 중복 경제·보호·원장 쓰기를 남긴다. IPC가 없으면 raw broker fallback하지 않는다. 장부 snapshot 부재/빈 응답/timeout은 예약 해제나 초기 인계 증거가 아니다.

### 3. 공식 증거 — 코드와 별도 작업

[공식 근거 요청표](https://github.com/qwq-partners/qwq-ai-trader/blob/ab044c4edeb702911fee998973cb00a263a7b085/docs/integrations/kis-execution-evidence-request-2026-09-18.md)의 최초 잔고/체결 cutoff 또는 검증된 재조정, 취소 최종성, 원주문/자식 누적량 의미, 다중 페이지 일관성을 공식 명세나 승인된 비식별 실응답으로 확인한다.

증거가 없으면 **미지원/시작 차단 유지**다. 합성 시험은 외부 계약 증명이 아니다. **모든 MODIFY는 현재 미지원**이며 수량/가격 증가분의 실제 추가 예약·위험 상한 인수 없이 지원 상태로 바꾸거나 송신하지 않는다.

### 4. 최종 통합 판정

실제 전체 C/F/G/R, 남은 legacy 분석 원장 projection, 과거일 회계, 장기 이력/부하 시험 및 독립 broad 리뷰 이후 main/운영 전환을 별도로 판정한다. 사용자 목표는 **기존 위험 한도 유지 + 비용 차감 KODEX200 초과수익 검증**이며 아키텍처 안전성 통과가 투자 엣지 증명은 아니다.

### 5. 독립 운영 수정 후보는 별도 release로

`bdda0e9`의 limiter 취득별 lease·GET finally 정리는 별도 운영 개선 후보이나 커밋 전체가 safety collector에 의존한다. 필요하면 main 기준에서 관련 hunk만 추출하고 취소/timeout/late-response·기존 dict GET·US/unscoped 호환 인수와 독립 리뷰를 새로 한다. 이번에 추출·배포한 것으로 보고하지 않는다. `scripts/dev/claude_review.py`는 개발 도구이며 그 배포 때문에 거래 봇을 재시작할 이유가 없다.

## 모델 배정·실패 처리

- 전역 정본: `/home/ubuntu/.config/ai-agents/model-routing.md` (`ai-routing-v1-2026-09-20`).
- 일반 구현/테스트 Terra medium~high, 다중파일 감사 Sol/high, 핵심 상태·돈 경로 구현 Astra/high 또는 검증된 Opus/high, 독립 최종 critical 리뷰는 구현자와 다른 Astra/Opus xhigh. 문서/기계적 확인 Luna low~medium.
- coordinator1+worker3 **전체 공급자 합산** 상한. 별도 worktree·동일 base·파일당 단일 작성자·worker fanout0.
- Fable은 실제 Opus fallback이 관찰돼 자동 배정 제외다. Sonnet/Haiku 가용성을 검증됐다고 가정하지 않는다. 실제 모델 미노출은 요청값과 구분한다.
- Claude가 구현한 브랜치의 Codex 교차 리뷰는 `bash scripts/dev/codex_review.sh`의 read-only 경로를 사용한다.
- 외부 Opus 정적 리뷰는 engine 브랜치의 `scripts/dev/claude_review.py`를 사용한다. tools-off·명시 세션·한정 입력, 모델/result 검증, startup/idle/absolute deadline을 유지한다. 900초 제한을 숨기거나 무제한 대기로 바꾸지 않는다.
- 긴 리뷰의 timeout은 승인도 모델 장애 증명도 아니다. 작은 대조→범위 분해→실제 지적 재현/수정/재리뷰 순으로 처리한다. C4의 실패와 최종 승인은 별도 원장에 남아 있다.
- 다른 세션·전역 auth/config·운영 자격·토큰을 수정하지 않는다.

## 재현 명령 (오프라인)

engine 작업공간에서 실행한다. 외부/운영 자격을 전달하지 않고 프로젝트 테스트 가드를 유지한다.

```bash
cd /home/ubuntu/projects/qwq-ai-trader/.claude/worktrees/engine-safety-design-20260917
git status --short
git log -5 --oneline
git diff ab044c4 -- src scripts tests config requirements.txt
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC \
  PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  -q -p no:cacheprovider --tb=short tests
```

다음 실행은 같은 명령의 `TZ=Asia/Seoul`로 **앞 suite 종료 후** 수행한다. C4 원 완료 기록은 양TZ 각각4403passed/기존xfail2다. 새 변경 후에는 이 숫자를 복사하지 말고 실제 결과를 기록한다.

## 다음 세션에게 전달할 시작 문장 (2026-09-21)

> engine 브랜치의 단계 원장(`docs/reviews/b2b3-stage-ledger-2026-09-20.md`)의 "상태 요약"·"공통 작업 방법"·"S5 마감"과 이 문서의 "attach 설치 전에 닫아야 할 것" 15항을 읽어 주세요. B2/B3(S1~S5)를 재구현하지 말아 주세요 — 독립 인수 37건(`tests/test_execution_signal_gateway_acceptance.py`)은 새 제품 코드가 **한 글자 안 고치고** 통과해야 하는 기준선입니다. 먼저 현재 HEAD/dirty 상태/다른 세션의 PR 을 확인하고, **10A3a(정책 부품·owner 게이트 인수)와 10A3b-1(거부형 설치기 `install_attached_runtime`)도 끝났습니다 — 전부 부품이고 제품 호출자 0건이라 차단 사유는 하나도 닫히지 않았습니다.** 10A3b 에 앞선 여섯 결정은 사용자 위임으로 보수적 기본값에 확정돼 있습니다(`docs/superpowers/plans/2026-09-21-10a3-install-preparation.md` §3-1 — 뒤집으려면 사용자에게 확인). **지금 임계 경로는 코드가 아니라 증거입니다:** ① `docs/integrations/kis-execution-evidence-narrowing-2026-09-21.md` 의 **질문지 31문항을 KIS 에 보내거나 비식별 실응답을 모으는 것은 사용자의 일**입니다 — 답이 오면 같은 문서 ⑤절의 코드 지점(증거 파서의 TR·필드 철자·`cncl_yn`, `reconcile` 의 UNKNOWN 해제 경로, startup 대사 스키마)부터 Plan→Do→See 로 진행해 주세요. 공식 Wikidocs(`wikidocs.net/book/7847`)는 봇 차단으로 읽지 못한 가장 유력한 미확인 출처입니다. ② 답을 기다리는 동안 할 수 있는 코드 작업은 10C 의 남은 writer/sender(면제 추가의 owner command, 추세/VIX 갱신 순서의 배선 계약 포함)와 10A3b 의 나머지(시계 주입·attach 인지형 관측·store 의 읽기 전용 개방)인데, **둘 다 live 파일을 만지고 attach 가 설치되기 전에는 소비자가 없습니다 — 착수 전에 사용자에게 우선순위를 확인하세요.** ③ 운영(main) 쪽 별도 과제: KIS 구 TR_ID 5종의 신 TR 이행, 운영 폴백 루프의 현행 결함 5건(둘 다 작업 칩). main 병합·배포·재시작·주문·설정·Toss grant 변경은 이 인계만으로 실행하지 말고, `trading_ready` 강제·합성 startup 허가로 장벽을 우회하지 마세요. 완료/미완/실제 검증, 요청 모델/관측 모델을 구분해 보고해 주세요.

## Claude에게 전달할 시작 문장 (2026-09-20 원문 — 수행됨)

> 이 문서와 engine 브랜치의 최신 writer 이행 계획을 읽고, 기존 C4 완료 부분을 재구현하지 말아 주세요. 먼저 현재 HEAD/dirty 상태/다른 PR을 확인하고 B2/B3의 실제 qualification·최종 sizing request-bound 인수를 RED부터 진행해 주세요. Plan→Do→See, 역할별 모델·격리 병렬·독립 리뷰를 유지해 주세요. main 전체 병합·배포·재시작·주문·설정/토스 grant 변경은 이번 인계만으로 실행하지 마세요. 공식 startup·취소 증거와 전체 C/F/G/R이 미완이면 차단을 유지하고, 완료/미완/실제 검증을 구분해 보고해 주세요.
