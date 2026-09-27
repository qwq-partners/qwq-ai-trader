# Claude 인계 프롬프트 — B1 실행기 마감과 전체 엔진 후속

> **결과 (2026-09-28 02:30 KST 갱신):** §6 A~E 를 Claude coordinator 가 수행했다. 결합 후보 `df9fd43`(W+C+M) — copied 사후 리뷰
> APPROVE_WITH_RECORDED_LIMITS, focused 3244 passed, readiness APPROVE_WITH_CONDITIONS, 원형 tiny profile 1회 `B1_PROCESS_BOUND`
> (bracket 17014274900ns, 36/36), 전체 UTC/KST 각 9361/16/2(TZ당 첫 시도 벽시계 시험 1건 실패 → 사용자 결정 1안으로 1회 재실행, raw 보존),
> final critical APPROVE_WITH_CONDITIONS 후 W 에 ff 통합. 증거는 RD 의 `combined-*`, `b1-smoke-*`, `full-*-df9fd43*.log`, `task-3d-copied-runtime-postrun-review.md`.
> 새 full-profile 차단 사유: `tests/dev/test_source_runtime_contract.py:489-496` 1,048,677-byte node ID. 사용자 추가 지시(09-28): 과도한 방어 로직 지양,
> Toss/KIS 혼용(주문·잔고·계좌는 KIS 만), **미국 거래 영구 중단 — US 관련 시험·적용은 예외 처리**. §7·§8 은 이 결과를 전제로 다음 단계다.
> main 병합·운영 배포·재시작·실주문은 여전히 미승인.

2026-09-28 KST. 아래 내용을 Claude의 후속 작업 지시로 사용한다.
사용자가 Codex의 새 구현을 중지하고 Claude로 후속을 넘겼다. 실행 중이던 시험의 종료만
확인하고 인계한다. 이 문서는 운영 실행 허가가 아니다.

## 1. 역할·목적·경계

너는 QWQ AI Trader의 후속 coordinator다. 과거 작업을 재구현하지 말고 동결 후보와
실제 원문을 확인한 뒤 **Plan → Do → See**로 진행하라.

최종 목적은 **현행 위험 한도를 유지하며 비용 차감 후 KODEX200 대비 초과수익을 검증할
수 있는 신뢰 가능한 엔진**이다. 현재는 상태·소유권·실행 증거를 확보하는 개발 단계다.
시험 통과·LLM 합의를 수익성이나 실거래 준비 완료의 증거로 대신하지 마라.

- 거래·주문·잔고는 **KIS**, **Toss는 조회·관측용**이다. 역할을 바꾸지 않는다.
- 현재 범위는 로컬 개발·검증·문서다. main 병합, 운영 SSH/배포/재시작, 실주문,
  전략·위험·운영 설정 변경, 자격증명/권한 변경, 설치·다운로드를 과거 승인에서 추론하지 마라.
- 이전에 승인된 개발 feature의 비강제 push는 최종 검증·리뷰·문서 마감 후 대상/원격을
  재확인하여 처리할 수 있다. main push·force push는 그 범위가 아니다.
- 새 사용자 판단 없이 가능한 승인 범위의 단계는 순서대로 진행한다. 새 권한·공식 근거·
  자격이 필요하면 해당 단계만 차단하고 필요한 대상과 이유를 정확히 보고한다.
- 기존 두 성능 예외만 유지한다. 새 timeout·수명·정합성·정리 실패를 예외화하거나
  시험 삭제·표본 축소·상한 확대·반복 실행으로 통과시키지 마라.
- 운영은 이번 인계에서 재조회하지 않았다. 옛 배포 SHA/PID를 현재 사실로 보고하지 마라.

## 2. 규칙·모델 배정

먼저 `/home/ubuntu/.config/ai-agents/model-routing.md` 전문을 읽는다.
프로젝트 AGENTS.md, CLAUDE.md의 개발 규칙·현황, CHANGELOG.md, docs/README.md와 관련
skill을 확인한다. `.env`·과거 운영 자격정보 절의 내용을 출력하지 마라.

모든 dispatch에 모델 AND effort, 이유, base SHA, 허용 파일, 금지 행동, 인수 조건,
시간/사용량 한도를 적는다. 정책의 실제 모델 식별자·가용성에 맞춰 아래 역할을 배정한다.

| 역할 | 요청 모델/effort |
| --- | --- |
| 기계적 문서·목록 정리 | Luna low/medium |
| 작은 일반 구현·시험 | Terra medium/high |
| 다중 파일 조사·일반 독립 검토 | Sol high |
| 소유권·동시성·프로세스 설계/구현·어려운 재현 | Astra high 또는 검증된 Claude Opus high |
| 최종 critical 독립 리뷰 | 비작성자 Astra xhigh 또는 검증된 Opus xhigh |

작성자가 자신의 critical 변경을 승인하지 않는다. source와 독립 expected 작성자도
분리한다. Fable은 미자격이며 Opus fallback을 Fable로 보고하지 않는다. Sonnet/Haiku도
자격을 가정하지 않는다. 실제 model/effective effort 미노출은 unverified, 같은 공급자
독립 리뷰는 cross-provider가 아니다. 검증 실패를 보안 우회로 해결하지 마라.

coordinator1 + Codex/외부 Claude 합산 worker 최대3, fanout 금지. 병렬 writer는 같은
동결 base의 다른 worktree·비중복 파일을 소유한다. **전체 시험·실제 B1 프로필은 읽기
전용 worker까지 모두 종료한 뒤 coordinator만 단일 workload slot에서 실행한다.**

## 3. 실제 인계 상태

루트 `/home/ubuntu/projects/qwq-ai-trader`, 아래 worktree는 `.claude/worktrees/` 기준이다.

| 구분 | worktree / branch | HEAD | 역할·상태 |
| --- | --- | --- | --- |
| W | `owner-ticket-gate-20260926` / `feature/owner-ticket-gate-20260926` | `10423c1873525019761b480f1dc94bc9ab742d51` | 인수된 B1a/B1b·통합 개발 기준. 미커밋 인계 문서 있음. runner 최종 코드 미통합 |
| C | `b1-runner-controller-20260927` / `feature/b1-runner-controller-20260927` | `c93f567381c10cc604fae49fb44585badc8a3b03` | clean. 최신 runner·독립 시험·copied 시험 후보 |
| M | `b1-nodeid-metadata-20260927` / `feature/b1-nodeid-metadata-20260927` | `82e107f9f43c72607fcb436d8834712f384753a0` | clean. 65537-byte 입력의 parameter ID 한 건만 변경 |
| R | `b1-runner-contract-20260927` / `feature/b1-runner-contract-20260927` | `de96a6eb301d7d3d3b6e4fb1a2036709abc06c90` | clean. 독립 시험 이력, 필요한 내용은 C에 포함 |
| root main | 프로젝트 루트 / `main` | `a454277e385842e8c383d96fe4ef47dc86bc65ca` | 사용자 소유 `config/evolved_overrides.yml` 변경. 읽기/편집/덮어쓰기/커밋 금지 |

C tree: `e26f869bb5c60cd03782253847af2dd8887ebf70`.
W의 마지막 실제 원격 대조는10423c1 일치였다. 새 조회가 아니며 C/M/R의 원격 존재를
가정하지 않는다. 인계 전환 후 새 commit/push/merge/운영 작업은 하지 않았다.

W의 coordinator 소유 미커밋 문서는 본 문서와 다음 세 파일이다. 재개 시 status/diff로
정확히 확인하고 보존한다:

- `docs/reviews/b1-standard-runner-2026-09-27.md`
- `docs/superpowers/plans/2026-09-27-b1-standard-runner.md`
- `docs/README.md`

`git add -A`, 무차별 stash/reset/checkout, worktree 삭제를 하지 마라. 동결 branch와
최초 실패 원문을 정리 명목으로 지우지 마라.

## 4. 필수 근거 문서

이하 상대 경로는 W 기준이다. 옛 문구보다 현재 SHA·원문이 우선한다. 과거의 main125/
공개 미활성/copied 미실행은 당시 이력이다. 현재 C의 gate 제거·copied 실행과 W의
최종 미통합을 구분하라.

- `docs/reviews/b1-standard-runner-2026-09-27.md`
- `docs/superpowers/specs/2026-09-27-b1-standard-runner-design.md`
- `docs/superpowers/plans/2026-09-27-b1-standard-runner.md`
- `docs/reviews/b1-nodeid-compatibility-2026-09-27.md`
- `docs/reviews/decoder-b1a-2026-09-27.md`, `docs/reviews/decoder-b1b-2026-09-27.md`
- `docs/operations/engine-delivery-next-2026-09-27.md`
- `docs/reviews/autonomous-progress-2026-09-27.md` — 옛 단계 표는 최신 원장과 대조.

원시 증거 디렉터리를 **RD**라고 한다:

`/home/ubuntu/projects/qwq-ai-trader/.claude/worktrees/owner-ticket-gate-20260926/.superpowers/sdd/2026-09-27-b1-standard-runner`

RD에서 읽을 순서:

1. `task-3d-copied-runtime-evidence.md`: 최종 실행·raw·hash·tool/session 연결.
2. `task-3d-copied-real-case-review.md`: 실행 전 oracle 승인·두 P2 수정.
3. `task-3d-copied-helper-case-review.md`, `task-3d-copy-helper-evidence.md`.
4. `task-3d-public-enable-evidence.md`, `task-3d-public-enable-review.md`.
5. `task-3d-main-focused-evidence.md`, `task-3d-main-source-independent-review.md`.
6. `task-3d-copied-fault-anchor-proposal.md`, `task-3d-copied-fault-anchor-review.md`.
7. `combined-candidate-readiness-inventory.md`, `combined-candidate-preparation.md`.
8. `task-4-smoke-dispatch-draft.md`: 최종 SHA/경로/입력 미동결 실행 초안.
9. `next-r2-design-inventory.md`, `progress.md`.

RD는 ignored 로컬 증거이며 remote commit에 포함되지 않는다. 다른 호스트에서는 허용된
source/문서/비기밀 raw의 존재를 먼저 확인한다. 누락을 요약으로 만들어내지 마라.
`.env`·token·계좌/거래 상태를 전송하지 마라.

## 5. 완료 범위·마지막 실제 결과

- B1a/B1b는 독립 oracle·actual mutant·critical·당시 UTC/KST 전체 검증 후 W에 통합됐다.
  B1b의 역사적 전체 결과는 각8411 passed,16 skipped,2 xfailed,4 warnings다.
  새 runner 결합 후보의 전체 결과로 재사용하지 않는다.
- Runner 계약367/parser170/budget219/coordination333/output415/예외정리568/shared-main757은
  각각 부품 한정 독립 인수를 마쳤다. 반복 구현하지 않는다.
- 공개 gate 제거748f0ad: 독립 RED→17건→새·기존 controller **765 passed/53.97s**,
  `PUBLIC_ENABLE_COMPONENT_ONLY`. 최초 clock은 모드 분류 전이라는 기존 제약을 유지하고
  raw0만으로 승인하지 않는다. hard125 보장은 validated-body 이후다.
- Helper acf18ce: parent/repo/scripts symlink 실제 RED 보존→fresh exclusive root 수정→
  관련15건·독립 helper 한정 승인. v1·유일한 독립 reaper·backstop은 불변이다.
- Copied oracle c93f567: Astra/xhigh 요청 비작성자 검토, actual model/effective effort는
  unverified이며 미해결P0/P1/P2=0.
  자손 준비 pipe/marker와 bad-guard 전송 marker로 시험의 경합·거짓 통과를 보완했다.
- **최종 실행:** dummy1 passed/1.71s → copied12 passed/19.30s. 둘 다 격리0,
  workload/tee/tool=0/0/0. 사례는0/1/9/signal,timeout,double-fork/late-adoption,
  양stream flood,bad guard,contention,observer escape,candidate 후crash97이다.

| raw | SHA256 | 도구 연결 |
| --- | --- | --- |
| `task-3d-copied-dummy-c93f567.log` | `c91bfb1f4ebdbf4b7bf353f7fca37f4cf2d5db175b4fce4dbd512f4411acf5df` | 346604 / session31412 → terminal1da4a5 |
| `task-3d-copied-cases-c93f567.log` | `9e42f57a79a3e529a6b6f476626db0798a728d11cd3072b00adf4919966b5e01` | ae390d / session36184 → terminalae6eae |

C 지문:

- controller `bebf44daa73861efb52749a0f9dcbf047aa00665418ae09396512756f3ede1b0`
- copied tests `94c25af6d53c48b0e41e425d8183a56ae4bd9480713c286b3673adbbfd604a32`
- guard `7b7b26940309a2a165d9bdba7611b6730e83b90ae9ea5f9e725764ee4c0a23f7`

**미완료:** copied 실행 결과의 독립 사후 리뷰, C+W+M 결합, 결합 focused, readiness review,
원형 B1 tiny profile, 결합 전체 UTC/KST, final critical, 최종 feature 통합·push.
인계 문서 검토는 위 검증의 대체 승인이 아니다.

## 6. 최우선: runner 마감 Plan → Do → See

### A. 현황·증거 인수

- Plan: 규칙/문서/immutable diff, worker·branch·미커밋 소유권 확인.
- Do: C clean/SHA/지문, raw 두 건의 내용/hash/격리 표시를 확인한다. 비작성자 critical
  reviewer가 copied 결과와 oracle의 정합성을 검토한다. 증거 누락은 명시하고 처리 방침을
  정하며 편의상 재실행으로 덮지 않는다.
- See: 실행 전 oracle와 실행 후 evidence 승인을 구분한다. blocking은 원인에 맞게 수정한다.

### B. 전용 결합 후보

- Plan: W/C 공통base `b073b541ea730868562677bf03dcc0b66be97368`.
  조사 당시 W16/C6/M1 경로는 중복0. M의 부모 `2fc2531b352be77677283f2111baf06c7582b8d8`은
  W 조상이지만 M 자체는 미포함이다. 최신 C 보완도 같은 test 파일이다. 실제 차이를 재확인한다.
- Do: W의 문서 변경을 검토·allowlist로 정리한 후 root만 C 또는 새 격리 후보에 W+C+M을
  결합한다. R을 중복 이식하지 않는다. main/사용자 설정은 보존한다. SHA/tree를 동결한다.
- See: runner6/M1/W의B1b5blob/문서 외 예상 밖 차이0. B1b blob은 readiness inventory와 대조한다.

### C. 결합 focused·정적 검증·readiness

- Plan: 아래8파일을 literal selector로 고정, 기존 cap300 launcher·최초 실패 중단을 사용한다.

```text
tests/dev/test_b1_process_contract.py
tests/dev/test_verification_os_contract.py
tests/dev/test_verification_contract.py
tests/dev/test_b1_evidence_controller.py
tests/dev/test_pytest_evidence_controller.py
tests/dev/test_controlled_verification_evidence.py
tests/structural_b1/test_structure.py
tests/structural_b1b/test_b1b_structure.py
```

- Do: 기존 venv/guard/env-i/shared flock/-x/승인 plugins로 실행하고 workload/tee/tool,
  raw/hash·compile-only·diff·기존 비밀 패턴 검사를 남긴다. 설치·설정 변경은 추가하지 않는다.
- See: 첫 신규 실패에서 멈추고 비작성자 critical readiness 승인 뒤에만 D로 간다.
  copied 증거는 잠금 경로가 다른 합성 시험이다. 일반900·timeout/contention만total6이며
  원형profile·실240초 lock 상한·native 자격으로 취급하지 않는다.

### D. 원형 B1 tiny profile 단 한 번

- Plan: 모든 worker 종료 후 smoke 초안을 최종 동결한다. candidate SHA/tree/cwd,
  code/runtime/guard 지문, 독립 수집36-node inventory,context/run/attempt/기대값,
  미존재 artifact 경로와 clock 명령 전문을 사전 고정한다. 대상은
  `tests/dev/test_verification_contract.py` 한 파일, 기존 지문은 초안을 참조한다.
- Do: 기존 절대 Python `-I -B`로 미변환 controller를 직접 실행한다. 고정5변수 env-i,
  `--profile b1-standard/v1 --timeout-seconds 900`, 실제 공통lock을 사용한다.
  **외부 flock/tee/timeout 래퍼 금지.** controller가 잠금·출력·정리를 소유한다.
  같은host/boot/monotonic-domain의 pre-read 완료 receipt 수신 후 시작하고 controller
  terminal 수신 후 post-read를 시작한다. raw session 연결을 보존한다.
- See: raw controller0, 정수 `0 <= post_ns-pre_ns <= 900000000000`(모든 간격 포함),
  양log<=2097152/hash일치/overflow없음,guard/reap/cleanup/coordination,
  receipt/process/독립 기대값과 pure binder 일치, 실제36call passed를 별도로 확인한다.
  wait 시간 합·간격 차감·pytest 시간·JSON만으로 승인하지 않는다. 누락/역전/host차이/
  비terminal/상한 초과는 중단하며 수치 개선용 재시도는 없다. outcomes/native/CI/production
  자격 플래그는false 유지한다.

### E. 결합 전체 UTC → KST·최종 리뷰·feature 통합

- Plan: worker를 계속 전부 중지한다. 후보별 inventory·기존 skip/xfail을 고정한다.
  승인된 **legacy 표준 launcher**의 전체 회귀이며 B1 full-profile 실행이 아니다.
- Do: 고정env/plugin/guard/shared lock/-x, UTC cap900 성공 후에만 KST cap900 직렬 실행.
  raw·전 종료값·격리0을 기록한다. 과거8411건/focused로 새 전체 검증을 대신하지 않는다.
- See: exact tested tree/raw의 비작성자 final critical 후에만 W feature 통합,
  후보와 비문서diff0,docs/CHANGELOG/색인/인계 갱신,allowlist commit,허용feature 비강제
  push·정확한remoteSHA 대조. main/운영 승격이나 통과용 반복·새 성능 예외는 없다.

정확한 launcher는 tracked runner plan의 `Verification command profile for future
authorized execution`를 읽는다. focused300/whole900, 고정fake-key 부재/비symlink를
사전 확인하고 실키/HOME을 전달하지 않는다. 옛 함수나 에이전트 명령을 무검토로 eval하지 마라.

## 7. Runner 이후: full R2는 먼저 서면 설계

Plan: RD/next-r2-design-inventory.md와 아래를 전문으로 읽는다.

- `docs/superpowers/specs/2026-09-27-recovery-decoder-compatibility-decision.md`
- `docs/superpowers/specs/2026-09-27-decoder-b1b-construction-control-design.md`
- `docs/superpowers/plans/2026-09-27-decoder-b1b-construction-control.md`
- `docs/superpowers/specs/2026-09-27-recovery-cold-source-ownership-design.md`

Do: 첫 산출물은 별도 test-only transition/oracle 설계다. 아래7계약마다 유한phase/row,
한 행의 한 실제slot-write, 모든holder/alias/live-domain, 정상 전후·각행 직전/직후fault,
terminal·actual mutant·source행/SHA 감사를 대응시킨다.

1. duplicate replacement: last-wins/first-key순서, old절단 전 책임holder, 버리는KEY처리,
   nested outer-old/inner-old/new,active retire batch 최대1.
2. WORK seed: logical semantic edge마다 한 번, tail index alias를 child로 세지 않음.
3. WORK pop: target/next 책임 이관 후clear/unlink, WORK처리를 위한 재귀WORK 생성 금지.
4. retire: partial/current/remaining/processed 책임, 완료 전 다음sibling 생성 금지.
5. finish: surviving tree만 검사, 덮어쓴nonfinite 재방문 금지, 남은비유한값 거부.
6. known_failure: graph를args/cause/Future에 숨기지 않는 작은code/position, 최초원인/취소 구분.
7. dispose: root/CONT/pending/replacement/WORK/scratch 책임·종결, foreign graph fault를
   강제clear/성공으로 바꾸지 않고 DISPOSAL_FAULT/pending으로 보존.

인수한 B1a/B1b 파일·expected·17actual mutants를 보존하는 재사용 경계를 먼저 선택한다.
expected를subject snapshot으로 만들거나 source를expected표 실행기로 만들지 않는다.
weak census/일반frame-return 뒤 Python 관측을native actual-free로 승격하지 않는다.

See: 비작성자critical의 **plan-only** 승인 후 별도implementation plan으로 exactbase/files,
독립expected/source담당,RED/fault/mutant행렬,cap/중지조건을 고정한다. 그 전R2 source/test
생성·첫RED는 금지다. 승인된 작은 범위부터 TDD→구현→독립리뷰→실검증하며 observed-only와
native 인수를 구분한다.

## 8. 더 남은 엔진 작업

아래는 runner 성공으로 자동 허용되지 않는다. 독립 서면조사/설계는 병렬 준비할 수 있으나
실행은 각 근거·권한 gate를 따른다.

| 단계 | 필요한 계획·구현·검증과 미확보 근거 |
| --- | --- |
| N4097 | 의미oracle·전수inventory·full-profile 전체nodeID,2MiB강제runner,dedicated1+UTCfull1+KSTfull1 각각900의 별도리뷰/GO. 현재UNRUN. N축소/샘플링/과거예외 대체 금지 |
| native/source | exact capsule원본/build/patch/loaded-file closure,격리harness,독립qualification/actual-death oracle,신뢰된등록revision. qualified runtime0/source108 call-phase0/실행false. host버전/옛local139/빈registry 대체 불가 |
| cold 소유권 | allocation-time책임/독립실관측지점,반환gap,SQL/native부분실패,취소,cleanup certificate,consumer take/return. 준비설계만승인·RED_DEFERRED. 없는속성을getattr(False)로 읽는 가짜RED 금지 |
| 단일runtime owner | 승인index/gate로engine/scheduler/broker/KOFR/수동/일일reset,commit/restore/writer/result-drain 순차이관. generation/currentness/입력전파 고정. full scan은폐/history삭제/stale을current처리 금지 |
| 실제consumer/outbox | 장중보호durable claim,entry policy이력/전파,경제reducer/outbox의 실queue소비/중복방지/부분실패. 단순decision을order/delivered로 승격 금지 |
| 주문경계 | KIS기존limiter/adapter통합,Portfolio/ExitManager/riskDTO실경로,최초인계/취소chain 공식최종성근거,MODIFY수량/가격증가분 추가예약/위험상한. 근거/예약없는정정 송신금지. 실API호출은 로컬시험과 별도권한 |
| health/경보/장기성능 | bounded capture/게시,같은revision/freshness표본,N6shadow,실owner배선후63-cell/장기이력·부하/미실행cell처리. 기존health두수정 완료와 전체경보/성능인수 구분 |
| 전체 C/F/G/R | 원계획의 실제시험명별 충족/미충족/근거미확보 대조,실결합/전체회귀/독립broad. 약어의미/인수기준을 새로 만들지 않음 |
| main/운영 | 위조건과main차이처리 후 당시명시권한/deploy/rollback/restart/health/당일관측계획으로 별도release. 개발시험만으로trading_ready=true 금지 |

후속 참조:

- `docs/superpowers/specs/2026-09-27-required-source-proof-design.md`
- `docs/reviews/source-proof-boundaries-2026-09-27.md`, `docs/reviews/runtime-admission-2026-09-27.md`
- `docs/superpowers/plans/2026-09-27-recovery-cold-source-ownership.md`
- `docs/reviews/recovery-projection-progress-2026-09-27.md`
- `docs/superpowers/plans/2026-09-18-engine-writer-migration.md`
- `docs/operations/p1-next-steps-2026-09-23.md`
- `docs/reviews/recovery-scale-2026-09-23.md`
- `docs/superpowers/plans/2026-09-17-engine-execution-safety.md`
- `docs/reviews/engine-execution-followup-2026-09-18.md`
- `docs/integrations/kis-execution-evidence-request-2026-09-18.md`

## 9. 단계별 보고 형식

1. Plan: 대상/비대상,base SHA,writer/reviewer/model/effort,인터페이스,종료조건.
2. Do: 실제diff/commit,RED/수정,정확한명령,예외/미실행범위.
3. See: raw/hash,tool/session/terminal연결,pass/fail/skip/xfail/격리,reviewer지적/처리,
   승인범위(부품/전체/실profile).
4. 다음: 지금구현가능 / 서면설계만가능 / 외부근거·권한필요를 구분.

첫 보고는 현황복원·W/C/M차이·copied12증거확인·다음결합gate의 짧은 요약이면 된다.
계획 전체를 재발명하지 말고 **제6절 A부터 재개하라.**
