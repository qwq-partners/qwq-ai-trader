#!/usr/bin/env python3
"""Report existing LLM shadow observations; never change trading configuration.

The legacy filename is retained for scheduler compatibility. Process-quality
thresholds are descriptive only: they do not authorize model promotion, new
paid observations, or trading. Historical migration state is read-only.

Use --dry-run for local rendering without reading credentials or sending a report.
A fixed standalone snapshot may use --env-file for existing Telegram settings.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from html import escape
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Only notification credentials are needed; no provider/broker setup on import.
NOTIFICATION_KEYS = {"TELEGRAM_BOT_TOKEN", "TG_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TG_CHAT_ID"}


def _load_env(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in NOTIFICATION_KEYS and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


STATE_PATH = Path.home() / ".cache" / "ai_trader" / "llm_migration_state.json"
SHADOW_LOG_DIR = Path.home() / ".cache" / "ai_trader" / "llm_shadow"

# Historical process-quality thresholds; not a model-selection gate.
CRITERIA = {
    "min_pairs": 50,           # 최소 비교 쌍 (작으면 신뢰도 부족)
    "both_success_pct": 95.0,  # 둘 다 성공률 (%)
    "key_overlap_pct": 85.0,   # 응답 구조 동등성 (%)
    "shadow_failed_pct_max": 5.0,  # shadow 단독 실패율 (%)
}


def load_state() -> dict:
    try:
        state = json.loads(STATE_PATH.read_text())
        if isinstance(state, dict):
            return state
    except (OSError, ValueError):
        pass
    return {"phase": "unknown"}


def run_shadow_analysis(days: int = 7) -> dict:
    """analyze_shadow.py의 aggregate 로직을 직접 호출."""
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    try:
        from analyze_shadow import load_pairs, aggregate  # type: ignore
    except ImportError as e:
        return {"error": f"analyze_shadow import 실패: {e}"}
    pairs = load_pairs(SHADOW_LOG_DIR, days)
    if not pairs:
        return {"error": "shadow 로그 없음", "pairs": 0}
    agg = aggregate(pairs)
    agg["pairs"] = len(pairs)
    return agg


def evaluate_criteria(agg: dict) -> dict:
    """Summarize historical process quality without authorizing any action."""
    if "error" in agg:
        return {"pass": False, "reason": agg["error"]}
    n = agg.get("pairs", 0)
    st = agg.get("stats", {})
    if n < CRITERIA["min_pairs"]:
        return {"pass": False, "reason": f"표본 부족 ({n} < {CRITERIA['min_pairs']})", "n": n}

    both = st["both_success"]
    primary_only = st["primary_only"]
    both_pct = both / n * 100
    shadow_failed_pct = primary_only / n * 100

    key_ov_count = st.get("key_overlap_count", 0)
    key_ov_avg = (st.get("key_overlap_sum", 0) / key_ov_count * 100) if key_ov_count else 0.0

    reasons = []
    if both_pct < CRITERIA["both_success_pct"]:
        reasons.append(f"둘다성공 {both_pct:.1f}% < {CRITERIA['both_success_pct']}%")
    if key_ov_avg < CRITERIA["key_overlap_pct"]:
        reasons.append(f"key overlap {key_ov_avg:.1f}% < {CRITERIA['key_overlap_pct']}%")
    if shadow_failed_pct > CRITERIA["shadow_failed_pct_max"]:
        reasons.append(f"shadow 실패율 {shadow_failed_pct:.1f}% > {CRITERIA['shadow_failed_pct_max']}%")

    return {
        "pass": len(reasons) == 0,
        "reasons": reasons,
        "n": n,
        "both_pct": both_pct,
        "key_overlap_pct": key_ov_avg,
        "shadow_failed_pct": shadow_failed_pct,
    }


async def send_telegram(text: str) -> bool:
    """텔레그램 알림 발송."""
    try:
        from src.utils.telegram import TelegramNotifier
        notifier = TelegramNotifier()
        if not notifier.is_configured:
            print("[WARN] 텔레그램 미설정")
            return False
        try:
            return bool(await notifier.send_message(text))
        finally:
            # aiohttp 세션 정리 (unclosed warning 회피)
            sess = getattr(notifier, "_session", None)
            if sess is not None and not sess.closed:
                await sess.close()
    except Exception as e:
        print(f"[WARN] 텔레그램 발송 실패: {type(e).__name__}")
        return False


def daily_summary(agg: dict, state: dict) -> str:
    phase = escape(str(state.get("phase", "unknown")))
    if "error" in agg:
        return (
            f"📊 <b>LLM 비교 일일 모니터 · 보고 전용</b>\n"
            f"- 과거 상태 파일: {phase} (현재 모델을 뜻하지 않음)\n"
            f"- 상태: {escape(str(agg['error']))}\n"
        )
    n = agg["pairs"]
    st = agg["stats"]
    both = st["both_success"]
    primary_only = st["primary_only"]
    shadow_only = st["shadow_only"]
    both_failed = st["both_failed"]
    key_ov_count = st.get("key_overlap_count", 0)
    key_ov_avg = (st.get("key_overlap_sum", 0) / key_ov_count * 100) if key_ov_count else 0.0
    lat_avg = (st["shadow_latency_ms_sum"] / max(1, n))

    lines = [
        f"📊 <b>LLM 비교 일일 모니터 · 보고 전용</b> (과거 상태 {phase})",
        f"- Shadow 비교 쌍: <b>{n}</b>건 (최근 7일)",
        f"- 둘 다 성공: {both} ({both/n*100:.1f}%)",
        f"- Primary만 (shadow 실패): {primary_only} ({primary_only/n*100:.1f}%)",
        f"- Shadow만 (primary 실패): {shadow_only}",
        f"- 둘 다 실패: {both_failed}",
        f"- Key overlap 평균: {key_ov_avg:.1f}%",
        f"- Shadow 평균 지연: {lat_avg:.0f}ms",
    ]
    return "\n".join(lines)


def quality_report(result: dict) -> str:
    if result["pass"]:
        status = "과정 품질 기준 충족"
    else:
        reasons = result.get("reasons") or [result.get("reason", "자료 부족")]
        status = "과정 품질 기준 미충족: " + "; ".join(str(reason) for reason in reasons)
    return "\n".join([
        escape(status),
        "※ 요청 성공·JSON 키 일치 통계이며 판단 정확도·순수익의 증거가 아닙니다.",
        "※ 과거 상태 파일은 현재 운영 모델을 확인한 결과가 아닙니다.",
        "보고 전용: 모델·설정·상태 변경 및 봇 재시작 없음.",
        "모델 선택은 실행 원장 E04의 동일 입력·지연 후 가격·비용 검증을 따릅니다.",
    ])


async def main_async(*, dry_run: bool = False) -> int:
    state = load_state()
    try:
        aggregate = run_shadow_analysis(days=7)
        message = daily_summary(aggregate, state) + "\n\n" + quality_report(evaluate_criteria(aggregate))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as exc:
        aggregate = {"error": f"자료 분석 불가 ({type(exc).__name__})"}
        message = daily_summary(aggregate, state) + "\n\n" + quality_report(evaluate_criteria(aggregate))
    print(message)
    if dry_run:
        return 0
    return 0 if await send_telegram(message) else 1


def main(argv: list[str] | None = None) -> int:
    global STATE_PATH, SHADOW_LOG_DIR
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Render only; no credentials or Telegram")
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--state-file", type=Path, default=STATE_PATH)
    parser.add_argument("--log-dir", type=Path, default=SHADOW_LOG_DIR)
    args = parser.parse_args(argv)
    STATE_PATH, SHADOW_LOG_DIR = args.state_file, args.log_dir
    if not args.dry_run:
        _load_env(args.env_file)
    return asyncio.run(main_async(dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
