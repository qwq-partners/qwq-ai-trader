#!/usr/bin/env python3
"""
shadow 관측 리포트 — 팀 심의가 실주문으로 승격할 만한지 판단하는 근거를 모은다.

`docs/agents/trading-team.md`의 승격 기준을 그대로 항목화해 현재 달성도를 보여준다.
"며칠 관측했다"가 아니라 **무엇이 충족됐고 무엇이 남았는지**를 숫자로 답한다.

사용:
    python scripts/shadow_report.py              # 오늘
    python scripts/shadow_report.py --days 7     # 최근 7일 누적
    python scripts/shadow_report.py --telegram   # 텔레그램 전송
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

VERDICT_DIR = Path.home() / ".cache" / "ai_trader" / "team_verdicts"
LEDGER_DIR = Path.home() / ".cache" / "ai_trader" / "llm_ledger"

# 승격 기준 (docs/agents/trading-team.md와 동일해야 한다)
MIN_SAMPLES = 200
MIN_AGREEMENT = 80.0


def _load_days(base: Path, prefix: str, days: int) -> list:
    """최근 N일치 파일을 모아 읽는다"""
    rows = []
    for i in range(days):
        d = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
        p = base / f"{prefix}{d}.json"
        pj = base / f"{prefix}{d}.jsonl"
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    rows.extend(data)
            except (json.JSONDecodeError, OSError):
                pass
        elif pj.exists():
            try:
                for line in pj.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        rows.append(json.loads(line))
            except (json.JSONDecodeError, OSError):
                pass
    return rows


def build_report(days: int = 1) -> str:
    verdicts = _load_days(VERDICT_DIR, "verdicts_", days)
    ledger = _load_days(LEDGER_DIR, "llm_", days)

    period = "오늘" if days == 1 else f"최근 {days}일"
    L = [f"📋 팀 심의 shadow 리포트 ({period})", ""]

    if not verdicts:
        L.append("아직 심의 기록이 없습니다.")
        L.append("심의는 장중 10:30 / 14:00에 실행됩니다 (휴장일 제외).")
        return "\n".join(L)

    # ── 심의 현황 ──
    stances = Counter()
    approved = 0
    blocked_reasons = Counter()
    elapsed = []
    for v in verdicts:
        d = v.get("decision") or {}
        st = d.get("stance", "?")
        stances[st] += 1
        if d.get("approved"):
            approved += 1
        else:
            r = (d.get("reason") or "")[:40]
            if r:
                blocked_reasons[r] += 1
        if v.get("elapsed_sec"):
            elapsed.append(float(v["elapsed_sec"]))

    L.append(f"■ 심의 {len(verdicts)}건")
    L.append(f"  판정: " + " / ".join(f"{k} {n}" for k, n in stances.most_common()))
    L.append(f"  승인 {approved} / 보류·거부 {len(verdicts) - approved}")
    if elapsed:
        L.append(f"  소요: 평균 {sum(elapsed)/len(elapsed):.1f}초 (최대 {max(elapsed):.1f}초)")

    if blocked_reasons:
        L.append("")
        L.append("■ 보류·거부 사유 (상위 5)")
        for reason, n in blocked_reasons.most_common(5):
            L.append(f"  {n}건 — {reason}")

    # ── 토론 품질 ──
    consensus = Counter()
    rounds = []
    for v in verdicts:
        deb = v.get("debate") or {}
        if not deb:
            continue
        c = deb.get("consensus")
        consensus["지지" if c is True else ("반대" if c is False else "미성립")] += 1
        if deb.get("rounds_run"):
            rounds.append(deb["rounds_run"])
    if consensus:
        L.append("")
        L.append("■ 토론")
        L.append("  " + " / ".join(f"{k} {n}" for k, n in consensus.most_common()))
        if rounds:
            L.append(f"  평균 {sum(rounds)/len(rounds):.1f}라운드")

    # ── 재현성 (승격 핵심 지표) ──
    L.append("")
    L.append("■ 재현성")
    if ledger:
        groups = {}
        empty = 0
        models = Counter()
        for r in ledger:
            if not r.get("success"):
                continue
            if not (r.get("response") or "").strip():
                empty += 1
            models[f"{r.get('provider','?')}/{r.get('model','?')}"] += 1
            h = r.get("prompt_hash")
            if h:
                groups.setdefault(h, []).append(r.get("verdict"))
        repeated = {h: v for h, v in groups.items() if len(v) >= 2}
        if repeated:
            consistent = sum(1 for v in repeated.values() if len(set(v)) == 1)
            rate = consistent / len(repeated) * 100
            mark = "✅" if rate >= MIN_AGREEMENT else "⚠️"
            L.append(f"  {mark} 일치율 {rate:.1f}% ({consistent}/{len(repeated)}종, 기준 {MIN_AGREEMENT}%)")
        else:
            L.append("  동일 입력 반복 없음 — 측정 불가 (정상: 종목마다 입력이 다름)")
        L.append(f"  LLM 호출 {len(ledger)}건 / 빈 응답 {empty}건")
        L.append("  모델: " + ", ".join(f"{k} {n}" for k, n in models.most_common(3)))
    else:
        L.append("  원장 기록 없음")

    # ── 승격 기준 달성도 ──
    L.append("")
    L.append("■ 실주문 승격 기준")
    L.append(f"  {'✅' if len(verdicts) >= MIN_SAMPLES else '⬜'} 표본 {len(verdicts)}/{MIN_SAMPLES}건")
    L.append("  ⬜ 레짐별 각 30건")
    L.append("  ⬜ 비용 반영 shadow P&L (+)")
    L.append("  ⬜ 기존 경로 대비 증분 효과")
    L.append("  ⬜ 장애 시 주문 0건 증명")
    L.append("")
    L.append("※ shadow 단계 — 실제 주문은 나가지 않습니다.")
    return "\n".join(L)


async def _send(text: str) -> None:
    from src.utils.telegram import send_alert
    await send_alert(text)


def main():
    ap = argparse.ArgumentParser(description="팀 심의 shadow 관측 리포트")
    ap.add_argument("--days", type=int, default=1, help="집계 일수 (기본 1=오늘)")
    ap.add_argument("--telegram", action="store_true", help="텔레그램 전송")
    args = ap.parse_args()

    report = build_report(max(1, args.days))
    print(report)

    if args.telegram:
        try:
            from dotenv import load_dotenv
            load_dotenv(str(Path(__file__).resolve().parents[1] / ".env"))
            asyncio.run(_send(report))
            print("\n[전송 완료]")
        except Exception as e:
            print(f"\n[전송 실패] {e}")


if __name__ == "__main__":
    main()
