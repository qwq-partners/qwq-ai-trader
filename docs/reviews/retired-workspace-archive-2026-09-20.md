# 퇴역 작업공간 보관 전환 — 2026-09-20

## 결론

사용자의 “추가 확인 없이 독자적으로 정리” 지시에 따라, 1차 정리에서 보존했던 작업을 **폐기하지 않고 활성 목록 밖으로 보관 전환**했다. 최종 상시 개발선은 `main`과 `feature/engine-safety-design-20260917` 두 개다. 이 문서 PR의 임시 브랜치/작업공간은 병합 후 제거한다.

| 항목 | 추가 정리 전 | 보관 전환 | 상시 잔여 |
| --- | ---: | ---: | ---: |
| 로컬 branch heads | 56 | 54 → `refs/archive/2026-09-20/…` | 2 |
| 원격 branch heads | 9 | 7 → 같은 SHA의 archive tags | 2 |
| 등록 worktrees | 55 | 53 → 호스트 로컬 보호 보관소 | 2 |

53개에는 **dirty28개·staged12개**가 포함된다. 보관한 일반 파일33,070개(759,586,886 bytes), 디렉터리 모드, Git 인덱스와 미커밋/ignored 자료를 모두 유지했다. 파일 삭제에 의한 디스크 회수나 미완 구현의 main 병합이 아니다.

## Plan — 경계와 검토

- 시작 기준 main `4222f7442a63ded50dc2d96a3840a0f8acb041e6`, engine `792a4988c87049615bdda3689852f7b3acc0ac04`. 둘 다 clean, 열린 PR0. engine 제품 소스 기준은 계속 `ab044c4`다.
- coordinator가 실행하고 Sol/high가 inventory, Astra/high가 보존/복구 안전을 읽기 전용 독립 검토했다. 모델/effort는 요청값이며 native actual model metadata는 미노출이다. worker fan-out0.
- staged-only blob은 `bundle --all`로 보장되지 않는다는 검토 지적을 반영해 **전체 `.git` 독립 복사본**을 추가했다. 하드링크0, 객체/인덱스/메타데이터 파일 해시 대조.
- `git worktree prune`는 대상별 제한이 불가능하므로 사용하지 않았다. 대상53개의 worktree 디렉터리와 정확한 Git admin 디렉터리만 각각 rename했다.
- 이동 직전 프로세스 cwd/exe/fd/maps·Git lock·HEAD·전체 파일·인덱스를 재대조했다. 읽을 수 있는 프로세스의 대상 사용0. 권한상 읽지 못한 PID129개가 있어 모든 시스템 프로세스를 검사했다고 주장하지 않는다. 후보에 nested mount·symlink·비정규 파일은 없었다.

## Do — 보존과 정리

보호 보관소(mode700, 호스트 로컬):

`/home/ubuntu/projects/qwq-retired-workspaces-20260920.c2dhUU`

| 자료 | 용도 |
| --- | --- |
| `snapshot.json` | 원래 경로·branch·full SHA·파일 해시/모드·Git 상태·index/diff 해시·전체 Git 메타데이터 manifest |
| `all-refs.bundle` | 추가 정리 직전 refs의 전체 Git 이력, verify 통과 |
| `git-common.snapshot/` | 독립 `.git` 복사본. staged-only 객체까지 포함하며 원본과6532개 항목 대조 |
| `worktrees/<기존 디렉터리 이름>/` | 원본53개를 통째로 rename한 파일 자료; 미커밋·ignored 포함 |
| `retired-admins/<admin ID>/` | 원본53개의 Git worktree admin/index 자료 |
| `recovery-rehearsal/`, `rehearsal.json` | 세 대표 작업공간을 별도 복제한 실제 복구 시험/결과 |
| `move-journal.jsonl`, `archive-result.json` | 명시 경로 이동53건, 사후 전체 파일/Git 상태/admin 대조 |
| `remote-transaction.json`, `remote-result.json` | 정확한 기존 원격 SHA, 신규 tag, atomic 거래와 사후 재조회 |
| `PLAN.md`, `archive_workspaces.py` | 한정 작업 계획과 실행 기록용 도구. 제품 코드/운영 배포 대상 아님 |

bundle SHA256: `1b6c50811cb196ac6424f80cc341fc9c12f5d3cf8c2caf9bee7b0b4d7d54201d`.

보관소는 암호화된 원격 백업이 아니다. Git 설정·로컬 자료가 포함될 수 있으므로 통째 업로드/커밋하지 않는다. 이전 [1차 보관소](branch-consolidation-2026-09-20.md)의 bundle/SDD 백업도 유지한다.

- 로컬54개는 `update-ref` 단일 transaction으로 archive ref 생성과 기존 head 삭제를 수행했다. 정확한 old SHA를 검증하며 커밋과 도달 가능성은 유지한다. `branch -D`/reset/force worktree 제거0.
- 원격7개는 **새 태그 생성 + 기존 head 삭제를 한 atomic push**로 처리했다. 기존 head의 full SHA와 태그 부재를 lease로 검증했으며, 후속 조회에서 동일 SHA를 확인했다. 커밋 강제 덮어쓰기나 비공개 작업의 신규 게시가 아니다.
- 태그 접두사는 `archive/2026-09-20/feature/`. 대상 suffix는 `toss-observer-{installer,launcher,runtime}-20260917`, `toss-runtime-{auth,budget,http,ledger}-20260916`다. [1차 보고서](branch-consolidation-2026-09-20.md)의 같은 이름/SHA가 그대로 대응하며 full SHA는 `remote-result.json`을 참조한다.
- 이전 worker를 다시 병합하거나 병렬로 재구현하지 않는다. archive는 복구/근거 참조용이고 계속 개발은 canonical engine에서 한다.

## See — 복구와 검증

- 대표3개(`wf_6a23d1ba-7b5-1`, `regime-noon-review-20260920`, `toss-phase1-offline-20260916`)를 독립 복제하여 파일 manifest·staged/unstaged diff·index entries·status가 동일함을 확인했다. 복사본의 index 객체350/636/451개를 각각 읽을 수 있었다.
- 이동 후 **53/53 전체 파일/Git 상태 일치**, actual retired admin과 sealed snapshot 일치, 전체 common snapshot 불변을 확인했다. 사전 검사 후 `git add` 경쟁을 놓치지 않도록 대상별 직전/직후 actual admin을 추가 검증하고 불일치 시 해당 이동을 되돌리도록 했다.
- Sol/high 사후 독립 감사는 53개 전체 파일/state/admin, copied Git의 index 객체25,105개 readable/missing0, local54 refs·remote7 tags의 exact SHA, bundle 해시와 verify를 확인해 차단0으로 승인했다. Astra/high 문서 리뷰도 복구 열람 명령·기록/현황 구분·문서-only 범위를 직접 대조해 승인했다.
- 새 오프라인 main 전체 시험: **KST1810 passed/기존2 xfailed/기존warning1, 76.92초, exit0, 운영 상태·외부 네트워크 접근 시도0**. pytest 종료 후 과거 `/tmp/pytest-of-ubuntu/garbage-*` 정리 경고가 별도 발생했다. 프로젝트 실패로 숨기거나 경고0으로 보고하지 않는다. 해당 임시 경로는 이번 브랜치 정리 범위 밖이라 변경하지 않았다.
- 이 후속은 **문서만 변경**한다. engine 전체 suite4403/2는 직전 정리/통합의 기록이며 이번 추가 정리에서 재실행한 수치로 쓰지 않는다. required CI·독립 문서 리뷰·문서-only main/engine 동기화의 정확한 최종 SHA는 해당 PR 기록으로 확인한다.

## 복구할 때

**옮긴 디렉터리의 `.git` 파일은 원래 위치를 가리킨다. 그 안에서 평범하게 `git`을 실행하지 않는다.** `retired-admins`도 상대 `commondir`가 맞지 않으므로 그대로 `--git-dir`로 쓰지 않는다.

읽기 전용 열람은 snapshot의 admin과 보관 worktree를 명시한다. 예:

```bash
GIT_OPTIONAL_LOCKS=0 git \
  --git-dir=/home/ubuntu/projects/qwq-retired-workspaces-20260920.c2dhUU/git-common.snapshot/worktrees/wf_6a23d1ba-7b5-1 \
  --work-tree=/home/ubuntu/projects/qwq-retired-workspaces-20260920.c2dhUU/worktrees/wf_6a23d1ba-7b5-1 \
  status --short
```

snapshot에는 쓰지 않는다. 실제 개발 복원은 다음을 **선택한 한 대상에만** 적용한다.

1. `snapshot.json`에서 original path/admin ID/branch/full SHA를 확정하고, 원래 디렉터리·admin ID·branch가 모두 비어 있는지 확인한다. 사용 중이면 덮어쓰지 않고 별도 복원 경로를 설계한다.
2. `refs/archive/2026-09-20/<원래 branch>` 또는 bundle에서 원래 branch를 같은 SHA로 복원한다. 원래 head 대신 현재 main에 dirty 파일을 덮지 않는다.
3. snapshot의 objects에만 있는 객체를 원본 common Git에 **기존 파일을 덮지 않는 방식으로 복사**한다. staged-only 객체 때문에 이 단계를 생략하지 않는다. 또는 완전한 snapshot 복제본에서 독립 복구 환경을 만든다.
4. 선택한 `worktrees/<name>`과 `retired-admins/<id>`를 각각 원래 경로와 `.git/worktrees/<id>`로 되돌린다. 원래 `.git`/`gitdir`/`commondir` 연결과 staged 상태가 함께 복원된다.
5. branch SHA, status, index entries, staged/unstaged diff 및 파일 manifest를 snapshot과 대조한 뒤 개발을 재개한다. 중간 실패 복구는 journal과 **실제 양쪽 경로 존재 여부**를 함께 확인한다. 한정 실행 도구를 무작정 재실행하지 않는다.

## 운영 및 다음 세션

서비스 재시작·SSH·주문·설정·토큰/grant 변경0. 기존 거래 엔진 소스는 그대로이며 이번 문서 반영을 새로운 엔진 배포로 세지 않는다. KIS 거래/잔고·Toss 관측 전용, 관측 만료09/22 18:00 KST는 변함없다.

전체 engine의 `trading_ready=False`, 모든 MODIFY 미지원, 공식 startup/취소 최종성 증거 및 전체 C/F/G/R·독립 broad 인수 차단을 유지한다. Claude가 이어갈 정확한 순서는 [인계 문서](../operations/claude-migration-handoff-2026-09-20.md)의 B2/B3 request-bound qualification/최종 sizing → writer/factory → 공식 증거 → 전체 인수다.
