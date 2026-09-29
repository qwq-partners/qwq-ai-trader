#!/usr/bin/env bash
# 운영 서버(Lightsail) 안에서 직접 배포 — remote_deploy.sh 절차의 서버 측 미러 (2026-09-03~)
# 사용: bash scripts/deploy/local_deploy.sh <SHA>
#   권한 확인 → fetch → detached checkout → verify.sh → restart → /api/health 대기(12×5s)
#   실패 시 이전 커밋으로 자동 롤백. origin/main 이 아닌 SHA(핫픽스)도 허용 — 머지 후 main 복귀 필요.
#   장중(09:00~15:30) 사용 금지(P0 예외), pending 주문 있으면 호출 전 보류.
set -uo pipefail
REPO=${QWQ_DEPLOY_REPO:-/home/ubuntu/projects/qwq-ai-trader}
SERVICE=${QWQ_DEPLOY_SERVICE:-qwq-ai-trader.service}
HEALTH=${QWQ_DEPLOY_HEALTH_URL:-http://127.0.0.1:8080/api/health}
LOCK_FILE=${QWQ_DEPLOY_LOCK_FILE:-/tmp/qwq-ai-trader-deploy.lock}
TARGET=${1:-}
fail() { printf '[실패] %s\n' "$1" >&2; exit 1; }
[[ -n $TARGET ]] || fail "대상 SHA가 필요합니다"
exec 9>"$LOCK_FILE" || fail "배포 잠금 파일 열기 실패"
flock -n 9 || fail "다른 배포 진행 중"
# 목록 검사만 수행한다. 인증이나 권한 정책 변경 없이 실패하면 배포 전 중단한다.
sudo -n -l systemctl restart "$SERVICE" >/dev/null 2>&1 || fail "비대화형 서비스 재시작 권한 없음"
# config/evolved_overrides.yml 은 봇 진화 시스템이 직접 쓰는 파일이라 미커밋 수정을 허용·보존한다 (2026-09-30 사용자 결정).
# 다른 변경은 여전히 중단. 대상 커밋이 그 파일을 바꾸면 운영 수정과 충돌하므로 아래에서 중단한다.
KEEP=" M config/evolved_overrides.yml"
DIRTY=$(git -C "$REPO" status --porcelain)
[[ -z $(grep -vxF "$KEEP" <<<"$DIRTY") ]] || fail "운영 작업 트리 변경 있음 (evolved_overrides 외)"
PREV=$(git -C "$REPO" rev-parse HEAD)
git -C "$REPO" fetch -q origin || fail "fetch 실패"
git -C "$REPO" cat-file -e "$TARGET^{commit}" 2>/dev/null || fail "대상 커밋 없음: $TARGET"
if [[ -n $DIRTY ]]; then
  git -C "$REPO" diff --quiet HEAD "$TARGET" -- config/evolved_overrides.yml \
    || fail "대상 커밋이 config/evolved_overrides.yml 을 바꾼다 — 운영 미커밋 수정과 충돌, 수동 처리"
fi
restart() { sudo -n systemctl restart "$SERVICE"; }
wait_healthy() { for _ in $(seq 1 12); do systemctl is-active --quiet "$SERVICE" && curl -fsS --max-time 5 "$HEALTH" >/dev/null 2>&1 && return 0; sleep 5; done; return 1; }
apply() { git -C "$REPO" checkout -q --detach "$TARGET" && QWQ_VERIFY_ROOT="$REPO" QWQ_VERIFY_PYTHON="$REPO/venv/bin/python" bash "$REPO/scripts/dev/verify.sh" && restart && wait_healthy; }
if apply; then printf '[완료] 배포 %.12s (이전 %.12s)\n' "$TARGET" "$PREV"; exit 0; fi
printf '[실패] 검증/기동 실패 → %.12s 롤백\n' "$PREV" >&2
git -C "$REPO" checkout -q --detach "$PREV" && restart && wait_healthy && { printf '[복구] 롤백 완료\n' >&2; exit 1; }
printf '[긴급] 롤백 실패 — 즉시 점검\n' >&2; exit 2
