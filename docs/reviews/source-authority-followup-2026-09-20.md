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

Do: N1 한정 승인 후 Astra/high가 별도 `feature/source-authority-impl-20260920` 워크트리에서 source/seal 모듈과 집중 시험만 구현하고 소유권을 반환했다. 새 동기 `read_source`는 현재 소비 가능성과 실제 terminal 결과를 분리한다. N1 통합본 위에서 부모가 원본9시험을 다시 실행해7 RED/2 GREEN(UTC2.74초·격리0)을 확인했다. 구현 동결본을 부모 워크트리에 이식했으며 독립 See는 진행 중이다.

추가 실제 경합 RED: begin 사전검사 이후 저장 조회를 기다리는 동안 다른 facade가 의존 source 갱신을 접수하면 reducer가 정상 거부하지만 결과 task 실패 latch까지 켜졌다. reducer의 이 재검사에만 전용 예외를 두고 외부 caller에는 기존 ValueError를 돌려준다. SQL/조회/게시 일반 오류 포착이나 latch 초기화는 없다. 같은 lane의 이전 source를 hard dependency로 삼는 기존 begin 접수→complete stale 의미도 자기 pending token 하나만 제외해 보존했다. 다른 facade의 같은 lane은 계속 장벽에 포함한다.

구현자 검증은 집중46시험(원본9+보강37), 관련15파일 UTC304/38.76초·KST304/39.08초, 별도 replay 완전성/무결성 UTC26/7.27초·KST26/7.32초, 각각 exit0·격리0이다. 보강37 전부가 새로운 RED는 아니며 actual day prepare→valuation→rollover→resume 이후 전일 retained, schema2 own-hook+실제 core fill/합성 journal ACK, 취소·SQL 전/후 장애·복원을 포함한다. cold restore는 같은 프로세스의 새 객체이며 실 API/실 PostgreSQL 검증이 아니다. 부모도 원본9시험+helper3의 AST 불변과 통합3파일의 SHA 동일성을 확인했다.

최초 동결 지문: source `6b6241b181c85db8e10d0f71706fcc20f4a70ba9aa00a37ee4141920a1f87601`, seal `86cb6ed4da88fa102f2032c5ca61fa33f346eb2b25ffb7a0a4abc9bb5d588709`, test `3f258d1d32b4c29f1ea6e9f63a3cba3d44f49cede11ad2b93ccb70422c48788c`. 최초 후보 전체는 **UTC4119 passed / 2 known xfailed / 4기존 warnings / 184.52초**, **KST4119 / 2 / 4 / 180.44초**, 각각 exit0·격리0이다. 문법·비밀패턴·diff 검사도 통과했다.

독립 Opus5/xhigh 리뷰는687.025초·child0·terminal success·오류0으로 실행 완료했지만 **REQUEST_CHANGES**다. 입력186204바이트/SHA `348be3426700b6edce4edfc9d13b5bca8d714c95ea1ce8b037d9ac2f463ae6d1`. Critical0, Important2: (I1) 읽은 의존이 없는 결과까지 같은 lane의 미저장 begin 때문에 영구 stale/input_seal로 기록되는 과잉 무효화, (I2) 충돌한 source의 직접 read/hard dependency 거부 인수 공백. 코드 판독 리뷰이며 독립 시험 실행이 아니다. 부모가 I1의 공통 검사기 호출 경계와 원 소비 의미를 확인했고, 별도 Terra/high 시험 작업에서 재현·직접 대조를 추가한 뒤 좁게 수정·재리뷰한다. 위4119통과를 승인이나 다음 실제 writer 착수로 해석하지 않는다.

선택 Minor10의 처분: 공개 ValueError 정확 타입과 legacy own-hook의 직접 read 대조는 시험으로 보강한다. 과거일 stale/missing 표시 차이는 `FinalEntryGuard.evaluate`가 둘 다 `observation_status != 'success'`로 동일 차단함을 부모가 확인했다. 사유 문자열/expected-version 우선순위/중복 deepcopy/도달 불가 삭제 경계/손상 상태 ValueError 매핑·성능 계측은 현 fail-closed/기존 API를 바꾸지 않고 broad 리뷰의 보류 목록으로 남긴다.

I1 보완: Terra의 별도 시험에서 실제1 RED/4 GREEN(UTC1.16초)을 확인했다. 부모는 `inputs()`에서 자기 lane을 제거하고 `inspect()`의 현재 소비 검사에서만 추가하도록 두 줄을 수정했다. 실제로 읽은 lane의 pending 차단은 그대로다. source 최종 SHA `15aa2ef64ebdf1095360d87a3449768da3d8f94b9b51806793199288546c856b`; seal/기존46시험은 무변경이다. I2의 충돌 직접 대조2개는 최초 GREEN이며 새로운 결함2건이 아니다.

신규5시험에는 실제 reducer 경합의 공개 ValueError 정확 타입과 legacy policy hook 이후 current 조회도 포함한다. 작성자51시험 UTC6.66초/KST6.05초, 부모 통합51시험6.27초 후 타 요청의 일시 게시 장벽을 읽지 않도록 그 완료를 먼저 await하는 시험 순서만 보완했다(단언 삭제 없음). 최종 test SHA `9da07054e47b5d81ffd399ff207e8194b585e126c80fc36ab8801636e179dfb9`, 부모51시험6.14초·격리0.

한정 재리뷰는 실제 `claude-opus-5`/요청xhigh·307.174초·child0·terminal success·오류0으로 **APPROVE_THIS_SLICE**, I1/I2 해소·새Important0이다. 입력87686바이트/SHA `f382e0b845ca280eb8809de23bb299ffbf0c38c866c0d2fc4cd00e0c321484d1`, 메타데이터+답변 보존본 SHA `b2db35534464558b5df985b0f079a4bc401cc788610cb579acf14c19d9365152`. 원본9 전문과 현재 helper를 입력에 포함했으며 AST12/12 불변은 부모 실행 근거를 따른다. 정적 reviewer가 실제 테스트를 실행한 것은 아니다.

최종 전체 **UTC4124 passed / 2 known xfailed / 4기존 warnings / 179.75초**, **KST4124 / 2 / 4 / 181.14초**, 각각 exit0·격리0. 문법·비밀패턴·diff 검사 통과다. 재리뷰의 선택 의견(첫-await 시험의 reason 단언 강화, helper 결합, Event observer 설명, lookup ID 필터)은 보류 minor로 원장에 남기며 승인 뒤 제품/시험을 다시 바꾸지 않았다. 이로써 C2b 소스 권한의 한정 gate를 닫고 실제2분 단위로 진행한다. 전체 engine/writer/운영 승인은 아니다.

## 이후 순서와 미완

1. C2b의 실제 owner/SQLite 인수·독립 리뷰: 위 한정 gate 완료.
2. 명시 기준선을 가진 RegimeOwner와 실제 2분 루프. 기존 두 지수 조회 횟수·동시성·VIX/전문가 순서·하트비트를 유지하며 2분에서 보호 적용을 새로 추가하지 않는다.
3. 정오 cap 선행 commit, LLM 지연/역순 결과 차단, 실제 보호 변경과 replay 원장의 같은 commit, cold restore.
4. 별도 morning diagnosis와 나머지 writer, 전체 C/F/G/R·독립 broad 리뷰. 공식 증거 부족은 미지원/시작 차단으로 남긴다.

main 통합·운영 전환은 자동 후속이 아니며 별도 판단이다.
