#!/usr/bin/env bash
# WSL(또는 임의의 개발 머신)에서 운영 서버(Lightsail) 배포를 시작하는 유일한 진입점.
# 서버 안의 정본 scripts/deploy/local_deploy.sh <SHA> 를 SSH 로 호출한다(권한 검사 → fetch →
# detached checkout → verify.sh → restart → /api/health 대기, 실패 시 자동 롤백, evolved_overrides 보존).
#
# 사용:
#   bash scripts/deploy/deploy_lightsail.sh                      # --check: 읽기 전용 사전 점검
#   bash scripts/deploy/deploy_lightsail.sh --deploy [SHA]       # 실제 배포 (기본 SHA = origin/main)
#   bash scripts/deploy/deploy_lightsail.sh --postcheck [since]  # 재시작 뒤 ops_check 재집계 (기본 "5 minutes ago")
#
# 경계: 사용자가 현재 요청에서 배포를 승인했을 때만 --deploy 를 실행한다. 장중(평일 09:00~15:30 KST)은
# QWQ_DEPLOY_ALLOW_MARKET_HOURS=1 없이는 거부한다. pending 주문이 있으면 중단한다. SSH 키·.env 는 로컬에만 둔다.
set -euo pipefail

DEFAULT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
ROOT=${QWQ_DEPLOY_ROOT:-$DEFAULT_ROOT}
SSH_HOST=${QWQ_DEPLOY_SSH_HOST:-ubuntu@52.79.96.24}
SSH_KEY=${QWQ_DEPLOY_SSH_KEY:-$HOME/.ssh/lightsail_qwq}
REMOTE_REPO=${QWQ_DEPLOY_REMOTE_REPO:-/home/ubuntu/projects/qwq-ai-trader}
MODE=--check
ARG=

fail() {
  printf '[실패] %s\n' "$1" >&2
  exit 1
}

usage() {
  printf '사용법: %s [--check | --deploy [SHA] | --postcheck [since]]\n' "${0##*/}"
}

if (($# > 2)); then
  usage >&2
  exit 2
fi
if (($# >= 1)); then
  case "$1" in
    --check|--deploy|--postcheck) MODE=$1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  ARG=${2:-}
fi

# 장중 재시작 금지(P0 예외는 QWQ_DEPLOY_ALLOW_MARKET_HOURS=1 로 명시). 시험용 QWQ_DEPLOY_NOW="YYYY-MM-DD HH:MM" 주입 허용.
market_hours() {
  local now=${QWQ_DEPLOY_NOW:-}
  local dow hm
  if [[ -n $now ]]; then
    dow=$(TZ=Asia/Seoul date -d "$now" +%u) && hm=$(TZ=Asia/Seoul date -d "$now" +%H%M) || fail "QWQ_DEPLOY_NOW 형식 오류: $now"
  else
    dow=$(TZ=Asia/Seoul date +%u); hm=$(TZ=Asia/Seoul date +%H%M)
  fi
  ((dow <= 5)) && ((10#$hm >= 900 && 10#$hm < 1530))
}

if [[ $MODE != --postcheck ]] && market_hours && [[ ${QWQ_DEPLOY_ALLOW_MARKET_HOURS:-0} != 1 ]]; then
  fail "장중(평일 09:00~15:30 KST)에는 배포/재시작을 하지 않습니다. P0 예외만 QWQ_DEPLOY_ALLOW_MARKET_HOURS=1 로 명시하세요."
fi

if [[ $MODE != --postcheck && ${QWQ_DEPLOY_SKIP_LOCAL_GIT_CHECKS:-0} != 1 ]]; then
  [[ -f "$SSH_KEY" ]] || fail "SSH 키를 찾지 못했습니다: $SSH_KEY"
  [[ -z $(git -C "$ROOT" status --porcelain) ]] || fail "로컬 작업 트리에 커밋되지 않은 변경이 있습니다."
  [[ $(git -C "$ROOT" branch --show-current) == main ]] || fail "main 브랜치에서만 배포할 수 있습니다."
  git -C "$ROOT" fetch --quiet origin main
  [[ $(git -C "$ROOT" rev-parse HEAD) == $(git -C "$ROOT" rev-parse origin/main) ]] || \
    fail "로컬 main과 origin/main이 일치하지 않습니다."
fi

SSH_ARGS=(-o BatchMode=yes -o ConnectTimeout=10)
if [[ -f "$SSH_KEY" ]]; then
  SSH_ARGS+=(-i "$SSH_KEY")
fi

remote() {
  ssh "${SSH_ARGS[@]}" "$SSH_HOST" "$@"
}

if [[ $MODE == --postcheck ]]; then
  since=${ARG:-5 minutes ago}
  printf '[점검] 재시작 뒤 운영 집계 (since %s)\n' "$since"
  remote "cd '$REMOTE_REPO' && journalctl -u qwq-ai-trader --since '$since' --no-pager -o cat | grep -E 'KIS API 연결 완료|엔진 시작' | head -5; bash scripts/dev/ops_check.sh '$since'"
  exit $?
fi

TARGET_SHA=${ARG:-${QWQ_DEPLOY_TARGET_SHA:-$(git -C "$ROOT" rev-parse origin/main)}}
[[ $TARGET_SHA =~ ^[0-9a-f]{40}$ ]] || fail "올바르지 않은 배포 SHA입니다(40자 전체 SHA 필요): $TARGET_SHA"

# 서버 읽기 전용 사전 점검: pending [] · 운영 트리(evolved_overrides 외 변경 없음) · 서비스 active · health · 현재 HEAD
PRECHECK='set -e; cd "$1"; pending=$(curl -fsS --max-time 5 http://127.0.0.1:8080/api/orders/pending); [[ $pending == "[]" ]] || { echo "[실패] pending 주문 있음: $pending" >&2; exit 1; }
dirty=$(git status --porcelain | grep -vxF " M config/evolved_overrides.yml" || true); [[ -z $dirty ]] || { echo "[실패] 운영 작업 트리 변경 있음 (evolved_overrides 외):" >&2; echo "$dirty" >&2; exit 1; }
systemctl is-active --quiet qwq-ai-trader || { echo "[실패] 서비스가 실행 중이 아닙니다." >&2; exit 1; }
curl -fsS --max-time 5 http://127.0.0.1:8080/api/health >/dev/null || { echo "[실패] 상태 API 응답 없음" >&2; exit 1; }
printf "[확인] 운영 HEAD %.12s · pending [] · 서비스 active · health OK\n" "$(git rev-parse HEAD)"'

if [[ $MODE == --check ]]; then
  printf '[점검] 읽기 전용으로 Lightsail 배포 준비 상태를 확인합니다: 대상 %.12s\n' "$TARGET_SHA"
  remote bash -s -- "$REMOTE_REPO" <<<"$PRECHECK"
  exit 0
fi

printf '[배포] Lightsail 에 고정 SHA 를 배포합니다: %.12s (서버의 local_deploy.sh 가 검증·재시작·롤백을 담당)\n' "$TARGET_SHA"
remote bash -s -- "$REMOTE_REPO" <<<"$PRECHECK"
# 종료 코드는 local_deploy.sh 그대로: 0 완료 / 1 검증·기동 실패 후 롤백 완료 / 2 롤백 실패(즉시 점검)
remote "cd '$REMOTE_REPO' && bash scripts/deploy/local_deploy.sh '$TARGET_SHA'"
