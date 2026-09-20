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

8A/B 당시 후속 항목은 확정 사실 ingress·quiescence와 KST 일자 전환, 최초 손절/지원되는 최종성 증거 및 R 원장, 기존 분석 원장 projection이었다. 전자의 core 범위는 아래8C에서 검증했으며 기존 분석 원장 projection과 실제 writer 연결은 남았다. 경제 사실을 재가감하거나 stage/high/R을 추정해 복구하지 않는다. **8A/B 및8C 한정 완료이며 전체 운영 경로 인수가 아니다.**

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

### Task9A/9A2/9B1 — 요청·전송·정책 선행 경계 (한정 리뷰 완료)

- Plan: 실제 계좌/세션·command/path/TR·수량/호가·부모·strategy를 하나의 불변 요청과 fingerprint로 고정한다. 실제 두 기존 위험 gate를 분리 특성화하고, 거래 POST 직전에는 준비한 그 요청을 검사한다. 생성·정책 허용을 예약/송신권으로 오인하지 않는다.
- Do: Astra/high가 request builder160시험과 순수 위험 정책316시험을 구현했고 부모가 prepared transport36시험을 추가했다. MODIFY는 전부 미지원이며 CANCEL 전량 wire0은 양의 원주문 잔여 커버리지와 구분한다. hash helper와 POST는 본문 사본을 따로 받으며 connect/token/hash/limiter 뒤 계좌 설정을 재검사한다. 마지막 guard 이후 POST까지 application await는 없다. 기존 raw send와 운영 broker는 아직 교체하지 않았다.
- See: Astra/xhigh 독립 리뷰에서 요청은 한정 승인했다(관련KST/UTC276건·추가18,905단언; 후자는 pytest 개수가 아님). 전송에서는 문자열`"false"` 등의 허가값을 truthy로 승인하던 P2를 재현해 exact DTO/bool/str 검증으로 고쳤다(신규10 RED→GREEN, 관련318건·원본75단언+5반례 한정 재승인). 정책에서는 정상 빈전략 pending 거부와 Decimal→float의0/Inf/NaN이 재진입을 허용/예외 처리하는 P2 두 건을 수정했다(신규6 RED→GREEN, 정책322건·원본600복합 legacy 비교 한정 재승인). 격리 위반0이며 전체 브랜치 검증은 이 후보 동결 후 다시 수행한다.

정책은 실제 함수의 bool/reason/효과 순서를 보존한다. 일일손실 경고구간의 trend 결측은 실제 코드상 전 전략 허용이고 dynamic/flex 슬롯은 해당 최종 gate에 적용되지 않는다. 문서 설명을 근거로 새 판단을 넣지 않았다. core/USER/SAFE_ASSET/SELL 적용 범위와1.3 sizing/1.015 예약/1.001 현금 gate를 분리하며 sync timeout/일자 리셋의 제안은 실행 장벽 해제가 아니다. 외부 유효 설정은 명시 입력이며 운영 설정을 읽거나 바꾸지 않았다.

### Task9B2 — 실제 요청의 예약·단일 owner 연결 (한정 재리뷰 완료)

Plan: 요청에서 계산한 현금·수량·노출·계획위험, 현재 owner 정책 입력, prepare/claim 및 단회 전송을 연결한다. SELL/CANCEL에 BUY 예산 조건을 새로 적용하거나 미측정 위험을0으로 만들지 않는다. 부모와 Astra/high 구현자가 파일 소유권을 나누고 독립 Astra/xhigh 리뷰를 병행한다.

Do: 순수 자원 계산은 실제 builder·FeeCalculator·planned_risk/risk_quantity_cap을 재사용한다. LIMIT 저평가 입력은 실제 호가 가격 이상으로 계산하고 MARKET wire0은 별도 양의 평가가격과 구별한다. 부모 검토에서 초안의 공통 양의 자산 조건이 SELL/CANCEL까지 막는 것을 발견해4건 RED→수정했다. 현재 자원95시험이다. 새 정책 어댑터는 외부 context에 cash/pending를 캐시하지 않고 현재 Portfolio/보호/위험/attempt로 재구성하며 관측시각과 계산시각을 분리한다.

실큐 부분체결의 새 노출/계획위험 예약 해제도 진행했다. 신규10건 중6RED/4기존방어GREEN을 확인했고 기존 cash와 같은 보수적 비례 해제·write-set 검증을 연결했다. 미측정None과 원본 request binding은 유지한다. 실제 전체체결에서 해당 종목의 sector 효과만 정리하며 다른 sector/sidecar 변경은 거부한다. 관련 실큐·경제·application139passed/격리0을 확인했다.

독립 순수 리뷰의 P2와 부모 연결 점검에서 수량·현금만0인 종결 행의 노출/위험 잔여를 놓치는 문제를 확인했다. 정확한 `reserved_exposure`/`reserved_planned_risk` pair·도메인을 검사하는 공통 predicate를 도입해 pending 투영·일자 quiescence·초기 R에 적용했다. predicate35건 missing-module RED, snapshot canonical32건 RED, 실제 큐로 만든 R/day 후보4건 RED→수정 후 관련207passed/격리0이다. 잔여0/riskNone의 정상 종료 대조와 sibling 위험도 추가했다. 독립 리뷰 원본 probe는 위험 키를 `planned_risk`로 잘못 쓴 한계가 발견되어 원본을 보존하고 재리뷰에서 정정한다. 원본32건을 모두 올바른 위험 키 재현으로 주장하지 않는다.

실제 ExitManager가 허용하는 빈 strategy가 보호 DTO에서 거부되어 정상 체결이 degraded로 남는 별도 문제도 DTO1·실큐2건 RED로 재현했다. None/빈 문자열을 값 그대로 보존하고 필수 종목/intent 식별자 제약은 완화하지 않았다. 명시 registration 인자/복원·잘못된 타입 대조 포함9시험이며 관련178passed/격리0을 확인했다. 당시 모듈 결과만으로 완료를 선언하지 않았으며, 이후 실제 bound command·결과·재시작 연결의 독립 통합 리뷰/최종 검증은 아래에 기록한다.

부모 추가 호출점 점검에서 별도 수동 CLI 두 경로를 확인했다. 현재 취소 예외를 무시한 뒤 포지션 조회 수량으로 재주문하며, `sell_specific`은 최초 요청량보다 큰 기존 보유까지 fallback 대상으로 삼을 수 있다. 소스 읽기만 수행했으며 스크립트의 import 시 `.env` 로딩/실행은 하지 않았다. 스케줄러 수동 매수와 별개 프로세스이므로 인프로세스 runtime 바인딩만으로 단일 owner가 완성되지 않는다. 이행 시 명시 owner 명령 전달/프로세스 소유권 계약을 갖추고, 미지원 경로는 지원 완료로 세지 않는다.

#### 9B2 통합 리뷰와 섹터 인계 수정

순수 자원/정책 snapshot의 독립 재리뷰는 canonical 위험 키32사례·추가302벡터와 원본 정상1736벡터를 재확인했다. KST181시험·UTC확장683시험·격리0, 한정 승인이다. 원본 잘못된 `planned_risk` probe는 그대로 보존하며 원본 스크립트 전체 GREEN으로 보고하지 않는다.

실제 명령 owner는 prepare/예약/effect의 단일 commit, 현재 정책·가격·손절의 claim/final 재검사, 단회 permit·정확 ACK 참조·UNKNOWN 결과 보존을 구현했다. 작성자107시험 중 bootstrap10건과 의미 결함32건의 최초 RED를 구별했다. 나머지65건은 최초 GREEN이었다. 작성자 관련13파일 KST/UTC1048passed는 전체 writer 인수가 아니다.

통합 독립 Astra/xhigh는 관련 KST/UTC905시험 통과와 별개로 **P2 I1**을 발견했다. 소유 주문의 sector가 첫 체결 Position에 이관되지 않아 부분/전량·재시작 후 섹터 한도1을 우회했다. metadata 결측/상이값 × 부분/전량 × 복원 유무8사례가 RED였다. 나머지16사례는 정상 요청→ACK→부분/전량→R, concurrent dispatch, 마지막 await 중 정책 변경, 체결 write-set 공격, 취소 부모 커버리지 대조다. 중간 전체 KST3270passed도 이 결함을 포함하므로 최종 완료 근거가 아니다.

부모는 실제 flow6건과 순수 경제18건(16RED/2최초GREEN)을 추가했다. bound 요청은 저장된 sector를 canonical로 이관하며 명시 None도 외부 관측으로 추측하지 않는다. binding의 누락/잘못된 타입·attempt와 충돌은 실패하고, unbound legacy만 기존 metadata fallback을 유지한다. 잘못된 관측 metadata 타입의 기존 거부도 유지한다. 수정 후 관련8파일318passed/22.79초·격리0이다. 원본 보고서·probe를 보존한 독립 Astra/xhigh 재리뷰는 **원본24passed(8RED→GREEN+16대조), KST216passed/20.21초, UTC929passed/40.58초, 추가50벡터·격리0**을 확인했다. I1 해소·남은 P0/P1/P2 발견 없음으로 Task9B2 검토 범위를 한정 승인했다. 재리뷰 보고서 hash는 `9e64119c4fe66864f2757323c73460b6ee90a5b699aa48fdcb3edaa28063950a`이다.

별도 손상 주입에서 active 예약을 원 binding보다 적게 만든 checkpoint를 restore할 수 있음을 확인했으나, 정상 공개 전이로 이 상태가 생기는 경로는 입증하지 못했다. 현 전이의 보수적 해제/write-set 보존과 구별하는 잔여 방어 항목이며 추가 정상 운영 P2로 세지 않는다.

수정 후 동결 source/test patch(기준4d6a4bb) SHA256 `eb80af55a4288c53dd0fa722cca1d6da2aba1272f7aee9a1b2d8cea008515a61`. 전체 KST **3294passed/2 known xfailed/1 기존warning,139.03초**, UTC **3294passed/2 known xfailed/1 기존warning,127.80초**로 격리0·문법·비밀정보 패턴 검사를 통과했다. 기존8C 대비 신규869시험이며 미래 writer/운영 인수는 포함하지 않는다. 핵심 수정 economics `16bdff6dbc4da2d6268e548bca057a5a71238d463912b982e2cdee31b4ad2a35`, 실제 연결 flow `ed4024d20aa036c9819a6a50c6fdf06d60f25a6ec38b08acad7ffa6a2e218467`이다.

후속 writer 이행은 [Task10 세분 계획](../superpowers/plans/2026-09-18-engine-writer-migration.md)으로 정리했다. 첫 범위는 계좌 singleton lease와 같은 runtime의 command/result 종료 drain이다. 이후 실제 WS 원시각/유효정책 publisher→기존 sizing/qualification 보존→실제 SIGNAL/ORDER→SAFE/USER/CLI·fill·sync/day 순서로 진행한다. 각 조각의 한정 승인을 전체 단계3 완료로 바꾸지 않는다.

### Task10A1 — 계좌 독점·runtime 명령 종료 (한정 승인, 운영 미설치)

Plan: 계좌 lease(Astra/high)와 runtime 종료 배선(부모)을 파일별 분리한다. 기존 수동 CLI/운영 서비스에는 아직 설치하지 않는다. 같은-host·같은 private root의 협력 프로세스 독점이며 다중-host나 별도 root까지 보장하지 않는다. 신규 명령을 먼저 닫고 이미 진행 중인 명령에서 늦게 생성되는 결과 저장까지 기다린다.

Do: 실제 SQLite/runtime/commands에서 HTTP 응답/결과 저장/호출 취소 뒤 남은 결과 및 prepare·quote·정책 commit 중 종료가 먼저 반환하는6건과 결과 저장 실패가 성공 종료로 표시되는1건을 먼저 RED로 확인했다. 첫 await 전 scope 등록과 runtime 결과-task registry/fixed-point drain을 연결했다. 종료 caller의 취소는 저장/송신 task로 전파하지 않고, 이미 닫힌 runtime의 새 prepare/dispatch/publisher를 거부한다. 미송신 요청은 기존 최종 guard로 POST0이며 진행 중 결과 저장을 새 주문 허가로 쓰지 않는다.

계좌 lease는 wire 계좌/상품·KIS 환경의 canonical digest를 사용한다. scope 별칭/config version/SQLite 경로는 독점 키가 아니며, 기존 private root/ancestor와 lock의 UID·권한·inode·link를 재검사한다. 실제 nonblocking flock, fork 자식 FD 정리, thread/fork 직렬화, GC finalizer를 포함한다. 파일을 삭제하거나 PID/TTL로 takeover하지 않는다. 신규62시험은 bootstrap26·의미 RED18·최초 GREEN18로 구별했다. 별도 Astra/xhigh의 독립 리뷰는 관련 KST268passed/3.97초·UTC268passed/3.93초와 추가 probe10개(KST0.45초·UTC0.42초), 격리0·동결 해시 일치를 확인하여 **lease 두 파일 한정 APPROVED, P0/P1/P2 0**이다. 리뷰 보고서 hash `a721ebedbc04fb01b3cc463922ef495cfddece275ec6ff351d7f534eefebd274`.

종료의 최초15시험은 의미 RED7·최초 GREEN8이었다. 독립 리뷰가 추가 **P2 D1**을 재현했다: 결과 task 이전의 prepare/quote/정책/claim에서 commit·게시가 실패해도 shutdown이 정상 반환했다(8RED·16대조GREEN). 최종 drain 후 owner 건강성·저장/게시/engine version 일치를 검사하도록 고쳤다. scope finally에서 순간의 unhealthy를 영구화하면 다른 정상 commit을 오인하므로 그런 방식은 쓰지 않았다. 신규8 RED 회귀와 정상 동시 commit 대조2개를 더해 종료25시험이다. 수정 후 관련 KST279passed/27.75초·격리0. 새 독립 Astra/xhigh는 원본24개(8RED→GREEN+16대조), 종료25개, 관련UTC249개 및 독립 추가16개를 통과해 **D1 해소·종료 경계 한정 승인, 신규 P0/P1/P2 0**으로 판정했다. 실제 정상 busy의 유효 요청 거부, lookup/commit 취소, 실제 restore 뒤 회복, engine 게시 version 불일치 및 결과 실패의 지속을 확인했다. 재리뷰 보고서 hash `1376a4bf9fb4bc16bb57d1722d09d8415a8f193b8a5bce9f3a58e0ada2a441a7`. 원본 실패 보고서와 probe는 보존했다.

See: 동결 source/test patch(기준bef213c) SHA256 `9385094f719ac57ce025eaecdda41c984d74b89b8cfda9952afbba263c9b79dc`. 전체 KST **3381passed/2 known xfailed/4 warnings,138.78초**, UTC **3381passed/2 known xfailed/4 warnings,143.26초**, 각각 격리0·문법·비밀정보 패턴 검사 통과다. 신규87개(lease62·종료25)이며4warning은 기존pykrx1과 의도적인 fork 경계 시험의 Python3.12 경고3이다. warning을 숨기거나 전부 기존 경고라고 쓰지 않는다. lease `e79ea390ae7beed978fb8140a030cc402802af99050ce68b21826d329da74bfa`, runtime `7dd0a5ef0756b3bd8a173bc9d6c7fbe968ba13ca51153ee29f0de33aa97252ec`, commands `1ab94a2f217a991f076edad9681d53cfceafafd4b5b4dffb7811cbbba2d3bd1b`. 이는 설치 factory나 실제 engine/scheduler shutdown 전체 인수가 아니다. 다음은10A2 실제 원관측/정책 publisher이며 main/운영/실API/주문/설정/Toss 변경은 없다.

### 후속 Task10A2a/b — 원관측·완료 증거·순수 위험 전이 (실제 writer 연결 전)

Plan: KRX/NXT 46필드 원 체결 시각을 수신 시각과 분리하고 같은 owner commit에서 보호 결과와 진입 가격 증거를 완료한다. 현재 정책 산술과 실제 writer의 시도 순서를 분리한다. 구현은 feed/순수 위험 전이 Terra/high, owner·교차 인수는 부모, 독립 검토는 Astra/xhigh로 분담했다. 전체 단계3 완료나 운영 설치를 뜻하지 않는다.

Do — feed: frozen `MarketObservation`에 원 TR·거래소·날짜/시간·수신 시각·연결/프레임/행 식별·원문 digest를 보존한다. 실제 WS feed는 전체 frame을 검증한 뒤 발행한다. 기존 US/naive event heap과 legacy 숫자/부호 의미는 유지했다. 독립 검토의 P2 두 건(Decimal 문맥에 따른 음수 반올림, malformed underscore 수치의 Decimal 허용)을 원본 6 RED로 재현하고 exact 부호 전환 및 기존 float lexical 경계로 수정했다. 최종 feed49 + 원본28 + 추가29 = 각 TZ106개 통과, 두 파일 수정 포함 5파일 한정 재리뷰 승인이다. 다중 record parser의 원자 검증은 downstream 여러 종목 commit의 원자성이나 전 이력 exactly-once 보장이 아니다.

Do — owner: 실제 Event/원 DTO 일치를 첫 await 전에 확인하고 durable 접수→보호/고점·원장 outbox·entry quote·완료 증거를 연결했다. 접수 중/미완/실패는 SUBMIT prepare·claim·최종 검사에 공통 장벽이며 CANCEL alpha 정책을 바꾸지 않는다. 같은 현재 원관측의 재전달은 같은 결과를 돌려주되 과거 전체 ID의 중복 방지로 과장하지 않는다. bare 보호 가격은 이전 진입 증거를 무효화한다.

독립 리뷰의 **P1 F1**은 cold restore에서 수락 가격이 사라져 일일 PnL -200000이0으로 바뀌고 fake POST1이 허용되는 문제였다. `quote_price_views`를 admission과 같은 commit에 저장하고 view·체결·일자 valuation의 기존 순서를 보존했다. **P1 F2**는 원관측 low와 다른 supplemental low가 기본 FIRST 단계 복합 청산을 없애는 문제였다. 원 OHLC 등 수치 충돌을 접수 전에 거부하고 low는 원 DTO 값으로 채운다. MA5/prev_low는 별도 지표로 남기며 그 출처까지 입증됐다고 주장하지 않는다. 신규30회귀는 최초13 RED/1대조와 수정 후16추가 대조로 구분했다. 기존 day 시험3개는 가격 영속화 계약에 맞춰 기대값만 변경했고 pending·startup 장벽은 유지했다. 원본9 재현은 두 TZ에서 GREEN이다.

재리뷰 **P2 F3**는 bound10000 완료→bare10100 완료 뒤 invalidation 한 필드가 소실된 합성 checkpoint를 healthy로 복원하고 오래된 source로 fake POST1을 허용하는 관계 검증 누락이었다. 정상 생성 경로에서의 손실이나 악의적인 DB 전체 위조 방어를 주장하지 않는다. current 판정은 최신 durable view와 proof admission의 정확한 일치를 요구하고, restore는 정상 newer pending과 완료 후 무효화 증거를 구분하도록 수정했다. author3회귀는2 RED/정상1 GREEN→수정 후 관련157pass, 동결 재리뷰 원본20은20pass다. 독립 최종 판정과 전체 동결 검증은 아래에 별도 기록한다.

Do — 위험 전이: 실제 persist=False ExitManager clone과 공통 classifier를 이용해 이전 정책 상태/보호 DTO→후보 상태/보호 DTO/선제 stale 필요 여부를 계산한다. 임계값·core/면제·same-level no-op·회복5분 cooldown을 보존했다. author36 및 독립58 시험은 두 TZ 각각94개 통과, **두 파일 한정 승인**이다. 숫자 문자열 정규화, 같은 단계/결측의 보호 DTO 검증 생략, 실제 ExitManager의 logger 호출을 명시한다. owner는 별도 checkpoint 검증을 해야 하며 ‘파일/network 직접 접근 없음’이 모든 log sink I/O 없음은 아니다. 반환 dict는 독립 복사본이지 권한/불변 capability가 아니다.

실제 `BatchAnalyzer.update_intraday_state`의 겹친 호출은 여전히 `(batch normal, ExitManager crash)`가 되는 **의미 RED1**로 남았다(순차 대조1 GREEN, 두 TZ). 순수 후보만으로 이 실제 writer가 수정됐다고 세지 않는다. 현재 5분·정오는 같은 루프이므로 항상 겹친다고 주장하지도 않는다. 다음은 I/O 전 durable begin·최신 시도 완료·의존 version·정책/보호/effect outbox 동일 commit, 실제 5분/정오/2분과 callback/factory 연결이다.

검증 주의: 개발 중 bare pytest의 scripts 수집/변경 중 소스 오류 및 probe와 tests 동시 수집의 conftest 중복 오류는 합격 근거에서 제외했다. guard 위반은0이며 이후 명시 tests 또는 standalone probe로만 실행했다. 실제 최초 인계·취소 체인, REST 시장 시각, 전체 C/F/G/R, 장기 저장/처리량은 계속 미입증/미완이다. `trading_ready=False`, 모든 MODIFY 미지원, KIS 거래·잔고/Toss 관측 전용, main·운영·실API·주문·설정 무변경을 유지한다.

See: 최종 source/test patch(기준a1003ed) SHA256 `5a32c0bba11c0ab2b263ad836d7f43d151c7335cbc33a08ac127c5a345294e99`. 전체 KST **3544passed/2 known xfailed/4 warnings,148.18초**, UTC **3544passed/2 known xfailed/4 warnings,155.18초**로 격리0·문법·비밀패턴 검사를 통과했다. 신규163개는 feed49·owner45+33·순수 위험36이며 warning은 기존pykrx1+의도적 fork 경계3이다. F3 전 후보3541 KST 통과는 최종 지문 근거로 쓰지 않는다.

독립 owner 최종 리뷰는 원본9·후속20·추가17을 각 TZ46개, 관련UTC328개 통과로 F1/F2/F3 해결·신규 P0/P1/P2 0을 확인해 **6파일 한정 승인**했다. 정상 pending bare/bound 복원, generation/가격/시각/status 손상 거부, 새 정상 bound 이후 actual fake POST1 대조를 포함한다. 승인 보고서 SHA256은 feed `ed456844f77a1f3b601c1059ac26c4c0edb5d9111db332b12a0bc59493c48d83`, owner `db203e80d6ce01a6b43e7e9d009d23d7f7fd7de334dc44bb80e69691158f9d00`, 순수 위험 `5b34734271c88fee069621023511ca6fe086b96d20a7cd8200d77f19f9f617be`다. 각 원본 실패 보고서/probe는 덮어쓰지 않았다. 이 승인은 사용자 단계5의 실제 전체 broad 리뷰를 대체하지 않는다.

### 후속 Task10A2b4/5 — 위험 관측 수명·실제 5분 호출점 (구간별 한정 승인, 운영 미설치)

Plan: 실제 KIS 지수 조회의 원 필드 결측과 receipt를 보존하고, I/O 전 durable begin부터 같은 runtime이 추적한다. 5분 정책과 정오 캡·2분 추세를 혼합하지 않는다. Astra/high는 source 수명, Terra/high는 선제 청산 선택·입력 정규화, 부모는 producer/실제 scheduler·owner 연결, Astra/xhigh는 별도 리뷰를 맡았다.

Do — source: `risk_sources`는 시도별 source lane/latest·명시 의존 version·완료 상태와 충돌을 저장한다. 조회 대기 중의 pending, 최신 실패/결측/취소는 이전 정상 승인을 가린다. 최신 유효 완료에서만 현재 owner DTO를 동기 reducer에 전달하며 상태와 결과를 한 commit으로 저장한다. 종료 도중 이미 수락한 외부 조회의 결과도 기존 scope token으로 drain한다. 시장 as_of가 없는 성공은 진입 unknown이다. author46·독립37 및 관련100을 각 TZ 직접 확인한 한정 승인 보고서 SHA `f9bd0e514dc8a020ff29657a1dd36d4c155888a758dcbd89f9ccc8bb3d20395b`를 보존했다. 이 source 모듈 승인 당시 actual publisher hook은 없었으므로 아래 실제 연결 리뷰와 구분한다.

Do — 입력: 실제 KIS 지수 숫자·반올림·10초 TTL·GET/limiter 수는 유지한다. metadata에는 raw 필드 valid/missing/invalid, 응답 UUID·원 receipt를 추가하되 시장 시각은 None이다. deepcopy로 반환값 변경이 cache 원관측을 바꾸지 못하게 했다(별도 의미 RED1). 이후 실제 병렬 조회 RED2(늦은 옛 정상의 최신 급락 cache 덮기·최신 실패 뒤 cache 재생성)를 per-index latest-begun cache-write guard로 수정했다. caller는 늦은 원 응답을 그대로 받으므로 owner의 시도 검사도 필요하다. 원 producer 한정 승인 보고서 SHA `2da8e59d883c9267eb5ba53736c8d6596798ed1b8f5c36bbd290662fc7b9b069`와 추가 cache/input 원본 리뷰 SHA `9f72082c8ac83340eef4bab9b564627a7e5f78c08db7583bc0621188be4a9df1`를 구분한다.

입력 정규화 독립 **P2 F1**은 collection status·극단 정수·timezone 변환 underflow가 missing 대신 예외로 빠지는 한 결함의 세 경계다. 원본13 RED/21대조를 보존하고 exact type·유한 수치 변환·좁은 시각 변환 방어로 수정했다. 별도 재리뷰는 작성자42·원본34·추가38을 각 TZ **114passed/격리0**로 직접 확인해 두 파일 한정 승인했다. 보고서 SHA `2ffc4e097921f928692011c1d5b2b8a113b22b503f1ae92bf7df508bd81d755b`. caller 시계/날짜 인자 오류와 임의 programming RuntimeError는 그대로 전파되며 blanket catch로 숨기지 않는다. 원 metadata가 missing인 outer0은 normal로 승인하지 않는다.

Do — 선택: 실제 Portfolio/ExitManager를 복호화한 선제 청산 selector는 core·면제 제외, 5영업일 이상·미실현1% 미만을 보존한다. 독립 **P2 F1/F2**는 저장 DTO 가격으로 PnL을 판단하면서 다른 최신 가격을 제안하던 불일치와 비유한/음수 Decimal 수용이었다. 원본11 RED/18대조, author9 RED/8대조 후 제공된 시세를 cloned Position에도 반영하고 입력을 검증했다. 최종 author17+원본29를 두 TZ 재확인한 두 파일 승인 SHA `a0672c20808def6a22f561fea8a26538bac02f5cd527d36e171b7b1108ec4b8d`. 이 helper는 주문/출처 권한을 만들지 않는다.

Do — 실제 5분: `run_batch_scheduler`→`_refresh_intraday_risk`→실제 KIS adapter(fake HTTP)→owner 완료 경로를 추가했다. 명시 설치 시에만 사용하며, 원래 운영 factory에는 설치하지 않았다. 증명된 최초 인계 대신 normal0 기준선을 자동 생성하지 않고 미존재 시 설치를 차단한다. 최신 유효 입력으로 현재 보호·현재 시세 view를 사용해 정책/선제 후보 outbox/완료 증거를 함께 저장하고, 전체 DTO 검증 뒤 batch와 adapter의 intraday mirror를 게시한다. 기존 임계값/5분 회복과 당일 보호 정책은 유지한다. 같은 원 응답의 cache 재조회는 분류 시각을 새로 찍지 않는다. 회복해도 이미 저장된 pending 청산 후보를 지우지 않는다.

실제 raw batch mutation/선제 emit 우회2건, aware cooldown의 기존 소비자 비교3건, UTC 호스트에서 실제 loop의 GET0이 된1건을 의미 RED→GREEN으로 고정했다. 신규 API 부재5건은 bootstrap이고 실제 기존 결함5건이라고 세지 않는다. 잘못된 test helper 메서드명1건은 시험 작성 실수로 분리했다. installed loop는 명시 runtime KST 시계, cooldown은 aware 시계로 비교하며 uninstalled legacy 동작은 유지한다. 실제 보호/outbox 전달은 **저장까지만** 연결됐으며 정상 Signal→gateway 송신 이행 완료가 아니다. 원본 uninstalled 겹침 RED도 보존한다.

See: source/test patch(기준552fb25, 이번14파일 전체) SHA `997ef674b03fc8989d23818425fb316bf737e0286709686d38be70c51a99c445`. KST 전체 **3702passed/2 known xfailed/4 warnings,130.71초**, UTC **3702/2/4,125.77초**·각각 격리0, tracked 전체 Python 문법/비밀패턴 검사 통과다. 신규158개는 source46·producer35·입력42·선택17·실제5분18이다. 최초 중간 전체는 env 격리에서 HOME을 제거한 탓에 배포 *fake 시험*1건이 실패(3687pass)했다. 시험용 SSH-key 경로만 명시한 clean env로 바로잡았으며 제품 수정/운영 SSH로 해결하지 않았다. 4 warnings는 기존pykrx1+의도적 fork3이다.

실제5분 다섯 파일의 별도 독립 리뷰는 새 P0/P1/P2 없이 **한정 승인**했다. 리뷰어가 관련210시험(UTC10.80초/KST13.02초)과 독립27시험(UTC3.64초/KST3.51초)을 직접 실행했고 각각 격리0이다. 실제 HTTP 응답 대기 중 pending, 역순 정상/급락/결측/실패, 동시 체결·시세, 새 store/runtime/engine/ExitManager 복원, SQL/게시 실패, 손상 crosslink, 늦은 cache, 달력·과거 진입일 변화, 실제 adapter bull cap을 확인했다. 보고서 SHA `599416ed16fd1acdef416d107b1443786523039194ef927a2cb2fc0843a0a5c9`, 독립 probe SHA `60426fc5f68b66fbc25046df82392d81220aa20517fae60254bd4e5b9afbc91c`. 리뷰 수치를 전체3702에 합산하지 않는다. 이 리뷰가 닫지 않은 입력 F1은 위 별도 입력 재리뷰에서 해결됐고, 정책 재생·effect 전달·나머지 writer 및 운영 승인은 이 한정 승인에 포함되지 않는다.

후속 미완을 별도 RED로 확인했다. 실제 core 등록실패→5분 caution/crash/severe→보호 repair는 현재 정책 이력이 기존 fill/quote 재생에 연결되지 않아 `current_protection_evidence_mismatch`로 **3건 BLOCKED**다(UTC1.19초·격리0). 원본 probe SHA `8b5cd60e3a0da6ddb219c77c67f45a490543f5274a534bb7df608744d40d6b24`. 이는 위험하게 추정 복구하지 않는 차단이며 다음 정책 입력 기록·재생 작업 대상이다. 5분 연결의 한정 리뷰를 복구까지 전체 완료로 확대하지 않는다.

정오/2분/LLM·정책 재생·WS callback·실제 effect/gateway·수동/KOFR·sync/day·factory, 전체 C/F/G/R와 장기 성능은 아직 미완이다. REST 시장 시각/최초 인계/취소 체인 미입증, 모든 MODIFY 미지원과 `trading_ready=False`를 유지한다. main/운영/실API/주문/설정/Toss 변경 없음.

### 후속 Task10A2b6 — 실제 5분 정책의 보호 재생 (한정 재리뷰 승인)

Plan: 등록 실패로 degraded가 된 실제 포지션에 5분 정책 전이가 생겼을 때, 원 source의 시도/완료 version·digest와 원 보호 scope를 같은 owner commit에 기록한다. 복구는 당시 순서로 실제 보호 전이를 재생하며 경제·예약·초기 R·outbox를 재적용하지 않는다. 구현 Astra/high, actual hook·실제 scheduler/SQLite 인수는 부모, 독립 검토는 별도 Astra/xhigh로 분리했다.

Do: typed `IntradayPolicyReplayInput`과 `capture_intraday_transition`을 실제 completion reducer에 연결했다. 테스트 전용 wrapper가 hook을 대신 설치하지 않는다. 원본 actual degraded→5분→repair 3건은 연결 후 KST/UTC 모두 통과했고, 실제 scheduler·warm/cold restore·SQL 실패·과거 quote 순서를 포함한 관련109개도 양 TZ에서 통과했다(UTC13.83초/KST14.07초). 정상 source의 시장 시각 결측과 거래 시작 차단은 그대로다.

독립 검토에서 이력 완전성 P2가 추가 재현됐다. 원 accepted source/정책 root는 그대로인데 policy event를 삭제하거나 quote로 바꾸고 연결 hash를 다시 계산한 합성 손상 checkpoint가 repair를 허용했다. crash→normal 전이를 함께 누락하면 최종 글로벌 정책도 맞아 단순 최종 대조로는 과거 손절 누락을 찾을 수 없었다. 독립 세션은 최종 보고서 작성 전 실패로 종료돼 **완성된 승인 보고서로 세지 않는다**. 남은 원본14시험(probe SHA `5a0c05edfac947e58205de83dbddb026fc30b08b1e56d674b0c54c6a1bf3784e`)을 부모가 그대로 실행해 UTC8실패/6통과7.15초·KST8실패/6통과7.18초·격리0으로 확인했고, 별도 영구 회귀8개도 전부 의미 RED였다. 삭제·변환된 보호 이력에 관한 로컬 복구 정합성 시험이며 실제 운영 원장 손상 사고를 관측했다는 주장은 아니다.

Do — 보완: 모든 event의 글로벌 정책 전후 일치와 최신 정상 상태 이후 원 accepted level-change의 정확한 순서·누락/중복을 검증한다. 추가 의미 RED2는 재생 fill 버전을 옮겨 검사 범위를 줄이는 경우와 policy를 정상 상태 quote로 바꾸는 경우였다. 이제 시작점은 실제 fill만 허용하고, fill 적용 버전을 독립 경제 outbox와 대조한다. 이전 정상 lifecycle의 전이·same-level/duplicate/missing/older는 잘못 추가 요구하지 않으며, 실제 dispatcher ACK 후에도 원 payload/버전을 유지해 정상 복구한다. 전체 checkpoint의 모든 독립 root를 함께 바꾼 경우까지 인증한다는 주장은 아니다.

중간 See: 작성자가 관련117개(41+11+8+18+기존39)를 UTC17.50초/KST19.22초, 불변 독립 원본14개를 UTC3.88초/KST3.98초, 각각 별도 process로 통과·격리0을 확인했다. 작성자 보완 보고서 SHA `4e5e871fc0fc479ca899301f9e3d5c8b8dce417f542b0e5f40dc7d82e5c86fb3`. 이 중간 결과 후에도 다음 독립 P1이 발견됐으므로 당시 통과를 최종 승인으로 쓰지 않는다.

후속 독립 **P1 F1**: 실제 후속 fill의 ID/버전이 맞아도 그 `before.protection`만 정상으로 바꾸면 latest anchor가 과거 미기록 손절을 건너뛰었다. 실제40주 degraded→crash→9700quote→normal→누적100주 degraded 뒤 이력만 바꾼 warm/cold 두 경우가 RED였다. 기존135+원본14 GREEN은 이 결함을 해결하지 않는다. 원본 보고서 SHA `952c6adfdc28a65f53381317df30030726723c133aeceb56c6f8bf954b82c598`, 새 불변 probe SHA `159dc205382391762acf0d714d29b94adb69ed39b722c9033bb5aba412390354`를 보존했다.

부모 보완: `capture_fill`의 원 최초 commit에서 새 경제 outbox에 정확한 replay event digest를 결합하고, 복구 시작점을 고르기 전에 모든 fill 이력을 독립 outbox와 대조한다. 연결은 journal 원 payload에 포함된다. repair/restore/중복 체결/ACK 때 새로 만들지 않으며, 링크가 없는 과거 checkpoint를 소급 보정하지 않고 BLOCKED로 남긴다. 실제 repair 후 새 degradation은 정상 대조로 유지해 전체 이력의 무조건 연속성을 강요하지 않는다. 새 영구5시험은 연결 필드 부재 bootstrap1·실제 위조2·미입증 링크 요구1 RED, 정상 재복구1 최초 GREEN이었다. 수정 후 부모 관련122개 UTC18.41초/KST19.12초·불변 원본14개 UTC4.27초·F1원본4개 UTC2.78초 통과·격리0이다.

추가 경계10시험은 별도 Astra/high가 ACK/중복/콜드 복원·누락/비정규 링크·SQL commit 전후 실패를 고정했다. 수정 소스가 먼저 반영돼 최초 제품 RED는 관측하지 못했으며 fixture8·예외 wrapper2 작성 오류를 제품 RED로 세지 않는다. 자체10+부모5를 UTC3.58초/KST2.67초 통과·격리0, 보고서 SHA `dff5b4cd2448b2b74a5420ccc25f6f35579be2b5a7dad4779a945b8acf7e01af`.

최종 See: 별도 Astra/xhigh가 named140·경계10·불변 원본14·P1 원본4·추가 whole-event7을 **KST/UTC 각각175passed, 격리0**으로 직접 검증해 여덟 파일/실제5분 degraded 보호 재생 범위만 승인했다. 후속 보고서 SHA `8ec487a0299ed6984131f021d9fcb0f2e673e14b7ed87320484ae7df07720cf5`, 별도 추가 probe SHA `fac6ca4b3b37781e2699cb224cf40d61737eee5e3096575c6b429e4c09c49ef9`. 원본 보고서/실패 probe는 덮어쓰지 않았다. 보호와 경제의 독립 root 모두를 조작하는 경우의 인증·과거 증거 소급 생성·전체 writer/운영 승인은 범위 밖이다.

### 후속 Task10B1 — 실제 수량 계산의 순수 단계 분리 (한정 재리뷰 승인)

Plan: 기존 함수의 숫자·조기 반환·외부 의존 호출 순서를 보존한 상태에서 현재 owned 자원으로 재계산할 산술부만 분리한다. Terra/high는 특성화17·kernel9, 부모는 실제 wrapper 연결, 별도 Astra/xhigh는 기준 `7cafd74` 함수 AST와 새 함수를 직접 비교했다. 초기 특성화 GREEN과 새 API 부재 bootstrap RED를 제품 결함으로 세지 않는다.

Do: nominal/risk·core/hybrid pool·기존 배율/예외 순서·전략 잔여/최소금액/3주/1.3 affordability/수수료 포함 위험 상한을 pure phases로 옮겼다. metadata/실제 resolver·calendar/volatility/conviction 읽기는 wrapper에 남겼다. nominal이 쓰지 않는 risk 설정을 새로 요구하던 연결 회귀1건을 수정했다. 독립 비교275개와 정상139주·R/수수료 순서1개는 GREEN이었지만, 최소금액/최소수량 거부 전에 수수료 설정을 읽는 P2 F1 두 사례가 RED였다. 실제 getter는 현재 메모리 조회이며, 실패를 주입해 드러낸 조기 반환 계약 회귀이지 운영 수수료 장애를 관측한 것은 아니다.

F1은 `pre_fee_quantity`와 명시 수수료 입력의 `apply_fee_risk_cap`으로 분리해 수정했다. 거부될 수량은 수수료를 읽지 않으며 최소금액 로그의 순서도 유지한다. 원본 보고서 SHA `420c5085a882cae52c619a69373f3c3b7f67d0b61a3c7f36c88e712e66369e2a`, 원본 probe SHA `3a345d931278e1a62442f18e28c44a7c8d3effa41aedb33cd13aabaa161b5a53`는 보존했다. 부모가 원본278개를 UTC1.18초/KST1.23초, 영구 집중35개를 UTC0.59초/KST0.64초 각각 통과·격리0으로 재확인했다. probe와 tests를 한 process로 모은 첫 명령은 conftest 이중 등록으로 수집 오류1건이었으며, guard를 끄지 않고 별도 process로 바로잡았다.

See: 별도 독립 재리뷰는 집중35·불변 원본278·추가19를 KST/UTC 각각332개 통과·격리0으로 직접 확인해 **다섯 파일 한정 승인**했다. 보고서 SHA `224840646fd5e745a03322b792b85aefdb0311763e8df5e8128fcf843d46884e`, 추가 probe SHA `08783314a6bf5a8aa45a7a287e9adb899a56c56d343249d7322561a00049cf7c`. 최종 전체 검증은 별도 기록한다. 계산 결과는 주문 허가가 아니다. qualification/source version·최종 현재 자원 예약·실제 SIGNAL→ORDER gateway(B2/B3)는 아직 이행하지 않았다.

**P1 F1 수정 전 후보**의 동결 source/test11파일 patch(기준 `7cafd74`) SHA `e0286133a163368759cb4070cb8baa486eb691a14a951b2e4159b25969966e89`. 부모 전체 KST **3815passed/2 known xfailed/4 warnings,178.47초**, UTC **3815/2/4,179.67초**, 각각 격리0이다. 당시 신규113개는 재생41·실제 연결11·무결성8·완전성18·수량35이며 ignored 독립 probe를 더하지 않는다. 당시 문법/비밀패턴 검사도 통과했지만 이 결과를 P1 수정본의 검증으로 사용하지 않는다. 경고는 기존pykrx1+의도적 fork3이다. 전체 통과만으로 두 범위 승인이나 단계3/5 완료를 선언하지 않는다.

최종 P1 수정 후보의 source/test13파일 patch(기준 `7cafd74`) SHA `3e7014010a30c25d7efcd016ba83ad729b016f97eb7461f43e6fb8102206c58c`. 부모가 수정/경계시험 동결 뒤 전체 **KST/UTC 각각3830passed/2 known xfailed/4 warnings(190.32~190.35초)**, 격리0을 새로 확인했다. 신규128개는 앞선113+anchor5+경계10이며 ignored 독립 probe를 합산하지 않는다. 새 파일까지 stage한 뒤 Python 문법/비밀패턴 검사와 `git diff --check`도 통과했다. 두 범위의 한정 승인이며 단계3 전체/단계5 broad/운영 승인은 아니다.

### Task10A2c — 계산 보존·입력 증거 기반 (09-19, 실제 writer 연결 전)

Plan: 2분 추세·정오 JSON LLM·장전 text LLM을 서로 다른 producer로 유지한다. 2분 루프에 기존에 없던 ExitManager 적용을 추가하지 않는다. 기존 수치/조건부 시계·VIX/전문가 조회 순서는 보존하고, 실제 호출점의 결측/owner 우회를 별도 RED로 고정한다. source별 최신 시도와 입력 증거는 전역 execution version 비교와 구분한다.

Do — 계산/입력:

- Terra/high가 sidecar 추세·중기 pending/VIX·전문가 pending의 순수 계산을 추출하고 실제 legacy 메서드 세 곳을 연결했다. 부모 대조에서 발견한 09:00–10:00 neutral의 pending 유실 회귀는 실제 RED 후 수정했다. 기존 미설치 경로의 한쪽 지수 0 fallback은 특성화 대상으로 보존하며 owned 입력의 승인 규칙으로 쓰지 않는다.
- 부모가 두 지수의 원 metadata·OHLC/등락률·응답 ID·receipt를 검증하는 frozen 입력을 구현했다. 결측/비유한/불일치/미래·전일 receipt는 missing이고, 원 등락률 0은 valid면 유지한다. 시장 시각은 None이며 TTL·추가 조회·가격 clipping은 넣지 않았다. 신규 API 부재51 RED와 DTO 직접 생성의 가변/충돌 허용5 RED를 구분하고 최종 관련133개를 양 TZ로 확인했다.
- 별도 Astra/xhigh가 동결7파일을 한정 승인했다. 직접 관련216·새 독립532·원본 특성화5, **각 TZ753개·격리0**. 독립532 중425는 `4f8f098` 실제 메서드 AST와 정확 float/속성 유무/시계·공급자 호출·로그·예외의 차등 대조이며107은 원자료/receipt/DTO 경계다. 실제 writer/source authority·source seal·전체 인수 승인으로 확대하지 않는다. 독립 probe SHA `f7dc5368dc7e9d27b59c22c15396bb197a6cf45e0a941a87d5a70f89198396a0`.

Do — input seal 기반:

- Astra/high가 기존 source begin 요청에 opt-in `require_seal`을 내구적으로 결합하고 owner가 읽은 lane 상태/원 accepted 보존 사실/정책 값과 detached 입력을 고정했다. 새 VIX/전문가/장전 diagnosis lane은 독립이며 기존 noon/intraday mapping은 유지한다. 다른 facade가 required를 제거하거나 완료 hook이 seal root를 바꾸지 못한다. 완료/복원에서 source·seal 충돌과 역사적 admission/terminal 시점을 대조한다.
- 반복 seal은 멱등, 다른 본문은 원본을 보존한 conflict다. success 전에 관련 read가 변하면 hook 전 stale, missing/failed/cancelled는 seal 없이도 종료 가능하다. fill/ACK 같은 무관한 commit은 판단을 굶기지 않는다. 전일 VIX 유지도 원 날짜와 현재 실패 lane을 함께 보존하며 새 성공/신선도로 바꾸지 않는다.
- 작성자 직접 시험은 신규41+기존116 = **157개 UTC18.32초/KST18.50초·격리0**. 새 구현의 conflict 의존 누락·hook 증거 삭제·잘못된 root/version을 실제 RED로 고정했다. API 부재/receipt 필드 부재와 fixture 작성 오류는 기존 제품 결함 재현으로 세지 않는다. 독립 seal 리뷰와 부모 전체 검증은 아래 최종 판정에 별도로 기록한다.
- **미해결 한계:** 정책 selector는 값/존재/digest만 비교하여 A→B→A를 놓친다(실제 owner 대조). `updated_at`은 generation이 아니다. 실제 caller를 연결하기 전 정책별 owner generation과 값의 동시 고정이 필요하다. 이 기반의 성공 receipt만으로 version-safe 소비 권한이나 거래 허가를 만들지 않는다. trusted caller의 입력 선택 완전성과 임의 동시 checkpoint 변조 인증도 제공하지 않는다.
- seal의 `source_lanes`는 **완료 전** 명시 read 비교다. 완료 뒤에도 계속 추적하는 기존 ticket의 hard `dependencies`와 같지 않으며, 후속 source 변경/첫-await pending을 최종 소비자에게 전파하는 권한까지 제공하지 않는다. 실제 caller 연결은 정책 generation뿐 아니라 이 지속 무효화/현재 snapshot 경계도 인수해야 한다.

Do — 실제 경로의 다음 RED:

부모가 실제 `run_market_trend_monitor` 1회 반복→KIS adapter→sidecar→SQLite runtime→owned 진입 snapshot을 실행했다. 정상 양지수에서 live sidecar=False/owned=True 불일치, 고가 결측과 KOSDAQ 실패에도 live 회복/하트비트 성공을 재현했다. GET/limiter 각각2회, owner version 변화0, SQL와 owner는 동일, `trading_ready=False`다. 원본 `task-10a2c-trend-writer-probe.py` SHA `4d3c9bb80ffc60ae42ca671705b28aef9327e42162d91f280108724550572e07`, UTC3 RED/0.94초·KST3 RED/3.16초·격리0. 정상 흐름을 owner로 옮길 때 별도 인수로 닫으며 기존 정오/LLM RED3도 미해결이다. ignored 실패 probe는 passing suite에 숨겨 합산하거나 xfail로 완료 처리하지 않는다.

See: 계산/입력7파일과 input seal 기반3파일은 각각 독립 한정 승인됐다. seal 리뷰어는 관련87+새19개를 양 TZ로 직접 통과·격리0으로 확인했고, 위 완료 후 지속 권한 한계도 hard dependency 유/무의 실제 pending→commit→cold restore로 대조했다. 계산 리뷰 보고서 SHA `09aebdc881e2b2999f352dea3172656c1040f798df1496159d299fc3cd75c1b6`, seal 보고서 SHA `704f050abf9c343315992072873dfe69ebb92512c7b11286956a12834df1221a`, seal probe SHA `b3e178b162e1ac2a84e6a63c9304bf8a66fcc1d9dae2d3a8a147601358bac6c4`다. 전체 C2/단계3·broad 승인과 구분한다.

부모 전체 검증은 **KST3933passed/2 known xfailed/4 warnings(193.81초), UTC3933/2/4(193.42초)**·격리0이다. 신규103개(계산/legacy parity6·지수56·seal41)이며 ignored 독립/RED는 합산하지 않는다. source/test10파일 patch(기준 `4f8f098`) SHA `3b47ab86a676f396cd71f1f57b9561a120afb0ad47d29e6924ce9752d4b3e1a3`, 새파일 stage 후 문법/비밀패턴·diff check도 통과했다. 경고는 기존pykrx1+fork3이다. 2분 RED 원본의 주석 정리 후 UTC 재실행도3 RED/1.11초·격리0이며 위 probe hash와 동일하다. 하위 에이전트 한도 오류는 미실행으로 구분했고 제한 해제 시각 이후 재개했다. 다음은 정책별 변경 이력/지속 소비 권한→실제2분 owner→정오/LLM 보호 replay다. main/운영/실API/주문/설정/Toss 변경은 없다.

위 구현·검증·한정 리뷰 결과는 feature 커밋 `bfeb4a6`으로 저장·push했고, 원격 branch의 전체 SHA 일치를 확인했다. main/운영 반영은 아니다. 교훈 문서 후속은 소스/시험 변경 없이 정책 ABA와 지속 소비 권한의 구별만 기록한다.

### Task10A2c/C2a — 정책별 변경 이력 (09-20, 시험 완료·독립 리뷰 보류)

최신 후속: 사용자는 Astra 대체가 아니라 Opus 실패 원인의 진단/수정을 우선 지시했다. 동일 원 입력·Opus/xhigh는 계측 재실행에서692.32초에 정상 완료했다. 기존360초 예산/진행 정보 소실/래퍼 exit0 문제와 수정은 [실행기 보고서](opus-review-runner-2026-09-20.md)를 따른다. **Opus 판정은 CHANGES_REQUIRED**이며 B1 등 지적은 아직 실제 재현·범위 판정 전이다. 원문은 SDD `task-10a2c-generation-opus-review-completed-20260920.md`(SHA `d75fd2ff0eb4706f0cfad8be1a65f39528cd903aaf8203b533d0fedf8b1a2bb8`)에 보존했다. 아래 timeout/대체 선택 대기는 진단 이전 이력이며 지금의 블로커는 리뷰 미도착이 아니라 지적 검토/해소다. 엔진 후보 코드/시험7파일 지문은 그대로다.

Plan: 사용자가 전역 모델 규칙 설치·확인 후 기존 순서를 재개하도록 승인했다. 전역 `ai-routing-v1-2026-09-20`은 Codex/Claude 새 소비자에서 확인했고, 프로젝트 요약은 [공통 배정 규칙](../operations/agent-routing.md)에 기록했다. Astra/high가 commit 경계, Terra/high가 별도 worktree의 source 전파 RED, 별도 읽기 전용 Astra/high가 다음 실제 caller 인터페이스를 담당했다. 실제 모델 ID가 노출되지 않는 native 작업은 배정값을 실측값으로 주장하지 않는다.

Do: 명시 등록한 다섯 정책 selector의 version·presence·value·digest 이력을 중앙 owner finalizer가 SQL 직전에 기록한다. reducer의 이력 root 변경은 거부하며 복원/게시에서 generation을 생성하지 않는다. 새 `versioned_policy_reads`는 legacy 값-only 기록과 구분한 schema2로 봉인하고 seal S/완료 C **엄격히 이전** 이력과 대조한다. own completion hook의 C 변경은 자신을 무효화하지 않는다. 등록 자체는 기준선/시장 신선도/거래 허가가 아니다.

부모가 같은 등록 ID/다른 selector 요청이 기존 성공처럼 반환되는 경계를 지적했고 실제 RED 후 등록 요청 목록/commit version까지 내구적으로 결합했다. 작성자 신규31개, 집중118·확대 관련320개를 UTC/KST 각각 통과·격리0으로 보고했다(서로 포함되는 집합이므로 합산하지 않음). 실제 queue fill·quote·journal ACK의 무관 변화, config/command-effect ABA, intraday 선택 분리, SQL 전후 실패·게시 실패·복원 및 history/seal 변조를 검사했다. sidecar effect 시험은 전체 scheduler/dispatch 인수가 아니며 신규 registry의 day/account 종단·fill 취소 조합은 추가 검증 대상으로 남겼다.

See: 코드/시험6파일 동결 뒤 도구를 끈 Claude Opus/xhigh에 원문·해시·명세를 전달했으나 두 시도 모두360.02초 내 리뷰 결론을 반환하지 않았다. 첫 input202716자/두 번째 핵심 발췌137827자, 두 번째 assistant 메타데이터의 실제 모델은 `claude-opus-5`, toolcalls0·리뷰문/최종 result 없음이다. 래퍼 exit0은 child timeout(-9)을 처리한 결과일 뿐 성공 리뷰가 아니다. 유효 effort/사용량/최종 비용은 관측하지 못했으며, 로그인 오류나 영구 공급자 장애라고 단정하지 않는다. 소스 manifest는 두 시도 전후 동일했다. 세 번째 재시도나 자격/설정 우회는 하지 않았고, 독립 Astra로 검증을 대체할지 사용자에게 요청했다. 승인 전 C2b/actual caller 통합은 보류한다.

부모의 동결 코드 전체 시험은 **UTC3964passed/2 known xfailed/4 warnings143.63초, KST3964/2/4 147.99초**, 격리0이다. Terra/high가 등록 이력을 포함한 실제 day prepare→valuation→roll→resume/cold restore, 계좌 불일치의 stale/새 접수 거부, SQLite 체결 precommit abort, 큐/처리 waiter 취소 후 단회 commit·중복 방지4개를 별도 probe로 UTC1.73초/KST1.52초 통과했다. 원 probe SHA `e12ccba27af1fb60da3ae22d133b6d54e873e3d8e11066c5dea4b833f7e00ac3`, 보고서 SHA `3e8847af6265aadff323466d7ef1ea61e1852d66131a82c09f3674e3a05fa459`. pre-fence versioned seal이 roll을 가로지르는 별도 행렬과 협조된 DB 재작성 인증은 이4개가 증명하지 않는다.

부모는 원 probe를 보존하고4개를 `tests/test_execution_policy_generation_boundaries.py`로 옮겼다. 정규 파일만 UTC1.41초/KST1.05초·격리0 통과, 새 파일 포함 stage 후 Python 문법/비밀패턴·diff 검사 통과. 여기서 verify의 테스트 생략은 별도 전체 실행과 구분한다. source/test7파일 staged patch SHA(기준77c3f5d) `e62f1c53e267dac313bd880f32fc0b6b11b339e60af02b755011486ee785cd81`.

경계4개를 포함한 최종 전체 `tests` 실행은 **UTC3968passed/2 known xfailed/4 warnings(172.36초), KST3968/2/4(172.61초)**, 각각 exit0·운영 상태/외부 네트워크 접근 시도0이다. 이전 전체3964나 별도 probe 수치를 여기에 합산하지 않는다. 깨끗한 환경·pytest 외부 plugin 자동 로딩 OFF·명시적 tests 경로로 실행했다. 경고는 기존 pykrx1/fork3이다. 최종 검사까지 source/test 변경은 없으며, 이 상태는 독립 코드 리뷰 승인이나 전체 단계 완료가 아니다.

C2b 선행 RED: Terra의 별도 동일 base `77c3f5d` worktree에서 완료 후 선언 source 변경·전이 의존·첫-await pending·후속 begin/complete·cold restore를 시험했다. 부모 재실행 **7 behavioral RED/2 GREEN controls, UTC1.93초·격리0**. 실패/부재 사실이 그대로인 optional VIX 및 own hook 정책 변경은 유지해야 하는 대조다. 원 테스트 SHA `227def6e2cea5daefd4fbca22aba4cc6cd092b0c4fc655f45a1473f149563a4c`, 보고서 SHA `9eaa6879a62ba6917d0dc55216a78ffbd0d3cc18a14678ee8ecf3bab26994d00`. 이 원본을 보존하고 C2a 소유권 해제 후 source 현재성 evaluator를 순차 구현한다.

다음 실제 연결의 고정 경계: 2분 보호 적용0, sidecar는 `entry_policy_effects.sidecar_active` 단일 정본, mid/expert pending은 새 RegimeOwner selector, VIX6시간/단일 background task의 기존 실행 순서, noon/5분 horizon은 영구 max가 아니라 관측시각·commit 순서에 따른 단일 projection. 정오/JSON LLM/보호 적용은 typed replay와 같은 commit으로 이행해야 한다. 아직 이 실제 caller/replay를 구현 완료했다고 주장하지 않는다. main/운영·실API·주문·거래 설정·Toss 무변경, `trading_ready=False` 및 모든 MODIFY 미지원 유지.

현재 C2a 후속 상태는 [Opus 수정·인수 원장](policy-generation-remediation-2026-09-20.md)이 정본이다. 위의 최초 후보/timeout 수치는 당시 이력으로 보존한다. B1/B2/B4를 재현 후 수정하고 신규 정규32시험을 포함한 전체 UTC/KST 각각4053passed/기존xfail2·경고4·격리0을 확인했다. 복구된 실행기의 실제 Opus5/xhigh 재리뷰는366.318초에 정상 완료·APPROVE_THIS_SLICE다. 다음은 retained source 요청 경계(N1)와 C2b 지속 소비 권한이며 실제 caller 이행/운영 승인은 아니다.

## 단계4 — 공식 계약 증거

Plan: 공개 KIS 공식 자료에서 현행 TR의 취소/정정 체인 의미와 잔고–체결 cutoff를 확인한다. 최신 TR로 자동 치환하지 않는다.

Do: Astra/high가 기존 고정 revision `b4e6249714418aa57833d1cbbbced39cbcc5b125`의 legacy·Postman·현재 예제·체결 통보를 재확인했다. 실 API/계좌/자격은 사용하지 않았다. [계약 정본](../integrations/kis-execution-contract-2026-09-17.md) 참조.

See: 요청/페이지 계약 외에, 취소/정정 원행과 자식행의 누적 중복 처리·최종수량 정의, 공통 snapshot/cutoff·지연 상한은 확인 자료 범위에서 **미입증**이다. API에 기능이 없다고 단정하지 않는다. 공식 포털 인증 후 상세 명세는 확인하지 못했다. ACK·빈 목록 반복·웹소켓 연결만으로 startup/최종성을 열지 않는다. 자동 최초 인계/미지원 체인 승격은 완료가 아니다.

09-19 후속으로 [부족 증거 인수 목록](../integrations/kis-execution-evidence-request-2026-09-18.md)을 정리했다. 공식 답변에 필요한 질문·승인된 기존 비식별 자료·저장 금지 비밀정보·실제 lifecycle/큐/예약 인수 순서를 명시했다. 새 외부 증거 확보나 시험용 실주문/API 호출은 하지 않았다. 모든 MODIFY 미지원과 최초 인계 시작 차단은 그대로다.

## 단계5 — 마지막 통합 리뷰

09-20 추가 진행: C2a 정책 이력·N1 요청 오류 분리·C2b 현재 source 소비 권한은 각각 독립 한정 승인을 받았다. C2b 최초 Opus I1/I2를 재현/인수 보강 후 수정해307.174초 재리뷰 승인, 최종 전체 UTC/KST 각각4124passed·기존2xfail·경고4·격리0이다. 다음 실제2분·정오·LLM·보호 재생/전체 통합 완료로 확대하지 않는다. [최신 Plan–Do–See 및 근거](source-authority-followup-2026-09-20.md)를 참조한다.

09-20 추가 진행(위4124 이후): 실제2분 owner/caller·schema3 captured reads를 연결하고 등록/취소·typed completion·baseline scope의 독립 지적을 재현 후 보완했다. 최종 UTC4271passed222.71초/KST4271passed206.95초·각 기존xfail2/경고4·격리0, 네이티브/Opus 한정 재리뷰 승인이다. 별도 개발용 시간 시험은 인과적 종료 대조로 정정·독립 승인했다. [실제2분 Plan–Do–See](two-minute-regime-owner-2026-09-20.md)가 이 구간의 정본이다. 다음은 승인 계약의 정오 cap 선행 저장→JSON LLM→실제 보호 적용·typed replay/repair이며, 전체 C/F/G/R·성능·운영 이행은 계속 미완이다.

Plan: 실제 전체 C/F/G/R 시험명을 명세와 대조하고 독립 broad 리뷰·수정·한정 재리뷰·UTC/KST 전체 검증을 수행한다. 이전 모듈 리뷰를 대신 쓰지 않는다.

Do/See: 아직 실행 전이다. 충족/미충족과 운영 전환의 증거 조건을 분리하고, 미충족 상태에서 main/운영 GO를 선언하지 않는다.

### 실제 인수 매핑 (bdda0e9 이후 후속 증거 누적)

09-20 C3 한정 완료: 정오 위험 cap 선행 commit→JSON LLM→full 보호 application/typed replay 및 실제 scheduler 즉시/주기 분기를 같은 owner에 연결했다. 네이티브 첫 리뷰3건·경합/결측/이력 누락과 Opus A의 원시각·명시 결측 문제를 보완했고 네이티브/Opus A 재리뷰·B 모두 한정 승인이다. 신규79 포함 최종 UTC4350passed244.32초/KST4350passed239.88초·각 기존xfail2/경고4·격리0이다. Opus 첫 전체 입력900초 상한은 미승인 이력으로 보존하고 같은 모델·권한·effort의 소입력 대조 후 분할 검토로 완료했다. [C3 Plan–Do–See](noon-regime-protection-replay-2026-09-20.md)에13범주의 실제 확인 범위·미실행 조합·비차단 advisory를 기록했다. 전체5단계 완료가 아니며 C4·qualification/최종 sizing·effect/gateway·나머지 writer/factory·전체 C/F/G/R·공식 증거·전이력 성능은 잔여다.

아래는 부모가 시험 함수와 실제 호출점을 대조한 **미완 범위 목록**이다. 전체 통합 승인이나 독립 broad 리뷰가 아니다. `tests/` 아래 파일명을 사용하며 뒤 단계에서 같은 표를 갱신한다.

09-20 C4 한정 완료: 위 C3 당시 잔여였던 장전 text diagnosis·정책/표시 소비 경계를 구현했다. 실제 caller·KST 날짜·optional 호출·schema 활성 경합·미게시 읽기·DTO/HTML을 보완했다. 신규53 포함 최종 UTC4403passed287.03초/KST4403passed268.28초·각기존xfail2/경고4·격리0, native/Opus 한정 승인·외부 호출부 확인 조건도 충족했다. [C4 Plan–Do–See](morning-regime-owner-2026-09-20.md)에 원 반려·수정·재리뷰·advisory를 보존했다. unknown baseline은 명시 차단이며 새 day/startup 기준선 인계를 자동 지원하지 않는다. 아래 전체 C/F/G/R와 qualification/최종 sizing·writer/factory·공식 증거·성능·운영 전환은 이 C4 한정 검증으로 완료되지 않는다.

| ID | 현재 구체 근거 | 남은 전체 인수 |
|---|---|---|
| C1/C8 | `test_execution_lifecycle.py::test_cancel_result_never_releases_original_sell_reservation`, `test_execution_guards.py::test_common_safety_barrier_applies_to_all_trade_commands` | 실제 broker cancel→scheduler fallback 연결에서 추가POST0/원pending 보존 |
| C2/C6 | `test_kis_order_evidence.py::test_cancel_quantity_fields_do_not_invent_proven_finality`, `test_kis_execution_query_integration.py`의 실제GET 페이지/실패 시험 | 취소 체인 미지원 유지, 조회 결과→실제 intent 대사 배선 |
| C3 | `test_execution_lifecycle.py::test_replacement_requires_terminal_evidence_and_applied_fills` | 합성 최종성은 공식 취소 계약 증명이 아님. 지원 증거가 없어 실경로 다음5주 주문은 미지원 |
| C4 | `test_execution_lifecycle.py::test_evidence_other_scope_never_matches`, `test_late_or_wrong_sender_result_cannot_release_new_attempt` | 실제 수집기 scope→broker ref→체결큐 연동 |
| C5 | lifecycle 재시작/단회 POST 시험 및 `test_execution_command_shutdown.py`의 실제 commands·SQLite·fake HTTP 응답/결과 drain·저장 실패 인수 | 명령 owner 한정 연결됨. 실제 gateway 설치·engine/scheduler/수동 호출·broker/lease 종료 순서 인수는 미완 |
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
| G1/G2 | final guard 시험 및 `test_execution_command_owner.py::test_current_state_after_every_network_await_blocks_post`, 실제 bound prepare→ACK→실큐 체결의 `test_execution_command_flow.py` | 요청/예약 owner 한정 연결됨. 실제 분석/분산대기/LLM·현재 source publisher→broker 전체 경로는 미완 |
| G3/G3a | `test_execution_guards.py::test_timezone_host_does_not_change_kst_cutoff`, `test_failure_attempt_hides_previous_normal_until_new_valid_observation` | 현재 위험 publisher를 실제 scheduler 관측 성공/실패에 배선 |
| G4 | `test_execution_guards.py::test_explicit_routes_exempt_only_alpha_and_metadata_cannot_issue_context` | 실제 KOFR/수동/SELL 경로 이행·기존 위험 예외 특성화 |
| G5 | 공통 guard와 `test_execution_command_owner.py`, `test_execution_command_shutdown.py`의 실제 owner→prepared request→fake HTTP·DB/게시 장애 | 실제 설치자의 모든 raw 송신점 이관·계좌 lease 최종 검사와 결합해야 함. MODIFY 미지원 유지 |
| R1/R1a | `test_execution_state_store.py::test_existing_invalid_database_is_rejected_not_recreated`, `test_execution_runtime.py::test_empty_database_does_not_replace_known_live_portfolio` | 최초 인계 근거 부재로 시작 차단 유지. 실제 시작 후 모든POST0 인수 미완 |
| R2 | Task9 동결본 KST/UTC 전체3294passed/기존xfail2, Task10A1 추가 종료/lease 회귀는 위 진행 원장에 별도 기록 | 공용 파일 변경 때마다 반복. US 실행 안전성 완료 근거는 아님 |

### 이후 구현 순서의 구체 경계

1. Task8A/B를 리뷰해 보호 repair/원장 ACK를 먼저 닫는다. 다음은 수락된 큐 작업까지 확인하는 KST rollover와 최초 손절·검증된 최종성 증거를 보존하는 initial R이다. 오래된 불명 이력은 추정하지 않는다.
2. 실제 request의 command/TR/계좌범위/종목/side/수량/가격/부모 참조 fingerprint를 durable attempt·claim·예약과 결합한다. 마지막 await 뒤 현재 예약/현금/수량/기존 위험 한도를 다시 확인한다. 시장가 wire 가격0은 예약 금액0이 아니다.
3. 취소/정정 체인 미지원 중에는 **모든 정정(감소·매도 포함) 미송신**을 유지한다. 향후 계약이 확보돼도 가격상승·수량감소처럼 방향이 다른 경우의 추가 현금·계획손실·노출을 독립 계산·예약해야 하며 BUY alpha 통과는 이를 대신하지 않는다.
4. 위 계약이 준비되면 core→broker→scheduler 체결/청산→batch→KOFR/수동→일일 초기화→시작 인계 순서로 writer를 이행한다. 각각 실제 호출점의 성공과 실패 시험을 추가한다. legacy를 단순 거부한 경로는 이행 완료로 세지 않는다.
5. 최초 인계 cutoff·체인 수량 범위 자료를 공식 명세 또는 승인된 비식별 응답으로 보충한다. 소프트웨어 시험 fixture는 그 외부 사실의 증명이 아니다. 미충족 인수를 공개한 뒤 독립 전체 리뷰와 별도 main/운영 전환 판단을 한다.
