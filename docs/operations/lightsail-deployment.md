# Lightsail 운영 배포 — WSL에서 실행하는 명세 (Claude Code·Codex 공통)

> 2026-10-31 사용자 지시: 앞으로 Codex든 Claude든 배포 작업을 사용자 WSL에서 할 수 있어야 한다. 이 문서가 그 절차의 정본이다.
> 서버 안에서 직접 할 때의 스킬(`/deploy-local`, `/pr-merge`, `/ops-check`)과 같은 스크립트를 쓰므로 결과와 영수증 형식은 동일하다.

## 0. 원칙

- **배포는 사용자가 현재 요청에서 명시 승인했을 때만** 실행한다. "병합해"는 배포 승인이 아니다. 승인 문구를 원장에 그대로 인용한다.
- **장외 창만** 사용한다: 평일 09:00~15:30 KST에는 스크립트가 거부한다(P0 예외만 `QWQ_DEPLOY_ALLOW_MARKET_HOURS=1`, 사용자 승인 필요).
- **고정 SHA**를 배포한다. 기본은 `origin/main`의 40자 SHA이며, 병합 뒤 main이 또 움직였으면 그 SHA가 무엇을 포함하는지 확인하고 보고한다(다른 PR이 함께 나간다).
- 서버의 정본 `scripts/deploy/local_deploy.sh`가 전체 검증(`verify.sh`, CI와 동일) → 재시작 → `/api/health` 대기 → 실패 시 자동 롤백을 담당한다. WSL 쪽 실행기는 그것을 SSH로 부를 뿐이며 운영 트리를 직접 고치지 않는다.
- 운영 checkout의 `config/evolved_overrides.yml` 미커밋 수정은 봇 진화 시스템 것이라 보존한다. 그 외 변경이 있으면 중단한다.
- WSL에는 SSH 키만 있으면 된다. `.env`, KIS 자격, 운영 캐시, 텔레그램 토큰을 WSL로 가져오지 않는다. 배포 중 주문·매수 재개·킬스위치·설정 변경은 하지 않는다.
- 배포 완료는 "스크립트 exit 0"이 아니라 **기동 후 인수 점검까지**다(아래 5단계).

## 1. 준비 (한 번)

```bash
git clone git@github.com:qwq-partners/qwq-ai-trader.git ~/projects/qwq-ai-trader && cd ~/projects/qwq-ai-trader
```

```bash
QWQ_BOOTSTRAP_REQUIRED_TOOLS="git gh" QWQ_BOOTSTRAP_PARALLEL=1 bash scripts/dev/bootstrap.sh
```

- SSH 키 `~/.ssh/lightsail_qwq`(권한 600), 호스트 기본값 `ubuntu@52.79.96.24`. 다른 값은 `QWQ_DEPLOY_SSH_KEY`, `QWQ_DEPLOY_SSH_HOST`로 재정의한다. 비밀번호 로그인은 꺼져 있다.
- `gh auth status`가 통과해야 PR 병합이 가능하다(fine-grained PAT, Contents/Pull requests RW).
- 저장소는 WSL 내부 디스크(`~/` 아래)에 둔다. `/mnt/c`는 검증이 수 배 느리다.

## 2. 코드 변경 → 로컬 검증 → PR

```bash
git switch -c feature/<작업명> origin/main
```

구현·테스트·문서(CHANGELOG, docs, CLAUDE.md)를 함께 바꾼 뒤 **푸시 전에** CI와 동일한 검증을 로컬에서 통과시킨다.

```bash
QWQ_VERIFY_PARALLEL=auto bash scripts/dev/verify.sh
```

```bash
git push -u origin HEAD && gh pr create --base main --head "$(git branch --show-current)" --title "<type>: <요약>" --body "<변경·검증·배포 여부>"
```

PR 본문에 "배포 대상 여부"를 적는다. 독립 리뷰가 필요한 변경(돈 경로·동시성·공개 페이지·LLM 호출)은 병합 전에 받는다.

## 3. CI 통과 → 병합

```bash
gh pr checks <n> --watch
```

```bash
gh pr merge <n> --merge
```

병합 뒤 `main`이 다른 PR로 더 움직였으면 `git log --oneline <내 머지>..origin/main`으로 함께 나갈 변경을 확인하고 사용자에게 알린다.

## 4. 배포 (사용자 승인 뒤)

```bash
git switch main && git pull --ff-only origin main
```

```bash
bash scripts/deploy/deploy_lightsail.sh
```

읽기 전용 점검이다. 서버의 pending 주문 `[]`, 운영 트리(evolved_overrides 외 변경 없음), 서비스 active, `/api/health`, 현재 HEAD를 출력한다. 실패 항목이 있으면 배포하지 않고 보고한다.

```bash
bash scripts/deploy/deploy_lightsail.sh --deploy
```

- 기본 대상은 `origin/main`. 특정 SHA면 `--deploy <40자 SHA>`.
- 서버에서 `local_deploy.sh`가 돈다: 권한 검사 → fetch → detached checkout → 전체 `verify.sh`(서버에서 14~19분) → `systemctl restart` → health 12×5초. 실패하면 이전 SHA로 자동 롤백.
- 종료 코드: `0` 완료 / `1` 검증·기동 실패 후 롤백 완료(`[복구]`) / `2` 롤백 실패(`[긴급]`, 즉시 서버 점검). `1`은 사전 점검 실패에도 쓰이므로 `[복구]` 표식과 실제 HEAD/PID를 함께 본다.
- 재시작 전후로 배포 전 지문(보호 파일·crontab·서비스 PID·킬스위치)을 남긴다. WSL에서는 `ssh … 'sha256sum <보호 파일…>; crontab -l | sha256sum'`을 전후로 떠서 같은 값인지 비교한다. 서버 안의 기존 절차는 `~/.local/state/qwq-deploy/<작업>/`에 JSON으로 보존돼 있다.

## 5. 인수 점검 (재시작 150초 뒤)

```bash
bash scripts/deploy/deploy_lightsail.sh --postcheck "<재시작 시각 HH:MM>"
```

기동 로그의 `KIS API 연결 완료`·`엔진 시작` 줄과 `ops_check.sh` 집계(ERROR/Traceback 0, KIS 거절 0, 포트폴리오)를 본다. 추가로 확인할 것:

- 서비스 `active/running`, 새 PID, `NRestarts=0`, `/api/health`의 `broker.connected=true`, `execution_recovery.status=ready`, pending `[]`.
- 보호 파일·crontab 지문이 배포 전과 같다. 킬스위치(`KILL_SWITCH_KR`)가 그대로 있다.
- 운영 checkout HEAD가 대상 SHA다(detached). 문서 후속만을 위한 재배포는 하지 않는다.

## 6. 기록

`docs/operations/execution-roadmap.md`의 해당 작업 절에 **운영 인수 영수증**(시각, 고정 SHA, PID, 검사 수, 기동 초, 보호 파일 수, 보존 항목, 미확인 항목)을 쓰고 CHANGELOG·CLAUDE.md 머리글을 갱신한 뒤 별도 PR로 병합한다. 문서만의 변경은 재배포하지 않는다.

## 7. 실패·예외

- 사전 점검 실패(pending 있음, 트리 변경, 서비스 비정상): 배포하지 않고 원인을 보고한다. 트리 변경을 임의로 되돌리지 않는다.
- 검증 실패 롤백(`exit 1`): 롤백 HEAD/PID·복구 상태·미체결·보호 설정을 확인하고 실패 영수증을 보존한다. 테스트를 skip하거나 맹목 재시도로 우회하지 않는다. 원인 수정 → 새 PR → 새 고정 SHA.
- `[긴급]`(`exit 2`): 서버에 SSH로 들어가 `systemctl status qwq-ai-trader`, `journalctl -u qwq-ai-trader -n 100 --no-pager`, `git -C /home/ubuntu/projects/qwq-ai-trader rev-parse HEAD`를 확인하고 사용자에게 즉시 보고한다.
- 다른 배포가 진행 중(`다른 배포 진행 중` 잠금 메시지): 기다리지 말고 보고한다. 두 세션이 동시에 배포하지 않는다.
- SSH 실패: 키 경로·권한(600)·호스트를 확인한다. 서버 쪽 `sudo -n systemctl restart` 권한이 없으면 스크립트가 재시작 전에 멈춘다.

## 8. 역할

- **Claude Code / Codex(에이전트)**: 1~3단계는 스스로 진행한다. 4단계 `--deploy`와 7단계의 서버 개입은 사용자 승인 문구가 현재 요청에 있을 때만 실행하고, 승인·시각·SHA·결과를 원장에 남긴다. 다른 에이전트의 배포 실행기 상태 디렉터리나 cron을 건드리지 않는다.
- **사용자**: 배포 승인, 장중 예외 승인, `[긴급]` 시 최종 판단.

## 참고

- 서버 내부 직접 배포: `bash scripts/deploy/local_deploy.sh <SHA>`(이 실행기가 SSH로 부르는 것과 같다). 운영 checkout을 미리 새 SHA로 당기면 롤백 기준을 잃으므로 피한다. 이 스크립트는 의존성 설치를 하지 않으므로 `requirements.txt`가 바뀐 배포는 먼저 서버에서 `venv/bin/pip install -r requirements.txt`를 별도 승인으로 실행한다.
- `verify.sh`는 검증 대상 Git 작업 트리를 작업 경로로 쓴다. cron·다른 디렉터리에서 절대 경로로 불러도 홈 디렉터리를 수집하지 않는다(2026-10-06 실패 교훈).
- 과거의 `remote_deploy.sh`(stdin 전송·`git reset --hard`·매번 pip 설치)는 evolved_overrides 보존 정책과 충돌해 2026-10-31에 삭제했다.
