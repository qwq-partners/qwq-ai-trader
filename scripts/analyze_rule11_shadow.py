#!/usr/bin/env python3
"""규칙 #11 (전문가 BEAR 합의) Shadow mode hit rate 분석

사용:
    python3 scripts/analyze_rule11_shadow.py [--days 7] [--threshold-pct -3.0]

동작:
    1. ~/.cache/ai_trader/rule11_shadow_log.jsonl 읽기
    2. 각 기록에 대해 +N영업일 후 KIS/yfinance 현재가 조회
    3. -3% 이상 하락한 경우 → 차단 정당 (hit)
    4. hit rate 계산 → 50% 이상이면 shadow_mode: false 권장
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")


_LOG_PATH = Path.home() / ".cache" / "ai_trader" / "rule11_shadow_log.jsonl"


def load_hits(days: int) -> List[Dict]:
    if not _LOG_PATH.exists():
        return []
    cutoff = datetime.now() - timedelta(days=days)
    out = []
    with _LOG_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts = datetime.fromisoformat(rec["timestamp"])
                if ts >= cutoff:
                    out.append(rec)
            except Exception:
                continue
    return out


async def get_price_change(symbol: str, since: datetime) -> Optional[float]:
    """해당 시점부터 현재까지 변동률 (yfinance fallback)"""
    try:
        import yfinance as yf
        # KR 종목코드 → .KS / .KQ 시도
        candidates = [symbol]
        if symbol.isdigit() and len(symbol) == 6:
            candidates = [f"{symbol}.KS", f"{symbol}.KQ"]
        for tkr in candidates:
            try:
                hist = yf.Ticker(tkr).history(
                    start=since.strftime("%Y-%m-%d"),
                    end=(datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"),
                    auto_adjust=False,
                )
                if hist.empty or len(hist) < 2:
                    continue
                start = float(hist["Close"].iloc[0])
                last = float(hist["Close"].iloc[-1])
                if start <= 0:
                    continue
                return (last - start) / start * 100
            except Exception:
                continue
    except Exception:
        pass
    return None


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--threshold-pct", type=float, default=-3.0,
                        help="하락 임계값 (-3.0이면 -3% 이상 하락 시 hit)")
    args = parser.parse_args()

    hits = load_hits(args.days)
    if not hits:
        print(f"❌ 최근 {args.days}일간 기록 없음: {_LOG_PATH}")
        return

    print(f"📊 규칙 #11 Shadow Log 분석 ({args.days}일, {len(hits)}건)")
    print(f"   임계값: {args.threshold_pct}% (이하 하락 시 차단 정당)")
    print()

    correct = 0    # hit (-3% 이상 하락)
    incorrect = 0  # 차단했으면 손해
    no_data = 0

    for rec in hits:
        ts = datetime.fromisoformat(rec["timestamp"])
        symbol = rec["symbol"]
        change = await get_price_change(symbol, ts)
        if change is None:
            no_data += 1
            status = "?"
        elif change <= args.threshold_pct:
            correct += 1
            status = f"✓ {change:+.1f}%"
        else:
            incorrect += 1
            status = f"✗ {change:+.1f}%"
        print(f"  {ts.strftime('%m-%d %H:%M')} {symbol:<10} {status}")

    evaluated = correct + incorrect
    hit_rate = correct / evaluated * 100 if evaluated > 0 else 0
    print()
    print(f"📈 결과")
    print(f"  - 차단 정당 (hit): {correct}건")
    print(f"  - 차단 부당 (miss): {incorrect}건")
    print(f"  - 데이터 부족: {no_data}건")
    print(f"  - Hit Rate: {hit_rate:.1f}% ({correct}/{evaluated})")
    print()
    if hit_rate >= 60:
        print("✅ 권장: shadow_mode: false로 전환 (실제 차단 활성화)")
    elif hit_rate >= 50:
        print("⚠️  권장: 추가 1주일 관찰 후 재평가")
    else:
        print("❌ 권장: 규칙 #11 비활성 유지 (전문가 hit rate 낮음)")


if __name__ == "__main__":
    asyncio.run(main())
