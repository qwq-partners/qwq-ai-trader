#!/usr/bin/env python3
"""청산(ladder/channel) × 보유(current/extended) × 사이징(nominal/risk) 2×2×2 A/B.

2026-09-13 종합 리뷰 권고 1~3을 **하나의 백테스트에서 동시** 검증한다 — 따로 검증하면
상쇄돼 결론이 뒤집힌다(리뷰 §5, 2026-08-02 사례). 지표는 포지션(왕복) 단위, walk-forward
3구간 승수는 baseline(ladder/current/nominal) 대비. 결과: docs/research/exit-policy-ab-2026-09.md

실행: venv/bin/python scripts/ab_exit_policy.py --months 6,12 --strategies sepa,rsi2 sepa
KIS API 무접촉 — OHLCV는 pykrx/FDR + ~/.cache/ai_trader/backtest 캐시.
"""

import argparse
import contextlib
import hashlib
import io
import itertools
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import backtest_strategies as bt  # noqa: E402
from src.core.evolution.backtest_gate import BacktestGate, WF_SEGMENTS  # noqa: E402
from src.utils.config import effective_config_hash, load_effective_config  # noqa: E402

AXES = [("ladder", "channel"), ("current", "extended"), ("nominal", "risk")]
BASELINE = "ladder/current/nominal"
MDD_TOLERANCE_PP = 3.0
WF_MIN_WINS = 2


def make_config(months: int, exit_policy: str, holding: str, sizing: str,
                strategies: str, universe: int, args: argparse.Namespace,
                effective: dict) -> "bt.BacktestConfig":
    """유효 설정 builder(T6)로 셀 설정 생성 — 운영 게이트와 같은 기준군을 쓴다.

    A/B 축(exit_policy·holding·sizing)과 러너 옵션(초기 손절·슬롯·offline·종료일)만 덮어쓴다.
    """
    cfg = bt.build_backtest_config_from_effective(
        effective, months=months, strategies=strategies.split(","),
        universe_size=universe, use_cache=True,
        entry_stop_mode=args.entry_stop_mode, slot_policy=args.slot_policy,
        offline=args.offline, end_date=args.end_date,
    )
    cfg.exit_policy = exit_policy
    cfg.sizing = sizing
    bt.apply_holding_policy(cfg, holding)
    if exit_policy == "ladder":
        # 실엔진 미러: TP1이 무장하는 복합 MA5-0.5%/전일저가 청산 + 익절후 저효율 (리뷰 §2-2)
        cfg.enable_composite_exit = True
        cfg.post_exit_stale_days = 5
    return cfg


def _repo_sha() -> str:
    """통합 SHA (실행 시점 HEAD). 저장소 밖에서 실행되면 unknown."""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                             check=True, capture_output=True, text=True)
        return out.stdout.strip()
    except Exception:
        return "unknown"


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def build_manifest(args: argparse.Namespace, effective: dict, *,
                   universe=None, cache_files=()) -> dict:
    """실행 입력 고정 기록 (T7-A).

    통합 SHA·설정 snapshot/hash·계산기 버전·유니버스·OHLCV 캐시 hash·난수 사용 여부·CLI 인자를
    남겨 같은 결과를 다시 만들 수 있게 한다.
    """
    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "integration_sha": _repo_sha(),
        "calculator_version": bt.CALC_VERSION,
        "cli_args": vars(args),
        "offline": bool(args.offline),
        "end_date": args.end_date,
        "entry_stop_mode": args.entry_stop_mode,
        "slot_policy": args.slot_policy,
        "config_source": args.effective_config or "config/default.yml + evolved_overrides.yml",
        "config_hash": effective_config_hash(effective),
        "config_snapshot": effective,
        "universe": {"size": args.universe_size, "tickers": list(universe or [])},
        "ohlcv_cache": {Path(f).name: _file_hash(Path(f)) for f in cache_files},
        # 백테스터는 난수를 쓰지 않는다 (같은 입력 → 같은 결과)
        "random_used": False,
    }


def save_cell_outputs(outdir: Path, cell: str, cfg, engine, analyzer) -> Path:
    """셀 원자료 저장 — 저장 데이터만으로 R·PF·비용·회전·상위3 제외를 재계산할 수 있어야 한다."""
    d = outdir / cell.replace("/", "_")
    d.mkdir(parents=True, exist_ok=True)
    dumps = lambda o: json.dumps(o, ensure_ascii=False, indent=1, default=str)  # noqa: E731
    (d / "positions.json").write_text(dumps(analyzer.positions()), encoding="utf-8")
    (d / "fills.json").write_text(dumps([vars(t) for t in engine.trades]), encoding="utf-8")
    (d / "equity.json").write_text(dumps(engine.equity_curve), encoding="utf-8")
    (d / "config.json").write_text(dumps(vars(cfg)), encoding="utf-8")
    return d


def run_cell(cfg, shared: dict, outdir: Path = None, cell: str = "") -> dict:
    engine = bt.BacktestEngine(cfg)
    # 유니버스·레짐 지표는 셀 간 동일 → 첫 셀만 네트워크, 이후 재사용
    if shared.get("tickers"):
        engine.universe.tickers = shared["tickers"]
        engine.universe.names = shared["names"]
        engine.universe.build_universe = lambda ref_date: None
    regime_key = ("regime", cfg.months)
    if regime_key in shared:
        engine.regime.kospi_data = shared[regime_key]
        engine.regime.load = lambda s, e: None
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m = engine.run(save_results=False)
    shared["tickers"], shared["names"] = engine.universe.tickers, engine.universe.names
    shared[regime_key] = engine.regime.kospi_data
    if not m:
        raise RuntimeError("백테스트 결과 없음:\n" + buf.getvalue()[-2000:])

    an = bt.ResultAnalyzer(cfg, engine.trades, engine.equity_curve)
    if outdir is not None and cell:
        save_cell_outputs(outdir, cell, cfg, engine, an)
    shared.setdefault("cache_files", [])
    shared["cache_files"] = sorted({*shared["cache_files"], *map(str, engine.universe.cache_files),
                                    *map(str, engine.regime.cache_files)})
    ps = an.positions()
    per_strategy = {
        s: an.position_metrics([p for p in ps if p["strategy"] == s])
        for s in sorted({p["strategy"] for p in ps})
    }
    return {
        "start_date": m["start_date"], "end_date": m["end_date"],
        "n_days": len(engine.equity_curve),
        "total_return_pct": m["total_return_pct"], "mdd_pct": m["mdd_pct"],
        "sharpe": m["sharpe"], "total_fees": m["total_fees"],
        "position": m["position_level"], "per_strategy": per_strategy,
        "segments": BacktestGate._segment_returns(engine.equity_curve, WF_SEGMENTS),
        "config": {k: getattr(cfg, k) for k in (
            "exit_policy", "sizing", "min_holding_days", "stale_exit_days",
            "stale_exit_pnl_pct", "stale_high_days", "stale_high_min_pnl_pct",
            "sepa_max_holding_days", "min_stop_pct", "max_stop_pct", "atr_multiplier",
            "first_exit_pct", "first_exit_ratio", "trailing_stop_pct",
            "base_position_pct", "max_position_pct", "risk_per_trade_pct",
            "risk_max_position_pct", "risk_max_positions", "max_positions_short",
            "enable_composite_exit", "post_exit_stale_days")},
    }


def benchmark_return(start: str, end: str) -> dict:
    """KODEX200(069500) 매수보유 수익률 — 리뷰 판정 기준(초과수익). 실패 시 KS11 폴백."""
    import FinanceDataReader as fdr
    for code in ("069500", "KS11"):
        try:
            df = fdr.DataReader(code, start, end)
            if df is not None and len(df) >= 2:
                c = df["Close"].astype(float)
                return {"code": code, "return_pct": (c.iloc[-1] / c.iloc[0] - 1) * 100,
                        "start": str(df.index[0])[:10], "end": str(df.index[-1])[:10]}
        except Exception as e:  # 네트워크 — 벤치마크 없이도 표는 만든다
            print(f"  벤치마크 {code} 실패: {e}")
    return {"code": None, "return_pct": None}


def judge(cell: dict, base: dict) -> dict:
    """baseline 대비 판정: 기대값·PF 우위 AND WF≥2/3 AND MDD 악화 ≤3pp."""
    cp, bp = cell["position"], base["position"]
    wf = (sum(1 for c, b in zip(cell["segments"] or [], base["segments"] or []) if c > b)
          if cell["segments"] and base["segments"] else None)
    checks = {
        "expectancy": cp["expectancy_pct"] > bp["expectancy_pct"],
        "profit_factor": cp["profit_factor"] > bp["profit_factor"],
        "wf": (wf is not None and wf >= WF_MIN_WINS),
        "mdd": cell["mdd_pct"] >= base["mdd_pct"] - MDD_TOLERANCE_PP,
    }
    return {"wf_wins": wf, "checks": checks, "beats_baseline": all(checks.values())}


def md_table(cells: dict) -> str:
    hdr = ("| 셀 | 거래 | 승률 | 평균익 | 평균손 | 손익비 | 기대값% | 기대값R | PF | 수익률 | 초과(KODEX200) | Sharpe | MDD | 최대연패 "
           "| 보유중앙 | 회전(연) | 수수료드래그 | WF승 | 판정 |")
    sep = "|" + "---|" * 19
    rows = [hdr, sep]
    for name, c in cells.items():
        p, j = c["position"], c["judge"]
        wf = "—" if name == BASELINE else (f"{j['wf_wins']}/3" if j["wf_wins"] is not None else "n/a")
        verdict = "기준" if name == BASELINE else ("**통과**" if j["beats_baseline"] else
                  "미달(" + ",".join(k for k, v in j["checks"].items() if not v) + ")")
        ex = c.get("excess_return_pct")
        rows.append(
            f"| {name} | {p['trades']} | {p['win_rate']:.1f}% | {p['avg_win_pct']:+.2f}% | {p['avg_loss_pct']:+.2f}% "
            f"| {p['payoff']:.2f} | {p['expectancy_pct']:+.2f}% | {p['expectancy_r']:+.2f} | {p['profit_factor']:.2f} "
            f"| {c['total_return_pct']:+.2f}% | {'n/a' if ex is None else f'{ex:+.2f}pp'} | {c['sharpe']:.2f} "
            f"| {c['mdd_pct']:.2f}% | {p['max_consec_losses']} | {p['median_holding_days']:.0f}일 "
            f"| {p['turnover_annual']:.1f}x | {p['fee_drag_pct']:.2f}% | {wf} | {verdict} |")
    return "\n".join(rows)


def md_strategy_table(cells: dict) -> str:
    rows = ["| 셀 | 전략 | 거래 | 승률 | 기대값% | 기대값R | PF | 순손익 | 보유중앙 |", "|---|---|---|---|---|---|---|---|---|"]
    for name, c in cells.items():
        for s, p in c["per_strategy"].items():
            rows.append(f"| {name} | {s} | {p['trades']} | {p['win_rate']:.1f}% | {p['expectancy_pct']:+.2f}% "
                        f"| {p['expectancy_r']:+.2f} | {p['profit_factor']:.2f} | {p['net_pnl']:+,.0f} | {p['median_holding_days']:.0f}일 |")
    return "\n".join(rows)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="청산×보유×사이징 2×2×2 A/B")
    ap.add_argument("--months", default="6,12")
    ap.add_argument("--strategies", nargs="+", default=["sepa,rsi2", "sepa"],
                    help="전략 집합(콤마 구분)별로 그리드를 따로 돈다")
    ap.add_argument("--universe-size", type=int, default=60)
    ap.add_argument("--holding", default="current,extended",
                    help="보유 정책 축 (기본 2×2×2; 'none' 추가 시 보유 규칙 해제 보충 셀)")
    ap.add_argument("--out", default=str(bt.RESULTS_DIR / "ab_exit_policy_2026-09.json"),
                    help="요약 JSON 경로 (기존 형식 유지)")
    # ── T7-A 실행 입력 고정 ────────────────────────────────────────────────
    ap.add_argument("--offline", action="store_true",
                    help="캐시에 없는 입력은 다운로드하지 않고 데이터 부족으로 종료 (네트워크 무접촉)")
    ap.add_argument("--end-date", default="2026-09-11",
                    help="마지막 완결 거래일 (기존 연구와 동일, 기본 2026-09-11)")
    ap.add_argument("--output-dir", default=str(bt.RESULTS_DIR / "ab_exit_policy_review_v2"),
                    help="manifest·summary·positions·fills·equity 저장 디렉터리")
    ap.add_argument("--entry-stop-mode", choices=list(bt.ENTRY_STOP_MODES), default="live_policy",
                    help="신규 진입 초기 손절 (기본 live_policy=실엔진 미러)")
    ap.add_argument("--slot-policy", choices=list(bt.SLOT_POLICIES), default="live_weighted",
                    help="동시 보유 슬롯 (기본 live_weighted=실엔진 잔여비율 가중)")
    ap.add_argument("--effective-config", default=None,
                    help="유효 설정 YAML 경로 (미지정 시 config/default.yml + evolved_overrides.yml)")
    return ap


def main():
    args = build_parser().parse_args()
    effective = load_effective_config(args.effective_config)
    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)

    axes = [AXES[0], tuple(args.holding.split(",")), AXES[2]]
    windows = [int(m) for m in args.months.split(",")]
    result = {"run_at": datetime.now().isoformat(timespec="seconds"),
              "universe_size": args.universe_size, "baseline": BASELINE,
              "axes": {"exit_policy": axes[0], "holding_policy": axes[1], "sizing": axes[2]},
              "gate": {"wf_min_wins": WF_MIN_WINS, "mdd_tolerance_pp": MDD_TOLERANCE_PP,
                       "rule": "기대값%·PF > baseline AND WF≥2/3 AND MDD 악화 ≤3pp, 모든 윈도우"},
              "grids": {}}
    shared: dict = {}
    t0 = time.time()
    for strategies in args.strategies:
        grid = {"windows": {}}
        for months in windows:
            cells = {}
            for ex, ho, sz in itertools.product(*axes):
                name = f"{ex}/{ho}/{sz}"
                t1 = time.time()
                cells[name] = run_cell(
                    make_config(months, ex, ho, sz, strategies, args.universe_size,
                                args, effective),
                    shared, outdir=outdir / f"{strategies}_{months}m", cell=name)
                p = cells[name]["position"]
                print(f"[{strategies} {months}m] {name:<28} 거래 {p['trades']:>3} 기대값 {p['expectancy_pct']:+.2f}% "
                      f"PF {p['profit_factor']:.2f} 수익 {cells[name]['total_return_pct']:+.2f}% "
                      f"MDD {cells[name]['mdd_pct']:.1f}% ({time.time() - t1:.0f}s)", flush=True)
            base = cells[BASELINE]
            bench_key = ("bench", base["start_date"], base["end_date"])
            if bench_key not in shared:
                shared[bench_key] = benchmark_return(base["start_date"], base["end_date"])
            bench = shared[bench_key]
            for name, c in cells.items():
                c["judge"] = judge(c, base)
                c["benchmark"] = bench
                c["excess_return_pct"] = (c["total_return_pct"] - bench["return_pct"]
                                          if bench["return_pct"] is not None else None)
            grid["windows"][f"{months}m"] = {
                "period": [base["start_date"], base["end_date"]], "n_days": base["n_days"],
                "benchmark": bench, "cells": cells,
                "table_md": md_table(cells), "strategy_table_md": md_strategy_table(cells),
            }
        # 모든 윈도우 통과 셀 → 기대값R 평균 최대
        names = [f"{ex}/{ho}/{sz}" for ex, ho, sz in itertools.product(*axes)]
        passing = [n for n in names if n != BASELINE
                   and all(w["cells"][n]["judge"]["beats_baseline"] for w in grid["windows"].values())]
        rank = sorted(names, key=lambda n: -sum(w["cells"][n]["position"]["expectancy_r"]
                                                 for w in grid["windows"].values()))
        score = lambda n: sum(w["cells"][n]["position"]["expectancy_r"]  # noqa: E731
                              for w in grid["windows"].values())
        main = [n for n in names if n.split("/")[1] in ("current", "extended")]  # 본 그리드 2×2×2
        main_pass = [n for n in passing if n in main]
        grid["verdict"] = {"passing_cells": passing,
                           "winner": max(passing, key=score) if passing else None,
                           "rank_by_expectancy_r": rank,
                           "main_grid": {"cells": main, "passing_cells": main_pass,
                                         "winner": max(main_pass, key=score) if main_pass else None},
                           "supplement_none": {"passing_cells": [n for n in passing
                                                                 if n.split("/")[1] == "none"]}}
        result["grids"][strategies] = grid
        for wname, w in grid["windows"].items():
            print(f"\n### {strategies} — {wname} ({w['period'][0]} ~ {w['period'][1]}, {w['n_days']}일)\n")
            print(w["table_md"]); print(); print(w["strategy_table_md"])
        print(f"\n[{strategies}] 통과 셀: {passing or '없음'} → winner: {grid['verdict']['winner']}\n")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    # 실행 입력·요약 보존 (T7-A). 기존 결과 파일은 덮어쓰지 않는다 — --out 과 별도 디렉터리.
    manifest = build_manifest(args, effective, universe=shared.get("tickers"),
                              cache_files=shared.get("cache_files", []))
    (outdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    (outdir / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"저장: {out} · {outdir}  (총 {time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
