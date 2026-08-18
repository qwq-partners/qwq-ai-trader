# Local Development Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** WSL2의 `~/projects/qwq-ai-trader`에서 Claude Code와 Codex가 운영 환경을 건드리지 않고 재현 가능한 개발·검증 흐름을 사용할 수 있게 한다.

**Architecture:** 저장소 루트의 `AGENTS.md`가 Codex 규칙의 진입점이 되고, `scripts/dev/bootstrap.sh`와 `scripts/dev/verify.sh`가 각각 환경 준비와 로컬 검증의 단일 명령이 된다. 스크립트 로직은 `tests/dev/`의 pytest 테스트가 격리된 임시 디렉터리와 가짜 실행 파일을 사용해 검증하며, 운영 SSH·systemd·주문 경로는 호출하지 않는다.

**Tech Stack:** WSL2 Ubuntu, Bash, Python 3.11+, `venv`, pip, pytest, Git, GitHub CLI, Claude Code, Codex

**Spec:** `docs/superpowers/specs/2026-08-18-local-development-control-plane-design.md`

## Global Constraints

- 표준 로컬 환경은 Windows 위의 WSL2 Ubuntu다.
- 저장소 경로는 `~/projects/qwq-ai-trader`다. `/mnt/c` 아래에는 별도 작업본을 만들지 않는다.
- Python 3.11 이상을 지원하며 현재 WSL의 Python 3.12를 사용한다.
- 로컬 환경은 실거래 주문을 실행하지 않는다.
- 운영 `.env`, 로그, 캐시, PID 및 거래 상태 파일을 로컬로 복사하지 않는다.
- 모든 변경은 `feature/*` 작업 브랜치에서 수행하고 PR을 통해 `main`에 병합한다.
- `main` 병합은 이 단계에서 운영 배포를 유발하지 않는다.
- 기존 `.env`, `venv`, 로그, 캐시 파일을 삭제하거나 덮어쓰지 않는다.
- 스크립트와 테스트는 운영 SSH, `systemctl`, 주문 스크립트를 실행하지 않는다.

---

## File Map

- Create `AGENTS.md`: Codex의 프로젝트 진입 규칙과 로컬 안전 경계.
- Create `scripts/dev/bootstrap.sh`: 도구 검사, Python 버전 검사, `venv` 생성 및 의존성 설치.
- Create `scripts/dev/verify.sh`: Python 문법, pytest, 고확률 비밀 패턴 검사.
- Create `tests/dev/test_bootstrap.py`: 부트스트랩의 버전·누락 도구·기존 환경 보존 테스트.
- Create `tests/dev/test_verify.py`: 검증 단계의 성공·문법 실패·테스트 실패·비밀 감지 테스트.
- Create `.env.example`: 값이 비어 있는 로컬 설정 템플릿과 안전 모드 설명.
- Create `docs/operations/local-development.md`: WSL 복제부터 PR까지의 운영 절차.
- Modify `docs/README.md`: 로컬 개발 문서를 문서 인덱스에 연결.
- Modify `CHANGELOG.md`: 로컬 개발 Control Plane 추가 기록.

### Task 1: Codex Project Guardrails

**Files:**
- Create: `AGENTS.md`
- Test: `tests/dev/test_agents_policy.py`

**Interfaces:**
- Consumes: 기존 `CLAUDE.md`, `CHANGELOG.md`, `docs/README.md` 규칙.
- Produces: Codex가 읽을 루트 지침 파일 `AGENTS.md`.

- [ ] **Step 1: Write the failing policy test**

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_agents_policy_contains_required_boundaries():
    policy = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for required in (
        "CLAUDE.md",
        "CHANGELOG.md",
        "docs/README.md",
        "실거래 주문",
        "운영 서버",
        "systemctl",
        "배포",
        "테스트",
    ):
        assert required in policy
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `venv/bin/python -m pytest tests/dev/test_agents_policy.py -v`

Expected: FAIL because `AGENTS.md` does not exist.

- [ ] **Step 3: Add the minimal root policy**

Create `AGENTS.md` with these exact sections:

```markdown
# QWQ AI Trader — Codex 작업 규칙

## 세션 시작

작업 전에 `CLAUDE.md`, `CHANGELOG.md`, `docs/README.md`와 작업 분야의 문서를 읽는다. `CLAUDE.md`를 프로젝트 상세 규칙의 기준으로 삼고 이 문서는 Codex 전용 안전 경계만 보충한다.

## 로컬 안전 경계

- 사용자가 현재 요청에서 명시하지 않은 실거래 주문을 실행하지 않는다.
- 사용자가 현재 요청에서 명시하지 않은 운영 서버 SSH, `systemctl`, 재시작, 배포를 실행하지 않는다.
- 로컬 기본 검증에서는 외부 API와 운영 자격증명을 사용하지 않는다.
- `.env`, 로그, 캐시, PID, 거래 상태 파일을 커밋하지 않는다.

## 변경과 검증

- `feature/*` 브랜치에서 작업하고 관련 테스트와 문서를 함께 갱신한다.
- 구현 후 관련 테스트, 전체 테스트, 비밀정보 검사를 수행한다.
- 구현자와 최종 리뷰어 역할을 가능하면 분리하고 동일 파일을 동시에 수정하지 않는다.
```

- [ ] **Step 4: Run the policy test**

Run: `venv/bin/python -m pytest tests/dev/test_agents_policy.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add AGENTS.md tests/dev/test_agents_policy.py
git commit -m "docs: Codex 로컬 안전 규칙 추가"
```

### Task 2: Idempotent WSL Bootstrap

**Files:**
- Create: `scripts/dev/bootstrap.sh`
- Create: `tests/dev/test_bootstrap.py`

**Interfaces:**
- Consumes: repository root, `requirements.txt`, optional environment variables `QWQ_BOOTSTRAP_CHECK_ONLY`, `QWQ_PYTHON_BIN`, `QWQ_VENV_DIR` used only for testability.
- Produces: executable `scripts/dev/bootstrap.sh`; exit 0 on a usable environment, non-zero with a Korean diagnostic on failure.

- [ ] **Step 1: Write failing tests for Python validation and environment preservation**

```python
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev/bootstrap.sh"


def run_bootstrap(tmp_path: Path, python_body: str):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("git", "node", "npm", "gh", "claude", "codex"):
        tool = fake_bin / name
        tool.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        tool.chmod(0o755)
    python = fake_bin / "python-under-test"
    python.write_text(python_body, encoding="utf-8")
    python.chmod(0o755)
    env = os.environ | {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "QWQ_BOOTSTRAP_CHECK_ONLY": "1",
        "QWQ_PYTHON_BIN": str(python),
        "QWQ_VENV_DIR": str(tmp_path / "venv"),
    }
    return subprocess.run(
        ["bash", str(SCRIPT)], cwd=ROOT, env=env, text=True,
        capture_output=True, check=False,
    )


def test_bootstrap_accepts_python_311_or_newer(tmp_path):
    result = run_bootstrap(
        tmp_path,
        "#!/usr/bin/env bash\n[[ $1 == --version ]] && echo 'Python 3.12.3'\n",
    )
    assert result.returncode == 0, result.stderr


def test_bootstrap_rejects_old_python(tmp_path):
    result = run_bootstrap(
        tmp_path,
        "#!/usr/bin/env bash\n[[ $1 == --version ]] && echo 'Python 3.10.14'\n",
    )
    assert result.returncode != 0
    assert "Python 3.11 이상" in result.stderr


def test_check_only_does_not_replace_existing_venv(tmp_path):
    marker = tmp_path / "venv" / "keep"
    marker.parent.mkdir()
    marker.write_text("preserve", encoding="utf-8")
    result = run_bootstrap(
        tmp_path,
        "#!/usr/bin/env bash\n[[ $1 == --version ]] && echo 'Python 3.12.3'\n",
    )
    assert result.returncode == 0
    assert marker.read_text(encoding="utf-8") == "preserve"
```

- [ ] **Step 2: Run the bootstrap tests and verify they fail**

Run: `venv/bin/python -m pytest tests/dev/test_bootstrap.py -v`

Expected: FAIL because `scripts/dev/bootstrap.sh` does not exist.

- [ ] **Step 3: Implement the bootstrap script**

Implement `scripts/dev/bootstrap.sh` with `set -euo pipefail`, repository-root discovery from the script location, Linux/WSL validation, required command checks, version parsing from `$QWQ_PYTHON_BIN --version`, and an early successful exit when `QWQ_BOOTSTRAP_CHECK_ONLY=1`.

The normal path must run these operations without overwriting existing files:

```bash
if [[ ! -d "$VENV_DIR" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$ROOT/requirements.txt"
```

Tool validation must execute `--version`, not only `command -v`, so a broken Windows PATH shim such as the current Codex entry is reported as unusable. The final message must print `scripts/dev/verify.sh` as the next command and must never print token or environment values.

- [ ] **Step 4: Run bootstrap tests**

Run: `venv/bin/python -m pytest tests/dev/test_bootstrap.py -v`

Expected: PASS.

- [ ] **Step 5: Run check-only against the real WSL environment**

Run: `QWQ_BOOTSTRAP_CHECK_ONLY=1 bash scripts/dev/bootstrap.sh`

Expected: either PASS, or a precise actionable failure identifying the broken Codex installation without changing `venv`.

- [ ] **Step 6: Commit**

```bash
git add scripts/dev/bootstrap.sh tests/dev/test_bootstrap.py
git commit -m "feat: WSL 개발환경 부트스트랩 추가"
```

### Task 3: Local Verification Gate

**Files:**
- Create: `scripts/dev/verify.sh`
- Create: `tests/dev/test_verify.py`

**Interfaces:**
- Consumes: repository root; Git-tracked files; `QWQ_VERIFY_ROOT`, `QWQ_VERIFY_PYTHON`, `QWQ_VERIFY_SKIP_TESTS` for isolated tests.
- Produces: executable `scripts/dev/verify.sh`; sequential syntax, pytest, and secret-scan stages with non-zero exit on any failure.

- [ ] **Step 1: Write failing verification tests**

```python
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev/verify.sh"


def run_verify(repo: Path):
    env = os.environ | {
        "QWQ_VERIFY_ROOT": str(repo),
        "QWQ_VERIFY_PYTHON": os.environ.get("PYTHON", "python3"),
        "QWQ_VERIFY_SKIP_TESTS": "1",
    }
    return subprocess.run(
        ["bash", str(SCRIPT)], cwd=ROOT, env=env, text=True,
        capture_output=True, check=False,
    )


def init_repo(path: Path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)


def track_all(path: Path):
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)


def test_verify_passes_clean_source(tmp_path):
    init_repo(tmp_path)
    (tmp_path / "ok.py").write_text("VALUE = 1\n", encoding="utf-8")
    track_all(tmp_path)
    assert run_verify(tmp_path).returncode == 0


def test_verify_rejects_python_syntax_error(tmp_path):
    init_repo(tmp_path)
    (tmp_path / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    track_all(tmp_path)
    result = run_verify(tmp_path)
    assert result.returncode != 0
    assert "문법 검사" in result.stdout


def test_verify_rejects_private_key(tmp_path):
    init_repo(tmp_path)
    (tmp_path / "leak.txt").write_text(
        "-----BEGIN PRIVATE KEY-----\nsecret\n", encoding="utf-8"
    )
    track_all(tmp_path)
    result = run_verify(tmp_path)
    assert result.returncode != 0
    assert "비밀정보" in result.stdout
```

- [ ] **Step 2: Run the verification tests and verify they fail**

Run: `venv/bin/python -m pytest tests/dev/test_verify.py -v`

Expected: FAIL because `scripts/dev/verify.sh` does not exist.

- [ ] **Step 3: Implement the verification script**

Implement three explicit functions and call them in order:

```bash
check_python_syntax() {
  mapfile -d '' files < <(git -C "$ROOT" ls-files -z '*.py')
  ((${#files[@]} == 0)) || "$PYTHON_BIN" -m py_compile "${files[@]/#/$ROOT/}"
}

run_tests() {
  [[ "${QWQ_VERIFY_SKIP_TESTS:-0}" == "1" ]] ||
    "$PYTHON_BIN" -m pytest "$ROOT/tests" -q
}

scan_secrets() {
  local pattern='-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----|AKIA[0-9A-Z]{16}|(api[_-]?key|token)[[:space:]]*=[[:space:]]*["'\'''][^"'\'']{16,}["'\'']'
  if git -C "$ROOT" grep -nEI "$pattern" -- ':!docs/superpowers/plans/*'; then
    printf '%s\n' '[실패] 비밀정보 의심 패턴을 발견했습니다.'
    return 1
  fi
}
```

Before these stages, require a Git worktree and an executable Python. Do not source `.env`. Never invoke SSH, `systemctl`, `run_trader.py`, or `liquidate_all.py`.

- [ ] **Step 4: Run isolated verification tests**

Run: `venv/bin/python -m pytest tests/dev/test_verify.py -v`

Expected: PASS.

- [ ] **Step 5: Run the real verification gate**

Run: `bash scripts/dev/verify.sh`

Expected: PASS after compiling tracked Python files, running all local tests, and scanning tracked files.

- [ ] **Step 6: Commit**

```bash
git add scripts/dev/verify.sh tests/dev/test_verify.py
git commit -m "feat: 로컬 검증 게이트 추가"
```

### Task 4: Safe Environment Template and Local Runbook

**Files:**
- Create: `.env.example`
- Create: `docs/operations/local-development.md`
- Modify: `docs/README.md`
- Test: `tests/dev/test_local_docs.py`

**Interfaces:**
- Consumes: `scripts/dev/bootstrap.sh`, `scripts/dev/verify.sh`, existing `CLAUDE.md` environment-variable list.
- Produces: secret-free environment template and complete local setup/runbook entry.

- [ ] **Step 1: Write failing documentation tests**

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_env_example_is_safe_and_complete():
    sample = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "KIS_ENV=dev" in sample
    assert "KIS_APPKEY=" in sample
    assert "KIS_APPSECRET=" in sample
    assert "OPENAI_API_KEY=" in sample
    assert "user123!" not in sample


def test_local_runbook_covers_complete_flow():
    guide = (ROOT / "docs/operations/local-development.md").read_text(encoding="utf-8")
    for required in (
        "~/projects/qwq-ai-trader",
        "scripts/dev/bootstrap.sh",
        "scripts/dev/verify.sh",
        "feature/",
        "--dry-run",
        "운영 서버",
    ):
        assert required in guide


def test_docs_index_links_local_runbook():
    index = (ROOT / "docs/README.md").read_text(encoding="utf-8")
    assert "operations/local-development.md" in index
```

- [ ] **Step 2: Run documentation tests and verify they fail**

Run: `venv/bin/python -m pytest tests/dev/test_local_docs.py -v`

Expected: FAIL because the template and local runbook do not exist.

- [ ] **Step 3: Create the secret-free `.env.example`**

Group variables by KIS, LLM, Telegram, and local behavior. Use empty values for every credential, set only `KIS_ENV=dev`, and add comments stating that the template does not authorize order execution and `.env` must never be committed.

- [ ] **Step 4: Write the local runbook and index entry**

Document exact commands for clone, `cd`, bootstrap, tool version checks, `gh auth status`, Claude/Codex start, `git switch -c feature/<name>`, verification, push, and PR creation. Put dry-run in a separate warning section requiring an explicit user decision and `--dry-run`; state that the guide never instructs the user to copy production `.env`, cache, or logs.

Add this exact list item under Operations in `docs/README.md`:

```markdown
- `operations/local-development.md` — WSL2 기반 Claude Code·Codex 로컬 개발환경 구성과 안전한 PR 흐름
```

- [ ] **Step 5: Run documentation tests**

Run: `venv/bin/python -m pytest tests/dev/test_local_docs.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add .env.example docs/operations/local-development.md docs/README.md tests/dev/test_local_docs.py
git commit -m "docs: WSL 로컬 개발 절차 추가"
```

### Task 5: Changelog and End-to-End Verification

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `docs/superpowers/plans/2026-08-18-local-development-control-plane.md` (check completed boxes only after each verified step)

**Interfaces:**
- Consumes: all deliverables from Tasks 1–4.
- Produces: auditable change record and a clean, pushed feature branch.

- [ ] **Step 1: Add the changelog entry**

At the top of the current changelog section, add a 2026-08-18 entry listing `AGENTS.md`, both development scripts, their tests, `.env.example`, and the local development runbook. State explicitly that deployment and production service configuration were not changed.

- [ ] **Step 2: Run focused tests**

Run: `venv/bin/python -m pytest tests/dev -v`

Expected: PASS for every development-control-plane test.

- [ ] **Step 3: Run the complete verification command**

Run: `bash scripts/dev/verify.sh`

Expected: PASS with successful syntax, pytest, and secret-scan stages.

- [ ] **Step 4: Inspect the final diff**

Run: `git diff --check && git status --short && git diff --stat origin/main...HEAD`

Expected: no whitespace errors; only the files named in this plan are changed.

- [ ] **Step 5: Commit and push**

```bash
git add CHANGELOG.md docs/superpowers/plans/2026-08-18-local-development-control-plane.md
git commit -m "docs: 로컬 개발환경 변경 이력 기록"
git push origin feature/local-dev-control-plane
```

- [ ] **Step 6: Prepare the PR**

Use this title:

```text
feat: WSL 로컬 개발 Control Plane 구축
```

The PR body must summarize the local-only safety boundary, list `bash scripts/dev/verify.sh` as verification evidence, and state that GitHub CI and Lightsail deployment are intentionally deferred to later projects.
