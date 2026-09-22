# QWQ AI Trader - CLAUDE.md
> **2026-09-22 밤 P1 Plan 완료 — Do 착수는 사용자 결정 대기(제품 0줄):** 보호 SELL 의 main 동등. 조사 3 → 설계 → 심사 2관점 **REVISE ×2**(must-fix 16 — intent 굽기 순환·EOD 의 면제 우회·시간외 세션의 `'05'` 주문·`ma5/prev_low` 경로 부재 등) → 처분 전부 확정(`docs/superpowers/plans/2026-09-22-p1-protective-sell-parity.md` §3). **설계의 중심 = 보호 SELL 을 `regular` 세션에서 시장가로 내서 취소·3분류·폴백을 작성하지 않는다**(취소 최종성 미상 아래 유일한 길 — 두 심사 모두 논지 수용). **§4-1 시장가 전환은 매매 행동을 바꾸는 돈 경로 결정이라 사용자 승인 필요**(추천: 승인, 대가 = 스프레드 1틱·저유동 종목 슬리피지). 그 밖의 결정(closing 지정가 강등·달력일 보유기간 미이식·F8 ① P2 이관)은 보수적 기본값. 새 차단 사유 후보 24~27. 아래 줄은 P0-4 기록이다.
> **2026-09-22 밤 P0-4 완료 — 래치 설정 조건 축소·예약/세션 계약 고정·stale pending attach 가드(engine 브랜치, 제품 변경 2곳: `runtime.py` admit 한 곳·`kr_scheduler.py` 조기 return 1줄(legacy 바이트 동일)·운영 미설치):** 결정 문서 §4 약한 지점 2·4·7·9. **설계 초안의 "래치 해제 = 저장 행 재개"는 심사 2관점(REVISE ×2)이 게이트 개방으로 판정해 폐기**(아무도 받지 않는 보호 결정 커밋 → 손절이 조용히 사라짐·낡은 증거로 BUY 개방·교차 종목 fail-open) → S-A 는 `admit` 의 사전 거부(중복 접수 포함)가 래치하지 않게만, **해제 경로 0(의도) → 새 차단 사유 23**(durable 보호 접수 행 해제 불가 + BLOCKED rollover 한 번이 `_day_closed` 영구 래치 — 첫 런타임 재현). 항목 4 는 **예약 산술이 정상**이라 결정 문서 정정(봉쇄는 side 무관 종목 잠금 + 만료 계약 부재 → **차단 22**). 항목 7 은 계약 고정(재준비 소유자 = 생산자, next_market 은 `ORD_UNPR` 로만 구분). 항목 9 는 attach 조기 return. 구현 3 ∥ 독립 재현 3(CHANGES_REQUIRED 2·APPROVE 1 — 전부 제품 0줄 처분, 생존 변이 3종 kill) → **Codex 17차 APPROVE**, 전체 UTC/KST 각 **5116 passed**/기존 xfail2·경고4·격리0. **사용자 확인 필요: main 별도 PR 후보 M1**(전량 청산 pending 의 "취소 0건=소멸" 해석이 미확인 가정 의존 — 이 worktree 에서 main 본문 미검증). **다음 P1:** 보호 SELL 의 main 동등 — owner quote 생산자(차단 21)·취소 뒤 3분류·F8 ①/late-fill/자식 가드·closing 처리·래치 해제(23)를 **한 설계**로, 적대적 심사 2관점. 정본 `docs/superpowers/plans/2026-09-22-p0-4-latch-reservation-session-stale.md`(§3 처분·§5 Do·See·§5-4 마감). 아래 줄은 저녁 기록이다.
> **2026-09-22 저녁 P0-3 완료 — 생산자 배선·BUY 생존 게이트·sync 읽기 전용 관측·live writer 가드(engine 브랜치 `80571eb`, live 파일 3개 수정이나 미설치 경로 바이트 동일·설치기 제품 호출자 0건·운영 미설치):** `install_attached_runtime(…, collect)` 가 attach 직후 `start_reconciler` 를 배선(구간 1 거부 3종 추가) → 설치 차단 사유 **18 설치기 기준 닫힘**. 자동·수동 BUY 는 `reconciler_live`(주기 task 생존·굶지 않음·**오늘 주기 완주 ≤15초**·대상 있으면 오늘 조회 완료 ≤15초, 기동 시각 폴백 없음) 없이는 `reconciler_unavailable`, SELL·CANCEL 통과 — 그 대가로 주기는 대상 0 이어도 3초마다 돈다(조회 0회). `_sync_portfolio` attach 분기(재시도 방어 뒤 읽기 전용 대조·불일치 알람) → **17 닫힘**. `monitor_positions`·`_check_exit_signal` attach 전면 skip → **새 차단 사유 21: attach 에는 보호 청산 구동기가 없다**(owner quote 생산자 0건 조사 확정 — P1 보호 SELL main 동등에서 닫는다), **20**(`entry_policy_context` 재게시자 0 → 10C). SELL `exit_type`(`src/utils/exit_types.py`)·BUY `name`. 독립 재현 둘 다 CHANGES_REQUIRED → 처분(변이 7종 kill), **Codex 15차 P1 2·P2 1 → 2건 수정·1건 결정 유지(미배선 통과 — 설치기가 유일한 attach 경로) → 16차 APPROVE**, 전체 UTC/KST 각 **5086 passed**/기존 xfail2·경고4·격리0. **시계 창 정정:** cross_validator 09:00~10:30·12:30~13:00 이 프로세스 지역시각에 걸린다 — UTC 전체 suite 는 18:00~19:30 KST 회피. Toss 관측 서비스는 18:00:02 KST 승인 만료로 정상 종료(조치 없음). **다음 P0-4:** `_protection_failed` 해제·부분 체결 예약 감소·세션 경계 소멸·`_cleanup_stale_pending` owner 경로. 실계좌 스모크(계획서 §5)는 사용자 확인 대기. 정본 `docs/superpowers/plans/2026-09-22-p0-3-wiring-sync-gate.md`(§3 처분·§6 Do·See·§7 마감). 아래 줄은 오후 기록이다.
> **2026-09-22 오후 P0-2 완료 — attach 의 체결·종결 증거 생산자(engine 브랜치 `7500d3c`, 부품·제품 호출자 0건·운영 미설치):** 사용자 전략 결정(원문 "엔진 전체를 옮기도록 하자") = **attach 전환**. `runtime.py::start_reconciler` 주기 task 가 미해결 attempt 가 있을 때만 `TTTC0081R`·`ALL` 조회 → 파서(2단계·`chain`·거래소 대조) → `reconcile` → 저장 상태 재처리 → `apply_execution_observation`. 마감시각 기반 예산(주기 60/조회 45/B2 유보 15), shield 된 owner task + drain, `_closing` 자연 종료. TR 은 `TTTC0081R` 고정(스키마 검증으로 종결하지 않는다). 설계는 Codex BLOCK(must-fix 10) → 개정 → REVISE ×2 → 처분 P-1~P-13 확정, 구현은 독립 재현 2회 + **Codex 11차 P1 3 → 12차 2 → 13차 1 → 14차 APPROVE**, 전체 UTC/KST 각 **5057 passed**/기존 xfail2·격리0. 설치 차단 사유 18 은 **부품 완성**(닫힘 아님 — 배선은 P0-3), **새 사유 19**(체결마다 전 종목 명령 정지 창). **다음 P0-3(live 파일):** `start_reconciler` 배선·기동 순서·환경 거부 · 게이트 F8(side 인지형) · `_sync_portfolio` attach 분기 · SELL `exit_type` · 실계좌 스모크(D10 기록기 — 사용자 확인 대기). 정본 `docs/superpowers/plans/2026-09-22-p0-2-fill-evidence-producer.md`(§5 확정 설계·§7 마감). 아래 줄은 오전 기록이다.
> **2026-09-22 P0-1 완료 — attach 송신 경로가 킬스위치·감사 원장을 거친다(engine 브랜치, 부품·제품 호출자 0건·운영 미설치):** `src/execution/safety/transport.py::send_prepared` 가 최종 guard 뒤·POST 앞의 동기 구간(새 await 0)에서 `kill_switch.check`(SUBMIT/MODIFY, CANCEL 제외)와 `EV_SUBMIT`/`EV_CANCEL` → 응답 뒤 `EV_ACCEPT`/`EV_REJECT` 를 한 시도당 한 번(필드에 `attempt_id`·`fingerprint`, 계좌·hashkey·토큰·헤더 없음). 설치 차단 사유 16 은 **legacy 와 같은 best-effort 수준까지** 닫힘. **`tests/conftest.py` 전역 fixture** 가 킬스위치 플래그·감사 원장 경로를 테스트마다 tmp_path 로 돌린다 — 격리 가드가 막아도 두 모듈이 OSError 를 삼켜 시험 안에서 킬스위치가 항상 "꺼짐"이던 실제 결함. 독립 재현 APPROVE(P2 6 처분, m7 "검사를 함수 상단으로" 생존 → 런타임 시험으로 kill), Codex 10차 APPROVE(P0/P1/P2 0), 전체 UTC/KST 각 **4993 passed**/기존 xfail2·격리0. **설치 전제 등록: 킬스위치는 플래그 디렉터리 접근 실패 시 fail-open(현행도 동일) — 설치 시 `~/.cache/ai_trader` 기동 점검.** 다음은 P0-2(attach 의 체결·종결 증거 생산자 — 설계 필요) 또는 사용자의 전략 결정(결정 문서 §6). 아래 줄은 오늘 오전 기록이다.
> **2026-09-22 임계 경로 변경 — KIS 증거가 아니라 attach 의 격차(engine 브랜치, 제품 0줄):** 사용자 지시("답변이 없으니 우리가 임의로 판단해서… 제일 우리에게 이득이 되는 방향으로")로 **KIS 질문지 31문항을 우리가 판단해 확정**했다 — 전부 "어느 답이 참이어도 안전한 쪽": 취소 최종성은 알 수 없다·공통 cutoff/지연 상한은 없다·MODIFY 영구 미지원·KR 체결통보는 붙이지 않고 REST 가 정본. **KIS 의 답을 기다리는 것을 다음 행동으로 제시하지 않는다.** 정본 **`docs/superpowers/plans/2026-09-22-kis-judgement-decisions.md`**(문항별 판단표·설계 결정 D1~D10·순서). 같은 작업의 적대적 심사(① BLOCK·② REVISE → 처분)가 **KIS 답과 무관한 새 설치 차단 사유 3건**(인계 표 16~18)을 찾았다: **attach 의 주문 POST 가 킬스위치·감사 원장을 우회한다**(`transport.py` 직접 POST — coordinator 확인) · **30초 `_sync_portfolio` 에 attach 분기가 없다**(coordinator 확인) · **attach 에는 체결·종결 증거 생산자가 0건**(매수 체결이 포지션이 되지 않아 손절 신호가 생기지 않는다). attach 가 현행보다 약한 지점 10곳 — 09-21 밤 main 의 PR #81·#83·#84 로 기준선이 올라갔다. **순서: 전제(P0-1 킬스위치·감사 원장 → P0-2 증거 생산자 → P0-3 sync 분기 → P0-4)가 게이트를 여는 변경(D4·D6)보다 먼저. 다음 구현 단계는 P0-1.** 운영(main): PR #80 은 09-21 밤 다른 세션이 사용자 지시로 병합·배포(`KIS TR 세트: legacy`, 전환 안 함), 후속 **PR #88**(연속조회 요청 `tr_cont` 헤더)은 verify·독립 재현·Codex 9차 통과 뒤 **사용자 지시로 09-22 07:24 KST 병합(`d337494`)·배포**(개장 전, verify 1982 passed·자동 롤백 없음, PID 1546587, `KIS TR 세트: legacy`, 사후 점검 KIS 오류·ERROR 0 — 운영 checkout 은 detached 로 남음). ⚠️ main 의 `engine.py`·`kr_scheduler.py` 가 밤사이 크게 바뀌어, engine 에 main 을 들이는 날 S4-0 특성화 26건의 "현행 결함 그대로" 단언이 뒤집힌다. 아래 줄은 09-21 저녁 기록이다(그 안의 "PR #80 미병합"은 과거 시점).
> **2026-09-21 저녁 KIS 공식 저장소 기준 정합(engine 브랜치 부품 + main 무동작 PR #80 — 운영 미설치·미병합):** 사용자 지시로 KIS 기준 자료를 **공식 저장소 `koreainvestment/open-trading-api@b4e6249`** 로 고정했다. 전수 조사·적대적 검증 결과 **저장소가 고정하는 것은 요청·응답의 모양뿐이고 취소 최종성·누적 범위·cutoff 는 저장소도 말하지 않는다 — 설치 차단 사유 1·2·12 불변**(상태표 `docs/integrations/kis-repo-grounding-2026-09-21.md`). engine: 증거 파서가 취소확인수량의 두 철자를 받고(어긋나면 malformed — 허용 원시 스키마가 넓어졌고 종결 조건식은 그대로) 수집기를 신 TR(`TTTC0081R`+`EXCG_ID_DVSN_CD="KRX"`/`TTTC0084R`)로, `LEDGER_TR_IDS` 에 신 TR 2종. **chain 판정은 조회한 거래소 범위에 의존한다 — KRX 만 보면 NXT/SOR 자식행이 안 보여 최종성 인정이 더 쉬워지는 fail-open 방향**(docstring 경고, Q27·Q29). main: **PR #80 `fix/kis-tr-id-migration-switch`** — `KIS_TR_SET` 기본 `legacy` 에서 요청이 바이트 단위로 같고, `new` 에서도 신 TR·신 본문은 정규장 접수에만. **전환 전 미해결: 취소·정정이 주문의 접수 세션을 모른 채 `"KRX"` 를 붙인다**(실계좌로만 확인). 독립 재현 둘 다 CHANGES_REQUIRED → 보강, Codex 8차 P0/P1 0·P2 2 → 처분. **결정: 취소 POST 의 재시도는 유지한다**(전량 취소는 두 번 닿아도 노출을 만들 수 없고 재시도를 빼면 보호 취소 성공률만 떨어진다 — 고칠 것은 "취소는 멱등"이라는 근거 없는 문장). 구현 상태 정본은 `docs/integrations/kis-tr-migration-spec-2026-09-21.md` §8. **PR #80 병합·배포·재시작·전환은 사용자 확인 뒤, 장 마감 후에만.** 아래 줄은 10A3b-1 마감 당시 기록이다.
> **2026-09-21 10A3b-1 완료 + KIS 증거 좁히기(engine 브랜치, 부품 — 운영 미설치):** 사용자 위임으로 10A3b 에 앞선 여섯 결정을 전부 보수적 기본값으로 확정(`docs/superpowers/plans/2026-09-21-10a3-install-preparation.md` §3-1). ① **`docs/integrations/kis-execution-evidence-narrowing-2026-09-21.md`** — 공개 공식 자료 재조사를 적대적으로 검증(닫힌 계약 행 0개), **KIS 에 그대로 보낼 질문지 31문항**·비식별 응답 수집 체크리스트. 설치 차단 사유 1·2·12 는 그 답이나 승인된 비식별 실응답으로만 열린다 — **사용자가 할 수 있는 다음 행동**이다. ② `src/execution/safety/factory.py` 에 **거부형 설치기 `install_attached_runtime`**(설치 순서와 명명된 거부 16종, 거부는 전부 live 를 건드리기 전·attach 는 마지막 인접 두 줄, **제품 호출자 0건 — 제품에는 그것을 통과시킬 checkpoint 를 만드는 코드가 없어 운영에서는 항상 거부로 끝난다**). 전체 UTC/KST 각 **4950 passed**/기존 xfail2·격리0, 독립 재현 APPROVE, Codex 7차 P0 0·P1 2·P2 1 → 처분. 코드로 확정: **UNKNOWN 은 `reconcile` 로도 풀 수 없다**(`order_ref` None 이라 첫 검사에서 탈락) · 증거 파서와 수집기의 TR 불일치·필드 철자 불일치(fail-closed) · 성공한 추세 갱신이 뒤에 거는 VIX 갱신이 추세 판단을 stale 로 만든다(10C 배선 계약). **운영(main)에 해당: 제품의 KIS 주문·취소·조회 TR 5종이 전부 "구TR"**(자동 매핑 중·삭제 일정 없음 — 작업 칩으로 분리). 미룬 것(실제 설치 단계): 시계 주입·attach 인지형 관측·사이징 표 상수화·store 의 읽기 전용 개방. 아래 줄은 10A3a 마감 당시 기록이다.
> **2026-09-21 10A3a 완료(engine 브랜치, 부품·인수 — 운영 미설치, 차단 사유는 하나도 닫지 않음):** 신규 `src/execution/safety/factory.py`(로드된 `RiskConfig` 인스턴스에서 owner 정책을 만드는 builder·`config_version` 5축 어댑터·`PolicyContext` publisher — **기존 제품 파일 0줄·제품 호출자 0건**)와 owner 게이트 단독 결정·누적 정합 인수 11건. 전체 UTC/KST 각 **4917 passed**/기존 xfail2·격리0, 독립 재현 둘 다 APPROVE, Codex 6차 P0/P1 0. 10A3 Plan 의 적대적 심사(must-fix 13)가 **설치 factory 본체는 증거 계약·사용자 결정 없이는 명세할 수 없다**를 보여 범위를 나눴다 — **10A3b(설치 factory·관측·시계 주입)는 `docs/superpowers/plans/2026-09-21-10a3-install-preparation.md` §3 의 사용자 결정 6건과 공식 KIS 증거를 기다린다.** 확정 사실: UNKNOWN 인 SUBMIT 1건은 **보호 SELL·CANCEL 까지** attach 의 모든 새 명령을 멈춘다 · 런타임 `add_exit_exempt` 는 이중 실패(다음 명령 거부 + 이후 게시에서 면제 소실) · `runtime.restore()` 는 live 포트폴리오를 저장본으로 덮어쓴다 · 일자 전환 없이는 아침 재기동 설치 불가. 아래 줄은 S5 마감 당시 기록이다.
> **2026-09-21 B2/B3 완료(engine 브랜치, S1~S5 전부 한정 승인·운영 미설치):** S5 는 **제품 수정 0** — S3·S4 시험에서 아무것도 물려받지 않은 독립 인수 37건(`tests/test_execution_signal_gateway_acceptance.py`: 실제 `risk/manager.py` 게이트·CV·RegimeOwner·팩터 버킷, POST 는 전선 본문으로·차단은 분기 도달 증거로 단언)과 Codex 5차 최종 broad 리뷰(네 결정 모두 방향 확인, 지적은 문서 정밀화뿐). 전체 UTC/KST 각 **4872 passed**/기존 xfail2·격리0. **인수 GREEN 은 설치 승인이 아니다** — 제품 attach 호출자 0건·`trading_ready=False`·MODIFY 미지원 그대로이고, **attach 설치 전에 닫아야 할 차단 사유 15항이 `docs/operations/claude-migration-handoff-2026-09-20.md` 에 모여 있다**(취소·체결 최종성 증거 부재로 보호 SELL 이 legacy 보다 덜 안전 · 가격 없는 SELL 은 attach 에서 거부 · UNKNOWN ACK 1건이 자동 매수 경로 전체를 멈추고 해제 수단의 제품 호출자 0 · CV 의 판단 시각은 벽시계 · 면제 추가 경로·교체 상한이 owner 밖 등). 다음은 공식 KIS 증거(취소·체결 최종성) → 10A3 factory(정책·설정 출처 일원화, `recover_unsent()` 호출, `_exit_exempt_ref` 주입, CV clock 주입) → 10C. 아래 줄은 S3·S4 당시 기록이다.
> **2026-09-21 B2/B3 진행(engine 브랜치):** S1(불변 판단 사실·final kernel 재검사)·S2(실제 qualification publisher 부품)·**S3(SIGNAL→gateway→owner prepare/dispatch)** 모두 **한정 승인·운영 미설치**. S3 가 만든 것: 미송신 시도의 원자적 종료(`abandon_candidate`)·claim 이전 실패의 사유 보존·`safety/gateway.py`(단일 송신로)·`engine.py` 의 attach 분기(H1~H7, +98/−1, 추가 실행 줄은 전부 attach 가드 안). **제품에 `KRExecutionRuntime` 생성·`attach()`·`install_gateway()`·`recover_unsent()` 호출자는 0건**이고 `trading_ready=False`·MODIFY 미지원 그대로다 — 설치가 아니다. HEAD `10d2ca7` 전체 UTC/KST 각 4758 passed/기존 xfail2·격리0. Codex 1차 APPROVE·2차/3차 CHANGES_REQUIRED → 처분(3차 처분의 Codex 재확인은 S5 범위). **S4 도 완료(한정 승인·운영 미설치, 전체 UTC/KST 각 4835 passed, Codex 4차 APPROVE):** legacy 세 경로(90초 SELL 폴백·10분 BUY 정리·eviction)의 첫 특성화, 미claim 자식 명령의 종료, eviction 의 owner 경로. **S4 는 범위를 줄였다 — 취소 최종성 증거가 제품에 없어 attach 모드에는 미체결 SELL 의 시장가 에스컬레이션과 미체결 BUY 의 타임아웃 취소가 없다. 보호 SELL 에 관해 attach 는 legacy 보다 계속 덜 안전하며 이것이 attach 설치의 차단 사유다**(여는 전제: 취소·체결 최종성의 증거 계약 = 공식 KIS 증거·10A2/10C). 결정·실제 인터페이스는 `docs/superpowers/plans/2026-09-21-s4-owner-path-restoration.md`. 다음은 S5(독립 실큐 인수·최종 broad 리뷰 — 원장의 "S5 진입 조건") → 10A3(factory·`config_version` 5축·`recover_unsent()` 호출·`_exit_exempt_ref` 주입 재현·attach 인지형 대시보드). **리뷰·인계는 `docs/reviews/b2b3-stage-ledger-2026-09-20.md`(단계 원장)부터**, 계약은 `docs/superpowers/plans/2026-09-20-b2b3-request-bound-qualification.md`, S3 의 결정·실제 인터페이스는 `docs/superpowers/plans/2026-09-21-s3-signal-gateway.md`. 이 호스트는 운영 서버(2 vCPU·3.8GB·스왑 100%)이고 다른 세션과도 공유된다 — pytest 에이전트 동시 ≤2, 전체 suite 는 **읽기 전용 에이전트도 없이** 단독 직렬, live 파일을 만진 단계는 지정 파일 GREEN 만으로 통합하지 않는다. main/운영·배포·재시작·주문·설정·Toss grant 무변경.
> **2026-09-20 추가 정리 완료:** 상시 개발선은 main·`feature/engine-safety-design-20260917` 두 개다. 퇴역 local54/remote7/worktree53(dirty28/staged12)을 복구 가능한 archive로 전환했다. 이전 worker 원경로를 사용하지 않는다. 보관소·복구 검증은 `docs/reviews/retired-workspace-archive-2026-09-20.md`, 다음 Claude 시작점은 `docs/operations/claude-migration-handoff-2026-09-20.md`다. 제품 소스/운영 무변경·전체 엔진 승격 차단 유지. 아래 보존 개수/경로는 이전 시점 기록이다.
> **2026-09-20 브랜치 정리·Claude 인계:** 운영 기준은 main, 개발 정본은 `feature/engine-safety-design-20260917`(C4 source `ab044c4`)다. 병합 완료 local166/remote67/clean worktree83개 정리, dirty·고유 이력·증거 보존. root checkout을 `8ff2f55→465a029`로 동기화했고 기존 거래 코드·PID3534327/Toss PID3335469·보호7경로는 불변, 재시작0. 엔진 전체는 미병합·운영 미승격이며 다음은10B2/B3 request-bound qualification/최종 sizing이다. 새 세션은 **`docs/operations/claude-migration-handoff-2026-09-20.md`**부터 읽는다. 아래 운영 SHA/PID는 과거 시점 기록이다.
> **현재 운영 상태 — 2026-09-17 21:49 KST:** 사용자가09/18·21·22/09/22 18시만료를 확정했고, 별도 `qwq-toss-observer.service`를21:47:37 ON(PID3335469, release877768e)했다. 초기 발급 generation1/ready·단일 sender·장외idle/표본0. 기존 거래 봇 PID3274983/checkout8ff2f55/설정7경로 그대로, 재시작0. 아래 미설치/일정대기 문구는 이전 인계 이력이다. **observer 같은 grant 재시작·토큰/영수증 삭제 금지**, 장애 시 새 서비스만 중단하고 상태를 보존한다. 정본: `docs/reviews/toss-observer-service-2026-09-17.md`.
> 2026-09-17 21:34 KST: 독립 관측 서비스 PR #74는 required CI1809 passed/기존xfail2 후 원격 main `c2fe787`에 병합됐다. **기존 거래 봇 로컬 checkout8ff2f55/PID3274983 유지, 새 서비스 설치/ON 미실행**. 새 관측 날짜09/18·21·22와09/22 18:00 KST 만료는 아직 사용자 답변 대기이며 자동 연장하지 않는다. 최종 artifact/CI/운영 대기 근거는 `docs/reviews/toss-observer-service-2026-09-17.md`.
> 2026-09-17 관측 서비스 후속: 사용자가 **기존 거래 봇 유지·별도 Toss 서비스 ON·단일 발급 주체·추가 KIS 조회0·주문 무영향** 상세 설계를 승인했다. 격리 병렬 구현·독립 broad/한정 리뷰(C/I/M0), UTC/KST 각각1809 passed/기존xfail2를 완료했다. CI/main 병합은 PR #74에서 확인하며 **운영 설치/ON은 아직 미실행**이다. 첫 관측일 경과로 새 일정 사용자 확인이 남았다. 현재 상태 정본은 `docs/reviews/toss-observer-service-2026-09-17.md`. 보유 종목만 관측하며 근거 없는 KIS 가격 비교는 제외한다. 기존 거래 봇 checkout/설정/PID는 변경하지 않는다.
> 2026-09-17 후속: PR #70/#68/#72 병합 후 **main `84ec1cc` 00:47:47 KST 배포·재시작(PID3274983)**. 운영 verify1684 passed/2 known xfailed·설정/킬스위치7경로 지문 동일. Toss는 기본 OFF·실관측 미시작이며 실제 launcher/관측 서비스 연결 방식 확인이 남았다. 현황 정본 `docs/reviews/toss-pr-integration-2026-09-17.md`.
> 최종 업데이트: 2026-09-16 (Toss 승인 기반 관측 런타임 source `984dbdf`, UTC/KST 각각1684 passed/2 known xfailed·독립 소스 리뷰 승인. PR #70 Draft, #68 보류. 검증 정본 `docs/reviews/toss-runtime-2026-09-16.md`. 기본 OFF·실관측 미시작; main/운영·배포/재시작·주문/설정 무변경. 과거 운영 배포 기록은 아래 이력이며 이번에 실행 PID/SHA를 조회하지 않음)

## 세션 시작 시 필수 읽기

작업 시작 전 **반드시** 아래 파일을 읽을 것:

1. **`CHANGELOG.md`** — 최근 변경 이력 확인
   - 이미 구현된 기능 중복 작업 방지
   - 설계 결정 맥락 파악 (왜 이렇게 짜여 있는지)
   - 알려진 미해결 이슈 파악
2. **`docs/README.md`** — 기술 문서 인덱스
   - 아키텍처, 전략, 리스크, 진화 시스템, 운영, API 연동 상세 문서
   - 에이전트별 참조 가이드 포함

> 예: 유저가 "X 기능 추가해줘" 요청 시 → CHANGELOG에서 이미 구현됐는지 먼저 확인
> 예: 전략 수정 시 → `docs/strategies/kr-strategies.md` 참조

## 언어 & 소통
- 모든 대화는 반드시 한국어(한글)로 진행할 것
- '커밋해줘' = commit AND push. '푸시' = push. 애매하면 commit + push 기본.
- 'new' 또는 'fresh'로 요청하면 이전 실패한 패턴 참조 금지.

## Git & GitHub
- Use SSH for git push (not HTTPS). PAT-based auth if SSH unavailable.
- Always commit and push together unless explicitly told otherwise.
- `gh auth login` interactive mode does NOT work in this environment.

## 에이전트 팀 (15명 — 운영·분석 8명 + 도메인 전문가 7명)
- 명단·역할·주기: `.claude/agents/` 디렉토리 참조
- 전문가 시스템 상세: `docs/agents/expert-system.md` / 코드: `src/experts/`
- 출력: `ExpertOpinion` (score/bias/confidence/findings) → market_regime + cross_validator

## 하위 에이전트 위임 규칙 (2026-09-20 Codex·Claude 공통 규칙)
- Agent/Workflow 로 하위 에이전트를 띄울 때는 기본값을 쓰지 말고 **작업 성격에 맞춰 모델·effort 를 매번 명시**한다.
  - 기계적·저위험 요약·작은 문서 → `gpt-5.6-luna`, low~medium. 검색·단순 시험 실행은 별도 모델보다 도구를 직접 사용한다.
  - 범위가 고정된 일반 구현·특성화 시험 → `gpt-5.6-terra`, medium~high; 다중 파일 분석·일반 독립 리뷰 → `gpt-5.6-sol`, high.
  - 설계·어려운 재현·주문/사이징/청산·보안·동시성·상태 무결성 → `gpt-6-astra` 또는 확인된 `claude-opus-5`, high. 중요한 최종 리뷰는 구현자와 분리해 지원되는 xhigh를 명시하고 다른 공급자를 우선한다.
- 같은 워크플로 안에서도 단계별로 다르게 지정하고, 선택 근거를 label/프롬프트 첫 줄에 남긴다.
- 이 호스트 전역 정본은 `/home/ubuntu/.config/ai-agents/model-routing.md` (`ai-routing-v1-2026-09-20`), 로딩/설정 검증은 같은 폴더 `verification-2026-09-20.md`다. 다른 호스트는 별도 설치/확인이 필요하다. 프로젝트 안전 경계는 계속 적용한다.
- Plan→Do→See: 같은 base SHA·격리 worktree·파일별 단일 writer, 부모만 통합한다. native Codex와 외부 Claude를 합해 작업자 최대3명(실제 도구 한도가 더 낮으면 그 한도), worker 재위임 금지. 요청 모델/effort와 실제 관측 모델을 구별한다.
- Fable은 요청 후 Opus로 fallback된 상태여서 자동 배정 제외. Sonnet/Haiku도 이 환경에서는 실제 모델 확인 전 자동 사용하지 않는다. max/ultra는 기본값이 아니다.
- 외부 Claude는 우선 도구를 끈 비식별 source 입력으로 리뷰한다. 도구 허용 구현에는 검증된 개발 sandbox가 필요하며 worktree는 보안 sandbox가 아니다. 이 규칙은 실주문·운영 SSH·배포·재시작·설정 변경 권한을 부여하지 않는다.

## 프로젝트 개요
- KR+US 통합 트레이딩 엔진 (Full Rewrite)
- 단일 KIS appkey로 국내+해외 주식 동시 운영
- 비동기(asyncio) 이벤트 기반 아키텍처
- 단일 포트 8080에서 KR+US 대시보드 통합 서빙
- 크로스 전략 검증 게이트 + 시장 체제 사전 적응

### 토스 후속 작업 상태 (2026-09-16)

아래는 09-16 인계 당시 기록이다. 09-17 사용자가 열린 PR 전체 통합·배포·재시작·활성화를 지시해 #68 보류 결정은 검토 후 정합화 방식으로 변경됐다. 원본의 안전하지 않은 구현을 복구하지 않으며 실행 트리는 #67/#70을 유지한다. 실관측 연결·승인 정책의 준비와 실제 활성화 결과는 후속 보고서로 구분한다.

- **최신 인계:** #67은 main `8c159d0`에 병합됐다. #69/#71까지 main `a3187a8` 기준 feature/PR #70의 **Plan→Do→See 오프라인 구현·통합/독립 소스 리뷰 완료**(source `984dbdf`, 검증 정본 `docs/reviews/toss-runtime-2026-09-16.md`). 중복 PR #68 `0b978e0`는 보류 유지(임의 병합/리베이스/닫기 금지). 설계 `docs/superpowers/specs/2026-09-16-toss-runtime-shadow-design.md`, 실행 계획 `docs/superpowers/plans/2026-09-16-toss-runtime-shadow.md`, 배치 경계 `docs/operations/toss-shadow-runtime.md` 참조.
- `src/data/providers/toss/`는 기본 OFF다. 승인/발급 context·실 OAuth 어댑터·bounded GET/POST·원장·격리 worker·5분 현재가/별도 캘린더 관측을 오프라인 구현/검증했다. **운영 배포/실관측 미시작**이며 승인 등록부·plan·신뢰된 시작 시점 attestation 없이는 `TOSS_API=1`도 실행 거부다. broker fallback·일봉 live·후보 점수·돈 경로 변경은 미포함이다. grant `client_identity`는 정확한 OAuth client_id이며 발급 예산은 worker 수명당 상한이다.
- 합성 입력 전용 `scripts/replay_toss_shadow.py`의 CLI·검증·독립 리뷰 근거는 `docs/reviews/toss-phase1-offline-2026-09-16.md`에 기록한다. 합성 결과는 항상 `production_eligible=False`; 실자료 승인이나 매매 성능 근거가 아니다.
- 실자료 관측 전에 약관·발급 소유권·시장/수정주가 기준·관측 manifest를 별도로 확정해야 한다. 주문·청산·사이징·계좌·잔고는 계속 KIS 단독이며 기존 설정과 운영 상태는 변경하지 않는다.

## 프로젝트 경로
- 소스: `/home/ubuntu/projects/qwq-ai-trader`
- 가상환경: `venv/` (.venv 아님)
- 설정: `config/default.yml` (kr: + us: 섹션) + `config/evolved_overrides.yml`
- 환경변수: `.env`
- 로그: `logs/YYYYMMDD/`
- 캐시/상태: `~/.cache/ai_trader/`
  - `trade_journal[_kr|_us].json` — 거래 기록
  - `daily_stats[_kr|_us].json` — 일일 손익 영속화
  - `unified_trader.pid` — PID 파일

## 설정 주의사항
> **`evolved_overrides.yml`이 `default.yml` 위에 머지됨**
>
> 설정 변경 시 양쪽 모두 확인 필요. evolved_overrides가 default를 덮어쓰므로,
> default.yml만 바꿔도 evolved_overrides에 같은 키가 있으면 적용 안 됨.

## 아키텍처 & 실행 흐름
- UnifiedEngine 구조·스케줄러 태스크 주기·KR 배치 시각: `docs/architecture/system-overview.md` 및 `src/schedulers/` 소스 참조
- 디렉토리 구조는 `src/` 하위 `ls`로 확인 (모듈별 한 줄 설명은 위 아키텍처 문서)

### 목적 정합성 1단계 (09-17 착수, 09-18 경제/보호·core 큐 연결 검증)

- 최신 C4(09-20): 명시 장전 text diagnosis owner·actual caller·중기 정책 version/표시 소비 경계를 구현하고 신규53시험을 추가했다. 동일 순간 KST 급락·optional 호출 순서·schema 활성 규칙 경합·미게시 getter·DTO/HTML 경계를 보완했다. 최종 UTC/KST 각각4403passed/기존xfail2/경고4·격리0, native/Opus 한정 승인과 외부 조건의 실제14개 소비자 검증을 완료했다. 정본은 `docs/reviews/morning-regime-owner-2026-09-20.md`이며 아래 C3 당시의 C4 미완 표기는 과거 이력이다. 전체5단계·factory/나머지 writer·qualification/최종 sizing·gateway·공식 증거·장기 성능·운영 이행은 여전히 미완이다.
- 목표: 비용 차감 KODEX200 초과수익 검증 + 현행 위험 한도 유지. 첫 범위는 KR 취소/체결/복구 정합성과 최종 진입 검사다.
- 상세 설계는 사용자 승인됐으며 구현 계획은 `docs/superpowers/plans/2026-09-17-engine-execution-safety.md`다. feature 브랜치에는 저장/주문/guard 기반과 실제 Portfolio·ExitManager·위험 DTO/reducer, UnifiedEngine 누적체결 큐/receipt를 구현했다. **run_trader·scheduler·broker 운영 경로에는 설치하지 않았다.** 전체 1단계/US 안전성/수익성 완료가 아니다.
- 공식 GitHub legacy에 현행 TTTC8001R/TTTC8036R 자료가 있다. `docs/integrations/kis-execution-contract-2026-09-17.md`에서 고정 출처와 지원/미지원 범위를 확인한다. 빈 조회·취소 ACK·같은 잔고 반복으로 최초 인계나 취소 최종성을 승인하지 않는다.
- 09-18 후속: 실제 broker GET/공용 limiter와 legacy 수집기의 오프라인 통합을 검증했다. 실제 aiohttp 헤더와 취득별 lease를 사용하고 운영 poller/거래 POST는 아직 연결하지 않았다. 단계별 정본은 `docs/reviews/engine-execution-followup-2026-09-18.md`이며 조회 complete를 거래 허가로 쓰지 않는다.
- Task8A/B: 증거 기반 보호 repair와 전용 경제 원장 durable ACK를 한정 검증했다. quote는 durable admission 후에만 view를 게시하며 미해결 입력을 재시작 뒤 repair로 덮지 않는다. 실제 임시 PostgreSQL 인수 포함 KST/UTC2292passed/기존xfail2·격리0, 별도 독립 재리뷰 승인. 전용 이벤트 원장 ACK는 기존 분석 원장·R/canary 완료가 아니다.
- Task8C: 확정 사실 ingress·KST fence/valuation 기반 일자 전환·최초 손절/최종성/R과 typed ACK의 core 범위를 한정 검증했다. C1 독립 리뷰 P1 3/P2 1, C2 P2 1을 수정·재리뷰 승인했다. 최종 전체 KST/UTC2425passed/기존xfail2·격리0. 명시 stale quote는 수락 전 거부하고 durable 최신 시각/원 provenance를 보존한다. 이력 크기/ingress registry 장기 성장, 실제 scheduler 일일 writer 및 legacy 분석 원장 projection은 남는다.
- Task9A/B: 실제 요청·기존 위험 정책·예약을 같은 owner의 prepare/claim/최종 검사에 연결하고 체결 sector 인계 등 독립 P2를 수정·한정 재리뷰 승인했다. 당시 전체 KST/UTC3294passed/기존xfail2·격리0. 모든 MODIFY는 미지원, 실제 publisher와 전체 writer 이행은 미완이다.
- Task10A1: 같은 host/고정 private root 계좌 lease와 runtime command/result 종료 drain을 구현·별도 독립 리뷰했다. 저장/게시 실패의 정상 종료 오인 P2를 수정·재리뷰 승인, 당시 전체 KST/UTC3381passed/기존xfail2·격리0. factory/core/run_trader/CLI에는 아직 설치하지 않았다.
- Task10A2 부분: feed/보호·진입 증거에 이어 실제 지수 metadata·위험 source 수명과 명시5분 owner 경로를 구현했다. source/input/선제 selector와 실제5분 연결은 각각 독립 한정 승인이다. KST/UTC3702passed/기존xfail2·격리0(경고4: pykrx1+fork3). pending effect 전달·정책 재생·정오/2분/LLM·callback/factory 및 전체 writer는 미완이다. actual degraded→5분→repair 차단 RED3을 후속 고정했고 원본 uninstalled batch 중첩 RED도 보존한다. 시장 시각 미입증을 receipt now로 채우지 않으며 `trading_ready=False`다. 세분 계획은 `docs/superpowers/plans/2026-09-18-engine-writer-migration.md`, 완료 범위 정본은 `docs/reviews/engine-execution-followup-2026-09-18.md`다.
- Task10A2b6/B1 후속: 실제5분 정책 재생과 기존 수량 산술 분리를 각각 한정 재리뷰 승인했다. 정책 누락/건강 anchor 위조·조기 거부 전 수수료 조회 회귀를 수정했고, 최초 fill commit의 replay digest를 경제 outbox에 결합해 증거 없는 과거 이력은 BLOCKED로 유지한다. 수정 후 전체 KST/UTC3830passed/기존xfail2·격리0·기존경고4다. 정오/2분/LLM 실제 writer RED3, qualification/최종 sizing·effect 전달·나머지 writer/설치·전체 C/F/G/R는 계속 미완이며 main/운영 변경은 없다.
- Task10A2c/C2a 정책 변경 이력·schema2 seal: 최초 Opus CHANGES_REQUIRED의 B1/B2/B4를 실제 재현 후 수정했다. 미등록 요청 사전거부, 등록 day/closing/drain, checkpoint↔SQL 등록 receipt 양방향 복원 대조가 포함된다. 신규 정규32시험 포함 전체 UTC/KST 각4053passed/기존xfail2·경고4·격리0; 실제 Opus5/xhigh366.318초 정상 완료·APPROVE_THIS_SLICE. 정적 리뷰는 시험 실행/전체 engine/운영 승인이 아니다. 처분 정본 `docs/reviews/policy-generation-remediation-2026-09-20.md`. 다음은 retained source 요청 경계(N1) → 지속 source 권한(기존 RED7/대조2) → 실제2분/정오·LLM·보호 replay다. 전일 checkpoint는 rollover/resume 후 selector 등록·versioned seal 순서. 전체 writer/C/F/G/R·main/운영 전환은 미완이다.
- 후속 N1 retained 요청 경계(최종4073)에 이어 C2b 현재 source 소비 권한을 구현했다. 과거 receipt 보존·현재 의존/실패/pending 검사·정상 접수 거부와 SQL 장애 분리, 최초 Opus I1/I2 보완 후 실제 Opus5/xhigh307.174초·APPROVE_THIS_SLICE다. 최종 전체 UTC/KST4124통과·기존xfail2·경고4·격리0. 최신 정본 `docs/reviews/source-authority-followup-2026-09-20.md`; 실제2분·정오/LLM/replay·전체 writer/운영은 여전히 미완이다.
- 09-20 개발용 Opus 실행기는 timeout/진행 분리·nonzero 실패·자식 정리·모델/result 검증 후 독립 Astra/xhigh 한정 승인됐다. 당시 전체 UTC/KST4021통과·실제 smoke2.72초이며 이후 엔진 C2a는 위 별도 한정 승인을 받았다. 실행 완료와 코드 승인·운영 승인을 계속 구분한다. 사용법 `docs/operations/agent-routing.md`, 원인/검증 이력 `docs/reviews/opus-review-runner-2026-09-20.md`.
- 실제2분 C2 후속: owner/caller·schema3 captured reads와 등록/취소·typed source/baseline 경계를 독립 재현 후 수정·네이티브/Opus 한정 승인했다. 최종 UTC/KST4271passed·기존xfail2/경고4·격리0. 정본 `docs/reviews/two-minute-regime-owner-2026-09-20.md`. 다음은 명시 horizon·정오/JSON LLM/보호 적용·typed replay/repair13개 실큐 인수다. 전이력 성능·전체 writer·factory/운영은 미완이며2분 보호 적용0·기존 임계값을 유지한다.
- 명시 runtime 설치 후에는 legacy SIGNAL/ORDER/FILL와 직접 체결/가격 writer를 거부한다. 큐 적재는 적용 성공이 아니며, caller 취소·계산 실패를 pending 해제로 해석하지 않는다. 미설치 운영 경로의 기존 동작이 바뀌었다고 보고하지 않는다.
- C3 후속 한정 승인: 정오 cap/JSON LLM/full 보호 application·typed replay와 실제 즉시/주기 caller를 단일 owner에 연결했다. 독립3건·경합/복구 이력과 Opus A의 원시각/명시 결측을 보완했고 네이티브/Opus A 재리뷰·B 모두 한정 승인이다. 신규79 포함 최종UTC4350passed244.32초/KST4350passed239.88초·각 기존xfail2/경고4·격리0. 첫 전체 입력900초 timeout은 미승인 이력으로 남기고, 모델/effort/권한 변경 없이 소입력 대조·분할 리뷰로 완료했다. 정본 `docs/reviews/noon-regime-protection-replay-2026-09-20.md`, 고정계약 `docs/superpowers/specs/2026-09-20-regime-noon-owner-contract.md`. C4 장전 text diagnosis·consumer closure와 전체writer/factory/공식증거/성능은 미완이며 운영 설치 승인이 아니다.
- KIS 거래·잔고, Toss 별도 관측, 면제·위험 수치·설정 유지. 운영 배포·재시작/주문/설정 변경은 하지 않는다. 모든 writer·HTTP/별도 수동 CLI 통합, 실제 일일 초기화 writer, 최초 인계·기존 분석 원장 projection·전체 인수는 잔여다. 비용은 기존 요율의 누적 추정 비용 차분으로 기록하며 실제 징수액과 구분한다. 정본: `docs/reviews/engine-execution-followup-2026-09-18.md`.

---

## 매매 전략

### 공통 사항
- 모든 전략은 `BaseStrategy` 상속, `generate_signal()` + `calculate_score()` 구현
- Decimal 정밀 계산, 최소 주가 KR 1,000원 / US $5

### KR 전략 (6개)
| 전략 | 파일 | 설명 |
|------|------|------|
| 모멘텀 | `kr/momentum.py` | 20일 고가 돌파 + 거래량 급증 |
| 테마추종 | `kr/theme_chasing.py` | 🚫 **폐지** (2026-05-04, allocation 0%) |
| 갭상승 | `kr/gap_and_go.py` | 갭상승 후 눌림목 매수 |
| SEPA | `kr/sepa_trend.py` | SEPA 추세 전략 (스윙) |
| RSI2 반전 | `kr/rsi2_reversal.py` | 🚫 **폐지** (2026-08-02, enabled=false + allocation 0% — 백테스트 단독 -15.44%, 근거는 evolved_overrides `_meta`) |
| VCP 돌파 | (배치 스캔 라인) | 변동성 수축 후 20일 고점 돌파 — **선행 발굴** (2026-08-03~) |
| 밸류코어 | `value_growth_screener.py` | 가치·성장 2버킷 장기보유 — 🔍 **shadow 관측 중** (2026-08-04~, 주문 없음, 설계 `docs/strategies/value-growth-core-design.md`) |

### US 전략 (4개)
| 전략 | 파일 | 설명 |
|------|------|------|
| 모멘텀 | `us/momentum.py` | 20일 고가 돌파 브레이크아웃 |
| SEPA | `us/sepa_trend.py` | SEPA 추세 (RS 등급 기반) |
| 어닝스 드리프트 | `us/earnings_drift.py` | EPS 서프라이즈 후 모멘텀 (🚫 비활성 — 2026-08-03 검증 통과했으나 US 미운용·EPS 커버리지 부족으로 보류) |
| 어닝스 리버설 | `us/earnings_reversal.py` | 발표 전 낙폭과대 반등 (⛔ 검증 기각 2026-08-03 — 활성화 금지) |

### 청산 관리 (ExitManager)
- **1차 익절**: +10% → 10% 매도 (2026-08-02 백테스트 검증으로 +5%/20%에서 조정)
  - 기존값은 평균 1.9일에 발동해 추세 초입을 절단 (익절 +5.2% < 손절 -6.2%)
  - ⚠️ 변경 시 4곳 동시 수정: `default.yml` / `evolved_overrides.yml` / `ExitConfig` / **`REGIME_EXIT_PARAMS`**
- **2차 익절**: +15% → 잔여의 50% 매도
- **3차 익절**: +25% → 잔여의 50% 매도 (기본값, 레짐별 REGIME_EXIT_PARAMS로 조정)
- **트레일링**: 고점 대비 3% 하락, 수익 +5% 이상 시 활성화
- **ATR 동적 손절**: 기본 5%, ATR×2, 범위 4~8% (evolved_overrides, min_stop 4.0)
- **포지션 상태**: `PositionExitState` — NONE/FIRST/SECOND/THIRD/TRAILING 단계 추적

### 코어홀딩 A안 (2026-05-11~ "장기 추세 캐처")
- **진입 필터**: MA200 위 + 60일 ≥+5% + 신고가 80% 이내 → 박스권 자동 배제
- **점수 (100점)**: 추세 20 + 펀더 20 + 수급 20 + 모멘텀 30 + RS등급 10
  + **저변동성 감점** 0~-10 (일수익률 60일 σ 기준, 2026-08-03~, 급등락형 배제)
  + **자산 확장 감점** 0~-5 (DART 총자산 증가율 ≥30/50%, 2026-08-03~, 퀄리티 팩터)
- **청산**: stop_loss 10%, trailing 12% (느슨), 분할익절 OFF, max_holding 무제한
- **stale**: Tier1 20영업일±3%, Tier2 30영업일±3% OR 20영업일±2%+거래량50%
- **리밸런싱**: 격주 (rebalance_interval_weeks=2)

### 캘린더 시즈널리티 오버레이 (2026-08-03~)
- `src/utils/calendar_seasonality.py` — 전 전략 매수 **사이징 배율** (독립 전략 아님)
- turn-of-month(월말 2+월초 3거래일) KR/US ×1.10, US 옵션만기주 ×1.05 (KR 만기주 미적용)
- 부스트 전용(차단·축소 없음), 상한(max_position_pct)은 재적용됨
- 비활성화: `CALENDAR_SEASONALITY=0`

### 조건부 변동성 타게팅 오버레이 (2026-08-19~)
- `src/utils/volatility_targeting.py` — 모멘텀 계열(sepa/gap/momentum/vcp) 매수 **사이징 축소 배율**
- KOSPI 20일 실현변동성 > **25%**(전체 거래일의 ~11% 극단 국면)일 때만 ×(25/vol), 하한 0.4
- 검증: KODEX200 2015~26 — Sharpe 0.721→0.787, MDD -40.8→-34.8% (CAGR -2.1%p 비용)
- 캐시: `~/.cache/ai_trader/vol_targeting.json` (매 거래일 08:30 갱신, 노후 3일+ 시 무개입)
- 축소 전용(레버리지 없음), 일수익률 |12%| 초과는 데이터 오류로 제외
- 비활성화: `VOL_TARGETING=0` / 상세: `docs/research/ai-trading-research-2026-08.md`

### 팀 심의 conviction 부스트 (2026-08-20~)
- `src/utils/team_conviction.py` — 팀 심의(BUY 승인 + conviction ≥0.75/0.90)
  종목의 신규 매수 사이징 ×1.10/×1.20 **부스트 전용**
- HOLD/REJECT는 사이징에 미반영 — CF 실측(47건): HOLD 차단 후보가 5일 +6.31%
  (차단·감액 용도는 손해로 판명)
- **2026-09-13 비활성화 (`TEAM_CONVICTION=0`, 운영 .env)**: 근거였던 "+6.31%"는 강세장 베타(KODEX200 대비 -1.0pp, 최신 코호트 -3.1pp), 승인 BUY 종목이 HOLD보다 열위, conviction 94%가 0.90 고정 — 초과수익 ≥60건 확보 전 부스트 금지 (리뷰 §3)

---

## 리스크 관리

### KR 리스크
| 항목 | 값 | 비고 |
|------|---|------|
| 일일 최대 손실 | -5.0% | effective_daily_pnl 기준 |
| 일일 거래 횟수 | 10회 | daily_max_trades |
| 일일 신규 매수 | 5개 | max_daily_new_buys |
| 최대 포지션 수 | 8개 | max_positions |
| 기본 포지션 비율 | 25% | nominal 모드만. **`sizing_mode: risk`(2026-09-13~, 09-14 정합화)** — equity×0.7% ÷ **신규 체결 실제 고정 SL**(sepa 5 / gap 3.5 / vcp 4, 급락 cap 미적용) → sepa 14%·gap 18%(상한)·vcp 17.5%. 모든 오버레이·3주 보정 뒤 매수수수료 포함 계획 위험 ≤ 0.7% 를 최종 상한으로 재클램프(1천만·1만원·SL5% → 139주) |
| 최대 포지션 비율 | 28% | nominal 상한 / risk 모드는 `risk_max_position_pct` 18% |
| 최소 현금 보유 | 5% | total_equity 대비 |
| 최소 포지션 금액 | 20만원 | 미달 시 매수 거부 |

### US 리스크
| 항목 | 값 | 비고 |
|------|---|------|
| 일일 최대 손실 | -3.0% | |
| 최대 포지션 수 | 10개 | |
| 기본 포지션 비율 | 25% | |
| 최대 포지션 비율 | 35% | |
| 최소 현금 보유 | 10% | |
| 최소 포지션 금액 | $50 | |
| 연속 손실 중단 | 3회 | 사이징 50% 축소 |

### 팩터 버킷 위험예산 (2026-08-08~, shadow 관측 중)
- trend 65% / quality 20% / reversion 10% — 상관 전략 묶음 총 노출 캡 (`default.yml factor_budgets`)
- 현재 `enforce: false` (초과 시 로그만) — 상세 `docs/risk/risk-and-exit.md`
- **승격 보류 (2026-08-19)**: 8월 매수 0건으로 초과 이벤트 표본 부재 →
  일일 노출 스냅샷(`factor_exposure_log.jsonl`, 저녁 품질검증 잡) 2주 축적 후 재판단
  → **2026-09-03 재판단 불가**: 전략 포지션 0건이라 스냅샷 전부 0% — 매수 재개 후 2주로 이월

### 운영 상태 — 현금 고갈 (2026-07-01~, 2026-09-03 확인)
- 펩트론 087010 120주(`manual`, exit_exempt)가 자산의 **99.6%**, 현금 **0.4%(~7.8만원)**
  → 봇 신규 매수가 구조적으로 불가 (8·9월 주문 0건은 버그가 아니라 현금 부족).
  진화·CF·승격 표본 축적 전부 정지 상태. 해소는 사용자 판단(펩트론 일부 매도/입금).
- `/api/portfolio`의 `cash_ratio`로 즉시 확인. 코어홀딩 "빈슬롯 매수 시도(예산 잔여 2.9M)"
  로그는 equity 기준 예산이라 현금과 무관 — 0건 반복은 정상.

### 2026-09-15 Codex 후속 수정 — PR #58 배포 완료, Codex 독립 리뷰 후속

- **MCP 정리(PR #63 배포 완료):** 미사용 MCP 클라이언트·부팅·보조 조회를 제거했다. KIS 수급·일반 pykrx·네이버 뉴스·DART 직접 경로는 유지하고 수급/공매도/버즈 미획득을 검증 완료로 승격하지 않는다. UTC/KST 각 1019 passed / 2 xfailed·격리 위반 0, Astra/xhigh 독립 리뷰 신규 P0/P1/P2 0·승인(비차단 테스트 의견 1건 반영). 필수 CI 통과 후 main `c9923bf` **22:37:08 KST 재시작(PID3082563)**, 새 PID MCP 경고 0·KIS/검증기 정상 초기화. 패키지 설치·주문·설정·킬스위치 변경 없음. 아래 PR #61의 MCP 경고 잔존은 당시 기록이며 운영 관찰 정본은 `docs/reviews/mcp-retirement-2026-09-15.md` 참고.
- 기준 `1aba7d7`, 브랜치 `feature/codex-review-fixes-20260915`. 리뷰 결함 R1~R8(08:00 동기화 경계·갭 손절 체결·A/B 판단 시각·MCP/DART 획득 상태·검증 가산분 분리·전문가 평가 기권·CF 불변 원장 연결) 수정·오프라인 검증. 상세 `docs/reviews/codex-remediation-2026-09-15.md`.
- 사용자 후속 승인으로 PR #58 필수 verify 통과 후 **main `53ea967` 20:47 KST 배포·재시작 완료**. **주문·설정·킬스위치는 유지**(지문 동일), 위험 사이징/에이전트 정책 승격·canary 판단 무변경. UTC에서 드러난 시계 호환성 회귀도 수정해 UTC/KST 각각 916 passed / 2 xfailed, 추가 시계 수정 2파일은 Astra/xhigh 독립 재실행·한정 리뷰 지적 0. 실제 배포·운영 점검 기록은 보고서 참고.
- 이후 사용자 지시로 Claude 대신 **Codex Astra/xhigh 3관점 독립 리뷰 완료**. 신규 P2 2건(CF 판정 불가 시 측정 삭제, DART 제목 손상 정상 획득)과 기존 시간 계약 P2 2건(aware 만료·보고서 관측 미래 누수)을 재현했다. `4b70a34` 기준 격리 병렬 수정과 작업별 독립 리뷰·추가 P2 2건 재리뷰까지 승인됐다. CF는 미상 원 측정 보존/평가 제외·정확한 날짜·영속 조회 공정성을, DART는 불완전 입력의 실제 긍정 가산 억제를, replay는 만료/관측 정합화·검사 오류 별도 집계를 반영한다. 실제 주문·설정·킬스위치 변경/승격/canary 시작 없음. 통합 검증·최종 리뷰·배포와 운영 SHA의 정본은 `docs/reviews/codex-followups-2026-09-15.md`. 이전 Claude 대기 문구는 당시 기록이며 현재 리뷰 담당이 아니다.
- **PR #61 배포 완료:** 문서 포함 `9584df4`의 독립 Astra/xhigh 최종 리뷰 신규 P0/P1/P2 0·승인, UTC/KST 전체 각각 **1022 passed / 2 xfailed**. 필수 verify(run34971599511) 통과 후 main `82b5039` 병합, **21:56:43 KST 재시작(PID3037106)**. 설정 3파일·킬스위치 4경로 지문 동일, pending0·브로커 연결·초기 정체 없음. 기존 `mcp` 부재의 pykrx/naver_search 경고는 별도 한계이며 환경/패키지를 변경하지 않았다. 이후 관찰 결과는 보고서 참고.

### 전략·아키텍처 종합 리뷰 (2026-09-13) — `docs/reviews/strategy-architecture-review-2026-09.md`
- **1단계 반영(2026-09-13)**: 배분 core 0 / gap 15 / sepa 40 / vcp 10 (합 65, 잔여 현금) · `TEAM_CONVICTION=0` · 계측 기준 교체
- **2단계 반영(2026-09-13) → 후속 수정(2026-09-14, `docs/superpowers/plans/2026-09-13-review-remediation.md`)**: 09-13 배포본(de111b7)의 위험 사이징은 분모가 실제 신규 체결 SL(고정 sepa 5/gap 3.5/vcp 4)과 달랐고(F1) 근거 A/B 는 미래정보 포함(F2·F8). PR #33~#38 로 F1~F8 수정(동기화 빈 응답 방어·실제 SL 기준 사이징+0.7% 최종 상한·백테스터 시점·하트비트 성공/실패 구분·entry_risk 원장·유효 설정 게이트·parity). **2026-09-15 03:08 KST main `3f6b1bf`(F1~F22 전부 포함) 배포·재기동 완료**(1차 시도는 conftest 가드 과잉 차단으로 자동 롤백 후 핫픽스 #48).
- **재검증(2026-09-14, `docs/research/risk-sizing-revalidation-2026-09.md`)**: SEPA 단독 6셀(nominal/risk/고정 14% 대조군 × 6m/12m) 오프라인 재실행 — risk 는 운영 게이트 4조건 충족이나 **대조군도 동일 통과**·parity 미해소 2건 → **승격 보류**. live_policy 에서 SEPA SL 5% 단일값이라 risk = 고정 14% 명목(상한 18% 미발동). 검증된 것은 노출 축소(노출 26~30→21~26%, 회전 39~41→33~34배, MDD 3pp). 전 셀 KOSPI 대비 -26~-96pp, 상위 3건 제외 순손익 전 셀 음수 — 엣지 미입증 유지. canary(`scripts/review_risk_canary.py`, 원장 `scripts/export_risk_ledger.py --source db`) **미시작**. 자동 nominal 복귀 금지.
- **T9(2026-09-14 저녁, PR #42~#45)**: 모닝브리프↔실제 장 괴리(-3.26%) 분석 반영 — 12:00 레짐 재분류가 당일 지수·급락 상태(당일 갱신 게이트)를 쓰고 crash/severe 면 bull 미적용, LLM 입력 결측은 '결측'(0 금지)·`input_meta`, 지수 키 정규화·VIX 수집, 유효 레짐 `effective_regime`(장중 위험 당일 게이트, bull→sideways 강등 전용), 모닝브리프는 미국 자료뿐이면 "미국시장 마감 요약"으로 제한·개장 단정 문장 제거·전문가 상충 표시, 전문가 결측 `data_status`(insufficient ≤0.2/partial ≤0.7)·집계 제외·커버리지 게이트(<4명 무보정), 수동 거시 오버라이드 `valid_until`, 야간선물 `fetched_at`, 장전 전망 사후 평가 원장(20:30). verify 456 passed. 2026-09-15 `3f6b1bf` 로 배포됨. 상세 `docs/reviews/remediation-2026-09-14.md` §10
- **T10(2026-09-15, PR #47·#48 머지·배포)**: T9 후속 교차 리뷰 결함 10건(F13~F22) — 단계는 맞았으나 단계 사이 연결(자료→검증→레짐→소비자→발송→평가)이 끊긴 결함. 정오 당일 봉 교체/직전거래일 추가 구분, 급락 캡을 감지기·이번 조회·어댑터 당일 관측 3소스 중 보수적 값으로 병합, `monitor_positions`의 레짐 재적용 제거(30분 sync 단일화), G2가 어댑터 유효 레짐 우선, 6명 전문가 `data_status` 판정 확장 + `from_dict` unknown 집계 제외, 야간선물 세션 as_of 확인 시만 집계, Yahoo 지수 결측 None 보존, 07:00 문구 한국 방향 단정 제거, 모닝브리프 테마-업종 매핑 사전 고정, 07:30 발송 스냅샷 기준 저녁 평가(날짜별 아카이브 신설). 임계값 무변경. verify 552 passed / 2 xfailed(D 독립 재현 14건 + 통합 E2E 8건), Codex 리뷰 미실행(샌드박스). **2026-09-15 03:08 KST main `3f6b1bf` 배포·재기동 검증 완료**(보고서 §11.8). 상세 `docs/reviews/remediation-2026-09-14.md` §11, 계획서 `docs/superpowers/plans/2026-09-13-review-remediation.md` T10 절
- **T11(2026-09-15, PR #50~#53 머지 → 15:41 KST main `8c27fe8` 배포·재기동 완료)**: 에이전트 팀 근거 계약(EvidenceItem, positive_basis≠risk_clear, 관측 시각 모르면 None)·판단 v2 shadow(`TeamAssessment`: 매수 매력/위험 허용/자료 충분성/진입 조건 분리, 합의≠확률, 확률 미보정)·EntryPlan(PendingSignal 정본, `check_entry_plan` shadow 기록만)·append-only 심의 원장·실행 상태 6단계(plan_rejected 포함)·CF `team_buy_unfilled`·A/B/C 오프라인 러너(합성 검증만). 기존 TradeProposal/conviction/주문 경로 기준선 불변(특성화 테스트). 승인된 기존 경로 버그 수정 2건(vol_ratio 키, confidence=0 유효 소스). **투자 성능 미검증·운영 승격 없음(shadow 기록만).** 1차 배포(15:33, `04279c6`)는 시각 의존 테스트 7건이 장 마감 후 verify 에서 실패해 자동 롤백 → #53(테스트 시계 동결) 후 재배포. 분석가 산식 수정 2건은 shadow 플래그 밖이라 사이징 차단은 운영 `.env` `TEAM_CONVICTION=0`(+위험모드 0.7% 클램프)에 의존 — 재활성화 시 재평가 필수. 현금 고갈 상태에선 EntryPlan shadow 행·CF 승인 BUY 표본이 쌓이지 않음(엔진 현금 검사가 훅보다 앞). 상세 `docs/agents/trading-team.md` T11 절, 계획서 `docs/superpowers/plans/2026-09-15-agent-team-evidence-entryplan.md`
- 실거래 266건: 수수료 전 총손익 ≈ 0, 차감 후 -139만, t=-0.18, KOSPI +37% vs 자산 -7.5% — **엣지 미입증**
- 구조 원인: 연 91배 회전(수수료 = 손실 전부) · 1차 익절이 타이트 청산 무장(p90 +4.9%) · 명목 사이징 3~4종목 집중 · 배분 55%가 근거 없는 라인 · 레짐 4겹 후행
- 판정 기준은 **KODEX200 초과수익 + 손절 클립** (절대수익 판정으로 04-23·08-20 결정이 뒤집힘). 권고 1~8·금지 목록은 리뷰 §5~6

### 섀도우 관측 현황 (2026-09-03 운영 서버 점검 기준)
관측 전용(주문 무관) 항목 전체 목록 — 상세는 각 문서 참조:

| 항목 | 시작 | 상태 |
|------|------|------|
| 밸류코어 (`value_growth_core.shadow_mode`) | 08-04 | ✅ 주간 이력 3/8주 (W34~W36, 승격 평가는 8주+) |
| 비대칭 수확 G3 (`harvest_shadow.py`) | 08-13 | ✅ 매일 08:40 실행 중 — 2/30체결, 누적 -3.2R |
| 에이전트 팀 심의 (`trading_team`) | 08-02 | ✅ 장중 10:30/11:30/13:00/14:00 4슬롯 verdicts 축적 중 (T11: append-only 원장 병행) |
| 규칙 #11 전문가 BEAR (`experts.shadow_mode`) | 08-02 | ⏳ 14건 축적 / 승격 기준 CF 9/20건·r5 56% |
| 규칙 #12 섹터 카운슬 | 08-07 | ⏸ hit 0건 (BEAR 섹터 매수 후보 없음) |
| 팩터 버킷 (`factor_budgets.enforce: false`) | 08-08 | ⏸ 초과 표본 없음 → 노출 스냅샷으로 보완 (08-19~) |
| Counterfactual 추적 | 08-08 | ✅ 209건 추적 중 (신규 매수 0건이라 증가분은 규칙 게이트 발화분) |
| Shadow Lab (calibration/bandit) | 08-10 | ✅ 주기 리포트 발송 중 |
| LLM Shadow A/B (`openai_model_light_shadow`) | 06-17 | 🚫 **비활성화 (08-19)** — 발화 경로 소멸로 8/3 이후 표본 0 |

### 수수료
- **KR** (한투 BanKIS, 2026년~): 매수 0.014%, 매도 0.213% (수수료+거래세 0.20%), 왕복 약 0.227%
- **US** (KIS 해외주식): Zero-commission

---

## 검증 프로토콜 (절대 규칙)
코드 수정 후 반드시 아래 순서 수행:
1. `python3 -m py_compile <수정파일>` — 문법 검증
2. **봇 재시작**: `echo 'user123!' | sudo -S -k systemctl restart qwq-ai-trader`
   - ⚠️ `nohup python scripts/run_trader.py` 직접 실행 **절대 금지** (systemd와 충돌)
3. 상태 확인: `systemctl is-active qwq-ai-trader`
4. 로그 확인: `journalctl -u qwq-ai-trader -n 20 --no-pager`
5. 에러 없으면 완료 보고, 있으면 즉시 수정

```bash
# 문법 검증 (전체)
cd /home/ubuntu/projects/qwq-ai-trader
source venv/bin/activate
find src/ scripts/ -name "*.py" -size +0c -exec python3 -m py_compile {} \;

# 봇 관리 명령어
echo 'user123!' | sudo -S -k systemctl restart qwq-ai-trader  # 재시작
echo 'user123!' | sudo -S -k systemctl stop qwq-ai-trader     # 중지
systemctl is-active qwq-ai-trader                              # 상태
journalctl -u qwq-ai-trader -f                                 # 실시간 로그
```

### 운영 스킬 (2026-09-10~)
- `/ops-check` 운영 점검 요약 (`scripts/dev/ops_check.sh`) · `/deploy-local` 서버 측 배포 (`scripts/deploy/local_deploy.sh`, 자동 롤백) · `/pr-merge` gh 기반 PR→verify→머지
- `main`은 보호 브랜치 — 직접 push 불가, 반드시 PR + `verify` 통과

## 코드 리뷰 프로토콜
사용자가 "리뷰해봐" 요청 시:
1. 변경된 모든 파일 재읽기 (캐시 의존 금지)
2. P0(치명적), P1(중요), P2(경미) 우선순위로 이슈 분류
3. 각 이슈: 파일명 + 라인번호 + 구체적 문제 + 수정방안
4. P0부터 수정 → py_compile → 재시작 → 로그 확인

## 문서 업데이트 (절대 규칙)

> **대전제: 모든 코드 변경은 관련 문서에도 반드시 반영해야 한다.**

코드 변경 시 **반드시** 아래 문서를 함께 업데이트:

1. **`CHANGELOG.md`** — 변경 이력 (날짜, 커밋, 수정 파일, 상세 내용)
2. **`docs/` 관련 문서** — 변경된 기능에 해당하는 기술 문서 업데이트
   - 전략 수정 → `docs/strategies/kr-strategies.md` 또는 `us-strategies.md`
   - 리스크/청산 변경 → `docs/risk/risk-and-exit.md`
   - 진화/위키 변경 → `docs/evolution/evolution-system.md`
   - 아키텍처/흐름 변경 → `docs/architecture/system-overview.md`
   - API 연동 변경 → `docs/integrations/external-apis.md`
   - 운영 절차 변경 → `docs/operations/runbook.md`
3. **`CLAUDE.md`** — 현재 상태(current state) 반영 (설정값, 전략 배분 등)
4. **`MEMORY.md`** — 교훈/패턴/규칙만 기록 (변경 이력 금지, 150줄 이하)

**문서 미업데이트 시 코드 변경 불완전으로 간주한다.**

---

## 대시보드 개발
- 새 기능 추가 절차·API 라우트·페이지 목록: `.claude/skills/dashboard-feature/SKILL.md` (대시보드 작업 시 로드)
- 가상 오피스 `/office`: 재빌드 `bash tools/office/build.sh` — **`static/office/`는 빌드 산출물이므로 직접 수정 금지** (상세: `docs/operations/virtual-office.md`)

---

## 코딩 규칙

### 패턴
- **비동기**: 모든 I/O는 `async/await` (aiohttp, asyncio)
- **데이터클래스**: 도메인 모델은 `@dataclass`
- **정밀 계산**: 금액/가격은 `Decimal` 사용 — `Decimal(str(value))` 로 변환 (float → Decimal 오차 방지)
- **한국어**: 주석, 로그 메시지 모두 한국어
- **로그 태그**: `[리스크]`, `[스크리닝]`, `[진화]` 등
- **pykrx**: 반드시 `await asyncio.to_thread(pykrx_func)` 래핑 — 동기 블로킹 금지
- **aiohttp timeout**: `timeout=aiohttp.ClientTimeout(total=30)` (숫자 리터럴 금지)

### 절대 금지 패턴

```python
# ❌ 잘못된 패턴 — 0, 0.0, "" 이 False로 처리됨
if value and value < 0:        # 0.0은 통과 안 됨
if atr and atr > 0:            # atr=0 조건 누락
result = value or default      # value=0 이면 default 반환

# ✅ 올바른 패턴
if value is not None and value < 0:
if atr is not None and atr > 0:
result = value if value is not None else default
```

### 주의사항
- `.env`에 API 키 저장 (커밋 금지)
- KIS API 토큰은 `~/.cache/ai_trader/`에 캐시
- **Position.current_price 반드시 체결가로 초기화** — 미초기화 시 unrealized_pnl -100% → 일일손실 즉시 트리거
- **pending 상태 관리**: 예외 핸들러에서 반드시 `clear_pending()` 호출 (누수 방지)
- **파일 수정 시 연관 체크**: types.py ↔ engine.py, exit_manager.py ↔ schedulers, config.py ↔ YAML
- **수수료 계산**: `FeeCalculator` 단일 사용 — data_collector/storage 내 하드코딩 금지
- **영업일 계산**: `is_kr_market_holiday()` 반드시 사용 (주말/공휴일 처리)
- **KIS 주문 POST는 재전송 금지**: 접수/정정은 `_api_post(retry=False)` — 응답 유실 시 재전송하면 중복 주문 (2026-09-03 P0). 새 주문 계열 TR도 동일
- **KIS 직접 호출은 `await kis_rate_limit.acquire(tr_id)` 선행**: 브로커·시세·스크리너가 같은 appkey라 초당 한도는 합산(EGW00201). 원장 TR(잔고/매수가능/체결/미체결)은 계좌당 초당 1건(EGW00215) — 새 원장 TR은 `utils/kis_rate_limit.LEDGER_TR_IDS`에 추가
- **KIS 연속조회 종료는 응답 헤더 `tr_cont`(F/M 다음, D/E 마지막)로 판정** — 본문 `ctx_area_*100` 키는 마지막 페이지에도 채워져 오므로 종료 근거가 못 된다(2026-09-15 EGW00215 반복 원인: 보유 1종목 계좌가 8434R 을 10회 호출). `_api_get` 이 `data["_tr_cont"]` 로 실어 준다. 요청 헤더 `tr_cont: N` 은 아직 미송신(다중 페이지 후속)

---

## 환경변수 (.env)
```
KIS_APPKEY, KIS_APPSECRET, KIS_CANO, KIS_ENV (prod/dev)
KIS_EXT_ACCOUNTS (외부 계좌, 형식: 이름:CANO:ACNT_PRDT_CD 쉼표 구분)
OPENAI_API_KEY, GEMINI_API_KEY
MANUS_API_KEY (미사용 — 2026-08-19 구독 해지, manus.enabled=false)
TEAM_ASSESSMENT_V2 (기본 1 — 팀 심의 shadow 판단 v2·원장 기록; 0 이면 미계산. 돈 경로 무영향, 2026-09-15 T11)
ENTRY_PLAN_SHADOW (기본 1 — 주문 직전 EntryPlan shadow 검증 기록만; 0 이면 미호출. 허용/차단 없음, 2026-09-15 T11)
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
INITIAL_CAPITAL (KR, 기본 500000)
```

## LLM 모델 선택
| 작업 | Primary | Fallback |
|------|---------|----------|
| 테마 탐지, 뉴스 요약 | Gemini 3.1 Flash Lite | OpenAI gpt-5-mini |
| 거래 복기, 전략 진화 | OpenAI gpt-5.6-sol (2026-08-19~ Manus 해지로 회귀) | Gemini 3.1 Pro |

---

## 긴급 정지 (킬스위치, 2026-08-02~)

파일 하나로 주문을 즉시 차단. **봇 재시작 불필요**, 브로커 계층에서 검사하므로 엔진 오작동 시에도 동작.

```bash
touch ~/.cache/ai_trader/KILL_SWITCH       # 신규 매수만 차단 (청산 허용)
touch ~/.cache/ai_trader/KILL_SWITCH_ALL   # 전면 동결 (⚠️ 손절도 막힘)
rm ~/.cache/ai_trader/KILL_SWITCH          # 해제
```

- 시장별: `KILL_SWITCH_KR` / `KILL_SWITCH_US`, 파일 내용은 차단 사유로 기록됨 (반영 최대 2초)
- 감사 원장: `~/.cache/ai_trader/audit/audit_YYYYMM.jsonl` (시도된 모든 주문 append-only)

## 진화 시스템

- 매일 20:30 자동 실행 (KR)
- `TradeReviewer` → `DailyReviewer` → `StrategyEvolver` → **`BacktestGate`**
- 최대 1개 파라미터만 변경 (race condition 방지)
- 평가 기간: 5영업일 + 10건 이상 거래
- 신뢰도 >= 0.6인 파라미터만 자동 적용
- **백테스트 사전 검증 (2026-08-02~)**: 적용 전 A/B 백테스트(6개월/60종목)로 개선 확인.
  수익률 개선 + **walk-forward 구간승 2/3** (2개월×3구간, 2026-08-03~) + MDD 악화 ≤1%p
  + 거래 ≥10건이어야 통과. 실패 시 **보류(fail-closed)**.
  `EVOLUTION_BACKTEST_GATE=0`으로 비활성화 가능
- 롤백: 적용 후 5영업일+10건 실거래에서 승률 -5%p 또는 손익비 -0.3 악화 시 (10영업일 초과·10건 미달도 보수적 롤백) — 코드 기준 (2026-09-13 문서 정정)
- 제안 경로 4종(내장 규칙·약점 마이너·일일 복기·LLM) 전부 게이트 경유, 14일 내 기각·롤백 파라미터는 소스 무관 재제안 억제 (2026-09-13 WikiSkill 정렬)
- `RUNTIME_WIKI=0`: 런타임(크로스검증 규칙#9·G4 LLM 2차·팀 심의)에 위키/메모리 비노출 — 기본 켜짐, 신호 이벤트 `wiki_context_used`·팀 verdict 태그로 영향 측정 후 결정
- 내장 규칙: 승률 < 40% → 진입 기준 +5, 승률 > 65% → 진입 기준 -5
- 결과는 `evolved_overrides.yml`에 영속화

## 주간 매도 후속 복기 (Post-Exit Review)

- 매주 토요일 09:00 KST 자동 실행 (`run_post_exit_review_scheduler`)
- 최근 30일 KR 매도 거래 → KIS 현재가 조회 → 매도 후 변동 추적
- 분류: +3% 이상=놓침, -3% 이하=회피, 그 사이=타당
- LLM: GPT-5.4 (STRATEGY_ANALYSIS, fallback Gemini Pro), 표본 ≥5건
- 출력: JSON + Wiki 페이지(`weekly_post_exit_YYYY-WNN.md`) + 텔레그램
- Wiki 페이지는 다음 weekly rebalance 시 LLM 컨텍스트로 자동 흡수

## Trade Wiki (Karpathy LLM Wiki 패턴)

- 거래 교훈을 전략/섹터/시장체제/**종목**별 마크다운 위키로 축적 (종목 차원 2026-08-07~)
- 위치: `~/.cache/ai_trader/wiki/`
- 3가지 오퍼레이션:
  - **Ingest**: 매도 체결 → 관련 위키 3~5개 페이지 자동 업데이트 + LLM(Gemini Flash) 교훈 추출
  - **Query**: 크로스검증 시 전략/섹터/체제별 교훈 + **종목 노트**(query_symbol) 컨텍스트 반환
  - **Lint**: 토요일 주간 헬스체크 (stale/저조 페이지 감지 + 180일 종목 페이지 아카이브)
- **종목 페이지** (`symbols/<코드>.md`): 거래 이력·교훈 + 전문가 리서치 노트
  (orchestrator가 9명 전문가 affected_symbols를 fire-and-forget 기록, 일일 출처 dedup,
  30개 롤링, 리서치 전용 신규 페이지 상한 200)
- 동시성: `asyncio.Lock`, fire-and-forget (매매 비차단)
- 크기 제한: 페이지 200줄, 로그 500줄, 전체 ~1MB

## US 엔진 고도화

- ATR 기반 포지션 사이징 (3개 전략 통일)
- SPY/QQQ 기반 시장 체제 판단 (`us_market_regime.py`)
- 크로스 검증 게이트 6규칙 (수급 제외, bear시 어닝스 허용)
- 체제별 파라미터: min_score_adj, max_daily_new_buys, position_mult_boost

---

## 트러블슈팅
- **전체 절차는 `docs/operations/runbook.md` 참조** — 봇 미응답, 싱글톤 락 충돌, 매수 미실행 체크리스트, 유령 포지션, WebSocket 중복 프로세스, DB 좀비 정리, 긴급 전량 매도

## 실행 방법
```bash
source venv/bin/activate
python scripts/run_trader.py --market both                # KR+US 동시 실거래
python scripts/run_trader.py --market kr --dry-run        # KR 테스트
python scripts/run_trader.py --market us                  # US만 실거래
```
