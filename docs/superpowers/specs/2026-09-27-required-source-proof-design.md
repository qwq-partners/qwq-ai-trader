# L3 조건부 source 증명 — 별도 필수 검증 계약

2026-09-27 KST. **사용자 `rㄱ` 승인 후 오프라인1차 도구를 구현·검증 중이다.** 현재 근거는 [구현 진행 원장](../../reviews/verification-evidence-2026-09-27.md)이 정본이다. 기존 성능 실패의 개발 예외는 유지하며, 아래 최종 CI 계약의 전체 구현·활성화/runtime qualification은 미완료다.

## 사용자 결정 추가 — 기존 성능 실패의 개발 차단 해제

사용자는 설계 목적 설명 뒤 “기존 성능실패는 예외 처리로 놔두자. … 이어서 개발 진행해보자”라고
지시했다. 직전 설명의 controlled5k13.023871ms·controlled100k33.173999749ms를 **알려진 성능 위험의
개발 진행 예외**로 기록한다. 이는 설계 방향 승인과 후속 개발 요청이며 성능 안전성이 입증됐다는
판정은 아니다. 기존 실패 수치·원본 시험·진단 결과는 그대로 보존한다.

아래 §7·§9의 “기존 두 성능 실패 때문에 개발/전체 회귀를 시작할 수 없다”는 조건은 이 지시로
대체한다. 계획된 오프라인 개발·회귀는 진행할 수 있고, 동일 이력 자체를 이유로 다시 중단하지 않는다.
다만 원시 pytest 실패를 PASS로 바꾸거나 skip/xfail·임계 상향·자동 rc 변조로 지우지는 않는다.
개발 처분은 `ACCEPTED_PERFORMANCE_RISK`로 원시 결과와 별도로 표시한다. 기존 단회 진단을
반복하거나 새로운 성능 원인을 추정하기 위한 허가는 아니다.

예외는 주문/수량/경제/복구 정합성, 자원 수명 증명, 격리, 수집 누락, timeout·미완료 시험,
새 성능 실패에 전파하지 않는다. 같은 시험의 다른 assertion 실패도 자동 면제하지 않는다.
다른 과거 성능 사례는 별도 목록으로 남기며 이 두 사례와 합치지 않는다. 실제 CI 필수 check와
main/운영 전환은 검증 환경·독립 리뷰·원격 보호 확인 등 나머지 조건을 계속 충족해야 한다.
실행계획과 예외 적용 근거는 [후속 계획](../plans/2026-09-27-required-source-proof.md)에 고정한다.

## 1. 목적과 승인 범위

사용자는 기존 전체 시험을 유지하면서 별도의 필수 증명 시험을 추가하는 **설계 작성**을 승인했다.
목적은 CPython native 동작에 한정된 시험을 일반 Python 지원 인증으로 오해하지 않으면서,
일반 회귀와 조건부 증명 중 어느 것도 빠지지 않는 검증 경로를 만드는 것이다.
사용자의 후속 개발 지시를 반영해 별도 구현계획을 작성한다. 구현계획 검토 절차는 유지한다.

기준 checkout은 `b7e297b07ae45dd0e5e54aff34d8fa340aa8bc32`다.
[이전 계획](../plans/2026-09-27-recovery-lifecycle-proof.md)과
[실제 결과](../../reviews/l3-proof-results-2026-09-27.md)는 이력으로 보존한다.
이 설계가 향후 승인되면 이전 계획의 source 파일 위치·지원 환경·coordinator See만 새 계획에서
명시적으로 대체한다. 이전 문서나 실패 원문을 덮어쓰지 않는다.

비목표: source/sequence 알고리즘 변경, portable native capability, cold-open/decoder/consumer,
성능 원인 진단·재측정, 5ms/GC/입력/보존 정책 변경, 전체 엔진·main·운영 전환.
KIS 거래·잔고/Toss 관측·주문·전략·위험 설정도 범위 밖이다.

## 2. 확인한 문제와 대안

- source helper는 exact CPython3.12.3만 허용한다. 현재 관련139 passed는 source108+기존31이며
  synthetic warm store의 생성 이력·native 수명에 조건부인 증거다. 다른 build 인증이 아니다.
- 현행 `.github/workflows/verify.yml`은 micro 미고정 Python3.12, `verify.sh`는 `tests` 전체를
  실행한다. 정상 source 시험을 그대로 기본수집에 넣으면 지원 계약이 충돌한다.
- source 두 파일은 아직 별도 worktree의 **새 미커밋 파일**이다. 기존 tracked 회귀 시험을
  밖으로 옮기거나 삭제하는 변경이 아니다. sequence는 이 설계에서 이동하지 않는다.

| 방식 | 판단 |
| --- | --- |
| 별도 필수 proof + 기존 전체 회귀 | 채택 방향. native 한정성을 보존하되 두 경로 모두 필요하다. 환경 qualification 비용이 든다. |
| 일반 CI에서 runtime capability를 매번 새로 증명 | 보류. 미지의 layout을 충분히 식별하는 새 API·증명 부담이 더 크다. |
| 전체 CI를3.12.3으로 내리거나 proof skip/xfail | 채택 안 함. 일반 지원 범위 축소·증거 누락 또는 확인되지 않은 build 등가성을 전제한다. |

## 3. 구성과 최종 판정

단일 `Verify` workflow 안에 세 역할을 둔다. 이름은 다음 계약으로 고정한다.

| 역할 | 수행 | 성공 조건 |
| --- | --- | --- |
| `verify-standard` | 현행 Python3.12 환경의 기존 전체 `tests` + 문법·비밀 패턴 검사 | UTC 전체 성공 후 KST 전체 성공, 기존 시험 보존·격리0 |
| `verify-source-proof` | 승인된 고정 runtime 안의 명시 source suite + store/gate/registration 회귀 | 환경 일치·실제 증명·UTC→KST·격리0 |
| `verify` | 위 두 작업의 결과와 동일 실행 증거 대조 | 두 작업 모두 실제 `success`, 누락·불일치 없음 |

최종 `verify`는 `needs` 두 작업과 `always()`를 사용하되, checkout/설치/시험을 다시 하는 작업이
아닌 짧은 판정 작업이다. 각 결과가 정확히 `success`가 아니면 실패한다. `skipped`, `neutral`,
`cancelled`, timeout, 인프라 오류, 누락을 성공으로 바꾸지 않는다. 최종 작업 자체가 완료되지
않으면 인수 불가다. 실패를 `continue-on-error`, `|| true`, cache된 성공으로 덮지 않는다.

GitHub는 조건부로 생략된 작업을 성공으로 취급할 수 있으므로 작업 존재만으로 필수성을
보장하지 않는다. 위 명시 판정이 필요하다.
[공식 required-check 설명](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks),
[workflow 의존성](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).

기존 `pull_request`(main 대상), `push`(main), 수동 트리거를 보존하고 `merge_group`도 지원하도록
설계한다. 수동 실행은 개발 증거이며 PR 필수 check 대체로 사용하지 않는다. path filter나
proof-only 변경 조건은 두 필수 작업과 aggregator에 두지 않는다. PR에서는 두 lane 모두 같은
검증용 merge SHA를, 다른 이벤트에서는 해당 이벤트 SHA를 checkout한다. head SHA와 merge SHA를
섞거나 새 실행에 이전 run/attempt의 성공을 가져오지 않는다.
[이벤트별 SHA](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows).

`verify` 이름을 보존하더라도 원격 보호 설정이 자동으로 확인됐다는 뜻은 아니다. 활성화 전
check 이름·GitHub App·최신 SHA·up-to-date 규칙을 읽기 전용으로 확인하고, 실제 실패/생략이
병합을 막는 시험 증거가 필요하다. 보호 설정 변경이나 관리자 우회는 별도 권한 대상이다.

## 4. 시험 배치와 빠짐 방지

최소 경로는 `tests/proofs/l3_source/l3_source_lease_probe.py`와
`tests/proofs/l3_source/source_lease_cases.py`다. cases 파일은 default `test_*.py` 이름을 쓰지
않고 필수 proof runner가 **정확한 파일 경로로** 선택한다. 따라서 `pytest tests`와 원 계획의
`pytest -q` 모두에서 기존 수집을 유지하면서 source proof만 명시 경로로 분리할 수 있다.
이를 추정으로 승인하지 않고 다음 집합 인수를 요구한다.

1. 일반 기본수집은 source proof node0, 기존 baseline node 집합 전체 포함이다. 기존 node 삭제·
   이름 변경·새 skip/xfail·deselection은 자동 승인하지 않는다. 합법적 변화도 별도 처분이 필요하다.
   기존 strict xfail2건과 이미 존재하는 조건부 skip은 identity·조건으로 보존한다. 새 환경에서
   처음 관측된 skip을 단순히 개수가 작다는 이유로 기존 허용 결과라고 추론하지 않는다.
2. 명시 proof 수집은 승인된 expected node 집합과 **정확히 일치**해야 한다.0개·누락·중복·예상 밖
   node·skip·xfail·XPASS 모두 실패다.108이라는 숫자만으로 동일 시험이라고 인정하지 않는다.
3. 기존 관련3파일 `test_execution_state_store.py`, `test_execution_owner_ticket_gate.py`,
   `test_execution_policy_registration_boundaries.py`는 일반 전체 시험에 그대로 남고 proof 환경에서도
   재검증한다. 두 환경/시간대의 건수를 합산해 한쪽 누락을 가리지 않는다.
4. 최초 inventory는 승인된 다음 실행계획 아래 격리 collection으로 만들고, 기존 두 source 파일의
   함수·parameter node를 경로만 정규화해 대조한다. 현재 이 설계에서는 collection도 실행하지 않았다.
   이후 expected inventory 수정은 시험 코드와 함께 독립 검토한다. actual을 그대로 expected로
   복사하는 자기검증이나 런타임 자동 갱신은 금지한다.

기존 `tests` 파일을 삭제·이동하거나 전역 `pytest.ini`, `testpaths`, ignore hook, `-k/-m` 필터를
추가해 분리하지 않는다. source helper 본문은 검토된 bytes를 보존한다. cases 이동에 필요한
경로/child 격리 증거 변경은 별도 RED·독립 검토 대상이며 native oracle 단언을 바꾸지 않는다.

## 5. 격리와 직접 child

parent는 `tests` 아래의 명시 파일을 실행하므로 기존 `tests/conftest.py`의 자동 로딩을 유지한다.
그 경로와 실제 module object가 하나임을 검증하고, 시험/fixture 종료 뒤 실제 `VIOLATIONS`가0인지
검사한다. 현재 conftest는 위반 요약을 출력할 뿐 종료 코드를 바꾸지 않으므로 pytest rc0만으로
격리0을 추론하지 않는다. 별도 evidence plugin이 전체 세션의 격리 사실을 수집·판정한다.

직접 실행하는 negative child는 pytest의 조상 conftest 자동 로딩이 없다. cases 기준 root는
`parents[3]`, child `PYTHONPATH`는 명시 `root/tests`와 `root`이고 같은 파일의 `__main__` 경로를
유지한다. 제품 import 전에 guard의 exact 파일/hash·단일 module identity를 검사한다. child
원시 증거에도 guard identity와 실제 위반 수를 결속하고 부모가 엄격한 schema로 확인한다.
출력 단언을 삼킨 예외나 고정된 `isolation_ok=True`로 대체하지 않는다.

negative child는 원래처럼 cleanup 미완·gate 보유 상태를 관측한 뒤 부모가 종료하는 시험이다.
이 경우에만 원래의 live-record 확인·bounded terminate/reap가 기대 경로다. child를 정상 cleanup이나
exit0으로 위장하지 않으며, parent pytest 성공과 child의 의도된 종료를 구분한다. 출력 누락·중복·
기한 초과·회수 실패는 변이 검출 성공이 아니다. 현행10초 child 상한과 그 의미는 보존한다.

일반 suite도 동일 guard의 실제 세션 위반 수를 기록한다. 표준 시험 중 기존 별도 subprocess들의
격리 계약을 이 설계만으로 전부 인증했다고 주장하지 않는다. 새 계약이 소유하는 source child는
모두 목록화하고 누락0을 요구한다. 회귀 중 새 외부 접근이 필요해지면 가드를 끄지 말고 차단한다.
테스트 환경은 명시 허용 변수만 전달하며 `PYTEST_ADDOPTS` 등 외부 선택 옵션을 받지 않는다.
운영 home/자격증명/상태를 mount·복사하지 않으며 시스템 HOME 변수를 재지정하지 않는다.

## 6. 조건부 runtime qualification

일반 CI의 Python3.12 선택은 그대로 둔다. source proof만 고정된 **검증 전용 runtime capsule**을
사용한다. 이는 한정된 실행 파일·라이브러리·의존성을 묶은 식별 단위이지 portable 안전성 보증이
아니다. 현재 승인된 capsule은0개이며 실제 digest를 이 문서에서 꾸며 넣지 않는다.

등록 identity에는 최소한 다음을 결속한다.

- 정확한 플랫폼별 image digest/OS/architecture, Python implementation·3.12.3·ABI·build 정보;
- interpreter·사용 libpython·stdlib·`_sqlite3`·실제 연결된 SQLite와 native 로더/의존 closure 지문;
- CPython/SQLite 원본·빌드 이력, 고정 dependency 목록/내용 지문, pytest/plugin 버전;
- helper·cases·격리 guard·runner/oracle의 검토된 subject 지문 및 제한된 fixture provenance.

상위 multi-architecture tag, version 문자열, `sqlite_version`만으로 등가성을 인정하지 않는다.
최초 build identity가 기존 로컬 환경과 다르면 새 후보일 뿐이다. 리뷰는 그 실제 build의 native
참조 구조·상한과 코드 근거를 대조하고, 실제 provenance/수명/변이 oracle 실행도 요구한다.
과거 로컬139 passed나 image digest만으로 native 지원을 인증하지 않는다.

순환 승인을 피하기 위해 상태를 분리한다.

| 상태 | 허용/판정 |
| --- | --- |
| UNKNOWN | 필수 proof 경로에서 SQL/source 실행 전 거부; 기본 Python이나 host로 폴백하지 않음 |
| BOOTSTRAP_ALLOWED | exact 후보·검토된 계획·격리 개발 환경에서 qualification만 수행; 필수 check 성공 발행 불가 |
| QUALIFIED | 실제 증거·독립 native 검토·명시 등록이 완료된 subject만 필수 job에서 사용 |
| RETIRED | 사용 불가; 과거 증거는 보존, 다른 digest로 자동 대체 불가 |

qualification subject hash는 실행 코드·의존성·runtime을 포함하고 후작성 승인 보고서 자체는
제외해 자기지문 순환을 피한다. 매 필수 job은 등록 identity를 실제 파일과 대조하고 native oracle을
다시 수행한다. hash가 맞아도 현재 oracle 실패면 실패다. 관련 subject 변경은 재qualification,
docs-only 변경은 현재 run SHA를 새로 검증하되 native subject가 동일한지 명시 대조한다.
기록은 감사·실수 탐지용이며 악의적인 저장소 관리자에 대한 암호학적 승인 보장은 아니다.

정규 job의 승인 등록부는 candidate diff가 아니라 **사전 승인된 base revision**에서 읽는다.
PR의 새 runtime manifest/QUALIFIED 문자열로 자기 자격을 만들 수 없다. 등록부가 없거나 승인
subject가 다른 후보는 bootstrap 관측 자료만 만들 수 있고 required proof는 차단된다.
첫 등록은 독립 검토 기록을 포함한 선행 등록 단계로, 필수 workflow 활성화와 분리한다.
해당 등록 revision의 신뢰·검토를 확정하지 못하면 활성화할 수 없다. main에 기록을 미리 넣거나
원격 설정을 변경하는 권한을 여기서 부여하는 것은 아니다.

등록부를 검사하는 코드도 승인 경계에 포함한다. workflow, runner/verifier/parser/aggregator,
격리 guard, 승인 등록부와 expected inventory는 **gate-critical 파일**이다. 변경 작성자와 다른
critical reviewer가 그 revision을 승인하기 전에는 후보가 생성한 CI green을 통합 근거로
인정하지 않는다. 승인된 등록부를 읽더라도 후보 verifier가 비교를 생략하면 안전하지 않다.
JSON/hash·같은 App/check 이름만으로 후보의 workflow 변경까지 막는다고 주장하지 않는다.

활성화 전, 이 독립 승인/경로 보호가 후보 workflow의 성공만으로 우회되지 않는지 실제 원격
보호 효력을 확인해야 한다. 현재 저장소 문서에는 required approvals0 이력이 있지만 실제
ruleset은 미조회다. 보호 부재·불명확이면 **필수 gate 활성화 차단**이며 advisory 성공으로
대체하지 않는다. 설정·권한 변경이 필요하면 별도 결정 대상이다. 이벤트별 신뢰 base와 승인
revision 해석은 다음 계획에 고정하고 후보 제공 revision을 무검증으로 신뢰하지 않는다.
이 한정된 reviewed gate-file 경계에 새 서명키나 외부 범용 control-plane은 요구하지 않는다.

capsule이 host kernel까지 고정하지는 않는다. 승인 execution profile에 OS/kernel 계열·
architecture/ABI 전제를 두고 매 run에서 대조한다. 알 수 없는 profile은 이전 자격을 상속하지
않으며, 같은 kernel 문자열이나 container digest로 latency/호스트 무간섭을 인증하지 않는다.

향후 후보 제작·의존성 확보는 별도 실행계획에서 허용 범위·예산을 확정한다. 네트워크 준비와
테스트 실행을 분리하고 source proof는 network-off, 읽기 전용 runtime/source, 전용 임시 쓰기
공간에서 실행한다. GitHub-hosted 격리 worker를 기본으로 하며 이 운영 호스트를 self-hosted
runner로 등록하지 않는다. CI/테스트 job에는 registry publish·SSH·배포 권한을 주지 않는다.
runtime이 없거나 보안/빌드 근거가 부족하면 **UNSUPPORTED/차단**이지 생략 성공이 아니다.
[GitHub 실행 보안 지침](https://docs.github.com/en/actions/reference/security/secure-use).

## 7. 실행 증거·로컬 계약

두 lane은 동일한 schema의 bounded evidence를 남긴다: schema version, 후보 SHA/tree digest,
workflow/runner/contract 지문, run ID·attempt·이벤트·lane ID·시간대, runtime identity, expected/actual
node 집합 지문, 실제 실행 결과별 node 목록/수, collection·pytest·문법·비밀 검사 종료 코드,
격리 guard/hash/위반 수, source child 목록·종료 처분, timeout/원문 로그 지문.
중복 JSON key·unknown schema·비정상 타입·누락/추가 결과·부분 파일은 거부한다.
스키마를 통과한 자기신고만 신뢰하지 않고 pytest hook/실제 process 결과를 생산자가 기록한다.

aggregator는 GitHub의 `needs.*.result`와 같은 run/attempt의 artifact를 함께 대조한다.
공유 실행 tuple은 **이벤트·검증 SHA/tree·공통 계약 revision·run ID·attempt**이며 모두 같아야 한다.
필수 슬롯은 `standard×{UTC,KST}`, `source-proof×{UTC,KST}` 네 개다. runtime/profile,
expected inventory와 producer 지문은 서로 다른 lane끼리 동등 비교하지 않고 **각 lane×시간대의
승인된 기대값**과 대조한다. 네 슬롯의 정확한 완전성을 요구하며 누락·중복·다른 슬롯의 재사용·
이전 attempt 대체는 실패다. UTC 실패 뒤 KST NOT_RUN도 의도된 전체 실패다.
artifact가 없거나 다운로드되지 않으면 실패이며, pytest나 job의 rc0만으로 증거를 합성하지 않는다.
실제 endpoint 권한 확대 없이 동일 workflow의 artifact만 사용한다. 정확한 producer/parser 필드·
크기 상한은 구현계획에 고정한다.

표준 lane은 기존 `scripts/dev/verify.sh`의 검사 의미를 유지하고 evidence 수집만 덧붙인다.
`QWQ_VERIFY_SKIP_TESTS=1`은 현재 synthetic script 시험의 내부 fixture 용도로만 남는다.
새 통합 진입점은 해당 우회값·선택 옵션을 거부한다. standalone verify 성공은 표준 lane 성공일
뿐이고 최종 L3 증명 완료로 표시하지 않는다. 별도 로컬 통합 runner는 standard UTC→KST 후
qualified capsule의 proof UTC→KST를 직렬 실행하고 동일 parser로 판정한다.

단일 호스트의 두 lane/시간대 workload는 병렬 실행하지 않는다. CI의 별도 worker도 동일 runner를
공유하면 직렬화한다. 기존 전체 시험900초/시간대와 proof 집중180초/시간대·child10초는 유지한다.
CI job timeout이 먼저 닿아도 실패로 남기며 통과를 위해 기한을 늘리거나 재시도하지 않는다.
최초 실패/미확정이면 이후 시간대는 NOT_RUN이며 최종 실패다. 모든 기록을 남기고 passing attempt를
고르지 않는다. 특히 현재 종료된 성능 진단을 이 runner 개발의 테스트 명목으로 재실행하지 않는다.

종료된 진단 driver의 반복 금지는 유지한다. 실제 전체 standard UTC/KST의 개발 검증은 상단
사용자 결정에 따라 기존 두 실패만으로 착수를 차단하지 않는다. runner·파일 위치·환경 포장 변경은
새 성능 근거가 아니며, 예외는 raw 실패·timeout을 숨길 근거가 아니다. 원격 CI 활성화와 runtime
자격 취득은 별도 선행조건을 유지한다. 합성 임시-repo 시험·격리 collection·source bootstrap은
후속 계획의 범위·예산·증거 기준에 따라 구분한다.

## 8. 실패 인수와 변경 경계

다음은 구현계획에 반드시 포함할 RED 인수다. 지금 수행했다는 뜻이 아니다.

- standard 실패/proof 성공, 그 반대, 한 lane skip/cancel/timeout/누락 → 최종 nonzero.
- 둘 다 rc0지만 다른 SHA/attempt, artifact 중복·누락·위조 count → nonzero.
- 서로 다른 정상 lane의 runtime/inventory는 각 기대값으로 승인; UTC 증거를 KST로 대체하면 nonzero.
- 일반 node 삭제·새 deselection, proof0개·일부만 선택·중복 node·새 skip/xfail → nonzero.
- guard 미로딩/다른 conftest/위반 삼킴/child 증거 누락 → nonzero, 실제 외부 접속 없이 합성 재현.
- 같은3.12.3이나 다른 native 지문, 미등록/퇴역 capsule, 동적 dependency 주입 → source SQL 전 거부.
- 정상 분리: baseline 일반 node 보존, proof 명시 집합 일치, child10초/provenance/변이 검출 의미 유지.
- 실제 CI PR의 required-check 실패와 merge SHA 결속. synthetic YAML 시험만으로 원격 보호를 인증하지 않음.

향후 최소 변경 후보는 workflow, dev runner/evidence plugin/집계기와 그 `tests/dev` 시험,
위 두 proof 파일, 검토된 inventory/runtime 등록부, 관련 문서다. 기존 `src`·제품 설정·기존 회귀
단언·conftest 보호 범위를 변경하지 않는다. 파일별 literal allowlist와 새 schema는 다음 계획에서
확정한다. corpus 준비·runtime build는 별도 bootstrap 단계이며 무제한 범용 플랫폼을 만들지 않는다.

## 9. 진행 순서와 변하지 않는 관문

1. 이 **서면 설계** 독립 검토·사용자 후속 지시 반영(위 성능 예외 포함).
2. 구현계획에 파일 소유권·인터페이스·RED·예산·증거 상한·bootstrap 승인 기록을 고정하고 검토.
3. 합성 게이트/수집 경계 구현, exact runtime 후보 qualification 및 독립 검토.
4. path 변경 proof와 일반 회귀의 수집 보존을 확인하고 두 필수 lane/최종 gate를 오프라인 배선한다.
   이 단계가 실제 전체시험·원격 CI를 자동 실행하는 계기가 되어서는 안 된다.
5. 사용자 성능 예외 처분을 원시 결과와 구분하고 gate-critical 파일 독립 승인/원격 보호 확인을 선행한다.
   그 뒤 허용된 실제 검증·독립 broad 검토·원격 필수 check 결속 확인을 거쳐 해당 변경 통합을 판단한다.
   선행 조건이 없으면 구조 구현이 끝나도 실제 전체 인수는 **미실행/차단**으로 남는다.

현재는3~5의 완료 증거가 없으며 개발 요청과 실행 완료를 혼동하지 않는다. 새 환경 확보 실패가
있으면 그 의존 항목만 미지원으로 남기고 독립적인 오프라인 개발을 계속한다.
이 분리 계약은 source의 CI 지원 충돌을 다루지만 **기존 controlled100k33.174ms와
frozen controlled5k13.023871ms 실패를 해결하지 않는다.** 새 `verify` 성공은 이 실행 계약의
충족일 뿐, 과거 성능 실패의 원인 해소/면제나 전체 L3·Task4–15·엔진 승격 허가는 아니다.
성능 예외는 해결 증거가 아니라 사용자 위험 수용이며 자동 운영 승격 근거가 아니다. 기존 미승인
후보의 다른 미해결 조건까지 소급 승인하거나 main 통합하는 권한으로 확대하지 않는다.

## 10. Plan–Do–See 기록

아래 독립 리뷰 승인은 성능 예외 지시 이전 설계의 이력이다. 이후 사용자 결정 추가와
오프라인1차 구현계획은 별도 변경이며 해당 승인에 소급 포함시키지 않는다.

Plan: 사용자 의도·기존 지원 충돌 확인, Sol/high 수집/격리와 Astra/high native 계약을 병렬 분석.
Do: coordinator가 기존 코드·원장과 GitHub 공식 설명을 대조해 이 설계만 작성.
See: 독립 Astra/xhigh 첫 리뷰의 P1 두 건(검사 코드 신뢰 경계·실제 성능 실행 선행권한)과
P2 한 건(lane×시간대별 증거 비교)을 반영했다. 재리뷰는 세 건 모두 closed·새 지적0으로
`APPROVE_DESIGN_ONLY`를 판정했다. 검토본 SHA256은
`8674b6511796e0fdd380cc412654ae92c8470070022d68ca18396c086918324d`이고 이후 변경은 상태 기록뿐이다.
재리뷰 artifact `required-proof-design-rereview.md` SHA256은
`44583cd94f807a28ba8a6dabcfb79f0a3a75f095d04ec85c81d642c88e04535c`다.
실제 모델/effective effort metadata는 미노출·미검증이며
동일 공급자 독립 리뷰를 cross-provider 리뷰라고 하지 않는다. 시험/collection/qualification0,
소스·CI·설정·운영 변경0. 분석 artifact는 기존 SDD 경로에 보존한다.

이번 리뷰에서 판단하지 않은 항목은 누락된 PASS가 아니라 별도 미검증 범위다: 실제 node 수집,
runner/CI RED→GREEN, native capsule 안전성·가용성, 원격 보호 효력, 전체 UTC/KST·성능 재측정,
기존 모든 subprocess 격리. 관리자/host 대상 암호학적 인증 및 전체 엔진·운영 전환은 비목표다.
문서 commit/push는 coordinator의 문서 검증 대상으로만 처리하며 후보 코드/main 통합은 포함하지 않는다.
