#!/usr/bin/env bash
set -euo pipefail

DEFAULT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
ROOT=${QWQ_REVIEW_ROOT:-$DEFAULT_ROOT}
CODEX_BIN=${QWQ_REVIEW_CODEX_BIN:-codex}
BASE_BRANCH=${QWQ_REVIEW_BASE:-main}
MODE=--branch

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

# CLAUDE.md 코드 리뷰 프로토콜과 동일한 출력 형식을 요구한다.
REVIEW_PROMPT='리뷰어 관점에서 변경 사항을 검토하라.
- 이슈는 P0(치명적)/P1(중요)/P2(경미)로 분류하고, 각 이슈에 파일명·라인번호·구체적 문제·수정방안을 제시하라.
- 트레이딩 로직 변경은 리스크 한도와 기존 전략 동작이 보존되는지 반드시 확인하라.
- CLAUDE.md의 "절대 금지 패턴"(0/0.0/"" falsy 처리, or 기본값)과 Decimal 정밀 계산 규칙 위반을 점검하라.
- 코드 변경에 상응하는 CHANGELOG.md 및 docs/ 갱신이 포함됐는지 확인하라.
- 결론에 병합 가능 여부를 명시하라. 답변은 한국어로 작성하라.'

ARGS=(exec --sandbox read-only review)
if [[ $MODE == --branch ]]; then
  CURRENT_BRANCH=$(git -C "$ROOT" branch --show-current)
  [[ -n "$CURRENT_BRANCH" ]] || fail "브랜치를 확인할 수 없습니다 (detached HEAD)."
  [[ "$CURRENT_BRANCH" != "$BASE_BRANCH" ]] || \
    fail "현재 브랜치가 기준 브랜치($BASE_BRANCH)와 같습니다. feature 브랜치에서 실행해 주세요."
  git -C "$ROOT" rev-parse --verify --quiet "$BASE_BRANCH" >/dev/null || \
    fail "기준 브랜치를 찾지 못했습니다: $BASE_BRANCH"
  ARGS+=(--base "$BASE_BRANCH")
else
  ARGS+=(--uncommitted)
fi
ARGS+=("$REVIEW_PROMPT")

printf '[리뷰] Codex 읽기 전용 교차 리뷰를 시작합니다 (모드: %s, 기준: %s)\n' "${MODE#--}" "$BASE_BRANCH"
cd "$ROOT"
exec "$CODEX_BIN" "${ARGS[@]}"
