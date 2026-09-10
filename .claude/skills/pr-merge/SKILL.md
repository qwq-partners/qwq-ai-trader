---
name: pr-merge
description: 브랜치 → PR 생성 → verify 체크 대기 → main 머지 → 운영 체크아웃 main 갱신. main은 보호 브랜치(직접 push 거부, PR + verify 필수). "PR 만들어", "머지해줘", 커밋 후 반영 요청에 사용.
---
# PR 생성·머지 (pr-merge)

인증: `gh`는 `~/.gh_token`(fine-grained PAT, Contents/Pull requests RW)으로 로그인돼 있음. 풀리면 `gh auth login --with-token < ~/.gh_token`. API 직접 호출은 `curl -H "Authorization: Bearer $(cat ~/.gh_token)"`.

1. 로컬 검증: `QWQ_VERIFY_PYTHON=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python bash scripts/dev/verify.sh`
2. 푸시: `git push -u origin <branch>`
3. PR: `gh pr create --base main --head <branch> --title "<type>: <요약>" --body "<변경·검증·배포 메모>"` (본문 한국어, 배포 여부 명시)
4. 체크 대기: `gh pr checks <n> --watch` (verify ≈ 1분). 실패 시 로컬 verify로 재현 후 수정 커밋
5. 머지: `gh pr merge <n> --merge` (기존 PR과 같은 merge commit 방식)
6. 운영 체크아웃 갱신: `git -C /home/ubuntu/projects/qwq-ai-trader pull --ff-only origin main` — 코드 변경이면 `/deploy-local`로 재시작, 문서만이면 끝
