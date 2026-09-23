# 보호 경로 수동 복구 진단 — 읽기 전용 절차

이 문서는 **진단과 증거 수집 절차**다. pending 해제·취소·재주문·재시작·설정 변경을
승인하지 않는다. 현재 owner 엔진은 운영 미설치이며 `trading_ready=False`다.
운영 legacy와 개발 attached 상태를 섞어 판정하지 않는다.

N3 모듈형 exporter의 계약/검증 상태는 [N3 설계](../architecture/recovery-diagnostics-2026-09-23.md)와
[결과 원장](../reviews/recovery-diagnostics-2026-09-23.md)을 확인한다. 운영 CLI/HTTP/경보
소비자는 배선하지 않는다. 아래 절차가 실 운영 접근 경로를 제공한다는 뜻이 아니다.

## 1. 먼저 실행 모드를 확인한다

code SHA·process 시작 시각·관측 시각(KST)·legacy/attached/partial_install 여부를 기록한다.
운영 상태의 원문이나 계좌·브로커 주문번호를 공유 문서/커밋에 넣지 않는다.

- legacy: 설치 시도/owner 복원 이력이 없다고 확인된 기존 경로다. 단순히
  engine._execution_runtime가 None이라고 legacy로 판정하지 않는다.
  owner/version/admission은 **해당 없음**이다. 기존 대시보드·일지·pending
  관측을 이용하고 owner 복구 절차를 실행하지 않는다.
- attached 개발/인수 환경: 이미 보유한 runtime의 지정 private memory를 N3 adapter로
  읽는다. `health()`는 clock 콜백과 retry 내부 list alias가 있어 exporter에서 호출하지
  않는다. 진단을 위해 runtime을 새로 설치하거나
  저장기를 새로 열지 않는다. 생성자/기동 sweep은 읽기 전용으로 간주할 수 없다.
- partial_install/unsupported_stop: factory의 복원 뒤 attach 전 실패 또는
  gateway/producer/reconciler 배선 실패다. 이미 존재하는 runtime 참조·engine과의
  동일성·설치 결과·owner 복원/version·gateway/producer 상태를 대조한다. 근거가
  없거나 불완전하면 이 범주/모드 미상으로 두며 legacy 자동 복귀를 허용하지 않는다.
- 일반 `/api/health`에 owner 진단 exporter는 아직 배선되지 않았다. 운영용 CLI나
  자동 복구 명령이 있다고 가정하지 않는다. 없는 접근 경로는 **미지원**으로 기록한다.

## 2. 일관된 증거를 고정한다

동일 런타임에서 owner.version, owner.published_version, engine._execution_version,
store_healthy, publication_recovery_required와 health의 pending/실패 계수를 읽는다.
`store_healthy`는 이름과 달리 owner.healthy의 복합 상태이며 직접 저장소 검사 결과가
아니다. 게시 실패/버전 차이도 false를 만들 수 있다. 독립 저장소 건강 검사는 미지원이다.

읽기 전후 owner version 변화는 불안정 증거다. 반면 owner/published/engine version
불일치가 안정적으로 관측될 수도 있다. **스냅샷의 안정성과 게시 정합성을 별도 축**으로
기록한다. 같은 owner version만 두 번 읽는 것으로는 충분하지 않다. producer의
episode/recovery/failure RAM, ingress/task 투영은 owner commit 없이 바뀔 수 있다.
두 캡처의 지정 private 복사본과 유지된 task 참조/상태도 일치해야 RAM과 owner 사실을 결합하며,
이를 확보하지 못하면 volatile/insufficient로 표시한다. 두 표본 일치는 원자성이나
중간 변화 뒤 원상복귀(ABA)의 부재, durable 저장소 검사 결과를 증명하지 않는다.
안정 캡처가 나올 때까지
자동 반복하거나 읽기를 안정화하려고 task를 멈추지 않는다.

원 상태를 반환하는 API를 새로 만들 때에는 복사본만 제공해야 한다. 진단 함수가
mutable owner 참조를 반환하거나 비동기 조회 사이에 바뀐 RAM을 같은 시점으로 포장하면
안 된다. 외부 브로커 증거가 필요하면 **별도 승인된 읽기 전용 과정**으로 확보한다.
Toss 조회 자료는 KIS 주문/잔고의 정본 또는 취소 최종성 증거가 아니다.
내부 캡처와 공유 보고서는 분리한다. owner.state의 account_scope/order_ref/가격과
health의 retained_decisions.price 등 원문을 통째로 직렬화하지 않는다. 공유/export는
허용 필드만 선택하고 식별자·가격 원문을 제거한다. 내부 증거를 문서/커밋에 옮기지 않는다.

## 3. 원인별 분류

| 관측 | 진단 | 필요한 추가 증거 / 현재 행동 |
| --- | --- | --- |
| owner unhealthy 또는 version 불일치 | 저장/게시 결과 불명 | commit lookup·게시 version/latch 대조. 독립 durable 게시 receipt는 미지원. 장벽 유지 |
| unapplied_inbox>0, observed≠applied | 관측과 경제 적용 사이 | 같은 attempt의 inbox·적용 receipt·예약 대조. 수량 보정 금지 |
| BUY가 blocked_unknown 또는 예약 잔존 | 접수/체결/노출 불명 | KIS 원 주문·체결·잔고의 동시점 근거. 시간 경과로 해제 금지 |
| cancel child 미확정 또는 parent 연결 부족 | 취소 체인 불명 | child/parent 인과·최종 응답 명세. 취소0·빈 조회를 소멸로 읽지 않음 |
| protection_decision은 있으나 intent/admission/RAM 연결 없음 | 미제출 또는 재시작 유실 | 원 command/intent/admission receipt 확인. 새 SELL 생성 금지 |
| 연결된 symbol/target_quantity와 현재 보유/보호 수량 불일치 | 보호 증거 불일치 | 뒤늦은 체결·수동 거래·원 결정 시점 비교. 현재 수량으로 자동 재작성 금지 |
| 당시 승인된 호출의 A stale/C abandoned 반환 증거가 보존됨 | 해당 source 판정 보류 | 원 입력/version·명령 적용 결과 확인. 합계 count로 종목을 추정하지 않음 |
| 재시작으로 A/C 판정 근거 없음 | disposition_not_durable | unknown으로 유지. 빈 RAM을 '실패 없음'으로 해석하지 않음 |
| repair-only·unattributed·행 없는 실패 | 영향/commit 결과 귀속 불명 | 원 실패와 durable receipt를 사람이 조사. 감사행 삭제 금지 |
| 정상 ACK SELL pending | 진행 중인 주문 | 동일 주문 관측을 기다림. 동일 수량 재발행 금지 |

`ProtectionProducer.health()`의 recovery_required/retained_decisions는 현재 RAM의
관측 투영이다. 원 durable 증거의 대체재가 아니며 재시작 뒤 비어 있을 수 있다.
A/C disposition은 mutating resume 호출의 반환과 메모리 합계로만 관측되며,
내구성 있는 종목별 표식이 아니다. 진단을 위해 다시 호출하지 않는다. 당시 반환을
보존하지 않았다면 합계·현재 RAM에서 역추정하지 말고 disposition_not_durable로 남긴다.
원 outbox 분류도 함께 확인한다. 일반 보호 생산자의 판정은
`row.get('effect_source') is None`인데 health의 protection_decisions_pending은
키 부재만 센다. 명시적 null은 unclassified 계수에 들어가므로 해당 계수0만으로
미연결 결정이 없다고 결론내리지 않는다. 이번 문서는 이 기존 차이를 수정한 제품이 아니다.
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
N2에서는 health 두 경로를 실제 owner/생산자 하네스에서 위 mutation·network 경계를
금지하고 전후 동등성/일부 반환 복사본의 중첩 변경을 시험했다
(`test_runtime_and_producer_health_reads_do_not_mutate_or_return_live_state`).
이는 한정 증거다. N3 감사가 확인한 restart_retries.intent_ids alias와 주입 clock
콜백까지 검증한 것은 아니다. 기존 health 구현을 무부작용 API로 확대 해석하지 않는다.
N3의 raw 표본은 함수 내부에서만 사용하며 반환 snapshot에는 고정 필드/건수만 담는다.
raw 객체를 보관·외부 재사용·공유하지 않는다.

## 4-1. 개발 모듈 호출 경계

```python
from src.execution.safety.recovery_capture import capture_recovery_snapshot
from src.execution.safety.recovery_diagnostics import build_recovery_diagnostic

def diagnose_existing_runtime(existing_runtime, observation_time):
    snapshot = capture_recovery_snapshot(existing_runtime, captured_at=observation_time)
    return build_recovery_diagnostic(snapshot)
```

호출자가 이미 보유한 runtime과 aware 관측 시각만 받는다. 시각/객체를 얻으려고
설치기·store·broker를 호출하지 않는다. 지원하지 않는 객체·결측·손상은 unknown/
insufficient다. 출력의 attached_candidate는 배선 흔적일 뿐 설치 성공 영수증이 아니다.
수동 소비자도 `installation_verified=False`, `trading_ready=False`를 유지해야 한다.
원문 가격/식별자를 별도 필드로 보충하거나 JSON default=str로 직렬화하지 않는다.

`mutation_in_flight=True`이면 지정 lock을 잡은 작업이 진행 중이다. 정상 commit의
SQL 대기 동안에도 게시 latch가 닫히므로 이때의 publication_inconsistent만으로
영구 고장을 단정하지 않는다. `counts_complete=False`이면 목록에 없는 진단을0건으로
간주하지 않는다. True도 구현된 owner 진단 범위 안의 뜻이며 A/C 역사·intraday source·
브로커 최종성·설치 성공을 포함하지 않는다. None planned risk는 정상 SELL에도 있을 수
있고 위험 증거 부족으로 남는다. pending_sell과 evidence_invalid의 공존을 자동 장애/
재주문 신호로 사용하지 않는다. 읽기 실패·volatile의 mode는 unknown이다.

## 5. 사람이 내릴 수 있는 판정

진단 결과는 원인 분류, 부족한 증거, 영향 범위, 다음 조사 담당까지만 기록한다.
`read_only=true`, `automatic_action_allowed=false`를 명시한다. 확인된 문제가 없어도
"자동 복구 가능", "실거래 준비 완료", "C/F/G/R 통과"로 승격하지 않는다.

실제 복구가 필요한 경우 별도 변경 명세에 종목/원 intent·command, 기대 owner version,
보존할 예약·감사 증거, 실패 시 정지 상태, 공식 최종성 근거, 독립 리뷰를 지정한다.
그 명세가 없으면 현 장벽을 유지한다. 재시작이나 legacy 자동 복귀로 우회하지 않는다.
