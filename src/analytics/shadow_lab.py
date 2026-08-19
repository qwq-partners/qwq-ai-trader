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


def evaluate_vg_history(snapshots: List[dict], closes_map: dict) -> Optional[dict]:
    """밸류코어 shadow 이력의 20영업일 포워드 평가 (순수 함수 — 테스트 주입 가능)

    설계 §9 기준 구현 (2026-08-20 Codex P1 반영):
      · 동일 가중: 주차별로 픽 수익률을 먼저 평균 → 주차 평균들의 평균
        (종목 수 많은 주가 과대 가중되는 pooled 평균 금지)
      · 하방 가드: 전체 픽의 **원시**(초과 아님) 20일 수익률 25분위수 >= -15%
      · ok = 주간 초과수익 평균 > 0 AND 하방 가드 통과 (평가 주 >= 6 필요)

    Args:
        snapshots: value_growth_history 주차 스냅샷 리스트 ({scan_date, final})
        closes_map: {symbol: 종가 Series (DatetimeIndex)} — "069500" 벤치마크 필수
    Returns:
        {n_eval_weeks, mean_week_excess, q25_raw, ok} 또는 벤치마크 부재 시 None
    """
    bench = closes_map.get("069500")
    if bench is None:
        return None
    week_excesses: List[float] = []
    raw_rets: List[float] = []
    for snap in snapshots:
        scan_d = snap.get("scan_date", "")
        picks = snap.get("final")
        if not scan_d or not isinstance(picks, list) or not picks:
            continue
        b = bench[bench.index >= scan_d]
        if len(b) < 21:
            continue  # 포워드 창 미경과
        bench_ret = float(b.iloc[20] / b.iloc[0] - 1) * 100
        week_raws: List[float] = []
        for sym in picks:
            c = closes_map.get(sym)
            if c is None:
                continue
            c = c[c.index >= scan_d]
            if len(c) >= 21:
                week_raws.append(float(c.iloc[20] / c.iloc[0] - 1) * 100)
        if not week_raws:
            continue
        raw_rets.extend(week_raws)
        week_excesses.append(sum(week_raws) / len(week_raws) - bench_ret)
    if not week_excesses:
        return {"n_eval_weeks": 0, "mean_week_excess": 0.0, "q25_raw": 0.0, "ok": False}
    mean_week_excess = sum(week_excesses) / len(week_excesses)
    # 하위 25% 분위 — 표본 소규모라 보수적으로 버림 인덱스 사용
    _sorted = sorted(raw_rets)
    q25_raw = _sorted[int(len(_sorted) * 0.25)] if _sorted else 0.0
    n_eval = len(week_excesses)
    ok = n_eval >= 6 and mean_week_excess > 0 and q25_raw >= -15.0
    return {
        "n_eval_weeks": n_eval,
        "mean_week_excess": mean_week_excess,
        "q25_raw": q25_raw,
        "ok": ok,
    }


async def promotion_readiness_report() -> str:
    """섀도우 승격 기준 주간 점검 (2026-08-20 — 관측 전용, 설정 불변)

    기준 출처:
      · 규칙 #11: default.yml experts.shadow_mode 주석 — CF 표본 >=20 & r5 정확도 >=55%
      · 밸류코어: docs/strategies/value-growth-core-design.md §9 —
        관측 8주+ & 픽 20영업일 포워드 평균이 KODEX200 우위 (평가 가능 >=6주)
    기준 충족 시 맨 앞에 🚀 마커 — 사용자가 보고 승격 작업을 지시하는 트리거.
    """
    lines: List[str] = []
    ready: List[str] = []
    try:
        # ── 규칙 #11 ──
        cf_path = _CACHE / "counterfactual_state.json"
        n_r5, acc = 0, 0.0
        if cf_path.exists():
            cf = json.loads(cf_path.read_text())
            r5 = [v["r5"] for v in cf.values()
                  if v.get("source") == "rule11" and v.get("r5") is not None]
            n_r5 = len(r5)
            acc = (sum(1 for x in r5 if x < 0) / n_r5 * 100) if n_r5 else 0.0
        _r11_ok = n_r5 >= 20 and acc >= 55.0
        if _r11_ok:
            ready.append("규칙#11 실차단 전환")
        lines.append(
            f"· 규칙#11: CF 표본 {n_r5}/20건, r5 정확도 {acc:.0f}%/55% "
            f"→ {'✅ 기준 충족' if _r11_ok else '⏳ 축적 중'}"
        )

        # ── 밸류코어 ──
        hist_dir = _CACHE / "value_growth_history"
        weeks = sorted(hist_dir.glob("*.json")) if hist_dir.exists() else []
        n_weeks = len(weeks)
        _vg_line = f"· 밸류코어: 이력 {n_weeks}/8주"
        if n_weeks >= 8:
            try:
                import FinanceDataReader as fdr
                import asyncio as _aio

                def _closes(sym: str):
                    return fdr.DataReader(sym, "2026-08-01")["Close"]

                # 픽 종가를 미리 로드해 순수 평가 함수에 주입 (블로킹은 스레드로)
                snapshots = [json.loads(wf.read_text()) for wf in weeks]
                _syms = {"069500"}
                for s in snapshots:
                    _syms.update(s.get("final") or [])
                closes_map = {}
                for sym in _syms:
                    try:
                        closes_map[sym] = await _aio.to_thread(_closes, sym)
                    except Exception:
                        continue
                result = evaluate_vg_history(snapshots, closes_map)
                if result is None:
                    _vg_line += " — ⏳ 포워드 창 대기"
                elif result["n_eval_weeks"] < 6:
                    _vg_line += f", 평가 가능 {result['n_eval_weeks']}/6주 — ⏳ 포워드 창 대기"
                else:
                    _vg_ok = result["ok"]
                    if _vg_ok:
                        ready.append("밸류코어 실배분 승격 준비")
                    _vg_line += (
                        f", 평가 {result['n_eval_weeks']}주 "
                        f"주간 초과수익 {result['mean_week_excess']:+.2f}%p / "
                        f"하위25% {result['q25_raw']:+.1f}% "
                        f"→ {'✅ 기준 충족' if _vg_ok else '❌ 기준 미달'}"
                    )
            except Exception as _e:
                _vg_line += f" (포워드 평가 실패: {str(_e)[:40]})"
        else:
            _vg_line += " — ⏳ 축적 중"
        lines.append(_vg_line)
    except Exception as e:
        logger.warning(f"[승격점검] 실패: {e}")
        return ""

    head = ""
    if ready:
        head = "🚀 승격 기준 충족 — Claude에게 진행 지시: " + ", ".join(ready) + "\n"
    return head + "\n".join(lines)
