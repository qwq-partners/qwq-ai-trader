# WSL2 로컬 개발환경

이 문서는 Windows의 WSL2 Ubuntu에서 Claude Code와 Codex로 QWQ AI Trader를 개발하고 PR까지 올리는 절차를 설명한다. 로컬 환경은 테스트, 백테스트, 명시적으로 선택한 dry-run 전용이다. 실거래 주문, 운영 서버 재시작, 배포는 이 흐름에 포함하지 않는다.

## 1. 작업 위치

Windows 드라이브(`/mnt/c`)가 아니라 WSL의 Linux 파일시스템을 사용한다.

```bash
mkdir -p ~/projects
cd ~/projects
git clone git@github.com:qwq-partners/qwq-ai-trader.git
cd ~/projects/qwq-ai-trader
git remote -v
```

기존 작업본이 있다면 새로 복제하지 말고 상태부터 확인한다.

```bash
cd ~/projects/qwq-ai-trader
git status --short --branch
```

운영 서버의 `.env`, `logs/`, `~/.cache/ai_trader/`, PID 또는 거래 상태 파일을 로컬로 복사하지 않는다.

## 2. 개발 도구 준비

다음 명령은 Git, Python 3.11 이상, Node.js, npm, GitHub CLI, Claude Code, Codex가 WSL 내부에서 실제로 실행되는지 검사한다. 기존 `venv`와 `.env`는 덮어쓰지 않는다.

```bash
cd ~/projects/qwq-ai-trader
bash scripts/dev/bootstrap.sh
```

현재 상태만 확인하고 패키지를 설치하지 않으려면 다음과 같이 실행한다.

```bash
QWQ_BOOTSTRAP_CHECK_ONLY=1 bash scripts/dev/bootstrap.sh
```

### Codex가 Windows 경로를 가리킬 때

`codex --version`이 WSL에서 실행되지 않으면 Windows 앱의 경로가 먼저 잡혔을 수 있다. [공식 OpenAI Codex CLI 문서](https://learn.chatgpt.com/docs/codex/cli)에 따라 WSL 터미널 안에서 Linux용 CLI를 설치한 뒤 새 셸을 연다.

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
hash -r
codex --version
```

설치 스크립트를 검토해야 하는 환경에서는 먼저 파일로 내려받아 내용을 확인한 뒤 실행한다. 부트스트랩은 이 전역 설치를 자동으로 수행하지 않는다.

## 3. 인증 상태 확인

비밀값 자체를 출력하지 말고 사용 가능 여부만 확인한다.

```bash
gh auth status
claude --version
codex --version
```

Claude Code와 Codex의 최초 로그인은 각 명령을 직접 실행해 대화형 안내를 따른다.

```bash
claude
codex
```

## 4. 환경변수

단위 테스트와 문법 검사는 `.env` 없이 실행되어야 한다. 조회용 외부 API가 필요한 작업에서만 예제 파일을 복사하고 필요한 빈칸만 채운다.

```bash
cp .env.example .env
```

`.env.example`의 `KIS_ENV=dev`는 안전한 기본 방향을 나타낼 뿐 주문 실행을 허가하지 않는다. 운영 자격증명 전체를 로컬로 복사하지 않는다.

## 5. 작업 브랜치

`main`에서 직접 개발하지 않는다.

```bash
git switch main
git pull --ff-only origin main
git switch -c feature/<작업명>
```

Claude Code와 Codex는 세션 시작 시 `CLAUDE.md`, `AGENTS.md`, `CHANGELOG.md`, `docs/README.md`와 작업 분야 문서를 먼저 읽는다. 한 에이전트가 구현한 변경은 다른 에이전트가 최종 검토하도록 역할을 분리한다.

## 6. 로컬 검증

다음 한 명령은 Git이 추적하는 Python 파일의 문법, `tests/` 테스트 모음, 고확률 비밀정보 패턴을 순서대로 검사한다.

```bash
bash scripts/dev/verify.sh
```

이 명령은 `.env`를 읽지 않으며 SSH, `systemctl`, `scripts/run_trader.py`, `scripts/liquidate_all.py`를 호출하지 않는다. 실패하면 원인을 수정하고 같은 명령을 다시 실행한다.

## 7. Push와 PR

검증이 통과한 뒤 feature 브랜치만 push하고 PR을 만든다.

```bash
git status --short
git push -u origin HEAD
gh pr create --fill
```

이 단계에서 `main` 병합은 운영 서버 배포나 재시작을 자동으로 유발하지 않는다.

## 8. Dry-run

dry-run도 외부 조회 API를 사용할 수 있으므로 사용자가 현재 작업에서 명시적으로 선택한 경우에만 수행한다. 반드시 `--dry-run`을 포함하고 실행 전 시장 인자를 확인한다.

```bash
source venv/bin/activate
python scripts/run_trader.py --market kr --dry-run
```

`--dry-run`이 없는 `run_trader.py`, `liquidate_all.py`, 운영 서버 SSH, `systemctl` 명령은 이 로컬 개발 절차에서 실행하지 않는다.

## 문제 해결

- `venv`가 손상되었다면 자동 삭제하지 말고 먼저 이름을 바꾸어 보존한 뒤 부트스트랩을 다시 실행한다.
- 전체 저장소에서 `pytest`만 실행하면 `scripts/test_new_tr.py`의 수동 KIS 진단 함수가 수집될 수 있다. 표준 검증은 외부 API 진단을 제외하기 위해 `tests/`만 실행한다.
- 인증 오류가 나면 토큰 값을 화면에 출력하지 말고 `gh auth status`, Claude Code 또는 Codex의 로그인 흐름으로 갱신한다.
