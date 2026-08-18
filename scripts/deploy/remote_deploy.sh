#!/usr/bin/env bash
set -euo pipefail

REPO=${QWQ_DEPLOY_REPO:-/home/ubuntu/projects/qwq-ai-trader}
SERVICE=${QWQ_DEPLOY_SERVICE:-qwq-ai-trader.service}
HEALTH_URL=${QWQ_DEPLOY_HEALTH_URL:-http://127.0.0.1:8080/api/health}
LOCK_FILE=${QWQ_DEPLOY_LOCK_FILE:-/tmp/qwq-ai-trader-deploy.lock}
ATTEMPTS=${QWQ_DEPLOY_HEALTH_ATTEMPTS:-12}
INTERVAL=${QWQ_DEPLOY_HEALTH_INTERVAL:-5}
MODE=${1:-}
TARGET_SHA=${2:-}

fail() {
  printf '[실패] %s\n' "$1" >&2
  exit 1
}

[[ $MODE == --check || $MODE == --deploy ]] || fail "--check 또는 --deploy가 필요합니다."
[[ $TARGET_SHA =~ ^[0-9a-f]{40}$ ]] || fail "올바르지 않은 배포 SHA입니다."
[[ -d "$REPO/.git" ]] || fail "운영 Git 저장소를 찾지 못했습니다: $REPO"

exec 9>"$LOCK_FILE"
flock -n 9 || fail "다른 배포가 진행 중입니다."
cd "$REPO"

[[ -z $(git status --porcelain) ]] || fail "운영 작업 트리에 변경 사항이 있어 중단합니다."
git fetch --quiet origin main
REMOTE_SHA=$(git rev-parse origin/main)
[[ $TARGET_SHA == "$REMOTE_SHA" ]] || fail "요청 SHA가 현재 origin/main과 다릅니다."

if [[ $MODE == --check ]]; then
  CURRENT_SHA=$(git rev-parse HEAD)
  sudo -n systemctl is-active --quiet "$SERVICE" || fail "서비스가 실행 중이 아닙니다."
  curl --fail --silent --show-error --max-time 5 "$HEALTH_URL" >/dev/null || fail "상태 API가 응답하지 않습니다."
  printf '[완료] 배포 준비 상태가 정상입니다. 현재 %.12s, 대상 %.12s\n' "$CURRENT_SHA" "$TARGET_SHA"
  exit 0
fi

install_dependencies() {
  if [[ ${QWQ_DEPLOY_SKIP_INSTALL:-0} == 1 ]]; then
    return
  fi
  [[ -x venv/bin/python ]] || return 1
  venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
}

verify_target() {
  if [[ -n ${QWQ_DEPLOY_VERIFY_COMMAND:-} ]]; then
    bash -lc "$QWQ_DEPLOY_VERIFY_COMMAND"
  else
    QWQ_VERIFY_PYTHON="$REPO/venv/bin/python" bash scripts/dev/verify.sh
  fi
}

restart_and_wait() {
  sudo -n systemctl restart "$SERVICE"
  local attempt
  for ((attempt = 1; attempt <= ATTEMPTS; attempt++)); do
    if sudo -n systemctl is-active --quiet "$SERVICE" && \
       curl --fail --silent --show-error --max-time 5 "$HEALTH_URL" >/dev/null; then
      return 0
    fi
    sleep "$INTERVAL"
  done
  return 1
}

PREVIOUS_SHA=$(git rev-parse HEAD)

apply_target() {
  git reset --hard --quiet "$TARGET_SHA"
  install_dependencies
  verify_target
  restart_and_wait
}

rollback() {
  printf '[복구] 이전 커밋 %.12s로 롤백합니다.\n' "$PREVIOUS_SHA" >&2
  git reset --hard --quiet "$PREVIOUS_SHA"
  install_dependencies
  restart_and_wait
}

if (set -e; apply_target); then
  printf '[완료] 배포가 완료되었습니다: %.12s\n' "$TARGET_SHA"
  exit 0
fi

printf '[실패] 대상 배포 검증에 실패했습니다.\n' >&2
if (set -e; rollback); then
  printf '[복구] 롤백 완료: %.12s\n' "$PREVIOUS_SHA" >&2
  exit 1
fi

printf '[긴급] 자동 롤백도 실패했습니다. 서버를 즉시 점검하세요.\n' >&2
exit 2
