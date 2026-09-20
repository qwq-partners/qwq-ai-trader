# 장전 text diagnosis·소비 경로 정합화 — C4 Plan–Do–See

기준 `da74849`, `feature/engine-safety-design-20260917`. **C4 한정 구현·독립 재리뷰·전체 검증을 완료했다.** [고정 계약](../superpowers/specs/2026-09-20-regime-morning-owner-contract.md)과 [writer 이행 계획](../superpowers/plans/2026-09-18-engine-writer-migration.md)의 C4만 다룬다. C3는 [별도 완료 보고](noon-regime-protection-replay-2026-09-20.md)를 따른다. 전체5단계·운영 설치·투자 성능 승인이 아니다.

## Plan

실제 installed 2분 루프가 trend 처리 뒤 continue하여 장전 진단 전체를 건너뛰는 것을 독립 감사와 실제 caller RED로 확인했다. legacy 진단문/성공일/open expectation은 메모리에만 있었으며 공격/방어 태그는 중기 레짐을 직접 바꿨다. text만 저장해서는 정책 버전·메모리 복사본·cold restore가 일치하지 않는다.

따라서 명시 morning baseline, typed morning source와 chronological transition, 원 window/optional 순서의 실제 caller, adapter/engine 표시 복원, 최신 중기 정책의 source/version 검증을 한 단위로 이행했다. 다음 지수 갱신이 최신 중기 권한을 대체해도 성공일·진단문은 역사로 보존한다.

| 소비 상태 | 정본/역할 | 이번 경계 |
| --- | --- | --- |
| 중기 레짐·owned 최소 현금 reserve | typed 전이 + 최신 source 권한 | morning 조정 뒤 morning commit version, 다음 정상 index 뒤 새 index version |
| adapter/engine 레짐 복사본 | owner projection | 저장·게시·복원과 표시를 구분 |
| 진단문·성공일 | 명시 baseline + accepted morning 역사 | 실패는 성공으로 세지 않고 성공 뒤 같은 날 외부 재호출0 |
| open expectation | 당일09:30까지만 표시 | 만료 문장을 매수 허가로 쓰지 않음 |
| dashboard·Telegram·감사 라벨 | 표시/감사 projection | 파일/raw LLM 문장을 정본으로 되읽지 않음 |
| qualification·최종 sizing/getter | 후속 request-bound 이행 | 이번 projection 정합성과 최종 주문 승인을 구분 |

역할은 ai-routing-v1-2026-09-20에 따라 Astra/high 구현, Terra/high 독립 인수, Sol/high 소비 감사, Astra/xhigh 독립 critical 리뷰로 분리했다. 각 역할은 같은 base의 격리 feature worktree를 사용했다. native 실제 모델/실효 effort metadata는 미노출이므로 요청값만 기록한다. 외부 정적 리뷰는 tools-off Claude Opus5/xhigh이며 실행기가 실제 모델/result를 검증한다. 부모는 통합·문서·추가 표시 경계 시험을 담당했다. 외부 리뷰를 포함해 동시 작업자3명 상한을 유지했다.

## Do

- schema3는 명시 morning 기준선과 역사를 추가하며 기존 schema1/2를 자동 승격하지 않는다. unknown은 다음 날에도 known-empty가 되지 않는다. 초기 계좌/일자/generation/fence/evidence/선행 기준선 version을 사전·reducer 양쪽에서 대조한다.
- 첫 optional I/O 전에 source begin을 접수한다. 현재 index dependency, 실제 읽은 trend/horizon의 versioned seal, 원 prompt/stage/task/max_tokens를 고정한다. 관련 변화/ABA/일자 변경은 stale이고 무관 ACK는 그대로 허용한다.
- KST08:48~08:55:59, 성공1회/일·실패 재시도, 테마→보유 첫5 시간외 순차→뉴스→optional macro→text LLM1회를 보존한다. 추가 지수 GET0, 신규 TTL/timeout/임계값0이다.
- 진단 성공은 source·mid 전이·진단문/날짜/open expectation·engine projection을 같은 commit에 저장한다. 기술 갱신시각·regime_data·pending·sidecar·index/VIX/expert ancestry를 보존한다. generic source 완료로 typed 전이를 우회할 수 없다.
- schema3의 engine/adapter cap은 같은 folded horizon과 KST 날짜를 사용한다. 기존 index 역사 v1은 그대로 두고 새 horizon 읽기는 별도 v2 rule로 검증한다. 실제5분·C3/보호 repair 경로도 schema3를 허용한다.
- Telegram은 이번 completed+accepted의 immutable envelope/seal만 읽는다. 외부 text의 HTML은 표시할 때만 escape하며 prompt·원문·durable state는 바꾸지 않는다. 전송 실패를 성공 재시도로 바꾸지 않는다.

### RED와 리뷰 처분

| 발견 | 실제 근거 | 처분 |
| --- | --- | --- |
| installed 장전 caller 누락 | 실제 SQLite+08:48 KST monitor에서 morning source0, 부모1failed/1.21초/격리0 | accepted trend 뒤 별도 owner caller 연결 |
| typed owner 우회 | generic morning success가 typed 전이 없이 승인되는 행동 RED | bound typed callback·baseline admission guard |
| native P1 날짜 비교 | 같은 순간23:50Z=08:50KST crash가 전일 취급되어 공격 bear→sideways 승인 | 비교 양쪽 KST 정규화. 원본 RED+same-instant/실제전일 대조 통과 |
| native P2 외부 호출 순서 | malformed positive quote는 legacy macro0, 새 경로 macro1 후 실패 | macro 전 순수 prompt 검증. change_pct/volume 대조 통과 |
| Opus B P1-1 failed도 accepted | 실제 source reducer/역사 검증은 success만 accepted. 실패 모델·seal전 오류 actual scheduler에서 Telegram0 | 제공 문맥 부족에 따른 조건부 지적. 코드 변경 대신 반증 근거 제공 |
| Opus B P1-2 accepted index None | required 등락 필드는 finite float 강제, 잘못된 입력은 missing·정책 불변 | legacy None 가능성과 typed accepted 입력을 구분 |
| Opus B P1-3 루프 종료/대기 누락 | 바깥 except+공통120초 sleep 존재. baseline 부재 actual monitor도 index2/sleep60,120 | 스케줄러 전체 문맥으로 반증; 기준선 없는 상태는 계속 차단 |
| Telegram HTML 표시 | 2RED/2대조 후 escape,4passed/3.65초/격리0 | 실제 messenger에는 plain-text fallback도 있음. “무조건 메시지 유실” 주장은 채택하지 않음 |
| 외부 B 소비 getter 후속 | 실제 SQL 성공·publisher 실패 뒤 세 getter가 미게시 값을 노출,3RED/2.94초 | readiness 두 줄로 차단, 실제 새 runtime 복원 대조·알림 포함7개 양TZ 독립 통과 |
| Opus A2 schema/rule 경합 | expert 완료 또는 seal 저장 뒤 schema2→3 등록 시 옛v1 index accepted,2RED | 알려진 v1/v2 경합만 stale/input_seal, cold는 기준선 활성 version 전후의 rule 대조. 정상 과거v1 보존 |
| Opus A1 날짜 DTO 예외 | business_day/assessment_day 정수 입력이 TypeError,2RED/0.83초 | 명시 str 검사로 계약상 ValueError. 편집 중 들여쓰기 오류는 수집 검사에서 즉시 수정, 제품 RED와 구분 |

원 caller probe SHA `54348b8810c02b657fc0c0f1332caf0dcdfb031c7ad164f33e7987d0d083b299`는 보존했다. 최종 fixture는 새 명시 C4 기준선을 등록하므로 원 probe가 무수정 통과했다고 주장하지 않는다. 최초 scheduler08:48/runtime10:00 불일치와 frozen dataclass 직접 변경 등 fixture 오류는 제품 RED에 포함하지 않는다. 기존 계획 RED3은 C3 JSON/protection 경로이지 새 C4 RED가 아니다.

native 원본15시험 SHA `e338efbb0a3364ee2cc5a5bb9380576d23c9ee4cb853fd76c46b4972a160f49f`는 수정 없이 보존했다. 첫 UTC7.91초/KST7.17초 각각2failed13passed에서 수정 뒤 작성자13과 합쳐 UTC28passed14.42초/KST28passed15.72초, 격리0으로 재승인됐다. 이 승인은 후속 외부 지적의 처분까지 자동 포함하지 않는다.

## See

### 실제 인수와 한계

| 범위 | 실제 확인 | 미실행/후속 |
| --- | --- | --- |
| actual installed caller | window 포함/제외, index2·추가조회0, model task150 | 실제 시장/API 운영 |
| optional parity | 테마·첫5·순차·뉴스·macro·LLM 순서, 원 prompt/if-elif | 모든 외부 공급자 형식 |
| 재시도/멱등 | 실패/빈문장 재시도, 같은날 성공·복원 재호출0 | 명시 다음날 baseline 교체 |
| 동시성/입력 권한 | 동시 모델1, 관련 index 변경, 무관 실제 ACK | C4 경제 fill의 독립 대조·모든 ABA 순열 |
| 취소/SQL | begin SQL 취소 drain, completion precommit/응답유실/게시 실패 | 모든 await의 반복취소·seal SQL 조합 |
| cold restore | 새 store/engine/runtime의 성공·미종결 복원 | 전체 장기 이력/부하 |
| 정책/표시 | index→morning→index version,09:30 표시 만료,UTC/KST cap | 모든 cap/표시 장애 조합 |
| 활성 규칙 경합 | expert/seal 대기 중 schema3 등록→정상 stale·healthy, 이후 v2/cap·기존v1 복원 | 모든 설치/일일 전환 writer |
| 게시 읽기 | 미게시3종 getter 차단→실제 cold reopen 정상 읽기 | 임의 setter/모든 게시 중간점 조합 |
| schema 호환 | schema3 실제5분→C3 application→degraded repair | 모든 legacy writer/factory |
| Telegram | escape는 표시만, 실패격리·성공중복0·실패본문전송0 | 실제 Telegram 서버 |
| 복원 교차검증 | typed transition/source/seal/baseline/history 검사 | 다중 행 협조 위조의 인증 보장 |

모든 시험은 외부 I/O·시계만 합성하고 실제 owner/store/runtime/가능한 실제 scheduler를 사용했다. “전체 조합 검증”이나 합성 입력의 공식 외부 계약 증명을 주장하지 않는다.

### 최종 게이트

- 첫 36신규 후보 전체: UTC4386passed/기존xfail2/기존경고4(262.48초), KST4386/2/4(269.37초), 각각exit0·격리0. 표시4시험 추가 전 중간 결과다.
- 최종 제품 후보의 신규53 포함 전체: **UTC4403 passed / 2 known xfailed / 4기존 warnings,287.03초; KST4403/2/4,268.28초**. 각각exit0·격리0이며 두 전체 suite는 직렬 실행했다. UTC 중 테스트2개 EOF의 빈 줄만 제거했고 독립 리뷰가 최종 지문의6개를 양TZ 재실행했다. 제품/시험 동작 변경은 없으며 KST 전체는 이 정리 이후 새로 실행했다. 경고는 기존 pykrx1/fork3이다.
- 최종 native 통합은 SPEC/QUALITY **APPROVE_THIS_SLICE**. 실제53시험 UTC29.92초/KST34.23초·기존 typed guard10개 UTC2.41초·최종 EOF지문6개 UTC4.04초/KST3.95초, 각각exit0·격리0. source/source-rule/필수seal의 실제 SQLite 추가 대조도 통과했다. 이를 모든 fault 조합/전체5단계 승인으로 확대하지 않는다.
- 전체 tracked Python 문법·비밀정보 의심 패턴·diff 검사를 통과했다. 이 검사에서 중복 pytest는 명시적으로 생략했으며 전체 pytest는 위 별도 명령 결과를 따른다. 기능 검증 후 문서만 완료 상태로 갱신했다.
- Opus A 원77,425바이트는 progress725·오류0·stderr0이나900.137초 total_timeout/exit124로 종료했다. 최종 판정 없음, 승인으로 세지 않는다. 같은 모델/effort/권한의164바이트 대조는3.435초 정상 CONTROL_OK였다(코드 승인 아님).
- 원인 처분은 로그인/권한/모델 변경이 아니라 독립 검토 범위 분해다. A1(32,036바이트/456.006초)·A2(46,636바이트/265.966초)·B(59,506바이트/424.074초)는 모두 정상 종료했지만 최초 판정에 변경 요청이 포함됐다. 시간/비용 상한은 유지했다. 정상 진행이 있어 인증/샌드박스 실패라는 근거는 없지만 “입력 크기만이 유일한 원인”도 증명하지 않는다.
- A1 후속29,900바이트/334.464초도 unknown baseline·기존 sibling DTO를 추가 차단으로 제기했다. native와 계약 문맥을 대조하고 전체 계약·현재 begin/complete를 제공한21,056바이트/60.346초 재검토에서 두 건은 미래 설계/품질 advisory로 정정됐으며 SPEC/QUALITY **APPROVE_THIS_SLICE**를 받았다. 앞선 CHANGES_REQUIRED 기록은 보존한다.
- A2+B 수정·반증 후속42,810바이트/429.609초는 SPEC/QUALITY **APPROVE_THIS_SLICE**이며, getter 예외의 전체파일 호출부 확인을 native에 위임하는 조건을 남겼다. 추가 확인에서 실제 SQL 성공/게시 실패 뒤 adapter·engine·G2 조회14개가 모두 ApplicationBlocked를 유지했고 installed SIGNAL/ORDER handler0, 일반 dispatch ERROR만 발행·ORDER0·SQL불변·격리0을 확인해 조건을 충족했다. 원 barrier3도 UTC1.84초 재통과했다. 정상 설치는 유효 root 검증·초기 publish 뒤에만 owner를 부착하므로 root 부재를 legacy fallback으로 감추는 변경은 하지 않았다. 실제 모델은 각 실행에서claude-opus-5, terminal success/child0으로 검증됐고 tools-off 정적 리뷰는 시험 실행을 대신하지 않는다.

관련 ignored 증거는 `.superpowers/sdd/2026-09-17-engine-execution-safety/task-10a2c-c4-*` 아래 보존한다. 원 입력·산출물 지문, 실패→수정→재리뷰 이력이며 운영 데이터는 포함하지 않는다.

### 조건부 지적·비차단 advisory의 처리 원칙

- A1의 단독 morning 재기준선화 우려는 실제 등록 API가 다른 supplied를 거부하므로 현재 지원되지 않는다. 기존 이력을 prune하거나 미래 baseline 이동을 임의 구현하지 않았다. source ticket의 kind→lane 매핑과 index hard dependency 재귀는 기존 코드로 확인했다.
- A2의 자정 계산 불일치는 실제 schema2/3에서 seal 직후 다음 날로 이동시키면 typed 계산 전에 stale/day_or_generation으로 차단되고 healthy를 유지한다. morning version 반환은 정책 버전의 의도된 계약이다.
- `morning_assessment()`는 baseline DTO가 아닌 표시 dict다. 만료 시 expectation은 None이지만 원 as_of/assessment/day는 역사 증거로 남으며 그대로 baseline 재등록/roundtrip하면 안 된다. schema3 adapter는 이 함수로 위임하고 legacy/schema1/2만 기존 만료 경로를 쓴다.
- SQL/seal/게시 장애 후 unresolved를 자동 재호출 가능한 failed로 바꾸지 않는다. 이미 저장된 사실은 복원으로 확인하며, 이전 context DTO를 새 day/fence 인계로 재승인하지 않는다. 등록 commit 뒤 context가 이동하면 응답은 차단될 수 있으므로 이를 rollback으로 보고하면 안 된다.
- 미사용 import·window 리터럴 중복·normal pending의 추가 관측성·fallback provenance 명명·잘못 구현된 provider의 진단 개선은 비차단 후속이다. 새 응답 길이 제한/임의 절단, rule-v1의 미래 변경, 장기 이력 자원 상한은 이번 동작 보존 범위에서 시행하지 않았다. 미래 rule 변경에는 과거 decoder 보존/명시 이행이 필요하다.
- 실패 문맥의 가정만으로 코드를 바꾸거나, 반대로 외부 리뷰 전체를 무시하지 않았다. 실제 재현된 rule 경합·게시 읽기 장벽·DTO 예외는 보완했고, 미재현/미지원/문맥 누락은 원문과 반증을 함께 남긴다.

## 다음 순서와 유지하는 경계

1. C4 한정 gate 뒤 남은 qualification·최종 sizing/정책 read와 소비 경로를 실제 request/claim/dispatch에 묶는 RED부터 고정한다.
2. 명시 factory/일일 초기화/KOFR/수동·broker/effect/WS 등 남은 writer와 sender를 차례로 이행한다. source 조회 성공을 거래 권한으로 쓰지 않는다.
3. 공식 최초 인계·취소 체인 증거를 별도 확보하고 지원/미지원 판정을 유지한다. 수량/가격 증가 정정은 실제 추가 예약·위험 인수 없이 송신하지 않으며 현재 모든 MODIFY는 미지원이다.
4. 실제 전체 C/F/G/R·독립 broad·장기 이력 성능을 통과한 뒤에만 main 통합과 운영 전환을 별도 판단한다. 투자 우위는 비용 차감 KODEX200 대비 별도 검증한다.

main/운영 SSH·배포·재시작·실API·주문·설정 변경0. KIS 거래/잔고 단독, Toss 관측 전용, `trading_ready=False`와 미입증 최초 인계·취소 최종성의 시작 차단을 유지한다.
