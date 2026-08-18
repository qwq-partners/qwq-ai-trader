#!/usr/bin/env bash
set -euo pipefail

DEFAULT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
ROOT=${QWQ_DEPLOY_ROOT:-$DEFAULT_ROOT}
REMOTE_SCRIPT="$ROOT/scripts/deploy/remote_deploy.sh"
SSH_HOST=${QWQ_DEPLOY_SSH_HOST:-ubuntu@52.79.96.24}
SSH_KEY=${QWQ_DEPLOY_SSH_KEY:-$HOME/.ssh/lightsail_qwq}
MODE=--check

fail() {
  printf '[실패] %s\n' "$1" >&2
  exit 1
}

usage() {
  printf '사용법: %s [--check|--deploy]\n' "${0##*/}"
}

if (($# > 1)); then
  usage >&2
  exit 2
fi
if (($# == 1)); then
  case "$1" in
    --check|--deploy) MODE=$1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
fi

[[ -f "$REMOTE_SCRIPT" ]] || fail "원격 배포 실행기를 찾지 못했습니다: $REMOTE_SCRIPT"

if [[ ${QWQ_DEPLOY_SKIP_LOCAL_GIT_CHECKS:-0} != 1 ]]; then
  [[ -f "$SSH_KEY" ]] || fail "SSH 키를 찾지 못했습니다: $SSH_KEY"
  [[ -z $(git -C "$ROOT" status --porcelain) ]] || fail "로컬 작업 트리에 커밋되지 않은 변경이 있습니다."
  [[ $(git -C "$ROOT" branch --show-current) == main ]] || fail "main 브랜치에서만 배포할 수 있습니다."
  git -C "$ROOT" fetch --quiet origin main
  [[ $(git -C "$ROOT" rev-parse HEAD) == $(git -C "$ROOT" rev-parse origin/main) ]] || \
    fail "로컬 main과 origin/main이 일치하지 않습니다."
fi

TARGET_SHA=${QWQ_DEPLOY_TARGET_SHA:-$(git -C "$ROOT" rev-parse origin/main)}
[[ $TARGET_SHA =~ ^[0-9a-f]{40}$ ]] || fail "올바르지 않은 배포 SHA입니다."

SSH_ARGS=(-o BatchMode=yes -o ConnectTimeout=10)
if [[ -f "$SSH_KEY" ]]; then
  SSH_ARGS+=(-i "$SSH_KEY")
fi

if [[ $MODE == --check ]]; then
  printf '[점검] 읽기 전용으로 Lightsail 배포 준비 상태를 확인합니다: %.12s\n' "$TARGET_SHA"
else
  printf '[배포] Lightsail에 origin/main 커밋을 배포합니다: %.12s\n' "$TARGET_SHA"
fi

ssh "${SSH_ARGS[@]}" "$SSH_HOST" bash -s -- "$MODE" "$TARGET_SHA" < "$REMOTE_SCRIPT"
