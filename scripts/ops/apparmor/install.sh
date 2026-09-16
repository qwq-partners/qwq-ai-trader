#!/usr/bin/env bash
# Codex 번들 bubblewrap 에 userns 를 허용하는 AppArmor 프로파일 설치/갱신 (Ubuntu 24.04+, 운영 서버).
# 근거·롤백은 프로파일 파일 머리말과 docs/operations/local-development.md §9 참조.
set -euo pipefail
SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/codex-bwrap
DST=/etc/apparmor.d/codex-bwrap
sudo install -o root -g root -m 0644 "$SRC" "$DST"
sudo apparmor_parser -r "$DST"
sudo aa-status | grep -q "codex-bwrap" && printf '[완료] %s 로드됨\n' "$DST"
