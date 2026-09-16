#!/usr/bin/env bash
set -euo pipefail

DEFAULT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
ROOT=${QWQ_REVIEW_ROOT:-$DEFAULT_ROOT}
CODEX_BIN=${QWQ_REVIEW_CODEX_BIN:-codex}
BASE_BRANCH=${QWQ_REVIEW_BASE:-main}
MODE=--branch
# 샌드박스 모드. 기본은 **비지정** — `~/.codex/config.toml` 의 sandbox_mode 를 그대로 따른다.
# 이 호스트(Ubuntu, kernel.apparmor_restrict_unprivileged_userns=1)에서는 Codex 번들 bwrap 이
# 사용자·네트워크 네임스페이스를 못 만들어(`bwrap: loopback: Failed RTM_NEWADDR`) 샌드박스를
# 켜는 순간 파일 읽기·git 이 전부 실패하고 리뷰가 "미검증"으로 끝난다(2026-08-02 우회로 config 에
# danger-full-access 를 뒀는데, 이 스크립트가 `--sandbox read-only` 를 하드코딩해 그걸 덮어쓰던
# 것이 2026-08~09 리뷰 연속 실패의 직접 원인 — 2026-09-16 확정).
# 2026-09-16 저녁: 운영 서버에 번들 bwrap 용 AppArmor 프로파일(scripts/ops/apparmor/)을 설치해 샌드박스가
# 다시 뜬다. 그래서 기본값은 **read-only 샌드박스**로 되돌린다. 프로파일이 없는 호스트에서는 아래 프로브가
# fail-fast 하므로, 그 호스트에서 config.toml 설정을 그대로 쓰려면 QWQ_REVIEW_SANDBOX= (빈 값) 으로 실행한다.
#   미설정 → read-only(프로브 후) / 빈 값 → 플래그 없음(config 따름) / 그 외 → 그 모드(프로브 후)
SANDBOX=${QWQ_REVIEW_SANDBOX-read-only}

fail() {
  printf '[실패] %s\n' "$1" >&2
  exit 1
}

usage() {
  printf '사용법: %s [--branch|--uncommitted]\n' "${0##*/}"
  printf '  --branch       현재 브랜치와 %s의 차이를 리뷰 (기본)\n' "$BASE_BRANCH"
  printf '  --uncommitted  커밋되지 않은 변경만 리뷰\n'
}

if (($# > 1)); then
  usage >&2
  exit 2
fi
if (($# == 1)); then
  case "$1" in
    --branch|--uncommitted) MODE=$1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
fi

[[ -d "$ROOT/.git" || -f "$ROOT/.git" ]] || fail "Git 작업 트리를 찾지 못했습니다: $ROOT"
command -v "$CODEX_BIN" >/dev/null 2>&1 || \
  fail "Codex CLI를 찾지 못했습니다: $CODEX_BIN (설치: docs/operations/local-development.md 2절)"

# codex exec review는 --base/--uncommitted와 커스텀 프롬프트를 함께 받지 못하므로
# (0.147.0 기준) 일반 exec + 읽기 전용 샌드박스에 diff 지시를 포함한 프롬프트를 쓴다.
if [[ $MODE == --branch ]]; then
  CURRENT_BRANCH=$(git -C "$ROOT" branch --show-current)
  [[ -n "$CURRENT_BRANCH" ]] || fail "브랜치를 확인할 수 없습니다 (detached HEAD)."
  [[ "$CURRENT_BRANCH" != "$BASE_BRANCH" ]] || \
    fail "현재 브랜치가 기준 브랜치($BASE_BRANCH)와 같습니다. feature 브랜치에서 실행해 주세요."
  git -C "$ROOT" rev-parse --verify --quiet "$BASE_BRANCH" >/dev/null || \
    fail "기준 브랜치를 찾지 못했습니다: $BASE_BRANCH"
  SCOPE_PROMPT="git diff $BASE_BRANCH...HEAD 와 git log $BASE_BRANCH..HEAD --oneline 으로 현재 브랜치의 변경을 파악한 뒤,"
else
  SCOPE_PROMPT="git status --porcelain 과 git diff HEAD (미추적 파일 포함)로 커밋되지 않은 변경을 파악한 뒤,"
fi

# CLAUDE.md 코드 리뷰 프로토콜과 동일한 출력 형식을 요구한다.
REVIEW_PROMPT="$SCOPE_PROMPT 리뷰어 관점에서 검토하라.
- **읽기 전용**: 파일 수정·생성·삭제, git 쓰기(commit/checkout/stash/reset), 패키지 설치, 네트워크 요청을 하지 말 것. 조회 명령(git diff/log/show, cat, grep, pytest 개별 파일)만 허용.
- 이슈는 P0(치명적)/P1(중요)/P2(경미)로 분류하고, 각 이슈에 파일명·라인번호·구체적 문제·수정방안을 제시하라.
- 트레이딩 로직 변경은 리스크 한도와 기존 전략 동작이 보존되는지 반드시 확인하라.
- CLAUDE.md의 \"절대 금지 패턴\"(0/0.0/\"\" falsy 처리, or 기본값)과 Decimal 정밀 계산 규칙 위반을 점검하라.
- 코드 변경에 상응하는 CHANGELOG.md 및 docs/ 갱신이 포함됐는지 확인하라.
- 결론에 병합 가능 여부를 명시하라. 답변은 한국어로 작성하라."

cd "$ROOT"   # 프로브와 본 실행을 같은 디렉터리·같은 조건에서 돌린다

# 리뷰 전후 작업 트리·HEAD 대조 — 프로브까지 포함해 Codex 가 무언가 바꿨으면 숨기지 않고 경고 + 비정상
# 종료한다. 샌드박스가 있어도 남기는 이중 안전장치이고, 없을 때는 "읽기 전용" 계약 위반의 유일한 탐지선이다.
before_head=$(git rev-parse HEAD 2>/dev/null || true)
before_status=$(git status --porcelain --untracked-files=all 2>/dev/null || true)

ARGS=(exec)
if [[ -n $SANDBOX ]]; then
  # 요청된 샌드박스가 이 호스트에서 실제로 뜨는지 **1회** 프로브 — 종료 코드와 출력을 따로 판정한다
  # (파이프라인에 넣으면 pipefail 때문에 오류 문자열을 잡고도 fail 분기를 건너뛴다).
  probe_rc=0
  probe_out=$("$CODEX_BIN" exec --sandbox "$SANDBOX" "run the shell command: echo QWQ_SANDBOX_PROBE" 2>&1) || probe_rc=$?
  if (( probe_rc != 0 )) || grep -q "bwrap: " <<<"$probe_out"; then
    fail "샌드박스($SANDBOX)가 이 호스트에서 기동하지 않습니다 (rc=$probe_rc, bwrap/userns 제한). QWQ_REVIEW_SANDBOX 를 비우고 config.toml 의 sandbox_mode 를 따르거나 환경을 고치세요."
  fi
  ARGS+=(--sandbox "$SANDBOX")
fi
ARGS+=("$REVIEW_PROMPT")

printf '[리뷰] Codex 교차 리뷰를 시작합니다 (모드: %s, 기준: %s, 샌드박스: %s)\n' "${MODE#--}" "$BASE_BRANCH" "${SANDBOX:-config.toml 설정}"
review_rc=0
"$CODEX_BIN" "${ARGS[@]}" || review_rc=$?

after_head=$(git rev-parse HEAD 2>/dev/null || true)
after_status=$(git status --porcelain --untracked-files=all 2>/dev/null || true)
if [[ "$before_head" != "$after_head" || "$before_status" != "$after_status" ]]; then
  printf '[경고] 리뷰 중 저장소가 변경됐습니다 — 읽기 전용 계약 위반. 아래 diff 를 확인하세요.\n' >&2
  diff <(printf '%s\n' "$before_status") <(printf '%s\n' "$after_status") >&2 || true
  [[ "$before_head" != "$after_head" ]] && printf '[경고] HEAD %s -> %s\n' "$before_head" "$after_head" >&2
  exit 3
fi
exit "$review_rc"
