#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# 가상 오피스(Agent Virtual Office) 정적 번들 재빌드 스크립트
#
# upstream(KbWen/agent-virtual-office, MIT)을 내려받아 QWQ 대시보드용 패치를
# 적용한 뒤 빌드하고, 결과를 src/dashboard/static/office/ 로 배치한다.
#
# 패치 내용 (3가지):
#   1) API 경로   /api/status → /api/office/status  (대시보드의 기존 /api/status와 충돌 회피)
#                 /api/lang   → /api/office/lang
#   2) 한국어 로케일 ko.json 추가 + 기본 언어를 ko로 (캐릭터명 = 트레이딩 역할)
#   3) index.html 제목/설명 한국어화, 외부 OG 이미지 메타 제거
#
# 사용법:
#   bash tools/office/build.sh              # 고정 커밋(PINNED_REF)으로 빌드
#   UPSTREAM_REF=main bash tools/office/build.sh   # 최신 main으로 빌드
#
# 요구사항: node >= 22, npm, git
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TOOL_DIR="$REPO_ROOT/tools/office"
OUT_DIR="$REPO_ROOT/src/dashboard/static/office"

# 검증된 upstream 커밋 (2026-08-01, v1.6.4)
PINNED_REF="685e767b060a65c15f64122520dab5f2192fa16a"
UPSTREAM_REF="${UPSTREAM_REF:-$PINNED_REF}"
UPSTREAM_URL="https://github.com/KbWen/agent-virtual-office.git"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "[1/5] upstream 클론 ($UPSTREAM_REF)"
git clone --quiet "$UPSTREAM_URL" "$WORK_DIR/avo"
git -C "$WORK_DIR/avo" checkout --quiet "$UPSTREAM_REF"

cd "$WORK_DIR/avo"

echo "[2/5] 패치 적용"
# (1) API 경로 — 대시보드는 /api/status를 이미 KR 봇 상태로 쓰고 있음
sed -i "s#'/api/status'#'/api/office/status'#g; \
        s#'/api/status/stream'#'/api/office/status/stream'#g; \
        s#'/api/lang'#'/api/office/lang'#g" src/inference/inferStatus.js src/i18n.js
grep -q "/api/office/status" src/inference/inferStatus.js || { echo "패치 실패: API 경로"; exit 1; }

# (2) 한국어 로케일
cp "$TOOL_DIR/ko.json" src/locales/ko.json
python3 - <<'PY'
import pathlib
p = pathlib.Path('src/i18n.js')
s = p.read_text(encoding='utf-8')
s = s.replace("import zhTW from './locales/zh-TW.json'",
              "import zhTW from './locales/zh-TW.json'\nimport ko from './locales/ko.json'")
s = s.replace("const LOCALES = { en, 'zh-TW': zhTW }",
              "// QWQ 통합: 한국어(ko)를 기본 로케일로 추가 (대시보드 전체가 한국어)\nconst LOCALES = { ko, en, 'zh-TW': zhTW }")
s = s.replace("""    if (nav === 'zh-TW' || nav === 'zh-HK' || nav === 'zh-Hant' || nav?.startsWith('zh-Hant')) return 'zh-TW'
  }
  return 'en'""",
              """    if (nav === 'zh-TW' || nav === 'zh-HK' || nav === 'zh-Hant' || nav?.startsWith('zh-Hant')) return 'zh-TW'
    if (nav?.startsWith('en')) return 'en'
  }
  return 'ko'""")
assert "locales/ko.json" in s and "return 'ko'" in s, "패치 실패: i18n"
p.write_text(s, encoding='utf-8')
PY

# (3) index.html 한국어화 + 외부 OG 이미지 제거
python3 - <<'PY'
import pathlib, re
p = pathlib.Path('index.html')
s = p.read_text(encoding='utf-8')
s = s.replace('<html lang="en">', '<html lang="ko">')
s = re.sub(r'<title>.*?</title>', '<title>QWQ 가상 오피스 — 트레이딩 엔진 활동 시각화</title>', s, flags=re.S)
s = re.sub(r'<meta name="description" content="[^"]*"',
           '<meta name="description" content="트레이딩 엔진과 Claude Code의 활동을 픽셀아트 사무실로 실시간 시각화합니다."', s)
s = re.sub(r'\n\s*<!-- Open Graph -->.*?<meta name="twitter:image"[^>]*/>\n', '\n', s, flags=re.S)
p.write_text(s, encoding='utf-8')
PY

echo "[3/5] 의존성 설치"
npm install --no-audit --no-fund --silent

echo "[4/5] 빌드 (base=/static/office/)"
npx vite build --base=/static/office/ >/dev/null

echo "[5/5] 배치 → $OUT_DIR"
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
cp -r dist/. "$OUT_DIR"/
cp LICENSE "$OUT_DIR/LICENSE"
git -C "$WORK_DIR/avo" rev-parse HEAD > "$OUT_DIR/UPSTREAM_REF"

echo "완료. 대시보드 재시작 후 http://<host>:8080/office 확인"
