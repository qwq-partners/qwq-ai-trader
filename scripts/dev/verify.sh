#!/usr/bin/env bash
set -euo pipefail

CALLER_PWD=$PWD
DEFAULT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
ROOT=${QWQ_VERIFY_ROOT:-$DEFAULT_ROOT}
[[ $ROOT = /* ]] || ROOT="$CALLER_PWD/$ROOT"
if [[ -d "$ROOT" ]]; then
  ROOT=$(cd "$ROOT" && pwd -P)
fi
PYTHON_BIN=${QWQ_VERIFY_PYTHON:-$ROOT/venv/bin/python}
if [[ -n ${QWQ_VERIFY_PYTHON:-} && $PYTHON_BIN != /* ]]; then
  PYTHON_BIN="$CALLER_PWD/$PYTHON_BIN"
fi

fail() {
  printf '[실패] %s\n' "$1" >&2
  exit 1
}

[[ -d "$ROOT/.git" || -f "$ROOT/.git" ]] || fail "Git 작업 트리를 찾지 못했습니다: $ROOT"
[[ -x "$PYTHON_BIN" ]] || fail "실행 가능한 Python을 찾지 못했습니다: $PYTHON_BIN"
cd "$ROOT"

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
  # QWQ_VERIFY_TESTS: 로컬에서 일부만 돌릴 때의 경로(기본 tests 전체). CI·서버 배포는 기본값을 쓴다.
  local target=${QWQ_VERIFY_TESTS:-$ROOT/tests}
  [[ $target = /* ]] || target="$ROOT/$target"
  [[ -e "$target" ]] || fail "테스트 경로를 찾지 못했습니다: $target"
  local -a extra=()
  # QWQ_VERIFY_PARALLEL=auto|N: 로컬 전용 병렬 실행(pytest-xdist 필요). 서버 배포 검증은 메모리 때문에 직렬 유지.
  if [[ -n ${QWQ_VERIFY_PARALLEL:-} ]]; then
    "$PYTHON_BIN" -c 'import xdist' 2>/dev/null \
      || fail "QWQ_VERIFY_PARALLEL 에는 pytest-xdist 가 필요합니다: $PYTHON_BIN -m pip install pytest-xdist"
    [[ $QWQ_VERIFY_PARALLEL == auto || $QWQ_VERIFY_PARALLEL =~ ^[1-9][0-9]*$ ]] \
      || fail "QWQ_VERIFY_PARALLEL 은 auto 또는 양의 정수여야 합니다: $QWQ_VERIFY_PARALLEL"
    extra+=(-n "$QWQ_VERIFY_PARALLEL" --dist loadfile)
    printf '[검증] 테스트 (병렬 %s, 파일 단위 분배)\n' "$QWQ_VERIFY_PARALLEL"
  else
    printf '[검증] 테스트\n'
  fi
  "$PYTHON_BIN" -m pytest "$target" -q "${extra[@]}"
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
