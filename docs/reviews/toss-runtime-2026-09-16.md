# Toss 승인 기반 관측 런타임 — Plan / Do / See

상태: 오프라인 구현·통합 검증·독립 소스 리뷰 완료. 실관측·배포·재시작 없음. 검증된 소스 SHA와 범위는 아래 최종 절이 정본이다.

## Plan — 범위와 기준

- main 기준 `a3187a8252c11cbed4b27097d260456c5078dcec`; 통합 worktree 기준 `9624559`, 계획 커밋 `bb40cc8`.
- [승인 설계](../superpowers/specs/2026-09-16-toss-runtime-shadow-design.md), [구현 계획](../superpowers/plans/2026-09-16-toss-runtime-shadow.md), [운영 경계](../operations/toss-shadow-runtime.md).
- #67 기반 보존, 기본 OFF, 현재가·캘린더 관측만. 추가 KIS HTTP 0; 일봉 live·후보/점수·표시 fallback·주문/청산/사이징 제외.
- #68 `0b978e095ab1625a2f40b3669f0d06537df3f5e5`은 보류 유지. 병합·리베이스·닫기·전체 cherry-pick하지 않는다. 후속은 Draft [PR #70](https://github.com/qwq-partners/qwq-ai-trader/pull/70)에 통합한다.
- 실행 코드/운영 PID를 조회하지 않았다. 작업공간 SHA는 운영 실행 SHA가 아니다.

## Do — 역할·모델과 구현

| 역할 | 모델 / effort | 책임 |
|---|---|---|
| A | gpt-6-astra / high | 승인 등록부·immutable authority·토큰 context·예산 전검사 |
| B | gpt-5.6-terra / high → gpt-6-astra / high | 지속 원장·관측·캘린더·보고; 영속성 리뷰 결함 이후 상향 |
| C | gpt-6-astra / high | OAuth·bounded HTTP·공개 스펙 계약 |
| 부모 | 현재 주 에이전트 | worker·배치/송신 gate·KR 최소 훅·하트비트·통합·문서 |
| 독립 See | 각 구현자와 다른 gpt-6-astra / xhigh | task별 재현·수정 후 재리뷰·통합 확인 |

별도 feature worktree와 파일 소유권으로 병렬 진행했다. 공유 파일은 부모만 편집했다. TDD 실패 재현을 확인하고 수정했으며 리뷰어는 구현 파일을 수정하지 않았다.

인증 없는 [공식 공개 OpenAPI](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json)만 조회해 `1.2.17`, 원본 SHA256 `791082da4cb379117ed9fdc29a45bd42746f7a1aec368da1e9f4e1f3bfbff5b4`를 고정했다. OAuth 형식과 캘린더 세션 경계를 새 fixture에 보존했고 기존 synthetic fixture를 교체하지 않았다. 캘린더 auction 경계는 optional/nullable이므로 존재하는 값만 검증한다.

## See — 발견·수정·검증 원칙

테스트 수는 재현의 범위를 대신하지 않는다. 주요 리뷰 결함과 조치는 다음과 같다.

- Authority의 직접 생성/변경 우회와 갱신 불허 시 유효 캐시 손상을 막았다. context 없는 발급·다른 task의 context 재사용을 거부하고 revoked 안전 기록은 중단 후에도 유지한다.
- GET의 aiohttp timeout 반올림에 의한 승인 기한 초과를 바깥 정확한 deadline으로 차단했다. GET와 관측 유래 renewal POST 모두 원래 슬롯 monotonic 상한과 송신 시점의 날짜/세션을 검사한다.
- 발급 횟수 소진을 intent 뒤에 검사하여 정상 캐시가 unknown으로 잠기던 경로를 수정하고 재리뷰했다. POST 가능성이 있었던 실패의 unknown은 자동 해제하지 않는다.
- 원장 최초 구현은 short write/fsync·단일 writer·엄격한 재읽기 스키마·중복 terminal·지속 비교 근거/분모·공식 캘린더 형식에서 수정 요청을 받았다. 수정 후 세션 경계의 동일 원관측 재사용도 제외하도록 보완했고 `d396bff`를 원 리뷰어가 통합 승인했다(앞선 239건 및 최종 한정 10건 독립 통과).
- worker 반복 종료가 새 소유자를 지우던 문제를 수정했다. 종료 확인 전에는 소유권을 반환하지 않고, 취소된 제출은 진행 작업을 중단한다.
- 하트비트의 host-local naive 시각을 KST로 오인하던 문제를 UTC/KST 기본 호출 테스트로 고정했다. 선택적 observer import 실패에도 기존 KR task 12개의 관리 핸들을 반환한다.
- 승인 대기 중 OFF/취소, 원장 대기 timeout, 작업 사이 슬롯 만료를 재현했다. 종료/취소를 approval_pending이나 정상 완료로 잘못 표시하지 않는다.
- 최종 통합 리뷰에서 승인 client와 실제 자격 client 불일치(P1)를 찾아 lazy 로더의 정확한 `client_id` 대조로 차단했다. OFF가 초기화/관측을 중단한 경우에도 종료 확인 후에만 disabled를 게시하며 `stopping_unconfirmed`를 가리지 않는다(P2).

## 인수 근거 매핑

모든 테스트는 가짜 HTTP·시계·임시 파일·합성 grant다. 운영 환경에서 재현했다고 해석하지 않는다.

| ID | 로컬 근거 | 한계/별도 인수 |
|---|---|---|
| R01 | live_authority, runtime, runtime_factory, shadow_scheduler | 실제 launcher/승인 등록부 설치 미실시 |
| R02 | authorized_tokens, token_contract, runtime | 다른 호스트 발급 중지는 로컬 lock으로 강제하지 못함 |
| R03 | oauth, http_body, client_boundary | 실제 자격/토큰·공급자 송신 미실시 |
| R04 | 기존 request budget/rate/client 회귀 + observation 청크 | 운영 한도/429 관측 미실시 |
| R05 | observation 비교·Fraction·시각/상태/시장 검증 | 현 캐시 비교 0건은 정상 insufficient일 수 있음 |
| R06–07 | observation_ledger 재시작·ACK·단일 writer·손상/쓰기 실패 | 커널 정지의 강제 종료/자동 복구 기능 없음 |
| R08 | observation + shadow_scheduler + loop_heartbeat | 성공은 관측 성공, 소비자 승격 아님 |
| R09 | calendar_observation + 날짜 경계/별도 일정 | 기존 세션·휴장 정본 변경 0 |
| R10–11 | snapshot 선정·청크 실패·money_path_invariance | KIS 시각/시장 기준 보강은 별도 작업 |
| R12 | 실제 worker thread/취소·정지 미확인 + 감독 테스트 | thread는 프로세스 메모리/GIL 격리가 아님 |
| R13 | 실제 REST→ExitManager를 OFF/ON × KIS 성공/실패로 비교; sync/fill/exit 메서드 정적 검사 | 모든 브로커/주문 호출부의 동적 fault matrix를 실행한 것은 아님 |
| R14 | 기존 market/shadow API·fixture 회귀 + report eligible=False | 최소 영업일 실자료 인수·승격 미실시 |

## 운영 해석

`TOSS_API=1`만으로 활성화되지 않는다. 운영자가 관리하는 read-only registry·plan/grant와 시작 시점 release/config/artifact/host/UID attestation이 모두 필요하다. 현재 launcher/배치 공급자는 설치하지 않았다. 실관측에 필요한 값은 샘플에서 자동 채우지 않는다.

`auth_max_issues`는 worker 수명당 POST 상한이다. 재시작을 합친 grant 전체 누적 상한은 아니다. durable bootstrap 1회와 승인 기간/권한 검사는 별도로 유지한다. grant 전체 예산이 필요하면 추가 설계·검증 전 활성화하지 않는다.

원장 성공·KIS 가격 비교 성공·운영 승격을 구분한다. 원장 full-plan expected에는 미래 예정 슬롯도 포함되므로 미기록/coverage와 물리적 원장 incomplete를 동일시하지 않는다. 모든 결과의 `production_eligible=False`는 유지한다.

## 최종 검증·인계

검증된 소스: **`984dbdf4504d33cec590fbe6e963323a4314b6f5`**. 이후 문서 인계 커밋과 구분한다. 주요 통합 이력은 HTTP `d834cb9/9469dc4`, authority `bc1011f/5009c4e`, 예산 `61ab705`, 원장 `86dbab4/f3fbc38/ba75399`, worker/배선 `984dbdf`다.

- clean-env `scripts/dev/verify.sh`: **UTC 1684 passed / 2 xfailed / 1 warning (65.51초)**, **KST 동일 수치 (64.21초)**. 두 실행 모두 문법·비밀정보 패턴 검사 통과, 외부/운영 접근 0.
- 기준선 `9624559` 1379 passed 대비 305건 증가. 기존 xfail 2건은 손절 수수료 기준·익절 접촉 백테스트 parity 차이이며 이번 Toss 작업에서 해소했다고 주장하지 않는다. warning은 기존 pykrx deprecation이다.
- 독립 통합 리뷰: Astra/xhigh, 구현자와 분리. Task2 연결 127건, 최종 runtime/factory/실모듈 E2E/scheduler/돈 경로 **UTC/KST 각각 55건** 독립 통과. client 불일치 POST 0 및 OFF로 실제 worker 초기화가 중단되는 경로를 재현해 수정 후 승인, 잔여 P0/P1/P2 0.
- 검토 고정 해시(SHA256): factory `ad6b74593c2d99ba5b2602a92b7dcdba60fb866014371218fd0976b05eb12628`; supervisor `b5e22fb7e7284500f95caf0ebd568c974a90a54c1d4d3bc44e577ca993274eb0`; heartbeat `8ac306ea7ffc4f15811e88078906f8df777569d6f8475b735309010b325880a5`; worker `8fa72e56a358e1911cb3e7e875350045c4afe04347f24c24a173859ca58744d8`.
- 기존 synthetic fixture 3개 및 `src/core`, `src/execution/broker`, 설정/의존성 파일은 main 기준 무변경. main worktree도 clean으로 확인했다. #68 head/state는 기존 SHA·OPEN 그대로다.
- PR #70은 Draft로 유지한다. source `984dbdf`의 [GitHub verify](https://github.com/qwq-partners/qwq-ai-trader/actions/runs/35109894762)는 SUCCESS로 확인했다. 로컬 검증과 별도이며 후속 문서 head의 CI도 PR에서 확인한다. 이 보고서로 main 병합이나 실관측 활성화를 승인하지 않는다.

실관측 전 별도 작업: 운영자 grant/약관·저장/자격 소유권 승인, immutable release/launcher attestation 배치, 승인 기간/정책, 실제 자료 최소 영업일 검증. KIS 시각/시장 기준 보강과 모든 주문 경로의 확장 동적 fault matrix, 일봉 live 및 소비자 승격은 아직 수행하지 않았다. 운영 토큰·설정·주문·SSH·배포/재시작은 이번 작업에서 0이다.
