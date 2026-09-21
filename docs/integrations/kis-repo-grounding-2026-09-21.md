# KIS 공식 저장소 기준 — 질문지 31문항의 답변 상태 (2026-09-21)

> 사용자 지시(2026-09-21): "Kis api는 https://github.com/koreainvestment/open-trading-api 여길 참고하고". 저장소 전체를 로컬에 받아 세 관점(REST 주문·조회 / 실시간 체결통보·웹소켓 / 제품 코드와의 한 줄씩 대조)으로 전수 조사하고 **적대적으로 검증**했다 — 주장 97건 중 확정 71·부분 25·근거 없음 1, 과장 12건 제거, 조사가 놓친 자료 10건 보충. 실 API 호출·자격증명·운영 로그 열람 0, pytest 0. 운영 이행 명세는 [구 TR → 신 TR 이행 명세](kis-tr-migration-spec-2026-09-21.md), 선행 문서는 [KIS 증거 좁히기](kis-execution-evidence-narrowing-2026-09-21.md).
>
> **결론:** 이 저장소는 요청·응답의 **모양(shape)** 을 고정해 주지만 **보장**(취소 최종성·수량의 누적 범위·조회 일관성·원장 cutoff)을 고정하지 못한다. KIS 자신의 앱 두 곳도 취소 ACK 하나로 확정 처리한다 — 관행이지 계약이 아니다. **attach 설치 차단 사유 1·2·12 는 이 조사로 바뀌지 않는다.** 남은 길은 실응답 1묶음(철자·`cncl_yn`·자식행 등재는 그것으로 닫힌다)이나 KIS 의 답변이다.

> 기준 자료: 사용자 지정 `koreainvestment/open-trading-api` 로컬 사본(main = `b4e6249714418aa57833d1cbbbced39cbcc5b125`, 2026-08-26 스냅샷, 2026-09-21 수령). 질문 원문은 `docs/integrations/kis-execution-evidence-narrowing-2026-09-21.md` ③절.
>
> **분류**
> - **A 저장소가 답함** — 저장소의 문장(docstring·주석·README)이 직접 답한다. 인용 가능.
> - **B 예제 동작으로만** — "공식 코드가 그렇게 한다"는 사실뿐. 보장이 아니다.
> - **C 저장소에 없음** — 문장도 코드도 답하지 않는다(부재의 기록이지 KIS 의 부정문이 아니다).
>
> **채택 열** — 사용자가 이 저장소를 기준 자료로 지정했으므로, 저장소의 예제 동작을 **제품 구현 기준으로 채택할 수 있는가**를 별도로 표시한다.
> - ✅ **채택** — 요청/응답의 *모양(shape)·프로토콜*이라 채택해도 돈 경로의 보장 범위가 넓어지지 않는다.
> - ⛔ **채택 금지** — 취소·체결의 **최종성**, 수량의 **누적 범위**, 조회의 **일관성·cutoff**. 저장소가 보장을 말하지 않으므로 예제 동작을 제품 판정 근거로 승격하면 *증거 없이 돈 경로를 여는* 것이 된다.
>
> **저장소 인용의 상한(모든 행에 적용)** — `README.md:8-10`: "샘플 코드는 … 참고용으로 제공되고 있습니다 · 별도의 공지 없이 지속적으로 업데이트될 수 있습니다 · 샘플 코드를 활용하여 제작한 고객님의 프로그램으로 인한 손해에 대해서는 당사에서 책임지지 않습니다." 이 세 줄 때문에 저장소의 **코드 동작**은 어떤 경우에도 경제적 보장으로 승격할 수 없다.
> 반대 방향의 근거도 있다 — `llms.txt:27` "Prefer `examples_llm/` for endpoint-level implementations." / `:30` "Follow request structures and parameter conventions demonstrated in the examples." 즉 **요청 구조·파라미터 규약에 한해서는 examples_llm 이 저장소의 현행 입장**이다. 이 두 문장이 위 ✅/⛔ 경계와 정확히 겹친다.

## A. 조회의 주문 범위 (Q1~Q6)

| Q | 질문 요지 | 분류 | 저장소 근거 | 채택 |
|---|---|---|---|---|
| Q1 | 정정/취소 자식이 `output1` 에 독립 행으로 등재되는가 | **C**(간접 단서만) | `examples_llm/domestic_stock/inquire_daily_ccld/chk_inquire_daily_ccld.py:24` `'orgn_odno': '원주문번호',` — 응답에 원주문번호 **필드가 있다**는 사실뿐. 등재 규칙 문장 0건 | ⛔ |
| Q2 | 자식 등재 시 원행도 함께 남는가 | **C** | 저장소 전수 grep 0건. `backtester/kis_backtest/providers/kis/brokerage.py:181-193` 이 정정 응답 `ODNO` 로 새 `Order(status=SUBMITTED)` 를 만들지만 **원 주문 행의 존속은 모델링하지 않는다**(B 수준 정황) | ⛔ |
| Q3 | 자식행 `tot_ccld_qty`·`tot_ccld_amt` 의 범위 | **C** | `chk_inquire_daily_ccld.py:22-57` 은 **한글 라벨만** 주고 설명 문장이 하나도 없다. 더 나아가 `:65 NUMERIC_COLUMNS = []` + `:115` 주석 "숫자형 컬럼 … (메타데이터에 number 자료형이 명시된 필드 없음)" — KIS 자신의 생성기가 **수량·금액 필드에 형식 선언조차 없다**고 적는다 | ⛔ |
| Q4 | 원행+자식행 합산 시 중복 집계 | **C** | 없음 | ⛔ |
| Q5 | `rmn_qty` 가 그 행 기준인가 체인 기준인가 | **C** | 없음. `strategy_builder/core/data_fetcher.py:610,628` 이 일별조회의 `rmn_qty` 를 `unfilled_qty` 로 바꿔 `>0` 필터에 쓰지만(B), 범위 진술은 없다 | ⛔ |
| Q6 | `취소확인수량` 이 확정 수량인가 요청 수량인가 | **C** | `chk_inquire_daily_ccld.py:41` `'cnc_cfrm_qty': '취소확인수량',` — 라벨뿐 | ⛔ |

## B. 취소 최종성 (Q7~Q12)

| Q | 질문 요지 | 분류 | 저장소 근거 | 채택 |
|---|---|---|---|---|
| Q7 | `rt_cd="0"` + "주문 전송 완료" 가 취소 확정인가 | **B**(관행만) | `strategy_builder/core/data_fetcher.py:705-709` `return {"success": True, … "message": "주문이 취소되었습니다"}` · `backtester/…/brokerage.py:144-146` `if resp.is_ok(): logger.info("주문 취소 완료: …"); return True` — **KIS 자신의 두 앱이 ACK 하나로 확정하고 즉시 추적에서 지운다.** 확정을 재확인하는 경로가 저장소에 0건 | ⛔ **핵심 금지 행.** 제품 운영 `main:src/execution/broker/kis_kr.py:858-870` 이 같은 모양이라는 대조일 뿐, 안전 근거가 아니다 |
| Q8 | 취소 접수 후 추가 체결 가능 여부 | **C** | `strategy_builder/backend/routers/orders.py:546,555` 이 취소 실패를 "취소 실패(체결 완료 추정)" 로 읽고 목록에서 지운다 — **코드 주석이 스스로 '추정'이라 밝힌다** | ⛔ |
| Q9 | 취소 확정을 판정할 필드/조합 | **C**(부재의 결합) | `chk_order_rvsecncl.py:22-24` 응답 output 은 `krx_fwdg_ord_orgno`·`odno`·`ord_tmd` **3필드뿐** — 취소 상태·확정 수량 필드가 없다 | ⛔ |
| Q10 | 통보 `CNTG_YN=1`+`RCTF_CLS=2`+`ACPT_YN=2` 가 취소 확정 통보인가 | **부분 A / 조합은 C** | A: `examples_llm/domestic_stock/ccnl_notice/ccnl_notice.py:25-29` "(14번째 값(CNTG_YN;체결여부)가 2이면 체결통보, 1이면 주문·정정·취소·거부 접수 통보입니다.)" · B: `legacy/Sample01/kis_domstk_ws.py:239` `'ACPT_YN',  # 접수여부(1 : 주문접수, 2 : 확인 )` · **C: `RCTF_CLS` 의 값 열거는 저장소 전체 0건**(라벨조차 `chk_ccnl_notice.py:28` '접수구분' vs `kis_domstk_ws.py:230` '정정구분'으로 갈린다) | ⛔. 덧붙여 제품 `src/data/feeds/kis_us_ws.py:30` 의 주석 `RCTF_CLS … (0=정상, 1=정정, 2=취소)` 는 **저장소로 확인 불가** — 질문지 Q10 의 전제 자체가 아직 무근거다 |
| Q11 | 확정된 취소 수량이 실리는 필드 | **C** | `ccnl_notice.py:70-76` 26칼럼에 취소확인수량 칼럼이 **없다**(수량 필드는 `CNTG_QTY`·`ODER_QTY` 둘뿐) | ⛔ |
| Q12 | 취소가능 목록 부재 = 취소 확정인가 | **C**(반대로 위험 근거만 추가) | A(다른 방향): `order_rvsecncl.py:44` = `inquire_psbl_rvsecncl.py:41` "※ 주식주문(정정취소) 호출 전에 **반드시** 주식정정취소가능주문조회 호출을 통해 정정취소가능수량(output > psbl_qty)을 확인하신 후 …" · B: `inquire_psbl_rvsecncl.py:39` "한 번의 호출에 최대 50건" + `:75-80` `if depth > max_depth: logging.warning("Max recursive depth reached."); return dataframe` — **잘린 목록과 빈 목록이 반환값으로 구분되지 않는다** | 선행 `psbl_qty` 조회 의무는 ✅ **채택**(제품에 없는 안전 처리). '부재 = 확정' 은 ⛔ |

## C. 다단 정정 (Q13~Q15)

| Q | 질문 요지 | 분류 | 저장소 근거 | 채택 |
|---|---|---|---|---|
| Q13 | 부분체결 주문의 잔여 정정 가능 여부 | **부분 A / 핵심은 C** | A: `order_rvsecncl.py:40` "단, 이미 체결된 건은 정정 및 취소가 불가합니다." · `:42` "※ 정정이 가능한 수량은 원주문수량을 초과 할 수 없습니다." · C: '이미 체결된 건'에 **부분체결이 포함되는지** 없음 | 상한 한 줄(입력 검증)은 ✅. 부분체결 해석은 ⛔ — MODIFY 미지원 유지 |
| Q14 | 2단 이상 체인에서 `orgn_odno` 가 어느 세대인가 | **C**(입력 출처만 A) | A: `legacy/Sample01/kis_domstk.py:112` `"ORGN_ODNO": orgn_odno,  # 주식일별주문체결조회 API output의 odno(주문번호) 값 입력` — **취소 입력의 정본이 일별조회 `odno` 라는 것**만 고정된다 | 입력 출처는 ✅. 세대 판정은 ⛔ |
| Q15 | `psbl_qty` 확인 ↔ 도달 사이 경합 | **C** | 없음. 저장소가 선행 확인을 요구하면서 경합 결과를 말하지 않는다 | ⛔ |

## D. 조회 일관성 (Q16~Q18)

| Q | 질문 요지 | 분류 | 저장소 근거 | 채택 |
|---|---|---|---|---|
| Q16 | 연속조회 페이지 집합이 동일 시점 스냅샷인가 | **C** | 없음(`inquire_daily_ccld.py:176-208` 성공 경로가 `rt_cd`·output·`tr_cont`·ctx 키만 읽고 시각 필드를 읽지 않는다) | ⛔ |
| Q17 | 페이지 전환 중 행 중복 가능성 | **C** | 없음 | ⛔ |
| Q18 | 페이지 전환 중 행 누락 가능성 | **C** | 없음 | ⛔ |
| — | *(부속) 연속조회 **규약*** | **A+B** | A: `legacy/Sample01/kis_domstk.py:275,278` `if tr_cont == "D" or tr_cont == "E": # 마지막 페이지 … elif tr_cont == "F" or tr_cont == "M":` · B: `inquire_daily_ccld.py:193-204` 응답 헤더에서 `tr_cont` 를 읽고 2페이지부터 요청 `"N"`, 커서는 본문 `ctx_area_fk100/nk100` · `kis_auth.py:428` `headers["tr_cont"] = tr_cont` | ✅ **채택**. 제품 `evidence.py:94-100`·`queries.py:185-186` 규칙이 이미 저장소와 같다 |
| — | *(경고) 공식 예제의 인자 밀림* | **B(결함)** | `inquire_daily_ccld.py:33-41` 시그니처 대비 `:201-204` 재귀 호출이 한 칸 밀려 2페이지부터 `CCLD_DVSN` 자리에 `pdno`, `INQR_DVSN` 자리에 `ccld_dvsn` 이 들어간다(`examples_user/domestic_stock/domestic_stock_functions.py:4297-4300` 동일) | **복사 금지.** "공식 샘플이 다페이지를 올바르게 돈다"는 전제는 성립하지 않는다 |

## E. 잔고와 체결의 대사 (Q19~Q23)

| Q | 질문 요지 | 분류 | 저장소 근거 | 채택 |
|---|---|---|---|---|
| Q19 | 잔고·체결 조회가 같은 원장 cutoff 를 공유하는가 | **C** | 없음. A(다른 방향): `inquire_balance.py:49` "당일 전량매도한 잔고도 보유수량 0으로 보여질 수 있으나, … 최종 D-2일 이후에는 잔고에서 사라집니다." — **표시 규칙이지 cutoff 가 아니다** | ⛔ |
| Q20 | 응답에서 기준시각을 확인할 필드 | **C**(부재 확인) | `chk_inquire_balance.py:22-71` output1/output2 50필드에 **시각·sequence 계열 0건** | ⛔ |
| Q21 | 3개월 이내 당일 조회의 지연 상한 | **방향만 A / 수치는 C** | A: `legacy/postman/실전계좌_POSTMAN_샘플코드_v2.6.json:886`(모의 `v1.6.json:1750` 동일) tr_id 설명 말미 "**\* 일별 조회로, 당일 주문내역은 지연될 수 있습니다.**" — 이 설명은 `TTTC8001R`/`CTSC9115R`/`VTTC8001R`/`VTSC9115R` **네 TR 을 모두 나열한 뒤** 붙는다. ⇒ **선행 문서가 '지연 경고는 3개월 이전 한정'이라고 좁혀 둔 것은 이 자료로 부정된다.** B: `strategy_builder/backend/routers/orders.py:40-43` "주문 직후 추가된 항목이 API 지연으로 사라지지 않도록 … `OPTIMISTIC_GRACE_SECONDS = 15`" | 지연이 **실재한다는 전제**로 fail-closed 를 유지하는 것은 ✅. **15초를 제품 대기시간의 근거로 승격하는 것은 ⛔**(임의의 대기시간으로 증명을 대신하는 것) |
| Q22 | 재시작 후 권장 조회 순서·재조정 절차 | **C** | 없음. A(부분·조건부): `inquire_psbl_order.py:43-44` "ORD_DVSN:00(지정가)는 종목증거금율이 반영되지 않습니다. 따라서 \"반드시\" ORD_DVSN:01(시장가)로 지정하여 …" — **단 `:46` 에 예외가 붙는다** "(다만, 조건부지정가 등 특정 주문구분(ex.IOC)으로 주문 시 가능수량을 확인할 경우 주문 시와 동일한 주문구분(ex.IOC) 입력하여 가능수량 확인)". B: `strategy_builder/core/order_executor.py:171-172` 은 매도 전 매도가능수량조회(`TTTC8408R`)를 **호출조차 하지 않는다**(저장소 전체 사용처: `examples_user/domestic_stock/domestic_stock_examples.py:656` 과 전용 chk 파일뿐) | ⛔. `:46` 예외 때문에 "제품의 `ORD_DVSN=00` 조회가 저장소 경고와 정반대" 라고도 단정할 수 없다 |
| Q23 | 끊긴 구간 통보의 재전송 | **C** | `kis_auth.py:715-731` 재연결은 1초 대기 후 **전 구독 재등록뿐**, 갭을 REST 로 메우는 경로가 없다. `:660` `max_retries: int = 3` 이고 성공 시 `retry_count` 를 초기화하지 않아 **수명 전체에서 3회 뒤 조용히 종료**된다. B(채택 가능): `kis_auth.py:700` `await ws.pong(raw)` — PINGPONG 되돌리기 | 프레임 읽기·PINGPONG·재구독 골격은 ✅. "통보 누락을 무시해도 된다"는 ⛔ |

## F. 필드 표기 불일치 (Q24~Q26)

| Q | 질문 요지 | 분류 | 저장소 근거 | 채택 |
|---|---|---|---|---|
| Q24 | `cncl_cfrm_qty` 인가 `cnc_cfrm_qty` 인가 | **B(표기만) — 충돌 미해소** | 저장소는 `cnc_cfrm_qty` 한 철자뿐이고 `cncl_cfrm_qty` 는 **repo-wide 0건**. 출처는 `chk_inquire_daily_ccld.py:41` 과 그 기계 파생본 `MCP/KIS Code Assistant MCP/data.csv:3140` **두 곳**(`MCP/Kis Trading MCP/configs/domestic_stock.json` 에는 `column_mapping` 키 자체가 0건 — '세 곳 일치'가 아니다). 포털 가이드·응답예제는 반대 철자 | **두 표기를 모두 수용**하는 파싱은 ✅(모양 문제, 값 검사는 그대로). 어느 쪽이 정본인지 단정은 ⛔ — 실응답 1건이 여전히 결정적 |
| Q25 | `cncl_yn` 이 정상 주문에서 항상 `"N"` 인가 | **C** | `chk_inquire_daily_ccld.py:36` `'cncl_yn': '취소여부',` 라벨뿐, 값 집합 정의 0건 | 현행 `Y/N` 강제(위반 시 행 거부)는 fail-closed 이므로 ✅ **유지**. **빈 문자열을 허용하도록 완화하는 것은 ⛔**(근거 없이 수용 범위를 넓힌다) |
| Q26 | `output2` 라벨 회전이 맞는가 | **B(포털 단독 오타가 아님만 확인)** | `chk_inquire_daily_ccld.py:58-62` 이 같은 dict 안에서 `'tot_ccld_amt': '총체결금액'`(:37)을 `'tot_ccld_amt': '매입평균가격'`(:60)으로 **덮어쓴다**. `prsm_tlex_smtl='총체결금액'`·`pchs_avg_pric='추정제비용합계'` 회전도 동일 | ⛔(어느 쪽이 실제 의미인지 미확정). 제품은 output2 미사용이라 현재 영향 0 |

## G. TR 전환과 거래소 (Q27~Q31)

| Q | 질문 요지 | 분류 | 저장소 근거 | 채택 |
|---|---|---|---|---|
| Q27 | 구TR `TTTC8001R` 이 `EXCG_ID_DVSN_CD`(특히 `"ALL"`)를 수용하는가 | **C** | 구TR 에 이 필드를 실은 사례가 저장소에 **0건**. 오히려 `strategy_builder/core/data_fetcher.py:568-584` 는 구TR 을 **이 필드 없이** 호출한다(B). ⇒ 제품 `main:src/execution/broker/kis_kr.py:1868` 의 `TTTC8001R + "EXCG_ID_DVSN_CD":"ALL"` 조합을 저장소가 **뒷받침하지 않는다** | ⛔ |
| Q28 | 신TR 조회에서 미입력 시 처리 | **C** | `inquire_daily_ccld.py:44,173-174` `excg_id_dvsn_cd: Optional[str] = "KRX"` / `if excg_id_dvsn_cd is not None: params["EXCG_ID_DVSN_CD"] = …` — **미입력이 허용된다는 코드 사실**뿐, "미입력시 KRX" 문장은 없다 | 신TR 에 **명시적으로 `"KRX"` 를 보내는 것**은 ✅(예제 기본값 = 범위를 좁히는 방향). 미입력 동작에 기대는 것은 ⛔ |
| Q29 | SOR 체결을 어느 값으로 조회하는가 | **C** | `inquire_psbl_rvsecncl.py:84-91` 취소가능조회에는 **거래소 요청 파라미터 자체가 없고**, 응답에만 `excg_id_dvsn_cd`/`_name` 이 실린다(`chk_inquire_psbl_rvsecncl.py:39-40`) | ⛔ |
| Q30 | 구TR 지원 종료 일정·고지 정책 | **C**(현행 입장의 방향만 A) | A: `llms.txt:27` "Prefer `examples_llm/` …" + examples_llm 이 **전부 신TR**. 내부 근거: `docs/convention.md:76` "코드가 변경되면 주석을 변경" — 즉 `inquire_daily_ccld.py:55` 의 `CTSC9115R` 잔존과 `order_cash.py:40` 의 `TTC0802U` 는 **의도된 이중 표기가 아니라 저장소 자체 규약 위반(드리프트)** 이다. 반대 정황(B): `strategy_builder`·`backtester` 는 2026-08-26 시점에도 구TR 을 쓴다 | **요청 구조·TR 값을 신TR 로 맞추는 것은 ✅**(llms.txt:30 이 정확히 이 범위를 지시). "구TR 이 계속 동작한다"는 결론은 ⛔ |
| Q31 | 실전 1계좌당 초당 18건이 유효한가 | **C** | `README.md:395-398` 은 `EGW00201` 을 언급하면서 **수치를 주지 않는다**. 유일한 페이싱은 `kis_auth.py:57 _smartSleep = 0.1` 이며, `:138 changeTREnv` 에 `global _smartSleep` 선언이 없어(`:141` 은 `global _isPaper` 뿐) `:146/:151` 의 0.05/0.5 대입이 **지역변수**가 된다 — 실효값은 항상 0.1. `EGW00215` 는 저장소 전체 0건 | ⛔. 제품 `MAX_RPS = 10`·`LEDGER_TR_IDS` 는 전적으로 **제품 실측** 위에 있고 저장소가 이를 지지도 부정도 하지 않는다 |

## 요약

- **A(저장소가 문장으로 답함)**: Q13 상한 한 줄, Q10 의 `CNTG_YN` 정의, Q12 의 *선행 조회 의무*, Q21 의 *지연 방향*, Q14 의 *입력 출처*, Q19 의 *D-2 표시 규칙* — **전부 부속 사실이고, 질문의 핵심에는 하나도 답하지 않는다.**
- **채택 가능(✅)한 것은 모두 요청/응답의 모양과 프로토콜**이다: 신TR TR-id·필수 필드, 연속조회 tr_cont 규약, 취소 전 `psbl_qty` 선행 조회, 통보 프레임 분해·PINGPONG, 취소확인수량 두 철자 수용.
- **채택 금지(⛔)는 Q1~Q11·Q15~Q22·Q26·Q29·Q31** — 취소 최종성, 수량 누적 범위, 조회 일관성, 원장 cutoff, 유량 한도. 이 저장소는 **shape 을 고정해 주지만 보장을 고정하지 못한다.**
- 따라서 **attach 설치 차단 사유는 이 조사로 바뀌지 않는다.** 저장소를 기준 자료로 채택해도 `trading_ready=False`·MODIFY 미지원·보호 SELL 열위는 그대로다.