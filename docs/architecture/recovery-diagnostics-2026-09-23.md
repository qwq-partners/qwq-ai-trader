# N3 읽기 전용 복구 진단 exporter

## 목적과 권한

N2 완료 기준 `3f7a189` 위에서 이미 존재하는 단일 owner runtime의 부족한 복구
증거를 비식별 보고서로 만든다. 모듈만 구현한다. CLI, `/api/health`, 경보, 설치기,
운영 서비스에 배선하지 않는다. main 병합·배포·재시작·실 API·주문·설정·자동 복구는
이번 범위가 아니다. KIS만 거래/잔고 정본이며 Toss는 이 진단의 주문 증거가 아니다.

## N2 계약 보완

`runtime.health()`는 `day_admission_closed`를 통해 주입 clock을 실행한다.
producer health의 restart retry 내부 list도 원 RAM과 공유된다. 따라서 N2 제안의
health 원문 복사 대신 정확한 제품 타입에 한정한 **private memory adapter**를 쓴다.
health/clock/store/load/restore/audit/sweep/recovery 함수를 호출하지 않는다.
기존 health 구현 자체를 고치는 작업은 별도이며 이 exporter의 무부작용 보장으로
기존 health 전체를 승인하지 않는다.

## 인터페이스와 지원 범위

- `src/execution/safety/recovery_capture.py`:
  `capture_recovery_snapshot(runtime, *, captured_at) -> RecoverySnapshot`.
- `src/execution/safety/recovery_diagnostics.py`:
  frozen 안전 DTO와 `build_recovery_diagnostic(snapshot) -> dict`.
- `captured_at`은 호출자가 제공한 timezone-aware exact datetime만 인정한다.
  runtime의 clock이나 현재 시간을 새로 호출하지 않는다. UTC ISO로 표시한다.
- runtime/owner/producer/engine은 알려진 정확한 제품 타입만 지원한다. 없음·다른 타입·
  비정상 payload는 unknown/insufficient이며 예외 메시지/객체 repr를 반환하지 않는다.
- 캡처는 동기·무 await·무 lock 획득·무 재시도다. 지정 필드를 두 번 복사해 비교한다.
  owner state/version/published/version-latch, engine 게시 version/binding/ingress,
  runtime 실패·복구·task/command/reconciler·binding, producer episode/retry/restart/
  pending/recovery/stats/source/task RAM을 포함한다. task는 참조를 두 표본 동안
  유지하여 객체 동일성과 done/cancelled를 비교한다. 단순 개수 일치는 부족하다.
- JSON tree는 exact builtin 검증 뒤 복사한다. Decimal/datetime/정해진 dataclass/
  task는 명시적 adapter만 허용한다. 임의 `str/repr/bool/deepcopy/getattr/eq` hook을
  실행하지 않는다. 순환·깊이·노드 예산 초과는 고정 오류로 거부한다.
- 비교 범위/상한은 코드 상수와 테스트로 명시한다. 두 표본이 같아도 원자성/ABA 부재/
  durable 저장소 상태/브로커 최종성/실 설치 성공을 증명하지 않는다.

## 보고서 계약

항상 `schema_version=1`, `read_only=True`, `automatic_action_allowed=False`,
`trading_ready=False`, `installation_verified=False`다. fixed enum과 건수/버전만 내보낸다.
`snapshot_stable`과 `publication_consistent`는 독립 bool/None이다.
owner composite health는 저장소 단독 검사로 부르지 않는다. 불안정하거나 읽지 못한
표본의 건수를 정상 0으로 만들지 않는다. frozen snapshot에도 raw state/health/가격/
계좌·symbol·intent·command·order ID·자유문구·digest를 담지 않는다.
builder 결과의 중첩 변경은 snapshot이나 runtime을 바꾸지 않는다.

mode는 `unknown`, `partial_install`, `attached_candidate` 중 하나다. runtime 부재는
legacy 증거가 아니다. 배선 흔적이 완전해 보여도 성공 설치 receipt가 없어 candidate다.
일반 producer 배선 부재를 정상 legacy로 판정하지 않는다.
읽기 실패·volatile일 때도 mode는 unknown이다. 지원되는 안정 배선 표본만 partial/
candidate를 판정한다. `mutation_in_flight`는 지정 lock 중 하나라도 held이면 True,
읽지 못하면 None이다. commit await 중 정상적으로 게시 latch가 닫힐 수 있으므로
publication_inconsistent는 영구 결함 확정이 아니라 **관측 시점의 미확인**이다.

`counts_complete`는 안정 표본·진행 중 lock 없음·evidence_invalid 없음일 때만 True다.
False이면 findings에 없는 코드를0건으로 읽지 않는다. True도 **아래 구현된 진단 범위**
안에서만 건수 완전성을 의미하며 브로커 최종성/설치/A·C 역사/intraday source 검증은
포함하지 않는다. None planned risk는 SELL에서도 측정된0이 아니며 해당 위험 증거는
불충분(evidence_invalid)으로 남긴다. 정상 pending과 위험 증거 부족이 함께 나올 수 있다.

필수 owner 루트(attempts/intents/portfolio/outbox/protection) 및 보호의 states/
pending_owners/degraded 구조 부재는 지원 계약 위반으로 전체 unavailable이다.
반면 MARKET_DATA 핸들러 부재는 읽을 수 있는 미완 배선(partial_install) 사실이다.
exit_exempt는 자동 청산 판단만 면제하며 owner 체결 reducer의 보호 수량 원장은 유지한다.
따라서 면제 목록만으로 누락된 보호 행을 정상화하지 않는다.

findings는 `{code, count, evidence, next_check}`의 고정 whitelist로 결정적 정렬한다.
문제 없음이나 0도 거래/복구 허가로 변환하지 않는다. 필수 코드/의미:

- `snapshot_unavailable` / `snapshot_volatile`: 지원되지 않거나 변한 두 표본.
- `publication_inconsistent` / `owner_health_unconfirmed`: 게시 불일치/복합 건강 불명.
- `unapplied_inbox`, `observation_not_applied`: inbox 및 observed/applied 차이.
- `unknown_buy`, `remaining_reservation`, `terminal_reservation`: 접수 불명/양수 예약.
  수량뿐 아니라 cash/exposure/risk 쌍을 확인하며 None 위험은 측정된 0이 아니다.
- `cancel_unconfirmed`, `attempt_link_inconsistent`: 취소 ACK는 최종성이 아니며 parent/
  intent/symbol/side/scope 연결을 확인한다. 취소0·빈 응답은 소멸 근거가 아니다.
- `protection_unsubmitted`, `protection_link_inconsistent`, `protection_quantity_inconsistent`:
  raw outbox의 effect_source 키 부재와 null은 일반 보호이며 delivered도 검사한다.
  원 감사→intent/admission/attempt/RAM 연결을 확인한다. 초기 target은 현재 보유량과
  직접 비교하지 않는다(부분체결). 감사 target↔intent target, 현재 portfolio↔protection
  remaining을 구분한다. 다른 source는 일반 producer와 합치지 않는다.
- `protection_failure_unattributed`, `repair_only`: RAM 실패 귀속 불명/수동 조사 필요.
- `pending_sell`: 모순 없는 ACK 비종결 SELL은 진행 중이지 자동 재발행 대상이 아니다.
- `evidence_invalid`: 잘못된 타입/음수/NaN/Infinity/예약 쌍 누락은 증거 부족.
- `disposition_not_durable`: 항상 A stale/C abandoned의 durable 종목별 증거는 미지원.
  현재 RAM 합계로 과거 A/C나 실패 부재를 추정하지 않는다.

## 검증 및 역할

구현 작업자는 Astra/high, 독립 acceptance는 coordinator, critical 최종 리뷰는
작성자가 아닌 Astra/xhigh 및 실제 모델 확인 Opus5/xhigh다. native actual/effective
metadata가 없으면 미검증으로 기록한다. N3 외부 검토는 새 범위 최대2회×USD5/600초,
stdin에 선별한 비민감 소스만 전달하고 tools-off 유지. 작업자 fan-out은 금지한다.
같은 spec commit에서 별도 worktree/비중복 파일로 작업한다. 전체 suite는 모든 작업자가
종료한 뒤 coordinator가 UTC→KST 직렬 실행한다. 실제 성공 증거 전 완료/승격 금지.

테스트는 합성 상태와 실제 runtime/owner/producer 하네스를 사용한다. 모든 mutation,
health/clock/store/network 호출을 실패 spy로 막고 전후 상태·예약·version·RAM을 비교한다.
출력 비밀 sentinel/중첩 반환 변이, custom hook/순환/불량 수치, owner version 불변인
RAM/같은 개수 task 교체, partial 설치, 정상 부분체결, null source/역사 감사행을 인수한다.

현재 상한: 깊이32, clone node100,000, 단일 문자열16,384자, 합계 scalar 문자 예산
2,000,000(Decimal digits/파생 float·datetime·ZoneInfo key 포함), 정수256bit.
타입·scalar의 비공개 비교 증거로 `1 == True` 및 datetime→문자열/Task→유사 tuple
충돌을 막는다. 이 비교 증거는 보고서나 DTO에 저장하지 않는다. 정확한 ZoneInfo라도
from_file의 key는 사용자 객체일 수 있어 exact str/None과 길이를 별도로 검증한다.
