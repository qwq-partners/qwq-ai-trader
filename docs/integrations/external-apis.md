# 외부 API 연동

> **10/27 장후 보고 전용 지수:** `FHKUP03500200`의 `output2`에서 같은 행의 `stck_bsop_date`·`stck_cntg_hour`·`bstp_nmix_prpr`만 결합한다. 미소비 한국 거래일16:40 보고에서0001/1001 각1GET·당일 첫 페이지·재시도/redirect 없음. 전용 HTTP 세션으로 자동 GET 재시도를 끄며 공유 거래 캐시/세션은 유지한다. `999999`/`888888`은 유효 시각이 아니고 output1 등락률/전일값을 섞지 않는다. 결과는 참고 가격이며 확정 종가/전일대비/시장 방향은 미산출. [계약과 검증](../superpowers/plans/2026-10-27-report-index-health.md) · [운영 상태](../operations/execution-roadmap.md#r02-index-health-20261027).

> **R02 보고 전용 필드:** Yahoo v7의 원래 `market_time`과 v8의 필드 부재는 거래 소비자 호환을 유지한다. 보고는 가격과 짝인 `report_market_time`/`report_market_time_kind`(regular_market/daily_bar)로 날짜를 검증한다. KIS `report_values`는 결측과0을 분리하지만 원천시각이 없는 응답에 수신시각을 대신 붙이지 않는다. 새 외부 조회는 추가하지 않는다. [보고 계약](../agents/reporting.md) · [구현/운영 상태](../operations/execution-roadmap.md#r02-telegram).

> **2026-10-24 Toss 보고 helper:** [실제 실행 경로·v3 보고 승인·설치/검증 상태](../superpowers/plans/2026-10-24-toss-report-runtime.md). 고정 `-I -S` 자식이 승인/토큰/원장을 소유하고 부모는 같은 아침 cohort의 가격만 소비한다. `TOSS_REPORT_API=1` opt-in·기본 OFF, 기존 observer namespace 불변. 첫 효과는 pykrx/Naver 절감이며 KIS 구독 용량/실측과 구분한다.

> **2026-10-18 개발 후속:** [수급 원문 근거와 캐시 구분](../reviews/supply-evidence-correctness-2026-10-18.md). 기존 KIS 수치 필드는 호환 유지하고 원문 유효성·자료 날짜 메타데이터를 추가한다. 중복 수급을 합산하지 않으며 Swing/5일 누적은 별도 버전 캐시를 쓴다. 기존 캐시와10월19일 고정 관측은 보존한다. 운영 미배포.

> **2026-10-18 운영 갱신:** [직접 KIS 계측 배포·v4 예약/판독](../operations/selection-capture-2026-10-19.md#10월18일-직접-kis-계측-배포와-v4-교체). 고정 `a0bc9c94`·PID2208722·NRestarts0·복구ready·미체결0. 운영 전체7290검사와 독립 중요 리뷰 통과, 매수 중지·월100·unknown1/normal·기존 설정/자료 보존.10월19일09:25 v4 예약과10:05~10:20 판독 입력 사전검사 완료. 실수집·판독은 아직이며 자동 판독은 미예약이다. 후속 문서 커밋은 재배포하지 않는다. 아래 미배포/v3 표시는 당시 이력이다.

> **2026-10-17 직접 GET 계측 개발:** 아래 [시장자료·스크리너 계측](#2026-10-17--시장자료스크리너-직접-get-계측-운영-미배포)은 운영 미배포 후속이다. 운영 `8003acbd`와10월19일 v3 수집/판독 입력은 그대로 유지한다.

> **2026-10-17 15:51 KST 이후 배포·v3 설치 확인:** [운영·계측·예약 근거](../operations/selection-capture-2026-10-19.md#10월17일-대시보드-배포와-v3-교체). PR #212 고정 `8003acbd`·PID2140438·NRestarts0·복구ready·미체결0. 운영 전체7105검사 통과, KR 매수 중지·월100·unknown1/normal·기존 원장과 설정 보존. 월요일10월19일09:25 v3 예약 enabled/active, 이전 v1/v2는 정지/미소비 보존. 실수집·순수익은 아직 미확정이다. 아래 운영·예약 상태는 각 시점의 이력이며, 후속 문서만 재배포하지 않는다. 아래 미배포 표시는 구현 당시 이력이다.

> **2026-10-17 — Gemini timeout fallback:** [호출 상한 계약·별도 비용 보류·정식 모델 전환](../research/gemini-timeout-fallback-2026-10-17.md). 일반task의 대체 경로를 유지하면서, 인증된 미래 시간 초과만 비용 보류 후 다음 공급자를 호출한다. 고정 역할/비교 공급자는 자동 교체하지 않는다. 운영 미배포.

> **2026-10-14 — 기존 REST 현재가 상태 보존:** [공식 필드 근거·지문·시간 한계·명시 opt-in](../research/rest-market-state-evidence-2026-10-14.md). KRX `FHKST01010100`의 기존 응답만 사용하며 추가 호출은 없다. N의 정상 의미/거래소 as-of는 미확인이라 항상 UNKNOWN. 논리 호출 구간은 리미터·재시도를 포함한다. 운영 미배포.

> **72·73차 KIS 상태:** [원천 규격과 한계](../research/kis-market-state-capture-2026-10-13.md), [한 종목 파일럿과 실효 채널 가드](../research/market-state-pilot-2026-10-13.md). 단일 연결의 정적 묶음을 사용하며 운영 PRICE 전달/REST 커버리지와 상태 기록을 분리한다. 장운영 사건 부재나 정규장 코드만으로 정상 연속매매를 확정하지 않는다.

> **2026-10-06 — 51차 수신 결함 수정:** KRX/NXT PRICE의 plain 다중 메시지를46/47필드로 엄격 분할하고 전체 값을 검증한 뒤 등록/현재 연결 세대별로 전달한다. BOOK 다중·암호화·형식 오류를 첫 값으로 축약하지 않는다. 토스 후보 조회는 통신 실패와 잘못된 입력을 구분하고 연속 실패2회에서 중단하며, 종료 봉인 회수 기한을 넘겨 완료로 판정하지 않는다. 고정 실패 분류만 저장하며 과거 불완전 자료는 보존한다. [코드 흐름·공식 근거·검증/실제 적용 범위](../operations/capture-execution-2026-10-06.md#51차--수신-결함-수정과-종목-선정진입의-다음-검증). 아래 각 날짜의 미구현/미배포 표시는 당시 이력이다.

> **41차 로컬 관측:** 명시v4에서 기존5종 프레임 오류만 제한된 TR/count/암호화/현재lease 범위로 기록한다. 원문/종목은 추가 진단에 저장하지 않고 다중/암호화 프레임을 첫 종목에 귀속하지 않는다. 기존 ACK/PRICE/BOOK 처리와 incomplete는 유지한다. API 호출/구독 정책·실접속 변경 없음. [PDS와 계약](../research/current-engine-capture-and-account-pds-2026-10-02.md).

> **23차 토스 WS(10-01, 로컬 미배포):** `orderbook_stream`은 인증된 전용 소켓의 소유권을 주입받는 한정 관측기다. 연결/토큰 발급 없이 국내 호가 full-replace1회·ACK 대조·최우선 호가·종료를 처리한다. 통합 KRX+NXT/LOSSY는 별도 후보 품질 보고에만 사용하고 KIS 호가/주문 경로에 투입하지 않는다. [선정·진입 규약23차](../research/current-engine-selection-entry-protocol-2026-09-30.md) 참조.

> **15차 관측 수명(10-01):** 명시 Runner 설치가 정지 feed의 기존 관측 조정기를 사용한다. 관측 종료는 후보 lease 수요만 닫고 운영 PRICE/BOOK·미확인 등록을 보존한다. 이미 전송 중인 요청은 해제 완료로 바꾸지 않으며 정확한 해제 ACK 허용 집합은 여전히 기본 빈 값이다. 새 세션/운영 API 호출을 이번 검증에서 실행하지 않았다.

> 최종 갱신: 2026-09-29 (접수 불명 분리·주문 CLI 거부·OrderRef·KOSPI 069500 대체 배포 반영). 토스 제한 관측은 09-22 18:00 KST grant 만료 뒤 서비스 disabled/inactive(재발급·퇴역은 사용자 결정 대기), 거래 소비자 미연결.


> **일별 체결 조회 완결 판정 (2026-09-29, 06:45 KST 배포 main `081ab6a`):** `KISBroker.get_fills_for_date_checked(d)` 는 `_query_daily_fills(…, status={})` 로
> 조회 완결 여부를 함께 돌려준다 — 완결 = 모든 페이지 `rt_cd=="0"` 이고 마지막 응답 헤더 `tr_cont` 가 D/E(10번째 페이지 포함).
> F/M 인데 ctx 가 비었거나(`contradictory_continuation`) 같은 ctx 반복(`repeated_ctx`), 헤더 없음(`missing_tr_cont`), 10페이지 상한(`page_cap`),
> 미연결·`rt_cd` 실패·예외·행 정규화 실패는 미완. 기본 호출(`status=None`)은 요청·반환이 이전과 같다(`check_fills` 무변경).
> 소비자는 초과수익 원장의 20:30 거래일 기록 대사다(설계 A §5-1).

## 2026-10-17 — 시장자료·스크리너 직접 GET 계측 (운영 미배포)

**PLAN:** 브로커 집계에서 빠지는 시장자료·스크리너 조회를 별도 집계해, 후보 수집 실패가 KIS 요청 제한과 연결되는지 확인할 준비를 한다. 실제 호출 수와 오류가 어느 경로에서 생기는지 구분한 뒤 캐시·호출 간격 개선의 우선순위를 정한다. 이번 계측만으로 순수익이나 요청 제한 해결을 주장하지 않는다.

**DO:** `KISMarketData`의 직접 GET10곳과 `KRScreener`의 KIS GET2곳을 관측한다. 기존 리미터를 통과한 뒤 GET 시도와 종료 결과만 메모리에서 누적한다. 휴장일의 여러 날짜 조회와 야간선물 시장구분 대체 조회는 각각 실제 GET 건수로 센다. 캐시로 끝난 호출·기존 리미터 대기 중 취소·네이버 크롤링은 이 집계에 들어가지 않는다. GET 진입 뒤 취소는 시도로 집계하며 실제 네트워크 송신 완료를 증명하지 않는다. 조회 인자·토큰 발급·캐시·대기·재시도·후보 계산은 기존대로 둔다.

- 상태 응답의 `broker.kis_direct_requests`에서 `scope=kr_market_data_and_screener_get`, `window=process_lifetime`과 고정 component/operation/TR별 건수를 읽는다. 기존 `broker.kis_requests`의 `kr_broker_get` 집계는 분리해 보존한다. 서로 다른 기간이나 다른 프로세스의 누적값을 바로 비교하지 않는다.
- HTTP 오류·API 거절·네트워크 오류·취소·JSON 오류를 구분한다. `EGW00201`/`EGW00215`는 원래 읽던 JSON의 코드가 있을 때만 추가 분류한다. 기존 코드가 non-200 본문을 읽지 않았다면 새로 읽지 않으므로 그 HTTP 오류의 세부 KIS 코드는 미확인이다.
- API 성공은 응답 봉투의 성공 표시다. 빈 결과·필드 결측·숫자 변환 오류와 후보 품질까지 정상이라고 뜻하지 않는다. 반환 뒤 데이터 처리 오류를 네트워크 실패에 섞지 않는다.
- URL·종목·계좌·헤더·본문·예외 문자열을 집계에 저장하지 않는다. 고정 차원과 프로세스 수명 누적만 사용하며 원장·파일·추가 요청·백그라운드 작업을 만들지 않는다. 계측 실패는 `available=false`로 표시하고 원래 호출의 성공/실패/취소를 바꾸지 않는다.

**SEE:** 구현자 관련70검사·통합 관련53검사·최종 직접 계측26검사를 통과했다. 로컬 전체 verify는7132 passed/2 skipped/2 xfailed,271.06초·rc0·격리 위반0·문법/비밀정보 검사 통과다. 전체 시험 목록 수집 뒤 추가한 고정 차원/개인정보1검사는 최종26검사에 포함해 별도로 확인했으며, 최종 커밋의 전체 결과는 PR/main CI 실행 기록을 따른다. 독립 Sol/high 요청 리뷰의 P2 표현/개인정보 검사 두 건을 반영해 미해결 P0/P1/P2 0으로 승인됐다. 실제 모델/실효 effort metadata는 미노출이다. 근거는 `~/.local/state/qwq-kis-direct-metrics-20261017/`에 보존한다. 운영에 배포하지 않았으므로 현재 운영의 직접 GET 실측값은 아직 없다. 월요일 고정 관측·봉인·판독을 마친 뒤 별도 배포 후보로 검토한다. 개별 송수신 시각, 다른 프로세스의 공유 키 사용, non-200 본문 코드와 전체 KIS 호출 범위는 이번 집계 밖이다.

## 2026-10-17 — 대시보드 KIS 조회 절약·출처 계측 (운영 미배포)

**PLAN:** 휴장 중에도 반복되던 전광판 요청을 줄이고, 추가 API 요청 없이 브로커 호출의 출처·시세 종류·거절 코드를 구분한다. 거래 가격/주문/리미터 정책과 월요일 관측의 고정 운영 입력은 유지한다.

**DO:** SSE 구독자가 없으면 표시 데이터 수집을 쉬고, 접속자가 있으면 기존 갱신 주기를 재개한다. 대시보드 서버별 `DashboardMarketCache`를 SSE와 HTTP에 공유하여 같은 종목의 동시 요청을 합친다. KIS 지수2개·고정 종목3개의 표시용 응답만 보관하며, 거래용 `broker.get_quote()`는 캐시를 우회한다.

- 한국 거래일/세션은 명시 KST와 공통 휴장일 판정으로 계산한다. 장중10초, 휴장/야간300초이며, 거래일08:00~20:00의 동시호가·중간 CLOSED 구간은10초를 유지한다. 날짜/세션 변경 시 다시 조회한다.
- 미국 지수 조회는 한국 휴장 캐시와 분리한다. 무접속일 때만 SSE 전체 조회가 쉬며, 사용자 HTTP 요청은 계속 처리한다.
- 실패/빈 응답도 같은 주기 동안 재호출을 억제한다. 이전 성공값이 있으면 `stale=true`와 원래 `fetched_at`을 유지하고 전광판에 “갱신 지연”을 표시한다. 없으면 결측/기존 Yahoo 폴백을 따른다. `fetched_at`은 대시보드가 공급자 메서드의 값을 받은 시각으로, 거래소 시각이나 새로운 HTTP 수신 인증이 아니다(공급자 자체 캐시가 있을 수 있다).
- `kis_request_metrics`에 `dashboard_sse_indices`/`dashboard_http_indices`, `quote`/`overtime_quote`/`daily_prices`와 시세 TR3종을 고정 allowlist로 추가한다. 일봉 페이지와 EGW00201 응답도 별도 집계한다. 기록 실패는 거래 반환/재시도 제어에 영향을 주지 않는다.
- 계측 범위는 계속 `kr_broker_get`이다. 직접 `KISMarketData`/스크리너의 HTTP 요청, 다른 프로세스, 공급자 실제 버킷은 포함되지 않는다. 이번 변경은 개별 송신/응답 시각 원장이나 direct non-200 코드 분류를 추가하지 않는다.

**SEE:** 최종 전체 verify **7043 passed / 2 skipped / 2 xfailed**(253.27초, rc0), 관련61검사·최종 실제 서버 연결7검사/UTC7검사·JS 문법/지연·정상·기존 응답 표시3경우 통과. 최종 운영 상태/외부 네트워크 접근 시도0·문법/비밀정보 검사 통과. 독립 Sol/high 요청 리뷰 P0/P1/P2 0으로 승인했다(실제 모델/실효 effort metadata 미노출). 브로커 구현은 Terra/high 요청 작업자와 분리했고, 루트가 차이·시험 근거를 확인해 통합했다. 실제 KIS 감소량·거절 해소·순수익 개선은 운영 실측 전이며, 계산상 무접속 전광판 요청0·지속 접속 휴장 중 KIS 표시용 요청 최대5개/300초(재시도 별도)는 조건부 예측이다. HTTP가 사용하는 Yahoo 지수와 폴백, 거래/다른 호출의 총량은 이 예측에 포함하지 않는다.

**후속 순서:** 개발 검증/병합 → 기존10월19일 고정 관측 완료 → 별도 배포와 동일 구간 요청/거절 실측. 직접 HTTP의 코드 분류·요청별 시각 귀속은 별도 후속이다. 이번 작업은 운영 배포/재시작·예약/설정 변경·증권사 API/거래 엔진 유료 LLM 수동 호출을 수행하지 않았다. 운영 체크아웃846c3215와 기존 evolved 설정의 미커밋 상태를 보존했다. 커밋/푸시 뒤 PR 및 병합 CI 상태는 GitHub 정본을 따른다.

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


## 테마 JSON 출력 예시 — 2026-10-29

테마/종목 임팩트 프롬프트의 예시는 실제 JSON 숫자를 사용하고, 필드 범위·설명은 객체 밖에 둔다. `impact`는 기존 -10~10이며 양수는 `6`처럼 선행 `+` 없이 출력하도록 명시한다. 예시 값은 형식 설명이며 실제 뉴스 판단값을 고정하지 않는다. 엄격 JSON 해석과 최대1회 재시도·모델/비용 계약은 그대로다. [자연 실패 근거·검증/운영 상태](../operations/execution-roadmap.md#e04-theme-json-example-20261029)를 따르며 새 유료 시험 호출은 추가하지 않는다.

## Claude 엔진 연결 — 2026-10-28

일반 뉴스·분석·보고는 GPT를 사용하고, 진입 반증/팀 Bear는 `claude-opus-5-5`/low로 검토한다. 로그인된 CLI를 먼저 호출하며 가용성 실패 때만 같은 모델의 Anthropic Messages API를 한 번 호출한다. 신규 Gemini는 client 경계에서 차단하며 과거 원장은 보존한다. 아래 과거 LLM 계약의 모델/라우팅 설명보다 이 절과 [현재 실행 원장](../operations/execution-roadmap.md#e04-gpt-claude-20261028)을 우선한다.

CLI는 빈 임시 cwd·입력 stdin·도구/MCP0·safe mode·기존 구독 로그인·API 환경 제외를 사용한다. 자식 그룹을 종료/회수한 후 API로 전환한다. 기존 전체12초/20초 안에서 CLI에 절반(최대8초), API에 실제 남은 시한을 준다. 모델 불일치·명시적 거절·도구/인증 정책 위반·호출자 취소는 자동 대체하지 않는다. 두 검토가 완성되지 않으면 검토 미완료로 보류한다.

API는 `https://api.anthropic.com/v1/messages`·리디렉션/자동재시도 없음·출력상한400을 쓴다. Opus5.5 API는 adaptive thinking을 요구하므로 `thinking.type=adaptive`와 `output_config.effort=low`를 전달하고 추론 포함 출력 한도를 유지한다. CLI의 요청 옵션과 API의 실효 추론이 같다고 추정하지 않는다. [공식 마이그레이션 계약](https://platform.claude.com/docs/en/models/opus-5-5/migration-guide).

`ANTHROPIC_API_KEY_FILE` 기본 경로는 `~/.config/qwq-ai-trader/anthropic-api-key`다. 대체 호출 시에만 읽으며 소유자·정규 파일·0600·크기·symlink를 확인한다. 선택적인 `ANTHROPIC_API_KEY`는 CLI에 상속하지 않는다. 키 값은 YAML/명령줄/로그/원장에 기록하지 않는다.

CLI는 `billing_channel=subscription`, API는 `api`다. 구독 표시 list cost는 별도 추정치이며 실제 추가 청구액은null이다. 월USD100·80/95/100%·KST 월초 복원·`log_and_continue`는 API 원장에 적용한다. API 가격 견적은 입력 캐시 쓰기 상한까지 포함하며 청구서 확정액이 아니다. 실제 모델/가용성 확인, 소스 병합, 운영 배포는 [Plan→Do→See](../superpowers/plans/2026-10-28-claude-cli-api.md)에서 분리한다.

## LLM — OpenAI / Gemini 호출 계약 (2026-10-12, 과거)

기본은 OpenAI heavy `gpt-5.6-sol`/light `gpt-5-mini`, Gemini heavy `gemini-3.1-pro-preview`/light `gemini-3.1-flash-lite-preview`다. 실제 라우팅은 `LLMManager.TASK_CONFIG`이며 `llm.primary`/`fallback`은 기존 설명용 설정으로 이를 덮어쓰지 않는다. 모델은 이번에 변경하지 않았다.

`reasoning_effort`(OpenAI), `thinking_level`(Gemini)은 지원하는 모델·값 조합만 명시 전달한다. 다른 공급자의 옵션이나 미지원 조합은 전송 전에 실패하며 임의 번역하지 않는다. GPT-5/6에는 temperature를 보내지 않고 max_completion_tokens를 사용한다. Gemini3.8에도 temperature를 보내지 않는다. 옵션 미지정은 공급자 기본값 요청이며 유효 추론 강도를 관측한 것이 아니다.

응답의 `.model`은 호환을 위해 요청 이름을 유지하고 `requested_model`/`resolved_model`(서버 보고 시만)·요청 추론 옵션·reasoning_tokens·finish_reason·latency를 별도로 보존한다. OpenAI output에는 추론이 이미 포함되고, Gemini output은 candidates+thoughts다. 비용은 `llm_pricing.rate_quote`의 공개 표준 요율 추정이며 미확인 모델은 unpriced다. Flash-Lite preview의 계열 참조 가격은 실제 청구 alias 인증이 아니다.

`LLMManager(..., audit_dir=명시경로)`만 메타데이터 감사를 쓰고 기본은 미기록이다. 프롬프트/응답 본문·키·계좌·원본 오류를 넣지 않는다. 날짜별 보존을 제한한다. 기존 본문 로그와 별개이며 현재 운영에서 새 감사가 켜졌다는 의미가 아니다. 단계별 범위/비용/실측 계획은 [68차 정본](../research/llm-and-observation-priorities-2026-10-12.md)을 따른다.

### 용도 (heavy 작업)
| 태스크 | 용도 |
|--------|------|
| STRATEGY_ANALYSIS | 매수 전 LLM 이중검증 (크로스검증) |
| TRADE_REVIEW | 일일 거래 복기 (20:30) |
| MARKET_ANALYSIS | 장전 시장 진단 (08:50) |

### 한도
- 이중검증: 10회/일
- 기본 예산: $5, `llm.daily_budget`의 유한 비음수 USD를 읽는다.0이면 차단한다. 빈 응답/재시도/fallback/shadow의 각 시도를 집계한다. **프로세스 내 사후 집계**이므로 동시 요청·재시작·미상 청구를 포함하는 엄격한 전역 일일 상한이 아니다. 미상 단가/전송 후 미상 사용량이면 후속 유료 요청을 보류한다.
- 외부 timeout에 의한 전송 후 취소도 미상 사용량으로 정확히 한 번 기록하고 원래 취소를 전달한다. 생성 전4xx의 명확한 오류 구조(usage/output 부재)만 별도 비과금 추정으로 분리해 fallback을 허용한다. 감사 비용은None이며 청구0확정이 아니다.5xx/취소/불완전 응답은 이 예외가 아니므로 후속 LLM 가용성에 영향을 준다.

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

## 2026-10-23 후속: KIS 직접 거절 피드백과 분석 일봉 공유

정본: [조회 분담 PLAN→DO→SEE](../superpowers/plans/2026-10-23-kis-toss-read-offload.md).
아래는 개발 계약이며 실제 운영 적용/측정은 정본 영수증을 따른다.

- 직접 GET의 non-200 JSON은 최대16KiB·0.25초 안에서 읽는다. 정확한
  `msg_cd=EGW00201`만 기존 `note_rejection(tr_id)`에 전달하며, 일반500·HTML·
  과대/부분 본문을 호출 제한이라고 추정하지 않는다. API200 오류도 같은 규칙이다.
  기존 원장/주문 리미터 간격·10RPS·재시도 횟수는 유지한다.
- 분석 일봉은 `get_analysis_daily_prices`에 명시 연결한 스크리너와 섹터만
  재사용한다. 요청 시작 후60초·같은 브로커/loop·KST 날짜/세션·충분한 자료 깊이,
  최대64항목/각500봉이며 반환은 독립 복사다. 실패·부족·비유한 자료는 캐시하지
  않는다. 한 대기자 취소가 나머지를 끊지 않고 마지막 취소는 송신을 회수한다.
- health의 `analysis_daily_requests`는 해당 분석 요청/캐시 적중/동시 합치기/
  공급자 메서드 호출 수다. HTTP 페이지 수나 전체 KIS 요청 감소율로 읽지 않는다.
  다음5분 스캔의 당일 봉은 다시 조회하므로 재발 오류 전부 해결이라고 표시하지 않는다.
- Toss 시세가 존재한다는 것과 KIS 소비자를 대체한다는 것은 다르다. 새 v3의
  `daily_report_current_price` 목적·`report_display` capability와 명시 deployment
  opt-in에서만 보고 경로가 가격·시각·원천을 소비한다. 기존 보고의 `_bot` 미주입 때문에 이 경로
  자체의 KIS 운영 절감은 주장하지 않는다. 대시보드·일봉·WS 확대는 별도 검증한다.
  현재16:00 보고는 기존09:00–15:20 관측 세션으로 충족되지 않는다. 새 계획의
  승인 시각과 실제 수집 완료 순서·TTL을 확인해야 하며, 기존 설정은 자동 확대하지 않는다.
