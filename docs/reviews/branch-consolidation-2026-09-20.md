# 브랜치 정리·운영 동기화 — 2026-09-20

## Plan

사용자 요청: 과도하게 남은 브랜치 정리, 운영 반영 가능한 내용 적용, migration 후속과 Claude 새 세션 인계. main 병합 여부·현재 HEAD·dirty/untracked/ignored 증거·실제 프로세스를 먼저 감사했다. Sol/high가 브랜치/작업공간을, Astra/xhigh가 migration의 배포 경계를 읽기 전용 독립 감사했다. 요청 모델/effort이며 native actual model metadata는 미노출이다.

시작 시 열린 PR0. origin/main `465a029`, root main `8ff2f55`, engine `ab044c4`. engine은 origin/main의 후손(+18/-0)이므로 “오래된 main 때문에 갈라진 개발선”이 아니다. 임시 인계 작업공간을 제외한 정리 전 규모는 로컬222브랜치·원격76브랜치·138워크트리다. 임시 작업공간 생성 뒤 감사 snapshot은223/76/139였다.

정리 기준: main 조상·현재 SHA 일치·열린 PR 없음·사용 중 작업공간 제외를 확인한다. clean worktree의 ignored 파일도 검사하며 pytest/pycache 이외 증거가 있으면 보존한다. `reset --hard`, `branch -D`, `worktree remove --force`, 강제 push로 커밋 덮어쓰기, 일괄 prune은 사용하지 않는다.

## Do — 완료한 1차 정리

- clean·main 도달 가능 작업공간 **83개**를 명시 경로로 `git worktree remove`(force 없음)했다. 제거 직전 HEAD/상태/ignored 파일과 프로세스 cwd를 다시 대조했다.
- 작업공간에서 사용하지 않는 병합 완료 로컬 브랜치 **166개**를 `git branch -d`로 삭제했다.
- 원격 병합 완료 브랜치 **67개**를 정확한 full SHA별 lease와 atomic 삭제 push로 제거했다. main 및 고유 원격8개는 그대로다. 이는 커밋 강제 덮어쓰기가 아니라 동시 변경 시 거부하는 조건부 ref 삭제다.
- 사용자 개발선 기준 잔여는 **로컬56·원격9·워크트리55**다. 인계용 임시 브랜치/작업공간을 포함한 1차 직후 값은57/9/56이며, 인계 PR의 임시 원격 ref는 PR 작업 중에만 추가된다.
- dirty28워크트리와 고유 커밋/비캐시 증거 작업공간은 삭제·수정하지 않았다. engine-only worker의 기존 미커밋 파일도 유지했다. 보존은 활성 개발 지시가 아니며 새 작업은 engine 정본에서만 시작한다.

## 복구 자료

호스트 로컬 보호 디렉터리:

`/home/ubuntu/projects/qwq-branch-archive-20260920.vlFfQE`

| 파일 | 의미 |
| --- | --- |
| `all-refs.bundle` | 정리 전 모든 Git refs 및 detached worktree HEAD의 도달 가능한 커밋·전체 이력, verify 통과 |
| `refs-before.txt` | 로컬/remote-tracking refs 300개와 full SHA |
| `clean-worktrees-before.json` | 제거한83개 경로·HEAD·branch·캐시 파일 수 |
| `merged-branches-before.json` | 삭제 로컬166·원격67개의 정확한 이름/full SHA |
| `retained-after-first-cleanup.json` | 남은 refs/작업공간 및 dirty/비캐시 증거 수 |
| `engine-sdd-evidence.tar.gz` | engine의 ignored `.superpowers/sdd` 리뷰/재현 증거 별도 백업 |

bundle SHA256: `11ffd5a26663415b8084ccd674a4c982da140381623427504ec2b26ba4993fc0`.
SDD archive SHA256: `6f1ec19bbd532ba4ed30f4d13ff0f5fd3554f0dfe764e7f1bd426b5e17aa423f`.

bundle만으로 dirty/untracked 파일은 보존되지 않는다. **그래서 해당 작업공간을 원위치에 남겼다.** engine SDD도 원본을 삭제하지 않았다. 제거한 것은 재생성 가능한 clean checkout과 캐시이며 커밋 복구는 가능하다. 백업은 이 호스트 로컬이고 별도 원격 백업을 했다고 주장하지 않는다.

특정 삭제 브랜치가 필요한 경우 기존 ref를 덮어쓰지 않고 새 복구 이름으로 가져온다. 예:

```bash
git bundle verify /home/ubuntu/projects/qwq-branch-archive-20260920.vlFfQE/all-refs.bundle
git fetch /home/ubuntu/projects/qwq-branch-archive-20260920.vlFfQE/all-refs.bundle \
  refs/heads/feature/toss-pr68-integration-20260917:refs/heads/recovery/toss-pr68-integration-20260917
```

증거 archive는 기존 작업공간 위에 덮어 풀지 않는다. 별도 새 디렉터리에서 검사한다.

## 운영 반영과 미반영

- root main `8ff2f55→465a029` fast-forward 완료. 기존 거래 경로·설정·의존성 diff0이고 이미 별도 운영 중인 관측 서비스 소스/도구/문서를 동기화했다.
- 기존 거래 PID3534327, Toss PID3335469 유지. 10:36 KST broker connected·pending0·stale0, 보호7경로 지문 동일. 새 API/주문·설정·토큰 발급·서비스 재시작0.
- 이후 인계 문서 PR은 문서만 main에 추가한다. checkout 갱신과 새 엔진 프로세스 배포는 구분한다.
- 엔진166파일(+44,697/-389) 전체는 미병합 유지한다. `trading_ready=False`, MODIFY 미지원, 공식 startup/cancel evidence 및 전체 C/F/G/R·broad 인수 미완이다.
- 새 owner 미설치만으로 dormant라고 주장하지 않는다. WS parser·limiter·기존 sizing/regime wrapper·DDL은 기본 운영 경로도 바꾼다.
- limiter lease/finally hotfix는 독립 추출 가능성만 확인했다. 신규 safety collector 의존성이 있어 `bdda0e9` 전체를 운영에 cherry-pick하지 않았다. 별도 main 기반 추출·검증이 필요하다.

## See — 검증 기록

- main `465a029`의 새 직렬 전체 시험: **KST1810passed/기존xfail2/기존warning1,74.60초; UTC1810/2/1,70.69초**. 각각exit0·운영 상태/외부 네트워크 접근 시도0.
- engine `ab044c4`의 이번 새 직렬 전체: **KST4403passed/기존xfail2/기존warning4,280.83초; UTC4403/2/4,269.10초**, 각각exit0·격리0. 이전 C4 완료 기록도 양TZ4403/2이지만 이번 실행과 구분한다.
- Astra/xhigh 독립 문서 리뷰는 SPEC/QUALITY PASS·APPROVE_DOCS_ONLY_INTEGRATION·차단0이다. 고정 링크·복구 ref/해시·명령 문법·거래 경로 diff0을 직접 대조했다. Sol/high 사후 정리 감사도 삭제 refs/경로 잔존0·보존 SHA/dirty 수 변화0·증거289개 일치를 확인했다.
- tracked Python 문법·비밀정보 패턴·staged diff 검사를 통과했다. verify의 중복 pytest만 명시 생략했고 전체 suite는 위에서 별도 실행했다. required CI·main 병합 및 문서만 engine merge는 후속 Git/PR 기록과 함께 확인한다. 아직 실행하지 않은 게이트를 완료로 주장하지 않는다.
- 읽기 전용 운영 최근2시간: ERROR/Traceback0·원장/토큰 오류0, 게이트웨이 EGW00201 12건. 원인 미확정·별도 관찰 항목.

## 보존 원격 브랜치 (정리 직후)

| 이름 | SHA | 역할 |
| --- | --- | --- |
| main | `465a029` | 운영 기준, 후속 문서 PR만 병합 |
| feature/engine-safety-design-20260917 | `ab044c4` | 유일한 계속 개발 정본 |
| feature/toss-observer-installer-20260917 | `696b91c` | 고유 이력 보존 |
| feature/toss-observer-launcher-20260917 | `0decfbf` | 고유 이력 보존 |
| feature/toss-observer-runtime-20260917 | `f5c60a7` | 고유 이력 보존 |
| feature/toss-runtime-auth-20260916 | `34e9659` | 고유 이력 보존 |
| feature/toss-runtime-budget-20260916 | `d4e2b03` | 고유 이력 보존 |
| feature/toss-runtime-http-20260916 | `d5b3fc6` | 고유 이력 보존 |
| feature/toss-runtime-ledger-20260916 | `d396bff` | 고유 이력 보존 |

위7개 토스 worker 이력은 현재 main과 동등한 최종 구현이라고 간주하거나 다시 통째 병합하지 않는다. 검토된 통합본은 main에 이미 있으며, 고유 이력의 폐기/별도 archive는 다음 사용자의 명시 판단 대상이다.

## 보존 로컬 브랜치 (main·임시 인계 브랜치 제외)

표는 정리 직후의 SHA이며 engine에 문서 main을 merge하면 그 tip만 전진한다. 각 branch의 실제 작업공간 절대경로/dirty 수는 복구 manifest를 따른다.

| 브랜치 | SHA | 보존 이유 |
| --- | --- | --- |
| feat/loop-heartbeat | `3852b942c9ed` | 미커밋 보존 (6 tracked/0 untracked) |
| feature/architecture-doc-sync | `fe3403651fc0` | 고유 커밋 보존 — 재병합/폐기 승인 아님 |
| feature/codex-cf-preserve-20260915 | `2d55315daa27` | 고유 커밋 보존 — 재병합/폐기 승인 아님 |
| feature/codex-dart-schema-20260915 | `a7a12747e1bb` | 고유 커밋 보존 — 재병합/폐기 승인 아님 |
| feature/codex-evidence-contract-fixes | `2256105c1e36` | 고유 커밋 보존 — 재병합/폐기 승인 아님 |
| feature/codex-host-clock-compat | `515b9660aa88` | 고유 커밋 보존 — 재병합/폐기 승인 아님 |
| feature/codex-mcp-strategic-20260915 | `22fe17872313` | 고유 커밋 보존 — 재병합/폐기 승인 아님 |
| feature/codex-mcp-validator-20260915 | `9b651840cdc9` | 고유 커밋 보존 — 재병합/폐기 승인 아님 |
| feature/codex-policy-replay-fixes | `fb088c5801e2` | 고유 커밋 보존 — 재병합/폐기 승인 아님 |
| feature/codex-review-followups-20260915 | `9584df4cd59a` | 병합 완료지만 ignored 비캐시 증거 보존 |
| feature/codex-sync-open-boundary | `599440cde398` | 고유 커밋 보존 — 재병합/폐기 승인 아님 |
| feature/codex-time-contract-20260915 | `bcbd7910bfef` | 고유 커밋 보존 — 재병합/폐기 승인 아님 |
| feature/engine-safety-design-20260917 | `ab044c4edeb7` | 유일한 계속 개발 정본·C4 완료, 전체 운영 미승격 |
| feature/opus-review-runner-20260920 | `77c3f5d50162` | 미커밋 보존 (0 tracked/2 untracked) |
| feature/regime-morning-owner-20260920 | `da7484996904` | 미커밋 보존 (7 tracked/5 untracked) |
| feature/regime-morning-review-20260920 | `da7484996904` | 미커밋 보존 (7 tracked/6 untracked) |
| feature/regime-morning-tests-20260920 | `da7484996904` | 미커밋 보존 (7 tracked/4 untracked) |
| feature/regime-noon-replay-20260920 | `6109d1106c98` | 미커밋 보존 (8 tracked/6 untracked) |
| feature/regime-noon-replay-tests-20260920 | `6109d1106c98` | 미커밋 보존 (8 tracked/8 untracked) |
| feature/regime-noon-review-20260920 | `6109d1106c98` | 미커밋 보존 (8 tracked/10 untracked) |
| feature/regime-noon-scheduler-tests-20260920 | `6109d1106c98` | 미커밋 보존 (8 tracked/6 untracked) |
| feature/regime-owner-review-20260920 | `0fdd073a8b5f` | 미커밋 보존 (9 tracked/12 untracked) |
| feature/regime-owner-two-minute-20260920 | `0fdd073a8b5f` | 미커밋 보존 (9 tracked/9 untracked) |
| feature/regime-owner-two-minute-tests-20260920 | `0fdd073a8b5f` | 미커밋 보존 (10 tracked/10 untracked) |
| feature/retained-input-n1-20260920 | `77c3f5d50162` | 미커밋 보존 (5 tracked/11 untracked) |
| feature/review-canary-report | `35736f9d9c6e` | 미커밋 보존 (3 tracked/0 untracked) |
| feature/review-entry-risk-ledger | `d95f77b3ca97` | 미커밋 보존 (9 tracked/0 untracked) |
| feature/review-entry-risk-wiring | `761a0a256a34` | 미커밋 보존 (40 tracked/0 untracked) |
| feature/review-gate-parity | `c3dfbc1d5567` | 미커밋 보존 (15 tracked/0 untracked) |
| feature/review-heartbeat-status | `39d05417d1c3` | 미커밋 보존 (3 tracked/0 untracked) |
| feature/review-remediation-plan | `de111b75c9cc` | 미커밋 보존 (2 tracked/1 untracked) |
| feature/review-sync-safety | `7e63fcb03b72` | 미커밋 보존 (8 tracked/0 untracked) |
| feature/source-authority-impl-20260920 | `77c3f5d50162` | 미커밋 보존 (5 tracked/12 untracked) |
| feature/source-authority-review-20260920 | `dcd32d1e0386` | 미커밋 보존 (2 tracked/2 untracked) |
| feature/source-authority-tests-20260920 | `77c3f5d50162` | 미커밋 보존 (0 tracked/2 untracked) |
| feature/t9-brief-scope-and-eval | `5aead282fa85` | 미커밋 보존 (20 tracked/0 untracked) |
| feature/t9-data-freshness | `7d26280e8b32` | 미커밋 보존 (11 tracked/0 untracked) |
| feature/t9-intraday-regime-inputs | `4b2aad44f8c2` | 미커밋 보존 (26 tracked/0 untracked) |
| feature/toss-client-offline-20260916 | `8b5b0e09cb54` | 고유 커밋 보존 — 재병합/폐기 승인 아님 |
| feature/toss-market-offline-20260916 | `1e38b841e4ed` | 고유 커밋 보존 — 재병합/폐기 승인 아님 |
| feature/toss-observer-activation-20260917 | `27355af5a3d0` | 병합 완료지만 ignored 비캐시 증거 보존 |
| feature/toss-observer-installer-20260917 | `696b91ca61ff` | 고유 커밋 보존 — 재병합/폐기 승인 아님 |
| feature/toss-observer-launcher-20260917 | `0decfbfa52d1` | 고유 커밋 보존 — 재병합/폐기 승인 아님 |
| feature/toss-observer-runtime-20260917 | `f5c60a731d23` | 고유 커밋 보존 — 재병합/폐기 승인 아님 |
| feature/toss-phase1-offline-20260916 | `871fb7f0808b` | 병합 완료지만 ignored 비캐시 증거 보존 |
| feature/toss-runtime-auth-20260916 | `34e9659b76a1` | 고유 커밋 보존 — 재병합/폐기 승인 아님 |
| feature/toss-runtime-budget-20260916 | `d4e2b03c310d` | 고유 커밋 보존 — 재병합/폐기 승인 아님 |
| feature/toss-runtime-design-20260916 | `5a834ffaa7e3` | 병합 완료지만 ignored 비캐시 증거 보존 |
| feature/toss-runtime-http-20260916 | `d5b3fc641958` | 고유 커밋 보존 — 재병합/폐기 승인 아님 |
| feature/toss-runtime-ledger-20260916 | `d396bff34885` | 고유 커밋 보존 — 재병합/폐기 승인 아님 |
| feature/toss-shadow-offline-20260916 | `faa0785d3f00` | 고유 커밋 보존 — 재병합/폐기 승인 아님 |
| feature/toss-token-offline-20260916 | `d4d9de230ac0` | 고유 커밋 보존 — 재병합/폐기 승인 아님 |
| fix/codex-sandbox-apparmor | `06190090c548` | 병합 완료지만 ignored 비캐시 증거 보존 |
| research/exit-policy-ab | `36541e58f3ed` | 미커밋 보존 (11 tracked/0 untracked) |
| test/exit-manager-characterization | `6dd85ee25067` | 미커밋 보존 (5 tracked/0 untracked) |

후속 개발의 순서·실행 명령·모델/리뷰 규칙·운영 금지는 [Claude 인계 문서](../operations/claude-migration-handoff-2026-09-20.md)에 모았다.
