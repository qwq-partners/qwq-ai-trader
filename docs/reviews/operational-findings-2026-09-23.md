# 09-23 운영 관측 결함 — Plan → Do → See

## 범위와 사실 확인

기준 main/운영 체크아웃 `afa6e1e`, 수정 후보 `3cfcdca`. 별도 feature 브랜치에서
구현했으며, 아래 테스트는 오프라인 합성 검증이다. 주문·설정·킬스위치·Toss grant·
운영 원장 변경 및 재시작은 하지 않았다. 단일 owner 엔진의 운영 설치와 별개다.

`journalctl`의 MESSAGE가 문자열 또는 UTF-8 byte list인 경우를 모두 읽어 집계했다.
원문 계좌·주문 자료는 문서에 옮기지 않았다. 같은 거절이 broker와 limiter에 각각
기록되므로 로그 줄 수를 실제 거절 횟수와 비교하면 안 된다.

| 날짜 | broker 경고 | limiter 경고 | 실제 거절 수 |
| --- | ---: | ---: | ---: |
| 09-21 | 95 | 95 | 95 |
| 09-22 | 118 | 118 | 118 |
| 09-23 | 101 | 101 | 101 |

전부 `TTTC8434R`. 오늘 202줄은 101건의 중복 기록이다. `(PID, 원장 초과 순번)`의
고유 수와 broker 경고 수가 일치했다. 장중 동기화 실패 표식은10:04이며, 별도로
장외01:47 표식도 있었다. 보유 종목 증가가 원인이라는 가설은 아직 입증되지 않았다.
KOSPI 마지막 봉09-17 로드 로그는 오늘2회, 만료 macro 경고는36줄 확인했다.

LG디스플레이의 실제 개별 주문 ID·체결 원장을 추적해 과거 사유를 확정한 것은 아니다.
확인한 코드 결함은 `Signal.reason → Order.reason → Fill.reason` 뒤 체결 루프가
`Fill.reason`을 버리고 종목 캐시에만 의존한 점이다. `sync_detected`는 저장기의
분류 결과이지 `_sync_portfolio`가 매도를 실행했다는 증거가 아니다.

## Plan — 경계

- 호출 수를 먼저 계측한다. 기존 원장 한도·백오프·TTL·주문·취소 의미는 바꾸지 않는다.
- 일지 원인은 실제 체결에 연결된 주문 근거를 사용한다. 기존 리스크 재진입 제한의
  분류와는 분리한다. 없는 원인을 캐시·텔레그램 문장으로 추정하지 않는다.
- 오래된 지수 자료는 제외하고, 실제 지수 소스만 대체재로 사용한다.
- macro 만료를 연장하거나 삭제하지 않는다. 동일 만료 자료의 반복 경고만 줄인다.
- root+최대3workers, 같은 base·격리 worktree·파일별 단일 작성자. 분석은
  Sol/high·Terra/high, 중요 구현은 Astra/high, 독립 검토는 Astra/xhigh와
  tools-off Opus/xhigh. native 실제 모델/실효 effort는 메타데이터 미노출로 미검증.

## Do — 구현

### 호출 계측

`/api/health`의 `broker.kis_requests`에 프로세스 누적값을 제공한다.

- `logical`: source/operation별 calls, cache_hits, cache_misses.
- `http`: source/operation/TR/page별 attempts, retries, egw00215, 결과별 횟수.
- source: startup, portfolio_sync, portfolio_sync_consistency_retry, fill_check,
  batch_guard, dashboard_external_accounts, dashboard_settlement, unknown.
- `attempts`는 실제 GET 전송 시도, `retries`는 실제 두 번째 이후 전송이다.
  재시도 로그만 남기고 취소된 대기는 전송으로 세지 않는다. 거절 응답은 한 번 센다.
- 계좌·종목·주문 ID·URL·본문·예외 원문을 차원으로 받지 않는다. 고정 allowlist를
  벗어나면 unknown/other. 계측 실패는 available=false이며 거래 예외를 대체하지 않는다.
- positions의 기존5초·1회용 잔고 스냅샷만 cache hit/miss 대상이다. 다른 operation의
  0은 캐시가 없거나 해당 없음이지 100% miss가 아니다.
- 페이지는 잔고/외부계좌/일일체결의 원장 루프를 구분한다. 기타 GET의 다중 페이지는
  이번 상세 계측 범위가 아니며 기본 page1이다. POST는 계측·변경하지 않았다.
- 별도 API·파일 I/O·로그·주기 task를 추가하지 않는다. recent rate-limit 값은
  KR에 없던 `_api_call_times` 대신 기존 공유 limiter의 최근1초 기록을 읽는다.

관측 절차: 같은 프로세스의 시작/종료 두 health 스냅샷 차이를 사용한다. 운영자가
기존 인증된 대시보드 경로에서 조회하면 추가 KIS 호출은 없다. `TTTC8434R`만 모아
source별 attempts/retries/egw00215를 비교하고 page>1·consistency_retry·cache_miss를
분해한다. 재시작을 걸친 차분, available=false, 음수 차분은 유효 표본이 아니다.
절대 원장 호출 한도의 공식 증거나 다른 프로세스의 호출량으로 확대하지 않는다.

### 매도 일지

주문과 매칭된 `Fill.reason`을 JSON·TradeStorage DB 큐·DB fallback·복기 메타데이터에
전달한다. LLM 종가 판단은 `llm_eod`. 실제 기본 저장기의 문구 재분류가 `본전 이탈`을
`breakeven`으로 덮던 독립 리뷰 지적도 고쳤다. 리스크 `record_exit`는 기존 분류 그대로다.
판단 로그는 한 줄·사유300자 제한이며 주문/텔레그램의 원문은 변경하지 않는다.

DB 직접 폴백의 부분청산은 SELL 이벤트 INSERT에 사유를 저장한다. 부모 `trades`
부분 UPDATE는 기준선처럼 수량·손익·수정시각만 갱신하며 대표 사유를 바꾸지 않는다.
저장소 스키마의 exit_type/status는 문자열이며 유형별 SQL 집계도 닫힌 enum이 아니다.
화면은 미등록 유형을 원문으로 표시하고 전체 SELL 손익에 포함한다. 실제 운영 DB에
별도 수동 제약이 추가됐는지와 저장소 밖 소비자는 검증하지 않았다.

한계: 재시작 후 사라진 주문 사유, 시장가 폴백의 부모 주문 원인 복원은 미지원이다.
빈 사유는 fill_detected/sync_detected라는 기존 미상 표기로 남는다. 원인 없는 후속
체결이 누적 대표 사유를 덮는 기존 한계도 남는다. 과거 일지 자동 수정은 하지 않는다.

### 지수·만료 경고

KS11의 마지막 봉·가격·정렬·중복을 검증하고 실패하면 기존 FDR의 명시적
`YAHOO:^KS11` 지수로 한 번 대체한다. 주식 일봉 API의 `0001`을 지수로 쓰지 않는다.
당일 부분봉 또는 직전 한국 거래일까지만 허용하고, 소비할 때 다시 검사한다.
둘 다 무효면 MRS에서 제외하고 상태/사유를 남긴다. 동기 FDR 작업의15초 await
시간 제한이 이미 실행 중인 thread를 강제 종료하는 것은 아니다.

LLM 레짐의 c5/c20은 오래되거나 날짜 미상이면 None이며 당일 지수 하나로 중간
누락 봉을 메우지 않는다. 종가 LLM의 c5를 '오늘 KOSPI'로 표기하던 것도
'최근5거래일'로 바로잡았다. numeric getter의0/neutral 호환 폴백은 유지하므로
자료 부족 시 모든 매수를 막는 새 정책을 구현했다고 해석하면 안 된다.

macro 파일을 매번 다시 읽고 만료를 판정한다. 같은 만료 자료는 프로세스당 요약
경고1회, 변경·제거 후 재등장 시 재경고한다. 값·키를 새 요약 로그에 싣지 않는다.
기존 잘못된 valid_until 파싱 경고 정책은 범위 밖이다. 실제 만료 파일은 무변경이다.

## See — 검증 기록

- 기준선 전체: UTC1982 passed / 기존2 xfailed, 격리 위반0.
- 매도 최초 구현: 신규14건 포함 관련180 passed. 실제 KIS 합성 누적40→100이
  증분40+60으로 같은 주문 사유를 보존하는 경로 포함.
- benchmark: RED24 failed / 특성화2 passed → 보강 후 UTC/KST 관련각98 passed.
- macro: RED7 failed → 기존 결측 시험 포함23 passed.
- 후배선: 날짜/품질 가드 RED4 failed, EOD 기간 RED2 failed, 호출자 RED4 failed.
- 독립 native1차: 신규52 passed, P2 저장기 재분류1건. 실제 저장 경로 RED1 failed
  /14 passed 재현 후 명시적 llm_eod 보존으로 수정했다.
- 통합 후보 관련9파일:141 passed / 기존pykrx warning1 / 격리0.
- native 최종 재검토: 동일 후보 관련141 passed, 신규 P0/P1/P2 없음. 외부 리뷰의
  미확인 소비 경로를 추가 확인해 관련5 passed·실제 UI 함수의 합성 JS 검사 통과.
- 외부 검토는 새 운영 관측 범위에서 최대2회·각600초/$5로 한정했다.
  1회는124,011bytes 입력·600.136초에서 total_timeout(exit124). 모델 진행487회였고
  인증·프로토콜 오류는 없었다. 승인이 아니며 모델 사용 불가로 해석하지 않았다.
  2회는 동일 모델/effort/권한에 제품 경계만79,529bytes로 줄여343.091초에 완료했다.
  실제 metadata `claude-opus-5`, requested xhigh(실효 effort 별도 미노출), $0.950295,
  exit0·APPROVE. tools0·수동 제공 텍스트 검토이며 테스트를 직접 실행한 결과가 아니다.
  2회 입력 SHA256: `87d80775a0cc170b52e3b4592902cc6bc841c39b45672391e634cd093013a779`.
- 외부 미확인2건(부분 UPDATE/llm_eod 소비)은 위 native 추가 확인으로 닫았다.
  FDR0.9.110의 실제 디스패처가 YAHOO:^KS11을 YahooDailyReader로 보내는 오프라인
  시험도 통과. health의 None은 JSON null로 보존되며 저장소 UI에 해당 필드 소비자는 없다.
- 비차단 권고는 후속으로 남긴다: 관측 콜백을 HTTP 오류 파싱 try 밖으로 분리,
  LLM 결측 분기 c5/c20 명시 초기화, 휴일 역탐색의 유한 상한. 현재 일반 경로 회귀로
  재현되지 않았으며 이번 후보의 변경 범위를 임의 확장하지 않았다.
- 최종 전체 suite 결과는 아래 후보별 최종 검증 절에 추가한다.

## 최종 후보 검증

제품 SHA3cfcdca. 모든 native/외부 작업자 종료 뒤 coordinator가 전체 suite를 직렬
실행한다. 문서만 편집하는 동안 제품/시험 tree는 고정한다.

- UTC 전체: **2069 passed / 기존2 xfailed / pykrx 경고1**,80.08초, exit0·격리 위반0.
- KST 전체: **2069 passed / 기존2 xfailed / pykrx 경고1**,79.63초, exit0·격리 위반0.
- 전체 추적 Python 문법·diff --check 통과. 프로젝트 private-key/AWS/GitHub 비밀
  패턴 매치0. 이는 제한된 패턴 검사이지 기존 저장소의 모든 민감정보 부재 보증이 아니다.

## 후속 — main 정합화·수동 복구

운영선 결함 수정과 미설치 단일 owner 개발선을 섞어 운영 완료로 보고하지 않는다.
main의 실제 보호 동작과 개발선의 고정 기대값 차이를 명시하고, 수동 복구는 읽기 전용
증거 분류부터 시작한다. 원장 삭제·TTL 해제·수량 자동 보정·UNKNOWN 소멸 추정·
`trading_ready=True` 강제는 금지한다. 공식 증거와 실제 C/F/G/R 인수 전에는 설치 차단 유지.
