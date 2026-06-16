#!/usr/bin/env python3
"""OpenAI 후속 모델 가용성 프로브 (Phase 2 진입)

gpt-5-mini-2025-08-07 (deprecation 직접 대상)를 대체할 후속 모델 후보들을
OpenAI API에 실제 호출하여 200 응답 + resolved snapshot ID 확인.

알 수 없는 모델은 invalid_request_error (400) 반환 → 가용 여부 즉시 판정.

사용:
    python scripts/probe_openai_models.py
    python scripts/probe_openai_models.py --json out.json
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


# Phase 2: gpt-5-mini 대체 후보 목록
CANDIDATES_LIGHT = [
    # gpt-5.x mini 계열
    "gpt-5.4-mini",
    "gpt-5.5-mini",
    "gpt-5.6-mini",
    # 차세대
    "gpt-6-mini",
    "gpt-6-nano",
    # nano 계열
    "gpt-5.4-nano",
    "gpt-5.5-nano",
    # 신규 snapshot
    "gpt-5-mini-2026-03-05",
    "gpt-5-mini-latest",
]

# heavy(gpt-5.4) 후속 후보 (현재 안전하지만 사전 조사)
CANDIDATES_HEAVY = [
    "gpt-5.5",
    "gpt-5.6",
    "gpt-6",
    "gpt-6-pro",
    "gpt-5.4-pro",
]


def load_api_key() -> str:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if api_key:
        return api_key
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


async def probe_model(session: aiohttp.ClientSession, api_key: str, model: str) -> Dict:
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "ok"}],
        "max_completion_tokens": 30,
    }
    try:
        async with session.post(url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            status = resp.status
            text = await resp.text()
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = {}
            resolved = data.get("model", "") if status == 200 else ""
            err = data.get("error") if status >= 400 else None
            err_code = (err or {}).get("code") if isinstance(err, dict) else None
            err_msg = (err or {}).get("message", "")[:120] if isinstance(err, dict) else ""
            return {
                "candidate": model,
                "http_status": status,
                "available": status == 200,
                "resolved_model": resolved,
                "error_code": err_code,
                "error_message": err_msg,
            }
    except Exception as e:
        return {
            "candidate": model,
            "http_status": 0,
            "available": False,
            "resolved_model": "",
            "error_code": "network",
            "error_message": str(e)[:120],
        }


async def main_async(json_out: str = "") -> int:
    api_key = load_api_key()
    if not api_key:
        print("[ERROR] OPENAI_API_KEY 필요", file=sys.stderr)
        return 1

    all_candidates = CANDIDATES_LIGHT + CANDIDATES_HEAVY
    print(f"[INFO] 후보 {len(all_candidates)}개 프로브 중...")
    print()

    async with aiohttp.ClientSession() as session:
        # 동시 요청은 rate limit 위험 → 직렬화
        results: List[Dict] = []
        for c in all_candidates:
            r = await probe_model(session, api_key, c)
            results.append(r)

    # 출력
    print("# OpenAI 후속 모델 가용성 프로브")
    print(f"- 캡처 시각: {datetime.now().isoformat(timespec='seconds')}")
    print()
    print("## Light 후보 (gpt-5-mini 대체)")
    print("| Candidate | HTTP | Available | Resolved Snapshot | Error |")
    print("|-----------|------|-----------|-------------------|-------|")
    for r in results[:len(CANDIDATES_LIGHT)]:
        flag = "✅" if r["available"] else "❌"
        snap = r["resolved_model"] or "-"
        err = f"{r['error_code']}: {r['error_message']}" if r["error_code"] else ""
        print(f"| {r['candidate']} | {r['http_status']} | {flag} | {snap} | {err} |")

    print()
    print("## Heavy 후보 (gpt-5.4 미래 대비)")
    print("| Candidate | HTTP | Available | Resolved Snapshot | Error |")
    print("|-----------|------|-----------|-------------------|-------|")
    for r in results[len(CANDIDATES_LIGHT):]:
        flag = "✅" if r["available"] else "❌"
        snap = r["resolved_model"] or "-"
        err = f"{r['error_code']}: {r['error_message']}" if r["error_code"] else ""
        print(f"| {r['candidate']} | {r['http_status']} | {flag} | {snap} | {err} |")

    light_avail = [r for r in results[:len(CANDIDATES_LIGHT)] if r["available"]]
    if light_avail:
        print()
        print("## ✅ Light 가용 모델")
        for r in light_avail:
            print(f"- **{r['candidate']}** → resolved `{r['resolved_model']}`")
    else:
        print()
        print("## ⚠️  Light 후속 가용 모델 없음 — Gemini 폴백 전환 또는 OpenAI 공지 대기 필요")

    if json_out:
        out = {
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "results": results,
        }
        Path(json_out).write_text(json.dumps(out, ensure_ascii=False, indent=2))
        print(f"\n[JSON 저장] {json_out}", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--json", default="", help="JSON 출력 경로")
    args = p.parse_args()
    return asyncio.run(main_async(args.json))


if __name__ == "__main__":
    sys.exit(main())
