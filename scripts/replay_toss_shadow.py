#!/usr/bin/env python3
"""합성 파일만으로 Toss Phase 1 shadow 통계를 재현한다."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data.providers.toss.shadow import ShadowManifest, summarize_pairs  # noqa: E402


def _load_json(path: Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="합성 가격쌍 오프라인 shadow 재현")
    parser.add_argument("--manifest", required=True, type=Path, help="offline synthetic manifest JSON")
    parser.add_argument("--input", required=True, type=Path, help="synthetic pair array JSON")
    args = parser.parse_args(argv)
    manifest = ShadowManifest.from_dict(_load_json(args.manifest))
    rows = _load_json(args.input)
    report = summarize_pairs(rows, manifest)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
