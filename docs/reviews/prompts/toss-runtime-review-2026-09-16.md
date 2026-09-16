# Toss 관측 런타임 독립 교차 리뷰 프롬프트

아래 범위를 읽기 전용으로 리뷰하라. 기존 승인/통과를 그대로 인용하지 말고 소스와 재현 증거로 판단하라. 구현 수정·git 쓰기·패키지 설치·자격/운영 파일·인증 API·SSH·배포/재시작·주문/설정 변경은 금지한다. 테스트는 임시 경로·가짜 HTTP·합성 승인만 사용한다.

## 고정 기준

- 저장소: `qwq-partners/qwq-ai-trader`
- PR: https://github.com/qwq-partners/qwq-ai-trader/pull/70 (Draft)
- 기준 main: `a3187a8252c11cbed4b27097d260456c5078dcec`
- 검증된 **소스 head: `984dbdf4504d33cec590fbe6e963323a4314b6f5`**. 이후 문서 전용 커밋과 구분한다. 시작할 때 실제 HEAD/dirty 상태를 기록하되 checkout/rebase/pull을 하지 않는다.
- 중복 #68 `0b978e095ab1625a2f40b3669f0d06537df3f5e5`은 보류다. 비교 참고만 가능하며 병합·닫기·리베이스 대상이 아니다.
- `AGENTS.md`, `CLAUDE.md`, 최신 `CHANGELOG.md`, `docs/README.md`를 읽고, 승인 설계/구현 계획/최종 보고서의 범위를 대조하라. 자격 예제/운영 비밀을 출력에 복사하지 않는다.

## 읽을 변경과 계약

```text
docs/superpowers/specs/2026-09-16-toss-runtime-shadow-design.md
docs/superpowers/plans/2026-09-16-toss-runtime-shadow.md
docs/reviews/toss-runtime-2026-09-16.md
docs/operations/toss-shadow-runtime.md
src/data/providers/toss/{approval,authorized_tokens,oauth,http_body,transport}.py
src/data/providers/toss/{observation_ledger,observation,calendar_observation}.py
src/data/providers/toss/{runtime,runtime_factory}.py
src/schedulers/toss_shadow.py
src/schedulers/kr_scheduler.py (관측 task와 성공 후보 복사 훅만 변경)
src/utils/loop_heartbeat.py
scripts/review_toss_observation.py
tests/test_toss_*.py, tests/test_loop_heartbeat*.py
tests/fixtures/toss/runtime_spec_contract.json
```

`git diff --stat <base> <source-head>`와 `git diff --name-only`로 실제 목록을 확인하라. 기존 token/store/client/budget/normalizer/synthetic 계약을 함께 읽되 범위 밖 재구현을 제안하지 않는다.

## 우선 검토

1. OFF 부작용 0, 신뢰된 read-only registry·plan·실행 attestation 없으면 ON도 거부. 실제 배치 공급자가 설치되지 않은 것은 명시된 live 사전조건이다.
2. `client_identity`는 실제 OAuth `client_id`와 정확히 일치해야 한다. reader는 키를 읽지 않고, startup/bootstrap·갱신·조회 권한은 분리된다. `auth_max_issues`는 worker 수명당 상한이며 grant 전체 누적 상한이라고 오해하지 않는다.
3. 관측 유래 GET/갱신 POST의 최초 monotonic 슬롯 기한+송신 시점 날짜/세션 재검사, 태스크 context 격리, 발급 예산 소진 전 intent 방어, revoked/unknown의 안전 후처리.
4. 단일 writer/fd·전체 write/fsync·손상 보존·ACK 유실·재시작 시 HTTP 중복 0. 세션별 cohort와 `(plan,dataset,pair_id)` 원관측 중복 판정 분리. full-plan expected(미래 포함)와 물리 incomplete 분리.
5. 가격 시각/시장 결측을 발명하지 않음, 0 유효 비교는 None/insufficient, outlier를 유효 분모에서 빼지 않음, 캘린더 optional auction/null 휴장 및 날짜 경계.
6. worker 실제 종료 전 소유권 반환/교체 금지, 취소/timeout/OFF 후 상태 정확성. 기존 거래 loop에 HTTP/fsync await·추가 KIS 호출·Toss 결과 쓰기 0.
7. 관측 success와 유효 가격 비교/승격 분리, 부분 실패 계수·UTC/KST default clock·미등록/비활성·동적 OFF 경보 확인. 모든 보고서 `production_eligible=False`.
8. 실제 REST→ExitManager의 OFF/ON×KIS 실패 회귀와 기존 금지 경로를 확인하라. 모든 broker/order 호출부의 동적 fault matrix까지 수행한 것으로 확대 보고하지 않는다.

## 기록된 검증과 재실행

source head에서 clean-env 전체 UTC/KST 각각1684 passed/2 known xfailed/1 기존 pykrx warning, 격리 위반0·문법/비밀정보 검사 통과했다. 독립 최종 범위55건은 UTC/KST 각각 통과했다. 알려진 xfail은 백테스트 손절 수수료·익절 접촉 차이로 이 PR에서 수정/승격하지 않았다. 이를 새 실패나 해결된 결함으로 잘못 집계하지 않는다.

실제 경로를 확인한 뒤 다음처럼 자격 없는 환경에서 실행한다(공유 venv 사용, 운영 HOME/키 상속 금지):

```bash
toss_review_cache_dir=$(mktemp -d)
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=Asia/Seoul PYTHONPYCACHEPREFIX="$toss_review_cache_dir" PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTEST_ADDOPTS='-p no:cacheprovider --tb=short -rx' QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key QWQ_VERIFY_PYTHON=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python bash scripts/dev/verify.sh
```

UTC로도 반복한다. 명시적 `py_compile`은 bytecode 금지 환경변수와 별개로 캐시를 쓰므로 위 임시 prefix로 소스 옆 쓰기를 막는다. 테스트/검증이 파일을 요구하면 합성 자료·검증 캐시를 임시 디렉터리에만 생성한다. sandbox/의존성/시간 제한으로 재현하지 못하면 명시하고 PASS로 계산하지 않는다. 코드 수정은 하지 않는다.

## 답변 형식

- 우선순위별 실제 결함: P0/P1/P2, 파일·행, 원인, 영향, 최소 재현, 수정 방향.
- 기존 수정 재확인/잔여 위험과 미검증 항목을 분리한다.
- 실행 명령·테스트 수·검토 SHA·환경 격리 결과.
- 승인 / 수정 후 재리뷰 / 판단 불가 중 결론. 결함이 없으면 없다고 말하며 억지 개선 목록을 만들지 않는다.

이 리뷰는 오프라인 소스 평가다. 실관측/최소 영업일 성능 인수·운영 설치·배포·소비자 승격을 승인하지 않는다.
