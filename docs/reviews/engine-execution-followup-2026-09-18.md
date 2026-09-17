# KR 실행 안전성 후속 — 단계별 Plan·Do·See

기준 feature `dd3b0f7`. 사용자가 후속5단계를 순서대로 처리하고 마지막 통합 리뷰·잔여 과제 정리를 요청했다. 운영 상태 조회/실 API/주문/설정·Toss 변경과 main 병합·배포는 수행하지 않는다. 이 문서는 진행 원장이며 완료 보고서가 아니다.

## 단계1 — legacy 조회와 실제 broker 연결

- Plan: 기존 GET/토큰 복구/공용 원장 limiter를 유지하면서 실제 status·연속조회 header를 수집기에 전달한다. 다음 페이지 N을 송신하고 취소/timeout 뒤 limiter를 반환한다. dev에 실전 TR을 보내지 않는다.
- Do: Task7 Astra/high 담당이 실제 broker+fake HTTP/토큰 경계 시험을 RED부터 구현했다. API 부재16failed와 취소 후 busy 잔류1failed를 확인한 뒤 신규23+기존99의 관련122시험을 통과했다. 부모도 수집기/신규 통합71시험을 재실행했다.
- See: fresh Astra/xhigh 독립 spec/quality 리뷰에서 P1 1·P2 1을 재현했다. 실제 aiohttp `istr` 헤더 거부는 원래 항목 순회·str 정규화·중복 충돌 검사로, stale 재취득 뒤 이전 caller의 busy 해제는 취득별 lease 일치 검사로 수정했다. 추가 RED는 헤더4·소유권5건. 신규32+기존99의 **131 passed**, reviewer 별도 재실행131passed/10.04s·격리0. 한정 재리뷰 spec/quality **APPROVED, 신규 P0/P1/P2 0**. 조회 완결은 최종성·거래 허가를 뜻하지 않는다.

전체 KST/UTC 각각2224passed/기존xfail2·warning1(92.28/90.40초)·격리0·문법/비밀정보 패턴 검사 통과. 소스 동결 SHA-256: broker `e34ff8d54a431e007b7bb030f94309c157c41970c718e5220bb5b64096e8cb7e`, limiter `ce9f8a30fec790488ad79a6106b1294ae5622c500b7b2ef81f49700ef2b000e0`, 시험 `2be304b039932918d35369f729f1742686884061a9314014eeda9a966fcfd051`.

## 단계2 — 경제·보호·원장/복구

- Plan: `dd3b0f7`의 DTO/경제/core receipt116시험을 유지하고 보호repair·KST일자전환·명시종결 initialR·durable outbox ACK를 보완한다. 기존 TradeStorage 비동기 큐/PositionLedger 오류삼킴은 durable ACK가 아니다.
- Do: 아직 후속 구현 전. 현재 블록의 집중 기준선164passed/2.30s·격리0을 재확인했다.
- See: 현재 APPLIED는 경제/보호 checkpoint 게시이며 journal_synced가 아니다. 경제 사실을 재가감하거나 stage/high/R을 추정해 복구하지 않는다.

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
| batch | 보유 current/high 쓰기, monitor/rebalance/trim SELL·BUY | quote/intent 경유, 후보 파일은 거래 정본 아님 |
| 시작 | run_trader 잔고주입/_load_existing_positions/면제·보호·daily복원 | startup 장벽·최초 인계 증거 |
| WS | run_trader 시장 이벤트 발행 | 보유 quote의 동일 owner |

Do/See: 아직 전체 이행 전이다. 특히 정정의 수량/가격 증가는 request-bound 추가 예약·위험 상한 없이는 송신하지 않는다. 신규 가드로 legacy 호출을 거부하는 것만으로 정상 기능의 이행을 완료했다고 보고하지 않는다.

## 단계4 — 공식 계약 증거

Plan: 공개 KIS 공식 자료에서 현행 TR의 취소/정정 체인 의미와 잔고–체결 cutoff를 확인한다. 최신 TR로 자동 치환하지 않는다.

Do: Astra/high가 기존 고정 revision `b4e6249714418aa57833d1cbbbced39cbcc5b125`의 legacy·Postman·현재 예제·체결 통보를 재확인했다. 실 API/계좌/자격은 사용하지 않았다. [계약 정본](../integrations/kis-execution-contract-2026-09-17.md) 참조.

See: 요청/페이지 계약 외에, 취소/정정 원행과 자식행의 누적 중복 처리·최종수량 정의, 공통 snapshot/cutoff·지연 상한은 확인 자료 범위에서 **미입증**이다. API에 기능이 없다고 단정하지 않는다. 공식 포털 인증 후 상세 명세는 확인하지 못했다. ACK·빈 목록 반복·웹소켓 연결만으로 startup/최종성을 열지 않는다. 자동 최초 인계/미지원 체인 승격은 완료가 아니다.

## 단계5 — 마지막 통합 리뷰

Plan: 실제 전체 C/F/G/R 시험명을 명세와 대조하고 독립 broad 리뷰·수정·한정 재리뷰·UTC/KST 전체 검증을 수행한다. 이전 모듈 리뷰를 대신 쓰지 않는다.

Do/See: 아직 실행 전이다. 충족/미충족과 운영 전환의 증거 조건을 분리하고, 미충족 상태에서 main/운영 GO를 선언하지 않는다.
