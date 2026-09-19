# C2a 정책 변경 이력 — Opus 후속 Plan–Do–See (2026-09-20)

## 범위와 판정 원칙

기준 feature HEAD `77c3f5d50162781f10565ac23f4a306680aa7948`의 미커밋 C2a 후보에 대한 후속이다. 복구된 실행기로 완료한 원 Opus 리뷰는 CHANGES_REQUIRED였고, 이번에는 그 지적을 실제 경로에서 재현·판정한다. 실행기 승인과 엔진 승인을 구분한다. main/운영·외부 거래 API·주문·거래 설정은 변경하지 않는다.

## Plan

- 부모가 B1 요청 거부와 B2 등록 수명, B4 복원 교차검증의 제품 코드/회귀/문서를 맡았다.
- Astra/high가 B2/B4/B6를 읽기 전용으로 독립 재현하고 경계 설계를 검토했다.
- Terra/high가 V1/V3/V4/V5를 실제 core queue/owner/SQLite로 검증하고 전용 테스트 파일만 작성했다.
- 마지막은 Opus5/xhigh의 도구 없는 독립 정적 재리뷰다. 실제 실행 결과와 코드 판정을 따로 기록하고, 실행하지 않은 시험을 독립 실행으로 주장하지 않는다.

## Do — 지적별 처분

| 지적 | 재현·판정 | 조치/범위 |
| --- | --- | --- |
| B1 미등록 selector 요청이 owner와 종료를 영구 차단 | P1 확정. 신규 7시험 중 4 RED/3 대조 GREEN | 최초 유효 seal의 미등록 선택만 result-task 접수 전 거부한다. 이미 sealed인 충돌·이미 stale인 원 요청은 기존 durable 판정을 유지한다. reducer 검사와 실제 SQL 실패의 failed latch는 제거하지 않는다. |
| B2 등록이 day/closing/drain 경계를 우회 | P2 확정. 독립 원본 3 RED 및 실제 rollover ID 소모 대조 | 기존 public owner 등록 경로에 runtime scope/guard를 주입한다. 첫 await 전 접수, lock 획득/lookup 뒤 day 재확인, closing 이후 새 접수 거부. 이미 접수된 작업은 closing에도 끝까지 drain한다. |
| B3 중복 검증 성능 비용 | 실측 없는 성능 의견, 기능 결함 미확정 | 검증 삭제/최적화는 보류한다. 긴 이력에서 측정 후 별도 작업으로 판단한다. 전체 시험 통과는 장시간 성능 보장이 아니다. |
| B4 checkpoint 내부 정합성만 검사 | P2 확정. SQL commits 불변인 이력 prefix 수정이 cold restore에서 healthy | 같은 SQLite 읽기 transaction에서 checkpoint와 policy-registration 전체 ID/version을 읽고 게시 전 양방향 대조한다. receipt 누락/이름 변경·registry 전체 삭제도 차단한다. 체크포인트와 commits의 전면 일관 재작성 인증은 제공하지 않는다. |
| B5 pure finalizer의 정렬 반환값 미사용 | 현 public caller는 이미 정렬하여 전달; 도달 가능한 오류 미확정 | 호출자가 canonical tuple을 전달하는 기존 계약 유지. 별도 API 확대 없이 변경 보류한다. |
| B6 중간 개발 schema1 호환성 | receipt 없는 중간 형태는 기존에도 fail-closed. 미배포 개발 후보 범위 | 최종 schema1은 schema/selectors/registrations를 모두 요구한다. 등록 없는 실제 legacy는 허용하고 이력을 자동 합성하지 않는다. 중간 형태가 지원 대상이었다는 근거가 생기면 명시 migration을 별도 설계한다. |

B2는 별도 detached result task를 만들지 않는다. command_scope가 caller task를 강하게 참조하며 기존 store._run은 취소돼도 SQL 종료를 기다리므로 scope가 실제 writer 종료까지 유지된다. 예상된 ValueError/닫힌 day 거부는 owner나 result-failed latch를 손상하지 않는다. 실제 SQL/게시 실패는 owner가 차단되고 shutdown은 command_state_drain_failed로 거부된다. 복원으로 상태를 확인하거나 새 runtime을 만들어야 하며, 단순 종료 성공으로 처리하지 않는다.

기존 시험 세 경우가 등록 후 SQL 취소/체결 ACK 유실·게시 실패에도 정상 shutdown을 가정했다. 이 가정을 명시 unclean cold-restore 인수로 바꿨다. 종료 실패를 임의 무시하지 않고 정확한 reason·unhealthy·pending=0을 검사한 뒤 임시 저장소를 닫아 새 runtime 복원을 검증한다.

## 인수와 원본 보존

- B1: 신규7 중 실제 4 RED/3 GREEN → 수정 후 기존 관련 합계83 GREEN.
- B2: 정규 lifecycle5는 수정 전 모두 행동 RED. PREPARED 거부 뒤 원 rollover ID 정상 사용, shutdown의 lock 대기 등록 drain, 종료 후 거부, lock/lookup 도중 실제 day fence 발생을 검사한다.
- B4: 정규 restore7은 수정 전 4 RED/3 GREEN. 내부 prefix·전체 registry 삭제·SQL receipt 누락/이름 변경을 잡고, 기존 단독 receipt 손상/개발 schema 거부와 실제 legacy 복원을 대조한다.
- V1/V3/V4/V5 원본 probe4는 최초 GREEN으로 기존 정상 경로 보강이지 결함 수정4건이 아니다. V1은 실제 prepare→dispatch→reconcile→core queue 체결로 pending sector만 해제하며 sidecar generation은 유지한다. V3 부분/상위집합 등록, V4 서로 다른 legacy/versioned 혼합·같은 selector 중복 거부, V5 실제 SQL thread gate/취소→durable→복원/멱등이다.
- V5 정규 이식에 B2 종료 차단 조건을 추가했을 때는 수정 전 실제 RED1이다. 마지막에는 warm restore 후 종료와 별도 새 runtime 복원을 모두 확인한다.
- V2는 B1, V6은 B4, V7은 B2로 처리한다. V8의 범위 한계는 유지한다: 여기의 큐/주문 transport는 합성 broker를 쓰며 라이브 송신·전체 스케줄러/모든 writer 인수는 아니다. 실제 PostgreSQL/저널 전 경로를 이번 시험으로 새로 검증했다고 주장하지 않는다.
- 독립 원본 audit는 4 RED/5 GREEN이며 보고서 SHA `47e37ed74f4f0a1d084c1312fdec57d795453283d4718605e8d17b25488ee596`, probe SHA `08e9e16fe339f75ed08d8956597dc5f3e576fe126c9c3cd875ee9d1f3f7e3ea3`. 인수 원본4 SHA `3722b7fe0d759cf4b80419a88c9ebcf3177975988da40cc6438aa6e9dad8cf06`. 원본을 수정하지 않고 정규 테스트를 별도 추가했다.
- probe 작성 중 snapshot을 begin() 이전과 비교한 실패, conftest 이중 수집 오류, 첫 정규 lifecycle의 미도달 health key 오기는 시험 작성 문제로 구분했다. 제품 결함 수나 제품 RED 증거로 합산하지 않는다.

## See — 한정 승인·최종 검증

부모 관련8개 파일: **178 passed / 17.60초 / UTC / 격리0**. B1/B2/B4와 정규 인수·기존 generation/seal/day recovery를 포함한다. 변경 직후 기존 cold-restore fixture 가정 3건이 실패한 결과도 보존하며, 수정 후 이 최종 관련 실행과 혼동하지 않는다.

- 최종 전체: **UTC4053 passed / 2 known xfailed / 4기존 warnings / 229.44초**, **KST4053 / 2 / 4 / 228.65초**, 각각 exit0·운영 상태/외부 네트워크 접근 시도0. 신규 정규시험32개는 B1 7+B2 5+B4 7+인수4+독립 경계9이며 외부 원본 probe를 다시 더하지 않는다. 앞선 UTC4044/2/4(174.66초)는 경계9개 이식 전의 중간 결과다.
- Astra/high 후속 독립 검증: 실제 SQLite 취소/종료4·읽기 오류 rollback3·동시 snapshot1·복원 취소1, 총9개 통과. 원본과 byte-identical 정규 이식본 SHA `d948ffa993334c28154f0ad13bb74672c3f805a41d60e95452f6ec06eaf81906`, 단독 UTC9(1.16초)/KST9(1.06초)·격리0. 읽기 snapshot 시험은 양쪽 DB 사실을 함께 다시 쓰는 인증 한계도 실제로 보여 준다.
- 독립 **Opus5/xhigh: APPROVE_THIS_SLICE**. 요청 effort xhigh, 실제 assistant 모델 `claude-opus-5`, **366.318초/child0/terminal success**, 오류·stderr·도구 사용 없음. 입력296125바이트 모두 전송/SHA `d0f0ae3c08a095909732d7f2f572efe7be1d1c1a52349dcd458988ad982921d1`; 실행 메타+답변 보존본 SHA `1ca83d52b4f0ba50a774d7d036a88b933f9cbc091b4222b6d20b74cb00761d48`. Opus는 소스만 정적으로 검토했으며 테스트 실행/전체 suite 결과를 독립 검증한 것이 아니다. 실제 유효 effort와 청구액은 별도 검증하지 않았다.
- Python 문법/비밀정보 의심 패턴 검사 통과. verify.sh의 시험 생략 실행은 이 두 검사에만 쓰며 전체 시험의 근거는 위 명시 `tests` 실행이다. 승인 입력 이후 제품 소스 변경0; restore 시험 파일의 마지막 빈 줄1개 정리와 독립 경계9개의 추가 이식은 의미 변경 없는 별도 기록이다.
- 최종 엔진 source/test13파일의 staged patch(기준 HEAD77c3f5d) SHA `a8e81f957c53f704cbf21433b55e7e2ec2f197f9b42922d25d353516be0ec67d`. 실행기/문서 변경은 이 지문의 범위가 아니다. main8ff2f55는 clean·불변이며 이번 작업은 feature에 보존, commit/push/운영 전환은 하지 않았다.

### 재리뷰 추가 의견 N1–N5

| 의견 | 처분 |
| --- | --- |
| N1 잘못된 retained_sources 요청도 result-failed로 확대 | 선행 seal 경로의 별도 결함을 실제 재현했다. C2a 승인 지문은 동결하며 C2b 첫 작업으로 사전검사·기존 conflict/stale 보존·실제 저장 실패 차단을 인수한다. 아래 증거와 같이 미수정 상태를 명시한다. |
| N2 startup 일자 전환 전 등록 불가 | 의도된 fail-closed. 전일 checkpoint의 rollover/resume를 마친 뒤 정책 등록, 그 뒤 versioned seal/실제 producer 연결 순서로 고정한다. |
| N3 등록 이후 종료 계약 강화 | 등록을 한 번이라도 수행한 runtime은 shutdown 때 healthy/published/engine version 일치를 요구한다. 예상된 거부와 실제 저장 실패의 차이를 Do 절과 cold-restore 시험에 기록했다. |
| N4 등록 상태의 실제 일자 전환 시험 부재 | 입력에 미제공한 기존 `test_registered_generation_survives_actual_day_fence_roll_and_cold_restore`가 등록→prepare→valuation→roll→resume→새 runtime→versioned seal을 이미 검사한다. 최종 전체에도 포함됐다. 실제 reset_daily는 현재 등록 대상 정책을 변경하지 않는다. 향후 selector/일일 정책 확장 시 추가 인수 필요. |
| N5 SQL 접두 조회의 전체 scan | 성능 미실측 의견. B3과 묶어 장기 이력 restore 측정 후 판단하며 근거 없이 검증을 제거하지 않는다. |

리뷰의 V5 ‘새 프로세스’ 표현은 **같은 테스트 프로세스 안에서 새 Runtime/Store/Engine 객체로 cold restore**한다는 의미로 한정한다. 실제 별도 OS 프로세스/전원 장애 시험은 아니다. 알려진 DB 전면 일관 재작성은 승인 입력에는 문서만 있었으나, 별도 독립 snapshot 시험이 그 한계를 실제 대조했다.

N1 독립 실제 재현: missing operation·다른 lane 선언·미accepted terminal 세 경우 모두 `invalid_retained_input_source` 후 durable 불변, owner unhealthy, restore 후 healthy이나 shutdown은 `command_result_drain_failed`다. SQL 저장 실패가 아닌 요청 오류 확대이며 `77c3f5d`에도 같은 retained 검증이 있었다. Terra/high 원본 probe는 **1 RED(세 경우 집계)/1 GREEN 대조, UTC1.25초·격리0**, SHA `74475148e5e3667d6d2bf5746ef3a02e4dbc1727841e5dfe67f76df6767644c6`. valid retain 대조는 **같은 날** accepted source이며 전일 자료 인수를 증명하지 않는다. 실패 원본을 ignored SDD에 보존하고 passing suite의 xfail로 숨기거나 수정 완료로 표시하지 않았다. C2b 실제 producer 연결 전 반드시 닫을 선행 항목이다.

## 다음 단계와 비승인 범위

다음은 N1 retained source 요청 오류 경계 수정 → 지속 source 소비 권한(C2b)의 기존 RED7/대조2 → 실제 2분 루프 → 정오/장전 LLM/보호 재생 통합 순서다. N1 원본을 부모도 별도 재실행해1 RED/1 GREEN(1.10초, 격리0)을 확인했다. N4 기존 day-cycle과 restore 회귀11개는2.10초/격리0으로 재확인했다. 이후 qualification/현재 자원 재검사·모든 writer/송신점·공식 초기 인계/취소 증거·전체 C/F/G/R와 broad 리뷰가 남는다. `trading_ready=False`, 모든 MODIFY 미지원은 그대로다. 한정 승인은 main/운영 전환 승인이 아니다.
