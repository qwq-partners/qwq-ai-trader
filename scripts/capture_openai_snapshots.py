#!/usr/bin/env python3
"""OpenAI alias → snapshot ID 캡처 (Phase 1 — GPT-5 deprecation 마이그레이션)

config/default.yml 에서 사용 중인 OpenAI 모델 alias들을 호출하여
실제 응답의 model 필드에서 snapshot ID를 추출.

OpenAI는 alias 호출 시 응답 JSON에 `"model": "<snapshot-id>"` 형태로
실제 사용된 snapshot을 반환함 (예: gpt-5-mini → gpt-5-mini-2025-08-07).

이 매핑이 deprecation 직접 대상 snapshot이면 즉시 마이그레이션 필요.

사용:
    python scripts/capture_openai_snapshots.py
    python scripts/capture_openai_snapshots.py --json out.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import aiohttp


# Deprecation 대상 snapshot IDs (2026-12-10 셧다운)
DEPRECATED_SNAPSHOTS = {
    "gpt-5-2025-08-07",
    "gpt-5-mini-2025-08-07",
    "gpt-5-nano-2025-08-07",
    "gpt-5-pro-2025-10-06",
}


def load_aliases_from_config() -> List[str]:
    """config/default.yml + evolved_overrides.yml에서 OpenAI 모델 alias 추출."""
    try:
        import yaml
    except ImportError:
        print("[ERROR] pyyaml 필요: pip install pyyaml", file=sys.stderr)
        return []

    project_root = Path(__file__).parent.parent
    paths = [
        project_root / "config" / "default.yml",
        project_root / "config" / "evolved_overrides.yml",
    ]
    aliases = set()
    for p in paths:
        if not p.exists():
            continue
        with p.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        llm = cfg.get("llm") or {}
        for key in ("openai_model_heavy", "openai_model_light"):
            v = llm.get(key)
            if v:
                aliases.add(v)
    return sorted(aliases)


async def call_openai_alias(session: aiohttp.ClientSession, api_key: str, alias: str) -> Dict:
    """단일 alias 호출 → 응답 model 필드 추출."""
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": alias,
        "messages": [{"role": "user", "content": "Say 'ok' only."}],
        "max_completion_tokens": 50,
    }
    try:
        async with session.post(url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            status = resp.status
            text = await resp.text()
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = {}
            resolved = data.get("model", "")
            return {
                "alias": alias,
                "http_status": status,
                "resolved_model": resolved,
                "is_deprecated": resolved in DEPRECATED_SNAPSHOTS,
                "raw_error": data.get("error") if status >= 400 else None,
            }
    except Exception as e:
        return {
            "alias": alias,
            "http_status": 0,
            "resolved_model": "",
            "is_deprecated": False,
            "raw_error": str(e),
        }


async def main_async(json_out: str = "") -> int:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        # .env 폴백
        env_path = Path(__file__).parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("OPENAI_API_KEY="):
                    api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not api_key:
        print("[ERROR] OPENAI_API_KEY 환경변수 또는 .env 파일 필요", file=sys.stderr)
        return 1

    aliases = load_aliases_from_config()
    if not aliases:
        print("[ERROR] config에서 OpenAI 모델 alias를 찾지 못함", file=sys.stderr)
        return 1

    print(f"[INFO] 캡처 대상 alias: {aliases}")
    print()

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *[call_openai_alias(session, api_key, a) for a in aliases]
        )

    # 출력
    print("# OpenAI alias → snapshot 매핑")
    print(f"- 캡처 시각: {datetime.now().isoformat(timespec='seconds')}")
    print()
    print("| Alias | HTTP | Resolved Snapshot | Deprecation 대상 |")
    print("|-------|------|-------------------|------------------|")
    deprecated_found = False
    for r in results:
        flag = "🔴 YES (2026-12-10 셧다운)" if r["is_deprecated"] else "🟢 NO"
        if r["is_deprecated"]:
            deprecated_found = True
        snap = r["resolved_model"] or "(응답 model 없음)"
        if r["raw_error"]:
            snap = f"ERROR: {r['raw_error']}"
        print(f"| {r['alias']} | {r['http_status']} | {snap} | {flag} |")

    print()
    if deprecated_found:
        print("⚠️  **즉시 Phase 2 진입 필요** — 사용 중 alias가 deprecation 대상 snapshot으로 해석됨")
    else:
        print("✅ 현재 사용 alias 중 deprecation 직접 대상 매핑 없음")

    if json_out:
        out = {
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "results": results,
            "deprecated_found": deprecated_found,
        }
        Path(json_out).write_text(json.dumps(out, ensure_ascii=False, indent=2))
        print(f"\n[JSON 저장] {json_out}", file=sys.stderr)

    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--json", default="", help="JSON 출력 파일 경로")
    args = p.parse_args()
    return asyncio.run(main_async(args.json))


if __name__ == "__main__":
    sys.exit(main())
