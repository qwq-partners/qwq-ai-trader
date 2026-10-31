#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
PYTHON_BIN=${QWQ_PYTHON_BIN:-python3}
VENV_DIR=${QWQ_VENV_DIR:-$ROOT/venv}
# 검증만 할 머신(Codex/Claude 미설치)에서는 QWQ_BOOTSTRAP_REQUIRED_TOOLS="git" 처럼 줄여 실행한다.
read -r -a REQUIRED_TOOLS <<< "${QWQ_BOOTSTRAP_REQUIRED_TOOLS:-git node npm gh claude codex}"

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
if [[ ${QWQ_BOOTSTRAP_PARALLEL:-0} == 1 ]]; then
  # 로컬 전용: CI/서버와 같은 verify.sh 를 QWQ_VERIFY_PARALLEL=auto 로 돌리기 위한 추가 패키지.
  "$VENV_DIR/bin/python" -m pip install pytest-xdist
fi

# 실제 이미지 렌더 테스트는 한글 글꼴이 있어야 통과한다(CI 는 fonts-wqy-zenhei 를 설치한다).
font_ready() {
  local f
  for f in /usr/share/fonts/truetype/nanum/NanumGothic.ttf /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc \
           /usr/share/fonts/truetype/wqy/wqy-zenhei.ttc "$HOME/.local/share/fonts/NotoSansKR.ttf"; do
    [[ -f $f ]] && return 0
  done
  return 1
}
if font_ready; then
  printf '[확인] 한글 렌더 글꼴이 있습니다.\n'
elif [[ ${QWQ_BOOTSTRAP_SKIP_FONT:-0} == 1 ]]; then
  printf '[건너뜀] 한글 글꼴 설치 (이미지 테스트는 실패합니다)\n'
elif command -v apt-get >/dev/null 2>&1; then
  printf '[준비] 한글 렌더 글꼴을 설치합니다 (sudo apt-get install fonts-wqy-zenhei)\n'
  sudo apt-get update && sudo apt-get install --no-install-recommends -y fonts-wqy-zenhei
  font_ready || fail "글꼴 설치 후에도 한글 글꼴을 찾지 못했습니다."
else
  fail "한글 렌더 글꼴이 없습니다. NanumGothic/NotoSansCJK/wqy-zenhei 중 하나를 설치하거나 QWQ_BOOTSTRAP_SKIP_FONT=1 로 건너뛰세요."
fi

printf '[완료] 로컬 개발환경 준비가 끝났습니다.\n'
printf '다음 명령: bash scripts/dev/verify.sh   (CI 와 동일한 전체 검증)\n'
printf '병렬 실행: QWQ_VERIFY_PARALLEL=auto bash scripts/dev/verify.sh   (QWQ_BOOTSTRAP_PARALLEL=1 로 준비했을 때)\n'
