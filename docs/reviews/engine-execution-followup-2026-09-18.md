# KR 실행 안전성 후속 — 단계별 Plan·Do·See

기준 feature `dd3b0f7`. 사용자가 후속5단계를 순서대로 처리하고 마지막 통합 리뷰·잔여 과제 정리를 요청했다. 운영 상태 조회/실 API/주문/설정·Toss 변경과 main 병합·배포는 수행하지 않는다. 이 문서는 진행 원장이며 완료 보고서가 아니다.

## 단계1 — legacy 조회와 실제 broker 연결

- Plan: 기존 GET/토큰 복구/공용 원장 limiter를 유지하면서 실제 status·연속조회 header를 수집기에 전달한다. 다음 페이지 N을 송신하고 취소/timeout 뒤 limiter를 반환한다. dev에 실전 TR을 보내지 않는다.
- Do: Task7 Astra/high 담당이 실제 broker+fake HTTP/토큰 경계 시험을 RED부터 구현했다. API 부재16failed와 취소 후 busy 잔류1failed를 확인한 뒤 신규23+기존99의 관련122시험을 통과했다. 부모도 수집기/신규 통합71시험을 재실행했다.
- See: fresh Astra/xhigh 독립 spec/quality 리뷰에서 P1 1·P2 1을 재현했다. 실제 aiohttp `istr` 헤더 거부는 원래 항목 순회·str 정규화·중복 충돌 검사로, stale 재취득 뒤 이전 caller의 busy 해제는 취득별 lease 일치 검사로 수정했다. 추가 RED는 헤더4·소유권5건. 신규32+기존99의 **131 passed**, reviewer 별도 재실행131passed/10.04s·격리0. 한정 재리뷰 spec/quality **APPROVED, 신규 P0/P1/P2 0**. 조회 완결은 최종성·거래 허가를 뜻하지 않는다.

전체 KST/UTC 각각2224passed/기존xfail2·warning1(92.28/90.40초)·격리0·문법/비밀정보 패턴 검사 통과. 소스 동결 SHA-256: broker `e34ff8d54a431e007b7bb030f94309c157c41970c718e5220bb5b64096e8cb7e`, limiter `ce9f8a30fec790488ad79a6106b1294ae5622c500b7b2ef81f49700ef2b000e0`, 시험 `2be304b039932918d35369f729f1742686884061a9314014eeda9a966fcfd051`.

## 단계2 — 경제·보호·원장/복구

- Plan: `dd3b0f7`의 DTO/경제/core receipt116시험을 유지하고 보호repair·KST일자전환·명시종결 initialR·durable outbox ACK를 보완한다. 기존 TradeStorage 비동기 큐/PositionLedger 오류삼킴은 durable ACK가 아니다.
- Do: Astra/high 두 역할로 보호 재생·복구(8A)와 전용 경제 이벤트 원장 전달(8B)을 분리 구현했다. 실제 core 큐·SQLite·Portfolio/ExitManager를 사용하며 초기 위험값은 아직 pending이다. 부모 교차 시험은 원장 ACK→보호 복구→후속 체결→재시작을 연결했다. 전달 완료 행을 health의 pending으로 잘못 세던 문제도 RED→GREEN으로 고정했다.
- See: fresh Astra/xhigh 독립 리뷰는 8B의 전용 이벤트 원장 경계를 한정 승인했고, 8A에서 **P1-A1**을 재현했다. 손절 접촉 quote의 저장 실패 뒤 같은 프로세스 restore 및 SQLite 재개방에서 repair가 누락을 모르고 APPLIED로 승인했다. 정상 저장 대조군은 BLOCKED였다. 아래 durable admission 수정 후 다른 fresh Astra/xhigh가 한정 재리뷰하여 **spec/quality APPROVED, 신규 P0/P1/P2 0**으로 판정했다. 독립208passed/7.68초·격리0와 별도 게시 실패/호출 취소·fill 경합3사례를 확인했다. 수정 전 전체 KST2276passed는 최종 근거로 사용하지 않는다.

8A 수정은 가격 입력의 durable admission과 보호 적용 완료를 분리했다. admission 저장·게시 전에는 성공 수락이나 현재가 view 게시를 하지 않고, 그 이후 적용 실패는 영속 미해결 입력으로 남겨 repair를 차단한다. 뒤의 높은 가격으로 미해결 손절 접촉을 덮을 수도 없다. 기존 high/BE·중간 손절 접촉 보존 인수는 유지했다. 이 변화는 아직 운영에 설치되지 않은 새 runtime의 수락 경계 변경이며 가격·손절 정책 수치 변경이 아니다. 정상 quote의 commit/publish가2회가 되므로 운영 연결 전 처리량/지연 검증은 남는다. 저장 전 실패 입력까지 재시작으로 되살리는 보장은 없고 upstream 재전달이 필요하다.

8B의 ACK는 새 `execution_journal`의 이벤트 키·payload가 같은 DB transaction으로 저장됐다는 뜻이다. 기존 `trades`/`trade_events`/PositionLedger·성과·canary projection의 완료를 뜻하지 않는다. 최초 리뷰에서는 PostgreSQL pool 경계가 합성이었으므로 임시 디렉터리의 별도 PostgreSQL16.15·UNIX socket에서 실제 DDL/SQL/asyncpg/중복·충돌·core ACK 실패 후 재개방 **5건**을 추가 실행했다. 기존 합성28건과 합계33passed, 독립 재실행33passed/6.13초·skip0·격리0·한정 spec/quality APPROVED. 기존 DB/자격/운영 서비스는 사용하지 않았고 fixture가 만든 서버·DB만 종료/정리했다. DB 전원 유실이나 모든 race schedule·운영 DB 인수를 주장하지 않는다.

최종 동결 후보 전체 검증은 KST **2292passed/2 known xfailed/1 기존warning,97.70초**, UTC **2292passed/2 known xfailed/1 기존warning,97.34초**다. 두 번 모두 격리0·문법·비밀정보 패턴 검사를 통과했고 신규68시험(보호34·합성원장28·실제DB5·교차1)을 포함한다. 핵심 해시: runtime `824070f81bd5300b99d96a9c3aeacd8ff72f641480247fead846a2be1b5a6512`, recovery `499b8d1ff1b65984b0b1c2c073fc3c7c3a28624feda53548d476c1d335d55eae`, journal `1e1bda8a83d56804edc218e23064fa116e9c5e3a5967f5b454b631e555dc6ee2`, 실제DB시험 `960ba02c42f8309ac3eea66d01ff07df124f84370726d9516cd8e7d98ae8334f`.

남은 단계2: 확정 사실 ingress·quiescence와 KST 일자 전환, 최초 손절/지원되는 최종성 증거 및 R 원장, 기존 분석 원장 projection. 경제 사실을 재가감하거나 stage/high/R을 추정해 복구하지 않는다. **8A/B 한정 완료이며 단계2 전체 완료가 아니다.**

### 후속 Task8C — 일자 경합·초기 R (core 범위 한정 검증 완료)

Plan: 확정 체결 접수와 주문 허가를 분리하고, 첫 await부터 작업을 추적한다. 일자 전환은 명시 가격 시각/출처·포트폴리오 fingerprint와 fence를 요구한다. 초기 R은 최초 등록 당시 uncapped 손절 증거와 검증된 최종 누적대금으로 계산한다. 공유 runtime 배선은 직렬 적용한다.

Do: Astra/high 구현 역할을 일자 전환(C1)과 최초 R·typed 원장(C2)으로 분리했다. C2는 초기 R 순수 reducer·증거 불변성·typed outbox와 실제 임시 PostgreSQL R 인수3건을 추가했다. 등록 시 severe가 state SL5를2로 먼저 바꾸므로 원본 registration_params의 SL과 당시 설정을 보존해 uncapped R을 계산한다. 보호 SL 정책 자체는 바꾸지 않았다. 실제 core 큐 훅은 연결 전 RED로 유지했으며 단위 시험 통과를 실경로 완료로 표시하지 않는다.

부모 진단에서 degraded quote250건의 checkpoint가 약1.26MB까지 커지는 중복 snapshot 저장을 확인했다. 모든 가격·시각·순서·hash를 유지하고 변하지 않은 scope 두 벌만 digest 참조로 바꿨다. 기존 전체 snapshot 형식도 재생한다. 신규4건 RED→GREEN 및 관련39passed·격리0을 확인했다. 별도 합성 진단은 같은250건에서 약145KB였으나 병렬 개발 중 측정이므로 성능 인수/운영 처리량 증거가 아니다. 이력은 여전히 무한 누적될 수 있어 장기 보관/별도 append-only 저장소 및 부하 인수는 남는다. R 원장 ACK 전용 플래그는 보호 증거에서 제외하지만 R 값·손절·lifecycle 자체는 제외하지 않는다.

See: C1 소스 동결 뒤 부모가 C2 실제 큐 훅을 연결했다. 연결 전 신규7건과 기존 실큐1건의 RED를 확인했고, 이후 관련6파일161passed·격리0을 확인했다. 독립 C2 리뷰가 발견한 정상 SUPERSEDED 관측의 R 확정 차단(P2)은 실제 큐 재현→수정했다. C2 연결부 후속 독립 리뷰도163passed·격리0, 미해결 P0/P1/P2 0으로 한정 승인했다. 초기 R 삭제/False→0 변조 및 owner-lock 대기 caller 취소를 별도 독립 시험했다.

중간 전체 KST2386passed/2xfail/1warning(114.70초), UTC2386passed/2xfail/1warning(109.28초), 각각 격리0·문법/비밀패턴 통과였다. 그러나 C1 독립 리뷰가 그 후보에서 **P1 3건**을 추가 재현했으므로 이 숫자는 완료 근거가 아니다. 늦은 시세의 직접 view 게시가 valuation 시각 검사를 우회하며 후속 fill 뒤에는 as_of 하한도 사라졌고, 다음날 부분매도 후 잔여 보유의 view가 전일 DTO 가격으로 역행했다. 오래된 손절 접촉은 현재 시점의 새 청산 제안으로도 잘못 승격됐다. 실제 큐·SQLite·시각 주입으로 각각 재현했으며 별도 구현자 Astra/high와 독립 Astra/xhigh가 수정/재리뷰를 나눠 수행한다.

수정본은 모든 게시에 같은 가격 선택 함수를 사용하고, 최신 체결의 증분 대금/수량을 fallback으로 쓴다(최신 시장 틱 가격 보장이 아니다). 명시 시각이 알려진 하한보다 오래된 새 시세는 수락 전에 `stale_market_quote`로 거부한다. 이미 영속 수락된 입력은 후행 검사로 버리지 않는다. 추가 재현에서 정상 시세끼리의 역순 도착·재시작에도 같은 위험이 있어 종목별 `latest_explicit_quote` 한 행을 admission과 같은 commit에 저장했다. 시장 사실 digest는 호출 intent/새 수신시각과 분리하며, 잘못된 watermark는 게시/restore를 실패시킨다. 시각 없는 legacy 입력에 신선도를 만들어 주지 않고, 원 시장 시각·출처를 보호 결정/재생 증거에도 보존한다.

이 후보의 관련6파일은 KST237passed/15.78초·UTC237passed/15.52초(격리0)였다. fresh 재리뷰는 원본 P1 해소를 확인했지만 valuation과 동일 수신시각의 후속 정상 시세를 view가 반영하지 않는 **P2**를 추가 재현했다. 동일시각의 순서는 admission version과 수신시각을 함께 비교하도록 수정했다. 실제 후속 시세2건 RED와 이전 시세 대조2건을 포함한 회귀4건을 추가했다. fresh 독립 재리뷰는 최종 동결본에서 KST344passed/32.52초·UTC344passed/24.40초(격리0), 기존 P1 3건·신규 P2 해소와 잔여 P0/P1/P2 0을 확인하여 **C1 수정 범위 한정 APPROVED**로 판정했다. 이 승인은 전체 설계/운영 GO가 아니다.

최종 C1/C2 통합 후보의 소스·시험 patch(기준8978f68) SHA256은 `f46687dacf410a822aaa310be34be0ab164374b3b400ebfefc7a06f940989ea3`이다. 전체 KST **2425passed/2 known xfailed/1 기존warning,134.96초**, UTC **2425passed/2 known xfailed/1 기존warning,117.07초**로 두 번 모두 격리0·문법/비밀패턴 검사를 통과했다. 기존 대비 신규133건이며 실제 임시 PostgreSQL 시험은 이전5건+R3건이다. 핵심 해시: runtime `3e272ba3830b40d85f7270965483ae24aad9badc905f7d4372296ea78a90c067`, day 시험 `11428e2a0380afc621df34f486af5f3dbf5826fbcf57a5ed1cb931664ebd532a`, initial R `48fd69d7e7440c5e8da59a9ca5ef47d1b6a5d9375b455df2a3e49d6b204728da`.

잔여: 완료 ingress ticket/Future/context의 메모리 누적·전수 순회, checkpoint/outbox·degraded 이력의 장기 크기/처리량, 기존 분석 원장 projection, 전일 늦은 체결의 과거일 회계, 실제 일일 scheduler writer 연결이다. 최초 인계·취소/정정 계약 근거를 시험 fixture로 대체하지 않는다. `trading_ready=False`와 운영 미설치를 유지한다.

## 단계3 — 단일 owner·송신점 이행

Plan: 아래는 Terra/high 읽기 전용 조사에서 얻은 기준 HEAD의 주요 경로다. 메서드를 기준으로 이행하고 코드 수정 후 시험명/새 위치를 추가한다. **목록만 있다고 이행 완료가 아니다.**

| 영역 | 현재 주요 writer/송신점 | 필요한 경계 |
|---|---|---|
| core 경제 | update_position, RiskManager.on_fill | 누적 관측→실큐→commit/게시 receipt |
| core 가격 | update_position_price | quote view와 보호 변경의 직렬화 |
| core 주문 | on_signal 예약·90초 SELL fallback, on_order | durable intent/claim·최종 guard |
| broker | submit_order/cancel_order/modify_order | 마지막 await 뒤 단회POST, UNKNOWN 보존 |
| scheduler 체결 | run_fill_check의 emit 뒤 BUY등록/SELL직접감산·risk·journal | receipt 기다림, 후속 원장은 outbox |
| scheduler 청산 | _check_exit_signal/EOD exit/REST quote | 보호 판단과 SELL intent 분리 |
| 일일 초기화 | engine.reset_daily_stats, scheduler 전일취소·pending clear | 대사 전 취소/해제 금지, 원자 rollover |
| KOFR | 직접BUY/SELL·safe_asset_state write | safe_asset 신뢰경로·체결 이후 상태 |
| 수동 | 직접BUY·면제 설정·지시목록 삭제 | user 신뢰경로·UNKNOWN 보존·면제 유지 |
| 별도 수동 CLI | `scripts/liquidate_all.py::liquidate_kr`, `scripts/sell_specific.py::main`의 직접 submit·취소 후 fallback | 같은 계좌 owner/intent를 공유하는 명시 명령 경계; 취소 불명 때 재주문0·최초 목표 잔여량만 허용 |
| batch | 보유 current/high 쓰기, monitor/rebalance/trim SELL·BUY | quote/intent 경유, 후보 파일은 거래 정본 아님 |
| 시작 | run_trader 잔고주입/_load_existing_positions/면제·보호·daily복원 | startup 장벽·최초 인계 증거 |
| WS | run_trader 시장 이벤트 발행 | 보유 quote의 동일 owner |

Do/See: 아직 전체 이행 전이다. 특히 정정의 수량/가격 증가는 request-bound 추가 예약·위험 상한 없이는 송신하지 않는다. 신규 가드로 legacy 호출을 거부하는 것만으로 정상 기능의 이행을 완료했다고 보고하지 않는다.

부모 추가 호출점 점검에서 별도 수동 CLI 두 경로를 확인했다. 현재 취소 예외를 무시한 뒤 포지션 조회 수량으로 재주문하며, `sell_specific`은 최초 요청량보다 큰 기존 보유까지 fallback 대상으로 삼을 수 있다. 소스 읽기만 수행했으며 스크립트의 import 시 `.env` 로딩/실행은 하지 않았다. 스케줄러 수동 매수와 별개 프로세스이므로 인프로세스 runtime 바인딩만으로 단일 owner가 완성되지 않는다. 이행 시 명시 owner 명령 전달/프로세스 소유권 계약을 갖추고, 미지원 경로는 지원 완료로 세지 않는다.

## 단계4 — 공식 계약 증거

Plan: 공개 KIS 공식 자료에서 현행 TR의 취소/정정 체인 의미와 잔고–체결 cutoff를 확인한다. 최신 TR로 자동 치환하지 않는다.

Do: Astra/high가 기존 고정 revision `b4e6249714418aa57833d1cbbbced39cbcc5b125`의 legacy·Postman·현재 예제·체결 통보를 재확인했다. 실 API/계좌/자격은 사용하지 않았다. [계약 정본](../integrations/kis-execution-contract-2026-09-17.md) 참조.

See: 요청/페이지 계약 외에, 취소/정정 원행과 자식행의 누적 중복 처리·최종수량 정의, 공통 snapshot/cutoff·지연 상한은 확인 자료 범위에서 **미입증**이다. API에 기능이 없다고 단정하지 않는다. 공식 포털 인증 후 상세 명세는 확인하지 못했다. ACK·빈 목록 반복·웹소켓 연결만으로 startup/최종성을 열지 않는다. 자동 최초 인계/미지원 체인 승격은 완료가 아니다.

## 단계5 — 마지막 통합 리뷰

Plan: 실제 전체 C/F/G/R 시험명을 명세와 대조하고 독립 broad 리뷰·수정·한정 재리뷰·UTC/KST 전체 검증을 수행한다. 이전 모듈 리뷰를 대신 쓰지 않는다.

Do/See: 아직 실행 전이다. 충족/미충족과 운영 전환의 증거 조건을 분리하고, 미충족 상태에서 main/운영 GO를 선언하지 않는다.

### 실제 인수 매핑 (bdda0e9 이후 후속 증거 누적)

아래는 부모가 시험 함수와 실제 호출점을 대조한 **미완 범위 목록**이다. 전체 통합 승인이나 독립 broad 리뷰가 아니다. `tests/` 아래 파일명을 사용하며 뒤 단계에서 같은 표를 갱신한다.

| ID | 현재 구체 근거 | 남은 전체 인수 |
|---|---|---|
| C1/C8 | `test_execution_lifecycle.py::test_cancel_result_never_releases_original_sell_reservation`, `test_execution_guards.py::test_common_safety_barrier_applies_to_all_trade_commands` | 실제 broker cancel→scheduler fallback 연결에서 추가POST0/원pending 보존 |
| C2/C6 | `test_kis_order_evidence.py::test_cancel_quantity_fields_do_not_invent_proven_finality`, `test_kis_execution_query_integration.py`의 실제GET 페이지/실패 시험 | 취소 체인 미지원 유지, 조회 결과→실제 intent 대사 배선 |
| C3 | `test_execution_lifecycle.py::test_replacement_requires_terminal_evidence_and_applied_fills` | 합성 최종성은 공식 취소 계약 증명이 아님. 지원 증거가 없어 실경로 다음5주 주문은 미지원 |
| C4 | `test_execution_lifecycle.py::test_evidence_other_scope_never_matches`, `test_late_or_wrong_sender_result_cannot_release_new_attempt` | 실제 수집기 scope→broker ref→체결큐 연동 |
| C5 | `test_execution_lifecycle.py::test_real_store_restart_preserves_claim_and_unresolved_reservation`, `test_kr_final_dispatch.py::test_post_never_repeats_after_auth_network_or_malformed_reply` | 실제 broker·owner 연결에서 POST 전후/ACK 저장 종료 인수 |
| C7 | `test_execution_lifecycle.py::test_concurrent_claims_and_duplicate_prepare_have_one_sender` | 실제 engine와scheduler 동시 fallback을 같은 intent로 합류 |
| F1/F2 | `test_execution_runtime.py::test_real_queue_buy40_60_then_sell40_60_applies_cash_and_protection_once` | core 실큐 범위 충족. scheduler의 중복 적용 제거 후 전체 경로 재실행 |
| F3 | 같은 실큐 시험 및 `test_full_engine_loop_delivers_receipt_and_reopen_replays_without_reapplying` | 실제 조회→큐 replay 인수 추가 |
| F4 | 시작 장벽·unknown 보존 모듈 시험 | 잔고 snapshot/cutoff 증거와 sync/fill 경합 실경로 없음; 미충족 |
| F5/F5a | `test_execution_runtime.py::test_committed_fill_with_failed_publication_recovers_without_double_cash`, `test_execution_fill_application.py::test_economic_commit_failure_keeps_inbox_and_reservation_for_recovery` | core 범위 시험. broker송신/ACK 저장까지 결합 필요 |
| F5b | `test_execution_runtime.py::test_protection_failure_commits_economics_once_and_stays_degraded`, `test_execution_protection_recovery.py::test_queue_failed_registration_duplicate_repair_and_reopen`, `::test_lost_stop_quote_cannot_be_ignored_after_restore` | Task8A 실제 repair·재시작 한정 승인. scheduler/외부 writer가 같은 증거를 보존하도록 연결한 전체 인수는 미완 |
| F6/F6a | `test_execution_protection_checkpoint.py::test_same_entry_partial_does_not_reset_existing_protection`, `test_execution_runtime.py::test_quote_during_fill_commit_preserves_current_view_and_serializes_high_be` | quote/fill core 범위 충족. sync 및 레짐·면제 writer 이행 필요 |
| F7 | `test_execution_protection_checkpoint.py::test_addon_accumulates_small_fills_then_resets_only_once`, `test_execution_initial_r.py::test_runtime_queue_captures_first_stop_and_finality_then_finalizes_once`, `test_execution_initial_r_runtime.py::test_initial_r_failed_commit_or_publish_reopens_without_reapplying_economics`, `test_execution_journal_postgres.py` | 최초 R 및 전용 원장 core 인수는 한정 검증. 공식 취소 최종성 지원·기존 분석 원장 projection·전체 경로 미완 |
| F8 | `test_execution_protection_checkpoint.py::test_missing_protection_degrades_and_later_fill_cannot_fake_recovery`, `test_exemption_survives_fills_and_full_close_clears_owned_protection` | 실제 기존 보유 인계·복구/면제 변경 경로 연결 |
| F9 | `test_execution_runtime.py::test_real_risk_manager_receives_persisted_loss_intent_and_one_use_state`, `test_execution_day_recovery.py::test_day_publish_resets_real_risk_metrics_once_without_legacy_writes`, `::test_rollover_fault_and_reopen_keep_durable_date_and_closed_admission` | runtime 날짜 전환/복원 검증과 실제 scheduler 일일 초기화 writer 이행을 구분. 후자는 미완 |
| G1/G2 | `test_kr_final_dispatch.py::test_market_changes_during_each_await_prevent_http`, `test_execution_guards.py::test_clock_and_snapshot_are_read_at_each_decision_not_at_construction` | 실제 분석/분산대기/LLM→broker 전체 경로·요청/예약 바인딩 미완 |
| G3/G3a | `test_execution_guards.py::test_timezone_host_does_not_change_kst_cutoff`, `test_failure_attempt_hides_previous_normal_until_new_valid_observation` | 현재 위험 publisher를 실제 scheduler 관측 성공/실패에 배선 |
| G4 | `test_execution_guards.py::test_explicit_routes_exempt_only_alpha_and_metadata_cannot_issue_context` | 실제 KOFR/수동/SELL 경로 이행·기존 위험 예외 특성화 |
| G5 | `test_execution_guards.py::test_common_safety_barrier_applies_to_all_trade_commands` | owner→실제 request→HTTP 결합과 DB 장애 장벽 시험 |
| R1/R1a | `test_execution_state_store.py::test_existing_invalid_database_is_rejected_not_recreated`, `test_execution_runtime.py::test_empty_database_does_not_replace_known_live_portfolio` | 최초 인계 근거 부재로 시작 차단 유지. 실제 시작 후 모든POST0 인수 미완 |
| R2 | C1/C2 동결본 KST/UTC 전체 회귀2425passed/기존xfail2, ExitManager 기존 특성화 포함 | 공용 파일 변경 때마다 반복. US 실행 안전성 완료 근거는 아님 |

### 이후 구현 순서의 구체 경계

1. Task8A/B를 리뷰해 보호 repair/원장 ACK를 먼저 닫는다. 다음은 수락된 큐 작업까지 확인하는 KST rollover와 최초 손절·검증된 최종성 증거를 보존하는 initial R이다. 오래된 불명 이력은 추정하지 않는다.
2. 실제 request의 command/TR/계좌범위/종목/side/수량/가격/부모 참조 fingerprint를 durable attempt·claim·예약과 결합한다. 마지막 await 뒤 현재 예약/현금/수량/기존 위험 한도를 다시 확인한다. 시장가 wire 가격0은 예약 금액0이 아니다.
3. 취소/정정 체인 미지원 중에는 **모든 정정(감소·매도 포함) 미송신**을 유지한다. 향후 계약이 확보돼도 가격상승·수량감소처럼 방향이 다른 경우의 추가 현금·계획손실·노출을 독립 계산·예약해야 하며 BUY alpha 통과는 이를 대신하지 않는다.
4. 위 계약이 준비되면 core→broker→scheduler 체결/청산→batch→KOFR/수동→일일 초기화→시작 인계 순서로 writer를 이행한다. 각각 실제 호출점의 성공과 실패 시험을 추가한다. legacy를 단순 거부한 경로는 이행 완료로 세지 않는다.
5. 최초 인계 cutoff·체인 수량 범위 자료를 공식 명세 또는 승인된 비식별 응답으로 보충한다. 소프트웨어 시험 fixture는 그 외부 사실의 증명이 아니다. 미충족 인수를 공개한 뒤 독립 전체 리뷰와 별도 main/운영 전환 판단을 한다.
