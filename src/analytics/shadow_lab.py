"""Shadow 계측 모음 (2026-08-10 — 하네스 설계 Phase 3)

주문·설정을 절대 건드리지 않는 관측 전용 리포트 2종. 텔레그램 문자열만 반환.

1. 전문가 calibration — 전문가별 regime_bias 발언의 5거래일 방향 적중률
   (벤치마크: KODEX 200 069500). attribution 없는 전문가 가중 자동 조정 금지
   (설계 §3 편집 표면 정책) — 이 리포트가 그 전제 데이터를 쌓는다.
2. bandit shadow — 원장 포지션 성과 기반 Thompson 샘플링 배분 제안.
   적용 없음, 현재 배분과 나란히 보여주기만 (contextual bandit의 예습).

# ponytail: bias 3분류(강세/약세/중립)와 ±1.5% 중립 밴드는 단순 휴리스틱 —
# Brier score·자산별 벤치마크가 필요해지면 업그레이드.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

_CACHE = Path.home() / ".cache" / "ai_trader"
_BENCH_ETF = "069500"  # KODEX 200
_NEUTRAL_BAND = 1.5    # |r5| < 1.5% = 중립 적중


async def expert_calibration_report(broker, days: int = 28) -> str:
    """전문가별 bias 방향 적중률 ('' = 데이터 부족)"""
    try:
        prices = await broker.get_daily_prices(_BENCH_ETF, days=days + 10)
        if not prices or len(prices) < 7:
            return ""
        # oldest-first (sector_momentum 결함 교훈 — 순서 가정 주의)
        by_date: Dict[str, float] = {}
        dates: List[str] = []
        for p in prices:
            d = str(p.get("date") or p.get("stck_bsop_date") or "")
            c = float(p.get("stck_clpr") or p.get("close") or 0)
            if d and c > 0:
                by_date[d] = c
                dates.append(d)

        def r5_after(date_str: str) -> Optional[float]:
            """해당 일자 이후 5거래일 수익률 (미래 데이터 부족 시 None)"""
            d8 = date_str.replace("-", "")[:8]
            try:
                idx = next(i for i, d in enumerate(dates) if d >= d8)
            except StopIteration:
                return None
            if idx + 5 >= len(dates):
                return None
            base, fwd = by_date[dates[idx]], by_date[dates[idx + 5]]
            return (fwd - base) / base * 100

        stats: Dict[str, Dict[str, int]] = {}
        cutoff = datetime.now() - timedelta(days=days)
        for f in sorted(_CACHE.glob("experts/*.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    op = json.loads(line)
                    issued = datetime.fromisoformat(op["issued_at"])
                    if issued < cutoff:
                        continue
                    bias = str(op.get("regime_bias", "")).lower()
                    r5 = r5_after(issued.strftime("%Y%m%d"))
                    if r5 is None or not bias:
                        continue
                    if "bull" in bias:
                        hit = r5 > 0
                    elif "bear" in bias:
                        hit = r5 < 0
                    else:
                        hit = abs(r5) < _NEUTRAL_BAND
                    s = stats.setdefault(op.get("expert", "?"), {"n": 0, "hits": 0})
                    s["n"] += 1
                    s["hits"] += 1 if hit else 0
                except (KeyError, ValueError, json.JSONDecodeError):
                    continue

        rows = [(k, v) for k, v in stats.items() if v["n"] >= 3]
        if not rows:
            return ""
        rows.sort(key=lambda kv: -(kv[1]["hits"] / kv[1]["n"]))
        return "\n".join(
            f"· {name}: {v['hits']}/{v['n']} ({v['hits'] / v['n'] * 100:.0f}%)"
            for name, v in rows
        )
    except Exception as e:
        logger.warning(f"[전문가calibration] 실패: {e}")
        return ""


def bandit_shadow_report(days: int = 90) -> str:
    """원장 기반 Thompson 샘플링 배분 제안 ('' = 표본 부족, 적용 없음)"""
    try:
        from src.analytics.position_ledger import get_position_ledger
        stats = get_position_ledger("KR").stats_by_strategy(days=days)
        eligible = {s: g for s, g in stats.items() if g.get("n", 0) >= 5}
        if len(eligible) < 2:
            return ""  # 비교 대상 2전략 미만 — 제안 무의미
        rng = random.Random(datetime.now().strftime("%Y-%m-%d"))  # 일 단위 재현성
        draws = 2000
        wins_count = {s: 0 for s in eligible}
        for _ in range(draws):
            best, best_v = None, -1.0
            for s, g in eligible.items():
                v = rng.betavariate(g["wins"] + 1, (g["n"] - g["wins"]) + 1)
                if v > best_v:
                    best, best_v = s, v
            wins_count[best] += 1
        lines = [
            f"· {s}: 제안 {wins_count[s] / draws * 100:.0f}% "
            f"(원장 승률 {g['win_rate']:.0f}%, n={g['n']})"
            for s, g in sorted(eligible.items(), key=lambda kv: -wins_count[kv[0]])
        ]
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"[bandit-shadow] 실패: {e}")
        return ""
