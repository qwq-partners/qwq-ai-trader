#!/usr/bin/env python3
"""LLM 마이그레이션 자동 모니터 (Phase 3 → Phase 4 자동 진행)

매일 실행:
- Shadow 누적 분석 (`scripts/analyze_shadow.py` 로직 임포트)
- 텔레그램 일일 요약 발송

Shadow 시작 후 7일+ 경과 시 자동 전환 검토:
- 기준: both_success ≥95% AND key_overlap ≥85% AND shadow_failed ≤5%
- 충족 시: evolved_overrides.yml에 `openai_model_light: gpt-5.4-mini` 추가
  → 다음 봇 재시작 시 자동 적용 (또는 즉시 systemctl restart)
- 미충족 시: 텔레그램 경고 + 수동 결정 요청

전환 후:
- 매일 새 모델 성공률 모니터 → ≥7일 안정 시 shadow 비활성 (Phase 5)
- 실패율 급증 시 즉시 롤백 알림

상태 파일: ~/.cache/ai_trader/llm_migration_state.json
- {"phase": "shadow"|"transitioned"|"completed", "transitioned_at": "...", ...}

사용 (cron 등록):
    0 22 * * * cd /home/ubuntu/projects/qwq-ai-trader && venv/bin/python scripts/llm_migration_monitor.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# .env 로드
def _load_env():
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k not in os.environ:
            os.environ[k] = v

_load_env()


SHADOW_START = date(2026, 6, 17)
TRANSITION_AFTER_DAYS = 7  # Shadow 시작 후 7일+ 경과 시 검토
CURRENT_LIGHT = "gpt-5-mini"
CANDIDATE_LIGHT = "gpt-5.4-mini"

STATE_PATH = Path.home() / ".cache" / "ai_trader" / "llm_migration_state.json"
EVOLVED_OVERRIDES_PATH = PROJECT_ROOT / "config" / "evolved_overrides.yml"
SHADOW_LOG_DIR = Path.home() / ".cache" / "ai_trader" / "llm_shadow"

# 전환 기준
CRITERIA = {
    "min_pairs": 50,           # 최소 비교 쌍 (작으면 신뢰도 부족)
    "both_success_pct": 95.0,  # 둘 다 성공률 (%)
    "key_overlap_pct": 85.0,   # 응답 구조 동등성 (%)
    "shadow_failed_pct_max": 5.0,  # shadow 단독 실패율 (%)
}


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {"phase": "shadow", "shadow_start": SHADOW_START.isoformat()}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))


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
    """전환 기준 평가."""
    if "error" in agg:
        return {"pass": False, "reason": agg["error"]}
    n = agg.get("pairs", 0)
    st = agg.get("stats", {})
    if n < CRITERIA["min_pairs"]:
        return {"pass": False, "reason": f"표본 부족 ({n} < {CRITERIA['min_pairs']})", "n": n}

    both = st["both_success"]
    primary_only = st["primary_only"]
    shadow_only = st["shadow_only"]
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


def write_transition(new_light: str) -> None:
    """evolved_overrides.yml에 llm.openai_model_light override 추가."""
    import yaml  # PyYAML

    cfg = {}
    if EVOLVED_OVERRIDES_PATH.exists():
        try:
            cfg = yaml.safe_load(EVOLVED_OVERRIDES_PATH.read_text()) or {}
        except Exception:
            cfg = {}
    llm = cfg.get("llm") or {}
    llm["openai_model_light"] = new_light
    # shadow는 비활성 — Phase 5 정리 대기 (즉시 비활성 시 비교 데이터 단절)
    cfg["llm"] = llm

    # _meta 자취
    meta = cfg.get("_meta") or {}
    meta["llm.openai_model_light"] = {
        "source": "llm_migration_monitor",
        "timestamp": datetime.now().isoformat(),
        "note": f"자동 전환: {CURRENT_LIGHT} → {new_light} (Phase 3 Shadow 기준 충족, 2026-12-10 deprecation 대응)",
    }
    cfg["_meta"] = meta

    EVOLVED_OVERRIDES_PATH.write_text(
        yaml.safe_dump(cfg, allow_unicode=True, default_flow_style=False, sort_keys=False)
    )


async def send_telegram(text: str) -> None:
    """텔레그램 알림 발송."""
    try:
        from src.utils.telegram import TelegramNotifier
        notifier = TelegramNotifier()
        if not notifier.is_configured:
            print("[WARN] 텔레그램 미설정")
            return
        try:
            await notifier.send_message(text)
        finally:
            # aiohttp 세션 정리 (unclosed warning 회피)
            sess = getattr(notifier, "_session", None)
            if sess is not None and not sess.closed:
                await sess.close()
    except Exception as e:
        print(f"[WARN] 텔레그램 발송 실패: {e}")


def restart_bot() -> bool:
    """systemctl restart로 새 모델 즉시 반영."""
    import subprocess
    try:
        subprocess.run(
            ["sudo", "-S", "-k", "systemctl", "restart", "qwq-ai-trader"],
            input="user123!\n",
            text=True,
            timeout=30,
            check=False,
        )
        return True
    except Exception as e:
        print(f"[WARN] 봇 재시작 실패: {e}")
        return False


def daily_summary(agg: dict, state: dict) -> str:
    phase = state.get("phase", "shadow")
    if "error" in agg:
        return (
            f"📊 <b>LLM 마이그레이션 일일 모니터</b>\n"
            f"- Phase: {phase}\n"
            f"- 상태: {agg['error']}\n"
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
        f"📊 <b>LLM 마이그레이션 일일 모니터</b> (Phase {phase})",
        f"- Shadow 비교 쌍: <b>{n}</b>건 (최근 7일)",
        f"- 둘 다 성공: {both} ({both/n*100:.1f}%)",
        f"- Primary만 (shadow 실패): {primary_only} ({primary_only/n*100:.1f}%)",
        f"- Shadow만 (primary 실패): {shadow_only}",
        f"- 둘 다 실패: {both_failed}",
        f"- Key overlap 평균: {key_ov_avg:.1f}%",
        f"- Shadow 평균 지연: {lat_avg:.0f}ms",
    ]
    return "\n".join(lines)


def transition_report(eval_result: dict, success: bool, restart_ok: bool) -> str:
    n = eval_result.get("n", 0)
    both = eval_result.get("both_pct", 0)
    ov = eval_result.get("key_overlap_pct", 0)
    sf = eval_result.get("shadow_failed_pct", 0)
    head = "✅ <b>LLM 자동 전환 완료</b>" if success else "⚠️ <b>LLM 전환 보류</b>"
    lines = [
        head,
        f"- 표본: {n}쌍 (7일)",
        f"- 둘다성공: {both:.1f}% (기준 ≥{CRITERIA['both_success_pct']}%)",
        f"- Key overlap: {ov:.1f}% (기준 ≥{CRITERIA['key_overlap_pct']}%)",
        f"- Shadow 실패율: {sf:.1f}% (기준 ≤{CRITERIA['shadow_failed_pct_max']}%)",
    ]
    if success:
        lines.append(f"- <b>{CURRENT_LIGHT} → {CANDIDATE_LIGHT}</b> 전환 (evolved_overrides.yml)")
        lines.append(f"- 봇 재시작: {'✅ OK' if restart_ok else '❌ 수동 필요'}")
        lines.append("- 롤백: evolved_overrides.yml의 llm.openai_model_light 제거")
    else:
        lines.append("- 미충족 사유:")
        for r in eval_result.get("reasons", []):
            lines.append(f"  · {r}")
        lines.append("- 수동 확인 필요. shadow 1주 추가 수집 후 재평가 권장.")
    return "\n".join(lines)


async def main_async() -> int:
    state = load_state()
    phase = state.get("phase", "shadow")
    today = date.today()
    days_since_shadow = (today - SHADOW_START).days

    agg = run_shadow_analysis(days=7)

    # 일일 요약 (항상 발송)
    summary = daily_summary(agg, state)
    print(summary)

    # 전환 결정
    transition_attempted = False
    transition_msg = ""

    if phase == "shadow" and days_since_shadow >= TRANSITION_AFTER_DAYS:
        ev = evaluate_criteria(agg)
        transition_attempted = True
        if ev["pass"]:
            try:
                write_transition(CANDIDATE_LIGHT)
                restart_ok = restart_bot()
                state["phase"] = "transitioned"
                state["transitioned_at"] = datetime.now().isoformat()
                state["new_model"] = CANDIDATE_LIGHT
                state["criteria_result"] = ev
                save_state(state)
                transition_msg = transition_report(ev, success=True, restart_ok=restart_ok)
            except Exception as e:
                transition_msg = (
                    f"❌ <b>LLM 전환 실패</b>\n"
                    f"- 기준은 충족했으나 적용 중 오류: {e}\n"
                    f"- 수동 확인 필요."
                )
        else:
            transition_msg = transition_report(ev, success=False, restart_ok=False)
            # 표본 부족이 아니면 phase는 그대로 두고, 미충족 사유 누적
            state.setdefault("failed_evals", []).append({
                "date": today.isoformat(), "result": ev,
            })
            save_state(state)
    elif phase == "transitioned":
        # 전환 후 안정성 모니터 (7일 안정 시 Phase 5)
        trans_at = state.get("transitioned_at")
        if trans_at:
            try:
                trans_date = datetime.fromisoformat(trans_at).date()
                if (today - trans_date).days >= 7 and "error" not in agg:
                    n = agg["pairs"]
                    if n > 0 and agg["stats"]["both_success"] / n >= 0.95:
                        transition_msg = (
                            "🎯 <b>LLM Phase 5 완료 대기</b>\n"
                            "- 전환 후 7일 안정성 확인됨\n"
                            "- shadow 비활성 권장 (config/default.yml openai_model_light_shadow=\"\")"
                        )
            except Exception:
                pass

    # 텔레그램 발송
    msg = summary
    if transition_msg:
        msg += "\n\n" + transition_msg
    await send_telegram(msg)

    # 추가 로그
    if transition_attempted:
        print("\n--- 전환 시도 결과 ---")
        print(transition_msg)

    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
