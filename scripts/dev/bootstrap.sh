#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
PYTHON_BIN=${QWQ_PYTHON_BIN:-python3}
VENV_DIR=${QWQ_VENV_DIR:-$ROOT/venv}
REQUIRED_TOOLS=(git node npm gh claude codex)

fail() {
  printf '[실패] %s\n' "$1" >&2
  exit 1
}

[[ $(uname -s) == Linux ]] || fail "WSL2 Ubuntu 또는 Linux에서 실행해 주세요."
[[ -f "$ROOT/requirements.txt" ]] || fail "저장소 루트에서 requirements.txt를 찾지 못했습니다."

for tool in "${REQUIRED_TOOLS[@]}"; do
  command -v "$tool" >/dev/null 2>&1 || fail "필수 도구 '$tool'을 찾지 못했습니다."
  "$tool" --version >/dev/null 2>&1 || fail "필수 도구 '$tool'을 실행할 수 없습니다. WSL 내부 설치를 확인해 주세요."
done

command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "Python 실행 파일 '$PYTHON_BIN'을 찾지 못했습니다."
python_version=$($PYTHON_BIN --version 2>&1)
if [[ ! $python_version =~ ^Python[[:space:]]+([0-9]+)\.([0-9]+) ]]; then
  fail "Python 버전을 확인할 수 없습니다: $python_version"
fi
python_major=${BASH_REMATCH[1]}
python_minor=${BASH_REMATCH[2]}
if ((python_major < 3 || (python_major == 3 && python_minor < 11))); then
  fail "Python 3.11 이상이 필요합니다. 현재 버전: $python_version"
fi

printf '[확인] 필수 도구와 %s을 확인했습니다.\n' "$python_version"

if [[ ${QWQ_BOOTSTRAP_CHECK_ONLY:-0} == 1 ]]; then
  printf '[완료] 점검 전용 모드에서는 파일을 변경하지 않았습니다.\n'
  exit 0
fi

if [[ ! -d "$VENV_DIR" ]]; then
  printf '[준비] 가상환경을 생성합니다: %s\n' "$VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
elif [[ ! -x "$VENV_DIR/bin/python" ]]; then
  fail "기존 가상환경에 실행 가능한 Python이 없습니다: $VENV_DIR"
else
  printf '[보존] 기존 가상환경을 사용합니다: %s\n' "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$ROOT/requirements.txt"

printf '[완료] 로컬 개발환경 준비가 끝났습니다.\n'
printf '다음 명령: bash scripts/dev/verify.sh\n'
