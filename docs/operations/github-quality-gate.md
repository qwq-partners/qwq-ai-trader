# GitHub 품질 관문

## 목적

`main`에 합치기 전에 로컬과 동일한 문법·테스트·비밀정보 검사를 자동 수행한다. 이 workflow는 배포하지 않으며 운영 자격증명, SSH, systemd, 주문 경로를 사용하지 않는다.

## 자동 검사

`.github/workflows/verify.yml`은 `main` 대상 PR, `main` push, 수동 실행에서 동작한다. 필수 check 이름은 `verify`다.

검사 순서는 다음과 같다.

1. 읽기 전용 checkout
2. Python 3.12와 pip cache 준비
3. `requirements.txt` 설치
4. `bash scripts/dev/verify.sh`

같은 PR에 새 커밋이 올라오면 이전 실행은 취소된다. 전체 제한 시간은 15분이다.

## 실패 확인과 재실행

PR의 **Checks** 또는 저장소의 **Actions → Verify**에서 실패 단계를 확인한다. 실패 원인을 로컬에서 재현한다.

```bash
cd ~/projects/qwq-ai-trader
git switch <feature-branch>
bash scripts/dev/verify.sh
```

수정 후 같은 feature 브랜치에 push하면 새 검사가 자동 실행된다. 실패한 검사를 우회해 병합하지 않는다.

## Main 브랜치 보호

GitHub의 **Settings → Branches → Add branch protection rule**에서 branch name pattern을 `main`으로 설정하고 다음 항목을 켠다.

- Require a pull request before merging
- Required approvals: `0`
- Require status checks to pass before merging
- Require branches to be up to date before merging
- Required status check: `verify`
- Require conversation resolution before merging
- Allow force pushes: off
- Allow deletions: off

`verify`는 workflow가 GitHub에서 한 번 실행된 뒤 필수 check 목록에 나타난다.

## API 검증

관리 권한이 있는 GitHub CLI로 현재 보호 상태를 확인할 수 있다.

```bash
gh api repos/qwq-partners/qwq-ai-trader/branches/main/protection
```

토큰 권한이 부족하면 403 응답이 발생할 수 있다. 이 경우 토큰을 출력하거나 새 토큰을 코드에 넣지 말고 GitHub UI에서 위 설정을 적용한다.
