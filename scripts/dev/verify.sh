#!/usr/bin/env bash
set -euo pipefail

DEFAULT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
ROOT=${QWQ_VERIFY_ROOT:-$DEFAULT_ROOT}
PYTHON_BIN=${QWQ_VERIFY_PYTHON:-$ROOT/venv/bin/python}

fail() {
  printf '[실패] %s\n' "$1" >&2
  exit 1
}

[[ -d "$ROOT/.git" || -f "$ROOT/.git" ]] || fail "Git 작업 트리를 찾지 못했습니다: $ROOT"
[[ -x "$PYTHON_BIN" ]] || fail "실행 가능한 Python을 찾지 못했습니다: $PYTHON_BIN"

check_python_syntax() {
  printf '[검증] Python 문법 검사\n'
  local -a tracked_files=()
  local -a python_files=()
  mapfile -d '' tracked_files < <(git -C "$ROOT" ls-files -z -- '*.py')
  local path
  for path in "${tracked_files[@]}"; do
    python_files+=("$ROOT/$path")
  done
  if ((${#python_files[@]} > 0)); then
    "$PYTHON_BIN" -m py_compile "${python_files[@]}"
  fi
}

run_tests() {
  if [[ ${QWQ_VERIFY_SKIP_TESTS:-0} == 1 ]]; then
    printf '[건너뜀] 테스트 (격리된 스크립트 테스트 전용 설정)\n'
    return
  fi
  printf '[검증] 테스트\n'
  [[ -d "$ROOT/tests" ]] || fail "tests 디렉터리를 찾지 못했습니다."
  "$PYTHON_BIN" -m pytest "$ROOT/tests" -q
}

scan_secrets() {
  printf '[검증] 비밀정보 의심 패턴 검사\n'
  local pattern='-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36,}'
  if git -C "$ROOT" grep -lEI -e "$pattern" -- ':!docs/superpowers/plans/*'; then
    printf '[실패] 비밀정보 의심 패턴을 발견했습니다. 값은 커밋하지 말고 해당 파일을 확인하세요.\n'
    return 1
  fi
}

check_python_syntax
run_tests
scan_secrets
printf '[완료] 로컬 검증을 통과했습니다.\n'
