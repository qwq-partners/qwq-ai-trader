# KIS 구 TR → 신 TR 이행 명세 — 운영(main) 기준 (2026-09-21)

> [KIS 공식 저장소 기준 조사](kis-repo-grounding-2026-09-21.md)의 산출물이다. coordinator 가 코드로 재확인한 것: **취소 POST 에만 `retry=False` 가 빠져 있다**(`main:src/execution/broker/kis_kr.py:858` — 접수 `:502`·정정 `:953` 에는 있다). 아래 §3 의 12~15 는 TR 이행과 별개로 지금도 성립하는 인접 결함이다.

> **범위** — 운영(main) 브랜치의 구 TR 5종을 저장소 현행 신 TR 로 옮기는 **명세**다. 구현·배포·재시작·주문·설정 변경을 포함하지 않는다. 이 문서는 `koreainvestment/open-trading-api@b4e6249`(2026-08-26 스냅샷) 의 `examples_llm/` 기준이며, `llms.txt:30` 의 "Follow request structures and parameter conventions demonstrated in the examples." 범위 안에서만 채택한다.
>
> **주의** — main 을 직접 열지 않고 이 worktree 에서 `git show main:…` 로 읽었다. 줄번호는 `65ea146` 시점의 `main` 기준이다.

## 1. TR 표

| # | 용도 | 구 TR (현행 main) | 신 TR (저장소) | 모의 | main 수정 지점 |
|---|---|---|---|---|---|
| 1 | 주식주문(현금) 매수 | `TTTC0802U` | **`TTTC0012U`** | `VTTC0012U` | `main:src/execution/broker/kis_kr.py:626` (`_order_tr_id`), 주석 `:619` |
| 2 | 주식주문(현금) 매도 | `TTTC0801U` | **`TTTC0011U`** | `VTTC0011U` | `main:src/execution/broker/kis_kr.py:628`, 주석 `:620` |
| 3 | 주식주문(정정취소) | `TTTC0803U` | **`TTTC0013U`** | `VTTC0013U` | `main:src/execution/broker/kis_kr.py:839`(cancel_order) · `:924`(modify_order) |
| 4 | 주식일별주문체결조회(3개월이내) | `TTTC8001R` | **`TTTC0081R`** | `VTTC0081R` | `main:src/execution/broker/kis_kr.py:1847`(`_query_daily_fills`) · `main:src/utils/kis_rate_limit.py:30-31`(`LEDGER_TR_IDS`) |
| 5 | 주식정정취소가능주문조회 | `TTTC8036R` | **`TTTC0084R`** | 저장소 예제에 **모의 분기 없음** | `main:src/execution/broker/kis_kr.py:1000`(`get_exchange_open_orders`) · `main:src/utils/kis_rate_limit.py:31` |
| — | 주식잔고조회 | `TTTC8434R` | **변경 없음**(저장소에 구/신 표기 없음) | `VTTC8434R` | `:1071`, `:1151`, `:1463` — 손대지 않는다 |
| — | 매수가능조회 | `TTTC8908R` | **변경 없음** | `VTTC8908R` | `:1519` — 손대지 않는다 |

**저장소 근거(원문)**
- `examples_llm/domestic_stock/order_cash/order_cash.py:103-107` — `if env_dv == "real": if ord_dv == "sell": tr_id = "TTTC0011U" elif ord_dv == "buy": tr_id = "TTTC0012U"` (모의 `:110-116`)
- `examples_llm/domestic_stock/order_rvsecncl/order_rvsecncl.py:106-109` — `if env_dv == "real": tr_id = "TTTC0013U" elif env_dv == "demo": tr_id = "VTTC0013U"`
- `examples_llm/domestic_stock/inquire_daily_ccld/inquire_daily_ccld.py:141-150` — `tr_id = "CTSC9215R" … "TTTC0081R" … "VTSC9215R" … "VTTC0081R"`
- `examples_llm/domestic_stock/inquire_psbl_rvsecncl/inquire_psbl_rvsecncl.py:82` — `tr_id = "TTTC0084R"  # 주식정정취소가능주문조회`
- 모의 TR 규칙: `examples_llm/kis_auth.py:422-424` — `if ptr_id[0] in ("T","J","C"): if isPaperTrading(): tr_id = "V" + ptr_id[1:]`

**저장소가 말하지 않는 것** — 구TR 의 지원 종료 일정, 구TR 호출 시의 동작(수용/매핑), 신·구 응답 필드가 같은지(Q30·Q27 미답). 덧붙여 저장소 안에서도 `strategy_builder/core/order_executor.py:202` 는 구TR `TTTC0802U/TTTC0801U`, `core/data_fetcher.py:568` 은 구TR `TTTC8001R` 을 쓰면서 `:673` 은 신TR `TTTC0013U` 를 쓴다 — **한 모듈 안에서 구/신이 섞여 있다.** 즉 "구TR 이 아직 동작한다"는 정황은 강하지만 보장은 아니다.

## 2. 요청 본문·헤더 차이

### 2-1. order-cash (`/uapi/domestic-stock/v1/trading/order-cash`)

| 키 | 저장소 신TR | main 현행(`:479-492`) | 조치 |
|---|---|---|---|
| `CANO`,`ACNT_PRDT_CD`,`PDNO`,`ORD_DVSN`,`ORD_QTY`,`ORD_UNPR` | 있음 | 있음 | 그대로 |
| **`EXCG_ID_DVSN_CD`** | **[필수]** — `:99-100` `raise ValueError("excg_id_dvsn_cd is required (e.g. 'KRX')")` | **없음** | **`"KRX"` 추가** |
| **`CNDT_PRIC`** | 있음(`:129`, 값이 비어도 키를 보냄) | 없음 | `""` 추가 |
| `SLL_TYPE` | 있음 — `:63` `(01:일반매도,02:임의매매,05:대차매도)` | 있음(`매도 "01"` / `매수 ""`) | **일치, 변경 없음** |
| `CTAC_TLNO`,`ALGO_NO` | 저장소 9키에 **없음** | 있음(빈 문자열) | **제거 보류** — '예제에 없다'가 '서버가 거부한다'가 아니다 |
| `AFHR_FLPR_YN` | order-cash 에 **없음**(저장소 전체에서 조회 파라미터로만 등장: `inquire_balance.py:55`) | `session in (pre_market,next_market)` 일 때 `"Y"`(`:492`) | **제거 보류**, 실계좌 확인 항목 |

저장소 본문 원문 — `order_cash.py:120-130`: `{"CANO","ACNT_PRDT_CD","PDNO","ORD_DVSN","ORD_QTY","ORD_UNPR","EXCG_ID_DVSN_CD","SLL_TYPE","CNDT_PRIC"}` 9키.

### 2-2. order-rvsecncl (`/uapi/domestic-stock/v1/trading/order-rvsecncl`)

저장소 본문 10키(`order_rvsecncl.py:113-124`): `CANO, ACNT_PRDT_CD, KRX_FWDG_ORD_ORGNO, ORGN_ODNO, ORD_DVSN, RVSE_CNCL_DVSN_CD, ORD_QTY, ORD_UNPR, QTY_ALL_ORD_YN, EXCG_ID_DVSN_CD` + 값이 있을 때만 `CNDT_PRIC`(`:127-128`).

main 은 취소(`:842-850`)·정정(`:937-945`) 모두 **9키**이고 `EXCG_ID_DVSN_CD` 가 없다 → **`"KRX"` 추가**가 유일한 필수 변경.
- `RVSE_CNCL_DVSN_CD` `"02"`(취소)/`"01"`(정정)은 저장소 정의(`:32` `01:정정,02:취소`)와 일치.
- `ORD_QTY`: main 취소는 `str(order.quantity)`(`:848`) — 저장소는 세 갈래(legacy `0` 강제 `kis_domstk.py:96-98` / backtester `"0"` / strategy_builder `str(qty)` / 공식 예제 실수량+실단가)로 **판정하지 않는다.** 이번 이행에서 **건드리지 않는다.**
- `ORGN_ODNO`/`KRX_FWDG_ORD_ORGNO` 의 출처는 저장소가 말한다 — `legacy/Sample01/kis_domstk.py:112` "주식일별주문체결조회 API output의 odno(주문번호) 값 입력". main 이 `_pending_orders` 캐시의 `orgno`/`kis_ord_no` 를 쓰는 것과 **같은 정본인지는 실계좌 확인 항목**.

### 2-3. inquire-daily-ccld

저장소(`inquire_daily_ccld.py:156-174`): 14키 고정 + `excg_id_dvsn_cd is not None` 일 때만 `EXCG_ID_DVSN_CD`(기본값 `"KRX"`, 허용 `KRX/NXT/SOR/ALL`).
main(`:1858-1868`): 키 집합은 같고 **`EXCG_ID_DVSN_CD="ALL"`** 을 구TR 에 실어 보낸다 — 저장소에 **구TR + 이 필드** 사례가 0건이다(Q27 미답).
- 이행 시 값 결정: 저장소 기준의 안전한 값은 **`"KRX"`**. `"ALL"` 을 유지하려면 실계좌 확인이 선행돼야 한다.
- `CCLD_DVSN="01"`(체결만)·`INQR_DVSN="00"`(역순)은 저장소 코드표(`:34-35` `00 전체 / 01 체결 / 02 미체결`, `00 역순 / 01 정순`)상 유효값이며 이행과 무관하게 유지.
- `INQR_DVSN_1=""` 은 현행 예제(`:40`)와 일치(legacy 는 같은 파일 안에서 `"0"`(`:213`)과 `""`(`:257`)로 자기모순).

### 2-4. inquire-psbl-rvsecncl

저장소(`inquire_psbl_rvsecncl.py:84-91`): `CANO, ACNT_PRDT_CD, INQR_DVSN_1, INQR_DVSN_2, CTX_AREA_FK100, CTX_AREA_NK100` **6키, 거래소 파라미터 없음**. main(`:1003-1008`)과 키 집합 동일 → **TR 값 외 본문 변경 없음.**
- 값 차이: 저장소 예제·chk 는 `inqr_dvsn_1="1"`(종목), main 은 `"0"`(주문). 저장소 안에서 이 필드의 의미가 두 갈래(`:46` '0: 주문, 1: 종목' vs `legacy/Sample01/kis_domstk.py:147` '정렬순서')라 **어느 값이 옳은지 저장소가 판정하지 않는다** → 현행 `"0"` 유지.

### 2-5. 헤더

| 헤더 | 저장소 | main(`_get_headers` `:213-225`) | 조치 |
|---|---|---|---|
| `tr_id` | 있음 | 있음 | TR 값만 교체 |
| `custtype` | **항상 `"P"`** — `kis_auth.py:427` `headers["custtype"] = "P"  # 일반(개인고객,법인고객) "P", 제휴사 "B"` | **없음** | 되돌릴 수 있는 별도 단계로 추가 권장. 필수인지 기본값이 P 인지 저장소는 말하지 않는다 |
| `tr_cont` | 항상 실림 — `kis_auth.py:428`; 첫 페이지 `""`, 2페이지부터 `"N"`(`inquire_daily_ccld.py:201-204`) | **`_api_get(:233)` 에 `tr_cont` 인자가 아예 없어 요청 헤더로 절대 나가지 않는다** | **TR 이행과 별개의 기존 결함.** 신TR 에서도 커서만으로 다음 페이지를 받을 수 있다는 근거는 저장소에 없다 |
| `hashkey` | **"필수 아님, 생략 가능"** — `kis_auth.py:270` `# 현재는 hash key 필수 사항아님, 생략가능…`, `:441-443` 에서 발급 호출이 주석 처리 | 모든 주문 POST 앞에 발급하고 실패 시 주문 포기(`:496-497`, `:853-854`, `:948-949`) | **유지.** 저장소보다 엄격하다. 다만 hashkey POST 가 공용 리미터를 1건 더 쓰는 비용과 실패 모드는 그대로 남는다 |

## 3. 제품에서 고칠 지점 전체 (main 기준)

**필수(TR 값)**
1. `src/execution/broker/kis_kr.py:626,628` — `_order_tr_id` 의 매수/매도 TR
2. `src/execution/broker/kis_kr.py:839` — `cancel_order` TR
3. `src/execution/broker/kis_kr.py:924` — `modify_order` TR
4. `src/execution/broker/kis_kr.py:1847` — `_query_daily_fills` TR
5. `src/execution/broker/kis_kr.py:1000` — `get_exchange_open_orders` TR
6. `src/utils/kis_rate_limit.py:30-31` — `LEDGER_TR_IDS` 에 `TTTC0081R`·`TTTC0084R` 추가(**제품 규칙 근거**: CLAUDE.md '새 원장 TR은 `LEDGER_TR_IDS`에 추가'. 신TR 이 원장 유량 대상인지 저장소는 말하지 않는다)
7. `src/utils/kis_rate_limit.py:33-37` — `LEDGER_TR_INTERVALS` 는 `TTTC8434R` 전용이라 변경 없음(확인만)

**필수(본문 필드)**
8. `src/execution/broker/kis_kr.py:479-492` — order-cash 본문에 `EXCG_ID_DVSN_CD`(+`CNDT_PRIC`)
9. `src/execution/broker/kis_kr.py:842-850` — 취소 본문에 `EXCG_ID_DVSN_CD`
10. `src/execution/broker/kis_kr.py:937-945` — 정정 본문에 `EXCG_ID_DVSN_CD`
11. `src/execution/broker/kis_kr.py:1868` — 일별조회 `EXCG_ID_DVSN_CD` 값 재결정(`"ALL"` → `"KRX"` 또는 실계좌 확인 후 유지)

**같은 호출 지점에서 드러난 인접 결함(이행과 함께 다뤄야 하나 TR 이행 자체는 아님)**
12. `src/execution/broker/kis_kr.py:858` — **취소 POST 만 `retry=False` 가 빠져 있다.** 접수(`:502`)·정정(`:953`)에는 있다. 저장소는 재시도 정책도 취소의 멱등성도 규정하지 않으므로(`kis_auth.py:443` 단발 POST) **안전하다고 볼 근거가 없다.** CLAUDE.md 의 '주문 POST 재전송 금지' 규칙이 '접수/정정'만 열거해 취소가 빠진 상태다.
13. `src/execution/broker/kis_kr.py:1032` — `get_exchange_open_orders` 가 응답 행에서 **`rmn_qty`** 를 읽는데, 저장소의 `TTTC0084R` output 매핑(`chk_inquire_psbl_rvsecncl.py:21-43`)에 **`rmn_qty` 가 없다**(있는 것은 `psbl_qty`·`tot_ccld_qty`·`ord_qty`). 이행하면 `qty` 가 조용히 0 이 될 수 있다. *현재 유일한 소비자*(`scripts/run_trader.py:870-877`)는 `symbol`/`side` 만 보므로 즉시 위험은 아니지만, 이행 시 `psbl_qty` 로 바꾸거나 키 부재를 명시적으로 실패시켜야 한다.
14. `src/execution/broker/kis_kr.py:1000-1024` — 이 조회가 **연속조회를 하지 않는다**(CTX 빈 문자열 고정, 1회 호출). 저장소는 `inquire_psbl_rvsecncl.py:39` "한 번의 호출에 최대 50건" 이라고 명시한다. 미체결 50건 초과 계좌에서 목록이 잘리고, 이 목록은 ExitManager pending 만료 검증(이중 매도 방지)의 근거다 — **잘린 목록이 '미체결 없음'으로 읽히는 구조.** 이행과 함께 페이지 루프를 넣거나, 잘림을 `None`(판단 불가)으로 올려야 한다.
15. `src/execution/broker/kis_kr.py:233` — `_api_get` 에 요청 `tr_cont` 를 실을 수단이 없다(위 2-5).

**문서**
16. `src/utils/kis_rate_limit.py` docstring 의 '게이트웨이 한도 실전 20/s' 전제 — 저장소는 수치를 주지 않는다(`README.md:395-398`). 정정은 별도 문서 과제.
17. `CLAUDE.md` 의 KIS 주의사항 3개 항목(POST 재전송·`LEDGER_TR_IDS`·`tr_cont` 종료 판정)에 신TR 반영.

## 4. 되돌릴 수 있는 전환 방식

한 곳에서만 결정한다 — 새 추상화·새 설정 파일·새 클래스 없음.

```python
# src/execution/broker/kis_kr.py 모듈 상수
# KIS_TR_SET=new 로만 신TR. 기본 legacy(현행 그대로) — 되돌리기는 env 제거 한 줄.
_TR_NEW = os.getenv("KIS_TR_SET", "legacy") == "new"
_TR = {
    "buy":       "TTTC0012U" if _TR_NEW else "TTTC0802U",
    "sell":      "TTTC0011U" if _TR_NEW else "TTTC0801U",
    "rvsecncl":  "TTTC0013U" if _TR_NEW else "TTTC0803U",
    "daily":     "TTTC0081R" if _TR_NEW else "TTTC8001R",
    "cancelable":"TTTC0084R" if _TR_NEW else "TTTC8036R",
}
```
- 기존 `tr_id = "…"` 리터럴 5곳을 `_TR[...]` 조회로 바꾼다.
- `EXCG_ID_DVSN_CD` 는 **`_TR_NEW` 일 때만** 본문에 추가한다(구TR 수용 여부가 미확인이므로 현행 동작을 건드리지 않는다). 단 `_query_daily_fills` 의 기존 `"ALL"` 은 `_TR_NEW` 와 무관하게 이미 나가고 있으므로, 값 변경은 **별도 플래그 없이 한 번에 결정**하고 그 자체를 롤백 단위로 둔다.
- `LEDGER_TR_IDS` 에는 **신·구 TR 을 둘 다** 넣는다(양쪽 모드에서 원장 직렬화 유지, 롤백해도 한도 보호가 끊기지 않는다).
- 배포는 `/deploy-local` 의 자동 롤백 창 안에서, **조회 TR(4·5) 먼저 → 주문 TR(1·2·3) 나중**의 두 단계로 나눈다. 조회는 되돌리기가 싸고, 주문은 비가역이다.

`ponytail:` env 플래그 하나 — 모드별 어댑터 클래스나 설정 스키마는 만들지 않는다. 이행이 끝나면 플래그와 구TR 값을 함께 지운다.

## 5. 오프라인(fake HTTP) 검증 목록

`_api_get`/`_api_post` 를 가짜 세션으로 갈아끼우고 **실호출 0건**으로 확인한다.

1. **TR 매핑** — `KIS_TR_SET` 두 값 × 5 호출 경로 = 10 스냅샷. 헤더 `tr_id` 가 표 1 과 정확히 일치.
2. **본문 스냅샷** — 신 모드에서 order-cash 본문에 `EXCG_ID_DVSN_CD="KRX"`·`CNDT_PRIC=""` 가 있고, 구 모드 본문이 **현행과 바이트 단위로 동일**(회귀 없음).
3. **필드 누락 거부** — 신 모드에서 `EXCG_ID_DVSN_CD` 를 비우면 요청을 보내기 전에 실패(저장소 예제의 `ValueError` 와 같은 방향, `order_cash.py:99-100`).
4. **연속조회 요청 헤더** — 응답 헤더 `tr_cont` 가 `F → M → D` 로 오는 가짜 3페이지에서 요청 `tr_cont` 가 `"" → "N" → "N"` 인지(현재는 **전 페이지 미송신**이라 이 시험이 먼저 RED 가 된다 — 그게 정상이다).
5. **종료 판정** — `D`/`E` 에서 멈추고, `tr_cont` 가 빈 값/예상 밖이면 **완결로 처리하지 않는지**. 커서가 마지막 페이지에도 채워져 오는 응답(2026-09-15 실측 현상)에서 헤더 우선 판정이 유지되는지.
6. **취소 POST 재전송** — 429/500/502/503 가짜 응답에서 취소 본문이 **한 번만** 나가는지(`:858` 에 `retry=False` 를 넣은 뒤 RED→GREEN).
7. **미체결 조회 잘림** — `TTTC0084R` 가짜 응답이 50건 + `tr_cont="M"` 일 때 `get_exchange_open_orders` 가 잘린 목록을 그대로 반환하지 않고 페이지를 잇거나 `None` 을 돌려주는지.
8. **`rmn_qty` 부재** — `psbl_qty` 만 있는 신TR 모양 응답에서 `qty` 가 조용히 0 이 되지 않는지(키 부재를 명시적 실패 또는 `psbl_qty` 대체로).
9. **유량** — `LEDGER_TR_IDS` 에 신TR 을 넣었을 때 계좌 원장 TR 간 최소 간격이 유지되는지(기존 리미터 시험 확장).
10. **파서 계약** — `check_fills`(`main:…:1943`)가 읽는 `odno`·`tot_ccld_qty`·`avg_prvs` 세 키가 저장소의 신TR 컬럼 사전(`chk_inquire_daily_ccld.py:22-57`)에 **모두 존재**함을 고정하는 대조 시험.
11. **시계 고정** — 배포 창 verify 는 장 마감 후 벽시계라 세션 분기(`_order_tr_id` 의 `ORD_DVSN` 결정)를 건드리는 시험은 시계를 동결한다(프로젝트 기존 함정).

## 6. 실계좌로만 확인 가능한 항목 (오프라인으로 닫을 수 없음)

1. **구TR 에 `EXCG_ID_DVSN_CD`(특히 `"ALL"`)가 수용되는가** — 저장소에 사례 0건(Q27). 현행 `main:…:1868` 이 이미 그렇게 보내고 있으므로, **운영 응답 1건으로 즉시 확인 가능한 항목**이다(코드 변경 없이 로그/응답만 보면 된다).
2. **신TR 조회에서 `EXCG_ID_DVSN_CD` 미입력 시 처리**(Q28) — 저장소는 "미입력이 허용된다"는 코드 사실만 준다.
3. **신TR 응답의 실제 키 철자** — `cnc_cfrm_qty` 인가 `cncl_cfrm_qty` 인가(Q24). 저장소는 전자, 포털은 후자. **실응답 1건이 결정적.**
4. **`cncl_yn` 의 실제 값**(Q25) — 정상 주문에서 `"N"` 인가 빈 문자열인가.
5. **구·신 TR 응답 필드 집합이 같은가** — 저장소에 구TR 필드 목록이 없다. 특히 `check_fills` 가 읽는 3키와 `get_exchange_open_orders` 의 `rmn_qty`.
6. **`custtype` 미송신으로 신TR 이 정상 응답하는가** — 구TR 에서는 정상 동작 중이라는 운영 사실만 있다.
7. **`CTAC_TLNO`·`ALGO_NO`·`AFHR_FLPR_YN` 을 신TR 본문에 남겨도 수용되는가** — 저장소 9키에 없다.
8. **요청 헤더 `tr_cont` 없이 신TR 다음 페이지를 받을 수 있는가** — 구TR 에서는 받아 왔다.
9. **자동 매핑 여부** — 구TR 호출이 내부적으로 신TR 로 매핑되는지, 그때 `EXCG_ID_DVSN_CD` 까지 매핑되는지(Q30).
10. **`TTTC0084R` 의 모의투자 지원 여부** — 저장소 예제에 `env_dv` 분기가 아예 없다(부재가 '미지원'의 진술은 아니다).
11. **취소 본문 `ORD_QTY` 의 올바른 값** — 저장소가 세 갈래로 갈려 판정하지 않는다. 제품 안에서도 `main:…:848`(실수량)과 `requests.py:284`(`'0'`)로 갈려 있다.

## 7. 이 이행이 바꾸지 않는 것

- **취소 최종성은 그대로 미확정이다.** 신TR 로 옮겨도 `order_rvsecncl` 응답은 3필드뿐이고(`chk_order_rvsecncl.py:22-24`) 취소 확정 필드가 생기지 않는다.
- **조회 일관성·원장 cutoff·당일 지연 상한도 그대로다**(Q16~Q22). 신TR 응답에도 기준시각 필드가 없다.
- 따라서 **attach 설치 차단 사유는 이 이행으로 해소되지 않는다.** `trading_ready=False`, MODIFY 미지원, 보호 SELL 의 legacy 대비 열위는 불변이다.
- engine 브랜치의 `evidence.py`/`queries.py`/`requests.py` 는 **호출자 0건**이라 이 이행과 독립이다. 두 작업을 한 배포에 묶지 않는다.