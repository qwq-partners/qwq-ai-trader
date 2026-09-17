# KR 실행 안전성 — 구현 중간 검증 (2026-09-17)

09-17 착수. 최종 로컬 검증·인계는 2026-09-18 KST에 확정하며, 날짜가 있는 문서 경로는 시작일을 유지한다.

## 판정과 범위

**전체 재설계 미완. 신규 안전 기반 모듈과 공식 KIS 계약 조사까지이며, 실제 KR 거래 경로는 교체하지 않았다.**
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
| 1 저장·적용 인터페이스 | SQLite 원자 checkpoint, 누적 inbox/cursor, commit→publish→receipt, 복구 장벽 | 실제 Portfolio/ExitManager/리스크 경제 reducer·큐 연결 |
| 2 주문·조회 증거 | 구조화 명령 결과, intent/attempt/claim, 예약·최종성 분리, 순수 파서 | 현행 TR의 취소/정정 체인 실응답 최종성 |
| 3 송신 검사 | 최신 위험 시도, 신뢰된 경로 분류, KST 마감, 공통 송신 장벽, 재전송 없는 transport | 실제 모든 KR 송신 호출점 배선·request fingerprint와 durable attempt 연결 |
| 4a 현행 조회 계약 | 별도 모듈의 요청/페이지/시각·cursor 보존, 오프라인48시험 | 거래 봇의 폴러 교체/스케줄러 연결 |
| 4/5 통합·최초 인계 | **미완·실경로 미교체** | 모든 writer 직렬화, sync cutoff, startup/outbox/health 및 실제 큐 고장 인수 |
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

## See

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

현재 시험은 C1–C8/F3/F5/G1–G5/R1에 해당하는 **모듈 경계 일부**를 검증할 뿐, 명세의 실제 큐/브로커/복구 인수를 대체하지 않는다.
F1/F2의 실제 경제·보호 동시 적용, F4 sync 양방향·cutoff, F6/F6a 실제 보호 소유권,
F7 lot/저널, F9 위험 카운터·V권, R1a 시작 중 실경로 차단은 전체 통합 시험이 남았다.
US 호환 시험과 US 실행 안전성 완료도 별개다.

후속 순서:

1. 완성한 legacy 조회 수집기를 실제 broker의 기존 limiter/응답 어댑터와 연결하는 통합 시험을 고정한다(직접 실 API를 호출하는 지시가 아님).
2. 실제 Portfolio/ExitManager/리스크 DTO·경제 reducer·outbox를 구현하고 실큐 인수시험을 RED부터 고정한다.
3. runtime을 단일 owner로 만든 뒤 engine/scheduler/broker/KOFR/수동/일일 초기화의 모든 writer와 송신점을 순서대로 옮긴다.
   정정의 수량/가격 증가분에 대한 추가 예약·위험 상한도 실제 request와 연결해야 한다. 현재 child attempt 추적/BUY 정정 alpha 검사만으로 노출 증가의 예산 인수를 대체하지 않는다. 지원 근거/예약이 없는 정정은 송신하면 안 된다.
4. 최초 인계·취소 체인의 부족한 증거는 별도 공식 명세/승인된 비식별 실응답으로 검증한다. 증거가 없으면 미지원/시작 차단을 유지하며 완료로 보고하지 않는다.
5. 실제 전체 C/F/G/R 인수 및 독립 broad 리뷰를 통과한 이후에만 main 통합·운영 전환을 별도 판단한다.

이 중간 브랜치를 임의 배포하거나, 안전 모듈만 존재한다는 이유로 기존 KR 오류가 해결됐다고 공지하면 안 된다.

## 최종 로컬 검증

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
