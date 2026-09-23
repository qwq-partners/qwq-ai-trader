# Main 안전 동작 정합화와 수동 복구 진단 — N2 설계

## 의도와 경계

09-23 사용자 요청의 두 번째 단계다. 현 main의 안전 강화와 미설치 단일 owner
개발선이 서로 덮어쓰지 않게 정합화하고, 복구가 막힌 이유를 변경 없이 진단한다.
사용자의 자율 진행·역할별 병렬 Plan→Do→See 요청에 따라 coordinator가 구현 범위를
선택한다. 이 설계는 main 병합·배포·실 attach·주문·수동 복구 변경의 허가가 아니다.

기준은 engine `78a253b`, main `afa6e1e`, 운영 관측 수정 후보 `3cfcdca`다.
후보의 독립 검토가 끝나기 전 운영 전환에 쓰지 않는다. 기존 readiness=False와
공식 취소/최초 인계 증거 미충족 및 C/F/G/R 차단은 유지한다.

## 선택

1. 전체 개발선을 main 위에 덮는 방식은 기각한다. main #80/#81/#83/#84/#88의
   BUY 해제·면제·살아 있는 분할 SELL·TR/페이지 강화가 사라질 수 있다.
2. main만 채택하는 방식도 기각한다. feature의 owner/예약/응답 증거/lease가 사라진다.
3. **main의 실제 행동 시험을 먼저 RED로 고정하고, 양쪽 경계별로 수동 통합한다.**
   텍스트 자동 병합도 의미 검토 대상이다. 과거 버그를 고정한26개 특성화 기대는
   유지/뒤집기/분할을 각각 기록하며 일괄 삭제·xfail로 숨기지 않는다.

## 정합 계약

- legacy에서는 main의 실제 장부·분할 의도·세대 재검사·면제·취소 생존 판정을 유지한다.
- attached runtime에서는 owner/gateway 이외의 경제 writer/송신을 허용하지 않는다.
  main에서 새로 등록하는 RiskManager.on_heartbeat도 runtime이 있으면 즉시 반환한다.
- 보호30초 cooldown과 일반 신호 cooldown 분리는 유지한다. N1을 재구현하지 않는다.
- 브로커는 기본 legacy/new TR opt-in·NXT 예외·연속조회 N/D/E 계약을 보존한다.
  feature QueryResponse·중복 헤더 검증·실패 HTTP 증거·획득별 limiter lease도 보존한다.
  legacy 기본 첫 페이지의 tr_cont 미송신과 execution 수집기의 명시적 빈 헤더 계약은
  호출 모드별로 구분한다. 관측 계측은 요청·반환·재시도를 변경하지 않는다.
- submit/modify retry=False, cancel의 기존 재시도 특성화는 그대로다. 정정 지원을
  추가하거나 취소0/빈 조회를 최종성 증거로 일반화하지 않는다.

## 읽기 전용 수동 복구 진단

우선 실제 진단 절차를 문서화하고, 별도 함수가 필요한 경우 다음 계약만 구현한다.
기존 runtime.health와 owner snapshot을 읽는 행위는 복구 실행이 아니다.

`capture_recovery_snapshot(runtime, *, captured_at)`은 owner/state, runtime.health,
게시 version을 복사하고 시작/끝 version을 비교한다. 새 네트워크·송신·mutate·commit은
금지한다. `build_recovery_diagnostic(snapshot)`은 순수·결정적·JSON-safe 분류다.
계좌·자격·브로커 주문번호·시세 원문은 보고서에 포함하지 않는다.

필수 분류: 게시 version 불일치, 저장 건강 불명, A stale/C abandoned 또는 지속 증거
부재, 미제출/불일치 보호 감사 결정, admission/RAM/intent/attempt 연결 부족, 수량
불일치, UNKNOWN BUY, 미확정 cancel child, 관측 미적용 inbox, repair-only/행 없는 실패.
증거가 없으면 unknown/insufficient이지 정상이나 복구 가능이 아니다.

모든 결과에 read_only=true, automatic_action_allowed=false를 둔다. 안정 스냅샷
두 번과 별도 승인된 브로커 증거를 사람이 대조한다. 결과가 비어도 readiness나
운영 이행 완료를 뜻하지 않는다. 실제 복구 변경은 종목·정확한 expected version·
원 명령/예약 증거·독립 리뷰를 갖춘 별도 작업이다.

금지: `_audit_recovery` 호출(이름과 달리 변경함수), resume/repair/release,
TTL 해제, 원장·감사행 삭제, 수량 재계산 덮어쓰기, readiness 강제, 자동 재시도 주문.

## 검증·자원

기준 main1982·feature5386은 역사 수치이지 병합 후보의 결과가 아니다. 새로운 후보는
main5파일 + 실제 attach/owner/gateway/계좌lease/설치 경계 + UTC/KST 전체를 검증한다.
전체 suite는 작업자가 모두 종료한 뒤 coordinator가 직렬 실행한다.

root가 통합을 소유한다. main 계약 고정은 Astra/high, 읽기 전용 분석 Sol/high,
critical 최종 리뷰 Astra/xhigh·검증된 Opus/xhigh를 사용한다. N2 외부 검토 예산은
독립된 새 변경 범위에서 최대2회×USD5/600초이며, 오늘 관측 수정 예산을 재사용하지 않는다.
실제 모델·비용·결과와 미완 차단을 기록한다. 기존 운영/사용자 작업 트리는 보존한다.
