# KR 실행 안전성 — 구현 중간 검증 (2026-09-17)

09-17 착수. 최종 로컬 검증·인계는 2026-09-18 KST에 확정하며, 날짜가 있는 문서 경로는 시작일을 유지한다.

## 판정과 범위

**전체 재설계 미완. 기반·공식 KIS 계약 조사에 이어 Task4b 경제/보호·실제 core 큐 연결을 오프라인 검증했다. 운영 KR 거래 경로는 교체하지 않았다.**
feature/engine-safety-design-20260917, 기준 main `465a029`, 상세 설계 커밋 `da90741` 위에서 진행했다.
main merge·운영 SSH/systemctl·배포/재시작·실주문·설정/킬스위치·상태 파일 변경은 하지 않았다.
현재 프로세스/PID/실계좌 건강성을 조회한 보고서가 아니다.

KIS는 거래·취소/정정·체결·계좌·잔고의 유일한 제공자다. Toss 서비스·코드·승인 일정·만료·추가 KIS 조회0 원칙은 이번 작업에서 변경하지 않는다.
수익률·위험/청산 수치·배분을 바꾸지 않았고, 수익 개선이나 투자 엣지의 검증도 아니다.

## Plan

사용자 승인 상세 설계: [명세](../superpowers/specs/2026-09-17-engine-execution-safety-design.md).
[6작업 실행 계획](../superpowers/plans/2026-09-17-engine-execution-safety.md)을 만들고 TDD/구현자·리뷰어 분리 방식으로 진행했다.

| 작업 | 실제 상태 | 아직 증명하지 않은 것 |
|---|---|---|
| 1 저장·적용 인터페이스 | SQLite 원자 checkpoint, 누적 inbox/cursor, commit→publish→receipt, 복구 장벽 | 운영 상태 최초 인계·전체 복구 |
| 2 주문·조회 증거 | 구조화 명령 결과, intent/attempt/claim, 예약·최종성 분리, 순수 파서 | 현행 TR의 취소/정정 체인 실응답 최종성 |
| 3 송신 검사 | 최신 위험 시도, 신뢰된 경로 분류, KST 마감, 공통 송신 장벽, 재전송 없는 transport | 실제 모든 KR 송신 호출점 배선·request fingerprint와 durable attempt 연결 |
| 4a 현행 조회 계약 | 별도 모듈의 요청/페이지/시각·cursor 보존, 오프라인48시험 | 거래 봇의 폴러 교체/스케줄러 연결 |
| 4b 경제/보호·core 큐 | 실제 Portfolio/ExitManager/RiskManager projection, 중요 이벤트/receipt, 부분체결·취소/게시 고장 인수 | 운영 설치·모든 writer·보호 repair·일자전환·initial R/외부 원장 |
| 4/5 통합·최초 인계 | **미완·운영 경로 미교체** | 모든 writer 직렬화, sync cutoff, startup/outbox/API health 및 전체 C/F/G/R 인수 |
| 6 중간 See | 모듈 교차 리뷰·전체 호환 검증·문서·feature 저장 | 전체 재설계 인수/배포 승인 |

실제 배치: 기존 Astra/high 2개를 재사용하고 부모가 guard/transport/ExitManager 준비 변경과 통합 판단을 맡았다.
당시 추가 생성 슬롯이 부족해 fresh reviewer/xhigh/저가 모델 실행을 주장하지 않는다.
저장/체결과 주문/증거의 파일 소유를 나눠 병렬 작업했으며, 기존 live 파일의 병렬 수정은 하지 않았다.
같은 코드의 구현자와 승인자를 분리했다(주문/증거 구현은 부모 리뷰, 저장/가드는 다른 하위 에이전트 리뷰).

## Do

### 안전 기반 모듈

- `store.py`: 명시 절대 경로만 사용, SQLite WAL/FULL·단일 writer·version CAS·commit ID/payload digest.
  손상/미지원 DB를 빈 DB로 덮어쓰지 않는다. 새 DB의 빈 checkpoint는 일반 저장 계층의 값이며, 실행 준비가 됐다는 뜻이 아니다.
- `application.py`: 누적 수량/대금/비용 차이를 한 번만 반영하는 인터페이스, durable inbox·고정 주문 identity,
  후보 commit 후 동기 publish, 실패 시 복구 장벽. 경제 reducer는 주입 경계이며 시험의 합성 reducer를 실제 거래 계산기로 오인하지 않는다.
- fill reducer의 쓰기 범위는 portfolio/protection/risk/lots/outbox와 **동일 단일 submit의 적용수량·예약**으로 제한한다.
  다른 attempt/intents/startup/inbox/cursor의 삭제·변경은 거부한다. runtime은 관측 저장→apply→최종 reconcile 순서를 따라야 한다.
- `lifecycle.py`: 취소 ACK/NOT_SENT/REJECTED가 원 주문을 해제하지 않는다. 미확정·관측/적용 불일치는 replacement0.
  늦은 실패와 체결이 모순되면 UNKNOWN을 유지한다. claim 복구는 POST 재송신 권한이 아니다.
- `evidence.py`: 범위/시간/행/페이지/수량 보존 검사. **TTTC0081R 단순 KRX 정규장 현금 주문 전량체결**이라는 제한된 합성 계약만 종결 후보로 해석한다.
  현행 TTTC8001R에 신형 필드 계약을 상속하지 않으며, 취소/정정/NXT/SOR/빈 목록은 자동 종결로 승격하지 않는다.
- `guards.py`/`transport.py`: 최신 실패가 이전 normal을 가리고, 제출 시점 현재 KST·severe·회복 대기를 검사한다.
  자동 BUY 정정도 alpha 검사를 받는다. 모든 거래 명령에는 공통 저장/게시/시작/claim 장벽이 있다.
  첫 await 전 주문 본문을 깊게 고정해 hashkey와 POST가 같은 본문을 쓰며 거래 POST는 재시도하지 않는다.
  **기존 `KISBroker._api_post`를 교체한 것은 아니다.**
- 기존 `ExitManager`에는 `persist=False`와 명시 시계/상태 경로 주입만 추가했다.
  기본값은 기존 파일 영속화를 유지한다. 실제 coordinator 후보 상태를 만들기 위한 준비이며, 현재 core/scheduler의 직접 쓰기를 제거하지 않았다.
- `queries.py`: 현행 TTTC8001R/TTTC8036R의 읽기 전용 수집기. 전체 범위·월 단위3개월·페이지 상한/timeout·다음 cursor/헤더·시각을 검증한다.
  외부 fetch/시계만 주입하며 실제 broker 인증/limiter/폴러 연결은 없다. raw row·계좌 파라미터·cursor를 repr/오류에 노출하지 않는다.
  페이지 완결과 거래 최종성을 구분하며 모든 결과의 `finality_supported`/`trading_permission`은 False다.

### 공식 GitHub 조사

정본: [고정 출처·파일 SHA256·해석 한계](../integrations/kis-execution-contract-2026-09-17.md).

현행 TTTC8001R/TTTC8036R은 공식 legacy 예제에 존재한다. 최신 예제와 TR이 다르다는 이유로 공식 자료가 없다고 판단하면 안 된다.
전체/체결/미체결 구분과 F/M→요청 N, D/E 종료를 확인했다.
공식 Postman에는 당일 주문내역 지연 가능성도 명시되어 있다.

독립 대조 결과, **확인한 공개 자료만으로** 다음 두 가지는 충분히 입증하지 못했다.

1. 잔고에 이미 포함된 체결과 이후 체결을 구별하는 최초 인계 cutoff/조회 지연 상한.
2. 원주문·취소/정정 자식행의 최종수량과 중복 집계 규칙.

이는 KIS가 해당 기능을 제공하지 않는다는 단정이 아니다.
추가 근거 없이 두 번의 빈 응답·취소가능 목록 부재·ACK만으로 자동 전환하는 구현을 하지 않는다.
오프라인 기반 작업은 계속 가능하며, 계약 검증과 운영 전환 승인은 별개의 조건이다.

## Task4b — 경제·보호·실제 core 큐 (09-18 후속)

기준은 기반 커밋 `db49c5f`다. 부모는 runtime/core, 기존 Astra/high 저장 담당은 economics, 생명주기 담당은 protection을 병렬 구현했다. 서로 다른 담당자가 교차 리뷰했으며 자기 구현의 독립 승인은 하지 않는다.

### 구현과 의도된 의미

- 경제 DTO는 실제 Portfolio/Position 필드와 fill-linked 위험 상태를 명시적으로 복원한다. 돈은 Decimal 문자열, 시각은 aware ISO다. legacy naive 시각 export와 위험 객체의 기존 시간 projection은 KST로 명시한다. 운영 원가·이전 날짜를 임의로 추정하거나 미측정 비용을0으로 바꾸지 않는다.
- 누적 대금의 차이와 기존 FeeCalculator의 **누적 추정 비용 차분**으로 현금·잔여 취득대금/매수비용·실현손익을 계산한다. 요율/반올림 규칙은 유지하지만 legacy 증분별 반올림과 원 단위 차이가 날 수 있다. 실제 징수 비용으로 표기하지 않는다.
- BUY 주문당 거래1회, 손실 청산 intent당1회, 손절 후 새 진입 체결의 V권 소모가 같은 checkpoint에 들어간다. 승패/연속손실의 기존 미호출 KR 정책을 새로 활성화하지 않는다. initial R과 모호한 추가 lot lifecycle 연결은 pending이다.
- 보호 DTO는 ExitConfig/PositionExitState 전 필드·면제/진입시각·레짐·pending 소유권을 포함한다. 초기 등록/매도/가격 계산은 실제 ExitManager를 메모리 전용 후보로 사용한다. 같은 주문 후속 체결은 재등록하지 않고, 별도 추가 주문 누적10% 도달 시에만 기존 stage/BE 리셋을 한 번 실행한다.
- SELL은 같은 pending intent만 stage를 진행한다. 기존 pending은1800초가 지나도 시간만으로 풀지 않는다. 보호 계산 실패는 확인된 경제 수량의 degraded이며 후속 체결로 자동 복구하지 않는다. 경제 보유0이면 존재하지 않는 보호 상태는 제거한다. 면제 set의 기존 공유 참조도 보존한다.
- `ExecutionFillEvent`는 큐 포화 때도 보존하고, 호출자는 core commit/게시 receipt를 기다린다. 호출자 취소가 수락한 체결 적용을 취소하지 않는다. DB commit 후 게시 실패는 복구 장벽, restore+재전달은 이중 적용 없이 ALREADY_APPLIED다.
- 현재가 view와 고점/BE/pending을 분리한다. 보호 명령은 직렬 coordinator로 처리하고 수락 후 caller 취소와 분리·작업 추적/종료 drain을 한다. 실패는 health에 남기며, 이것을 자동 복구 완료로 표시하지 않는다.
- 명시 설치한 core는 legacy 거래 이벤트와 직접 체결/가격 writer를 거부한다. pause 중에도 이미 발생한 누적체결은 처리하며 다른 이벤트는 계속 정지한다. startup 대기 중 취소도 queued waiter/종료 정리에 포함한다.

### 리뷰·재현 원장

| 범위 | Important 지적 | 조치 |
|---|---|---|
| 경제 I1 | 같은 누적수량에 저장된 관측과 다른 대금 수용 | 동일 수량이면 대금도 동일, 오류 시 경제/예약/cursor 미변경 |
| 경제 I2 | 과거 applied가 있는데 lot 누락을 새 addon으로 생성 | lot 존재·OrderRef/종목/attempt/intent/kind/누적수량·대금 연속성 검증 |
| runtime I1 | 초기화 await 취소가 finally 밖이라 receipt 유실 | startup부터 종료 보호 범위에 포함 |
| runtime I2 | pause가 체결 사실 반영까지 정지 | bound core에서 누적체결만 별도 dequeue |
| runtime I3 | caller 취소로 대기 중 가격 보호 명령 유실 | task 소유/취소 분리·종료 drain·실패 지표 |
| 보호 I1 | 별도 추가매수의 원수량/고점 누락으로 슬롯 가중치가 달라짐 | 기존 정책대로 original=현재 보유·high=max(기존,체결), stage reset만 주문당1회 |
| 보호 I2 | 진입시각 없는 정상 state 복원으로 보유기간 보호 우회 | 정상 state의 entry_times 연결 필수 검사 |

부모 연결 검사에서 SELL의 `exit` 분류 누락과 매도 면제 공유 set 재할당도 고정했다. 보호 시험 작성 중 stage=NONE에서 BE를 기대한1건은 기존 정책을 변경하지 않고 fixture를 FIRST로 바로잡았다. 수정 중 그룹 실패와 최종 동결 검증을 혼동하지 않는다.

초기 runtime RED는 모듈 부재, 이후 실제 SELL 잔량 불일치와 종료 후 수락을 재현했다. runtime의 Important3건은 **3 failed/11 deselected(1.46초)**로 재현한 뒤 각1건 시험과14건 통합을 통과했고, 종료 drain/실패 health 시험까지 최종16건이다. 경제의 Important2건은 **8 failed/47 passed →56 passed(0.41초)**이며 실제 SQLite 재시작·충돌 inbox 보존도 포함한다. 보호 Important2건은 **3 failed/41 passed →44 passed(0.35초)**로 검증했다. 부모의 최종 집중 그룹은 **159 passed(2.93초)**다(신규116+기존43). 모든 집중 실행은 격리0이었다. 최종 재리뷰/전체 결과는 아래 Task4b 검증 절에서 확정한다.

### 실제 인수와 미완 경계

| 명세 | 이번에 실행한 근거 | 남은 범위 |
|---|---|---|
| F1/F2 | 실제 큐 BUY40+60/SELL40+60·Portfolio/Exit 수량·현금/비용/횟수 | 실제 KIS 관측 어댑터·scheduler 발신 교체 |
| F3/F5a | 중복/과거 관측·SQLite reopen·commit 뒤 게시 실패 복구 | 모든 프로세스 종료점·실계좌 대사 |
| F5b | 보호 등록 실패 후 APPLIED+degraded·재전달 경제1회 | 별도 멱등 보호 repair |
| F6/F6a | stage/BE/high/R 보존 모듈 및 fill DB 대기 중 실제 quote 직렬화 | 모든 가격·레짐 writer 이전 |
| F7 | addon 정책1회·pending lot/outbox 유지 | 종결 initial R 확정·외부 journal 재시도 |
| F9 | 실제 RiskManager의 손실 intent·V권 게시/restore·중복 보존 | 날짜 인계·운영 재시작 전체 경로 |

`run_trader`에는 attach 호출이 없고 runtime의 trading_ready는 **항상 False**다. startup status를 임의 ready로 써도 거래 전송 API/허가가 생기지 않는다. 실제 API health 연결도 아직 아니며 `health()`는 로컬 점검 인터페이스다. 모든 scheduler/sync/KOFR/수동 writer를 옮긴 것으로 보고하지 않는다.

재사용 ExitManager의 기존 logger 호출은 후보 계산 중에도 발생한다. legacy 상태파일·외부 저널·네트워크 쓰기는 없지만, 후보 로그를 durable commit 증거로 쓰면 안 된다. process 강제 종료 중 아직 저장되지 않은 가격의 재수집/인계 역시 전체 복구 단계의 검증 대상이다.

### Task4b 검증 — 고정 후보와 재리뷰

Important7은 모두 수정 뒤 **다른 구현자의 한정 재리뷰에서 ADDRESSED**였다. 경제는 보호 담당, 보호/runtime은 경제 담당이 검토했다. 수정으로 새로 발견한 C/I/M0이며, 전체 live 통합 broad 승인으로 확대하지 않는다. 재리뷰는 제공된 수정 diff·회귀시험·구현 보고를 읽었고 직접 전체 시험을 재실행하지 않았다. 부모가 아래 검증을 별도로 실행했다.

소스/시험 동결 후보(부모 `db49c5f`에 대한 `git diff --cached -- src tests` SHA256):
`2500f28e620809fd37f68625c8738157d41aad11bffa2345769fb9ee6d68d8a3`.

| 환경 | 전체 verify | 격리·문법·비밀정보 패턴 |
|---|---|---|
| Asia/Seoul | 2192 passed / 2 known xfailed / 1 기존 warning, 91.95s | 접근 시도0·통과 |
| UTC | 2192 passed / 2 known xfailed / 1 기존 warning, 89.93s | 접근 시도0·통과 |

신규 시험116개(경제56·보호44·runtime16)를 제외하지 않은 전체 실행이다. 기존 xfail2는 손절 비용 기준/익절 접촉 parity, warning1은 pykrx 폐기 예정 API로 이번 범위에서 변경하지 않는다. 운영 자격/네트워크/상태 파일 없이 env-i와 테스트 격리 guard를 사용했다. 비밀정보 패턴 검사는 제한된 패턴 검사이지 저장소 전체의 비밀정보 부재 보증이 아니다.

운영 checkout의 Git HEAD는 `8ff2f55`/main 그대로임을 확인했다. 현재 PID·서비스·계좌 상태는 조회하지 않았다. scheduler/broker/run_trader·Toss·설정/운영 상태 경로는 이번 diff에 없다. 소스 동결 뒤 문서만 갱신하며 커밋·원격 확인은 결과 인계에 별도 기록한다.

최종 문서 한정 독립 검토는 C0/I0/M0였다. 구현/운영 경계·정책 설명·잔여 사항을 검토한 판정이며 소스 broad 리뷰나 시험 재실행이 아니다. 검토 시 UTC는 진행 중이었고, 완료 후 부모가 위 실측치로 표와 계획/CHANGELOG를 갱신했다. 양 시간대 실행 전후 소스/시험 해시는 동일했다.

## 기반 단계 See (`db49c5f` 당시)

### 지적·수정·재리뷰

| 검토 | 지적 | 조치/재판정 |
|---|---|---|
| 저장·체결 I1 | 주문번호 등 주변 공백 변형으로 같은 체결 중복 적용 | identity 주변 공백/합성 주문번호 거부, 재시작 회귀; ADDRESSED |
| 저장·체결 I2 | reducer가 예약·시작 장벽 root 삭제/변경 가능 | 명시 write-set, 단일 attempt·누적/예약 해제 한도, 원상태 보존; ADDRESSED |
| 주문·증거 I1 | 관측 체결 뒤 늦은 실패가 예약 해제 | 체결 모순 sticky UNKNOWN; ADDRESSED |
| 주문·증거 I2 | 관측보다 미래 KR 영업일을 증거로 승인 | KST 기준 미래 주문일 거부; ADDRESSED |
| 주문·증거 M1 | legacy 조회의 출처가 신형 TR로 표시 | 실제 요청 TR 출처 보존; ADDRESSED |
| 가드·전송 I1 | BUY modify가 alpha 검사 우회 | 취소만 alpha 예외, 매수 정정 검사; ADDRESSED |
| 가드·전송 I2 | 대기 중 mutable payload가 예약과 다른 주문을 송신 | 최초 await 전 깊은 JSON snapshot; ADDRESSED |

한정 재리뷰의 신규 Critical/Important0. 이 판정은 수정된 모듈 범위의 것이며 전체 live 통합 승인이 아니다.

추가 기반 broad 리뷰는 C0/I0/M1이었다. RiskSnapshotPublisher 설명의 주체가 뒤집힌 문장을 바로잡았다(동작 변경 없음).
검토자는 자신이 구현한 lifecycle/evidence를 독립 승인에 포함하지 않았고, 그 영역은 부모의 검토 근거를 사용했다.

Task4a는 부모의 페이지 provenance·월 범위 보완 후 Astra/high 독립 리뷰에서 **module GO, C0/I0/M0**였다.
고정 공식 예제와 요청·페이지 계약을 대조했고, 최종48시험 후보의 두 파일 SHA256도 일치했다.
소스 동결 뒤 전체 KST/UTC 검증을 순차 실행했다.

### 실행 근거

- 저장/적용: RED23 failed/31 passed → 경계 보강 후 **65 passed**, 격리0.
- 주문/증거: 초기 및 후속 RED 재현 후 **74 passed**, 격리0.
- 가드/전송: 수정 전 RED17 failed/59 passed → **76 passed**, 격리0.
- 메모리 전용 ExitManager3 + 기존 특성화40 통과.
- 부모 재실행: 주문/증거+가드/전송+ExitManager 합계 **193 passed / 1.41s**, 격리0.
- 신규 조회 시험을 제외한 중간 KST verify: **2028 passed / 2 known xfailed / 1 기존 warning / 87.34s**, 격리0. `--ignore=tests/test_kis_execution_queries.py`인 중간 실행이며 최종 전체 결과와 구분한다.
- Task4a: source 부재 RED와 provenance/날짜 경계 RED 후 최종48시험. 부모 재실행 **48 passed / 0.25s**, 격리0.
- 전체 UTC/KST 최종 결과: 아래 최종 검증 절 참조.

전체 verify의 비밀정보 패턴 검사는 알려진 몇 가지 패턴 검사이지 완전한 비밀정보 부재 보증은 아니다.
실제 API 호출/운영 자격 사용 없이 env-i·tmp_path·격리 guard를 사용한다.

## 인수와 남은 작업

기반 시험은 C1–C8/F3/F5/G1–G5/R1의 모듈 경계 일부이며, Task4b의 실제 큐·경제/보호 인수는 위 표에서 별도로 구분한다.
F4 sync 양방향·cutoff, 모든 writer 보호 소유권, F7 종결 lot/저널, F9 날짜 인계,
R1a startup 실경로 및 브로커/전송 전체 인수는 남았다.
US 호환 시험과 US 실행 안전성 완료도 별개다.

후속 순서:

1. 완성한 legacy 조회 수집기를 실제 broker의 기존 limiter/응답 어댑터와 연결하는 통합 시험을 고정한다(직접 실 API를 호출하는 지시가 아님).
2. Task4b DTO·경제/보호·실큐 위에 보호 repair·날짜 전환·initial R 확정·outbox 배출을 완성한다. 미측정 baseline을 가짜 정상 상태로 만들지 않는다.
3. runtime을 단일 owner로 만든 뒤 engine/scheduler/broker/KOFR/수동/일일 초기화의 모든 writer와 송신점을 순서대로 옮긴다.
   정정의 수량/가격 증가분에 대한 추가 예약·위험 상한도 실제 request와 연결해야 한다. 현재 child attempt 추적/BUY 정정 alpha 검사만으로 노출 증가의 예산 인수를 대체하지 않는다. 지원 근거/예약이 없는 정정은 송신하면 안 된다.
4. 최초 인계·취소 체인의 부족한 증거는 별도 공식 명세/승인된 비식별 실응답으로 검증한다. 증거가 없으면 미지원/시작 차단을 유지하며 완료로 보고하지 않는다.
5. 실제 전체 C/F/G/R 인수 및 독립 broad 리뷰를 통과한 이후에만 main 통합·운영 전환을 별도 판단한다.

이 중간 브랜치를 임의 배포하거나, 안전 모듈만 존재한다는 이유로 기존 KR 오류가 해결됐다고 공지하면 안 된다.

## 기반 커밋 로컬 검증 (`db49c5f`, 이전 근거)

소스/시험 동결 후보(부모 da90741에 대한 `git diff --cached -- src tests` SHA256):
`9f7b11947556187fc0429b8500759b68c70715a6abfd40876543371768c89855`.
검증 후 결과 문서만 갱신하며 소스/시험이 바뀌면 이 근거는 재검증해야 한다.

| 환경 | 전체 verify | 격리·문법·비밀정보 패턴 |
|---|---|---|
| Asia/Seoul | 2076 passed / 2 known xfailed / 1 기존 warning, 86.38s | 접근 시도0·통과 |
| UTC | 2076 passed / 2 known xfailed / 1 기존 warning, 89.76s | 접근 시도0·통과 |

신규 시험266개가 포함된 전체 실행이며 조회 수집기 시험도 제외하지 않았다.
기존 xfail2는 실거래/백테스트 손절·익절 parity 차이이며 이 작업에서 해소하지 않았다.
기존 warning은 pykrx의 importlib.resources.path 폐기 예정 경고다.
설정·Toss·운영 상태 파일은 staged 변경에 없으며 운영 검증/배포는 수행하지 않았다.
