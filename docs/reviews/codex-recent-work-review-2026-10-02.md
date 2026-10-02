# Codex 최근 작업(PR #119~#138) 교차 리뷰 — 2026-10-02

> 사용자 요청: 최근 3일 Codex 작업 리뷰 → 리뷰 정본 기록 + P1-1·P1-2 수정.
> 리뷰 체계: 코디네이터(Claude) 직접 검토 + 독립 리뷰어 3명(브로커·스케줄러·저장소, 요청 Claude Opus / effort high).
> 작성자가 Codex이므로 교차 공급자 리뷰에 해당한다. 리뷰어가 보고한 실제 모델 식별은 런타임 표기(`claude-opus-5-5`)만이고
> 응답 메타데이터로는 미확인이다. 아래 P1은 전부 코디네이터가 코드에서 재확인했다. 가설은 별도 표기한다.

## 범위와 검증 사실

| 항목 | 값 |
|---|---|
| 리뷰 대상 PR | #119~#138 (10-01~10-02 병합, 20건) |
| 전체 변경 | 176파일, +45,142 / −1,085 |
| 운영 버전 | 리뷰 시작 시 5468208 (#119, PID 34408). **리뷰 도중 22:17:04 KST 다른 세션이 df1a5af를 일반 모드로 배포**(PR #139/#140, PID 172854, NRestarts 0) — 즉 아래 P1이 있는 코드가 현재 운영 중 |
| 리뷰한 돈 경로 diff | 5468208→df1a5af (#120~#137), 제품 코드 43파일 +6,661 / −1,017. 10월 6일 프로필은 old/new HEAD 모두 df1a5af(checkout 생략) |
| 작업본(0843c54) 전체 verify | 4045 passed / 2 xfailed, 186.81초, 격리 위반 0 |
| 운영 실행 원장 파일 | 리뷰 시점 없음 → 22:17 배포로 1개 생성(현재 세션 열림, 수정 전 코드가 기록 중). 첫 비정상 종료부터 P1-1 영구 보류가 실제로 걸릴 수 있다 |
| 운영 보유 | 042700 1주(gap_and_go, 09-29 진입, 저널 진입 3·매도 2), 087010 120주(manual, 자동매도 금지) |

#120~#137의 체결·장부·브로커 변경(실행 원장, 식별 체결, 잔고 동기화 보류, 저널 transaction)은 리뷰 도중 22:17에 운영에 올라갔다.
돈 경로 리뷰는 그 diff(5468208→df1a5af)를 대상으로 했다. 관측/분석(`src/analytics/*`, `src/observation/*`, 토스 WS)은 기본 비활성·opt-in이라 호출부만 봤다.

## 결론

**조건부.** P0 없음. P1 네 건의 공통 뿌리는 하나다. 새 실행 원장과 식별 체결 경로가 "한 번 실패하면 영구 보류, 자동 복구 없음"으로
설계됐는데 그 보류를 푸는 수단이 코드에도 runbook에도 없었다. 매수는 `KILL_SWITCH_KR`이 막고 있어 실제 영향은 분할 익절 중단·잔고 동기화 정지·저널 유실이다.

## 발견 (코디네이터 재확인)

### P1-1. 비정상 종료 상태가 영구 상속되고 해제 경로가 없음 — **이번 PR에서 수정**
- `execution_ledger.py` `_apply("open")`: `prior_unclean = any(not clean or prior_unclean)`. 과거 세션 하나가 비정상 종료면 이후 모든 세션이 `prior_unclean`.
- `_can_close`: `prior_unclean`인 세션은 clean close 불가 → 다음 세션도 다시 비정상 → 스스로 풀리지 않는다.
- `execution_history.hold()` → `kis_kr.unknown_buy_hold()`/`has_unknown_sell()`: 신규 BUY 영구 보류 + **전 종목 분할 익절 중단**(ExitManager `set_partial_exit_block`).
- 비정상 종료가 되는 일상 경로: OOM kill(09-29 실제), 미체결 주문이 남은 재시작, 접수 불명 1건, 장부 commit 미확인 1건, 긴급 CLI `sell_specific.py`/`liquidate_all.py` 사용(체결 확인 없이 종료).
- 기존 `order_unknown.json`은 날짜 변경으로 풀렸지만 이 원장은 영구였다.

### P1-2. 원장 open 실패 시 손절 포함 전 주문 거부 — **이번 PR에서 수정**
- `kis_kr.submit_order`·`_api_post`: `history.session_recorded`(= opened and not closed)가 False면 전량 SELL까지 거부.
- `execution_history.open()`: 한 번 실패하면 fault, 재시도 없음. 원인 예: 디스크 가득 참, 스키마/무결성 검증 실패, 계좌 범위 불일치, SQLite 잠금 5초 타임아웃.
- 원장은 `KISBroker.__init__`에서 무조건 켜지고 끄는 스위치가 없다. "보호 SELL은 어떤 장부 장애에서도 지연 금지" 불변식 위반.

### P1-3. 식별 체결 하나의 저널 실패가 세션 전체 보류로 번짐 — **이번 PR에서 수정(2차 커밋)**
- 모든 KR 체결이 식별 체결. SELL 저널 기록 실패 → `_pending_fill_handoffs` 실패 고정 → `_reconciliation_token()` None → 잔고 동기화 재시작까지 정지 → `history.fail('journal_commit_unconfirmed')` → P1-1 상태.
- 실패 조건: trade_id 없는 포지션(수동 매수·동기화 유입), 30일 창 밖 거래, DB 미연결·순단(재시도 없음), 과거 부분체결로 `entry_quantity`가 실제 보유보다 작은 거래.
- 042700은 DB 복원이 정상이면 통과(복원 쿼리가 `exit_quantity < entry_quantity` 행 포함, 3−2=1=보유). 재기동 시 DB가 내려가 있으면 trade_id 미복원 → 첫 손절 SELL이 P1-3을 일으킨다.
- fault 뒤 식별 거래의 SELL은 JSON·DB 어디에도 기록되지 않아(`identified_trade_requires_execution_identity`를 warning으로 삼킴) 거래가 영구 미청산으로 남고 초과수익 원장이 왜곡된다.
- 권고: 식별 SELL 전 DB/파일에서 거래 복원, 불일치 시 "대사 필요" 기록으로 남기고 후처리 완료 처리, 실패 범위를 해당 종목으로 한정.

### P1-4. 동기화 보류가 경보·시한 없이 지속됨 — **이번 PR에서 수정(2차 커밋)**
- `kr_scheduler._defer_portfolio_sync`: 실패로 표시되지 않은 보류(해소 안 되는 취소 관측, 수동 취소된 pending, 적용 전 예외로 남은 체결)는 `record_idle`만 → 정체 경보·매수 차단 둘 다 미작동.
- 실패로 표시된 보류는 `set_sync_status(False)`지만 `risk/manager.py` 10분 강제 해제로 약 90초 주기 차단·해제 반복.
- 운영 로그상 9월 이후 발생 0건. 권고: 보류 시작 시각 기록, N분 초과 시 `record_failure` + 텔레그램.

### P2 (미수정, 기록)
- 삭제된 `sync_from_kis` 자동 보충(누락 매수·매도 저널, 수수료 포함 PnL 보정, 전략 추론)이 대체 없이 사라져 대시보드·20:30 원장 집계가 달라질 수 있다. KR `_reconcile_pnl`은 호출처 없는 죽은 코드.
- 매수가능조회 실패 시 포지션 대사까지 통째로 중단(이전엔 예수금 대체 후 진행).
- 손절 POST 직전 원장 기록 2회 + FULL fsync가 공용 `to_thread` 풀을 거침. 저널 JSON fsync는 이벤트 루프에서 동기 실행.
- 취소 종료 판정이 미확인 KIS 응답 형태(`cncl_yn`, 원주문 행 `tot_ccld_qty+cnc_cfrm_qty`)에 의존(**가설**, 실계좌 8001R 원본 행 1건으로 확정 필요).
- 저장 장애(`unavailable`/`unknown`/`pending`)에 재시도·확정 조회(`lookup_execution_receipt`, 미호출) 없음. 미commit 식별 기록을 DB로 재전송하는 경로 없음(`predecessor_uncommitted` 연쇄).
- "미해결 주문" 상태가 네 곳(order_unknown.json, 원장 hold, 체결 관측, handoff)에 겹치고 해제 규칙이 다름.
- `execution_recovery_status()` 소비처(대시보드·ops_check) 없음. 미사용 변수(`_sig_cache_h`, `_sig_cache`), `_exit_basis_is_idle` 종목별 검사는 토큰이 이미 계좌 전체로 막아 사실상 죽은 검사.
- `allow_nan=False`·저장 예외 전파가 기존 경로에도 적용돼 지표에 NaN 하나면 같은 날짜 파일의 이후 저장이 모두 실패(**가설**).

### 집중 질문 중 이상 없음으로 확인된 것
- 잔고 보류의 활동 카운터 자체는 `finally`에서 항상 감소(누수 없음). 영구 보류는 위 P1-3/P1-4 경로만.
- 체결 중복 방지 키는 `세션:주문ID:누적수량` + 원장 `_observe` + 메모리 `filled_quantity` 이중. 같은 누계 재통지는 Fill 미생성, 같은 누계·다른 평균가는 fault.
- `fill_matches_pending`의 `fill.order_id`는 로컬 주문 ID(브로커 `_orders` 키)라 pending 해제 조건 성립. 폴백 시장가 교체 시 `_pending_order_ids` 갱신됨.
- TradeStorage↔TradeJournal↔호출부의 새 kwarg(`execution_identity`, `execution_time`) 시그니처 일치. 기존 호출부는 기본값 None으로 기존 경로.
- 평단 보정은 첫 await 전 캡처·잠금 안 재검사·재검사와 쓰기 사이 await 없음(안전).
- SIGTERM 정상 종료는 `shutdown→disconnect→close`로 clean 기록 가능. 10월 6일 첫 기동은 원장 파일 부재로 보류 없음.

## 수정 (이번 PR)

| 항목 | 변경 |
|---|---|
| `src/execution/execution_ledger.py` | `acknowledge(note)` 이벤트 추가. 현재 세션이 아닌 비정상 세션·미완결 주문에 `acknowledged=True` 표시, 현재 세션 `prior_unclean=False`. `open`의 상속 계산과 `_can_close`가 acknowledged를 제외. `order_unresolved()` 공용화 |
| `src/execution/execution_history.py` | `hold()`가 acknowledged 과거 주문 제외. `execution_ledger_location()` 공용 헬퍼(브로커·CLI 동일 경로/범위) |
| `src/execution/broker/kis_kr.py` | `session_recorded=False`(원장 open 실패)일 때 BUY·분할 SELL만 거부, 보호 전량 SELL은 ERROR 로그 후 전송. `_api_post` 전송 직전 재확인도 동일. `cancel_guard` 없는 order-cash는 기존대로 차단 |
| `scripts/ops/acknowledge_execution_ledger.py` | 봇 정지 중(`hold_or_exit`) 운영자 확인 CLI. `--note` 필수(원장 영구 기록), 기본 경로는 운영 KIS 설정으로 계산, `--ledger/--account-scope` 명시 가능. 전/후 요약 JSON 출력, 확인 세션 clean close 실패 시 exit 1 |
| `tests/test_execution_ledger_acknowledge.py` | 상속·해제·재생 일관성·hold 해제·CLI(락 거부·반쪽 인자·손상 범위)·브로커 가드(BUY/분할 차단·전량 통과·cancel_guard 없는 POST 차단)·귀속 미확정 종목 한정 보류 |
| `src/schedulers/kr_scheduler.py` (P1-3) | `_drain_fill_handoffs`: 장부 미제출·commit 미확정은 `_record_unattributed_execution`(JSONL fsync, 브로커 종목 표시, 경보 1회/종목·일)로 보존하고 `handoff_returned`·ack 를 진행. 원장 docstring의 "handoff_returned 는 호출부 반환 증거이며 DB 저장 증거가 아니다"에 맞춘다. 식별 SELL은 trade_id 가 있으면 `recover_trade`로 DB 복구, 종목 추정은 금지 유지. 기록 실패 시 종전 실패 경로 |
| `src/core/evolution/trade_journal.py`·`src/data/storage/trade_storage.py` | `recover_trade(trade_id)` (래퍼 시그니처 동기화) |
| `src/execution/broker/kis_kr.py` (P1-3) | `_unattributed_symbols`·`mark_unattributed_execution`; 그 종목 BUY 거부(머리 게이트 + POST 직전), `has_unknown_sell` True(분할 SELL 보류), `execution_recovery_status` 에 `unattributed_symbols`/`journal_unattributed`. `record_execution_journal_failure`(세션 fault)는 더 이상 정상 경로에서 호출되지 않는다 |
| `src/schedulers/kr_scheduler.py` (P1-4) | `_defer_portfolio_sync`: 보류 시작 시각 추적, 15분 초과 또는 실패 표시 보류 → `set_sync_status(False)`·`record_failure`·경보 1회. 성공 동기화가 초기화 |
| `tests/test_journal_commit_handoff.py`·`tests/test_sync_defer_alert.py`·`tests/test_durable_execution_integration.py` | 37차 "영구 보류" 2건·35차 "handoff_returned False" 1건을 새 계약으로 교체, DB 복구·기록 실패 fail-closed·경보 승격/초기화 신규 |

바꾸지 않은 것: 과거 체결 자동 재생(여전히 금지), 현재 세션 주문의 보류, fault 뒤 신규 BUY/분할 SELL 차단, `order_unknown.json` 당일 규칙, 운영 설정·예약·매수 중지.

**35차 설계 결정의 번복(P1-2).** 35차는 "session 시작 기록조차 실패하면 기록 없는 주문이 다음 시작에서 사라질 수 있어 전량 포함 새 주문을 보류한다"로 정했고 독립 리뷰가 "최초 RUN_OPEN 실패 뒤 미기록 SELL"을 P1로 잡아 테스트(`test_initial_open_failure_cannot_send_unmarked_emergency_sell`)로 고정했다. 이번에 그 테스트를 새 계약으로 교체했다. 근거: 막힌 손절은 상한 없는 손실이고, 미기록 전량 SELL은 KIS 매도가능수량이 이중 매도를 막으며 체결은 잔고 동기화와 기동 시 거래소 미체결 대사(`get_exchange_open_orders`)로 반영되는 유한한 대사 공백이다. 전송은 ERROR 로그를 남기고 `execution_recovery_status()`는 `storage_fault`를 유지하며 그 세션은 clean close가 불가해 다음 기동이 보류(→ acknowledge 절차)로 이어진다.

**버전 호환 조건(독립 리뷰 P1 반영).** `acknowledge` 이벤트가 든 원장은 47차 이전 코드(df1a5af 포함)가 `알 수 없는 이벤트`로 손상 판정해 열지 못하고, 그 코드의 P1-2 동작으로 손절까지 전 주문이 거부된다. 그래서 runbook은 "봇 코드가 47차 이후일 때만 CLI 실행, ack 뒤 이전 SHA 롤백 금지, 미체결 0 확인 후 실행"을 조건으로 둔다. ack 이후 `reconcile_execution_evidence` 보고서의 `unclean_session_window_unknown`은 사실 보존을 위해 계속 남는다.

## See — 검증

- 신규 테스트와 인접 7파일: 아래 "검증 기록" 참조.
- 전체 verify: 아래 "검증 기록" 참조.

## 2차 수정(P1-3·P1-4)의 독립 리뷰와 반영

독립 리뷰(요청 Claude Opus / high, 작성자 아님, 같은 공급자라 교차 공급자 리뷰는 아님) 결론 **조건부 승인** → 전부 반영:

| 지적 | 반영 |
|---|---|
| P1 식별 SELL의 DB 복구(`recover_trade`)가 루프 안에서 동기 `future.result(timeout=10)`로 다른 종목 손절을 막음 | `asyncio.wait_for(asyncio.to_thread(...), 5)`, 예외/시한은 복구 실패로 처리 |
| P2 비식별 체결까지 미귀속으로 기록돼 대사 자료 오염 | `identified` 체결만 미귀속 기록, 레거시 경로는 종전대로. 대조 테스트 추가 |
| P2 특성화 테스트 1건 홈 미격리(실제 `~/.cache` mkdir 시도) | `Path.home` 패치 추가, 전체 verify 격리 위반 0 재확인 |
| P2 미귀속 종목 BUY 거부가 감사 원장(`record_blocked`)에 안 남음 | 킬스위치·접수 불명과 같은 형식으로 기록 |
| P2 오래된 주석("commit 확인 뒤 반환")·runbook "30분 동기화" 오기 | 원장 정의에 맞게 주석 갱신, 주기(장중 30초·장외 300초) 정정 |
| 성공 동기화의 초기화를 테스트가 수동으로 수행 | 실제 `_sync_portfolio` 성공 경로 초기화 테스트 추가 |

리뷰어 판단 중 수용한 것: 재시작 뒤 종목 보류가 사라지는 것은 장부 연속성 문제이고 돈 안전(KIS 원천)이 아니므로 수용, 다음 미귀속 체결이 표시를 되살린다. `trade_journal is None` 기동도 종전(전역 fault + 동기화 정지)보다 좁다. 후속 권고(미반영): 기동 시 JSONL 미대사 행 경고, `journal_unattributed` 상태의 대시보드/ops_check 소비자, BUY 머리 게이트의 종목별 미귀속 인지.

## 10월 6일 전 남은 결정

1. **이 수정의 배포 시점(사용자 결정).** 운영은 수정 전 df1a5af이고 10월 6일 활성화 프로필은 old/new HEAD 모두 df1a5af라, 이 PR을 먼저 배포하면 `rev-parse HEAD != old_head`로 10월 6일 활성화가 중단된다. 선택지는 (a) 10월 6일 관측 종료 뒤 배포, (b) 지금 배포 + 프로필 재무장(47차 준비 절차 재실행). 어느 쪽이든 **배포 전까지 `acknowledge` CLI 실행 금지** — 운영 코드가 그 이벤트를 손상으로 판정해 손절까지 전 주문을 거부한다. 그 사이 운영에서 비정상 종료(OOM·미체결 재시작·긴급 CLI)가 나면 P1-1 보류는 배포 뒤에야 풀 수 있다.
2. ~~P1-3·P1-4 수정 여부~~ → 2차 커밋에서 수정. 남은 설계 선택: `unattributed_executions.jsonl`의 운영자 대사 절차(runbook 기재)와 종목 보류 해제(프로세스 수명 → 재시작으로 해제, 영구 아님).
3. 10월 6일 기동 전 점검 추가: PostgreSQL 연결, 042700 DB 행 수량(3/2)과 `trade_id=1개` 복원 로그, 긴급 CLI 사용 금지 공지.
4. 적용 후 첫 주에 `execution_recovery_status()`를 ops_check에 노출.

## 검증 기록

| 단계 | 명령/범위 | 결과 |
|---|---|---|
| 리뷰 기준본 | `scripts/dev/verify.sh` @ 0843c54 (수정 전) | 4045 passed / 2 xfailed, 186.81초, 격리 위반 0 |
| main 병합 뒤(#139/#140 포함, 85a7e44) | `scripts/dev/verify.sh` | **4078 passed / 2 xfailed**, 134.00초, 격리 위반 0, 비밀정보 검사 통과 |
| P1-3·P1-4 수정 뒤(2차 커밋) | 관련 8파일 217 passed → `scripts/dev/verify.sh` | **4084 passed / 2 xfailed**, 143.72초, 격리 위반 0, 비밀정보 검사 통과 |
| 2차 독립 리뷰 반영 뒤(최종) | `scripts/dev/verify.sh` | **4086 passed / 2 xfailed**, 132.31초, 격리 위반 0, 비밀정보 검사 통과 |
| 신규 + 인접 7파일 | `test_execution_ledger_acknowledge.py` 외 ledger/readonly/cancel_fill/order_post_unknown/recovery_evidence(+cli) | 220 passed, 7.84초 |
| 35차 테스트 교체 뒤 | `test_durable_execution_integration.py` + 신규 | 36 passed, 4.04초 |
| 독립 리뷰(요청 Opus/high, 작성자 아님) | 수정 전 코드로 신규 테스트 재실행 | 9개 중 6개 실패(ack 4·CLI 1·open_failure 1) → 결함을 잡는 테스트임을 확인. 조건부 APPROVE, 지적 전부 반영 |
| 최종 전체 | `scripts/dev/verify.sh` (수정 후) | **4054 passed / 2 xfailed**, 138.02초, 격리 위반 0, 비밀정보 검사 통과 |

이 세션은 운영 서버 상태를 바꾸지 않았다(배포 22:17은 다른 세션의 사용자 승인 작업). 운영 자료 접근은 저널 JSON·대시보드 API·systemd 상태 읽기 전용(수량·ID만).
