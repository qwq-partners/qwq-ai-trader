#!/usr/bin/env python3
"""
파라미터 민감도 분석 스크립트.
과거 pending_signals 기록과 포지션 기록을 활용해
주요 파라미터 변경 시 선택되는 종목이 어떻게 달라지는지 분석.

사용법:
  python scripts/sensitivity_analysis.py --days 30
  python scripts/sensitivity_analysis.py --param sepa_min_score --range 50,70,5
"""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional

# 분석 대상 파라미터
PARAMS_TO_ANALYZE = {
    "sepa_min_score": {
        "default": 60.0,
        "range": [50, 55, 60, 65, 70],
        "description": "SEPA 전략 최소 진입 점수",
        "filter_key": "score",
        "strategy": "sepa_trend",
    },
    "rsi2_min_score": {
        "default": 60.0,
        "range": [50, 55, 60, 65, 70],
        "description": "RSI2 전략 최소 진입 점수",
        "filter_key": "score",
        "strategy": "rsi2_reversal",
    },
    "first_exit_ratio": {
        "default": 0.20,
        "range": [0.10, 0.15, 0.20, 0.25, 0.30],
        "description": "1차 익절 매도 비율",
        "filter_key": None,
        "strategy": None,
    },
    "trailing_pct": {
        "default": 3.0,
        "range": [2.0, 2.5, 3.0, 3.5, 4.0],
        "description": "트레일링 스탑 %",
        "filter_key": None,
        "strategy": None,
    },
}


def load_pending_signals(cache_dir: Path, days: int) -> List[Dict[str, Any]]:
    """과거 pending_signals 파일에서 시그널 로드"""
    all_signals = []
    cutoff = datetime.now() - timedelta(days=days)

    # 현재 pending_signals.json
    main_file = cache_dir / "pending_signals.json"
    if main_file.exists():
        try:
            data = json.loads(main_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                all_signals.extend(data)
        except Exception:
            pass

    # 날짜별 백업 파일 (pending_signals_YYYY-MM-DD.json 형식)
    for sig_file in sorted(cache_dir.glob("pending_signals_*.json")):
        try:
            # 파일명에서 날짜 추출
            date_str = sig_file.stem.replace("pending_signals_", "")
            file_date = datetime.fromisoformat(date_str)
            if file_date < cutoff:
                continue
            data = json.loads(sig_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                all_signals.extend(data)
        except Exception:
            continue

    return all_signals


def load_trade_journal(cache_dir: Path, days: int) -> List[Dict[str, Any]]:
    """거래 기록 로드"""
    trades = []
    cutoff = datetime.now() - timedelta(days=days)

    for journal_file in ["trade_journal_kr.json", "trade_journal.json"]:
        jpath = cache_dir / journal_file
        if not jpath.exists():
            continue
        try:
            data = json.loads(jpath.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "trades" in data:
                data = data["trades"]
            if isinstance(data, list):
                for t in data:
                    entry_time = t.get("entry_time", "")
                    if entry_time:
                        try:
                            et = datetime.fromisoformat(entry_time)
                            if et >= cutoff:
                                trades.append(t)
                        except Exception:
                            trades.append(t)
                    else:
                        trades.append(t)
        except Exception:
            continue

    return trades


def analyze_score_sensitivity(
    signals: List[Dict[str, Any]],
    trades: List[Dict[str, Any]],
    param_name: str,
    param_cfg: Dict[str, Any],
) -> Dict[float, Dict[str, Any]]:
    """점수 기반 파라미터 민감도 분석"""
    strategy = param_cfg.get("strategy")
    values = param_cfg["range"]
    results = {}

    for value in values:
        passed = []
        for sig in signals:
            sig_strategy = sig.get("strategy", "")
            if strategy and sig_strategy != strategy:
                continue
            sig_score = sig.get("score", 0)
            if sig_score >= value:
                passed.append(sig)

        # 거래 기록에서 해당 전략 승률 계산
        strat_trades = [t for t in trades if t.get("strategy") == strategy] if strategy else trades
        wins = sum(1 for t in strat_trades if float(t.get("pnl_pct", t.get("net_pnl_pct", 0))) > 0)
        total = len(strat_trades)

        results[value] = {
            "signal_count": len(passed),
            "trade_count": total,
            "win_rate": (wins / total * 100) if total > 0 else 0,
            "symbols": [s.get("symbol", "?") for s in passed[:5]],
        }

    return results


def analyze_exit_sensitivity(
    trades: List[Dict[str, Any]],
    param_name: str,
    param_cfg: Dict[str, Any],
) -> Dict[float, Dict[str, Any]]:
    """청산 파라미터 민감도 분석 (과거 거래 기반 시뮬레이션)"""
    values = param_cfg["range"]
    results = {}

    for value in values:
        # 트레일링/익절 파라미터는 과거 거래의 최고 수익률 대비 분석
        trades_with_pnl = [
            t for t in trades
            if t.get("exit_time") and t.get("pnl_pct") is not None
        ]

        total_pnl = 0.0
        affected = 0
        for t in trades_with_pnl:
            pnl = float(t.get("pnl_pct", t.get("net_pnl_pct", 0)))
            high_pnl = float(t.get("highest_pnl_pct", pnl))

            if param_name == "trailing_pct":
                # 최고 수익률에서 trailing_pct만큼 하락 시 청산되었을 가상 손익
                if high_pnl > 5.0:  # 트레일링 활성화 조건
                    simulated_exit = high_pnl - value
                    total_pnl += min(pnl, simulated_exit)
                    affected += 1
                else:
                    total_pnl += pnl
            elif param_name == "first_exit_ratio":
                # 1차 익절 비율 변경 시 영향 (직접 시뮬레이션은 한계가 있으므로 참고용)
                total_pnl += pnl
                if pnl > 5.0:
                    affected += 1
            else:
                total_pnl += pnl

        avg_pnl = total_pnl / len(trades_with_pnl) if trades_with_pnl else 0

        results[value] = {
            "avg_pnl": round(avg_pnl, 2),
            "affected_trades": affected,
            "total_trades": len(trades_with_pnl),
        }

    return results


def parse_range(range_str: str) -> List[float]:
    """'50,70,5' 형식의 범위 문자열을 리스트로 변환"""
    parts = range_str.split(",")
    if len(parts) == 3:
        start, end, step = float(parts[0]), float(parts[1]), float(parts[2])
        values = []
        v = start
        while v <= end + 0.001:
            values.append(round(v, 2))
            v += step
        return values
    else:
        return [float(x.strip()) for x in parts]


def main():
    parser = argparse.ArgumentParser(description="QWQ AI Trader 파라미터 민감도 분석")
    parser.add_argument("--days", type=int, default=30, help="분석 기간 (일)")
    parser.add_argument("--param", type=str, help="특정 파라미터만 분석")
    parser.add_argument("--range", type=str, help="분석 범위 (예: 50,70,5)")
    args = parser.parse_args()

    cache_dir = Path.home() / ".cache" / "ai_trader"

    if not cache_dir.exists():
        print(f"캐시 디렉토리 없음: {cache_dir}")
        sys.exit(1)

    print("=" * 60)
    print("QWQ AI Trader 파라미터 민감도 분석")
    print(f"분석 기간: 최근 {args.days}일")
    print("=" * 60)

    # 데이터 로드
    signals = load_pending_signals(cache_dir, args.days)
    trades = load_trade_journal(cache_dir, args.days)
    print(f"\n로드 완료: 시그널 {len(signals)}개, 거래 {len(trades)}개")

    # 분석 대상 파라미터 결정
    params_to_run = PARAMS_TO_ANALYZE.copy()
    if args.param:
        if args.param not in params_to_run:
            print(f"\n알 수 없는 파라미터: {args.param}")
            print(f"사용 가능: {', '.join(params_to_run.keys())}")
            sys.exit(1)
        params_to_run = {args.param: params_to_run[args.param]}
        if args.range:
            params_to_run[args.param]["range"] = parse_range(args.range)

    # 파라미터별 분석
    for param_name, param_cfg in params_to_run.items():
        print(f"\n{'─' * 60}")
        print(f"📊 {param_name}: {param_cfg.get('description', '')}")
        print(f"   기본값: {param_cfg['default']}")
        print(f"{'─' * 60}")

        if param_cfg.get("filter_key") == "score":
            results = analyze_score_sensitivity(signals, trades, param_name, param_cfg)
            print(f"  {'값':>8}  {'시그널수':>8}  {'거래수':>6}  {'승률':>6}  {'샘플 종목'}")
            print(f"  {'─' * 50}")
            for v, r in sorted(results.items()):
                marker = " ← 현재" if v == param_cfg["default"] else ""
                symbols_str = ", ".join(r["symbols"][:3]) if r["symbols"] else "-"
                print(
                    f"  {v:>8.1f}  {r['signal_count']:>8}개  "
                    f"{r['trade_count']:>6}건  "
                    f"{r['win_rate']:>5.1f}%  "
                    f"{symbols_str}{marker}"
                )
        else:
            results = analyze_exit_sensitivity(trades, param_name, param_cfg)
            print(f"  {'값':>8}  {'평균손익':>8}  {'영향거래':>8}  {'전체거래':>8}")
            print(f"  {'─' * 42}")
            for v, r in sorted(results.items()):
                marker = " ← 현재" if v == param_cfg["default"] else ""
                print(
                    f"  {v:>8.2f}  {r['avg_pnl']:>+7.2f}%  "
                    f"{r['affected_trades']:>8}건  "
                    f"{r['total_trades']:>8}건{marker}"
                )

    print(f"\n{'=' * 60}")
    print("분석 완료!")
    print("=" * 60)


if __name__ == "__main__":
    main()
