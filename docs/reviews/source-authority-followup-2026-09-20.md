# 입력 소비 권한 후속 — Plan–Do–See (2026-09-20)

## 범위

승인된 C2a 정책 변경 이력 뒤의 연속 작업이다. 순서는 N1 요청 오류 분리 → C2b 완료 후 입력 변경 전파 → 실제 2분 owner/caller → 정오·LLM·보호 재생이다. 기반 `77c3f5d`에 더해진 C2a의 한정 승인·4053시험은 [별도 보고서](policy-generation-remediation-2026-09-20.md)에 보존한다. 과거 3933시험 인계로 되돌리거나 기존 작업을 다시 구현하지 않는다.

KIS만 거래·잔고를 담당하고 Toss는 관측 전용이다. main/운영·실 API·주문·설정은 변경하지 않는다. `trading_ready=False`, 모든 MODIFY 미지원, 공식 최초 인계/취소 체인 증거 부족은 유지한다. 이번 시험이 수익성·실거래 준비를 입증하지 않는다.

## N1 — 잘못된 retained source 요청의 오류 범위

### Plan

- Terra/high: 실제 owner/SQLite 요청 경계 테스트와 별도 `feature/retained-input-n1-20260920` 워크트리의 소규모 수정. 부모가 유일한 통합 담당이다.
- Astra/high: C2b의 공통 검사기·역사적 증거·종료 drain 계약을 읽기 전용 병렬 점검.
- Opus5/xhigh: 도구 없는 독립 정적 N1 리뷰. 실행 완료와 코드 승인을 별도로 확인한다. 네이티브 모델명은 요청 값이며 도구가 실제 식별자를 별도로 노출하지 않았다.

### Do

없는 operation, 선언 lane 불일치, pending/failed/stale terminal을 schema1 값 기반·schema2 등록 이력 기반에서 각각 검사했다. 수정 전 **10 행동 RED / 8 대조 GREEN**이다. ValueError가 detached 결과 저장 task까지 전파돼 runtime을 unhealthy로 만들고 복원 후에도 종료 실패가 남았다. 원본 probe와 수정 전 결과는 개발 원장에 보존했다.

`_retained_reads`가 기존 retained 선택 검사를 공유한다. 최초이며 현재 유효한 seal만 command 결과 접수 전에 검사한다. malformed reseal은 기존 durable conflict, 이미 오래된 요청은 기존 stale receipt 경로를 유지한다. reducer 검증·역사적 cutoff·실제 SQL/게시 실패 차단은 제거하지 않는다. 결과 실패 latch를 초기화하거나 ValueError 전체를 무시하지 않는다.

### See — 제품 한정 승인

구현자: 신규18시험1.78초, 관련104시험9.24초, 인수4시험1.52초 통과. 부모 통합 후 관련 **126 passed / 13.28초 / UTC**, 격리 위반0. source SHA `b578b968b84025f16bfbd4d82bf7fd0dba075d27226c4aef43026087fb7c38b5`, 신규 test SHA `adba409571e22d39e0c202dfd7e45e8177e2da2c047af7b33a2528971783bbcf`로 구현자와 통합본이 일치한다.

전체 **UTC4071 passed / 2 known xfailed / 4기존 warnings / 180.53초**, **KST4071 / 2 / 4 / 181.45초**, 각각 exit0·격리0이다. 문법·비밀정보 패턴 검사도 통과했다. valid retained 대조는 같은 날 자료이며 전일 인수로 보고하지 않는다.

실제 `claude-opus-5`, 요청 xhigh의 정적 리뷰는 **APPROVE_THIS_SLICE**, spec/quality PASS·차단0, 326.685초/child0/terminal success로 완료됐다. input139472바이트/SHA `02df65a8a3e0996ffd785bd43d5cf03236ed68fcd58ed7817b6e5a96e32c1da1`, 메타데이터+답변 보존본 SHA `681a446c2cac77f516de33ede27412e040551215d81c183813ff1c124abac675`. 독립 실행 시험이나 전체 브랜치 승인이 아니다.

비차단 F1–F6 처분:

- F1: 부모/작성자가 실제 day prepare→rollover→resume와 deferred ingress 경로를 읽어 확인했다. 정상 재개는 generation/fence/day를 바꾸므로 기존 ticket의 stale가 해제되지 않는다. `owner.state`는 deepcopy이며 precheck 전 await는 없다. 임의 raw checkpoint 변조·비단조 시험 시계·미이행 외부 writer까지 불가능하다고 확대하지 않는다. reducer가 최종 권위인 경계는 유지한다.
- F2: retained는 accepted completion을 **await한 뒤** seal한다. 아직 pending인 동시 요청이 나중에 우연히 성공할 가능성에 의존하지 않는다.
- F3: 승인 이후 source 무변경으로 실제 outer command scope의 malformed seal 회귀2개를 추가했다. 작성자20passed/1.92초·부모20passed/2.02초·각각 격리0, 최초 GREEN이며 추가 결함2건이 아니다. 위 전체4071은 이2개 추가 전 결과다. 최종20시험 SHA는 `33cad08d81a6709e7eb9ac23cb570329ac98eae014a724da61facfedeac0363d`이다.
- F4/F5: helper 전제·방어 조건 주석 권고는 deferred minor로 남긴다. F6 빈 retained 요청의 추가 context 검사도 미실측 최적화 권고로 보류한다. 마지막 broad 리뷰에 전달하며 코드 승인 지문을 무의미하게 바꾸지 않는다.

최종20시험 포함 전체 재실행은 **UTC4073 passed / 2 known xfailed / 4기존 warnings / 178.52초**, **KST4073 / 2 / 4 / 181.18초**이며 각각 exit0·격리0이다. N1의 한정 승인으로 C2b 구현을 시작한다. 실제 caller·전체 후속 완료는 아니다.

## C2b — 완료 후 입력 변경 전파

Plan: 이미 accepted된 receipt/history는 보존하면서, 현재 결과를 다시 사용할 수 있는지 공통 검사한다. accepted-only 필수 의존, 선택 관측의 absent/pending/실패/충돌 사실, 명시 retained의 역사적 동일성을 구분한다. 실제 source pending의 첫 await 장벽과 전이적 의존도 함께 검증한다. 완료 hook 자신의 정책 변경을 source 변경으로 오인하지 않도록 정책 이력 비교와 분리한다.

원래 별도 워크트리의 실제 RED7/대조2를 보존했다. 독립 읽기 전용 재실행 UTC1.95초/KST1.89초, 격리0. 이는 새 구현 통과나 독립 승인 근거가 아니다. schema2 own-hook 후 재사용, 실제 rollover/resume의 전일 retained, 취소·SQL 장애·observed-only 연쇄 등을 추가 인수한다.

Do: N1 한정 승인 후 Astra/high가 별도 `feature/source-authority-impl-20260920` 워크트리에서 source/seal 모듈과 집중 시험만 소유한다. 새 동기 `read_source`는 현재 소비 가능성과 실제 terminal 결과를 분리한다. N1 통합본 위에서 부모가 원본9시험을 다시 실행해7 RED/2 GREEN(UTC2.74초·격리0)을 확인했다. 구현·독립 See는 진행 중이다.

추가 실제 경합 RED: begin 사전검사 이후 저장 조회를 기다리는 동안 다른 facade가 의존 source 갱신을 접수하면 reducer가 정상 거부하지만 결과 task 실패 latch까지 켜진다. reducer의 이 재검사에만 전용 예외를 두고 외부 caller에는 기존 ValueError를 돌려주는 좁은 경계를 승인했다. SQL/조회/게시 오류 포착이나 latch 초기화는 금지하고, 취소된 caller의 drain·다른 접수 요청의 장벽·거부 요청의 durable 행 부재를 함께 검증한다. 아직 수정 완료/리뷰 승인 근거는 아니다.

## 이후 순서와 미완

1. C2b의 실제 owner/SQLite 인수·독립 리뷰.
2. 명시 기준선을 가진 RegimeOwner와 실제 2분 루프. 기존 두 지수 조회 횟수·동시성·VIX/전문가 순서·하트비트를 유지하며 2분에서 보호 적용을 새로 추가하지 않는다.
3. 정오 cap 선행 commit, LLM 지연/역순 결과 차단, 실제 보호 변경과 replay 원장의 같은 commit, cold restore.
4. 별도 morning diagnosis와 나머지 writer, 전체 C/F/G/R·독립 broad 리뷰. 공식 증거 부족은 미지원/시작 차단으로 남긴다.

main 통합·운영 전환은 자동 후속이 아니며 별도 판단이다.
