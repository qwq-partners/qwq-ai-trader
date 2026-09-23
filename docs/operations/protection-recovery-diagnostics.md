# 보호 경로 수동 복구 진단 — 읽기 전용 절차

이 문서는 **진단과 증거 수집 절차**다. pending 해제·취소·재주문·재시작·설정 변경을
승인하지 않는다. 현재 owner 엔진은 운영 미설치이며 `trading_ready=False`다.
운영 legacy와 개발 attached 상태를 섞어 판정하지 않는다.

## 1. 먼저 실행 모드를 확인한다

code SHA·process 시작 시각·관측 시각(KST)·legacy/attached 여부를 기록한다.
운영 상태의 원문이나 계좌·브로커 주문번호를 공유 문서/커밋에 넣지 않는다.

- legacy: owner/version/admission은 **해당 없음**이다. 기존 대시보드·일지·pending
  관측을 이용하고 owner 복구 절차를 실행하지 않는다.
- attached 개발/인수 환경: 이미 보유한 runtime에서 `runtime.health()`와
  `runtime.owner.state`의 복사본을 읽는다. 진단을 위해 runtime을 새로 설치하거나
  저장기를 새로 열지 않는다. 생성자/기동 sweep은 읽기 전용으로 간주할 수 없다.
- 일반 `/api/health`에 owner 진단 exporter는 아직 배선되지 않았다. 운영용 CLI나
  자동 복구 명령이 있다고 가정하지 않는다. 없는 접근 경로는 **미지원**으로 기록한다.

## 2. 일관된 증거를 고정한다

동일 런타임에서 owner.version, owner.published_version, engine._execution_version,
store_healthy, publication_recovery_required와 health의 pending/실패 계수를 읽는다.
읽기 전후 owner version이 다르거나 게시 version이 맞지 않으면 불안정 스냅샷이다.
같은 version의 안정 스냅샷을 두 번 얻기 전에는 서로 다른 시점의 사실을 연결하지 않는다.

원 상태를 반환하는 API를 새로 만들 때에는 복사본만 제공해야 한다. 진단 함수가
mutable owner 참조를 반환하거나 비동기 조회 사이에 바뀐 RAM을 같은 시점으로 포장하면
안 된다. 외부 브로커 증거가 필요하면 **별도 승인된 읽기 전용 과정**으로 확보한다.
Toss 조회 자료는 KIS 주문/잔고의 정본 또는 취소 최종성 증거가 아니다.

## 3. 원인별 분류

| 관측 | 진단 | 필요한 추가 증거 / 현재 행동 |
| --- | --- | --- |
| owner unhealthy 또는 version 불일치 | 저장/게시 결과 불명 | commit·게시 receipt와 원 버전 대조. 장벽 유지 |
| unapplied_inbox>0, observed≠applied | 관측과 경제 적용 사이 | 같은 attempt의 inbox·적용 receipt·예약 대조. 수량 보정 금지 |
| BUY가 blocked_unknown 또는 예약 잔존 | 접수/체결/노출 불명 | KIS 원 주문·체결·잔고의 동시점 근거. 시간 경과로 해제 금지 |
| cancel child 미확정 또는 parent 연결 부족 | 취소 체인 불명 | child/parent 인과·최종 응답 명세. 취소0·빈 조회를 소멸로 읽지 않음 |
| protection_decision은 있으나 intent/admission/RAM 연결 없음 | 미제출 또는 재시작 유실 | 원 command/intent/admission receipt 확인. 새 SELL 생성 금지 |
| 연결된 symbol/target_quantity와 현재 보유/보호 수량 불일치 | 보호 증거 불일치 | 뒤늦은 체결·수동 거래·원 결정 시점 비교. 현재 수량으로 자동 재작성 금지 |
| A stale/C abandoned 표식 존재 | 해당 source 판정 보류 | 원 입력/version·명령 적용 결과 확인. 다른 source 관측으로 장벽 해제 금지 |
| 재시작으로 A/C 판정 근거 없음 | disposition_not_durable | unknown으로 유지. 빈 RAM을 '실패 없음'으로 해석하지 않음 |
| repair-only·unattributed·행 없는 실패 | 영향/commit 결과 귀속 불명 | 원 실패와 durable receipt를 사람이 조사. 감사행 삭제 금지 |
| 정상 ACK SELL pending | 진행 중인 주문 | 동일 주문 관측을 기다림. 동일 수량 재발행 금지 |

`ProtectionProducer.health()`의 recovery_required/retained_decisions는 현재 RAM의
관측 투영이다. 원 durable 증거의 대체재가 아니며 재시작 뒤 비어 있을 수 있다.
`runtime.health()`의 readiness=False를 진단기의 '문제 없음'과 바꾸지 않는다.

## 4. 호출하면 안 되는 함수

읽기 전용 검사에서 다음을 사용하지 않는다.

- `ProtectionProducer._audit_recovery`: 이름과 달리 recovery/stat을 변경한다.
- producer sweep/on_market_data, resume_protection_admission, repair_protection,
  release_protection_pending, owner.mutate, store.commit.
- clear_pending, rollback_stage, 임의 TTL 삭제, 재생/초기화 실행, broker 조회/송신.

진단 exporter를 구현할 때에는 이 함수들을 '호출 시 즉시 실패' spy로 바꾸고,
진단 전후 owner·RAM·예약·게시 version deep equality를 시험한다. 현재 절차 문서가
이러한 exporter 구현·운영 설치를 완료했다는 뜻은 아니다.

## 5. 사람이 내릴 수 있는 판정

진단 결과는 원인 분류, 부족한 증거, 영향 범위, 다음 조사 담당까지만 기록한다.
`read_only=true`, `automatic_action_allowed=false`를 명시한다. 확인된 문제가 없어도
"자동 복구 가능", "실거래 준비 완료", "C/F/G/R 통과"로 승격하지 않는다.

실제 복구가 필요한 경우 별도 변경 명세에 종목/원 intent·command, 기대 owner version,
보존할 예약·감사 증거, 실패 시 정지 상태, 공식 최종성 근거, 독립 리뷰를 지정한다.
그 명세가 없으면 현 장벽을 유지한다. 재시작이나 legacy 자동 복귀로 우회하지 않는다.
