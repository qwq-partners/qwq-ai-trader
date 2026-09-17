# KIS 실행 증거 계약 조사 — 2026-09-17

공개 공식 저장소만 조회했다. 실계좌 API·자격·운영 상태 파일은 사용하지 않았다.
고정 revision: `koreainvestment/open-trading-api@b4e6249714418aa57833d1cbbbced39cbcc5b125`.

## 확인 결과

현행 `TTTC8001R`/`TTTC8036R`의 근거는 공식 GitHub **legacy 경로에 존재한다**.
최신 예제의 TR이 다르다는 것과 현행 TR의 공식 자료가 없다는 것은 다른 말이다.

| 공식 파일 | 확인한 내용 | 이 자료만으로 확인하지 못한 내용 |
|---|---|---|
| [legacy/Sample01/kis_domstk.py](https://github.com/koreainvestment/open-trading-api/blob/b4e6249714418aa57833d1cbbbced39cbcc5b125/legacy/Sample01/kis_domstk.py) | `TTTC8001R` 일별조회, `CCLD_DVSN` 전체/체결/미체결, 날짜/주문번호 범위. `TTTC8036R` 취소가능조회. F/M이면 N 연속조회, D/E 마지막 페이지 | 취소/정정 체인의 최종 상태 정의, 단일 원자적 잔고+체결 cursor |
| [legacy/rest/kis_api.py](https://github.com/koreainvestment/open-trading-api/blob/b4e6249714418aa57833d1cbbbced39cbcc5b125/legacy/rest/kis_api.py) | 일별행의 주문번호·원번호·주문량·취소여부·체결대금·잔량 소비 | 정정 자식행과 원행의 체결 중복 집계 규칙 |
| [legacy/postman 실전 v2.6](https://github.com/koreainvestment/open-trading-api/blob/b4e6249714418aa57833d1cbbbced39cbcc5b125/legacy/postman/%EC%8B%A4%EC%A0%84%EA%B3%84%EC%A2%8C_POSTMAN_%EC%83%98%ED%94%8C%EC%BD%94%EB%93%9C_v2.6.json) | 현행 TR와 요청 필드. 일별 조회에 당일 주문내역 지연 가능성을 명시. 세 대상 API의 저장된 response 예시는 빈 목록 | 빈 결과의 주문 부재 증명, 지연 상한, 완결 주문/잔고의 동시성 보장 |
| [현재 일별조회 예제](https://github.com/koreainvestment/open-trading-api/blob/b4e6249714418aa57833d1cbbbced39cbcc5b125/examples_llm/domestic_stock/inquire_daily_ccld/inquire_daily_ccld.py) | `TTTC0081R`, 거래소/날짜/주문번호·연속조회 | 이 필드를 기존 TR에 동일하게 적용할 수 있다는 보장 |
| [현재 응답 필드 표시기](https://github.com/koreainvestment/open-trading-api/blob/b4e6249714418aa57833d1cbbbced39cbcc5b125/examples_llm/domestic_stock/inquire_daily_ccld/chk_inquire_daily_ccld.py) | `cnc_cfrm_qty`, `rmn_qty`, `rjct_qty`, `tot_ccld_qty`, `tot_ccld_amt` 열거 | 표시용 dict의 중복 키/라벨 때문에 그 mapping만으로 경제 의미·체인 규칙을 확정할 수 없음 |
| [현재 취소가능조회](https://github.com/koreainvestment/open-trading-api/blob/b4e6249714418aa57833d1cbbbced39cbcc5b125/examples_llm/domestic_stock/inquire_psbl_rvsecncl/inquire_psbl_rvsecncl.py) | `TTTC0084R`, 정정취소 가능수량·연속조회 | 취소가능 목록에서 사라짐 = 최종 취소라는 보장 |

레거시 정정취소 예제는 잔량 전부(`QTY_ALL_ORD_YN=Y`)일 때 `ORD_QTY=0`을 사용한다.
요청 ACK와 원 주문 최종성은 분리해야 한다. 이 문서의 발견만으로 기존 TR 또는 운영 주문 본문을 변경하지 않는다.

TTTC8001R의 '3개월 이내'는 같은 예제에서 **월 단위**로 설명한다(2024-04-25이면 2024-01월~04월).
조회 수집기는 요청 KST 월에서 3개월 전 월의 1일부터 요청일까지로 범위를 제한한다.
90일이나 동일 일자에서 3개월을 빼는 해석을 쓰지 않으며, 범위 밖 요청을 CTSC9115R로 자동 전환하지 않는다.

## 구현 판정

- 조회의 요청 계약/페이지 완결성 검사는 공식 legacy 예제를 근거로 구현할 수 있다. F/M의 다음 페이지를 건너뛰거나 반복 cursor/페이지 상한을 완결로 취급하지 않는다.
- 지연 가능한 일별 조회의 빈 결과, 취소가능 목록 부재, 동일 잔고 반복은 주문 부재/취소 완료/안전한 startup cutoff의 증거가 아니다.
- `unsupported_finality`와 `incomplete`는 별개다. 필드를 정규화할 수 있어도 최종 상태 의미를 입증하지 못한 조합은 예약을 보존한다.
- 최신 조회의 단순 KRX 현금 주문 전량 체결은 명시적 수량·잔량·대금·원번호·완전한 범위 검사를 거친 제한 계약으로 시험한다. 취소·정정 체인/NXT/SOR 및 현행 TR의 최종성 승격은 별도 검증 항목이다.
- 외부/미식별 주문을 기존 intent에 수량·종목 유사성만으로 연결하지 않는다. 시작 대사는 상호 일치한 로컬 상태만으로 성공시키지 않는다.
- 합성 fixture는 파서·상태기계 인수 근거이지 실 API 응답 인수나 실거래 안전성 완료 증거가 아니다.

## 09-18 시세 원관측 시각의 추가 근거 (writer 이전 준비)

같은 고정 revision의 [legacy WS 시장 체결가 배열](https://github.com/koreainvestment/open-trading-api/blob/b4e6249714418aa57833d1cbbbced39cbcc5b125/legacy/Sample01/kis_domstk_ws.py#L103)과 [현재 WS 함수 예제](https://github.com/koreainvestment/open-trading-api/blob/b4e6249714418aa57833d1cbbbced39cbcc5b125/examples_user/domestic_stock/domestic_stock_functions_ws.py)를 공개 원문으로 확인했다. 시장 체결가 H0STCNT0/KRX 및 현재 예제 H0NXCNT0/NXT의 배열에는 시간(index1)과 영업일자 BSOP_DATE(index33)가 있다. legacy는 시간 필드의 이름을 TICK_HOUR로 바꿔 소비한다.

현재 로컬 `kis_websocket._handle_price_data`는 HHMMSS를 읽고 버리며 영업일자는 읽지 않는다. Event의 생성 시각을 이 원 시각으로 오인하면 안 된다. 후속 어댑터는 원 TR·날짜/시간·수신 시각을 분리하고, 정확한 레코드 범위·필드 수·날짜/시간 유효성을 검증해야 한다. 현재 REST `KISBroker.get_quote` 반환에는 시장 as_of가 없으므로 수신 now로 채우지 않는다. 이 문단은 원문 조사 결과이며 해당 feed 배선이 이미 구현됐다는 뜻이 아니다.

**시장 시세 시각은 계좌 체결 sequence나 잔고–체결 공통 cutoff의 증명이 아니다.** 최초 인계/취소 체인 미입증 판정은 그대로다. 외부 예제의 인증/실행 코드를 실행하지 않았고 실 API를 호출하지 않았다.

## 고정 파일 SHA-256

### 09-18 후속 재확인

공개 main의 고정 revision은 동일했다. [실시간 체결통보 예제](https://github.com/koreainvestment/open-trading-api/blob/b4e6249714418aa57833d1cbbbced39cbcc5b125/examples_llm/domestic_stock/ccnl_notice/ccnl_notice.py)는 접수 통보와 체결 통보를 구분하지만, 끊김 이후 무손실 재생·전역 sequence·잔고와 공유하는 cutoff까지 보장하지 않는다. 연결 성공이나 취소 접수 통보를 최초 인계/최종 취소 증거로 쓰지 않는다.

공식 Postman의 `response: []`는 저장된 응답 예제0건이라는 뜻이며 실제 API의 빈 output 증거가 아니다. 잔고 AFHR/PRCS 범위 선택도 원자적 스냅샷 근거는 아니다. 원주문/정정 자식의 누적 체결 중복, 부분취소 이후 잔여 권리, 다단정정의 수량 보존식·지연 상한은 확인 자료에서 충분히 입증하지 못했다. 공식 포털 검색에서는 추가 유효 자료를 얻지 못했고 인증 후 상세 명세를 확인했다고 주장하지 않는다.

추가 자료는 실제 TR·환경·시장/세션·요청 시작/응답 완료 aware시각, 페이지별 header/cursor, 일관 가명 처리한 원주문/자식 식별자, 응답에 실제 존재하는 수량·대금·상태필드를 연결해 제공해야 한다. 체결 없는 취소/부분체결 뒤 취소/다단정정/경합/날짜경계의 fixture는 회귀를 보강하지만, 표본만으로 최대 지연·원자성을 증명하지 않는다. 공식 수량 의미·cutoff 보장 범위와 함께 지원 계약을 확정한다. 계좌번호·자격·인증헤더는 필요하지 않으며 수집을 위한 실주문은 요청하지 않는다.

현재 미입증 범위는 `unsupported_finality`/startup 차단으로 유지한다. [단계별 실행 원장](../reviews/engine-execution-followup-2026-09-18.md)에 구현/인수와 분리해 기록한다.

### 기존 및 추가 파일 지문

- legacy Sample01: `d7bc6da85f4b086de3063f110d6e426fbc5751bc340b45e533ccdf9a5d55e575`
- legacy REST: `59c9722c08c3d91c08fa8907cb705b826ac4596233dd87f9d8429bf73b9c5596`
- 현재 일별조회: `9a039eef4f4c61e6d3b5ec15a85b18508e69f5b069bbbf447a0ba5cfe6044b5b`
- 현재 응답 필드 표시기: `e97ad745cac44abeff7615c68afe96258dc664da1ed60db33d49b4736a34735c`
- 현재 취소가능조회: `c51f69737ebf2faf5bda92030e81deffe43f7f30d1214afc409f3a5481f36fd0`
- 실전 Postman: `8309124f93909600011847451b0cb7bb45aecebb22d07981370742604610bc04`
- 현재 체결 통보: `440f80ec223c7b8643a7d434c2190f700716fa193f50580c06372627ec21084f`
