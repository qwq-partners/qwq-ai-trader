# 계좌 순수익 기준선 — 확보 항목과 결측 (2026-10-13, 64차 A)

목적은 [계좌 순손익 입력 계약](account-net-return-input.md)(`account-net-return-v1`)을 채우기 위해 **확보한 항목과 실제 결측을 분리**하는 것이다. 금액·보유·주문 식별자는 Git 밖 비공개 파일에만 둔다: `~/.local/share/qwq-private/kis-account-review/account-baseline-status-20261013.json` (sha256 `0cc0b371928d2d4683d2763d62cacba6f55e3adff1ab3a4f63fca63ce6546117`). 이번 단계에서 **새 KIS API 호출은 0건**이며(장중·봇 가동 중 원장 TR 충돌 회피), 2026-10-02 읽기 전용 조회(44차)의 결과를 재사용했다.

## 확보한 항목

| 항목 | 상태 | 근거 |
|---|---|---|
| 기간 조회 | 2025-01-01~2026-10-01 요청, 46회 완료, 주문 2,651행·체결 2,540행, 일별/종목 금액·비용 대사 `exact` | `account-review-20261002-1332.json` |
| 증권사 보고 실현손익(비용 차감 후)·수수료·거래세·대출이자 비용·회전금액 | 확보(원화) | 같은 파일. `loan_int`는 대출이자이며 이자 수입 증빙이 아니다. **비용은 실현손익에 이미 반영돼 다시 차감하지 않는다** |
| 기간 끝 순자산 스냅샷 | 2026-10-02 13:27 KST 순자산·예수금·대출·미실현·보유 2종목 | 같은 파일 `balance` |
| 추정 청산 29건 | 경제적 왕복 29/29 일치(일자·종목·방향·수량·가격 부분집합) — **주문 식별 인증 아님**(내부 주문 ID 부재), 비용 버킷 완전 커버 아님(혼합 비용일 4건) | `economic-reconciliation-v2.json`, `integrated-findings.json` |
| 계좌 밖 운영비 중 LLM 유료 호출 | 월 예산 원장 SQLite 에 추정액 존재 | `~/.local/state/qwq-ai-trader/llm-budget.sqlite3` |

## 결측과 조회 한계

| 항목 | 왜 없나 | 확보 방법 | 영향 |
|---|---|---|---|
| 기간 **시작** 순자산 | 검토한 공식 현재 잔고/투자계좌자산 조회 요청에 임의 기준일 파라미터가 없음 | 증권사 월말/일별 잔고 명세 또는 별도 과거 평가 API 확인 | 원화 손익·TWR null |
| 외부 입출금·이체 | 공식 공개 국내주식 카탈로그/샘플에서 일반 현금흐름 TR 미확인(부재 확정 아님) | KIS에 TR·개인 권한·보존기간 문의 또는 입출금 명세 | 끝−시작을 손익으로 쓸 수 없음 |
| 배당·분배금·이자 수입 | 공식 계좌 권리 TR `CTRGA011R` 발견, 실제 조회 미완료 | 아래 기간별계좌권리현황 읽기 전용 조회·명세 대사. 일반 예수금/CMA 이자 포함 여부 미확인 | `income_in_equity` 미확인 |
| 기타 운영비(서버·데이터) | 미집계 | 청구서 집계·`allocation_ref` | 운영비 차감 후 손익 null |
| 현재 엔진 귀속 | 계좌 총계가 수동 거래·여러 엔진 버전 혼합 | entry-risk SHA 보존 포지션 cohort 만 별도 집계 | 현재 엔진 귀속 손익 null |

빈 월별 응답은 거래·입출금이 없었다는 증거로 쓰지 않았다(첫 반환 거래일 2025-12-29 이전 구간은 "조회 결과 없음"일 뿐이다).

## 지금 계산 가능한 값과 아닌 값

- 계산 가능: **거래 기간의 증권사 보고 실현손익(비용 포함)**. 미실현·입출금·배당·운영비가 빠진 값이라 계좌 순수익률이나 TWR 이 아니다.
- null 보존: `account_net_profit_krw`, `account_twr`, `current_engine_attributed_profit`, 운영비 차감 후 손익. `account-net-return-v1` 입력은 시작 순자산·외부흐름이 없어 만들 수 없고 CLI(`scripts/report_account_net_return.py`)는 실행하지 않았다(`_money` 는 null 을 거부한다 — 억지로 0 을 넣지 않는다).

## 66차 — 권리조회(CTRGA011R) 수집 도구

`scripts/ops/kis_period_rights.py --start 2025-01-01 --end 2026-10-01 --out-dir <비공개 경로>` 가 월 단위 창마다 모든 페이지를 회수한다(공식 샘플 파라미터, 응답 헤더 `tr_cont` F/M 이면 `ctx_area_fk100/nk100` 으로 다음 페이지, D/E/부재면 종료). 창별 상태는 `complete`/`empty`/`partial_max_pages`(공식 샘플의 재귀 상한 도달은 완료가 아님)/`partial_page_repeat`(같은 연속조회 키 반복)/`error`/`malformed` 로 구분하고, 원본 응답·금액·식별자는 out-dir(비공개) 에만 저장하며 표준 출력은 상태·행 수만 낸다. 장중(09:00~15:30 KST)에는 실행을 거부하고 — 운영 봇과 계좌 원장 TR 초당 1건 한도를 공유하므로(`CTRGA011R` 을 `LEDGER_TR_IDS` 에 추가) — 전송 직전마다 장 시작까지의 여유(`max(--market-margin-seconds, 3×타임아웃+15초)`, 내부 재시도까지 덮음)를 다시 검사한다. **봇(`qwq-ai-trader`)이 가동 중이면 장외에도 기본 거부**한다: 별도 프로세스는 봇의 레이트 리미터를 공유하지 않아 봇의 잔고 동기화 조회와 같은 초에 겹칠 수 있다. 명시 우회 `--allow-bot-running` 은 `--min-interval` ≥2.1초일 때만 받고, 그래도 충돌 가능성이 0 은 아니다(봇의 원장 TR 호출과 같은 초에 겹칠 확률만 낮춘다). 연결은 `connect()` 를 쓰지 않는 조회 전용 경로다: `connect()` 는 운영 실행 원장(ExecutionHistory) 세션을 열어 봇의 기록을 깨뜨릴 수 있고, 토큰은 봇과 같은 캐시 파일을 읽기만 하며 유효하지 않으면 새 발급 없이 중단한다(새 발급은 봇 토큰을 무효화할 수 있다). 출력 디렉터리는 본인 소유·0700 이어야 하고 파일은 0600 으로 쓴다. 원본 응답은 페이지 전체를 보존한다. 요약(`summarize_rights`)은 권리유형별 행 수·`last_alct_amt` 합(Decimal 문자열)·파싱 불가 건수만 집계하고 해석하지 않는다.

**해석 금지.** 조회 성공 ≠ 소득 확정. `last_alct_amt` 의 세전/세후·실제 지급·보존기간·예수금/CMA 이자 포함 여부는 지급 증빙 대사 전 미확인. 권리 이벤트의 현금은 외부 입출금·이체가 아니다. `loan_int` 는 대출이자 비용이다.

실조회 결과는 아래 "실조회 기록" 절에 상태·건수만 적는다(금액은 비공개 summary.json).

## 실조회 기록

| 구간 | 실행 시각(KST) | 봇 상태 | 창 | 창 상태 | 행 | 권리유형 | API 호출 | 파싱 불가 |
|---|---|---|---|---|---|---|---|---|
| 2025-01-01~2026-10-01 | 2026-10-13 15:35:05~15:35:37 | 가동 중(PID 1114252, 간격 1.2초 — 가동 가드 도입 **전** 코드) | 22(월) | complete 3 · empty 19 · partial/error/malformed 0 | 8 | `03` 7 · `04` 1 | 22 | 0 |
| 2024-01-01~2024-12-31 등 이전 기간 | 미실행 | — | — | — | — | — | — | — |

- 산출물(원본 22페이지·summary·manifest, 금액 포함)은 `~/.local/share/qwq-private/kis-account-review/20261013-rights-20250101-20261001/`(0700/0600) 에만 있다. 여기에는 상태·건수만 적는다.
- 행이 있는 창은 2026-03(3행)·2026-04(1행)·2026-05(4행) 이고 나머지 19개월은 빈 응답(`empty`, `rt_cd 0`·output 없음)이다. 빈 응답은 "권리 없음"과 "보존기간 밖"을 구분하지 못한다 — 2025년 전체가 비어 있는 것이 어느 쪽인지는 KIS 보존범위 확인 전 미확정.
- `03`·`04` 코드의 의미(배당/무상 등)는 공식 문서의 코드표로 대조하기 전 해석하지 않는다. `last_alct_amt` 8행 합은 summary 에 Decimal 문자열로 있고, 세전/세후·실제 지급·예수금 이자 포함 여부는 미확인이라 **계좌 소득으로 승격하지 않는다**.
- 실행 중 봇 로그(15:34:30~15:36:30)에 `EGW00215`/`EGW00201`/오류 0건, 토큰 캐시 파일 변경 없음(05:06 발급분 유지). 다만 이 실행은 봇 가동 중 간격 1.2초로 돌았고, 3차 리뷰에서 이 충돌 위험이 P1 로 지적돼 그 뒤 코드는 봇 가동 중 기본 거부·우회 시 ≥2.1초로 바뀌었다. 결과 자체는 유효하다(22창 모두 `complete`/`empty`, 오류 0).
- 이전 기간(2024년 이하)은 자동 실행이 1구간 뒤 취소돼 미실행이다. 다음 실행은 봇 정지 창(배포 직후 등) 또는 `--allow-bot-running --min-interval 2.5` 로 장외에 한다.

## 다음 완료 조건

0. KIS `CTRGA011R` 2025-01~2026-09 수집은 완료(위 표). 남은 것: 2024년 이하 기간 조회(보존범위 확인 겸), 권리유형 코드표 대조, 한 건의 현금 지급 명세와 `last_alct_amt` 의 의미 대사. 대사 전에는 계좌 소득으로 승격하지 않는다.
1. 시작 순자산(또는 첫 거래일 평가액)과 외부 입출금 명세가 확보되면 정규화 입력을 만들고 기존 CLI 로 원화 손익·TWR 을 낸다. 입출금 경계 평가액이 없으면 TWR 은 null 로 둔다.
2. 29건은 원주문/체결 ID 가 있는 증권사 명세가 있을 때만 식별 확정으로 승격한다.
3. 벤치마크(KODEX200·반도체 제외)는 같은 시작/끝/외부흐름 정의의 비교 포트폴리오가 있을 때만 수행한다.

## 10월13일 공식 API 조사 — 확보 가능성과 미확인 구분

공식 `koreainvestment/open-trading-api` 커밋 `208279102f43fd0c0f15fcf66c9e5a5adda27050`와 공식 API 포털을 조사했다. 공개 소스만 읽었으며 새 계좌 API 호출·운영 변경은 없다. 조사 라우팅은 Sol/high 요청, 실제 모델 메타데이터 미노출이고 아래 원천은 coordinator도 확인했다.

- [기간별계좌권리현황조회](https://github.com/koreainvestment/open-trading-api/blob/208279102f43fd0c0f15fcf66c9e5a5adda27050/examples_llm/domestic_stock/period_rights/period_rights.py): `CTRGA011R`, `/uapi/domestic-stock/v1/trading/period-rights`. 계좌·시작/종료일·권리 유형으로 조회하고 `CTX_AREA_FK100/NK100`와 `tr_cont`로 연속 조회한다. 공식 샘플은 재귀 상한에서 부분 결과를 반환하므로 그대로 복사해 완전 조회라고 표시하면 안 된다.
- [응답 필드](https://github.com/koreainvestment/open-trading-api/blob/208279102f43fd0c0f15fcf66c9e5a5adda27050/examples_llm/domestic_stock/period_rights/chk_period_rights.py): 권리유형·권리잔고유형·`last_alct_amt`(최종배정금액)·`tax_amt`·`cash_dfrm_dt`·지연이자 등이 있다. 배정금액의 세전/세후 의미, 최대 조회기간/보존, 실제 입금 확정 상태 및 일반 이자 포함 범위는 아직 검증되지 않았다. 권리 이벤트의 현금 입출금 코드를 일반 은행 입출금 원장으로 사용하지 않는다.
- [주식잔고조회](https://github.com/koreainvestment/open-trading-api/blob/208279102f43fd0c0f15fcf66c9e5a5adda27050/examples_llm/domestic_stock/inquire_balance/inquire_balance.py)와 [투자계좌자산현황](https://github.com/koreainvestment/open-trading-api/blob/208279102f43fd0c0f15fcf66c9e5a5adda27050/examples_llm/domestic_stock/inquire_account_balance/inquire_account_balance.py)은 평가액을 제공하나 요청에 임의 과거 기준일이 없다. 검토한 TR로 2025년 시작 NAV를 얻을 수 있다고 주장하지 않는다. 향후 일별 NAV 저장은 과거 결측 복원이 아닌 재발 방지다.
- [기간별손익](https://github.com/koreainvestment/open-trading-api/blob/208279102f43fd0c0f15fcf66c9e5a5adda27050/examples_llm/domestic_stock/inquire_period_profit/inquire_period_profit.py)은 기간 조회가 가능하지만 실현손익/비용 자료로 NAV를 대체하지 못한다. [공식 카탈로그](https://apiportal.koreainvestment.com/apiservice-category)에서 일반 입출금 이력 TR은 확인하지 못했다. KIS 문의가 필요하면 일반 위탁계좌의 은행 입출금/계좌간 이체, TR ID/path, 보존기간, 연속 조회, 개인 Open API 권한을 구체적으로 확인한다.
